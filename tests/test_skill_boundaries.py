from __future__ import annotations

import io
import zipfile

import pytest

from skills import MAX_SKILL_TEXT_BYTES, SkillError, SkillManager


def _bundle(skill_text: bytes, member: str = "nested/SKILL.md") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, skill_text)
        archive.writestr("nested/_meta.json", '{"version":"1.0.0"}')
    return buffer.getvalue()


def test_skill_bundle_reads_only_bounded_manifest_content():
    text, metadata = SkillManager._extract_zip_bundle(
        _bundle(b"---\nname: safe\n---\n\n# Safe skill\n")
    )

    assert "# Safe skill" in text
    assert metadata == {"version": "1.0.0"}


def test_skill_bundle_rejects_zip_bomb_sized_manifest():
    oversized = b"a" * (MAX_SKILL_TEXT_BYTES + 1)

    with pytest.raises(SkillError, match="uncompressed size limit"):
        SkillManager._extract_zip_bundle(_bundle(oversized))
