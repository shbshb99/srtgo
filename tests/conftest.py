"""봇 테스트용 가짜 텔레그램·철도사·keyring.

열차와 예약은 가짜 dict 가 아니라 실제 ktx.Train / srt.SRTTrain / ktx.Reservation /
srt.SRTReservation 을 API 응답 모양의 데이터로 만들어 쓴다. 가짜 객체로만 테스트하면
실제 객체에만 있는 속성 차이(SRT 는 train_number, KTX 는 train_no 등)를 놓친다.
"""

import asyncio
import itertools
import pathlib
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from srtgo import bot as botmod  # noqa: E402
from srtgo import ktx, srt  # noqa: E402
from srtgo import srtgo as cli  # noqa: E402

OWNER = "100"
FAMILY = "200"
STRANGER = "300"


def day(offset=1):
    return (botmod.kst_now() + timedelta(days=offset)).strftime("%Y%m%d")


def run(coro):
    return asyncio.run(coro)


async def until(cond, timeout=5.0, step=0.01):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(step)
    raise AssertionError("시간 안에 조건이 만족되지 않음")


# --- keyring ---------------------------------------------------------------
class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    fk = FakeKeyring()
    monkeypatch.setattr(botmod, "keyring", fk)
    monkeypatch.setattr(cli, "keyring", fk)
    return fk


# --- 열차·예약 (실제 클래스) -----------------------------------------------
def ktx_train(no, dep, arr, date=None, general="13", special="13", wait="-1", name="KTX",
              dep_name="서울", arr_name="부산"):
    """general/special: '11' 가능, '13' 매진. wait: '9' 예약대기 가능, '-1' 없음."""
    date = date or day()
    return ktx.Train(
        {
            "h_trn_clsf_cd": "100",
            "h_trn_clsf_nm": name,
            "h_trn_gp_cd": "100",
            "h_trn_no": f"{int(no):05d}",
            "h_dpt_rs_stn_nm": dep_name,
            "h_dpt_rs_stn_cd": "0001",
            "h_dpt_dt": date,
            "h_dpt_tm": dep,
            "h_arv_rs_stn_nm": arr_name,
            "h_arv_rs_stn_cd": "0020",
            "h_arv_dt": date,
            "h_arv_tm": arr,
            "h_run_dt": date,
            "h_rsv_psb_flg": "Y",
            "h_rsv_psb_nm": "예약가능",
            "h_spe_rsv_cd": special,
            "h_gen_rsv_cd": general,
            "h_wait_rsv_flg": wait,
        }
    )


def ktx_reservation(train, pnr, waiting=False):
    data = {
        "h_trn_clsf_cd": train.train_type,
        "h_trn_clsf_nm": train.train_type_name,
        "h_trn_gp_cd": train.train_group,
        "h_trn_no": train.train_no,
        "h_dpt_rs_stn_nm": train.dep_name,
        "h_dpt_rs_stn_cd": train.dep_code,
        "h_dpt_dt": train.dep_date,
        "h_dpt_tm": train.dep_time,
        "h_arv_rs_stn_nm": train.arr_name,
        "h_arv_rs_stn_cd": train.arr_code,
        "h_arv_dt": train.arr_date,
        "h_arv_tm": train.arr_time,
        "h_run_dt": train.run_date,
        "h_pnr_no": pnr,
        "h_tot_seat_cnt": "1",
        "h_ntisu_lmt_dt": "00000000" if waiting else train.dep_date,
        "h_ntisu_lmt_tm": "235959" if waiting else "235000",
        "h_rsv_amt": "59800",
    }
    r = ktx.Reservation(data)
    r.tickets = [] if waiting else [
        ktx.Seat({"h_srcar_no": "8", "h_seat_no": "3D", "h_psrm_cl_nm": "일반실", "h_psg_tp_dv_nm": "어른", "h_rcvd_amt": "59800"})
    ]
    r.wct_no = "W1"
    return r


def srt_train(no, dep, arr, date=None, general="매진", special="매진", wait="-1"):
    date = date or day()
    return srt.SRTTrain(
        {
            "stlbTrnClsfCd": "17",
            "trnNo": f"{int(no):05d}",
            "dptDt": date,
            "dptTm": dep,
            "dptRsStnCd": "0551",
            "dptStnRunOrdr": "000001",
            "dptStnConsOrdr": "000001",
            "arvDt": date,
            "arvTm": arr,
            "arvRsStnCd": "0020",
            "arvStnRunOrdr": "000010",
            "arvStnConsOrdr": "000010",
            "gnrmRsvPsbStr": general,
            "sprmRsvPsbStr": special,
            "rsvWaitPsbCdNm": "신청하기" if wait == "9" else "",
            "rsvWaitPsbCd": wait,
        }
    )


def srt_reservation(train, pnr, paid=False, waiting=False):
    t = {"pnrNo": pnr, "rcvdAmt": "52900", "seatNum": "1", "tkSpecNum": "1"}
    pay = {
        "stlbTrnClsfCd": "17",
        "trnNo": train.train_number,
        "dptDt": train.dep_date,
        "dptTm": train.dep_time,
        "dptRsStnCd": train.dep_station_code,
        "arvTm": train.arr_time,
        "arvRsStnCd": train.arr_station_code,
        "iseLmtDt": "" if waiting else train.dep_date,
        "iseLmtTm": "" if waiting else "235000",
        "stlFlg": "Y" if paid else "N",
    }
    return srt.SRTReservation(t, pay, [])


# --- 철도사 (Korail / SRT 와 같은 모양) ------------------------------------
class FakeKorail:
    rail_type = "KTX"

    def __init__(self, trains=(), name="홍길동"):
        self.trains = list(trains)
        self.name = name
        self.logined = True
        self.searches = 0
        self.last_params = None
        self.search_error = None  # 예외 또는 예외를 돌려주는 함수
        self.reserve_calls = []
        self.reserve_error = None  # 서버가 거절 (예약 안 잡힘)
        self.after_reserve_error = None  # 예약은 잡혔는데 그 뒤 조회에서 터짐
        self.return_list = False  # 코레일: 번호로 못 찾으면 전체 목록을 돌려줌
        self.reserve_gate = None  # threading.Event: 예약을 붙잡아 두기
        self.list_error = None
        self.break_list_on_reserve = False  # 예약하는 순간부터 목록 조회가 안 됨
        self.held = []
        self.ticket_list = []
        self.paid = []
        self.cleared = 0
        self._pnr = itertools.count(1)

    def search_train(self, **kw):
        self.searches += 1
        self.last_params = kw
        err = self.search_error() if callable(self.search_error) else self.search_error
        if err:
            raise err
        if not self.trains:
            raise ktx.NoResultsError()
        return list(self.trains)

    def _make(self, train):
        return ktx_reservation(train, f"P{next(self._pnr)}", waiting=not train.has_seat())

    def reserve(self, train, passengers=None, option=None):
        self.reserve_calls.append((train, passengers, option))
        if self.reserve_gate is not None:
            self.reserve_gate.wait(5)
        if self.reserve_error:
            raise self.reserve_error
        r = self._make(train)
        self.held.append(r)
        if self.break_list_on_reserve:
            self.list_error = RuntimeError("down")
        if self.after_reserve_error:
            raise self.after_reserve_error
        return list(self.held) if self.return_list else r

    def reservations(self, rsv_id=None):
        if self.list_error:
            raise self.list_error
        return list(self.held)

    def tickets(self):
        return list(self.ticket_list)

    def cancel(self, rsv):
        self.held.remove(rsv)
        return True

    def refund(self, ticket):
        self.ticket_list.remove(ticket)
        return True

    def pay_with_card(self, rsv, *args):
        self.paid.append(rsv)
        return True

    def clear(self):
        self.cleared += 1


class FakeSRT(FakeKorail):
    rail_type = "SRT"

    def __init__(self, trains=(), name="홍길동"):
        super().__init__(trains, name)
        del self.name
        self.membership_name = name

    def search_train(self, **kw):
        self.searches += 1
        self.last_params = kw
        err = self.search_error() if callable(self.search_error) else self.search_error
        if err:
            raise err
        return list(self.trains)

    def _make(self, train):
        return srt_reservation(train, f"S{next(self._pnr)}", waiting=not train.seat_available())

    def get_reservations(self):
        if self.list_error:
            raise self.list_error
        return list(self.held)

    def reservations(self, rsv_id=None):  # SRT 에는 없다
        raise AttributeError("reservations")


# --- 텔레그램 ---------------------------------------------------------------
class Msg:
    _ids = itertools.count(1)

    def __init__(self, tg, chat_id, text, markup):
        self.tg = tg
        self.chat_id = str(chat_id)
        self.message_id = next(Msg._ids)
        self.text = text
        self.reply_markup = markup
        self.deleted = False

    async def edit_text(self, text, reply_markup=None):
        self.text = text
        self.reply_markup = reply_markup
        self.tg.touch(self)

    async def delete(self):
        self.deleted = True

    def buttons(self):
        if not self.reply_markup:
            return []
        return [(b.text, b.callback_data) for row in self.reply_markup.inline_keyboard for b in row]

    def data(self):
        return [d for _, d in self.buttons()]

    def labels(self):
        return [t for t, _ in self.buttons()]


class FakeTG:
    def __init__(self):
        self.messages = []
        self.order = []
        self.commands = None

    def touch(self, msg):
        if msg in self.order:
            self.order.remove(msg)
        self.order.append(msg)

    async def send_message(self, chat_id, text, reply_markup=None):
        msg = Msg(self, chat_id, text, reply_markup)
        self.messages.append(msg)
        self.touch(msg)
        return msg

    async def set_my_commands(self, commands):
        self.commands = commands

    def to(self, chat_id):
        return [m for m in self.messages if m.chat_id == str(chat_id)]

    def texts(self, chat_id):
        return "\n---\n".join(m.text for m in self.to(chat_id))

    def latest(self, chat_id):
        for m in reversed(self.order):
            if m.chat_id == str(chat_id):
                return m
        return None


class FakeQuery:
    def __init__(self, data, message, delay=0):
        self.data = data
        self.message = message
        self.answered = False
        self.delay = delay  # 텔레그램 응답이 늦게 오는 상황

    async def answer(self, text=None, **kw):
        await asyncio.sleep(self.delay)
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None):
        await self.message.edit_text(text, reply_markup=reply_markup)


class UserMsg:
    def __init__(self, text):
        self.text = text
        self.deleted = False

    async def delete(self):
        self.deleted = True


class Phone:
    """한 사람의 텔레그램. 보내고, 누르고, 화면을 본다."""

    def __init__(self, bot, tg, chat_id, name="홍길동", handle=None):
        self.bot = bot
        self.tg = tg
        self.chat_id = str(chat_id)
        self.name = name
        self.handle = handle

    def _update(self, message=None, query=None):
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=int(self.chat_id), type="private"),
            effective_user=SimpleNamespace(full_name=self.name, username=self.handle),
            message=message,
            callback_query=query,
        )

    @property
    def ctx(self):
        return SimpleNamespace(bot=self.tg)

    async def command(self, name):
        handler = {
            "start": self.bot.cmd_start,
            "help": self.bot.cmd_help,
            "status": self.bot.cmd_status,
            "stop": self.bot.cmd_stop,
            "settings": self.bot.cmd_settings,
        }.get(name, self.bot.cmd_unknown)
        await handler(self._update(message=UserMsg("/" + name)), self.ctx)

    async def say(self, text):
        msg = UserMsg(text)
        await self.bot.on_text(self._update(message=msg), self.ctx)
        return msg

    @property
    def screen(self):
        return self.tg.latest(self.chat_id)

    async def press(self, want, on=None, delay=0):
        """callback_data 가 정확히 같거나, 버튼 글자에 want 가 들어 있는 버튼을 누른다."""
        msg = on or self.screen
        assert msg is not None, "화면이 없음"
        data = None
        for text, d in msg.buttons():
            if d == want:
                data = d
                break
        if data is None:
            for text, d in msg.buttons():
                if want in text:
                    data = d
                    break
        assert data is not None, f"버튼 {want!r} 없음. 있는 버튼: {msg.buttons()}"
        await self.bot.on_button(self._update(query=FakeQuery(data, msg, delay)), self.ctx)

    async def tap(self, data, on=None):
        """화면에 없는 버튼 데이터를 억지로 보낸다 (오래된 버튼, 조작된 콜백)."""
        msg = on or self.screen or Msg(self.tg, self.chat_id, "", None)
        await self.bot.on_button(self._update(query=FakeQuery(data, msg)), self.ctx)


def fast(bot):
    """테스트에서 실제 초 단위로 기다리지 않게."""
    bot._interval = lambda rail_type: 0.005
    bot._backoff = lambda fails: 0.005
    bot._login_backoff = lambda fails: 0.005
    bot.IMPORTANT_SEND_DELAYS = (0, 0, 0)
    bot.VERIFY_DELAYS = (0, 0, 0)
    return bot


@pytest.fixture
def env(monkeypatch, fake_keyring, tmp_path):
    """오너·가족이 승인돼 있고, 각자 철도 계정이 연결된 봇."""
    tg = FakeTG()
    store = botmod.StateStore(tmp_path / "bot_state.json")
    bot = fast(botmod.Bot(OWNER, store, tg=tg))
    store.set_user(FAMILY, status="approved", name="엄마")
    rails = {}

    def build_rail(rail_type, chat_id, is_owner):
        key = (str(chat_id), rail_type)
        if key not in rails:
            raise botmod.RailLoginError(rail_type, "계정이 연결되어 있지 않습니다", True)
        rail = rails[key]
        if isinstance(rail, Exception):
            raise rail
        if callable(rail) and not hasattr(rail, "search_train"):
            return rail()
        return rail

    monkeypatch.setattr(botmod, "build_rail", build_rail)
    for cid in (OWNER, FAMILY):
        for rt in ("SRT", "KTX"):
            botmod.Creds.set(cid, rt, f"{cid}-{rt}-id", f"{cid}-{rt}-pw")
    return SimpleNamespace(
        bot=bot,
        tg=tg,
        store=store,
        rails=rails,
        keyring=fake_keyring,
        path=tmp_path / "bot_state.json",
        owner=Phone(bot, tg, OWNER, "나"),
        family=Phone(bot, tg, FAMILY, "엄마"),
        stranger=Phone(bot, tg, STRANGER, "홍길동", "gildong"),
    )
