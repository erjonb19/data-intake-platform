"""Signed chain tokens (24.10): a run id names a chain only if the platform issued it.

A chain's run ids (`chain-x/incident`, `intake-y/map#2`) scope what its tools read and
write: an intake step reads its own chain's profile, the audit log groups by chain. The
run id travels in the tool call's `_run_id`, which the client sets, so without a check a
client holding any agent key could name another chain and read its candidate.

With DIP_RUN_TOKEN_KEY set (Terraform puts the same random key in the MCP server's Secret
and in the scheduled runners' Secrets; no agent and no model sees it), the runner that
claims the work signs the chain id it creates (`sign`), the client sends the token in
`_run_token` beside `_run_id`, and the MCP server refuses, audited as denied, any call
whose run id has no valid token for its chain (`verify`). The chain is the run id's chain
(`chain-x` of `chain-x/triage`), or the run id itself when it names no step: several tools
fall back to the raw run id as the chain, so a bare `intake-y` must be signed too. Tokens
expire after TTL. A call with no run id needs none. Without the key (CI's replays, a laptop)
nothing is signed or checked.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

KEY_VAR = "DIP_RUN_TOKEN_KEY"
TTL = 2 * 3600  # longer than a chain run's 20-minute task timeout, with room


def chain_for(run_id: str | None) -> str | None:
    """What a token for this run id must be for: its chain, or the run id itself."""
    from dip.agents.registry import chain_of

    return chain_of(run_id) or run_id or None


def _key(env: dict[str, str] | None = None) -> bytes | None:
    key = (os.environ if env is None else env).get(KEY_VAR, "")
    return key.encode() if key else None


def _mac(key: bytes, chain: str, expires: int) -> str:
    return hmac.new(key, f"{chain}|{expires}".encode(), hashlib.sha256).hexdigest()


def sign(
    chain: str | None, now: float | None = None, env: dict[str, str] | None = None
) -> str | None:
    """The token for `chain`, or None without a key or a chain."""
    key = _key(env)
    if not key or not chain:
        return None
    expires = int((time.time() if now is None else now) + TTL)
    return f"{expires}.{_mac(key, chain, expires)}"


def verify(chain: str | None, token: str | None, now: float | None = None,
           env: dict[str, str] | None = None) -> str | None:  # fmt: skip
    """Why the call is refused, or None. No key or no chain: never refused."""
    key = _key(env)
    if not key or not chain:
        return None
    if not token:
        return "the run id names a chain but carries no run token"
    expires_s, _, mac = token.partition(".")
    if not (expires_s.isascii() and expires_s.isdecimal() and mac.isascii()):
        return "the run token is malformed"
    expires = int(expires_s)
    if not hmac.compare_digest(mac.encode(), _mac(key, chain, expires).encode()):
        return "the run token is not for this chain"
    if expires < (time.time() if now is None else now):
        return "the run token has expired"
    return None
