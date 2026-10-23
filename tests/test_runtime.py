import json
import tempfile
import unittest
from pathlib import Path

from opslens.runtime import AgentRuntime, _bounded_result
from opslens.store import CheckpointConflict, RunStore


def call(name, arguments, call_id="call-1"):
    return {"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
    ]}, "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


def report(evidence=None):
    return {"status": "diagnosed", "cause_code": "connection_pool_exhaustion",
            "root_cause": "pool limit changed", "evidence_ids": evidence or ["m1"],
            "explanation": "saturation after configuration change", "next_steps": ["verify configuration"]}


class FixtureTools:
    case_id, alert, fingerprint, schemas = "unit-case", "latency elevated", "snapshot-hash", []

    def execute(self, name, arguments):
        if name == "query_metrics":
            return {"ok": True, "records": [{"id": "m1", "content": "pool saturated", "kind": "metric"}]}
        return {"ok": False, "error": {"code": "unknown_tool", "message": "unknown tool"}}


class SequenceProvider:
    name = "test-provider"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.count = 0

    def complete(self, messages, tools):
        self.count += 1
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name) / "runs.sqlite3")

    def runtime(self, responses):
        return AgentRuntime(FixtureTools(), SequenceProvider(responses), self.store)

    def test_fabricated_evidence_rejected_then_model_can_recover(self):
        runtime = self.runtime([call("submit_diagnosis", report(["made-up"])),
                                call("query_metrics", {}), call("submit_diagnosis", report())])
        state = runtime.run(runtime.start())
        self.assertEqual(state["status"], "completed")
        results = [event for event in state["events"] if event["kind"] == "tool_result"]
        self.assertEqual(results[0]["result"]["error"]["code"], "invalid_report")
        self.assertEqual(state["usage"]["total_tokens"], 45)

    def test_pause_resume_retains_observations_and_budget(self):
        runtime = self.runtime([call("query_metrics", {}), call("submit_diagnosis", report())])
        state = runtime.run(runtime.start(), stop_after_steps=1)
        self.assertEqual(state["status"], "paused")
        loaded = self.store.load(state["run_id"])
        self.assertIn("m1", loaded["observed"])
        state = runtime.run(loaded)
        self.assertEqual((state["status"], state["steps"]), ("completed", 2))
        count = runtime.provider.count
        runtime.run(self.store.load(state["run_id"]))
        self.assertEqual(runtime.provider.count, count)

    def test_tool_budget_includes_rejected_calls(self):
        runtime = self.runtime([call("delete_everything", {}), call("query_metrics", {})])
        state = runtime.run(runtime.start(max_tool_calls=1))
        self.assertEqual((state["status"], state["tool_calls"]), ("budget_exhausted", 1))
        self.assertIsNone(state["report"])

    def test_unobserved_after_truncation_cannot_be_cited(self):
        result = _bounded_result({"ok": True, "records": [{"id": "huge", "content": "a" * 1000}]}, 100)
        self.assertEqual(result["records"], [])
        self.assertTrue(result["truncated"])

    def test_model_failure_keeps_checkpoint_and_redacts_exception(self):
        runtime = self.runtime([RuntimeError("secret-key-do-not-store")])
        state = runtime.run(runtime.start())
        self.assertEqual(state["status"], "provider_error")
        self.assertEqual(state["steps"], 1)
        self.assertFalse(state["usage_complete"])
        self.assertNotIn("secret-key-do-not-store", json.dumps(self.store.load(state["run_id"])))

    def test_resume_rejects_changed_case(self):
        runtime = self.runtime([])
        state = runtime.start()
        runtime.tools.fingerprint = "different"
        with self.assertRaises(ValueError):
            runtime.run(state)

    def test_checkpoint_optimistic_lock(self):
        state = self.runtime([]).start()
        stale = self.store.load(state["run_id"])
        self.store.save(state)
        with self.assertRaises(CheckpointConflict):
            self.store.save(stale)

    def test_invalid_tool_json_consumes_budget_without_crashing(self):
        response = call("query_metrics", {})
        response["message"]["tool_calls"][0]["function"]["arguments"] = "{invalid"
        runtime = self.runtime([response])
        state = runtime.run(runtime.start(max_steps=1))
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["events"][-2]["result"]["error"]["code"], "invalid_arguments")

    def test_nonfinite_arguments_are_repairable_and_keep_usage_and_checkpoint(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e999", '-1e999', '{"nested":[NaN]}'):
            with self.subTest(value=value):
                response = call("query_metrics", {})
                response["message"]["tool_calls"][0]["function"]["arguments"] = '{"service":' + value + '}'
                runtime = self.runtime([response, call("query_metrics", {}), call("submit_diagnosis", report())])
                state = runtime.run(runtime.start(), stop_after_steps=1)
                saved = self.store.load(state["run_id"])
                rejection = next(event for event in saved["events"] if event["kind"] == "tool_result")
                self.assertEqual(rejection["result"]["error"]["code"], "invalid_arguments")
                self.assertIsNone(rejection["arguments"])
                self.assertEqual(saved["observed"], {})
                self.assertFalse(saved["pending_model_request"])
                self.assertEqual((saved["steps"], saved["tool_calls"], saved["usage"]["total_tokens"]), (1, 1, 15))
                completed = runtime.run(saved)
                self.assertEqual((completed["status"], completed["steps"], completed["usage"]["total_tokens"]), ("completed", 3, 45))
                self.assertTrue(completed["usage_complete"])
                self.assertEqual(self.store.load(state["run_id"])["report"], report())

    def test_missing_usage_is_not_reported_as_measured_zero(self):
        response = call("query_metrics", {})
        response["usage"] = {}
        runtime = self.runtime([response])
        state = runtime.run(runtime.start(max_steps=1))
        self.assertFalse(state["usage_complete"])

    def test_interrupted_request_marks_usage_incomplete_after_resume(self):
        runtime = self.runtime([KeyboardInterrupt(), call("query_metrics", {}), call("submit_diagnosis", report())])
        state = runtime.start()
        with self.assertRaises(KeyboardInterrupt):
            runtime.run(state)
        loaded = self.store.load(state["run_id"])
        self.assertTrue(loaded["pending_model_request"])
        state = runtime.run(loaded)
        self.assertEqual(state["status"], "completed")
        self.assertFalse(state["usage_complete"])
        self.assertEqual(state["steps"], 3)

    def test_protocol_error_still_honors_pause(self):
        runtime = self.runtime([{"message": {}, "usage": {}}])
        state = runtime.run(runtime.start(max_steps=3), stop_after_steps=1)
        self.assertEqual((state["status"], state["steps"]), ("paused", 1))


if __name__ == "__main__":
    unittest.main()
