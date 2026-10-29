import io
import json
import urllib.error

import pytest

from srtgo import human_client as hc
from srtgo import scrape_server as ss

COND = {"dep": "동탄", "arr": "순천", "date": "20261030", "hour": 9}


def form(start="서울", end="부산", date="2026-10-29(목) 17:00"):
    return {"start": start, "end": end, "date": date, "rows": 0, "list": False}


def lst(start="동탄", end="순천", date="2026-10-30(금) 09:00", rows=2):
    return {"start": start, "end": end, "date": date, "rows": rows, "list": True}


def clicks(plan):
    return [a.get("click") or a for a in plan["actions"]]


def test_unreadable_screen_means_full_input():
    assert ss.plan_korail(None, COND)["mode"] == "full"
    assert ss.plan_korail({"start": None, "end": None, "date": None}, COND)["mode"] == "full"


def test_same_values_on_list_only_reloads():
    p = ss.plan_korail(lst(), COND)
    assert p["mode"] == "refresh" and p["changed"] == []
    assert p["actions"][0] == {"reload": True}
    assert not any("click" in a for a in p["actions"])      # 이미 선택된 값은 다시 고르지 않는다


def test_same_values_on_form_only_presses_lookup():
    p = ss.plan_korail(form("동탄", "순천", "2026-10-30(금) 09:00"), COND)
    assert p["mode"] == "lookup"
    assert [a.get("click") for a in p["actions"] if "click" in a] == [".btn_lookup"]


def test_only_hour_differs_on_list():
    p = ss.plan_korail(lst(date="2026-10-30(금) 08:00"), COND)
    assert p["mode"] == "edit" and p["changed"] == ["hour"]
    cs = clicks(p)
    assert "input[name=startDate]" in cs and {"cal_hour": 9} in cs
    assert not any("txtGo" in str(c) or "StationPop" in str(c) for c in cs)   # 역은 건드리지 않는다
    assert not any(c == ".btn_lookup" for c in cs)                              # 목록 화면의 .btn_lookup 은 '다음날 조회'
    assert p["actions"][-1] == {"settle": 3500} and p["actions"][0] == {"mark_rows": True}
    assert not any("cal_month" in a for a in p["actions"])                      # 날짜가 같으면 날짜는 다시 고르지 않는다


def test_day_change_picks_month_and_day():
    p = ss.plan_korail(lst(date="2026-10-29(목) 09:00"), COND)
    assert p["changed"] == ["day"]
    assert {"cal_month": "2026-10"} in p["actions"]
    assert any('span.day:text-is("30")' in str(a.get("click")) for a in p["actions"])
    assert {"cal_hour": 9} in p["actions"]      # 달력을 열었으니 시각도 고른 뒤 적용한다


def test_station_change_on_list_uses_station_buttons():
    p = ss.plan_korail(lst(start="서울", end="순천"), COND)
    assert p["changed"] == ["dep"]
    assert ".btn_pop-openStationPop >> nth=0" in clicks(p)
    assert 'a:text-is("동탄") >> visible=true' in clicks(p)
    assert ".btn_pop-openStationPop >> nth=1" not in clicks(p)


def test_swapped_stations_use_swap_button():
    p = ss.plan_korail(lst(start="순천", end="동탄"), COND)
    assert ".btnChange >> visible=true" in clicks(p)
    assert not any("StationPop" in str(c) for c in clicks(p))


def test_form_edit_ends_with_lookup_and_uses_input_fields():
    p = ss.plan_korail(form(), COND)
    assert set(p["changed"]) == {"dep", "arr", "day", "hour"}
    cs = clicks(p)
    assert "input[name=txtGoStart]" in cs and "input[name=txtGoEnd]" in cs
    assert cs[-2] == ".btn_lookup" and p["actions"][-1]["wait_for"] == "li.tckList"
    assert not any(a.get("mark_rows") or a.get("settle") for a in p["actions"])


def test_mid_clicks_rest_short_and_only_final_lookup_is_long():
    p = ss.plan_korail(form(), COND)
    for a in p["actions"]:
        if "click" in a and a["click"] != ".btn_lookup":
            assert a["rest"] == ss.REST_MID
    assert "rest" not in next(a for a in p["actions"] if a.get("click") == ".btn_lookup")


@pytest.mark.parametrize("first,last,hour,want", [
    (17, 21, 17, []),                       # 지금 보이는 칸이면 그대로
    (17, 21, 9, ["prev", "prev"]),          # 17~21 → 12~16 → 7~11
    (0, 4, 9, ["next"]),                    # 0~4 → 5~9
    (0, 4, 23, ["next"] * 4),               # 0~4 → 5~9 → 10~14 → 15~19 → 19~23(끝에서 멈춤)
    (19, 23, 3, ["prev"] * 4),
])
def test_hour_window_clicks(first, last, hour, want):
    assert ss.hour_window_clicks(first, last, 24, hour) == want


def test_client_gives_old_servers_a_full_action_list_with_short_rests():
    acts = hc.build_actions("동탄", "순천", "20261030", 9, today=__import__("datetime").datetime(2026, 10, 29))
    assert acts[-2] == {"click": ".btn_lookup"}
    assert all(a.get("rest") == [200, 600] for a in acts[:-2] if "click" in a)
    assert not any("wait" in a for a in acts[:-1])    # 사이 고정 대기는 없앴다


def test_error_text_reads_http_body_up_to_600_chars():
    body = json.dumps({"error": "TargetClosedError", "detail": "가" * 900}).encode()
    ex = urllib.error.HTTPError("http://x", 502, "Bad Gateway", {}, io.BytesIO(body))
    text = hc._error_text(ex)
    assert text.startswith("통로 오류 502 TargetClosedError: ") and 500 < len(text) <= hc.ERROR_TEXT_LIMIT


def test_no_server_never_spawns_one(monkeypatch):
    monkeypatch.setattr(hc, "_health", lambda api: None)
    monkeypatch.setattr(hc, "_session_id", lambda: 0)
    slept = []
    monkeypatch.setattr(hc, "_sleep", lambda s: slept.append(s))
    with pytest.raises(hc.HumanFetchError) as e:
        hc._ensure_local_server()
    assert "ShbScrapeApi" in str(e.value) and slept == []      # 세션 0 이면 기다리지도 않는다
    assert not hasattr(hc, "subprocess")


def test_screen_session_waits_briefly_then_reports(monkeypatch):
    monkeypatch.setattr(hc, "_health", lambda api: None)
    monkeypatch.setattr(hc, "_session_id", lambda: 2)
    slept = []
    monkeypatch.setattr(hc, "_sleep", lambda s: slept.append(s))
    with pytest.raises(hc.HumanFetchError, match="ShbScrapeApi"):
        hc._ensure_local_server()
    assert len(slept) == 12


def test_old_server_is_asked_to_quit_and_new_one_is_awaited(monkeypatch):
    seen = {"health": [{"ok": True, "v": 1}, None, {"ok": True, "v": hc.WANT_SERVER_VERSION}], "req": []}
    monkeypatch.setattr(hc, "_health", lambda api: seen["health"].pop(0) if len(seen["health"]) > 1 else seen["health"][0])
    monkeypatch.setattr(hc, "_sleep", lambda s: None)

    class R:
        def read(self):
            return b""

    monkeypatch.setattr(hc.urllib.request, "urlopen", lambda req, timeout=0: seen["req"].append(req.full_url) or R())
    hc._ensure_local_server()
    assert seen["req"] == [hc.LOCAL_API + "/quit"]


def test_server_version_matches_client():
    assert ss.SERVER_VERSION == hc.WANT_SERVER_VERSION
