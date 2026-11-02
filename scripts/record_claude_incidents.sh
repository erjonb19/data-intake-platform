#!/usr/bin/env bash
# Record the incident eval cases and the demo on Claude (Stage 11, S11.7), into
# agents/evals/incident/recordings/ and agents/evals/demo/recordings/. Run by the proof
# workflow's claude-recordings job, because the laptop lacks the memory. Needs
# ANTHROPIC_API_KEY, DIP_SPEND_CAP_USD, DIP_EVAL_DSN (a throwaway database) and
# DIP_DEMO_DSN (another: the demo needs one of its own). Both run in one process, so the
# cap holds for the two together, not for each.
set -uo pipefail

python - <<'EOF'
import os, sys
from dip.__main__ import main
from dip.agents import llm

rc = main(["incident", "eval", "--record"])
print(f"incident cases: exit {rc}, spent ${llm.SPENT['usd']:.4f}")
os.environ["DIP_EVAL_DSN"] = os.environ["DIP_DEMO_DSN"]
rc2 = main(["demo", "--record"])
print(f"demo: exit {rc2}, spent ${llm.SPENT['usd']:.4f} in all (cap ${os.environ['DIP_SPEND_CAP_USD']})")
sys.exit(1 if rc or rc2 else 0)
EOF
status=$?

# Scrub: the key, and any auth header, out of every recording (none should be there: a
# recording holds the model's turns and request hashes, never the request); then check.
dirs="agents/evals/incident/recordings agents/evals/demo/recordings"
python - $dirs <<'EOF'
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
for d in $dirs; do
  if grep -lF "$ANTHROPIC_API_KEY" "$d"/*.json; then
    echo "the key is still in a recording"; exit 1
  fi
done
ls -l $dirs
exit $status
