"""Known unsafe conclusions must trigger repair, including on saved-run scoring."""

import copy
import tempfile
import unittest
from pathlib import Path

from opslens.runtime import AgentRuntime
from opslens.context import build_context
from opslens.store import RunStore
from opslens.evaluation import score_run, _validate_label
from opslens.benchmark import BudgetedTools, BudgetedProvider, ModelCallBudget, _new_state, _run_baseline
from tests.test_runtime import FixtureTools, SequenceProvider, call, report
from tests.test_evaluation import make_state, make_label


class NumericTools(FixtureTools):
    source = "synthetic"

    def execute(self, name, arguments):
        if name == "list_services":
            return {"ok": True, "records": [{"id": "s1", "service": "checkout", "kind": "service", "content": "checkout"}]}
        if name == "query_metrics":
            return {"ok": True, "records": [{"id": "m1", "kind": "metric", "service": "checkout",
                                              "content": "DB_POOL_MAX changed from 100 to 20."}]}
        return {"ok": True, "records": []}


def numeric_report(correct=False):
    value = report()
    value["root_service"] = "checkout"
    value["explanation"] = "连接池从100降到20，容量减少80%。" if correct else "连接池从100降到20，容量砍半。"
    return value


class QualityIntegrationTests(unittest.TestCase):
    def test_budget_notice_is_current_bounded_and_does_not_mutate_history(self):
        state = {"messages": [{"role": "system", "content": "instructions"}, {"role": "user", "content": "alert"}],
                 "max_steps": 6, "steps": 5, "max_tool_calls": 32, "tool_calls": 29}
        original = copy.deepcopy(state)
        context, compaction = build_context(state, [], 12000)
        import json
        notice = json.loads(context[-1]["content"])
        self.assertEqual(notice["runtime_budget"], {"remaining_model_calls_including_this": 1, "remaining_tool_calls": 3})
        self.assertEqual(state, original)
        self.assertIsNone(compaction)

    def test_rejected_numeric_claim_can_be_repaired_and_is_preserved_in_trace(self):
        provider = SequenceProvider([call("query_metrics", {}), call("submit_diagnosis", numeric_report()),
                                     call("submit_diagnosis", numeric_report(True))])
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            runtime = AgentRuntime(NumericTools(), provider, store)
            state = runtime.run(runtime.start())
            self.assertEqual(state["status"], "completed")
            rejected = [e for e in state["events"] if e.get("result", {}).get("error", {}).get("code") == "report_audit_failed"]
            self.assertEqual(len(rejected), 1)
            self.assertIn("砍半", rejected[0]["arguments"]["explanation"])
            self.assertEqual(store.load(state["run_id"])["report_audit"]["issues"], [])
            self.assertEqual(state["steps"], 3)

    def test_one_call_baseline_has_the_same_numeric_gate_and_no_free_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            tools = BudgetedTools(NumericTools())
            provider = BudgetedProvider(SequenceProvider([call("submit_diagnosis", numeric_report())]), ModelCallBudget(1))
            state = _new_state(tools.case_id, tools.fingerprint, provider.name, "single-shot")
            state = _run_baseline(tools, provider, store, "single-shot", 24, state)
            self.assertEqual(provider.model_calls, 1)
            self.assertIsNone(state["report"])
            self.assertTrue(state["report_audit"]["issues"])
            self.assertEqual(state["events"][-1]["result"]["error"]["code"], "report_audit_failed")
            score = score_run(state, {"cause_code": "connection_pool_exhaustion", "required_evidence": ["m1"], "split": "dev"})
            self.assertIsNone(score["numeric_audit_passed"])
            self.assertEqual(score["numeric_rejection_count"], 1)
            self.assertEqual(score["failure_category"], "report_audit_failed")

    def test_rescore_does_not_trust_old_audit_and_separates_structure_from_quality(self):
        state = make_state()
        state["report"]["explanation"] = "连接池从100降到20，容量砍半。"
        state["report_audit"] = {"version": "obsolete", "issues": []}
        original = copy.deepcopy(state)
        score = score_run(state, make_label())
        self.assertTrue(score["structural_task_success"])
        self.assertFalse(score["task_success"])
        self.assertEqual(score["failure_category"], "report_audit_failed")
        self.assertEqual(state, original)

    def test_each_alternative_evidence_group_requires_an_observed_citation(self):
        state = make_state()
        state["observed"]["alternative"] = {"id": "alternative"}
        label = make_label(required=[])
        label["evidence_groups"] = [["metric", "alternative"], ["log"]]
        state["report"]["evidence_ids"] = ["alternative", "log"]
        self.assertTrue(score_run(state, label)["task_success"])
        state["report"]["evidence_ids"] = ["alternative"]
        score = score_run(state, label)
        self.assertFalse(score["task_success"])
        self.assertEqual(score["missing_evidence_groups"], [["log"]])
        self.assertEqual(score["failure_category"], "missing_evidence_group")
        del state["observed"]["alternative"]
        self.assertEqual(score_run(state, label)["evidence_groups_recall"], 0)

    def test_malformed_evidence_groups_are_rejected(self):
        for groups in ("metric", [[]], [["metric", "metric"]], [[17]], [[""]]):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                _validate_label({**make_label(), "evidence_groups": groups})


if __name__ == "__main__":
    unittest.main()
