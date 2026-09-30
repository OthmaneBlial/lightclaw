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
    assert all(item.get("runtimePlatform") == "Python 3.10-3.14" for item in graph)

    social = (SITE / "assets" / "social-preview.png").read_bytes()
    assert social.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", social[16:24]) == (1280, 640)


def test_readme_and_site_share_the_playful_brand_assets() -> None:
    html, _ = _document()
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert '<meta name="theme-color" content="#fff2bd"' in html
    assert "--paper: #fff2bd" in css
    assert "--coral: #ff694f" in css
    assert "class=\"top-ribbon\"" in html
    assert "✨" in html and "🧪" in readme
    assert "assets/social-preview.png" in readme
    assert (ROOT / "assets" / "social-preview.png").read_bytes() == (
        SITE / "assets" / "social-preview.png"
    ).read_bytes()


def test_site_explains_allowlisted_group_chat_boundary() -> None:
    html, _ = _document()
    assert "Allowlisted bots accept commands only in private chats." in html
    assert "require explicit public mode and no user allowlist" in html


def test_site_discloses_credential_context_guards() -> None:
    html, _ = _document()
    assert "Automatic edit retries omit common credential paths" in html
    assert "generated edits cannot write common credential paths" in html


def test_site_discloses_automatic_task_git_controls() -> None:
    html, _ = _document()
    llms = (SITE / "llms.txt").read_text(encoding="utf-8")

    for content in (html, llms):
        assert "disable configured hooks and filesystem monitors" in content
        assert "staging and checkouts bypass configured filters" in content
        assert "commits skip signing" in content
        assert "diffs ignore external helpers" in content


def test_site_discloses_private_sqlite_file_permissions() -> None:
    html, _ = _document()
    llms = (SITE / "llms.txt").read_text(encoding="utf-8")

    assert "Memory and job SQLite databases" in html
    assert "existing WAL/SHM sidecars" in html
    assert "owner-only permissions before use" in llms


def test_site_discloses_per_user_message_rate_limits() -> None:
    html, _ = _document()
    llms = (SITE / "llms.txt").read_text(encoding="utf-8")

    assert "20 text messages" in html and "six voice transcriptions" in html
    assert "20 per Telegram user per minute before model processing" in llms
    assert "six requests per user per minute" in llms


def test_readme_and_site_disclose_persistent_memory_retention_and_controls() -> None:
    html, _ = _document()
    llms = (SITE / "llms.txt").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "session summaries" in html and "90 days by default" in html
    assert "MEMORY_RETENTION_DAYS" in html
    assert "<code>/clear</code>" in html and "<code>/wipe_memory</code>" in html
    assert "initiating Telegram user" in html and "90 seconds" in html
    assert "90 days by default" in llms and "MEMORY_RETENTION_DAYS" in llms
    assert "/clear" in llms and "/wipe_memory" in llms
    assert "initiating Telegram user" in llms and "90 seconds" in llms
    assert "persisted summaries" in readme and "90-day default retention" in readme


def test_site_discovery_files_are_canonical_and_bounded() -> None:
    sitemap = ET.parse(SITE / "sitemap.xml").getroot()
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = [node.text for node in sitemap.findall("sm:url/sm:loc", namespace)]
    assert locations == ["https://othmaneblial.github.io/lightclaw/"]

    llms = (SITE / "llms.txt").read_text(encoding="utf-8")
    assert "alpha" in llms.lower()
    assert "hosted model providers remain external" in llms
    assert "never pushes or publishes a demo result" in llms
