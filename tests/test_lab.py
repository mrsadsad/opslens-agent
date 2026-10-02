"""Integration tests use actual loopback TCP requests, without mocking HTTP."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from opslens.lab import (CAUSES, CONCURRENCY, LabValidationError, PHASES,
                         REQUESTS_PER_PHASE, SCENARIOS, collect_case, collect_suite)
from opslens.tools import IncidentTools


class LocalFaultLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.output = Path(cls.temporary.name)
        cls.suite = collect_suite(cls.output, seeds=[17])
        cls.cases = {result["provenance"]["scenario"]: result for result in cls.suite["cases"]}

    def _events(self, scenario):
        path = Path(self.cases[scenario]["case_path"]).parent / "requests.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_all_real_interventions_recover(self):
        self.assertEqual(set(self.cases), set(SCENARIOS))
        for scenario, result in self.cases.items():
            with self.subTest(scenario=scenario):
                provenance = result["provenance"]
                self.assertTrue(provenance["validation"]["passed"])
                expected_root = (None if scenario == "healthy" else provenance["services"][
                    "dependency" if scenario == "dependency_timeout" else "checkout"])
                self.assertEqual(result["label"]["root_service"], expected_root)
                self.assertEqual(provenance["workload"]["concurrency"], CONCURRENCY)
                measured = provenance["measurements"]
                self.assertLessEqual(measured["baseline"]["load_client"]["error_rate"], 0.1)
                self.assertLessEqual(measured["recovery"]["load_client"]["error_rate"], 0.1)
                if scenario != "healthy":
                    self.assertGreaterEqual(measured["fault"]["load_client"]["error_rate"], 0.25)
                    self.assertGreater(measured["fault"]["load_client"]["error_rate"],
                                       measured["recovery"]["load_client"]["error_rate"] + 0.2)
                else:
                    self.assertLessEqual(measured["fault"]["load_client"]["error_rate"], 0.1)

    def test_pool_fault_is_real_acquisition_contention(self):
        measured = self.cases["pool_exhaustion"]["provenance"]["measurements"]
        fault = measured["fault"]["checkout"]
        self.assertEqual(fault["pool_max"], 1)
        self.assertEqual(fault["pool_peak_in_use"], 1)
        self.assertGreaterEqual(fault["pool_peak_waiters"], 2)
        self.assertGreater(fault["error_counts"].get("pool_acquire_timeout", 0), 0)
        self.assertEqual(measured["recovery"]["checkout"]["pool_max"], CONCURRENCY)
        self.assertEqual(measured["fault"]["dependency"]["error_rate"], 0)
        self.assertIn(503, {row["status"] for row in self._events("pool_exhaustion")
                            if row["phase"] == "fault" and row["component"] == "load_client"})

    def test_dependency_timeout_crosses_real_http_deadline(self):
        measured = self.cases["dependency_timeout"]["provenance"]["measurements"]
        self.assertGreater(measured["fault"]["dependency"]["latency_p95_ms"],
                           measured["baseline"]["dependency"]["latency_p95_ms"] + 150)
        self.assertGreater(measured["fault"]["checkout"]["error_counts"].get("dependency_deadline_exceeded", 0), 0)
        rows = self._events("dependency_timeout")
        failed = next(row for row in rows if row["phase"] == "fault" and row["component"] == "load_client" and row["status"] == 504)
        downstream = [row for row in rows if row["component"] == "dependency" and row["trace_id"] == failed["trace_id"]]
        self.assertEqual(len(downstream), 1)
        self.assertEqual(downstream[0]["status"], 200)
        self.assertGreater(downstream[0]["latency_ms"], 250)

    def test_bad_configuration_hits_a_real_missing_route(self):
        rows = self._events("bad_configuration")
        fault = [row for row in rows if row["phase"] == "fault"]
        self.assertTrue(any(row["component"] == "dependency" and row["status"] == 404 for row in fault))
        self.assertTrue(any(row["component"] == "load_client" and row["status"] == 502 for row in fault))
        self.assertFalse(any(row["error_code"] == "load_client_error" for row in rows))

    def test_sampled_trace_remains_queryable_across_services(self):
        result = self.cases["dependency_timeout"]
        tools = IncidentTools(Path(result["case_path"]))
        front = result["provenance"]["services"]["checkout"]
        logs = tools.execute("search_logs", {"service": front, "phase": "fault"})["records"]
        self.assertTrue(logs)
        trace = tools.execute("get_trace", {"trace_id": logs[0]["trace_id"]})["records"]
        self.assertEqual({record["service"] for record in trace}, set(result["provenance"]["services"].values()))

    def test_every_metric_reconciles_with_saved_raw_requests(self):
        for scenario, result in self.cases.items():
            events = self._events(scenario)
            client_rows = [row for row in events if row["component"] == "load_client"]
            self.assertEqual(len(client_rows), len(PHASES) * REQUESTS_PER_PHASE)
            self.assertEqual(len({row["request_id"] for row in client_rows}), len(client_rows))
            self.assertEqual(len({row["trace_id"] for row in client_rows}), len(client_rows))
            for phase in PHASES:
                for component in ("load_client", "checkout", "dependency"):
                    with self.subTest(scenario=scenario, phase=phase, component=component):
                        rows = [row for row in events if row["phase"] == phase and row["component"] == component]
                        metric = result["provenance"]["measurements"][phase][component]
                        self.assertEqual(metric["requests"], len(rows))
                        self.assertEqual(metric["errors"], sum(not 200 <= row["status"] < 300 for row in rows))
                        self.assertEqual(metric["status_counts"], dict(Counter(str(row["status"]) for row in rows)))
                        self.assertTrue(all(row["request_id"] and row["trace_id"] for row in rows))
            snapshot = json.loads(Path(result["case_path"]).read_text(encoding="utf-8"))
            for record in snapshot["records"]:
                if record["kind"] == "log":
                    self.assertIn(json.loads(record["content"]), events)
                if record["kind"] == "metric":
                    actual = json.loads(record["content"])
                    self.assertEqual(actual, result["provenance"]["measurements"][actual["phase"]][actual["component"]])

    def test_snapshot_and_manifest_separate_control_plane_labels(self):
        manifest_path = Path(self.suite["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(len(manifest["cases"]), 4)
        service_aliases, all_evidence_ids = [], set()
        for entry in manifest["cases"]:
            path = manifest_path.parent / entry["path"]
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(entry["case_id"], snapshot["case_id"])
            self.assertEqual(entry["source"], "local_fault_lab")
            self.assertEqual(entry["label"]["split"], "dev")
            self.assertIs(snapshot["synthetic"], False)
            self.assertNotIn("scenario", snapshot)
            self.assertNotIn("label", snapshot)
            self.assertNotIn("cause_code", snapshot)
            self.assertNotIn("root_service", snapshot)
            self.assertNotIn("control_plane", snapshot)
            self.assertTrue((manifest_path.parent / entry["provenance_path"]).exists())
            ids = {record["id"] for record in snapshot["records"]}
            self.assertFalse(ids.intersection(all_evidence_ids))
            all_evidence_ids.update(ids)
            self.assertTrue(set(entry["label"]["required_evidence"]).issubset(ids))
            for name in SCENARIOS:
                self.assertNotIn(name, path.name)
                self.assertNotIn(name, snapshot["case_id"])
                self.assertNotIn(name, snapshot["alert"])
                self.assertTrue(all(name not in value for value in ids))
            service_aliases.append(snapshot["services"])
        # Same seed preserves service aliases while individual observation IDs vary.
        self.assertTrue(all(aliases == service_aliases[0] for aliases in service_aliases))

    def test_ports_and_server_threads_are_released(self):
        for result in self.cases.values():
            provenance = result["provenance"]
            self.assertTrue(provenance["servers_closed"])
            for endpoint in provenance["endpoints"].values():
                self.assertEqual(endpoint["host"], "127.0.0.1")
                with socket.socket() as connection:
                    connection.settimeout(0.2)
                    self.assertNotEqual(connection.connect_ex((endpoint["host"], endpoint["port"])), 0)
        self.assertFalse(any(thread.name.startswith("opslens-lab-") for thread in threading.enumerate()))

    def test_rejected_observations_preserve_audit_but_no_label(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("opslens.lab._validate_observations", return_value=["intentional quality-gate rejection"]):
                with self.assertRaisesRegex(LabValidationError, "audit preserved"):
                    collect_case(Path(directory), "healthy")
            destinations = list(Path(directory).iterdir())
            self.assertEqual(len(destinations), 1)
            case = destinations[0]
            self.assertTrue((case / "snapshot.json").exists())
            self.assertTrue((case / "requests.jsonl").exists())
            self.assertFalse((case / "label.json").exists())
            provenance = json.loads((case / "provenance.json").read_text(encoding="utf-8"))
            self.assertFalse(provenance["validation"]["passed"])

    def test_invalid_inputs_start_no_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for scenario, seed in (("not-a-scenario", 0), ("healthy", True), ("healthy", 2**64)):
                with self.assertRaises(ValueError):
                    collect_case(path, scenario, seed)
            for arguments in ({"seeds": []}, {"seeds": [0, 0]}, {"scenarios": []},
                              {"scenarios": ["healthy", "healthy"]}):
                with self.assertRaises(ValueError):
                    collect_suite(path, **arguments)
            self.assertEqual(list(path.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
