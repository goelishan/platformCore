"""
Prometheus exporter over the diagnosis journal.

  - Reads the store's diagnoses table on every scrape and exposes totals. The journal
    is the record; this module only aggregates it, so nothing here can disagree with
    what the eval and the UI read.
  - Counters are real counters. diagnoses is not partitioned and retention never
    touches it, so each total only grows, and an exporter restart resumes at the
    same values instead of resetting to zero.
  - An unreachable store is exposed as oncall_journal_up 0 with every journal series
    absent. Emitting zeros would read as "no diagnoses failed" when the truth is
    "nothing could be counted".
  - Every outcome is emitted for every (model, prompt_version) seen, at zero when none
    occurred, and so is every series for the configured model and the current prompt
    version before any row exists. increase() ignores a series' first sample, so a
    series that first appears at 1 hides the diagnosis that created it.
  - oncall_store_login_up opens a new connection on every scrape, outside the pool.
    journal_up reads through pooled sessions, which survive a revoked login; drill D3
    (2026-10-13) revoked it and journal_up stayed 1 while every new connection failed.
    The two gauges answer different questions: can the journal be read now, and will
    the next reconnect succeed.
  - Totals move when a row reaches the store, not when the diagnosis was made. A
    diagnosis journalled while Postgres was down arrives with the next shipping cycle
    and counts then.

    make -C oncall exporter            # serves :9464/metrics
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Iterable, Iterator
from typing import Any

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector

from oncall import config
from oncall.diagnosis.prompt import PROMPT_VERSION
from oncall.diagnosis.schema import Outcome
from oncall.store import connection

log = logging.getLogger(__name__)

DEFAULT_PORT = 9464

# Seconds. Shaped around what has been measured: p50 about 16 s and p95 about 34 s from
# a laptop, one repair roughly doubling a call, and a 90 s read timeout per attempt.
LATENCY_BUCKETS: tuple[float, ...] = (5, 10, 15, 20, 25, 30, 45, 60, 90, 120, 180, 300)

# Kept below Prometheus' default 10 s scrape timeout, so a slow store yields
# oncall_journal_up 0 rather than a scrape the server abandons with no explanation.
STATEMENT_TIMEOUT_MS = 5000

# prompt_too_large sends nothing and journals latency 0. Counted as a diagnosis, left
# out of latency, tokens and cost, which describe calls that were made.
_SENT = "attempts > 0"



#--------------------------------------------------------------------------------------------------------
# QUERIES
#--------------------------------------------------------------------------------------------------------
#
# One aggregate per family over the whole table. The journal holds one row per diagnose
# call, so a full scan stays cheap at any volume this agent produces.



_OUTCOMES_SQL = """
SELECT coalesce(model, '') AS model,
       coalesce(prompt_version, '') AS prompt_version,
       coalesce(outcome, '') AS outcome,
       count(*) AS n,
       count(*) FILTER (WHERE outcome = 'ok' AND attempts = 1) AS first_attempt_ok,
       count(*) FILTER (WHERE insufficient_evidence) AS insufficient,
       coalesce(sum(attempts), 0) AS attempts
FROM diagnoses
GROUP BY 1, 2, 3
"""

_CALLS_SQL = f"""
SELECT coalesce(model, '') AS model,
       count(*) AS n,
       coalesce(sum(latency_ms), 0) AS latency_ms,
       coalesce(sum(input_tokens), 0) AS input_tokens,
       coalesce(sum(output_tokens), 0) AS output_tokens,
       coalesce(sum(cost_usd), 0) AS cost_usd,
       count(*) FILTER (WHERE cost_usd IS NULL) AS unpriced,
       extract(epoch FROM max(created_at)) AS last_ts,
       {", ".join(
           f"count(*) FILTER (WHERE latency_ms <= {int(b * 1000)}) AS le_{i}"
           for i, b in enumerate(LATENCY_BUCKETS)
       )}
FROM diagnoses
WHERE {_SENT}
GROUP BY 1
"""



# Evaluations are gauges, not counters. Each (diagnosis, evaluator) row is replaced when
# re-evaluated, so a verdict can move between labels (persisted to resolved, or an
# engineer changing their mind), and a counter that went down would read as a reset.
_EVALUATIONS_SQL = """
SELECT coalesce(d.model, '') AS model,
       coalesce(d.prompt_version, '') AS prompt_version,
       e.evaluator, e.verdict, count(*) AS n
FROM evaluations e JOIN diagnoses d USING (diagnosis_id)
GROUP BY 1, 2, 3, 4
"""

_FLAGS_SQL = """
SELECT coalesce(d.model, '') AS model,
       coalesce(d.prompt_version, '') AS prompt_version,
       flag, count(*) AS n
FROM evaluations e JOIN diagnoses d USING (diagnosis_id),
     jsonb_array_elements_text(coalesce(e.details -> 'flags', '[]'::jsonb)) AS flag
WHERE e.evaluator = 'checks'
GROUP BY 1, 2, 3
"""

_HUMAN_SQL = """
SELECT coalesce(d.model, '') AS model,
       coalesce(d.prompt_version, '') AS prompt_version,
       count(*) FILTER (WHERE e.details ->> 'top1_match' IS NOT NULL) AS labelled,
       count(*) FILTER (WHERE (e.details ->> 'top1_match')::boolean) AS top1
FROM evaluations e JOIN diagnoses d USING (diagnosis_id)
WHERE e.evaluator = 'human'
GROUP BY 1, 2
"""

_UNCHECKED_SQL = """
SELECT count(*) AS n FROM diagnoses d
WHERE NOT EXISTS (SELECT 1 FROM evaluations e
                  WHERE e.diagnosis_id = d.diagnosis_id AND e.evaluator = 'checks')
"""



#--------------------------------------------------------------------------------------------------------
# COLLECTOR
#--------------------------------------------------------------------------------------------------------



class JournalCollector(Collector):
    """Runs both queries per scrape. A failed read yields only the health gauges."""

    def collect(self) -> Iterator[Metric]:
        yield from login_families()
        started = time.monotonic()
        try:
            outcomes, calls = self._read()
        except Exception as exc:  # noqa: BLE001 - any failure is reported, never raised
            log.warning("journal read failed: %s", exc)
            yield _gauge("oncall_journal_up", "1 when the journal was read on this scrape.", 0)
            yield _duration(started)
            return

        yield _gauge("oncall_journal_up", "1 when the journal was read on this scrape.", 1)
        yield _duration(started)
        yield from outcome_families(outcomes, seed=_seed())
        yield from call_families(calls, seed=_seed())
        try:
            yield from evaluation_families(*self._read_evaluations(), seed=_seed())
        except Exception as exc:  # noqa: BLE001 - a store before 0008 still exports the rest
            log.warning("evaluations not read: %s", exc)

    def _read(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with connection.connect() as conn:
            conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            return (
                connection.fetch_all(conn, _OUTCOMES_SQL),
                connection.fetch_all(conn, _CALLS_SQL),
            )

    def _read_evaluations(self) -> tuple[list, list, list, int]:
        with connection.connect() as conn:
            conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            unchecked = connection.fetch_one(conn, _UNCHECKED_SQL)
            return (
                connection.fetch_all(conn, _EVALUATIONS_SQL),
                connection.fetch_all(conn, _FLAGS_SQL),
                connection.fetch_all(conn, _HUMAN_SQL),
                int(unchecked["n"]) if unchecked else 0,
            )


def login_families() -> Iterator[Metric]:
    """A fresh login, reported whether or not the journal read below succeeds."""
    started = time.monotonic()
    try:
        connection.fresh_login()
        up = 1
    except Exception as exc:  # noqa: BLE001 - any failure is reported, never raised
        log.warning("fresh store login failed: %s", exc)
        up = 0
    yield _gauge("oncall_store_login_up",
                 "1 when a new connection could authenticate to the store on this scrape.", up)
    yield _gauge("oncall_store_login_duration_seconds",
                 "Time spent opening and authenticating that connection.",
                 time.monotonic() - started)


def _seed() -> tuple[str, str] | None:
    """The (model, prompt_version) the next diagnosis will carry, if a model is set."""
    return (config.BEDROCK_MODEL_ID, PROMPT_VERSION) if config.BEDROCK_MODEL_ID else None


def outcome_families(
    rows: Iterable[dict[str, Any]], seed: tuple[str, str] | None = None
) -> list[Metric]:
    diagnoses = CounterMetricFamily(
        "oncall_diagnoses",
        "Diagnose calls journalled, by outcome.",
        labels=["model", "prompt_version", "outcome"],
    )
    first_ok = CounterMetricFamily(
        "oncall_diagnoses_first_attempt_ok",
        "Diagnoses valid on the first attempt, with no repair.",
        labels=["model", "prompt_version"],
    )
    insufficient = CounterMetricFamily(
        "oncall_diagnoses_insufficient_evidence",
        "Diagnoses that declined to name a cause.",
        labels=["model", "prompt_version"],
    )
    attempts = CounterMetricFamily(
        "oncall_diagnosis_attempts",
        "Model calls made, repairs included.",
        labels=["model", "prompt_version"],
    )

    by_outcome: dict[tuple[str, str], dict[str, int]] = {}
    per_version: dict[tuple[str, str], list[int]] = {}
    if seed:
        by_outcome[seed] = {}
        per_version[seed] = [0, 0, 0]
    for r in rows:
        key = (r["model"], r["prompt_version"])
        by_outcome.setdefault(key, {})[r["outcome"]] = int(r["n"])
        totals = per_version.setdefault(key, [0, 0, 0])
        totals[0] += int(r["first_attempt_ok"])
        totals[1] += int(r["insufficient"])
        totals[2] += int(r["attempts"])

    for key in sorted(by_outcome):
        seen = by_outcome[key]
        for outcome in sorted({o.value for o in Outcome} | set(seen)):
            diagnoses.add_metric([*key, outcome], seen.get(outcome, 0))
        ok, declined, made = per_version[key]
        first_ok.add_metric(list(key), ok)
        insufficient.add_metric(list(key), declined)
        attempts.add_metric(list(key), made)

    return [diagnoses, first_ok, insufficient, attempts]


def call_families(
    rows: Iterable[dict[str, Any]], seed: tuple[str, str] | None = None
) -> list[Metric]:
    latency = HistogramMetricFamily(
        "oncall_diagnosis_latency_seconds",
        "Wall clock per diagnosis as the agent measured it, repairs included.",
        labels=["model"],
    )
    tokens = CounterMetricFamily(
        "oncall_diagnosis_tokens", "Tokens billed, by direction.", labels=["model", "direction"]
    )
    cost = CounterMetricFamily(
        "oncall_diagnosis_cost_usd",
        "List-price cost of diagnoses that had a configured price.",
        labels=["model"],
    )
    unpriced = CounterMetricFamily(
        "oncall_diagnoses_unpriced",
        "Diagnoses with no configured price, absent from the cost total.",
        labels=["model"],
    )
    last = GaugeMetricFamily(
        "oncall_diagnosis_last_timestamp_seconds",
        "created_at of the newest diagnosis that reached the store.",
        labels=["model"],
    )

    rows = list(rows)
    if seed and seed[0] not in {r["model"] for r in rows}:
        rows.append(_empty_calls(seed[0]))

    for r in sorted(rows, key=lambda r: r["model"]):
        model = [r["model"]]
        buckets = [(_le(b), int(r[f"le_{i}"])) for i, b in enumerate(LATENCY_BUCKETS)]
        buckets.append(("+Inf", int(r["n"])))
        latency.add_metric(model, buckets, sum_value=int(r["latency_ms"]) / 1000)
        tokens.add_metric([r["model"], "input"], int(r["input_tokens"]))
        tokens.add_metric([r["model"], "output"], int(r["output_tokens"]))
        cost.add_metric(model, float(r["cost_usd"]))
        unpriced.add_metric(model, int(r["unpriced"]))
        if r["last_ts"] is not None:
            last.add_metric(model, float(r["last_ts"]))

    return [latency, tokens, cost, unpriced, last]


# Every verdict each evaluator can give, so the seed below can emit them all at zero.
VERDICTS = {
    "checks": ("pass", "warn", "fail"),
    "outcome": ("resolved", "persisted"),
    "human": ("correct", "partial", "wrong"),
}


def evaluation_families(evaluations: Iterable[dict[str, Any]], flags: Iterable[dict[str, Any]],
                        human: Iterable[dict[str, Any]], unchecked: int,
                        seed: tuple[str, str] | None = None) -> list[Metric]:
    """Seeded like the outcome families: every verdict and flag for the configured model
    and current prompt exists at zero before the first row, so the first wrong verdict is
    a change from 0 rather than a series that appears from nowhere."""
    from oncall.eval.online import FLAGS

    verdicts = GaugeMetricFamily(
        "oncall_evaluations",
        "Diagnoses by evaluator and current verdict (checks, outcome, human).",
        labels=["model", "prompt_version", "evaluator", "verdict"],
    )
    flagged = GaugeMetricFamily(
        "oncall_evaluation_flags",
        "Diagnoses whose automatic checks raised each flag.",
        labels=["model", "prompt_version", "flag"],
    )
    labelled = GaugeMetricFamily(
        "oncall_human_labelled",
        "Diagnoses an engineer gave a cause for.",
        labels=["model", "prompt_version"],
    )
    top1 = GaugeMetricFamily(
        "oncall_human_top1_matches",
        "Of those, diagnoses whose rank-1 cause matched the engineer's.",
        labels=["model", "prompt_version"],
    )
    by_verdict = {(r["model"], r["prompt_version"], r["evaluator"], r["verdict"]): int(r["n"])
                  for r in evaluations}
    by_flag = {(r["model"], r["prompt_version"], r["flag"]): int(r["n"]) for r in flags}
    by_human = {(r["model"], r["prompt_version"]): (int(r["labelled"]), int(r["top1"]))
                for r in human}
    if seed:
        for evaluator, names in VERDICTS.items():
            for verdict in names:
                by_verdict.setdefault((*seed, evaluator, verdict), 0)
        for flag in FLAGS:
            by_flag.setdefault((*seed, flag), 0)
        by_human.setdefault(seed, (0, 0))

    for key in sorted(by_verdict):
        verdicts.add_metric(list(key), by_verdict[key])
    for key in sorted(by_flag):
        flagged.add_metric(list(key), by_flag[key])
    for key in sorted(by_human):
        labelled.add_metric(list(key), by_human[key][0])
        top1.add_metric(list(key), by_human[key][1])
    pending = _gauge("oncall_evaluation_backlog",
                     "Diagnoses in the store with no automatic checks yet.", unchecked)
    return [verdicts, flagged, labelled, top1, pending]


def _empty_calls(model: str) -> dict[str, Any]:
    row: dict[str, Any] = {"model": model, "n": 0, "latency_ms": 0, "input_tokens": 0,
                           "output_tokens": 0, "cost_usd": 0, "unpriced": 0, "last_ts": None}
    row.update({f"le_{i}": 0 for i in range(len(LATENCY_BUCKETS))})
    return row


def _le(seconds: float) -> str:
    return str(float(seconds))


def _gauge(name: str, doc: str, value: float) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, doc, value=value)


def _duration(started: float) -> GaugeMetricFamily:
    return _gauge(
        "oncall_journal_read_duration_seconds",
        "Time spent reading the journal on this scrape.",
        time.monotonic() - started,
    )



#--------------------------------------------------------------------------------------------------------
# ENTRY POINT
#--------------------------------------------------------------------------------------------------------



def main(argv: list[str] | None = None) -> int:
    from prometheus_client import start_http_server
    from prometheus_client.core import REGISTRY

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--addr", default="127.0.0.1",
                    help="bind address; 0.0.0.0 only inside a container")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    REGISTRY.register(JournalCollector())
    start_http_server(args.port, addr=args.addr)
    log.info("journal exporter on %s:%d/metrics", args.addr, args.port)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
