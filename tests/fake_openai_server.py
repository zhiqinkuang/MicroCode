import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        # 只记录 path 和 payload：请求头（尤其是 Authorization）刻意不进任何记录，
        # 这样断言失败时打印 server.requests 也绝不会泄露密钥。
        self.server.requests.append({"path": self.path, "payload": payload})
        response = {
            "id": f"chatcmpl-{len(self.server.requests)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", "test-vision"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self.server.reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        }
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        return


class FakeOpenAIServer:
    def __init__(self, reply="fake vision response"):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = []
        self.httpd.reply = reply
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/v1"

    @property
    def requests(self):
        return self.httpd.requests

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
