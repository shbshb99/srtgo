"""코레일 웹 로그인·예약(결제 전 단계까지). 실제 코레일은 부르지 않고 가짜 화면 값으로 시험한다."""
import json
from datetime import datetime

import pytest

from srtgo import bot as botmod
from srtgo import human_client as hc
from srtgo.ktx import AdultPassenger, ChildPassenger, Korail, Train


def make_train(no="067", special=False):
    return Train({
        "h_trn_clsf_nm": "KTX", "h_trn_no": no, "h_dpt_rs_stn_nm": "서울", "h_arv_rs_stn_nm": "부산",
        "h_dpt_dt": "20261030", "h_dpt_tm": "092800", "h_arv_dt": "20261030", "h_arv_tm": "123500",
        "h_run_dt": "20261030", "h_rsv_psb_flg": "Y", "h_rsv_psb_nm": "예약가능",
        "h_gen_rsv_cd": "11", "h_spe_rsv_cd": "00", "h_wait_rsv_flg": "-2",
    })


def test_login_tab_by_id_shape():
    assert hc.login_tab("1234567890") == "memberNo"
    assert hc.login_tab("a@b.com") == "email"
    assert hc.login_tab("010-1234-5678") == "phone"


def test_login_actions_use_real_input_and_keep_password_only_in_text():
    acts = hc.build_login_actions("1234567890", "pw-secret-1")
    assert acts[0] == {"click": "button#memberNo"}
    texts = [a["text"] for a in acts if "text" in a]
    assert texts == ["1234567890", "pw-secret-1"]
    assert any(a.get("click") == ".btn_bn-depblue" for a in acts)


@pytest.mark.parametrize("raw,tab,value", [
    ("1234567890", "memberNo", "1234567890"),
    (" 1234567890\n", "memberNo", "1234567890"),
    ("fake.probe@example.com", "email", "fake.probe@example.com"),
    ("01012345678", "phone", "01012345678"),
    ("010-1234-5678", "phone", "01012345678"),
    ("010 1234 5678", "phone", "01012345678"),
    ("+82 10-1234-5678", "phone", "01012345678"),
    ("010-123-4567", "phone", "0101234567"),
    ("１２３４５６７８９０", "memberNo", "1234567890"),
])
def test_login_tab_and_value_per_format(raw, tab, value):
    assert hc.login_tab(raw) == tab
    assert hc.login_id_value(raw) == value


def test_login_actions_pick_matching_tab_and_input_value():
    for raw, tab, typed in (("1234567890", "memberNo", "1234567890"), ("a@b.kr", "email", "a@b.kr"),
                            ("010-1234-5678", "phone", "01012345678")):
        acts = hc.build_login_actions(raw, "pw-1")
        assert acts[0] == {"click": "button#%s" % tab}
        assert [a["text"] for a in acts if "text" in a] == [typed, "pw-1"]


def test_login_id_problem_never_leaks_value():
    assert hc.login_id_problem("1234567890") is None and hc.login_id_problem("a@b.kr") is None
    assert hc.login_id_problem("01012345678") is None
    why = hc.login_id_problem("12345")
    assert "아이디 종류" in why and "5자" in why and "12345" not in why
    assert any(h in why for h in botmod.CREDENTIAL_HINTS)


def test_login_with_rejects_bad_shape_without_touching_web(monkeypatch):
    monkeypatch.setattr(hc, "_state", lambda *a, **k: pytest.fail("웹을 부르면 안 된다"))
    ok, why = hc.login_with("12345", "pw")
    assert not ok and "아이디 종류" in why


def test_classify_login_failure():
    assert hc.classify_login_failure("안내 메시지 회원번호를 정확하게 입력하여 주십시오. 확인")[0] == "format"
    assert "아이디 종류" in hc.classify_login_failure("회원번호를 정확하게 입력하여 주십시오")[1]
    assert hc.classify_login_failure("로그인 정보를 다시 확인해 주세요. 비밀번호 5회 연속 오류시 로그인이 제한됩니다")[0] == "account"
    assert hc.classify_login_failure("인증되지 않은 휴대전화번호입니다")[0] == "account"
    kind, why = hc.classify_login_failure("전혀 모르는 안내 " * 40)
    assert kind == "other" and len(why) == 100


def test_login_failure_text_is_passed_on_instead_of_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "LOGIN_ID_MARK", tmp_path / "mark")
    monkeypatch.setattr(hc, "is_logged_in", lambda: False)
    monkeypatch.setattr(hc, "_state", lambda *a, **k: {
        "url": "https://www.korail.com/ticket/login", "loggedOut": True, "modal": "", "body": "점검 중입니다 " * 40})
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    ok, why = hc.login_with("1234567890", "pw")
    assert not ok and why == hc.LOGIN_UNKNOWN_MSG and "점검" not in why   # 화면 본문은 내보내지 않는다


def test_standby_text_hidden_for_web_reserve(monkeypatch):
    monkeypatch.setenv("SRTGO_HUMAN_FETCH", "1")
    assert botmod.web_reserve_mode(botmod.BOOKING_RAIL)
    monkeypatch.setenv("SRTGO_HUMAN_FETCH", "0")
    assert not botmod.web_reserve_mode(botmod.BOOKING_RAIL)


def test_reserve_actions_stop_before_payment():
    acts = hc.build_reserve_actions(make_train(), False, "20261030", 9, today=datetime(2026, 10, 29))
    flat = json.dumps(acts, ensure_ascii=False)
    assert 'span.num:text-is(\\"067\\")' in flat and ".price_box.gen" in flat
    assert 'button.reservbtn' in flat and "결제" not in flat
    assert ".price_box.spe" in json.dumps(
        hc.build_reserve_actions(make_train(), True, "20261030", 9, today=datetime(2026, 10, 29)))


def test_parse_limit_and_pay_seconds():
    assert hc.parse_limit("예약이 완료되었습니다. 결제 제한시간 2026-10-29 17:30 까지") == "2026-10-29 17:30"
    assert hc.parse_limit("아무 글") is None
    now = datetime(2026, 10, 29, 17, 0)
    assert botmod.pay_limit_seconds("2026-10-29 17:30", now) == 1800
    assert botmod.pay_limit_seconds("17:10", now) == 600
    assert botmod.pay_limit_seconds("약 10분, 확인 못 함", now) == 600
    assert botmod.pay_limit_seconds(None, now) is None


def test_classify_reserve_page():
    assert hc.classify_reserve_page({"url": "https://www.korail.com/ticket/login"})[0] == "login"
    assert hc.classify_reserve_page({"url": "https://www.korail.com/ticket/search/list",
                                     "modal": "안내 메세지 잔여석이 없습니다"})[0] == "fail"
    assert hc.classify_reserve_page({"url": "https://www.korail.com/ticket/search/list"})[0] == "fail"
    assert hc.classify_reserve_page({"url": "https://www.korail.com/ticket/x", "body": "화면"})[0] == "unknown"
    ok = {"url": "https://www.korail.com/ticket/reservation/done", "body": "예약이 완료 결제 제한시간 17:30"}
    assert hc.classify_reserve_page(ok)[0] == "ok"


@pytest.fixture
def fake_state(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "OUT_DIR", tmp_path)
    monkeypatch.setattr(hc, "LOGIN_ID_MARK", tmp_path / "mark")
    monkeypatch.setattr(hc, "is_logged_in", lambda: True)
    calls = []

    def run(page):
        def _state(actions=None, url=None, query=None, smart=None):
            calls.append((url, actions))
            return page
        monkeypatch.setattr(hc, "_state", _state)
        return calls
    return run


def test_reserve_ok_returns_web_reservation(fake_state):
    calls = fake_state({"url": "https://www.korail.com/ticket/reservation/done",
                        "body": "예약이 완료 결제 제한시간 2026-10-29 17:30"})
    res = hc.reserve(make_train(), creds=("1234567890", "pw"))
    assert isinstance(res, hc.WebReservation) and res.limit_text == "2026-10-29 17:30"
    assert calls and calls[0][0] == hc.LIST_URL


def test_reserve_without_limit_uses_fallback(fake_state):
    fake_state({"url": "https://www.korail.com/ticket/reservation/done", "body": "예약이 완료 결제하기"})
    assert hc.reserve(make_train(), creds=("1", "pw")).limit_text == hc.PAY_LIMIT_FALLBACK


def test_reserve_rejected_is_definite_and_unknown_is_not(fake_state):
    fake_state({"url": "https://www.korail.com/ticket/search/list", "modal": "잔여석이 없습니다"})
    with pytest.raises(hc.HumanReserveError) as e:
        hc.reserve(make_train(), creds=("1", "pw"))
    assert e.value.definite
    fake_state({"url": "https://www.korail.com/ticket/zzz", "body": "?"})
    with pytest.raises(hc.HumanReserveError) as e:
        hc.reserve(make_train(), creds=("1", "pw"))
    assert not e.value.definite


def test_login_with_reuses_session_and_reports_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "OUT_DIR", tmp_path)
    monkeypatch.setattr(hc, "LOGIN_ID_MARK", tmp_path / "mark")
    seq = []
    monkeypatch.setattr(hc, "is_logged_in", lambda: True)

    def _state(actions=None, url=None, query=None, smart=None):
        seq.append(actions)
        return {"url": "https://www.korail.com/ticket/login", "loggedOut": True,
                "modal": "안내 메세지 등록되지 않는 회원번호입니다. 확인"}
    monkeypatch.setattr(hc, "_state", _state)
    ok, why = hc.login_with("0000000000", "x")
    assert not ok and "회원" in why and any(h in why for h in botmod.CREDENTIAL_HINTS)

    def _state_ok(actions=None, url=None, query=None):
        seq.append(actions)
        return {"url": "https://www.korail.com/ticket/main", "loggedOut": False, "modal": "", "body": "승차권예매 로그아웃 홍길동 님 마일리지 : 1234p"}
    monkeypatch.setattr(hc, "_state", _state_ok)
    assert hc.login_with("1234567890", "pw") == (True, None)
    n = len(seq)
    assert hc.login_with("1234567890", "pw") == (True, None)   # 쿠키가 살아 있으면 다시 입력하지 않는다
    assert len(seq) == n


def test_korail_reserve_goes_to_web_only_for_one_adult(monkeypatch):
    monkeypatch.setenv("SRTGO_HUMAN_FETCH", "1")
    k = Korail("1234567890", "pw", auto_login=False)
    got = {}
    monkeypatch.setattr(hc, "reserve", lambda train, special=False, creds=None: got.update(special=special, creds=creds) or "R")
    assert k.reserve(make_train(), passengers=[AdultPassenger(1)]) == "R"
    assert got["creds"] == ("1234567890", "pw")
    with pytest.raises(hc.HumanReserveError):
        k.reserve(make_train(), passengers=[AdultPassenger(2)])
    with pytest.raises(hc.HumanReserveError):
        k.reserve(make_train(), passengers=[ChildPassenger(1)])


def test_korail_login_uses_web_login(monkeypatch):
    monkeypatch.setenv("SRTGO_HUMAN_FETCH", "1")
    monkeypatch.setattr(hc, "login_with", lambda i, p: (False, "비밀번호가 일치하지 않습니다"))
    k = Korail("1234567890", "bad")
    assert not k.logined and "비밀번호" in k.login_error
    monkeypatch.setattr(hc, "login_with", lambda i, p: (True, None))
    assert Korail("1234567890", "pw").logined

