"""계정 연결·로그인 실패 처리·다중 사용자(승인제)·권한 경계."""

import json
from types import SimpleNamespace

import pytest

from conftest import (
    FAMILY,
    OWNER,
    STRANGER,
    FakeKorail,
    Phone,
    day,
    ktx_reservation,
    ktx_train,
    run,
    until,
)
from srtgo import bot as botmod
from srtgo import srt
from test_booking import start_watch


class RejectingKorail:
    """코레일은 로그인에 실패해도 예외를 던지지 않는다. logined 로만 알 수 있다."""

    def __init__(self, user_id, password):
        self.logined = False
        self.login_error = "비밀번호가 일치하지 않습니다."


class OkKorail:
    def __init__(self, user_id, password):
        self.logined = True
        self.login_error = None
        self.name = "김가족"


def new_user(env, chat_id="400"):
    env.store.set_user(chat_id, status="approved", name="새식구")
    return Phone(env.bot, env.tg, chat_id, "새식구")


def test_ktx_wrong_password_is_not_saved(env, monkeypatch):
    """예전엔 KTX 로그인 실패를 성공으로 알고 틀린 비밀번호를 저장했다."""
    monkeypatch.setattr(botmod, "Korail", RejectingKorail)
    p = new_user(env)

    async def go():
        await p.command("settings")
        await p.press("acct")
        assert "KTX: ⬜ 연결 안 됨" in p.screen.text
        await p.press("link:KTX")
        m1 = await p.say("010-1234-5678")
        m2 = await p.say("wrong-pw")
        assert m1.deleted and m2.deleted, "계정 정보 메시지를 지우지 않음"
        assert "로그인 실패: 비밀번호가 일치하지 않습니다." in p.screen.text
        assert botmod.Creds.get("400", "KTX") == (None, None)
        assert "wrong-pw" not in env.tg.texts("400")

    run(go())


def test_link_success_saves_and_shows_name(env, monkeypatch):
    monkeypatch.setattr(botmod, "Korail", OkKorail)
    p = new_user(env)

    async def go():
        await p.command("settings")
        await p.press("acct")
        await p.press("link:KTX")
        await p.say("010-1234-5678")
        await p.say("good-pw")
        assert "✅ KTX 계정을 연결했습니다 (김가족님)" in p.screen.text
        assert botmod.Creds.get("400", "KTX") == ("010-1234-5678", "good-pw")
        assert "good-pw" not in env.path.read_text(), "비밀번호가 상태 파일에 들어감"
        await p.press("acct")
        assert "KTX: ✅ 연결됨 (010*" in p.screen.text
        await p.press("unlink:KTX")
        await p.press("unlinkok:KTX")
        assert botmod.Creds.get("400", "KTX") == (None, None)

    run(go())


def test_pressing_a_button_cancels_pending_input(env):
    """아이디를 기다리는 중에 다른 버튼을 누르면, 나중 메시지를 계정으로 받지 않는다."""
    p = new_user(env)

    async def go():
        await p.command("settings")
        await p.press("acct")
        await p.press("link:SRT")
        await p.press("acct")  # 취소
        msg = await p.say("아무 말")
        assert not msg.deleted and "버튼으로 조작" in p.screen.text
        assert botmod.Creds.get("400", "SRT") == (None, None)

    run(go())


def test_open_rail_classifies_login_failures(monkeypatch):
    monkeypatch.setattr(botmod, "Korail", RejectingKorail)
    with pytest.raises(botmod.RailLoginError) as e:
        botmod.open_rail("KTX", "id", "pw")
    assert e.value.permanent, "비밀번호 오류를 재시도 대상으로 봄 (계정 잠김 위험)"

    class MacroKorail(RejectingKorail):
        def __init__(self, *a):
            super().__init__(*a)
            self.login_error = "MACRO ERROR"

    monkeypatch.setattr(botmod, "Korail", MacroKorail)
    with pytest.raises(botmod.RailLoginError) as e:
        botmod.open_rail("KTX", "id", "pw")
    assert not e.value.permanent

    def srt_reject(msg):
        def make(*a):
            raise srt.SRTLoginError(msg)

        return make

    monkeypatch.setattr(botmod, "SRT", srt_reject("비밀번호 오류"))
    with pytest.raises(botmod.RailLoginError) as e:
        botmod.open_rail("SRT", "id", "pw")
    assert e.value.permanent
    monkeypatch.setattr(botmod, "SRT", srt_reject("Your IP Address Blocked"))
    with pytest.raises(botmod.RailLoginError) as e:
        botmod.open_rail("SRT", "id", "pw")
    assert not e.value.permanent


def test_build_rail_uses_own_account_then_pc_and_never_prompts(fake_keyring, monkeypatch):
    calls = []
    monkeypatch.setattr(botmod, "open_rail", lambda rt, i, p: calls.append((rt, i, p)) or "rail")
    # 계정이 없으면 (예전처럼 터미널 입력을 기다리지 않고) 바로 실패
    with pytest.raises(botmod.RailLoginError) as e:
        botmod.build_rail("KTX", OWNER, True)
    assert e.value.permanent
    fake_keyring.set_password("KTX", "id", "pc-id")
    fake_keyring.set_password("KTX", "pass", "pc-pw")
    botmod.build_rail("KTX", OWNER, True)
    with pytest.raises(botmod.RailLoginError):
        botmod.build_rail("KTX", FAMILY, False)  # 가족은 오너의 PC 계정을 못 쓴다
    botmod.Creds.set(OWNER, "KTX", "tg-id", "tg-pw")
    botmod.build_rail("KTX", OWNER, True)
    assert calls == [("KTX", "pc-id", "pc-pw"), ("KTX", "tg-id", "tg-pw")]
    assert botmod.account_source(OWNER, "KTX", True) == "bot"
    assert botmod.account_source(FAMILY, "KTX", False) is None


def test_watch_stops_on_rejected_login_without_hammering(env):
    rail = FakeKorail([ktx_train(527, "200900", "231200")])
    env.rails[(FAMILY, "KTX")] = rail
    attempts = []

    def rejected():
        attempts.append(1)
        raise botmod.RailLoginError("KTX", "비밀번호가 일치하지 않습니다.", True)

    async def swap():
        env.rails[(FAMILY, "KTX")] = rejected  # 그새 비밀번호가 바뀌었다

    async def go():
        await start_watch(env.family, "KTX", "서울", "부산", ["tr:527:200900"], before_go=swap)
        u = env.bot.context_for(FAMILY)
        await until(lambda: not u.watches)
        assert len(attempts) == 1, "틀린 비밀번호로 계속 로그인 시도"
        text = env.tg.texts(FAMILY)
        assert "로그인이 거부되어 대기를 멈췄습니다" in text and "link:KTX" in env.tg.latest(FAMILY).data()

    run(go())


def test_watch_retries_temporary_login_failures(env):
    rail = FakeKorail([ktx_train(527, "200900", "231200")])
    env.rails[(FAMILY, "KTX")] = rail
    seq = [botmod.RailLoginError("KTX", "MACRO ERROR", False), RuntimeError("DNSError"), rail]

    def flaky():
        item = seq.pop(0) if seq else rail
        if isinstance(item, Exception):
            raise item
        return item

    async def swap():
        env.rails[(FAMILY, "KTX")] = flaky

    async def go():
        await start_watch(env.family, "KTX", "서울", "부산", ["tr:527:200900"], before_go=swap)
        u = env.bot.context_for(FAMILY)
        (w,) = u.watches.values()
        await until(lambda: rail.searches >= 3)
        assert w.error_count == 2 and w.last_error is None, "복구 후에도 오류가 남음"
        assert "✅ 정상" in env.bot.status_text(u)

    run(go())


# --- 다중 사용자 -------------------------------------------------------------
def test_stranger_asks_owner_and_gets_approved(env):
    async def go():
        s, o = env.stranger, env.owner
        await s.command("start")
        req = [m for m in env.tg.to(OWNER) if "사용을 요청" in m.text]
        assert req and "홍길동 (@gildong)" in req[0].text
        assert {f"approve:{STRANGER}", f"deny:{STRANGER}"} <= set(req[0].data())
        assert "승인을 요청했습니다" in env.tg.texts(STRANGER)

        # 다시 말 걸면 조용히 무시하지 않고 기다리라고 알려 주되, 오너를 도배하지 않는다
        before = len(env.tg.to(OWNER))
        await s.say("아직이에요?")
        assert "승인을 기다리는 중" in s.screen.text
        assert len(env.tg.to(OWNER)) == before
        # 승인 전에는 버튼을 눌러도 아무것도 안 된다
        await s.tap("book:KTX")
        assert "승인을 기다리는 중" in s.screen.text

        # 오너 메뉴에 승인 대기 표시
        await o.command("start")
        assert "👥 사용자 관리 (승인 대기 1)" in o.screen.labels()
        await o.press(f"approve:{STRANGER}", on=req[0])
        assert env.store.status(STRANGER) == "approved"
        assert "사용이 승인되었습니다" in env.tg.texts(STRANGER)
        await s.command("start")
        assert "book:KTX" in s.screen.data()

    run(go())


def test_family_cannot_use_owner_only_actions(env):
    env.keyring.set_password("card", "ok", "1")
    rail = FakeKorail()
    rail.held.append(ktx_reservation(ktx_train(527, "200900", "231200"), "P9"))
    env.rails[(FAMILY, "KTX")] = rail

    async def go():
        f = env.family
        await f.tap(f"approve:{STRANGER}")
        assert env.store.status(STRANGER) is None
        await f.tap("users")
        assert "사용자 관리" not in f.screen.text
        await f.tap(f"revokeok:{OWNER}")
        assert env.store.status(OWNER) == "approved"
        await f.command("start")
        assert "users" not in f.screen.data()

        # 오너 카드로 결제하는 길은 가족에게 없다 (버튼도, 억지 콜백도)
        await f.press("rv:KTX")
        await f.press("1. ")
        assert not any(d.startswith("rvpay") for d in f.screen.data())
        tag = botmod.item_tag(rail.held[0])
        await f.tap(f"rvpay:0:{tag}")
        await f.tap(f"rvpayok:0:{tag}")
        assert rail.paid == []

        rail.trains = [ktx_train(527, "200900", "231200")]
        await f.command("start")
        await f.press("book:KTX")
        await f.press("dep:서울")
        await f.press("arr:부산")
        await f.press(f"date:{day()}")
        await f.press("time:19")
        await f.press("search")
        await f.press("tr:527:200900")
        await f.press("trok")
        assert "autopay" not in f.screen.data()
        await f.tap("autopay")
        assert env.bot.context_for(FAMILY).draft.pay is False

    run(go())


def test_owner_pays_and_cancels_from_reservation_list(env):
    env.keyring.set_password("card", "ok", "1")
    for k, v in {"number": "1234", "password": "12", "birthday": "900101", "expire": "2912"}.items():
        env.keyring.set_password("card", k, v)
    rail = FakeKorail()
    r1 = ktx_reservation(ktx_train(527, "200900", "231200"), "P1")
    r2 = ktx_reservation(ktx_train(131, "210000", "234500"), "P2")
    rail.held += [r1, r2]
    env.rails[(OWNER, "KTX")] = rail

    async def go():
        o = env.owner
        await o.command("start")
        await o.press("rv:KTX")
        assert "KTX 예매내역 (2건)" in o.screen.text
        await o.press("1. ")
        await o.press("💳 카드로 결제")
        assert rail.paid == [], "확인 없이 결제함"
        await o.press("💳 결제")
        assert rail.paid == [r1] and "결제 완료" in o.screen.text

        await o.press("2. ")
        await o.press("❌ 예약 취소")
        assert r2 in rail.held, "확인 없이 취소함"
        await o.press("❌ 예약 취소 확정")
        assert rail.held == [r1] and "예약 취소 완료" in o.screen.text

    run(go())


def test_cancel_with_stale_index_does_not_cancel_the_wrong_one(env):
    rail = FakeKorail()
    r1 = ktx_reservation(ktx_train(527, "200900", "231200"), "P1")
    r2 = ktx_reservation(ktx_train(131, "210000", "234500"), "P2")
    rail.held += [r1, r2]
    env.rails[(FAMILY, "KTX")] = rail

    async def go():
        f = env.family
        await f.command("start")
        await f.press("rv:KTX")
        await f.press("1. ")
        await f.press("❌ 예약 취소")
        confirm = f.screen
        # 목록을 새로 불러오니 순서가 바뀌었다 (1번 자리에 다른 예약)
        rail.held.reverse()
        await f.command("start")
        await f.press("rv:KTX")
        await f.press("❌ 예약 취소 확정", on=confirm)
        assert rail.held == [r2, r1], "바뀐 목록에서 엉뚱한 예약을 취소함"
        assert "목록이 바뀌어" in f.screen.text

    run(go())


def test_revoke_stops_watches_and_clears_accounts(env):
    env.rails[(FAMILY, "KTX")] = FakeKorail([ktx_train(527, "200900", "231200")])

    async def go():
        f, o = env.family, env.owner
        await start_watch(f, "KTX", "서울", "부산", ["tr:527:200900"])
        u = env.bot.context_for(FAMILY)
        assert u.watches
        await o.command("start")
        await o.press("users")
        assert "엄마" in o.screen.text and "대기 1건" in o.screen.text
        await o.press(f"revoke:{FAMILY}")
        await o.press(f"revokeok:{FAMILY}")
        assert not u.watches and json.loads(env.path.read_text())["watches"] == {}
        assert botmod.Creds.get(FAMILY, "KTX") == (None, None)
        assert env.store.status(FAMILY) == "denied"
        assert "사용 권한이 해제되었습니다" in env.tg.texts(FAMILY)
        n = len(env.tg.to(FAMILY))
        await f.say("왜요")
        assert len(env.tg.to(FAMILY)) == n, "해제된 사람에게 응답함"

        await o.press(f"forget:{FAMILY}")
        await f.command("start")
        assert any("사용을 요청" in m.text and "엄마" in m.text for m in env.tg.to(OWNER))

    run(go())


def test_old_keyring_user_list_is_migrated(fake_keyring, tmp_path):
    fake_keyring.set_password(
        "srtgo-bot", "users", json.dumps({"200": {"status": "approved", "name": "엄마"}})
    )
    store = botmod.StateStore(tmp_path / "s.json")
    botmod.migrate_users(store)
    assert store.status("200") == "approved"
    assert json.loads((tmp_path / "s.json").read_text())["users"]["200"]["name"] == "엄마"


def test_group_chats_are_ignored(env):
    async def go():
        up = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-555, type="group"),
            effective_user=SimpleNamespace(full_name="누구", username=None),
            message=None,
            callback_query=None,
        )
        assert await env.bot._gate(up) is None
        assert env.store.status("-555") is None
        assert not env.tg.to(OWNER)

    run(go())
