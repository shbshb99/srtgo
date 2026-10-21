"""워치독 자동 업데이트: 새 릴리스를 확인·검증·설치하고, 새 버전이 바로 죽으면 되돌린다."""

import asyncio
import hashlib
import json
import logging
import sys
import textwrap
import time

import pytest

from srtgo import bot as botmod
from srtgo import updater as up
from srtgo import watchdog as wd

WHEEL = b"PK fake wheel"
LOG = logging.getLogger("test-updater")


def release(version, data=WHEEL, digest="good"):
    m = {"version": version, "wheel": f"srtgo-{version}-py3-none-any.whl"}
    if digest == "good":
        m["sha256"] = hashlib.sha256(data).hexdigest()
    elif digest:
        m["sha256"] = digest
    return m


class Env:
    def __init__(self, tmp_path, current="2.2.2+ktx.10", rel=None, pip_code=0):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.pkg = tmp_path / "site" / "srtgo"
        self.pkg.mkdir(parents=True)
        (self.pkg / "bot.py").write_text("OLD")
        self.version = current
        self.release = rel
        self.pip_code = pip_code
        self.downloads = []
        self.installed = []
        self.api_release = None
        self.u = up.Updater(
            self.home, LOG, get=self.get, pip=self.pip, current=lambda: self.version, pkg_dir=lambda: self.pkg
        )
        self.u.next_check = 0

    def get(self, url):
        if url.startswith("https://api.github.com/"):
            if self.api_release is None:
                raise OSError("rate limited")
            return json.dumps(self.api_release).encode()
        if url.endswith(up.MANIFEST):
            if url.startswith(up.UPDATE_BASES[0]):
                raise OSError("404")  # main 에는 아직 없다 -> 다음 주소로
            return json.dumps(self.release).encode()
        assert url.startswith(up.UPDATE_BASES[1])
        self.downloads.append(url)
        return WHEEL

    def pip(self, wheel):
        self.installed.append(wheel.name)
        if self.pip_code == 0:
            (self.pkg / "bot.py").write_text("NEW")
            self.version = up.WHEEL_RE.match(wheel.name).group(1)
        return self.pip_code, "pip output"


def test_version_order():
    assert up.version_key("2.2.2+ktx.10") > up.version_key("2.2.2+ktx.9")
    assert up.version_key("2.3.0") > up.version_key("2.2.2+ktx.99")


def test_new_release_is_verified_installed_and_confirmed(tmp_path):
    e = Env(tmp_path, rel=release("2.2.2+ktx.11"))
    found = e.u.find_update()
    assert found and found[0] == "2.2.2+ktx.11" and found[1].read_bytes() == WHEEL
    assert e.u.install(*found) is True
    assert e.installed == ["srtgo-2.2.2+ktx.11-py3-none-any.whl"]
    assert e.u.state()["pending"]["previous"] == "2.2.2+ktx.10"
    e.u.after_run(ran_seconds=999, code=1, healthy_after=120)  # 오래 돌다 죽은 건 업데이트 탓 아님
    assert "pending" not in e.u.state()
    assert (e.pkg / "bot.py").read_text() == "NEW"


@pytest.mark.parametrize("digest", ["0" * 64, None])
def test_unverifiable_download_is_not_installed(tmp_path, digest):
    e = Env(tmp_path, rel=release("2.2.2+ktx.11", digest=digest))
    assert e.u.find_update() is None
    assert e.installed == []


def test_manifest_pointing_elsewhere_is_rejected(tmp_path):
    rel = release("2.2.2+ktx.11")
    rel["wheel"] = "../evil/srtgo-2.2.2+ktx.11-py3-none-any.whl"
    e = Env(tmp_path, rel=rel)
    assert e.u.find_update() is None and e.downloads == []


def test_same_or_older_release_is_ignored_without_downloading(tmp_path):
    e = Env(tmp_path, rel=release("2.2.2+ktx.10"))
    assert e.u.find_update() is None
    assert e.downloads == []


def test_broken_new_version_is_rolled_back_and_skipped(tmp_path):
    e = Env(tmp_path, rel=release("2.2.2+ktx.11"))
    e.u.install(*e.u.find_update())
    e.u.after_run(ran_seconds=3, code=1, healthy_after=120)
    assert (e.pkg / "bot.py").read_text() == "OLD", "되돌리지 않음"
    assert "2.2.2+ktx.11" in e.u.state()["skip"]
    e.version = "2.2.2+ktx.10"
    e.u.next_check = 0
    assert e.u.find_update() is None, "망가진 버전을 다시 설치하려 함"


def test_pip_failure_keeps_current_version(tmp_path):
    e = Env(tmp_path, rel=release("2.2.2+ktx.11"), pip_code=1)
    assert e.u.install(*e.u.find_update()) is False
    assert (e.pkg / "bot.py").read_text() == "OLD"
    assert "pending" not in e.u.state()


def test_offline_check_waits_for_next_round(tmp_path):
    e = Env(tmp_path)
    e.u.get = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
    assert e.u.find_update() is None
    assert not e.u.due()  # 6시간 뒤에 다시


def test_off_switch(tmp_path):
    e = Env(tmp_path, rel=release("2.2.2+ktx.11"))
    (e.home / up.OFF_FILE).write_text("")
    assert not e.u.due()


def test_watchdog_stops_bot_gracefully_then_installs(tmp_path, monkeypatch):
    """실제 자식 프로세스로: 워치독이 restart.request 를 쓰면 봇이 스스로 꺼지고 그다음 설치한다."""
    e = Env(tmp_path, rel=release("2.2.2+ktx.11"))
    heartbeat = e.home / "heartbeat"
    fake_bot = tmp_path / "fake_bot.py"
    fake_bot.write_text(textwrap.dedent(f"""
        import pathlib, time
        hb = pathlib.Path({str(heartbeat)!r})
        req = hb.with_name({wd.RESTART_REQUEST_FILE!r})
        while not req.exists():
            hb.write_text(str(time.time()))
            time.sleep(0.05)
        hb.with_name("stopped_cleanly").write_text("yes")
    """))
    monkeypatch.setattr(wd, "BOT_COMMAND", [sys.executable, str(fake_bot)])
    monkeypatch.setattr(wd, "STARTUP_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(wd, "POLL_SECONDS", 0.1)
    (e.home / up.APPROVED_FILE).write_text("2.2.2+ktx.11")  # 오너가 폰에서 승인
    order = []
    real_install = e.u.install
    e.u.install = lambda *a: order.append((e.home / "stopped_cleanly").exists()) or real_install(*a)
    code, ran, reason = wd.run_once(LOG, heartbeat, e.u)
    assert reason == wd.UPDATE_REASON and code == 0
    assert order == [True], "봇이 꺼지기 전에 설치함"
    assert e.installed and not (e.home / wd.RESTART_REQUEST_FILE).exists()
    assert not (e.home / up.APPROVED_FILE).exists() and not (e.home / up.AVAILABLE_FILE).exists()


def test_bot_stops_itself_on_restart_request(tmp_path):
    b = botmod.Bot("1")
    b.heartbeat_path = tmp_path / "heartbeat"
    (tmp_path / wd.RESTART_REQUEST_FILE).write_text("x")
    stopped = []

    class App:
        updater = None

        def stop_running(self):
            stopped.append(True)

    asyncio.run(asyncio.wait_for(b._heartbeat(App()), 2))
    assert stopped == [True] and not (tmp_path / wd.RESTART_REQUEST_FILE).exists()


# --- 승인 흐름 ------------------------------------------------------------
def test_new_version_waits_for_owner_approval(tmp_path):
    e = Env(tmp_path, rel=release("2.2.2+ktx.11"))
    info = e.u.check()
    assert up.read_json(e.home / up.AVAILABLE_FILE)["version"] == "2.2.2+ktx.11"
    assert not e.u.approved(info), "승인 없이 설치하려 함"
    (e.home / up.APPROVED_FILE).write_text("2.2.2+ktx.10")  # 다른 버전 승인은 무효
    assert not e.u.approved(info)
    (e.home / up.APPROVED_FILE).write_text("2.2.2+ktx.11")
    assert e.u.approved(info)


def test_auto_and_off_modes(tmp_path):
    e = Env(tmp_path, rel=release("2.2.2+ktx.11"))
    info = e.u.check()
    up.set_mode(e.home, "auto")
    assert up.mode(e.home) == "auto" and e.u.approved(info)
    up.set_mode(e.home, "off")
    assert up.mode(e.home) == "off" and not e.u.approved(info) and not e.u.due()
    up.set_mode(e.home, "approve")
    assert up.mode(e.home) == "approve"


def test_check_now_request_is_honoured_and_cleared(tmp_path):
    e = Env(tmp_path, rel=release("2.2.2+ktx.10"))
    e.u.next_check = time.time() + 9999
    assert not e.u.due()
    (e.home / up.CHECK_NOW_FILE).write_text("")
    assert e.u.due()
    assert e.u.check() is None
    assert not (e.home / up.CHECK_NOW_FILE).exists()
    assert up.read_json(e.home / "update_checked.json")["latest"] == "2.2.2+ktx.10"


def test_results_are_recorded_for_the_owner(tmp_path):
    e = Env(tmp_path, rel=release("2.2.2+ktx.11"))
    e.u.install(*e.u.find_update())
    e.u.confirm()
    assert up.read_json(e.home / up.RESULT_FILE)["status"] == "installed"
    (tmp_path / "b").mkdir()
    e2 = Env(tmp_path / "b", rel=release("2.2.2+ktx.11"))
    e2.u.install(*e2.u.find_update())
    e2.u.after_run(ran_seconds=1, code=1, healthy_after=120)
    assert up.read_json(e2.home / up.RESULT_FILE)["status"] == "rolled_back"


def test_manifest_prefers_github_api_over_stale_raw_cache(tmp_path):
    e = Env(tmp_path, rel=release("2.2.2+ktx.10"))  # raw 는 아직 옛 내용
    e.api_release = release("2.2.2+ktx.11")  # API 는 방금 올린 내용
    info = e.u.check()
    assert info and info["version"] == "2.2.2+ktx.11"
    assert info["base"] == up.UPDATE_BASES[0]
