"""대기 루프가 어떤 오류에도 조용히 죽지 않는지, 상태 표시가 현실을 반영하는지."""
import asyncio
import pathlib
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from srtgo import bot as botmod
from srtgo.ktx import NeedToLoginError


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, s, u):
        return self.store.get((s, u))

    def set_password(self, s, u, v):
        self.store[(s, u)] = v

    def delete_password(self, s, u):
        self.store.pop((s, u), None)


def ctx():
    c = MagicMock()
    c.bot.send_message = AsyncMock()
    return c


def make_session():
    s = botmod.Session()
    s.rail_type, s.dep, s.arr = "KTX", "용산", "여수EXPO"
    s.date, s.time = "20260924", "190000"
    s.seat_option = "GENERAL_FIRST"
    s.selected = [("527", "20260924", "200900")]
    s.started_at = time.time()
    return s


async def run():
    fk = FakeKeyring()
    with patch.object(botmod, "keyring", fk), patch.object(
        botmod, "get_options", return_value=[]
    ):
        bot = botmod.Bot("1")
        u = bot.context_for("1")

        # --- 1. 세션 만료 -> 재로그인 실패해도 루프가 살아있어야 한다 ---
        class ExpiredRail:
            def __init__(self):
                self.searches = 0

            def search_train(self, **kw):
                self.searches += 1
                raise NeedToLoginError("P058")

        expired = ExpiredRail()
        logins = []

        def failing_login(rail_type, chat_id, is_owner):
            logins.append(1)
            if len(logins) == 1:
                return expired  # 최초 진입
            raise RuntimeError("코레일 로그인 거부")

        s = make_session()
        c = ctx()
        with patch.object(botmod, "build_rail", side_effect=failing_login):
            t = asyncio.create_task(bot._reserve_loop(c, u, s))
            await asyncio.sleep(3.0)
            assert not t.done(), "재로그인 실패로 대기가 죽음"
            assert expired.searches > 1, f"재시도 안 함 ({expired.searches})"
            assert len(logins) > 1, "재로그인 시도 안 함"
            assert "재로그인 실패" in (s.last_error or ""), s.last_error
            t.cancel()
        print(f"OK: 재로그인 실패해도 대기 유지 (조회 {expired.searches}회, 재로그인 {len(logins)-1}회)")

        # --- 2. 예상 못 한 예외로 루프가 끝나면 사용자에게 알린다 ---
        s2 = make_session()
        c2 = ctx()
        with patch.object(botmod, "build_rail", side_effect=RuntimeError("설정 깨짐")):
            await bot._reserve_loop(c2, u, s2)
        sent = " ".join(str(k) for k in c2.bot.send_message.call_args_list)
        assert "대기가 중단되었습니다" in sent, sent
        print("OK: 루프가 끝나면 조용히 죽지 않고 알림")

        # --- 3. 취소는 알림 없이 깔끔하게 ---
        s3 = make_session()
        c3 = ctx()
        rail = MagicMock()
        rail.search_train = MagicMock(return_value=[])
        with patch.object(botmod, "build_rail", return_value=rail):
            t = asyncio.create_task(bot._reserve_loop(c3, u, s3))
            await asyncio.sleep(0.3)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        sent = " ".join(str(k) for k in c3.bot.send_message.call_args_list)
        assert "중단되었습니다" not in sent, "중지했는데 오류 알림이 감"
        print("OK: 사용자가 중지한 경우엔 오류 알림 없음")

        # --- 4. 상태 표시가 현재를 반영한다 ---
        s4 = make_session()
        u.watching = s4
        u.task = MagicMock()
        u.task.done.return_value = False

        assert "✅ 정상" in bot.status_text(u)
        s4.note_error("Need to Login (P058)")
        assert "조회 실패 중" in bot.status_text(u)
        assert s4.error_count == 1
        s4.clear_error()
        txt = bot.status_text(u)
        assert "P058" not in txt, "복구됐는데 옛날 오류가 남아있음"
        assert "오류 1회, 모두 자동 복구" in txt, txt
        print("OK: 상태 표시 — 복구되면 오류가 사라지고 누적 횟수만 남음")


asyncio.run(run())
print("\n=== 복원력 전부 통과 ===")
