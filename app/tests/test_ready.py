#--------------------------------------------------------------------------------------------------------
# READINESS — /ready SPEAKS FOR THE DATABASE, AND ONLY FOR IT
#--------------------------------------------------------------------------------------------------------
#
#   - 200 when the connection succeeds
#
#   - 503 when the connection raises
#
#   - 503 naming the variable when configuration is missing
#
#   - the success cache: reused within the TTL, never applied to a failure
#


import logging


def test_ready_returns_200_when_the_connection_succeeds(client, connect_spy):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    # The probe is only worth its cost if it actually round-trips to the database.
    assert len(connect_spy.calls) == 1
    assert connect_spy.connections[0].cursors[0].executed == ["SELECT 1"]
    # And the round-trip is bounded. An unbounded connect holds a threadpool worker
    # for the OS retry budget long after the kubelet has given up on the probe, which
    # is how a stalled database turns into a restart of every replica.
    assert "connect_timeout=" in connect_spy.calls[0]


def test_ready_returns_503_when_the_connection_raises(client, connect_spy):
    connect_spy.raising(OSError("connection refused"))

    response = client.get("/ready")

    # 503 is what removes the Pod from the Service endpoints. Any 5xx that FastAPI
    # produced by accident would read as unhealthy to the kubelet too, so the body is
    # asserted as well: this failure has to be the deliberate one.
    assert response.status_code == 503
    assert "connection refused" in response.json()["detail"]


def test_ready_returns_503_naming_the_missing_variable(client, connect_spy, monkeypatch, caplog):
    monkeypatch.delenv("RDS_HOST")

    with caplog.at_level(logging.ERROR, logger="platformcore"):
        response = client.get("/ready")

    assert response.status_code == 503
    assert "RDS_HOST" in response.json()["detail"]
    # Configuration that is absent is not a database that is down: nothing should be
    # dialled, and the log line has to name the variable rather than the `vars`
    # builtin an f-string typo used to interpolate here.
    assert connect_spy.calls == []
    assert "RDS_HOST" in caplog.text
    assert "built-in" not in caplog.text


def test_ready_serves_a_second_probe_from_the_cached_success(client, connect_spy):
    # Wired to a probe, this endpoint runs on every period on every replica forever.
    # Two calls inside the TTL must cost one connection, or the cache is not doing the
    # job it was added for.
    assert client.get("/ready").status_code == 200
    assert client.get("/ready").status_code == 200

    assert len(connect_spy.calls) == 1


def test_ready_never_caches_a_failure(client, connect_spy):
    # The asymmetry is the point: caching good news bounds load, caching bad news would
    # delay recovery. A database that comes back has to be seen on the next probe.
    connect_spy.raising(OSError("connection refused"))

    assert client.get("/ready").status_code == 503
    assert client.get("/ready").status_code == 503

    assert len(connect_spy.calls) == 2
