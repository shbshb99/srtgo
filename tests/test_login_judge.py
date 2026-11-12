"""코레일 웹 로그인 성공·실패 판정 시험. 이름·마일리지는 더미(홍길동·1234p)다. 실제 계정으로 로그인하지 않는다."""
import pytest

from srtgo import bot as botmod
from srtgo import human_client as hc

# 운영PC 에서 로그인된 메인 화면 본문(이름·숫자만 더미로 바꿈)
SUCCESS_BODY = ("본문 바로가기 주메뉴 바로가기 한국철도 승차권예매 기차여행 로그아웃 장바구니 마이페이지 고객센터 기업전용 "
                "Language 팝업열기 비즈니스 홍길동 님 마일리지 : 1234p 승차권 예매 승차권 확인")
FORM_BODY = "한국철도 로그인 회원번호 이메일 휴대전화 아이디 비밀번호 로그인 아이디 찾기 비밀번호 찾기 회원가입"
ERROR_MODAL = "안내 메세지 비밀번호가 일치하지 않습니다. 홍길동 님 1234567890 확인"

SUCCESS = {"url": "https://www.korail.com/ticket/main", "loggedOut": False, "loginForm": False,
           "logoutLink": True, "modal": "", "body": SUCCESS_BODY}
FORM = {"url": "https://www.korail.com/ticket/login", "loggedOut": True, "loginForm": True,
        "logoutLink": False, "modal": "", "body": FORM_BODY}
ERROR = dict(FORM, modal=ERROR_MODAL)


@pytest.fixture
def web(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "OUT_DIR", tmp_path)
    monkeypatch.setattr(hc, "LOGIN_ID_MARK", tmp_path / "mark")
    monkeypatch.setattr(hc, "is_logged_in", lambda: False)
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)

    def run(*pages):
        seq = list(pages)
        calls = []

        def _state(actions=None, url=None, query=None, smart=None):
            calls.append((url, actions))
            return seq.pop(0) if len(seq) > 1 else seq[0]
        monkeypatch.setattr(hc, "_state", _state)
        return calls
    return run


def test_judge_success_screen_even_without_new_flags():
    assert hc.judge_login(SUCCESS) == "ok"
    old = {"url": SUCCESS["url"], "loggedOut": True, "modal": "", "body": SUCCESS_BODY}   # 옛 읽기: loggedOut 이 거꾸로였다
    assert hc.judge_login(old) == "ok"


def test_judge_login_form_and_error_are_fail():
    assert hc.judge_login(FORM) == "fail"
    assert hc.judge_login(ERROR) == "fail"
    assert hc.judge_login({"url": FORM["url"], "modal": "", "body": FORM_BODY}) == "fail"


def test_judge_unknown_when_no_signal():
    assert hc.judge_login({"url": "https://www.korail.com/ticket/x", "body": "점검 중입니다"}) == "unknown"
    assert hc.judge_login({"url": FORM["url"], "body": "점검 중입니다", "loginForm": False}) == "unknown"


def test_login_with_success_screen_is_ok_and_remembers(web):
    calls = web(SUCCESS)
    assert hc.login_with("1234567890", "pw") == (True, None)
    assert len(calls) == 1
    assert hc.LOGIN_ID_MARK.exists()


def test_login_with_reuses_cookie_without_typing(web, monkeypatch):
    calls = web(SUCCESS)
    assert hc.login_with("1234567890", "pw") == (True, None)
    monkeypatch.setattr(hc, "is_logged_in", lambda: True)
    n = len(calls)
    assert hc.login_with("1234567890", "pw") == (True, None)
    assert len(calls) == n   # 이어서 진행: 다시 입력하지 않았다


def test_login_with_error_sends_only_short_masked_modal(web):
    web(ERROR)
    ok, why = hc.login_with("1234567890", "pw")
    assert not ok and "비밀번호" in why
    assert "홍길동" not in why and "1234567890" not in why and len(why) <= 100
    assert any(h in why for h in botmod.CREDENTIAL_HINTS)


def test_login_with_form_without_modal_does_not_send_body(web):
    web(FORM)
    ok, why = hc.login_with("1234567890", "pw")
    assert not ok and FORM_BODY[:20] not in why and "안내 문구가 없습니다" in why


def test_login_with_unknown_rereads_once_then_stops_without_body(web):
    unknown = {"url": "https://www.korail.com/ticket/x", "loginForm": False, "logoutLink": False,
               "modal": "", "body": "홍길동 님 알 수 없는 화면 1234p"}
    calls = web(unknown)
    ok, why = hc.login_with("1234567890", "pw")
    assert not ok and why == hc.LOGIN_UNKNOWN_MSG
    assert "홍길동" not in why and "1234p" not in why
    assert len(calls) == 2   # 처음 읽기 + 다시 읽기 한 번
    assert any(h in why for h in botmod.CREDENTIAL_HINTS)   # 대기를 멈춘다(계속 두드리지 않는다)


def test_login_with_unknown_then_success_on_reread(web):
    unknown = {"url": "https://www.korail.com/ticket/x", "modal": "", "body": "로딩"}
    web(unknown, SUCCESS)
    assert hc.login_with("1234567890", "pw") == (True, None)


def test_mask_personal():
    m = hc.mask_personal("홍길동 님 환영합니다 마일리지 1234p 회원번호 1234567890 " + "가" * 200)
    assert "홍길동" not in m and "1234" not in m and len(m) <= 100
    assert "님" in m


def test_reserve_record_file_has_no_personal_values(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "OUT_DIR", tmp_path)
    monkeypatch.setattr(hc, "LOGIN_ID_MARK", tmp_path / "mark")
    monkeypatch.setattr(hc, "is_logged_in", lambda: True)
    monkeypatch.setattr(hc, "_state", lambda *a, **k: {
        "url": "https://www.korail.com/ticket/reservation/done", "body": "예약 완료 홍길동 님 결제 제한시간 17:30 1234567890"})
    from tests.test_human_reserve import make_train
    hc.reserve(make_train(), creds=("1234567890", "pw"))
    saved = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("reserve_*.txt"))
    assert saved and "홍길동" not in saved and "1234567890" not in saved
