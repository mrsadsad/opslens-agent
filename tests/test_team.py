import json
from pathlib import Path
import tempfile
import threading
import unittest

from opslens.providers import DemoProvider, ProviderError
from opslens.store import RunStore, CheckpointConflict
from opslens.team import TeamRuntime, TeamDemoProvider, ScopedTools, ROLES
from opslens.tools import IncidentTools


CASE = Path(__file__).resolve().parents[1] / "opslens/fixtures/inc001.json"


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RunStore(Path(self.temp.name) / "runs.sqlite3")
        self.tools = IncidentTools(CASE)
        self.calls = []

    def runtime(self, barrier=None, fail=None, fake_citation=False):
        calls = self.calls

        class Provider:
            name = "test-team-provider"

            def complete(self, messages, schemas):
                prompt = messages[0]["content"]
                role = next((r for r in ROLES if f"You are the {r} specialist" in prompt), "coordinator")
                index = sum(m.get("role") == "assistant" for m in messages)
                calls.append((role, index))
                if barrier and role != "coordinator" and index == 0:
                    barrier.wait(timeout=5)
                if role == fail:
                    raise ProviderError("test_failure")
                response = TeamDemoProvider(role).complete(messages, schemas)
                if fake_citation and role == "coordinator" and index >= 1:
                    function = response["message"]["tool_calls"][0]["function"]
                    args = json.loads(function["arguments"])
                    args["evidence_ids"] = ["fabricated"]
                    function["arguments"] = json.dumps(args)
                response.update(usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                                usage_complete=True, transport_attempts=1)
                return response

        return TeamRuntime(self.tools, Provider, self.store)

    def test_parallel_scoped_agents_then_verified_coordinator(self):
        runtime = self.runtime(barrier=threading.Barrier(3))
        state = runtime.run(runtime.start())
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["steps"], 11)
        self.assertEqual(state["usage"]["total_tokens"], 33)
        self.assertTrue(state["usage_complete"])
        self.assertEqual(len({m["run_id"] for m in state["_members"].values()}), 4)
        self.assertEqual([role for role, _ in self.calls[-2:]], ["coordinator"] * 2)
        coordinator = state["_members"]["coordinator"]
        self.assertTrue(set(state["report"]["evidence_ids"]) <= coordinator["observed"].keys())
        self.assertIn("get_evidence", [e.get("tool") for e in coordinator["events"]])
        for role in ROLES:
            kinds = {r["kind"] for r in state["_members"][role]["observed"].values()}
            self.assertTrue(kinds <= ROLES[role][2])
        rows, _ = self.store.list_runs({self.tools.case_id: self.tools.fingerprint})
        self.assertEqual(len(rows), 1)  # no orphan/duplicate child jobs in history
        self.assertEqual([e["seq"] for e in state["events"]], list(range(1, len(state["events"]) + 1)))

    def test_resume_reuses_specialists_and_preserves_budgets(self):
        runtime = self.runtime()
        state = runtime.run(runtime.start(), stop_after_steps=3)
        self.assertEqual(state["phase"], "coordinator")
        self.assertEqual(state["steps"], 9)
        original_ids = {r: m["run_id"] for r, m in state["_members"].items()}
        self.calls.clear()
        state = self.runtime().run(self.store.load(state["run_id"]))
        self.assertEqual(self.calls, [("coordinator", 0), ("coordinator", 1)])
        self.assertEqual(state["steps"], 11)
        self.assertEqual(state["usage"]["total_tokens"], 33)
        self.assertEqual({r: state["_members"][r]["run_id"] for r in ROLES}, original_ids)

    def test_worker_failure_is_handed_off_and_does_not_block_coordinator(self):
        runtime = self.runtime(fail="logs")
        state = runtime.run(runtime.start())
        self.assertEqual(state["status"], "completed")
        self.assertTrue(state["partial"])
        self.assertFalse(state["usage_complete"])
        handoff = json.loads(state["_members"]["coordinator"]["messages"][1]["content"])
        self.assertEqual(next(f for f in handoff["peer_findings"] if f["agent"] == "logs")["status"], "provider_error")
        self.assertEqual(self.calls.count(("logs", 0)), 1)

    def test_fabricated_coordinator_citation_never_becomes_final_report(self):
        runtime = self.runtime(fake_citation=True)
        state = runtime.run(runtime.start())
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertIsNone(state["report"])
        self.assertEqual(state["steps"], 12)
        self.assertLessEqual(state["tool_calls"], state["max_tool_calls"])
        self.assertTrue(any(e.get("result", {}).get("error", {}).get("code") == "invalid_report" for e in state["events"]))

    def test_scope_is_enforced_in_execution_not_only_prompt(self):
        tools = ScopedTools(self.tools, "metrics")
        self.assertFalse(tools.execute("search_logs", {"service": "checkout"})["ok"])
        self.assertFalse(tools.execute("get_evidence", {"ids": ["inc001-l1"]})["ok"])
        self.assertTrue(tools.execute("get_evidence", {"ids": ["inc001-m1"]})["ok"])

    def test_definition_change_and_stale_resume_are_rejected(self):
        runtime = self.runtime()
        state = runtime.start()
        stale = self.store.load(state["run_id"])
        runtime.run(state, stop_after_steps=1)
        with self.assertRaises(CheckpointConflict):
            self.runtime().run(stale)
        changed = self.store.load(state["run_id"])
        changed["definition_hash"] = "old"
        with self.assertRaises(ValueError):
            self.runtime().run(changed)

    def test_interrupted_request_is_not_refunded(self):
        runtime = self.runtime()
        state = runtime.run(runtime.start(), stop_after_steps=1)
        member = state["_members"]["metrics"]
        member["steps"] += 1
        member["pending_model_request"] = True
        self.store.save(state)
        state = self.runtime().run(self.store.load(state["run_id"]))
        self.assertLessEqual(state["steps"], state["max_steps"])
        self.assertFalse(state["usage_complete"])
        self.assertTrue(state["partial"])
        self.assertTrue(any(e.get("agent_kind") == "interrupted_request" for e in state["events"]))

    def test_insufficient_team_budgets_are_rejected_before_creating_run(self):
        for limits in ({"max_steps": 11}, {"max_tool_calls": 15}, {"max_total_tokens": 3}):
            with self.assertRaises(ValueError):
                self.runtime().start(**limits)
        rows, _ = self.store.list_runs({self.tools.case_id: self.tools.fingerprint})
        self.assertEqual(rows, [])

    def test_reported_team_token_limit_stops_next_round(self):
        runtime = self.runtime()
        state = runtime.run(runtime.start(max_total_tokens=9))
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["budget_reason"], "reported_token_limit")
        self.assertEqual(state["steps"], 3)
        self.assertEqual(state["agents"]["coordinator"]["status"], "pending")

    def test_scripted_demo_and_export_hide_member_transcripts(self):
        from opslens.__main__ import _export
        runtime = TeamRuntime(self.tools, DemoProvider, self.store)
        state = runtime.run(runtime.start())
        self.assertEqual(state["status"], "completed")
        _export(state, Path(self.temp.name))
        public = json.loads((Path(self.temp.name) / state["run_id"] / "result.json").read_text(encoding="utf-8"))
        self.assertNotIn("_members", public)
        self.assertNotIn("messages", public)
        self.assertEqual(set(public["agents"]), {*ROLES, "coordinator"})

    def test_conflicting_diagnoses_are_explicit_in_coordinator_input(self):
        runtime = self.runtime()
        state = runtime.run(runtime.start(), stop_after_steps=3)
        for role, cause in (("metrics", "connection_pool_exhaustion"), ("logs", "dependency_timeout")):
            state["_members"][role]["report"].update(
                status="diagnosed", cause_code=cause, root_service="checkout")
        self.store.save(state)
        state = self.runtime().run(self.store.load(state["run_id"]))
        handoff = json.loads(state["_members"]["coordinator"]["messages"][1]["content"])
        self.assertTrue(handoff["conflicting_diagnoses"])
        self.assertEqual(len(handoff["peer_findings"]), 3)
        self.assertIn("No majority vote", state["_members"]["coordinator"]["messages"][0]["content"])

    def test_resume_repairs_interrupted_coordinator_initialization(self):
        runtime = self.runtime()
        state = runtime.run(runtime.start(), stop_after_steps=3)
        coordinator = runtime._runtime(state, "coordinator")
        coordinator.start(3, 12, 12000, 48000)  # crash before copying peer handoff
        self.assertEqual(state["_members"]["coordinator"]["messages"][1]["content"], self.tools.alert)
        self.store.save(state)
        state = self.runtime().run(self.store.load(state["run_id"]))
        self.assertEqual(state["status"], "completed")
        handoff = json.loads(state["_members"]["coordinator"]["messages"][1]["content"])
        self.assertEqual(len(handoff["peer_findings"]), 3)


if __name__ == "__main__":
    unittest.main()
