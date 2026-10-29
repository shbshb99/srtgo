"""화면 세션에서 도는 스크래핑 통로(상시 브라우저). srtgo 패키지 안에 있어 봇 업데이트 때 함께 바뀐다.

실행(스크래플링 가상환경 파이썬으로, 화면이 있는 로그온 세션에서):
  C:\\apps\\scrapling_venv\\Scripts\\python.exe -I <srtgo 패키지 폴더>\\scrape_server.py serve [--port 8765]
봇(human_client)은 이 통로를 직접 띄우지 않는다. 예약작업 ShbScrapeApi 가 켜 둔다(setup\\prod2_scrape_server_pkg.ps1).
이 파일은 단독으로 돌아야 하므로 srtgo 의 다른 모듈을 가져오지 않는다.

요청: POST /fetch  {"url", "profile", "timeout", "actions", "js", "hold": {"query", "resume_actions", "retry"}, "smart": {...}}
  smart(코레일 열차 조회 조건)가 있으면 화면에 이미 들어 있는 값을 읽어 다른 칸만 고친다.
응답: {"js", "marks"(단계별 누적 초), "timing"(동작별 시작 시각), "mode", "retry", "v"}
"""
import argparse
import json
import os
import random
import re
import sys
import threading
import time
import traceback

SERVER_VERSION = 4   # 올리면 봇이 옛 통로를 /quit 로 끄고 예약작업이 새로 켠다(human_client.WANT_SERVER_VERSION 과 같이)
PROFILE_ROOT = os.environ.get("SCRAPE_PROFILE_ROOT", r"C:\shb-run\browser\scrape_profiles")
LIST_HOST_PATH = "korail.com/ticket/search"
LOOKUP_BTN = ".btn_lookup"
RESUME_ACTIONS = [{"press": "F5"}, {"wait_for": "li.tckList", "timeout": 20000}]
REST_MID = [40, 160]       # 클릭 뒤 쉼(밀리초). 사람은 누른 뒤 곧바로 다음 목표로 가므로 짧게. 화면이 바뀌길 기다리는 일은 쉼이 아니라 아래 기다림 함수가 한다
PRE_CLICK = (30, 110)      # 마우스가 목표에 닿은 뒤 누르기 전(밀리초)
HOLD_MS = (40, 100)        # 눌렀다 떼기(밀리초)
LOOK_REST = (200, 400)     # 시작할 때 둘러본 뒤 쉼(밀리초)
LOG_PATH = os.path.join(os.environ.get("SRTGO_HOME") or os.path.join(os.path.expanduser("~"), ".srtgo"), "scrape_server.log")

# 조회 전 화면(/ticket/search/general)에는 txtGoStart·txtGoEnd 입력칸이, 조회 뒤 목록 화면(/ticket/search/list)에는
# #labelstart·#labelend 가 있다(2026-10-29 직접 확인). 날짜 칸(startDate)은 둘 다 "2026-10-30(금) 09:00" 꼴로 시각까지 든다.
STATE_JS = r"""(() => {
  const val = (...sels) => { for (const s of sels) { const e = document.querySelector(s); if (e) return e.value; } return null; };
  return {start: val('#labelstart', 'input[name=txtGoStart]'), end: val('#labelend', 'input[name=txtGoEnd]'),
          date: val('input[name=startDate]'), rows: document.querySelectorAll('li.tckList').length,
          list: location.href.includes('/ticket/search/list'), url: location.href};
})()"""
ROWS_SIG_JS = r"""(() => [...document.querySelectorAll('li.tckList')].map(e => e.innerText.replace(/\s+/g, ' ')).join('|'))()"""
CAL_MONTH_JS = r"""(() => {
  const s = [...document.querySelectorAll('.slick-slide.slick-active')].find(e => e.querySelector('.datepicker'));
  const m = s && s.innerText.match(/(\d{4})\.\s*(\d{1,2})\./);
  return m ? m[1] + '-' + ('0' + m[2]).slice(-2) : null;
})()"""
CAL_HOURS_JS = r"""(() => {
  const sl = [...document.querySelectorAll('.slide_list .slick-slide')];
  const act = sl.map((e, i) => e.classList.contains('slick-active') ? i : -1).filter(i => i >= 0);
  return act.length ? [act[0], act[act.length - 1], sl.length] : null;
})()"""


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, file=sys.stderr, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def profile_dir(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise ValueError("profile 이름은 영문·숫자·_- 만")
    return os.path.join(PROFILE_ROOT, name)


# --- 화면 값 비교로 바뀐 칸만 고치는 계획(브라우저 없이 시험할 수 있는 순수 함수) -----------------
def plan_korail(state, cond):
    """화면의 현재 값(state)과 원하는 조건(cond: dep, arr, date=YYYYMMDD, hour)을 비교해 할 일을 정한다.
    돌려주는 값: {"mode": "full"|"refresh"|"lookup"|"edit", "actions": [...], "changed": [...]}
      full    화면 값을 못 읽었다 → 전부 다시 입력(호출한 쪽이 가진 전체 동작 목록)
      refresh 값이 모두 같고 목록이 떠 있다 → F5 만
      lookup  값이 모두 같고 조회 전 화면이다 → 조회 버튼만
      edit    다른 칸만 고친다. 목록 화면에서는 역·날짜·시각을 바꾸면 저절로 다시 조회되고(직접 확인),
              조회 전 화면에서는 마지막에 조회 버튼을 누른다.
    목록 화면의 .btn_lookup 은 '다음날 조회' 라서 목록 화면에서는 절대 누르지 않는다."""
    if not state or state.get("start") is None or state.get("end") is None or state.get("date") is None:
        return {"mode": "full", "actions": [], "changed": []}
    on_list = bool(state.get("list"))
    want_day = "%s-%s-%s" % (cond["date"][:4], cond["date"][4:6], cond["date"][6:8])
    want_hh = "%02d" % int(cond["hour"])
    cur = state["date"]
    same_day = cur[:10] == want_day
    same_hour = len(cur) >= 5 and cur[-5:-3] == want_hh
    swap = (state["start"] == cond["arr"] and state["end"] == cond["dep"] and cond["dep"] != cond["arr"])
    changed = []
    if state["start"] != cond["dep"]:
        changed.append("dep")
    if state["end"] != cond["arr"]:
        changed.append("arr")
    if not same_day:
        changed.append("day")
    if not same_hour:
        changed.append("hour")
    if not changed:
        if on_list:
            return {"mode": "refresh", "actions": [{"reload": True}, {"wait_for": "li.tckList", "timeout": 20000}], "changed": []}
        return {"mode": "lookup", "actions": lookup_actions(), "changed": []}
    r = REST_MID
    acts = []
    if on_list:
        acts.append({"mark_rows": True})
    start_btn = ".btn_pop-openStationPop >> nth=0" if on_list else "input[name=txtGoStart]"
    end_btn = ".btn_pop-openStationPop >> nth=1" if on_list else "input[name=txtGoEnd]"
    if swap and "dep" in changed and "arr" in changed:
        acts.append({"click": ".btnChange >> visible=true", "rest": r})
    else:
        if "dep" in changed:
            acts += [{"click": start_btn, "rest": r}, {"click": 'a:text-is("%s") >> visible=true' % cond["dep"], "rest": r}]
        if "arr" in changed:
            acts += [{"click": end_btn, "rest": r}, {"click": 'a:text-is("%s") >> visible=true' % cond["arr"], "rest": r}]
    if "day" in changed or "hour" in changed:
        acts += [{"click": "input[name=startDate]", "rest": r}]
        if "day" in changed:
            acts += [{"cal_month": want_day[:7]},
                     {"click": 'td:not(.disabled) a:has(span.day:text-is("%d")) >> visible=true' % int(cond["date"][6:8]),
                      "rest": r}]
        acts += [{"cal_hour": int(cond["hour"])}, {"click": 'text="적용" >> visible=true', "rest": r}]
    if on_list:
        acts.append({"settle": 3500})
    else:
        acts += lookup_actions()
    return {"mode": "edit", "actions": acts, "changed": changed}


def lookup_actions():
    return [{"click": LOOKUP_BTN}, {"wait_for": "li.tckList", "timeout": 20000}]


def hour_window_clicks(first, last, total, hour):
    """시각 줄(5칸씩 보임, 한 번에 5칸 이동, 양 끝에서 멈춤)에서 hour 가 보이려면 누를 방향 목록."""
    clicks = []
    width = last - first + 1
    for _ in range(8):
        if first <= hour <= last:
            return clicks
        if hour > last:
            clicks.append("next")
            first = min(first + width, total - width)
        else:
            clicks.append("prev")
            first = max(first - width, 0)
        last = first + width - 1
    return clicks


# --- 사람처럼 입력 ------------------------------------------------------------------------
def human_actions(p, steps, look=True, timing=None, t0=None):
    """진짜 마우스·키보드로 입력한다(페이지 안 스크립트 입력은 isTrusted=false 로 들킨다).
    steps 의 click 은 "rest": [최소, 최대] 밀리초로 누른 뒤 쉼을 바꿀 수 있다(기본 REST_MID).
    cal_month("YYYY-MM")·cal_hour(시)는 달력·시각 줄을 읽으며 필요한 만큼만 넘긴다.
    클릭 사이에는 마우스 이동 시간만 걸리게 한다. 화면이 바뀌길 기다려야 할 때는 고정 쉼 대신 바뀐 것이 보일 때까지만 기다린다."""
    vw = (p.viewport_size or {}).get("width", 1280)
    vh = (p.viewport_size or {}).get("height", 800)
    pos = [random.uniform(vw * 0.3, vw * 0.7), random.uniform(vh * 0.2, vh * 0.5)]

    def move(x, y):
        mx = (pos[0] + x) / 2 + random.uniform(-80, 80)
        my = (pos[1] + y) / 2 + random.uniform(-60, 60)
        p.mouse.move(mx, my, steps=random.randint(8, 18))
        p.mouse.move(x, y, steps=random.randint(8, 18))
        pos[0], pos[1] = x, y

    def box(sel):
        el = p.locator(sel).first
        el.scroll_into_view_if_needed()
        return el.bounding_box()

    def press(sel, rest=None, left=False):
        b = box(sel)
        fx, fy = random.uniform(0.3, 0.7), random.uniform(0.35, 0.65)

        def point(b):
            return b["x"] + (random.uniform(6, 14) if left else b["width"] * fx), b["y"] + b["height"] * fy

        x, y = point(b)
        move(x, y)
        p.wait_for_timeout(random.randint(*PRE_CLICK))
        # 누르기 직전에 칸이 움직였으면(팝업이 열리는 중) 멎을 때까지만 기다렸다가 다시 겨눈다
        for _ in range(12):
            b2 = box(sel)
            if all(abs(b2[k] - b[k]) < 1.5 for k in ("x", "y", "width", "height")):
                break
            b = b2
            x, y = point(b)
            move(x, y)
            p.wait_for_timeout(40)
        p.mouse.down()
        p.wait_for_timeout(random.randint(*HOLD_MS))
        p.mouse.up()
        return rest

    def rest_after(rest):
        lo, hi = rest or REST_MID
        p.wait_for_timeout(random.randint(int(lo), int(hi)))

    def visible_within(sel, timeout_ms=3000):
        """그 칸이 화면에 보일 때까지만 기다린다. 보이면 참, 시간이 지나도 안 보이면 거짓."""
        try:
            p.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            return False

    def wait_value(js, old, timeout_ms):
        """js 값이 비어 있지 않고 old 와 달라진 뒤 두 번 연속 같을 때(움직임이 멎음)까지만 기다려 그 값을 돌려준다(시간이 지나면 마지막 값).
        열리거나 넘어가는 중의 값을 읽고 엉뚱한 곳을 누르지 않게 하려고 바뀐 직후가 아니라 멎은 값을 쓴다."""
        end = time.monotonic() + timeout_ms / 1000.0
        last = None
        while True:
            v = p.evaluate(js)
            if v is not None and v != old and v == last:
                return v
            if time.monotonic() >= end:
                return v
            last = v
            p.wait_for_timeout(40)

    def typed(text):
        for ch in str(text):
            p.keyboard.type(ch)
            p.wait_for_timeout(random.randint(70, 220))

    t0 = t0 if t0 is not None else time.monotonic()
    rows_sig = [None]
    p.mouse.move(pos[0], pos[1])
    if look:
        move(random.uniform(100, vw - 100), random.uniform(100, vh - 100))
        p.mouse.wheel(0, random.randint(80, 240))
        p.wait_for_timeout(random.randint(*LOOK_REST))

    for s in steps:
        if timing is not None:
            shown = {k: ("***" if k in ("text", "keys", "value") else v) for k, v in s.items()}
            timing.append((round(time.monotonic() - t0, 1), json.dumps(shown, ensure_ascii=False)[:70]))
        if "click_if" in s:
            try:
                p.locator(s["click_if"]).first.wait_for(state="visible", timeout=int(s.get("timeout", 2500)))
            except Exception:
                continue
            press(s["click_if"])
            rest_after(s.get("rest"))
        elif "wait_for" in s:
            p.locator(s["wait_for"]).first.wait_for(state="visible", timeout=int(s.get("timeout", 15000)))
            p.wait_for_timeout(random.randint(300, 700))
        elif "wait" in s:
            p.wait_for_timeout(int(s["wait"]))
        elif "reload" in s:
            # 진짜 F5 키는 브라우저가 아니라 페이지로만 전달돼 새로고침이 되지 않는다(2026-10-29 직접 확인: 15초 기다려도 문서가 안 바뀜).
            # 그래서 화면을 직접 다시 불러온다. 옛 문서의 줄을 읽지 않도록 옛 문서에 표시를 달고, 표시가 사라질 때(새 문서)까지 기다린다
            p.evaluate("window.__srtgoStale = 1")
            p.reload(wait_until="domcontentloaded")
            end = time.monotonic() + 15
            while time.monotonic() < end:
                try:
                    if not p.evaluate("window.__srtgoStale"):
                        break
                except Exception:
                    pass   # 문서가 바뀌는 중
                p.wait_for_timeout(100)
        elif "mark_rows" in s:
            rows_sig[0] = p.evaluate(ROWS_SIG_JS)
        elif "settle" in s:
            # 목록 화면은 값을 바꾸면 저절로 다시 조회된다. 열차 줄이 바뀔 때까지(최대 settle 밀리초) 기다린다. 줄이 같으면 그대로 읽는다
            end = time.monotonic() + int(s["settle"]) / 1000.0
            while time.monotonic() < end and p.evaluate(ROWS_SIG_JS) == rows_sig[0]:
                p.wait_for_timeout(150)
            p.wait_for_timeout(random.randint(100, 250))
        elif "cal_month" in s:
            want = s["cal_month"]
            visible_within(".slick-next >> visible=true >> nth=0")   # 달력이 열릴 때까지만(닫힌 채 숨어 있던 옛 값을 읽지 않는다)
            cur = wait_value(CAL_MONTH_JS, None, 3000)
            for _ in range(14):
                if cur is None:
                    raise RuntimeError("달력의 월을 읽지 못했습니다")
                if cur == want:
                    break
                press(".slick-next >> visible=true >> nth=0" if cur < want else ".slick-prev >> visible=true >> nth=0")
                cur = wait_value(CAL_MONTH_JS, cur, 1500)   # 넘어간 달이 보일 때까지만
            else:
                raise RuntimeError("달력을 %s 로 넘기지 못했습니다" % want)
        elif "cal_hour" in s:
            hour = int(s["cal_hour"])
            want_sel = '.slide_list .slick-active a:text-is("%02d시")' % hour
            visible_within(".slide_list .slick-slide >> visible=true")   # 시각 줄이 열릴 때까지만
            for _ in range(3):
                win = wait_value(CAL_HOURS_JS, None, 3000)
                if win is None:
                    raise RuntimeError("시각 줄을 읽지 못했습니다")
                for d in hour_window_clicks(win[0], win[1], win[2], hour):
                    press(".slide_list .slick-%s >> visible=true" % d)
                    win = wait_value(CAL_HOURS_JS, win, 1500)   # 칸이 넘어가 활성 칸이 바뀔 때까지만
                if visible_within(want_sel, 2500):
                    break        # 원하는 시각이 보인다
            press(want_sel)
            rest_after(s.get("rest"))
        elif "press" in s:
            p.wait_for_timeout(random.randint(*PRE_CLICK))
            p.keyboard.press(s["press"])
        elif "keys" in s:
            typed(s["keys"])
        elif "scroll" in s:
            p.mouse.wheel(0, int(s["scroll"]))
            p.wait_for_timeout(random.randint(100, 250))
        elif "click" in s or "type" in s:
            press(s.get("click") or s["type"], left="type" in s)
            if "type" in s:
                p.wait_for_timeout(random.randint(300, 700))
                typed(s["text"])
            rest_after(s.get("rest"))


# --- 상시 브라우저 -------------------------------------------------------------------------
class HoldBrowser:
    """크롬 창을 한 번 띄워 두고 조회마다 새로 켜지 않는다. 플레이라이트 동기 방식은 만든 스레드에서만 쓸 수 있어
    전용 스레드 하나가 창을 갖고 일을 순서대로 처리한다. 창이 죽거나 막히면 닫고 새로 띄워 한 번만 다시 한다(retry 가 거짓이면 다시 하지 않는다)."""

    def __init__(self):
        self.jobs = None
        self.sess = None
        self.page = None
        self.profile = None
        self.query = None
        self.lock = threading.Lock()

    def _close(self):
        try:
            if self.sess:
                self.sess.close()
        except Exception:
            pass
        self.sess = self.page = self.query = None

    def _open(self, profile, timeout):
        from scrapling.fetchers import StealthySession
        os.makedirs(profile_dir(profile), exist_ok=True)
        self.sess = StealthySession(headless=False, real_chrome=True, user_data_dir=profile_dir(profile),
                                    solve_cloudflare=True, network_idle=True, timeout=int(timeout) * 1000)
        self.sess.start()
        self.profile = profile

    def _on_form(self, p):
        """조회 전 화면이거나 목록 화면이면 값을 읽을 수 있다."""
        try:
            return LIST_HOST_PATH in p.url and p.evaluate(STATE_JS).get("start") is not None
        except Exception:
            return False

    def _run(self, a, resume):
        t0 = time.monotonic()
        marks, timing = [], []

        def mark(name):
            marks.append((name, round(time.monotonic() - t0, 1)))

        if self.sess is None or self.profile != a["profile"]:
            self._close()
            self._open(a["profile"], a["timeout"])
            mark("크롬 켜기")
        if self.page is None or self.page.is_closed():
            self.page = self.sess.context.new_page()
            self.page.set_default_timeout(int(a["timeout"]) * 1000)
            self.query = None
        p = self.page
        mode = "full"
        smart = a.get("smart")
        if resume and not smart:
            human_actions(p, resume, look=False, timing=timing, t0=t0)
            mark("이어서 조회(입력)")
            mode = "resume"
        else:
            fresh = not (smart and self._on_form(p))
            if fresh:
                p.goto(a["url"], referer="https://www.google.com/")
                self.sess._wait_for_page_stability(p, True, True)
                mark("화면 열기")
                if a["solve"]:
                    self.sess._cloudflare_solver(p)
                    self.sess._wait_for_page_stability(p, True, True)
                mark("클라우드플레어 확인")
                try:   # 고정 1.5초 대신 입력칸이 나타날 때까지만 기다린다
                    p.locator("input[name=txtGoStart], #labelstart").first.wait_for(state="attached", timeout=8000)
                except Exception:
                    pass
            plan = None
            if smart:
                st = p.evaluate(STATE_JS)
                plan = plan_korail(st, smart)
                mark("화면 값 읽기(%s %s)" % (plan["mode"], ",".join(plan["changed"]) or "같음"))
            if plan and plan["mode"] != "full":
                mode = plan["mode"]
                human_actions(p, plan["actions"], look=fresh, timing=timing, t0=t0)
                mark("입력 동작(%s)" % mode)
            else:
                human_actions(p, a["steps"], look=True, timing=timing, t0=t0)   # 전체 목록에는 뒤 동작(after)도 들어 있다
                mark("둘러보기+입력 동작")
                smart = dict(smart, after=None) if smart else smart
            if smart and a.get("code") and not smart.get("after"):
                after = p.evaluate(STATE_JS)
                bad = [k for k, want in (("start", smart["dep"]), ("end", smart["arr"])) if after.get(k) != want]
                if after.get("date", "")[:10] != "%s-%s-%s" % (smart["date"][:4], smart["date"][4:6], smart["date"][6:8]):
                    bad.append("date")
                if bad:
                    raise RuntimeError("조회 뒤 화면 값이 원하는 조건과 다릅니다: %s %s" % (bad, {k: after.get(k) for k in ("start", "end", "date")}))
            if smart and smart.get("after"):
                human_actions(p, smart["after"], look=False, timing=timing, t0=t0)
                mark("뒤 동작")
        self.query = a["query"]
        value = p.evaluate(a["code"]) if a["code"] else None
        mark("결과 읽기")
        if a["shot"]:
            p.screenshot(path=a["shot"])
        return {"js": value, "marks": marks, "timing": timing, "resumed": mode in ("resume", "refresh"), "mode": mode, "v": SERVER_VERSION}

    def fetch(self, a):
        with self.lock:
            retries = []
            for attempt in (0, 1):
                resume = None
                b = a
                if attempt == 0 and a.get("resume_steps") and self.query == a["query"] and self.page is not None:
                    resume = a["resume_steps"]
                if attempt == 1:
                    b = dict(a, smart=None)   # 다시 할 때는 화면을 새로 열고 전부 입력한다
                try:
                    res = self._thread_call(lambda: self._run(b, resume))
                    if retries:
                        res["retry"] = retries
                    return res
                except Exception as e:
                    self._thread_call(self._close)
                    where = [f for f in traceback.extract_tb(e.__traceback__) if f.filename.endswith("scrape_server.py")]
                    why = "%s: %s%s" % (type(e).__name__, str(e)[:300].replace("\n", " "), " (줄 %d)" % where[-1].lineno if where else "")
                    retries.append(why)
                    log("상시 브라우저 오류(시도 %d): %s" % (attempt + 1, why))
                    if attempt == 1 or a.get("retry") is False:
                        raise

    def _thread_call(self, fn):
        import queue
        if self.jobs is None:
            self.jobs = queue.Queue()
            threading.Thread(target=self._loop, daemon=True).start()
        box = queue.Queue(1)
        self.jobs.put((fn, box))
        ok, val = box.get()
        if not ok:
            raise val
        return val

    def _loop(self):
        while True:
            fn, box = self.jobs.get()
            try:
                box.put((True, fn()))
            except BaseException as e:
                box.put((False, e))


HOLD = HoldBrowser()


def make_app():
    from starlette.applications import Starlette
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    token = os.environ.get("SCRAPE_API_TOKEN", "")

    def denied(request):
        return bool(token) and request.headers.get("authorization") != "Bearer " + token

    async def fetch(request):
        if denied(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
            h = body.get("hold")
            if not (h and body.get("actions")):
                return JSONResponse({"error": "NotSupported", "detail": "이 통로는 hold 요청만 받습니다"}, status_code=400)
            res = await run_in_threadpool(HOLD.fetch, {
                "url": body["url"], "profile": body.get("profile") or "hold", "timeout": body.get("timeout", 120),
                "solve": body.get("solve_cloudflare", True), "steps": body["actions"],
                "resume_steps": h.get("resume_actions"), "query": h.get("query"), "code": body.get("js"),
                "shot": h.get("shot"), "smart": body.get("smart"), "retry": h.get("retry", True),
            })
            return JSONResponse(res)
        except Exception as e:   # 막힘·시간 초과도 그대로 알려 준다(봇이 앞 600자를 로그와 알림에 남긴다)
            log("요청 실패 %s: %s" % (type(e).__name__, str(e)[:500].replace("\n", " ")))
            return JSONResponse({"error": type(e).__name__, "detail": str(e)[:600]}, status_code=502)

    async def health(request):
        return JSONResponse({"ok": True, "v": SERVER_VERSION})

    async def quit_(request):
        if denied(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        def bye():
            time.sleep(0.5)
            try:
                HOLD._thread_call(HOLD._close)
            except Exception:
                pass
            os._exit(0)   # 예약작업이 10초 안에 새 파일로 다시 켠다

        threading.Thread(target=bye, daemon=True).start()
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/fetch", fetch, methods=["POST"]), Route("/health", health),
                             Route("/quit", quit_, methods=["POST"])])


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.host != "127.0.0.1" and not os.environ.get("SCRAPE_API_TOKEN"):
        sys.exit("127.0.0.1 밖으로 열려면 환경변수 SCRAPE_API_TOKEN 이 필요하다")
    import uvicorn
    log("통로 시작 v%d 포트 %d" % (SERVER_VERSION, a.port))
    uvicorn.run(make_app(), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
