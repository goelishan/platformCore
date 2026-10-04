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


# ---- environment file ------------------------------------------------------
# oncall/.env is where dev credentials live, and the only place they live: nothing in
# code carries a working password. Read before anything below so its values act as
# overrides of the defaults. Three limits keep it safe to leave enabled everywhere:
# only ONCALL_* keys are read, so AWS credentials can never arrive through a file; the
# real environment always wins; and a missing file is simply nothing to read.


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("ONCALL_"):
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


_load_env_file(Path(os.getenv("ONCALL_ENV_FILE", str(PACKAGE_ROOT / ".env"))))

# dev or prod. prod refuses to start with anything that would work in dev and be
# unsafe in production: see _production_problems at the end of this module.
ENV = os.getenv("ONCALL_ENV", "dev")

DATA_DIR = Path(os.getenv("ONCALL_DATA_DIR", PACKAGE_ROOT / "data"))
BUFFER_DB_PATH = DATA_DIR / "buffer.db"
BLOB_DIR = DATA_DIR / "blobs"

CLUSTER_NAME = os.getenv("ONCALL_CLUSTER", "platformcore")

# The kubeconfig context to read, when not running inside the cluster. Required: a
# collector that silently used whatever context kubectl last switched to could read a
# production cluster from a laptop. In the cluster the service account is used and
# this is ignored.
KUBE_CONTEXT = os.getenv("ONCALL_KUBE_CONTEXT") or None

# Region for every AWS call the agent makes: RDS IAM tokens and Bedrock.
AWS_REGION = (
    os.getenv("ONCALL_AWS_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
)


# ---- the store -------------------------------------------------------------
# Assembled from parts, because the parts are what change between environments: dev
# points at the compose container, production at RDS with an IAM token per connection.
#
# The password is never part of the DSN. It is passed to the pool separately, or, with
# IAM auth, generated fresh for every connection, so no DSN string anywhere carries a
# credential that a log line or a repr could expose. There is no default password and
# the default sslmode is verify-full: code fails closed, and dev loosens it in .env.
#
# A whole DSN can still be supplied, which is what an operator-provisioned database
# hands over. It is used as given.

STORE_HOST = os.getenv("ONCALL_STORE_HOST", "localhost")
STORE_PORT = int(os.getenv("ONCALL_STORE_PORT", "5432"))
STORE_DB = os.getenv("ONCALL_STORE_DB", "oncall")
STORE_USER = os.getenv("ONCALL_STORE_USER", "oncall")
STORE_PASSWORD = os.getenv("ONCALL_STORE_PASSWORD") or None
STORE_SSLMODE = os.getenv("ONCALL_STORE_SSLMODE", "verify-full")
STORE_SSLROOTCERT = os.getenv("ONCALL_STORE_SSLROOTCERT") or None
STORE_IAM_AUTH = os.getenv("ONCALL_STORE_IAM_AUTH", "false").lower() in {"1", "true", "yes"}

STORE_DSN = os.getenv("ONCALL_STORE_DSN") or " ".join(
    f"{key}={value}"
    for key, value in (
        ("host", STORE_HOST),
        ("port", STORE_PORT),
        ("dbname", STORE_DB),
        ("user", STORE_USER),
        ("sslmode", STORE_SSLMODE),
        ("sslrootcert", STORE_SSLROOTCERT),
    )
    if value
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
    # A snapshot like pod state, and just as re-readable. Same interval so a Service
    # and the pods behind it are observed close enough together to be read as one
    # moment when the assembler joins them.
    "k8s_services": 60,
    # Slower than the rest. Node conditions change on the order of minutes when they
    # change at all, and a tighter interval buys nothing while costing a list of
    # every node in the cluster.
    "k8s_nodes": 120,
    "k8s_events": 30,
    # Slowest of the three, and last in the cycle. It reads what the other two wrote,
    # so it is always one cycle behind them — see LOG_LOOKBACK_SECONDS.
    "k8s_logs": 120,
}

# Sources whose signals choose which pods k8s_logs reads. Here rather than in the
# collector because the assembler has to know it too, and importing a collector would
# drag the kubernetes client into a layer that must never touch the cluster. One list,
# so an upstream added for the collector cannot be missed by the coverage check.
LOG_UPSTREAM_SOURCES = ("k8s_pods", "k8s_events")


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


# ---- assembler --------------------------------------------------------------
# What one incident's evidence window starts as, before anything is known about the
# evidence. Wide enough to hold the pre-incident history that usually contains the
# cause, and narrow enough that a busy namespace does not bury the subject.
BUNDLE_LOOKBACK_SECONDS = int(os.getenv("ONCALL_BUNDLE_LOOKBACK_SECONDS", "3600"))

# How far back the window may be extended once the evidence has been read. Extension
# exists so a problem that started before the alert is described from its beginning;
# the cap exists because the buffer holds two days and a window reaching past that
# silently becomes a window over whatever survived retention.
BUNDLE_MAX_LOOKBACK_SECONDS = int(os.getenv("ONCALL_BUNDLE_MAX_LOOKBACK_SECONDS", "21600"))

# How many findings may enter a bundle. Everything above this is recorded as an
# omission rather than dropped, because a bundle that silently omits reads as complete.
BUNDLE_MAX_FINDINGS = int(os.getenv("ONCALL_BUNDLE_MAX_FINDINGS", "40"))

# Unabridged log text the bundle may carry, in total. The excerpt is lossy by
# construction and the blob is not, so this buys back the loss where it matters most —
# and it is a prompt budget, not a storage one: the bytes are already on disk.
BUNDLE_PROMOTE_BYTES = int(os.getenv("ONCALL_BUNDLE_PROMOTE_BYTES", str(16 * 1024)))

# Hard ceiling on the rendered bundle, in bytes. A model's context is what actually
# limits the prompt, so findings are dropped lowest rank first until the render fits,
# and each one dropped is listed in omitted. Roughly 30k tokens at the default, well
# inside any current Bedrock Claude model and leaving room for instructions and answer.
PROMPT_MAX_BYTES = int(os.getenv("ONCALL_PROMPT_MAX_BYTES", str(120 * 1024)))


# ---- the model: Amazon Bedrock ---------------------------------------------
# No key, token or secret variable exists here, and none should be added. Credentials
# come from the default AWS chain: the service account's IRSA role in the cluster, an
# SSO session or named profile on a laptop. The role needs bedrock:InvokeModel on this
# one model or inference profile ARN and nothing else.

BEDROCK_REGION = os.getenv("ONCALL_BEDROCK_REGION") or AWS_REGION

# An inference profile ID or ARN, not a bare model ID: current Claude models on Bedrock
# are invoked through inference profiles. Unset until M5 needs it, and required then.
BEDROCK_MODEL_ID = os.getenv("ONCALL_BEDROCK_MODEL_ID") or None

# A Bedrock guardrail, optional. A second layer of PII and secret filtering on top of
# this agent's own redaction, never a replacement for it.
BEDROCK_GUARDRAIL_ID = os.getenv("ONCALL_BEDROCK_GUARDRAIL_ID") or None
BEDROCK_GUARDRAIL_VERSION = os.getenv("ONCALL_BEDROCK_GUARDRAIL_VERSION") or None

# Bounded so a slow model surfaces as a failed diagnosis inside the incident, not as a
# UI that hangs while someone waits at 3am.
BEDROCK_CONNECT_TIMEOUT = int(os.getenv("ONCALL_BEDROCK_CONNECT_TIMEOUT", "5"))
BEDROCK_READ_TIMEOUT = int(os.getenv("ONCALL_BEDROCK_READ_TIMEOUT", "90"))
BEDROCK_MAX_ATTEMPTS = int(os.getenv("ONCALL_BEDROCK_MAX_ATTEMPTS", "3"))
BEDROCK_MAX_TOKENS = int(os.getenv("ONCALL_BEDROCK_MAX_TOKENS", "2000"))


# ---- production guard ------------------------------------------------------
# Every setting above has a value that is fine on a laptop and unsafe in production.
# In prod the module refuses to import with any of them, so the failure is a crash at
# startup with the reason, not a quiet downgrade discovered after an incident.


def _production_problems() -> list[str]:
    problems: list[str] = []
    dsn_given = bool(os.getenv("ONCALL_STORE_DSN"))

    if dsn_given:
        if "sslmode=verify-full" not in STORE_DSN:
            problems.append("ONCALL_STORE_DSN must carry sslmode=verify-full")
    else:
        if not os.getenv("ONCALL_STORE_HOST"):
            problems.append("ONCALL_STORE_HOST must be set explicitly")
        if STORE_SSLMODE != "verify-full":
            problems.append(f"ONCALL_STORE_SSLMODE is {STORE_SSLMODE}, must be verify-full")
        if not STORE_SSLROOTCERT:
            problems.append("ONCALL_STORE_SSLROOTCERT must point at the RDS CA bundle")
        if not STORE_IAM_AUTH:
            problems.append("ONCALL_STORE_IAM_AUTH must be true; no static password in prod")
        if STORE_PASSWORD:
            problems.append("ONCALL_STORE_PASSWORD must not be set in prod")
    if not AWS_REGION:
        problems.append("ONCALL_AWS_REGION (or AWS_REGION) must be set")
    return problems


if ENV == "prod":
    _problems = _production_problems()
    if _problems:
        raise RuntimeError("refusing to start in prod: " + "; ".join(_problems))
elif ENV != "dev":
    raise RuntimeError(f"ONCALL_ENV must be dev or prod, not {ENV!r}")
