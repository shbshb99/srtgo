"""예매 흐름 전체: 역 선택 -> 승객 -> 검색 -> 감시 -> 예매, 그리고 예매내역 조작."""
import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from srtgo import bot as botmod


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, s, u):
        return self.store.get((s, u))

    def set_password(self, s, u, v):
        self.store[(s, u)] = v

    def delete_password(self, s, u):
        self.store.pop((s, u), None)


class FakeTrain:
    def __init__(self, no, seat=False):
        self.train_no = no
        self.dep_date = "20260924"
        self.dep_time = "200900"
        self._seat = seat

    def has_seat(self):
        return self._seat

    def has_general_seat(self):
        return self._seat

    def has_special_seat(self):
        return False

    def has_waiting_list(self):
        return False

    def __str__(self):
        return f"[KTX {self.train_no}] 09/24 20:09~23:12 용산~여수EXPO"


class FakeReservation:
    is_waiting = False
    tickets = ["8호차 3D (일반실)"]

    def __str__(self):
        return "[KTX 527] 09/24 43500원(1석)"


class FakeRail:
    def __init__(self, trains):
        self.trains = trains
        self.searches = 0
        self.reserved = None

    def search_train(self, **kw):
        self.searches += 1
        return self.trains

    def reserve(self, train, **kw):
        self.reserved = train
        return FakeReservation()


class Item:
    def __init__(self, label, is_ticket=False):
        self.label = label
        self.is_ticket = is_ticket
        self.is_waiting = False

    def __str__(self):
        return self.label


def q():
    x = MagicMock()
    x.answer = AsyncMock()
    x.edit_message_text = AsyncMock()
    x.edit_message_reply_markup = AsyncMock()
    return x


def ctx():
    c = MagicMock()
    c.bot.send_message = AsyncMock()
    return c


def buttons(query):
    m = query.edit_message_text.call_args.kwargs.get("reply_markup")
    return [b.callback_data for row in m.inline_keyboard for b in row]


def labels(query):
    m = query.edit_message_text.call_args.kwargs.get("reply_markup")
    return [b.text for row in m.inline_keyboard for b in row]


async def run():
    fk = FakeKeyring()
    with patch.object(botmod, "keyring", fk):
        bot = botmod.Bot("1")
        u = bot.context_for("1")
        botmod.UserStore.set_credentials("1", "KTX", "id", "pw")
        c = ctx()

        sold_out = FakeTrain("527", seat=False)
        rail = FakeRail([sold_out])

        with patch.object(botmod, "build_rail", return_value=rail), patch.object(
            botmod, "get_station", return_value=(None, ["서울", "부산"])
        ), patch.object(botmod, "get_options", return_value=["child"]):

            # 역: 기본은 단축목록, 전체 보기로 확장
            x = q()
            await bot._on_rail(x, c, "KTX", u)
            assert "dep:서울" in buttons(x) and "dep:용산" not in buttons(x)
            assert "stn:dep:all" in buttons(x)
            x = q()
            await bot._on_stn(x, c, "dep:all", u)
            assert "dep:용산" in buttons(x) and "dep:여수EXPO" in buttons(x)
            print(f"OK: 전체 역 보기 ({len([b for b in buttons(x) if b.startswith('dep:')])}개)")

            x = q()
            await bot._on_dep(x, c, "용산", u)
            x = q()
            await bot._on_stn(x, c, "arr:all", u)
            assert "arr:용산" not in buttons(x), "출발역이 도착역에 남음"
            print("OK: 도착역에서 출발역 제외")

            x = q()
            await bot._on_arr(x, c, "여수EXPO", u)
            assert any(b.startswith("date:") for b in buttons(x))
            x = q()
            await bot._on_date(x, c, "20260924", u)
            x = q()
            await bot._on_time(x, c, "190000", u)
            assert "어린이" in " ".join(labels(x))
            assert "경로우대" not in " ".join(labels(x)), "안 켠 유형이 보임"
            print("OK: 옵션에서 켠 승객 유형만 노출")

            for _ in range(2):
                await bot._on_pax(q(), c, "child:+", u)
            assert u.session.total_count() == 3
            for _ in range(20):
                await bot._on_pax(q(), c, "adult:+", u)
            assert u.session.total_count() == botmod.MAX_PASSENGERS
            for _ in range(20):
                await bot._on_pax(q(), c, "child:-", u)
            assert u.session.counts["child"] == 0
            u.session.counts = {"adult": 2, "child": 1}
            assert {type(p).__name__ for p in u.session.passengers()} == {
                "AdultPassenger",
                "ChildPassenger",
            }
            assert u.session.search_params()["passengers"][0].count == 3
            print("OK: 승객 증감·상한·구성 (검색은 총 3명을 어른으로)")

            x = q()
            await bot._on_pax(x, c, "done:0", u)
            assert "toggle:0" in buttons(x)
            print("OK: 검색 -> 열차 목록")

            await bot._on_toggle(q(), c, "0", u)
            assert u.session.selected == [("527", "20260924", "200900")]
            await bot._on_toggle(q(), c, "0", u)
            assert u.session.selected == []
            await bot._on_toggle(q(), c, "0", u)

            await bot._on_seatmenu(q(), c, "", u)
            await bot._on_seat(q(), c, "GENERAL_FIRST", u)
            assert u.task is not None and u.watching is u.session
            print("OK: 대기 시작")

            await asyncio.sleep(1.5)
            assert rail.reserved is None, "매진인데 예매함"
            assert "감시 중" in bot.status_text(u)
            print(f"OK: 매진 중 대기 유지 ({rail.searches}회 검색)")

            sold_out._seat = True
            await asyncio.sleep(3.0)
            assert rail.reserved is sold_out, "자리 났는데 예매 안 함"
            sent = " ".join(str(k) for k in c.bot.send_message.call_args_list)
            assert "예매 성공" in sent
            print("OK: 자리 발생 -> 예매 + 알림")

            # '처음으로'는 대기를 유지, '중지'만 취소
            u.task = MagicMock()
            u.task.done.return_value = False
            u.watching = u.session
            await bot._on_home(q(), c, "", u)
            assert u.task.cancel.call_count == 0, "홈이 대기를 취소함"
            assert "중지했습니다" in bot.cancel_task(u)
            print("OK: 홈은 유지, 중지만 취소")

        # --- 예매내역 조작 ---
        rail2 = MagicMock()
        rail2.cancel = MagicMock()
        rail2.refund = MagicMock()
        unpaid, ticket = Item("미결제"), Item("발권완료", is_ticket=True)
        with patch.object(botmod, "build_rail", return_value=rail2), patch.object(
            botmod, "load_reservations", return_value=[ticket, unpaid]
        ):
            fk.set_password("card", "ok", "1")
            x = q()
            await bot._on_rv(x, c, "KTX", u)
            assert "rvpick:0" in buttons(x) and "rvpick:1" in buttons(x)
            x = q()
            await bot._on_rvpick(x, c, "1", u)
            assert "rvpay:1" in buttons(x)
            x = q()
            await bot._on_rvpick(x, c, "0", u)
            assert "rvpay:0" not in buttons(x), "발권건에 결제 버튼"
            print("OK: 예매내역 + 미결제만 결제 버튼")

            x = q()
            await bot._on_rvcancel(x, c, "1", u)
            assert "rvdo:1" in buttons(x) and rail2.cancel.call_count == 0
            await bot._on_rvdo(q(), c, "1", u)
            assert rail2.cancel.call_count == 1 and rail2.refund.call_count == 0
            await bot._on_rvdo(q(), c, "0", u)
            assert rail2.refund.call_count == 1
            print("OK: 확인 후 취소(cancel) / 환불(refund) 분기")

        # 오류가 나도 루프가 죽지 않는다
        class Broken(FakeRail):
            def search_train(self, **kw):
                self.searches += 1
                raise RuntimeError("boom")

        broken = Broken([])
        s = botmod.Session()
        s.rail_type, s.dep, s.arr = "KTX", "용산", "여수EXPO"
        s.date, s.time = "20260924", "190000"
        s.seat_option = "GENERAL_FIRST"
        s.selected = [("527", "20260924", "200900")]
        s.started_at = time.time()
        c2 = ctx()
        with patch.object(botmod, "build_rail", return_value=broken), patch.object(
            botmod, "get_options", return_value=[]
        ):
            t = asyncio.create_task(bot._reserve_loop(c2, u, s))
            await asyncio.sleep(3.0)
            assert not t.done(), "오류로 루프가 죽음"
            assert broken.searches > 1
            assert c2.bot.send_message.await_count >= 1
            t.cancel()
        print(f"OK: 오류에도 계속 재시도 ({broken.searches}회) + 알림")


asyncio.run(run())
print("\n=== 흐름 전부 통과 ===")
