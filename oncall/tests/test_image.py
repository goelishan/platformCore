"""
What the image may contain and how it is built, checked from the build files.

  - The base is pinned by tag and digest, and dependencies install only from the hashed
    lock. An unpinned base or an unhashed install is a different image on every build.
  - Credentials and runtime state are excluded from the build context, not merely
    unused: a file in the context can be copied into a layer by any later COPY.
  - The lock covers every runtime requirement, so a dependency added to requirements.txt
    and not relocked fails here instead of at import time in the cluster.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
IGNORED = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
           if line.strip() and not line.startswith("#")}
LOCK = (ROOT / "requirements.lock").read_text()


def test_the_base_image_is_pinned_by_tag_and_a_real_digest():
    ref = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", DOCKERFILE, re.M).group(1)
    tag, _, digest = ref.partition("@")

    assert tag.startswith("python:3.11-slim"), tag
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), ref
    assert digest != "sha256:" + "0" * 64, "placeholder digest: resolve it with imagetools"


def test_dependencies_install_only_from_the_hashed_lock():
    assert "--require-hashes" in DOCKERFILE
    assert "requirements.txt" not in DOCKERFILE


def test_the_image_runs_as_a_non_root_user():
    users = re.findall(r"^USER (\S+)$", DOCKERFILE, re.M)

    assert users and users[-1].split(":")[0] not in {"0", "root"}


def test_credentials_and_state_never_enter_the_build_context():
    assert {".env", ".env.*", "data/", "scratch/", ".venv/"} <= IGNORED


def test_the_lock_covers_every_runtime_requirement():
    wanted = {
        re.split(r"[<>=\[ ]", line.strip(), maxsplit=1)[0].lower()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    } - {"pytest"}
    locked = {m.lower() for m in re.findall(r"^([A-Za-z0-9_.-]+)==", LOCK, re.M)}

    assert wanted <= locked, f"relock: make -C oncall lock; missing {sorted(wanted - locked)}"
    assert "pytest" not in locked
    pinned = re.findall(r"^[A-Za-z0-9_.-]+==", LOCK, re.M)
    hashed = re.findall(r"^[A-Za-z0-9_.-]+==[^\n]*\\\n\s+--hash=sha256:[0-9a-f]{64}", LOCK, re.M)
    assert len(hashed) == len(pinned), "every pinned package needs its hashes"


def test_the_prod_store_guard_finds_the_ca_bundle_the_image_ships():
    assert "ONCALL_STORE_SSLROOTCERT=/etc/ssl/certs/rds-global-bundle.pem" in DOCKERFILE
    assert "/etc/ssl/certs/rds-global-bundle.pem" in DOCKERFILE.split("FROM ${PYTHON_IMAGE}\n")[-1]


def test_source_modes_are_normalised_rather_than_inherited_from_the_builder():
    """Found on the first build: a 0600 checkout made /app/oncall unreadable to UID 10001.
    The runtime stage must take the source from the build stage, after chmod."""
    build, runtime = DOCKERFILE.split("AS build", 1)[1].split("# hadolint ignore=DL3006\nFROM", 1)

    assert "chmod -R a+rX" in build
    assert "COPY --from=build /opt/src/oncall /app/oncall" in runtime
    assert not re.search(r"^COPY \. ", runtime, re.M), "runtime must not copy the context directly"


def test_the_data_directory_is_inside_the_volume_not_its_root():
    """Found on the first kind deploy: bootstrap chmods DATA_DIR to 0700, and only the owner
    may chmod. A volume's root belongs to its provisioner, so DATA_DIR must be a directory
    the agent creates, and the probes must read the heartbeat where the agent writes it."""
    data_dir = re.search(r"ONCALL_DATA_DIR=(\S+)", DOCKERFILE).group(1)
    chart = (ROOT / "deploy" / "chart" / "templates" / "agent.yaml").read_text()
    mount = re.search(r"\{name: data, mountPath: (\S+)\}", chart).group(1)

    assert data_dir.startswith(mount + "/"), (data_dir, mount)
    assert chart.count(f"{data_dir}/heartbeat") == 2
