from core.markdown import markdown_to_telegram_html


def test_html_special_characters_and_quotes_are_escaped():
    rendered = markdown_to_telegram_html('<tag a="quoted"> & text')
    assert rendered == "&lt;tag a=&quot;quoted&quot;&gt; &amp; text"


def test_valid_http_links_are_rendered_and_attribute_quotes_are_escaped():
    rendered = markdown_to_telegram_html('[Docs](https://example.com/a?x=1&y=2)')
    assert rendered == '<a href="https://example.com/a?x=1&amp;y=2">Docs</a>'


def test_non_web_and_malformed_links_are_not_emitted_as_anchors():
    rendered = markdown_to_telegram_html(
        '[local](javascript:alert(1)) [broken](not a url) [secure](https://example.com)'
    )
    assert '<a href="javascript:' not in rendered
    assert '<a href="not a url"' not in rendered
    assert "local" in rendered and "broken" in rendered
    assert '<a href="https://example.com">secure</a>' in rendered


def test_code_and_formatting_remain_supported():
    rendered = markdown_to_telegram_html('**bold** `x < y`\n```python\nprint("ok")\n```')
    assert "<b>bold</b>" in rendered
    assert "<code>x &lt; y</code>" in rendered
    assert '<pre><code>print(&quot;ok&quot;)' in rendered
