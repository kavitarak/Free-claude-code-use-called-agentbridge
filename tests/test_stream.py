import json
import unittest

from agentbridge.stream import StreamTranslator, format_event, iter_sse_data


def run(chunks):
    tr = StreamTranslator("claude-test")
    events = tr.start()
    for c in chunks:
        events += tr.feed(c)
    return events + tr.finish()


def d(**delta):
    return {"choices": [{"index": 0, "delta": delta}]}


class StreamTests(unittest.TestCase):
    def test_text_stream(self):
        ev = run([d(content="Hel"), d(content="lo"), {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}])
        names = [e[0] for e in ev]
        self.assertEqual(names, ["message_start", "content_block_start", "content_block_delta", "content_block_delta",
                                 "content_block_stop", "message_delta", "message_stop"])
        self.assertEqual("".join(e[1]["delta"]["text"] for e in ev if e[0] == "content_block_delta"), "Hello")
        self.assertEqual(ev[-2][1]["delta"]["stop_reason"], "end_turn")
        self.assertEqual(ev[-2][1]["usage"], {"output_tokens": 2, "input_tokens": 9})

    def test_text_then_tool_call_with_split_arguments(self):
        ev = run([d(content="Sure."),
                  d(tool_calls=[{"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": ""}}]),
                  d(tool_calls=[{"index": 0, "function": {"arguments": "{\"city\": "}}]),
                  d(tool_calls=[{"index": 0, "function": {"arguments": "\"Bhilai\"}"}}]),
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}])
        starts = [e[1] for e in ev if e[0] == "content_block_start"]
        self.assertEqual([s["content_block"]["type"] for s in starts], ["text", "tool_use"])
        self.assertEqual([s["index"] for s in starts], [0, 1])
        self.assertEqual(starts[1]["content_block"]["id"], "call_1")
        args = "".join(e[1]["delta"]["partial_json"] for e in ev if e[0] == "content_block_delta" and e[1]["index"] == 1)
        self.assertEqual(json.loads(args), {"city": "Bhilai"})
        self.assertEqual([e[1]["index"] for e in ev if e[0] == "content_block_stop"], [0, 1])
        self.assertEqual(ev[-2][1]["delta"]["stop_reason"], "tool_use")  # tool seen, even with finish "stop"

    def test_two_tools_and_tool_without_arguments(self):
        ev = run([d(tool_calls=[{"index": 0, "function": {"name": "list_files"}}]),
                  d(tool_calls=[{"index": 1, "id": "b", "function": {"name": "read", "arguments": "{}"}}])])
        deltas = [e[1] for e in ev if e[0] == "content_block_delta"]
        self.assertEqual(deltas[0], {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}})
        self.assertTrue([e for e in ev if e[0] == "content_block_start"][0][1]["content_block"]["id"].startswith("toolu_"))

    def test_empty_stream_and_length_stop(self):
        ev = run([{"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}])
        self.assertEqual([e[0] for e in ev], ["message_start", "content_block_start", "content_block_stop", "message_delta", "message_stop"])
        self.assertEqual(ev[-2][1]["delta"]["stop_reason"], "max_tokens")

    def test_sse_parsing_and_format(self):
        lines = [b": ping\n", b"\n", b'data: {"a": 1}\n', b"data: not json\n", b"data: [DONE]\n", b'data: {"b": 2}\n']
        self.assertEqual(list(iter_sse_data(lines)), [{"a": 1}])
        self.assertEqual(format_event("ping", {"type": "ping"}), b'event: ping\ndata: {"type": "ping"}\n\n')


if __name__ == "__main__":
    unittest.main()
