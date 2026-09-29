import pytest

from core.bot.file_ops import BotFileOpsMixin


def _hunk(search, replace):
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE"


@pytest.mark.parametrize(
    ("content", "edit_body", "expected"),
    [
        ("before old after", _hunk("old", "new"), ("before new after", None)),
        (
            "old",
            _hunk("old", "new") + "\n" + _hunk("new", "final"),
            ("final", None),
        ),
        (
            "keep original",
            _hunk("absent", "new"),
            ("keep original", "hunk 1: SEARCH text not found (must match exactly)"),
        ),
        (
            "same same",
            _hunk("same", "new"),
            ("same same", "hunk 1: SEARCH text appears 2 times; add more context"),
        ),
        ("original", _hunk("", "insert"), ("original", "hunk 1: SEARCH block is empty")),
        (
            "original",
            _hunk("original", "changed") + "\n" + _hunk("absent", "later"),
            ("original", "hunk 2: SEARCH text not found (must match exactly)"),
        ),
    ],
)
def test_search_replace_hunk_contracts(content, edit_body, expected):
    assert BotFileOpsMixin._apply_search_replace_hunks(content, edit_body) == expected


def test_diff_stats_ignore_file_headers():
    diff = "--- a/file.py\n+++ b/file.py\n@@ -1 +1,2 @@\n-old\n+new\n+added"
    assert BotFileOpsMixin._diff_line_stats(diff) == (2, 1)


def test_response_compaction_keeps_plain_text_and_removes_markers_and_code():
    response = "[File updated: app.py]\nImplemented the fix.\n\n```python\nprint('hidden')\n```\n\nNext step."
    assert BotFileOpsMixin._compact_response_for_file_ops(response) == (
        "Implemented the fix.\n\nNext step."
    )
    assert BotFileOpsMixin._compact_response_for_file_ops("[No changes: app.py]") == "Done."


def test_chat_fence_stripping_keeps_surrounding_text():
    response = "Before\n```python\nprint('hidden')\n```\nAfter"
    assert BotFileOpsMixin._strip_fenced_code_for_chat(response) == "Before\n\nAfter"
    assert BotFileOpsMixin._strip_fenced_code_for_chat("plain reply") == "plain reply"


@pytest.mark.parametrize(
    ("text", "incomplete"),
    [
        ("<!doctype html><html><body>ok</body></html>", False),
        ("<html><body>missing html close</body>", True),
        ("<html>missing body and html closes", True),
        ("ordinary text with <b>inline HTML</b>", False),
    ],
)
def test_incomplete_html_detection(text, incomplete):
    assert BotFileOpsMixin._is_incomplete_html_text(text) is incomplete
