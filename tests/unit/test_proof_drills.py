"""Stage 11 (S11.10): scripts/proof_drills.py names its platform-check Jobs and chain pods
as Kubernetes requires (DNS-1123: lowercase letters, digits and '-'), for every drill."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("proof_drills", ROOT / "scripts" / "proof_drills.py")
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)
NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def test_every_job_and_pod_name_is_valid():
    for drill, *_ in proof.DRILLS:
        tag = drill.replace("_", "-")
        for name in (f"platform-check-e2e-{tag}-12345", f"platform-check-e2e-{tag}-after-12345",
                     f"chain-proof-{drill.replace('_', '-')}"):  # fmt: skip
            assert NAME.match(name) and len(name) <= 63, name


def test_the_four_drills_and_their_expected_causes():
    assert [(d[0], d[4]) for d in proof.DRILLS] == [
        ("scheduler_down", "platform_scheduler"), ("oom_task", "platform_resources"),
        ("bad_secret", "platform_credentials"), ("stuck_queued", "platform_capacity"),
    ]  # fmt: skip
