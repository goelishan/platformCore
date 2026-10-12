"""
The observability sources and what is rendered from them stay one thing.

  - The committed cluster manifests equal a fresh render, so a rule edited in the source
    and not re-rendered fails here instead of shipping a stale PrometheusRule.
  - Every metric the rules and the dashboard name is one the exporter emits, so a renamed
    family cannot leave a silent alert or an empty panel.
  - promtool runs the rule unit tests when it is on PATH; CI installs it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

OBS = Path(__file__).resolve().parents[1] / "observability"
RULES = OBS / "prometheus" / "rules" / "oncall-llm.rules.yml"
DASHBOARD = OBS / "grafana" / "dashboards" / "oncall-llm.json"

# Families the exporter emits; histogram and counter suffixes as Prometheus exposes them.
EXPORTED = {
    "oncall_journal_up", "oncall_journal_read_duration_seconds",
    "oncall_diagnoses_total", "oncall_diagnoses_first_attempt_ok_total",
    "oncall_diagnoses_insufficient_evidence_total", "oncall_diagnosis_attempts_total",
    "oncall_diagnosis_latency_seconds_bucket", "oncall_diagnosis_latency_seconds_count",
    "oncall_diagnosis_latency_seconds_sum", "oncall_diagnosis_tokens_total",
    "oncall_diagnosis_cost_usd_total", "oncall_diagnoses_unpriced_total",
    "oncall_diagnosis_last_timestamp_seconds",
}


def _render():
    spec = importlib.util.spec_from_file_location("render", OBS / "render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rule_exprs() -> list[str]:
    groups = yaml.safe_load(RULES.read_text())["groups"]
    return [r["expr"] for g in groups for r in g["rules"]]


def _dashboard_exprs() -> list[str]:
    panels = json.loads(DASHBOARD.read_text())["panels"]
    return [t["expr"] for p in panels for t in p.get("targets", [])]


def _metric_names(expr: str) -> set[str]:
    return set(re.findall(r"\b(oncall_[a-z_]+)\b", expr))


def test_the_committed_manifests_match_a_fresh_render():
    for path, text in _render().rendered().items():
        assert path.exists(), f"{path.name} missing; run make -C oncall obs-render"
        assert path.read_text() == text, f"{path.name} is stale; run make -C oncall obs-render"


def test_the_prometheus_rule_carries_the_release_label_the_operator_selects():
    manifest = yaml.safe_load(_render().prometheus_rule())

    assert manifest["metadata"]["labels"]["release"] == "kps"
    assert manifest["spec"]["groups"] == yaml.safe_load(RULES.read_text())["groups"]


def test_the_dashboard_configmap_is_found_by_the_grafana_sidecar():
    manifest = yaml.safe_load(_render().dashboard_configmap())

    assert manifest["metadata"]["labels"]["grafana_dashboard"] == "1"
    assert manifest["metadata"]["namespace"] == "monitoring"
    assert json.loads(manifest["data"]["oncall-llm.json"]) == json.loads(DASHBOARD.read_text())


@pytest.mark.parametrize("source", ["rules", "dashboard"])
def test_every_metric_named_is_one_the_exporter_emits(source):
    exprs = _rule_exprs() if source == "rules" else _dashboard_exprs()
    named = set().union(*(_metric_names(e) for e in exprs))

    assert named, "no metrics found; the pattern no longer matches"
    assert named <= EXPORTED, f"not exported: {sorted(named - EXPORTED)}"


def test_the_exported_list_matches_the_exporter(buffer, store_db, monkeypatch):
    """Keeps EXPORTED honest: it is checked against a real scrape, not maintained by hand."""
    from prometheus_client import CollectorRegistry, generate_latest
    from prometheus_client.parser import text_string_to_metric_families

    from oncall.exporter import journal as exporter

    monkeypatch.setattr(exporter.config, "BEDROCK_MODEL_ID", "m")
    registry = CollectorRegistry()
    registry.register(exporter.JournalCollector())
    text = generate_latest(registry).decode()
    emitted = {s.name for f in text_string_to_metric_families(text) for s in f.samples}

    assert emitted >= EXPORTED - {"oncall_diagnosis_last_timestamp_seconds"}
    assert emitted <= EXPORTED


def test_every_alert_has_a_severity_and_both_annotations():
    groups = yaml.safe_load(RULES.read_text())["groups"]
    alerts = [r for g in groups for r in g["rules"] if "alert" in r]

    assert len(alerts) == 9
    for a in alerts:
        assert a["labels"]["severity"] in {"info", "warning", "critical"}, a["alert"]
        assert a["labels"]["component"] == "oncall-agent", a["alert"]
        assert {"summary", "description"} <= set(a["annotations"]), a["alert"]


def test_a_rule_reads_only_series_recorded_earlier_in_its_own_group():
    """Groups evaluate in no fixed order; rules within one run in sequence. A rule reading
    a recording from another group, or from later in its own, sees last cycle's value or
    this one at random. CI caught it: the cost alert rendered $6.00 or $6.32, 9 runs in 60."""
    groups = yaml.safe_load(RULES.read_text())["groups"]
    recorded_anywhere = {r["record"] for g in groups for r in g["rules"] if "record" in r}

    for g in groups:
        seen: set[str] = set()
        for r in g["rules"]:
            reads = set(re.findall(r"\b(oncall:[a-z0-9_:]+)", r["expr"])) & recorded_anywhere
            assert reads <= seen, f"{r.get('alert') or r.get('record')} reads {sorted(reads - seen)}"
            if "record" in r:
                seen.add(r["record"])


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not on PATH")
def test_promtool_checks_and_unit_tests_pass():
    check = subprocess.run(["promtool", "check", "rules", str(RULES)],
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr

    tests = OBS / "prometheus" / "tests" / "oncall-llm.test.yml"
    run = subprocess.run(["promtool", "test", "rules", str(tests)],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
