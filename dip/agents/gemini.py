"""The Gemini fallback (PLAN.md section 7.1; S5.5): the same `LLM` interface as
AnthropicLLM, over Google's generateContent REST API, with no new dependency.

The agents speak Anthropic-shaped messages (text, tool_use and tool_result blocks); this
class translates them both ways:
  - tools become function declarations (their JSON schema as `parametersJsonSchema`);
  - an assistant turn's text and tool_use blocks become `model` parts; each tool_use block
    keeps the raw Gemini part it came from (`_gemini_part`), because Gemini 3 models sign
    their function calls (`thoughtSignature`) and want the signed part sent back as is;
  - tool_result blocks become `functionResponse` parts, named after the call they answer.

Model and key from config/llm.toml's gemini fallback (`gemini-3.1-flash-lite`, key in
GEMINI_API_KEY; `gemini-2.5-flash` is closed to new users). Overload (503) and rate limits
(429) are retried with backoff. Only generated, invented data is sent (decided 2026-10-23).
"""

from __future__ import annotations

import json
import os
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from typing import Any

from dip.agents.llm import CONFIG, Message

API = "https://generativelanguage.googleapis.com/v1beta/models"
RETRY = (429, 500, 502, 503, 504)


def gemini_settings() -> dict[str, Any]:
    with CONFIG.open("rb") as f:
        cfg = tomllib.load(f)
    for fb in cfg.get("fallback", []):
        if fb.get("provider") == "gemini":
            return {"model": os.environ.get("DIP_GEMINI_MODEL", fb.get("model",
                                                                         "gemini-3.1-flash-lite")),
                    "api_key_env": fb.get("api_key_env", "GEMINI_API_KEY")}  # fmt: skip
    raise LookupError("no gemini fallback in config/llm.toml")


def to_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-shaped messages as Gemini contents."""
    names: dict[str, str] = {}  # tool_use id -> function name
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": "user" if m["role"] == "user" else "model",
                        "parts": [{"text": content}]})  # fmt: skip
            continue
        parts = []
        for b in content:
            kind = b.get("type")
            if kind == "text" and b.get("text"):
                parts.append(b.get("_gemini_part") or {"text": b["text"]})
            elif kind == "tool_use":
                names[b["id"]] = b["name"]
                call = {"functionCall": {"name": b["name"], "args": b["input"]}}
                parts.append(b.get("_gemini_part") or call)
            elif kind == "tool_result":
                text = b.get("content", "")
                try:
                    payload = json.loads(text) if text[:1] in "{[" else {"result": text}
                except ValueError:
                    payload = {"result": text}
                if not isinstance(payload, dict):
                    payload = {"result": payload}
                if b.get("is_error"):
                    payload = {"error": payload}
                parts.append({"functionResponse": {
                    "name": names.get(b["tool_use_id"], "tool"), "response": payload}})  # fmt: skip
        if parts:
            out.append({"role": "user" if m["role"] == "user" else "model", "parts": parts})
    return out


def to_message(d: dict[str, Any], model: str) -> Message:
    """A blocked prompt (no candidates) or an answer with no parts is not an answer: its
    stop reason says so, so the evals cannot score it as a deliberate decline."""
    if not d.get("candidates"):
        reason = (d.get("promptFeedback") or {}).get("blockReason", "no_candidates")
        return Message([], f"blocked:{reason}", {}, d.get("modelVersion") or model)
    cand = d["candidates"][0]
    blocks: list[dict[str, Any]] = []
    for part in (cand.get("content") or {}).get("parts", []):
        if "functionCall" in part:
            fc = part["functionCall"]
            blocks.append({"type": "tool_use", "id": f"g-{uuid.uuid4().hex[:12]}",
                           "name": fc["name"], "input": fc.get("args") or {},
                           "_gemini_part": part})  # fmt: skip
        elif part.get("text") and not part.get("thought"):
            blocks.append({"type": "text", "text": part["text"], "_gemini_part": part})
    finish = cand.get("finishReason", "")
    if not blocks:
        return Message(
            [], f"empty:{(finish or 'none').lower()}", {}, d.get("modelVersion") or model
        )
    stop = ("tool_use" if any(b["type"] == "tool_use" for b in blocks)
            else "max_tokens" if finish == "MAX_TOKENS" else "end_turn" if finish == "STOP"
            else (finish or "end_turn").lower())  # fmt: skip
    u = d.get("usageMetadata", {})
    out_tokens = u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
    usage = {"input_tokens": u.get("promptTokenCount", 0), "output_tokens": out_tokens}
    return Message(blocks, stop, usage, d.get("modelVersion") or model)


class GeminiLLM:
    def __init__(self, max_tokens: int = 8000, tries: int = 6):
        cfg = gemini_settings()
        self.model, self.max_tokens, self.tries = cfg["model"], max_tokens, tries
        self.key = os.environ.get(cfg["api_key_env"], "")
        if not self.key:
            raise LookupError(f"{cfg['api_key_env']} is not set")

    def create(self, *, system: str, tools: list[dict], messages: list[dict]) -> Message:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": to_contents(messages),
            "tools": [{"functionDeclarations": [
                {"name": t["name"], "description": t.get("description", ""),
                 "parametersJsonSchema": t["input_schema"]} for t in tools]}],
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }  # fmt: skip
        req = urllib.request.Request(
            f"{API}/{self.model}:generateContent", json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.key},
        )  # fmt: skip
        for attempt in range(self.tries):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return to_message(json.loads(r.read()), self.model)
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRY or attempt == self.tries - 1:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                    raise RuntimeError(f"Gemini {exc.code}: {detail}") from exc
                wait = exc.headers.get("Retry-After", "") if exc.headers else ""
                time.sleep(min(60, int(wait) if wait.isdigit() else 5 * 2**attempt))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self.tries - 1:
                    raise RuntimeError(f"Gemini unreachable: {exc}") from exc
                time.sleep(min(60, 5 * 2**attempt))
        raise RuntimeError("unreachable")
