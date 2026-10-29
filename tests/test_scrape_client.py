import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from srtgo import scrape_client


def test_fetch_never_enables_bypass_options():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": 200}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    out = scrape_client.fetch("https://example.com", api="http://127.0.0.1:%d" % srv.server_port, timeout=5)
    assert out["status"] == 200
    assert seen["solve_cloudflare"] is False
    assert seen["real_chrome"] is True and seen["profile"] == "korail"
