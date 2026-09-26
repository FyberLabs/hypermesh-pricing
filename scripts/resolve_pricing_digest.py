#!/usr/bin/env python3
"""Print the GHCR manifest digest for an existing hypermesh-pricing tag.

The release job already knows the digest it pushed. workflow_dispatch
uses this when the tag is already in the registry. Docker must be logged
in when the package is private. The digest is the top-level manifest the
tag currently points at.
"""

from __future__ import annotations

import os
import subprocess
import sys

from bump_panopticon_pin import parse_imagetools_digest, require_tag

IMAGE = "ghcr.io/fyberlabs/hypermesh-pricing"


def inspect_digest(tag: str) -> str:
    tag = require_tag(tag)
    image = f"{IMAGE}:{tag}"
    proc = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"could not inspect {image}: {detail[:500]}")
    return parse_imagetools_digest(proc.stdout)


def write_output(digest: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"digest={digest}\n")


def main() -> int:
    try:
        digest = inspect_digest(os.environ.get("TAG", ""))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    write_output(digest)
    print(f"resolved {IMAGE}:{os.environ.get('TAG', '').strip()} {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
