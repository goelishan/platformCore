"""
How the model is doing on real incidents, and what to change next.

  - Reads every journalled diagnosis with its evaluations from both tiers, merged: the
    store for history, the buffer for what has not shipped. Says which tiers answered.
  - Grouped by model and prompt version, because a figure that mixes two prompts says
    nothing about either. Automatic and manual diagnoses are counted separately inside
    each group: the triage loop diagnoses what nobody asked about, and those are judged
    later and by fewer people.
  - Accuracy is only ever stated over the diagnoses an engineer labelled, with that
    denominator beside it. The checks and outcomes cover everything, and say less.
  - "What to improve" is derived from the figures by fixed rules, each naming the file
    or command it points at. Wrong diagnoses come with the command that turns each one
    into a regression case.

    make -C oncall review
    python -m oncall.eval.review --since 7d
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from oncall.eval import cases
from oncall.eval.metrics import Rate
from oncall.eval.online import FLAGS

# A flag on at least this share of good diagnoses is worth acting on. Below it, single
# cases are read one by one in the Errors and Flags sections instead.
FLAG_THRESHOLD = 0.2

ADVICE = {
    "needed_repair": "first answers break a rule the prompt states; the errors column of "
                     "those diagnoses names it. Restate that rule in diagnosis/prompt.py",
    "worst_evidence_uncited": "the hypotheses pass over the most severe finding; check its "
                              "rank in evidence/bundle.py and the citation rule in the prompt",
    "gaps_unacknowledged": "gaps in the evidence go unmentioned; tighten 'What could not be "
                           "seen' in diagnosis/prompt.py",
    "high_confidence_while_blind": "rank 1 is high confidence while a source is blind; "
                                   "state in the prompt that a blind source caps confidence",
    "no_remedy": "faults with no remedy; these are pre-v6 rows or a schema regression",
    "riskiest_step_first": "remedies lead with the most invasive step; ask for the least "
                           "invasive fix first",
}


@dataclass
class Group:
    model: str
    prompt_version: str
    rows: list[dict[str, Any]] = field(default_factory=list)



#--------------------------------------------------------------------------------------------------------
# LOADING
#--------------------------------------------------------------------------------------------------------



_SQL = """
SELECT d.diagnosis_id, d.created_at, d.model, d.prompt_version, d.outcome, d.attempts,
       d.requested_by, d.hypotheses, d.errors, d.cost_usd, i.title, i.trigger
FROM diagnoses d JOIN incidents i USING (incident_id)
WHERE d.created_at >= {p}
ORDER BY d.created_at
"""
_EVAL_SQL = "SELECT diagnosis_id, evaluator, verdict, score, cause_class, details, notes FROM evaluations"


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def load(since: datetime) -> tuple[str, list[dict[str, Any]]]:
    """(tiers read, rows), each row a diagnosis with an "evaluations" dict by evaluator.

    Both tiers, merged: the store holds all history, the buffer holds what has not
    shipped yet, which during a store outage is the newest of everything. Where both
    hold a row the buffer's wins, since it is the copy every later write landed in."""
    from oncall import landing_zone as lz
    from oncall.envelope.timefmt import iso

    diagnoses: dict[str, dict[str, Any]] = {}
    evals: dict[tuple[str, str], dict[str, Any]] = {}
    tiers = []
    try:
        from oncall import store

        with store.connect() as conn:
            for r in store.fetch_all(conn, _SQL.format(p="%s"), (since,)):
                diagnoses[r["diagnosis_id"]] = dict(r)
            for e in store.fetch_all(conn, _EVAL_SQL):
                evals[(e["diagnosis_id"], e["evaluator"])] = dict(e)
        tiers.append("store")
    except Exception as exc:  # noqa: BLE001 - the buffer is the answer when the store is not
        print(f"store not read ({type(exc).__name__}); reporting from the buffer only",
              file=sys.stderr)

    lz.bootstrap()
    with lz.connect() as conn:
        for r in conn.execute(_SQL.format(p="?"), (iso(since),)):
            diagnoses[r["diagnosis_id"]] = dict(r)
        for e in conn.execute(_EVAL_SQL):
            evals[(e["diagnosis_id"], e["evaluator"])] = dict(e)
    tiers.append("buffer")
    tier = " and ".join(tiers)

    by_diagnosis: dict[str, dict[str, Any]] = defaultdict(dict)
    for (diagnosis_id, evaluator), e in evals.items():
        e["details"] = _decode(e["details"]) or {}
        by_diagnosis[diagnosis_id][evaluator] = e
    rows = []
    for d in sorted(diagnoses.values(), key=lambda r: str(r["created_at"])):
        d = dict(d)
        d["hypotheses"] = _decode(d.get("hypotheses")) or []
        d["errors"] = _decode(d.get("errors")) or []
        d["evaluations"] = by_diagnosis.get(d["diagnosis_id"], {})
        rows.append(d)
    return tier, rows



#--------------------------------------------------------------------------------------------------------
# FIGURES
#--------------------------------------------------------------------------------------------------------



def _lead(row: dict[str, Any]) -> str | None:
    return row["hypotheses"][0]["cause_class"] if row["hypotheses"] else None


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if (r.get("outcome") or "ok") == "ok"]
    checks = [r["evaluations"]["checks"] for r in rows if "checks" in r["evaluations"]]
    outcomes = [r["evaluations"]["outcome"] for r in rows if "outcome" in r["evaluations"]]
    humans = [r["evaluations"]["human"] for r in rows if "human" in r["evaluations"]]

    flags = Counter(f for c in checks for f in c["details"].get("flags", []))
    resolved = [o for o in outcomes if o["verdict"] == "resolved"]
    minutes = [o["details"]["resolved_after_minutes"] for o in resolved
               if o["details"].get("resolved_after_minutes") is not None]
    stable = [o["details"]["cause_stable"] for o in outcomes
              if o["details"].get("cause_stable") is not None]
    tried = [h for h in humans if h["details"].get("remedy") in ("worked", "did_not_work")]
    priced = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]

    return {
        "diagnoses": len(rows),
        "auto": sum(1 for r in rows if r.get("requested_by") == "auto"),
        "ok": Rate(len(ok), len(rows)),
        "first_attempt": Rate(sum(1 for r in ok if (r.get("attempts") or 1) == 1), len(rows)),
        "outcomes": Counter(r.get("outcome") or "ok" for r in rows),
        "checks_pass": Rate(sum(1 for c in checks if c["verdict"] == "pass"), len(checks)),
        "checks_score": statistics.fmean(c["score"] for c in checks if c.get("score") is not None)
        if any(c.get("score") is not None for c in checks) else None,
        "flags": flags,
        "flag_base": sum(1 for c in checks if c["verdict"] != "fail"),
        "unchecked": len(rows) - len(checks),
        "resolved": Rate(len(resolved), len(outcomes)),
        "resolve_minutes_p50": statistics.median(minutes) if minutes else None,
        "cause_stable": Rate(sum(stable), len(stable)),
        "labelled": len(humans),
        "verdicts": Counter(h["verdict"] for h in humans),
        "human_top1": Rate(sum(1 for h in humans if h["details"].get("top1_match")),
                           sum(1 for h in humans if h["details"].get("top1_match") is not None)),
        "human_top3": Rate(sum(1 for h in humans if h["details"].get("top3_match")),
                           sum(1 for h in humans if h["details"].get("top3_match") is not None)),
        "remedy_worked": Rate(sum(1 for h in tried if h["details"]["remedy"] == "worked"),
                              len(tried)),
        "cost_usd": sum(priced) if priced else None,
    }


def advice(rows: list[dict[str, Any]], s: dict[str, Any]) -> list[str]:
    """Fixed rules over the figures. Each line names what to change or run."""
    out = []
    base = s["flag_base"]
    for flag, n in s["flags"].most_common():
        if base and n / base >= FLAG_THRESHOLD and flag in ADVICE:
            out.append(f"{flag} on {n}/{base} checked diagnoses: {ADVICE[flag]}")
    if s["cause_stable"].total and s["cause_stable"].value < 1 - FLAG_THRESHOLD:
        out.append(f"re-diagnoses of one incident disagreed ({s['cause_stable']} stable); "
                   "replay each with make -C oncall diagnose ARGS=\"--replay <id>\" and "
                   "compare what changed in the evidence")
    failed = s["diagnoses"] - s["ok"].hits
    if s["diagnoses"] and failed / s["diagnoses"] >= FLAG_THRESHOLD:
        out.append(f"{failed}/{s['diagnoses']} calls produced no diagnosis "
                   f"({dict(s['outcomes'])}); the Errors section says why")
    for r in rows:
        h = r["evaluations"].get("human")
        if h and h["verdict"] in ("wrong", "partial") and h.get("cause_class"):
            out.append(
                f"{h['verdict']}: {r['diagnosis_id']} ({r.get('title') or 'cluster'}) said "
                f"{_lead(r)}, was {h['cause_class']}. Add it to the eval: "
                f"make -C oncall eval-promote DIAGNOSIS={r['diagnosis_id']}"
            )
    unlabelled = s["diagnoses"] - s["labelled"]
    if unlabelled:
        out.append(f"{unlabelled}/{s['diagnoses']} diagnoses have no engineer verdict; "
                   "accuracy above is over the labelled ones only. Label from the UI's "
                   "History tab or make -C oncall feedback")
    return out



#--------------------------------------------------------------------------------------------------------
# REPORT
#--------------------------------------------------------------------------------------------------------



def groups(rows: list[dict[str, Any]]) -> list[Group]:
    found: dict[tuple[str, str], Group] = {}
    for r in rows:
        key = (r.get("model") or "-", r.get("prompt_version") or "-")
        found.setdefault(key, Group(*key)).rows.append(r)
    return [found[k] for k in sorted(found)]


def render(tier: str, since: datetime, rows: list[dict[str, Any]], now: datetime) -> str:
    lines = [
        f"# On-call agent review, {now:%Y-%m-%d %H:%M} UTC",
        "",
        f"{len(rows)} diagnoses since {since:%Y-%m-%d %H:%M} UTC, read from the {tier}"
        + (" (the store did not answer: only what this agent's buffer holds)"
           if tier == "buffer" else "") + ".",
    ]
    for g in groups(rows):
        s = summarise(g.rows)
        cost = f"${s['cost_usd']:.2f}" if s["cost_usd"] is not None else "not priced"
        lines += [
            "",
            f"## `{g.model}`, prompt v{g.prompt_version}",
            "",
            "| Figure | Value |",
            "|---|---|",
            f"| Diagnoses (automatic) | {s['diagnoses']} ({s['auto']}) |",
            f"| Produced a diagnosis | {s['ok']} |",
            f"| Valid on the first attempt | {s['first_attempt']} |",
            f"| Checks passed | {s['checks_pass']}"
            + (f", mean score {s['checks_score']:.2f}" if s["checks_score"] is not None else "")
            + (f", {s['unchecked']} not yet checked" if s["unchecked"] else "") + " |",
            f"| Incidents resolved after diagnosis | {s['resolved']}"
            + (f", median {s['resolve_minutes_p50']:.0f} min" if s["resolve_minutes_p50"]
               is not None else "") + " |",
            f"| Re-diagnoses agreeing | {s['cause_stable']} |",
            f"| Engineer verdicts | {s['labelled']}"
            + (f": {', '.join(f'{k} {v}' for k, v in sorted(s['verdicts'].items()))}"
               if s["labelled"] else "") + " |",
            f"| Rank 1 matched the engineer's cause | {s['human_top1']} |",
            f"| Cause anywhere in the three | {s['human_top3']} |",
            f"| Remedy worked, of those tried | {s['remedy_worked']} |",
            f"| Cost | {cost} |",
        ]
        if s["flags"]:
            lines += ["", "Flags raised by the checks:", ""]
            lines += [f"- {FLAGS.get(f, f)}: {n}" for f, n in s["flags"].most_common()]
        todo = advice(g.rows, s)
        lines += ["", "### What to improve", ""]
        lines += [f"- {a}" for a in todo] or ["- Nothing crosses a threshold."]
        errors = [(r["diagnosis_id"], e) for r in g.rows for e in r["errors"]
                  if (r.get("outcome") or "ok") != "ok"]
        if errors:
            lines += ["", "### Errors", ""]
            lines += [f"- {d}: {e}" for d, e in errors[:30]]
    return "\n".join(lines) + "\n"


def _since(text: str) -> timedelta:
    unit = {"h": "hours", "d": "days"}[text[-1]]
    return timedelta(**{unit: int(text[:-1])})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Review real diagnoses and their evaluations.")
    ap.add_argument("--since", default="30d", help="how far back: 24h, 7d, 30d")
    ap.add_argument("--root", type=Path, default=cases.EVAL_DIR)
    args = ap.parse_args(argv)

    now = datetime.now(UTC)
    since = now - _since(args.since)
    tier, rows = load(since)
    text = render(tier, since, rows, now)
    reports = args.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"review-{now:%Y-%m-%dT%H%M}.md"
    path.write_text(text)
    print(text)
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
