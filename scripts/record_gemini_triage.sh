#!/usr/bin/env bash
# Record the triage eval cases (cases.yaml) on Gemini (the free fallback), one case at a
# time, into agents/evals/triage/recordings/gemini/ (24.17: what triage sees changed). Run
# by the proof workflow's gemini-recordings job after the incident cases. Needs
# GEMINI_API_KEY and DIP_EVAL_DSN (a throwaway database). A case that fails to finish (a
# rate limit, an overload) waits and is retried, up to 5 tries; the others go on.
# Gemini is the weaker triager (docs/agents/EVALS.md): a case that ends with a refused call
# keeps its earlier recording (restored from git, and listed as kept), so one bad Gemini
# answer does not throw away the rest. The cases recorded now are listed in
# $out/recorded.txt, which the job's replay check replays.
set -uo pipefail

out="artifacts/gemini-triage"
mkdir -p "$out" agents/evals/triage/recordings/gemini
# The cases Gemini has recordings for already (the fallback's comparison set), again.
cases=""
for f in agents/evals/triage/recordings/gemini/*.json; do
  [ -e "$f" ] && cases="$cases $(basename "$f" .json)"
done
test -n "$cases" || { echo "no Gemini triage recordings to redo"; exit 1; }
failed=()
recorded=()
for c in $cases; do
  done_=""
  for try in 1 2 3 4 5; do
    line=$(timeout 300 python -m dip triage eval --record --provider gemini --case "$c" \
             --out "$out/$c" 2>&1 | tee "$out/$c.log" | grep -E "^(PASS|FAIL) " | head -1)
    if [ -n "$line" ]; then
      echo "$line"; done_=1; break
    fi
    echo "$c: try $try did not finish ($(grep -oE 'Gemini [0-9]+' "$out/$c.log" | head -1)); waiting"
    sleep $((60 * try))
  done
  # Gemini's scores are its own (a wrong cause is a known limitation, docs/agents/EVALS.md),
  # but a refused tool call fails the replay in CI: such a recording is never uploaded.
  if [ -z "$done_" ] || [[ "$line" != *"violations none" ]]; then
    failed+=("$c")
    git checkout -q -- "agents/evals/triage/recordings/gemini/$c.json"
  else
    recorded+=("$c")
  fi
done
printf '%s\n' "${recorded[@]}" > "$out/recorded.txt"

# Scrub: the key, and any auth header, out of every recording (none should be there: a
# recording holds the model's turns and request hashes, never the request); then check.
if ! python - <<'EOF'
import os, pathlib, re
key = os.environ["GEMINI_API_KEY"]
auth = re.compile(r'("?(x-goog-api-key|authorization)"?\s*[:=]\s*)"[^"]*"', re.I)
for p in pathlib.Path("agents/evals/triage/recordings/gemini").glob("*.json"):
    t = p.read_text("utf-8")
    s = auth.sub(r'\1"[redacted]"', t.replace(key, "[redacted]"))
    if s != t:
        p.write_text(s, "utf-8")
        print(f"scrubbed {p.name}")
EOF
then
  echo "scrub: FAILED"; exit 1
fi
echo "scrub: ok"
if grep -rlF "$GEMINI_API_KEY" agents/evals/triage/recordings/gemini; then
  echo "the key is still in a recording"; exit 1
fi
ls agents/evals/triage/recordings/gemini
if [ ${#failed[@]} -gt 0 ]; then
  echo "kept the earlier recording (not recorded, or with a policy violation): ${failed[*]}"
fi
if [ ${#recorded[@]} -eq 0 ]; then
  echo "no triage case recorded on Gemini"; exit 1
fi
