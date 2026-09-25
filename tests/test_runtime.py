"""워치독, 앱 구성, 실행 코드, 코레일 라이브러리 수정분."""

import asyncio
import json
import logging
import sys
import time
from types import SimpleNamespace

from telegram.error import BadRequest, Conflict

from conftest import OWNER, FakeTG, run, until
from srtgo import bot as botmod
from srtgo import ktx, watchdog


# --- 워치독 ---------------------------------------------------------------
def test_lock_is_exclusive(tmp_path):
    a = watchdog.acquire_lock(tmp_path / "x.lock")
    assert a is not None
    assert watchdog.acquire_lock(tmp_path / "x.lock") is None
    a.close()
    b = watchdog.acquire_lock(tmp_path / "x.lock")
    assert b is not None
    b.close()


def test_hung_bot_is_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        watchdog,
        "BOT_COMMAND",
        [sys.executable, "-c", "import time; print('봇 출력', flush=True); time.sleep(60)"],
    )
    monkeypatch.setattr(watchdog, "STARTUP_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(watchdog, "POLL_SECONDS", 0.1)
    started = time.time()
    code, ran, reason = watchdog.run_once(logging.getLogger("t"), tmp_path / "heartbeat")
    assert reason == "하트비트 없음" and time.time() - started < 10
    assert "봇 출력" in (tmp_path / "bot.console.log").read_text(encoding="utf-8")


def test_healthy_bot_is_left_alone_and_exit_code_passed(tmp_path, monkeypatch):
    script = (
        "import time, pathlib\n"
        f"p = pathlib.Path(r'{tmp_path}') / 'heartbeat'\n"
        "for _ in range(12):\n"
        "    p.write_text(str(time.time())); time.sleep(0.1)\n"
        "raise SystemExit(3)\n"
    )
    monkeypatch.setattr(watchdog, "BOT_COMMAND", [sys.executable, "-c", script])
    monkeypatch.setattr(watchdog, "STARTUP_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(watchdog, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(watchdog, "HEARTBEAT_STALE_SECONDS", 2)
    code, ran, reason = watchdog.run_once(logging.getLogger("t"), tmp_path / "heartbeat")
    assert code == 3 and reason is None


def test_watchdog_command_starts_the_bot_module():
    assert watchdog.BOT_COMMAND[1:] == ["-m", "srtgo.bot"]


# --- 앱 -------------------------------------------------------------------
def test_application_builds_with_all_handlers():
    bot = botmod.Bot(OWNER, botmod.StateStore(), tg=FakeTG())
    app = botmod.build_application(bot, "123456:TESTTOKEN")
    names = [type(h).__name__ for h in app.handlers[0]]
    assert names.count("CommandHandler") == 5
    assert "CallbackQueryHandler" in names and names.count("MessageHandler") == 3
    assert app.error_handlers


def test_post_init_registers_command_menu_and_heartbeat(env, tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "HEARTBEAT_SECONDS", 0.01)
    env.bot.heartbeat_path = tmp_path / "heartbeat"
    app = SimpleNamespace(bot=env.tg, updater=SimpleNamespace(running=True))

    async def go():
        await env.bot.post_init(app)
        assert [c.command for c in env.tg.commands] == ["start", "status", "stop", "settings", "help"]
        await until(lambda: env.bot.heartbeat_path.exists())
        await env.bot.shutdown()

    run(go())


def test_heartbeat_stops_when_polling_died(env, tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "HEARTBEAT_SECONDS", 0.01)
    env.bot.heartbeat_path = tmp_path / "heartbeat"
    app = SimpleNamespace(bot=env.tg, updater=SimpleNamespace(running=False))

    async def go():
        await env.bot.post_init(app)
        await asyncio.sleep(0.1)
        assert not env.bot.heartbeat_path.exists(), "텔레그램 수신이 멈췄는데 살아 있다고 보고함"
        await env.bot.shutdown()

    run(go())


def test_run_bot_exit_codes(monkeypatch, tmp_path, fake_keyring):
    monkeypatch.setenv("SRTGO_HOME", str(tmp_path))
    assert botmod.run_bot() == 2  # 텔레그램 설정 없음
    lock = watchdog.acquire_lock(tmp_path / "bot.lock")
    assert botmod.run_bot() == watchdog.EXIT_ALREADY_RUNNING
    lock.close()


def test_conflict_is_reported_to_owner_once(env):
    ctx = SimpleNamespace(error=Conflict("terminated by other getUpdates request"), bot=env.tg)

    async def go():
        await env.bot.on_error(None, ctx)
        await env.bot.on_error(None, ctx)

    run(go())
    assert sum("다른 PC에서도 실행 중" in m.text for m in env.tg.to(OWNER)) == 1


def test_screen_falls_back_to_new_message_when_edit_fails(env):
    class Query:
        data = "home"

        def __init__(self, error):
            self.error = error
            self.message = None

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, *a, **k):
            raise self.error

    async def go():
        u = env.bot.context_for(OWNER)
        await botmod.Reply(env.bot, u, Query(BadRequest("Message is not modified"))).show("x")
        assert env.tg.to(OWNER) == []
        await botmod.Reply(env.bot, u, Query(BadRequest("Message to edit not found"))).show("y")
        assert [m.text for m in env.tg.to(OWNER)] == ["y"]

    run(go())


# --- 코레일 라이브러리 ----------------------------------------------------
class FakeResponse:
    def __init__(self, obj):
        self.text = json.dumps(obj)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}

    def post(self, url, data=None, headers=None):
        return self.responses.pop(0)

    def get(self, url, params=None, headers=None):
        return self.responses.pop(0)


def test_korail_login_failure_is_visible():
    k = ktx.Korail("010-1234-5678", "pw", auto_login=False)
    k._session = FakeSession(
        [
            FakeResponse({"strResult": "SUCC", "app.login.cphd": {"idx": "1", "key": "k" * 32}}),
            FakeResponse({"strResult": "FAIL", "h_msg_txt": "비밀번호가 일치하지 않습니다."}),
        ]
    )
    assert k.login() is False
    assert k.logined is False and k.login_error == "비밀번호가 일치하지 않습니다."


def test_ticket_info_never_returns_none():
    """None 을 돌려주면 reservations() 의 언패킹이 터져, 잡힌 예약이 실패로 보인다."""
    k = ktx.Korail("x", "y", auto_login=False)
    k._session = FakeSession([FakeResponse({"strResult": "FAIL", "h_msg_cd": "P100"})])
    assert k.ticket_info("P1") == ([], None)
    k._session = FakeSession(
        [FakeResponse({"strResult": "SUCC", "h_wct_no": "W9", "jrny_infos": {"jrny_info": [{}]}})]
    )
    assert k.ticket_info("P1") == ([], "W9")


def test_waiting_list_reservation_does_not_crash_listing():
    k = ktx.Korail("x", "y", auto_login=False)
    train_info = {
        "h_trn_clsf_cd": "100", "h_trn_clsf_nm": "KTX", "h_trn_gp_cd": "100",
        "h_trn_no": "00527", "h_dpt_rs_stn_nm": "서울", "h_dpt_rs_stn_cd": "0001",
        "h_dpt_dt": "20990101", "h_dpt_tm": "200900", "h_arv_rs_stn_nm": "부산",
        "h_arv_rs_stn_cd": "0020", "h_arv_dt": "20990101", "h_arv_tm": "231200",
        "h_run_dt": "20990101", "h_pnr_no": "P1", "h_tot_seat_cnt": "1",
        "h_ntisu_lmt_dt": "00000000", "h_ntisu_lmt_tm": "235959", "h_rsv_amt": "59800",
    }
    k._session = FakeSession(
        [
            FakeResponse({"strResult": "SUCC", "jrny_infos": {"jrny_info": [{"train_infos": {"train_info": [train_info]}}]}}),
            FakeResponse({"strResult": "FAIL", "h_msg_cd": "P100"}),  # 좌석 없음 (예약대기)
        ]
    )
    (r,) = k.reservations()
    assert r.is_waiting and r.tickets == []
