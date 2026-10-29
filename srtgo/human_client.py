"""코레일 웹(www.korail.com) 열차 조회를 사람처럼 입력하는 통로로 보낸다. 표준 라이브러리만 쓴다.

앱 주소(smart.letskorail.com)는 -8204 로 막히므로, 열차 목록 조회(search_train)만 이 길로 돌린다.
레노버에서는 ops/human_fetch.ps1 을 직접 부르고, 다른 PC 에서는 레노버의 스크래플링 통로(HTTP)에 같은
동작 목록과 읽기 스크립트를 보낸다. 진짜 크롬 창에서 진짜 마우스·키보드로 역·날짜·시각을 넣고 조회를 눌러
나온 화면의 열차 줄(HTML)을 읽어 Train 객체로 바꾼다. 조회는 로그인이 필요 없다.
로그인(login)과 예약(reserve)도 같은 길로 웹 화면에 사람처럼 입력하고, 예약은 결제 전 단계에서 멈춘다. 결제는 사람이 한다.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from .pacing import HumanPacer

log = logging.getLogger("srtgo.human")

LIST_URL = "https://www.korail.com/ticket/search/general"
PROFILE = "korail"
HUMAN_FETCH_PS1 = os.environ.get("HUMAN_FETCH_PS1", r"C:\shb-gpt\ops\human_fetch.ps1")
REMOTE_API = os.environ.get("HUMAN_FETCH_API", "http://100.106.42.28:8765")
OUT_DIR = Path(os.environ.get("HUMAN_FETCH_OUT", r"C:\shb-run\tmp\human_fetch\korail"))
TIMEOUT = 240

# 열차 줄(li.tckList)을 한 줄씩 짧게 줄여 돌려준다. 값 읽기만 하고 입력은 하지 않는다.
READ_JS = r"""(async () => {
  const lis = [...document.querySelectorAll('li.tckList')];
  const c = s => s.replace(/\s+/g, ' ').replace(/ title=""/g, '').replace(/ href="#none"/g, '').replace(/> </g, '><');
  return JSON.stringify({url: location.href, when: ((document.body.innerText.match(/\d{4}-\d{2}-\d{2}\([^)]\) \d{2}:\d{2}/) || [''])[0]), rows: lis.map(l => c(l.outerHTML))});
})()
"""

_lock = threading.Lock()
_pacer = HumanPacer()
_last = [None]
_sleep = time.sleep
_clock = time.monotonic


class HumanFetchError(Exception):
    pass


def build_actions(dep, arr, date, hour, today=None):
    """역·날짜·시각을 넣고 조회를 누르는 동작 목록. date 는 YYYYMMDD, hour 는 0~23."""
    today = today or datetime.now()
    y, m, d = int(date[:4]), int(date[4:6]), int(date[6:8])
    months_ahead = (y - today.year) * 12 + (m - today.month)
    if months_ahead < 0:
        raise HumanFetchError("지난 날짜는 조회할 수 없습니다: " + date)
    r = [200, 600]   # 중간 클릭 뒤 쉼(밀리초). 마지막 조회 클릭만 기본(500~1300)으로 길게 둔다
    steps = [
        {"click": "input[name=txtGoStart]", "rest": r},
        {"click": 'a:text-is("%s") >> visible=true' % dep, "rest": r},
        {"click": "input[name=txtGoEnd]", "rest": r},
        {"click": 'a:text-is("%s") >> visible=true' % arr, "rest": r},
        {"click": "input[name=startDate]", "rest": r},
    ]
    for _ in range(months_ahead):
        steps += [{"click": ".slick-next >> visible=true >> nth=0", "rest": r}]
    steps += [
        {"click": 'td:not(.disabled) a:has(span.day:text-is("%d")) >> visible=true' % d, "rest": r},
    ]
    # 시각 줄은 5시간씩 움직이고, 처음 위치는 열 때의 시각(지금 시각 근처)이라 먼저 맨 앞(0~4시)으로 보낸다
    # (2026-10-29 확인: 15~19시에서 이전 3번이면 0~4시, 다음 1번이면 5~9시, 끝은 19~23시에서 멈춤)
    # 통로가 새 판(smart)이면 이 목록 대신 화면을 읽어 필요한 칸·칸수만 누른다. 이 목록은 옛 통로와 다시 하기용이다.
    for _ in range(4):
        steps += [{"click": ".slide_list .slick-prev >> visible=true", "rest": r}]
    for _ in range(hour // 5):
        steps += [{"click": ".slide_list .slick-next >> visible=true", "rest": r}]
    steps += [
        {"click": '.slide_list .slick-active a:text-is("%02d시")' % hour, "rest": r},
        {"click": 'text="적용" >> visible=true', "rest": r},
        {"click": ".btn_lookup"}, {"wait_for": "li.tckList", "timeout": 20000},
    ]
    return steps


LOCAL_API = "http://127.0.0.1:8765"
RESUME_ACTIONS = [{"press": "F5"}, {"wait_for": "li.tckList", "timeout": 20000}]
WANT_SERVER_VERSION = 3   # srtgo/scrape_server.py 의 SERVER_VERSION 과 같게 올린다
NO_SERVER_MSG = ("화면 세션의 통로(예약작업 ShbScrapeApi)가 꺼져 있습니다. 운영PC 에 로그온한 상태에서 "
                 "prod2_scrape_server_pkg.ps1 을 한 번 실행해 예약작업을 만드세요(봇이 통로를 직접 띄우지 않습니다)")
ERROR_TEXT_LIMIT = 600   # 통로·HTTP 오류 본문을 이만큼 남긴다(예전 200자는 원인이 잘렸다)
last_marks = []   # 마지막 조회의 단계별 누적 시간(초), 측정용
last_info = {}    # 마지막 조회의 통로 응답 요약(mode, retry, timing)


def _health(api):
    """통로가 살아 있으면 /health 응답(dict), 아니면 None."""
    try:
        with urllib.request.urlopen(api + "/health", timeout=2) as r:
            if r.status != 200:
                return None
            try:
                return json.load(r)
            except ValueError:
                return {}
    except Exception:
        return None


def _session_id():
    """이 프로세스의 윈도우 세션 번호. 0 은 보이지 않는 서비스 세션이다. 알 수 없으면 None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return sid.value
    except Exception:
        pass
    return None


def _auth_headers():
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SCRAPE_API_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _ensure_local_server():
    """이 PC 의 화면 세션 통로가 켜져 있는지만 본다. 봇은 통로나 크롬을 직접 띄우지 않는다.
    (세션 0 에서 띄운 통로가 8765 와 크롬 프로필을 잡아 502 로 죽은 일이 있었다, 2026-10-29)
    통로가 옛 판이면 /quit 로 끈다. 예약작업이 10초 안에 새 판으로 다시 켠다."""
    h = _health(LOCAL_API)
    if h is None:
        if _session_id() == 0:
            raise HumanFetchError(NO_SERVER_MSG)
        for _ in range(12):   # 예약작업이 죽은 통로를 10초 간격으로 다시 켠다
            _sleep(1)
            h = _health(LOCAL_API)
            if h is not None:
                break
        else:
            raise HumanFetchError(NO_SERVER_MSG)
    if int(h.get("v", 0)) < WANT_SERVER_VERSION:
        try:
            urllib.request.urlopen(urllib.request.Request(LOCAL_API + "/quit", b"{}", _auth_headers(), method="POST"), timeout=3)
        except Exception:
            return   # 옛 통로가 /quit 을 모르면 그대로 쓴다
        for _ in range(40):
            _sleep(1)
            h2 = _health(LOCAL_API)
            if h2 is not None and int(h2.get("v", 0)) >= WANT_SERVER_VERSION:
                return
        raise HumanFetchError("옛 통로를 끈 뒤 새 통로가 40초 안에 켜지지 않았습니다. " + NO_SERVER_MSG)


def _error_text(ex):
    """오류를 앞 ERROR_TEXT_LIMIT 자까지 풀어 쓴다. HTTP 오류는 통로가 준 본문(detail)을 읽는다."""
    if isinstance(ex, urllib.error.HTTPError):
        try:
            raw = ex.read().decode("utf-8", "replace")
            try:
                d = json.loads(raw)
                raw = "%s: %s" % (d.get("error", ""), d.get("detail", raw))
            except ValueError:
                pass
        except Exception:
            raw = str(ex)
        return ("통로 오류 %s %s" % (ex.code, raw))[:ERROR_TEXT_LIMIT]
    return ("%s: %s" % (type(ex).__name__, str(ex)))[:ERROR_TEXT_LIMIT]


def _send(api, body, error_cls, **kw):
    req = urllib.request.Request(api + "/fetch", json.dumps(body).encode(), _auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            res = json.load(r)
    except Exception as ex:
        text = _error_text(ex)
        log.warning("통로 호출 실패: %s", text)
        raise error_cls(text, **kw) from ex
    if not res.get("js"):
        text = "통로가 결과를 주지 않았습니다: " + str(res)[:ERROR_TEXT_LIMIT]
        log.warning(text)
        raise error_cls(text, **kw)
    last_marks[:] = res.get("marks") or []
    last_info.clear()
    last_info.update({k: res.get(k) for k in ("mode", "retry", "timing", "v")})
    if res.get("retry"):
        log.warning("통로가 한 번 다시 했습니다: %s", res["retry"])
    return res["js"]


def _post(api, actions, query, smart=None):
    body = {
        "url": LIST_URL, "mode": "stealth", "real_chrome": True, "solve_cloudflare": True,
        "profile": PROFILE, "timeout": 120, "actions": actions, "js": READ_JS,
        "hold": {"query": query, "resume_actions": RESUME_ACTIONS},
    }
    if smart:
        body["smart"] = smart   # 새 통로는 화면 값을 읽어 다른 칸만 고친다. 옛 통로는 모르는 칸이라 무시하고 actions 를 따른다
    return _send(api, body, HumanFetchError)


def _run_local(actions, query, smart=None):
    _ensure_local_server()
    return _post(LOCAL_API, actions, query, smart)


def _run_remote(actions, query, smart=None):
    return _post(REMOTE_API, actions, query, smart)


def fetch_list(dep, arr, date, hour):
    """열차 목록 화면을 읽어 {'when': 화면에 뜬 일시, 'rows': [줄 HTML...]} 로 돌려준다. 결과 HTML 은 OUT_DIR 에 남긴다."""
    with _lock:
        # 사람 같은 조회 간격: 마지막 조회로부터 pacing 규칙의 간격이 안 지났으면 남은 만큼 쉰다
        want = _pacer.next_delay()
        if _last[0] is not None:
            wait = want - (_clock() - _last[0])
            if wait > 0:
                _sleep(wait)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        actions = build_actions(dep, arr, date, hour)
        try:
            use_local = os.name == "nt" and os.path.exists(HUMAN_FETCH_PS1)
            smart = {"kind": "korail_list", "dep": dep, "arr": arr, "date": date, "hour": hour}
            raw = (_run_local if use_local else _run_remote)(actions, "%s|%s|%s|%d" % (dep, arr, date, hour), smart=smart)
        finally:
            _last[0] = _clock()
    try:
        data = json.loads(raw)
    except ValueError as ex:
        raise HumanFetchError("읽은 결과가 JSON 이 아닙니다: " + raw[:120]) from ex
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / ("search_%s.html" % stamp)).write_text("\n".join(data.get("rows", [])), encoding="utf-8")
    except OSError:
        pass
    return data


class _Row(HTMLParser):
    """열차 한 줄(li.tckList) 안에서 필요한 값만 모은다."""

    def __init__(self):
        super().__init__()
        self.stack = []        # (tag, 클래스 집합)
        self.type_name = ""
        self.num = ""
        self.h3 = []
        self.boxes = []        # 요금 칸 하나당 {'cls': 집합, 'price': bool}
        self._cur = None

    def handle_starttag(self, tag, attrs):
        cls = set((dict(attrs).get("class") or "").split())
        self.stack.append((tag, cls))
        if tag == "div" and "price_box" in cls:
            self._cur = {"cls": cls, "price": False}
            self.boxes.append(self._cur)
        if tag == "p" and "txt_price" in cls and self._cur is not None:
            self._cur["price"] = True

    def handle_endtag(self, tag):
        while self.stack:
            t, cls = self.stack.pop()
            if t == tag:
                if t == "div" and "price_box" in cls:
                    self._cur = None
                break

    def handle_data(self, data):
        text = data.strip()
        if not text or not self.stack:
            return
        tag, cls = self.stack[-1]
        if tag == "span" and "blind" in cls and not self.type_name:
            self.type_name = text
        elif tag == "span" and "num" in cls:
            self.num = text
        elif tag == "span" and any(t == "h3" for t, _ in self.stack):
            self.h3.append(text)


def _seat_code(box):
    if box is None or not box["price"]:
        return "00"
    return "00" if "sold_out" in box["cls"] else "11"   # sold_out_soon(매진임박)은 아직 자리가 있다


def parse_row(html, date):
    """열차 줄 HTML 하나를 Train 이 받는 dict 로 바꾼다. 못 읽으면 None."""
    p = _Row()
    p.feed(html)
    if len(p.h3) < 3 or not p.num:
        return None
    dep, arr, span = p.h3[0], p.h3[1], p.h3[2].strip("()")
    try:
        dep_t, arr_t = [x.strip().replace(":", "") + "00" for x in span.split("~")]
    except ValueError:
        return None
    gen = next((b for b in p.boxes if "gen" in b["cls"]), None)
    spe = next((b for b in p.boxes if "spe" in b["cls"]), None)
    gen_code, spe_code = _seat_code(gen), _seat_code(spe)
    ok = "11" in (gen_code, spe_code)
    arr_date = date
    if arr_t < dep_t:
        arr_date = datetime.fromordinal(datetime.strptime(date, "%Y%m%d").toordinal() + 1).strftime("%Y%m%d")
    return {
        "h_trn_clsf_nm": p.type_name,
        "h_trn_no": p.num,
        "h_dpt_rs_stn_nm": dep, "h_arv_rs_stn_nm": arr,
        "h_dpt_dt": date, "h_dpt_tm": dep_t,
        "h_arv_dt": arr_date, "h_arv_tm": arr_t,
        "h_run_dt": date,
        "h_rsv_psb_flg": "Y" if ok else "N",
        "h_rsv_psb_nm": "예약가능" if ok else "매진",
        "h_gen_rsv_cd": gen_code, "h_spe_rsv_cd": spe_code,
        "h_wait_rsv_flg": "-2",   # 웹 목록에는 예약대기 정보가 없다
    }


def parse_rows(rows, date):
    return [d for d in (parse_row(r, date) for r in rows) if d]


# --- 로그인·예약(결제 전 단계까지) -----------------------------------------------
LOGIN_URL = "https://www.korail.com/ticket/login"
PAY_LIMIT_FALLBACK = "약 10분, 확인 못 함"
LOGIN_ID_MARK = OUT_DIR / "login_id.sha"   # 로그인된 계정의 해시(아이디 원문은 남기지 않는다)

# 로그인 여부와 안내 창 글귀만 읽는다(값 입력 없음)
STATE_JS = r"""(async () => {
  const t = s => (document.querySelector(s) || {}).innerText || '';
  const modal = [...document.querySelectorAll('.ReactModal__Content, [role=dialog]')].map(e => e.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean).join(' | ');
  return JSON.stringify({url: location.href, loggedOut: !!document.querySelector('a.btnGoLogin, .logoutBtn'),
    modal: modal.slice(0, 400), body: document.body.innerText.replace(/\s+/g, ' ').slice(0, 2500)});
})()
"""


class HumanReserveError(Exception):
    """definite 가 True 면 예약이 잡히지 않았다고 확신하는 실패(다시 시도해도 중복 예매가 안 된다)."""

    def __init__(self, msg, definite=True):
        super().__init__(msg)
        self.definite = definite


class WebReservation:
    """웹으로 잡은 예약. 결제는 사람이 한다."""
    is_waiting = False

    def __init__(self, train, limit_text, page_text=""):
        self.train = train
        self.limit_text = limit_text
        self.page_text = page_text


def _id_hash(user_id):
    import hashlib
    return hashlib.sha256(("srtgo-korail:" + user_id).encode()).hexdigest()[:16]


def _state(actions=None, url=LOGIN_URL, query=None, smart=None):
    raw = _post_state(url, actions or [{"wait": 300}], query or "state|%f" % _clock(), smart=smart)
    try:
        return json.loads(raw)
    except ValueError as ex:
        raise HumanReserveError("읽은 결과가 JSON 이 아닙니다", definite=False) from ex


def _post_state(url, actions, query, js=STATE_JS, smart=None):
    # 상시 브라우저가 이 PC 에 있으면 이 PC 로, 없으면 레노버 통로로 보낸다(조회와 같다).
    # 레노버로는 사설망(Tailscale)만 지나고, 아이디·비밀번호는 측정 기록에 남지 않게 가려진다.
    # retry 거짓: 통로가 오류 뒤 처음부터 다시 하지 않는다(예매 버튼을 두 번 누르는 일을 막는다)
    use_local = os.name == "nt" and os.path.exists(HUMAN_FETCH_PS1)
    api = LOCAL_API if use_local else REMOTE_API
    if use_local:
        try:
            _ensure_local_server()
        except HumanFetchError as ex:
            raise HumanReserveError(str(ex), definite=True) from ex   # 통로가 없으면 아무것도 누르지 않았다
    body = {"url": url, "mode": "stealth", "real_chrome": True, "solve_cloudflare": True, "profile": PROFILE,
            "timeout": 120, "actions": actions, "js": js, "hold": {"query": query, "resume_actions": None, "retry": False}}
    if smart:
        body["smart"] = smart
    return _send(api, body, HumanReserveError, definite=False)


def login_tab(user_id):
    """코레일 웹 로그인 화면의 탭: 회원번호 / 이메일 / 휴대폰."""
    if "@" in user_id:
        return "email"
    digits = user_id.replace("-", "")
    if digits.startswith("01") and len(digits) in (10, 11):
        return "phone"
    return "memberNo"


def build_login_actions(user_id, password):
    tab = login_tab(user_id)
    return [
        {"click": "button#%s" % tab}, {"wait": 500},
        {"type": "input#id", "text": user_id.replace("-", "") if tab == "phone" else user_id},
        {"type": "input#password", "text": password}, {"wait": 500},
        {"click": ".btn_bn-depblue"}, {"wait": 3500},
    ]


def is_logged_in():
    """상시 브라우저에 로그인 상태(쿠키)가 남아 있는지만 본다. 입력은 하지 않는다."""
    st = _state(url="https://www.korail.com/ticket/search/general")
    return not st.get("loggedOut", True)


def login(chat_id, rail_type="KTX", creds=None):
    """keyring 의 아이디·비밀번호로 웹 로그인한다. 이미 로그인돼 있으면 다시 입력하지 않는다.
    돌려주는 값: (성공 여부, 실패 사유 또는 None). 사유는 CREDENTIAL_HINTS 로 계정 문제인지 가린다."""
    if creds is None:
        import keyring
        from .bot import CRED_SERVICE
        creds = (keyring.get_password(CRED_SERVICE, "%s:%s:id" % (chat_id, rail_type)),
                 keyring.get_password(CRED_SERVICE, "%s:%s:pw" % (chat_id, rail_type)))
    return login_with(*creds)


def login_with(user_id, password):
    if not (user_id and password):
        return False, "계정이 연결되어 있지 않습니다(아이디 없음)"
    want = _id_hash(user_id)
    with _lock:
        try:
            if LOGIN_ID_MARK.exists() and LOGIN_ID_MARK.read_text().strip() == want and is_logged_in():
                return True, None
        except OSError:
            pass
        st = _state(build_login_actions(user_id, password), url=LOGIN_URL, query="login|%f" % _clock())
        if not st.get("loggedOut", True) and "/login" not in st.get("url", ""):
            try:
                OUT_DIR.mkdir(parents=True, exist_ok=True)
                LOGIN_ID_MARK.write_text(want)
            except OSError:
                pass
            return True, None
        why = st.get("modal") or ""
        if not why:
            why = "로그인 화면에서 벗어나지 못했습니다(사유 미상)"
        return False, why[:200]


def _train_selector(train, special):
    box = "spe" if special else "gen"
    return 'li.tckList:has(span.num:text-is("%s")) .price_box.%s:has(.txt_price) a >> visible=true >> nth=0' % (
        str(train.train_no).strip(), box)


def build_reserve_actions(train, special, date, hour, today=None):
    """검색 화면을 사람처럼 다시 열고 그 열차의 요금 칸을 눌러 '예매'까지 누른다. 결제 버튼은 누르지 않는다."""
    steps = build_actions(train.dep_name, train.arr_name, date, hour, today)
    steps += [
        {"wait": 600},
        {"click": _train_selector(train, special)}, {"wait": 800},
        {"click_if": ".btn_pop-close >> visible=true", "timeout": 2500},   # 정차역 안내 창(나올 때만)
        {"click": "button.reservbtn >> visible=true"}, {"wait": 3500},   # 아래쪽 파란 '예매' 버튼(맨 위 경로 메뉴의 '예매'와 다름)
        {"click_if": ".btn_pop-close >> visible=true", "timeout": 2500},
        {"wait": 1500},
    ]
    return steps


def parse_limit(text):
    """결제 제한시간 글귀를 찾는다. 없으면 None. 예: '결제 제한시간 2026-10-29 17:30' 또는 '10분 이내'."""
    import re
    m = re.search(r"(?:결제|구입|구매)[^.]{0,20}?(?:기한|제한|시한)[^0-9]{0,12}(\d{4}[-./]\d{2}[-./]\d{2}[^가-힣]{0,6}\d{1,2}:\d{2}|\d{1,2}:\d{2}|\d+\s*분)", text)
    return m.group(1).strip() if m else None


def classify_reserve_page(st):
    """예매 버튼 뒤 화면을 판정한다: 'login' 로그인 필요 / 'ok' 예약됨 / 'fail' 거절 / 'unknown'."""
    url, body, modal = st.get("url", ""), st.get("body", ""), st.get("modal", "")
    if "/login" in url:
        return "login", modal
    # 로그인 뒤 예매 화면은 직접 못 봤다(실계정 없이 시험). 목록·로그인 화면을 벗어나 '결제'가 보이면 잡힌 것으로 본다.
    left_list = "/search/" not in url
    if any(k in url for k in ("/reservation", "/payment", "/mypage/cart", "/ticket/pay")) or (left_list and "결제" in body):
        return "ok", body
    if modal:
        return "fail", modal
    if "/search/list" in url:
        return "fail", "예매 버튼 뒤에도 열차 목록 화면에 머물렀습니다"
    return "unknown", body


def reserve(train, special=False, chat_id=None, rail_type="KTX", creds=None):
    """선택한 열차를 웹에서 예약한다(결제 전 단계에서 멈춘다). 결제는 사람이 한다.
    WebReservation 을 돌려주거나 HumanReserveError(definite)를 던진다."""
    with _lock:
        if not (creds and creds[0]) and not chat_id:
            raise HumanReserveError("계정 정보가 없습니다", definite=True)
        if not is_logged_in():
            ok, why = login(chat_id, rail_type, creds=creds) if creds is None else login_with(*creds)
            if not ok:
                raise HumanReserveError("로그인 필요: " + str(why), definite=True)
        date, hour = train.dep_date, int(train.dep_time[:2])
        actions = build_reserve_actions(train, special, date, hour)
        smart = {"kind": "korail_list", "dep": train.dep_name, "arr": train.arr_name, "date": date, "hour": hour,
                 "after": build_reserve_actions(train, special, date, hour)[len(build_actions(train.dep_name, train.arr_name, date, hour)):]}
        st = _state(actions, url=LIST_URL, query="reserve|%s|%f" % (train.train_no, _clock()), smart=smart)
        kind, text = classify_reserve_page(st)
        try:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUT_DIR / ("reserve_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S"))).write_text(
                "%s\n%s\n%s" % (st.get("url"), kind, text[:2500]), encoding="utf-8")
        except OSError:
            pass
        if kind == "ok":
            return WebReservation(train, parse_limit(text) or PAY_LIMIT_FALLBACK, text)
        if kind == "unknown":
            raise HumanReserveError("예약이 잡혔는지 확인하지 못했습니다: " + st.get("url", ""), definite=False)
        if kind == "login":
            try:
                LOGIN_ID_MARK.unlink()   # 로그인이 풀렸다. 다음에는 다시 입력해 로그인한다
            except OSError:
                pass
            raise HumanReserveError("로그인이 풀려 예약하지 못했습니다(다음 시도에서 다시 로그인)", definite=True)
        raise HumanReserveError("예약 거절: " + text[:200], definite=True)


def search_train(dep, arr, date=None, time=None, train_type="109", passengers=None,
                 include_no_seats=False, include_waiting_list=False):
    """Korail.search_train 과 같은 값을 돌려준다(Train 목록). 로그인 없이 되고, 화면은 어른 1명 기준이라 passengers 는 쓰지 않는다."""
    from .ktx import NoResultsError, Train, TrainType

    now = datetime.now()
    date = date or now.strftime("%Y%m%d")
    time_ = time or now.strftime("%H%M%S")
    data = fetch_list(dep, arr, date, int(time_[:2]))
    if not data.get("rows"):
        raise NoResultsError()
    trains = [Train(d) for d in parse_rows(data["rows"], date)]
    trains = [t for t in trains if t.dep_time >= time_[:4] + "00"]
    if train_type == TrainType.KTX:
        trains = [t for t in trains if (t.train_type_name or "").startswith("KTX")]
    if not include_no_seats:
        trains = [t for t in trains if t.has_seat()]
    if not trains:
        raise NoResultsError()
    return trains
