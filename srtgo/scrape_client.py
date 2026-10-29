"""운영 PC 의 스크래핑 통로(ops/scrape_api.py serve)를 부르는 클라이언트. 표준 라이브러리만 쓴다.

코레일에는 사람이 로그인해 둔 프로필의 보이는 크롬 창(real_chrome)으로만 간다.
숨김 브라우저·봇 확인 해제는 쓰지 않는다(solve_cloudflare 는 항상 끈다).
"""
import json
import os
import urllib.request

SCRAPE_API_URL = os.environ.get("SCRAPE_API_URL", "http://127.0.0.1:8765")
KORAIL_PROFILE = "korail"


class ScrapeError(Exception):
    pass


def fetch(url, css=None, profile=KORAIL_PROFILE, timeout=60, api=None):
    body = {
        "url": url,
        "mode": "stealth",       # 서버가 real_chrome 이면 어차피 이 경로(보이는 창)를 쓴다
        "real_chrome": True,
        "solve_cloudflare": False,
        "profile": profile,
        "timeout": timeout,
    }
    if css:
        body["css"] = css
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SCRAPE_API_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        (api or SCRAPE_API_URL) + "/fetch", json.dumps(body).encode(), headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 30) as r:
            return json.load(r)
    except Exception as ex:
        raise ScrapeError("%s: %s" % (type(ex).__name__, str(ex)[:200])) from ex
