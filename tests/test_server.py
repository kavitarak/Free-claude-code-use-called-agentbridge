import json
import threading
import unittest
import urllib.error
import urllib.request

from agentbridge.config import ConfigError, Settings
from agentbridge.server import make_server
from tests.fakes import FakeProvider

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def sse_events(raw: bytes):
    out = []
    for block in raw.decode().split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


class BridgeCase(unittest.TestCase):
    extra: dict = {}

    def setUp(self):
        self.fake = FakeProvider()
        env = {"CUSTOM_BASE_URL": self.fake.url, "CUSTOM_API_KEY": "sk-test-secret-123", "MODEL": "custom/model-ok",
               "AGENTBRIDGE_PORT": "0"}
        env.update(self.extra)
        self.settings = Settings.from_mapping(env)
        self.server = make_server(self.settings)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fake.close()

    def call(self, path, body=None, headers=None, method=None):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with NO_PROXY.open(req, timeout=10) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()


TOOLS = [{"name": "get_weather", "description": "weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]


class MessagesTests(BridgeCase):
    def test_health_and_models(self):
        self.assertEqual(json.loads(self.call("/health")[2])["name"], "agentbridge")
        self.assertEqual(json.loads(self.call("/v1/models")[2])["data"][0]["id"], "custom/model-ok")

    def test_non_stream_message(self):
        status, _, raw = self.call("/v1/messages?beta=true", {"model": "claude-sonnet-5-5", "max_tokens": 64,
                                                              "messages": [{"role": "user", "content": "hi"}]})
        msg = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(msg["content"], [{"type": "text", "text": "Hello from model-ok"}])
        self.assertEqual(msg["model"], "claude-sonnet-5-5")
        sent = self.fake.requests[-1]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["auth"], "Bearer sk-test-secret-123")
        self.assertEqual(sent["body"]["model"], "model-ok")

    def test_streaming_tool_call(self):
        status, ctype, raw = self.call("/v1/messages", {"model": "claude-opus", "stream": True, "max_tokens": 64, "tools": TOOLS,
                                                        "messages": [{"role": "user", "content": "weather in Bhilai?"}]})
        self.assertEqual((status, ctype), (200, "text/event-stream"))
        ev = sse_events(raw)
        self.assertEqual(ev[0][0], "message_start")
        self.assertEqual(ev[-1][0], "message_stop")
        text = "".join(d["delta"]["text"] for n, d in ev if n == "content_block_delta" and d["delta"]["type"] == "text_delta")
        args = "".join(d["delta"]["partial_json"] for n, d in ev if n == "content_block_delta" and d["delta"]["type"] == "input_json_delta")
        self.assertEqual(text, "Hello")
        self.assertEqual(json.loads(args), {"city": "Bhilai"})
        delta = [d for n, d in ev if n == "message_delta"][0]
        self.assertEqual(delta["delta"]["stop_reason"], "tool_use")
        self.assertEqual(delta["usage"], {"output_tokens": 5, "input_tokens": 20})

    def test_count_tokens(self):
        status, _, raw = self.call("/v1/messages/count_tokens", {"messages": [{"role": "user", "content": "x" * 400}]})
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(raw)["input_tokens"], 90)

    def test_bad_json_and_unknown_route(self):
        self.assertEqual(self.call("/v1/messages", b"{nope")[0], 400)
        status, _, raw = self.call("/v1/nothing", {})
        self.assertEqual((status, json.loads(raw)["error"]["type"]), (404, "not_found_error"))


class FallbackTests(BridgeCase):
    extra = {"MODEL": "custom/model-429", "FALLBACK_MODELS": "custom/model-500,custom/model-ok"}

    def test_falls_back_to_the_next_model(self):
        status, _, raw = self.call("/v1/messages", {"model": "claude-x", "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["content"][0]["text"], "Hello from model-ok")
        self.assertEqual([r["body"]["model"] for r in self.fake.requests], ["model-429", "model-500", "model-ok"])

    def test_stream_falls_back_before_first_byte(self):
        status, _, raw = self.call("/v1/messages", {"model": "claude-x", "stream": True, "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(sse_events(raw)[-1][0], "message_stop")


class KeyRotationTests(BridgeCase):
    extra = {"CUSTOM_API_KEY": "bad,good-key-456"}

    def test_bad_key_is_skipped(self):
        status, _, _ = self.call("/v1/messages", {"model": "m", "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertIn("Bearer good-key-456", [r["auth"] for r in self.fake.requests])


class ErrorTests(BridgeCase):
    def test_client_error_is_not_retried(self):
        self.settings.model = "custom/model-400"
        self.settings.fallbacks = ["custom/model-ok"]
        status, _, raw = self.call("/v1/messages", {"model": "m", "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual((status, json.loads(raw)["error"]["type"]), (400, "invalid_request_error"))
        self.assertEqual(len(self.fake.requests), 1)

    def test_secrets_never_reach_the_client(self):
        self.settings.model = "custom/model-leak"
        status, _, raw = self.call("/v1/messages", {"model": "m", "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 500)
        self.assertNotIn(b"sk-test-secret-123", raw)
        self.assertIn(b"***", raw)

    def test_stream_options_retry(self):
        self.settings.model = "custom/model-noopts"
        status, _, raw = self.call("/v1/messages", {"model": "m", "stream": True, "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(sse_events(raw)[-1][0], "message_stop")
        self.assertNotIn("stream_options", self.fake.requests[-1]["body"])

    def test_mid_stream_failure_becomes_error_event(self):
        self.settings.model = "custom/model-midfail"
        status, _, raw = self.call("/v1/messages", {"model": "m", "stream": True, "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]})
        ev = sse_events(raw)
        self.assertEqual(status, 200)
        self.assertEqual(ev[-1][0], "error")
        self.assertIn("provider crashed", ev[-1][1]["error"]["message"])

    def test_unreachable_provider(self):
        self.settings.env["CUSTOM_BASE_URL"] = "http://127.0.0.1:9/v1"
        status, _, raw = self.call("/v1/messages", {"model": "m", "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 502)
        self.assertIn("cannot reach", json.loads(raw)["error"]["message"])

    def test_missing_model_setting(self):
        self.settings.model = ""
        status, _, raw = self.call("/v1/messages", {"model": "claude-x", "max_tokens": 9, "messages": []})
        self.assertEqual(status, 500)
        self.assertIn("MODEL is not set", json.loads(raw)["error"]["message"])


class ChatCompletionsTests(BridgeCase):
    def test_passthrough_non_stream_and_stream(self):
        status, _, raw = self.call("/v1/chat/completions", {"model": "anything", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "Hello from model-ok")
        status, ctype, raw = self.call("/v1/chat/completions", {"model": "anything", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual((status, ctype), (200, "text/event-stream"))
        self.assertIn(b"data: [DONE]", raw)
        self.assertEqual(self.fake.requests[-1]["body"]["model"], "model-ok")

    def test_openai_error_shape(self):
        self.settings.model = "custom/model-400"
        status, _, raw = self.call("/v1/chat/completions", {"model": "x", "messages": []})
        self.assertEqual(status, 400)
        self.assertIn("message", json.loads(raw)["error"])


class AuthTests(BridgeCase):
    extra = {"AGENTBRIDGE_AUTH_TOKEN": "local-token-789"}

    def test_token_required_and_both_header_styles_work(self):
        body = {"model": "m", "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(self.call("/v1/messages", body)[0], 401)
        self.assertEqual(self.call("/v1/messages", body, {"x-api-key": "wrong"})[0], 401)
        self.assertEqual(self.call("/v1/messages", body, {"x-api-key": "local-token-789"})[0], 200)
        self.assertEqual(self.call("/v1/messages", body, {"Authorization": "Bearer local-token-789"})[0], 200)
        self.assertEqual(self.call("/v1/models")[0], 401)
        self.assertEqual(self.call("/health")[0], 200)  # health stays open for launchers

    def test_public_bind_needs_a_token(self):
        with self.assertRaises(ConfigError):
            make_server(Settings.from_mapping({"AGENTBRIDGE_HOST": "0.0.0.0", "AGENTBRIDGE_PORT": "0"}))


if __name__ == "__main__":
    unittest.main()
