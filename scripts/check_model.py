"""Confirm the configured primary model exists and is active, via the Models API.

Reads config/llm.toml (DIP_LLM_MODEL overrides the model) and the API key from the
environment or .env. Never prints the key. Exit 0 when the model is active.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main() -> int:
    load_dotenv(ROOT / ".env")
    config = tomllib.loads((ROOT / "config" / "llm.toml").read_text(encoding="utf-8"))
    primary = config["primary"]
    model = os.environ.get("DIP_LLM_MODEL") or primary["model"]
    key = os.environ.get(primary["api_key_env"], "")
    if not key:
        print(f"FAIL: {primary['api_key_env']} is not set (add it to .env)", file=sys.stderr)
        return 1

    req = urllib.request.Request(
        f"https://api.anthropic.com/v1/models/{model}",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            info = json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"FAIL: Models API returned HTTP {exc.code} for {model}", file=sys.stderr)
        return 1

    lifecycle = info.get("lifecycle", "unknown")
    print(
        f"{info['id']} ({info.get('display_name')}): lifecycle={lifecycle}, "
        f"max_input_tokens={info.get('max_input_tokens')}, max_tokens={info.get('max_tokens')}"
    )
    if lifecycle != "active":
        print(f"FAIL: {model} is not active", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
