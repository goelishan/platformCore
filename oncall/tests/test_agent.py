"""
The agent's scheduler: cadence per job, isolation between jobs, no catch-up bursts.

  - Driven by a fake clock, so each assertion is about scheduling and not about how long
    the test machine took.
  - The job list is checked against the registry and config, so a new collector or a
    changed interval reaches the agent without a separate edit.
"""

from __future__ import annotations

import threading

import pytest

from oncall import agent, config


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def job(name: str, interval: float, calls: list[str], fail: bool = False) -> agent.Job:
    def fn() -> None:
        calls.append(name)
        if fail:
            raise RuntimeError(f"{name} broke")

    return agent.Job(name, interval, fn)


def test_every_job_runs_on_the_first_tick_in_order():
    calls: list[str] = []
    s = agent.Scheduler([job("events", 30, calls), job("pods", 60, calls)], Clock())

    s.tick()

    assert calls == ["events", "pods"]


def test_each_job_keeps_its_own_interval():
    clock, calls = Clock(), []
    s = agent.Scheduler([job("events", 30, calls), job("pods", 60, calls)], clock)

    for t in range(0, 121, 30):
        clock.t = t
        s.tick()

    assert calls.count("events") == 5
    assert calls.count("pods") == 3


def test_tick_returns_when_the_next_job_is_due():
    clock, calls = Clock(), []
    s = agent.Scheduler([job("events", 30, calls), job("nodes", 120, calls)], clock)

    assert s.tick() == 30


def test_a_failing_job_is_counted_and_never_stops_the_rest():
    clock, calls = Clock(), []
    s = agent.Scheduler([job("broken", 30, calls, fail=True), job("pods", 30, calls)], clock)

    s.tick()
    clock.t = 30
    s.tick()

    broken = s.jobs[0]
    assert calls == ["broken", "pods", "broken", "pods"]
    assert broken.failures == 2 and broken.runs == 2
    assert broken.last_error == "RuntimeError: broken broke"


def test_a_stalled_job_runs_once_then_resumes_its_cadence():
    """Ten missed intervals are one collection, not ten back to back."""
    clock, calls = Clock(), []
    s = agent.Scheduler([job("events", 30, calls)], clock)
    s.tick()

    clock.t = 300
    s.tick()
    s.tick()

    assert calls == ["events", "events"]
    assert s.jobs[0].next_due == 330


def test_the_loop_beats_and_stops_on_request(tmp_path):
    beat = tmp_path / "heartbeat"
    stop = threading.Event()
    calls: list[str] = []

    def stopper() -> None:
        calls.append("ran")
        stop.set()

    agent.run(agent.Scheduler([agent.Job("once", 3600, stopper)]), stop, beat)

    assert calls == ["ran"]
    assert beat.exists()


def test_the_agent_runs_every_registered_collector_at_its_configured_interval():
    from oncall.collectors import registry

    jobs = {j.name: j.interval for j in agent.jobs()}

    for source in registry.COLLECTORS:
        assert jobs[f"collect:{source}"] == config.POLL_INTERVALS[source]
    assert jobs["ship"] == config.SHIP_INTERVAL
    assert {"sweep:buffer", "sweep:store"} <= set(jobs)


def test_events_are_collected_first_because_they_are_lost_if_missed():
    names = [j.name for j in agent.jobs()]

    assert names[0] == "collect:k8s_events"


@pytest.mark.parametrize("name", ["sweep:buffer"])
def test_the_buffer_sweep_runs_against_a_real_buffer(buffer, name):
    from oncall import landing_zone as lz

    lz.bootstrap()
    job = next(j for j in agent.jobs() if j.name == name)

    assert isinstance(job.fn(), dict)


def test_time_spent_inside_a_job_does_not_stretch_its_cadence():
    """A 5 s collection on a 30 s interval runs at 0, 30, 60, not 0, 35, 70."""
    clock = Clock()
    started: list[float] = []

    def slow() -> None:
        started.append(clock.t)
        clock.t += 5

    s = agent.Scheduler([agent.Job("events", 30, slow)], clock)
    for _ in range(3):
        clock.t = s.tick()

    assert started == [0, 30, 60]


def test_a_job_can_wait_before_its_first_run():
    clock, calls = Clock(), []
    jobs = [job("events", 100, calls), agent.Job("ship", 120, lambda: calls.append("ship"),
                                                 start_after=60)]
    s = agent.Scheduler(jobs, clock)

    s.tick()
    clock.t = 59
    s.tick()
    assert calls == ["events"]

    clock.t = 60
    s.tick()
    assert calls == ["events", "ship"]


def test_shipping_does_not_race_the_store_on_a_fresh_start():
    ship = next(j for j in agent.jobs() if j.name == "ship")

    assert ship.start_after > 0
