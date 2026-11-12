#!/usr/bin/env python3
"""Check the static site as published by GitHub project Pages.

The site is served at https://jvycee.github.io/showcase/ (trailing slash).
Relative URLs stay under that prefix. Root-relative and protocol-relative
URLs do not, so this check rejects them. External http(s) URLs are allowed
only on navigations (a, area) and on the one Google tag in <head>. Stylesheets,
fonts, and images stay local.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

ROOT = Path(__file__).resolve().parents[2]
SITE_HOST = "jvycee.github.io"
SITE_PREFIX = "/showcase/"
ALLOWED_EMAIL = "hello@cybercatlabs.com"
# GSC's Google Analytics verification looks for this exact tag in <head>.
# It is the only external script the site is allowed to load.
GTAG_SRC = "https://www.googletagmanager.com/gtag/js?id=G-8Y03WYLQF8"
GTAG_CONFIG = "gtag('config', 'G-8Y03WYLQF8')"
EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
CSS_URL_RE = re.compile(
    r"url\(\s*(?:'(?P<s>[^']*)'|\"(?P<d>[^\"]*)\"|(?P<u>[^)]*))\s*\)",
    re.IGNORECASE,
)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*(?:'(?P<s>[^']*)'|\"(?P<d>[^\"]*)\"|(?P<u>[^)]*))\s*\)|'(?P<sq>[^']*)'|\"(?P<dq>[^\"]*)\")",
    re.IGNORECASE,
)
JS_SCHEME_RE = re.compile(r"^\s*javascript\s*:", re.IGNORECASE)
LEADING_SLASH_RE = re.compile(
    r"""(?:(?:href|src|poster)\s*=\s*["']\s*/|url\(\s*["']?\s*/)""",
    re.IGNORECASE,
)

NAV_TAGS = {"a", "area"}
ASSET_TAGS = {
    "link",
    "script",
    "img",
    "source",
    "video",
    "audio",
    "iframe",
    "embed",
    "object",
    "track",
    "image",
    "use",
}


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str, str, str]] = []
        self.style_chunks: list[str] = []
        self.script_bodies: list[str] = []
        self.inline_scripts = 0
        self.base_hrefs: list[str] = []
        self._in_style = False
        self._in_script = False
        self._script_has_src = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._take(tag, attrs)
        if tag == "style":
            self._in_style = True
        if tag == "script":
            self._in_script = True
            self._script_has_src = any(k.lower() == "src" for k, _ in attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._take(tag, attrs)
        if tag == "script" and not any(k.lower() == "src" for k, _ in attrs):
            self.inline_scripts += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False
        if tag == "script":
            if self._in_script and not self._script_has_src:
                self.inline_scripts += 1
            self._in_script = False
            self._script_has_src = False

    def handle_data(self, data: str) -> None:
        if self._in_style and data.strip():
            self.style_chunks.append(data)
        if self._in_script and not self._script_has_src:
            self.script_bodies.append(data)

    def _take(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrmap = {k.lower(): v for k, v in attrs if v is not None}
        if tag == "base" and "href" in attrmap:
            self.base_hrefs.append(attrmap["href"])
        if "style" in attrmap:
            self.style_chunks.append(attrmap["style"])
        if tag not in ASSET_TAGS and tag not in NAV_TAGS:
            return
        names = [name for name in ("href", "src", "srcset", "poster", "xlink:href") if name in attrmap]
        if tag == "object" and "data" in attrmap:
            names.append("data")
        rel = set(attrmap.get("rel", "").lower().split())
        as_type = attrmap.get("as", "").lower()
        for name in names:
            values = split_srcset(attrmap[name]) if name == "srcset" else [attrmap[name]]
            for value in values:
                kind = "nav" if tag in NAV_TAGS else "asset"
                if tag == "script" or "modulepreload" in rel or ("preload" in rel and as_type == "script"):
                    kind = "script"
                self.refs.append((kind, tag, name, value))


def split_srcset(value: str) -> list[str]:
    urls = []
    for part in value.split(","):
        part = part.strip()
        if part:
            urls.append(part.split()[0])
    return urls


def css_urls(text: str) -> list[str]:
    found: list[str] = []
    for pattern in (CSS_URL_RE, CSS_IMPORT_RE):
        for match in pattern.finditer(text):
            found.append(next(group for group in match.groups() if group is not None))
    return found


def site_url_for(path: Path) -> str:
    rel = path.relative_to(ROOT).as_posix()
    return f"https://{SITE_HOST}{SITE_PREFIX}{rel}"


def iter_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.relative_to(ROOT).parts:
            continue
        files.append(path)
    return files


def check_url(raw: str, base_url: str, kind: str, problems: list[str], notes: list[str]) -> None:
    raw = raw.strip()
    where = f"{kind} {raw!r} (from {base_url})"
    if raw == "" or raw.startswith("#"):
        notes.append(f"fragment {raw!r} from {base_url}")
        return
    lowered = raw.lower()
    if lowered.startswith("mailto:") or lowered.startswith("tel:"):
        if kind != "nav":
            problems.append(f"disallowed mailto/tel on {where}")
        else:
            notes.append(f"navigation {raw}")
        return
    if JS_SCHEME_RE.match(raw) or lowered.startswith(("data:", "blob:", "vbscript:")):
        problems.append(f"disallowed scheme on {where}")
        return
    if raw.startswith("//"):
        problems.append(f"protocol-relative URL escapes /showcase/ on {where}")
        return
    absolute = urljoin(base_url, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("https", "http"):
        problems.append(f"unsupported URL {absolute} on {where}")
        return
    host = parsed.netloc.lower()
    if host != SITE_HOST:
        if kind == "nav":
            notes.append(f"external navigation {absolute}")
            return
        if kind == "script" and absolute == GTAG_SRC:
            notes.append(f"allowed google tag {absolute}")
            return
        label = "external script" if kind == "script" else "external asset"
        problems.append(f"{label} {absolute} on {where}")
        return
    path = unquote(parsed.path)
    if not path.startswith(SITE_PREFIX):
        problems.append(f"URL escapes /showcase/ : {absolute} on {where}")
        return
    rel = path[len(SITE_PREFIX):]
    if rel == "" or rel.endswith("/"):
        rel += "index.html"
    if "\\" in rel or rel.startswith("/"):
        problems.append(f"URL escapes /showcase/ : {absolute} on {where}")
        return
    candidate = (ROOT / rel).resolve()
    if candidate != ROOT and ROOT not in candidate.parents:
        problems.append(f"path escapes the repository: {rel} on {where}")
        return
    if not candidate.is_file():
        problems.append(f"missing local asset {rel} on {where}")
        return
    notes.append(f"local {rel} <- {raw} ({base_url})")


def check_emails(problems: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in iter_files():
        data = path.read_bytes()
        for match in EMAIL_RE.findall(data):
            address = match.decode("ascii", "replace")
            counts[address] = counts.get(address, 0) + 1
    others = sorted(addr for addr in counts if addr != ALLOWED_EMAIL)
    if others:
        problems.append(
            "unexpected email address(es): " + ", ".join(f"{addr} x{counts[addr]}" for addr in others)
        )
    if counts.get(ALLOWED_EMAIL, 0) < 1:
        problems.append(f"missing required email {ALLOWED_EMAIL}")
    return counts


def head_inner(text: str) -> str:
    match = re.search(r"<head\b[^>]*>(.*)</head>", text, re.IGNORECASE | re.DOTALL)
    return match.group(1) if match else ""


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []
    html_files = [path for path in iter_files() if path.suffix.lower() in {".html", ".htm"}]
    if not html_files:
        problems.append("no HTML files in the repository")

    seen_css: set[Path] = set()
    inline_scripts = 0

    def parse_css(path: Path, text: str) -> None:
        if path in seen_css and path.is_file():
            return
        seen_css.add(path)
        base_url = site_url_for(path) if path.is_relative_to(ROOT) else site_url_for(ROOT / "styles.css")
        for url in css_urls(text):
            check_url(url, base_url, "asset", problems, notes)

    for path in html_files:
        text = path.read_text(encoding="utf-8")
        if LEADING_SLASH_RE.search(text):
            problems.append(f"leading-slash URL in {path.relative_to(ROOT)}")
        parser = Collector()
        parser.feed(text)
        parser.close()
        inline_scripts += parser.inline_scripts
        if parser.base_hrefs:
            problems.append(
                f"<base> would retarget relative URLs in {path.relative_to(ROOT)}: {parser.base_hrefs}"
            )
        base_url = site_url_for(path)
        for kind, _tag, _attr, value in parser.refs:
            check_url(value, base_url, kind, problems, notes)
        for chunk in parser.style_chunks:
            for url in css_urls(chunk):
                check_url(url, base_url, "asset", problems, notes)
        for body in parser.script_bodies:
            if re.search(r"https?:|//", body):
                problems.append(f"inline script references an external URL in {path.relative_to(ROOT)}")
        if JS_SCHEME_RE.search(text):
            problems.append(f"javascript: URL in {path.relative_to(ROOT)}")
        head = head_inner(text)
        rel = path.relative_to(ROOT)
        if GTAG_SRC not in head:
            problems.append(f"google tag script missing from <head> in {rel}")
        if GTAG_CONFIG not in head:
            problems.append(f"google tag config missing from <head> in {rel}")

    for path in iter_files():
        if path.suffix.lower() != ".css":
            continue
        text = path.read_text(encoding="utf-8")
        if LEADING_SLASH_RE.search(text):
            problems.append(f"leading-slash URL in {path.relative_to(ROOT)}")
        parse_css(path, text)

    for path in iter_files():
        if path.suffix.lower() != ".svg":
            continue
        text = path.read_text(encoding="utf-8")
        if LEADING_SLASH_RE.search(text):
            problems.append(f"leading-slash URL in {path.relative_to(ROOT)}")
        if JS_SCHEME_RE.search(text) or re.search(r"<\s*script\b", text, re.IGNORECASE):
            problems.append(f"script in SVG {path.relative_to(ROOT)}")
        parser = Collector()
        parser.feed(text)
        parser.close()
        base_url = site_url_for(path)
        for kind, _tag, _attr, value in parser.refs:
            check_url(value, base_url, "script" if kind == "script" else "asset", problems, notes)

    emails = check_emails(problems)

    print("resolved references:")
    for note in notes:
        print(f"  {note}")
    print(f"inline scripts: {inline_scripts}")
    print("emails:")
    for address, count in sorted(emails.items()):
        print(f"  {address} x{count}")
    if problems:
        print("FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("OK: local assets resolve under /showcase/, sole email is hello@cybercatlabs.com, only external script is the Google tag in <head>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
