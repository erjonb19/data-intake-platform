"""The intake team's own tools (Stage 10, S10.4; PLAN.md section 8.12).

  report_profile     Propose tier, the profile worker's. Labels each column of the job's
                     file (`phi`: true or false; `role`: one of ROLES) and stores the job's
                     profile: facts about the file built here, in code, from what
                     `diff_schema` sees (dip.contract_author.observe), plus those labels.
  get_intake_profile Read tier, the map worker's: the job (feed, mode, file) and that
                     profile. It is the only view of the file the map worker gets.
  submit_candidate   Propose tier, the map worker's: the candidate it stands behind (by the
                     candidate_id validate_contract gave it in this chain) and its rationale.
                     Nothing is proposed yet: the validator, then the orchestrator, decide.

The profile carries no vendor text that could read as an instruction: counts and types;
value shapes (letters as A, digits as 9); code values only when every value fits
dip.contract_author.CODE (short, upper case or digits); column and sheet names only when
they fit the contract's column-name rule, else `column_<n>` / `sheet_<n>`, flagged.
"""

from __future__ import annotations

import json
import re
from typing import Any

from dip.contract_author import CODE, observe

ROLES = ("identifier", "code", "amount", "count", "date", "name", "free_text", "other")
NAME_OK = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SHEET_OK = re.compile(r"^[A-Za-z0-9_-]{1,31}$")
MAX_NAME_WORDS = 6  # more underscore-separated words than this reads as a sentence


def safe_name(name: str, i: int) -> str:
    """A vendor's column name as the map worker may see it: as is when it could be a contract
    column, else `column_<position>` (1-based)."""
    if NAME_OK.match(name) and len(name.split("_")) <= MAX_NAME_WORDS:
        return name
    return f"column_{i + 1}"


def safe_code(reason: str) -> str:
    """A reason code with a vendor column name in it (SCHEMA.UNEXPECTED_COLUMN:<name>),
    with the name kept only when it could be a contract column."""
    head, sep, tail = reason.partition(":")
    if not sep:
        return reason
    return (
        f"{head}:{tail}"
        if NAME_OK.match(tail) and len(tail.split("_")) <= MAX_NAME_WORDS
        else f"{head}:<column name not shown>"
    )


FILE_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def build_profile(seen: dict[str, Any], file_name: str | None = None) -> dict[str, Any]:
    """Facts about the file, from observe()'s output, with nothing a vendor wrote as text.
    `file_name`: the file's name, kept only when it is a plain name (letters, digits, `.`,
    `_`, `-`): the contract's filename_pattern and filename_date are written from it."""
    columns = []
    for i, c in enumerate(seen["columns"]):
        name = safe_name(c["column"], i)
        item: dict[str, Any] = {
            "column": name,
            "position": i + 1,
            "name_usable": name == c["column"],
            "declared": c["declared"],
            "declared_phi": c["phi"],
            "inferred_type": c["inferred_type"],
            "empty": c["empty"],
            "distinct": c["distinct"],
        }
        if "declared_type" in c:
            item["declared_type"] = c["declared_type"]
        if c.get("rule_failures"):
            item["rule_failures"] = {r: v["count"] for r, v in c["rule_failures"].items()}
        if c.get("shapes"):
            item["shapes"] = c["shapes"]
        codes = c.get("codes") or [v for v, _ in c.get("top_values", [])]
        codes = [v for v in codes if CODE.match(v)]
        if codes:
            item["codes"] = codes
            if c.get("top_values"):
                item["code_counts"] = {v: n for v, n in c["top_values"] if CODE.match(v)}
        columns.append(item)
    by_raw = {c["column"]: safe_name(c["column"], i) for i, c in enumerate(seen["columns"])}
    facts = dict(seen.get("file") or {})
    if "sheets" in facts:
        facts["sheets"] = [
            s if SHEET_OK.match(s) else f"sheet_{i + 1}" for i, s in enumerate(facts["sheets"])
        ]
    if file_name is not None:
        facts["file_name"] = file_name if FILE_NAME_OK.match(file_name) else "<name not shown>"
    return {
        "rows": seen["rows"],
        "file": facts,
        "columns": columns,
        "missing_declared": seen["missing_declared"],
        "unexpected": [by_raw.get(n, "column_?") for n in seen["unexpected"]],
        "order_differs": seen["order_differs"],
    }


def register_intake_tools(
    reg: Any, Tool: Any, conn: Any, file_of: Any, fmt_of: Any, contracts: dict[str, Any]
) -> None:
    """`file_of(feed, sha)` and `fmt_of(feed, name)`: the author tools' file lookup."""
    from dip.agents.registry import chain_of

    obj = {"type": "object", "additionalProperties": False}

    def _job(job_id: int) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                "select job_id, feed, mode, file_sha256, status, chain_id, profile"
                " from ops.intake_job where job_id = %s",
                (job_id,),
            )
            row = cur.fetchone()
        conn.commit()
        keys = ("job_id", "feed", "mode", "file_sha256", "status", "chain_id", "profile")
        return dict(zip(keys, row, strict=True)) if row else None

    def _running(job_id: int, run_id: str | None) -> dict[str, Any] | str:
        job = _job(job_id)
        if job is None or job["status"] != "running":
            return f"job {job_id} is not a running intake job"
        if job["chain_id"] and chain_of(run_id) != job["chain_id"]:
            return f"job {job_id} belongs to another chain"
        return job

    def report_profile(
        principal: str, run_id: str | None, job_id: int, columns: list[dict[str, Any]]
    ) -> dict:
        job = _running(job_id, run_id)
        if isinstance(job, str):
            return {"error": job}
        full, data, name, _ = file_of(job["feed"], job["file_sha256"])
        profile = build_profile(
            observe(contracts.get(job["feed"]), data, fmt_of(job["feed"], name)), name
        )
        known = {c["column"] for c in profile["columns"]}
        labels: dict[str, dict[str, Any]] = {}
        for item in columns:
            col, phi, role = item.get("column"), item.get("phi"), item.get("role")
            if col not in known:
                return {"error": f"no column {col!r} in the profile: {sorted(known)}"}
            if not isinstance(phi, bool) or role not in ROLES:
                return {"error": f"{col}: phi must be true or false, role one of {list(ROLES)}"}
            labels[col] = {"phi": phi, "role": role}
        for c in profile["columns"]:
            c["phi"] = labels.get(c["column"], {}).get("phi")
            c["role"] = labels.get(c["column"], {}).get("role")
        with conn.cursor() as cur:
            cur.execute(
                "update ops.intake_job set profile = %s where job_id = %s",
                (json.dumps(profile), job_id),
            )
        conn.commit()
        return {
            "job_id": job_id,
            "labelled": len(labels),
            "unlabelled": sorted(known - set(labels)),
        }

    def get_intake_profile(principal: str, run_id: str | None, job_id: int) -> dict:
        job = _running(job_id, run_id)
        if isinstance(job, str):
            return {"error": job}
        if not job["profile"]:
            return {"error": f"job {job_id} has no profile yet"}
        return {
            "job_id": job_id,
            "feed": job["feed"],
            "mode": job["mode"],
            "file_sha256": job["file_sha256"],
            "has_contract": job["feed"] in contracts,
            "profile": job["profile"],
            "notice": "Built by the platform from the file; no vendor text.",
        }

    def submit_candidate(
        principal: str, run_id: str | None, candidate_id: str, rationale: str
    ) -> dict:
        chain = chain_of(run_id) or run_id or ""
        with conn.cursor() as cur:
            cur.execute(
                "update ops.contract_candidate set submitted = true, rationale = %s"
                " where chain_id = %s and candidate_id = %s and created_by = %s"
                " returning feed",
                (rationale[:4000], chain, candidate_id, principal),
            )
            row = cur.fetchone()
        conn.commit()
        if row is None:
            return {
                "error": "unknown candidate_id: submit the candidate_id validate_contract "
                "returned to you in this run"
            }
        return {
            "candidate_id": candidate_id,
            "submitted": True,
            "note": "The validator checks it again; a person decides.",
        }

    reg.register(
        Tool(
            "report_profile",
            "propose",
            "Label every column of the job's file: phi (true when it identifies a person or is "
            "free text) and role. Stores the job's profile for the map worker.",
            {
                **obj,
                "properties": {
                    "job_id": {"type": "integer"},
                    "columns": {
                        "type": "array",
                        "maxItems": 200,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "column": {"type": "string"},
                                "phi": {"type": "boolean"},
                                "role": {"type": "string", "enum": list(ROLES)},
                            },
                            "required": ["column", "phi", "role"],
                        },
                    },
                },
                "required": ["job_id", "columns"],
            },
            report_profile,
        )
    )
    reg.register(
        Tool(
            "get_intake_profile",
            "read",
            "The intake job (feed, mode, file sha256) and its profile: per column the type, "
            "counts, shapes, code values, the profile worker's phi and role labels.",
            {**obj, "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]},
            get_intake_profile,
        )
    )
    reg.register(
        Tool(
            "submit_candidate",
            "propose",
            "Submit the candidate contract you stand behind, by the candidate_id validate_contract "
            "returned, with your rationale. Do not submit when the drift is a vendor mistake.",
            {
                **obj,
                "properties": {"candidate_id": {"type": "string"}, "rationale": {"type": "string"}},
                "required": ["candidate_id", "rationale"],
            },
            submit_candidate,
        )
    )


ISSUES = ("loosening", "phi", "unsupported_rule", "mistake_taken_for_change",
          "affected_models", "other")  # fmt: skip
VERDICTS = ("pass", "revise", "reject")


def register_review_tools(reg: Any, Tool: Any, conn: Any, check: Any) -> None:
    """The reviewer's tools (Stage 10, S10.5). `check(feed, yaml, sha)`: the author tools'
    validation (every rule, the dry run, the loosening list, the diff)."""
    from dip.agents.registry import chain_of

    obj = {"type": "object", "additionalProperties": False}

    def _candidate(run_id: str | None, cid: str) -> dict[str, Any] | str:
        chain = chain_of(run_id) or ""
        with conn.cursor() as cur:
            cur.execute(
                "select c.feed, c.file_sha256, c.contract_yaml, c.rationale, c.submitted,"
                " c.validator_valid, c.dbt_ok, j.job_id, j.mode, j.profile"
                " from ops.contract_candidate c join ops.intake_job j"
                "   on j.chain_id = c.chain_id and j.status = 'running'"
                " where c.chain_id = %s and c.candidate_id = %s",
                (chain, cid),
            )
            row = cur.fetchone()
        conn.commit()
        if row is None:
            return "no candidate with that id in this chain's running job"
        keys = ("feed", "file_sha256", "contract_yaml", "rationale", "submitted",
                "validator_valid", "dbt_ok", "job_id", "mode", "profile")  # fmt: skip
        c = dict(zip(keys, row, strict=True))
        if not (c["submitted"] and c["validator_valid"] and c["dbt_ok"]):
            return "the candidate has not been submitted and passed by the validator"
        return c | {"chain": chain}

    def get_review_packet(principal: str, run_id: str | None, candidate_id: str) -> dict:
        c = _candidate(run_id, candidate_id)
        if isinstance(c, str):
            return {"error": c}
        checked = check(c["feed"], c["contract_yaml"], c["file_sha256"])
        dry = checked.get("dry_run") or {}
        if "reason_codes" in dry:
            dry = {**dry, "reason_codes": {safe_code(k): v for k, v in dry["reason_codes"].items()}}
        return {
            "feed": c["feed"], "mode": c["mode"], "candidate_id": candidate_id,
            "candidate_yaml": c["contract_yaml"], "diff_against_current": checked.get("diff", ""),
            "dry_run": dry, "loosening": checked.get("loosening", []),
            "map_worker_rationale": c["rationale"], "profile": c["profile"],
            "notice": "The profile is built by the platform; the file itself is not shown.",
        }  # fmt: skip

    def report_review(principal: str, run_id: str | None, candidate_id: str, verdict: str,
                      reasons: list[dict[str, Any]]) -> dict:  # fmt: skip
        c = _candidate(run_id, candidate_id)
        if isinstance(c, str):
            return {"error": c}
        if verdict not in VERDICTS:
            return {"error": f"verdict must be one of {list(VERDICTS)}"}
        clean = [{"issue": r.get("issue"), "detail": str(r.get("detail", ""))[:300]}
                 for r in reasons[:10]]  # fmt: skip
        if any(r["issue"] not in ISSUES for r in clean):
            return {"error": f"each reason's issue is one of {list(ISSUES)}"}
        if verdict != "pass" and not clean:
            return {"error": "a revise or reject needs at least one reason"}
        review = {"verdict": verdict, "reasons": clean, "by": principal, "run_id": run_id}
        with conn.cursor() as cur:
            cur.execute("update ops.contract_candidate set review = %s"
                        " where chain_id = %s and candidate_id = %s",
                        (json.dumps(review), c["chain"], candidate_id))  # fmt: skip
            cur.execute("update ops.intake_job set review = %s where job_id = %s",
                        (json.dumps(review), c["job_id"]))  # fmt: skip
        conn.commit()
        return {"candidate_id": candidate_id, "verdict": verdict,
                "note": "Recorded. A person still decides."}  # fmt: skip

    reg.register(Tool("get_review_packet", "read",
        "Everything about a submitted, validated candidate contract: its YAML, the diff "
        "against the current contract, the dry run's counts, what it loosens, the map "
        "worker's rationale, and the file's profile (built by the platform).",
        {**obj, "properties": {"candidate_id": {"type": "string"}},
         "required": ["candidate_id"]}, get_review_packet))  # fmt: skip
    reg.register(Tool("report_review", "propose",
        "Your verdict on the candidate: pass, revise (send it back with reasons) or reject "
        "(nothing should be proposed). Each reason names an issue and its evidence.",
        {**obj, "properties": {
            "candidate_id": {"type": "string"},
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "reasons": {"type": "array", "maxItems": 10, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"issue": {"type": "string", "enum": list(ISSUES)},
                               "detail": {"type": "string"}},
                "required": ["issue", "detail"]}}},
         "required": ["candidate_id", "verdict", "reasons"]}, report_review))  # fmt: skip
