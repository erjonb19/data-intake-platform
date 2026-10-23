"""Recordings refuse to replay against a different prompt, tool list or tool output."""

from __future__ import annotations

import pytest

from dip.agents.llm import Message, Recorder, Recording


class Fixed:
    def create(self, **kwargs):
        return Message([{"type": "text", "text": "ok"}], "end_turn")


def test_replay_checks_what_the_model_is_sent(tmp_path):
    rec = Recorder(Fixed())
    sent = {"system": "S", "tools": [{"name": "t"}], "messages": [{"role": "user", "content":
            "group open since 2026-10-21"}]}  # fmt: skip
    rec.create(**sent)
    rec.save(tmp_path / "r.json")
    # Dates are masked: the same output on another day replays.
    later = {**sent, "messages": [{"role": "user", "content": "group open since 2026-12-01"}]}
    assert Recording(tmp_path / "r.json").create(**later).text == "ok"
    for changed in ({**sent, "system": "S2"}, {**sent, "tools": []},
                    {**sent, "messages": [{"role": "user", "content": "other"}]}):  # fmt: skip
        with pytest.raises(RuntimeError, match="re-record"):
            Recording(tmp_path / "r.json").create(**changed)
