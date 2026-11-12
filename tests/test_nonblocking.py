"""통로 호출이 오래 걸려도 봇이 멈추지 않는다 (ktx.26)."""
import asyncio
import threading

import pytest

from srtgo import human_client as hc


def test_slow_search_does_not_block_commands_or_buttons(env, monkeypatch):
    from conftest import ktx_train, day, until, run
    release = threading.Event()
    calls = []

    def slow_search(**kw):
        calls.append(kw)
        assert release.wait(10)
        return [ktx_train(527, "200900", "231200")]

    monkeypatch.setattr(hc, "search_train", slow_search)
    monkeypatch.setenv("SRTGO_HUMAN_FETCH", "1")

    async def go():
        o = env.owner
        await o.command("start")
        await o.press("book:KTX")
        await o.press("dep:서울")
        await o.press("arr:부산")
        await o.press(f"date:{day()}")
        await o.press("time:19")
        screen = o.screen
        search = asyncio.ensure_future(o.press("search"))
        await until(lambda: calls)
        # 조회가 도는 동안 명령은 바로 답한다
        await asyncio.wait_for(o.command("start"), 1)
        await asyncio.wait_for(o.command("stop"), 1)
        # 버튼은 줄 서지 않고 바로 끝난다(조회 중 안내), 새 조회도 쌓이지 않는다
        await asyncio.wait_for(o.tap("search", on=screen), 1)
        await asyncio.wait_for(o.tap("home", on=screen), 1)
        assert len(calls) == 1
        assert not search.done()
        release.set()
        await asyncio.wait_for(search, 5)
        assert any("KTX 527" in x for x in o.screen.labels()), o.screen.labels()
        assert env.bot.context_for(o.chat_id).busy is None

    run(go())


def test_remote_down_is_reported_without_calling(monkeypatch):
    monkeypatch.setattr(hc, "_health", lambda api: None)
    posted = []
    monkeypatch.setattr(hc, "_send", lambda *a, **k: posted.append(a))
    with pytest.raises(hc.HumanFetchError, match="꺼져 있습니다"):
        hc._run_remote([], "q")
    assert posted == []


def test_state_call_checks_remote_health_first(monkeypatch):
    monkeypatch.setattr(hc, "_health", lambda api: None)
    monkeypatch.setattr(hc, "HUMAN_FETCH_PS1", r"X:\없는파일.ps1")   # 이 PC 에 직접 길이 없으면 레노버 통로로 간다
    posted = []
    monkeypatch.setattr(hc, "_send", lambda *a, **k: posted.append(a))
    with pytest.raises(hc.HumanReserveError) as e:
        hc._post_state(hc.LOGIN_URL, [], "q")
    assert e.value.definite and "꺼져 있습니다" in str(e.value) and posted == []


def test_timeouts_by_kind(monkeypatch):
    seen = []

    class R:
        status = 200

        def read(self):
            return b'{"js": "{}"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(hc.urllib.request, "urlopen", lambda req, timeout=0: seen.append(timeout) or R())
    monkeypatch.setattr(hc, "_health", lambda api: {"ok": True, "v": hc.WANT_SERVER_VERSION})
    monkeypatch.setattr(hc, "HUMAN_FETCH_PS1", r"X:\없는파일.ps1")
    hc._run_remote([], "q")
    hc._post_state(hc.LOGIN_URL, [], "q")
    assert seen == [hc.FETCH_TIMEOUT, hc.ACTION_TIMEOUT] == [90, 120]


def test_busy_channel_gives_up_waiting(monkeypatch):
    monkeypatch.setattr(hc, "LOCK_WAIT", 0.05)
    with hc._Held():
        with pytest.raises(hc.HumanFetchError, match="아직 끝나지"):
            threading.Thread  # 같은 스레드에서 다시 잡으면 못 잡는다(재진입 아님)
            hc._Held().__enter__()
    with pytest.raises(hc.HumanReserveError) as e:
        with hc._Held():
            hc._Held(hc.HumanReserveError).__enter__()
    assert e.value.definite
