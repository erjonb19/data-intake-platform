"""The Gemini fallback's translation (S5.5), with no network: Anthropic-shaped messages to
Gemini contents and back, keeping each signed function-call part."""

from __future__ import annotations

from dip.agents.gemini import to_contents, to_message


def test_a_turn_round_trips_with_the_signed_part_kept():
    reply = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "Looking.", "thoughtSignature": "sig-a"},
        {"functionCall": {"name": "diff_schema", "args": {"feed": "f.x"}},
         "thoughtSignature": "sig-b"},
    ]}}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3,
                            "thoughtsTokenCount": 2}, "modelVersion": "g"}  # fmt: skip
    m = to_message(reply, "x")
    assert m.stop_reason == "tool_use" and [b["type"] for b in m.content] == ["text", "tool_use"]
    assert m.usage == {"input_tokens": 10, "output_tokens": 5}
    use = m.content[1]
    history = [
        {"role": "user", "content": "Do the task."},
        {"role": "assistant", "content": m.content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"],
                                      "content": '{"rows": 24}', "is_error": False}]},
    ]  # fmt: skip
    got = to_contents(history)
    assert got[0] == {"role": "user", "parts": [{"text": "Do the task."}]}
    assert got[1]["role"] == "model"
    assert got[1]["parts"][1]["thoughtSignature"] == "sig-b"  # sent back exactly as signed
    assert got[2]["parts"] == [{"functionResponse": {"name": "diff_schema",
                                                      "response": {"rows": 24}}}]  # fmt: skip


def test_errors_and_plain_text_results_are_objects():
    history = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x",
                                           "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": "denied by policy: no", "is_error": True}]},
    ]  # fmt: skip
    part = to_contents(history)[1]["parts"][0]["functionResponse"]
    assert part == {"name": "x", "response": {"error": {"result": "denied by policy: no"}}}


def test_a_final_answer_ends_the_turn():
    m = to_message({"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": "Proposed."}]}}]}, "gemini-3.1-flash-lite")  # fmt: skip
    assert m.stop_reason == "end_turn" and m.text == "Proposed."
