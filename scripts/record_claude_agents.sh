#!/usr/bin/env bash
# Record the triage agent's evals on Claude (24.17): the triage cases (cases.yaml, the
# held-out set, the cases made from rejections), Act mode (triage_act) and the
# break-and-fix loop (break_loop), plus any reviewer case that has no recording yet (24.29).
# Run by the proof workflow's claude-agent-recordings job, because the laptop lacks the
# memory. Needs ANTHROPIC_API_KEY, DIP_SPEND_CAP_USD and DIP_EVAL_DSN (a throwaway
# database). Everything runs in one process, so the cap holds for all of it together. The
# incident cases and the demo are claude-recordings' (scripts/record_claude_incidents.sh).
set -uo pipefail

python - <<'EOF'
import sys
from dip.__main__ import main
from dip.agents import llm, review_evals

T = "agents/evals/triage"
runs = [
    ("triage", ["triage", "eval", "--record", "--out", "artifacts/rec/triage"]),
    ("triage held-out", ["triage", "eval", "--record", "--cases-file", f"{T}/holdout.yaml",
                         "--out", "artifacts/rec/holdout"]),
    ("triage from rejections", ["triage", "eval", "--record", "--cases-file",
                                f"{T}/from_rejections.yaml", "--out", "artifacts/rec/rej"]),
    ("triage act", ["triage", "act-eval", "--record", "--out", "artifacts/rec/act"]),
    ("break-and-fix loop", ["triage", "loop", "--record", "--out", "artifacts/rec/loop"]),
]
# The reviewer's own cases did not change: only a new case (none recorded yet) is recorded.
new = [c["name"] for c in review_evals.load_cases()
       if not (review_evals.RECORDINGS / f"{c['name']}.json").exists()]
if new:
    runs.append(("reviewer (new cases)", ["contracts", "review-eval", "--record",
                                          *[a for n in new for a in ("--case", n)]]))
failed = []
for name, argv in runs:
    rc = main(argv)
    print(f"{name}: exit {rc}, spent ${llm.SPENT['usd']:.4f} so far")
    if rc:
        failed.append(name)
print(f"spent ${llm.SPENT['usd']:.4f} in all")
sys.exit(1 if failed else 0)
EOF
status=$?

# Scrub: the key, and any auth header, out of every recording (none should be there: a
# recording holds the model's turns and request hashes, never the request); then check.
dirs=(agents/evals/triage/recordings agents/evals/triage_act/recordings
      agents/evals/break_loop/recordings agents/evals/reviewer/recordings)
if ! python - "${dirs[@]}" <<'EOF'
import os, pathlib, re, sys
key = os.environ["ANTHROPIC_API_KEY"]
auth = re.compile(r'("?(x-api-key|authorization)"?\s*[:=]\s*)"[^"]*"', re.I)
for d in sys.argv[1:]:
    for p in pathlib.Path(d).glob("*.json"):  # not gemini/: those are another job's
        t = p.read_text("utf-8")
        s = auth.sub(r'\1"[redacted]"', t.replace(key, "[redacted]"))
        if s != t:
            p.write_text(s, "utf-8")
            print(f"scrubbed {p}")
EOF
then
  echo "scrub: FAILED"; exit 1
fi
echo "scrub: ok"
for d in "${dirs[@]}"; do
  if grep -lF "$ANTHROPIC_API_KEY" "$d"/*.json; then
    echo "the key is still in a recording"; exit 1
  fi
done
exit $status
