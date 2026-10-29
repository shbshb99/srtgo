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


def test_json_overrides_defaults(tmp_path):
    f = tmp_path / "pacing.json"
    f.write_text('{"MEDIAN_SECONDS": 20, "REST_CHANCE": 0, "HOURLY_CAP": 2, "CAP_WAIT_RANGE": [500, 600]}')
    cfg = pacing.load_settings(f)
    assert cfg["MEDIAN_SECONDS"] == 20.0 and cfg["SIGMA"] == pacing.SIGMA
    p = HumanPacer(clock=lambda: 0.0, path=f)
    p.next_delay(); p.next_delay()
    assert 500 <= p.next_delay() <= 600


def test_broken_json_falls_back(tmp_path):
    f = tmp_path / "pacing.json"
    f.write_text("{깨진")
    assert pacing.load_settings(f)["HOURLY_CAP"] == pacing.HOURLY_CAP


def test_write_example_keeps_existing(tmp_path):
    f = tmp_path / "pacing.json"
    pacing.write_example(f)
    assert pacing.load_settings(f) == pacing.load_settings(tmp_path / "none.json")
    f.write_text('{"HOURLY_CAP": 7}')
    pacing.write_example(f)
    assert pacing.load_settings(f)["HOURLY_CAP"] == 7
