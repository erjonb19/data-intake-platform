#!/usr/bin/env bash
# Record the incident eval cases on Gemini (the free fallback), one case at a time, into
# agents/evals/incident/recordings/gemini/ (Stage 11, S11.7). Run by the proof workflow's
# gemini-recordings job, because the laptop lacks the memory. Needs GEMINI_API_KEY and
# DIP_EVAL_DSN (a throwaway database). A case that fails to finish (a rate limit, an
# overload) waits and is retried, up to 5 tries; the others go on.
set -uo pipefail

out="artifacts/gemini"
mkdir -p "$out" agents/evals/incident/recordings/gemini
rm -f agents/evals/incident/recordings/gemini/*.json
cases=$(python -c "from dip.agents.incident_evals import load_cases; print(' '.join(c['name'] for c in load_cases()))")
failed=()
for c in $cases; do
  done_=""
  for try in 1 2 3 4 5; do
    line=$(timeout 300 python -m dip incident eval --record --provider gemini --case "$c" \
             --out "$out/$c" 2>&1 | tee "$out/$c.log" | grep -E "^(PASS|FAIL) " | head -1)
    if [ -n "$line" ]; then
      echo "$line"; done_=1; break
    fi
    echo "$c: try $try did not finish ($(grep -oE 'Gemini [0-9]+' "$out/$c.log" | head -1)); waiting"
    sleep $((60 * try))
  done
  [ -n "$done_" ] || failed+=("$c")
done

# Scrub: the key, and any auth header, out of every recording (none should be there: a
# recording holds the model's turns and request hashes, never the request); then check.
python - <<'EOF'
import os, pathlib, re
key = os.environ["GEMINI_API_KEY"]
auth = re.compile(r'("?(x-goog-api-key|authorization)"?\s*[:=]\s*)"[^"]*"', re.I)
for p in pathlib.Path("agents/evals/incident/recordings/gemini").glob("*.json"):
    t = p.read_text("utf-8")
    s = auth.sub(r'\1"[redacted]"', t.replace(key, "[redacted]"))
    if s != t:
        p.write_text(s, "utf-8")
        print(f"scrubbed {p.name}")
EOF
if grep -rlF "$GEMINI_API_KEY" agents/evals/incident/recordings/gemini; then
  echo "the key is still in a recording"; exit 1
fi
ls agents/evals/incident/recordings/gemini
if [ ${#failed[@]} -gt 0 ]; then
  echo "not recorded after 5 tries: ${failed[*]}"; exit 1
fi
