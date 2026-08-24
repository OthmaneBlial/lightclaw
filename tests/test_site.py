from __future__ import annotations

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
    assert any(attrs.get("aria-controls") == "site-nav" for attrs in elements)
    assert "@media (max-width: 760px)" in css
    assert "@media (max-width: 430px)" in css
    assert "overflow-x: clip" in css
    assert "min-height: 46px" in css
    assert 'event.key === "Escape"' in javascript
    assert '"ArrowLeft"' in javascript and '"ArrowRight"' in javascript
    assert not (SITE / "mobile-qa.html").exists()
