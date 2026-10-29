"""
The Alertmanager webhook: an alert that fires becomes an incident the agent diagnoses now.

  - POST /alertmanager takes Alertmanager's webhook payload (version 4). Each alert is
    written as a prometheus signal through the same mapping the poller uses, inside a
    collection run of its own, so a pushed alert and a polled one are the same row.
  - A firing alert at warning or worse, about a subject with no unresolved incident,
    opens one with trigger "alert:<alertname>". Triage diagnoses such an incident on its
    next pass without waiting out the settle period: the alert rule's own "for" clause
    has already done that settling. The handler wakes the agent's loop so that pass is
    seconds away, not two minutes.
  - The handler never calls Bedrock. A diagnosis takes 15 to 30 s, Alertmanager times a
    webhook out after 10, and a slow receiver makes it retry the same group. It writes,
    answers 200, and leaves the diagnosis to the loop, which also owns the spend caps.
  - Resolved alerts are written too, at info, which is what lets triage see the subject
    go quiet and close the incident.
  - Bounded and fail-closed: the body is capped, anything that is not a version 4
    payload is a 400, and with ONCALL_WEBHOOK_TOKEN set a request without the bearer
    token is a 401. GET /healthz answers 200 for the probe.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from oncall import config
from oncall import landing_zone as lz
from oncall.collectors import alerts
from oncall.envelope import Severity, SignalSource, SourceStatus

log = logging.getLogger(__name__)

PATH = "/alertmanager"
ALERT_TRIGGER_PREFIX = "alert:"

_RANK = {Severity.WARNING: 1, Severity.ERROR: 2, Severity.CRITICAL: 3}

# Alertmanager writes 0001-01-01T00:00:00Z for an endsAt it does not know.
_ZERO = "0001-01-01"


def _when(value: str) -> datetime:
    from oncall.collectors.prometheus import _parse_time

    return _parse_time(value)


def ingest(payload: dict[str, Any], cluster: str | None = None) -> dict[str, int]:
    """Write the payload's alerts and open incidents for the firing ones. Pure apart from
    the buffer; returns counts for the response body and the log."""
    if str(payload.get("version")) != "4" or not isinstance(payload.get("alerts"), list):
        raise ValueError("not an Alertmanager version 4 webhook payload")
    cluster = cluster or config.CLUSTER_NAME
    owner_of = alerts.buffer_owner_lookup(cluster)

    signals = []
    for a in payload["alerts"]:
        if not a.get("startsAt") or a["startsAt"].startswith(_ZERO):
            continue
        s = alerts.to_signal(a.get("labels") or {}, a.get("annotations") or {},
                             _when(a["startsAt"]), a.get("status", "firing"), cluster,
                             owner_of, extra={"via": "alertmanager"})
        if s is not None:
            signals.append(s)

    opened = 0
    with lz.connect() as conn:
        run = lz.start_run(conn, SignalSource.PROMETHEUS, cluster)
        written = lz.write_signals(conn, run, signals)
        lz.finish_run(conn, run, SourceStatus.OK if signals else SourceStatus.EMPTY, written)

        for s in signals:
            if _RANK.get(s.severity, 0) < 1 or s.subject is None:
                continue
            title = s.owner.name if s.owner else s.subject.name
            if lz.unresolved_incident(conn, cluster, title) is None:
                lz.open_incident(conn, cluster, s.namespace,
                                 trigger=f"{ALERT_TRIGGER_PREFIX}{s.payload.get('reason')}",
                                 title=title)
                opened += 1
    return {"alerts": len(payload["alerts"]), "written": written, "opened": opened}


class Handler(BaseHTTPRequestHandler):
    server_version = "oncall-webhook"
    # Set by serve(): called after an ingest that opened an incident.
    on_opened: Callable[[], None] | None = None

    def log_message(self, fmt: str, *args: Any) -> None:  # route to logging, not stderr
        log.info("webhook %s", fmt % args)

    def _reply(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        if self.path == "/healthz":
            self._reply(HTTPStatus.OK, {"status": "ok"})
        else:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        if self.path != PATH:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if config.WEBHOOK_TOKEN:
            given = self.headers.get("Authorization", "")
            if not hmac.compare_digest(given.encode(), f"Bearer {config.WEBHOOK_TOKEN}".encode()):
                self._reply(HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong token"})
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > config.WEBHOOK_MAX_BYTES:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "body missing or over the cap"})
            return
        try:
            result = ingest(json.loads(self.rfile.read(length)))
        except (ValueError, KeyError, TypeError) as exc:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": str(exc)[:200]})
            return
        except Exception:  # noqa: BLE001 - Alertmanager retries a 5xx, which is right here
            log.exception("webhook ingest failed")
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "ingest failed"})
            return
        log.info("alertmanager webhook: %s", result)
        if result["opened"] and Handler.on_opened:
            Handler.on_opened()
        self._reply(HTTPStatus.OK, result)


def serve(port: int, on_opened: Callable[[], None] | None = None,
          addr: str = "0.0.0.0") -> ThreadingHTTPServer:  # noqa: S104 - in-cluster Service
    """Start the receiver on a daemon thread and return the server (shutdown() stops it)."""
    Handler.on_opened = on_opened
    server = ThreadingHTTPServer((addr, port), Handler)
    threading.Thread(target=server.serve_forever, name="webhook", daemon=True).start()
    log.info("alertmanager webhook on %s:%d%s", addr, port, PATH)
    return server
