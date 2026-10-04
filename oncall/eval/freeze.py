"""
Freeze one bundle per golden subject, so the evaluation runs on evidence that cannot move.

  - Reads the buffer only. Run it straight after collecting from a lab with every fault
    applied, stage 5's actions included, so each subject's evidence is fresh.
  - Writes fixtures/<subject>.json in the snapshot format, and fixtures/manifest.json with
    each one's evidence receipt. run.py refuses a fixture that no longer matches it.
  - Refuses to write a fixture in which any redaction rule still fires. These files are
    committed, and a fixture is the one place a secret could leave the laptop in git.
    Each string value is scanned as itself, with its real line breaks. Scanning the JSON
    text instead turns every newline into the two characters \\n, and a rule that knows
    to skip its own "<redacted>" at the end of a line then fires on text it already
    cleaned (found on leakylogger, 2026-10-04).

    make -C oncall lab-all
    (stage 5 actions, wait two minutes)
    make -C oncall collect && sleep 60 && make -C oncall collect
    make -C oncall eval-freeze
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from oncall import config
from oncall import landing_zone as lz
from oncall.eval import cases
from oncall.evidence import bundle as assembler


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def rules_firing(dump: dict[str, Any]) -> list[str]:
    """Every redaction rule that still fires on any string in the bundle, once each."""
    fired: set[str] = set()
    for s in _strings(dump):
        fired.update(assembler.redact_rules(s)[1])
    return sorted(fired)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster", default=config.CLUSTER_NAME)
    ap.add_argument("--root", type=Path, default=cases.EVAL_DIR)
    args = ap.parse_args(argv)

    golden = cases.load(args.root / "golden.yaml")
    fixtures = args.root / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, str] = {}
    problems = 0
    for case in golden:
        with lz.connect() as conn:
            b = assembler.assemble(conn, args.cluster, subject_name=cases.subject_of(case.subject))
        dump = b.model_dump(mode="json")
        text = json.dumps(dump, indent=1, sort_keys=True)
        fired = rules_firing(dump)
        if fired:
            print(f"  REFUSED {case.subject:18} redaction rules fired: {', '.join(fired)}")
            problems += 1
            continue
        (fixtures / f"{case.subject}.json").write_text(text + "\n")
        receipt = assembler.evidence_receipt(b)
        manifest[case.subject] = receipt
        note = "  (no findings: is this fault applied and collected?)" if not b.findings else ""
        print(f"  {case.subject:18} {len(b.findings):3} findings  {receipt[:16]}{note}")

    (fixtures / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print(f"froze {len(manifest)} of {len(golden)} into {fixtures}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
