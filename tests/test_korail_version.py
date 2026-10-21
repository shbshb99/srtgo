"""코레일이 옛 앱 버전을 막을 때(-8204) 봇이 스스로 맞는 버전을 찾아 쓰는지."""

import json

import pytest

from srtgo import korail_version as kv
from srtgo import ktx

OUTDATED = {"strResult": "FAIL", "h_msg_cd": "-8204", "h_msg_txt": "최신 버전으로 업데이트해 주세요. (-8204)"}


class Resp:
    def __init__(self, obj):
        self.text = json.dumps(obj)


class Server:
    """AppVersion 이 minimum 이상이어야 받아 주는 가짜 코레일."""

    def __init__(self, minimum="7.1.0"):
        self.minimum = kv._parse(minimum)
        self.headers = {}
        self.sent = []

    def post(self, url, data=None, headers=None):
        self.sent.append((url, dict(data or {}), dict(headers or {})))
        if "MobileService" in url:
            return Resp({})
        if kv._parse(data.get("AppVersion", "0.0.0")) < self.minimum:
            return Resp(OUTDATED)
        return Resp({"strResult": "SUCC", "trn_infos": {"trn_info": []}})

    def get(self, url, params=None, headers=None):
        return self.post(url, data=params, headers=headers)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SRTGO_HOME", str(tmp_path))
    monkeypatch.setattr(kv, "_last_failure", 0.0)
    monkeypatch.setattr(kv, "PROBE_PAUSE_SECONDS", 0)
    monkeypatch.setattr(kv, "listeners", [])
    return tmp_path


def fake_probe(server):
    def probe(app, api):
        return kv._parse(app) >= server.minimum
    return probe


def guarded(server):
    k = ktx.Korail("a", "b", auto_login=False)
    k._session = kv.GuardedSession(server, k)
    return k


def test_blocked_request_does_not_hammer_by_default(home, monkeypatch):
    """자동 찾기는 꺼져 있다: 막히면 그대로 실패하고 버전을 찾으러 두드리지 않는다."""
    server = Server("7.1.0")
    monkeypatch.setattr(kv, "probe", lambda a, v: pytest.fail("자동으로 버전을 찾으러 감"))
    k = guarded(server)
    with pytest.raises(ktx.KorailError) as ei:
        k.search_train("동탄", "순천", "20261024", "160000")
    assert kv.is_blocked(ei.value) and len(server.sent) == 1


def test_blocked_search_finds_new_version_saves_it_and_retries(home, monkeypatch):
    monkeypatch.setattr(kv, "AUTO_HEAL", True)
    server = Server("7.1.0")
    probed = []
    monkeypatch.setattr(kv, "probe", lambda a, v: probed.append((a, v)) or fake_probe(server)(a, v))
    monkeypatch.setattr(kv, "store_version", lambda: None)
    k = guarded(server)
    with pytest.raises(ktx.NoResultsError):  # 막히지 않고 정상 응답(열차 없음)까지 갔다
        k.search_train("동탄", "순천", "20261024", "160000")
    assert k._app_version == "7.1.0"
    assert json.loads((home / "korail.json").read_text(encoding="utf-8"))["app_version"] == "7.1.0"
    retried = server.sent[-1]
    assert retried[1]["AppVersion"] == "7.1.0" and "x-dynapath-m-token" in retried[2]
    # 새로 만든 객체는 저장된 값으로 시작한다 (다시 찾지 않는다)
    assert ktx.Korail("a", "b", auto_login=False)._app_version == "7.1.0"


def test_store_version_is_tried_first(home):
    server = Server("7.4.2")
    tried = []

    def probe(a, v):
        tried.append(a)
        return fake_probe(server)(a, v)

    assert kv.heal("7.0.8", "250601003", probe_fn=probe, store_fn=lambda: "7.4.2") == ("7.4.2", "250601003")
    assert tried == ["7.4.2"]


def test_second_client_reuses_version_found_by_first(home):
    calls = []
    assert kv.heal("7.0.8", "250601003", probe_fn=lambda a, v: calls.append(a) or a == "7.0.9", store_fn=lambda: None)
    n = len(calls)
    assert kv.heal("7.0.8", "250601003", probe_fn=lambda a, v: calls.append(a), store_fn=lambda: None) == ("7.0.9", "250601003")
    assert len(calls) == n, "이미 찾은 값을 두고 서버를 다시 두드림"


def test_failure_backs_off_instead_of_hammering(home):
    calls = []
    assert kv.heal("7.0.8", "1", probe_fn=lambda a, v: calls.append(a) and False, store_fn=lambda: None) is None
    n = len(calls)
    assert kv.heal("7.0.8", "1", probe_fn=lambda a, v: calls.append(a) and False, store_fn=lambda: None) is None
    assert len(calls) == n


def test_store_version_parsing():
    page = 'junk [[["7.0.8"]],x [[["7.1.2"]], [[["1.0.0"]]'
    assert kv.store_version(fetch=lambda url: page) == "7.1.2"
    assert kv.store_version(fetch=lambda url: "nothing") is None
    assert kv.store_version(fetch=lambda url: (_ for _ in ()).throw(OSError("offline"))) is None


def test_other_failures_are_not_retried(home):
    server = Server("0.0.0")
    server.post = lambda url, data=None, headers=None: Resp({"strResult": "FAIL", "h_msg_cd": "P058", "h_msg_txt": "로그인"})
    k = guarded(server)
    with pytest.raises(ktx.NeedToLoginError):
        k.search_train("서울", "부산", "20261024", "090000")
    assert not (home / "korail.json").exists()


def test_owner_is_told_when_version_changes(home):
    seen = []
    kv.listeners.append(lambda found, blocked: seen.append((found, blocked)))
    kv.heal("7.0.8", "250601003", probe_fn=lambda a, v: a == "7.0.9", store_fn=lambda: None)
    assert seen == [(("7.0.9", "250601003"), ("7.0.8", "250601003"))]
