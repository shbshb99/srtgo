"""폰(텔레그램)만으로 운영PC 관리: 업데이트 승인, 결과 알림, 재시작, 기록, 옛 워치독 교체."""

import asyncio

import pytest

from srtgo import ops
from srtgo import updater as up
from srtgo import watchdog as wd

from conftest import FAMILY, OWNER, run

CURRENT = "2.2.2+ktx.12"
NEW = "2.2.2+ktx.13"


class FakeApp:
    def __init__(self):
        self.stopped = 0

    def stop_running(self):
        self.stopped += 1


@pytest.fixture
def admin(env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    env.bot.heartbeat_path = home / "heartbeat"
    env.bot._app = FakeApp()
    env.home = home
    monkeypatch.setattr(up, "installed_version", lambda: CURRENT)
    monkeypatch.setattr(ops, "watchdog_running", lambda home: True)
    return env


def offer(home, version=NEW, notes="역 목록 추가"):
    up.write_json(home / up.AVAILABLE_FILE, {"version": version, "wheel": f"srtgo-{version}-py3-none-any.whl", "sha256": "0" * 64, "base": "x", "notes": notes})


def owner_texts(env):
    return env.tg.texts(OWNER)


def test_new_version_is_offered_once_and_approved_from_phone(admin):
    offer(admin.home)

    async def go():
        await admin.bot._ops_tick()
        await admin.bot._ops_tick()
        msgs = [m for m in admin.tg.to(OWNER) if NEW in m.text]
        assert len(msgs) == 1, "같은 새 버전을 여러 번 알림"
        assert "역 목록 추가" in msgs[0].text
        await admin.owner.press(f"opsinstall:{NEW}", on=msgs[0])

    run(go())
    assert (admin.home / up.APPROVED_FILE).read_text() == NEW
    assert "승인했습니다" in admin.owner.screen.text
    assert not admin.tg.to(FAMILY), "가족에게 관리 알림이 감"


def test_install_result_is_reported_and_cleared(admin):
    up.write_json(admin.home / up.RESULT_FILE, {"version": NEW, "status": "rolled_back", "message": "되돌렸습니다"})
    run(admin.bot._ops_tick())
    assert "되돌림" in owner_texts(admin) and "되돌렸습니다" in owner_texts(admin)
    assert not (admin.home / up.RESULT_FILE).exists()


def test_family_cannot_use_admin(admin):
    offer(admin.home)

    async def go():
        await admin.family.command("admin")
        assert "관리자만" in admin.family.screen.text
        await admin.family.tap(f"opsinstall:{NEW}")
        await admin.family.tap("opsrestartok")

    run(go())
    assert not (admin.home / up.APPROVED_FILE).exists()
    assert admin.bot._app.stopped == 0


def test_admin_screen_and_mode_toggle(admin):
    offer(admin.home)

    async def go():
        await admin.owner.command("admin")
        text = admin.owner.screen.text
        assert CURRENT in text and NEW in text and "승인 후 설치" in text and "코레일 앱 버전" in text
        await admin.owner.press("opsmode")
        assert up.mode(admin.home) == "auto"
        await admin.owner.press("opsmode")
        assert up.mode(admin.home) == "off"
        await admin.owner.press("opsmode")
        assert up.mode(admin.home) == "approve"

    run(go())


def test_check_now_asks_watchdog_and_reports(admin, monkeypatch):
    monkeypatch.setattr(ops, "CHECK_WAIT_SECONDS", 5)

    async def go():
        await admin.owner.command("admin")
        await admin.owner.press("opscheck")
        assert (admin.home / up.CHECK_NOW_FILE).exists()
        # 워치독이 확인을 끝냈다
        offer(admin.home)
        (admin.home / up.CHECK_NOW_FILE).unlink()
        await asyncio.gather(*admin.bot._ops_tasks)
        assert f"새 버전 {NEW}" in admin.owner.screen.text

    run(go())


def test_restart_from_phone(admin):
    async def go():
        await admin.owner.command("admin")
        await admin.owner.press("opsrestart")
        await admin.owner.press("opsrestartok")
        await asyncio.sleep(1.2)

    run(go())
    assert admin.bot._app.stopped == 1


def test_logs_from_phone(admin):
    (admin.home / "bot.log").write_text("\n".join(f"line {i}" for i in range(100)), encoding="utf-8")

    async def go():
        await admin.owner.command("admin")
        await admin.owner.press("opslog")

    run(go())
    texts = owner_texts(admin)
    assert "line 99" in texts and "line 10\n" not in texts and "watchdog.log" in texts


def test_korail_check_from_phone(admin, monkeypatch):
    from srtgo import korail_version as kv

    monkeypatch.setattr(kv, "probe", lambda a, v: False)
    monkeypatch.setattr(kv, "heal", lambda a, v, force=False: ("7.1.0", v) if force else None)

    async def go():
        await admin.owner.command("admin")
        await admin.owner.press("opskorail")
        await asyncio.gather(*admin.bot._ops_tasks)

    run(go())
    assert "7.1.0" in admin.owner.screen.text


# --- 옛 워치독 교체 --------------------------------------------------------
@pytest.fixture
def takeover(admin, monkeypatch):
    spawned = []
    monkeypatch.setattr(ops, "TAKEOVER_DELAY_SECONDS", 0)
    monkeypatch.setattr(ops, "process_command_line", lambda pid: "pythonw.exe -m srtgo.watchdog")
    monkeypatch.setattr(ops, "spawn_detached", lambda args: spawned.append(args) or True)
    admin.spawned = spawned
    return admin


def test_old_watchdog_is_replaced(takeover, monkeypatch):
    monkeypatch.delenv("SRTGO_WATCHDOG_VERSION", raising=False)  # ktx.11 워치독은 버전을 안 넘긴다
    run(takeover.bot._maybe_take_over_watchdog())
    [args] = takeover.spawned
    assert args[-3:-1] == ["--takeover", str(__import__("os").getppid())]
    assert takeover.bot._app.stopped == 1
    assert "워치독" in owner_texts(takeover)
    # 바로 또 시도하지 않는다 (교체가 안 먹혀도 무한 반복 금지)
    run(takeover.bot._maybe_take_over_watchdog())
    assert len(takeover.spawned) == 1


def test_current_watchdog_is_left_alone(takeover, monkeypatch):
    monkeypatch.setenv("SRTGO_WATCHDOG_VERSION", CURRENT)
    run(takeover.bot._maybe_take_over_watchdog())
    assert takeover.spawned == [] and takeover.bot._app.stopped == 0


def test_not_replaced_when_parent_is_not_watchdog(takeover, monkeypatch):
    monkeypatch.delenv("SRTGO_WATCHDOG_VERSION", raising=False)
    monkeypatch.setattr(ops, "process_command_line", lambda pid: "python -m srtgo")
    run(takeover.bot._maybe_take_over_watchdog())
    assert takeover.spawned == []


def test_watchdog_take_over_kills_old_and_waits_for_bot(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(wd, "kill_process", killed.append)
    alive = iter([True, True, False])
    monkeypatch.setattr(wd, "pid_alive", lambda pid: next(alive))
    monkeypatch.setattr(wd.time, "sleep", lambda s: None)
    lock = wd.take_over(__import__("logging").getLogger("t"), tmp_path, 4242, 4343)
    assert lock is not None and killed == [4242]
    lock.close()


def test_owner_types_app_version_from_phone(admin, monkeypatch):
    from srtgo import korail_version as kv

    monkeypatch.setenv("SRTGO_HOME", str(admin.home))
    tried = []
    monkeypatch.setattr(kv, "probe", lambda a, v: tried.append(a) or True)

    async def go():
        await admin.owner.command("admin")
        await admin.owner.press("opsappver")
        await admin.owner.say("7.0.10")

    run(go())
    assert tried == ["7.0.10"] and kv.load_config()["app_version"] == "7.0.10"
    assert "조회가 됩니다" in admin.owner.screen.text


def test_bad_app_version_text_is_rejected(admin, monkeypatch):
    from srtgo import korail_version as kv

    monkeypatch.setenv("SRTGO_HOME", str(admin.home))

    async def go():
        await admin.owner.command("admin")
        await admin.owner.press("opsappver")
        await admin.owner.say("최신")

    run(go())
    assert "버전 모양이 아닙니다" in admin.owner.screen.text and not kv.load_config()
