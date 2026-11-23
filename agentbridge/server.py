"""The local HTTP server: Anthropic /v1/messages and OpenAI /v1/chat/completions."""
from __future__ import annotations

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__
from .config import ConfigError, Settings
from .stream import StreamTranslator, format_event, iter_sse_data
from .translate import anthropic_to_openai, estimate_tokens, openai_to_anthropic
from .upstream import Upstream, UpstreamError

log = logging.getLogger("agentbridge")
MAX_BODY = 64 * 1024 * 1024
ANTHROPIC_ERRORS = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
                    404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error", 529: "overloaded_error"}


class BadRequest(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = f"AgentBridge/{__version__}"
    server: "BridgeServer"

    def log_message(self, fmt, *args):  # quiet default access log (it can contain paths only, but keep it tidy)
        pass

    # --- helpers ---------------------------------------------------------
    headers_sent = False

    def _send_json(self, status: int, obj: dict) -> None:
        self.headers_sent = True
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, openai: bool = False) -> None:
        message = self.server.upstream.scrub(message)
        if openai:
            self._send_json(status, {"error": {"message": message, "type": "agentbridge_error", "code": status}})
        else:
            kind = ANTHROPIC_ERRORS.get(status, "api_error")
            self._send_json(status, {"type": "error", "error": {"type": kind, "message": message}})
        log.info("%s %s -> %s %s", self.command, self.path.split("?")[0], status, message[:120])

    def _authorized(self) -> bool:
        token = self.server.settings.auth_token
        if not token:
            return True
        given = self.headers.get("x-api-key") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        return hmac.compare_digest(given.encode(), token.encode())

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("bad Content-Length") from None
        if length > MAX_BODY:
            raise BadRequest("request body too large")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise BadRequest("body is not valid JSON") from None
        if not isinstance(data, dict):
            raise BadRequest("body must be a JSON object")
        return data

    # --- routes ----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/health"):
            return self._send_json(200, {"status": "ok", "name": "agentbridge", "version": __version__})
        if not self._authorized():
            return self._error(401, "missing or wrong AgentBridge token", openai=path.startswith("/v1/models"))
        if path == "/v1/models":
            ids = list(dict.fromkeys(self.server.settings.targets()))
            return self._send_json(200, {"object": "list", "data": [{"id": i, "object": "model", "owned_by": "agentbridge"} for i in ids]})
        return self._error(404, f"no route for GET {path}")

    def do_POST(self):
        path = urlparse(self.path).path
        openai = path.startswith("/v1/chat/")
        if not self._authorized():
            return self._error(401, "missing or wrong AgentBridge token", openai)
        try:
            body = self._body()
            if path == "/v1/messages":
                return self._messages(body)
            if path == "/v1/messages/count_tokens":
                return self._send_json(200, {"input_tokens": estimate_tokens(body)})
            if path == "/v1/chat/completions":
                return self._chat(body)
            return self._error(404, f"no route for POST {path}", openai)
        except BadRequest as e:
            return self._error(400, str(e), openai)
        except ConfigError as e:
            return self._error(500, str(e), openai)
        except UpstreamError as e:
            return self._error(e.status if 400 <= e.status < 600 else 502, e.message, openai)
        except (BrokenPipeError, ConnectionResetError):
            log.info("client disconnected")
        except Exception:  # a bug: log it, and answer cleanly if we still can
            log.exception("unexpected error on %s", path)
            if not self.headers_sent:
                self._error(500, "internal AgentBridge error (see the server log)", openai)

    def _start_stream(self) -> None:
        self.headers_sent = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _messages(self, req: dict) -> None:
        s, up = self.server.settings, self.server.upstream
        name = req.get("model") or "agentbridge"
        chain = s.resolve(req.get("model", ""))
        with up.slots:
            resp, target = up.open(chain, lambda m: anthropic_to_openai(req, m))
            with resp:
                if not req.get("stream"):
                    try:
                        data = json.loads(resp.read())
                    except json.JSONDecodeError:
                        raise UpstreamError(502, f"{target}: provider did not return JSON") from None
                    log.info("POST /v1/messages -> 200 via %s", target)
                    return self._send_json(200, openai_to_anthropic(data, name))
                self._start_stream()
                tr = StreamTranslator(name)
                write = self.wfile.write
                for ev in tr.start():
                    write(format_event(*ev))
                try:
                    for chunk in iter_sse_data(resp):
                        if chunk.get("error") and not chunk.get("choices"):
                            raise UpstreamError(502, f"{target}: {json.dumps(chunk['error'])[:300]}")
                        for ev in tr.feed(chunk):
                            write(format_event(*ev))
                        self.wfile.flush()
                    for ev in tr.finish():
                        write(format_event(*ev))
                except (BrokenPipeError, ConnectionResetError):
                    raise
                except Exception as e:  # provider died mid-stream: tell the client in-band
                    msg = up.scrub(getattr(e, "message", str(e)))
                    write(format_event("error", {"type": "error", "error": {"type": "api_error", "message": msg}}))
                self.wfile.flush()
                log.info("POST /v1/messages (stream) -> 200 via %s", target)

    def _chat(self, req: dict) -> None:
        s, up = self.server.settings, self.server.upstream
        chain = s.resolve(req.get("model", ""))

        def build(model: str) -> dict:
            out = dict(req)
            out["model"] = model
            return out

        with up.slots:
            resp, target = up.open(chain, build)
            with resp:
                if req.get("stream"):
                    self._start_stream()
                    for line in resp:
                        self.wfile.write(line)
                        if line in (b"\n", b"\r\n"):
                            self.wfile.flush()
                    self.wfile.flush()
                else:
                    data = resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
        log.info("POST /v1/chat/completions -> 200 via %s", target)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.upstream = Upstream(settings)
        super().__init__((settings.host, settings.port), Handler)


def make_server(settings: Settings) -> BridgeServer:
    if settings.host not in ("127.0.0.1", "localhost", "::1") and not settings.auth_token:
        raise ConfigError("refusing to listen beyond localhost without AGENTBRIDGE_AUTH_TOKEN")
    return BridgeServer(settings)
