"""Private-by-default run receipts with a stable JSON and Markdown contract."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .security import redact_text

RECEIPT_SCHEMA_VERSION = 1


def _redact_value(value):
    if isinstance(value, str):
        return redact_text(value, os.environ)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    return value


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


def render_receipt_markdown(receipt: dict[str, object]) -> str:
    """Render the bounded receipt fields for local human review."""
    lines = [
        f"# LightClaw Run Receipt: {receipt.get('run_id', 'unknown')}",
        "",
        f"- Status: **{receipt.get('disposition', 'unknown')}**",
        f"- Goal: {receipt.get('original_goal', '')}",
        f"- Approved scope: {receipt.get('approved_scope', '')}",
        f"- Risk: `{receipt.get('risk_level', 'unknown')}`",
        f"- Capability: `{receipt.get('capability_profile', 'unknown')}`",
        f"- Started: `{receipt.get('started_at', '')}`",
        f"- Finished: `{receipt.get('finished_at', '')}`",
        f"- Duration: `{receipt.get('duration_seconds', 0)}s`",
        "",
        "## Plan",
        "",
    ]
    for item in receipt.get("plan", []) if isinstance(receipt.get("plan"), list) else []:
        if isinstance(item, dict):
            deps = item.get("depends_on") or []
            dep_text = f" (after: {', '.join(str(dep) for dep in deps)})" if deps else ""
            lines.append(
                f"- **{item.get('label', 'step')}** / {item.get('worker', 'fixture')}"
                f"{dep_text}: {item.get('task', '')}"
            )

    lines.extend(["", "## Evidence", ""])
    checks = receipt.get("checks") if isinstance(receipt.get("checks"), list) else []
    for check in checks:
        if isinstance(check, dict):
            marker = "PASS" if check.get("passed") else "FAIL"
            lines.append(f"- [{marker}] {check.get('name', 'check')}: {check.get('evidence', '')}")

    lines.extend(["", "## Files", ""])
    changes = receipt.get("file_changes") if isinstance(receipt.get("file_changes"), list) else []
    for item in changes:
        if isinstance(item, dict):
            lines.append(
                f"- `{item.get('path', '')}` — {item.get('change', 'created')} "
                f"({item.get('bytes', 0)} bytes, sha256 `{item.get('sha256', '')}`)"
            )

    lines.extend(["", "## Commands", ""])
    commands = receipt.get("commands") if isinstance(receipt.get("commands"), list) else []
    if not commands:
        lines.append("- No host command executed.")
    for command in commands:
        if isinstance(command, dict):
            lines.append(
                f"- `{command.get('command', '')}` → exit `{command.get('exit_code', '')}`; "
                f"{command.get('summary', '')}"
            )

    lines.extend(["", "## Artifacts", ""])
    for artifact in receipt.get("artifacts", []) if isinstance(receipt.get("artifacts"), list) else []:
        lines.append(f"- `{artifact}`")

    checkpoint = receipt.get("checkpoint") if isinstance(receipt.get("checkpoint"), dict) else {}
    lines.extend(
        [
            "",
            "## Recovery",
            "",
            f"- Starting checkpoint: `{json.dumps(checkpoint, sort_keys=True)}`",
            f"- Undo: `{receipt.get('undo', 'not available')}`",
            "",
            "This receipt is local and private by default. Review it before sharing.",
            "",
        ]
    )
    return "\n".join(lines)


def write_receipt(
    receipt: dict[str, object],
    output_dir: str | Path,
) -> tuple[Path, Path, dict[str, object]]:
    """Redact and atomically write a receipt as JSON and Markdown."""
    safe = _redact_value(dict(receipt))
    safe["schema_version"] = RECEIPT_SCHEMA_VERSION
    output = Path(output_dir).expanduser().resolve()
    json_path = output / "receipt.json"
    markdown_path = output / "receipt.md"
    _write_private(json_path, json.dumps(safe, indent=2, sort_keys=True) + "\n")
    _write_private(markdown_path, render_receipt_markdown(safe))
    return json_path, markdown_path, safe
