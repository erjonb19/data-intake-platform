"""The contract-author agent (Stage 5, S5.2; PLAN.md sections 4.2 and 8.8): one agent, two
modes. `onboard` writes a contract for a new vendor's sample file; `drift` edits a feed's
contract so it accepts a vendor's deliberate change.

From Stage 10 (S10.4, section 8.12) it is the map worker of the intake team and runs only
inside the intake graph (dip/agents/intake_graph.py): it reads the profile the platform
built from the file (never the file), writes a candidate, validates it and submits it. The
validator checks it again in code, and the intake orchestrator proposes it for a person.
Its prompt is agents/prompts/contract_author.md and its policy
agents/policies/contract_author.yaml; the old single-agent loop, which proposed directly,
is gone, so there is one path to a contract proposal.
"""
