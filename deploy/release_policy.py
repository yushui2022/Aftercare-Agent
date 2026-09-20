"""Evaluate whether a release evidence manifest may be promoted."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

KNOWN_GATES = frozenset(
    {
        "image_signature",
        "target_registry_rescan",
        "target_environment_deployment",
    }
)


def evaluate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a promotion decision; reject malformed or unknown evidence gates."""
    if manifest.get("schema_version") != 1 or manifest.get("status") != "pass":
        raise ValueError("release evidence is not a passing schema v1 manifest")
    unresolved = manifest.get("unresolved_gates")
    if not isinstance(unresolved, list) or not all(isinstance(item, str) for item in unresolved):
        raise ValueError("release evidence has no valid unresolved_gates list")
    if len(set(unresolved)) != len(unresolved):
        raise ValueError("release evidence contains duplicate unresolved gates")
    unknown = set(unresolved) - KNOWN_GATES
    if unknown:
        raise ValueError(f"release evidence contains unknown gates: {sorted(unknown)}")
    gates = manifest.get("promotion_gates")
    if not isinstance(gates, dict) or set(gates) != KNOWN_GATES:
        raise ValueError("release evidence does not contain the fixed promotion gate set")
    derived_unresolved: list[str] = []
    for name in sorted(KNOWN_GATES):
        gate = gates[name]
        if not isinstance(gate, dict) or gate.get("status") not in {"open", "passed"}:
            raise ValueError(f"release evidence has an invalid status for gate: {name}")
        if gate["status"] == "open":
            derived_unresolved.append(name)
    if sorted(unresolved) != derived_unresolved:
        raise ValueError("release evidence gate statuses and unresolved_gates disagree")
    build = manifest.get("build")
    if not isinstance(build, dict):
        raise ValueError("release evidence has no build identity")
    source_revision = build.get("source_revision")
    if not isinstance(source_revision, str) or not source_revision:
        raise ValueError("release evidence has no source revision")
    return {
        "schema_version": 1,
        "decision": "hold" if derived_unresolved else "promote",
        "source_revision": source_revision,
        "unresolved_gates": derived_unresolved,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-hold", action="store_true")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read release evidence: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SystemExit("release evidence must be a JSON object")
    try:
        decision = evaluate_manifest(manifest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    serialized = json.dumps(decision, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    if args.fail_on_hold and decision["decision"] == "hold":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
