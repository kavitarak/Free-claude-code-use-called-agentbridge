import json
import unittest

from agentbridge.translate import anthropic_to_openai, estimate_tokens, openai_to_anthropic


class RequestTests(unittest.TestCase):
    def test_system_text_and_params(self):
        out = anthropic_to_openai({"system": [{"type": "text", "text": "Be brief."}], "max_tokens": 50, "temperature": 0.2,
                                   "stop_sequences": ["END"], "messages": [{"role": "user", "content": "Hi"}]}, "m")
        self.assertEqual(out["model"], "m")
        self.assertEqual(out["messages"], [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Hi"}])
        self.assertEqual((out["max_tokens"], out["temperature"], out["stop"]), (50, 0.2, ["END"]))
        self.assertNotIn("stream", out)

    def test_tool_round_trip_order(self):
        req = {"messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Checking."},
                                              {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Bhilai"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "31C"}]},
                                         {"type": "text", "text": "and tomorrow?"}]},
        ]}
        msgs = anthropic_to_openai(req, "m")["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool", "user"])
        call = msgs[1]["tool_calls"][0]
        self.assertEqual(msgs[1]["content"], "Checking.")
        self.assertEqual((call["id"], call["function"]["name"]), ("toolu_1", "get_weather"))
        self.assertEqual(json.loads(call["function"]["arguments"]), {"city": "Bhilai"})
        self.assertEqual(msgs[2], {"role": "tool", "tool_call_id": "toolu_1", "content": "31C"})

    def test_tool_error_and_images(self):
        req = {"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "is_error": True, "content": "not found"},
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}]}]}
        msgs = anthropic_to_openai(req, "m")["messages"]
        self.assertEqual(msgs[0]["content"], "Error: not found")
        self.assertEqual(msgs[1]["content"][1]["image_url"]["url"], "data:image/jpeg;base64,QUJD")

    def test_tools_and_tool_choice(self):
        tools = [{"name": "get_weather", "description": "d", "input_schema": {"type": "object"}},
                 {"type": "web_search_20250305", "name": "web_search"}]  # server tool: skipped
        for choice, expected in [({"type": "any"}, "required"), ({"type": "auto"}, "auto"), ({"type": "none"}, "none"),
                                 ({"type": "tool", "name": "get_weather"}, {"type": "function", "function": {"name": "get_weather"}})]:
            out = anthropic_to_openai({"messages": [], "tools": tools, "tool_choice": choice}, "m")
            self.assertEqual(len(out["tools"]), 1)
            self.assertEqual(out["tools"][0]["function"]["parameters"], {"type": "object"})
            self.assertEqual(out["tool_choice"], expected)

    def test_mid_conversation_system_messages_become_user_notes(self):
        # real Claude Code sends these, both as a string and as a list of blocks
        req = {"system": "top", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "text", "text": "there"}]},
            {"role": "system", "content": [{"type": "text", "text": "date changed"}]},
            {"role": "assistant", "content": "ok"},
            {"role": "system", "content": "another note"},
            {"role": "user", "content": "go on"},
        ]}
        msgs = anthropic_to_openai(req, "m")["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertEqual(sum(m["role"] == "system" for m in msgs), 1)  # only the one at the top
        self.assertIn("date changed", msgs[1]["content"])
        self.assertIn("another note", msgs[3]["content"])
        self.assertTrue(msgs[3]["content"].endswith("go on"))

    def test_adjacent_user_messages_with_images_are_merged_as_parts(self):
        req = {"messages": [{"role": "user", "content": "look"},
                            {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="}}]}]}
        msgs = anthropic_to_openai(req, "m")["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual([p["type"] for p in msgs[0]["content"]], ["text", "image_url"])

    def test_stream_flags(self):
        out = anthropic_to_openai({"messages": [], "stream": True}, "m")
        self.assertEqual(out["stream_options"], {"include_usage": True})
        self.assertNotIn("stream_options", anthropic_to_openai({"messages": [], "stream": True}, "m", stream_usage=False))


class ResponseTests(unittest.TestCase):
    def test_text_answer(self):
        msg = openai_to_anthropic({"choices": [{"message": {"content": "Hi"}, "finish_reason": "length"}],
                                   "usage": {"prompt_tokens": 3, "completion_tokens": 1}}, "claude-x")
        self.assertEqual(msg["content"], [{"type": "text", "text": "Hi"}])
        self.assertEqual((msg["stop_reason"], msg["model"], msg["usage"]), ("max_tokens", "claude-x", {"input_tokens": 3, "output_tokens": 1}))
        self.assertTrue(msg["id"].startswith("msg_"))

    def test_tool_calls_even_when_finish_reason_is_stop(self):
        msg = openai_to_anthropic({"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c", "function": {"name": "f", "arguments": "{\"a\": 1}"}},
            {"function": {"name": "g", "arguments": "not json"}}]}, "finish_reason": "stop"}]}, "m")
        self.assertEqual(msg["stop_reason"], "tool_use")
        self.assertEqual(msg["content"][0], {"type": "tool_use", "id": "c", "name": "f", "input": {"a": 1}})
        self.assertEqual(msg["content"][1]["input"], {})
        self.assertTrue(msg["content"][1]["id"].startswith("toolu_"))

    def test_empty_answer_still_has_a_block(self):
        self.assertEqual(openai_to_anthropic({"choices": [{"message": {}}]}, "m")["content"], [{"type": "text", "text": ""}])

    def test_estimate_tokens(self):
        self.assertGreaterEqual(estimate_tokens({"messages": [{"role": "user", "content": "x" * 400}]}), 100)


if __name__ == "__main__":
    unittest.main()
