# The intake team (Stage 10)

When a file lands for a feed with no contract, or one that drifted from its contract, the
intake team writes the contract and a person decides (PLAN.md §8.12). One orchestrator in
code and three workers, plus (S10.5) a reviewer:

```
 landing/_onboard/<pack>.<name>/   ──┐
 an executed propose_contract_change ┴─▶ ops.intake_job (queued)
                                          │  agent_chain DAG, intake_jobs task, every 5 min
                                          ▼
   profile ──▶ map ──▶ validate ──▶ propose ──▶ ops.approvals (contract_pr, pending)
                ▲          │                       │ a person approves and executes
                └──────────┘ once                  ▼
                                          a local branch: the contract + its dbt models
```

| Member | What it is | Key and policy | What it may do |
|---|---|---|---|
| **profile** | a model | `profile` (`agents/policies/profile.yaml`) | Read the file through `diff_schema` (PHI and undeclared columns masked); `report_profile` labels each column `phi` and `role` from fixed lists. The only member that sees vendor content |
| **map** | the contract author, a model | `contract_author` | `get_intake_profile`, `get_contract(_text)`, `validate_contract`, `submit_candidate`. It never sees the file, and it cannot propose to a person |
| **validate** | code | `validator` | `validate_contract` and `check_dbt_models` again on the submitted candidate; the server records the verdicts under the validator's name |
| **propose** | code, the orchestrator | `intake_orchestrator` | `propose_contract_pr` only |

The graph is `dip/agents/intake_graph.py`; jobs are claimed and run by
`dip/agents/intake_runner.py` (the DAG's task, in the agent venv). Routing is in code, as
for the incident chain (§4.1): every route is fixed, so a model supervisor would decide
nothing and add an injection surface.

## What keeps vendor text away from the map worker

The profile the map worker reads (`get_intake_profile`) is built by the server, in code,
from what `diff_schema` sees (`dip/agents/intake_tools.py`):

- counts, types, value shapes (`A999999999`), and rule failures as counts only;
- code values only when every value fits the short code pattern (at most 12, each 8
  characters or fewer, upper case or digits);
- column names only when they could be contract columns (`^[a-z][a-z0-9_]*$`, at most six
  words); any other name is `column_<n>`, flagged `name_usable: false`. Sheet names alike;
- `validate_contract`'s reason codes, which can carry a column name, get the same rule.

The profile worker's own output is labels from fixed lists, so a hijacked profile worker
can mislabel a column, and nothing more. The evals check this directly: in both injection
cases, no injected text appears anywhere the map worker read (its task or any tool result).

## What the server checks before anything is proposed

The run id comes from the client, so nothing rests on it alone. `propose_contract_pr`
accepts a candidate only:

- from the `intake_orchestrator` principal (the map worker's policy no longer has the tool);
- if the map worker submitted it (`submit_candidate`) in the same chain;
- if the validator, under its own key, found it valid, accepting the file, and its
  generated dbt models parsing (`ops.contract_candidate.validator_valid`, `dbt_ok`);
- while that chain's intake job is `running`, and of the job's mode.

The proposal carries the map worker's rationale, the dry run, the loosening list, the
diff, the generated dbt files and their hash, and the intake job and chain ids. Nothing is
written (no contract, no model, no branch, no warehouse table) until a person approves it
and runs `dip approvals execute`.

## Running it

- In the cluster: put a sample in MinIO under `landing/_onboard/<pack>.<name>/`; within
  about 5 minutes the `agent_chain` DAG queues and runs the job (with a model key and the
  team's keys in the `dip-agent-chain` Secret, S10.6). Without them, jobs wait in
  `ops.intake_job`.
- Evals: `python -m dip contracts eval [--record | --live --repeat N]` runs every
  contract-author case through the graph (`DIP_EVAL_DSN`, a throwaway database). Each case
  has two recordings, `<case>_profile.json` and `<case>_map.json` (`_map2.json` when the
  validator sent the candidate back).
