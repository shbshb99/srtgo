"""오너가 폰(텔레그램)만으로 운영PC를 관리하는 화면: 업데이트 승인, 재시작, 기록 보기, 코레일 버전 점검.

워치독과는 ~/.srtgo 의 파일로 주고받는다 (updater 모듈 설명 참고). 봇은 하트비트마다
(_ops_tick) 그 파일들을 보고 새 버전·설치 결과를 오너에게 알린다.

옛 코드로 돌던 워치독은 새 승인 방식을 모른다. 새 봇이 그걸 알아채면(워치독이 넘겨준
버전이 다르면) 새 워치독을 띄워 자리를 넘긴다 (_maybe_take_over_watchdog).
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import korail_version, updater
from .updater import read_json, remove, version_key, write_json

log = logging.getLogger("srtgo.bot")

ANNOUNCED_FILE = "update_announced"
TAKEOVER_FILE = "takeover.attempt"
TAKEOVER_DELAY_SECONDS = 30
CHECK_WAIT_SECONDS = 60
LOG_LINES = {"watchdog.log": 12, "bot.log": 25}
MODE_NAME = {"approve": "승인 후 설치", "auto": "자동 설치", "off": "꺼짐"}
STATUS_TEXT = {
    "installed": "✅ 업데이트 완료: {version}",
    "failed": "⚠️ 업데이트 실패: {version}",
    "rolled_back": "↩️ 업데이트 되돌림: {version}",
}


def _when(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m/%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def watchdog_running(home: Path) -> bool:
    from .watchdog import acquire_lock

    handle = acquire_lock(home / "watchdog.lock")
    if handle is None:
        return True
    handle.close()
    return False


def process_command_line(pid: int) -> str:
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            return out.stdout or ""
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except Exception:
        return ""


def spawn_detached(args) -> bool:
    """이 봇·워치독이 끝나도 살아남는 프로세스를 띄운다."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name != "nt":
        subprocess.Popen(args, start_new_session=True, **kw)
        return True
    detached, new_group, breakaway, no_window = 0x8, 0x200, 0x01000000, 0x08000000
    for flags in (detached | new_group | breakaway, detached | new_group, new_group | no_window):
        try:
            subprocess.Popen(args, creationflags=flags, **kw)
            return True
        except OSError as ex:
            log.info("분리 실행 실패 (flags %#x): %s", flags, ex)
    return False


class OpsMixin:
    """Bot 에 섞어 쓴다. self.owner_id, self._send, self.heartbeat_path 를 쓴다."""

    _app = None
    _ops_tasks = None

    @property
    def ops_home(self) -> Path:
        if self.heartbeat_path is not None:
            return self.heartbeat_path.parent
        from .watchdog import data_dir

        return data_dir()

    def _ops_task(self, coro):
        if self._ops_tasks is None:
            self._ops_tasks = set()
        task = asyncio.create_task(coro)
        self._ops_tasks.add(task)
        task.add_done_callback(self._ops_tasks.discard)
        return task

    # --- 하트비트마다 ----------------------------------------------------
    async def _ops_tick(self):
        from .bot import kb

        home = self.ops_home
        result = read_json(home / updater.RESULT_FILE)
        if result:
            remove(home / updater.RESULT_FILE)
            head = STATUS_TEXT.get(result.get("status"), "업데이트: {version}").format(version=result.get("version", "?"))
            await self._send(self.owner_id, f"{head}\n{result.get('message', '')}".strip(), kb([[("🛠 관리", "ops")]]))

        info = read_json(home / updater.AVAILABLE_FILE)
        if not info or version_key(info.get("version", "")) <= version_key(updater.installed_version()):
            return
        try:
            announced = (home / ANNOUNCED_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            announced = ""
        if announced == info["version"]:
            return
        (home / ANNOUNCED_FILE).write_text(info["version"], encoding="utf-8")
        notes = f"\n\n바뀐 점:\n{info['notes']}" if info.get("notes") else ""
        if updater.mode(home) == "auto":
            await self._send(
                self.owner_id,
                f"🆕 새 버전 {info['version']}을(를) 자동으로 설치합니다 (지금 {updater.installed_version()}).{notes}",
            )
            return
        await self._send(
            self.owner_id,
            f"🆕 새 버전이 있습니다: {info['version']} (지금 {updater.installed_version()}){notes}\n\n"
            "설치하면 봇이 1분쯤 꺼졌다 켜집니다. 대기는 이어집니다.",
            kb([[(f"✅ 지금 설치", f"opsinstall:{info['version']}"), ("나중에", "opslater")]]),
        )

    # --- 옛 워치독 교체 --------------------------------------------------
    async def _maybe_take_over_watchdog(self):
        """나를 띄운 워치독이 옛 코드면 새 워치독으로 바꾼다 (봇이 막 켜졌을 때 한 번)."""
        await asyncio.sleep(TAKEOVER_DELAY_SECONDS)
        mine = updater.installed_version()
        if os.environ.get("SRTGO_WATCHDOG_VERSION") == mine:
            return
        home = self.ops_home
        if not watchdog_running(home):
            return
        try:
            last = (home / TAKEOVER_FILE).read_text(encoding="utf-8").split()
            if last and last[0] == mine and time.time() - float(last[1]) < 3600:
                log.warning("워치독 교체를 이미 시도했는데 여전히 옛 워치독입니다. 다시 시도하지 않습니다.")
                return
        except (OSError, ValueError, IndexError):
            pass
        parent = os.getppid()
        if "srtgo.watchdog" not in process_command_line(parent):
            log.info("부모 프로세스(%s)가 워치독이 아니어서 교체하지 않습니다.", parent)
            return
        (home / TAKEOVER_FILE).write_text(f"{mine} {time.time()}", encoding="utf-8")
        args = [sys.executable, "-m", "srtgo.watchdog", "--takeover", str(parent), str(os.getpid())]
        if not spawn_detached(args):
            log.error("새 워치독을 띄우지 못했습니다. 다음 재부팅 때 바뀝니다.")
            return
        log.warning("옛 워치독(%s)을 새 코드(%s)로 바꿉니다. 봇을 다시 켭니다.", os.environ.get("SRTGO_WATCHDOG_VERSION", "옛 버전"), mine)
        await self._send(self.owner_id, "🔄 관리 프로그램(워치독)을 새 버전으로 바꾸는 중입니다. 1분쯤 뒤 봇이 다시 켜집니다.")
        if self._app is not None:
            self._app.stop_running()

    # --- 관리 화면 -------------------------------------------------------
    def ops_text(self) -> str:
        home = self.ops_home
        lines = ["🛠 관리 (운영PC)", ""]
        lines.append(f"프로그램: {updater.installed_version()}")
        try:
            wd = (home / updater.WATCHDOG_VERSION_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            wd = "옛 버전"
        running = watchdog_running(home)
        lines.append(f"워치독: {wd if running else '꺼져 있음 (업데이트·자동 재시작 안 됨)'}")
        mode = updater.mode(home)
        lines.append(f"업데이트: {MODE_NAME[mode]}")
        checked = read_json(home / "update_checked.json")
        if checked:
            lines.append(f"마지막 확인: {_when(checked.get('at'))} (저장소 최신 {checked.get('latest', '?')})")
        info = read_json(home / updater.AVAILABLE_FILE)
        if info and version_key(info.get("version", "")) > version_key(updater.installed_version()):
            lines.append(f"🆕 설치할 수 있는 새 버전: {info['version']}")
            if info.get("notes"):
                lines.append(info["notes"])
        app, api = korail_version_values()
        conf = korail_version.load_config()
        changed = f" ({conf['updated'].replace('T', ' ')[:16]} 자동 변경)" if conf.get("updated") else ""
        lines.append(f"코레일 앱 버전: {app} / {api}{changed}")
        return "\n".join(lines)

    def ops_markup(self):
        from .bot import HOME, kb

        home = self.ops_home
        rows = []
        info = read_json(home / updater.AVAILABLE_FILE)
        if info and version_key(info.get("version", "")) > version_key(updater.installed_version()):
            rows.append([(f"⬇️ {info['version']} 설치", f"opsinstall:{info['version']}")])
        mode = updater.mode(home)
        nxt = {"approve": "auto", "auto": "off", "off": "approve"}[mode]
        rows.append([("🔄 지금 확인", "opscheck"), (f"업데이트: {MODE_NAME[mode]} → {MODE_NAME[nxt]}", "opsmode")])
        rows.append([("🚅 코레일 버전 점검", "opskorail"), ("📜 기록 보기", "opslog")])
        rows.append([("🔁 봇 재시작", "opsrestart"), HOME])
        return kb(rows)

    async def _owner_only(self, r) -> bool:
        if r.u.is_owner:
            return True
        await self.show_home(r, note="관리자만 쓸 수 있습니다.")
        return False

    async def show_ops(self, r, note=None):
        text = self.ops_text()
        await r.show(f"{note}\n\n{text}" if note else text, self.ops_markup())

    async def cmd_admin(self, update, context):
        u = await self._enter(update, context)
        if u is None:
            return
        from .bot import Reply

        r = Reply(self, u)
        if await self._owner_only(r):
            await self.show_ops(r)

    async def btn_ops(self, r, arg):
        if await self._owner_only(r):
            await self.show_ops(r)

    async def btn_opslater(self, r, arg):
        if await self._owner_only(r):
            await self.show_ops(r, note="알겠습니다. 설치하려면 여기서 ⬇️ 버튼을 누르세요.")

    async def btn_opsinstall(self, r, arg):
        if not await self._owner_only(r):
            return
        home = self.ops_home
        info = read_json(home / updater.AVAILABLE_FILE)
        if not info or info.get("version") != arg:
            await self.show_ops(r, note="그 버전은 더 이상 설치 대상이 아닙니다.")
            return
        if not watchdog_running(home):
            await self.show_ops(r, note="⚠️ 워치독이 꺼져 있어 설치할 수 없습니다.")
            return
        (home / updater.APPROVED_FILE).write_text(arg, encoding="utf-8")
        log.warning("오너가 업데이트 %s 설치를 승인했습니다.", arg)
        await r.show(
            f"✅ {arg} 설치를 승인했습니다.\n받아서 확인한 뒤 봇을 잠깐 끄고 설치합니다. "
            "끝나면 결과를 보내 드립니다 (보통 1~3분)."
        )

    async def btn_opsmode(self, r, arg):
        if not await self._owner_only(r):
            return
        home = self.ops_home
        nxt = {"approve": "auto", "auto": "off", "off": "approve"}[updater.mode(home)]
        updater.set_mode(home, nxt)
        await self.show_ops(r, note=f"업데이트를 '{MODE_NAME[nxt]}'(으)로 바꿨습니다.")

    async def btn_opscheck(self, r, arg):
        if not await self._owner_only(r):
            return
        home = self.ops_home
        if not watchdog_running(home):
            await self.show_ops(r, note="⚠️ 워치독이 꺼져 있어 확인할 수 없습니다.")
            return
        if updater.mode(home) == "off":
            await self.show_ops(r, note="업데이트가 꺼져 있습니다. 먼저 켜 주세요.")
            return
        (home / updater.CHECK_NOW_FILE).write_text(str(time.time()))
        await r.show("🔄 새 버전을 확인하는 중입니다…")
        self._ops_task(self._finish_check(r))

    async def _finish_check(self, r):
        home = self.ops_home
        deadline = time.time() + CHECK_WAIT_SECONDS
        while (home / updater.CHECK_NOW_FILE).exists() and time.time() < deadline:
            await asyncio.sleep(2)
        if (home / updater.CHECK_NOW_FILE).exists():
            await self.show_ops(r, note="⏳ 아직 확인하지 못했습니다 (워치독이 바쁘거나 막 켜진 중). 잠시 뒤 다시 눌러 주세요.")
            return
        info = read_json(home / updater.AVAILABLE_FILE)
        if info and version_key(info.get("version", "")) > version_key(updater.installed_version()):
            await self.show_ops(r, note=f"🆕 새 버전 {info['version']}이(가) 있습니다.")
        else:
            await self.show_ops(r, note="✅ 지금이 최신 버전입니다.")

    async def btn_opslog(self, r, arg):
        if not await self._owner_only(r):
            return
        home = self.ops_home
        await r.show("📜 최근 기록을 보냅니다.")
        for name, n in LOG_LINES.items():
            try:
                lines = (home / name).read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
            except OSError:
                lines = ["(기록 없음)"]
            body = "\n".join(lines)[-3500:]
            await self._send(r.u.chat_id, f"📜 {name}\n{body}")
        await self.show_ops(Reply_new(self, r.u))

    async def btn_opskorail(self, r, arg):
        if not await self._owner_only(r):
            return
        await r.show("🚅 코레일 앱 버전을 점검하는 중입니다… (막혀 있으면 맞는 버전을 찾느라 최대 몇 분 걸립니다)")
        self._ops_task(self._finish_korail_check(r))

    async def _finish_korail_check(self, r):
        app, api = korail_version_values()
        ok = await asyncio.to_thread(korail_version.probe, app, api)
        if ok:
            await self.show_ops(r, note=f"✅ 코레일 앱 버전 {app} 정상입니다.")
            return
        found = await asyncio.to_thread(korail_version.heal, app, api, force=True)
        if found:
            await self.show_ops(r, note=f"🔧 {app}이(가) 막혀 있어 {found[0]}(으)로 바꿨습니다.")
        else:
            await self.show_ops(r, note="⚠️ 통과하는 코레일 앱 버전을 찾지 못했습니다. 프로그램 수정이 필요합니다.")

    async def btn_opsrestart(self, r, arg):
        if not await self._owner_only(r):
            return
        from .bot import kb

        await r.show(
            "🔁 봇을 다시 켤까요? 30초쯤 걸리고, 대기는 이어집니다.",
            kb([[("🔁 다시 켜기", "opsrestartok"), ("취소", "ops")]]),
        )

    async def btn_opsrestartok(self, r, arg):
        if not await self._owner_only(r):
            return
        if not watchdog_running(self.ops_home):
            await self.show_ops(r, note="⚠️ 워치독이 꺼져 있어, 끄면 다시 켜지지 않습니다. 재시작하지 않았습니다.")
            return
        await r.show("🔁 다시 켭니다. 30초쯤 뒤 /start 를 눌러 보세요.")
        log.warning("오너 요청으로 봇을 다시 켭니다.")
        if self._app is not None:
            asyncio.get_running_loop().call_later(1, self._app.stop_running)


def korail_version_values():
    from .ktx import korail_versions

    return korail_versions()


def Reply_new(bot, u):
    from .bot import Reply

    return Reply(bot, u)
