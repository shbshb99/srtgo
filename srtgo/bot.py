"""텔레그램 봇으로 예매를 조작한다.

터미널 앞에 붙어 있지 않아도 되도록, CLI가 하던 일(검색 -> 열차 선택 ->
대기 -> 예매 -> 결제)을 전부 텔레그램 인라인 버튼으로 옮긴 것이다. CLI와
가장 크게 다른 점은 오류 처리다: CLI는 오류마다 "계속할까요"로 사람을
기다리지만, 봇은 알리기만 하고 계속 돈다.

여러 사람이 쓴다. 모르는 사람이 말을 걸면 오너에게 승인 요청이 가고,
승인된 사람은 각자 자기 코레일/SRT 계정을 연결해 자기 예매만 한다.
계정은 운영PC의 keyring(OS 자격증명 저장소)에 사용자별로 들어간다.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta

import keyring
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
    SRTNetFunnelError,
)
from .srtgo import (
    RESERVE_INTERVAL_MIN,
    RESERVE_INTERVAL_SCALE,
    RESERVE_INTERVAL_SHAPE,
    STATIONS,
    _is_seat_available,
    get_options,
    get_station,
    get_telegram_credentials,
    login,
    pay_card,
)

RAIL_TYPES = ("SRT", "KTX")
SEAT_OPTIONS = (
    ("일반실 우선", "GENERAL_FIRST"),
    ("일반실만", "GENERAL_ONLY"),
    ("특실 우선", "SPECIAL_FIRST"),
    ("특실만", "SPECIAL_ONLY"),
)
# (키, 표시이름, SRT 클래스, KTX 클래스). 어른 외에는 '예매 옵션 설정'에서 켠 것만 보여준다.
PASSENGER_TYPES = (
    ("adult", "어른/청소년", Adult, AdultPassenger),
    ("child", "어린이", Child, ChildPassenger),
    ("senior", "경로우대", Senior, SeniorPassenger),
    ("disability1to3", "중증장애인", Disability1To3, Disability1To3Passenger),
    ("disability4to6", "경증장애인", Disability4To6, Disability4To6Passenger),
)
MAX_PASSENGERS = 9
DATE_CHOICE_COUNT = 16
# 조회가 계속 실패해도 메시지로 도배하지 않도록, 연속 실패는 이 간격으로만 알린다.
ERROR_NOTIFY_INTERVAL = 20
# 동시에 도는 대기 수. 한 IP에서 여러 명이 동시에 조회하면 매크로로 걸리기 쉽다.
MAX_CONCURRENT_WATCHES = 5

STORE_SERVICE = "srtgo-bot"
CRED_SERVICE = "srtgo-bot-cred"


class UserStore:
    """승인된 사용자와 사용자별 철도 계정. 전부 keyring에 저장한다."""

    @staticmethod
    def load() -> dict:
        raw = keyring.get_password(STORE_SERVICE, "users")
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def save(users: dict) -> None:
        keyring.set_password(STORE_SERVICE, "users", json.dumps(users))

    @classmethod
    def get(cls, chat_id) -> dict:
        return cls.load().get(str(chat_id), {})

    @classmethod
    def set_status(cls, chat_id, status, name=None) -> None:
        users = cls.load()
        entry = users.get(str(chat_id), {})
        entry["status"] = status
        if name:
            entry["name"] = name
        entry["at"] = int(time.time())
        users[str(chat_id)] = entry
        cls.save(users)

    @classmethod
    def approved(cls) -> dict:
        return {k: v for k, v in cls.load().items() if v.get("status") == "approved"}

    @classmethod
    def is_approved(cls, chat_id) -> bool:
        return cls.get(chat_id).get("status") == "approved"

    @staticmethod
    def set_credentials(chat_id, rail_type, user_id, password) -> None:
        keyring.set_password(CRED_SERVICE, f"{chat_id}:{rail_type}:id", user_id)
        keyring.set_password(CRED_SERVICE, f"{chat_id}:{rail_type}:pw", password)

    @staticmethod
    def credentials(chat_id, rail_type):
        return (
            keyring.get_password(CRED_SERVICE, f"{chat_id}:{rail_type}:id"),
            keyring.get_password(CRED_SERVICE, f"{chat_id}:{rail_type}:pw"),
        )

    @staticmethod
    def clear_credentials(chat_id, rail_type) -> None:
        for field in ("id", "pw"):
            try:
                keyring.delete_password(CRED_SERVICE, f"{chat_id}:{rail_type}:{field}")
            except Exception:
                pass


def build_rail(rail_type, chat_id, is_owner):
    """사용자별 계정으로 로그인한다. 오너는 PC에 이미 설정한 계정을 그대로 쓴다."""
    user_id, password = UserStore.credentials(chat_id, rail_type)
    if user_id and password:
        return (SRT if rail_type == "SRT" else Korail)(user_id, password)
    if is_owner:
        return login(rail_type)
    raise RuntimeError(f"{rail_type} 계정이 연결되어 있지 않습니다.")


def has_rail_account(chat_id, rail_type, is_owner):
    user_id, password = UserStore.credentials(chat_id, rail_type)
    if user_id and password:
        return True
    if is_owner:
        return bool(
            keyring.get_password(rail_type, "id")
            and keyring.get_password(rail_type, "pass")
        )
    return False


class Session:
    """한 사용자의 예매 진행 상태."""

    def __init__(self):
        self.rail_type = None
        self.dep = None
        self.arr = None
        self.date = None
        self.time = None
        self.counts = {"adult": 1}
        self.trains = []
        self.selected = []
        self.seat_option = None
        self.pay = False
        self.tries = 0
        self.started_at = None
        self.last_error = None
        self.reservations = []

    @property
    def is_srt(self):
        return self.rail_type == "SRT"

    def passenger_rows(self):
        """어른 + '예매 옵션 설정'에서 켜 둔 승객 유형."""
        enabled = get_options()
        return [
            row for row in PASSENGER_TYPES if row[0] == "adult" or row[0] in enabled
        ]

    def total_count(self):
        return sum(self.counts.values())

    def passengers(self):
        out = []
        for key, _, srt_cls, ktx_cls in self.passenger_rows():
            n = self.counts.get(key, 0)
            if n > 0:
                out.append((srt_cls if self.is_srt else ktx_cls)(n))
        return out

    def search_params(self):
        # CLI와 같게, 검색은 전체 인원을 어른으로 넘기고 실제 구성은 예매할 때 쓴다.
        passenger_cls = Adult if self.is_srt else AdultPassenger
        return {
            "dep": self.dep,
            "arr": self.arr,
            "date": self.date,
            "time": self.time,
            "passengers": [passenger_cls(self.total_count())],
            **(
                {"available_only": False}
                if self.is_srt
                else {
                    "include_no_seats": True,
                    **({"train_type": TrainType.KTX} if "ktx" in get_options() else {}),
                }
            ),
        }

    def seat_type(self):
        return getattr(SeatType if self.is_srt else ReserveOption, self.seat_option)


class UserContext:
    """사용자 한 명의 진행 상태. 대기 작업은 사용자마다 따로 돈다."""

    def __init__(self, chat_id, is_owner):
        self.chat_id = str(chat_id)
        self.is_owner = is_owner
        self.session = Session()
        self.task = None
        self.watching = None
        # 계정 연결 중 무엇을 기다리는지: (rail_type, "id"|"pw", 입력받은 id)
        self.awaiting = None


def train_key(train):
    """검색을 다시 할 때마다 목록 순서가 달라질 수 있으므로 열차 자체로 식별한다."""
    return (
        str(getattr(train, "train_no", "")),
        str(getattr(train, "dep_date", "")),
        str(getattr(train, "dep_time", "")),
    )


def keyboard(rows):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def chunk(items, size):
    return [items[i : i + size] for i in range(0, len(items), size)]


def main_menu(u):
    rows = [
        [("🚄 SRT 예매", "rail:SRT"), ("🚅 KTX 예매", "rail:KTX")],
        [("🎫 SRT 예매내역", "rv:SRT"), ("🎫 KTX 예매내역", "rv:KTX")],
        [("📋 상태 확인", "status"), ("⏹ 중지", "stop")],
        [("🔑 계정 연결", "link:menu")],
    ]
    if u.is_owner:
        rows.append([("👥 사용자 관리", "users:list")])
    return keyboard(rows)


def load_reservations(rail):
    """예약(미결제 포함)과 발권된 승차권을 한 목록으로 합친다."""
    if isinstance(rail, SRT):
        items = list(rail.get_reservations())
        tickets = []
    else:
        items = list(rail.reservations())
        tickets = list(rail.tickets())

    merged = []
    for t in tickets:
        t.is_ticket = True
        merged.append(t)
    for r in items:
        r.is_ticket = bool(getattr(r, "paid", False))
        merged.append(r)
    return merged


class Bot:
    def __init__(self, owner_id: str):
        self.owner_id = str(owner_id)
        self.contexts = {}
        UserStore.set_status(self.owner_id, "approved", "오너")

    # --- 사용자 식별 -------------------------------------------------
    def context_for(self, chat_id) -> UserContext:
        chat_id = str(chat_id)
        if chat_id not in self.contexts:
            self.contexts[chat_id] = UserContext(chat_id, chat_id == self.owner_id)
        return self.contexts[chat_id]

    def active_watches(self):
        return sum(1 for u in self.contexts.values() if u.task and not u.task.done())

    async def _gate(self, update, context):
        """승인된 사용자면 UserContext를, 아니면 None을 준다."""
        chat = update.effective_chat
        if chat is None:
            return None
        chat_id = str(chat.id)
        if chat_id == self.owner_id or UserStore.is_approved(chat_id):
            return self.context_for(chat_id)

        entry = UserStore.get(chat_id)
        if entry.get("status") in ("denied", "pending"):
            return None

        user = update.effective_user
        name = user.full_name if user else "?"
        UserStore.set_status(chat_id, "pending", name)
        await self._request_approval(
            context, chat_id, name, user.username if user else None
        )
        await self._try_send(
            context, chat_id, "사용 승인을 요청했습니다. 관리자가 승인하면 알려드릴게요."
        )
        return None

    async def _request_approval(self, context, chat_id, name, handle):
        who = name + (f" (@{handle})" if handle else "")
        await self._try_send(
            context,
            self.owner_id,
            f"👤 새 사용자가 사용을 요청했습니다.\n{who}\nchat_id: {chat_id}",
            markup=keyboard(
                [[("✅ 승인", f"approve:{chat_id}"), ("🚫 거절", f"deny:{chat_id}")]]
            ),
        )

    # --- 명령 --------------------------------------------------------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = await self._gate(update, context)
        if u is None:
            return
        await update.message.reply_text(
            "srtgo 봇입니다. 무엇을 할까요?", reply_markup=main_menu(u)
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = await self._gate(update, context)
        if u is None:
            return
        await update.message.reply_text(self.status_text(u))

    async def stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = await self._gate(update, context)
        if u is None:
            return
        await update.message.reply_text(self.cancel_task(u))

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """계정 연결 중에만 텍스트를 받는다. 받은 메시지는 즉시 지운다."""
        u = await self._gate(update, context)
        if u is None or u.awaiting is None:
            return

        rail_type, field, pending_id = u.awaiting
        value = (update.message.text or "").strip()

        # 받는 즉시 지운다 — 대화 기록에 계정 정보가 남지 않도록.
        try:
            await update.message.delete()
        except TelegramError:
            pass

        if field == "id":
            u.awaiting = (rail_type, "pw", value)
            await self._try_send(
                context,
                u.chat_id,
                f"{rail_type} 비밀번호를 보내주세요.\n(받는 즉시 메시지를 삭제합니다)",
            )
            return

        u.awaiting = None
        await self._try_send(context, u.chat_id, "확인하는 중...")
        try:
            rail_cls = SRT if rail_type == "SRT" else Korail
            await asyncio.to_thread(rail_cls, pending_id, value)
        except Exception as ex:
            await self._try_send(
                context,
                u.chat_id,
                f"로그인에 실패했습니다. 다시 시도해 주세요.\n{ex}",
                markup=main_menu(u),
            )
            return

        UserStore.set_credentials(u.chat_id, rail_type, pending_id, value)
        await self._try_send(
            context,
            u.chat_id,
            f"✅ {rail_type} 계정을 연결했습니다.",
            markup=main_menu(u),
        )

    # --- 상태 --------------------------------------------------------
    def status_text(self, u):
        s = u.watching
        if s is None or u.task is None or u.task.done():
            return "대기 중인 예매가 없습니다. /start 로 시작하세요."
        elapsed = int(time.time() - s.started_at)
        h, rem = divmod(elapsed, 3600)
        m, sec = divmod(rem, 60)
        lines = [
            f"🚉 {s.rail_type} {s.dep}~{s.arr}",
            f"📅 {s.date[4:6]}/{s.date[6:]} {s.time[:2]}시 이후, {s.total_count()}명",
            f"🎯 {len(s.selected)}개 열차 감시 중",
            f"🔁 {s.tries}회 시도 ({h:02d}:{m:02d}:{sec:02d} 경과)",
        ]
        if s.last_error:
            lines.append(f"⚠️ 마지막 오류: {s.last_error}")
        return "\n".join(lines)

    def cancel_task(self, u):
        if u.task is None or u.task.done():
            return "대기 중인 예매가 없습니다."
        u.task.cancel()
        u.task = None
        u.watching = None
        return "예매 대기를 중지했습니다."

    # --- 버튼 --------------------------------------------------------
    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = await self._gate(update, context)
        if u is None:
            return
        query = update.callback_query
        await query.answer()
        action, _, value = query.data.partition(":")
        handler = getattr(self, f"_on_{action}", None)
        if handler is None:
            return
        await handler(query, context, value, u)

    # --- 사용자 관리 (오너 전용) -------------------------------------
    async def _on_approve(self, query, context, value, u):
        if not u.is_owner:
            return
        UserStore.set_status(value, "approved")
        await query.edit_message_text(f"✅ {value} 승인했습니다.")
        await self._try_send(
            context,
            value,
            "사용 승인되었습니다. 먼저 코레일/SRT 계정을 연결해 주세요.",
            markup=main_menu(self.context_for(value)),
        )

    async def _on_deny(self, query, context, value, u):
        if not u.is_owner:
            return
        UserStore.set_status(value, "denied")
        await query.edit_message_text(f"🚫 {value} 거절했습니다.")

    async def _on_users(self, query, context, value, u):
        if not u.is_owner:
            return
        action, _, target = value.partition(":")
        if action == "revoke" and target:
            UserStore.set_status(target, "denied")
            for rail_type in RAIL_TYPES:
                UserStore.clear_credentials(target, rail_type)
            ctx = self.contexts.get(str(target))
            if ctx:
                self.cancel_task(ctx)

        others = {
            uid: v for uid, v in UserStore.approved().items() if uid != self.owner_id
        }
        rows = [
            [(v.get("name", uid), "noop"), ("🚫 해제", f"users:revoke:{uid}")]
            for uid, v in others.items()
        ]
        rows.append([("↩︎ 처음으로", "home")])
        await query.edit_message_text(
            f"👥 승인된 사용자 {len(others)}명\n해제하면 연결된 계정도 함께 지웁니다.",
            reply_markup=keyboard(rows),
        )

    # --- 계정 연결 ---------------------------------------------------
    async def _on_link(self, query, context, value, u):
        if value == "menu":
            rows = []
            for rail_type in RAIL_TYPES:
                mark = (
                    "✅" if has_rail_account(u.chat_id, rail_type, u.is_owner) else "⬜"
                )
                rows.append([(f"{mark} {rail_type} 계정 연결", f"link:{rail_type}")])
            rows.append([("↩︎ 처음으로", "home")])
            await query.edit_message_text(
                "연결할 계정을 고르세요.\n"
                "아이디와 비밀번호를 순서대로 보내주시면 됩니다. "
                "받는 즉시 메시지는 삭제하고, 운영PC의 자격증명 저장소에만 보관합니다.",
                reply_markup=keyboard(rows),
            )
            return

        u.awaiting = (value, "id", None)
        await query.edit_message_text(
            f"{value} 아이디(멤버십 번호·이메일·전화번호)를 보내주세요."
        )

    # --- 예매 흐름 ---------------------------------------------------
    async def _on_rail(self, query, context, value, u):
        if not has_rail_account(u.chat_id, value, u.is_owner):
            await query.edit_message_text(
                f"{value} 계정이 연결되어 있지 않습니다.\n'계정 연결'에서 먼저 등록해 주세요.",
                reply_markup=main_menu(u),
            )
            return
        u.session = Session()
        u.session.rail_type = value
        await self._show_stations(query, u, "dep", all_stations=False)

    def _station_markup(self, u, kind, all_stations, exclude=None):
        rail_type = u.session.rail_type
        if all_stations:
            names = STATIONS[rail_type]
            toggle = ("⭐ 자주 쓰는 역만", f"stn:{kind}:few")
        else:
            _, names = get_station(rail_type)
            toggle = ("🔍 전체 역 보기", f"stn:{kind}:all")
        options = [(st, f"{kind}:{st}") for st in names if st != exclude]
        return keyboard(chunk(options, 3) + [[toggle], [("↩︎ 처음으로", "home")]])

    async def _show_stations(self, query, u, kind, all_stations):
        s = u.session
        if kind == "dep":
            text = f"{s.rail_type} 출발역을 고르세요."
            exclude = None
        else:
            text = f"출발: {s.dep}\n도착역을 고르세요."
            exclude = s.dep
        await query.edit_message_text(
            text, reply_markup=self._station_markup(u, kind, all_stations, exclude)
        )

    async def _on_stn(self, query, context, value, u):
        kind, _, mode = value.partition(":")
        await self._show_stations(query, u, kind, all_stations=(mode == "all"))

    async def _on_dep(self, query, context, value, u):
        u.session.dep = value
        await self._show_stations(query, u, "arr", all_stations=False)

    async def _on_arr(self, query, context, value, u):
        s = u.session
        s.arr = value
        now = datetime.now() + timedelta(minutes=10)
        max_days = (30 if s.is_srt else 31) - (0 if now.hour >= 7 else 1)
        days = [
            (
                (now + timedelta(days=i)).strftime("%m/%d(%a)"),
                f"date:{(now + timedelta(days=i)).strftime('%Y%m%d')}",
            )
            for i in range(min(DATE_CHOICE_COUNT, max_days + 1))
        ]
        await query.edit_message_text(
            f"{s.dep} → {value}\n출발 날짜를 고르세요.",
            reply_markup=keyboard(chunk(days, 4) + [[("↩︎ 처음으로", "home")]]),
        )

    async def _on_date(self, query, context, value, u):
        u.session.date = value
        hours = [(f"{h:02d}시", f"time:{h:02d}0000") for h in range(24)]
        await query.edit_message_text(
            f"{value[4:6]}/{value[6:]} 출발\n이 시각 이후로 검색합니다.",
            reply_markup=keyboard(chunk(hours, 6) + [[("↩︎ 처음으로", "home")]]),
        )

    async def _on_time(self, query, context, value, u):
        u.session.time = value
        await self._show_passengers(query, u)

    async def _show_passengers(self, query, u):
        s = u.session
        rows = []
        for key, label, _, _ in s.passenger_rows():
            n = s.counts.get(key, 0)
            rows.append(
                [
                    ("➖", f"pax:{key}:-"),
                    (f"{label} {n}명", "noop"),
                    ("➕", f"pax:{key}:+"),
                ]
            )
        rows.append([("✔️ 검색하기", "pax:done:0"), ("↩︎ 처음으로", "home")])
        await query.edit_message_text(
            f"{s.time[:2]}시 이후 · 총 {s.total_count()}명\n승객 수를 정하세요.",
            reply_markup=keyboard(rows),
        )

    async def _on_noop(self, query, context, value, u):
        pass

    async def _on_pax(self, query, context, value, u):
        s = u.session
        key, _, op = value.partition(":")
        if key == "done":
            if s.total_count() < 1:
                return
            await query.edit_message_text("열차를 검색하고 있습니다...")
            await self._search(query, context, u)
            return

        n = s.counts.get(key, 0)
        if op == "+" and s.total_count() < MAX_PASSENGERS:
            s.counts[key] = n + 1
        elif op == "-" and n > 0:
            s.counts[key] = n - 1
        else:
            return
        await self._show_passengers(query, u)

    async def _search(self, query, context, u):
        s = u.session
        try:
            rail = await asyncio.to_thread(
                build_rail, s.rail_type, u.chat_id, u.is_owner
            )
            trains = await asyncio.to_thread(rail.search_train, **s.search_params())
        except NoResultsError:
            trains = []
        except Exception as ex:
            await query.edit_message_text(f"검색 실패: {ex}", reply_markup=main_menu(u))
            return

        if not trains:
            await query.edit_message_text(
                "해당 조건에 열차가 없습니다.", reply_markup=main_menu(u)
            )
            return

        s.trains = trains
        s.selected = []
        await query.edit_message_text(
            self._train_list_text(u), reply_markup=self._train_list_markup(u)
        )

    def _train_list_text(self, u):
        s = u.session
        return (
            f"🚉 {s.rail_type} {s.dep} → {s.arr}\n"
            f"📅 {s.date[4:6]}/{s.date[6:]} {s.time[:2]}시 이후\n\n"
            "감시할 열차를 고르고 '선택 완료'를 누르세요."
        )

    def _train_list_markup(self, u):
        s = u.session
        selected_keys = set(s.selected)
        rows = []
        for i, train in enumerate(s.trains):
            mark = "✅" if train_key(train) in selected_keys else "⬜"
            rows.append([(f"{mark} {train}"[:60], f"toggle:{i}")])
        rows.append([("✔️ 선택 완료", "seatmenu"), ("↩︎ 처음으로", "home")])
        return keyboard(rows)

    async def _on_toggle(self, query, context, value, u):
        s = u.session
        key = train_key(s.trains[int(value)])
        if key in s.selected:
            s.selected.remove(key)
        else:
            s.selected.append(key)
        await query.edit_message_reply_markup(reply_markup=self._train_list_markup(u))

    async def _on_seatmenu(self, query, context, value, u):
        if not u.session.selected:
            return
        await query.edit_message_text(
            "좌석 유형을 고르세요.",
            reply_markup=keyboard(
                chunk([(label, f"seat:{key}") for label, key in SEAT_OPTIONS], 2)
                + [[("↩︎ 처음으로", "home")]]
            ),
        )

    async def _on_seat(self, query, context, value, u):
        u.session.seat_option = value
        # 카드는 오너 것만 PC에 있다. 남의 카드로 결제되면 안 되므로 오너에게만 묻는다.
        if not (u.is_owner and keyring.get_password("card", "ok")):
            await self._begin(query, context, u)
            return
        await query.edit_message_text(
            "예매되면 카드로 바로 결제할까요?",
            reply_markup=keyboard([[("✅ 예", "pay:1"), ("❌ 아니오", "pay:0")]]),
        )

    async def _on_pay(self, query, context, value, u):
        u.session.pay = value == "1"
        await self._begin(query, context, u)

    async def _on_home(self, query, context, value, u):
        # 돌고 있는 대기는 건드리지 않는다. 중지는 '⏹ 중지'로만.
        await query.edit_message_text("무엇을 할까요?", reply_markup=main_menu(u))

    async def _on_status(self, query, context, value, u):
        await query.edit_message_text(self.status_text(u), reply_markup=main_menu(u))

    async def _on_stop(self, query, context, value, u):
        await query.edit_message_text(self.cancel_task(u), reply_markup=main_menu(u))

    # --- 예매 내역 ---------------------------------------------------
    async def _on_rv(self, query, context, value, u):
        if not has_rail_account(u.chat_id, value, u.is_owner):
            await query.edit_message_text(
                f"{value} 계정이 연결되어 있지 않습니다.", reply_markup=main_menu(u)
            )
            return
        await query.edit_message_text("예매 내역을 불러오는 중...")
        try:
            rail = await asyncio.to_thread(build_rail, value, u.chat_id, u.is_owner)
            items = await asyncio.to_thread(load_reservations, rail)
        except Exception as ex:
            await query.edit_message_text(f"조회 실패: {ex}", reply_markup=main_menu(u))
            return

        u.session.rail_type = value
        u.session.reservations = items
        if not items:
            await query.edit_message_text(
                f"{value} 예매 내역이 없습니다.", reply_markup=main_menu(u)
            )
            return

        rows = [[(f"{item}"[:60], f"rvpick:{i}")] for i, item in enumerate(items)]
        rows.append([("🔄 새로고침", f"rv:{value}"), ("↩︎ 처음으로", "home")])
        await query.edit_message_text(
            f"🎫 {value} 예매 내역", reply_markup=keyboard(rows)
        )

    async def _on_rvpick(self, query, context, value, u):
        idx = int(value)
        item = u.session.reservations[idx]
        rows = []
        # 발권 전 + 대기 아님 = 결제 가능. 카드는 오너만.
        if u.is_owner and not item.is_ticket and not getattr(item, "is_waiting", False):
            rows.append([("💳 결제하기", f"rvpay:{idx}")])
        rows.append([("❌ 취소/환불", f"rvcancel:{idx}")])
        rows.append([("↩︎ 목록으로", f"rv:{u.session.rail_type}")])
        await query.edit_message_text(f"{item}", reply_markup=keyboard(rows))

    async def _on_rvpay(self, query, context, value, u):
        if not u.is_owner:
            return
        item = u.session.reservations[int(value)]
        if not keyring.get_password("card", "ok"):
            await query.edit_message_text(
                "카드 정보가 없습니다. PC에서 '카드 설정'을 먼저 해주세요.",
                reply_markup=main_menu(u),
            )
            return
        await query.edit_message_text("결제 중...")
        try:
            rail = await asyncio.to_thread(
                build_rail, u.session.rail_type, u.chat_id, u.is_owner
            )
            ok = await asyncio.to_thread(pay_card, rail, item)
        except Exception as ex:
            await query.edit_message_text(f"결제 실패: {ex}", reply_markup=main_menu(u))
            return
        await query.edit_message_text(
            "💳 결제 완료" if ok else "결제에 실패했습니다.", reply_markup=main_menu(u)
        )

    async def _on_rvcancel(self, query, context, value, u):
        idx = int(value)
        item = u.session.reservations[idx]
        kind = "환불" if item.is_ticket else "예약 취소"
        await query.edit_message_text(
            f"{item}\n\n정말 {kind}할까요? 되돌릴 수 없습니다.",
            reply_markup=keyboard(
                [
                    [(f"❌ {kind} 확정", f"rvdo:{idx}")],
                    [("↩︎ 아니오", f"rvpick:{idx}")],
                ]
            ),
        )

    async def _on_rvdo(self, query, context, value, u):
        item = u.session.reservations[int(value)]
        await query.edit_message_text("처리 중...")
        try:
            rail = await asyncio.to_thread(
                build_rail, u.session.rail_type, u.chat_id, u.is_owner
            )
            action = rail.refund if item.is_ticket else rail.cancel
            await asyncio.to_thread(action, item)
        except Exception as ex:
            await query.edit_message_text(f"실패: {ex}", reply_markup=main_menu(u))
            return
        await query.edit_message_text(
            "처리했습니다.",
            reply_markup=keyboard(
                [
                    [("🎫 목록 새로고침", f"rv:{u.session.rail_type}")],
                    [("↩︎ 처음으로", "home")],
                ]
            ),
        )

    # --- 대기 루프 ---------------------------------------------------
    async def _begin(self, query, context, u):
        s = u.session
        if u.task and not u.task.done():
            u.task.cancel()
        elif self.active_watches() >= MAX_CONCURRENT_WATCHES:
            await query.edit_message_text(
                f"지금 대기가 {MAX_CONCURRENT_WATCHES}건이라 더 시작할 수 없습니다.\n"
                "잠시 후 다시 시도해 주세요.",
                reply_markup=main_menu(u),
            )
            return
        s.tries = 0
        s.started_at = time.time()
        s.last_error = None
        u.watching = s
        u.task = asyncio.create_task(self._reserve_loop(context, u, s))
        await query.edit_message_text(
            f"{len(s.selected)}개 열차를 감시합니다.\n"
            "자리가 나면 바로 예매하고 알려드립니다.\n\n"
            "/status 로 진행 상황, /stop 으로 중지할 수 있습니다."
        )

    async def _reserve_loop(self, context, u, s):
        rail = await asyncio.to_thread(build_rail, s.rail_type, u.chat_id, u.is_owner)
        params = s.search_params()
        wanted = set(s.selected)
        errors_since_notify = 0

        while True:
            try:
                s.tries += 1
                trains = await asyncio.to_thread(rail.search_train, **params)
                for train in trains:
                    if train_key(train) not in wanted:
                        continue
                    if not _is_seat_available(train, s.seat_type(), s.rail_type):
                        continue
                    await self._reserve(context, rail, train, u, s)
                    return
                errors_since_notify = 0
                await asyncio.sleep(self._interval())

            except asyncio.CancelledError:
                raise
            except NoResultsError:
                await asyncio.sleep(self._interval())
            except (NeedToLoginError, SRTNetFunnelError) as ex:
                s.last_error = str(ex)
                rail = await asyncio.to_thread(
                    build_rail, s.rail_type, u.chat_id, u.is_owner
                )
                await asyncio.sleep(self._interval())
            except Exception as ex:
                s.last_error = f"{type(ex).__name__}: {ex}"
                errors_since_notify += 1
                # CLI와 달리 사람을 기다리지 않는다. 계속 재시도하되 가끔만 알린다.
                if (
                    errors_since_notify == 1
                    or errors_since_notify % ERROR_NOTIFY_INTERVAL == 0
                ):
                    await self._try_send(
                        context, u.chat_id, f"⚠️ 계속 재시도 중입니다.\n{s.last_error}"
                    )
                if isinstance(ex, (SRTError, KorailError)):
                    rail = await asyncio.to_thread(
                        build_rail, s.rail_type, u.chat_id, u.is_owner
                    )
                await asyncio.sleep(self._interval())

    async def _reserve(self, context, rail, train, u, s):
        reservation = await asyncio.to_thread(
            rail.reserve, train, passengers=s.passengers(), option=s.seat_type()
        )
        msg = f"🎫 예매 성공!\n{reservation}"
        tickets = getattr(reservation, "tickets", None)
        if tickets:
            msg += "\n" + "\n".join(map(str, tickets))

        if s.pay and u.is_owner and not reservation.is_waiting:
            paid = await asyncio.to_thread(pay_card, rail, reservation)
            msg += (
                "\n\n💳 결제 완료" if paid else "\n\n💳 결제 실패 — 직접 결제해 주세요."
            )
        elif not u.is_owner:
            msg += "\n\n💳 결제는 코레일 앱에서 직접 해주세요."

        await self._try_send(context, u.chat_id, msg)

    async def _try_send(self, context, chat_id, text, markup=None):
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=markup
            )
        except TelegramError:
            pass

    @staticmethod
    def _interval():
        from random import gammavariate

        return (
            gammavariate(RESERVE_INTERVAL_SHAPE, RESERVE_INTERVAL_SCALE)
            + RESERVE_INTERVAL_MIN
        )


def main():
    token, chat_id = get_telegram_credentials()
    if not token or not chat_id:
        print("텔레그램 설정이 없습니다. srtgo 에서 '텔레그램 설정'을 먼저 해주세요.")
        return

    bot = Bot(chat_id)
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("status", bot.status))
    app.add_handler(CommandHandler("stop", bot.stop))
    app.add_handler(CallbackQueryHandler(bot.on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))

    print("텔레그램 봇을 시작합니다. 텔레그램에서 /start 를 보내세요. (Ctrl-C 로 종료)")
    app.run_polling()


if __name__ == "__main__":
    main()
