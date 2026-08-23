from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_showcase import PRIVATE_RECEIPT_FIELDS, validate_showcase


def test_committed_showcase_is_private_and_statically_valid():
    root = Path(__file__).resolve().parents[1] / "showcase"

    assert validate_showcase(root) == []

    for card_path in sorted((root / "entries").glob("*/run-card.json")):
        card = json.loads(card_path.read_text(encoding="utf-8"))
        assert card["share_card"] is True
        assert not (PRIVATE_RECEIPT_FIELDS & set(card))


def test_release_feature_gate_requires_real_community_entry():
    root = Path(__file__).resolve().parents[1] / "showcase"

    errors = validate_showcase(root, require_community_feature=True)

    assert errors == ["featured.json: a consented community entry is required for this release"]


def test_workflows_never_upload_private_runtime_material():
    workflow_root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    forbidden_upload_paths = {"receipt", "prompt", ".lightclaw", "workspace", "repository"}

    for workflow in workflow_root.glob("*.yml"):
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "uses: actions/upload-artifact@" not in line:
                continue
            block = "\n".join(lines[index : index + 8]).lower()
            for forbidden in forbidden_upload_paths:
                assert forbidden not in block, f"{workflow.name} uploads private {forbidden} data"
