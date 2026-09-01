#--------------------------------------------------------------------------------------------------------
# LIVENESS — /health MUST NOT DEPEND ON THE DATABASE
#--------------------------------------------------------------------------------------------------------
#
#   - answers 200 on the happy path
#
#   - answers 200 with the database unreachable, and opens no connection
#


def test_health_returns_200(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_health_opens_no_connection_when_the_database_is_unreachable(client, connect_spy):
    # This is the whole reason the endpoint exists. A liveness probe that touches RDS
    # turns a database outage into a restart loop across every replica, so the
    # assertion is on the connection attempt, not just the status code: a /health that
    # returned 200 after reaching the database would still be the wrong endpoint.
    connect_spy.raising(OSError("connection refused"))

    response = client.get("/health")

    assert response.status_code == 200
    assert connect_spy.calls == []
