"""다중 사용자: 승인 흐름, 계정 분리, 권한 경계 검증."""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from srtgo import bot as botmod

OWNER = "100"
FAMILY = "200"
STRANGER = "300"


class FakeKeyring:
    """keyring 대체 (샌드박스에 백엔드가 없어서)."""

    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


def make_update(chat_id, name="아무개", handle=None, text=None):
    up = MagicMock()
    up.effective_chat.id = chat_id
    up.effective_user.full_name = name
    up.effective_user.username = handle
    up.message.reply_text = AsyncMock()
    up.message.delete = AsyncMock()
    up.message.text = text
    return up


def make_ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def make_query():
    q = MagicMock()
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    return q


def sent_to(ctx, chat_id):
    return [
        c for c in ctx.bot.send_message.call_args_list
        if str(c.kwargs.get("chat_id")) == str(chat_id)
    ]


async def run():
    fk = FakeKeyring()
    with patch.object(botmod, "keyring", fk):
        bot = botmod.Bot(OWNER)

        # --- 모르는 사람이 말 걸면 오너에게 승인 요청 ---
        ctx = make_ctx()
        await bot.start(make_update(STRANGER, "홍길동", "gildong"), ctx)
        to_owner = sent_to(ctx, OWNER)
        assert to_owner, "오너에게 승인 요청이 안 감"
        assert "홍길동" in to_owner[0].kwargs["text"]
        btns = [
            b.callback_data
            for row in to_owner[0].kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert f"approve:{STRANGER}" in btns and f"deny:{STRANGER}" in btns
        assert sent_to(ctx, STRANGER), "요청자에게 안내가 안 감"
        print("OK: 신규 사용자 -> 오너에게 승인 요청")

        # 승인 전에는 아무 것도 못 한다
        ctx2 = make_ctx()
        up = make_update(STRANGER)
        await bot.start(up, ctx2)
        assert up.message.reply_text.await_count == 0, "승인 전에 메뉴가 열림"
        # 중복 요청으로 오너를 도배하지 않는다
        assert not sent_to(ctx2, OWNER), "승인 요청이 중복 발송됨"
        print("OK: 승인 전 차단 + 승인요청 중복 발송 안 함")

        # --- 오너가 승인 ---
        owner_u = bot.context_for(OWNER)
        q = make_query()
        ctx3 = make_ctx()
        await bot._on_approve(q, ctx3, STRANGER, owner_u)
        assert botmod.UserStore.is_approved(STRANGER)
        assert sent_to(ctx3, STRANGER), "승인 통보 안 감"
        print("OK: 오너 승인 -> 사용 가능")

        # 가족이 오너 행세를 못 한다
        fam_u = bot.context_for(STRANGER)
        assert not fam_u.is_owner
        q = make_query()
        await bot._on_approve(q, make_ctx(), "999", fam_u)
        assert not botmod.UserStore.is_approved("999"), "가족이 남을 승인함"
        q = make_query()
        await bot._on_users(q, make_ctx(), "revoke:100", fam_u)
        assert botmod.UserStore.is_approved(OWNER), "가족이 오너를 해제함"
        print("OK: 오너 전용 기능은 가족이 못 씀")

        # --- 계정 연결: 메시지 즉시 삭제 + 검증 후 저장 ---
        q = make_query()
        await bot._on_link(q, make_ctx(), "KTX", fam_u)
        assert fam_u.awaiting == ("KTX", "id", None)

        ctx4 = make_ctx()
        up_id = make_update(STRANGER, text="my-korail-id")
        await bot.on_text(up_id, ctx4)
        assert up_id.message.delete.await_count == 1, "아이디 메시지를 안 지움"
        assert fam_u.awaiting == ("KTX", "pw", "my-korail-id")

        up_pw = make_update(STRANGER, text="s3cret")
        with patch.object(botmod, "Korail", MagicMock()):
            await bot.on_text(up_pw, ctx4)
        assert up_pw.message.delete.await_count == 1, "비밀번호 메시지를 안 지움"
        got = botmod.UserStore.credentials(STRANGER, "KTX")
        assert got == ("my-korail-id", "s3cret"), got
        print("OK: 계정 연결 (아이디·비밀번호 메시지 즉시 삭제 후 저장)")

        # 로그인 실패하면 저장하지 않는다
        q = make_query()
        await bot._on_link(q, make_ctx(), "SRT", fam_u)
        await bot.on_text(make_update(STRANGER, text="bad-id"), make_ctx())
        with patch.object(botmod, "SRT", MagicMock(side_effect=RuntimeError("실패"))):
            await bot.on_text(make_update(STRANGER, text="bad-pw"), make_ctx())
        assert botmod.UserStore.credentials(STRANGER, "SRT") == (None, None)
        print("OK: 로그인 실패 시 계정 저장 안 함")

        # --- 계정 분리: 각자 자기 계정으로만 ---
        botmod.UserStore.set_credentials(OWNER, "KTX", "owner-id", "owner-pw")
        made = []
        with patch.object(botmod, "Korail", lambda i, p: made.append((i, p))):
            botmod.build_rail("KTX", STRANGER, is_owner=False)
            botmod.build_rail("KTX", OWNER, is_owner=True)
        assert made == [("my-korail-id", "s3cret"), ("owner-id", "owner-pw")], made
        print("OK: 사용자별로 자기 계정으로 로그인")

        # 계정 없는 사람은 예매 진입 불가
        no_acct = bot.context_for("400")
        q = make_query()
        await bot._on_rail(q, make_ctx(), "SRT", no_acct)
        assert "연결되어 있지 않" in q.edit_message_text.call_args.args[0]
        print("OK: 계정 미연결 시 예매 차단")

        # --- 결제: 가족에게는 결제 버튼이 없다 ---
        fk.set_password("card", "ok", "1")
        item = MagicMock()
        item.is_ticket = False
        item.is_waiting = False
        fam_u.session.reservations = [item]
        fam_u.session.rail_type = "KTX"
        q = make_query()
        await bot._on_rvpick(q, make_ctx(), "0", fam_u)
        fam_btns = [
            b.callback_data
            for row in q.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert "rvpay:0" not in fam_btns, fam_btns
        owner_u.session.reservations = [item]
        owner_u.session.rail_type = "KTX"
        q = make_query()
        await bot._on_rvpick(q, make_ctx(), "0", owner_u)
        own_btns = [
            b.callback_data
            for row in q.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        assert "rvpay:0" in own_btns, own_btns
        # 가족이 결제 콜백을 직접 쏴도 막힌다
        paid = []
        with patch.object(botmod, "pay_card", lambda *a: paid.append(1)):
            await bot._on_rvpay(make_query(), make_ctx(), "0", fam_u)
        assert not paid, "가족이 오너 카드로 결제함"
        print("OK: 카드 결제는 오너만 (콜백 직접 호출도 차단)")

        # --- 해제하면 계정도 지워진다 ---
        q = make_query()
        await bot._on_users(q, make_ctx(), f"revoke:{STRANGER}", owner_u)
        assert not botmod.UserStore.is_approved(STRANGER)
        assert botmod.UserStore.credentials(STRANGER, "KTX") == (None, None)
        print("OK: 사용자 해제 시 연결 계정도 삭제")

        # 해제된 사람은 다시 들어올 때 재승인 필요 (자동 재요청 안 됨)
        ctx5 = make_ctx()
        up = make_update(STRANGER)
        await bot.start(up, ctx5)
        assert up.message.reply_text.await_count == 0, "해제된 사용자가 그냥 들어옴"
        print("OK: 해제된 사용자 차단")

        # --- 동시 대기 상한 ---
        for i in range(botmod.MAX_CONCURRENT_WATCHES):
            c = bot.context_for(f"9{i}")
            c.task = MagicMock()
            c.task.done.return_value = False
        assert bot.active_watches() == botmod.MAX_CONCURRENT_WATCHES
        fresh = bot.context_for("777")
        fresh.session.selected = [("1", "2", "3")]
        q = make_query()
        await bot._begin(q, make_ctx(), fresh)
        assert fresh.task is None, "상한을 넘겨 시작됨"
        assert "더 시작할 수 없" in q.edit_message_text.call_args.args[0]
        print(f"OK: 동시 대기 {botmod.MAX_CONCURRENT_WATCHES}건 상한")


asyncio.run(run())
print("\n=== 다중 사용자 전부 통과 ===")
