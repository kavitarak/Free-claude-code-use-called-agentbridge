"""Convert between the Anthropic Messages API and OpenAI-style chat completions."""
from __future__ import annotations

import json
import uuid
from typing import Any

STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use"}


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def new_tool_id() -> str:
    return "toolu_" + uuid.uuid4().hex[:24]


def _text_of(content: Any) -> str:
    """Flatten a string or a list of content blocks to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                parts.append("[image omitted]")
    return "\n".join(p for p in parts if p)


def _image_part(block: dict) -> dict | None:
    src = block.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        return {"type": "image_url", "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"}}
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def _merge_adjacent_users(messages: list[dict]) -> list[dict]:
    """Join back-to-back user messages (some chat templates insist on alternating roles)."""
    out: list[dict] = []
    for m in messages:
        prev = out[-1] if out else None
        if prev and prev["role"] == "user" and m["role"] == "user":
            a = prev["content"] if isinstance(prev["content"], list) else [{"type": "text", "text": prev["content"]}]
            b = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            parts = a + b
            if all(p["type"] == "text" for p in parts):
                prev["content"] = "\n\n".join(p["text"] for p in parts)
            else:
                prev["content"] = parts
        else:
            out.append(dict(m))
    return out


def anthropic_to_openai(req: dict, model: str, *, stream_usage: bool = True) -> dict:
    """Build an OpenAI chat-completions body from an Anthropic /v1/messages body."""
    messages: list[dict] = []
    system = _text_of(req.get("system"))
    if system:
        messages.append({"role": "system", "content": system})

    for m in req.get("messages", []):
        role, content = m.get("role"), m.get("content")
        if role in ("system", "developer"):
            # Claude Code can send notes as system messages in the middle of a chat. Many providers
            # only accept one system message at the top, so deliver the note as a user message.
            note = _text_of(content)
            if note:
                messages.append({"role": "user", "content": f"<system-reminder>\n{note}\n</system-reminder>"})
            continue
        if isinstance(content, str):
            if content:
                messages.append({"role": role, "content": content})
            continue
        if role == "assistant":
            texts, calls = [], []
            for b in content or []:
                t = b.get("type")
                if t == "text" and b.get("text"):
                    texts.append(b["text"])
                elif t == "tool_use":
                    calls.append(
                        {
                            "id": b.get("id") or new_tool_id(),
                            "type": "function",
                            "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input") or {})},
                        }
                    )
                # thinking / redacted_thinking blocks are provider specific: drop them
            if texts or calls:
                msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
                if calls:
                    msg["tool_calls"] = calls
                messages.append(msg)
        else:
            parts: list[dict] = []
            results: list[dict] = []
            for b in content or []:
                t = b.get("type")
                if t == "text" and b.get("text"):
                    parts.append({"type": "text", "text": b["text"]})
                elif t == "image":
                    img = _image_part(b)
                    if img:
                        parts.append(img)
                elif t == "tool_result":
                    results.append(b)
            # tool results must directly follow the assistant tool call, so they come first
            for r in results:
                body = _text_of(r.get("content"))
                if r.get("is_error"):
                    body = "Error: " + body
                messages.append({"role": "tool", "tool_call_id": r.get("tool_use_id", ""), "content": body})
            if parts:
                if all(p["type"] == "text" for p in parts):
                    messages.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
                else:
                    messages.append({"role": "user", "content": parts})

    out: dict[str, Any] = {"model": model, "messages": _merge_adjacent_users(messages)}
    if "max_tokens" in req:
        out["max_tokens"] = req["max_tokens"]
    for k in ("temperature", "top_p"):
        if req.get(k) is not None:
            out[k] = req[k]
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]

    tools = []
    for t in req.get("tools") or []:
        if not t.get("input_schema"):  # Anthropic server-side tools have no schema: skip them
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            }
        )
    if tools:
        out["tools"] = tools
        tc = req.get("tool_choice") or {}
        kind = tc.get("type")
        if kind == "any":
            out["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif kind == "none":
            out["tool_choice"] = "none"
        elif kind == "auto":
            out["tool_choice"] = "auto"

    if req.get("stream"):
        out["stream"] = True
        if stream_usage:
            out["stream_options"] = {"include_usage": True}
    return out


def parse_arguments(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {"value": value}


def openai_to_anthropic(resp: dict, model: str) -> dict:
    """Convert a non-streaming chat completion into an Anthropic message."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks: list[dict] = []
    text = msg.get("content")
    if isinstance(text, list):  # some providers return content parts
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if text:
        blocks.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or new_tool_id(),
                "name": fn.get("name", ""),
                "input": parse_arguments(fn.get("arguments")),
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    stop = STOP_REASONS.get(choice.get("finish_reason") or "stop", "end_turn")
    if any(b["type"] == "tool_use" for b in blocks):
        stop = "tool_use"
    usage = resp.get("usage") or {}
    return {
        "id": new_message_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def estimate_tokens(req: dict) -> int:
    """Rough token count (about 4 characters per token) for /count_tokens."""
    return max(1, len(json.dumps(req, ensure_ascii=False)) // 4)
