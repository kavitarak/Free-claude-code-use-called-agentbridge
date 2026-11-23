import io
import os
import socket
import stat
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agentbridge import cli
from tests.fakes import FakeProvider


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"AGENTBRIDGE_HOME": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_init_writes_private_template_once(self):
        code, out = self.run_cli("init")
        f = self.home / ".env"
        self.assertEqual(code, 0)
        self.assertIn("MODEL=", f.read_text())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        f.write_text("MODEL=groq/mine\n")
        self.run_cli("init")
        self.assertEqual(f.read_text(), "MODEL=groq/mine\n")  # not overwritten without --force

    def test_env_prints_both_api_styles(self):
        _, out = self.run_cli("env", "--shell", "bash")
        self.assertIn('export ANTHROPIC_BASE_URL="http://127.0.0.1:8082"', out)
        self.assertIn('export OPENAI_BASE_URL="http://127.0.0.1:8082/v1"', out)
        _, out = self.run_cli("env", "--shell", "powershell")
        self.assertIn('$env:ANTHROPIC_AUTH_TOKEN="agentbridge-local"', out)

    def test_doctor_flags_missing_model(self):
        code, out = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("MODEL is not set", out)

    def test_doctor_never_prints_key_values(self):
        with mock.patch.dict(os.environ, {"MODEL": "groq/m", "GROQ_API_KEY": "gsk-very-secret"}):
            code, out = self.run_cli("doctor")
        self.assertNotIn("gsk-very-secret", out)
        self.assertIn("groq (1)", out)

    @unittest.skipIf(os.name == "nt", "uses a POSIX shebang script")
    def test_claude_launcher_end_to_end(self):
        fake = FakeProvider()
        self.addCleanup(fake.close)
        bindir = self.home / "bin"
        bindir.mkdir()
        result = self.home / "result.txt"
        script = bindir / "claude"
        # a stand-in for Claude Code: it talks to the bridge exactly the way the real CLI does
        script.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, os, sys, urllib.request
            base, token = os.environ["ANTHROPIC_BASE_URL"], os.environ["ANTHROPIC_AUTH_TOKEN"]
            body = json.dumps({{"model": "claude-sonnet-5-5", "max_tokens": 32,
                               "messages": [{{"role": "user", "content": " ".join(sys.argv[1:])}}]}}).encode()
            req = urllib.request.Request(base + "/v1/messages?beta=true", data=body,
                                         headers={{"Authorization": "Bearer " + token, "Content-Type": "application/json"}})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
            text = json.loads(opener.open(req, timeout=10).read())["content"][0]["text"]
            open({str(result)!r}, "w").write(text + "|" + str("ANTHROPIC_API_KEY" in os.environ))
            """))
        script.chmod(0o755)
        env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "MODEL": "custom/model-ok", "CUSTOM_BASE_URL": fake.url,
               "CUSTOM_API_KEY": "k-123456", "AGENTBRIDGE_PORT": str(free_port()), "ANTHROPIC_API_KEY": "real-anthropic-key"}
        with mock.patch.dict(os.environ, env):
            code = cli.main(["claude", "-p", "hello"])
        self.assertEqual(code, 0)
        self.assertEqual(result.read_text(), "Hello from model-ok|False")  # answered via bridge, real key not passed on
        self.assertEqual(fake.requests[-1]["body"]["messages"][-1]["content"], "-p hello")

    def test_claude_launcher_without_claude_installed(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(cli.main(["claude"]), 1)


if __name__ == "__main__":
    unittest.main()
