#!/usr/bin/env python3
"""Print the GHCR manifest digest for an existing hypermesh-pricing tag.

The release job already knows the digest it pushed. workflow_dispatch
uses this when the tag is already in the registry. Docker must be logged
in when the package is private. The digest is the top-level manifest the
tag currently points at.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from bump_panopticon_pin import TAG_RE, parse_imagetools_digest, require_tag

IMAGE = "ghcr.io/fyberlabs/hypermesh-pricing"
_SHA_TAG = re.compile(r"sha-[0-9a-f]{64}")


def pricing_image_ref(image_ref: str) -> str:
    """Accept only this image's main, sha-, or release tag."""
    text = image_ref.strip()
    prefix = f"{IMAGE}:"
    if not text.startswith(prefix):
        raise ValueError("refusing to inspect that image")
    tag = text[len(prefix):]
    if tag != "main" and _SHA_TAG.fullmatch(tag) is None and TAG_RE.fullmatch(tag) is None:
        raise ValueError("refusing to inspect that tag")
    return text


def inspect_ref(image_ref: str) -> str:
    image_ref = pricing_image_ref(image_ref)
    proc = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image_ref],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"could not inspect {image_ref}: {detail[:500]}")
    return parse_imagetools_digest(proc.stdout)


def inspect_digest(tag: str) -> str:
    tag = require_tag(tag)
    return inspect_ref(f"{IMAGE}:{tag}")


def write_output(digest: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"digest={digest}\n")


def main() -> int:
    image_ref = os.environ.get("IMAGE_REF", "").strip()
    try:
        if image_ref:
            # Stdout is only the digest so the image job can compare it.
            print(inspect_ref(image_ref))
            return 0
        digest = inspect_digest(os.environ.get("TAG", ""))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    write_output(digest)
    print(f"resolved {IMAGE}:{os.environ.get('TAG', '').strip()} {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
