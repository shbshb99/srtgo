from srtgo import pacing
from srtgo.pacing import HumanPacer


def test_delays_are_human_scale_and_vary():
    p = HumanPacer(clock=lambda: 0.0)
    ds = [p.next_delay() for _ in range(200)]
    assert min(ds) >= pacing.MIN_SECONDS
    assert len(set(round(d, 3) for d in ds)) > 150


def test_hourly_cap_forces_long_wait():
    p = HumanPacer(clock=lambda: 0.0)
    for _ in range(pacing.HOURLY_CAP):
        p.next_delay()
    assert p.next_delay() >= pacing.CAP_WAIT_RANGE[0]


def test_cap_window_expires():
    t = [0.0]
    p = HumanPacer(clock=lambda: t[0])
    for _ in range(pacing.HOURLY_CAP + 5):
        p.next_delay()
    t[0] = 4000.0
    assert p.next_delay() < pacing.CAP_WAIT_RANGE[0]
