#!/usr/bin/env python3
"""Generate the public provider matrix from registry and recorded fixtures."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.llm.matrix import build_matrix, json_text, render_markdown  # noqa: E402

FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "providers"
DEFAULT_MARKDOWN = PROJECT_ROOT / "docs" / "generated" / "provider-compatibility.md"
DEFAULT_JSON = PROJECT_ROOT / "docs" / "generated" / "provider-compatibility.json"


def _check(path: Path, expected: str) -> bool:
    return path.is_file() and path.read_text(encoding="utf-8") == expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if committed outputs drift")
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    args = parser.parse_args()

    matrix = build_matrix(FIXTURE_ROOT)
    markdown = render_markdown(matrix)
    raw_json = json_text(matrix)
    if args.check:
        stale = []
        if not _check(args.markdown, markdown):
            stale.append(args.markdown)
        if not _check(args.json, raw_json):
            stale.append(args.json)
        if stale:
            print("Provider matrix is stale: " + ", ".join(str(path) for path in stale))
            return 1
        print(f"Provider matrix is current for {len(matrix['providers'])} adapters.")
        return 0

    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(markdown, encoding="utf-8")
    args.json.write_text(raw_json, encoding="utf-8")
    print(f"Generated provider matrix for {len(matrix['providers'])} adapters.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
