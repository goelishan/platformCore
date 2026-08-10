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