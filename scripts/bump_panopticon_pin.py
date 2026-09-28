#!/usr/bin/env python3
"""Open or update one panopticon pull request that pins hypermesh-pricing.

The release workflow passes the digest it just pushed. workflow_dispatch
passes the digest of an image that is already in GHCR. If both compose
files already pin that digest, this exits without a pull request.

Cross-repo writes use the GitHub App installation token in GH_TOKEN.
This process does not mint a JWT and does not read a personal token.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass

COMPOSE_PATHS = ("docker-compose.yaml", "docker-compose.vm.yaml")
IMAGE = "ghcr.io/fyberlabs/hypermesh-pricing"
PIN_RE = re.compile(rf"{re.escape(IMAGE)}@sha256:[0-9a-f]{{64}}")
_TAG = r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?"
TAG_RE = re.compile(rf"^{_TAG}$")
VERSION_IN_TEXT = re.compile(_TAG)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class BranchState:
    sha: str
    parent: str | None
    files: dict[str, str]


@dataclass(frozen=True)
class Pull:
    number: int
    title: str
    body: str
    html_url: str


def require_tag(tag: str) -> str:
    text = tag.strip()
    if not TAG_RE.fullmatch(text):
        raise ValueError("tag must look like vX.Y.Z")
    return text


def normalize_digest(digest: str) -> str:
    text = digest.strip()
    hexpart = text[7:] if text.startswith("sha256:") else text
    if not re.fullmatch(r"[0-9a-f]{64}", hexpart):
        raise ValueError("digest must be sha256 and 64 hex characters")
    return f"sha256:{hexpart}"


def parse_imagetools_digest(text: str) -> str:
    """First top-level Digest line from `docker buildx imagetools inspect`."""
    match = re.search(r"(?m)^Digest:\s*(sha256:[0-9a-f]{64})\s*$", text)
    if not match:
        raise ValueError("imagetools inspect did not print a Digest line")
    return normalize_digest(match.group(1))


def digest_current(text: str, digest: str) -> bool:
    pins = PIN_RE.findall(text)
    target = f"{IMAGE}@{digest}"
    return bool(pins) and all(pin == target for pin in pins)


_SERVICE_KEY = re.compile(r"^[ \t]*hypermesh-pricing:[ \t]*$")


def rewrite_comments(text: str, tag: str) -> str:
    """Update version notes in the comment block directly above hypermesh-pricing."""
    lines = text.splitlines(keepends=True)
    chosen: set[int] = set()
    for index, line in enumerate(lines):
        if _SERVICE_KEY.fullmatch(line.rstrip("\r\n")) is None:
            continue
        cursor = index - 1
        while cursor >= 0:
            body = lines[cursor].rstrip("\r\n")
            if not body.strip() or not body.lstrip().startswith("#"):
                break
            chosen.add(cursor)
            cursor -= 1
    rewritten: list[str] = []
    for index, line in enumerate(lines):
        if index in chosen and ("digest pin" in line or "ghcr-test.yml" in line):
            line = VERSION_IN_TEXT.sub(tag, line, count=1)
        rewritten.append(line)
    return "".join(rewritten)


def rewrite(text: str, digest: str, tag: str) -> str:
    if not PIN_RE.search(text):
        raise ValueError("compose file has no hypermesh-pricing digest pin")
    pinned = PIN_RE.sub(f"{IMAGE}@{digest}", text)
    return rewrite_comments(pinned, tag)


def pr_title(tag: str) -> str:
    return f"chore(pricing): bump hypermesh-pricing to {tag}"


def release_notes_url(pricing_repository: str, tag: str) -> str:
    if not REPO_RE.fullmatch(pricing_repository):
        raise ValueError("PRICING_REPOSITORY must be owner/name")
    return f"https://github.com/{pricing_repository}/releases/tag/{tag}"


def pr_body(pricing_repository: str, tag: str, digest: str) -> str:
    image = f"{IMAGE}@{digest}"
    notes = release_notes_url(pricing_repository, tag)
    return (
        f"Bump the hypermesh-pricing image pin to `{tag}`.\n"
        "\n"
        "`docker-compose.yaml` and `docker-compose.vm.yaml` now pin:\n"
        "\n"
        "```text\n"
        f"{image}\n"
        "```\n"
        "\n"
        f"Release notes: {notes}\n"
        "\n"
        "The tag can move. The digest cannot.\n"
    )


def branch_name(tag: str) -> str:
    return f"chore/pricing-pin-{tag}"


def assert_bump_branch(branch: str) -> None:
    if not branch.startswith("chore/pricing-pin-v"):
        raise ValueError(f"refusing to write branch {branch}")


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _unified(path: str, old: str, new: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=path,
        tofile=path,
        n=1,
        lineterm="",
    )
    return "\n".join(diff)


def run(client, *, tag: str, digest: str, pricing_repository: str, dry_run: bool) -> str:
    tag = require_tag(tag)
    digest = normalize_digest(digest)
    branch = branch_name(tag)
    assert_bump_branch(branch)
    title = pr_title(tag)
    body = pr_body(pricing_repository, tag, digest)
    notes = release_notes_url(pricing_repository, tag)

    base = client.default_branch()
    base_files = {path: client.read_file(path, base) for path in COMPOSE_PATHS}
    if all(digest_current(base_files[path], digest) for path in COMPOSE_PATHS):
        prefix = "dry-run: " if dry_run else ""
        return f"{tag} {digest}: {prefix}already pinned; no pull request"

    desired: dict[str, str] = {}
    changed: dict[str, str] = {}
    for path in COMPOSE_PATHS:
        updated = rewrite(base_files[path], digest, tag)
        desired[path] = updated
        if updated != base_files[path]:
            changed[path] = updated
    if not changed:
        prefix = "dry-run: " if dry_run else ""
        return f"{tag} {digest}: {prefix}already pinned; no pull request"

    base_sha = client.base_head()
    state = client.branch_state(branch)
    existing = client.find_open_pr(branch)
    branch_ready = (
        state is not None
        and state.parent == base_sha
        and all(state.files.get(path) == desired[path] for path in COMPOSE_PATHS)
    )

    if dry_run:
        if branch_ready and existing is not None:
            verb = f"pull request already current ({existing.html_url})"
        elif branch_ready:
            verb = "would open a pull request; branch already has this pin"
        elif existing is not None:
            verb = f"would update {existing.html_url}"
        else:
            verb = "would open a pull request"
        diffs = "\n".join(
            _unified(path, base_files[path], desired[path])
            for path in COMPOSE_PATHS
            if desired[path] != base_files[path]
        )
        return (
            f"{tag} {digest}: dry-run: not opening a pull request\n"
            f"{verb}\n"
            f"{title}\n"
            f"release notes: {notes}\n"
            f"{diffs}"
        )

    pushed = False
    if not branch_ready:
        message = f"{title}\n\nRelease notes: {notes}\n\nImage: {IMAGE}@{digest}\n"
        sha = client.commit(base_sha, changed, message)
        client.force_branch(branch, sha)
        pushed = True

    if existing is None:
        opened = client.create_pr(branch, base, title, body)
        return f"{tag} {digest}: opened {opened.html_url}"

    if _norm(existing.title) != _norm(title) or _norm(existing.body) != _norm(body):
        refreshed = client.update_pr(existing.number, title, body)
        return f"{tag} {digest}: updated {refreshed.html_url}"
    if pushed:
        return f"{tag} {digest}: updated {existing.html_url}"
    return f"{tag} {digest}: pull request already current ({existing.html_url}); no change"


class GhClient:
    """panopticon reads and writes through `gh` and the App installation token."""

    def __init__(self, *, token: str, owner: str, repo: str) -> None:
        if not token:
            raise ValueError("GH_TOKEN is empty")
        if repo != "panopticon":
            raise ValueError("PANOPTICON_REPO must be panopticon")
        if not owner or "/" in owner or not re.fullmatch(r"[A-Za-z0-9_.-]+", owner):
            raise ValueError("PANOPTICON_OWNER is missing")
        self.token = token
        self.owner = owner
        self.repo = repo

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GH_TOKEN"] = self.token
        env["GITHUB_TOKEN"] = self.token
        env["GH_PROMPT_DISABLED"] = "1"
        return env

    def api(self, method: str, path: str, payload: dict | list | None = None, *, allow_404: bool = False):
        cmd = ["gh", "api", "--method", method, path]
        data = None
        if payload is not None:
            cmd.extend(["--input", "-"])
            data = json.dumps(payload)
        proc = subprocess.run(
            cmd,
            input=data,
            capture_output=True,
            text=True,
            env=self._env(),
            check=False,
        )
        if proc.returncode != 0:
            detail = f"{proc.stderr}\n{proc.stdout}".strip()
            if allow_404 and "404" in detail:
                return None
            short = path.split("?", 1)[0]
            raise RuntimeError(f"GitHub API {method} {short} failed: {detail[:500]}")
        if not proc.stdout.strip():
            return None
        return json.loads(proc.stdout)

    def default_branch(self) -> str:
        repo = self.api("GET", f"repos/{self.owner}/{self.repo}")
        name = repo["default_branch"]
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", name):
            raise RuntimeError("unexpected default branch name")
        return name

    def base_head(self) -> str:
        name = self.default_branch()
        ref = self.api("GET", f"repos/{self.owner}/{self.repo}/git/ref/heads/{name}")
        return ref["object"]["sha"]

    def read_file(self, path: str, ref: str) -> str:
        if path not in COMPOSE_PATHS:
            raise ValueError(f"refusing to read {path}")
        query = urllib.parse.urlencode({"ref": ref})
        data = self.api("GET", f"repos/{self.owner}/{self.repo}/contents/{path}?{query}")
        if not data or "content" not in data:
            raise RuntimeError(f"missing {path} at {ref}")
        return base64.b64decode(data["content"]).decode("utf-8")

    def branch_state(self, branch: str) -> BranchState | None:
        assert_bump_branch(branch)
        ref = self.api(
            "GET",
            f"repos/{self.owner}/{self.repo}/git/ref/heads/{branch}",
            allow_404=True,
        )
        if ref is None:
            return None
        sha = ref["object"]["sha"]
        commit = self.api("GET", f"repos/{self.owner}/{self.repo}/git/commits/{sha}")
        parents = commit.get("parents") or []
        parent = parents[0]["sha"] if parents else None
        files = {path: self.read_file(path, sha) for path in COMPOSE_PATHS}
        return BranchState(sha=sha, parent=parent, files=files)

    def commit(self, parent: str, files: dict[str, str], message: str) -> str:
        if any(path not in COMPOSE_PATHS for path in files):
            raise ValueError("refusing to commit a path outside the compose pins")
        parent_commit = self.api("GET", f"repos/{self.owner}/{self.repo}/git/commits/{parent}")
        tree_entries = []
        for path, content in files.items():
            blob = self.api(
                "POST",
                f"repos/{self.owner}/{self.repo}/git/blobs",
                {"content": content, "encoding": "utf-8"},
            )
            tree_entries.append(
                {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )
        tree = self.api(
            "POST",
            f"repos/{self.owner}/{self.repo}/git/trees",
            {"base_tree": parent_commit["tree"]["sha"], "tree": tree_entries},
        )
        commit = self.api(
            "POST",
            f"repos/{self.owner}/{self.repo}/git/commits",
            {"message": message, "tree": tree["sha"], "parents": [parent]},
        )
        return commit["sha"]

    def force_branch(self, branch: str, sha: str) -> None:
        assert_bump_branch(branch)
        ref_path = f"heads/{branch}"
        current = self.api(
            "GET",
            f"repos/{self.owner}/{self.repo}/git/ref/{ref_path}",
            allow_404=True,
        )
        if current is None:
            self.api(
                "POST",
                f"repos/{self.owner}/{self.repo}/git/refs",
                {"ref": f"refs/{ref_path}", "sha": sha},
            )
            return
        self.api(
            "PATCH",
            f"repos/{self.owner}/{self.repo}/git/refs/{ref_path}",
            {"sha": sha, "force": True},
        )

    def _pull(self, data: dict) -> Pull:
        return Pull(
            number=data["number"],
            title=data.get("title") or "",
            body=data.get("body") or "",
            html_url=data["html_url"],
        )

    def find_open_pr(self, branch: str) -> Pull | None:
        assert_bump_branch(branch)
        query = urllib.parse.urlencode(
            {"state": "open", "head": f"{self.owner}:{branch}", "per_page": "5"}
        )
        pulls = self.api("GET", f"repos/{self.owner}/{self.repo}/pulls?{query}") or []
        if not pulls:
            return None
        return self._pull(pulls[0])

    def create_pr(self, branch: str, base: str, title: str, body: str) -> Pull:
        assert_bump_branch(branch)
        try:
            data = self.api(
                "POST",
                f"repos/{self.owner}/{self.repo}/pulls",
                {"title": title, "head": branch, "base": base, "body": body},
            )
        except RuntimeError as exc:
            if "already exists" not in str(exc):
                raise
            existing = self.find_open_pr(branch)
            if existing is None:
                raise
            return self.update_pr(existing.number, title, body)
        return self._pull(data)

    def update_pr(self, number: int, title: str, body: str) -> Pull:
        data = self.api(
            "PATCH",
            f"repos/{self.owner}/{self.repo}/pulls/{number}",
            {"title": title, "body": body},
        )
        return self._pull(data)


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def main() -> int:
    token = os.environ.get("GH_TOKEN", "")
    owner = os.environ.get("PANOPTICON_OWNER", "")
    repo = os.environ.get("PANOPTICON_REPO", "panopticon")
    try:
        client = GhClient(token=token, owner=owner, repo=repo)
        status = run(
            client,
            tag=os.environ.get("PRICING_TAG", ""),
            digest=os.environ.get("PRICING_DIGEST", ""),
            pricing_repository=os.environ.get("PRICING_REPOSITORY", ""),
            dry_run=_truthy(os.environ.get("DRY_RUN", "false")),
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
