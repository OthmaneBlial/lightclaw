from __future__ import annotations

from pathlib import Path

from core.llm.matrix import build_matrix, render_markdown


def test_generated_provider_matrix_covers_registry_contracts():
    fixture_root = Path(__file__).parent / "fixtures" / "providers"
    matrix = build_matrix(fixture_root)

    assert len(matrix["providers"]) == 6
    assert {row["status"] for row in matrix["providers"]} == {"recorded-contract"}
    assert all(row["maintainer"] for row in matrix["providers"])
    assert all(len(row["fixture_sha256"]) == 64 for row in matrix["providers"])
    markdown = render_markdown(matrix)
    assert "do not edit manually" in markdown
    assert "live vendor availability" in markdown
