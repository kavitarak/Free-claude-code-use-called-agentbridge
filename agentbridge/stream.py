"""Turn an OpenAI-style SSE stream into Anthropic Messages SSE events."""
from __future__ import annotations

import json
from typing import Iterable, Iterator

from .translate import STOP_REASONS, new_message_id, new_tool_id

Event = tuple[str, dict]


def iter_sse_data(lines: Iterable[bytes]) -> Iterator[dict]:
    """Yield the JSON payload of every `data:` line; stops at [DONE]."""
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue  # blank lines and ':' keep-alive comments
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def format_event(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


class StreamTranslator:
    """Stateful converter: feed() each OpenAI chunk, then call finish() once."""

    def __init__(self, model: str):
        self.model = model
        self.msg_id = new_message_id()
        self._next_index = 0
        self._open: tuple[str, int] | None = None  # ("text"|"tool", block index)
        self._tools: dict[int, dict] = {}  # openai tool index -> {block, id, had_args}
        self._finish_reason: str | None = None
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._chars = 0
        self._saw_tool = False

    def start(self) -> list[Event]:
        return [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            )
        ]

    # --- block helpers ---------------------------------------------------
    def _close(self) -> list[Event]:
        if not self._open:
            return []
        kind, idx = self._open
        events: list[Event] = []
        if kind == "tool":
            tool = next(t for t in self._tools.values() if t["block"] == idx)
            if not tool["had_args"]:  # tool with no arguments still needs valid JSON
                events.append(
                    ("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "input_json_delta", "partial_json": "{}"}})
                )
        events.append(("content_block_stop", {"type": "content_block_stop", "index": idx}))
        self._open = None
        return events

    def _text(self, text: str) -> list[Event]:
        events: list[Event] = []
        if not self._open or self._open[0] != "text":
            events += self._close()
            idx = self._next_index
            self._next_index += 1
            self._open = ("text", idx)
            events.append(("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}}))
        self._chars += len(text)
        events.append(("content_block_delta", {"type": "content_block_delta", "index": self._open[1], "delta": {"type": "text_delta", "text": text}}))
        return events

    def _tool(self, call: dict) -> list[Event]:
        events: list[Event] = []
        oi = call.get("index", 0)
        fn = call.get("function") or {}
        tool = self._tools.get(oi)
        if tool is None:
            events += self._close()
            idx = self._next_index
            self._next_index += 1
            tool = {"block": idx, "id": call.get("id") or new_tool_id(), "had_args": False}
            self._tools[oi] = tool
            self._open = ("tool", idx)
            self._saw_tool = True
            events.append(
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": idx, "content_block": {"type": "tool_use", "id": tool["id"], "name": fn.get("name", ""), "input": {}}},
                )
            )
        args = fn.get("arguments")
        if args:
            tool["had_args"] = True
            self._chars += len(args)
            events.append(("content_block_delta", {"type": "content_block_delta", "index": tool["block"], "delta": {"type": "input_json_delta", "partial_json": args}}))
        return events

    # --- public ----------------------------------------------------------
    def feed(self, chunk: dict) -> list[Event]:
        usage = chunk.get("usage")
        if usage:
            self._prompt_tokens = usage.get("prompt_tokens", self._prompt_tokens)
            self._completion_tokens = usage.get("completion_tokens", self._completion_tokens)
        events: list[Event] = []
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                events += self._text(delta["content"])
            for call in delta.get("tool_calls") or []:
                events += self._tool(call)
            if choice.get("finish_reason"):
                self._finish_reason = choice["finish_reason"]
        return events

    def finish(self) -> list[Event]:
        events = self._close()
        if self._next_index == 0:  # the model said nothing: still return one empty text block
            events.append(("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
            events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
        stop = STOP_REASONS.get(self._finish_reason or "stop", "end_turn")
        if self._saw_tool:
            stop = "tool_use"
        out_tokens = self._completion_tokens or max(1, self._chars // 4)
        usage = {"output_tokens": out_tokens}
        if self._prompt_tokens:
            usage["input_tokens"] = self._prompt_tokens
        events.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": usage}))
        events.append(("message_stop", {"type": "message_stop"}))
        return events
