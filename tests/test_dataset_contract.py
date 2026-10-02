"""Dataset contracts only: these tests do not call a model or measure its ability."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from opslens.benchmark import load_manifest
from opslens.evaluation import _validate_label, score_run
from opslens.investigation import CAUSES
from opslens.tools import IncidentTools, MAX_CONTENT_CHARS
from scripts.freeze_fixtures import build_manifest


ROOT = Path(__file__).resolve().parents[1]
DEV_MANIFEST = ROOT / "eval/fixtures.manifest.json"
CHALLENGE_MANIFEST = ROOT / "eval/challenge_v1.manifest.json"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def contract_state(case, label, evidence_ids):
    """Construct a scoring fixture, never a claimed model-generated result."""
    report = {
        "status": "insufficient_evidence" if label["cause_code"] == "insufficient_evidence" else "diagnosed",
        "cause_code": label["cause_code"], "root_service": label.get("root_service") or "",
        "root_cause": "Contract-test placeholder, not a model diagnosis.",
        "explanation": "Checks the annotation and scorer interface only.",
        "evidence_ids": sorted(set(evidence_ids)), "next_steps": ["Human review required."],
    }
    return {
        "case_id": case["case_id"], "status": "completed", "report": report,
        "observed": {record["id"]: record for record in case["records"]},
        "provider": "contract-test-fixture", "steps": 0, "tool_calls": 0,
        "model_seconds": 0, "usage": {}, "usage_complete": False,
    }


class DatasetContractTests(unittest.TestCase):
    def test_dev_labels_manifest_and_single_run_scoring_agree(self):
        labels = read_json(ROOT / "eval/labels.json")
        manifest = read_json(DEV_MANIFEST)
        self.assertEqual(len(labels), 6)
        self.assertEqual({entry["case_id"] for entry in manifest["cases"]}, set(labels))
        self.assertEqual(manifest, build_manifest(ROOT))
        for entry in manifest["cases"]:
            with self.subTest(case=entry["case_id"]):
                label = labels[entry["case_id"]]
                self.assertEqual(label["split"], "dev")
                self.assertEqual(entry["label"], label)
                case = read_json(DEV_MANIFEST.parent / entry["path"])
                state = contract_state(case, label, label["required_evidence"])
                self.assertEqual(score_run(state, label), score_run(state, entry["label"]))
                self.assertTrue(score_run(state, label)["task_success"])

    def test_dev_component_labels_follow_observed_failure_ownership(self):
        labels = read_json(ROOT / "eval/labels.json")
        expected = {"inc001": "checkout", "inc002": "inventory", "inc003": "checkout",
                    "inc004": None, "inc005": "checkout", "inc006": "inventory"}
        self.assertEqual({key: value["root_service"] for key, value in labels.items()}, expected)
        for case_id, label in labels.items():
            snapshot = read_json(ROOT / "data/cases" / (case_id + ".json"))
            if label["root_service"] is not None:
                self.assertIn(label["root_service"], snapshot["services"])
                self.assertTrue(any(record["service"] == label["root_service"]
                                    for record in snapshot["records"]))
            else:
                self.assertEqual(label["cause_code"], "insufficient_evidence")

    def test_freezer_preserves_nested_metadata_alternative_groups_and_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data/cases").mkdir(parents=True)
            (root / "eval").mkdir()
            source = ROOT / "data/cases/inc001.json"
            (root / "data/cases/inc001.json").write_bytes(source.read_bytes())
            label = deepcopy(read_json(ROOT / "eval/labels.json")["inc001"])
            label["split"] = "test"
            label["evidence_groups"] = [["inc001-m1", "inc001-l1"]]
            label["annotation"] = {"review_version": 7, "notes": ["preserve", {"nested": True}]}
            label_path = root / "eval/labels.json"
            label_path.write_text(json.dumps({"inc001": label}), encoding="utf-8")
            before = label_path.read_bytes()
            frozen = build_manifest(root)
            self.assertEqual(frozen["cases"][0]["label"], label)
            self.assertEqual(label_path.read_bytes(), before)
            self.assertEqual(frozen, build_manifest(root))

    def test_frozen_hashes_and_manifest_validation(self):
        for path in (DEV_MANIFEST, CHALLENGE_MANIFEST):
            with self.subTest(manifest=path.name):
                manifest = read_json(path)
                for entry in manifest["cases"]:
                    raw = (path.parent / entry["path"]).read_bytes()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
                    IncidentTools._validate_snapshot(json.loads(raw))
                self.assertEqual(len(load_manifest(path)["cases"]), len(manifest["cases"]))

    def test_challenge_boundaries_opacity_and_distinct_groups(self):
        manifest = read_json(CHALLENGE_MANIFEST)
        dev = read_json(DEV_MANIFEST)
        self.assertEqual(len(manifest["cases"]), 4)
        self.assertIn("generation_method", manifest)
        self.assertGreaterEqual(len(manifest["limitations"]), 4)
        groups = {entry["group_id"] for entry in manifest["cases"]}
        self.assertEqual(len(groups), 4)
        self.assertFalse(groups & {entry["group_id"] for entry in dev["cases"]})
        seen_records = set()
        categories = set()
        for entry in manifest["cases"]:
            label = entry["label"]
            self.assertEqual(label["split"], "challenge")
            self.assertEqual(entry["source_kind"], "synthetic")
            self.assertRegex(entry["case_id"], r"^sc_[0-9a-f]{12}$")
            self.assertRegex(entry["group_id"], r"^challenge-v1:[0-9a-f]{12}$")
            snapshot = read_json(CHALLENGE_MANIFEST.parent / entry["path"])
            self.assertIs(snapshot["synthetic"], True)
            self.assertGreaterEqual(len(snapshot["services"]), 3)
            self.assertFalse(set(snapshot) & {"label", "cause_code", "root_service", "evidence_groups"})
            categories.add(label["cause_code"])
            if label["cause_code"] in {"insufficient_evidence", "healthy"}:
                self.assertIsNone(label["root_service"])
            else:
                self.assertIn(label["root_service"], snapshot["services"])
            text = snapshot["alert"].lower()
            for record in snapshot["records"]:
                self.assertRegex(record["id"], r"^ev_[0-9a-f]{12}$")
                self.assertNotIn(record["id"], seen_records)
                seen_records.add(record["id"])
                self.assertLessEqual(len(record["content"]), MAX_CONTENT_CHARS)
                self.assertFalse(set(record) & {"label", "cause_code", "root_service", "evidence_groups"})
                text += " " + record["content"].lower()
            for category in CAUSES:
                self.assertNotIn(category, text, "Do not print category answers in observable text")
        self.assertTrue({"healthy", "insufficient_evidence"}.issubset(categories))
        self.assertGreaterEqual(len(categories - {"healthy", "insufficient_evidence"}), 2)

    def test_evidence_groups_reference_existing_records_in_legal_form(self):
        for entry in read_json(CHALLENGE_MANIFEST)["cases"]:
            label = entry["label"]
            snapshot = read_json(CHALLENGE_MANIFEST.parent / entry["path"])
            known = {record["id"] for record in snapshot["records"]}
            groups = label["evidence_groups"]
            self.assertIsInstance(groups, list)
            self.assertGreaterEqual(len(groups), 2)
            for group in groups:
                self.assertIsInstance(group, list)
                self.assertGreaterEqual(len(group), 2, "Keep equivalent observations as alternatives")
                self.assertEqual(len(group), len(set(group)))
                self.assertTrue(all(isinstance(item, str) and item.strip() for item in group))
                self.assertTrue(set(group).issubset(known))
            _validate_label(label)

    def test_alternative_group_members_satisfy_scoring_contract(self):
        for entry in read_json(CHALLENGE_MANIFEST)["cases"]:
            label = entry["label"]
            snapshot = read_json(CHALLENGE_MANIFEST.parent / entry["path"])
            for member in (0, -1):
                citations = [group[member] for group in label["evidence_groups"]]
                state = contract_state(snapshot, label, citations)
                self.assertTrue(score_run(state, label)["task_success"],
                                "Either approved observation must satisfy its evidence group")
            partial = [group[0] for group in label["evidence_groups"][:-1]]
            state = contract_state(snapshot, label, partial)
            self.assertFalse(score_run(state, label)["task_success"],
                             "A missing evidence group must not pass")

    def test_malformed_evidence_groups_are_rejected(self):
        baseline = read_json(CHALLENGE_MANIFEST)["cases"][0]["label"]
        known = baseline["evidence_groups"][0][0]
        malformed = (None, "not-a-list", [known], [[]], [[known, known]], [[1]], [[""]])
        for groups in malformed:
            with self.subTest(groups=groups):
                label = {**baseline, "evidence_groups": groups}
                with self.assertRaises(ValueError):
                    _validate_label(label)

    def test_manifest_rejects_evidence_group_references_absent_from_case(self):
        manifest = read_json(CHALLENGE_MANIFEST)
        manifest["cases"] = [deepcopy(manifest["cases"][0])]
        entry = manifest["cases"][0]
        entry["path"] = str((CHALLENGE_MANIFEST.parent / entry["path"]).resolve())
        entry["label"]["evidence_groups"] = [["ev_missing_observation"]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
