"""자동 업데이트: 저장소에 새 버전이 올라오면 오너에게 텔레그램으로 묻고, 승인하면 설치한다.

워치독(설치 담당)과 봇(오너와 대화 담당)이 ~/.srtgo 의 파일로 주고받는다.
- 워치독: 30분마다(또는 오너가 '지금 확인'을 누르면) 저장소의 updates/latest.json
  ({"version", "wheel", "sha256", "notes"}) 을 보고, 새 버전이 있으면 update_available.json 에 쓴다.
- 봇: update_available.json 을 보면 오너에게 [설치] 버튼을 보낸다. 누르면 update_approved 에 그 버전을 쓴다.
- 워치독: 승인된 버전을 받아 SHA256 을 확인하고, 봇을 깔끔하게 끄게 한 뒤(restart.request) 설치한다.
  설치 전에 지금 코드를 통째로 복사해 둔다.
- 새 버전 봇이 2분 안에 죽으면 복사해 둔 코드로 되돌리고 그 버전은 건너뛴다.
- 결과(설치됨·실패·되돌림)는 update_result.json 에 쓰고, 봇이 오너에게 알린다.
- 모드: autoupdate.off 가 있으면 끔, autoupdate.auto 가 있으면 묻지 않고 설치, 없으면 승인 후 설치.

워치독과 같이 표준 라이브러리만 쓴다.
"""

import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from importlib import metadata
from pathlib import Path

REPO = "shbshb99/srtgo"
# 앞에서부터 차례로 본다 (기본 브랜치에 합쳐지면 main 이, 그 전에는 작업 브랜치가 쓰인다).
UPDATE_BASES = [
    f"https://raw.githubusercontent.com/{REPO}/main/updates/",
    f"https://raw.githubusercontent.com/{REPO}/claude/srtgo-ktx-compatibility-7gczbc/updates/",
]
MANIFEST = "latest.json"
# latest.json 은 먼저 GitHub API 로 읽는다. raw.githubusercontent.com 은 몇 분씩 옛 내용을
# 보여 줘서, 방금 올린 새 버전을 '최신입니다'로 놓친다.
MANIFEST_API = {
    base: f"https://api.github.com/repos/{REPO}/contents/updates/{MANIFEST}?ref={branch}"
    for base, branch in zip(UPDATE_BASES, ["main", "claude/srtgo-ktx-compatibility-7gczbc"])
}
CHECK_EVERY_SECONDS = 30 * 60
FIRST_CHECK_AFTER_SECONDS = 60
HTTP_TIMEOUT = 30
PIP_TIMEOUT = 600
OFF_FILE = "autoupdate.off"
AUTO_FILE = "autoupdate.auto"
STATE_FILE = "update_state.json"
AVAILABLE_FILE = "update_available.json"
APPROVED_FILE = "update_approved"
CHECK_NOW_FILE = "update_check_now"
RESULT_FILE = "update_result.json"
WATCHDOG_VERSION_FILE = "watchdog.version"
WHEEL_RE = re.compile(r"^srtgo-([^-]+)-py3-none-any\.whl$")


def version_key(v: str):
    """'2.2.2+ktx.10' -> (2, 2, 2, 10). 숫자만 순서대로 비교한다."""
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def installed_version() -> str:
    try:
        return metadata.version("srtgo")
    except metadata.PackageNotFoundError:
        return "0"


def read_json(path: Path):
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def mode(home: Path) -> str:
    """'off' / 'auto' / 'approve'."""
    if (home / OFF_FILE).exists():
        return "off"
    if (home / AUTO_FILE).exists():
        return "auto"
    return "approve"


def set_mode(home: Path, value: str) -> None:
    remove(home / OFF_FILE)
    remove(home / AUTO_FILE)
    if value == "off":
        (home / OFF_FILE).write_text("")
    elif value == "auto":
        (home / AUTO_FILE).write_text("")


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def http_get(url: str) -> bytes:
    headers = {"User-Agent": "srtgo-updater", "Cache-Control": "no-cache"}
    if url.startswith("https://api.github.com/"):
        headers["Accept"] = "application/vnd.github.raw+json"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read()


def run_pip(wheel: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", str(wheel)],
        capture_output=True,
        text=True,
        timeout=PIP_TIMEOUT,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class Updater:
    def __init__(self, home: Path, log, *, get=http_get, pip=run_pip, current=installed_version, pkg_dir=package_dir):
        self.home = home
        self.log = log
        self.get = get
        self.pip = pip
        self.current = current
        self.pkg_dir = pkg_dir
        self.next_check = time.time() + FIRST_CHECK_AFTER_SECONDS
        self.state_path = home / STATE_FILE
        self.dir = home / "updates"

    # --- 상태 ------------------------------------------------------------
    def state(self) -> dict:
        try:
            s = json.loads(self.state_path.read_text(encoding="utf-8"))
            return s if isinstance(s, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, s: dict) -> None:
        self.state_path.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

    def enabled(self) -> bool:
        return mode(self.home) != "off"

    def due(self) -> bool:
        if not self.enabled():
            return False
        return time.time() >= self.next_check or (self.home / CHECK_NOW_FILE).exists()

    # --- 확인 ------------------------------------------------------------
    def _manifest(self):
        """읽을 수 있는 첫 latest.json 과 그 폴더 주소. 하나도 못 읽으면 (None, None)."""
        errors = []
        for base in UPDATE_BASES:
            for url in (MANIFEST_API[base], base + MANIFEST):
                try:
                    m = json.loads(self.get(url))
                except Exception as ex:
                    errors.append(f"{url}: {ex}")
                    continue
                if isinstance(m, dict):
                    return m, base
        self.log.info("업데이트 확인 실패 (다음에 다시): %s", "; ".join(errors))
        return None, None

    def check(self):
        """저장소를 보고, 설치할 새 버전이 있으면 그 정보를 update_available.json 에 쓰고 돌려준다."""
        self.next_check = time.time() + CHECK_EVERY_SECONDS
        manifest, base = self._manifest()
        remove(self.home / CHECK_NOW_FILE)  # 오너가 '지금 확인'을 눌렀으면 확인은 끝났다
        available = self.home / AVAILABLE_FILE
        if not manifest:
            return self.available()
        current = self.current()
        version = str(manifest.get("version") or "")
        name = str(manifest.get("wheel") or "")
        sha = str(manifest.get("sha256") or "").lower()
        m = WHEEL_RE.match(name)
        if not m or m.group(1) != version or "/" in name or "\\" in name:
            self.log.warning("latest.json 형식이 이상해 업데이트하지 않습니다: %s", manifest)
            return None
        if version in set(self.state().get("skip", [])) or version_key(version) <= version_key(current):
            remove(available)
            write_json(self.home / "update_checked.json", {"at": time.time(), "current": current, "latest": version})
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            self.log.warning("업데이트 %s 에 SHA256 정보가 없어 설치하지 않습니다.", version)
            return None
        info = {
            "version": version,
            "wheel": name,
            "sha256": sha,
            "base": base,
            "notes": str(manifest.get("notes") or "")[:1500],
            "current": current,
        }
        if read_json(available) != info:
            write_json(available, info)
            self.log.info("새 버전 %s 이 있습니다 (지금 %s).", version, current)
        write_json(self.home / "update_checked.json", {"at": time.time(), "current": current, "latest": version})
        return info

    def available(self):
        info = read_json(self.home / AVAILABLE_FILE)
        if info and version_key(info.get("version", "")) > version_key(self.current()):
            return info
        return None

    def approved(self, info) -> bool:
        """설치해도 되나: 자동 모드이거나, 오너가 바로 이 버전을 승인했다."""
        if not info or not self.enabled():
            return False
        if mode(self.home) == "auto":
            return True
        try:
            return (self.home / APPROVED_FILE).read_text(encoding="utf-8").strip() == info["version"]
        except OSError:
            return False

    # --- 받기 ------------------------------------------------------------
    def download(self, info):
        version, name = info["version"], info["wheel"]
        try:
            data = self.get(info["base"] + name)
        except Exception as ex:
            self.log.info("업데이트 %s 받기 실패 (다음에 다시): %s", version, ex)
            return None
        if hashlib.sha256(data).hexdigest() != info["sha256"]:
            self.log.error("업데이트 %s 의 SHA256 이 맞지 않아 설치하지 않습니다.", version)
            self.result(version, "failed", "받은 파일의 SHA256 이 맞지 않아 설치하지 않았습니다.")
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        wheel = self.dir / name
        wheel.write_bytes(data)
        return wheel

    def find_update(self):
        """확인부터 받기까지 한 번에 (승인 여부와 상관없이). 있으면 (버전, .whl 경로)."""
        info = self.check()
        if not info:
            return None
        wheel = self.download(info)
        return (info["version"], wheel) if wheel else None

    def result(self, version: str, status: str, message: str = "") -> None:
        write_json(
            self.home / RESULT_FILE,
            {"version": version, "status": status, "message": message, "previous": self.state().get("previous_version", ""), "at": time.time()},
        )

    # --- 설치·되돌리기 ----------------------------------------------------
    def install(self, version: str, wheel: Path) -> bool:
        """봇이 꺼진 상태에서 부른다. 성공하면 True (새 봇이 살아남는지는 confirm/rollback 이 본다)."""
        previous = self.current()
        backup = self.dir / "backup" / previous
        try:
            if backup.exists():
                shutil.rmtree(backup)
            shutil.copytree(self.pkg_dir(), backup, ignore=shutil.ignore_patterns("__pycache__"))
        except OSError as ex:
            self.log.error("지금 코드를 백업하지 못해 업데이트를 건너뜁니다: %s", ex)
            return False
        try:
            code, out = self.pip(wheel)
        except Exception as ex:
            code, out = -1, str(ex)
        remove(self.home / APPROVED_FILE)
        remove(self.home / AVAILABLE_FILE)
        if code != 0:
            self.log.error("업데이트 %s 설치 실패 (지금 버전 유지):\n%s", version, out[-2000:])
            self._restore(backup)
            self._skip(version)
            self.result(version, "failed", "설치(pip)가 실패해 지금 버전을 그대로 씁니다.\n" + out[-500:])
            return False
        s = self.state()
        s["pending"] = {"version": version, "previous": previous, "backup": str(backup)}
        s["previous_version"] = previous
        self.save_state(s)
        self.log.warning("업데이트 설치: %s -> %s", previous, version)
        return True

    def after_run(self, ran_seconds: float, code: int, healthy_after: float) -> None:
        """봇이 한 번 끝났을 때 부른다. 막 설치한 버전이 바로 죽었으면 되돌린다."""
        s = self.state()
        pending = s.get("pending")
        if not pending:
            return
        if code == 0 or ran_seconds >= healthy_after:
            self.confirm()
            return
        self.log.error("새 버전 %s 이(가) 바로 죽어서 %s 로 되돌립니다.", pending["version"], pending["previous"])
        self._restore(Path(pending["backup"]))
        self._skip(pending["version"])
        s = self.state()
        s.pop("pending", None)
        self.save_state(s)
        self.result(
            pending["version"],
            "rolled_back",
            f"새 버전이 켜지자마자 죽어서 {pending['previous']}(으)로 되돌렸습니다. 이 버전은 다시 설치하지 않습니다.",
        )

    def confirm(self) -> None:
        s = self.state()
        pending = s.pop("pending", None)
        if pending:
            self.save_state(s)
            self.log.info("업데이트 확인 완료: 새 버전이 잘 돌고 있습니다.")
            self.result(pending["version"], "installed", "새 버전이 잘 돌고 있습니다.")

    def _restore(self, backup: Path) -> None:
        try:
            shutil.copytree(backup, self.pkg_dir(), dirs_exist_ok=True)
        except OSError as ex:
            self.log.error("되돌리기 실패: %s", ex)

    def _skip(self, version: str) -> None:
        s = self.state()
        skip = s.get("skip", [])
        if version not in skip:
            skip.append(version)
        s["skip"] = skip
        self.save_state(s)
