"""사람이 새로고침하는 것처럼 조회 간격을 만든다. 표준 라이브러리만 쓴다."""
import time
from collections import deque
from random import lognormvariate, random, uniform

MEDIAN_SECONDS = 8.0       # 보통 간격의 중앙값
SIGMA = 0.5                # 로그정규 퍼짐: 대부분 4~16초
MIN_SECONDS = 3.0
REST_CHANCE = 0.06         # 가끔 화면을 떠난 것처럼 쉰다
REST_RANGE = (30.0, 90.0)
HOURLY_CAP = 300           # 철도사별 시간당 조회 상한
CAP_WAIT_RANGE = (120.0, 300.0)


class HumanPacer:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._hits = deque()

    def next_delay(self):
        """다음 조회까지 기다릴 초. 호출할 때마다 조회 1건을 센다."""
        now = self._clock()
        while self._hits and now - self._hits[0] > 3600:
            self._hits.popleft()
        self._hits.append(now)
        if len(self._hits) > HOURLY_CAP:
            return uniform(*CAP_WAIT_RANGE)
        if random() < REST_CHANCE:
            return uniform(*REST_RANGE)
        return max(MIN_SECONDS, lognormvariate(0, SIGMA) * MEDIAN_SECONDS)
