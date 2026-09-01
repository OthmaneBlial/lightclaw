from __future__ import annotations

import json
import re
import struct
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


class _SiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes: list[tuple[str, str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.attributes.append((tag, tag, {key: value or "" for key, value in attrs}))


def _document() -> tuple[str, _SiteParser]:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    parser = _SiteParser()
    parser.feed(html)
    return html, parser


def test_site_local_assets_are_relative_and_present() -> None:
    _, parser = _document()
    local_references: list[str] = []

    for _, _, attrs in parser.attributes:
        for name in ("href", "src"):
            value = attrs.get(name, "")
            parsed = urlsplit(value)
            if not value or value.startswith("#") or parsed.scheme or parsed.netloc:
                continue
            assert not value.startswith("/"), value
            local_references.append(parsed.path)

    assert local_references
    assert all((SITE / reference).is_file() for reference in local_references)


def test_site_keeps_mobile_and_accessibility_contracts() -> None:
    html, parser = _document()
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    javascript = (SITE / "app.js").read_text(encoding="utf-8")
    elements = [attrs for _, _, attrs in parser.attributes]

    assert 'name="viewport"' in html
    assert 'class="skip-link"' in html
    assert 'aria-label="Primary navigation"' in html
    assert 'role="tablist"' in html
    assert html.count('role="tab"') == 3
    assert html.count("data-copy-target=") == 2
    assert html.count("data-receipt-") == 7
    assert any(attrs.get("aria-controls") == "site-nav" for attrs in elements)
    assert "@media (max-width: 760px)" in css
    assert "@media (max-width: 430px)" in css
    assert "overflow-x: clip" in css
    assert "min-height: 46px" in css
    assert 'event.key === "Escape"' in javascript
    assert '"ArrowLeft"' in javascript and '"ArrowRight"' in javascript
    assert not (SITE / "mobile-qa.html").exists()


def test_site_has_complete_share_and_search_metadata() -> None:
    html, _ = _document()

    assert '<link rel="canonical" href="https://othmaneblial.github.io/lightclaw/"' in html
    assert 'property="og:image"' in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert 'content="https://othmaneblial.github.io/lightclaw/assets/social-preview.png"' in html

    match = re.search(
        r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    schema = json.loads(match.group(1))
    graph = schema["@graph"]
    assert {item["@type"] for item in graph} == {
        "SoftwareApplication",
        "SoftwareSourceCode",
    }
    assert not any("aggregateRating" in item for item in graph)

    social = (SITE / "assets" / "social-preview.png").read_bytes()
    assert social.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", social[16:24]) == (1280, 640)


def test_site_discovery_files_are_canonical_and_bounded() -> None:
    sitemap = ET.parse(SITE / "sitemap.xml").getroot()
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = [node.text for node in sitemap.findall("sm:url/sm:loc", namespace)]
    assert locations == ["https://othmaneblial.github.io/lightclaw/"]

    llms = (SITE / "llms.txt").read_text(encoding="utf-8")
    assert "alpha" in llms.lower()
    assert "hosted model providers remain external" in llms
    assert "never pushes or publishes a demo result" in llms
