"""srtgo 텔레그램 봇을 항상 켜 둔다.

봇을 자식 프로세스로 띄워 두고
- 죽으면 다시 띄운다. 띄우자마자 또 죽으면 간격을 늘려 재시작 폭주를 막는다.
- 살아는 있는데 멈췄으면(하트비트가 끊기면) 죽이고 다시 띄운다.

봇 코드를 import 하지 않는다. 업데이트로 bot.py 가 깨져도 워치독은 살아남아
계속 재시도하고 기록을 남겨야 하기 때문이다. 표준 라이브러리만 쓴다.

실행: srtgo-watchdog  (또는 python -m srtgo.watchdog, 창 없이: pythonw -m srtgo.watchdog)
기록: ~/.srtgo/watchdog.log, 봇 자체 기록은 bot.log, 봇이 뜨자마자 죽을 때의
      오류(업데이트로 코드가 깨진 경우 등)는 bot.console.log (SRTGO_HOME 으로 위치 변경 가능)
"""

import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path

from .updater import APPROVED_FILE, WATCHDOG_VERSION_FILE, Updater, installed_version, remove

# 이 워치독 코드가 들어 있던 버전. 업데이트로 디스크의 코드가 바뀌어도 이 값은 그대로라,
# 봇은 이걸 보고 워치독이 옛 코드로 돌고 있는지 안다.
WATCHDOG_VERSION = installed_version()

# 봇은 20초마다 하트비트를 쓴다. 이만큼 끊기면 멈춘 것으로 본다.
HEARTBEAT_STALE_SECONDS = 180
# 막 뜬 봇은 텔레그램 연결·대기 복원에 시간이 걸린다. 그동안은 하트비트를 안 본다.
STARTUP_GRACE_SECONDS = 120
# 이만큼 멀쩡히 돌았으면 재시작 간격을 처음으로 되돌린다.
HEALTHY_RUN_SECONDS = 300
MIN_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300
POLL_SECONDS = 5
# bot.py 와 맞춘 종료 코드: 다른 봇이 이미 돌고 있음.
EXIT_ALREADY_RUNNING = 3
# 이 파일이 생기면 봇이 스스로 깔끔하게 꺼진다 (업데이트 설치 전에 쓴다).
RESTART_REQUEST_FILE = "restart.request"
BOT_COMMAND = [sys.executable, "-m", "srtgo.bot"]
CONSOLE_LOG_MAX_BYTES = 2_000_000


def data_dir() -> Path:
    path = Path(os.environ.get("SRTGO_HOME") or Path.home() / ".srtgo")
    path.mkdir(parents=True, exist_ok=True)
    return path


def acquire_lock(path: Path):
    """프로세스가 살아 있는 동안만 잡히는 배타 잠금. 이미 잡혀 있으면 None.

    프로세스가 죽으면 OS가 잠금을 풀어 주므로, 강제 종료돼도 남지 않는다.
    """
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def heartbeat_age(path: Path):
    try:
        return time.time() - float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def setup_logging(home: Path) -> logging.Logger:
    log = logging.getLogger("srtgo.watchdog")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        home / "watchdog.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        log.addHandler(stream)
    return log


def open_console_log(home: Path):
    """봇의 표준출력·오류를 받을 파일. 너무 커지면 하나 밀어 두고 새로 쓴다."""
    path = home / "bot.console.log"
    try:
        if path.stat().st_size > CONSOLE_LOG_MAX_BYTES:
            os.replace(path, home / "bot.console.log.1")
    except OSError:
        pass
    return open(path, "ab")


UPDATE_REASON = "업데이트 설치"
# 업데이트하려고 끄라고 한 뒤 이만큼 기다려도 안 꺼지면 강제로 끈다.
GRACEFUL_STOP_SECONDS = 120


def stop_for_update(log, proc, heartbeat: Path) -> None:
    """봇에게 스스로 꺼지라고 하고(대기를 저장하고 끝내도록) 기다린다."""
    request = heartbeat.with_name(RESTART_REQUEST_FILE)
    request.write_text(str(time.time()))
    try:
        proc.wait(timeout=GRACEFUL_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        log.warning("봇이 %d초 안에 꺼지지 않아 강제 종료합니다.", GRACEFUL_STOP_SECONDS)
        proc.kill()
        proc.wait()
    try:
        request.unlink()
    except FileNotFoundError:
        pass


def run_once(log, heartbeat: Path, updater=None):
    """봇을 한 번 띄우고 끝날 때까지 지켜본다. (종료 코드, 실행 시간, 강제종료 사유)."""
    try:
        heartbeat.unlink()
    except FileNotFoundError:
        pass

    started = time.time()
    # 창 없이(pythonw) 돌면 봇이 import 단계에서 죽을 때의 오류가 어디에도 안 남는다.
    console = open_console_log(heartbeat.parent)
    # 봇이 "나를 띄운 워치독이 옛 코드인지" 알 수 있게 버전을 넘겨준다 (takeover 참고).
    env = dict(os.environ, PYTHONIOENCODING="utf-8", SRTGO_WATCHDOG_VERSION=WATCHDOG_VERSION)
    try:
        proc = subprocess.Popen(BOT_COMMAND, stdout=console, stderr=subprocess.STDOUT, env=env)
    finally:
        console.close()  # 자식이 물려받았으니 여기선 닫아도 된다
    log.info("봇 시작 (pid %s)", proc.pid)

    reason = None
    confirmed = False
    try:
        while proc.poll() is None:
            time.sleep(POLL_SECONDS)
            if time.time() - started < STARTUP_GRACE_SECONDS:
                continue
            age = heartbeat_age(heartbeat)
            if updater is not None and age is not None and age <= HEARTBEAT_STALE_SECONDS:
                if not confirmed:
                    updater.confirm()  # 막 설치한 버전이 잘 떴다
                    confirmed = True
                if updater.due():
                    updater.check()
                info = updater.available()
                if updater.approved(info):
                    wheel = updater.download(info)
                    if wheel is None:
                        remove(heartbeat.with_name(APPROVED_FILE))  # 다시 승인하면 다시 받는다
                    else:
                        log.warning("새 버전 %s 설치를 위해 봇을 잠시 끕니다.", info["version"])
                        stop_for_update(log, proc, heartbeat)
                        updater.install(info["version"], wheel)
                        return proc.wait(), time.time() - started, UPDATE_REASON
            if age is None or age > HEARTBEAT_STALE_SECONDS:
                reason = "하트비트 없음" if age is None else f"하트비트 {int(age)}초째 끊김"
                log.warning("봇이 응답하지 않아 강제 종료합니다: %s", reason)
                proc.kill()
                break
        return proc.wait(), time.time() - started, reason
    except BaseException:
        # 워치독이 멈출 때(Ctrl-C 등) 봇을 고아로 남기지 않는다.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        raise


def updater_mode_name(home: Path) -> str:
    from .updater import mode

    return {"off": "꺼짐", "auto": "자동 설치", "approve": "승인 후 설치"}[mode(home)]


def kill_process(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        import signal

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def take_over(log, home: Path, old_pid: int, bot_pid=None):
    """옛 코드로 돌던 워치독을 끝내고 그 자리를 잇는다. 잠금을 잡으면 그 잠금을 돌려준다.

    봇은 그대로 둔다: 봇이 스스로 꺼지면(대기를 저장하고) 새 워치독이 새 봇을 띄운다.
    """
    log.warning("옛 워치독(pid %s)을 새 코드로 바꿉니다.", old_pid)
    kill_process(old_pid)  # 먼저 끝내야 봇이 꺼졌을 때 옛 워치독이 옛 방식으로 다시 띄우지 않는다
    deadline = time.time() + 60
    lock = None
    while time.time() < deadline:
        lock = acquire_lock(home / "watchdog.lock")
        if lock is not None:
            break
        time.sleep(1)
    # 봇이 대기를 저장하고 스스로 꺼질 때까지 기다린다 (아니면 새 봇이 '이미 실행 중'으로 막힌다).
    while bot_pid and pid_alive(bot_pid) and time.time() < deadline + 60:
        time.sleep(1)
    return lock


def main():
    home = data_dir()
    log = setup_logging(home)

    args = sys.argv[1:]
    if len(args) in (2, 3) and args[0] == "--takeover" and all(x.isdigit() for x in args[1:]):
        lock = take_over(log, home, int(args[1]), int(args[2]) if len(args) == 3 else None)
    else:
        lock = acquire_lock(home / "watchdog.lock")
    if lock is None:
        # 작업 스케줄러가 다시 띄운 경우 등. 실패 코드로 끝내면 스케줄러가 계속 재시도하므로 0 으로 끝낸다.
        log.info("워치독이 이미 실행 중입니다. 종료합니다.")
        sys.exit(0)
    try:
        (home / WATCHDOG_VERSION_FILE).write_text(WATCHDOG_VERSION)
    except OSError:
        pass

    heartbeat = home / "heartbeat"
    backoff = MIN_BACKOFF_SECONDS
    updater = Updater(home, log)
    log.info("워치독 %s 시작 (기록: %s, 업데이트 %s)", WATCHDOG_VERSION, home, updater_mode_name(home))
    try:
        while True:
            code, ran, reason = run_once(log, heartbeat, updater)
            if reason == UPDATE_REASON:
                backoff = MIN_BACKOFF_SECONDS
                continue
            updater.after_run(ran, code, STARTUP_GRACE_SECONDS)
            if code == EXIT_ALREADY_RUNNING:
                log.warning(
                    "이 PC에서 다른 봇이 이미 실행 중입니다 (srtgo 메뉴에서 켠 봇 등). "
                    "60초 뒤 다시 확인합니다."
                )
                time.sleep(60)
                continue
            if code != 0 and ran < 30:
                log.warning(
                    "봇이 곧바로 죽었습니다. 원인은 %s 또는 %s 에 있습니다.",
                    home / "bot.log",
                    home / "bot.console.log",
                )
            # 한동안 멀쩡히 돌다 죽었으면 바로 다시 띄우고, 뜨자마자 죽기를
            # 반복하면 그때마다 간격을 두 배로 늘린다.
            if ran >= HEALTHY_RUN_SECONDS:
                backoff = MIN_BACKOFF_SECONDS
            log.warning(
                "봇 종료 (코드 %s, %d초 실행%s). %d초 뒤 다시 시작합니다.",
                code,
                ran,
                f", {reason}" if reason else "",
                backoff,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    except KeyboardInterrupt:
        log.info("워치독을 종료합니다.")


if __name__ == "__main__":
    main()
