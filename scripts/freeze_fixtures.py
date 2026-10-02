"""Build the development manifest from checked-in teaching fixtures (no model calls)."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_manifest(root: Path = ROOT) -> dict:
    """Freeze bytes and copy the complete label contract without rewriting it.

    labels.json is the single source for single-run scoring and this manifest.
    Additional evaluator metadata, including alternative evidence groups, must
    survive regeneration unchanged. A label split is never silently rewritten.
    """
    root = Path(root)
    labels = json.loads((root / "eval/labels.json").read_text(encoding="utf-8"))
    entries = []
    seen = set()
    for path in sorted((root / "data/cases").glob("*.json")):
        raw = path.read_bytes()
        case_id = json.loads(raw)["case_id"]
        if case_id in seen:
            raise ValueError(f"duplicate fixture case_id: {case_id}")
        if case_id not in labels:
            raise ValueError(f"fixture has no scoring label: {case_id}")
        seen.add(case_id)
        label = deepcopy(labels[case_id])
        entries.append({"case_id": case_id, "path": "../data/cases/" + path.name,
                        "sha256": hashlib.sha256(raw).hexdigest(), "label": label,
                        "group_id": "handwritten-v1:" + label["cause_code"], "source_kind": "synthetic"})
    if seen != set(labels):
        raise ValueError("labels contain cases absent from data/cases")
    if not entries:
        raise ValueError("no teaching fixtures found")
    return {"schema_version": 1, "dataset_version": "teaching-fixtures-v2",
                "notes": "Current checked-in fixtures, including attack/decoy variants, are dev. Same-family variants are not held-out cases. Labels are copied verbatim from eval/labels.json; changing split is an explicit annotation change, never performed by this script.",
                "observation_budget": {"max_chars": 24000, "max_records": 100}, "cases": entries}


def main():
    manifest = build_manifest()
    destination = ROOT / "eval/fixtures.manifest.json"
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(destination)
    print(destination)


if __name__ == "__main__":
    main()
