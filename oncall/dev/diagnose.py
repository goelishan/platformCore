"""
Diagnose one subject: assemble, open or reuse the incident, ask the model, journal.

  - --dry-run prints the exact prompt, both receipts and the token estimate. Nothing is
    written and Bedrock is not called.
  - A live run opens an incident for the subject, or reuses the latest unresolved one,
    and attaches the bundle's candidate signals by id. Then it saves the bundle snapshot,
    calls the model and journals the record, failures included.
  - --replay <diagnosis_id> loads that diagnosis's snapshot, checks it still hashes to the
    journalled receipt, runs it against today's model and prompt, journals the new
    attempt and prints the two side by side. Same evidence, so a different answer is the
    model's or the prompt's doing and nothing else's.
  - The Bedrock call runs outside any buffer transaction. Holding SQLite's write lock for
    the 15 to 20 seconds a diagnosis takes would stall every collector during exactly
    the incident being diagnosed.

    make -C oncall diagnose SUBJECT=crashloop ARGS="--dry-run"
    make -C oncall diagnose SUBJECT=crashloop
    make -C oncall diagnose ARGS="--replay <diagnosis_id>"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

from oncall import config
from oncall import landing_zone as lz
from oncall.diagnosis import prompt, reasoner, snapshots
from oncall.diagnosis.journal import journal
from oncall.diagnosis.schema import DiagnosisRecord
from oncall.evidence import bundle as assembler


def _print_prompt(p: prompt.Prompt) -> None:
    print("==== system " + "=" * 68)
    print(p.system)
    print("==== user " + "=" * 70)
    print(p.user)
    print("==== tool " + "=" * 70)
    print(json.dumps(p.tool, indent=2))
    print("=" * 80)


def _receipt_line(r: DiagnosisRecord) -> str:
    cost = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "cost n/a"
    return (
        f"{r.outcome}  attempts={r.attempts}  tokens={r.input_tokens}/{r.output_tokens}  "
        f"latency={r.latency_ms}ms  {cost}  model={r.model}  prompt=v{r.prompt_version}"
    )


def _print_record(r: DiagnosisRecord, labels: dict[str, str]) -> None:
    """Citations are fingerprints in the record; shown as the labels the model used."""
    by_fp = {fp: label for label, fp in labels.items()}
    d = r.diagnosis
    if d is not None:
        print(d.summary)
        print()
        for h in d.hypotheses:
            cites = ", ".join(by_fp.get(fp, fp[:8]) for fp in h.supporting)
            print(f"  {h.rank}. [{h.cause_class}, {h.confidence}] {h.title}")
            print(f"     {h.mechanism}")
            print(f"     supported by {cites}")
            for c in h.verify:
                print(f"     $ {c.command}    # {c.purpose}")
            print()
        if d.unknowns:
            print("unknowns")
            for u in d.unknowns:
                print(f"  - {u}")
            print()
    for e in r.errors:
        print(f"error: {e}")
    print(_receipt_line(r))
    print(f"diagnosis_id {r.diagnosis_id}  incident {r.incident_id}")
    print(f"bundle_sha256 {r.bundle_sha256}")
    print(f"prompt_sha256 {r.prompt_sha256}")


def _top(d: dict[str, Any]) -> tuple[str, str]:
    hypotheses = d.get("hypotheses") or []
    if not hypotheses:
        return "-", "-"
    return str(hypotheses[0].get("cause_class")), str(hypotheses[0].get("confidence"))


def _journalled(diagnosis_id: str) -> dict[str, Any]:
    """The buffer first, then the store, where diagnoses outlive buffer retention."""
    with lz.connect() as conn:
        row = conn.execute(
            "SELECT * FROM diagnoses WHERE diagnosis_id = ?", (diagnosis_id,)
        ).fetchone()
    if row is not None:
        out = dict(row)
        out["hypotheses"] = json.loads(out["hypotheses"] or "[]")
        return out

    from oncall import store

    with store.connect() as conn:
        found = store.fetch_one(
            conn, "SELECT * FROM diagnoses WHERE diagnosis_id = %s", (diagnosis_id,)
        )
    if found is None:
        raise SystemExit(f"no diagnosis {diagnosis_id} in the buffer or the store")
    return dict(found)


def _labels(b: assembler.Bundle) -> dict[str, str]:
    """The labels the model saw, or none when the bundle was too large to send."""
    try:
        return prompt.build(b).labels
    except prompt.PromptTooLarge:
        return {}


def _warn_stale(b: assembler.Bundle) -> None:
    for note in b.degraded:
        if note.startswith(assembler.STALE_NOTE_PREFIX):
            print(f"warning: {note}", file=sys.stderr)


def dry_run(b: assembler.Bundle) -> int:
    _warn_stale(b)
    try:
        p = prompt.build(b)
    except prompt.PromptTooLarge as e:
        print(f"prompt too large: {e}; nothing would be sent")
        return 2
    _print_prompt(p)
    print(f"bundle_sha256  {p.bundle_sha256}")
    print(f"prompt_sha256  {p.prompt_sha256}")
    print(f"prompt v{p.version}  {p.size_bytes} bytes  ~{p.estimated_tokens} tokens (estimate)")
    print(f"findings {len(b.findings)}  candidates {len(b.candidate_signal_ids)}")
    print("dry run: no incident opened, nothing journalled, Bedrock not called")
    return 0


def live(b: assembler.Bundle, cluster: str, subject: str | None, client: Any) -> int:
    with lz.connect() as conn:
        incident = lz.unresolved_incident(conn, cluster, subject)
        reused = incident is not None
        if incident is None:
            incident = lz.open_incident(
                conn, cluster, b.scope.namespace if b.scope else None,
                trigger="manual", title=subject,
            )
        attached = lz.attach_signal_ids(conn, incident, b.candidate_signal_ids)

    snapshots.save(b)
    _warn_stale(b)
    record = reasoner.diagnose(b, incident_id=incident, client=client)
    with lz.connect() as conn:
        journal(conn, record)

    print(f"incident {incident} ({'reused' if reused else 'opened'}), {attached} signals attached")
    print()
    _print_record(record, _labels(b))
    return 0 if record.outcome == "ok" else 1


def replay(diagnosis_id: str, client: Any) -> int:
    old = _journalled(diagnosis_id)
    b = snapshots.load(old["bundle_sha256"])
    new = reasoner.diagnose(b, incident_id=old["incident_id"], client=client)
    with lz.connect() as conn:
        journal(conn, new)

    new_dump = new.diagnosis.model_dump(mode="json") if new.diagnosis else {}
    rows = [
        ("diagnosis", diagnosis_id, new.diagnosis_id),
        ("model", old["model"], new.model),
        ("prompt version", old.get("prompt_version") or "-", new.prompt_version),
        ("outcome", old.get("outcome") or "ok", str(new.outcome)),
        ("top cause", *(f"{c} ({k})" for c, k in (_top(old), _top(new_dump)))),
        ("tokens in/out", f"{old['input_tokens']}/{old['output_tokens']}",
         f"{new.input_tokens}/{new.output_tokens}"),
        ("latency ms", old["latency_ms"], new.latency_ms),
        ("prompt_sha256", (old.get("prompt_sha256") or "-")[:16],
         (new.prompt_sha256 or "-")[:16]),
    ]
    print(f"replay of {diagnosis_id} on bundle {old['bundle_sha256'][:16]} (receipt verified)")
    print()
    print(f"  {'':16}{'journalled':36}{'now':36}")
    for name, before, after in rows:
        print(f"  {name:16}{str(before):36}{str(after):36}")
    print()
    for e in new.errors:
        print(f"error: {e}")
    return 0 if new.outcome == "ok" else 1


def main(argv: list[str] | None = None, client: Any = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("subject", nargs="?", default=None,
                    help="pod, workload or node name; omit for the whole cluster")
    ap.add_argument("--cluster", default=config.CLUSTER_NAME)
    ap.add_argument("--at", type=datetime.fromisoformat, default=None,
                    help="derive the window from this moment instead of now")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--replay", metavar="DIAGNOSIS_ID")
    args = ap.parse_args(argv)

    if args.at is not None and args.at.tzinfo is None:
        ap.error("--at needs an offset, e.g. 2026-10-04T12:00:00+00:00")

    if args.replay:
        return replay(args.replay, client)

    with lz.connect() as conn:
        b = assembler.assemble(conn, args.cluster, subject_name=args.subject, now=args.at)
    if args.dry_run:
        return dry_run(b)
    return live(b, args.cluster, args.subject, client)


if __name__ == "__main__":
    sys.exit(main())
