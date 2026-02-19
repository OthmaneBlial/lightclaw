"""Workspace path resolution and file block processing/edit pipelines."""

from __future__ import annotations

import asyncio
import difflib
import os
import re
import time
from pathlib import Path

from ..logging_setup import log
from ..types import FileOperationResult


class BotFileOpsMixin:
    def _resolve_workspace_path(self, raw_path: str) -> tuple[Path | None, str | None, str | None]:
        """Resolve a user-provided path inside workspace, blocking traversal."""
        path_text = raw_path.strip().strip("`").strip()
        if not path_text:
            return None, None, "empty path"
        if os.path.isabs(path_text):
            return None, None, "absolute paths are not allowed"

        workspace = Path(self.config.workspace_path).resolve()
        lexical = workspace / path_text
        # Explicitly reject existing symlink segments to prevent workspace escape via link hops.
        probe = workspace
        for part in Path(path_text).parts:
            if part in ("", "."):
                continue
            if part == "..":
                return None, None, "parent traversal is not allowed"
            probe = probe / part
            if not probe.exists():
                break
            if probe.is_symlink():
                return None, None, f"path segment is a symlink: {part}"

        candidate = lexical.resolve()
        try:
            rel = candidate.relative_to(workspace)
        except ValueError:
            return None, None, "path is outside workspace/"

        if str(rel) == ".":
            return None, None, "path points to workspace root"
        if candidate.exists() and candidate.is_symlink():
            return None, None, "target path is a symlink"

        return candidate, rel.as_posix(), None

    def _workspace_display_path(self) -> str:
        """Human-friendly workspace path for status messages."""
        workspace = Path(self.config.workspace_path).resolve()
        runtime_home = os.getenv("LIGHTCLAW_HOME", "").strip()
        if runtime_home:
            try:
                rel = workspace.relative_to(Path(runtime_home).expanduser().resolve())
                return rel.as_posix()
            except ValueError:
                pass
        return workspace.as_posix()

    @staticmethod
    def _build_unified_diff(before: str, after: str, rel_path: str) -> str:
        """Build a unified diff between old and new content."""
        diff_lines = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm="\n",
        )
        return "".join(diff_lines).strip()

    @staticmethod
    def _apply_search_replace_hunks(content: str, edit_body: str) -> tuple[str, str | None]:
        """Apply SEARCH/REPLACE hunks with exact-match + unique-match semantics."""
        hunk_pattern = re.compile(
            r"<<<<<<<\s*SEARCH\r?\n([\s\S]*?)\r?\n=======\r?\n([\s\S]*?)\r?\n>>>>>>>\s*REPLACE",
            re.MULTILINE,
        )
        matches = list(hunk_pattern.finditer(edit_body))
        if not matches:
            return content, "no SEARCH/REPLACE hunks found"

        updated = content
        for idx, match in enumerate(matches, 1):
            old_text = match.group(1)
            new_text = match.group(2)

            if old_text == "":
                return content, f"hunk {idx}: SEARCH block is empty"

            occurrences = updated.count(old_text)
            if occurrences == 0:
                return content, f"hunk {idx}: SEARCH text not found (must match exactly)"
            if occurrences > 1:
                return content, f"hunk {idx}: SEARCH text appears {occurrences} times; add more context"

            updated = updated.replace(old_text, new_text, 1)

        return updated, None

    @staticmethod
    def _diff_line_stats(diff_text: str) -> tuple[int, int]:
        """Return added/deleted line counts from unified diff text."""
        added = 0
        deleted = 0
        for line in diff_text.splitlines():
            if line.startswith("+++ ") or line.startswith("--- "):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                deleted += 1
        return added, deleted

    @staticmethod
    def _compact_response_for_file_ops(text: str) -> str:
        """Compress verbose model prose when files were created/edited."""
        if not text:
            return ""

        compact = re.sub(r"\[File (saved|updated|edited): [^\]]+\]", "", text)
        compact = re.sub(r"\[No changes: [^\]]+\]", "", compact)
        compact = re.sub(r"```[\s\S]*?```", "", compact)
        compact = re.sub(r"\n{3,}", "\n\n", compact).strip()
        if not compact:
            return "Done."

        if len(compact) > 320 or compact.count("\n") > 6:
            # Keep only the first paragraph for speed/readability in Telegram.
            first = compact.split("\n\n", 1)[0].strip()
            if len(first) > 220:
                first = first[:217].rstrip() + "..."
            return first or "Done."

        return compact

    @staticmethod
    def _is_incomplete_html_text(text: str) -> bool:
        """Heuristic detection for likely-truncated HTML documents."""
        lower = (text or "").lower()
        if "<html" not in lower and "<!doctype html" not in lower:
            return False
        if "</html>" not in lower or "</body>" not in lower:
            return True
        if lower.count("<section") > lower.count("</section>") + 2:
            return True
        if lower.count("<div") > lower.count("</div>") + 12:
            return True
        return False

    @staticmethod
    def _render_file_operations(
        operations: list[FileOperationResult],
        include_diffs: bool = True,
        workspace_label: str = "workspace/",
    ) -> str:
        """Render a human-readable summary of applied file operations."""
        if not operations:
            return ""

        success = [op for op in operations if op.action != "error"]
        failures = [op for op in operations if op.action == "error"]
        lines: list[str] = []
        total_added = 0
        total_deleted = 0

        if success:
            lines.append(f"✅ Applied {len(success)} file operation(s):")
            for op in success:
                change_hint = ""
                if op.diff:
                    added, deleted = BotFileOpsMixin._diff_line_stats(op.diff)
                    total_added += added
                    total_deleted += deleted
                    if include_diffs:
                        change_hint = f" (+{added}/-{deleted} lines)"

                if op.action in ("created", "auto_created"):
                    lines.append(f"- Created `{op.path}`{change_hint}")
                elif op.action == "updated":
                    lines.append(f"- Updated `{op.path}`{change_hint}")
                elif op.action == "edited":
                    lines.append(f"- Edited `{op.path}`{change_hint}")
                elif op.action == "unchanged":
                    lines.append(f"- No changes in `{op.path}`")
                else:
                    lines.append(f"- `{op.path}`")

            if include_diffs and (total_added or total_deleted):
                lines.append(f"- Diff summary: +{total_added} / -{total_deleted} lines")

            lines.append("")
            lines.append(f"📁 Saved to {workspace_label}")

        if failures:
            if lines:
                lines.append("")
            lines.append(f"⚠️ {len(failures)} file operation(s) failed:")
            for op in failures:
                detail = op.detail or "unknown error"
                lines.append(f"- `{op.path}`: {detail}")

        return "\n".join(lines).strip()

    # ── File Operation Tool ───────────────────────────────────

    async def _process_file_blocks(self, response: str) -> tuple[list[FileOperationResult], str]:
        """Apply edit/create instructions from model response and return cleaned text."""
        operations: list[FileOperationResult] = []
        cleaned_response = response

        lang_extensions = {
            "html": ".html",
            "htm": ".html",
            "css": ".css",
            "javascript": ".js",
            "js": ".js",
            "python": ".py",
            "py": ".py",
            "json": ".json",
            "xml": ".xml",
            "sql": ".sql",
            "markdown": ".md",
            "md": ".md",
            "bash": ".sh",
            "sh": ".sh",
            "txt": ".txt",
            "java": ".java",
            "ts": ".ts",
            "tsx": ".tsx",
            "jsx": ".jsx",
            "go": ".go",
            "rs": ".rs",
            "c": ".c",
            "cpp": ".cpp",
            "yaml": ".yaml",
            "yml": ".yml",
        }

        edit_pattern = re.compile(
            r"```edit:(?P<path>[^\n`]+)\s*\n(?P<body>[\s\S]*?)```",
            re.IGNORECASE,
        )

        def success_count() -> int:
            return sum(1 for op in operations if op.action != "error")

        def write_workspace_file(raw_path: str, content: str, auto_generated: bool = False) -> str:
            target, rel_path, path_err = self._resolve_workspace_path(raw_path)
            display_path = rel_path or raw_path.strip() or "unknown"
            if path_err or target is None or rel_path is None:
                operations.append(FileOperationResult("error", display_path, path_err or "invalid path"))
                return f"[Save failed: {display_path}]"

            before = None
            if target.exists():
                try:
                    before = target.read_text(encoding="utf-8")
                except Exception as e:
                    operations.append(FileOperationResult("error", rel_path, f"failed to read file: {e}"))
                    return f"[Save failed: {rel_path}]"

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            except Exception as e:
                operations.append(FileOperationResult("error", rel_path, f"failed to write file: {e}"))
                return f"[Save failed: {rel_path}]"

            if before is None:
                action = "auto_created" if auto_generated else "created"
                diff_text = self._build_unified_diff("", content, rel_path)
                operations.append(FileOperationResult(action, rel_path, diff=diff_text))
                log.info(f"Saved file: {target}")
                return f"[File saved: {rel_path}]"

            if before == content:
                operations.append(FileOperationResult("unchanged", rel_path))
                return f"[No changes: {rel_path}]"

            diff_text = self._build_unified_diff(before, content, rel_path)
            operations.append(FileOperationResult("updated", rel_path, diff=diff_text))
            log.info(f"Updated file: {target}")
            return f"[File updated: {rel_path}]"

        def _max_overlap_suffix_prefix(left: str, right: str, max_len: int = 1500) -> int:
            if not left or not right:
                return 0
            max_check = min(len(left), len(right), max_len)
            for size in range(max_check, 0, -1):
                if left.endswith(right[:size]):
                    return size
            return 0

        def _strip_outer_code_fence(text: str) -> tuple[str, bool]:
            """Return (content_without_wrapping_fence, saw_closing_fence)."""
            chunk = (text or "").strip()
            if not chunk:
                return "", False

            wrapped = re.match(r"^```[^\n`]*\n([\s\S]*?)\n```$", chunk)
            if wrapped:
                return wrapped.group(1).strip(), True

            if chunk.startswith("```"):
                nl = chunk.find("\n")
                if nl >= 0:
                    chunk = chunk[nl + 1 :]

            if chunk.strip() == "```":
                return "", True

            close_idx = chunk.find("\n```")
            if close_idx >= 0:
                return chunk[:close_idx].rstrip(), True

            if chunk.endswith("```"):
                return chunk[:-3].rstrip(), True

            return chunk.strip(), False

        async def complete_unclosed_named_fence(
            lang: str,
            raw_path: str,
            partial_content: str,
        ) -> tuple[str, bool]:
            """Try to continue a truncated ```lang:path fenced block."""
            assembled = (partial_content or "").rstrip()
            lang_name = (lang or "txt").strip().lower()
            attempts = 3

            for _ in range(attempts):
                if lang_name in {"html", "htm"} and not self._is_incomplete_html_text(assembled):
                    return assembled, True

                continuation_system = (
                    "You are continuing a truncated fenced file block.\n"
                    "Return ONLY the missing tail starting from the exact next character.\n"
                    "Do NOT repeat already-sent text.\n"
                    "Do NOT include explanations.\n"
                    "When complete, end with a closing fence line: ```"
                )
                continuation_user = (
                    f"The following block was truncated before the closing fence.\n\n"
                    f"```{lang_name}:{raw_path}\n"
                    f"{assembled}\n\n"
                    "Continue now from the next character only."
                )

                try:
                    continuation = await self.llm.chat(
                        [{"role": "user", "content": continuation_user}],
                        system_prompt=continuation_system,
                    )
                except Exception as e:
                    log.error(f"Continuation pass failed for {raw_path}: {e}")
                    return assembled, False

                if not continuation:
                    break

                # If model returned a full fenced replacement block, use it directly.
                full_block = re.search(
                    rf"```[a-zA-Z0-9_+\-]*:{re.escape(raw_path)}\s*\n([\s\S]*?)```",
                    continuation,
                    re.IGNORECASE,
                )
                if full_block:
                    candidate = full_block.group(1).strip()
                    return candidate, True

                piece, saw_closing = _strip_outer_code_fence(continuation)
                if piece:
                    overlap = _max_overlap_suffix_prefix(assembled, piece)
                    piece = piece[overlap:]
                    if piece:
                        if assembled and not assembled.endswith("\n") and not piece.startswith("\n"):
                            assembled += "\n"
                        assembled += piece

                if saw_closing:
                    if lang_name in {"html", "htm"} and self._is_incomplete_html_text(assembled):
                        continue
                    return assembled, True

            if lang_name in {"html", "htm"} and not self._is_incomplete_html_text(assembled):
                return assembled, True

            return assembled, False

        async def complete_unclosed_generic_fence(
            lang: str,
            partial_content: str,
        ) -> tuple[str, bool]:
            """Try to continue a truncated ```lang fenced block with no explicit path."""
            assembled = (partial_content or "").rstrip()
            lang_name = (lang or "txt").strip().lower()

            for _ in range(2):
                continuation_system = (
                    "You are continuing a truncated fenced code block.\n"
                    "Return ONLY the missing tail from the exact next character.\n"
                    "No explanation. End with ``` when complete."
                )
                continuation_user = (
                    f"```{lang_name}\n"
                    f"{assembled}\n\n"
                    "Continue now from the next character only."
                )
                try:
                    continuation = await self.llm.chat(
                        [{"role": "user", "content": continuation_user}],
                        system_prompt=continuation_system,
                    )
                except Exception:
                    return assembled, False

                if not continuation:
                    break

                piece, saw_closing = _strip_outer_code_fence(continuation)
                if piece:
                    overlap = _max_overlap_suffix_prefix(assembled, piece)
                    piece = piece[overlap:]
                    if piece:
                        if assembled and not assembled.endswith("\n") and not piece.startswith("\n"):
                            assembled += "\n"
                        assembled += piece
                if saw_closing:
                    return assembled, True

            return assembled, False

        def apply_edit_block(match: re.Match) -> str:
            raw_path = match.group("path").strip()
            edit_body = match.group("body").strip("\n")

            target, rel_path, path_err = self._resolve_workspace_path(raw_path)
            display_path = rel_path or raw_path or "unknown"
            if path_err or target is None or rel_path is None:
                operations.append(FileOperationResult("error", display_path, path_err or "invalid path"))
                return f"[Edit failed: {display_path}]"

            if not target.exists():
                operations.append(FileOperationResult("error", rel_path, "file not found"))
                return f"[Edit failed: {rel_path}]"

            try:
                before = target.read_text(encoding="utf-8")
            except Exception as e:
                operations.append(FileOperationResult("error", rel_path, f"failed to read file: {e}"))
                return f"[Edit failed: {rel_path}]"

            after, apply_err = self._apply_search_replace_hunks(before, edit_body)
            if apply_err:
                operations.append(FileOperationResult("error", rel_path, apply_err))
                return f"[Edit failed: {rel_path}]"

            if after == before:
                operations.append(FileOperationResult("unchanged", rel_path))
                return f"[No changes: {rel_path}]"

            try:
                target.write_text(after, encoding="utf-8")
            except Exception as e:
                operations.append(FileOperationResult("error", rel_path, f"failed to write file: {e}"))
                return f"[Edit failed: {rel_path}]"

            diff_text = self._build_unified_diff(before, after, rel_path)
            operations.append(FileOperationResult("edited", rel_path, diff=diff_text))
            log.info(f"Applied edit block: {target}")
            return f"[File edited: {rel_path}]"

        cleaned_response = re.sub(edit_pattern, apply_edit_block, cleaned_response)

        pattern_named = re.compile(
            r"```([a-zA-Z0-9_+\-]+):([^\n`]+)\s*\n([\s\S]*?)```",
            re.MULTILINE,
        )

        def apply_named_file_block(match: re.Match) -> str:
            raw_path = match.group(2).strip()
            content = match.group(3).strip()
            return write_workspace_file(raw_path, content)

        cleaned_response = re.sub(pattern_named, apply_named_file_block, cleaned_response)

        # Common malformed style: ```index.html ... ```
        pattern_filename_fence = re.compile(
            r"```(?P<path>[^\n`]+\.[a-zA-Z0-9]{1,10})\s*\n(?P<body>[\s\S]*?)```",
            re.MULTILINE,
        )

        def apply_filename_fence_block(match: re.Match) -> str:
            raw_path = match.group("path").strip()
            content = match.group("body").strip()
            return write_workspace_file(raw_path, content)

        cleaned_response = re.sub(pattern_filename_fence, apply_filename_fence_block, cleaned_response)

        pattern_file_label = re.compile(
            r"File:\s*([^\n`]+)\s*\n```([a-zA-Z0-9_+\-]+)?\s*\n?([\s\S]*?)```",
            re.IGNORECASE,
        )

        def apply_file_label_block(match: re.Match) -> str:
            raw_path = match.group(1).strip()
            content = match.group(3).strip()
            return write_workspace_file(raw_path, content)

        cleaned_response = re.sub(pattern_file_label, apply_file_label_block, cleaned_response)

        file_counter = 1
        pattern_auto = re.compile(r"```([a-zA-Z0-9_+\-]+)?\s*\n([\s\S]*?)```")

        def is_code_like(lang: str, content: str) -> bool:
            if lang and lang not in {"text", "txt", "plain"}:
                return True
            hints = ("<!doctype", "<html", "{", "};", "function ", "class ", "import ", "def ")
            lowered = content.lower()
            return any(h in lowered for h in hints)

        def apply_auto_block(match: re.Match) -> str:
            nonlocal file_counter
            lang = (match.group(1) or "txt").strip().lower()
            content = match.group(2).strip()

            # Keep tiny snippets inline; move large/code-like blocks to workspace.
            if lang == "diff":
                return match.group(0)
            if len(content) < 120 and not is_code_like(lang, content):
                return match.group(0)

            ext = lang_extensions.get(lang, ".txt")
            filename = f"output_{int(time.time())}_{file_counter}{ext}"
            file_counter += 1
            return write_workspace_file(filename, content, auto_generated=True)

        cleaned_response = re.sub(pattern_auto, apply_auto_block, cleaned_response)

        # Salvage malformed/unclosed named fence: ```html:index.html ...EOF
        if success_count() == 0:
            unclosed_named = re.search(
                r"```([a-zA-Z0-9_+\-]+):([^\n`]+)\s*\n([\s\S]+)$",
                cleaned_response,
                re.MULTILINE,
            )
            if unclosed_named:
                lang = (unclosed_named.group(1) or "txt").strip().lower()
                raw_path = unclosed_named.group(2).strip()
                content = unclosed_named.group(3).strip()
                if content:
                    completed_content, completed = await complete_unclosed_named_fence(
                        lang=lang,
                        raw_path=raw_path,
                        partial_content=content,
                    )
                    if completed:
                        marker = write_workspace_file(raw_path, completed_content)
                    else:
                        _, rel_path, _ = self._resolve_workspace_path(raw_path)
                        display_path = rel_path or raw_path or "unknown"
                        operations.append(
                            FileOperationResult(
                                "error",
                                display_path,
                                "incomplete code block (model output truncated before closing fence)",
                            )
                        )
                        marker = f"[Save failed: {display_path}]"
                    prefix = cleaned_response[: unclosed_named.start()].rstrip()
                    cleaned_response = (
                        (prefix + "\n\n" if prefix else "")
                        + marker
                    ).strip()

        # Salvage malformed/unclosed generic fence: ```html ...EOF
        if success_count() == 0:
            unclosed_generic = re.search(
                r"```([a-zA-Z0-9_+\-]+)?\s*\n([\s\S]+)$",
                cleaned_response,
                re.MULTILINE,
            )
            if unclosed_generic:
                lang = (unclosed_generic.group(1) or "txt").strip().lower()
                content = unclosed_generic.group(2).strip()
                if lang != "diff" and len(content) >= 120:
                    ext = lang_extensions.get(lang, ".txt")
                    filename = f"output_{int(time.time())}_unclosed{ext}"
                    completed_content, completed = await complete_unclosed_generic_fence(
                        lang=lang,
                        partial_content=content,
                    )
                    if completed:
                        marker = write_workspace_file(filename, completed_content, auto_generated=True)
                    else:
                        operations.append(
                            FileOperationResult(
                                "error",
                                filename,
                                "incomplete code block (model output truncated before closing fence)",
                            )
                        )
                        marker = f"[Save failed: {filename}]"
                    prefix = cleaned_response[: unclosed_generic.start()].rstrip()
                    cleaned_response = (
                        (prefix + "\n\n" if prefix else "")
                        + marker
                    ).strip()

        # Last resort: HTML document without fences.
        if success_count() == 0:
            html_start = cleaned_response.lower().find("<!doctype html")
            if html_start < 0:
                html_start = cleaned_response.lower().find("<html")
            if html_start >= 0:
                html = cleaned_response[html_start:].strip()
                if len(html) >= 200:
                    if self._is_incomplete_html_text(html):
                        operations.append(
                            FileOperationResult(
                                "error",
                                "index.html",
                                "incomplete html output (missing closing tags)",
                            )
                        )
                        marker = "[Save failed: index.html]"
                    else:
                        marker = write_workspace_file("index.html", html, auto_generated=True)
                    intro = cleaned_response[:html_start].strip()
                    cleaned_response = (f"{intro}\n\n{marker}" if intro else marker).strip()

        # Never return huge fenced code in chat: remove any remaining large code blocks.
        def strip_large_leftover_code(match: re.Match) -> str:
            content = match.group(2).strip()
            if len(content) >= 120:
                return "[Large code omitted in chat]"
            return match.group(0)

        cleaned_response = re.sub(pattern_auto, strip_large_leftover_code, cleaned_response)

        return operations, cleaned_response.strip()

    async def _retry_failed_edits(
        self,
        user_text: str,
        original_model_response: str,
        failed_ops: list[FileOperationResult],
    ) -> tuple[list[FileOperationResult], str]:
        """Retry failed edit operations once using exact current file content."""
        retryable_errors = [
            op for op in failed_ops
            if op.action == "error"
            and any(
                marker in op.detail
                for marker in (
                    "SEARCH text not found",
                    "SEARCH text appears",
                    "no SEARCH/REPLACE hunks found",
                )
            )
        ]
        if not retryable_errors:
            return [], ""

        snippets: list[str] = []
        retry_paths: list[str] = []
        for op in retryable_errors:
            target, rel_path, path_err = self._resolve_workspace_path(op.path)
            if path_err or target is None or rel_path is None:
                continue
            if not target.exists():
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except Exception:
                continue

            # Keep retry prompt bounded.
            max_chars = 9000
            shown = content[:max_chars]
            if len(content) > max_chars:
                shown += "\n... [truncated]"

            snippets.append(f"### {rel_path}\n```text\n{shown}\n```")
            retry_paths.append(rel_path)

        if not snippets:
            return [], ""

        retry_system = (
            "You are a precise code editor. "
            "Return ONLY edit blocks in this exact format:\n"
            "```edit:path/to/file.ext\n"
            "<<<<<<< SEARCH\n"
            "exact old text\n"
            "=======\n"
            "new text\n"
            ">>>>>>> REPLACE\n"
            "```\n"
            "Do not include prose."
        )
        retry_user = (
            "The previous edit failed because SEARCH text did not match exactly.\n\n"
            f"Original user request:\n{user_text}\n\n"
            "Previous model response:\n"
            f"{original_model_response}\n\n"
            "Current file contents:\n"
            f"{'\n\n'.join(snippets)}\n\n"
            "Generate corrected edit blocks that apply exactly to these files. "
            "If no change is needed, reply exactly: NO_CHANGES"
        )

        try:
            retry_response = await self.llm.chat(
                [{"role": "user", "content": retry_user}],
                system_prompt=retry_system,
            )
        except Exception as e:
            log.error(f"Retry edit call failed: {e}")
            return [], ""

        if not retry_response or retry_response.strip().upper() == "NO_CHANGES":
            return [], ""

        retry_ops, retry_cleaned = await self._process_file_blocks(retry_response)

        if retry_ops:
            ok_count = sum(1 for op in retry_ops if op.action != "error")
            err_count = sum(1 for op in retry_ops if op.action == "error")
            log.info(
                f"Retry edit result for {', '.join(retry_paths)}: "
                f"{ok_count} succeeded, {err_count} failed"
            )

        return retry_ops, retry_cleaned

    async def _force_file_ops_pass(
        self,
        session_id: str,
        user_text: str,
        prior_model_response: str,
    ) -> tuple[list[FileOperationResult], str]:
        """Force a file operation pass when the model returned prose/no-op."""
        target_files = self._collect_workspace_candidates(user_text, session_id, limit=4)
        snippets: list[str] = []
        for rel_path in target_files:
            target, _, err = self._resolve_workspace_path(rel_path)
            if err or target is None or not target.exists():
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except Exception:
                continue
            shown = content[:14000]
            if len(content) > 14000:
                shown += "\n... [truncated]"
            snippets.append(f"### {rel_path}\n```text\n{shown}\n```")

        file_context = (
            f"Current candidate workspace files:\n{'\n\n'.join(snippets)}"
            if snippets
            else "Current candidate workspace files:\n(none yet - create new files in workspace as needed)"
        )

        forced_system = (
            "You are a file operation engine for LightClaw. "
            "Do NOT ask to inspect/read files. You already have file contents. "
            "You MUST perform the requested modifications now.\n"
            "Return ONLY file operation blocks:\n"
            "1) Edits:\n"
            "```edit:path/to/file.ext\n"
            "<<<<<<< SEARCH\n"
            "exact old text\n"
            "=======\n"
            "new text\n"
            ">>>>>>> REPLACE\n"
            "```\n"
            "2) Full rewrite if large changes:\n"
            "```lang:path/to/file.ext\n"
            "<full file>\n"
            "```\n"
            "Use a language tag that matches the file extension.\n"
            "No prose."
        )
        forced_user = (
            f"User request:\n{user_text}\n\n"
            "Previous model response (incorrect/no-op):\n"
            f"{prior_model_response}\n\n"
            f"{file_context}\n\n"
            "Now apply the request directly. Return file operation blocks only."
        )

        try:
            forced_response = await self.llm.chat(
                [{"role": "user", "content": forced_user}],
                system_prompt=forced_system,
            )
        except Exception as e:
            log.error(f"Forced file-op pass failed: {e}")
            return [], ""

        if not forced_response:
            return [], ""

        return await self._process_file_blocks(forced_response)

    async def _repair_incomplete_html(
        self,
        session_id: str,
        user_text: str,
        file_ops: list[FileOperationResult],
    ) -> list[FileOperationResult]:
        """Repair likely-truncated HTML files created/updated by the model."""
        repair_ops: list[FileOperationResult] = []
        success_ops = [op for op in file_ops if op.action != "error"]
        html_paths = [op.path for op in success_ops if op.path.lower().endswith((".html", ".htm"))]
        if not html_paths:
            return repair_ops

        # Preserve order while avoiding duplicate repair attempts per path.
        seen_paths: set[str] = set()
        ordered_html_paths: list[str] = []
        for path in html_paths:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            ordered_html_paths.append(path)

        for rel_path in ordered_html_paths:
            target, _, err = self._resolve_workspace_path(rel_path)
            if err or target is None or not target.exists():
                continue

            max_attempts = 3
            repaired = False

            for attempt in range(1, max_attempts + 1):
                try:
                    content = target.read_text(encoding="utf-8")
                except Exception:
                    break

                if not self._is_incomplete_html_text(content):
                    repaired = True
                    break

                repair_system = (
                    "You are an HTML repair engine. "
                    "The file below is truncated/incomplete. "
                    "Return ONLY one full-file block in this format:\n"
                    "```html:path/to/file.html\n"
                    "<complete valid HTML document>\n"
                    "```\n"
                    "CRITICAL:\n"
                    "- Include </body> and </html>\n"
                    "- Return full file, not a diff\n"
                    "- No prose."
                )
                repair_user = (
                    f"Attempt: {attempt}/{max_attempts}\n"
                    f"User context/request:\n{user_text}\n\n"
                    f"Repair this file and keep its design intent:\n"
                    f"Path: {rel_path}\n"
                    "Current content:\n"
                    f"```html\n{content}\n```"
                )
                try:
                    repair_response = await self.llm.chat(
                        [{"role": "user", "content": repair_user}],
                        system_prompt=repair_system,
                    )
                except Exception as e:
                    log.error(f"HTML repair pass failed for {rel_path}: {e}")
                    continue

                if not repair_response:
                    continue

                ops, _ = await self._process_file_blocks(repair_response)
                if ops:
                    ok = sum(1 for op in ops if op.action != "error")
                    err_count = sum(1 for op in ops if op.action == "error")
                    log.info(
                        f"[{session_id}] HTML repair {rel_path} (attempt {attempt}): "
                        f"{ok} succeeded, {err_count} failed"
                    )
                    repair_ops.extend(ops)

                try:
                    updated = target.read_text(encoding="utf-8")
                except Exception:
                    updated = ""
                if updated and not self._is_incomplete_html_text(updated):
                    repaired = True
                    break

            if not repaired:
                repair_ops.append(
                    FileOperationResult(
                        "error",
                        rel_path,
                        "html file still incomplete after repair attempts",
                    )
                )

        return repair_ops

    # ── /show ─────────────────────────────────────────────────
