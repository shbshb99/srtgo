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
    asset = {
        "name": f"srtgo-{version}-py3-none-any.whl",
        "browser_download_url": f"https://example/{version}.whl",
    }
    if digest == "good":
        asset["digest"] = "sha256:" + hashlib.sha256(data).hexdigest()
    elif digest:
        asset["digest"] = digest
    return {"tag_name": f"v{version}", "assets": [asset]}


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
        self.u = up.Updater(
            self.home, LOG, get=self.get, pip=self.pip, current=lambda: self.version, pkg_dir=lambda: self.pkg
        )
        self.u.next_check = 0

    def get(self, url, accept=None):
        if url == up.LATEST_URL:
            return json.dumps(self.release).encode()
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


@pytest.mark.parametrize("digest", ["sha256:" + "0" * 64, None])
def test_unverifiable_download_is_not_installed(tmp_path, digest):
    e = Env(tmp_path, rel=release("2.2.2+ktx.11", digest=digest))
    assert e.u.find_update() is None
    assert e.installed == []


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
    monkeypatch.setattr(wd, "STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(wd, "POLL_SECONDS", 0.1)
    order = []
    real_install = e.u.install
    e.u.install = lambda *a: order.append((e.home / "stopped_cleanly").exists()) or real_install(*a)
    code, ran, reason = wd.run_once(LOG, heartbeat, e.u)
    assert reason == wd.UPDATE_REASON and code == 0
    assert order == [True], "봇이 꺼지기 전에 설치함"
    assert e.installed and not (e.home / wd.RESTART_REQUEST_FILE).exists()


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
