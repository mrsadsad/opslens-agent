import copy
import json
import tempfile
import unittest
from pathlib import Path

from opslens.context import build_context
from opslens.investigation import record_hypothesis, diagnosis_gate
from opslens.runtime import AgentRuntime, validate_report
from opslens.store import RunStore
from opslens.providers import ProviderError
from tests.test_runtime import FixtureTools, SequenceProvider, call, report


def hypothesis(**updates):
    return {"id": "pool", "cause_code": "connection_pool_exhaustion", "statement": "Reduced pool capacity causes waiting",
            "status": "supported", "supporting_evidence_ids": ["m1"], "contradicting_evidence_ids": [],
            "next_check": "Inspect downstream latency to exclude dependency saturation", **updates}


class InvestigationTests(unittest.TestCase):
    def test_workflow_rejects_premature_diagnosis_then_accepts_checked_alternative(self):
        responses = [call("query_metrics", {}), call("submit_diagnosis", report()),
                     call("record_hypothesis", hypothesis()),
                     call("record_hypothesis", hypothesis(id="dependency", cause_code="dependency_timeout", status="ruled_out",
                          supporting_evidence_ids=[], contradicting_evidence_ids=["m1"])),
                     call("submit_diagnosis", report())]
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(FixtureTools(), SequenceProvider(responses), RunStore(Path(directory) / "db"), policy="investigator")
            state = runtime.run(runtime.start())
            self.assertEqual(state["status"], "completed")
            self.assertEqual(len(state["hypotheses"]), 2)
            errors = [event for event in state["events"] if event.get("result", {}).get("ok") is False]
            self.assertEqual(errors[0]["result"]["error"]["code"], "invalid_report")
        # This test verifies workflow only. Its deliberately simple fixture does
        # NOT validate whether one metric semantically supports the alternatives.

    def test_cannot_forge_or_double_assign_evidence(self):
        observed = {"m1": {"kind": "metric"}}
        for item in (hypothesis(supporting_evidence_ids=["invented"]), hypothesis(contradicting_evidence_ids=["m1"])):
            self.assertFalse(record_hypothesis(item, observed, {})["ok"])

    def test_runbook_only_cannot_establish_cause(self):
        self.assertFalse(record_hypothesis(hypothesis(), {"m1": {"kind": "runbook"}}, {})["ok"])

    def test_abstention_never_requires_inventing_alternative_evidence(self):
        self.assertIsNone(diagnosis_gate({"status": "insufficient_evidence"}, {}))

    def test_component_must_have_been_observed(self):
        diagnosis = report()
        diagnosis["root_service"] = "guessed-service"
        self.assertIsNotNone(validate_report(diagnosis, {"m1": {"kind": "metric", "service": "checkout"}}))

    def test_compaction_keeps_exact_records_and_no_broken_tool_protocol(self):
        state = {"messages": [{"role": "system", "content": "instructions"}, {"role": "user", "content": "alert"},
                              {"role": "assistant", "content": "old" * 3000}],
                 "observed": {f"m{i}": {"id": f"m{i}", "kind": "metric", "content": "data" * 100} for i in range(8)},
                 "hypotheses": {}, "max_steps": 10, "steps": 3, "max_tool_calls": 20, "tool_calls": 4}
        original = copy.deepcopy(state)
        context, metadata = build_context(state, [], 2000)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 2000)
        self.assertGreater(metadata["omitted_records"], 0)
        self.assertEqual([message["role"] for message in context], ["system", "user", "user"])
        ledger = json.loads(context[-1]["content"])
        for record in ledger["records"]:
            self.assertEqual(record, state["observed"][record["id"]])
        self.assertEqual(state, original)

    def test_token_stop_prevents_another_request(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(FixtureTools(), SequenceProvider([call("query_metrics", {})]), RunStore(Path(directory) / "db"))
            state = runtime.run(runtime.start(max_total_tokens=10))
            self.assertEqual(state["status"], "budget_exhausted")
            self.assertEqual(state["steps"], 1)
            self.assertEqual(state["usage"]["total_tokens"], 15)  # Threshold checked between calls, not a spend ceiling.

    def test_transport_retry_marks_usage_partial(self):
        response = call("query_metrics", {})
        response.update(usage_complete=False, transport_attempts=2)
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(FixtureTools(), SequenceProvider([response]), RunStore(Path(directory) / "db"))
            state = runtime.run(runtime.start(max_steps=1))
            self.assertFalse(state["usage_complete"])
            self.assertEqual(state["transport_attempts"], 2)

    def test_failed_transport_still_counts_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(FixtureTools(), SequenceProvider([ProviderError("network_error", transport_attempts=2)]),
                                   RunStore(Path(directory) / "db"))
            state = runtime.run(runtime.start())
            self.assertEqual(state["transport_attempts"], 2)
            self.assertTrue(state["transport_attempts_complete"])

    def test_compaction_budget_and_repair_feedback_at_boundary(self):
        state = {"messages": [{"role": "system", "content": "instructions"}, {"role": "user", "content": "alert"},
                              {"role": "assistant", "content": "old" * 10000}],
                 "observed": {f"m{i}": {"id": f"m{i}", "kind": "log", "content": "a" * 1040} for i in range(12)},
                 "hypotheses": {}, "max_steps": 10, "steps": 3, "max_tool_calls": 20, "tool_calls": 4,
                 "events": [{"kind": "tool_result", "tool": "submit_diagnosis", "result": {"ok": False,
                     "error": {"code": "invalid_report", "message": "unobserved evidence"}}}]}
        context, metadata = build_context(state, [], 12000)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 12000)
        self.assertLessEqual(metadata["after_chars"], 12000)
        ledger = json.loads(context[-1]["content"])
        self.assertEqual(ledger["recent_errors"][0]["code"], "invalid_report")
        self.assertIn("unobserved evidence", ledger["recent_errors"][0]["message"])


if __name__ == "__main__":
    unittest.main()
