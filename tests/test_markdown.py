import pytest

from core.markdown import _escape_html, markdown_to_telegram_html


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('<tag a="x"> & \'quoted\'', "&lt;tag a=&quot;x&quot;&gt; &amp; &#x27;quoted&#x27;"),
        ("`x < y & z`", "<code>x &lt; y &amp; z</code>"),
        ("```python\nprint('ok')\n```", "<pre><code>print(&#x27;ok&#x27;)\n</code></pre>"),
        ("## Heading", "Heading"),
        ("> quoted", "quoted"),
        ("- one\n* two", "• one\n• two"),
        ("**bold** __also bold__ _italic_ ~~removed~~", "<b>bold</b> <b>also bold</b> <i>italic</i> <s>removed</s>"),
        ("**unfinished <tag", "**unfinished &lt;tag"),
        ("", ""),
    ],
)
def test_markdown_formats_and_escapes(source, expected):
    assert markdown_to_telegram_html(source) == expected


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        ("http://example.com", '<a href="http://example.com">Docs</a>'),
        ("https://example.com/path?a=1&b=2", '<a href="https://example.com/path?a=1&amp;b=2">Docs</a>'),
        (
            'https://example.com/?q="onclick="x',
            '<a href="https://example.com/?q=&quot;onclick=&quot;x">Docs</a>',
        ),
        (
            "javascript:alert(1)",
            "[Docs](javascript:alert(1))",
        ),
        ("data:text/html,hello", "[Docs](data:text/html,hello)"),
        ("not a URL", "[Docs](not a URL)"),
        ("https://", "[Docs](https://)"),
        ("https://[::1", "[Docs](https://[::1)"),
        ("https://example.com:99999", "[Docs](https://example.com:99999)"),
        ("https://user@example.com", "[Docs](https://user@example.com)"),
    ],
)
def test_links_require_safe_absolute_web_urls(destination, expected):
    assert markdown_to_telegram_html(f"[Docs]({destination})") == expected


def test_link_label_keeps_supported_formatting():
    assert markdown_to_telegram_html("[**Docs**](https://example.com)") == (
        '<a href="https://example.com"><b>Docs</b></a>'
    )


def test_source_text_cannot_collide_with_internal_placeholders():
    source = "\x00LCLAWLK0\x00 [Docs](https://example.com)"
    assert markdown_to_telegram_html(source) == (
        "\x00LCLAWLK0\x00 <a href=\"https://example.com\">Docs</a>"
    )


def test_html_escape_includes_quotes():
    assert _escape_html('"quoted"') == "&quot;quoted&quot;"
