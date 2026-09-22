"""텔레그램 봇으로 예매를 조작한다.

터미널 앞에 붙어 있지 않아도 되도록, CLI가 하던 일(검색 -> 열차 선택 ->
대기 -> 예매 -> 결제)을 전부 텔레그램 인라인 버튼으로 옮긴 것이다. CLI와
가장 크게 다른 점은 오류 처리다: CLI는 오류마다 "계속할까요"로 사람을
기다리지만, 봇은 알리기만 하고 계속 돈다.
"""

import asyncio
import time
from datetime import datetime, timedelta

import keyring
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .ktx import (
    AdultPassenger,
    KorailError,
    NeedToLoginError,
    NoResultsError,
    ReserveOption,
    TrainType,
)
from .srt import Adult, SeatType, SRTError, SRTNetFunnelError
from .srtgo import (
    RESERVE_INTERVAL_MIN,
    RESERVE_INTERVAL_SCALE,
    RESERVE_INTERVAL_SHAPE,
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
DATE_CHOICE_COUNT = 16
# 조회가 계속 실패해도 메시지로 도배하지 않도록, 연속 실패는 이 간격으로만 알린다.
ERROR_NOTIFY_INTERVAL = 20


class Session:
    """한 사용자의 예매 진행 상태."""

    def __init__(self):
        self.rail_type = None
        self.dep = None
        self.arr = None
        self.date = None
        self.time = None
        self.adult = 1
        self.trains = []
        self.selected = []
        self.seat_option = None
        self.pay = False
        self.task = None
        self.tries = 0
        self.started_at = None
        self.last_error = None

    @property
    def is_srt(self):
        return self.rail_type == "SRT"

    def search_params(self):
        options = get_options()
        passenger_cls = Adult if self.is_srt else AdultPassenger
        return {
            "dep": self.dep,
            "arr": self.arr,
            "date": self.date,
            "time": self.time,
            "passengers": [passenger_cls(self.adult)],
            **(
                {"available_only": False}
                if self.is_srt
                else {
                    "include_no_seats": True,
                    **({"train_type": TrainType.KTX} if "ktx" in options else {}),
                }
            ),
        }

    def passengers(self):
        return [(Adult if self.is_srt else AdultPassenger)(self.adult)]

    def seat_type(self):
        return getattr(SeatType if self.is_srt else ReserveOption, self.seat_option)


def credentials_ready(rail_type: str) -> bool:
    """CLI의 login()은 정보가 없으면 터미널로 되묻는다 — 봇에서는 그 전에 걸러낸다."""
    return bool(
        keyring.get_password(rail_type, "id") and keyring.get_password(rail_type, "pass")
    )


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


def main_menu():
    return keyboard(
        [
            [("🚄 SRT 예매", "rail:SRT"), ("🚅 KTX 예매", "rail:KTX")],
            [("📋 상태 확인", "status"), ("⏹ 중지", "stop")],
        ]
    )


class Bot:
    def __init__(self, chat_id: str):
        self.chat_id = str(chat_id)
        self.session = Session()

    def authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and str(chat.id) == self.chat_id

    async def send(self, context, text, markup=None):
        await context.bot.send_message(
            chat_id=self.chat_id, text=text, reply_markup=markup
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.authorized(update):
            return
        await update.message.reply_text(
            "srtgo 봇입니다. 무엇을 할까요?", reply_markup=main_menu()
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.authorized(update):
            return
        await update.message.reply_text(self.status_text())

    async def stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.authorized(update):
            return
        await update.message.reply_text(self.cancel_task())

    def status_text(self):
        s = self.session
        if s.task is None or s.task.done():
            return "대기 중인 예매가 없습니다. /start 로 시작하세요."
        elapsed = int(time.time() - s.started_at)
        h, rem = divmod(elapsed, 3600)
        m, sec = divmod(rem, 60)
        lines = [
            f"🚉 {s.rail_type} {s.dep}~{s.arr}",
            f"📅 {s.date[4:6]}/{s.date[6:]} {s.time[:2]}시 이후, 어른 {s.adult}명",
            f"🎯 {len(s.selected)}개 열차 감시 중",
            f"🔁 {s.tries}회 시도 ({h:02d}:{m:02d}:{sec:02d} 경과)",
        ]
        if s.last_error:
            lines.append(f"⚠️ 마지막 오류: {s.last_error}")
        return "\n".join(lines)

    def cancel_task(self):
        s = self.session
        if s.task is None or s.task.done():
            return "대기 중인 예매가 없습니다."
        s.task.cancel()
        s.task = None
        return "예매 대기를 중지했습니다."

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.authorized(update):
            return
        query = update.callback_query
        await query.answer()
        action, _, value = query.data.partition(":")
        handler = getattr(self, f"_on_{action}", None)
        if handler is None:
            return
        await handler(query, context, value)

    async def _on_rail(self, query, context, value):
        if not credentials_ready(value):
            await query.edit_message_text(
                f"{value} 로그인 정보가 없습니다.\n"
                "PC에서 srtgo 를 실행해 '로그인 설정'을 먼저 해주세요.",
                reply_markup=main_menu(),
            )
            return
        self.session = Session()
        self.session.rail_type = value
        _, chosen = get_station(value)
        await query.edit_message_text(
            f"{value} 출발역을 고르세요.",
            reply_markup=keyboard(
                chunk([(st, f"dep:{st}") for st in chosen], 3)
                + [[("↩︎ 처음으로", "home")]]
            ),
        )

    async def _on_dep(self, query, context, value):
        self.session.dep = value
        _, chosen = get_station(self.session.rail_type)
        options = [(st, f"arr:{st}") for st in chosen if st != value]
        await query.edit_message_text(
            f"출발: {value}\n도착역을 고르세요.",
            reply_markup=keyboard(chunk(options, 3) + [[("↩︎ 처음으로", "home")]]),
        )

    async def _on_arr(self, query, context, value):
        self.session.arr = value
        now = datetime.now() + timedelta(minutes=10)
        max_days = (30 if self.session.is_srt else 31) - (0 if now.hour >= 7 else 1)
        days = [
            (
                (now + timedelta(days=i)).strftime("%m/%d(%a)"),
                f"date:{(now + timedelta(days=i)).strftime('%Y%m%d')}",
            )
            for i in range(min(DATE_CHOICE_COUNT, max_days + 1))
        ]
        await query.edit_message_text(
            f"{self.session.dep} → {value}\n출발 날짜를 고르세요.",
            reply_markup=keyboard(chunk(days, 4) + [[("↩︎ 처음으로", "home")]]),
        )

    async def _on_date(self, query, context, value):
        self.session.date = value
        hours = [(f"{h:02d}시", f"time:{h:02d}0000") for h in range(24)]
        await query.edit_message_text(
            f"{value[4:6]}/{value[6:]} 출발\n이 시각 이후로 검색합니다.",
            reply_markup=keyboard(chunk(hours, 6) + [[("↩︎ 처음으로", "home")]]),
        )

    async def _on_time(self, query, context, value):
        self.session.time = value
        counts = [(f"{n}명", f"adult:{n}") for n in range(1, 10)]
        await query.edit_message_text(
            f"{value[:2]}시 이후\n어른 승객 수를 고르세요.",
            reply_markup=keyboard(chunk(counts, 5) + [[("↩︎ 처음으로", "home")]]),
        )

    async def _on_adult(self, query, context, value):
        self.session.adult = int(value)
        await query.edit_message_text("열차를 검색하고 있습니다...")
        await self._search(query, context)

    async def _search(self, query, context):
        s = self.session
        rail = await asyncio.to_thread(login, s.rail_type)
        try:
            trains = await asyncio.to_thread(rail.search_train, **s.search_params())
        except NoResultsError:
            trains = []
        except (SRTError, KorailError) as ex:
            await query.edit_message_text(
                f"검색 실패: {ex}", reply_markup=main_menu()
            )
            return

        if not trains:
            await query.edit_message_text(
                "해당 조건에 열차가 없습니다.", reply_markup=main_menu()
            )
            return

        s.trains = trains
        s.selected = []
        await query.edit_message_text(
            self._train_list_text(), reply_markup=self._train_list_markup()
        )

    def _train_list_text(self):
        s = self.session
        return (
            f"🚉 {s.rail_type} {s.dep} → {s.arr}\n"
            f"📅 {s.date[4:6]}/{s.date[6:]} {s.time[:2]}시 이후\n\n"
            "감시할 열차를 고르고 '선택 완료'를 누르세요."
        )

    def _train_list_markup(self):
        s = self.session
        selected_keys = set(s.selected)
        rows = []
        for i, train in enumerate(s.trains):
            mark = "✅" if train_key(train) in selected_keys else "⬜"
            rows.append([(f"{mark} {train}"[:60], f"toggle:{i}")])
        rows.append([("✔️ 선택 완료", "seatmenu"), ("↩︎ 처음으로", "home")])
        return keyboard(rows)

    async def _on_toggle(self, query, context, value):
        s = self.session
        key = train_key(s.trains[int(value)])
        if key in s.selected:
            s.selected.remove(key)
        else:
            s.selected.append(key)
        await query.edit_message_reply_markup(reply_markup=self._train_list_markup())

    async def _on_seatmenu(self, query, context, value):
        if not self.session.selected:
            await query.answer()
            return
        await query.edit_message_text(
            "좌석 유형을 고르세요.",
            reply_markup=keyboard(
                chunk([(label, f"seat:{key}") for label, key in SEAT_OPTIONS], 2)
                + [[("↩︎ 처음으로", "home")]]
            ),
        )

    async def _on_seat(self, query, context, value):
        self.session.seat_option = value
        if not keyring.get_password("card", "ok"):
            await self._begin(query, context)
            return
        await query.edit_message_text(
            "예매되면 카드로 바로 결제할까요?",
            reply_markup=keyboard([[("✅ 예", "pay:1"), ("❌ 아니오", "pay:0")]]),
        )

    async def _on_pay(self, query, context, value):
        self.session.pay = value == "1"
        await self._begin(query, context)

    async def _on_status(self, query, context, value):
        await query.edit_message_text(self.status_text(), reply_markup=main_menu())

    async def _on_stop(self, query, context, value):
        await query.edit_message_text(self.cancel_task(), reply_markup=main_menu())

    async def _on_home(self, query, context, value):
        self.cancel_task()
        await query.edit_message_text("무엇을 할까요?", reply_markup=main_menu())

    async def _begin(self, query, context):
        s = self.session
        if s.task and not s.task.done():
            s.task.cancel()
        s.tries = 0
        s.started_at = time.time()
        s.last_error = None
        s.task = asyncio.create_task(self._reserve_loop(context))
        await query.edit_message_text(
            f"{len(s.selected)}개 열차를 감시합니다.\n"
            "자리가 나면 바로 예매하고 알려드립니다.\n\n"
            "/status 로 진행 상황, /stop 으로 중지할 수 있습니다."
        )

    async def _reserve_loop(self, context):
        s = self.session
        rail = await asyncio.to_thread(login, s.rail_type)
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
                    await self._reserve(context, rail, train)
                    return
                errors_since_notify = 0
                await asyncio.sleep(self._interval())

            except asyncio.CancelledError:
                raise
            except NoResultsError:
                await asyncio.sleep(self._interval())
            except (NeedToLoginError, SRTNetFunnelError) as ex:
                s.last_error = str(ex)
                rail = await asyncio.to_thread(login, s.rail_type)
                await asyncio.sleep(self._interval())
            except Exception as ex:
                s.last_error = f"{type(ex).__name__}: {ex}"
                errors_since_notify += 1
                # CLI와 달리 사람을 기다리지 않는다. 계속 재시도하되 가끔만 알린다.
                if errors_since_notify == 1 or errors_since_notify % ERROR_NOTIFY_INTERVAL == 0:
                    await self._try_send(
                        context, f"⚠️ 계속 재시도 중입니다.\n{s.last_error}"
                    )
                if isinstance(ex, (SRTError, KorailError)):
                    rail = await asyncio.to_thread(login, s.rail_type)
                await asyncio.sleep(self._interval())

    async def _reserve(self, context, rail, train):
        s = self.session
        reservation = await asyncio.to_thread(
            rail.reserve, train, passengers=s.passengers(), option=s.seat_type()
        )
        msg = f"🎫 예매 성공!\n{reservation}"
        tickets = getattr(reservation, "tickets", None)
        if tickets:
            msg += "\n" + "\n".join(map(str, tickets))

        if s.pay and not reservation.is_waiting:
            paid = await asyncio.to_thread(pay_card, rail, reservation)
            msg += "\n\n💳 결제 완료" if paid else "\n\n💳 결제 실패 — 직접 결제해 주세요."

        await self._try_send(context, msg)

    async def _try_send(self, context, text):
        try:
            await context.bot.send_message(chat_id=self.chat_id, text=text)
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

    print("텔레그램 봇을 시작합니다. 텔레그램에서 /start 를 보내세요. (Ctrl-C 로 종료)")
    app.run_polling()


if __name__ == "__main__":
    main()
