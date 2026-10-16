"""QpuLimiter sliding windows with a fake clock."""

from __future__ import annotations

import pytest

from azure_finops.ratelimit import DEFAULT_WINDOWS, QpuLimiter, qpu_for_days


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def limiter(windows=DEFAULT_WINDOWS):
    clock = FakeClock()
    return QpuLimiter(windows=windows, clock=clock, sleep=clock.sleep), clock


def test_defaults_match_documented_limits():
    assert DEFAULT_WINDOWS == ((10.0, 12), (60.0, 60), (3600.0, 600))


def test_twelve_qpu_per_ten_seconds():
    lim, clock = limiter()
    for _ in range(12):
        assert lim.acquire(1) == 0.0
    assert clock.sleeps == []
    assert lim.acquire(1) == pytest.approx(10.0)
    assert clock.now == pytest.approx(10.0)
    # the next 11 fit in the new window without waiting
    for _ in range(11):
        assert lim.acquire(1) == 0.0
    assert lim.acquire(1) == pytest.approx(10.0)
    assert lim.total_waited == pytest.approx(20.0)


def test_wait_is_until_enough_old_units_expire():
    lim, clock = limiter()
    lim.acquire(6)  # t=0
    clock.now = 4.0
    lim.acquire(6)  # t=4, window now full
    clock.now = 5.0
    assert lim.wait_time(1) == pytest.approx(5.0)  # the t=0 batch leaves at t=10
    assert lim.wait_time(7) == pytest.approx(9.0)  # needs both batches gone: t=4 batch leaves at t=14
    assert lim.acquire(1) == pytest.approx(5.0)
    assert clock.now == pytest.approx(10.0)


def test_partial_window_does_not_wait():
    lim, clock = limiter()
    lim.acquire(5)
    clock.now = 3.0
    assert lim.wait_time(7) == 0.0
    assert lim.acquire(7) == 0.0
    assert lim.wait_time(1) == pytest.approx(7.0)


def test_minute_window_applies_after_short_windows_clear():
    lim, clock = limiter()
    for _ in range(60):
        lim.acquire(1)
    # 60 QPU took 40s of 10-second windows (5 batches of 12 at t=0,10,20,30,40)
    assert clock.now == pytest.approx(40.0)
    waited = lim.acquire(1)
    # the 10 s window would allow t=50, but the 60 s window holds until the t=0 batch expires at t=60
    assert waited == pytest.approx(20.0)
    assert clock.now == pytest.approx(60.0)
    assert lim.total_waited == pytest.approx(60.0)


def test_hour_window_with_custom_windows():
    lim, clock = limiter(windows=((1.0, 5), (100.0, 6)))
    for _ in range(5):
        lim.acquire(1)
    assert lim.acquire(1) == pytest.approx(1.0)  # 1-second window
    assert lim.acquire(1) == pytest.approx(99.0)  # long window: the t=0 batch expires at t=100
    assert clock.now == pytest.approx(100.0)


def test_requests_larger_than_smallest_limit_are_clamped():
    lim, clock = limiter()
    assert lim.acquire(50) == 0.0  # clamped to 12 so it can never deadlock
    assert lim.wait_time(1) == pytest.approx(10.0)
    assert lim.acquire(0) == pytest.approx(10.0)  # minimum charge is 1


def test_old_events_are_pruned():
    lim, clock = limiter()
    lim.acquire(3)
    clock.now = 4000.0
    assert lim.wait_time(1) == 0.0
    assert len(lim._events) == 0


@pytest.mark.parametrize(
    ("days", "qpu"),
    [(0, 1), (1, 1), (7, 1), (30, 1), (31, 1), (32, 2), (62, 2), (63, 3), (90, 3), (365, 12), (366, 12)],
)
def test_qpu_for_days(days, qpu):
    assert qpu_for_days(days) == qpu
