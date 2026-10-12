"""
Copies the observability sources into the agent's Helm chart.

  - prometheus/rules/oncall-llm.rules.yml and grafana/dashboards/*.json are the sources;
    promtool tests the rules and the local stack loads both from here.
  - Helm can read files only from inside the chart, so the chart carries copies under
    deploy/chart/files/. The chart turns them into a PrometheusRule and a dashboard
    ConfigMap, gated by monitoring.rules and monitoring.dashboard.
  - A test fails when a copy drifts from its source, so a rule edited in one place and not
    the other cannot reach a cluster.

    make -C oncall obs-render
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RULES = ROOT / "prometheus" / "rules" / "oncall-llm.rules.yml"
DASHBOARDS = ROOT / "grafana" / "dashboards"
CHART_FILES = ROOT.parent / "deploy" / "chart" / "files"


def copies() -> dict[Path, Path]:
    """Chart copy -> source."""
    out = {CHART_FILES / RULES.name: RULES}
    out.update({CHART_FILES / d.name: d for d in sorted(DASHBOARDS.glob("*.json"))})
    return out


def main() -> int:
    CHART_FILES.mkdir(parents=True, exist_ok=True)
    for target, source in copies().items():
        shutil.copyfile(source, target)
        print(f"copied {source.relative_to(ROOT.parent)} -> {target.relative_to(ROOT.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
