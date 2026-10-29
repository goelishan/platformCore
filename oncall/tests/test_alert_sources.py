"""
Prometheus alerts, Argo CD Applications and the Alertmanager webhook.

  - The mappings are pure and tested on payloads in the shapes the real APIs return:
    /api/v1/alerts, an Application custom object, an Alertmanager version 4 webhook.
  - The webhook is tested over HTTP on a real socket, because the token check, the body
    cap and the status codes are the contract Alertmanager sees.
  - Triage is tested on what an alert changes: no settle period, no severity floor.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from oncall import agent, config, triage, webhook
from oncall import landing_zone as lz
from oncall.collectors import alerts, argocd, prometheus
from oncall.envelope import Owner, Severity, SignalSource, iso
from oncall.evidence import bundle
from oncall.tests import test_bundle as tb
from oncall.tests import test_diagnosis_reasoner as tr

NOW = tb.NOW


def prom_alert(labels, state="firing", active=NOW - timedelta(minutes=5), **annotations):
    return {"labels": {"alertname": "KubePodCrashLooping", "severity": "warning", **labels},
            "annotations": annotations, "state": state,
            "activeAt": active.isoformat().replace("+00:00", ".123456789Z"), "value": "3e+00"}


def body(*alerts_):
    return {"status": "success", "data": {"alerts": list(alerts_)}}


# ---- Prometheus alerts --------------------------------------------------------------------


def test_a_pod_alert_lands_on_the_pod_with_its_owner_from_the_buffer():
    owner_of = {("prod", "api-7f9"): Owner(kind="Deployment", name="api")}.get

    (s,) = prometheus.signals_from(body(prom_alert({"namespace": "prod", "pod": "api-7f9"},
                                                   summary="restarting")),
                                   tb.CLUSTER, lambda ns, p: owner_of((ns, p)))

    assert (s.subject.kind, s.subject.name, s.owner.name) == ("Pod", "api-7f9", "api")
    assert (s.source, str(s.kind), s.severity) == (SignalSource.PROMETHEUS, "alert",
                                                   Severity.WARNING)
    assert s.payload["summary"] == "restarting"
    assert s.event_time == (NOW - timedelta(minutes=5)).replace(microsecond=123456)


@pytest.mark.parametrize(("labels", "kind", "name"), [
    ({"namespace": "prod", "deployment": "api"}, "Deployment", "api"),
    ({"namespace": "prod", "job_name": "nightly"}, "Job", "nightly"),
    ({"node": "ip-10-0-1-5"}, "Node", "ip-10-0-1-5"),
    ({"namespace": "prod"}, "Namespace", "prod"),
    ({}, "Alert", "KubePodCrashLooping"),
])
def test_the_subject_is_the_most_specific_object_the_labels_name(labels, kind, name):
    (s,) = prometheus.signals_from(body(prom_alert(labels)), tb.CLUSTER)

    assert (s.subject.kind, s.subject.name) == (kind, name)


def test_the_pipelines_own_alerts_and_the_agents_are_dropped():
    found = prometheus.signals_from(body(
        prom_alert({"alertname": "Watchdog"}),
        prom_alert({"namespace": "oncall", "pod": "oncall-agent-1"}),
        prom_alert({"component": "oncall-agent", "alertname": "OncallModelErrors"}),
    ), tb.CLUSTER)

    assert found == []


def test_pending_and_resolved_are_recorded_at_info_and_the_value_never_splits_a_fingerprint():
    firing = prometheus.signals_from(body(prom_alert({"namespace": "prod", "pod": "p"})),
                                     tb.CLUSTER)[0]
    again = prom_alert({"namespace": "prod", "pod": "p"}, state="pending")
    again["value"] = "9e+00"
    pending = prometheus.signals_from(body(again), tb.CLUSTER)[0]

    assert pending.severity == Severity.INFO
    assert pending.signal_id == firing.signal_id


def test_an_alert_seen_through_two_scrape_targets_is_one_problem():
    """instance and job vary with the scrape path; only the object's identity keys it."""
    a = prom_alert({"namespace": "prod", "pod": "p", "instance": "10.0.1.5:8080"})
    b = prom_alert({"namespace": "prod", "pod": "p", "instance": "10.0.2.9:8080",
                    "job": "kube-state-metrics"})

    first, second = (prometheus.signals_from(body(x), tb.CLUSTER)[0] for x in (a, b))

    assert first.fingerprint == second.fingerprint


def test_kube_system_alerts_are_kept_because_nothing_else_sees_cluster_dns():
    (s,) = prometheus.signals_from(body(prom_alert({"namespace": "kube-system",
                                                    "pod": "coredns-1"})), tb.CLUSTER)

    assert s.namespace == "kube-system"


def test_an_unreachable_prometheus_is_unavailable_with_its_reason(monkeypatch):
    monkeypatch.setattr(config, "PROMETHEUS_URL", "http://prometheus.invalid:9090")

    def refuse(url):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(prometheus, "fetch", refuse)
    status, signals, error = prometheus.collect(tb.CLUSTER)

    assert (str(status), signals) == ("unavailable", [])
    assert "no route" in error


def test_the_optional_sources_are_scheduled_only_when_configured_and_logs_stay_last():
    code = ("from oncall.collectors import registry; from oncall import config;"
            "print([str(s) for s in registry.COLLECTORS]);"
            "print(sorted(config.POLL_INTERVALS))")
    env = {**os.environ, "ONCALL_PROMETHEUS_URL": "http://p:9090", "ONCALL_ARGOCD_ENABLED": "false"}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         check=True).stdout.splitlines()

    order = eval(out[0])  # noqa: S307 - our own print of a list of strings
    assert order[-1] == "k8s_logs" and "prometheus" in order and "argocd" not in order
    assert "argocd" not in out[1]


# ---- Argo CD -----------------------------------------------------------------------------


def app(health="Healthy", sync="Synced", phase="Succeeded", dest="platformcore", **status):
    return {
        "metadata": {"name": "platformcore", "namespace": "argocd", "uid": "u-1",
                     "creationTimestamp": "2026-10-01T00:00:00Z"},
        "spec": {"destination": {"namespace": dest},
                 "source": {"repoURL": "https://github.com/x/y", "path": "charts/app",
                            "targetRevision": "HEAD"},
                 "syncPolicy": {"automated": {"selfHeal": True, "prune": True}}},
        "status": {"health": {"status": health}, "sync": {"status": sync, "revision": "abc"},
                   "operationState": {"phase": phase, "finishedAt": "2026-10-29T09:00:00Z",
                                      "message": "ok"},
                   "history": [{"deployedAt": "2026-10-29T08:59:00Z"}], **status},
    }


@pytest.mark.parametrize(("kwargs", "reason", "severity"), [
    ({}, "Healthy", Severity.INFO),
    ({"health": "Degraded"}, "Degraded", Severity.ERROR),
    ({"health": "Missing"}, "Missing", Severity.ERROR),
    ({"sync": "OutOfSync"}, "OutOfSync", Severity.WARNING),
    ({"phase": "Failed"}, "SyncFailed", Severity.ERROR),
    ({"health": "Progressing"}, "Progressing", Severity.INFO),
])
def test_an_application_is_assessed_by_the_worst_of_operation_health_and_sync(
        kwargs, reason, severity):
    assert argocd.assess(app(**kwargs)) == (reason, severity)


def test_an_application_is_written_into_the_namespace_it_deploys_to():
    s = argocd.signal_for(app(health="Degraded", resources=[
        {"kind": "Deployment", "name": "api", "namespace": "platformcore", "status": "Synced",
         "health": {"status": "Degraded", "message": "progress deadline exceeded"}},
        {"kind": "Service", "name": "api", "status": "Synced", "health": {"status": "Healthy"}},
    ]), tb.CLUSTER)

    assert (s.namespace, s.subject.kind, s.payload["managed_by"]) == (
        "platformcore", "Application", "argocd")
    assert s.payload["self_heal"] is True
    assert [r["name"] for r in s.payload["out_of_sync_or_unhealthy"]] == ["api"]
    assert s.event_time == datetime(2026, 10, 29, 8, 59, tzinfo=UTC)


def test_an_application_deploying_into_an_excluded_namespace_is_skipped():
    assert argocd.signal_for(app(dest="oncall"), tb.CLUSTER) is None


def test_no_application_crd_is_an_empty_run_not_an_outage(monkeypatch):
    class NotFound(Exception):
        status = 404

    def missing():
        raise NotFound()

    monkeypatch.setattr(argocd, "list_applications", missing)

    assert argocd.collect(tb.CLUSTER) == (argocd.SourceStatus.EMPTY, [], None)


def test_a_workloads_bundle_carries_the_application_that_deploys_it(buffer):
    tb.store([tb.make_signal(when=NOW - timedelta(minutes=5))])
    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.ARGOCD, tb.CLUSTER)
        lz.write_signals(conn, run, [argocd.signal_for(app(dest="prod"), tb.CLUSTER)])
        lz.finish_run(conn, run, argocd.SourceStatus.OK, 1)

    b = tb.build()

    joined = {f.source: f.joined_by for f in b.findings}
    assert joined[SignalSource.ARGOCD] == "namespace"
    assert "managed_by" in bundle.render(b)


# ---- the webhook -----------------------------------------------------------------------


def am_payload(*alerts_):
    return {"version": "4", "status": "firing", "receiver": "oncall-agent",
            "alerts": list(alerts_)}


def am_alert(labels, status="firing", starts=NOW - timedelta(minutes=3)):
    return {"status": status, "labels": {"alertname": "KubePodNotReady", "severity": "warning",
                                         **labels},
            "annotations": {"summary": "not ready"}, "startsAt": iso(starts),
            "endsAt": "0001-01-01T00:00:00Z", "fingerprint": "f1"}


def test_a_firing_alert_opens_an_incident_on_the_workload_behind_the_pod(buffer):
    tb.store([tb.make_signal(when=NOW - timedelta(minutes=9))])
    with lz.connect() as conn:
        conn.execute("UPDATE signals SET owner_kind = 'Deployment', owner_name = 'api'")

    result = webhook.ingest(am_payload(am_alert({"namespace": "prod", "pod": tb.POD})),
                            cluster=tb.CLUSTER)

    assert result == {"alerts": 1, "written": 1, "opened": 1}
    with lz.connect() as conn:
        inc = conn.execute("SELECT trigger, title FROM incidents").fetchone()
    assert (inc["trigger"], inc["title"]) == ("alert:KubePodNotReady", "api")


def test_a_resolved_alert_is_written_at_info_and_opens_nothing(buffer):
    webhook.ingest(am_payload(am_alert({"namespace": "prod", "pod": "p"}, status="resolved")),
                   cluster=tb.CLUSTER)

    with lz.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM incidents").fetchone()["n"] == 0
        assert conn.execute("SELECT severity FROM signals").fetchone()["severity"] == "info"


def test_a_payload_that_is_not_alertmanager_v4_is_refused(buffer):
    with pytest.raises(ValueError, match="version 4"):
        webhook.ingest({"version": "3", "alerts": []})


@pytest.fixture
def server(buffer, monkeypatch):
    monkeypatch.setattr(config, "CLUSTER_NAME", tb.CLUSTER)
    monkeypatch.setattr(config, "WEBHOOK_TOKEN", "s3cret-token-value")
    opened = threading.Event()
    srv = webhook.serve(0, on_opened=opened.set, addr="127.0.0.1")
    yield f"http://127.0.0.1:{srv.server_address[1]}", opened
    srv.shutdown()


def post(url, data, token="s3cret-token-value"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_receiver_answers_alertmanager_and_wakes_the_loop(server):
    base, opened = server
    data = json.dumps(am_payload(am_alert({"namespace": "prod", "pod": "p"}))).encode()

    status, result = post(f"{base}/alertmanager", data)

    assert (status, result["opened"]) == (200, 1)
    assert opened.wait(2)


@pytest.mark.parametrize(("path", "data", "token", "code"), [
    ("/alertmanager", b"{}", None, 401),
    ("/alertmanager", b"{}", "wrong", 401),
    ("/alertmanager", b"not json", "s3cret-token-value", 400),
    ("/alertmanager", b'{"version": "3"}', "s3cret-token-value", 400),
    ("/elsewhere", b"{}", "s3cret-token-value", 404),
])
def test_the_receiver_refuses_what_is_not_an_authenticated_v4_payload(server, path, data,
                                                                      token, code):
    base, _ = server

    assert post(f"{base}{path}", data, token)[0] == code


def test_the_receiver_caps_the_body(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(config, "WEBHOOK_MAX_BYTES", 10)

    assert post(f"{base}/alertmanager", b'{"version": "4", "alerts": []}')[0] == 400


def test_healthz_answers_without_a_token(server):
    base, _ = server
    with urllib.request.urlopen(f"{base}/healthz", timeout=5) as resp:
        assert resp.status == 200


# ---- triage and the agent ---------------------------------------------------------------


def test_an_alert_incident_is_diagnosed_at_once_even_below_the_severity_floor(buffer):
    """A warning alert two seconds old: no settle period, no floor. The rule decided."""
    webhook.ingest(am_payload(am_alert({"namespace": "prod", "pod": tb.POD},
                                       starts=NOW - timedelta(seconds=2))), cluster=tb.CLUSTER)
    tb.record_run(SignalSource.K8S_EVENTS)
    client = tr.FakeBedrock(tr.answer(tr.diagnosis()))

    report = triage.run_once(now=NOW, client=client, cluster=tb.CLUSTER)

    assert report.diagnosed == [(tb.POD, "ok")]
    assert len(client.requests) == 1


def test_argo_cd_rows_are_context_never_candidates(buffer):
    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.ARGOCD, tb.CLUSTER)
        lz.write_signals(conn, run, [argocd.signal_for(app(health="Degraded", dest="prod"),
                                                       tb.CLUSTER).model_copy(
            update={"event_time": NOW - timedelta(minutes=10)})])
        lz.finish_run(conn, run, argocd.SourceStatus.OK, 1)
        found = triage.candidates(conn, tb.CLUSTER, NOW)

    assert found == []


def test_a_wake_from_the_webhook_runs_triage_now_not_at_its_next_interval(tmp_path):
    ran = threading.Event()
    calls = []

    def job():
        calls.append(1)
        if len(calls) == 2:
            ran.set()

    stop, wake = threading.Event(), threading.Event()
    s = agent.Scheduler([agent.Job("triage", 3600, job)])
    loop = threading.Thread(target=agent.run, args=(s, stop, tmp_path / "beat", wake))
    loop.start()
    try:
        for _ in range(50):
            if calls:
                break
            threading.Event().wait(0.02)
        wake.set()
        assert ran.wait(2), "triage did not run again after the wake"
    finally:
        stop.set()
        wake.set()
        loop.join(2)
    assert not loop.is_alive()


def test_owner_lookup_reads_the_newest_pod_row(buffer):
    tb.store([tb.make_signal(when=NOW - timedelta(minutes=2))])
    with lz.connect() as conn:
        conn.execute("UPDATE signals SET owner_kind = 'Deployment', owner_name = 'api'")

    lookup = alerts.buffer_owner_lookup(tb.CLUSTER)

    assert lookup("prod", tb.POD) == Owner(kind="Deployment", name="api")
    assert lookup("prod", "unknown") is None
