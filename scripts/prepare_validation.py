"""Prepare the preregistered eight-case local experiment without model calls.

Run from the repository root: python -m scripts.prepare_validation --output runs/validation-v03-data
This collects actual loopback HTTP observations. Evaluator labels never enter tools.
"""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path

from opslens.benchmark import load_manifest, validate_manifest
from opslens.lab import collect_suite


ROOT = Path(__file__).resolve().parents[1]


def write_manifest(destination, entries, version):
    result = {"schema_version": 1, "dataset_version": version,
              "observation_budget": {"max_chars": 24000, "max_records": 100}, "cases": []}
    for entry in entries:
        item = {key: deepcopy(value) for key, value in entry.items()
                if key not in ("path", "raw_snapshot", "raw_provenance", "provenance_sha256")}
        item["path"] = Path(os.path.relpath(entry["path"], destination.parent)).as_posix()
        provenance = entry["path"].parent / "provenance.json"
        if "raw_provenance" in entry:
            item["provenance_path"] = Path(os.path.relpath(provenance, destination.parent)).as_posix()
        result["cases"].append(item)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return validate_manifest(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("output must be empty; do not replace previous captures")
    output.mkdir(parents=True, exist_ok=True)
    # Validate frozen fixtures before starting the local workload.
    dev = load_manifest(ROOT / "eval/fixtures.manifest.json")
    challenge = load_manifest(ROOT / "eval/challenge_v1.manifest.json")
    if len(challenge["cases"]) != 4:
        raise ValueError("protocol requires exactly four challenge cases")
    selected = [row for row in dev["cases"] if row["case_id"] in ("inc001", "inc006")]
    if len(selected) != 2:
        raise ValueError("protocol requires inc001 and inc006")
    captured = collect_suite(output / "lab", seeds=[41], scenarios=["pool_exhaustion", "dependency_timeout"])
    lab = load_manifest(captured["manifest_path"])
    smoke_path, paired_path = output / "smoke.manifest.json", output / "paired.manifest.json"
    smoke = write_manifest(smoke_path, [row for row in selected if row["case_id"] == "inc001"], "v03-smoke")
    paired = write_manifest(paired_path, selected + lab["cases"] + challenge["cases"], "v03-preregistered-paired")
    print(json.dumps({"smoke": {"path": str(smoke_path), **smoke},
                      "paired": {"path": str(paired_path), **paired}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
