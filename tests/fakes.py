"""A tiny fake OpenAI-compatible provider for end-to-end tests."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeProvider:
    """Behaviour is chosen per model name:
    model-ok       -> normal answer (text, or a tool call if the request has tools)
    model-text     -> always a plain text answer
    model-429      -> 429 rate limited
    model-500      -> 500
    model-400      -> 400 bad request
    model-noopts   -> 400 if stream_options is present, else normal
    model-leak     -> 500 whose body echoes the Authorization header
    model-midfail  -> stream starts, then sends an error payload
    Key "bad" gets 401.
    """

    def __init__(self):
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                auth = self.headers.get("Authorization", "")
                fake.requests.append({"path": self.path, "body": body, "auth": auth})
                model = body["model"]
                if auth == "Bearer bad":
                    return self._json(401, {"error": {"message": "invalid key"}})
                if model == "model-429":
                    return self._json(429, {"error": {"message": "rate limited"}})
                if model == "model-500":
                    return self._json(500, {"error": {"message": "boom"}})
                if model == "model-400":
                    return self._json(400, {"error": {"message": "bad field"}})
                if model == "model-leak":
                    return self._json(500, {"error": {"message": f"denied for {auth}"}})
                if model == "model-noopts" and "stream_options" in body:
                    return self._json(400, {"error": {"message": "unknown field stream_options"}})
                use_tool = bool(body.get("tools")) and model != "model-text"
                if body.get("stream"):
                    return self._stream(model, use_tool, body)
                msg = {"role": "assistant", "content": "Hello from " + model}
                finish = "stop"
                if use_tool:
                    msg = {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\": \"Bhilai\"}"}}]}
                    finish = "tool_calls"
                self._json(200, {"id": "x", "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                 "usage": {"prompt_tokens": 11, "completion_tokens": 7}})

            def _json(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, model, use_tool, body):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(obj):
                    self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                    self.wfile.flush()

                self.wfile.write(b": keep-alive\n\n")
                send({"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]})
                send({"choices": [{"index": 0, "delta": {"content": "lo"}}]})
                if model == "model-midfail":
                    send({"error": {"message": "provider crashed"}})
                    return
                finish = "stop"
                if use_tool:
                    send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_9", "type": "function", "function": {"name": "get_weather", "arguments": ""}}]}}]})
                    send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"city\": "}}]}}]})
                    send({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"Bhilai\"}"}}]}}]})
                    finish = "tool_calls"
                send({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
                if body.get("stream_options"):
                    send({"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}})
                self.wfile.write(b"data: [DONE]\n\n")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
