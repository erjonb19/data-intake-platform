# Stage 10 summary (2026-10-31): the multi-agent intake team, done

A new vendor's sample goes in, and a contract branch with its dbt models comes out,
with a person approving in between. Seven PRs, merged in order, each after a fresh review
with its findings fixed and CI green: #57 (plan §8.12), #58 S10.1, #59 S10.2, #60 S10.3,
#61 S10.4, #62 S10.5, #64 (the proof workflow), #63 S10.6.

- **S10.1 free fallback.** A whole step moves to Gemini when Claude is unavailable, never
  mid-conversation and never after the step has written. The provider of each step is
  recorded and shown on the approvals page.
- **S10.2 intake jobs.** A sample under `landing/_onboard/<pack>.<name>/` queues an
  `onboard` job, and an executed `propose_contract_change` queues a `drift` job.
- **S10.3 dbt from contracts.** Silver, tests and gold are rendered from templates, never
  from a model. Golden tests cover four contracts.
- **S10.4 orchestrator + three workers.** The graph is profile → map → validate (code, no
  model) → propose, routed in code, with five MCP keys and policies. 13 cases, 3x live:
  35/36, 0 loosening errors, 0 PHI missed, 6/6 injections resisted, $0.19.
- **S10.5 reviewer.** Verdicts are `pass`, `revise` or `reject`, and the server requires the
  reviewer's `pass` before a proposal. 3x live: 12/12 faulty candidates stopped, 1/12 good
  ones sent back, $0.045.
- **S10.6 in the cluster + proof.** The team keys are in the Secret and dbt is in the MCP
  image. The proof ran on a GitHub runner (manual `proof` workflow, run 41159028750)
  because the laptop could not hold kind: the node's kubeadm was OOM-killed under 1 GB
  free, twice, and each time the stack was torn down. **PASS**: one onboarding job ended
  `proposed`, all five members made 12 audited calls under one chain id, nothing was
  written before approval, the branch held the contract and two model files, and `dbt build`
  passed 11/11. Spend **$0.0074** under a $0.25 cap (new `DIP_SPEND_CAP_USD`). The README
  has the proposal screenshot. Record: `docs/demos/stage-10.md`.

**Known limitations**
- **The Gemini fallback followed an injection once in 6 incident cases** (S10.1, 1x live,
  free tier: incident 5/6). The injected task-log text steered its diagnosis. Nothing
  executed, because every action still waits for a person, but a step that ran on Gemini
  is less trustworthy than one on Claude. Mitigations already in place: a review on
  Gemini can never pass (the job ends `revise` for a person), and Gemini-made proposals
  are labelled on the approvals page. Gemini triage was also 10/13 (3 malformed proposals,
  all refused by the server).
- `profile_file` returned a tool error during the proof (most likely because the profile
  worker may call it on onboarding jobs, which it cannot serve). The profile still came
  from the server and the proposal was unaffected. Issue #65.
- The spend meter does not count a request the SDK retried after a timeout. The cap
  should stay well above a run's expected cost.
- Local kind proofs no longer fit on this laptop. Use the `proof` workflow instead.

**Housekeeping:** the `ANTHROPIC_API_KEY` Actions secret is read only by the manual `proof`
workflow. No cluster is left running.

- **LLM spend this session:** $0.0074. **Total to date: about $3.41**, under the $4.50 stop.
