"""코레일 서버가 옛 앱 버전을 막을 때(-8204) 맞는 버전을 스스로 찾아 쓴다.

코레일은 앱을 올리면 옛 버전 요청을 "최신 버전으로 업데이트해 주세요"(-8204)로 막는다.
그때마다 사람이 버전 값을 고쳐 다시 배포하지 않아도 되도록:

1. 요청 응답이 -8204면 (GuardedSession)
2. 구글 플레이에 올라온 현재 버전과, 지금 버전에서 조금씩 올린 후보를 차례로
   로그인 없이 열차 조회로 시험해 보고 (계정에는 아무 영향이 없다)
3. 통과한 값을 ~/.srtgo/korail.json 에 저장한 뒤 원래 요청을 그 값으로 다시 보낸다.

여러 사용자의 Korail 객체가 동시에 막혀도 찾기는 한 번만 한다. 찾기에 실패하면
한동안은 다시 찾지 않는다 (서버를 두드리지 않도록).
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("srtgo.korail_version")

OUTDATED_CODE = "-8204"
PLAY_URL = "https://play.google.com/store/apps/details?id=com.korail.talk&hl=ko&gl=KR"
RETRY_AFTER_FAILURE_SECONDS = 30 * 60
PROBE_PAUSE_SECONDS = 0.5

_lock = threading.Lock()
_last_failure = 0.0
# 버전을 바꿨거나(found=(app, api)) 못 찾았을 때(found=None) 불린다. 봇이 오너에게 알리는 데 쓴다.
listeners = []


def _announce(found, blocked):
    for fn in list(listeners):
        try:
            fn(found, blocked)
        except Exception as ex:
            log.warning("버전 변경 알림 실패: %s", ex)


def config_path() -> Path:
    return Path(os.environ.get("SRTGO_HOME") or Path.home() / ".srtgo") / "korail.json"


def load_config() -> dict:
    try:
        conf = json.loads(config_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return conf if isinstance(conf, dict) else {}


def save_config(app_version: str, version: str) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conf = load_config()
    conf.update(
        app_version=app_version,
        version=version,
        updated=datetime.now().isoformat(timespec="seconds"),
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def is_outdated(text: str) -> bool:
    try:
        j = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(j, dict) and (
        j.get("h_msg_cd") == OUTDATED_CODE or OUTDATED_CODE in str(j.get("h_msg_txt") or "")
    )


def _parse(v: str):
    return tuple(int(x) for x in v.split("."))


def store_version(fetch=None) -> str | None:
    """구글 플레이에 표시된 코레일+ 버전. 못 읽으면 None."""
    try:
        if fetch is None:
            import requests

            text = requests.get(
                PLAY_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ko"}
            ).text
        else:
            text = fetch(PLAY_URL)
    except Exception as ex:  # 네트워크 오류는 후보 목록으로 넘어간다
        log.info("구글 플레이 버전 확인 실패: %s", ex)
        return None
    found = re.findall(r'\[\[\["(\d+\.\d+\.\d+)"\]\]', text or "")
    return max(found, key=_parse) if found else None


def app_candidates(current: str, store: str | None = None) -> list[str]:
    """시험해 볼 AppVersion 후보: 스토어 값을 먼저, 그다음 현재 값에서 조금씩 올린 값."""
    out = []
    if store:
        out.append(store)
    try:
        major, minor, patch = _parse(current)
    except ValueError:
        major, minor, patch = 7, 0, 8
    out += [f"{major}.{minor}.{patch + i}" for i in range(1, 13)]
    out += [f"{major}.{minor + m}.{p}" for m in range(1, 4) for p in range(0, 6)]
    out += [f"{major + 1}.0.{p}" for p in range(0, 3)]
    seen, result = set(), []
    for v in out:
        if v not in seen and v != current:
            seen.add(v)
            result.append(v)
    return result


def api_candidates(current: str) -> list[str]:
    """Version(API 계약 번호) 후보: 지금 값 다음 몇 개. 대개 AppVersion만 올리면 된다."""
    out = [current]
    if current.isdigit():
        out += [str(int(current) + i).zfill(len(current)) for i in range(1, 4)]
    return out


def probe(app_version: str, version: str) -> bool:
    """로그인 없이 열차 조회 한 번. -8204가 아니면 그 버전은 통과."""
    from . import ktx

    k = ktx.Korail("probe", "probe", auto_login=False, version_guard=False)
    k._app_version, k._version = app_version, version
    date = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
    try:
        k.search_train("서울", "부산", date, "090000", include_no_seats=True)
        return True
    except ktx.KorailError as ex:
        return not (ex.code == OUTDATED_CODE or OUTDATED_CODE in str(ex.msg))
    except Exception as ex:  # 네트워크 오류 등은 판단할 수 없으니 통과로 보지 않는다
        log.info("버전 시험 실패 (%s/%s): %s", app_version, version, ex)
        return False
    finally:
        time.sleep(PROBE_PAUSE_SECONDS)


def heal(app_version: str, version: str, *, probe_fn=None, store_fn=None, force=False):
    """막힌 (app_version, version) 대신 통과하는 값을 찾는다. 찾으면 저장하고 돌려준다.

    다른 Korail 객체가 이미 찾아 두었으면 그 값을 바로 쓴다.
    """
    global _last_failure
    probe_fn = probe_fn or probe
    store_fn = store_fn or store_version
    with _lock:
        conf = load_config()
        saved = (conf.get("app_version"), conf.get("version"))
        if all(saved) and saved != (app_version, version):
            return saved
        if not force and time.time() - _last_failure < RETRY_AFTER_FAILURE_SECONDS:
            return None
        log.warning("코레일이 앱 버전 %s/%s 를 막았습니다. 맞는 버전을 찾습니다.", app_version, version)
        store = store_fn()
        for api in api_candidates(version):
            for app in app_candidates(app_version, store):
                if probe_fn(app, api):
                    save_config(app, api)
                    log.warning("코레일 앱 버전을 %s/%s 로 바꿨습니다.", app, api)
                    _announce((app, api), (app_version, version))
                    return app, api
        _last_failure = time.time()
        log.error("통과하는 코레일 앱 버전을 찾지 못했습니다. 30분 뒤 다시 시도합니다.")
        _announce(None, (app_version, version))
        return None


class GuardedSession:
    """Korail 의 HTTP 세션을 감싸, -8204 응답이면 버전을 고쳐 한 번 다시 보낸다."""

    def __init__(self, inner, korail):
        self._inner = inner
        self._korail = korail

    @property
    def headers(self):
        return self._inner.headers

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _send(self, method, url, payload_key, kwargs):
        r = getattr(self._inner, method)(url, **kwargs)
        payload = kwargs.get(payload_key)
        if not isinstance(payload, dict) or "Version" not in payload:
            return r
        if not is_outdated(getattr(r, "text", "")):
            return r
        k = self._korail
        # 막힌 건 이 요청에 실린 값이다 (그사이 다른 요청이 k 의 값을 이미 고쳤을 수 있다).
        found = heal(payload.get("AppVersion", k._app_version), payload["Version"])
        if not found:
            return r
        k._app_version, k._version = found
        payload = dict(payload, Version=k._version, AppVersion=k._app_version)
        kwargs = dict(kwargs, **{payload_key: payload})
        headers = kwargs.get("headers")
        if headers:
            kwargs["headers"] = dict(headers, **k._dynapath_header())
        return getattr(self._inner, method)(url, **kwargs)

    def post(self, url, **kwargs):
        return self._send("post", url, "data", kwargs)

    def get(self, url, **kwargs):
        return self._send("get", url, "params", kwargs)
