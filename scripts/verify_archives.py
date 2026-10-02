"""Check published datasets and frozen experiment source without model requests."""

import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from opslens.benchmark import validate_manifest


def main():
    report = {"passed": False, "external_model_requests": 0, "manifests": [],
              "frozen_source_files_checked": 0}
    try:
        manifests = [ROOT / "eval/fixtures.manifest.json", ROOT / "eval/challenge_v1.manifest.json",
                     *sorted((ROOT / "eval/results").glob("*/manifest.json"))]
        for manifest in manifests:
            plan = validate_manifest(manifest)
            report["manifests"].append({"path": manifest.relative_to(ROOT).as_posix(),
                                        "case_references": plan["case_count"],
                                        "sha256": plan["manifest_sha256"]})
        for summary in sorted((ROOT / "eval/results").glob("*/summary.json")):
            source = summary.parent / "source/opslens"
            hashes = json.loads(summary.read_text(encoding="utf-8"))["source_hashes"]
            if not hashes:
                raise ValueError(f"Empty source manifest: {summary.relative_to(ROOT)}")
            for name, expected in hashes.items():
                if Path(name).name != name:
                    raise ValueError("Frozen source hash entries must be filenames")
                raw = (source / name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError(f"Frozen source changed: {(source / name).relative_to(ROOT)}")
                report["frozen_source_files_checked"] += 1
        report["passed"] = True
    except (KeyError, ValueError, OSError) as error:
        report["error"] = f"{type(error).__name__}: {error}"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
