"""Command line: agentbridge serve | init | claude | env | doctor."""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import urllib.request

from . import __version__
from .config import PROVIDERS, ConfigError, Settings, env_file, file_is_private, home_dir
from .server import make_server

LOCAL_TOKEN = "agentbridge-local"

ENV_TEMPLATE = """# AgentBridge settings. Keep this file private: it holds your API keys.
# Pick models as provider/model-id. Providers: nvidia_nim, openrouter, groq, gemini,
# deepseek, ollama, lmstudio, custom. Check each provider's site for current model ids,
# free-tier limits and terms.

# Default model for every request
MODEL=nvidia_nim/<model-id>

# Optional: a different model per Claude Code tier (opus / sonnet / haiku)
# MODEL_OPUS=openrouter/<model-id>
# MODEL_SONNET=nvidia_nim/<model-id>
# MODEL_HAIKU=groq/<model-id>

# Optional: tried in order when the main model is rate limited or down
# FALLBACK_MODELS=openrouter/<model-id>,ollama/<model-id>

# API keys (several keys for one provider: separate them with commas)
NVIDIA_NIM_API_KEY=
OPENROUTER_API_KEY=
GROQ_API_KEY=
GEMINI_API_KEY=
DEEPSEEK_API_KEY=

# Local servers (no key needed)
# OLLAMA_BASE_URL=http://localhost:11434/v1
# LMSTUDIO_BASE_URL=http://localhost:1234/v1

# Any other OpenAI-compatible endpoint
# CUSTOM_BASE_URL=https://example.com/v1
# CUSTOM_API_KEY=

# Server
AGENTBRIDGE_PORT=8082
# AGENTBRIDGE_AUTH_TOKEN=   # set this if anything other than your own tools can reach the port
# MAX_CONCURRENCY=4
# REQUEST_TIMEOUT=120
"""


def client_url(s: Settings) -> str:
    host = "127.0.0.1" if s.host in ("0.0.0.0", "::", "localhost") else s.host
    return f"http://{host}:{s.port}"


def healthy(url: str) -> bool:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/health", timeout=1.5) as r:
            return json.loads(r.read()).get("name") == "agentbridge"
    except Exception:
        return False


def warn_problems(s: Settings) -> None:
    for p in s.validate():
        print(f"! {p}", file=sys.stderr)
    f = env_file()
    if f.exists() and not file_is_private(f):
        print(f"! {f} can be read by other users. Fix: chmod 600 {f}", file=sys.stderr)


def cmd_serve(s: Settings, args) -> int:
    if args.host:
        s.host = args.host
    if args.port:
        s.port = args.port
    warn_problems(s)
    server = make_server(s)
    print(f"AgentBridge {__version__} listening on {client_url(s)}  (Ctrl+C to stop)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_init(s: Settings, args) -> int:
    f = env_file()
    if f.exists() and not args.force:
        print(f"{f} already exists (use --force to overwrite)")
        return 0
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(ENV_TEMPLATE, encoding="utf-8")
    if os.name != "nt":
        os.chmod(f, 0o600)
    print(f"Created {f}\nAdd one API key and set MODEL, then run: agentbridge claude")
    return 0


def cmd_claude(s: Settings, rest: list[str]) -> int:
    exe = shutil.which("claude")
    if not exe:
        print("Claude Code was not found. Install it first: https://docs.claude.com/en/docs/claude-code", file=sys.stderr)
        return 1
    url = client_url(s)
    server = None
    if not healthy(url):
        problems = s.validate()
        if problems:
            warn_problems(s)
            print("Fix the settings above (agentbridge doctor), then try again.", file=sys.stderr)
            return 1
        # log to a file: the Claude Code screen must stay clean
        log_path = home_dir() / "agentbridge.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.getLogger().handlers.clear()
        logging.basicConfig(filename=log_path, level=logging.INFO, format="%(asctime)s %(message)s")
        server = make_server(s)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)  # never send a real Anthropic key to the local bridge
    env["ANTHROPIC_BASE_URL"] = url
    env["ANTHROPIC_AUTH_TOKEN"] = s.auth_token or LOCAL_TOKEN
    try:
        return subprocess.call([exe, *rest], env=env)
    except KeyboardInterrupt:
        return 130
    finally:
        if server:
            server.shutdown()
            server.server_close()


def cmd_env(s: Settings, args) -> int:
    url, token = client_url(s), s.auth_token or LOCAL_TOKEN
    pairs = [("ANTHROPIC_BASE_URL", url), ("ANTHROPIC_AUTH_TOKEN", token), ("OPENAI_BASE_URL", url + "/v1"), ("OPENAI_API_KEY", token)]
    for k, v in pairs:
        if args.shell == "powershell":
            print(f'$env:{k}="{v}"')
        elif args.shell == "cmd":
            print(f"set {k}={v}")
        else:
            print(f'export {k}="{v}"')
    return 0


def cmd_doctor(s: Settings, args) -> int:
    ok = True
    print(f"AgentBridge {__version__} · Python {platform.python_version()} · {platform.system()}")
    f = env_file()
    print(f"settings file : {f} ({'found' if f.exists() else 'missing: run agentbridge init'})")
    if f.exists() and not file_is_private(f):
        print("  ! readable by other users: chmod 600 it")
        ok = False
    configured = [p.name for p in PROVIDERS.values() if p.key_env and s.keys(p.name)]
    print("keys set for  : " + (", ".join(f"{n} ({len(s.keys(n))})" for n in configured) or "none"))
    print(f"default model : {s.model or '-'}")
    for tier, target in s.tier_models.items():
        print(f"  {tier:<6} -> {target}")
    if s.fallbacks:
        print("fallbacks     : " + " → ".join(s.fallbacks))
    for problem in s.validate():
        print(f"  ! {problem}")
        ok = False
    print(f"claude in PATH: {'yes' if shutil.which('claude') else 'no'}")
    print(f"server        : {client_url(s)} {'running' if healthy(client_url(s)) else 'not running'}")
    print("OK" if ok else "Fix the items marked with !")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentbridge", description="Use Claude Code and OpenAI-compatible tools with the model provider you choose.")
    p.add_argument("--version", action="version", version=f"agentbridge {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("serve", help="run the local gateway")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    ip = sub.add_parser("init", help="create ~/.agentbridge/.env")
    ip.add_argument("--force", action="store_true")
    sub.add_parser("claude", help="start Claude Code through AgentBridge (extra args go to claude)", add_help=False)
    ep = sub.add_parser("env", help="print the variables other tools need")
    ep.add_argument("--shell", choices=["bash", "powershell", "cmd"], default="powershell" if os.name == "nt" else "bash")
    sub.add_parser("doctor", help="check your setup")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    try:
        s = Settings.load()
    except ConfigError as e:
        print(f"! {e}", file=sys.stderr)
        return 2
    if argv and argv[0] == "claude":  # pass everything after `claude` straight to Claude Code
        return cmd_claude(s, argv[1:])
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "serve":
            return cmd_serve(s, args)
        if args.cmd == "init":
            return cmd_init(s, args)
        if args.cmd == "env":
            return cmd_env(s, args)
        if args.cmd == "doctor":
            return cmd_doctor(s, args)
    except ConfigError as e:
        print(f"! {e}", file=sys.stderr)
        return 2
    build_parser().print_help()
    return 0
