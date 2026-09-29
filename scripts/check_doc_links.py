#!/usr/bin/env python3
"""Check tracked Markdown links and anchors without making network requests."""

from __future__ import annotations

import re
import subprocess
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CLOSE_FENCE_RE = re.compile(r"^ {0,3}(`+|~+)\s*$")
INLINE_CODE_RE = re.compile(r"`+[^`\n]*`+")
# Keep escaped characters and ordinary URL characters disjoint to avoid ReDoS.
LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>\n]+>|(?:\\.|[^)\\\s])+)(?:\s+[^)]*)?\)")
REFERENCE_RE = re.compile(r"^\s*\[[^\]]+\]:\s*(<[^>\n]+>|\S+)")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if value and (name == "id" or (tag == "a" and name == "name")):
                self.anchors.add(value)


def _visible_lines(markdown: str) -> list[tuple[int, str]]:
    visible = []
    fence_char = ""
    fence_length = 0
    for line_number, line in enumerate(markdown.splitlines(), 1):
        if fence_char:
            closing = CLOSE_FENCE_RE.match(line)
            if closing and closing.group(1)[0] == fence_char and len(closing.group(1)) >= fence_length:
                fence_char = ""
            continue
        opening = FENCE_RE.match(line)
        if opening:
            fence_char = opening.group(1)[0]
            fence_length = len(opening.group(1))
            continue
        visible.append((line_number, line))
    return visible


def _heading_slug(heading: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text.replace("`", "")).casefold()
    text = re.sub(r"[^\w -]", "", text)
    return re.sub(r"\s+", "-", text.strip())


def _anchors(markdown: str) -> set[str]:
    visible = _visible_lines(markdown)
    parser = _AnchorParser()
    parser.feed("\n".join(INLINE_CODE_RE.sub("", line) for _, line in visible))
    anchors = parser.anchors
    duplicates: dict[str, int] = {}
    for _, line in visible:
        match = HEADING_RE.match(line)
        if not match:
            continue
        base = _heading_slug(match.group(2))
        if not base:
            continue
        suffix = duplicates.get(base, 0)
        slug = base if suffix == 0 else f"{base}-{suffix}"
        while slug in anchors:
            suffix += 1
            slug = f"{base}-{suffix}"
        anchors.add(slug)
        duplicates[base] = suffix + 1
    return anchors


def _tracked_markdown(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / Path(os_path) for os_path in _decode_paths(result.stdout)]


def _decode_paths(output: bytes) -> list[str]:
    import os

    return [os.fsdecode(path) for path in output.split(b"\0") if path]


def _destination(match: re.Match[str]) -> str:
    value = match.group(1)
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    return value.replace(r"\ ", " ")


def _resolve_markdown_target(root: Path, source: Path, target: str) -> tuple[Path | None, str | None]:
    parsed = urlsplit(target)
    path = unquote(parsed.path)
    candidate = (source.parent / path).resolve() if path else source.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, f"target escapes repository: {target}"

    if candidate.is_dir():
        for name in ("README.md", "index.md"):
            markdown = candidate / name
            if markdown.is_file():
                return markdown, None
        return (None if parsed.fragment else candidate), None
    if candidate.is_file():
        return candidate, None
    if not candidate.suffix:
        for alternative in (candidate.with_suffix(".md"), candidate / "README.md"):
            if alternative.is_file():
                return alternative, None
    return None, f"missing target: {target}"


def check_repository(root: Path) -> list[str]:
    root = root.resolve()
    files = _tracked_markdown(root)
    anchor_cache: dict[Path, set[str]] = {}
    errors: list[str] = []

    for source in files:
        try:
            visible = _visible_lines(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            errors.append(f"{source.relative_to(root)}: cannot read Markdown: {exc}")
            continue

        for line_number, raw_line in visible:
            line = INLINE_CODE_RE.sub("", raw_line)
            links = [_destination(match) for match in LINK_RE.finditer(line)]
            reference = REFERENCE_RE.match(line)
            if reference:
                value = reference.group(1)
                links.append(value[1:-1] if value.startswith("<") and value.endswith(">") else value)

            for target in links:
                location = f"{source.relative_to(root)}:{line_number}"
                try:
                    parsed = urlsplit(target)
                except ValueError as exc:
                    errors.append(f"{location}: malformed URL {target!r}: {exc}")
                    continue
                if parsed.scheme or parsed.netloc:
                    if parsed.scheme in {"http", "https"} and (not parsed.hostname or not parsed.netloc):
                        errors.append(f"{location}: malformed external URL {target!r}")
                    elif parsed.scheme == "mailto" and ("@" not in parsed.path or not parsed.path):
                        errors.append(f"{location}: malformed external URL {target!r}")
                    else:
                        try:
                            _ = parsed.port
                        except ValueError as exc:
                            errors.append(f"{location}: malformed external URL {target!r}: {exc}")
                    continue

                target_path, error = _resolve_markdown_target(root, source, target)
                if error:
                    errors.append(f"{location}: {error}")
                    continue
                if target_path is None:
                    errors.append(f"{location}: missing Markdown page for anchor in {target!r}")
                    continue
                fragment = unquote(parsed.fragment)
                if fragment and target_path.suffix.lower() == ".md":
                    anchors = anchor_cache.get(target_path)
                    if anchors is None:
                        try:
                            anchors = _anchors(target_path.read_text(encoding="utf-8"))
                        except (OSError, UnicodeError) as exc:
                            errors.append(f"{location}: cannot read anchor target {target!r}: {exc}")
                            continue
                        anchor_cache[target_path] = anchors
                    if fragment not in anchors:
                        errors.append(f"{location}: missing anchor #{fragment} in {target_path.relative_to(root)}")

    return sorted(errors)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        errors = check_repository(root)
    except subprocess.CalledProcessError as exc:
        print(f"Could not list tracked Markdown files: {exc}")
        return 1
    if errors:
        print("Documentation link check failed:")
        print("\n".join(errors))
        return 1
    print("Documentation links and anchors passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
