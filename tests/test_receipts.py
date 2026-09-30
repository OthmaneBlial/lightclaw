from __future__ import annotations

import json
import stat

import pytest

from core.receipts import (
    export_share_card,
    read_receipt,
    validate_receipt,
    write_receipt,
)


def _receipt(secret: str = "safe goal") -> dict[str, object]:
    return {
        "run_id": "run-fixture-1",
        "original_goal": secret,
        "approved_scope": "src/ and tests/",
        "risk_level": "medium",
        "capability_profile": "workspace-write",
        "plan": [
            {
                "label": "builder",
                "worker": "fixture",
                "model": "recorded",
                "depends_on": [],
                "task": "edit the fixture",
            }
        ],
        "started_at": "2026-08-23T20:00:00Z",
        "finished_at": "2026-08-23T20:00:01Z",
        "duration_seconds": 1,
        "usage": {"provider": "fixture", "tokens": 0, "estimated_cost_usd": 0},
        "commands": [{"command": "python -m pytest", "exit_code": 0, "summary": "passed"}],
        "file_changes": [{"path": "src/app.py", "change": "updated", "bytes": 10, "sha256": "abc"}],
        "diff_summary": "1 file changed",
        "checks": [{"name": "tests", "passed": True, "evidence": "1 passed"}],
        "handoffs": [{"from": "builder", "to": "reviewer", "status": "accepted"}],
        "artifacts": ["changes.patch"],
        "failures": [],
        "retries": 0,
        "disposition": "accepted",
        "checkpoint": {"commit": "deadbeef"},
        "undo": "lightclaw undo run-fixture-1",
    }


def test_receipt_contract_and_markdown_cover_required_evidence(tmp_path):
    json_path, markdown_path, safe = write_receipt(_receipt(), tmp_path)

    assert validate_receipt(safe) == []
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "## Usage" in markdown
    assert "## Handoffs, failures, and retries" in markdown
    assert "Final disposition" in markdown
    assert stat.S_IMODE(json_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(markdown_path.stat().st_mode) == 0o600


def test_receipt_rejects_missing_required_fields(tmp_path):
    with pytest.raises(ValueError, match="missing required field: approved_scope"):
        write_receipt({"run_id": "incomplete"}, tmp_path)


@pytest.mark.parametrize("schema_version", [True, 1.5, "1", [], None])
def test_receipt_schema_version_requires_an_integer(schema_version):
    receipt = _receipt()
    receipt["schema_version"] = schema_version

    assert validate_receipt(receipt) == ["unsupported schema_version; expected 1"]


def test_receipt_retries_rejects_boolean():
    receipt = _receipt()
    receipt["retries"] = True

    assert validate_receipt(receipt) == ["field must be an integer or list: retries"]


def test_share_card_rejects_malformed_schema_version_with_value_error(tmp_path):
    source, _, _ = write_receipt(_receipt(), tmp_path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["schema_version"] = []
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported schema_version; expected 1"):
        export_share_card(source, tmp_path / "share-card.json")


def test_read_receipt_is_bounded_and_rejects_non_objects(tmp_path, monkeypatch):
    source = tmp_path / "receipt.json"
    monkeypatch.setattr("core.receipts.MAX_RECEIPT_BYTES", 8)
    source.write_bytes(b" " * 9)

    with pytest.raises(ValueError, match="too large"):
        read_receipt(source)

    source.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        read_receipt(source)


def test_write_receipt_enforces_read_limit_before_creating_files(tmp_path, monkeypatch):
    monkeypatch.setattr("core.receipts.MAX_RECEIPT_BYTES", 128)

    with pytest.raises(ValueError, match="exceeds size limit"):
        write_receipt(_receipt("x" * 256), tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_share_card_is_preview_first_whitelisted_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fixture-sensitive")
    source = tmp_path / "receipt.json"
    write_receipt(_receipt("Do not expose sk-fixture-sensitive"), tmp_path)
    output = tmp_path / "public-card.json"

    preview = export_share_card(source, output)
    assert preview["applied"] is False
    assert not output.exists()
    assert "commands" not in preview["card"]
    assert "handoffs" not in preview["card"]
    assert "checkpoint" not in preview["card"]
    assert "undo" not in preview["card"]
    assert "[REDACTED]" in preview["card"]["original_goal"]

    applied = export_share_card(source, output, apply=True)
    assert applied["applied"] is True
    card = json.loads(output.read_text(encoding="utf-8"))
    assert card["share_card"] is True
    assert "sk-fixture-sensitive" not in output.read_text(encoding="utf-8")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_share_card_refuses_to_overwrite_private_receipt(tmp_path):
    source, _, _ = write_receipt(_receipt(), tmp_path)
    with pytest.raises(ValueError, match="must not overwrite"):
        export_share_card(source, source, apply=True)
