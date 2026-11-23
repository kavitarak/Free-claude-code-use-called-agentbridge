import os
import tempfile
import unittest
from pathlib import Path

from agentbridge.config import ConfigError, Settings, file_is_private, parse_env_file, split_target


class ConfigTests(unittest.TestCase):
    def test_split_target_keeps_slashes_and_colons_in_model(self):
        self.assertEqual(split_target("openrouter/vendor/model:free"), ("openrouter", "vendor/model:free"))

    def test_split_target_rejects_unknown_provider(self):
        with self.assertRaises(ConfigError):
            split_target("nope/model")
        with self.assertRaises(ConfigError):
            split_target("openrouter")

    def test_tiers_fallbacks_and_explicit_targets(self):
        s = Settings.from_mapping({"MODEL": "groq/base", "MODEL_HAIKU": "ollama/small",
                                   "FALLBACK_MODELS": "openrouter/fb1, groq/base ,openrouter/fb2"})
        self.assertEqual(s.resolve("claude-haiku-4-5"), ["ollama/small", "openrouter/fb1", "groq/base", "openrouter/fb2"])
        self.assertEqual(s.resolve("claude-sonnet-5-5")[0], "groq/base")  # no sonnet override -> default
        self.assertEqual(s.resolve("deepseek/chat")[0], "deepseek/chat")  # explicit provider/model
        self.assertEqual(s.resolve("")[0], "groq/base")

    def test_resolve_without_model_explains_fix(self):
        with self.assertRaisesRegex(ConfigError, "agentbridge init"):
            Settings.from_mapping({}).resolve("claude-opus")

    def test_validate_reports_missing_key_and_unsafe_host(self):
        s = Settings.from_mapping({"MODEL": "nvidia_nim/m", "AGENTBRIDGE_HOST": "0.0.0.0"})
        problems = " | ".join(s.validate())
        self.assertIn("NVIDIA_NIM_API_KEY", problems)
        self.assertIn("unsafe", problems)
        ok = Settings.from_mapping({"MODEL": "ollama/m"})
        self.assertEqual(ok.validate(), [])

    def test_multiple_keys_and_base_url_override(self):
        s = Settings.from_mapping({"GROQ_API_KEY": "k1, k2,,", "OLLAMA_BASE_URL": "http://box:11434/v1/"})
        self.assertEqual(s.keys("groq"), ["k1", "k2"])
        self.assertEqual(s.keys("ollama"), [""])
        self.assertEqual(s.base_url("ollama"), "http://box:11434/v1")

    def test_bad_port_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            Settings.from_mapping({"AGENTBRIDGE_PORT": "abc"})

    def test_env_file_parsing_and_override(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text('# comment\nMODEL="groq/a"\nGROQ_API_KEY=\'secret\'\nnot a line\n', encoding="utf-8")
            self.assertEqual(parse_env_file(f), {"MODEL": "groq/a", "GROQ_API_KEY": "secret"})
            s = Settings.load(environ={"MODEL": "ollama/b"}, path=f)
            self.assertEqual(s.model, "ollama/b")  # real environment wins
            self.assertEqual(s.keys("groq"), ["secret"])

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_file_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text("x=1")
            os.chmod(f, 0o644)
            self.assertFalse(file_is_private(f))
            os.chmod(f, 0o600)
            self.assertTrue(file_is_private(f))


if __name__ == "__main__":
    unittest.main()
