"""워치독이 쓰는 자동 업데이트: 저장소에 새 버전이 올라오면 받아서 설치한다.

- 확인: 6시간마다 저장소의 updates/latest.json ({"version", "wheel", "sha256"}) 을 본다.
- 받기: 같은 폴더의 .whl 을 받아 latest.json 의 SHA256 과 맞는지 확인한다.
  해시가 없거나 다르면 설치하지 않는다.
- 설치: 봇을 깔끔하게 끄게 한 뒤(restart.request) pip 로 설치한다. 설치 전에
  지금 코드를 통째로 복사해 둔다.
- 되돌리기: 새 버전 봇이 2분 안에 죽으면 복사해 둔 코드로 되돌리고, 그 버전은
  다시 설치하지 않는다.
- 끄기: ~/.srtgo/autoupdate.off 파일을 만들면 자동 업데이트를 하지 않는다.

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
CHECK_EVERY_SECONDS = 6 * 3600
FIRST_CHECK_AFTER_SECONDS = 10 * 60
HTTP_TIMEOUT = 30
PIP_TIMEOUT = 600
OFF_FILE = "autoupdate.off"
STATE_FILE = "update_state.json"
WHEEL_RE = re.compile(r"^srtgo-([^-]+)-py3-none-any\.whl$")


def version_key(v: str):
    """'2.2.2+ktx.10' -> (2, 2, 2, 10). 숫자만 순서대로 비교한다."""
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def installed_version() -> str:
    try:
        return metadata.version("srtgo")
    except metadata.PackageNotFoundError:
        return "0"


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "srtgo-updater", "Cache-Control": "no-cache"})
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
        return not (self.home / OFF_FILE).exists()

    def due(self) -> bool:
        return self.enabled() and time.time() >= self.next_check

    # --- 확인·받기 --------------------------------------------------------
    def _manifest(self):
        """읽을 수 있는 첫 latest.json 과 그 폴더 주소. 하나도 못 읽으면 (None, None)."""
        errors = []
        for base in UPDATE_BASES:
            try:
                m = json.loads(self.get(base + MANIFEST))
            except Exception as ex:
                errors.append(f"{base}: {ex}")
                continue
            if isinstance(m, dict):
                return m, base
        self.log.info("업데이트 확인 실패 (다음에 다시): %s", "; ".join(errors))
        return None, None

    def find_update(self):
        """설치할 새 버전이 있으면 (버전, 받은 .whl 경로). 없으면 None."""
        self.next_check = time.time() + CHECK_EVERY_SECONDS
        manifest, base = self._manifest()
        if not manifest:
            return None
        current = self.current()
        version = str(manifest.get("version") or "")
        name = str(manifest.get("wheel") or "")
        sha = str(manifest.get("sha256") or "").lower()
        m = WHEEL_RE.match(name)
        if not m or m.group(1) != version or "/" in name or "\\" in name:
            self.log.warning("latest.json 형식이 이상해 업데이트하지 않습니다: %s", manifest)
            return None
        if version in set(self.state().get("skip", [])) or version_key(version) <= version_key(current):
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            self.log.warning("업데이트 %s 에 SHA256 정보가 없어 설치하지 않습니다.", version)
            return None
        try:
            data = self.get(base + name)
        except Exception as ex:
            self.log.info("업데이트 %s 받기 실패 (다음에 다시): %s", version, ex)
            return None
        if hashlib.sha256(data).hexdigest() != sha:
            self.log.error("업데이트 %s 의 SHA256 이 맞지 않아 설치하지 않습니다.", version)
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        wheel = self.dir / name
        wheel.write_bytes(data)
        self.log.info("새 버전 %s 을 받았습니다 (지금 %s).", version, current)
        return version, wheel

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
        if code != 0:
            self.log.error("업데이트 %s 설치 실패 (지금 버전 유지):\n%s", version, out[-2000:])
            self._restore(backup)
            self._skip(version)
            return False
        s = self.state()
        s["pending"] = {"version": version, "previous": previous, "backup": str(backup)}
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

    def confirm(self) -> None:
        s = self.state()
        if s.pop("pending", None):
            self.save_state(s)
            self.log.info("업데이트 확인 완료: 새 버전이 잘 돌고 있습니다.")

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
