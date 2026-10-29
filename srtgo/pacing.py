"""사람이 새로고침하는 것처럼 조회 간격을 만든다. 표준 라이브러리만 쓴다.

값은 아래 기본값이고, `~/.srtgo/pacing.json`(SRTGO_HOME) 이 있으면 그 파일 값이 우선한다.
코드를 고치지 않고 이 파일만 고치면 되고, 봇이 다음 조회 때 바로 반영한다(재시작 불필요).
"""
import json
import os
import time
from collections import deque
from pathlib import Path
from random import lognormvariate, random, uniform

MEDIAN_SECONDS = 8.0       # 보통 간격의 중앙값
SIGMA = 0.5                # 로그정규 퍼짐: 대부분 4~16초
MIN_SECONDS = 3.0
REST_CHANCE = 0.06         # 가끔 화면을 떠난 것처럼 쉰다
REST_RANGE = (30.0, 90.0)
HOURLY_CAP = 300           # 철도사별 시간당 조회 상한
CAP_WAIT_RANGE = (120.0, 300.0)

_KEYS = (
    "MEDIAN_SECONDS", "SIGMA", "MIN_SECONDS", "REST_CHANCE",
    "REST_RANGE", "HOURLY_CAP", "CAP_WAIT_RANGE",
)


def settings_path():
    return Path(os.environ.get("SRTGO_HOME") or Path.home() / ".srtgo") / "pacing.json"


def load_settings(path=None):
    """기본값에 pacing.json 을 덮어쓴 dict. 파일이 없거나 깨졌으면 기본값 그대로."""
    cfg = {k: globals()[k] for k in _KEYS}
    try:
        data = json.loads(Path(path or settings_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    for k in _KEYS:
        if k not in data:
            continue
        v = data[k]
        if isinstance(cfg[k], tuple):
            if isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v):
                cfg[k] = (float(v[0]), float(v[1]))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            cfg[k] = type(cfg[k])(v)
    return cfg


def write_example(path=None):
    """수정용 pacing.json 이 없으면 기본값으로 만들어 준다. 있으면 건드리지 않는다."""
    p = Path(path or settings_path())
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({k: globals()[k] for k in _KEYS}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return p


class HumanPacer:
    def __init__(self, clock=time.monotonic, path=None):
        self._clock = clock
        self._path = path
        self._hits = deque()

    def next_delay(self):
        """다음 조회까지 기다릴 초. 호출할 때마다 조회 1건을 센다."""
        c = load_settings(self._path)
        now = self._clock()
        while self._hits and now - self._hits[0] > 3600:
            self._hits.popleft()
        self._hits.append(now)
        if len(self._hits) > c["HOURLY_CAP"]:
            return uniform(*c["CAP_WAIT_RANGE"])
        if random() < c["REST_CHANCE"]:
            return uniform(*c["REST_RANGE"])
        return max(c["MIN_SECONDS"], lognormvariate(0, c["SIGMA"]) * c["MEDIAN_SECONDS"])
