"""예약대기 챙기기: 등록·상태 전이·저장/복원·기한 알림.

이 기능은 코레일에 접속하지 않는다. 그 점도 테스트로 묶어 둔다.
"""

import json
from datetime import timedelta

from conftest import FAMILY, OWNER, FakeKorail, day, ktx_train, run

from srtgo import bot as botmod


def at(ymd, time_="0000"):
    return botmod.Reminder.at(ymd, time_)


async def add(p, date=None, dep="서울", arr="부산", text="101 0900 어른 2"):
    """예약대기 한 건을 화면으로 등록한다."""
    date = date or day(10)
    await p.command("start")
    await p.press("rm")
    await p.press("rmnew")
    await p.press(f"rmdate:{date}")
    if f"rmdep:{dep}" not in p.screen.data():
        await p.press("rmall:dep")
    await p.press(f"rmdep:{dep}")
    if f"rmarr:{arr}" not in p.screen.data():
        await p.press("rmall:arr")
    await p.press(f"rmarr:{arr}")
    if text is None:
        await p.press("rmskip")
    else:
        await p.say(text)
    return date


def one(env, chat_id=OWNER):
    u = env.bot.context_for(chat_id)
    assert len(u.reminders) == 1, u.reminders
    return next(iter(u.reminders.values()))


# --- 등록 ------------------------------------------------------------------
def test_register_shows_in_list_and_is_saved(env):
    async def go():
        date = await add(env.owner)
        m = one(env)
        assert (m.date, m.dep, m.arr, m.train_no, m.dep_time, m.pax) == (
            date, "서울", "부산", "101", "0900", "어른 2"
        )
        assert m.state == "등록"
        assert "서울→부산" in env.owner.screen.text and "101 09:00" in env.owner.screen.text
        saved = json.loads(env.path.read_text(encoding="utf-8"))["reminders"][OWNER]
        assert saved[0]["date"] == date and saved[0]["train_no"] == "101"

    run(go())


def test_register_without_train_info(env):
    async def go():
        await add(env.owner, text=None)
        m = one(env)
        assert (m.train_no, m.dep_time) == ("", "")
        assert "열차 미정" in env.owner.screen.text

    run(go())


def test_register_never_touches_korail(env):
    """챙기기는 코레일에 요청을 보내지 않는다 (막힌 원인이 연속조회였다)."""

    async def go():
        rail = FakeKorail([ktx_train(101, "090000", "120000")])
        env.rails[(OWNER, "KTX")] = rail
        await add(env.owner)
        await env.bot._standby_tick(at(day(10), "0900"))
        assert rail.searches == 0 and rail.reserve_calls == []

    run(go())


def test_cap_per_user(env):
    async def go():
        u = env.bot.context_for(OWNER)
        for i in range(botmod.MAX_REMINDERS_PER_USER):
            u.reminders[f"x{i}"] = botmod.Reminder(id=f"x{i}", dep="서울", arr="부산", date=day(5))
        u.save_reminders()
        await env.owner.command("start")
        await env.owner.press("rm")
        assert "rmnew" not in env.owner.screen.data()
        await env.owner.tap("rmnew")
        assert "건까지만" in env.owner.screen.text

    run(go())


# --- 입력 파싱 --------------------------------------------------------------
def test_parse_train_text():
    parse = botmod.Bot.parse_train_text
    assert parse("101 0900 어른 2") == ("101", "0900", "어른 2")
    assert parse("101") == ("101", "", "")
    assert parse("09:00") == ("", "0900", "")
    assert parse("0900 101") == ("101", "0900", "")
    assert parse("") == ("", "", "")
    # 시각이 될 수 없는 네 자리는 열차번호로 본다
    assert parse("2599") == ("2599", "", "")


# --- 상태 전이 --------------------------------------------------------------
def test_state_flow_applied_assigned_paid(env):
    async def go():
        await add(env.owner)
        m = one(env)
        await env.owner.press(f"rmopen:{m.id}")
        await env.owner.press(f"rmstate:{m.id}:applied")
        assert m.state == "신청함"
        await env.owner.press(f"rmopen:{m.id}")
        await env.owner.press(f"rmstate:{m.id}:assigned")
        assert m.state == "배정됨"
        assert m.assigned_date == botmod.kst_now().strftime("%Y%m%d")
        assert m.pay_deadline == botmod.PAY_DEADLINE_DEFAULT
        await env.owner.press(f"rmopen:{m.id}")
        await env.owner.press(f"rmstate:{m.id}:paid")
        assert m.state == "결제함"
        saved = json.loads(env.path.read_text(encoding="utf-8"))["reminders"][OWNER]
        assert saved[0]["state"] == "결제함"

    run(go())


def test_pay_deadline_can_be_changed_and_bad_input_rejected(env):
    async def go():
        await add(env.owner)
        m = one(env)
        await env.owner.press(f"rmopen:{m.id}")
        await env.owner.press(f"rmstate:{m.id}:assigned")
        await env.owner.press(f"rmopen:{m.id}")
        await env.owner.press(f"rmpay:{m.id}")
        await env.owner.say("아무거나")
        assert m.pay_deadline == "1800" and "네 자리" in env.owner.screen.text
        await env.owner.say("2400")
        assert m.pay_deadline == "1800"
        await env.owner.say("20:30")
        assert m.pay_deadline == "2030" and "20:30" in env.owner.screen.text

    run(go())


def test_changing_deadline_lets_pay_notices_fire_again(env):
    """기한을 고치면 이미 보낸 결제 알림은 지워야 새 기한으로 다시 울린다."""

    async def go():
        await add(env.owner)
        m = one(env)
        m.state, m.assigned_date = "배정됨", day(0)
        m.sent = ["payam", "pay2h"]
        env.bot.context_for(OWNER).save_reminders()
        await env.owner.press(f"rmopen:{m.id}")
        await env.owner.press(f"rmpay:{m.id}")
        await env.owner.say("2030")
        assert m.sent == []

    run(go())


def test_delete(env):
    async def go():
        await add(env.owner)
        m = one(env)
        await env.owner.press(f"rmopen:{m.id}")
        await env.owner.press(f"rmdel:{m.id}")
        await env.owner.press(f"rmdelok:{m.id}")
        assert env.bot.context_for(OWNER).reminders == {}
        assert json.loads(env.path.read_text(encoding="utf-8"))["reminders"] == {}

    run(go())


# --- 저장·복원 --------------------------------------------------------------
def test_survives_restart(env, tmp_path):
    async def go():
        await add(env.owner)
        m = one(env)
        m.state = "신청함"
        env.bot.context_for(OWNER).save_reminders()

        store = botmod.StateStore(env.path)
        fresh = botmod.Bot(OWNER, store, tg=env.tg)
        back = fresh.context_for(OWNER).reminders
        assert len(back) == 1
        again = next(iter(back.values()))
        assert (again.id, again.state, again.train_no) == (m.id, "신청함", "101")

    run(go())


def test_broken_entry_is_dropped_not_crashing(env):
    store = env.store
    store.data["reminders"][OWNER] = [
        {"dep": "서울", "arr": "부산", "date": "20261001", "state": "등록"},
        {"dep": "서울", "date": "엉망"},
        {"dep": "서울", "arr": "부산", "date": "20261002", "state": "없는상태"},
    ]
    store.save()
    u = botmod.Bot(OWNER, botmod.StateStore(env.path), tg=env.tg).context_for(OWNER)
    assert [m.date for m in u.reminders.values()] == ["20261001"]


# --- 알림 ------------------------------------------------------------------
def keys(m, now):
    return [k for k, _, _ in botmod.Bot.reminder_notices(m, now)]


def make(date, **kw):
    return botmod.Reminder(dep="서울", arr="부산", date=date, train_no="101", dep_time="0900", **kw)


def test_apply_deadline_notices():
    date = "20261101"  # 신청 마감 10/30 23:59
    m = make(date)
    assert keys(m, at("20261029", "0859")) == []
    assert keys(m, at("20261029", "0900")) == ["apply3"]
    assert keys(m, at("20261030", "0900")) == ["apply3", "apply2"]
    assert keys(m, at("20261030", "1800")) == ["apply3", "apply2", "apply2e"]
    # 마감이 지나면 신청 알림은 더 없다
    assert "apply2" not in keys(m, at("20261031", "0900"))


def test_already_sent_keys_are_not_repeated():
    m = make("20261101", sent=["apply3"])
    assert keys(m, at("20261030", "0900")) == ["apply2"]


def test_cancel_window_only_for_open_states():
    m = make("20261101")
    assert keys(m, at("20261031", "0900")) == ["cancelwin"]
    m.state = "신청함"
    assert keys(m, at("20261031", "0900")) == ["cancelwin"]
    m.state = "결제함"
    assert keys(m, at("20261031", "0900")) == []


def test_pay_notices_and_miss():
    m = make("20261101", state="배정됨", assigned_date="20261030")
    assert keys(m, at("20261030", "0859")) == []
    assert keys(m, at("20261030", "0900")) == ["payam"]
    assert keys(m, at("20261030", "1600")) == ["payam", "pay2h"]
    assert keys(m, at("20261030", "1735")) == ["payam", "pay2h", "pay30m"]
    # 기한이 지나면 남은 재촉은 안 하고, 놓쳤다고만 알린다
    assert keys(m, at("20261030", "1811")) == ["paymiss"]
    assert keys(m, at("20261031", "1900")) == [], "하루 넘은 건 조용히 둔다"


def test_departure_day_notice_only_when_paid():
    m = make("20261101", state="결제함")
    assert keys(m, at("20261101", "0700")) == ["depday"]
    assert keys(m, at("20261101", "0930")) == [], "출발 시각이 지나면 안 보낸다"


def test_pay_deadline_respects_custom_time():
    m = make("20261101", state="배정됨", assigned_date="20261030", pay_deadline="2030")
    assert "pay30m" not in keys(m, at("20261030", "1735"))
    assert "pay30m" in keys(m, at("20261030", "2005"))


def test_finished_states_get_nothing():
    for state in botmod.Reminder.DONE:
        m = make("20261101", state=state, assigned_date="20261030")
        assert keys(m, at("20261030", "0900")) == [], state


# --- tick ------------------------------------------------------------------
def test_tick_sends_once_and_marks_state(env):
    async def go():
        await add(env.owner, date=day(3))
        m = one(env)
        m.state, m.assigned_date, m.pay_deadline = "배정됨", day(0), "1800"
        env.bot.context_for(OWNER).save_reminders()
        before = len(env.tg.to(OWNER))

        now = at(day(0), "1735")
        assert await env.bot._standby_tick(now) is True
        after = env.tg.to(OWNER)[before:]
        assert len(after) == 3 and "30분" in after[-1].text
        # 같은 시각에 다시 돌아도 또 보내지 않는다
        assert await env.bot._standby_tick(now) is False
        assert len(env.tg.to(OWNER)) == before + 3

        await env.bot._standby_tick(at(day(0), "1811"))
        assert m.state == "놓침"
        assert "기한이 지났습니다" in env.tg.to(OWNER)[-1].text

    run(go())


def test_tick_skips_notices_whose_deadline_already_passed(env):
    """봇이 꺼져 있던 동안 지나간 알림은 켜졌을 때 쏟아지지 않는다."""

    async def go():
        await add(env.owner, date=day(3))
        m = one(env)
        m.state, m.assigned_date = "배정됨", day(-2)
        env.bot.context_for(OWNER).save_reminders()
        before = len(env.tg.to(OWNER))
        await env.bot._standby_tick(at(day(0), "1200"))
        assert len(env.tg.to(OWNER)) == before, env.tg.texts(OWNER)

    run(go())


def test_tick_closes_departed_items_without_message(env):
    async def go():
        await add(env.owner, date=day(1))
        m = one(env)
        before = len(env.tg.to(OWNER))
        await env.bot._standby_tick(botmod.kst_now() + timedelta(days=2))
        assert m.state == "종료" and len(env.tg.to(OWNER)) == before

    run(go())


def test_tick_drops_reminders_of_removed_user(env):
    async def go():
        await add(env.family, date=day(5))
        env.store.forget_user(FAMILY)
        await env.bot._standby_tick(at(day(5), "0900"))
        assert FAMILY not in env.store.data["reminders"]

    run(go())


def test_home_shows_waiting_count(env):
    async def go():
        await add(env.owner, date=day(5))
        await env.owner.command("start")
        assert "예약대기 1건" in env.owner.screen.text
        assert "rm" in env.owner.screen.data()

    run(go())


def test_register_after_apply_deadline_says_so(env):
    """신청 마감이 지난 열차를 등록하면 지난 날짜를 신청하라고 하지 않고 마감이 지났다고 알린다."""

    async def go():
        await add(env.owner, date=day(1))  # 내일 출발 = 신청 마감은 이미 지났다
        text = env.owner.screen.text
        assert "이미 지나" in text and "신청하세요" not in text, text
        assert "취소표" in text
        assert one(env).state == "등록"

    run(go())


def test_register_before_apply_deadline_asks_to_apply(env):
    async def go():
        await add(env.owner, date=day(10))
        assert "신청하세요" in env.owner.screen.text and "이미 지나" not in env.owner.screen.text

    run(go())
