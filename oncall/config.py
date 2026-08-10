"""
Runtime configuration for the on-call agent.

  - Single home for paths and retention policy; collectors read it, never redefine it.
  - Env overrides let tests redirect the landing zone without touching real data.
"""

from __future__ import annotations
import os
from datetime import timedelta
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent

DATA_DIR = Path(os.getenv("ONCALL_DATA_DIR", PACKAGE_ROOT / "data"))
DB_PATH = DATA_DIR/"oncall.db"
BLOB_DIR = DATA_DIR/"blobs"

CLUSTER_NAME = os.getenv("ONCALL_CLUSTER","platformcore")

# ---- retention -------------------------------------------------------------
# Diverges by kind because value decays at different rates. Pod state is cheap and
# useful for trend; raw log excerpts are bulky and stale within a day. Signals
# attached to an incident are exempt entirely — they are the eval corpus.


RETENTION = {
    "pod_state": timedelta(days=7),
    "event": timedelta(days=7),
    "log_excerpt": timedelta(hours=24),
    "metric": timedelta(days=7),
    "deploy": timedelta(days=30)
}


RETENTION_DEFAULT=timedelta(days=7)


# ---- collection scope ------------------------------------------------------
# An empty allowlist means every namespace. The agent's own namespace is always
# excluded: without it the agent restarting emits a signal about itself, which
# triggers log collection about itself, which produces more signals.


NAMESPACES = [ns for ns in os.getenv("ONCALL_NAMESPACES", "").split(",") if ns]

EXCLUDE_NAMESPACES = {"oncall", "kube-system"}


# ---- poll cadence, seconds -------------------------------------------------
# Events are garbage-collected by the API server after roughly an hour, so a
# missed poll loses them permanently. Pod state is a snapshot that can always be
# re-read, so it is polled less aggressively.


POLL_INTERVALS = {
    "k8s_pods": 60,
    "k8s_events": 30,
}