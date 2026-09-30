"""
Effects against a real cluster, where the unit suite can only assert on functions.

  - The two worst bugs this project has had were invisible to every unit test: a
    client that never loaded auth, and a client that stringified bytes rather than
    decoding them. Both lived at the Kubernetes boundary, and the only thing crossing
    that boundary in the unit suite is a stub more correct than the real client.
  - Skips unless the active kube context is the lab. Assertions that read "a failing
    pod exists" must never run against a cluster where one existing is an incident.
  - Needs stage 1 of the fault library: make -C oncall lab-up && make -C oncall
    lab-stage STAGE=1, then a few minutes for crashloop to have a previous container.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oncall import config
from oncall import landing_zone as lz
from oncall.collectors import k8s_client as k8s
from oncall.collectors import k8s_logs, registry
from oncall.envelope import SignalSource, SourceStatus
from oncall.evidence import bundle

LAB_CONTEXT = "kind-oncall"
LAB_NS = "oncall-lab"
CLUSTER = "integration"


def _active_context() -> str | None:
    try:
        from kubernetes.config import list_kube_config_contexts

        _, active = list_kube_config_contexts()
        return active["name"] if active else None
    except Exception:  # noqa: BLE001 - no kubeconfig at all is a reason to skip
        return None


@pytest.fixture(scope="module")
def crashloop_pod():
    context = _active_context()
    if context != LAB_CONTEXT:
        pytest.skip(f"active kube context is {context!r}, not {LAB_CONTEXT!r}")

    try:
        pods = k8s.core_v1().list_namespaced_pod(
            LAB_NS, label_selector="app=crashloop", _request_timeout=5
        ).items
    except Exception as exc:  # noqa: BLE001 - an unreachable lab is a skip, not a failure
        pytest.skip(f"lab not reachable: {type(exc).__name__}")

    if not pods:
        pytest.skip("no crashloop pod; run make -C oncall lab-stage STAGE=1")
    return pods[0]


def test_a_real_log_read_is_decoded_and_timestamped(crashloop_pod):
    """The boundary the stub hid. A stringified body arrives as one line with no
    parseable prefix, so lines_seen is 1 and covered_through is None: both assertions
    fail on exactly the bug that made this collector's output garbage for two weeks."""
    target = k8s_logs.Target(
        namespace=LAB_NS,
        pod=crashloop_pod.metadata.name,
        uid=crashloop_pod.metadata.uid,
        container="app",
        streams=(k8s_logs.STREAM_PREVIOUS,),
        node=None,
        owner_kind=None,
        owner_name=None,
        severity=None,
        event_time="",
        trigger_source="",
        trigger_fingerprint="",
        trigger_signal_id="",
    )

    fetch = k8s_logs._read_log(target, k8s_logs.STREAM_PREVIOUS)
    excerpt = k8s_logs.build_excerpt(fetch.text)

    assert fetch.status == str(SourceStatus.OK), fetch.error
    assert k8s_logs.from_log_stream(fetch.text)
    assert excerpt.lines_seen > 1
    assert excerpt.covered_through is not None


def test_one_cycle_yields_a_bundle_that_names_the_fault(buffer, monkeypatch, crashloop_pod):
    """Collection to assembly with nothing stubbed. Every source ran, the log excerpt
    reached the bundle, and the evidence receipt is a function of the evidence alone."""
    monkeypatch.setattr(config, "NAMESPACES", [LAB_NS])
    monkeypatch.setattr(config, "CLUSTER_NAME", CLUSTER)

    results = registry.run_all()
    assert all(r is not None for r in results.values()), results

    now = datetime.now(UTC)
    with lz.connect() as conn:
        first = bundle.assemble(conn, CLUSTER, "crashloop", now=now)
        again = bundle.assemble(conn, CLUSTER, "crashloop", now=now)

    assert first.findings
    assert any(f.source == SignalSource.K8S_LOGS for f in first.findings)
    assert not any(s.never_ran for s in first.sources)
    assert bundle.evidence_receipt(first) == bundle.evidence_receipt(again)