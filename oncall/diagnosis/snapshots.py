"""
The exact evidence a diagnosis was made from, kept so it can be replayed.

  - One JSON file per bundle, named by its own evidence receipt. Identical evidence is
    stored once, and the name is a claim the loader checks: a file whose contents no
    longer hash to its name is refused rather than replayed.
  - Replay needs this because the evidence cannot be rebuilt later. The window bounds
    are deliberately not journalled, source health is a fact about the moment it was
    read, and the buffer forgets after its retention period.
  - The same shape as the eval's frozen fixtures, Bundle.model_dump(mode="json"), so
    replay and the eval share one loader.
  - Under DATA_DIR, owner-only like the buffer and the blobs, and excluded from git and
    the image. Local disk is a development home; a deployed agent needs object storage.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from oncall import config
from oncall.evidence.bundle import Bundle, evidence_receipt


class SnapshotMismatch(ValueError):
    """The file's contents do not hash to the receipt it is filed under."""


def directory() -> Path:
    # Resolved at call time, not import time, so tests that move DATA_DIR move this too.
    return config.DATA_DIR / "bundles"


def path_for(bundle_sha256: str) -> Path:
    return directory() / f"{bundle_sha256}.json"


def save(bundle: Bundle) -> Path:
    sha = evidence_receipt(bundle)
    target = path_for(sha)
    if target.exists():
        return target

    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(bundle.model_dump(mode="json"), sort_keys=True).encode()
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def load_file(path: Path) -> Bundle:
    return Bundle.model_validate(json.loads(path.read_text()))


def load(bundle_sha256: str) -> Bundle:
    path = path_for(bundle_sha256)
    if not path.exists():
        raise FileNotFoundError(f"no snapshot for bundle {bundle_sha256} at {path}")
    bundle = load_file(path)
    actual = evidence_receipt(bundle)
    if actual != bundle_sha256:
        raise SnapshotMismatch(f"{path} hashes to {actual}, not {bundle_sha256}")
    return bundle
