"""The panopticon pin bump is idempotent and does not write on a dry run."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bump_panopticon_pin as bump  # noqa: E402
import resolve_pricing_digest as resolve  # noqa: E402

OLD = "96de4c59dd1bc44743c7f091d4dde860155bde3cb2d63806749b28b1b8f90090"
NEW = "b" * 64
OTHER = "a" * 64
TAG = "v1.2.3"
PRICING_REPO = "example/hypermesh-pricing"

LAPTOP = f"""\
# Keycloak 26+ official bootstrap names.
  # v0.1.1 digest pin, not a moving tag. A tag is not a valid pin.
  hypermesh-pricing:
    image: ${{HYPERMESH_PRICING_IMAGE:-ghcr.io/fyberlabs/hypermesh-pricing@sha256:{OLD}}}
  payment-service:
    image: ghcr.io/fyberlabs/payment-service@sha256:{OTHER}
"""

VM = f"""\
  # Pricing engine. No published port. Digest-pinned external image.
  # v0.1.1. Not built by ghcr-test.yml.
  hypermesh-pricing:
    image: ${{HYPERMESH_PRICING_IMAGE:-ghcr.io/fyberlabs/hypermesh-pricing@sha256:{OLD}}}
"""


class FakeGitHub:
    def __init__(self, files: dict[str, str]) -> None:
        self.files_at_base = dict(files)
        self.base_sha = "base-sha"
        self.default = "main"
        self.branch = None
        self.branch_ref = None
        self.commits: list[dict] = []
        self.prs: list[dict] = []
        self._pending = None

    def default_branch(self) -> str:
        return self.default

    def base_head(self) -> str:
        return self.base_sha

    def read_file(self, path: str, ref: str) -> str:
        if ref in {self.default, self.base_sha}:
            return self.files_at_base[path]
        if self.branch is not None and ref == self.branch.sha:
            return self.branch.files[path]
        raise RuntimeError(f"missing {path} at {ref}")

    def branch_state(self, branch: str):
        bump.assert_bump_branch(branch)
        if self.branch_ref != branch:
            return None
        return self.branch

    def commit(self, parent: str, files: dict[str, str], message: str) -> str:
        assert parent == self.base_sha
        full = dict(self.files_at_base)
        full.update(files)
        sha = f"commit-{len(self.commits) + 1}"
        self.commits.append({"parent": parent, "files": files, "message": message, "sha": sha})
        self._pending = bump.BranchState(sha=sha, parent=parent, files=full)
        return sha

    def force_branch(self, branch: str, sha: str) -> None:
        bump.assert_bump_branch(branch)
        assert self._pending is not None
        assert self._pending.sha == sha
        self.branch_ref = branch
        self.branch = self._pending

    def find_open_pr(self, branch: str):
        for pr in self.prs:
            if pr["branch"] == branch and pr["open"]:
                return pr["pull"]
        return None

    def create_pr(self, branch: str, base: str, title: str, body: str):
        assert base == self.default
        pull = bump.Pull(
            number=len(self.prs) + 1,
            title=title,
            body=body,
            html_url=f"https://github.example/panopticon/pull/{len(self.prs) + 1}",
        )
        self.prs.append({"branch": branch, "open": True, "pull": pull})
        return pull

    def update_pr(self, number: int, title: str, body: str):
        for pr in self.prs:
            if pr["pull"].number == number:
                pull = bump.Pull(number=number, title=title, body=body, html_url=pr["pull"].html_url)
                pr["pull"] = pull
                return pull
        raise RuntimeError("missing pull request")


def _files() -> dict[str, str]:
    return {"docker-compose.yaml": LAPTOP, "docker-compose.vm.yaml": VM}


def _run(client: FakeGitHub, *, dry_run: bool = False, digest: str = NEW, tag: str = TAG) -> str:
    return bump.run(
        client,
        tag=tag,
        digest=digest,
        pricing_repository=PRICING_REPO,
        dry_run=dry_run,
    )


def test_rewrite_replaces_only_the_pricing_pin_and_version_comments():
    updated = bump.rewrite(LAPTOP, f"sha256:{NEW}", TAG)
    assert f"hypermesh-pricing@sha256:{NEW}" in updated
    assert f"payment-service@sha256:{OTHER}" in updated
    assert "Keycloak 26+" in updated
    assert "# v1.2.3 digest pin, not a moving tag." in updated
    assert bump.rewrite(updated, f"sha256:{NEW}", TAG) == updated


def test_unrelated_digest_pin_comment_is_left_alone():
    text = "# openapi-generator v7.10.0 digest pin\nservices:\n" + LAPTOP
    updated = bump.rewrite(text, f"sha256:{NEW}", TAG)
    assert "# openapi-generator v7.10.0 digest pin\n" in updated
    assert "# v1.2.3 digest pin, not a moving tag." in updated
    assert f"hypermesh-pricing@sha256:{NEW}" in updated


def test_prerelease_comment_is_stable():
    once = bump.rewrite(VM, f"sha256:{NEW}", "v1.2.3-rc.1")
    twice = bump.rewrite(once, f"sha256:{NEW}", "v1.2.3-rc.1")
    assert "# v1.2.3-rc.1. Not built by ghcr-test.yml." in once
    assert twice == once


def test_missing_pin_is_an_error():
    with pytest.raises(ValueError, match="no hypermesh-pricing digest pin"):
        bump.rewrite("services: {}\n", f"sha256:{NEW}", TAG)


def test_tag_and_digest_validation():
    assert bump.require_tag("v0.1.1") == "v0.1.1"
    assert bump.require_tag(" v1.2.3-rc.1 ") == "v1.2.3-rc.1"
    for bad in ("main", "v1.2", "v1.2.3;rm", "sha-abc", "v1.2.3+build"):
        with pytest.raises(ValueError):
            bump.require_tag(bad)
    assert bump.normalize_digest(NEW) == f"sha256:{NEW}"
    assert bump.normalize_digest(f"sha256:{NEW}") == f"sha256:{NEW}"
    with pytest.raises(ValueError):
        bump.normalize_digest("B" * 64)
    with pytest.raises(ValueError):
        bump.normalize_digest("sha256:" + "ab")


def test_title_body_and_branch():
    assert bump.pr_title(TAG) == "chore(pricing): bump hypermesh-pricing to v1.2.3"
    body = bump.pr_body(PRICING_REPO, TAG, f"sha256:{NEW}")
    assert "https://github.com/example/hypermesh-pricing/releases/tag/v1.2.3" in body
    assert f"ghcr.io/fyberlabs/hypermesh-pricing@sha256:{NEW}" in body
    assert bump.branch_name(TAG) == "chore/pricing-pin-v1.2.3"
    with pytest.raises(ValueError):
        bump.assert_bump_branch("main")


def test_pricing_image_ref_accepts_only_this_images_tags():
    assert resolve.pricing_image_ref("ghcr.io/fyberlabs/hypermesh-pricing:v1.2.3")
    assert resolve.pricing_image_ref("ghcr.io/fyberlabs/hypermesh-pricing:v1.2.3-rc.1")
    assert resolve.pricing_image_ref("ghcr.io/fyberlabs/hypermesh-pricing:main")
    sha_tag = "sha-" + ("ab" * 32)
    assert resolve.pricing_image_ref(f"ghcr.io/fyberlabs/hypermesh-pricing:{sha_tag}")
    for bad in (
        "ghcr.io/example/other:v1.2.3",
        "ghcr.io/fyberlabs/hypermesh-pricing:v1.2.3;rm",
        "ghcr.io/fyberlabs/hypermesh-pricing:sha-abc",
        "ghcr.io/fyberlabs/hypermesh-pricing:latest",
    ):
        with pytest.raises(ValueError):
            resolve.pricing_image_ref(bad)


def test_pricing_image_ref_accepts_github_sha_tag():
    # 73dddab. GITHUB_SHA is 40 hex, which is what :sha-<commit> uses.
    github_sha = "sha-73dddab269adc5915e4f860d16cab3c5fa989301"
    assert len(github_sha.removeprefix("sha-")) == 40
    image = f"ghcr.io/fyberlabs/hypermesh-pricing:{github_sha}"
    assert resolve.pricing_image_ref(image) == image


def test_imagetools_digest_uses_the_top_level_line():
    text = (
        "Name:      ghcr.io/fyberlabs/hypermesh-pricing:v1.2.3\n"
        "MediaType: application/vnd.oci.image.index.v1+json\n"
        f"Digest:    sha256:{NEW}\n"
        "\n"
        "Manifests:\n"
        f"  Digest:  sha256:{OLD}\n"
    )
    assert bump.parse_imagetools_digest(text) == f"sha256:{NEW}"
    with pytest.raises(ValueError):
        bump.parse_imagetools_digest("no digest here\n")


def test_stale_version_comment_does_not_open_a_pull_request_when_the_digest_matches():
    files = {path: text.replace(OLD, NEW) for path, text in _files().items()}
    client = FakeGitHub(files)
    status = _run(client)
    assert "already pinned" in status
    assert "no pull request" in status
    assert client.commits == []
    assert client.prs == []


def test_already_pinned_does_not_open_a_pull_request():
    pinned = {
        path: bump.rewrite(text, f"sha256:{NEW}", TAG) for path, text in _files().items()
    }
    # Comments may move with the digest. Pinning means the digest, so put the
    # new digest back into files that still mention the old version comment.
    client = FakeGitHub(pinned)
    status = _run(client)
    assert "already pinned" in status
    assert "no pull request" in status
    assert client.commits == []
    assert client.prs == []


def test_open_then_rerun_is_idempotent():
    client = FakeGitHub(_files())
    first = _run(client)
    assert first.startswith("v1.2.3")
    assert "opened https://github.example/panopticon/pull/1" in first
    assert len(client.commits) == 1
    assert len(client.prs) == 1
    committed = client.commits[0]["files"]
    assert f"@sha256:{NEW}" in committed["docker-compose.yaml"]
    assert f"payment-service@sha256:{OTHER}" in committed["docker-compose.yaml"]
    assert f"@sha256:{NEW}" in committed["docker-compose.vm.yaml"]
    assert "Release notes: https://github.com/example/hypermesh-pricing/releases/tag/v1.2.3" in client.commits[0]["message"]

    second = _run(client)
    assert "already current" in second
    assert "no change" in second
    assert len(client.commits) == 1
    assert len(client.prs) == 1


def test_dry_run_does_not_write():
    client = FakeGitHub(_files())
    status = _run(client, dry_run=True)
    assert "dry-run: not opening a pull request" in status
    assert "would open a pull request" in status
    assert "chore(pricing): bump hypermesh-pricing to v1.2.3" in status
    assert "releases/tag/v1.2.3" in status
    assert f"@sha256:{NEW}" in status
    assert client.commits == []
    assert client.prs == []
    assert client.branch is None


def test_base_move_updates_the_same_pull_request():
    client = FakeGitHub(_files())
    _run(client)
    client.base_sha = "base-sha-2"
    client.files_at_base = {
        path: text + "# unrelated\n" for path, text in client.files_at_base.items()
    }
    status = _run(client)
    assert "updated https://github.example/panopticon/pull/1" in status
    assert len(client.prs) == 1
    assert len(client.commits) == 2
    assert client.commits[1]["parent"] == "base-sha-2"
    assert "# unrelated" in client.branch.files["docker-compose.yaml"]
    assert f"hypermesh-pricing@sha256:{NEW}" in client.branch.files["docker-compose.yaml"]


def test_release_workflow_bumps_on_a_hosted_runner_without_pull_request_secrets():
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    header, jobs = release.split("jobs:", 1)
    image, bump_job = jobs.split("bump-panopticon:", 1)

    assert "workflow_dispatch:" in header
    assert "dry_run:" in header
    assert "pull_request" not in header
    assert "packages: write" not in header
    assert release.count("packages: write") == 1
    assert "packages: write" in image
    assert "packages: write" not in bump_job
    assert "PANOPTICON_BUMP" not in ci
    assert release.count("runs-on:") == release.count("runs-on: ubuntu-latest") == 2
    assert "if: github.event_name == 'push'" in image
    assert "group: hypermesh-pricing-image-${{ github.ref }}" in image
    assert "cancel-in-progress: false" in image
    assert "PANOPTICON_BUMP_APP_PRIVATE_KEY" not in image
    assert "id: push" in image
    assert 'echo "digest=${digest}" >> "$GITHUB_OUTPUT"' in image
    assert "RepoDigests" not in release
    assert 'IMAGE_REF="${image}:${tag}" python3 scripts/resolve_pricing_digest.py' in image
    assert "does not match the digest from docker push" in image
    assert "from bump_panopticon_pin import require_tag" in image
    assert "require_tag(os.environ[\"TAG\"])" in image

    assert "environment: panopticon-bump" in bump_job
    assert "actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349 # v2.2.2" in bump_job
    assert "vars.PANOPTICON_BUMP_APP_ID" in bump_job
    assert "secrets.PANOPTICON_BUMP_APP_ID" not in release
    assert "secrets.PANOPTICON_BUMP_APP_PRIVATE_KEY" in bump_job
    assert "permission-contents: write" in bump_job
    assert "permission-pull-requests: write" in bump_job
    assert "repositories: panopticon" in bump_job
    assert "steps.app-token.outputs.token" in bump_job
    assert "scripts/bump_panopticon_pin.py" in bump_job
    assert "scripts/resolve_pricing_digest.py" in bump_job
    assert "github.event_name != 'pull_request'" in bump_job
    assert "pull_request:" not in release
    assert "pull_request_target:" not in release
    assert "deploy:" not in release
    visibility = release.split("Set package visibility to public", 1)[1]
    assert "exit 1" not in visibility
    assert "GITHUB_STEP_SUMMARY" not in visibility

    for phrase in (
        "hypermesh-pricing-bump",
        "panopticon-bump",
        "PANOPTICON_BUMP_APP_ID",
        "PANOPTICON_BUMP_APP_PRIVATE_KEY",
        "vars.PANOPTICON_BUMP_APP_ID",
        "secrets.PANOPTICON_BUMP_APP_PRIVATE_KEY",
        "contents:write",
        "pull-requests:write",
        "actions/create-github-app-token",
        "docker-compose.yaml",
        "docker-compose.vm.yaml",
        "dry_run",
        "chore(pricing): bump hypermesh-pricing to vX.Y.Z",
        "v*.*.*",
    ):
        assert phrase in readme
