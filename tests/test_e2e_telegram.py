"""실제 python-telegram-bot Application 을 가짜 텔레그램 서버에 붙여 끝까지 돌려 본다.

핸들러를 직접 부르는 테스트로는 텔레그램 쪽 연결(명령 인식, 버튼 콜백 응답,
메시지 수정, 명령어 메뉴 등록, 폴링)이 실제로 되는지 알 수 없다.
"""

import asyncio
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conftest import FakeKorail, day, fast, ktx_train
from srtgo import bot as botmod

TOKEN = "123456:TESTTOKEN"
OWNER = 100
STRANGER = 300


class FakeTelegramServer:
    def __init__(self):
        self.cond = threading.Condition()
        self.updates = []
        self.update_id = 0
        self.next_id = 1000
        self.calls = []
        self.messages = {}  # message_id -> message dict
        self.version = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                params = {}
                for key, values in urllib.parse.parse_qs(body.decode("utf-8")).items():
                    try:
                        params[key] = json.loads(values[0])
                    except ValueError:
                        params[key] = values[0]
                method = self.path.rsplit("/", 1)[-1]
                data = json.dumps({"ok": True, "result": server.handle(method, params)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    def _user(self, uid):
        return {"id": uid, "is_bot": False, "first_name": "나" if uid == OWNER else "홍길동"}

    def handle(self, method, params):
        with self.cond:
            self.calls.append((method, params))
            if method == "getMe":
                return {"id": 1, "is_bot": True, "first_name": "srtgo", "username": "srtgo_test_bot"}
            if method == "getUpdates":
                offset = int(params.get("offset") or 0)
                self.cond.wait_for(
                    lambda: any(u["update_id"] >= offset for u in self.updates),
                    timeout=min(float(params.get("timeout") or 0), 0.5),
                )
                return [u for u in self.updates if u["update_id"] >= offset]
            if method == "sendMessage":
                msg = {
                    "message_id": self._new_id(),
                    "date": int(time.time()),
                    "chat": {"id": int(params["chat_id"]), "type": "private"},
                    "text": str(params["text"]),
                }
                if params.get("reply_markup"):
                    msg["reply_markup"] = params["reply_markup"]
                self.messages[msg["message_id"]] = msg
                msg["_v"] = self.version = self.version + 1
                self.cond.notify_all()
                return msg
            if method == "editMessageText":
                msg = self.messages[int(params["message_id"])]
                msg["text"] = str(params["text"])
                if params.get("reply_markup"):
                    msg["reply_markup"] = params["reply_markup"]
                else:
                    msg.pop("reply_markup", None)
                msg["_v"] = self.version = self.version + 1
                self.cond.notify_all()
                return msg
            return True  # deleteWebhook, setMyCommands, answerCallbackQuery, deleteMessage ...

    def push(self, update):
        with self.cond:
            self.update_id += 1
            update["update_id"] = self.update_id
            self.updates.append(update)
            self.cond.notify_all()

    def say(self, uid, text):
        msg = {
            "message_id": self._new_id(),
            "date": int(time.time()),
            "chat": {"id": uid, "type": "private"},
            "from": self._user(uid),
            "text": text,
        }
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        self.push({"message": msg})

    def press(self, uid, msg, data):
        """버튼을 누른다. 누른 시점의 화면 버전을 돌려준다 (그 뒤의 화면을 기다리려고)."""
        with self.cond:
            before = self.version
        buttons = [b["callback_data"] for row in msg["reply_markup"]["inline_keyboard"] for b in row]
        assert data in buttons, f"{data} 없음: {buttons}"
        clean = {k: v for k, v in msg.items() if not k.startswith("_")}
        self.push(
            {
                "callback_query": {
                    "id": str(self._new_id()),
                    "from": self._user(uid),
                    "chat_instance": "ci",
                    "data": data,
                    "message": clean,
                }
            }
        )
        return before

    def wait_screen(self, uid, contains, after=0, timeout=10):
        """uid 에게 after 이후 새로 오거나 고쳐진 메시지 중 contains 가 들어간 것."""
        end = time.time() + timeout
        with self.cond:
            while time.time() < end:
                for msg in sorted(self.messages.values(), key=lambda m: -m["_v"]):
                    if msg["chat"]["id"] == uid and msg["_v"] > after and contains in msg["text"]:
                        return msg
                self.cond.wait(0.1)
        texts = [m["text"][:80] for m in self.messages.values() if m["chat"]["id"] == uid]
        raise AssertionError(f"{contains!r} 화면이 안 옴. 받은 것: {texts}")

    def called(self, method):
        with self.cond:
            return [p for m, p in self.calls if m == method]


def test_real_telegram_application_end_to_end(monkeypatch, tmp_path, fake_keyring):
    server = FakeTelegramServer()
    t527 = ktx_train(527, "200900", "231200")
    rail = FakeKorail([t527])
    monkeypatch.setattr(botmod, "build_rail", lambda rt, cid, owner: rail)
    fake_keyring.set_password("KTX", "id", "pc-id")  # 오너의 PC '로그인 설정'
    fake_keyring.set_password("KTX", "pass", "pc-pw")
    monkeypatch.setattr(botmod, "HEARTBEAT_SECONDS", 0.05)

    bot = fast(botmod.Bot(str(OWNER), botmod.StateStore(tmp_path / "state.json")))
    bot.heartbeat_path = tmp_path / "heartbeat"
    app = botmod.build_application(bot, TOKEN, base_url=f"http://127.0.0.1:{server.port}/bot")
    loops = {}

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loops["loop"] = loop
        app.run_polling(stop_signals=None, timeout=1, allowed_updates=["message", "callback_query"])

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    try:
        end = time.time() + 10
        while not server.called("setMyCommands") and time.time() < end:
            time.sleep(0.02)
        commands = server.called("setMyCommands")[0]["commands"]
        assert [c["command"] for c in commands] == ["start", "status", "stop", "settings", "help"]

        server.say(OWNER, "/start")
        screen = server.wait_screen(OWNER, "무엇을 할까요")
        for data, expect in [
            ("book:KTX", "출발역을 고르세요"),
            ("dep:서울", "도착역을 고르세요"),
            ("arr:부산", "출발 날짜를 고르세요"),
            (f"date:{day()}", "몇 시 이후"),
            ("time:19", "인원을 정하세요"),
            ("search", "감시할 열차를 모두 고르세요"),
            ("tr:527:200900", "감시할 열차를 모두 고르세요"),
            ("trok", "좌석 유형"),
            ("go", "대기를 시작했습니다"),
        ]:
            before = server.press(OWNER, screen, data)
            screen = server.wait_screen(OWNER, expect, after=before)
        assert server.called("answerCallbackQuery"), "버튼 누름에 응답하지 않음 (버튼이 계속 로딩 중으로 보임)"

        t527.general_seat = "11"
        done = server.wait_screen(OWNER, "🎉 예매 성공")
        assert "rv:KTX" in json.dumps(done["reply_markup"])

        # 모르는 사람: 승인 요청이 오너에게 간다
        server.say(STRANGER, "/start")
        request = server.wait_screen(OWNER, "사용을 요청했습니다")
        assert f"approve:{STRANGER}" in json.dumps(request["reply_markup"])
        server.wait_screen(STRANGER, "승인을 요청했습니다")

        # 평문 입력에도 반응한다
        server.say(OWNER, "안녕")
        server.wait_screen(OWNER, "버튼으로 조작해 주세요")
        assert (tmp_path / "heartbeat").exists(), "하트비트를 쓰지 않음"
    finally:
        if "loop" in loops:
            loops["loop"].call_soon_threadsafe(app.stop_running)
        thread.join(20)
        server.close()
    assert not thread.is_alive(), "봇이 멈추지 않음 (종료가 막힘)"
