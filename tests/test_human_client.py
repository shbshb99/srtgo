import json
from pathlib import Path

import pytest

from srtgo import human_client as hc
from srtgo.ktx import NoResultsError, Train, TrainType

FIX = json.loads((Path(__file__).parent / "fixtures" / "korail_list_20261030.json").read_text(encoding="utf-8"))

# 2026-10-29 실제 화면에서 읽은 줄: 입석+좌석·매진 칸(049), 일반실 가능·특실 매진임박(125)
ROW_SOLD = ('<li class="tckList clear "><div class="tck_inner"><div class="info_inner fl-l"><div class="info_box"><div class="tit_box type02"><div class="flag_wrap type2"><span class="train_ktx_ticket "><span class="blind">KTX</span></span><span class="num "> 049</span></div></div><div class="data_box right"><h3 class="txt_bk"><span>서울</span> → <span>부산</span><span>(15:45 ~ 18:03)</span></h3><p class="s_txt">소요시간: 2시간 18분</p></div></div></div><div class="price_box fl-l yms "><div class="inner type02"><a><div class="tck_etc_small_text"><div class="tck_etc_use">입석 + 좌석</div></div></a></div></div><div class="price_box fl-l sold_out "><div class="inner type02"><a><div class="tck_etc_text"><div class="tck_etc_use">매진</div></div></a></div></div></div></li>')
ROW_SOON = ('<li class="tckList clear "><div class="tck_inner"><div class="info_inner fl-l"><div class="info_box"><div class="tit_box type02"><div class="flag_wrap type2"><span class="train_ktx_ticket "><span class="blind">KTX</span></span><span class="num "> 125</span><p class="">수원정차</p></div></div><div class="data_box right"><h3 class="txt_bk"><span>서울</span> → <span>부산</span><span>(16:02 ~ 19:23)</span></h3><p class="s_txt">소요시간: 3시간 21분</p></div></div></div><div class="price_box fl-l gen "><div class="inner type02"><a><p class="txt_ch">일반실</p><p class="txt_price txt_bk">47,500원</p><p class="txt_gr">5%적립</p></a></div></div><div class="price_box fl-l spe sold_out_soon"><div class="inner type02"><a><p class="txt_ch">특실(매진임박)</p><p class="txt_price txt_bk">68,100원</p><p class="txt_gr">5%적립</p></a></div></div></div></li>')


def test_parse_real_page():
    rows = hc.parse_rows(FIX["rows"], "20261030")
    assert len(rows) == len(FIX["rows"]) == 10
    t = Train(rows[0])
    assert (t.train_type_name, t.train_no) == ("KTX", "001")
    assert (t.dep_name, t.arr_name, t.dep_time, t.arr_time) == ("서울", "부산", "051300", "075000")
    assert t.has_general_seat() and t.has_special_seat()
    assert Train(rows[2]).train_type_name.startswith("ITX")
    assert not Train(rows[2]).has_special_seat()      # 특실 칸이 '-' 이면 자리 없음


def test_parse_sold_out_and_soon():
    a = Train(hc.parse_row(ROW_SOLD, "20261029"))
    assert not a.has_seat() and a.reserve_possible == "N"
    b = Train(hc.parse_row(ROW_SOON, "20261029"))
    assert b.has_general_seat() and b.has_special_seat() and b.reserve_possible == "Y"
    assert b.train_no == "125"


def test_parse_broken_row_is_skipped():
    assert hc.parse_row("<li>이상한 줄</li>", "20261030") is None


def test_build_actions_hour_and_month():
    from datetime import datetime
    now = datetime(2026, 10, 29)
    acts = hc.build_actions("서울", "부산", "20261030", 8, today=now)
    assert sum(1 for a in acts if a.get("click") == ".slide_list .slick-prev >> visible=true") == 4   # 먼저 맨 앞으로
    assert sum(1 for a in acts if a.get("click") == ".slide_list .slick-next >> visible=true") == 1
    assert any('"08시"' in a.get("click", "") for a in acts)
    assert not any(a.get("click") == ".slick-next >> visible=true >> nth=0" for a in acts)
    late = hc.build_actions("서울", "부산", "20261130", 23, today=now)
    assert sum(1 for a in late if a.get("click") == ".slide_list .slick-next >> visible=true") == 4
    assert any(a.get("click") == ".slick-next >> visible=true >> nth=0" for a in late)
    assert all("js" not in a for a in acts)          # 값 넣기는 진짜 입력 동작만
    with pytest.raises(hc.HumanFetchError):
        hc.build_actions("서울", "부산", "20260101", 8, today=now)


def test_search_train_filters(monkeypatch):
    monkeypatch.setattr(hc, "fetch_list", lambda dep, arr, date, hour: {"rows": FIX["rows"] + [ROW_SOLD]})
    got = hc.search_train("서울", "부산", "20261030", "060000", include_no_seats=True)
    assert all(t.dep_time >= "060000" for t in got)
    ktx = hc.search_train("서울", "부산", "20261030", "050000", train_type=TrainType.KTX)
    assert ktx and all(t.train_type_name.startswith("KTX") and t.has_seat() for t in ktx)
    monkeypatch.setattr(hc, "fetch_list", lambda *a: {"rows": []})
    with pytest.raises(NoResultsError):
        hc.search_train("서울", "부산", "20261030", "050000")


def test_pacing_waits_between_calls(monkeypatch, tmp_path):
    slept = []
    now = [100.0]
    monkeypatch.setattr(hc, "_sleep", lambda s: slept.append(s))
    monkeypatch.setattr(hc, "_clock", lambda: now[0])
    monkeypatch.setattr(hc, "_last", [None])
    monkeypatch.setattr(hc._pacer, "next_delay", lambda: 8.0)
    monkeypatch.setattr(hc, "HUMAN_FETCH_PS1", str(tmp_path / "없는파일.ps1"))   # 이 PC 에 직접 길이 없으면 통로(HTTP)로 간다
    monkeypatch.setattr(hc, "_run_remote", lambda actions, stamp: json.dumps({"rows": []}))
    monkeypatch.setattr(hc, "OUT_DIR", tmp_path)
    hc.fetch_list("서울", "부산", "20261030", 8)
    assert slept == []                               # 첫 조회는 기다리지 않는다
    now[0] += 3.0
    hc.fetch_list("서울", "부산", "20261030", 8)
    assert slept and abs(slept[0] - 5.0) < 1e-6      # 간격 8초 중 3초가 지났으니 5초 쉰다


def test_ktx_search_train_uses_human_path(monkeypatch):
    from srtgo import ktx
    monkeypatch.setenv("SRTGO_HUMAN_FETCH", "1")
    seen = {}
    monkeypatch.setattr(hc, "search_train", lambda *a, **k: seen.setdefault("args", (a, k)) or [])
    k = ktx.Korail.__new__(ktx.Korail)
    k.search_train("서울", "부산", "20261030", "090000")
    assert seen["args"][0][:4] == ("서울", "부산", "20261030", "090000")


def test_bot_search_button_uses_human_path_without_login(env, monkeypatch):
    """열차 검색 버튼은 로그인한 철도사 없이 사람처럼 조회 통로로 간다. 끄면(0) 예전처럼 철도사로 간다."""
    from conftest import FakeKorail, OWNER, day, ktx_train, run
    seen = []
    train = ktx_train(527, "200900", "231200")
    monkeypatch.setattr(hc, "search_train", lambda **kw: seen.append(kw) or [train])
    monkeypatch.setenv("SRTGO_HUMAN_FETCH", "1")
    rail = FakeKorail([ktx_train(131, "210000", "234500")])
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        o = env.owner
        await o.command("start")
        await o.press("book:KTX")
        await o.press("dep:서울")
        await o.press("arr:부산")
        await o.press(f"date:{day()}")
        await o.press("time:19")
        await o.press("search")
        assert any("KTX 527" in x for x in o.screen.labels()), o.screen.labels()

    run(go())
    assert len(seen) == 1 and seen[0]["dep"] == "서울" and rail.searches == 0
