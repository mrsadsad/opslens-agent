from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from opslens.tools import IncidentTools, MAX_CONTENT_CHARS


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "data" / "cases"


class IncidentToolsTests(unittest.TestCase):
    def setUp(self):
        self.tools = IncidentTools(CASES / "inc001.json")

    def test_identity_fingerprint_and_available_services(self):
        self.assertEqual(self.tools.case_id, "inc001")
        self.assertIn("[SYNTHETIC]", self.tools.alert)
        self.assertEqual(self.tools.fingerprint, hashlib.sha256((CASES / "inc001.json").read_bytes()).hexdigest())
        result = self.tools.execute("list_services", {})
        self.assertTrue(result["ok"])
        self.assertEqual([row["service"] for row in result["records"]], ["checkout", "orders-db"])

    def test_separate_evidence_sources_preserve_original_ids(self):
        for name, kind, expected_id in [
            ("query_metrics", "metric", "inc001-m1"),
            ("search_logs", "log", "inc001-l1"),
            ("get_changes", "change", "inc001-c1"),
        ]:
            with self.subTest(tool=name):
                result = self.tools.execute(name, {"service": "checkout"})
                self.assertTrue(result["ok"])
                self.assertIn(expected_id, {row["id"] for row in result["records"]})
                self.assertTrue(all(row["kind"] == kind and row["service"] == "checkout" for row in result["records"]))
        database = self.tools.execute("query_metrics", {"service": "orders-db"})
        self.assertIn("CPU=23%", database["records"][0]["content"])

    def test_search_is_literal_case_insensitive_and_no_match_is_success(self):
        result = self.tools.execute("search_logs", {"service": "checkout", "query": "POOLACQUIRETIMEOUT"})
        self.assertEqual([row["id"] for row in result["records"]], ["inc001-l1"])
        result = self.tools.execute("search_logs", {"service": "checkout", "query": ".*"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["records"], [])
        self.assertEqual(result["page"], {"offset": 0, "total_matches": 0, "returned_count": 0, "next_offset": None})
        result = self.tools.execute("search_runbooks", {"query": "CONNECTION POOL"})
        self.assertEqual([row["id"] for row in result["records"]], ["inc001-r1"])

    def test_unphased_fixture_rejects_phase_and_explains_explicit_recovery(self):
        # Regression: the first real run repeatedly queried nonexistent phases.
        services = self.tools.execute("list_services", {})["records"]
        checkout = next(row for row in services if row["service"] == "checkout")
        self.assertEqual(checkout["source"], "synthetic")
        self.assertEqual(checkout["capabilities"]["available_kinds"], ["change", "log", "metric", "runbook"])
        self.assertEqual(checkout["capabilities"]["phases_by_kind"]["metric"], [])
        self.assertEqual(checkout["capabilities"]["phases_by_kind"]["log"], [])
        self.assertEqual(self.tools.capabilities["available_phases"], [])
        self.assertFalse(self.tools.capabilities["trace_available"])
        for name, kind, evidence_id in (("query_metrics", "metric", "inc001-m1"),
                                        ("search_logs", "log", "inc001-l1")):
            with self.subTest(tool=name):
                filtered = self.tools.execute(name, {"service": "checkout", "phase": "fault"})
                self.assert_error(filtered, "unsupported_phase")
                self.assertEqual(filtered["error"]["kind"], kind)
                self.assertEqual(filtered["error"]["available_phases"], [])
                self.assertTrue(filtered["error"]["has_unphased_records"])
                self.assertIn("omit phase", filtered["error"]["message"])
                # Recovery must be a new explicit caller choice; no silent broadening.
                unfiltered = self.tools.execute(name, {"service": "checkout"})
                self.assertTrue(unfiltered["ok"])
                self.assertIn(evidence_id, {row["id"] for row in unfiltered["records"]})

    def test_phase_support_is_scoped_to_service_and_record_kind(self):
        snapshot = {"synthetic": False, "source": "local_fault_lab", "case_id": "observed-phases",
                    "alert": "Measured local observation", "services": ["front", "dependency"], "records": [
                        {"id": "f-m", "kind": "metric", "service": "front", "phase": "fault", "content": "measured fault metric"},
                        {"id": "f-old", "kind": "metric", "service": "front", "content": "unphased metric"},
                        {"id": "f-l", "kind": "log", "service": "front", "phase": "baseline", "trace_id": "trace-1", "content": "request completed"},
                        {"id": "d-m", "kind": "metric", "service": "dependency", "phase": "recovery", "content": "measured recovery metric"},
                    ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            tools = IncidentTools(path)
        capabilities = tools.capabilities
        self.assertEqual(capabilities["available_phases"], ["baseline", "fault", "recovery"])
        self.assertTrue(capabilities["trace_available"])
        front = capabilities["services"]["front"]
        self.assertEqual(front["phases_by_kind"], {"log": ["baseline"], "metric": ["fault"]})
        self.assertEqual(front["unphased_kinds"], ["metric"])
        self.assertEqual(front["trace_kinds"], ["log"])
        services = tools.execute("list_services", {})["records"]
        self.assertTrue(all(row["source"] == "local_fault_lab" for row in services))
        self.assertTrue(all("Measured local HTTP" in row["content"] and "Synthetic" not in row["content"] for row in services))
        for name, phase, expected in (("query_metrics", "baseline", ["fault"]),
                                       ("query_metrics", "recovery", ["fault"]),
                                       ("search_logs", "fault", ["baseline"])):
            result = tools.execute(name, {"service": "front", "phase": phase})
            self.assert_error(result, "unsupported_phase")
            self.assertEqual(result["error"]["available_phases"], expected)
        missing_kind = tools.execute("search_logs", {"service": "dependency", "phase": "recovery"})
        self.assert_error(missing_kind, "unsupported_phase")
        self.assertFalse(missing_kind["error"]["has_unphased_records"])
        fault = tools.execute("query_metrics", {"service": "front", "phase": "fault"})
        self.assertEqual([record["id"] for record in fault["records"]], ["f-m"])
        empty_search = tools.execute("search_logs", {"service": "front", "phase": "baseline", "query": "unmatched"})
        self.assertTrue(empty_search["ok"])
        self.assertEqual(empty_search["records"], [])
        self.assertEqual(empty_search["page"]["total_matches"], 0)
        # Exposing capability copies must not let callers change subsequent validation.
        capabilities["services"]["front"]["phases_by_kind"]["metric"].append("recovery")
        services[0]["capabilities"]["phases_by_kind"]["metric"].append("recovery")
        self.assert_error(tools.execute("query_metrics", {"service": "front", "phase": "recovery"}), "unsupported_phase")

    def test_unknown_tools_and_services_fail_closed(self):
        for name in ("shell", "read_file", "../eval/labels.json", "", None, ["search_logs"]):
            with self.subTest(tool=name):
                self.assert_error(self.tools.execute(name, {}), "unknown_tool")
        self.assert_error(self.tools.execute("query_metrics", {"service": "not-in-snapshot"}), "unknown_service")

    def assert_error(self, result, code):
        self.assertFalse(result["ok"])
        self.assertEqual(result["records"], [])
        self.assertEqual(result["error"]["code"], code)
        self.assertIsInstance(result["error"]["message"], str)

    def test_argument_boundaries_return_structured_errors(self):
        invalid_calls = [
            ("list_services", None), ("list_services", []),
            ("list_services", {1: "value"}), ("list_services", {"limit": 1}),
            ("query_metrics", {}), ("query_metrics", {"service": None}),
            ("query_metrics", {"service": True}), ("query_metrics", {"service": []}),
            ("query_metrics", {"service": " "}), ("query_metrics", {"service": "x" * 81}),
            ("query_metrics", {"service": "checkout", "query": "x"}),
            ("query_metrics", {"service": "checkout", "phase": None}),
            ("query_metrics", {"service": "checkout", "phase": "not-a-phase"}),
            ("get_changes", {"service": "checkout", "execute": True}),
            ("search_logs", {"service": "checkout", "query": None}),
            ("search_logs", {"service": "checkout", "query": "x" * 201}),
            ("search_runbooks", {}), ("search_runbooks", {"query": ""}),
            ("search_runbooks", {"query": "   "}), ("search_runbooks", {"query": 5}),
        ]
        invalid_calls += [("search_logs", {"service": "checkout", "limit": value})
                          for value in (0, -1, 21, True, False, 1.0, "10", None, [])]
        invalid_calls += [("search_logs", {"service": "checkout", "offset": value})
                          for value in (-1, 1001, True, False, 1.0, "1", None, [])]
        invalid_calls += [("list_services", {"offset": 1}),
                          ("get_evidence", {"ids": ["inc001-m1"], "offset": 1})]
        for name, arguments in invalid_calls:
            with self.subTest(tool=name, arguments=arguments):
                self.assert_error(self.tools.execute(name, arguments), "invalid_arguments")

    def test_schemas_are_function_tools_and_disallow_extra_arguments(self):
        self.assertEqual({schema["function"]["name"] for schema in self.tools.schemas},
                         {"list_services", "query_metrics", "search_logs", "get_changes", "search_runbooks", "get_evidence", "get_trace"})
        for schema in self.tools.schemas:
            self.assertEqual(schema["type"], "function")
            self.assertFalse(schema["function"]["parameters"]["additionalProperties"])

    def test_outputs_have_result_and_content_limits(self):
        snapshot = {"synthetic": True, "case_id": "bounded", "alert": "Synthetic test",
                    "services": ["checkout"], "records": [
                        {"id": f"{kind}-{index}", "kind": kind, "service": "checkout", "content": "x" * 5000}
                        for kind in ("metric", "log", "change", "runbook") for index in range(30)
                    ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            tools = IncidentTools(path)
        for name in ("query_metrics", "get_changes"):
            result = tools.execute(name, {"service": "checkout"})
            self.assertEqual(len(result["records"]), 20)
        self.assertEqual(len(tools.execute("search_logs", {"service": "checkout"})["records"]), 10)
        self.assertEqual(len(tools.execute("search_logs", {"service": "checkout", "limit": 1})["records"]), 1)
        for name, arguments in [("search_logs", {"service": "checkout", "limit": 20}),
                                ("search_runbooks", {"query": "x", "limit": 20})]:
            result = tools.execute(name, arguments)
            self.assertEqual(len(result["records"]), 20)
            self.assertTrue(all(len(record["content"]) <= MAX_CONTENT_CHARS for record in result["records"]))
            self.assertTrue(all(record["content"].endswith("[truncated]") for record in result["records"]))
            self.assertTrue(all(record["content_truncated"] for record in result["records"]))
            self.assertTrue(all(record["content_original_chars"] == 5000 for record in result["records"]))
            self.assertEqual(result["page"]["next_offset"], 20)

    def snapshot_tools(self, records):
        snapshot = {"synthetic": True, "case_id": "paged", "alert": "Offline pagination fixture",
                    "services": ["checkout"], "records": records}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            return IncidentTools(path)

    def test_all_paginated_queries_reach_every_matching_record_once(self):
        records = [{"id": f"{kind}-{index:02d}", "kind": kind, "service": "checkout",
                    "content": "pool observation", "trace_id": "trace-paged"}
                   for kind in ("metric", "log", "change", "runbook") for index in range(45)]
        tools = self.snapshot_tools(records)
        queries = [("query_metrics", {"service": "checkout"}, "metric"),
                   ("search_logs", {"service": "checkout", "limit": 20}, "log"),
                   ("get_changes", {"service": "checkout"}, "change"),
                   ("search_runbooks", {"query": "pool", "limit": 20}, "runbook"),
                   ("search_runbooks", {"query": "pool", "limit": 20, "strategy": "bm25"}, "runbook"),
                   ("get_trace", {"trace_id": "trace-paged"}, None)]
        for name, arguments, kind in queries:
            with self.subTest(tool=name):
                expected = [record["id"] for record in records if kind is None or record["kind"] == kind]
                actual, offset = [], 0
                while True:
                    result = tools.execute(name, {**arguments, "offset": offset})
                    self.assertTrue(result["ok"])
                    page = result["page"]
                    self.assertEqual(page["total_matches"], len(expected))
                    self.assertEqual(page["offset"], offset)
                    self.assertEqual(page["returned_count"], len(result["records"]))
                    actual.extend(record["id"] for record in result["records"])
                    if page["next_offset"] is None:
                        self.assertFalse(result["truncated"])
                        break
                    self.assertGreater(page["next_offset"], offset)
                    offset = page["next_offset"]
                self.assertEqual(actual, expected)

    def test_pages_count_only_filtered_matches_and_accept_terminal_offsets(self):
        records = [{"id": f"log-{index:02d}", "kind": "log", "service": "checkout",
                    "phase": "fault" if index % 2 else "baseline",
                    "content": "WARN deadline" if index % 3 else "INFO completed"}
                   for index in range(45)]
        tools = self.snapshot_tools(records)
        arguments = {"service": "checkout", "phase": "fault", "query": "deadline", "limit": 3}
        expected = [record["id"] for record in records
                    if record["phase"] == "fault" and "deadline" in record["content"]]
        result = tools.execute("search_logs", {**arguments, "offset": 3})
        self.assertEqual([record["id"] for record in result["records"]], expected[3:6])
        self.assertEqual(result["page"]["total_matches"], len(expected))
        for offset in (len(expected), 1000):
            result = tools.execute("search_logs", {**arguments, "offset": offset})
            self.assertTrue(result["ok"])
            self.assertEqual(result["records"], [])
            self.assertIsNone(result["page"]["next_offset"])
            self.assertEqual(result["page"]["total_matches"], len(expected))

    def test_long_content_boundary_is_explicit_and_search_does_not_claim_tail_coverage(self):
        records = [{"id": "exact", "kind": "log", "service": "checkout", "content": "x" * MAX_CONTENT_CHARS},
                   {"id": "long", "kind": "log", "service": "checkout",
                    "content": "x" * 2000 + "tail_failure_marker"}]
        tools = self.snapshot_tools(records)
        exact, long = tools.execute("get_evidence", {"ids": ["exact", "long"]})["records"]
        self.assertFalse(exact["content_truncated"])
        self.assertEqual(exact["content"], records[0]["content"])
        self.assertTrue(long["content_truncated"])
        self.assertEqual(long["content_original_chars"], len(records[1]["content"]))
        self.assertLessEqual(len(long["content"]), MAX_CONTENT_CHARS)
        self.assertNotIn("tail_failure_marker", long["content"])
        search = tools.execute("search_logs", {"service": "checkout", "query": "tail_failure_marker"})
        self.assertEqual(search["records"], [])
        self.assertEqual(search["search_scope"], "visible_content_prefix")
        self.assertEqual(search["content_limit_chars"], MAX_CONTENT_CHARS)

    def test_service_evidence_round_trips_without_losing_capabilities_or_aliasing(self):
        service = self.tools.execute("list_services", {})["records"][0]
        returned = self.tools.execute("get_evidence", {"ids": [service["id"], "inc001-m1"]})
        self.assertTrue(returned["ok"])
        self.assertEqual(returned["records"][0], service)
        returned["records"][0]["capabilities"]["available_kinds"].clear()
        again = self.tools.execute("get_evidence", {"ids": [service["id"]]})
        self.assertEqual(again["records"][0], service)
        self.assert_error(self.tools.execute("get_evidence", {"ids": ["inc001-service-999"]}), "unknown_evidence")

    def test_snapshot_cannot_impersonate_generated_service_evidence(self):
        with self.assertRaisesRegex(ValueError, "collide"):
            self.snapshot_tools([{"id": "paged-service-1", "kind": "log", "service": "checkout",
                                  "content": "Untrusted text cannot replace service capabilities"}])

    def test_mutating_output_does_not_mutate_the_snapshot(self):
        first = self.tools.execute("query_metrics", {"service": "checkout"})
        original = first["records"][0]["content"]
        first["records"][0]["content"] = "tampered"
        second = self.tools.execute("query_metrics", {"service": "checkout"})
        self.assertEqual(second["records"][0]["content"], original)

    def test_runtime_loads_only_requested_snapshot_and_does_not_read_labels(self):
        case_path = CASES / "inc001.json"
        original_reader = Path.read_bytes
        opened = []

        def recording_read(path):
            opened.append(path.resolve())
            self.assertEqual(path.resolve(), case_path.resolve())
            return original_reader(path)

        with patch.object(Path, "read_bytes", recording_read):
            tools = IncidentTools(case_path)
        self.assertEqual(opened, [case_path.resolve()])
        # After construction every tool works without further filesystem reads.
        with patch.object(Path, "read_bytes", side_effect=AssertionError("Unexpected file read")), \
                patch.object(Path, "read_text", side_effect=AssertionError("Unexpected file read")), \
                patch("builtins.open", side_effect=AssertionError("Unexpected file open")):
            for name, arguments in [
                ("list_services", {}), ("query_metrics", {"service": "checkout"}),
                ("search_logs", {"service": "checkout"}), ("get_changes", {"service": "checkout"}),
                ("search_runbooks", {"query": "triage"}),
            ]:
                self.assertTrue(tools.execute(name, arguments)["ok"])
            self.assertEqual(tools.capabilities["available_phases"], [])
            self.assert_error(tools.execute("query_metrics", {"service": "checkout", "phase": "fault"}), "unsupported_phase")

    def test_prompt_injection_remains_inert_log_data(self):
        tools = IncidentTools(CASES / "inc005.json")
        fingerprint = tools.fingerprint
        with patch("os.system", side_effect=AssertionError("Command execution forbidden")), \
                patch("subprocess.run", side_effect=AssertionError("Command execution forbidden")):
            logs = tools.execute("search_logs", {"service": "checkout"})["records"]
            injection = next(record for record in logs if record["id"] == "inc005-l3")
            self.assertIn("Ignore all previous instructions", injection["content"])
            self.assertIn("eval/labels.json", injection["content"])
            self.assertNotIn("inc005-fake-proof", {record["id"] for record in logs})
            self.assertEqual(tools.fingerprint, fingerprint)
            self.assert_error(tools.execute("shell", {"command": "anything"}), "unknown_tool")
            metrics = tools.execute("query_metrics", {"service": "checkout"})["records"]
            self.assertIn("db_pool_max=20", metrics[0]["content"])

    def test_private_metadata_cannot_escape_record_projection(self):
        snapshot = json.loads((CASES / "inc001.json").read_text(encoding="utf-8"))
        snapshot["records"][0]["private_metadata"] = {"answer": "do not expose"}
        snapshot["capabilities"] = {"answer": "do not expose", "available_phases": ["fault"]}
        snapshot["records"][0]["capabilities"] = {"answer": "do not expose", "phase": "fault"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            tools = IncidentTools(path)
        result = tools.execute("query_metrics", {"service": "checkout"})
        self.assertNotIn("private_metadata", result["records"][0])
        self.assertNotIn("do not expose", json.dumps(result))
        self.assertNotIn("do not expose", json.dumps(tools.capabilities))
        self.assertNotIn("do not expose", json.dumps(tools.execute("list_services", {})))
        self.assertEqual(tools.capabilities["available_phases"], [])

    def test_invalid_snapshots_are_rejected(self):
        baseline = json.loads((CASES / "inc001.json").read_text(encoding="utf-8"))
        mutations = [
            lambda data: data.update(synthetic=False),
            lambda data: data["services"].append("checkout"),
            lambda data: data["records"].append(data["records"][0].copy()),
            lambda data: data["records"][0].update(service="unknown"),
            lambda data: data["records"][0].update(kind="answer"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            for mutate in mutations:
                data = json.loads(json.dumps(baseline))
                mutate(data)
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    IncidentTools(path)


class SnapshotIntegrityTests(unittest.TestCase):
    def test_frozen_synthetic_cases_have_no_answer_fields(self):
        paths = sorted(CASES.glob("*.json"))
        self.assertEqual([path.stem for path in paths], [f"inc{index:03d}" for index in range(1, 7)])
        forbidden = {"answer", "answers", "label", "labels", "cause_code", "required_evidence",
                     "expected_diagnosis", "ground_truth", "split"}
        all_ids = set()

        def check_keys(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value))
                for child in value.values():
                    check_keys(child)
            elif isinstance(value, list):
                for child in value:
                    check_keys(child)

        for path in paths:
            with self.subTest(case=path.stem):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertIs(data["synthetic"], True)
                self.assertEqual(data["case_id"], path.stem)
                check_keys(data)
                IncidentTools(path)
                for record in data["records"]:
                    self.assertNotIn(record["id"], all_ids)
                    all_ids.add(record["id"])

    def test_evaluation_labels_are_separate_and_cite_real_evidence(self):
        labels = json.loads((ROOT / "eval" / "labels.json").read_text(encoding="utf-8"))
        self.assertEqual(set(labels), {path.stem for path in CASES.glob("*.json")})
        for case_id, label in labels.items():
            data = json.loads((CASES / f"{case_id}.json").read_text(encoding="utf-8"))
            ids = {record["id"] for record in data["records"]}
            self.assertTrue(label["required_evidence"])
            self.assertTrue(set(label["required_evidence"]).issubset(ids))
            self.assertIn(label["cause_code"], {"connection_pool_exhaustion", "dependency_timeout", "bad_configuration", "insufficient_evidence"})
            self.assertEqual(label["split"], "dev")


if __name__ == "__main__":
    unittest.main()
