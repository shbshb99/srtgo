"""사람처럼 입력할 때 클릭 사이 쉼이 정한 범위 안인지, 화면이 바뀌길 기다리는 곳은 고정 쉼 없이 값이 바뀔 때까지만 기다리는지 본다.
시계는 가짜 쪽(wait_for_timeout 이 쓴 시간을 모은다)이라 실제로 기다리지 않는다."""
import time

import pytest

from srtgo import scrape_server as ss


class Mouse:
    def __init__(self, page):
        self.page = page

    def move(self, x, y, steps=1):
        self.page.log.append("move")

    def wheel(self, dx, dy):
        self.page.log.append("wheel")

    def down(self):
        self.page.log.append("down")

    def up(self):
        self.page.log.append("up")
        self.page.clicks += 1


class Loc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel
        self.first = self

    def scroll_into_view_if_needed(self):
        pass

    def bounding_box(self):
        return {"x": 10, "y": 20, "width": 100, "height": 30}

    def evaluate(self, js, arg=None):
        return True

    def wait_for(self, **kw):
        pass


class Page:
    viewport_size = {"width": 1280, "height": 800}

    def __init__(self, month_after_click=None):
        self.waits, self.log, self.clicks = [], [], 0
        self.mouse = Mouse(self)
        self.month_after_click = month_after_click

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        self.log.append(("wait", ms))

    def locator(self, sel):
        return Loc(self, sel)

    def evaluate(self, js, arg=None):
        if js == ss.CAL_MONTH_JS:
            return "2026-11" if self.clicks >= 1 else "2026-10"
        if js == ss.CAL_HOURS_JS:
            return [5, 9, 24] if self.clicks >= 1 else [0, 4, 24]
        return None


def test_click_gaps_are_inside_the_short_ranges():
    for _ in range(30):
        p = Page()
        steps = [{"click": "a", "rest": ss.REST_MID} for _ in range(4)] + [{"click": ".btn_lookup"}]
        ss.human_actions(p, steps, look=False)
        assert len(p.waits) == 15                      # 클릭마다 누르기 전·누른 채·누른 뒤 쉼 세 번뿐
        for pre, hold, rest in zip(p.waits[0::3], p.waits[1::3], p.waits[2::3]):
            assert ss.PRE_CLICK[0] <= pre <= ss.PRE_CLICK[1]
            assert ss.HOLD_MS[0] <= hold <= ss.HOLD_MS[1]
            assert ss.REST_MID[0] <= rest <= ss.REST_MID[1]
        assert max(p.waits) <= 160                     # 어떤 클릭 사이에도 160ms 를 넘는 쉼이 없다

def test_last_click_default_rest_is_short_too():
    p = Page()
    ss.human_actions(p, [{"click": ".btn_lookup"}], look=False)
    assert 40 <= p.waits[-1] <= 160                  # 예전에는 500~1300


def test_look_around_is_one_move_one_scroll_and_short_rest():
    for _ in range(20):
        p = Page()
        ss.human_actions(p, [], look=True)
        assert p.log.count("wheel") == 1
        assert len([w for w in p.waits]) == 1 and ss.LOOK_REST[0] <= p.waits[0] <= ss.LOOK_REST[1]


def test_calendar_waits_for_change_not_fixed_rest():
    """달력은 열릴 때까지, 넘긴 뒤에는 달이 바뀔 때까지만 기다린다. 이미 떠 있고 바로 바뀌면 고정 쉼이 끼지 않는다."""
    p = Page()
    ss.human_actions(p, [{"cal_month": "2026-11"}], look=False, timing=[])
    assert p.clicks == 1
    assert max(p.waits) <= max(ss.PRE_CLICK[1], ss.HOLD_MS[1], 160)       # 40ms 확인 간격 이상으로 기다린 일이 없다


def test_hour_slider_waits_for_active_window_change():
    p = Page()
    ss.human_actions(p, [{"cal_hour": 7}], look=False, timing=[])           # 0~4 에서 7시로: 다음 1번 누르고 7시 칸 누름
    assert p.clicks == 2
    assert max(p.waits) <= 160


def test_waits_until_the_value_changes_then_goes(monkeypatch):
    """값이 아직 안 바뀌면 40ms 씩 기다리고, 바뀐 값이 두 번 연속 같으면(멎으면) 곧바로 다음으로 간다."""
    p = Page()
    seq = iter([None, None, "2026-11", "2026-11"])
    monkeypatch.setattr(p, "evaluate", lambda js, arg=None: next(seq))
    ss.human_actions(p, [{"cal_month": "2026-11"}], look=False)
    assert p.waits == [40, 40, 40] and p.clicks == 0


def test_stale_box_retargets_before_click(monkeypatch):
    """누르기 직전에 칸이 움직이면(열리는 중) 새 자리를 겨눈 뒤 누른다."""
    p = Page()
    boxes = iter([{"x": 10, "y": 20, "width": 100, "height": 30}, {"x": 10, "y": 60, "width": 100, "height": 30},
                  {"x": 10, "y": 60, "width": 100, "height": 30}])
    monkeypatch.setattr(Loc, "bounding_box", lambda self: next(boxes))
    ss.human_actions(p, [{"click": "a"}], look=False)
    assert p.clicks == 1 and 40 in p.waits[:-1]


def test_no_fixed_long_sleep_left_in_click_path():
    src = open(ss.__file__, encoding="utf-8").read()
    for bad in ("randint(150, 450)", "randint(50, 130)", "randint(200, 600)", "randint(600, 1200)", "(500, 1300)", "wait_for_timeout(1500)"):
        assert bad not in src, bad
