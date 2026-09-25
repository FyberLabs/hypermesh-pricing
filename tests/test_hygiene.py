"""The public tree does not point at private infrastructure."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Split so this file does not itself contain the private pointers it rejects.
FORBIDDEN = (
    "self-" + "hosted",
    "Fyber" + "Labs/",
    "hypermesh-" + "docs",
    "df06a" + "323",
)
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "pricing_core.egg-info", "pricing_service.egg-info"}


def test_tracked_text_has_no_private_pointers():
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix not in {".py", ".md", ".yml", ".yaml", ".json", ".toml", ".sha256", ""}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert offenders == []


def test_ci_uses_github_hosted_runners_and_does_not_deploy():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert workflow.count("runs-on: ubuntu-latest") == 2
    assert workflow.count("runs-on:") == 2
    assert "deploy:" not in workflow
