"""텔레그램 봇으로 예매를 조작한다.

터미널 앞에 붙어 있지 않아도 되도록, CLI가 하던 일을 전부 텔레그램 버튼으로
옮겼다: 역·승객 유형 설정, 검색 -> 열차 선택 -> 대기 -> 예매 -> 결제,
예매내역 확인·결제·취소·환불. CLI와 가장 크게 다른 점은 오류 처리다. CLI는
오류마다 "계속할까요"로 사람을 기다리지만, 봇은 알리기만 하고 계속 돈다.

여러 사람이 쓴다. 모르는 사람이 말을 걸면 오너에게 승인 요청이 가고, 승인된
사람은 각자 자기 코레일/SRT 계정을 연결해 자기 예매만 한다. 계정(비밀번호)은
운영PC의 keyring(OS 자격증명 저장소)에만 둔다. 비밀이 아닌 것(승인 목록,
사용자별 설정, 진행 중인 대기)은 ~/.srtgo/bot_state.json 에 둬서, 봇이
재시작돼도 대기를 이어간다. 항상 켜 두려면 srtgo-watchdog 으로 띄운다.
"""

import asyncio
import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from random import gammavariate

import keyring
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .ktx import (
    AdultPassenger,
    ChildPassenger,
    Disability1To3Passenger,
    Disability4To6Passenger,
    Korail,
    KorailError,
    NeedToLoginError,
    NoResultsError,
    ReserveOption,
    SeniorPassenger,
    SoldOutError,
    TrainType,
)
from .srt import (
    SRT,
    Adult,
    Child,
    Disability1To3,
    Disability4To6,
    SeatType,
    Senior,
    SRTError,
    SRTLoginError,
    SRTNetFunnelError,
    SRTNotLoggedInError,
)
from .srt import STATION_CODE as SRT_STATION_CODE
from .srtgo import (
    DEFAULT_STATIONS,
    RESERVE_INTERVAL_MIN,
    RESERVE_INTERVAL_SCALE,
    RESERVE_INTERVAL_SHAPE,
    STATIONS,
    SUSEO_LINE_STATIONS,
    _is_seat_available,
    get_options,
    get_station,
    get_telegram_credentials,
    pay_card,
)
from .watchdog import EXIT_ALREADY_RUNNING, acquire_lock, data_dir

log = logging.getLogger("srtgo.bot")

RAIL_TYPES = ("SRT", "KTX")
# 2026년 9월 1일 SRT 가 KTX 로 통합됐다. 코레일 계정 하나로 수서·동탄·평택지제 출발
# 열차까지 예매하고, SRT 앱은 회원 예매를 막았다. 그래서 새 예매·예매내역·계정 연결은
# 코레일(KTX) 쪽으로만 받는다. SRT 코드는 예전에 걸어 둔 대기를 이어가려고만 남겨 둔다.
BOOKING_RAIL = "KTX"
RAIL_ICON = {"SRT": "🚄", "KTX": "🚅"}
RAIL_APP = {"SRT": "SRT 앱", "KTX": "코레일+ 앱"}
ACCOUNT_NAME = {"SRT": "SRT", "KTX": "코레일"}
INTEGRATION_NOTE = (
    "🚄 SRT는 2026년 9월 1일부터 KTX로 통합됐습니다.\n"
    "'🚅 열차 예매'에서 수서·동탄·평택지제를 고르면 코레일 계정으로 예매됩니다."
)
SEAT_OPTIONS = (
    ("일반실 우선", "GENERAL_FIRST"),
    ("일반실만", "GENERAL_ONLY"),
    ("특실 우선", "SPECIAL_FIRST"),
    ("특실만", "SPECIAL_ONLY"),
)
SEAT_LABEL = {key: label for label, key in SEAT_OPTIONS}
# (키, 표시이름, SRT 클래스, KTX 클래스). 어른 외에는 각자 설정에서 켠 것만 보여준다.
PASSENGER_TYPES = (
    ("adult", "어른/청소년", Adult, AdultPassenger),
    ("child", "어린이", Child, ChildPassenger),
    ("senior", "경로우대", Senior, SeniorPassenger),
    ("disability1to3", "중증장애인", Disability1To3, Disability1To3Passenger),
    ("disability4to6", "경증장애인", Disability4To6, Disability4To6Passenger),
)
PAX_LABEL = {row[0]: row[1] for row in PASSENGER_TYPES}
OPTIONAL_PAX = tuple(row[0] for row in PASSENGER_TYPES[1:])
MAX_PASSENGERS = 9
WEEKDAYS = "월화수목금토일"
NUMBER_ICONS = "1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣ 6️⃣ 7️⃣ 8️⃣ 9️⃣".split()

# CLI 목록(STATIONS["KTX"])에 없는 코레일 역. '전체 역'에 함께 보여준다.
# 여기에도 없는 역은 설정 -> 역 편집 -> '직접 입력'으로 넣으면 된다.
KTX_MORE_STATIONS = (
    "평택", "천안", "조치원", "구미", "왜관", "청도", "물금", "태화강", "부전",
    "진영", "창원", "진주", "공주", "계룡", "김제", "장성", "남원", "곡성",
    "구례구", "여천", "나주", "상봉", "양평", "만종", "횡성", "둔내", "평창",
    "진부(오대산)", "묵호", "동해", "서원주", "원주", "제천", "단양", "풍기",
    "영주", "안동", "부발", "충주",
)
MAX_STATION_NAME = 15
HANGUL = re.compile("[가-힣]")

# 동시에 도는 대기 수. 한 IP에서 너무 많이 조회하면 매크로로 걸리기 쉽다.
MAX_CONCURRENT_WATCHES = 6
# 가는 편·오는 편을 따로 걸 수 있도록 한 사람당 여러 건.
MAX_WATCHES_PER_USER = 3
# 같은 철도사 대기가 이보다 많으면 각 대기의 간격을 늘려 전체 조회 속도를 묶는다.
FULL_SPEED_WATCHES = 2
# 연속 실패 시 간격을 늘리되 여기까지만. 너무 길면 자리가 나도 놓친다.
MAX_BACKOFF_SECONDS = 60
LOGIN_BACKOFF_START = 10
LOGIN_BACKOFF_MAX = 300
# 잠깐의 끊김(심야 점검 등)마다 알리면 시끄럽다. 이만큼 계속 실패할 때만 알린다.
ERROR_ALERT_AFTER_SECONDS = 20 * 60
ERROR_REALERT_SECONDS = 3 * 3600
# 조회가 계속 실패하면 세션이 조용히 죽었을 수 있으니 이 횟수마다 다시 로그인한다.
RELOGIN_EVERY_ERRORS = 5
PERSIST_EVERY_TRIES = 100
HEARTBEAT_SECONDS = 20
APPROVAL_RESEND_SECONDS = 12 * 3600

STORE_SERVICE = "srtgo-bot"  # 예전 버전이 승인 목록을 두던 keyring 자리 (옮겨 오기용)
CRED_SERVICE = "srtgo-bot-cred"
STATE_FILE = "bot_state.json"

COMMANDS = (
    ("start", "메뉴 열기"),
    ("status", "대기 상황 보기"),
    ("stop", "대기 중지"),
    ("settings", "설정: 역·승객·계정"),
    ("help", "도움말"),
)

KST = timezone(timedelta(hours=9))
UNKNOWN = object()


# --- 작은 도우미 -------------------------------------------------------
def kst_now() -> datetime:
    """운영PC 시간대 설정과 상관없는 한국 시각. 열차 시각이 전부 한국 시각이다."""
    return datetime.now(KST).replace(tzinfo=None)


def fmt_date(ymd) -> str:
    try:
        d = datetime.strptime(str(ymd), "%Y%m%d")
    except ValueError:
        return str(ymd)
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def hhmm(t) -> str:
    t = str(t or "")
    return f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else "?"


def ago(seconds) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분"
    return f"{seconds // 3600}시간 {seconds % 3600 // 60}분"


def hms(seconds) -> str:
    h, rem = divmod(int(max(0, seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def clip(text, limit=4000) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 20] + "\n…(너무 길어 생략)"


def mask(value) -> str:
    value = str(value or "")
    if len(value) <= 3:
        return value[:1] + "**"
    return value[:3] + "*" * min(len(value) - 3, 6)


def number_icon(i) -> str:
    return NUMBER_ICONS[i - 1] if 1 <= i <= len(NUMBER_ICONS) else f"{i}."


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text, callback_data=data) for text, data in row]
            for row in rows
            if row
        ]
    )


def chunk(items, size):
    return [items[i : i + size] for i in range(0, len(items), size)]


HOME = ("↩︎ 처음으로", "home")


def safe_options():
    """CLI '예매 옵션 설정' 값. 오너의 기본값으로만 쓴다."""
    try:
        return get_options()
    except Exception:
        return []


def card_ready() -> bool:
    try:
        return bool(keyring.get_password("card", "ok"))
    except Exception:
        return False


def describe_error(ex) -> str:
    text = str(ex).strip() or type(ex).__name__
    if not isinstance(ex, (KorailError, SRTError, RailLoginError)):
        text = f"{type(ex).__name__}: {text}"
    return text[:300]


# --- 역 -------------------------------------------------------------------
def valid_station(rail_type, name) -> bool:
    if not name or len(name) > MAX_STATION_NAME or not HANGUL.search(name):
        return False
    # SRT는 역 코드표에 있는 역만 조회할 수 있다. 코레일은 이름으로 조회한다.
    return name in SRT_STATION_CODE if rail_type == "SRT" else True


def all_stations(rail_type, extra=()):
    names = list(STATIONS[rail_type])
    if rail_type == "KTX":
        names += [s for s in KTX_MORE_STATIONS if s not in names]
    names += [s for s in extra if s not in names]
    return sorted(names)  # 가나다 순이 찾기 쉽다


def default_stations(rail_type, is_owner):
    """오너는 PC에서 해 둔 '역 설정'을 그대로, 나머지는 기본 역."""
    if is_owner:
        try:
            _, keys = get_station(rail_type)
        except Exception:
            keys = []
        keys = [k.strip() for k in keys or [] if valid_station(rail_type, k.strip())]
        if keys:
            return keys
    return list(DEFAULT_STATIONS[rail_type])


# --- 열차·예약 ------------------------------------------------------------
def train_no(obj) -> str:
    """KTX는 train_no, SRT는 train_number 에 열차 번호가 있다. 앞의 0은 뗀다."""
    raw = getattr(obj, "train_no", None) or getattr(obj, "train_number", None) or ""
    raw = str(raw).strip()
    return raw.lstrip("0") or raw


def train_name(obj) -> str:
    return str(
        getattr(obj, "train_type_name", None) or getattr(obj, "train_name", None) or ""
    )


def train_key(train):
    """재검색해도 같은 열차를 찾기 위한 식별자. 목록 순서는 매번 달라질 수 있다."""
    return (
        train_no(train),
        str(getattr(train, "dep_date", "") or ""),
        str(getattr(train, "dep_time", "") or ""),
    )


def same_train(a, b) -> bool:
    """검색 결과의 열차와 예약 내역이 같은 열차인지.

    코레일 예약 내역의 dep_date 는 실제로는 운행일(run_date)이라, 두 날짜 중
    하나라도 겹치면 같은 날로 본다.
    """
    if train_no(a) != train_no(b):
        return False
    if str(getattr(a, "dep_time", ""))[:4] != str(getattr(b, "dep_time", ""))[:4]:
        return False
    dates_a = {getattr(a, f, None) for f in ("dep_date", "run_date")} - {None, ""}
    dates_b = {getattr(b, f, None) for f in ("dep_date", "run_date")} - {None, ""}
    return not dates_a or not dates_b or bool(dates_a & dates_b)


def departs_at(train):
    try:
        return datetime.strptime(
            str(train.dep_date) + str(train.dep_time)[:6], "%Y%m%d%H%M%S"
        )
    except (AttributeError, ValueError):
        return None


def seat_flags(train, rail_type):
    """(일반실 있음, 특실 있음)"""
    if rail_type == "SRT":
        return train.general_seat_available(), train.special_seat_available()
    return train.has_general_seat(), train.has_special_seat()


def standby_state(train, rail_type):
    """예약대기: True 신청 가능, False 마감, None 이 열차엔 없음."""
    field = "reserve_wait_possible_code" if rail_type == "SRT" else "wait_reserve_flag"
    try:
        code = int(getattr(train, field, None))
    except (TypeError, ValueError):
        return None
    if code == 9:
        return True
    return False if code >= 0 else None


def availability(train, rail_type) -> str:
    general, special = seat_flags(train, rail_type)
    if general and special:
        return "일반·특실 있음"
    if general:
        return "일반실 있음"
    if special:
        return "특실만 있음"
    return "매진·대기 가능" if standby_state(train, rail_type) else "매진"


def train_title(train) -> str:
    return f"{hhmm(train.dep_time)} {train_name(train)} {train_no(train)}"


def train_line(train, rail_type) -> str:
    return (
        f"{hhmm(train.dep_time)}→{hhmm(train.arr_time)} "
        f"{train_name(train)} {train_no(train)} · {availability(train, rail_type)}"
    )


def rsv_id(item) -> str:
    return str(
        getattr(item, "rsv_id", None)
        or getattr(item, "reservation_number", None)
        or getattr(item, "pnr_no", None)
        or ""
    )


def describe(item) -> str:
    try:
        return str(item)
    except Exception:
        return f"예약 {rsv_id(item)}"


def item_state(item) -> str:
    if getattr(item, "is_ticket", False):
        return "🎟 발권완료"
    if getattr(item, "is_waiting", False):
        return "⏳ 예약대기"
    return "💳 결제대기"


def item_title(item) -> str:
    return (
        f"{fmt_date(getattr(item, 'dep_date', ''))} "
        f"{hhmm(getattr(item, 'dep_time', ''))} {train_name(item)} {train_no(item)}"
    )


def item_tag(item) -> str:
    """버튼이 가리키는 항목이 그새 바뀌지 않았는지 확인하는 짧은 표식."""
    base = rsv_id(item) + "|" + describe(item)
    return hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()[:6]


def active_reservations(rail, rail_type):
    """발권 전 예약(결제대기·예약대기). 방금 잡힌 예약을 찾을 때 쓴다."""
    if rail_type == "SRT":
        return list(rail.get_reservations())
    found = rail.reservations()
    return list(found) if isinstance(found, list) else [found]


def list_reservations(rail, rail_type):
    """예약(미결제·예약대기)과 발권된 승차권을 한 목록으로. is_ticket 을 붙인다."""
    if rail_type == "SRT":
        items = list(rail.get_reservations())
        for item in items:
            item.is_ticket = bool(getattr(item, "paid", False))
        return items
    tickets = list(rail.tickets() or [])
    for t in tickets:
        t.is_ticket = True
    items = active_reservations(rail, rail_type)
    for r in items:
        r.is_ticket = False
    return tickets + items


# --- 오류 분류 ------------------------------------------------------------
LOGIN_HINTS = ("로그인 후 사용", "로그인이 필요", "Need to Login")
NETFUNNEL_HINTS = ("정상적인 경로로 접근",)
# 오류가 아니라 '이번엔 못 잡았다'는 뜻인 응답들 (CLI 와 같은 목록).
BENIGN_HINTS = (
    "Sold out",
    "잔여석없음",
    "매진",
    "예약대기자한도수초과",
    "예약대기 접수가 마감",
    "사용자가 많아 접속이 원활하지 않습니다",
)


def classify(ex) -> str:
    """'login' 다시 로그인 / 'netfunnel' 대기열 키 초기화 / 'benign' 정상(못 잡음) / 'error'."""
    if isinstance(ex, (NeedToLoginError, SRTNotLoggedInError)):
        return "login"
    if isinstance(ex, SRTNetFunnelError):
        return "netfunnel"
    if isinstance(ex, (SoldOutError, NoResultsError)):
        return "benign"
    text = str(getattr(ex, "msg", "") or ex)
    if any(h in text for h in LOGIN_HINTS):
        return "login"
    if any(h in text for h in NETFUNNEL_HINTS):
        return "netfunnel"
    if any(h in text for h in BENIGN_HINTS):
        return "benign"
    return "error"


def clear_netfunnel(rail):
    try:
        rail.clear()  # SRT 만 있다
    except Exception:
        pass


# --- 로그인 ---------------------------------------------------------------
class RailLoginError(Exception):
    """철도사가 로그인을 거부했다.

    permanent 면 계정 문제(비밀번호 오류 등)라 다시 해도 소용없고, 계속 두드리면
    계정이 잠길 수 있다. 아니면(매크로 차단 등) 시간을 두고 다시 해 본다.
    """

    def __init__(self, rail_type, reason, permanent=True):
        super().__init__(f"{rail_type} 로그인 실패: {reason}")
        self.rail_type = rail_type
        self.reason = reason
        self.permanent = permanent


# 코레일은 로그인 실패를 예외 대신 응답 메시지로 준다. 계정 문제로 보이는 문구들.
CREDENTIAL_HINTS = ("비밀번호", "회원", "존재하지", "일치하지", "잠금", "잠겼", "탈퇴", "휴면", "아이디")


def open_rail(rail_type, user_id, password):
    """로그인해서 SRT/Korail 객체를 돌려준다. 거부되면 RailLoginError."""
    if rail_type == "SRT":
        try:
            return SRT(user_id, password)
        except SRTLoginError as ex:
            reason = str(ex)
            raise RailLoginError(rail_type, reason, permanent="IP" not in reason) from ex
    # 코레일은 로그인에 실패해도 예외를 던지지 않는다. logined 를 봐야 안다.
    rail = Korail(user_id, password)
    if not rail.logined:
        reason = getattr(rail, "login_error", None) or "사유 미상"
        raise RailLoginError(
            rail_type, reason, permanent=any(h in reason for h in CREDENTIAL_HINTS)
        )
    return rail


def account_name(rail) -> str:
    return str(getattr(rail, "name", None) or getattr(rail, "membership_name", None) or "")


class Creds:
    """사용자별 철도 계정. keyring 에만 둔다 (예전 버전과 같은 자리)."""

    @staticmethod
    def get(chat_id, rail_type):
        return (
            keyring.get_password(CRED_SERVICE, f"{chat_id}:{rail_type}:id"),
            keyring.get_password(CRED_SERVICE, f"{chat_id}:{rail_type}:pw"),
        )

    @staticmethod
    def set(chat_id, rail_type, user_id, password):
        keyring.set_password(CRED_SERVICE, f"{chat_id}:{rail_type}:id", user_id)
        keyring.set_password(CRED_SERVICE, f"{chat_id}:{rail_type}:pw", password)

    @staticmethod
    def clear(chat_id, rail_type):
        for field in ("id", "pw"):
            try:
                keyring.delete_password(CRED_SERVICE, f"{chat_id}:{rail_type}:{field}")
            except Exception:
                pass


def pc_credentials(rail_type):
    """PC 의 srtgo '로그인 설정' 계정. 오너만 쓴다."""
    user_id = keyring.get_password(rail_type, "id")
    password = keyring.get_password(rail_type, "pass")
    return (user_id, password) if user_id and password else (None, None)


def account_source(chat_id, rail_type, is_owner):
    """'bot' 텔레그램으로 연결 / 'pc' 오너의 PC 설정 / None 없음."""
    user_id, password = Creds.get(chat_id, rail_type)
    if user_id and password:
        return "bot"
    if is_owner and pc_credentials(rail_type)[0]:
        return "pc"
    return None


def build_rail(rail_type, chat_id, is_owner):
    """사용자 계정으로 로그인한다. 절대 입력을 기다리지 않는다.

    CLI 의 login() 은 계정이 없으면 터미널 입력을 기다리는데, 봇에서 그걸 부르면
    아무도 없는 운영PC에서 영원히 멈춘다.
    """
    user_id, password = Creds.get(chat_id, rail_type)
    if not (user_id and password) and is_owner:
        user_id, password = pc_credentials(rail_type)
    if not (user_id and password):
        raise RailLoginError(rail_type, "계정이 연결되어 있지 않습니다", permanent=True)
    return open_rail(rail_type, user_id, password)


def search_params(rail_type, dep, arr, date, time_, total, ktx_only):
    # CLI와 같게, 검색은 전체 인원을 어른으로 넘기고 실제 구성은 예매할 때 쓴다.
    if rail_type == "SRT":
        return {
            "dep": dep,
            "arr": arr,
            "date": date,
            "time": time_,
            "passengers": [Adult(total)],
            "available_only": False,
        }
    # 옛 SRT 노선엔 고속열차만 다닌다. 'KTX만' 필터를 걸면 옛 SRT 열차가 빠질 수 있어 뺀다.
    suseo_line = bool({dep, arr} & set(SUSEO_LINE_STATIONS))
    return {
        "dep": dep,
        "arr": arr,
        "date": date,
        "time": time_,
        "passengers": [AdultPassenger(total)],
        "include_no_seats": True,
        "train_type": TrainType.KTX if ktx_only and not suseo_line else TrainType.ALL,
    }


# --- 저장소 ---------------------------------------------------------------
class StateStore:
    """승인 목록, 사용자별 설정, 진행 중인 대기. 비밀은 두지 않는다.

    path 가 None 이면 메모리에만 둔다 (테스트용).
    """

    def __init__(self, path=None):
        self.path = path
        self.data = self._load()

    @staticmethod
    def _blank():
        return {"version": 1, "users": {}, "prefs": {}, "watches": {}}

    def _load(self):
        blank = self._blank()
        if self.path is None:
            return blank
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return blank
        except OSError as ex:
            log.error("상태 파일을 읽지 못했습니다: %s", ex)
            return blank
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("최상위가 객체가 아님")
        except ValueError as ex:
            broken = self.path.with_name(f"{self.path.stem}.broken-{int(time.time())}.json")
            try:
                self.path.replace(broken)
            except OSError:
                pass
            log.error("상태 파일이 깨져 %s 로 옮기고 새로 시작합니다: %s", broken.name, ex)
            return blank
        for key, value in blank.items():
            if not isinstance(data.get(key), type(value)):
                data[key] = value
        return data

    def save(self):
        if self.path is None:
            return
        payload = json.dumps(self.data, ensure_ascii=False, indent=1)
        tmp = self.path.with_name(self.path.name + ".tmp")
        last = None
        # 윈도에선 백신 등이 파일을 잠깐 잡고 있어 교체가 실패할 때가 있다.
        for _ in range(3):
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self.path)
                return
            except OSError as ex:
                last = ex
                time.sleep(0.05)
        log.error("상태 파일 저장 실패: %s", last)

    # 사용자 -------------------------------------------------------------
    def user(self, chat_id) -> dict:
        return self.data["users"].get(str(chat_id), {})

    def status(self, chat_id):
        return self.user(chat_id).get("status")

    def set_user(self, chat_id, **fields):
        entry = self.data["users"].setdefault(str(chat_id), {})
        entry.update({k: v for k, v in fields.items() if v is not None})
        entry["at"] = int(time.time())
        self.save()

    def forget_user(self, chat_id):
        self.data["users"].pop(str(chat_id), None)
        self.save()

    def users(self, status):
        return {k: v for k, v in self.data["users"].items() if v.get("status") == status}

    # 설정 ---------------------------------------------------------------
    def prefs(self, chat_id) -> dict:
        return self.data["prefs"].setdefault(str(chat_id), {})

    # 대기 ---------------------------------------------------------------
    def put_watches(self, chat_id, watches):
        if watches:
            self.data["watches"][str(chat_id)] = [w.to_dict() for w in watches]
        else:
            self.data["watches"].pop(str(chat_id), None)
        self.save()


def migrate_users(store):
    """예전 버전은 승인 목록을 keyring 에 뒀다. 한 번만 상태 파일로 옮겨 온다."""
    if store.data["users"]:
        return
    try:
        raw = keyring.get_password(STORE_SERVICE, "users")
        old = json.loads(raw) if raw else {}
    except Exception:
        return
    if isinstance(old, dict) and old:
        store.data["users"] = {str(k): v for k, v in old.items() if isinstance(v, dict)}
        store.save()
        log.info("keyring 의 사용자 목록 %d명을 상태 파일로 옮겼습니다.", len(store.data["users"]))


# --- 예매 조건 ------------------------------------------------------------
class Draft:
    """만들고 있는 예매 조건. 대기를 시작하면 Watch 로 복사하고 버린다."""

    def __init__(self, rail_type=None):
        self.rail_type = rail_type
        self.dep = None
        self.arr = None
        self.date = None
        self.time = None
        self.counts = {"adult": 1}
        self.trains = []  # 마지막 검색 결과
        self.selected = []  # 고른 열차의 train_key
        self.seat_option = "GENERAL_FIRST"
        self.standby = True
        self.pay = False
        self.searched_at = None

    def total(self):
        return sum(self.counts.values())

    def searchable(self):
        return bool(self.rail_type and self.dep and self.arr and self.date and self.time)

    def find_train(self, no, dep_time):
        for t in self.trains:
            if train_no(t) == no and str(t.dep_time) == dep_time:
                return t
        return None


class Watch:
    """돌고 있는 대기 하나. 재시작해도 이어가도록 FIELDS 를 저장한다."""

    FIELDS = (
        "id", "rail_type", "dep", "arr", "date", "time", "counts", "selected",
        "titles", "seat_option", "standby", "pay", "ktx_only", "started_at",
        "tries", "error_count",
    )

    def __init__(self, **kw):
        self.id = kw.get("id") or secrets.token_hex(3)
        self.rail_type = kw["rail_type"]
        self.dep = kw["dep"]
        self.arr = kw["arr"]
        self.date = kw["date"]
        self.time = kw["time"]
        self.counts = dict(kw["counts"])
        self.selected = [tuple(k) for k in kw["selected"]]
        self.titles = list(kw.get("titles") or [])
        self.seat_option = kw["seat_option"]
        self.standby = bool(kw.get("standby", True))
        self.pay = bool(kw.get("pay", False))
        self.ktx_only = bool(kw.get("ktx_only", False))
        self.started_at = float(kw.get("started_at") or time.time())
        self.tries = int(kw.get("tries") or 0)
        self.error_count = int(kw.get("error_count") or 0)
        # 저장하지 않는 실행 상태
        self.phase = "start"
        self.account = None
        self.last_ok_at = None
        self.last_error = None
        self.error_since = None
        self.alerted_at = None
        self.known_rsv = None  # 대기 시작 전부터 있던 예약 번호들

    @classmethod
    def from_draft(cls, d, ktx_only, pay):
        chosen = set(d.selected)
        trains = [t for t in d.trains if train_key(t) in chosen]
        return cls(
            rail_type=d.rail_type,
            dep=d.dep,
            arr=d.arr,
            date=d.date,
            time=d.time,
            counts={k: v for k, v in d.counts.items() if v > 0},
            selected=[train_key(t) for t in trains],
            titles=[train_title(t) for t in trains],
            seat_option=d.seat_option,
            standby=d.standby,
            pay=pay,
            ktx_only=ktx_only if d.rail_type == "KTX" else False,
        )

    def to_dict(self):
        data = {f: getattr(self, f) for f in self.FIELDS}
        data["selected"] = [list(k) for k in self.selected]
        return data

    @classmethod
    def from_dict(cls, data):
        try:
            if data.get("rail_type") not in RAIL_TYPES:
                raise ValueError("rail_type")
            if data.get("seat_option") not in SEAT_LABEL:
                raise ValueError("seat_option")
            for field in ("dep", "arr", "date", "time"):
                if not isinstance(data.get(field), str) or not data[field]:
                    raise ValueError(field)
            selected = [
                tuple(str(x) for x in k)
                for k in data.get("selected") or []
                if isinstance(k, (list, tuple)) and len(k) == 3
            ]
            if not selected:
                raise ValueError("selected")
            counts = {
                k: int(v) for k, v in (data.get("counts") or {}).items() if k in PAX_LABEL
            }
            counts = {k: v for k, v in counts.items() if v > 0}
            if not counts or sum(counts.values()) > MAX_PASSENGERS:
                raise ValueError("counts")
            return cls(**{**data, "selected": selected, "counts": counts})
        except (AttributeError, KeyError, TypeError, ValueError) as ex:
            raise ValueError(f"저장된 대기 형식이 잘못됨 ({ex})") from ex

    @property
    def is_srt(self):
        return self.rail_type == "SRT"

    def total(self):
        return sum(self.counts.values())

    def passengers(self):
        idx = 2 if self.is_srt else 3
        return [
            row[idx](self.counts[row[0]])
            for row in PASSENGER_TYPES
            if self.counts.get(row[0], 0) > 0
        ]

    def params(self):
        return search_params(
            self.rail_type, self.dep, self.arr, self.date, self.time, self.total(),
            self.ktx_only,
        )

    def seat_type(self):
        return getattr(SeatType if self.is_srt else ReserveOption, self.seat_option)

    def deadline(self):
        """감시 중인 열차 중 가장 늦게 떠나는 시각. 모르면 None (그럴 땐 끝내지 않는다)."""
        times = []
        for _, dep_date, dep_time in self.selected:
            try:
                times.append(datetime.strptime(dep_date + dep_time[:6], "%Y%m%d%H%M%S"))
            except ValueError:
                continue
        return max(times) if times else None

    def headline(self):
        return (
            f"{RAIL_ICON[self.rail_type]} {self.rail_type} {self.dep}→{self.arr} · "
            f"{fmt_date(self.date)} {self.time[:2]}시 이후 · {self.total()}명"
        )

    def options_line(self):
        parts = [SEAT_LABEL[self.seat_option]]
        if self.standby:
            parts.append("매진이면 예약대기")
        if self.pay:
            parts.append("자동 결제")
        return " · ".join(parts)

    def summary(self):
        return "\n".join(
            [
                self.headline(),
                f"🎯 {', '.join(self.titles) or f'{len(self.selected)}개 열차'}",
                f"💺 {self.options_line()}",
            ]
        )

    def note_error(self, message):
        self.last_error = message
        self.error_count += 1
        if self.error_since is None:
            self.error_since = time.time()

    def clear_error(self):
        """조회가 성공하면 지운다. 안 지우면 몇 시간 전 오류가 계속 떠 있다."""
        self.last_error = None
        self.error_since = None
        self.alerted_at = None

    def health(self):
        now = time.time()
        if self.last_error:
            return f"⚠️ {ago(now - self.error_since)}째 재시도 중: {self.last_error}"
        if self.phase in ("start", "login") and not self.last_ok_at:
            return "🔐 로그인 중..."
        if not self.last_ok_at:
            return "⏳ 첫 조회 중..."
        text = f"✅ 정상 (마지막 조회 {ago(now - self.last_ok_at)} 전)"
        if self.error_count:
            text += f" · 그동안 오류 {self.error_count}회, 모두 자동 복구"
        return text


class UserContext:
    """사용자 한 명의 상태. 예매 중인 조건(draft)과 돌고 있는 대기(watches)는 완전히
    따로 둔다. 같은 객체를 나눠 쓰면 예매내역 한 번 보는 것만으로도 돌던 대기가
    망가진다 (예전 버그)."""

    def __init__(self, chat_id, is_owner, store):
        self.chat_id = str(chat_id)
        self.is_owner = is_owner
        self.store = store
        self.draft = Draft()
        self.watches = {}  # id -> Watch (시작한 순서)
        self.tasks = {}  # id -> asyncio.Task
        # 입력을 기다리는 중: ("link", rail, "id"|"pw", 받은 id) / ("station", rail)
        self.awaiting = None
        self.rv_rail = None
        self.rv_items = []
        self.rails = {}  # 대화용 로그인 세션 (대기용과 따로)
        self.lock = asyncio.Lock()

    @property
    def prefs(self):
        return self.store.prefs(self.chat_id)

    def stations(self, rail_type):
        saved = self.prefs.get("stations", {}).get(rail_type)
        if isinstance(saved, list) and saved:
            return list(saved)
        return default_stations(rail_type, self.is_owner)

    def set_stations(self, rail_type, names):
        self.prefs.setdefault("stations", {})[rail_type] = list(names)
        self.store.save()

    def reset_stations(self, rail_type):
        self.prefs.get("stations", {}).pop(rail_type, None)
        self.store.save()

    def pax_types(self):
        saved = self.prefs.get("pax_types")
        if isinstance(saved, list):
            return [k for k in OPTIONAL_PAX if k in saved]
        if self.is_owner:
            return [k for k in OPTIONAL_PAX if k in safe_options()]
        return []

    def ktx_only(self):
        saved = self.prefs.get("ktx_only")
        if isinstance(saved, bool):
            return saved
        return self.is_owner and "ktx" in safe_options()

    def last_route(self, rail_type):
        last = self.prefs.get("last", {}).get(rail_type)
        if not isinstance(last, dict):
            return None
        if not (valid_station(rail_type, last.get("dep")) and valid_station(rail_type, last.get("arr"))):
            return None
        return last

    def remember_route(self, d):
        self.prefs.setdefault("last", {})[d.rail_type] = {
            "dep": d.dep,
            "arr": d.arr,
            "counts": {k: v for k, v in d.counts.items() if v > 0},
        }
        self.store.save()


# --- 화면 -----------------------------------------------------------------
class Reply:
    """화면을 그릴 곳. 버튼에서 왔으면 그 메시지를 고치고, 명령에서 왔으면 새로 보낸다.

    고칠 수 없는 메시지(너무 오래됨 등)면 새로 보내고, 이후엔 그 메시지를 고친다.
    """

    def __init__(self, bot, u, query=None):
        self.bot = bot
        self.u = u
        self.query = query
        self.message = None

    async def show(self, text, markup=None):
        text = clip(text)
        if self.query is not None or self.message is not None:
            try:
                if self.query is not None:
                    await self.query.edit_message_text(text, reply_markup=markup)
                else:
                    await self.message.edit_text(text, reply_markup=markup)
                return
            except BadRequest as ex:
                if "not modified" in str(ex).lower():
                    return
                log.info("화면을 고치지 못해 새로 보냅니다: %s", ex)
            except TelegramError as ex:
                log.warning("화면 수정 실패: %s", ex)
        self.query = None
        self.message = await self.bot._send(self.u.chat_id, text, markup)


# --- 봇 -------------------------------------------------------------------
class Bot:
    # 놓치면 안 되는 알림을 다시 보내는 간격, 예약 결과를 다시 확인하는 간격 (초)
    IMPORTANT_SEND_DELAYS = (0, 5, 15, 30, 60, 120, 300)
    VERIFY_DELAYS = (0, 3, 10)

    def __init__(self, owner_id, store=None, tg=None):
        self.owner_id = str(owner_id)
        self.store = store if store is not None else StateStore()
        self.tg = tg  # telegram.Bot (post_init 에서 채운다)
        self.contexts = {}
        self.heartbeat_path = None
        self._heartbeat_task = None
        self._bg = set()  # 예약 처리 중인 작업 (대기를 멈춰도 끝까지 돈다)
        self._turns = {}  # chat_id -> asyncio.Lock (한 사람의 입력을 순서대로)
        self._conflict_noted_at = 0
        if self.store.status(self.owner_id) != "approved":
            self.store.set_user(self.owner_id, status="approved", name="오너")

    # --- 기본 ---------------------------------------------------------
    def context_for(self, chat_id) -> UserContext:
        chat_id = str(chat_id)
        u = self.contexts.get(chat_id)
        if u is None:
            u = self.contexts[chat_id] = UserContext(
                chat_id, chat_id == self.owner_id, self.store
            )
        return u

    def is_allowed(self, chat_id) -> bool:
        return str(chat_id) == self.owner_id or self.store.status(chat_id) == "approved"

    def active_count(self):
        return sum(len(u.watches) for u in self.contexts.values())

    def _bind(self, context):
        if self.tg is None and context is not None:
            self.tg = context.bot

    async def _send(self, chat_id, text, markup=None):
        """보내기만 한다. 실패해도 예외를 내지 않는다 (알림 실패로 대기가 죽으면 안 된다)."""
        for attempt in (1, 2):
            try:
                return await self.tg.send_message(
                    chat_id=chat_id, text=clip(text), reply_markup=markup
                )
            except RetryAfter as ex:
                delay = ex.retry_after
                if isinstance(delay, timedelta):
                    delay = delay.total_seconds()
                if attempt == 2:
                    break
                await asyncio.sleep(min(float(delay), 60))
            except Forbidden:
                log.warning("%s 에게 보낼 수 없습니다 (봇을 차단했거나 대화를 지움)", chat_id)
                return None
            except TelegramError as ex:
                log.warning("메시지 전송 실패 (%s): %s", chat_id, ex)
                return None
        return None

    async def _send_important(self, chat_id, text, markup=None):
        """예매 결과처럼 놓치면 안 되는 알림. 통신이 끊겼으면 한동안 다시 보낸다."""
        for delay in self.IMPORTANT_SEND_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            if await self._send(chat_id, text, markup) is not None:
                return True
        log.error("중요 알림을 끝내 보내지 못했습니다 (%s): %s", chat_id, text)
        return False

    @staticmethod
    async def _delete(message) -> bool:
        try:
            await message.delete()
            return True
        except TelegramError:
            return False

    # --- 입장 관리 ------------------------------------------------------
    async def _gate(self, update):
        """승인된 사용자면 UserContext, 아니면 None. 처음 온 사람은 오너에게 알린다."""
        chat = update.effective_chat
        if chat is None:
            return None
        chat_id = str(chat.id)
        if getattr(chat, "type", "private") != "private" and chat_id != self.owner_id:
            return None
        if self.is_allowed(chat_id):
            return self.context_for(chat_id)

        user = update.effective_user
        name = (getattr(user, "full_name", None) or "이름 없음")[:40]
        handle = getattr(user, "username", None)
        is_message = update.callback_query is None
        status = self.store.status(chat_id)
        if status == "denied":
            return None
        if status == "pending":
            entry = self.store.user(chat_id)
            if time.time() - entry.get("asked_at", 0) > APPROVAL_RESEND_SECONDS:
                self.store.set_user(chat_id, asked_at=int(time.time()))
                await self._ask_owner(chat_id, name, handle, again=True)
            if is_message:
                await self._send(chat_id, "⏳ 관리자 승인을 기다리는 중입니다. 승인되면 알려드릴게요.")
            return None

        self.store.set_user(
            chat_id, status="pending", name=name, handle=handle, asked_at=int(time.time())
        )
        log.info("새 사용자 승인 요청: %s (%s)", chat_id, name)
        await self._ask_owner(chat_id, name, handle)
        await self._send(
            chat_id,
            "👋 안녕하세요! 이 봇은 승인된 사람만 쓸 수 있어요.\n"
            "관리자에게 사용 승인을 요청했습니다. 승인되면 알려드릴게요.",
        )
        return None

    async def _ask_owner(self, chat_id, name, handle, again=False):
        who = name + (f" (@{handle})" if handle else "")
        await self._send(
            self.owner_id,
            f"👤 {'(다시) ' if again else ''}새 사용자가 사용을 요청했습니다.\n{who}\nID: {chat_id}",
            kb([[("✅ 승인", f"approve:{chat_id}"), ("🚫 거절", f"deny:{chat_id}")]]),
        )

    # --- 명령 -----------------------------------------------------------
    async def _enter(self, update, context):
        self._bind(context)
        u = await self._gate(update)
        if u is not None:
            u.awaiting = None
        return u

    async def cmd_start(self, update, context):
        u = await self._enter(update, context)
        if u is not None:
            await self.show_home(Reply(self, u))

    async def cmd_help(self, update, context):
        u = await self._enter(update, context)
        if u is not None:
            await self.show_help(Reply(self, u))

    async def cmd_status(self, update, context):
        u = await self._enter(update, context)
        if u is not None:
            await self.show_status(Reply(self, u))

    async def cmd_settings(self, update, context):
        u = await self._enter(update, context)
        if u is not None:
            await self.show_settings(Reply(self, u))

    async def cmd_stop(self, update, context):
        u = await self._enter(update, context)
        if u is None:
            return
        r = Reply(self, u)
        if len(u.watches) == 1:
            w = self.stop_watch(u, next(iter(u.watches)))
            await r.show(f"⏹ 대기를 멈췄습니다.\n{w.headline()}", kb([[HOME]]))
            return
        await self.show_stop(r)

    async def cmd_unknown(self, update, context):
        u = await self._enter(update, context)
        if u is not None:
            await self.show_home(Reply(self, u), note="모르는 명령입니다. 아래 버튼을 써 주세요. 👇")

    async def on_other(self, update, context):
        """사진·스티커 등. 봇은 버튼으로 조작한다고 알려 준다."""
        u = await self._enter(update, context)
        if u is not None:
            await self.show_home(Reply(self, u), note="버튼으로 조작해 주세요. 👇")

    def _turn(self, update):
        """한 사람의 버튼·입력은 온 순서대로 하나씩 처리한다.

        업데이트를 동시에 처리하므로(느린 로그인이 다른 사람을 막지 않게), 그냥 두면
        '열차 선택' 직후 누른 '다음'이 먼저 처리되거나, 연달아 보낸 아이디와
        비밀번호의 처리 순서가 뒤바뀔 수 있다. 다른 사람끼리는 서로 기다리지 않는다.
        """
        chat = update.effective_chat
        key = str(chat.id) if chat is not None else ""
        lock = self._turns.get(key)
        if lock is None:
            lock = self._turns[key] = asyncio.Lock()
        return lock

    async def on_text(self, update, context):
        async with self._turn(update):
            await self._on_text(update, context)

    async def _on_text(self, update, context):
        self._bind(context)
        u = await self._gate(update)
        if u is None:
            return
        message = update.message
        text = (message.text or "").strip()
        waiting = u.awaiting
        if waiting and waiting[0] == "link":
            await self._on_link_text(u, message, waiting, text)
        elif waiting and waiting[0] == "station":
            await self._on_station_text(u, waiting[1], text)
        else:
            await self.show_home(Reply(self, u), note="버튼으로 조작해 주세요. 👇")

    # --- 버튼 -----------------------------------------------------------
    async def on_button(self, update, context):
        async with self._turn(update):
            await self._on_button(update, context)

    async def _on_button(self, update, context):
        self._bind(context)
        query = update.callback_query
        u = await self._gate(update)
        try:
            await query.answer(None if u else "승인된 사용자만 쓸 수 있습니다.")
        except TelegramError:
            pass
        if u is None:
            return
        action, _, arg = (query.data or "").partition(":")
        # 입력을 기다리던 중에 버튼을 누르면 입력 대기를 끝낸다. 그대로 두면
        # 나중에 보낸 엉뚱한 메시지가 비밀번호로 저장될 수 있다.
        u.awaiting = None
        handler = getattr(self, f"btn_{action}", None) if action.isalpha() else None
        r = Reply(self, u, query)
        if handler is None:
            await self.show_home(r, note="오래된 버튼입니다. 메뉴에서 다시 골라 주세요.")
            return
        await handler(r, arg)

    async def btn_noop(self, r, arg):
        pass

    async def btn_home(self, r, arg):
        await self.show_home(r)

    async def btn_help(self, r, arg):
        await self.show_help(r)

    # --- 오류 -----------------------------------------------------------
    async def on_error(self, update, context):
        err = context.error
        if isinstance(err, Conflict):
            log.error("같은 봇 토큰으로 다른 곳에서도 봇이 돌고 있습니다 (Conflict). 한 곳만 남기세요.")
            now = time.time()
            if now - self._conflict_noted_at > 3600 and self.tg is not None:
                self._conflict_noted_at = now
                await self._send(
                    self.owner_id,
                    "⚠️ 이 봇이 다른 PC에서도 실행 중입니다.\n"
                    "두 곳에서 동시에 돌면 버튼이 번갈아 먹통이 됩니다. 한 곳만 남기고 꺼 주세요.",
                )
            return
        if isinstance(err, (NetworkError, RetryAfter)):
            log.warning("텔레그램 통신 오류: %s", err)
            return
        log.error("처리 중 오류", exc_info=err)
        chat = update.effective_chat if isinstance(update, Update) else None
        if chat is not None and self.is_allowed(chat.id) and self.tg is not None:
            await self._send(chat.id, "⚠️ 처리 중 문제가 생겼습니다. /start 로 다시 시작해 주세요.")

    # --- 메인 화면 ------------------------------------------------------
    def home_markup(self, u):
        n = len(u.watches)
        rows = [
            [("🚅 열차 예매 (KTX·SRT)", f"book:{BOOKING_RAIL}")],
            [("🎫 예매내역", f"rv:{BOOKING_RAIL}"), (f"📋 대기 상황{f' ({n})' if n else ''}", "status")],
            [("⏹ 대기 중지", "stop"), ("⚙️ 설정", "set")],
            [("❓ 도움말", "help")],
        ]
        if u.is_owner:
            pending = len(self.store.users("pending"))
            rows.append([(f"👥 사용자 관리{f' (승인 대기 {pending})' if pending else ''}", "users")])
        return kb(rows)

    async def show_home(self, r, note=None):
        u = r.u
        lines = [note, ""] if note else []
        lines.append("🚅 srtgo 예매 봇 (KTX·SRT 통합)")
        if u.watches:
            lines.append(f"👀 대기 {len(u.watches)}건이 돌고 있습니다.")
        else:
            lines.append("무엇을 할까요?")
        await r.show("\n".join(lines), self.home_markup(u))

    async def show_help(self, r):
        u = r.u
        pay = (
            "💳 결제: 🎫 예매내역에서 카드 결제할 수 있고, 예매할 때 '자동 결제'도 고를 수 있어요."
            if u.is_owner
            else "💳 결제: 예매되면 구입기한 안에 코레일+ 앱에서 결제해 주세요. 안 하면 자동 취소됩니다."
        )
        text = "\n".join(
            [
                "❓ srtgo 봇 사용법",
                "",
                "🚅 예매: 열차 예매 → 출발역·도착역 → 날짜 → 시각 → 인원 → 감시할 열차 → 좌석 → 대기 시작",
                "  KTX와 옛 SRT(수서·동탄·평택지제) 열차 모두 코레일 계정 하나로 예매합니다.",
                "  자리가 나면 자동으로 예매하고 바로 알려 드립니다. 매진이면 예약대기도 신청할 수 있어요.",
                f"  대기는 한 사람당 {MAX_WATCHES_PER_USER}건까지 (가는 편·오는 편 따로 걸기).",
                "🎫 예매내역: 예약·승차권 보기, 취소·환불",
                pay,
                "⚙️ 설정: 자주 쓰는 역(⭐), 역 직접 추가, 승객 유형(어린이·경로·장애인), KTX만 검색, 코레일 계정 연결",
                "",
                "명령어",
                "/start 메뉴 · /status 대기 상황 · /stop 대기 중지 · /settings 설정 · /help 도움말",
                "",
                "봇은 운영PC에서 항상 켜져 있고, 재시작돼도 대기를 이어갑니다.",
                "조회 중 오류가 나도 멈추지 않고 계속 재시도합니다.",
            ]
        )
        await r.show(text, kb([[("⚙️ 설정", "set"), HOME]]))

    # --- 설정 -----------------------------------------------------------
    def account_line(self, u, rail_type):
        src = account_source(u.chat_id, rail_type, u.is_owner)
        if src == "bot":
            return f"✅ 연결됨 ({mask(Creds.get(u.chat_id, rail_type)[0])})"
        if src == "pc":
            return "🖥 PC에 설정된 계정 사용"
        return "⬜ 연결 안 됨"

    async def btn_set(self, r, arg):
        await self.show_settings(r)

    async def show_settings(self, r, note=None):
        u = r.u
        pax = u.pax_types()
        ktx_only = u.ktx_only()
        lines = [note, ""] if note else []
        lines += [
            "⚙️ 설정 (나에게만 적용)",
            "",
            f"⭐ 자주 쓰는 역: {', '.join(u.stations(BOOKING_RAIL))}",
            f"👥 승객 유형: 어른{''.join(' · ' + PAX_LABEL[k] for k in pax)}",
            f"🚅 검색: {'KTX 계열만 (ITX·무궁화 빼기)' if ktx_only else '모든 열차 (ITX·무궁화 포함)'}",
            f"🔑 코레일 계정: {self.account_line(u, BOOKING_RAIL)}",
            "",
            "승객 유형을 켜면 예매할 때 인원을 따로 고를 수 있어요.",
        ]
        rows = [
            [("⭐ 자주 쓰는 역 편집", f"fav:{BOOKING_RAIL}")],
            *chunk(
                [(f"{'✅' if k in pax else '⬜'} {PAX_LABEL[k]}", f"paxt:{k}") for k in OPTIONAL_PAX],
                2,
            ),
            [(f"{'✅' if ktx_only else '⬜'} KTX만 검색 (ITX·무궁화 빼기)", "ktxonly")],
            [("🔑 코레일 계정 연결·관리", "acct")],
            [HOME],
        ]
        await r.show("\n".join(lines), kb(rows))

    async def btn_paxt(self, r, key):
        u = r.u
        if key in OPTIONAL_PAX:
            pax = u.pax_types()
            pax = [k for k in pax if k != key] if key in pax else pax + [key]
            u.prefs["pax_types"] = [k for k in OPTIONAL_PAX if k in pax]
            u.store.save()
        await self.show_settings(r)

    async def btn_ktxonly(self, r, arg):
        u = r.u
        u.prefs["ktx_only"] = not u.ktx_only()
        u.store.save()
        await self.show_settings(r)

    async def _legacy_srt(self, r, rail_type):
        """통합 전에 받은 SRT 버튼을 누르면 안내한다. 안내했으면 True."""
        if rail_type != "SRT":
            return False
        await r.show(
            INTEGRATION_NOTE,
            kb([[("🚅 열차 예매 (KTX·SRT)", f"book:{BOOKING_RAIL}")], [("⚙️ 설정", "set"), HOME]]),
        )
        return True

    async def btn_fav(self, r, rail_type):
        if await self._legacy_srt(r, rail_type):
            return
        await self.show_fav(r, rail_type)

    async def show_fav(self, r, rail_type, note=None):
        if rail_type not in RAIL_TYPES:
            return await self.show_settings(r)
        u = r.u
        favs = u.stations(rail_type)
        lines = [note, ""] if note else []
        lines += [
            "⭐ 자주 쓰는 역" if rail_type == BOOKING_RAIL else f"⭐ {rail_type} 자주 쓰는 역",
            f"지금: {', '.join(favs)}",
            "",
            "역을 누르면 넣고 뺄 수 있어요. 예매할 때 ⭐ 역이 먼저 나옵니다.",
            "목록에 없는 역은 '➕ 직접 입력'으로 추가하세요.",
        ]
        buttons = [
            (f"⭐ {name}" if name in favs else name, f"favt:{rail_type}:{name}")
            for name in all_stations(rail_type, extra=favs)
        ]
        rows = chunk(buttons, 3) + [
            [("➕ 직접 입력", f"favadd:{rail_type}"), ("↺ 기본값으로", f"favreset:{rail_type}")],
            [("◀ 설정", "set"), HOME],
        ]
        await r.show("\n".join(lines), kb(rows))

    async def btn_favt(self, r, arg):
        rail_type, _, name = arg.partition(":")
        if rail_type not in RAIL_TYPES:
            return await self.show_settings(r)
        u = r.u
        favs = u.stations(rail_type)
        if name in favs:
            if len(favs) == 1:
                return await self.show_fav(r, rail_type, note="⚠️ 최소 1개 역은 남겨 두어야 합니다.")
            favs.remove(name)
        elif valid_station(rail_type, name):
            favs.append(name)
        u.set_stations(rail_type, favs)
        await self.show_fav(r, rail_type)

    async def btn_favadd(self, r, rail_type):
        if rail_type not in RAIL_TYPES:
            return await self.show_settings(r)
        if await self._legacy_srt(r, rail_type):
            return
        r.u.awaiting = ("station", rail_type)
        example = "진부(오대산), 태화강"
        await r.show(
            "➕ 추가할 역 이름을 이 대화창에 보내 주세요.\n"
            f"여러 개는 쉼표로 구분합니다. (예: {example})\n"
            "코레일+ 앱에 나오는 이름 그대로 써 주세요.",
            kb([[("◀ 역 편집으로", f"fav:{rail_type}")]]),
        )

    async def btn_favreset(self, r, rail_type):
        if rail_type not in RAIL_TYPES:
            return await self.show_settings(r)
        r.u.reset_stations(rail_type)
        await self.show_fav(r, rail_type, note="↺ 기본 역으로 되돌렸습니다.")

    async def _on_station_text(self, u, rail_type, text):
        names = [n.strip() for n in re.split(r"[,\s/·]+", text) if n.strip()]
        good = [n for n in names if valid_station(rail_type, n)]
        bad = [n for n in names if n not in good]
        favs = u.stations(rail_type)
        added = [n for n in dict.fromkeys(good) if n not in favs]
        if added:
            u.set_stations(rail_type, favs + added)
        notes = []
        if added:
            notes.append(f"➕ 추가했습니다: {', '.join(added)}")
        if bad:
            why = (
                "SRT가 서지 않는 역입니다"
                if rail_type == "SRT"
                else f"한글 역 이름({MAX_STATION_NAME}자 이내)이어야 합니다"
            )
            notes.append(f"⚠️ 추가하지 못함: {', '.join(bad)} — {why}")
        if not notes:
            notes.append("이미 들어 있는 역입니다.")
        # 입력 대기는 그대로 둬서 이어서 더 보낼 수 있게 한다.
        await self.show_fav(Reply(self, u), rail_type, note="\n".join(notes))

    # --- 계정 -----------------------------------------------------------
    async def btn_acct(self, r, arg):
        await self.show_accounts(r)

    async def show_accounts(self, r, note=None):
        u = r.u
        lines = [note, ""] if note else []
        lines += ["🔑 코레일 계정 (KTX·SRT 통합)", self.account_line(u, BOOKING_RAIL)]
        row = [("🔑 코레일 계정 연결·변경", f"link:{BOOKING_RAIL}")]
        if account_source(u.chat_id, BOOKING_RAIL, u.is_owner) == "bot":
            row.append(("🗑 연결 해제", f"unlink:{BOOKING_RAIL}"))
        rows = [row]
        if Creds.get(u.chat_id, "SRT")[0]:
            lines.append("옛 SRT 계정이 남아 있습니다. 이제 쓰지 않으니 지워도 됩니다.")
            rows.append([("🗑 옛 SRT 계정 지우기", "unlink:SRT")])
        lines += [
            "",
            "코레일 아이디는 멤버십 번호, 이메일, 휴대폰 번호 중 하나입니다.",
            "휴대폰 번호는 010-1234-5678처럼 하이픈(-)을 넣어 주세요.",
            "SRT만 쓰던 분은 코레일+ 앱에서 먼저 코레일 회원(통합회원)으로 가입해 주세요.",
            "",
            "아이디와 비밀번호를 차례로 보내 주시면 로그인을 확인한 뒤 저장합니다.",
            "받은 메시지는 즉시 지우고, 운영PC의 자격증명 저장소에만 보관합니다.",
        ]
        if u.is_owner:
            lines.append("오너는 연결하지 않으면 PC의 srtgo '로그인 설정'(KTX) 계정을 씁니다.")
        rows.append([("◀ 설정", "set"), HOME])
        await r.show("\n".join(lines), kb(rows))

    async def btn_link(self, r, rail_type):
        if rail_type not in RAIL_TYPES:
            return await self.show_accounts(r)
        if await self._legacy_srt(r, rail_type):
            return
        r.u.awaiting = ("link", rail_type, "id", None)
        await r.show(
            "🔑 코레일 계정 연결\n\n"
            "코레일 아이디를 이 대화창에 보내 주세요.\n"
            "멤버십 번호, 이메일, 휴대폰 번호(010-1234-5678처럼 하이픈 포함) 중 하나입니다.\n"
            "받는 즉시 메시지를 지웁니다.",
            kb([[("✖ 취소", "acct")]]),
        )

    async def _on_link_text(self, u, message, waiting, text):
        _, rail_type, field, pending_id = waiting
        # 받는 즉시 지운다 — 대화 기록에 계정 정보가 남지 않도록.
        deleted = await self._delete(message)
        warn = "" if deleted else "\n⚠️ 보낸 메시지를 지우지 못했습니다. 직접 지워 주세요."
        if not text:
            await self._send(u.chat_id, "빈 값입니다. 다시 보내 주세요." + warn)
            return
        if field == "id":
            u.awaiting = ("link", rail_type, "pw", text)
            await self._send(
                u.chat_id,
                f"🔒 이제 {ACCOUNT_NAME[rail_type]} 비밀번호를 보내 주세요. (받는 즉시 지웁니다){warn}",
                kb([[("✖ 취소", "acct")]]),
            )
            return

        u.awaiting = None
        r = Reply(self, u)
        await r.show(f"⏳ {ACCOUNT_NAME[rail_type]} 로그인 확인 중...{warn}")
        try:
            rail = await asyncio.to_thread(open_rail, rail_type, pending_id, text)
        except RailLoginError as ex:
            log.info("%s: %s 계정 연결 실패 (%s)", u.chat_id, rail_type, ex.reason)
            await r.show(
                f"❌ {ACCOUNT_NAME[rail_type]} 로그인 실패: {ex.reason}\n"
                "계정은 저장하지 않았습니다. 아이디·비밀번호를 확인하고 다시 시도해 주세요.",
                kb([[("🔑 다시 입력", f"link:{rail_type}")], [("◀ 계정 관리", "acct"), HOME]]),
            )
            return
        except Exception as ex:
            log.warning("%s: %s 로그인 확인 중 오류: %s", u.chat_id, rail_type, describe_error(ex))
            await r.show(
                f"⚠️ 로그인을 확인하는 중 오류가 났습니다. 계정은 저장하지 않았습니다.\n{describe_error(ex)}",
                kb([[("🔑 다시 시도", f"link:{rail_type}")], [("◀ 계정 관리", "acct"), HOME]]),
            )
            return

        Creds.set(u.chat_id, rail_type, pending_id, text)
        u.rails[rail_type] = rail
        name = account_name(rail)
        log.info("%s: %s 계정 연결", u.chat_id, rail_type)
        await r.show(
            f"✅ {ACCOUNT_NAME[rail_type]} 계정을 연결했습니다{f' ({name}님)' if name else ''}.",
            kb(
                [
                    [("🚅 열차 예매하기", f"book:{BOOKING_RAIL}")],
                    [("◀ 계정 관리", "acct"), HOME],
                ]
            ),
        )

    async def btn_unlink(self, r, rail_type):
        if rail_type not in RAIL_TYPES:
            return await self.show_accounts(r)
        await r.show(
            f"🗑 텔레그램으로 연결한 {ACCOUNT_NAME[rail_type]} 계정을 지울까요?\n"
            "돌고 있는 대기는 다음 로그인 때 계정이 없어 멈춥니다.",
            kb([[("🗑 지우기", f"unlinkok:{rail_type}"), ("◀ 아니오", "acct")]]),
        )

    async def btn_unlinkok(self, r, rail_type):
        if rail_type in RAIL_TYPES:
            Creds.clear(r.u.chat_id, rail_type)
            r.u.rails.pop(rail_type, None)
            log.info("%s: %s 계정 연결 해제", r.u.chat_id, rail_type)
        await self.show_accounts(r, note=f"🗑 {ACCOUNT_NAME.get(rail_type, rail_type)} 계정 연결을 지웠습니다.")

    async def show_login_failed(self, r, ex):
        lines = [f"🔑 {ACCOUNT_NAME.get(ex.rail_type, ex.rail_type)} 로그인 실패: {ex.reason}"]
        if ex.permanent:
            lines.append("계정을 다시 연결해 주세요.")
        else:
            lines.append("잠시 후 다시 시도해 주세요.")
        await r.show(
            "\n".join(lines),
            kb([[("🔑 코레일 계정 연결", f"link:{BOOKING_RAIL}")], [HOME]]),
        )

    # --- 사용자 관리 (오너 전용) -----------------------------------------
    def _who(self, entry, chat_id):
        name = entry.get("name") or chat_id
        handle = entry.get("handle")
        return name + (f" (@{handle})" if handle else "")

    async def btn_users(self, r, arg):
        if not r.u.is_owner:
            return await self.show_home(r)
        await self.show_users(r)

    async def show_users(self, r, note=None):
        users = self.store.data["users"]
        pending = [(k, v) for k, v in users.items() if v.get("status") == "pending"]
        approved = [
            (k, v) for k, v in users.items() if v.get("status") == "approved" and k != self.owner_id
        ]
        denied = [(k, v) for k, v in users.items() if v.get("status") == "denied"]
        lines = [note, ""] if note else []
        lines.append("👥 사용자 관리")
        rows = []
        if pending:
            lines += ["", f"⏳ 승인 대기 {len(pending)}명"]
            for cid, e in pending:
                lines.append(f"• {self._who(e, cid)} · ID {cid}")
                short = (e.get("name") or cid)[:12]
                rows.append([(f"✅ {short} 승인", f"approve:{cid}"), ("🚫 거절", f"deny:{cid}")])
        lines += ["", f"✅ 사용 중 {len(approved)}명"]
        for cid, e in approved:
            ctx = self.contexts.get(cid)
            n = len(ctx.watches) if ctx else len(self.store.data["watches"].get(cid, []))
            accts = f"코레일{'✅' if Creds.get(cid, BOOKING_RAIL)[0] else '⬜'}"
            lines.append(f"• {self._who(e, cid)} · 대기 {n}건 · {accts}")
            rows.append([(f"🚫 {(e.get('name') or cid)[:12]} 사용 해제", f"revoke:{cid}")])
        if denied:
            lines += ["", f"🚫 거절·해제 {len(denied)}명"]
            for cid, e in denied:
                lines.append(f"• {self._who(e, cid)}")
                short = (e.get("name") or cid)[:10]
                rows.append([(f"↩︎ {short} 승인", f"approve:{cid}"), ("🗑 목록에서 지움", f"forget:{cid}")])
        rows.append([("🔄 새로고침", "users"), HOME])
        await r.show("\n".join(lines), kb(rows))

    async def btn_approve(self, r, cid):
        if not r.u.is_owner or not cid or cid == self.owner_id:
            return await self.show_home(r)
        entry = self.store.user(cid)
        self.store.set_user(cid, status="approved")
        log.info("사용자 승인: %s (%s)", cid, entry.get("name"))
        await self._send(
            cid,
            "✅ 사용이 승인되었습니다!\n"
            "먼저 ⚙️ 설정 → 🔑 코레일 계정 연결에서 코레일 계정을 연결해 주세요.\n"
            "KTX와 옛 SRT(수서) 열차 모두 코레일 계정 하나로 예매합니다. "
            "SRT만 쓰던 분은 코레일+ 앱에서 먼저 코레일 회원으로 가입해 주세요.",
            kb([[("🔑 계정 연결", "acct")], [("↩︎ 메뉴 열기", "home")]]),
        )
        await self.show_users(r, note=f"✅ {self._who(entry, cid)} 님을 승인했습니다.")

    async def btn_deny(self, r, cid):
        if not r.u.is_owner or not cid or cid == self.owner_id:
            return await self.show_home(r)
        self.store.set_user(cid, status="denied")
        log.info("사용자 거절: %s", cid)
        await self.show_users(r, note="🚫 거절했습니다.")

    async def btn_revoke(self, r, cid):
        if not r.u.is_owner or not cid or cid == self.owner_id:
            return await self.show_home(r)
        entry = self.store.user(cid)
        await r.show(
            f"🚫 {self._who(entry, cid)} 님의 사용을 해제할까요?\n"
            "돌고 있는 대기를 멈추고, 연결된 계정도 지웁니다.",
            kb([[("🚫 해제", f"revokeok:{cid}"), ("◀ 아니오", "users")]]),
        )

    async def btn_revokeok(self, r, cid):
        if not r.u.is_owner or not cid or cid == self.owner_id:
            return await self.show_home(r)
        self.store.set_user(cid, status="denied")
        for rt in RAIL_TYPES:
            Creds.clear(cid, rt)
        target = self.contexts.get(cid)
        if target is not None:
            for wid in list(target.watches):
                self.stop_watch(target, wid)
            target.rails.clear()
            target.awaiting = None
        self.store.put_watches(cid, [])
        log.info("사용자 해제: %s", cid)
        await self._send(cid, "사용 권한이 해제되었습니다.")
        await self.show_users(r, note="🚫 사용을 해제했습니다.")

    async def btn_forget(self, r, cid):
        if not r.u.is_owner or not cid or cid == self.owner_id:
            return await self.show_home(r)
        self.store.forget_user(cid)
        await self.show_users(r, note="🗑 목록에서 지웠습니다. 다시 말을 걸면 승인 요청이 옵니다.")

    # --- 예매: 역 --------------------------------------------------------
    def _header(self, d):
        name = "열차" if d.rail_type == BOOKING_RAIL else d.rail_type
        parts = [f"{RAIL_ICON.get(d.rail_type, '')} {name} 예매"]
        if d.dep:
            parts.append(f"{d.dep} → {d.arr or '?'}")
        if d.date:
            parts.append(fmt_date(d.date) + (f" {hhmm(d.time)} 이후" if d.time else ""))
        return " · ".join(parts)

    async def restart_booking(self, r):
        await self.show_home(
            r, note="예매 진행 정보가 없습니다 (봇이 재시작됐거나 오래된 버튼). 처음부터 다시 해 주세요."
        )

    async def btn_book(self, r, rail_type):
        if rail_type not in RAIL_TYPES:
            return await self.show_home(r)
        if await self._legacy_srt(r, rail_type):
            return
        u = r.u
        if not account_source(u.chat_id, rail_type, u.is_owner):
            return await r.show(
                "🔑 코레일 계정이 아직 연결되지 않았습니다. 먼저 연결해 주세요.\n"
                "(KTX와 옛 SRT 열차 모두 코레일 계정 하나로 예매합니다)",
                kb([[("🔑 코레일 계정 연결", f"link:{BOOKING_RAIL}")], [HOME]]),
            )
        u.draft = d = Draft(rail_type)
        last = u.last_route(rail_type)
        if last:
            enabled = {"adult", *u.pax_types()}
            counts = {
                k: int(v)
                for k, v in (last.get("counts") or {}).items()
                if k in enabled and isinstance(v, int) and v > 0
            }
            if counts and sum(counts.values()) <= MAX_PASSENGERS:
                d.counts = counts
        await self.show_stations(r, "dep")

    async def show_stations(self, r, kind, show_all=False, note=None):
        u, d = r.u, r.u.draft
        if not d.rail_type or (kind == "arr" and not d.dep):
            return await self.restart_booking(r)
        favs = u.stations(d.rail_type)
        names = all_stations(d.rail_type, extra=favs) if show_all else favs
        if kind == "arr":
            names = [n for n in names if n != d.dep]
        lines = [note, ""] if note else []
        lines += [self._header(d), "", "출발역을 고르세요." if kind == "dep" else "도착역을 고르세요."]
        rows = []
        if kind == "dep" and not show_all:
            last = u.last_route(d.rail_type)
            if last:
                rows.append(
                    [
                        (f"🔁 {last['dep']}→{last['arr']}", "route:last"),
                        (f"🔁 {last['arr']}→{last['dep']}", "route:rev"),
                    ]
                )
        rows += chunk(
            [(f"⭐ {n}" if show_all and n in favs else n, f"{kind}:{n}") for n in names], 3
        )
        if show_all:
            rows.append([("⭐ 자주 쓰는 역만", f"stfew:{kind}")])
        else:
            rows.append([("🔍 전체 역 보기", f"stall:{kind}"), ("⭐ 역 편집", f"fav:{d.rail_type}")])
        rows.append(([("◀ 출발역 다시", "back:dep")] if kind == "arr" else []) + [HOME])
        await r.show("\n".join(lines), kb(rows))

    async def btn_stall(self, r, kind):
        await self.show_stations(r, "arr" if kind == "arr" else "dep", show_all=True)

    async def btn_stfew(self, r, kind):
        await self.show_stations(r, "arr" if kind == "arr" else "dep")

    def _station_ok(self, u, rail_type, name):
        return valid_station(rail_type, name) and (
            name in all_stations(rail_type) or name in u.stations(rail_type)
        )

    async def btn_dep(self, r, name):
        u, d = r.u, r.u.draft
        if not d.rail_type:
            return await self.restart_booking(r)
        if not self._station_ok(u, d.rail_type, name):
            return await self.show_stations(r, "dep", note="⚠️ 모르는 역입니다.")
        d.dep = name
        if d.arr == name:
            d.arr = None
        await self.show_stations(r, "arr")

    async def btn_arr(self, r, name):
        u, d = r.u, r.u.draft
        if not (d.rail_type and d.dep):
            return await self.restart_booking(r)
        if not self._station_ok(u, d.rail_type, name) or name == d.dep:
            return await self.show_stations(r, "arr", note="⚠️ 출발역과 다른 역을 골라 주세요.")
        d.arr = name
        await self.show_dates(r)

    async def btn_route(self, r, which):
        u, d = r.u, r.u.draft
        last = u.last_route(d.rail_type) if d.rail_type else None
        if not last:
            return await self.restart_booking(r)
        d.dep, d.arr = (last["arr"], last["dep"]) if which == "rev" else (last["dep"], last["arr"])
        await self.show_dates(r)

    # --- 예매: 날짜·시각·인원 --------------------------------------------
    @staticmethod
    def booking_days(rail_type):
        """예매 가능한 날짜들 (CLI와 같은 규칙: SRT 30일, KTX 31일, 07시 전엔 하루 덜)."""
        now = kst_now() + timedelta(minutes=10)
        max_days = (30 if rail_type == "SRT" else 31) - (0 if now.hour >= 7 else 1)
        return [(now + timedelta(days=i)).strftime("%Y%m%d") for i in range(max_days + 1)]

    async def show_dates(self, r):
        d = r.u.draft
        if not (d.rail_type and d.dep and d.arr):
            return await self.restart_booking(r)
        days = self.booking_days(d.rail_type)
        today = kst_now().strftime("%Y%m%d")
        tomorrow = (kst_now() + timedelta(days=1)).strftime("%Y%m%d")
        buttons = []
        for day in days:
            label = fmt_date(day)
            if day == today:
                label = "오늘 " + label
            elif day == tomorrow:
                label = "내일 " + label
            buttons.append((label, f"date:{day}"))
        rows = chunk(buttons, 4) + [[("◀ 도착역 다시", "back:arr"), HOME]]
        await r.show(f"{self._header(d)}\n\n출발 날짜를 고르세요.", kb(rows))

    async def btn_date(self, r, day):
        d = r.u.draft
        if not (d.rail_type and d.dep and d.arr):
            return await self.restart_booking(r)
        if day not in self.booking_days(d.rail_type):
            return await self.show_dates(r)
        d.date = day
        d.time = None
        await self.show_times(r)

    async def show_times(self, r):
        d = r.u.draft
        if not (d.rail_type and d.dep and d.arr and d.date):
            return await self.restart_booking(r)
        now = kst_now()
        start = now.hour if d.date == now.strftime("%Y%m%d") else 0
        buttons = [(f"{h:02d}시", f"time:{h:02d}") for h in range(start, 24)]
        rows = chunk(buttons, 6) + [[("◀ 날짜 다시", "back:date"), HOME]]
        await r.show(f"{self._header(d)}\n\n몇 시 이후 열차를 찾을까요?", kb(rows))

    async def btn_time(self, r, hour):
        d = r.u.draft
        if not (d.rail_type and d.dep and d.arr and d.date):
            return await self.restart_booking(r)
        if not (hour.isdigit() and 0 <= int(hour) <= 23):
            return await self.show_times(r)
        d.time = f"{int(hour):02d}0000"
        now = kst_now()
        if d.date == now.strftime("%Y%m%d"):
            # 오늘이면 이미 떠난 열차는 빼고 찾는다.
            d.time = max(d.time, now.strftime("%H%M%S"))
        await self.show_pax(r)

    async def show_pax(self, r, note=None):
        u, d = r.u, r.u.draft
        if not d.searchable():
            return await self.restart_booking(r)
        enabled = ["adult", *u.pax_types()]
        # 설정에서 끈 유형에 인원이 남아 있으면 치운다.
        d.counts = {k: v for k, v in d.counts.items() if k in enabled and v > 0} or {"adult": 1}
        lines = [note, ""] if note else []
        lines += [self._header(d), "", f"인원을 정하세요. (총 {d.total()}명, 최대 {MAX_PASSENGERS}명)"]
        if not u.pax_types():
            lines.append("어린이·경로우대 등은 ⚙️ 설정에서 승객 유형을 켜면 고를 수 있어요.")
        rows = [
            [
                ("➖", f"pax:{k}:-"),
                (f"{PAX_LABEL[k]} {d.counts.get(k, 0)}명", "noop"),
                ("➕", f"pax:{k}:+"),
            ]
            for k in enabled
        ]
        rows.append([("🔎 열차 검색", "search")])
        rows.append([("◀ 시각 다시", "back:time"), HOME])
        await r.show("\n".join(lines), kb(rows))

    async def btn_pax(self, r, arg):
        u, d = r.u, r.u.draft
        if not d.searchable():
            return await self.restart_booking(r)
        key, _, op = arg.partition(":")
        if key in PAX_LABEL and (key == "adult" or key in u.pax_types()):
            n = d.counts.get(key, 0)
            if op == "+" and d.total() < MAX_PASSENGERS:
                d.counts[key] = n + 1
            elif op == "-" and n > 0:
                d.counts[key] = n - 1
        await self.show_pax(r)

    # --- 예매: 검색·열차 선택 --------------------------------------------
    async def _with_rail(self, u, rail_type, fn):
        """대화용 로그인 세션으로 fn(rail) 을 돌린다. 세션이 만료됐으면 한 번 다시 로그인."""
        async with u.lock:
            for attempt in (1, 2):
                rail = u.rails.get(rail_type)
                if rail is None:
                    rail = await asyncio.to_thread(build_rail, rail_type, u.chat_id, u.is_owner)
                    u.rails[rail_type] = rail
                try:
                    return await asyncio.to_thread(fn, rail)
                except Exception as ex:
                    kind = classify(ex)
                    if attempt == 2 or kind not in ("login", "netfunnel"):
                        raise
                    if kind == "login":
                        u.rails.pop(rail_type, None)
                    else:
                        clear_netfunnel(rail)

    async def btn_search(self, r, arg):
        u, d = r.u, r.u.draft
        if not d.searchable():
            return await self.restart_booking(r)
        if d.total() < 1:
            return await self.show_pax(r, note="⚠️ 인원을 1명 이상 정해 주세요.")
        await r.show(f"{self._header(d)}\n\n🔎 열차를 찾는 중...")
        params = search_params(
            d.rail_type, d.dep, d.arr, d.date, d.time, d.total(), u.ktx_only()
        )
        try:
            trains = await self._with_rail(u, d.rail_type, lambda rail: rail.search_train(**params))
        except NoResultsError:
            trains = []
        except RailLoginError as ex:
            return await self.show_login_failed(r, ex)
        except Exception as ex:
            log.warning("%s: 검색 실패: %s", u.chat_id, describe_error(ex))
            return await r.show(
                f"{self._header(d)}\n\n⚠️ 검색 실패: {describe_error(ex)}",
                kb([[("🔄 다시 검색", "search")], [("◀ 시각 다시", "back:time"), HOME]]),
            )
        d.trains = list(trains or [])
        present = {train_key(t) for t in d.trains}
        d.selected = [k for k in d.selected if k in present]
        d.searched_at = time.time()
        u.remember_route(d)
        await self.show_trains(r)

    async def show_trains(self, r, note=None):
        d = r.u.draft
        if not d.searchable():
            return await self.restart_booking(r)
        lines = [note, ""] if note else []
        lines.append(self._header(d) + f" · {d.total()}명")
        if not d.trains:
            lines += ["", "😢 이 조건에 맞는 열차가 없습니다. 시각이나 날짜를 바꿔 보세요."]
            return await r.show(
                "\n".join(lines),
                kb([[("◀ 시각 다시", "back:time"), ("🔄 다시 검색", "search")], [HOME]]),
            )
        chosen = set(d.selected)
        lines += [
            "",
            "감시할 열차를 모두 고르세요 (여러 개 가능).",
            "매진이어도 고르면, 자리가 나는 순간 예매합니다.",
        ]
        if d.searched_at:
            lines.append(f"(좌석 상태는 {ago(time.time() - d.searched_at)} 전 조회 기준)")
        rows = []
        for t in d.trains:
            no, _, dep_time = train_key(t)
            mark = "✅" if train_key(t) in chosen else "⬜"
            rows.append([(f"{mark} {train_line(t, d.rail_type)}", f"tr:{no}:{dep_time}")])
        rows.append([("☑️ 전체 선택/해제", "trall"), ("🔄 다시 검색", "search")])
        rows.append([(f"➡️ 다음 ({len(d.selected)}개 선택됨)", "trok")])
        rows.append([("◀ 인원 다시", "back:pax"), HOME])
        await r.show("\n".join(lines), kb(rows))

    async def btn_tr(self, r, arg):
        d = r.u.draft
        if not d.trains:
            return await self.restart_booking(r)
        no, _, dep_time = arg.partition(":")
        train = d.find_train(no, dep_time)
        if train is None:
            return await self.show_trains(r, note="목록이 바뀌었습니다. 다시 골라 주세요.")
        key = train_key(train)
        if key in d.selected:
            d.selected.remove(key)
        else:
            d.selected.append(key)
        order = {train_key(t): i for i, t in enumerate(d.trains)}
        d.selected.sort(key=lambda k: order.get(k, 0))
        await self.show_trains(r)

    async def btn_trall(self, r, arg):
        d = r.u.draft
        if not d.trains:
            return await self.restart_booking(r)
        keys = [train_key(t) for t in d.trains]
        d.selected = [] if set(keys) <= set(d.selected) else keys
        await self.show_trains(r)

    async def btn_trok(self, r, arg):
        d = r.u.draft
        if not d.trains:
            return await self.restart_booking(r)
        if not d.selected:
            return await self.show_trains(r, note="⚠️ 열차를 1개 이상 골라 주세요.")
        await self.show_seat(r)

    # --- 예매: 좌석·옵션 -------------------------------------------------
    async def show_seat(self, r, note=None):
        u, d = r.u, r.u.draft
        if not (d.searchable() and d.selected):
            return await self.restart_booking(r)
        chosen = set(d.selected)
        titles = [train_title(t) for t in d.trains if train_key(t) in chosen]
        lines = [note, ""] if note else []
        lines += [
            self._header(d) + f" · {d.total()}명",
            f"🎯 {', '.join(titles)}",
            "",
            "좌석 유형과 옵션을 고르고 '대기 시작'을 누르세요.",
            "예약대기: 매진 열차에 대기를 걸어 두면 자리가 났을 때 철도사가 배정하고 문자로 알려 줍니다.",
        ]
        rows = chunk(
            [
                (f"{'🔘' if d.seat_option == key else '⚪'} {label}", f"seat:{key}")
                for label, key in SEAT_OPTIONS
            ],
            2,
        )
        rows.append([(f"{'✅' if d.standby else '⬜'} 매진이면 예약대기 신청", "standby")])
        # 카드는 오너 것만 PC에 있다. 남의 예매가 오너 카드로 결제되면 안 된다.
        if u.is_owner and card_ready():
            rows.append([(f"{'✅' if d.pay else '⬜'} 예매되면 바로 카드 결제", "autopay")])
        rows.append([("▶️ 대기 시작", "go")])
        rows.append([("◀ 열차 다시", "back:trains"), HOME])
        await r.show("\n".join(lines), kb(rows))

    async def btn_seat(self, r, key):
        d = r.u.draft
        if key in SEAT_LABEL:
            d.seat_option = key
        await self.show_seat(r)

    async def btn_standby(self, r, arg):
        r.u.draft.standby = not r.u.draft.standby
        await self.show_seat(r)

    async def btn_autopay(self, r, arg):
        u = r.u
        if u.is_owner and card_ready():
            u.draft.pay = not u.draft.pay
        await self.show_seat(r)

    async def btn_back(self, r, step):
        screens = {
            "dep": lambda: self.show_stations(r, "dep"),
            "arr": lambda: self.show_stations(r, "arr"),
            "date": lambda: self.show_dates(r),
            "time": lambda: self.show_times(r),
            "pax": lambda: self.show_pax(r),
            "trains": lambda: self.show_trains(r),
        }
        show = screens.get(step)
        if show is None:
            return await self.restart_booking(r)
        await show()

    # --- 대기 시작·중지·상황 ---------------------------------------------
    async def btn_go(self, r, arg):
        u, d = r.u, r.u.draft
        if not (d.searchable() and d.selected and d.seat_option in SEAT_LABEL):
            return await self.restart_booking(r)
        if len(u.watches) >= MAX_WATCHES_PER_USER:
            return await r.show(
                f"⚠️ 대기는 한 사람당 {MAX_WATCHES_PER_USER}건까지입니다.\n"
                "필요 없는 대기를 먼저 멈춰 주세요. (지금 고른 조건은 그대로 남아 있어요)",
                kb([[("⏹ 대기 중지", "stop")], [("◀ 돌아가기", "back:trains"), HOME]]),
            )
        if self.active_count() >= MAX_CONCURRENT_WATCHES:
            return await r.show(
                f"⚠️ 지금 전체 대기가 {MAX_CONCURRENT_WATCHES}건이라 더 시작할 수 없습니다.\n"
                "한 PC에서 너무 많이 조회하면 매크로로 차단될 수 있어서입니다. 잠시 후 다시 해 주세요.",
                kb([[("◀ 돌아가기", "back:trains"), HOME]]),
            )
        w = Watch.from_draft(d, ktx_only=u.ktx_only(), pay=d.pay and u.is_owner and card_ready())
        u.draft = Draft()
        self.start_watch(u, w)
        log.info("%s: 대기 시작 %s — %s", u.chat_id, w.id, w.summary().replace("\n", " / "))
        await r.show(
            "👀 대기를 시작했습니다.\n\n"
            f"{w.summary()}\n\n"
            "자리가 나면 바로 예매하고 알려 드릴게요.\n"
            "봇이 재시작돼도 대기는 이어집니다.",
            kb([[("📋 대기 상황", "status"), ("⏹ 이 대기 중지", f"stop:{w.id}")], [HOME]]),
        )

    def start_watch(self, u, w):
        u.watches[w.id] = w
        self.persist(u)
        u.tasks[w.id] = asyncio.create_task(self._run_watch(u, w), name=f"watch-{w.id}")

    def stop_watch(self, u, wid):
        w = u.watches.pop(wid, None)
        task = u.tasks.pop(wid, None)
        if task is not None and not task.done():
            task.cancel()
        self.persist(u)
        if w is not None:
            log.info("%s: 대기 중지 %s", u.chat_id, wid)
        return w

    def persist(self, u):
        self.store.put_watches(u.chat_id, list(u.watches.values()))

    async def btn_stop(self, r, wid):
        u = r.u
        if wid == "all":
            stopped = [self.stop_watch(u, x) for x in list(u.watches)]
            return await self.show_stop(r, note=f"⏹ 대기 {len(stopped)}건을 모두 멈췄습니다.")
        if wid:
            w = self.stop_watch(u, wid)
            note = f"⏹ 멈췄습니다: {w.headline()}" if w else "이미 끝난 대기입니다."
            return await self.show_stop(r, note=note)
        await self.show_stop(r)

    async def show_stop(self, r, note=None):
        u = r.u
        lines = [note, ""] if note else []
        if not u.watches:
            lines.append("진행 중인 대기가 없습니다.")
            return await r.show("\n".join(lines), kb([[HOME]]))
        lines.append("멈출 대기를 고르세요.")
        rows = []
        for i, w in enumerate(u.watches.values(), 1):
            lines.append(f"{number_icon(i)} {w.headline()}")
            rows.append([(f"⏹ {i}번 중지 ({w.dep}→{w.arr} {fmt_date(w.date)})", f"stop:{w.id}")])
        if len(u.watches) > 1:
            rows.append([("⏹ 모두 중지", "stop:all")])
        rows.append([("📋 대기 상황", "status"), HOME])
        await r.show("\n".join(lines), kb(rows))

    def status_text(self, u):
        if not u.watches:
            return "📋 진행 중인 대기가 없습니다.\n'🚄 SRT 예매' 또는 '🚅 KTX 예매'로 시작하세요."
        lines = [f"📋 대기 상황 ({len(u.watches)}건)"]
        for i, w in enumerate(u.watches.values(), 1):
            lines += [
                "",
                f"{number_icon(i)} {w.headline()}",
                f"   🎯 {', '.join(w.titles) or f'{len(w.selected)}개 열차'}",
                f"   💺 {w.options_line()}",
                f"   🔁 {w.tries:,}회 조회 · {hms(time.time() - w.started_at)} 경과"
                + (f" · 👤 {w.account}" if w.account else ""),
                f"   {w.health()}",
            ]
        return "\n".join(lines)

    async def btn_status(self, r, arg):
        await self.show_status(r)

    async def show_status(self, r):
        rows = [[("🔄 새로고침", "status"), ("⏹ 대기 중지", "stop")], [HOME]]
        await r.show(self.status_text(r.u), kb(rows))

    # --- 대기 루프 -------------------------------------------------------
    def _interval(self, rail_type):
        """조회 간격. 같은 철도사 대기가 많으면 늘려서 전체 조회 속도를 묶는다."""
        base = gammavariate(RESERVE_INTERVAL_SHAPE, RESERVE_INTERVAL_SCALE) + RESERVE_INTERVAL_MIN
        same = sum(
            1 for u in self.contexts.values() for w in u.watches.values() if w.rail_type == rail_type
        )
        return base * max(1.0, same / FULL_SPEED_WATCHES)

    def _backoff(self, fails):
        """연속 실패하면 간격을 벌린다. 1초마다 두드리는 것 자체가 매크로로 판정될 짓이다."""
        base = RESERVE_INTERVAL_SHAPE * RESERVE_INTERVAL_SCALE + RESERVE_INTERVAL_MIN
        return min(base * 2 ** min(fails, 10), MAX_BACKOFF_SECONDS)

    def _login_backoff(self, fails):
        return min(LOGIN_BACKOFF_START * 2 ** max(fails - 1, 0), LOGIN_BACKOFF_MAX)

    async def _run_watch(self, u, w):
        try:
            await self._watch(u, w)
        except asyncio.CancelledError:
            # 사용자가 멈췄으면 stop_watch 가 이미 지웠고, 봇이 꺼지는 중이면
            # 저장된 대기를 남겨 둬야 다음에 이어간다. 어느 쪽이든 손대지 않는다.
            raise
        except Exception as ex:
            log.exception("%s: 대기 %s 가 예상 못 한 오류로 멈춤", u.chat_id, w.id)
            await self._send(
                u.chat_id,
                f"⛔ 대기가 멈췄습니다.\n{w.headline()}\n사유: {describe_error(ex)}\n\n"
                "다시 시작해 주세요.",
                kb([[HOME]]),
            )
        self._finish(u, w)

    def _finish(self, u, w):
        if u.watches.get(w.id) is w:
            u.watches.pop(w.id)
            u.tasks.pop(w.id, None)
            self.persist(u)

    async def _failed(self, u, w, message):
        w.note_error(message)
        log.warning("%s: 대기 %s 실패 (%d회째): %s", u.chat_id, w.id, w.error_count, message)
        now = time.time()
        due = (
            now - w.error_since >= ERROR_ALERT_AFTER_SECONDS
            if w.alerted_at is None
            else now - w.alerted_at >= ERROR_REALERT_SECONDS
        )
        if due:
            w.alerted_at = now
            await self._send(
                u.chat_id,
                f"⚠️ {ago(now - w.error_since)}째 조회가 안 되고 있습니다. 멈추지 않고 계속 재시도합니다.\n"
                f"{w.headline()}\n마지막 오류: {message}",
            )

    async def _recovered(self, u, w):
        if w.alerted_at is not None:
            await self._send(u.chat_id, f"✅ 다시 정상적으로 조회하고 있습니다.\n{w.headline()}")
        w.clear_error()

    async def _reserve_problem(self, u, w, count, problem):
        w.note_error(problem)
        log.warning("%s: 대기 %s 예약 거절 (%d회 연속): %s", u.chat_id, w.id, count, problem)
        if count == 3 or (count > 3 and count % 30 == 0):
            await self._send(
                u.chat_id,
                f"⚠️ 자리가 났는데 예약이 계속 거절됩니다 ({count}회). 멈추지 않고 계속 시도합니다.\n"
                f"{w.headline()}\n사유: {problem}",
            )

    async def _reservation_ids(self, rail, rail_type):
        try:
            return {rsv_id(x) for x in await asyncio.to_thread(active_reservations, rail, rail_type)}
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.info("기존 예약 목록을 못 읽었습니다 (무시): %s", describe_error(ex))
            return None

    def can_book(self, train, w):
        """지금 이 열차를 예매 시도할지. 예약대기를 끈 대기는 진짜 좌석만 본다."""
        dep = departs_at(train)
        if dep is not None and dep <= kst_now():
            return False
        if not _is_seat_available(train, w.seat_type(), w.rail_type):
            return False
        if w.standby:
            return True
        return any(seat_flags(train, w.rail_type))

    async def _watch(self, u, w):
        rail = None
        wanted = set(w.selected)
        deadline = w.deadline()
        fails = 0  # 로그인·조회 연속 실패
        reserve_fails = 0  # 예약 시도 연속 거절
        while True:
            # 떠난 열차는 영영 안 잡힌다. 모르고 며칠씩 도는 일이 없도록 끝낸다.
            if deadline and kst_now() > deadline:
                await self._send(
                    u.chat_id,
                    f"🛑 감시하던 열차가 모두 출발해 대기를 끝냅니다.\n{w.headline()}",
                    kb([[HOME]]),
                )
                return

            if rail is None:
                w.phase = "login"
                try:
                    rail = await asyncio.to_thread(build_rail, w.rail_type, u.chat_id, u.is_owner)
                except asyncio.CancelledError:
                    raise
                except RailLoginError as ex:
                    if ex.permanent:
                        # 틀린 비밀번호로 계속 두드리면 계정이 잠긴다. 멈추고 알린다.
                        log.warning("%s: 대기 %s 로그인 거부로 중지: %s", u.chat_id, w.id, ex.reason)
                        await self._send(
                            u.chat_id,
                            f"🔑 {ACCOUNT_NAME[w.rail_type]} 로그인이 거부되어 대기를 멈췄습니다.\n"
                            f"사유: {ex.reason}\n\n{w.headline()}\n"
                            "계정을 다시 연결한 뒤 새로 시작해 주세요. "
                            "(계속 시도하면 계정이 잠길 수 있어 멈춥니다)",
                            kb([[("🔑 코레일 계정 연결", f"link:{BOOKING_RAIL}")], [HOME]]),
                        )
                        return
                    fails += 1
                    await self._failed(u, w, f"로그인 실패: {ex.reason}")
                    await asyncio.sleep(self._login_backoff(fails))
                    continue
                except Exception as ex:
                    fails += 1
                    await self._failed(u, w, f"로그인 실패: {describe_error(ex)}")
                    await asyncio.sleep(self._login_backoff(fails))
                    continue
                w.account = account_name(rail) or w.account
                if w.known_rsv is None:
                    w.known_rsv = await self._reservation_ids(rail, w.rail_type)

            w.phase = "search"
            w.tries += 1
            try:
                trains = await asyncio.to_thread(rail.search_train, **w.params())
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                kind = classify(ex)
                if kind == "benign":  # 결과 없음·혼잡: 오류가 아니다
                    trains = []
                else:
                    fails += 1
                    if kind == "login" or fails % RELOGIN_EVERY_ERRORS == 0:
                        rail = None
                    elif kind == "netfunnel":
                        clear_netfunnel(rail)
                    await self._failed(u, w, describe_error(ex))
                    await asyncio.sleep(self._backoff(fails))
                    continue

            if fails:
                await self._recovered(u, w)
                fails = 0
            elif w.last_error and not reserve_fails:
                w.clear_error()
            w.last_ok_at = time.time()

            problem = None
            for train in trains or []:
                if train_key(train) not in wanted or not self.can_book(train, w):
                    continue
                w.phase = "reserve"
                outcome, why = await self._try_reserve(u, w, rail, train)
                if outcome == "done":
                    return
                if outcome == "relogin":
                    rail = None
                    problem = "예약 중 로그인이 풀려 다시 로그인합니다"
                    break
                problem = why or problem
            w.phase = "search"

            # 자리는 보이는데 예약이 계속 거절되면(매수 초과 등) 간격을 벌린다.
            # 조회 성공과 따로 세야 한다. 안 그러면 매번 초기화돼 1~2초마다 두드린다.
            if problem:
                reserve_fails += 1
                await self._reserve_problem(u, w, reserve_fails, problem)
            elif reserve_fails:
                reserve_fails = 0
                w.clear_error()

            if w.tries % PERSIST_EVERY_TRIES == 0:
                self.persist(u)
            await asyncio.sleep(
                self._backoff(reserve_fails) if problem else self._interval(w.rail_type)
            )

    async def _try_reserve(self, u, w, rail, train):
        """예약 시도는 대기를 멈춰도 끝까지 돌린다.

        서버에 예약이 잡히는 중에 끊으면, 잡혔는지도 모른 채 알림도 없이 끝난다.
        그래서 별도 작업으로 떼어 내고(shield) 결과는 그 작업이 직접 알린다.
        """
        job = asyncio.ensure_future(self._reserve_and_report(u, w, rail, train))
        self._bg.add(job)
        job.add_done_callback(self._bg.discard)
        return await asyncio.shield(job)

    async def _reserve_and_report(self, u, w, rail, train):
        """('done'|'retry'|'relogin', 문제 설명 또는 None)."""
        try:
            try:
                result = await asyncio.to_thread(
                    rail.reserve, train, passengers=w.passengers(), option=w.seat_type()
                )
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                kind = classify(ex)
                if kind == "benign":
                    return "retry", None
                if kind == "login":
                    return "relogin", None
                if kind == "netfunnel":
                    clear_netfunnel(rail)
                    return "retry", None
                # 결과를 모른다. 서버에선 잡혔는데 그 뒤 조회에서 터졌을 수 있다.
                # 그냥 다시 예약하면 중복 예매가 되므로 실제로 잡혔는지 먼저 본다.
                log.warning("%s: 예약 결과 불명, 확인합니다: %s", u.chat_id, describe_error(ex))
                found = await self._find_new_reservation(u, w, rail, train)
                if found is UNKNOWN:
                    await self._send_important(
                        u.chat_id,
                        f"❓ {train_title(train)} 예약을 시도했는데 결과를 확인하지 못했습니다.\n"
                        f"{w.headline()}\n\n"
                        "중복 예매를 막으려고 이 대기를 멈춥니다. 예매내역을 꼭 확인해 주세요.\n"
                        f"({describe_error(ex)})",
                        kb([[("🎫 예매내역", f"rv:{w.rail_type}")], [HOME]]),
                    )
                    return "done", None
                if found is None:
                    return "retry", f"예약 실패: {describe_error(ex)}"
                result = found
            await self._report_success(u, w, rail, train, result)
            return "done", None
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.exception("%s: 예약 처리 중 오류", u.chat_id)
            await self._send_important(
                u.chat_id,
                f"❓ {train_title(train)} 예약 처리 중 오류가 났습니다. 예매내역을 확인해 주세요.\n"
                f"{w.headline()}\n({describe_error(ex)})",
                kb([[("🎫 예매내역", f"rv:{w.rail_type}")], [HOME]]),
            )
            return "done", None

    async def _find_new_reservation(self, u, w, rail, train):
        """방금 시도한 열차의 새 예약. 있으면 그 예약, 없으면 None, 확인 불가면 UNKNOWN."""
        for delay in self.VERIFY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                items = await asyncio.to_thread(active_reservations, rail, w.rail_type)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                log.warning("예약 확인 실패: %s", describe_error(ex))
                if classify(ex) == "login":
                    try:
                        rail = await asyncio.to_thread(
                            build_rail, w.rail_type, u.chat_id, u.is_owner
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                continue
            known = w.known_rsv or set()
            for item in items:
                if same_train(item, train) and rsv_id(item) not in known:
                    return item
            return None
        return UNKNOWN

    async def _report_success(self, u, w, rail, train, result):
        reservation = result
        if isinstance(result, list):
            # 코레일은 예약은 됐는데 번호로 못 찾으면 전체 예약 목록을 돌려준다.
            known = w.known_rsv or set()
            reservation = next(
                (x for x in result if same_train(x, train) and rsv_id(x) not in known), None
            )
        buttons = kb([[("🎫 예매내역", f"rv:{w.rail_type}")], [HOME]])
        if reservation is None:
            log.info("%s: 예약 접수 (상세 불명) %s", u.chat_id, train_title(train))
            await self._send_important(
                u.chat_id,
                f"🎫 예약이 접수됐습니다! {train_title(train)}\n{w.headline()}\n\n"
                "상세 정보를 불러오지 못했습니다. 예매내역에서 확인하고 구입기한 안에 결제해 주세요.",
                buttons,
            )
            return

        waiting = bool(getattr(reservation, "is_waiting", False))
        lines = [
            "⏳ 예약대기 신청 완료!" if waiting else "🎉 예매 성공!",
            w.headline(),
            "",
            describe(reservation),
        ]
        seats = getattr(reservation, "tickets", None)
        if isinstance(seats, list) and seats:
            lines += [f"  · {s}" for s in seats]
        lines.append("")
        if waiting:
            lines.append(
                "자리가 나면 철도사가 배정하고 문자로 알려 줍니다. "
                "배정되면 구입기한 안에 결제해야 합니다."
            )
        elif w.pay and u.is_owner:
            problem = None
            try:
                paid = await asyncio.to_thread(pay_card, rail, reservation)
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                paid, problem = False, describe_error(ex)
            if paid:
                lines.append("💳 카드 결제까지 끝났습니다. 승차권이 발권되었습니다.")
            else:
                lines.append(
                    "💳 자동 결제 실패 — 구입기한 안에 직접 결제해 주세요."
                    + (f"\n({problem})" if problem else "")
                )
        elif u.is_owner:
            lines.append("💳 구입기한 안에 결제해 주세요. 🎫 예매내역에서 카드 결제할 수 있어요.")
        else:
            lines.append(
                f"💳 구입기한 안에 {RAIL_APP[w.rail_type]}에서 결제해 주세요. 안 하면 자동 취소됩니다."
            )
        log.info("%s: 예매 성공 %s — %s", u.chat_id, w.id, describe(reservation))
        await self._send_important(u.chat_id, "\n".join(lines), buttons)

    # --- 예매내역 --------------------------------------------------------
    async def btn_rv(self, r, rail_type):
        await self.load_reservations(r, rail_type)

    async def load_reservations(self, r, rail_type, note=None):
        if rail_type not in RAIL_TYPES:
            return await self.show_home(r)
        if await self._legacy_srt(r, rail_type):
            return
        u = r.u
        if not account_source(u.chat_id, rail_type, u.is_owner):
            return await r.show(
                "🔑 코레일 계정이 아직 연결되지 않았습니다.",
                kb([[("🔑 코레일 계정 연결", f"link:{BOOKING_RAIL}")], [HOME]]),
            )
        await r.show(((note + "\n\n") if note else "") + "🎫 예매내역을 불러오는 중...")
        try:
            items = await self._with_rail(u, rail_type, lambda rail: list_reservations(rail, rail_type))
        except RailLoginError as ex:
            return await self.show_login_failed(r, ex)
        except Exception as ex:
            log.warning("%s: 예매내역 조회 실패: %s", u.chat_id, describe_error(ex))
            return await r.show(
                f"⚠️ 예매내역 조회 실패: {describe_error(ex)}",
                kb([[("🔄 다시 시도", f"rv:{rail_type}")], [HOME]]),
            )
        u.rv_rail, u.rv_items = rail_type, items
        await self.show_reservations(r, note=note)

    async def show_reservations(self, r, note=None):
        u = r.u
        rail_type, items = u.rv_rail, u.rv_items
        if rail_type not in RAIL_TYPES:
            return await self.show_home(r)
        lines = [note, ""] if note else []
        if not items:
            lines.append("🎫 예매내역이 없습니다.")
            return await r.show("\n".join(lines), kb([[("🔄 새로고침", f"rv:{rail_type}")], [HOME]]))
        lines.append(f"🎫 예매내역 ({len(items)}건)")
        rows = []
        for i, item in enumerate(items):
            lines += ["", f"{i + 1}. {item_state(item)}", f"   {describe(item)}"]
            rows.append([(f"{i + 1}. {item_title(item)} · {item_state(item)}", f"rvp:{i}:{item_tag(item)}")])
        lines += ["", "항목을 누르면 결제·취소할 수 있어요."]
        rows.append([("🔄 새로고침", f"rv:{rail_type}"), HOME])
        await r.show("\n".join(lines), kb(rows))

    def _pick(self, u, arg):
        """버튼이 가리키는 예매 항목. 그새 목록이 바뀌었으면 None."""
        idx, _, tag = arg.partition(":")
        if not idx.isdigit() or int(idx) >= len(u.rv_items):
            return None, None
        item = u.rv_items[int(idx)]
        return (item, int(idx)) if item_tag(item) == tag else (None, None)

    async def _stale(self, r):
        if r.u.rv_rail not in RAIL_TYPES:
            return await self.show_home(r, note="예매내역을 다시 열어 주세요.")
        await self.load_reservations(r, r.u.rv_rail, note="목록이 바뀌어 새로 불러왔습니다.")

    async def btn_rvl(self, r, arg):
        await self.show_reservations(r)

    async def btn_rvp(self, r, arg):
        u = r.u
        item, idx = self._pick(u, arg)
        if item is None:
            return await self._stale(r)
        ref = f"{idx}:{item_tag(item)}"
        lines = [item_state(item), describe(item)]
        seats = getattr(item, "tickets", None)
        if isinstance(seats, list) and seats:
            lines += [f"  · {s}" for s in seats]
        rows = []
        is_ticket = getattr(item, "is_ticket", False)
        if u.is_owner and card_ready() and not is_ticket and not getattr(item, "is_waiting", False):
            rows.append([("💳 카드로 결제", f"rvpay:{ref}")])
        rows.append([("↩️ 환불" if is_ticket else "❌ 예약 취소", f"rvx:{ref}")])
        rows.append([("◀ 목록", "rvl"), HOME])
        await r.show("\n".join(lines), kb(rows))

    async def btn_rvpay(self, r, arg):
        u = r.u
        if not (u.is_owner and card_ready()):
            return await self.show_home(r)
        item, idx = self._pick(u, arg)
        if item is None:
            return await self._stale(r)
        ref = f"{idx}:{item_tag(item)}"
        await r.show(
            f"💳 PC에 설정된 카드로 결제할까요?\n\n{describe(item)}",
            kb([[("💳 결제", f"rvpayok:{ref}"), ("◀ 아니오", f"rvp:{ref}")]]),
        )

    async def btn_rvpayok(self, r, arg):
        u = r.u
        if not (u.is_owner and card_ready()):
            return await self.show_home(r)
        item, _ = self._pick(u, arg)
        if item is None:
            return await self._stale(r)
        await r.show("💳 결제 중...")
        rail_type = u.rv_rail
        try:
            ok = await self._with_rail(u, rail_type, lambda rail: pay_card(rail, item))
        except Exception as ex:
            log.warning("결제 실패: %s", describe_error(ex))
            return await self.load_reservations(r, rail_type, note=f"⚠️ 결제 실패: {describe_error(ex)}")
        log.info("%s: 결제 %s — %s", u.chat_id, "완료" if ok else "실패", describe(item))
        await self.load_reservations(
            r, rail_type, note="💳 결제 완료! 승차권이 발권되었습니다." if ok else "⚠️ 결제에 실패했습니다."
        )

    async def btn_rvx(self, r, arg):
        item, idx = self._pick(r.u, arg)
        if item is None:
            return await self._stale(r)
        ref = f"{idx}:{item_tag(item)}"
        kind = "환불" if getattr(item, "is_ticket", False) else "예약 취소"
        await r.show(
            f"정말 {kind}할까요? 되돌릴 수 없습니다.\n\n{describe(item)}",
            kb([[(f"❌ {kind} 확정", f"rvxok:{ref}"), ("◀ 아니오", f"rvp:{ref}")]]),
        )

    async def btn_rvxok(self, r, arg):
        u = r.u
        item, _ = self._pick(u, arg)
        if item is None:
            return await self._stale(r)
        is_ticket = getattr(item, "is_ticket", False)
        kind = "환불" if is_ticket else "예약 취소"
        await r.show(f"⏳ {kind} 처리 중...")
        rail_type = u.rv_rail
        try:
            await self._with_rail(
                u, rail_type, lambda rail: (rail.refund if is_ticket else rail.cancel)(item)
            )
        except Exception as ex:
            log.warning("%s 실패: %s", kind, describe_error(ex))
            return await self.load_reservations(r, rail_type, note=f"⚠️ {kind} 실패: {describe_error(ex)}")
        log.info("%s: %s — %s", u.chat_id, kind, describe(item))
        await self.load_reservations(r, rail_type, note=f"✅ {kind} 완료")

    # --- 시작·종료 -------------------------------------------------------
    async def resume_watches(self):
        """재시작 전에 돌던 대기를 이어간다."""
        for chat_id, items in list(self.store.data["watches"].items()):
            if not self.is_allowed(chat_id):
                self.store.data["watches"].pop(chat_id, None)
                continue
            u = self.context_for(chat_id)
            resumed, expired = [], []
            for raw in items if isinstance(items, list) else []:
                try:
                    w = Watch.from_dict(raw)
                except ValueError as ex:
                    log.warning("저장된 대기를 읽지 못해 버립니다: %s", ex)
                    continue
                if w.id in u.watches:
                    continue
                deadline = w.deadline()
                if deadline and kst_now() > deadline:
                    expired.append(w)
                    continue
                resumed.append(w)
            for w in resumed:
                self.start_watch(u, w)
            self.persist(u)
            lines = []
            if resumed:
                lines += ["🔄 봇이 다시 켜져 대기를 이어갑니다."] + [f"• {w.headline()}" for w in resumed]
            if expired:
                lines += ["", "🛑 꺼져 있는 동안 열차가 출발해 끝난 대기:"] + [
                    f"• {w.headline()}" for w in expired
                ]
            if lines:
                log.info("%s: 대기 %d건 이어감, %d건 만료", chat_id, len(resumed), len(expired))
                await self._send(chat_id, "\n".join(lines).strip(), kb([[("📋 대기 상황", "status")], [HOME]]))
        self.store.save()

    async def _heartbeat(self, app):
        """워치독이 보는 생존 신호. 텔레그램 수신이 멈췄으면 쓰지 않아 재시작되게 한다."""
        while True:
            updater = getattr(app, "updater", None)
            if updater is None or updater.running:
                try:
                    self.heartbeat_path.write_text(str(time.time()))
                except OSError as ex:
                    log.warning("하트비트 기록 실패: %s", ex)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    async def post_init(self, app):
        self.tg = app.bot
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except TelegramError as ex:
            log.warning("명령어 메뉴 등록 실패: %s", ex)
        if self.heartbeat_path is not None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat(app))
        await self.resume_watches()
        log.info("봇 준비 완료")

    async def shutdown(self, app=None):
        """꺼질 때: 대기는 저장해 둔 채 멈춘다 (다음에 켜지면 이어간다)."""
        if self._bg:
            # 예약 처리 중이면 결과 알림까지는 끝내게 잠깐 기다린다.
            await asyncio.wait(set(self._bg), timeout=20)
        tasks = list(self._bg) + [t for u in self.contexts.values() for t in u.tasks.values()]
        if self._heartbeat_task is not None:
            tasks.append(self._heartbeat_task)
            self._heartbeat_task = None
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for u in self.contexts.values():
            u.tasks.clear()
            if u.watches:
                self.persist(u)
        self.store.save()


def build_application(bot, token, base_url=None):
    builder = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)  # 한 사람의 느린 로그인이 다른 사람을 막지 않게
        .post_init(bot.post_init)
        .post_stop(bot.shutdown)
        .post_shutdown(bot.shutdown)
    )
    if base_url:  # 테스트용 가짜 텔레그램 서버
        builder = builder.base_url(base_url)
    app = builder.build()
    app.add_handler(CommandHandler(["start", "menu"], bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("status", bot.cmd_status))
    app.add_handler(CommandHandler("stop", bot.cmd_stop))
    app.add_handler(CommandHandler(["settings", "setting"], bot.cmd_settings))
    app.add_handler(MessageHandler(filters.COMMAND, bot.cmd_unknown))
    app.add_handler(CallbackQueryHandler(bot.on_button))
    app.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, bot.on_text)
    )
    app.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & ~filters.TEXT & ~filters.COMMAND, bot.on_other)
    )
    app.add_error_handler(bot.on_error)
    return app


_logging_ready = False


def setup_logging(home):
    global _logging_ready
    if _logging_ready:
        return
    _logging_ready = True
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        home / "bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    stderr = sys.stderr
    if stderr is not None and getattr(stderr, "isatty", lambda: False)():
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)
    # httpx 는 요청마다 URL 을 INFO 로 남기는데, 그 URL 에 봇 토큰이 들어 있다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def run_bot() -> int:
    """봇을 돌린다. 종료 코드: 0 정상 종료, 2 설정 없음, 3 이미 실행 중."""
    home = data_dir()
    setup_logging(home)
    lock = acquire_lock(home / "bot.lock")
    if lock is None:
        log.error("이 PC에서 봇이 이미 실행 중입니다 (워치독이 띄운 봇일 수 있습니다).")
        return EXIT_ALREADY_RUNNING
    try:
        try:
            token, owner = get_telegram_credentials()
        except Exception as ex:
            log.error("keyring 을 읽지 못했습니다: %s", ex)
            return 2
        if not token or not owner:
            log.error(
                "keyring 에 텔레그램 설정(token/chat_id)이 없습니다. srtgo '텔레그램 설정'을 "
                "한 그 Windows 계정으로 실행하고 있는지 확인하세요."
            )
            return 2
        store = StateStore(home / STATE_FILE)
        migrate_users(store)
        bot = Bot(owner, store)
        bot.heartbeat_path = home / "heartbeat"
        app = build_application(bot, token)
        # CLI 메뉴에서 봇을 두 번 켜면 앞에서 닫힌 이벤트 루프가 남아 있어 실패한다.
        asyncio.set_event_loop(asyncio.new_event_loop())
        log.info("텔레그램 봇 시작 (데이터: %s)", home)
        app.run_polling(allowed_updates=["message", "callback_query"])
        log.info("텔레그램 봇 종료")
        return 0
    finally:
        lock.close()


def main():
    sys.exit(run_bot())


if __name__ == "__main__":
    main()
