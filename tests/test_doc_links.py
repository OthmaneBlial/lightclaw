import subprocess
from pathlib import Path

from scripts.check_doc_links import check_repository


def _tracked_repo(root: Path, files: dict[str, str]) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    subprocess.run(["git", "add", "--", *files], cwd=root, check=True)
    return root


def test_valid_relative_external_and_fenced_links(tmp_path):
    root = _tracked_repo(
        tmp_path,
        {
            "README.md": (
                "# Home\n\n[Guide](docs/guide.md#start) [Home](#home)\n"
                "[Website](https://example.com) [Email](mailto:help@example.com)\n\n"
                "```markdown\n[ignored](missing.md#bad)\n```\n"
            ),
            "docs/guide.md": "## Start\n",
        },
    )
    (root / "untracked.md").write_text("[ignored](missing.md)\n", encoding="utf-8")

    assert check_repository(root) == []


def test_encoded_paths_and_duplicate_heading_anchors(tmp_path):
    root = _tracked_repo(
        tmp_path,
        {
            "README.md": (
                "[second duplicate](docs/repeated.md#repeat-1)\n"
                "[encoded path and anchor](docs/space%20guide.md#hello%2Dthere)\n"
            ),
            "docs/repeated.md": "# Repeat\n# Repeat\n",
            "docs/space guide.md": "## Hello there\n",
        },
    )

    assert check_repository(root) == []


def test_broken_file_and_anchor_report_source_line(tmp_path):
    root = _tracked_repo(
        tmp_path,
        {
            "README.md": "# Home\n[missing file](nope.md)\n[missing anchor](present.md#absent)\n",
            "present.md": "# Present\n",
        },
    )

    assert check_repository(root) == [
        "README.md:2: missing target: nope.md",
        "README.md:3: missing anchor #absent in present.md",
    ]


def test_external_links_are_only_syntax_checked(tmp_path):
    root = _tracked_repo(
        tmp_path,
        {
            "README.md": (
                "[HTTP](http://example.com) [HTTPS](https://example.com) "
                "[Email](mailto:help@example.com)\n"
            )
        },
    )

    assert check_repository(root) == []


def test_malformed_external_url_reports_source_line_without_fetching(tmp_path):
    root = _tracked_repo(tmp_path, {"README.md": "[bad](https://[)\n[bad mail](mailto:)\n"})

    errors = check_repository(root)
    assert len(errors) == 2
    assert all(error.startswith("README.md:") and "malformed" in error for error in errors)
