"""24.35: which errors an agent may read in full. A tool raises some on purpose, with text
written for the model (a bad argument, nothing configured, an approval it may not run, a
contract rule); the model needs that text to correct its next call. Any other error (a
database or network failure, a bug) carries hosts, SQL, paths or values: the model gets only
an id, and the server's log has the full error under it."""

from __future__ import annotations

import logging
import secrets


def for_the_model(exc: BaseException) -> bool:
    """ValueError (ContractError is one), LookupError but not KeyError or IndexError (a raw
    key or index from inside the code), and any class marked `for_the_model = True`
    (ApprovalError)."""
    if isinstance(exc, (KeyError, IndexError)):
        return False
    return isinstance(exc, (ValueError, LookupError)) or getattr(type(exc), "for_the_model", False)


def hidden(log: logging.Logger, what: str, exc: BaseException) -> str:
    """Log `exc` in full under a new id; return the text the model may see instead."""
    error_id = secrets.token_hex(6)
    log.error("%s failed (error %s)", what, error_id, exc_info=exc)
    return f"internal error (error {error_id})"
