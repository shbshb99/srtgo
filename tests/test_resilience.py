"""대기 루프: 어떤 오류에도 조용히 죽지 않고, 중복 예매를 하지 않고, 재시작해도 이어간다."""

import asyncio
import json
import threading

from conftest import (
    FAMILY,
    OWNER,
    FakeKorail,
    FakeSRT,
    FakeTG,
    day,
    fast,
    ktx_reservation,
    ktx_train,
    run,
    srt_train,
    until,
)
from srtgo import bot as botmod
from srtgo import ktx, srt


def make_watch(trains, rail_type="KTX", date=None, **kw):
    return botmod.Watch(
        rail_type=rail_type,
        dep="서울" if rail_type == "KTX" else "수서",
        arr="부산",
        date=date or day(),
        time="190000",
        counts={"adult": 1},
        selected=[botmod.train_key(t) for t in trains],
        titles=[botmod.train_title(t) for t in trains],
        seat_option="GENERAL_FIRST",
        **kw,
    )


def start(env, chat_id, trains, rail_type="KTX", **kw):
    u = env.bot.context_for(chat_id)
    w = make_watch(trains, rail_type, **kw)
    env.bot.start_watch(u, w)
    return u, w


# --- 중복 예매 방지 ------------------------------------------------------
def test_korail_list_return_counts_as_success(env):
    """코레일 reserve() 는 예약 번호로 못 찾으면 전체 목록(list)을 돌려준다.
    예전엔 이걸 오류로 보고 다시 예약해 중복 예매가 될 수 있었다."""
    t = ktx_train(527, "200900", "231200", general="11")
    rail = FakeKorail([t])
    rail.return_list = True
    rail.held.append(ktx_reservation(ktx_train(99, "080000", "103000"), "OLD"))
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        u, w = start(env, OWNER, [t])
        await until(lambda: not u.watches)
        await asyncio.sleep(0.05)
        assert len(rail.reserve_calls) == 1
        text = env.tg.texts(OWNER)
        assert "🎉 예매 성공" in text and "8호차 3D" in text
        assert "00099" not in text, "다른 열차 예약까지 섞여 보임"

    run(go())


def test_reserved_but_lookup_crashed_is_found_not_rebooked(env):
    """서버엔 예약이 잡혔는데 그 뒤 조회에서 터진 경우 (예전 ticket_info None 버그 등)."""
    t = ktx_train(527, "200900", "231200", general="11")
    rail = FakeKorail([t])
    rail.after_reserve_error = TypeError("cannot unpack non-iterable NoneType object")
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        u, w = start(env, OWNER, [t])
        await until(lambda: not u.watches)
        await asyncio.sleep(0.05)
        assert len(rail.reserve_calls) == 1, "잡힌 예약을 모르고 또 예약함"
        assert len(rail.held) == 1
        assert "🎉 예매 성공" in env.tg.texts(OWNER)

    run(go())


def test_existing_reservation_of_same_train_is_not_mistaken_for_new(env):
    t = ktx_train(527, "200900", "231200", general="11")
    rail = FakeKorail([t])
    rail.held.append(ktx_reservation(t, "MINE-BEFORE"))  # 대기 전부터 있던 같은 열차 예약
    rail.reserve_error = RuntimeError("Read timed out")  # 이번 시도는 안 잡힘
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        u, w = start(env, OWNER, [t])
        await until(lambda: len(rail.reserve_calls) >= 1)
        await asyncio.sleep(0.05)
        assert u.watches, "예전 예약을 새 예약으로 착각하고 대기를 끝냄"
        rail.reserve_error = None
        await until(lambda: not u.watches)
        assert "🎉 예매 성공" in env.tg.texts(OWNER)
        assert [r.rsv_id for r in rail.held][0] == "MINE-BEFORE" and len(rail.held) == 2

    run(go())


def test_unverifiable_result_stops_instead_of_rebooking(env):
    t = ktx_train(527, "200900", "231200", general="11")
    rail = FakeKorail([t])
    rail.after_reserve_error = RuntimeError("Connection reset by peer")
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        rail.break_list_on_reserve = True  # 확인 조회도 안 된다
        u, w = start(env, OWNER, [t])
        await until(lambda: not u.watches)
        assert len(rail.reserve_calls) == 1
        text = env.tg.texts(OWNER)
        assert "결과를 확인하지 못했습니다" in text and "예매내역을 꼭 확인" in text
        assert "rv:KTX" in env.tg.latest(OWNER).data()

    run(go())


def test_stop_pressed_during_reservation_still_reports_result(env):
    t = ktx_train(527, "200900", "231200", general="11")
    rail = FakeKorail([t])
    rail.reserve_gate = threading.Event()
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        u, w = start(env, OWNER, [t])
        await until(lambda: rail.reserve_calls)
        env.bot.stop_watch(u, w.id)  # 예약이 서버에서 처리되는 중에 중지
        rail.reserve_gate.set()
        await until(lambda: "🎉 예매 성공" in env.tg.texts(OWNER))
        assert not u.watches

    run(go())


def test_repeated_rejections_back_off_and_alert(env):
    t = ktx_train(527, "200900", "231200", general="11")
    rail = FakeKorail([t])
    rail.reserve_error = ktx.KorailError("1인당 예약 가능 매수를 초과하였습니다", "ERR1")
    env.rails[(OWNER, "KTX")] = rail
    waits = []
    env.bot._backoff = lambda n: waits.append(n) or 0.005

    async def go():
        u, w = start(env, OWNER, [t])
        await until(lambda: "예약이 계속 거절됩니다" in env.tg.texts(OWNER))
        assert u.watches, "거절됐다고 대기를 끝냄"
        assert waits[:3] == [1, 2, 3], f"거절이 이어지는데 간격을 안 늘림 {waits}"
        assert "매수를 초과" in env.bot.status_text(u)
        env.bot.stop_watch(u, w.id)

    run(go())


def test_sold_out_on_reserve_is_not_an_error(env):
    t = ktx_train(527, "200900", "231200", general="11")
    rail = FakeKorail([t])
    rail.reserve_error = ktx.SoldOutError("ERR211161")
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        u, w = start(env, OWNER, [t])
        await until(lambda: len(rail.reserve_calls) >= 5)
        assert w.last_error is None and w.error_count == 0
        env.bot.stop_watch(u, w.id)

    run(go())


# --- 조회 오류 ------------------------------------------------------------
def test_search_errors_keep_retrying_quietly_then_alert_and_recover(env, monkeypatch):
    t = ktx_train(527, "200900", "231200")
    rail = FakeKorail([t])
    errors = [RuntimeError("DNSError: Could not resolve host")] * 4
    rail.search_error = lambda: errors.pop(0) if errors else None
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        u, w = start(env, OWNER, [t])
        await until(lambda: not errors and w.last_ok_at)
        text = env.tg.texts(OWNER)
        assert "조회가 안 되고 있습니다" not in text, "잠깐 끊긴 걸로 알림을 보냄"
        assert w.error_count == 4 and w.last_error is None
        env.bot.stop_watch(u, w.id)

        monkeypatch.setattr(botmod, "ERROR_ALERT_AFTER_SECONDS", 0)
        errors.extend([RuntimeError("DNSError")] * 3)
        u, w = start(env, OWNER, [t])
        await until(lambda: not errors and w.last_ok_at)
        text = env.tg.texts(OWNER)
        assert text.count("조회가 안 되고 있습니다") == 1, "알림 도배"
        assert "다시 정상적으로 조회하고 있습니다" in text
        env.bot.stop_watch(u, w.id)

    run(go())


def test_session_expiry_logs_in_again(env):
    t = ktx_train(527, "200900", "231200")
    rail = FakeKorail([t])
    builds = []

    def build():
        builds.append(1)
        return rail

    env.rails[(OWNER, "KTX")] = build
    errors = [ktx.NeedToLoginError("P058")]
    rail.search_error = lambda: errors.pop(0) if errors else None

    async def go():
        u, w = start(env, OWNER, [t])
        await until(lambda: len(builds) >= 2 and rail.searches >= 3)
        assert w.last_error is None
        env.bot.stop_watch(u, w.id)

    run(go())


def test_srt_netfunnel_error_clears_key(env):
    t = srt_train(301, "053000", "080000")
    rail = FakeSRT([t])
    errors = [srt.SRTNetFunnelError("Failed to complete NetFunnel"), srt.SRTResponseError("정상적인 경로로 접근 부탁드립니다")]
    rail.search_error = lambda: errors.pop(0) if errors else None
    env.rails[(OWNER, "SRT")] = rail

    async def go():
        u, w = start(env, OWNER, [t], rail_type="SRT")
        await until(lambda: rail.cleared >= 2 and w.last_ok_at)
        env.bot.stop_watch(u, w.id)

    run(go())


def test_congestion_message_is_not_an_error(env):
    t = srt_train(301, "053000", "080000")
    rail = FakeSRT([t])
    rail.search_error = srt.SRTResponseError("사용자가 많아 접속이 원활하지 않습니다")
    env.rails[(OWNER, "SRT")] = rail

    async def go():
        u, w = start(env, OWNER, [t], rail_type="SRT")
        await until(lambda: rail.searches >= 5)
        assert w.error_count == 0
        env.bot.stop_watch(u, w.id)

    run(go())


def test_departed_trains_end_the_watch(env):
    old = "20200101"
    t = ktx_train(527, "200900", "231200", date=old)
    rail = FakeKorail([t])
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        u, w = start(env, OWNER, [t], date=old)
        await until(lambda: not u.watches)
        assert rail.searches == 0
        assert "모두 출발해 대기를 끝냅니다" in env.tg.texts(OWNER)

    run(go())


def test_unexpected_bug_is_reported_not_silent(env):
    t = ktx_train(527, "200900", "231200", general="11")
    env.rails[(OWNER, "KTX")] = FakeKorail([t])

    def broken(train, w):
        raise KeyError("oops")

    env.bot.can_book = broken

    async def go():
        u, w = start(env, OWNER, [t])
        await until(lambda: not u.watches)
        assert "⛔ 대기가 멈췄습니다" in env.tg.texts(OWNER)

    run(go())


# --- 재시작 ---------------------------------------------------------------
def test_watches_survive_restart(env):
    t = ktx_train(527, "200900", "231200")
    rail = FakeKorail([t])
    env.rails[(FAMILY, "KTX")] = rail

    async def first():
        u, w = start(env, FAMILY, [t])
        gone = make_watch([ktx_train(1, "080000", "100000", date="20200101")], date="20200101")
        u.watches[gone.id] = gone
        env.bot.persist(u)
        await until(lambda: rail.searches >= 2)
        await env.bot.shutdown()

    run(first())
    saved = json.loads(env.path.read_text())
    assert len(saved["watches"][FAMILY]) == 2, "꺼질 때 대기가 저장되지 않음"
    assert "pw" not in env.path.read_text()

    tg2 = FakeTG()
    bot2 = fast(botmod.Bot(OWNER, botmod.StateStore(env.path), tg=tg2))

    async def second():
        await bot2.resume_watches()
        u2 = bot2.context_for(FAMILY)
        assert len(u2.watches) == 1
        text = tg2.texts(FAMILY)
        assert "대기를 이어갑니다" in text and "출발해 끝난 대기" in text
        before = rail.searches
        await until(lambda: rail.searches > before)
        t.general_seat = "11"
        await until(lambda: not u2.watches)
        assert "🎉 예매 성공" in tg2.texts(FAMILY)
        assert json.loads(env.path.read_text())["watches"] == {}

    run(second())


def test_resume_drops_watches_of_revoked_users_and_broken_entries(env):
    env.store.data["watches"] = {
        "999": [make_watch([ktx_train(527, "200900", "231200")]).to_dict()],
        FAMILY: [{"rail_type": "KTX", "garbage": True}],
    }
    env.store.save()

    async def go():
        await env.bot.resume_watches()
        assert env.store.data["watches"] == {}
        assert not env.tg.to("999")

    run(go())


def test_corrupt_state_file_is_moved_aside(tmp_path):
    path = tmp_path / "bot_state.json"
    path.write_text("{ 이건 json 이 아님", encoding="utf-8")
    store = botmod.StateStore(path)
    assert store.data["watches"] == {} and store.data["users"] == {}
    assert list(tmp_path.glob("bot_state.broken-*.json"))


def test_status_reflects_reality(env):
    t = ktx_train(527, "200900", "231200")
    rail = FakeKorail([t])
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        u, w = start(env, OWNER, [t])
        await until(lambda: w.last_ok_at)
        assert "✅ 정상 (마지막 조회" in env.bot.status_text(u)
        rail.search_error = RuntimeError("Read timed out")
        await until(lambda: w.last_error)
        assert "재시도 중: RuntimeError: Read timed out" in env.bot.status_text(u)
        rail.search_error = None
        await until(lambda: w.last_error is None)
        text = env.bot.status_text(u)
        assert "Read timed out" not in text and "모두 자동 복구" in text
        env.bot.stop_watch(u, w.id)

    run(go())


def test_many_watches_on_one_rail_slow_down_each(env):
    bot = botmod.Bot(OWNER, botmod.StateStore(), tg=FakeTG())
    u = bot.context_for(OWNER)
    for i in range(4):
        w = make_watch([ktx_train(500 + i, "200900", "231200")])
        u.watches[w.id] = w
    one = sum(bot._interval("SRT") for _ in range(400)) / 400
    four = sum(bot._interval("KTX") for _ in range(400)) / 400
    assert four > one * 1.6, (one, four)
