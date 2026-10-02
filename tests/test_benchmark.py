"""Protocol-only offline checks. These fixtures never read gold labels."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from opslens.benchmark import (
    BudgetedProvider, BudgetedTools, ModelCallBudget, execute_benchmark,
    load_manifest, summarize_attempts, validate_manifest,
)
from opslens.providers import ChatProvider, ProviderError


class ProtocolFixtureProvider:
    """Always abstains, regardless of case: tests plumbing, not diagnosis skill."""

    name = "offline-protocol-fixture"
    is_scripted = True

    def __init__(self, *, error=None, partial_usage=False):
        self.calls = []
        self.error = error
        self.partial_usage = partial_usage

    def complete(self, messages, tools):
        self.calls.append({"messages": json.dumps(messages), "tools": tools})
        if self.error:
            raise self.error
        report = {"status": "insufficient_evidence", "cause_code": "insufficient_evidence",
                  "root_cause": "Protocol fixture does not diagnose incidents.", "evidence_ids": [],
                  "explanation": "A fixed abstention exercises protocol handling only.", "next_steps": []}
        return {"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "fixture-call", "type": "function",
             "function": {"name": "submit_diagnosis", "arguments": json.dumps(report)}}]},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "usage_complete": not self.partial_usage}


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.providers = []

    def provider_factory(self):
        provider = ProtocolFixtureProvider()
        self.providers.append(provider)
        return provider

    def manifest(self, count=2, *, mixed_sources=False, budget=None):
        cases = []
        for index in range(count):
            case_id, service = f"case-{index}", f"svc-{index}"
            snapshot = {"synthetic": not (mixed_sources and index % 2), "case_id": case_id,
                        "alert": f"{service} latency and error rate elevated", "services": [service],
                        "records": [{"id": f"record-{index}-{kind}", "kind": kind,
                                     "service": service, "content": "latency error timeout observation"}
                                    for kind in ("metric", "log", "change", "runbook")]}
            if not snapshot["synthetic"]:
                snapshot["source"] = "local_fault_lab"
            case_path = self.root / f"input-{index}.json"
            case_path.write_text(json.dumps(snapshot), encoding="utf-8")
            cases.append({"case_id": case_id, "path": case_path.name,
                          "sha256": hashlib.sha256(case_path.read_bytes()).hexdigest(),
                          "group_id": f"family-{index}",
                          "label": {"cause_code": "dependency_timeout", "root_service": service,
                                    "required_evidence": [f"record-{index}-log"], "split": "test",
                                    "private_note": "GOLD_SENTINEL_MUST_NOT_REACH_PROVIDER"}})
        payload = {"schema_version": 1, "dataset_version": "unit-v1", "cases": cases}
        if budget:
            payload["observation_budget"] = budget
        path = self.root / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def read_rows(self, output="output"):
        return json.loads((self.root / output / "attempts.json").read_text(encoding="utf-8"))["attempts"]

    def test_grid_keeps_all_attempts_without_gold_in_model_input(self):
        result = execute_benchmark(self.manifest(), self.root / "output", self.provider_factory,
                                   ["single-shot", "fixed-workflow", "react", "investigator"], repeats=2)
        self.assertEqual(result["n_attempts"], 16)
        self.assertEqual(result["model_calls_used"], 16)
        self.assertTrue(result["scripted_demo"])
        self.assertEqual(result["interpretation"], "protocol_demo_only")
        self.assertIn("DEMO ONLY", result["warnings"][0])
        self.assertTrue(result["experiment_complete"])
        rows = self.read_rows()
        self.assertEqual(len({(r["case_id"], r["repeat"], r["policy"]) for r in rows}), 16)
        for row in rows:
            self.assertFalse(row["score"]["task_success"])
            self.assertTrue((self.root / "output" / row["state_path"]).is_file())
            self.assertTrue((self.root / "output" / row["score_path"]).is_file())
        for provider in self.providers:
            self.assertEqual(len(provider.calls), 1)
            self.assertNotIn("GOLD_SENTINEL", provider.calls[0]["messages"])
        self.assertEqual(load_manifest(self.root / "output" / "manifest.json")["dataset_version"], "unit-v1")
        for stats in result["strata"][0]["policies"].values():
            self.assertEqual(stats["n"], 4)
            self.assertFalse(stats["wilson_independent_trial_layout"])
            self.assertEqual(sum(stats["failure_taxonomy"].values()), 4)

    def test_global_budget_never_overruns_and_unattempted_rows_stay_in_denominator(self):
        result = execute_benchmark(self.manifest(3), self.root / "output", self.provider_factory,
                                   ["single-shot", "fixed-workflow"], max_model_calls=2)
        self.assertEqual(result["model_calls_used"], 2)
        self.assertEqual(sum(len(provider.calls) for provider in self.providers), 2)
        self.assertEqual(result["n_attempts"], 6)
        self.assertFalse(result["experiment_complete"])
        rows = self.read_rows()
        self.assertEqual(sum(r["score"]["failure_category"] == "global_budget_exhausted" for r in rows), 4)
        comparison = result["strata"][0]["paired_comparisons"][0]
        self.assertTrue(comparison["budget_or_interrupt_censored"])
        self.assertIsNone(comparison["ci_95"])
        self.assertEqual(result["strata"][0]["policies"]["single-shot"]["n"], 3)

    def test_zero_global_budget_does_not_construct_a_provider(self):
        result = execute_benchmark(self.manifest(1), self.root / "output", self.provider_factory,
                                   ["react"], max_model_calls=0)
        self.assertEqual(self.providers, [])
        self.assertEqual(result["model_calls_used"], 0)
        self.assertEqual(result["n_attempts"], 1)
        stats = result["strata"][0]["policies"]["react"]
        self.assertIsNone(stats["task_success_wilson_95"])
        self.assertEqual(stats["inference_block_reason"], "budget_or_interruption_censored")
        self.assertFalse(stats["wilson_independent_trial_layout"])

    def test_censoring_suppresses_all_policy_intervals_within_the_stratum(self):
        result = execute_benchmark(self.manifest(2), self.root / "output", self.provider_factory,
                                   ["single-shot", "fixed-workflow"], max_model_calls=3)
        # Rotating order lets fixed-workflow finish both cases, but the shared
        # comparison stratum is incomplete. Neither policy gets a Wilson CI.
        policies = result["strata"][0]["policies"]
        self.assertEqual(policies["fixed-workflow"]["n_attempted"], 2)
        self.assertEqual(policies["single-shot"]["n_attempted"], 1)
        for stats in policies.values():
            self.assertIsNone(stats["task_success_wilson_95"])
            self.assertIn("suppressed", stats["wilson_note"])
            self.assertEqual(stats["n"], 2)

    def test_provider_identity_change_invalidates_experiment_and_stops_further_calls(self):
        names, constructed = iter(["provider-A", "provider-B", "provider-A", "provider-A"]), []
        def factory():
            provider = ProtocolFixtureProvider()
            provider.name = next(names)
            constructed.append(provider)
            return provider
        policies = ["single-shot", "fixed-workflow"]
        result = execute_benchmark(self.manifest(2), self.root / "output", factory, policies)
        self.assertEqual(len(constructed), 2)
        self.assertEqual([len(provider.calls) for provider in constructed], [1, 0])
        self.assertEqual(result["model_calls_used"], 1)
        self.assertEqual(result["n_attempts"], 4)
        self.assertFalse(result["experiment_complete"])
        self.assertFalse(result["experiment_valid"])
        self.assertEqual(result["interpretation"], "invalid_experiment")
        self.assertEqual(result["invalid_reason"], "provider_identity_changed")
        self.assertEqual(result["invalid_details"]["expected_provider"], "provider-A")
        self.assertEqual(result["invalid_details"]["observed_provider"], "provider-B")
        rows = self.read_rows()
        self.assertEqual([row["score"]["run_status"] for row in rows],
                         ["completed", "experiment_invalid", "skipped_invalid", "skipped_invalid"])
        self.assertEqual([row["attempted"] for row in rows], [True, True, False, False])
        self.assertTrue(all((self.root / "output" / row["state_path"]).is_file() for row in rows))
        for stats in result["strata"][0]["policies"].values():
            self.assertEqual(stats["n"], 2)
            self.assertIsNone(stats["task_success_wilson_95"])
            self.assertEqual(stats["inference_block_reason"], "provider_identity_changed")
        comparison = result["strata"][0]["paired_comparisons"][0]
        self.assertIsNone(comparison["ci_95"])
        self.assertFalse(comparison["inference_valid"])
        self.assertEqual(comparison["inference_block_reason"], "provider_identity_changed")
        # Reloading the saved index must not bring invalid intervals back.
        self.assertEqual(summarize_attempts(rows, policies), result["strata"])

    def test_provider_identity_change_also_invalidates_earlier_complete_strata(self):
        names = iter(["provider-A", "provider-A", "provider-B"])
        def factory():
            provider = ProtocolFixtureProvider()
            provider.name = next(names)
            return provider
        result = execute_benchmark(self.manifest(2, mixed_sources=True), self.root / "output", factory,
                                   ["single-shot", "fixed-workflow"])
        synthetic = next(stratum for stratum in result["strata"] if stratum["source_kind"] == "synthetic")
        for stats in synthetic["policies"].values():
            self.assertEqual(stats["n_attempted"], 1)
            self.assertIsNone(stats["task_success_wilson_95"])
            self.assertEqual(stats["inference_block_reason"], "provider_identity_changed")
        self.assertFalse(synthetic["paired_comparisons"][0]["inference_valid"])

    def test_hash_mismatch_is_rejected_before_any_provider_or_output(self):
        manifest = self.manifest(1)
        (self.root / "input-0.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            execute_benchmark(manifest, self.root / "output", self.provider_factory, ["react"])
        self.assertFalse((self.root / "output").exists())
        self.assertEqual(self.providers, [])

    def test_related_groups_must_not_cross_splits(self):
        path = self.manifest()
        manifest = json.loads(path.read_text())
        manifest["cases"][1]["group_id"] = manifest["cases"][0]["group_id"]
        manifest["cases"][1]["label"]["split"] = "dev"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "crosses splits"):
            load_manifest(path)

    def test_source_kinds_are_separate_strata(self):
        result = execute_benchmark(self.manifest(mixed_sources=True), self.root / "output",
                                   self.provider_factory, ["single-shot"])
        self.assertEqual({group["source_kind"] for group in result["strata"]}, {"synthetic", "live_capture"})
        self.assertTrue(all(group["n_cases"] == 1 for group in result["strata"]))

    def test_baseline_prefetch_and_submission_share_tool_budget(self):
        execute_benchmark(self.manifest(1), self.root / "output", self.provider_factory,
                          ["single-shot", "fixed-workflow"], max_tool_calls=2)
        for row in self.read_rows():
            state = json.loads((self.root / "output" / row["state_path"]).read_text(encoding="utf-8"))
            self.assertEqual(state["tool_calls"], 2)
            self.assertEqual(state["observation_budget"]["retrieval_calls"], 1)
            self.assertEqual(row["model_calls"], 1)
        self.assertTrue(all(len(provider.calls[0]["tools"]) == 1 for provider in self.providers))

    def test_observation_limits_apply_to_baseline_collection(self):
        budget = {"max_chars": 200, "max_records": 1}
        execute_benchmark(self.manifest(1, budget=budget), self.root / "output",
                          self.provider_factory, ["single-shot", "fixed-workflow"])
        for row in self.read_rows():
            self.assertLessEqual(row["observation_budget"]["delivered_chars"], 200)
            self.assertLessEqual(row["observation_budget"]["delivered_records"], 1)

    def test_agent_cannot_make_a_second_request_after_global_budget_is_spent(self):
        class ReadThenAbstain(ProtocolFixtureProvider):
            def complete(self, messages, tools):
                if not self.calls:
                    self.calls.append({"messages": json.dumps(messages), "tools": tools})
                    return {"message": {"role": "assistant", "tool_calls": [
                        {"id": "read-services", "type": "function",
                         "function": {"name": "list_services", "arguments": "{}"}}]},
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
                return super().complete(messages, tools)
        providers = []
        def factory():
            provider = ReadThenAbstain()
            providers.append(provider)
            return provider
        result = execute_benchmark(self.manifest(1), self.root / "output", factory,
                                   ["react"], max_model_calls=1)
        self.assertEqual(result["model_calls_used"], 1)
        self.assertEqual(len(providers[0].calls), 1)
        row = self.read_rows()[0]
        self.assertEqual(row["score"]["failure_category"], "global_budget_exhausted")
        state = json.loads((self.root / "output" / row["state_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(state["observed"]), 1)
        self.assertEqual(state["observation_budget"]["delivered_records"], 1)
        self.assertEqual(state["source"], "synthetic")

    def test_one_call_baselines_really_use_distinct_retrieval_plans(self):
        execute_benchmark(self.manifest(1), self.root / "output", self.provider_factory,
                          ["single-shot", "fixed-workflow"])
        log_queries = {}
        for row in self.read_rows():
            state = json.loads((self.root / "output" / row["state_path"]).read_text(encoding="utf-8"))
            log_queries[row["policy"]] = [event["arguments"].get("query") for event in state["events"]
                                          if event.get("tool") == "search_logs"]
            self.assertEqual(state["steps"], 1)
        self.assertEqual(log_queries["single-shot"], [None])
        self.assertEqual(log_queries["fixed-workflow"], ["latency", "error"])

    def test_failure_is_preserved_without_exception_secret(self):
        def failing_factory():
            return ProtocolFixtureProvider(error=RuntimeError("private-provider-response-do-not-store"))
        result = execute_benchmark(self.manifest(1), self.root / "output", failing_factory,
                                   ["single-shot", "react"])
        self.assertEqual(result["n_attempts"], 2)
        for row in self.read_rows():
            state_text = (self.root / "output" / row["state_path"]).read_text(encoding="utf-8")
            self.assertNotIn("private-provider-response", state_text)
            self.assertEqual(row["score"]["failure_category"], "provider_error")
            self.assertFalse(row["score"]["usage_complete"])

    def test_interrupt_preserves_baseline_observations_and_marks_remaining_rows(self):
        result = execute_benchmark(self.manifest(), self.root / "output",
                                   lambda: ProtocolFixtureProvider(error=KeyboardInterrupt()),
                                   ["single-shot", "fixed-workflow"])
        self.assertFalse(result["experiment_complete"])
        self.assertEqual(result["model_calls_used"], 1)
        rows = self.read_rows()
        self.assertTrue(all(row["score"]["failure_category"] == "interrupted" for row in rows))
        first = json.loads((self.root / "output" / rows[0]["state_path"]).read_text(encoding="utf-8"))
        self.assertTrue(first["observed"])
        self.assertFalse(first["usage_complete"])
        for stats in result["strata"][0]["policies"].values():
            self.assertIsNone(stats["task_success_wilson_95"])
            self.assertEqual(stats["inference_block_reason"], "budget_or_interruption_censored")

    def test_partial_usage_flag_survives_baseline_response(self):
        result = execute_benchmark(self.manifest(1), self.root / "output",
                                   lambda: ProtocolFixtureProvider(partial_usage=True), ["single-shot"])
        self.assertFalse(result["strata"][0]["policies"]["single-shot"]["usage_complete"])

    def test_existing_output_cannot_be_overwritten(self):
        manifest = self.manifest(1)
        output = self.root / "output"
        output.mkdir()
        (output / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(ValueError, "must be empty"):
            execute_benchmark(manifest, output, self.provider_factory, ["react"])
        self.assertEqual((output / "keep.txt").read_text(), "keep")

    def test_dry_run_has_no_provider_or_output_and_is_json_serializable(self):
        result = validate_manifest(self.manifest())
        self.assertEqual(result["case_count"], 2)
        self.assertEqual(result["splits"], ["test"])
        self.assertEqual(result["source_kinds"], ["synthetic"])
        json.dumps(result)
        self.assertEqual(self.providers, [])
        self.assertFalse((self.root / "output").exists())

    def test_summary_rejects_duplicate_or_missing_policy_outcomes(self):
        execute_benchmark(self.manifest(1), self.root / "output", self.provider_factory,
                          ["single-shot", "fixed-workflow"])
        rows = self.read_rows()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            summarize_attempts(rows + [rows[0]], ["single-shot", "fixed-workflow"])
        with self.assertRaisesRegex(ValueError, "all scheduled"):
            summarize_attempts(rows[:1], ["single-shot", "fixed-workflow"])


class BudgetTests(unittest.TestCase):
    def test_global_reservation_is_atomic_across_workers(self):
        budget = ModelCallBudget(7)
        def reserve(_):
            try:
                budget.reserve()
                return True
            except ProviderError:
                return False
        with ThreadPoolExecutor(max_workers=8) as executor:
            successes = list(executor.map(reserve, range(40)))
        self.assertEqual(sum(successes), 7)
        self.assertEqual(budget.used, 7)
        self.assertEqual(budget.remaining, 0)

    def test_repeated_record_delivery_is_not_free(self):
        record = {"id": "opaque-id", "kind": "log", "content": "observation", "service": "svc"}
        raw = SimpleNamespace(case_id="case", alert="alert", fingerprint="hash", schemas=[],
                              execute=lambda *_: {"ok": True, "records": [record]})
        tools = BudgetedTools(raw, max_chars=1000, max_records=2)
        self.assertTrue(tools.execute("read", {})["records"])
        self.assertTrue(tools.execute("read", {})["records"])
        self.assertEqual(tools.execute("read", {})["records"], [])
        self.assertEqual(tools.measurements()["delivered_records"], 2)
        self.assertEqual(tools.measurements()["unique_records"], 1)

    def test_builtin_transport_retries_cannot_bypass_shared_budget(self):
        provider = ChatProvider("http://localhost:1234", "fixture-model", "test-key", max_attempts=3)
        wrapped = BudgetedProvider(provider, ModelCallBudget(0))
        self.assertEqual(provider.max_attempts, 1)
        with self.assertRaises(ProviderError):
            wrapped.complete([], [])
        self.assertEqual(wrapped.model_calls, 0)


if __name__ == "__main__":
    unittest.main()
