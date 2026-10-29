"""
Turn a production diagnosis an engineer labelled into a frozen eval case.

  - The fifteen lab faults are what the model is tuned against. A production incident it
    got wrong is the case most worth adding, and the only one that tests the shapes the
    lab does not produce. This makes that a single command.
  - Needs a human evaluation with a cause: the label is the engineer's, never the model's.
    A correct verdict is promoted too, with the model's cause as the label, because a
    case that stays right is what a regression check is for.
  - The evidence is the diagnosis's own snapshot, from this machine's data directory or
    from the store (shipped there since migration 0008), checked against its receipt.
  - Refused, like freeze, if any redaction rule still fires: the fixture is committed.
  - golden.yaml gains the entry with source: production, so make eval-freeze leaves it and
    its manifest line alone. It cannot be re-frozen: the cluster it came from has moved on.

    make -C oncall eval-promote DIAGNOSIS=<id>
    make -C oncall eval-promote DIAGNOSIS=<id> ARGS="--name prod-api-dns --terms coredns,dns"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oncall.eval import cases
from oncall.eval.freeze import rules_firing
from oncall.evidence import bundle as assembler

PRODUCTION = "production"


def _find(diagnosis_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(diagnosis row, human evaluation or None), buffer first, then the store."""
    from oncall import landing_zone as lz
    from oncall.eval import online

    lz.bootstrap()
    with lz.connect() as conn:
        row = online._row(conn, diagnosis_id)
    if row is not None:
        with lz.connect() as conn:
            incident = conn.execute("SELECT title FROM incidents WHERE incident_id = ?",
                                    (row["incident_id"],)).fetchone()
        row["title"] = incident["title"] if incident else None
        return row, online.evaluations_for(diagnosis_id).get(online.HUMAN)

    from oncall import store

    with store.connect() as conn:
        found = store.fetch_one(
            conn, "SELECT d.*, i.title FROM diagnoses d JOIN incidents i USING (incident_id) "
                  "WHERE d.diagnosis_id = %s", (diagnosis_id,))
        human = store.fetch_one(
            conn, "SELECT * FROM evaluations WHERE diagnosis_id = %s AND evaluator = %s",
            (diagnosis_id, online.HUMAN))
    if found is None:
        raise LookupError(f"no diagnosis {diagnosis_id} in the buffer or the store")
    return dict(found), dict(human) if human else None


def _bundle(sha: str) -> assembler.Bundle:
    from oncall.diagnosis import snapshots

    try:
        return snapshots.load(sha)
    except FileNotFoundError:
        pass
    from oncall import store

    with store.connect() as conn:
        data = store.snapshot(conn, sha)
    if data is None:
        raise LookupError(f"no snapshot {sha[:16]} here or in the store")
    b = assembler.Bundle.model_validate(data)
    if assembler.evidence_receipt(b) != sha:
        raise ValueError(f"the stored snapshot does not hash to {sha[:16]}")
    return b


def default_name(row: dict[str, Any]) -> str:
    subject = re.sub(r"[^a-z0-9-]+", "-", (row.get("title") or "cluster").lower()).strip("-")
    return f"prod-{subject}-{row['diagnosis_id'][:8]}"


def _yaml_str(text: str) -> str:
    return "'" + text.replace("'", "''").replace("\n", " ") + "'"


def golden_entry(name: str, cause: str, rationale: str, terms: list[str]) -> str:
    lines = [f"{name}:", f"  expected: {cause}", f"  source: {PRODUCTION}",
             f"  rationale: {_yaml_str(rationale)}"]
    if terms:
        lines.append(f"  remedy_terms: {json.dumps(terms)}")
    return "\n".join(lines) + "\n"


def promote(diagnosis_id: str, root: Path, name: str | None = None,
            terms: list[str] | None = None) -> Path:
    row, human = _find(diagnosis_id)
    if human is None or not human.get("cause_class"):
        raise ValueError(f"{diagnosis_id} has no engineer verdict with a cause; record one "
                         "in the UI or with make -C oncall feedback first")
    name = name or default_name(row)
    golden_path = root / "golden.yaml"
    if name in {c.subject for c in cases.load(golden_path)}:
        raise ValueError(f"{name} is already a case in golden.yaml; pass --name")

    b = _bundle(row["bundle_sha256"])
    dump = b.model_dump(mode="json")
    fired = rules_firing(dump)
    if fired:
        raise ValueError(f"redaction rules still fire in this bundle: {', '.join(fired)}")

    fixtures = root / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    target = fixtures / f"{name}.json"
    target.write_text(json.dumps(dump, indent=1, sort_keys=True) + "\n")

    manifest_path = fixtures / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest[name] = assembler.evidence_receipt(b)
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")

    lead = (row.get("hypotheses") or [{}])
    lead = (json.loads(lead) if isinstance(lead, str) else lead)[0].get("cause_class")
    when = str(row.get("created_at"))[:10]
    rationale = (f"production {when}, diagnosis {diagnosis_id[:12]}: engineer said "
                 f"{human['verdict']} (model said {lead})"
                 + (f"; {human['notes']}" if human.get("notes") else ""))
    text = golden_path.read_text()
    golden_path.write_text(text.rstrip("\n") + "\n\n"
                           + golden_entry(name, human["cause_class"], rationale, terms or []))
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Promote a labelled diagnosis into the eval.")
    ap.add_argument("diagnosis_id")
    ap.add_argument("--name", default=None, help="case name; default prod-<subject>-<id>")
    ap.add_argument("--terms", default="", help="comma-separated remedy_terms for the case")
    ap.add_argument("--root", type=Path, default=cases.EVAL_DIR)
    args = ap.parse_args(argv)

    terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    try:
        path = promote(args.diagnosis_id, args.root, args.name, terms)
    except (LookupError, ValueError) as exc:
        print(f"not promoted: {exc}", file=sys.stderr)
        return 1
    print(f"promoted {args.diagnosis_id} to {path} on {datetime.now(UTC):%Y-%m-%d}; "
          "review golden.yaml, then make -C oncall eval ARGS=\"--only "
          f"{path.stem}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
