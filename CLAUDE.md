# Working rules for this repository

## Model recordings

Replays are fingerprinted on what the agent sends the model (system prompt, tools, and
every tool result: runbook search hits, Kubernetes events, database rows). Any change to
those after a recording was made breaks its replay.

- **Record last.** Make recordings (`--record`, Claude or Gemini, incident, triage, demo)
  only after every review fix on the branch is applied. A fix that lands after recording
  means recording again.
- **The full replay suite passes in CI before merge.** A branch that adds or changes
  recordings is not merged until the CI `test` job, which replays every recording, is
  green on the branch's final commit.
- **Recordings reach the branch through the proof workflow.** Dispatch
  `gemini-recordings` or `claude-recordings` on an `s11-*` branch; its `commit-results`
  job commits the files to that branch only after the scrub, the key and auth-header check
  and the replay check pass. A push by the workflow's token starts no CI, so the branch's
  next push by a person (the decisions-log line, say) is the one CI must pass.
