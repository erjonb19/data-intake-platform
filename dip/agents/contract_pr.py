"""The human-approval step for a contract the contract-author agent proposed (Stage 5,
PLAN.md section 8.8; decided 2026-10-23, no GitHub App token).

`dip approvals execute <id>` on an approved `contract_pr` writes the candidate contract
as one commit on a new local branch, `contract/<feed>-approval-<id>`, cut from `main`
(DIP_CONTRACT_BASE), and prints the two commands that publish it: a `git push` and a
`gh pr create`. If the approval cannot be marked executed after the branch is written,
the row is `failed` and the branch stays: delete it (`git branch -D <branch>`) and propose
again. A person reviews the branch
and runs them; the agent never pushes, opens a PR or merges.

The commit is made with git plumbing on a temporary index (read-tree, hash-object,
update-index, write-tree, commit-tree, update-ref), so the person's working tree, index
and checked-out branch are never touched. Before writing, the candidate is checked again by
every contract rule (dip.contracts.parse_contract), and the contract file it replaces must
still be the one the agent proposed against.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from dip.contracts import REPO_ROOT, ContractError, parse_contract

FEED = re.compile(r"^(?P<pack>[a-z][a-z0-9_]*)\.(?P<name>[a-z][a-z0-9_]*)$")


def contract_path(feed: str, existing: Path | None, root: Path = REPO_ROOT) -> str:
    """The repo-relative path of the feed's contract: its current file, or for a new feed
    packs/<pack>/contracts/<name>.yaml in a pack that exists."""
    if existing is not None:
        return Path(existing).resolve().relative_to(root.resolve()).as_posix()
    m = FEED.match(feed)
    if not m or not (root / "packs" / m["pack"] / "contracts").is_dir():
        raise ContractError(f"{feed!r} must be <pack>.<name> for an existing pack")
    return f"packs/{m['pack']}/contracts/{m['name']}.yaml"


def text_sha256(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else None


def _git(
    root: Path, *args: str, stdin: str | None = None, env: dict | None = None, raw: bool = False
) -> str:
    """Bytes in and out: in text mode Windows would turn every \\n on stdin into \\r\\n,
    and the blob and message would be committed with CRLF."""
    data = stdin.replace("\r\n", "\n").encode("utf-8") if stdin is not None else None
    out = subprocess.run(["git", "-C", str(root), *args], input=data, capture_output=True,
                         env={**os.environ, **(env or {})}, check=False)  # fmt: skip
    if out.returncode:
        err = out.stderr.decode("utf-8", "replace").strip()[:500]
        raise RuntimeError(f"git {args[0]} failed: {err}")
    text = out.stdout.decode("utf-8", "replace")
    return text if raw else text.strip()


def write_branch(payload: dict[str, Any], approval_id: int, by: str, root: Path = REPO_ROOT,
                 base: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The commit is cut from the base branch (DIP_CONTRACT_BASE, default main), never from
    whatever the person has checked out, so the PR carries this one change."""
    base = base or os.environ.get("DIP_CONTRACT_BASE", "main")
    feed, rel = payload["feed"], payload["path"]
    target = root / rel
    if not target.resolve().is_relative_to((root / "packs").resolve()):
        raise ContractError(f"{rel}: contracts live under packs/")
    c = parse_contract(payload["contract_yaml"], target)
    if c.feed != feed:
        raise ContractError(f"the candidate is for {c.feed!r}, the proposal for {feed!r}")
    base_commit = _git(root, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}")
    # Against what the agent saw: the file on the base branch must not have changed since.
    try:
        current = _git(root, "show", f"{base_commit}:{rel}", raw=True)
    except RuntimeError:
        current = None
    if text_sha256(current) != payload.get("base_sha256"):
        raise RuntimeError(f"{rel} changed since the proposal was made: propose again")
    branch = f"contract/{feed}-approval-{approval_id}"
    if _git(root, "branch", "--list", branch):
        raise RuntimeError(f"branch {branch} exists already")
    text = payload["contract_yaml"]
    if not text.endswith("\n"):
        text += "\n"
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(root, "read-tree", base_commit, env=env)
        blob = _git(root, "hash-object", "-w", "--stdin", stdin=text)
        _git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}", env=env)
        tree = _git(root, "write-tree", env=env)
    message = (
        f"Contract for {feed}: {payload.get('mode', 'drift')} (approval {approval_id})\n\n"
        f"{payload.get('rationale', '')[:2000]}\n\n"
        f"Proposed by the contract-author agent; approved and written by {by}.\n"
    )
    commit = _git(root, "commit-tree", tree, "-p", base_commit, stdin=message)
    _git(root, "update-ref", f"refs/heads/{branch}", commit, "")
    body = Path(_git(root, "rev-parse", "--git-common-dir"))
    if not body.is_absolute():
        body = root / body
    body = body / f"dip-contract-pr-{approval_id}.md"
    body.write_text(_pr_body(payload, approval_id, by), encoding="utf-8")
    return {
        "branch": branch,
        "commit": commit,
        "path": rel,
        "next": [
            f"git push -u origin {branch}",
            f'gh pr create --base {base} --head {branch} --title "Contract for {feed}'
            f' (approval {approval_id})" --body-file "{body.as_posix()}"',
        ],
        "note": "Review the branch (git show " + branch + "), then run the two commands.",
    }


def _pr_body(payload: dict[str, Any], approval_id: int, by: str) -> str:
    v = payload.get("validation", {})
    loose = payload.get("loosening", [])
    lines = [
        f"Contract for `{payload['feed']}` ({payload.get('mode', 'drift')}), proposed by the "
        f"contract-author agent and approved by {by} (approval {approval_id}).",
        "",
        "## Why",
        payload.get("rationale", "")[:4000],
        "",
        "## Dry run on the sample file",
        f"- accepted {v.get('accepted')}, rejected {v.get('rejected')}, "
        f"file rejected: {v.get('file_rejected')}",
        f"- reason codes: {v.get('reason_codes', {})}",
        "",
        "## What it loosens",
    ]
    lines += [f"- `{x['where']}`: {x['rule']} {x['from']!r} -> {x['to']!r}" for x in loose]
    lines += ["- nothing"] if not loose else []
    lines += ["", "All data behind this proposal is generated and invented."]
    return "\n".join(lines) + "\n"
