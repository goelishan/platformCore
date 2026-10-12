"""
Run every frozen fixture through the reasoner and write the report.

  - Live Bedrock calls, so it costs money and is never run in CI. It is the gate for a
    prompt or model change: --check exits non-zero when top-1 falls more than one case
    below baseline.json.
  - Fixtures are verified against manifest.json before anything is sent, so a report is
    always about the evidence that was frozen and labelled.
  - Runs are not journalled. The journal is the record of real incidents; an eval run is
    recorded in its report, under reports/<UTC date>-v<prompt>-<model>.md and .json.
  - Sequential on purpose: latency is one of the figures, and parallel calls would
    measure Bedrock's queueing under this script's own load.

    make -C oncall eval
    make -C oncall eval ARGS="--repeat 3"
    make -C oncall eval ARGS="--model us.anthropic.claude-haiku-4-5-20251001-v1:0"
    make -C oncall eval ARGS="--check"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oncall import config
from oncall.diagnosis import prompt, reasoner, snapshots
from oncall.eval import cases
from oncall.eval.metrics import Rate, Run, Summary, regression, summarise
from oncall.evidence import bundle as assembler


def _fixtures(root: Path, golden: list[cases.Case]) -> dict[str, Any]:
    manifest = json.loads((root / "fixtures" / "manifest.json").read_text())
    loaded = {}
    for case in golden:
        path = root / "fixtures" / f"{case.subject}.json"
        if case.subject not in manifest or not path.exists():
            raise SystemExit(f"no frozen fixture for {case.subject}; run make eval-freeze")
        b = snapshots.load_file(path)
        if assembler.evidence_receipt(b) != manifest[case.subject]:
            raise SystemExit(f"{path} no longer matches manifest.json; refreeze or restore it")
        loaded[case.subject] = b
    return loaded


def _row(run: Run) -> str:
    r = run.record
    mark = "-" if not run.case.scored else ("yes" if run.top1 else "NO")
    cost = f"{r.cost_usd:.4f}" if r.cost_usd is not None else "n/a"
    return (
        f"| {run.case.subject} | {run.case.expected or '-'} | {run.top or '-'} | {mark} "
        f"| {'yes' if run.top3 else 'no'} | {r.outcome} | {r.attempts} "
        f"| {r.input_tokens}/{r.output_tokens} | {r.latency_ms} | {cost} |"
    )


def render_report(runs: list[Run], s: Summary, meta: dict[str, Any]) -> str:
    money = (f"${s.mean_cost_usd:.4f} mean, ${s.total_cost_usd:.4f} total"
             if s.mean_cost_usd is not None else "n/a (no prices configured)")
    lines = [
        f"# On-call reasoner evaluation, {meta['date']}",
        "",
        f"Model `{meta['model']}`, prompt v{meta['prompt_version']} "
        f"(`{meta['prompt_sha256'][:16]}` on the first fixture), {meta['runs']} runs over "
        f"{meta['fixtures']} frozen fixtures, repeat {meta['repeat']}.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Top-1 accuracy | {s.top1} |",
        f"| Top-3 accuracy | {s.top3} |",
        f"| Citation validity | {s.citation_validity} |",
        f"| Command safety | {s.command_safety} |",
        f"| Insufficient evidence rate | {s.insufficient_evidence} |",
        f"| First-attempt valid rate | {s.first_attempt_valid} |",
        f"| Latency p50 / p95 | {s.latency_p50_ms} ms / {s.latency_p95_ms} ms |",
        f"| Tokens, mean in / out | {s.mean_input_tokens or 0:.0f} / "
        f"{s.mean_output_tokens or 0:.0f} |",
        f"| Cost | {money} |",
        f"| Self-agreement across repeats | {s.agreement} |",
        f"| Outcomes | {', '.join(f'{k} {v}' for k, v in sorted(s.outcomes.items()))} |",
        "",
        "| Subject | Expected | Top-1 | Top-1 ok | Top-3 ok | Outcome | Attempts "
        "| Tokens | Latency ms | Cost USD |",
        "|---|---|---|---|---|---|---|---|---|---|",
        *(_row(r) for r in runs),
        "",
        "Latency is measured from the machine that ran the eval, wall clock per diagnosis "
        "including any repair attempt. Cost is from the list prices in oncall/.env.",
    ]
    failures = [r for r in runs if r.record.errors]
    if failures:
        lines += ["", "## Errors", ""]
        lines += [f"- {r.case.subject}: {e}" for r in failures for e in r.record.errors]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, client: Any = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=cases.EVAL_DIR)
    ap.add_argument("--model", default=None)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only", nargs="*", default=None, help="subjects to run")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if top-1 falls more than one case below baseline.json")
    ap.add_argument("--accept", action="store_true",
                    help="record this run as the baseline that --check compares against")
    args = ap.parse_args(argv)

    model = args.model or config.BEDROCK_MODEL_ID
    if not model:
        raise SystemExit("no model: set ONCALL_BEDROCK_MODEL_ID or pass --model")
    golden = [c for c in cases.load(args.root / "golden.yaml")
              if args.only is None or c.subject in args.only]
    bundles = _fixtures(args.root, golden)

    runs: list[Run] = []
    for i in range(args.repeat):
        for case in golden:
            record = reasoner.diagnose(
                bundles[case.subject], incident_id=f"eval-{case.subject}",
                client=client, model_id=model,
            )
            runs.append(Run(case, record))
            print(f"[{i + 1}/{args.repeat}] {_row(runs[-1])}", flush=True)

    s = summarise(runs)
    first = prompt.build(next(iter(bundles.values())))
    date = datetime.now(UTC)
    meta = {
        "date": date.strftime("%Y-%m-%d %H:%M UTC"),
        "model": model,
        "prompt_version": prompt.PROMPT_VERSION,
        "prompt_sha256": first.prompt_sha256,
        "runs": len(runs),
        "fixtures": len(bundles),
        "repeat": args.repeat,
    }
    reports = args.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stem = f"{date:%Y-%m-%dT%H%M}-v{prompt.PROMPT_VERSION}-{model.split('.')[-1].split(':')[0]}"
    (reports / f"{stem}.md").write_text(render_report(runs, s, meta))
    (reports / f"{stem}.json").write_text(json.dumps({
        "meta": meta,
        "summary": {k: (vars(v) if hasattr(v, "hits") else v) for k, v in vars(s).items()},
        "runs": [{"subject": r.case.subject, "expected": r.case.expected,
                  "scored": r.case.scored, "top1": r.top1, "top3": r.top3,
                  "record": r.record.model_dump(mode="json")} for r in runs],
    }, indent=1) + "\n")
    print()
    print(f"top-1 {s.top1}  top-3 {s.top3}  p95 {s.latency_p95_ms} ms  report {stem}.md")

    if args.check:
        baseline = json.loads((args.root / "baseline.json").read_text())
        accepted = Rate(baseline["top1_hits"], baseline["scored"])
        if regression(s.top1, accepted):
            print(f"REGRESSION: top-1 {s.top1} against baseline {accepted}")
            return 1
    if args.accept:
        (args.root / "baseline.json").write_text(json.dumps({
            "top1_hits": s.top1.hits,
            "scored": s.top1.total,
            "model": model,
            "prompt_version": prompt.PROMPT_VERSION,
            "report": f"reports/{stem}.md",
        }, indent=1) + "\n")
        print(f"baseline set to top-1 {s.top1.hits}/{s.top1.total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
