"""Private-by-default run receipts with a stable JSON and Markdown contract."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .fs import atomic_write_text
from .security import redact_text

RECEIPT_SCHEMA_VERSION = 1
REQUIRED_RECEIPT_FIELDS = (
    "run_id",
    "original_goal",
    "approved_scope",
    "risk_level",
    "capability_profile",
    "plan",
    "started_at",
    "finished_at",
    "commands",
    "file_changes",
    "checks",
    "handoffs",
    "artifacts",
    "failures",
    "retries",
    "disposition",
    "checkpoint",
    "undo",
)
SHARE_CARD_FIELDS = (
    "schema_version",
    "run_id",
    "original_goal",
    "approved_scope",
    "risk_level",
    "capability_profile",
    "plan",
    "usage",
    "file_changes",
    "diff_summary",
    "checks",
    "artifacts",
    "failures",
    "retries",
    "disposition",
)


def _redact_value(value):
    if isinstance(value, str):
        return redact_text(value, os.environ)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    return value


def _write_private(path: Path, content: str) -> None:
    atomic_write_text(path, content, mode=0o600)


def validate_receipt(receipt: dict[str, object]) -> list[str]:
    """Return stable contract errors without exposing receipt values."""
    errors: list[str] = []
    for field in REQUIRED_RECEIPT_FIELDS:
        if field not in receipt:
            errors.append(f"missing required field: {field}")
    for field in ("plan", "commands", "file_changes", "checks", "handoffs", "artifacts", "failures"):
        if field in receipt and not isinstance(receipt[field], list):
            errors.append(f"field must be a list: {field}")
    if "checkpoint" in receipt and not isinstance(receipt["checkpoint"], dict):
        errors.append("field must be an object: checkpoint")
    if "retries" in receipt and not isinstance(receipt["retries"], (int, list)):
        errors.append("field must be an integer or list: retries")
    if int(receipt.get("schema_version", RECEIPT_SCHEMA_VERSION)) != RECEIPT_SCHEMA_VERSION:
        errors.append(f"unsupported schema_version; expected {RECEIPT_SCHEMA_VERSION}")
    return errors


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

    usage = receipt.get("usage") if isinstance(receipt.get("usage"), dict) else {}
    lines.extend(
        [
            "",
            "## Usage",
            "",
            f"- Provider: `{usage.get('provider', 'not reported')}`",
            f"- Tokens: `{usage.get('tokens', 'not reported')}`",
            f"- Estimated cost USD: `{usage.get('estimated_cost_usd', 'not reported')}`",
        ]
    )

    lines.extend(["", "## Files", ""])
    changes = receipt.get("file_changes") if isinstance(receipt.get("file_changes"), list) else []
    for item in changes:
        if isinstance(item, dict):
            lines.append(
                f"- `{item.get('path', '')}` — {item.get('change', 'created')} "
                f"({item.get('bytes', 0)} bytes, sha256 `{item.get('sha256', '')}`)"
            )
    if receipt.get("diff_summary"):
        lines.append(f"- Diff summary: {receipt.get('diff_summary')}")

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

    lines.extend(["", "## Handoffs, failures, and retries", ""])
    handoffs = receipt.get("handoffs") if isinstance(receipt.get("handoffs"), list) else []
    failures = receipt.get("failures") if isinstance(receipt.get("failures"), list) else []
    if not handoffs:
        lines.append("- Handoffs: none")
    for handoff in handoffs:
        if isinstance(handoff, dict):
            lines.append(
                f"- Handoff `{handoff.get('from', handoff.get('lane', 'unknown'))}` → "
                f"`{handoff.get('to', 'final audit')}`: {handoff.get('status', 'recorded')}"
            )
        else:
            lines.append(f"- Handoff: {handoff}")
    if not failures:
        lines.append("- Failures: none")
    for failure in failures:
        lines.append(f"- Failure: {failure}")
    retries = receipt.get("retries", 0)
    lines.append(f"- Retries: `{json.dumps(retries, sort_keys=True)}`")
    lines.append(f"- Final disposition: `{receipt.get('disposition', 'unknown')}`")

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


def build_share_card(receipt: dict[str, object]) -> dict[str, object]:
    """Build the explicit, sanitized whitelist used for voluntary sharing."""
    safe = _redact_value(dict(receipt))
    card = {field: safe[field] for field in SHARE_CARD_FIELDS if field in safe}
    card["schema_version"] = RECEIPT_SCHEMA_VERSION
    card["share_card"] = True
    card["excluded_private_fields"] = [
        "commands",
        "handoffs",
        "checkpoint",
        "undo",
        "started_at",
        "finished_at",
    ]
    return card


def export_share_card(
    receipt_path: str | Path,
    output_path: str | Path,
    *,
    apply: bool = False,
) -> dict[str, object]:
    """Preview or write a sanitized Run Card; writing is always explicit."""
    source = Path(receipt_path).expanduser().resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("receipt is missing or invalid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("receipt must contain a JSON object")
    errors = validate_receipt(raw)
    if errors:
        raise ValueError("invalid receipt: " + "; ".join(errors))
    card = build_share_card(raw)
    destination = Path(output_path).expanduser().resolve()
    result: dict[str, object] = {
        "applied": False,
        "source": source.as_posix(),
        "output": destination.as_posix(),
        "included_fields": sorted(card),
        "excluded_private_fields": card["excluded_private_fields"],
        "card": card,
    }
    if apply:
        if destination == source:
            raise ValueError("share card output must not overwrite the private receipt")
        _write_private(destination, json.dumps(card, indent=2, sort_keys=True) + "\n")
        result["applied"] = True
    return result


def write_receipt(
    receipt: dict[str, object],
    output_dir: str | Path,
) -> tuple[Path, Path, dict[str, object]]:
    """Redact and atomically write a receipt as JSON and Markdown."""
    safe = _redact_value(dict(receipt))
    safe["schema_version"] = RECEIPT_SCHEMA_VERSION
    errors = validate_receipt(safe)
    if errors:
        raise ValueError("invalid receipt: " + "; ".join(errors))
    output = Path(output_dir).expanduser().resolve()
    json_path = output / "receipt.json"
    markdown_path = output / "receipt.md"
    _write_private(json_path, json.dumps(safe, indent=2, sort_keys=True) + "\n")
    _write_private(markdown_path, render_receipt_markdown(safe))
    return json_path, markdown_path, safe
