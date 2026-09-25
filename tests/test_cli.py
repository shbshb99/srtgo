"""CLI 설정 입력: 윈도 콘솔에서 Ctrl+V 로 붙여 넣으면 값 대신 제어문자(\\x16)만 들어온다."""

from srtgo import srtgo as cli

TOKEN = "123456789:AAHfakeTokenForTests_abcdefghijk"


def answer(monkeypatch, **values):
    monkeypatch.setattr(cli.inquirer, "prompt", lambda questions: dict(values))


def test_ctrl_v_does_not_wipe_saved_telegram_settings(monkeypatch, fake_keyring, capsys):
    fake_keyring.set_password("telegram", "token", TOKEN)
    fake_keyring.set_password("telegram", "chat_id", "100")
    answer(monkeypatch, token="\x16", chat_id="\x16")
    assert cli.set_telegram() is False
    out = capsys.readouterr().out
    assert "저장하지 않았습니다" in out and "오른쪽 클릭" in out
    assert fake_keyring.get_password("telegram", "token") == TOKEN, "빈 값으로 덮어씀"
    assert fake_keyring.get_password("telegram", "chat_id") == "100"


def test_empty_or_malformed_telegram_values_are_rejected(monkeypatch, fake_keyring, capsys):
    answer(monkeypatch, token="", chat_id="")
    assert cli.set_telegram() is False
    assert "오른쪽 클릭" in capsys.readouterr().out
    answer(monkeypatch, token="not-a-token", chat_id="abc")
    assert cli.set_telegram() is False
    out = capsys.readouterr().out
    assert "token 형식" in out and "chat_id 는 숫자" in out
    assert fake_keyring.get_password("telegram", "token") is None


def test_valid_telegram_values_are_saved(monkeypatch, fake_keyring):
    sent = []

    async def fake_send(text):
        sent.append(text)

    monkeypatch.setattr(cli, "get_telegram", lambda: fake_send)
    answer(monkeypatch, token=f" {TOKEN}\x16 ", chat_id="100")
    assert cli.set_telegram() is True
    assert fake_keyring.get_password("telegram", "token") == TOKEN
    assert fake_keyring.get_password("telegram", "chat_id") == "100"
    assert sent == ["[SRTGO] 텔레그램 설정 완료"]


def test_login_with_ctrl_v_does_not_try_to_log_in(monkeypatch, fake_keyring, capsys):
    tried = []
    monkeypatch.setattr(cli, "Korail", lambda *a, **k: tried.append(a))
    answer(monkeypatch, id="\x16", **{"pass": "pw"})
    assert cli.set_login("KTX") is False
    assert tried == [], "잘못 들어간 값으로 로그인을 시도함 (계정 잠김 위험)"
    assert "오른쪽 클릭" in capsys.readouterr().out
    assert fake_keyring.get_password("KTX", "id") is None


def test_card_with_ctrl_v_is_not_saved(monkeypatch, fake_keyring):
    answer(monkeypatch, number="\x16", password="12", birthday="900101", expire="2912")
    cli.set_card()
    assert fake_keyring.get_password("card", "ok") is None
    assert fake_keyring.get_password("card", "number") is None
