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
