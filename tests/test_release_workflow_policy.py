from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_workflow_separates_prerelease_and_stable_publication():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "publish_prerelease:" in workflow
    assert "manual publication is prerelease-only" in workflow
    assert "tag publication is stable-only" in workflow
    assert "--require-ready" in workflow
    assert "github.event_name == 'workflow_dispatch' && inputs.publish_prerelease" in workflow


def test_manual_publication_does_not_expand_container_or_github_release_jobs():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    github_release = workflow.index("  github-release-assets:")
    pypi = workflow.index("  pypi:")
    ghcr = workflow.index("  ghcr:")

    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow[github_release:pypi]
    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow[ghcr:]
    assert "inputs.publish_prerelease" not in workflow[github_release:pypi]
    assert "inputs.publish_prerelease" not in workflow[ghcr:]
