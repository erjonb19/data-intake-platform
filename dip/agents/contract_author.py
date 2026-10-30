"""The contract-author agent (Stage 5, S5.2; PLAN.md sections 4.2 and 8.8): one agent, two
modes. `onboard` writes a contract for a new vendor's sample file; `drift` edits a feed's
contract so it accepts a vendor's deliberate change. It only Reads and Proposes: the result
is an ops.approvals row that a person approves and publishes (dip/agents/contract_pr.py).

Like the triage agent it is an MCP client with its own key; the server's policy
(agents/policies/contract_author.yaml, PHI masked) decides and audits every call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from dip.agents.llm import LLM, PROMPT_DIR
from dip.agents.triage import TriageRun, run_agent

PROMPT = (PROMPT_DIR / "contract_author.md").read_text(encoding="utf-8")


def task(mode: str, feed: str, file_sha256: str, note: str = "") -> str:
    if mode == "onboard":
        text = (f"Onboard a new vendor feed, `{feed}`. Its sample file is {file_sha256}. "
                "Write its first contract (version 1), validate it, and propose it.")  # fmt: skip
    elif mode == "drift":
        text = (f"Feed `{feed}`: the file {file_sha256} no longer fits its contract. Find "
                "what changed, decide whether it is a deliberate change the contract should "
                "accept, and if so edit the contract, validate it, and propose it.")  # fmt: skip
    else:
        raise ValueError("mode must be onboard or drift")
    return text + (f" Context from the platform: {note}" if note else "")


@dataclass
class AuthorRun:
    run: TriageRun
    mode: str
    feed: str

    @property
    def proposal(self) -> dict[str, Any] | None:
        """The last successful propose_contract_pr (the policy allows two per run)."""
        for c in reversed(self.run.calls):
            if c.name == "propose_contract_pr" and not c.is_error:
                return {**c.args, **json.loads(c.result)}
        return None

    @property
    def validations(self) -> int:
        return sum(1 for c in self.run.calls if c.name == "validate_contract")


async def run(client: Any, llm: LLM, mode: str, feed: str, file_sha256: str, note: str = "",
              max_turns: int = 24, run_id: str | None = None) -> AuthorRun:  # fmt: skip
    r = await run_agent(client, llm, PROMPT, task(mode, feed, file_sha256, note), max_turns,
                        run_id)  # fmt: skip
    return AuthorRun(r, mode, feed)
