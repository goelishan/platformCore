"""
On-call triage UI: pick a subject, see the evidence, get ranked hypotheses to verify.

  - Layout only. The diagnose flow is diagnosis/service.py, shared with make diagnose,
    and every value shown is shaped by ui/view.py, which is tested without Streamlit.
  - Reads the buffer, and the store for history the buffer has swept. Collect now runs
    the registered collectors; nothing else here reaches the cluster.
  - Diagnose-only. Commands are shown to copy and run, never executed.
  - Optimised for time to a credible hypothesis: evidence gaps, then the first command to
    run, then the hypotheses, before anything else.

    make -C oncall ui
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import streamlit as st

from oncall import config
from oncall.diagnosis import service, snapshots
from oncall.evidence import bundle as assembler
from oncall.ui import view

WHOLE_CLUSTER = "(whole cluster)"
CANDIDATE_HOURS = 6

st.set_page_config(page_title="On-call triage", layout="wide")



#--------------------------------------------------------------------------------------------------------
# RENDERING
#--------------------------------------------------------------------------------------------------------



def show_diagnosis(v: view.DiagnosisView) -> None:
    if v.banner:
        level, text = v.banner
        (st.error if level == "error" else st.warning)(text)
        for e in v.errors:
            st.code(e, language=None, wrap_lines=True)

    # st.text, not markdown: these lines can carry error strings the cluster wrote, and
    # markdown would turn an image link in one into a request from the viewer's browser.
    if v.warnings:
        with st.expander(f"Evidence gaps ({len(v.warnings)})", expanded=True):
            for w in v.warnings:
                st.text(f"- {w}")

    if v.first_command:
        st.markdown("**Run first**")
        st.code(v.first_command, language="bash")

    if v.summary:
        st.markdown(v.summary)

    for h in v.hypotheses:
        with st.container(border=True):
            st.markdown(f"#### {h.rank}. {h.title}")
            st.caption(f"{h.cause_class}  ·  {h.confidence} confidence")
            st.markdown(h.mechanism)
            for c in h.verify:
                st.code(c["command"], language="bash")
                st.caption(c["purpose"])
            label = f"Evidence: {len(h.supporting)} supporting, {len(h.contradicting)} against"
            with st.expander(label):
                for side, items in (("supports", h.supporting), ("against", h.contradicting)):
                    for e in items:
                        show_evidence(e, side)

    st.markdown("**Unknowns**")
    if v.unknowns is None:
        st.caption("Not recorded for this diagnosis.")
    elif not v.unknowns:
        st.caption("The model named no gaps in the evidence.")
    else:
        for u in v.unknowns:
            st.markdown(f"- {u}")

    with st.expander("Receipt"):
        st.table({"field": list(v.receipt), "value": list(v.receipt.values())})


def show_evidence(e: view.Evidence, side: str) -> None:
    if not e.found:
        st.markdown(f"**{e.label}** ({side}): not in the snapshot, `{e.fingerprint}`")
        return
    st.markdown(
        f"**{e.label}** ({side})  ·  {e.source} {e.kind}  ·  {e.subject}  ·  "
        f"{e.state or 'event'}  ·  {e.severity or 'no severity'}  ·  recorded {e.occurrences}x"
    )
    if e.sample:
        st.code(e.sample, language=None)
    if e.log_excerpt:
        st.code(e.log_excerpt, language=None)
    if e.facts:
        st.json(e.facts, expanded=False)


def show_bundle(b: assembler.Bundle) -> None:
    st.caption(
        f"{len(b.findings)} findings  ·  window {b.window_start} to {b.window_end}  ·  "
        f"{b.render_bytes:,} bytes rendered"
    )
    for w in view.warnings(b):
        st.text(f"- {w}")
    st.code(assembler.render(b), language=None)



#--------------------------------------------------------------------------------------------------------
# SIDEBAR
#--------------------------------------------------------------------------------------------------------



cluster = st.sidebar.text_input("Cluster", value=config.CLUSTER_NAME)

st.sidebar.markdown("**Evidence sources**")
health = service.source_health(cluster)
if not health:
    st.sidebar.warning("No collection has run for this cluster.")
for h in health:
    stale = view.age(h["started_at"]).endswith("h ago")
    line = f"{h['source']}: {h['status']}, {view.age(h['started_at'])}"
    (st.sidebar.warning if stale or h["status"] != "ok" else st.sidebar.caption)(line)

if st.sidebar.button("Collect now", help="One cycle of every collector, as make collect"):
    with st.spinner("Collecting"):
        st.session_state["collected"] = service.collect_now()
    st.rerun()
for source, result in st.session_state.get("collected", {}).items():
    st.sidebar.caption(f"collected {source}: {result}")



#--------------------------------------------------------------------------------------------------------
# TABS
#--------------------------------------------------------------------------------------------------------



diagnose_tab, history_tab, evidence_tab = st.tabs(["Diagnose", "History", "Evidence"])

with diagnose_tab:
    since = datetime.now(UTC) - timedelta(hours=CANDIDATE_HOURS)
    candidates = service.subjects(cluster, since)
    options = [WHOLE_CLUSTER, *(c["name"] for c in candidates)]
    described = {c["name"]: f"{c['name']}  ·  {c['namespace']}  ·  {c['worst']}"
                 for c in candidates}
    choice = st.selectbox(
        f"Subject (warnings or worse in the last {CANDIDATE_HOURS} h)", options,
        format_func=lambda o: described.get(o, o),
    )
    typed = st.text_input("Or type a pod, workload or node name", value="")
    subject = typed.strip() or (None if choice == WHOLE_CLUSTER else choice)

    preview_col, diagnose_col = st.columns(2)
    if preview_col.button("Preview evidence", help="Assemble only: no incident, no model call"):
        p = service.preview(service.assemble(cluster, subject))
        st.session_state["bundle"] = p.bundle
        if p.prompt is None:
            st.error(f"Too large to send: {p.too_large}")
        else:
            st.info(f"Prompt v{p.prompt.version}: {p.prompt.size_bytes:,} bytes, about "
                    f"{p.prompt.estimated_tokens:,} tokens. Nothing has been sent.")
        for w in view.warnings(p.bundle):
            st.text(f"- {w}")

    if not config.DIAGNOSE_ENABLED:
        diagnose_col.caption("Diagnose is off here: this deployment has no AWS identity.")
    if diagnose_col.button("Diagnose", type="primary", disabled=not config.DIAGNOSE_ENABLED,
                           help="Opens or reuses the incident, calls Bedrock, journals"):
        with st.spinner("Assembling evidence and asking the model"):
            b = service.assemble(cluster, subject)
            done = service.diagnose(b, cluster, subject)
        st.session_state["bundle"] = b
        st.session_state["result"] = view.build(
            view.row_from_record(done.record), b, done.labels)
        state = "reused" if done.reused else "opened"
        st.session_state["incident_note"] = (
            f"Incident {done.incident_id} {state}, {done.attached} signals attached.")

    if "result" in st.session_state:
        st.caption(st.session_state.get("incident_note", ""))
        show_diagnosis(st.session_state["result"])

with history_tab:
    rows = service.recent(50)
    if not rows:
        st.caption("No diagnoses journalled yet.")
    else:
        def top(r: dict) -> str:
            hs = r.get("hypotheses") or []
            return f"{hs[0]['cause_class']} ({hs[0]['confidence']})" if hs else "-"

        st.dataframe(
            [{"when": r["created_at"][:19].replace("T", " "), "subject": r.get("title") or "-",
              "outcome": r.get("outcome") or "ok", "top cause": top(r),
              "latency s": round((r.get("latency_ms") or 0) / 1000, 1),
              "prompt": f"v{r.get('prompt_version') or '-'}", "id": r["diagnosis_id"]}
             for r in rows],
            hide_index=True, width="stretch",
        )
        by_id = {r["diagnosis_id"]: r for r in rows}
        picked = st.selectbox(
            "Open a diagnosis", list(by_id),
            format_func=lambda i: (f"{by_id[i]['created_at'][:16].replace('T', ' ')}  ·  "
                                   f"{by_id[i].get('title') or 'cluster'}  ·  "
                                   f"{by_id[i].get('outcome') or 'ok'}"),
        )
        row = by_id[picked]
        try:
            b = snapshots.load(row["bundle_sha256"])
        except snapshots.SnapshotMismatch as e:
            st.error(f"Snapshot refused: {e}")
            b = None
        except FileNotFoundError:
            b = None
        if b is not None:
            st.session_state["bundle"] = b
        show_diagnosis(view.build(row, b, service.labels(b) if b else {}))

        if b is not None and st.button("Replay on today's prompt and model",
                                       disabled=not config.DIAGNOSE_ENABLED,
                                       help="Same evidence, new call, journalled"):
            with st.spinner("Replaying"):
                r = service.replay(picked)
            st.session_state["replayed"] = view.build(
                view.row_from_record(r.record), r.bundle, service.labels(r.bundle))
        if "replayed" in st.session_state:
            st.markdown("### Replay")
            show_diagnosis(st.session_state["replayed"])

with evidence_tab:
    b = st.session_state.get("bundle")
    if b is None:
        st.caption("Preview, diagnose or open a diagnosis to see the evidence the model saw.")
    else:
        show_bundle(b)
        with st.expander("Bundle as JSON"):
            st.code(json.dumps(b.model_dump(mode="json"), indent=1)[:200_000], language="json")
