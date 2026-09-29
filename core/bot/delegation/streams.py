"""Bounded capture for delegated CLI output streams."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator

# ponytail: fixed stream caps bound CLI memory; raise them only for verified output needs.
MAX_STREAM_CAPTURE_CHARS = 2 * 1024 * 1024
MAX_STREAM_CAPTURE_LINES = 4096
MAX_STREAM_LINE_BYTES = 1024 * 1024


class BoundedStreamCapture:
    def __init__(self, name: str) -> None:
        self.name = name
        self.lines: deque[str] = deque()
        self.characters = 0
        self.truncated = False

    def text(self) -> str:
        return "\n".join(self.lines)

    def append_line(self, line: str) -> None:
        self._store(line)

    def _store(self, line: str) -> None:
        self.lines.append(line)
        self.characters += len(line) + 1
        while (
            len(self.lines) > MAX_STREAM_CAPTURE_LINES
            or self.characters > MAX_STREAM_CAPTURE_CHARS
        ):
            self.characters -= len(self.lines.popleft()) + 1
            self.truncated = True

    async def read_lines(self, stream: asyncio.StreamReader) -> AsyncIterator[str]:
        pending = bytearray()
        discarding_line = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                newline_idx = chunk.find(b"\n", offset)
                line_end = len(chunk) if newline_idx < 0 else newline_idx
                if not discarding_line:
                    fragment = chunk[offset:line_end]
                    if len(pending) + len(fragment) > MAX_STREAM_LINE_BYTES:
                        pending.clear()
                        discarding_line = True
                        self.truncated = True
                    else:
                        pending.extend(fragment)
                if newline_idx < 0:
                    break
                if discarding_line:
                    line = f"[LightClaw omitted overlong {self.name} line]"
                else:
                    raw_line = bytes(pending)
                    if raw_line.endswith(b"\r"):
                        raw_line = raw_line[:-1]
                    line = raw_line.decode("utf-8", errors="replace")
                pending.clear()
                discarding_line = False
                self._store(line)
                yield line
                offset = newline_idx + 1

        if discarding_line:
            line = f"[LightClaw omitted overlong {self.name} line]"
        elif pending:
            if pending.endswith(b"\r"):
                pending.pop()
            line = pending.decode("utf-8", errors="replace")
        else:
            return
        self._store(line)
        yield line
