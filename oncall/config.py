"""
Runtime configuration for the on-call agent.

  - Single home for paths, retention policy and endpoints; collectors read it, never
    redefine it. Env overrides let tests redirect storage without touching real data.
  - Storage is two-tier. The buffer is a local SQLite file that every write lands in
    first and that never depends on the network. The store is Postgres, durable and
    queryable, reached only by the shipper. The agent diagnoses broken clusters, so
    the write path must not fail when the cluster does.
  - The two tiers keep data for different reasons and therefore for different lengths
    of time. The buffer holds enough to survive a store outage; the store holds enough
    to answer "has this ever happened before".
"""

from __future__ import annotations
import os
from datetime import timedelta
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent

DATA_DIR = Path(os.getenv("ONCALL_DATA_DIR", PACKAGE_ROOT / "data"))
BUFFER_DB_PATH = DATA_DIR / "buffer.db"
BLOB_DIR = DATA_DIR / "blobs"

CLUSTER_NAME = os.getenv("ONCALL_CLUSTER", "platformcore")


# ---- the store -------------------------------------------------------------
# Assembled from parts rather than taking a single DSN string, because the parts are
# what changes between environments: dev points at the compose container, production
# points at RDS with an IAM-issued token in place of a password. Moving to RDS is then
# a change of environment variables, not of code.
#
# A whole DSN can still be supplied directly, which is what a Kubernetes Secret or an
# operator-provisioned database will hand over.


STORE_DSN = os.getenv("ONCALL_STORE_DSN") or (
    "host={host} port={port} dbname={db} user={user} password={pw} sslmode={ssl}".format(
        host=os.getenv("ONCALL_STORE_HOST", "localhost"),
        port=os.getenv("ONCALL_STORE_PORT", "5433"),
        db=os.getenv("ONCALL_STORE_DB", "oncall"),
        user=os.getenv("ONCALL_STORE_USER", "oncall"),
        pw=os.getenv("ONCALL_STORE_PASSWORD", "oncall"),
        ssl=os.getenv("ONCALL_STORE_SSLMODE", "prefer"),
    )
)

# Small on purpose. One shipper, one UI, one background collector — a large pool would
# only hide a leak, and every idle connection costs the server a backend process.
STORE_POOL_MIN = int(os.getenv("ONCALL_STORE_POOL_MIN", "1"))
STORE_POOL_MAX = int(os.getenv("ONCALL_STORE_POOL_MAX", "4"))

# Bounded so a wedged store surfaces as unavailable within one shipping cycle rather
# than hanging the shipper indefinitely, which would look exactly like an idle shipper.
STORE_CONNECT_TIMEOUT = int(os.getenv("ONCALL_STORE_CONNECT_TIMEOUT", "5"))


# ---- retention: the store --------------------------------------------------
# Value decays at different rates. Pod state is cheap and useful for trend; raw log
# excerpts are bulky and stale within a day; deploy records are tiny and are the first
# thing wanted in a postmortem. Signals attached to an incident are exempt entirely —
# they are the eval corpus.
#
# expires_at is computed from this at write time and travels with the row into the
# store. The buffer does not use it: a row can be long-lived in the store and still be
# dropped from the buffer the moment it has shipped.


STORE_RETENTION = {
    "pod_state": timedelta(days=7),
    "event": timedelta(days=7),
    "log_excerpt": timedelta(hours=24),
    "metric": timedelta(days=7),
    "deploy": timedelta(days=30),
}

STORE_RETENTION_DEFAULT = timedelta(days=7)


# ---- retention: the buffer -------------------------------------------------
# Two bounds, because they protect different things.
#
# The age bound is the policy: how long an outage the agent can absorb without losing
# history. Forty-eight hours covers a weekend.
#
# The size bound is the backstop: the agent runs inside the cluster it diagnoses, so a
# buffer that fills the volume takes the agent down during the incident it exists to
# explain. When the backstop fires it drops the oldest rows and records that it did.
# Silent loss would read downstream as a quiet period, which is the one failure this
# project is organised against.


BUFFER_RETENTION = timedelta(
    hours=int(os.getenv("ONCALL_BUFFER_RETENTION_HOURS", "48"))
)

BUFFER_MAX_BYTES = int(os.getenv("ONCALL_BUFFER_MAX_BYTES", str(512 * 1024 * 1024)))


# ---- retention: blobs ------------------------------------------------------
# Raw payloads on local disk beside the buffer, so they get the same pair of bounds and
# the same division of labour: age is the policy, size is the backstop.
#
# The age matches BUFFER_RETENTION rather than the store's 24-hour log_excerpt window.
# A blob is the unabridged version of an excerpt that is still in the buffer, and
# expiring it first would leave a live row pointing at nothing for a day.
#
# Together the two ceilings are the volume budget: 512 MiB of rows plus 512 MiB of
# blobs is what the PVC has to be able to give up without the agent dying mid-incident.
#
# The grace window exists because put() writes the file before the row, so every
# in-flight write looks momentarily like an orphan. Without it the sweep would delete
# blobs out from under a collector that is still committing them.


BLOB_RETENTION = timedelta(hours=int(os.getenv("ONCALL_BLOB_RETENTION_HOURS", "48")))

BLOB_MAX_BYTES = int(os.getenv("ONCALL_BLOB_MAX_BYTES", str(512 * 1024 * 1024)))

BLOB_ORPHAN_GRACE = timedelta(
    minutes=int(os.getenv("ONCALL_BLOB_ORPHAN_GRACE_MINUTES", "15"))
)


# ---- collection scope ------------------------------------------------------
# An empty allowlist means every namespace. The agent's own namespace is always
# excluded: without it the agent restarting emits a signal about itself, which
# triggers log collection about itself, which produces more signals.


NAMESPACES = [ns for ns in os.getenv("ONCALL_NAMESPACES", "").split(",") if ns]

EXCLUDE_NAMESPACES = {"oncall", "kube-system"}


# ---- cadence, seconds ------------------------------------------------------
# Events are garbage-collected by the API server after roughly an hour, so a missed
# poll loses them permanently. Pod state is a snapshot that can always be re-read, so
# it is polled less aggressively.


POLL_INTERVALS = {
    "k8s_pods": 60,
    "k8s_events": 30,
    # Slowest of the three, and last in the cycle. It reads what the other two wrote,
    # so it is always one cycle behind them — see LOG_LOOKBACK_SECONDS.
    "k8s_logs": 120,
}


# ---- log collection --------------------------------------------------------
# Signal-driven, not a sweep. Cost scales with the number of broken pods rather than
# with the size of the cluster, and log bytes are the most expensive thing this agent
# handles. The trigger is also where volume is actually controlled: no per-fetch bound
# helps if the collector is asking two hundred pods for their logs.
#
# WARNING is included deliberately. It is the severity k8s_pods assigns to a container
# that is running now and was killed earlier — ready, Ready condition true, every live
# indicator green — and the previous container's log is the only evidence that anything
# happened at all. Dropping the floor to ERROR would skip precisely that case.


LOG_TRIGGER_SEVERITIES = ("warning", "error", "critical")

# How far back to look for triggers. Must exceed the log collector's own interval plus
# the interval of the sources feeding it: the trigger was written by a cycle that has
# already finished, so a window equal to one interval would miss every signal written
# in the gap between the two runs.
LOG_LOOKBACK_SECONDS = int(os.getenv("ONCALL_LOG_LOOKBACK_SECONDS", "600"))

# Ceiling on targets per cycle. A cluster-wide failure produces hundreds of broken pods
# at once, and asking the API server to proxy hundreds of log streams during an outage
# is a self-inflicted second incident. When the cap bites, the run records how many
# targets it declined — a silent cap reads downstream as "that was everything".
LOG_MAX_TARGETS = int(os.getenv("ONCALL_LOG_MAX_TARGETS", "40"))

# The window is chosen by time, not by line count. tailLines returns a span of unknown
# duration — three seconds on a chatty pod, three days on a quiet one — and an excerpt
# whose interval is unknown cannot be joined against anything, which is the only thing
# this agent does with evidence.
LOG_SINCE_SECONDS = int(os.getenv("ONCALL_LOG_SINCE_SECONDS", "600"))

# And the byte cap is the backstop, not a competitor: since_seconds bounds *time* and
# has no upper bound on volume at all — a container logging ten thousand lines a second
# for ten minutes is millions of lines. Policy plus backstop, the same pair as the
# buffer's age and size bounds.
LOG_LIMIT_BYTES = int(os.getenv("ONCALL_LOG_LIMIT_BYTES", str(1024 * 1024)))

# What reaches the payload, and therefore the M5 prompt. The full fetch always reaches
# the blob, so this is a prompt-budget decision rather than a retention one, and it is
# expected to move once there is a reasoner to measure it against.
LOG_EXCERPT_LINES = int(os.getenv("ONCALL_LOG_EXCERPT_LINES", "60"))
LOG_EXCERPT_BYTES = int(os.getenv("ONCALL_LOG_EXCERPT_BYTES", str(8 * 1024)))

# Shipping is deliberately slower than collection. Batching is what lets a row that was
# upserted several times between cycles cross the wire once, and nothing downstream
# reads the store on the critical path of a diagnosis.
SHIP_INTERVAL = int(os.getenv("ONCALL_SHIP_INTERVAL", "120"))

# Caps one shipping cycle so a large backlog drains over several bounded transactions
# rather than one long one holding a write lock on the buffer.
SHIP_BATCH_SIZE = int(os.getenv("ONCALL_SHIP_BATCH_SIZE", "1000"))
