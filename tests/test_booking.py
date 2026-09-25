"""텔레그램 예매 흐름: 역 -> 날짜 -> 시각 -> 인원 -> 검색 -> 열차 -> 좌석 -> 대기 -> 예매."""

import asyncio
import json

from conftest import (
    FAMILY,
    OWNER,
    FakeKorail,
    FakeSRT,
    day,
    ktx_train,
    run,
    srt_train,
    until,
)
from srtgo import bot as botmod
from srtgo import ktx, srt


async def start_watch(p, rail_type, dep, arr, picks, hour="19", date=None, before_go=None):
    await p.command("start")
    await p.press(f"book:{rail_type}")
    if f"dep:{dep}" not in p.screen.data():
        await p.press("stall:dep")
    await p.press(f"dep:{dep}")
    if f"arr:{arr}" not in p.screen.data():
        await p.press("stall:arr")
    await p.press(f"arr:{arr}")
    await p.press(f"date:{date or day()}")
    await p.press(f"time:{hour}")
    await p.press("search")
    for pick in picks:
        await p.press(pick)
    await p.press("trok")
    if before_go:
        await before_go()
    await p.press("go")


def test_ktx_full_flow_reserves_once(env):
    t527 = ktx_train(527, "200900", "231200", wait="9")  # 매진, 예약대기 가능
    t131 = ktx_train(131, "210000", "234500")  # 매진
    rail = FakeKorail([t527, t131])
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        o = env.owner
        await o.command("start")
        assert {"book:KTX", "book:SRT", "rv:KTX", "status", "stop", "set", "users"} <= set(o.screen.data())

        await o.press("book:KTX")
        # 자주 쓰는 역이 먼저, 전체 역은 버튼으로
        assert "dep:서울" in o.screen.data() and "dep:용산" not in o.screen.data()
        await o.press("stall:dep")
        assert {"dep:용산", "dep:여수EXPO", "dep:진부(오대산)"} <= set(o.screen.data())
        await o.press("dep:용산")
        await o.press("stall:arr")
        assert "arr:용산" not in o.screen.data(), "출발역이 도착역 목록에 남음"
        await o.press("arr:여수EXPO")

        # 날짜: 예매 가능한 기간 전체 (예전엔 16일만 보였다)
        dates = [d for d in o.screen.data() if d.startswith("date:")]
        assert len(dates) == len(botmod.Bot.booking_days("KTX")) >= 31
        await o.press(f"date:{day()}")
        assert len([d for d in o.screen.data() if d.startswith("time:")]) == 24
        await o.press("time:19")
        await o.press("pax:adult:+")
        assert "어른/청소년 2명" in o.screen.labels()

        await o.press("search")
        labels = o.screen.labels()
        # 좌석 상태가 잘리지 않고 보인다 (예전엔 60자에서 잘려 예약대기가 안 보였다)
        assert any("20:09→23:12 KTX 527 · 매진·대기 가능" in x for x in labels), labels
        assert any("21:00→23:45 KTX 131 · 매진" in x for x in labels), labels
        assert rail.last_params["dep"] == "용산" and rail.last_params["time"] == "190000"
        assert rail.last_params["passengers"][0].count == 2

        await o.press("tr:527:200900")
        assert any(x.startswith("✅") and "527" in x for x in o.screen.labels())
        await o.press("trok")
        await o.press("seat:GENERAL_ONLY")
        await o.press("standby")  # 예약대기 끄기
        assert "⬜ 매진이면 예약대기 신청" in o.screen.labels()
        await o.press("go")
        assert "대기를 시작했습니다" in o.screen.text

        u = env.bot.context_for(OWNER)
        (w,) = u.watches.values()
        assert w.selected == [("527", day(), "200900")]
        assert json.loads(env.path.read_text())["watches"][OWNER][0]["selected"] == [["527", day(), "200900"]]

        # 매진인데 예약대기를 껐으니 예약하지 않는다
        await until(lambda: rail.searches >= 6)
        assert rail.reserve_calls == []

        # 자리가 나면 한 번만 예약한다
        t527.general_seat = "11"
        await until(lambda: not u.watches)
        await asyncio.sleep(0.05)
        assert len(rail.reserve_calls) == 1, "중복 예매"
        train, passengers, option = rail.reserve_calls[0]
        assert train is t527
        assert [(type(p).__name__, p.count) for p in passengers] == [("AdultPassenger", 2)]
        assert option == ktx.ReserveOption.GENERAL_ONLY
        text = env.tg.texts(OWNER)
        assert "🎉 예매 성공" in text and "8호차 3D" in text
        assert json.loads(env.path.read_text())["watches"] == {}

    run(go())


def test_srt_train_is_identified_and_reserved(env):
    """예전엔 SRT 열차 번호(train_number)를 못 읽어 모든 SRT 열차가 같은 열차로 보였다."""
    t301 = srt_train(301, "053000", "080000")
    t303 = srt_train(303, "060000", "083000", general="예약가능")
    rail = FakeSRT([t301, t303])
    env.rails[(OWNER, "SRT")] = rail

    async def go():
        o = env.owner
        await start_watch(o, "SRT", "수서", "부산", ["tr:301:053000"], hour="05")
        u = env.bot.context_for(OWNER)
        (w,) = u.watches.values()
        assert w.selected == [("301", day(), "053000")]
        await until(lambda: rail.searches >= 4)
        assert rail.reserve_calls == [], "고르지 않은 303을 예약함"

        t301.general_seat_state = "예약가능"
        await until(lambda: not u.watches)
        assert [c[0] for c in rail.reserve_calls] == [t301]
        assert rail.reserve_calls[0][2] == srt.SeatType.GENERAL_FIRST
        assert "🎉 예매 성공" in env.tg.texts(OWNER)

    run(go())


def test_srt_standby_is_used_when_sold_out(env):
    t = srt_train(301, "053000", "080000", wait="9")
    rail = FakeSRT([t])
    env.rails[(FAMILY, "SRT")] = rail

    async def go():
        await start_watch(env.family, "SRT", "수서", "부산", ["tr:301:053000"], hour="05")
        u = env.bot.context_for(FAMILY)
        await until(lambda: not u.watches)
        text = env.tg.texts(FAMILY)
        assert "⏳ 예약대기 신청 완료" in text
        assert "코레일톡 앱" not in text and "결제" in text

    run(go())


def test_reservation_list_does_not_disturb_running_watch(env):
    """예전엔 예매내역을 한 번 보기만 해도 돌던 대기의 철도 종류가 바뀌어 망가졌다."""
    t = ktx_train(527, "200900", "231200")
    rail = FakeKorail([t])
    other = FakeSRT([])
    env.rails[(OWNER, "KTX")] = rail
    env.rails[(OWNER, "SRT")] = other

    async def go():
        o = env.owner
        await start_watch(o, "KTX", "서울", "부산", ["tr:527:200900"])
        u = env.bot.context_for(OWNER)
        (w,) = u.watches.values()

        await o.press("home")
        await o.press("rv:SRT")
        assert "SRT 예매내역이 없습니다" in o.screen.text
        await o.press("home")
        await o.press("book:SRT")
        await o.press("dep:수서")

        assert w.rail_type == "KTX" and w.params()["dep"] == "서울"
        t.general_seat = "11"
        await until(lambda: not u.watches)
        assert len(rail.reserve_calls) == 1 and other.reserve_calls == []
        # 예매 중이던 SRT 조건은 그대로 남아 있다
        assert u.draft.rail_type == "SRT" and u.draft.dep == "수서"

    run(go())


def test_old_buttons_after_restart_give_guidance(env):
    async def go():
        o = env.owner
        for data in ("tr:527:200900", "date:20990101", "search", "go", "back:pax", "route:last"):
            await o.tap(data)
            assert "예매 진행 정보가 없습니다" in o.screen.text, data
        await o.tap("rvp:0:abcdef")
        assert "예매내역을 다시 열어" in o.screen.text
        await o.tap("whatever:1")
        assert "오래된 버튼" in o.screen.text
        await o.tap("stop:deadbe")
        assert "이미 끝난 대기" in o.screen.text

    run(go())


def test_stray_text_and_unknown_command_get_a_hint(env):
    async def go():
        f = env.family
        await f.say("안녕")
        assert "버튼으로 조작" in f.screen.text and "book:KTX" in f.screen.data()
        await f.command("foo")
        assert "모르는 명령" in f.screen.text
        await f.command("help")
        assert "/status" in f.screen.text and "대기는 한 사람당" in f.screen.text

    run(go())


def test_favorite_stations_editor(env):
    async def go():
        f = env.family
        u = env.bot.context_for(FAMILY)
        await f.command("settings")
        await f.press("fav:KTX")
        assert "⭐ 서울" in f.screen.labels()
        await f.press("favt:KTX:용산")
        await f.press("favt:KTX:서울")
        labels = f.screen.labels()
        assert "⭐ 용산" in labels and "서울" in labels and "⭐ 서울" not in labels

        await f.press("favadd:KTX")
        await f.say("진부(오대산), 태화강 abc")
        assert "추가했습니다: 진부(오대산), 태화강" in f.screen.text
        assert "추가하지 못함: abc" in f.screen.text
        assert u.stations("KTX") == ["대전", "동대구", "부산", "용산", "진부(오대산)", "태화강"]

        # SRT는 SRT가 서는 역만
        await f.press("set")
        await f.press("fav:SRT")
        await f.press("favadd:SRT")
        await f.say("서울, 평택지제")
        assert "추가했습니다: 평택지제" in f.screen.text and "추가하지 못함: 서울" in f.screen.text

        # 예매할 때 자주 쓰는 역이 그 순서로 나온다
        await f.command("start")
        await f.press("book:KTX")
        deps = [d for d in f.screen.data() if d.startswith("dep:")]
        assert deps == [f"dep:{s}" for s in ["대전", "동대구", "부산", "용산", "진부(오대산)", "태화강"]]

        # 마지막 하나는 뺄 수 없다
        u.set_stations("KTX", ["부산"])
        await f.command("settings")
        await f.press("fav:KTX")
        await f.press("favt:KTX:부산")
        assert "최소 1개" in f.screen.text and u.stations("KTX") == ["부산"]

        await f.press("favreset:KTX")
        assert u.stations("KTX") == ["서울", "대전", "동대구", "부산"]
        saved = json.loads(env.path.read_text())
        assert saved["prefs"][FAMILY]["stations"]["SRT"][-1] == "평택지제"

    run(go())


def test_passenger_types_and_ktx_only(env):
    rail = FakeKorail([ktx_train(527, "200900", "231200")])
    env.rails[(FAMILY, "KTX")] = rail

    async def go():
        f = env.family
        await f.command("settings")
        await f.press("paxt:child")
        await f.press("ktxonly")
        labels = f.screen.labels()
        assert "✅ 어린이" in labels and "✅ KTX만 검색 (KTX 예매)" in labels

        await f.command("start")
        await f.press("book:KTX")
        await f.press("dep:서울")
        await f.press("arr:부산")
        await f.press(f"date:{day()}")
        await f.press("time:19")
        assert "어린이 0명" in f.screen.labels() and "경로우대 0명" not in f.screen.labels()
        await f.press("pax:child:+")
        for _ in range(20):
            await f.press("pax:adult:+")
        assert sum(env.bot.context_for(FAMILY).draft.counts.values()) == botmod.MAX_PASSENGERS
        for _ in range(7):
            await f.press("pax:adult:-")
        await f.press("search")
        assert rail.last_params["train_type"] == ktx.TrainType.KTX
        assert rail.last_params["passengers"][0].count == 2

        await f.press("tr:527:200900")
        await f.press("trok")
        await f.press("go")
        u = env.bot.context_for(FAMILY)
        (w,) = u.watches.values()
        assert sorted((type(p).__name__, p.count) for p in w.passengers()) == [
            ("AdultPassenger", 1),
            ("ChildPassenger", 1),
        ]
        assert w.params()["train_type"] == ktx.TrainType.KTX
        env.bot.stop_watch(u, w.id)

    run(go())


def test_recent_route_shortcuts_and_back_buttons(env):
    rail = FakeKorail([ktx_train(527, "200900", "231200")])
    env.rails[(FAMILY, "KTX")] = rail

    async def go():
        f = env.family
        await f.command("start")
        await f.press("book:KTX")
        await f.press("dep:서울")
        await f.press("arr:부산")
        await f.press(f"date:{day()}")
        await f.press("back:date")
        assert any(d.startswith("date:") for d in f.screen.data())
        await f.press("back:arr")
        assert "arr:대전" in f.screen.data()
        await f.press("arr:부산")
        await f.press(f"date:{day()}")
        await f.press("time:10")
        await f.press("pax:adult:+")
        await f.press("search")

        await f.press("home")
        await f.press("book:KTX")
        assert "route:last" in f.screen.data() and "🔁 부산→서울" in f.screen.labels()
        await f.press("route:rev")
        assert "부산 → 서울" in f.screen.text
        await f.press(f"date:{day()}")
        await f.press("time:10")
        assert "어른/청소년 2명" in f.screen.labels(), "지난 인원을 기억하지 않음"

    run(go())


def test_no_trains_found_is_explained(env):
    env.rails[(FAMILY, "KTX")] = FakeKorail([])  # 코레일은 결과가 없으면 NoResultsError

    async def go():
        f = env.family
        await f.command("start")
        await f.press("book:KTX")
        await f.press("dep:서울")
        await f.press("arr:부산")
        await f.press(f"date:{day()}")
        await f.press("time:23")
        await f.press("search")
        assert "맞는 열차가 없습니다" in f.screen.text and "back:time" in f.screen.data()

    run(go())


def test_multiple_watches_per_user_and_caps(env, monkeypatch):
    rail = FakeKorail([ktx_train(527, "200900", "231200"), ktx_train(131, "210000", "234500")])
    env.rails[(FAMILY, "KTX")] = rail
    env.rails[(OWNER, "KTX")] = FakeKorail([ktx_train(527, "200900", "231200")])

    async def go():
        f = env.family
        await start_watch(f, "KTX", "서울", "부산", ["tr:527:200900"])
        await start_watch(f, "KTX", "부산", "서울", ["tr:131:210000"])
        u = env.bot.context_for(FAMILY)
        assert len(u.watches) == 2
        await until(lambda: all(w.last_ok_at for w in u.watches.values()))
        await f.command("status")
        text = f.screen.text
        assert "대기 상황 (2건)" in text and "서울→부산" in text and "부산→서울" in text
        assert "✅ 정상 (마지막 조회" in text and "👤 홍길동" in text

        monkeypatch.setattr(botmod, "MAX_WATCHES_PER_USER", 2)
        await start_watch(f, "KTX", "서울", "부산", ["tr:131:210000"])
        assert "한 사람당 2건까지" in f.screen.text and len(u.watches) == 2

        monkeypatch.setattr(botmod, "MAX_CONCURRENT_WATCHES", 2)
        await start_watch(env.owner, "KTX", "서울", "부산", ["tr:527:200900"])
        assert "전체 대기가 2건" in env.owner.screen.text

        await f.command("stop")
        assert "stop:all" in f.screen.data()
        first = next(iter(u.watches))
        await f.press(f"stop:{first}")
        assert len(u.watches) == 1 and "멈췄습니다" in f.screen.text
        await f.command("stop")  # 하나 남으면 바로 멈춘다
        assert not u.watches and "대기를 멈췄습니다" in f.screen.text
        assert json.loads(env.path.read_text())["watches"] == {}

    run(go())


def test_quick_taps_are_handled_in_order(env):
    """'열차 선택' 직후 바로 '다음'을 누르면, 먼저 누른 것부터 처리돼야 한다."""
    env.rails[(FAMILY, "KTX")] = FakeKorail([ktx_train(527, "200900", "231200")])

    async def go():
        f = env.family
        await f.command("start")
        await f.press("book:KTX")
        await f.press("dep:서울")
        await f.press("arr:부산")
        await f.press(f"date:{day()}")
        await f.press("time:19")
        await f.press("search")
        screen = f.screen
        # 첫 번째 누름의 응답이 늦게 와도 두 번째가 앞지르지 않는다
        await asyncio.gather(
            f.press("tr:527:200900", on=screen, delay=0.05),
            f.press("trok", on=screen),
        )
        assert "좌석 유형" in f.screen.text, f.screen.text

    run(go())
