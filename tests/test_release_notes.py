import json
from pathlib import Path

from scripts.check_release_notes import validate_release_notes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOTES_PATH = PROJECT_ROOT / "docs" / "releases" / "v0.1.0.md"


def test_v010_draft_has_every_required_release_subject():
    text = NOTES_PATH.read_text(encoding="utf-8")
    assert validate_release_notes(text, version="0.1.0") == []


def test_published_contract_rejects_draft_markers():
    text = NOTES_PATH.read_text(encoding="utf-8")
    errors = validate_release_notes(text, version="0.1.0", published_body=text)
    assert "release notes: published notes contain forbidden marker 'DRAFT'" in errors
    assert "release notes: published notes contain forbidden marker 'not released'" in errors


def test_published_contract_rejects_a_different_github_body():
    text = NOTES_PATH.read_text(encoding="utf-8").replace("DRAFT", "PREVIEW").replace(
        "not released", "pending"
    )
    errors = validate_release_notes(text, version="0.1.0", published_body=json.dumps(text))
    assert "release notes: GitHub release body differs from the committed versioned notes" in errors
