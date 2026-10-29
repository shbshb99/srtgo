"""코레일 웹(www.korail.com) 열차 조회를 사람처럼 입력하는 통로로 보낸다. 표준 라이브러리만 쓴다.

앱 주소(smart.letskorail.com)는 -8204 로 막히므로, 열차 목록 조회(search_train)만 이 길로 돌린다.
레노버에서는 ops/human_fetch.ps1 을 직접 부르고, 다른 PC 에서는 레노버의 스크래플링 통로(HTTP)에 같은
동작 목록과 읽기 스크립트를 보낸다. 진짜 크롬 창에서 진짜 마우스·키보드로 역·날짜·시각을 넣고 조회를 눌러
나온 화면의 열차 줄(HTML)을 읽어 Train 객체로 바꾼다. 로그인은 필요 없다. 예매·결제는 건드리지 않는다.
"""
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from .pacing import HumanPacer

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
    steps = [
        {"click": "input[name=txtGoStart]"}, {"wait": 400},
        {"click": 'a:text-is("%s") >> visible=true' % dep}, {"wait": 400},
        {"click": "input[name=txtGoEnd]"}, {"wait": 400},
        {"click": 'a:text-is("%s") >> visible=true' % arr}, {"wait": 400},
        {"click": "input[name=startDate]"}, {"wait": 400},
    ]
    for _ in range(months_ahead):
        steps += [{"click": ".slick-next >> visible=true >> nth=0"}, {"wait": 400}]
    steps += [
        {"click": 'td:not(.disabled) a:has(span.day:text-is("%d")) >> visible=true' % d}, {"wait": 300},
    ]
    # 시각 줄은 5시간씩 움직이고, 처음 위치는 열 때의 시각(지금 시각 근처)이라 먼저 맨 앞(0~4시)으로 보낸다
    # (2026-10-29 확인: 15~19시에서 이전 3번이면 0~4시, 다음 1번이면 5~9시, 끝은 19~23시에서 멈춤)
    for _ in range(4):
        steps += [{"click": ".slide_list .slick-prev >> visible=true"}, {"wait": 400}]
    for _ in range(hour // 5):
        steps += [{"click": ".slide_list .slick-next >> visible=true"}, {"wait": 400}]
    steps += [
        {"click": '.slide_list .slick-active a:text-is("%02d시")' % hour}, {"wait": 300},
        {"click": 'text="적용" >> visible=true'}, {"wait": 400},
        {"click": ".btn_lookup"}, {"wait_for": "li.tckList", "timeout": 20000},
    ]
    return steps


LOCAL_API = "http://127.0.0.1:8765"
SCRAPE_PY = os.environ.get("SCRAPE_PY", r"C:\apps\scrapling_venv\Scripts\python.exe")
SCRAPE_API_PY = os.path.join(os.path.dirname(HUMAN_FETCH_PS1), "scrape_api.py")
RESUME_ACTIONS = [{"press": "F5"}, {"wait_for": "li.tckList", "timeout": 20000}]
last_marks = []   # 마지막 조회의 단계별 걸린 시간(초), 측정용


def _health(api):
    try:
        with urllib.request.urlopen(api + "/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _ensure_local_server():
    """이 PC 의 스크래플링 통로(상시 브라우저를 들고 있는 프로세스)가 없으면 띄운다."""
    if _health(LOCAL_API):
        return
    if not (os.path.exists(SCRAPE_PY) and os.path.exists(SCRAPE_API_PY)):
        raise HumanFetchError("화면이 있는 계정에서 실행해야 합니다: 이 PC 에 스크래플링 통로가 없습니다")
    if os.environ.get("SESSIONNAME", "Console") == "" or os.environ.get("USERNAME", "").endswith("$"):
        raise HumanFetchError("화면이 있는 계정에서 실행해야 합니다")
    flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([SCRAPE_PY, "-I", SCRAPE_API_PY, "serve"], creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if _health(LOCAL_API):
            return
        _sleep(1)
    raise HumanFetchError("스크래플링 통로가 30초 안에 켜지지 않았습니다(화면이 있는 계정에서 실행해야 합니다)")


def _post(api, actions, query):
    body = {
        "url": LIST_URL, "mode": "stealth", "real_chrome": True, "solve_cloudflare": True,
        "profile": PROFILE, "timeout": 120, "actions": actions, "js": READ_JS,
        "hold": {"query": query, "resume_actions": RESUME_ACTIONS},
    }
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SCRAPE_API_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(api + "/fetch", json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            res = json.load(r)
    except Exception as ex:
        raise HumanFetchError("%s: %s" % (type(ex).__name__, str(ex)[:200])) from ex
    if not res.get("js"):
        raise HumanFetchError("통로가 결과를 주지 않았습니다: " + str(res)[:200])
    last_marks[:] = res.get("marks") or []
    return res["js"]


def _run_local(actions, query):
    _ensure_local_server()
    return _post(LOCAL_API, actions, query)


def _run_remote(actions, query):
    return _post(REMOTE_API, actions, query)

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
            raw = (_run_local if use_local else _run_remote)(actions, "%s|%s|%s|%d" % (dep, arr, date, hour))
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
