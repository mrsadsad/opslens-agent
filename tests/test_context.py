"""Compaction must preserve the evidence the next model turn actually needs."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from opslens.context import build_context, size
from opslens.runtime import AgentRuntime
from opslens.store import RunStore
from tests.test_runtime import FixtureTools, SequenceProvider, call, report


def compact_state(records):
    return {
        "messages": [{"role": "system", "content": "instructions"},
                     {"role": "user", "content": "alert"},
                     {"role": "assistant", "content": "old history " * 5000}],
        "observed": {record["id"]: record for record in records}, "hypotheses": {}, "events": [],
        "max_steps": 10, "steps": 3, "max_tool_calls": 20, "tool_calls": 4,
    }


def returned(tool, records, arguments=None):
    return {"kind": "tool_result", "tool": tool, "arguments": arguments or {},
            "result": {"ok": True, "records": records}}


class ContextTests(unittest.TestCase):
    def test_prioritizes_latest_return_then_hypotheses_and_service_capabilities(self):
        records = [{"id": f"m{i}", "kind": "metric", "content": "data " * 260} for i in range(16)]
        service = {"id": "service", "kind": "service", "service": "checkout",
                   "content": "service metadata", "capabilities": {"available_kinds": ["metric"]}}
        state = compact_state([*records, service])
        state["hypotheses"] = {"pool": {"supporting_evidence_ids": ["m1"], "contradicting_evidence_ids": ["m2"]}}
        state["events"] = [{"kind": "model_request"}, returned("get_evidence", [records[0]], {"ids": ["m0"]})]
        original = copy.deepcopy(state)
        context, metadata = build_context(state, [], 8000)
        ledger = json.loads(context[-1]["content"])
        visible = {record["id"]: record for record in ledger["records"]}
        self.assertEqual([record["id"] for record in ledger["records"]][:4], ["m0", "m1", "m2", "service"])
        self.assertEqual(visible["service"], service)
        self.assertGreater(metadata["omitted_records"], 0)
        self.assertLessEqual(size(context), 8000)
        self.assertEqual(state, original)

    def test_coverage_counts_disclose_that_even_the_omitted_index_can_be_partial(self):
        records = [{"id": f"m{i}-" + "x" * 90, "kind": "metric", "content": "unseen-tail " * 1000}
                   for i in range(80)]
        state = compact_state(records)
        context, metadata = build_context(state, [], 2600)
        ledger = json.loads(context[-1]["content"])
        coverage = ledger["evidence_coverage"]
        self.assertEqual(coverage["observed_records"], 80)
        self.assertEqual(coverage["visible_records"], len(ledger["records"]))
        self.assertEqual(coverage["omitted_records"], 80 - len(ledger["records"]))
        self.assertEqual(coverage["indexed_omitted_records"], len(ledger["omitted_record_ids"]))
        self.assertGreater(coverage["unindexed_omitted_records"], 0)
        self.assertEqual(coverage["omitted_records"], coverage["indexed_omitted_records"] + coverage["unindexed_omitted_records"])
        self.assertEqual(metadata["unindexed_omitted_records"], coverage["unindexed_omitted_records"])
        self.assertNotIn("unseen-tail", json.dumps(context))
        self.assertLessEqual(size(context), 2600)

    def test_keeps_query_arguments_and_empty_result_distinct_from_health(self):
        state = compact_state([])
        state["events"] = [returned("search_logs", [], {"service": "checkout", "query": "timeout"}),
                           {"kind": "tool_result", "tool": "query_metrics", "arguments": {"service": "checkout", "phase": "fault"},
                            "result": {"ok": False, "error": {"code": "unsupported_phase", "message": "Omit phase."}}}]
        context, _ = build_context(state, [], 3000)
        ledger = json.loads(context[-1]["content"])
        self.assertEqual(ledger["recent_queries"][0]["arguments"], {"service": "checkout", "query": "timeout"})
        self.assertEqual(ledger["recent_queries"][0]["returned_records"], 0)
        self.assertFalse(ledger["recent_queries"][0]["truncated"])
        self.assertEqual(ledger["recent_errors"][0]["arguments"]["phase"], "fault")
        self.assertIn("Empty queries do not establish health", ledger["notice"])

    def test_event_ids_never_reveal_evidence_outside_the_observed_ledger(self):
        state = compact_state([{"id": "visible", "content": "known"}])
        state["events"] = [returned("get_evidence", [{"id": "unobserved", "content": "private-unreturned"}])]
        context, _ = build_context(state, [], 3000)
        self.assertNotIn("unobserved", json.dumps(context))
        self.assertNotIn("private-unreturned", json.dumps(context))

    def test_page_cursor_and_query_survive_compaction_without_omitted_contents(self):
        record = {"id": "m20", "kind": "metric", "content": "only returned record"}
        state = compact_state([record])
        event = returned("query_metrics", [record], {"service": "checkout", "phase": "fault", "offset": 20})
        page = {"offset": 20, "total_matches": 80, "returned_count": 1, "next_offset": 21}
        event["result"].update(page={**page, "internal_record": "must-not-be-visible"}, truncated=True)
        state["events"] = [event]
        context, _ = build_context(state, [], 3000)
        ledger = json.loads(context[-1]["content"])
        query = ledger["recent_queries"][0]
        self.assertEqual(query["arguments"], event["arguments"])
        self.assertEqual(query["page"], page)
        self.assertEqual(query["returned_records"], 1)
        self.assertTrue(query["truncated"])
        self.assertEqual(ledger["evidence_coverage"]["observed_records"], 1)
        self.assertNotIn("must-not-be-visible", json.dumps(context))

    def test_refetched_old_record_reaches_next_model_after_checkpoint_resume(self):
        records = [{"id": f"m{i}", "kind": "metric", "content": f"Exact observation {i}: " + "a" * 1350}
                   for i in range(18)]

        class ManyTools(FixtureTools):
            def execute(self, name, arguments):
                if name == "get_evidence":
                    return {"ok": True, "records": [records[int(item[1:])] for item in arguments["ids"]]}
                start = arguments["start"]
                return {"ok": True, "records": records[start:start + 6]}

        class CapturingProvider(SequenceProvider):
            def __init__(self, responses):
                super().__init__(responses)
                self.contexts = []

            def complete(self, messages, tools):
                self.contexts.append(copy.deepcopy(messages))
                return super().complete(messages, tools)

        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / "state.db")
            provider = CapturingProvider([call("query_metrics", {"start": index}) for index in (0, 6, 12)] +
                                         [call("get_evidence", {"ids": ["m0"]})])
            runtime = AgentRuntime(ManyTools(), provider, store)
            state = runtime.run(runtime.start(max_context_chars=12000), stop_after_steps=4)
            before = json.loads(provider.contexts[3][-1]["content"])
            self.assertNotIn("m0", [item["id"] for item in before["records"]])
            self.assertIn("m0", before["omitted_record_ids"])
            resumed_provider = CapturingProvider([call("submit_diagnosis", report(["m0"]))])
            resumed = AgentRuntime(ManyTools(), resumed_provider, store).run(store.load(state["run_id"]))
            after = json.loads(resumed_provider.contexts[0][-1]["content"])
            self.assertEqual(after["records"][0], records[0])
            self.assertNotIn("m0", after["omitted_record_ids"])
            self.assertEqual((resumed["status"], resumed["steps"], resumed["usage"]["total_tokens"]), ("completed", 5, 75))
            self.assertEqual([message["role"] for message in resumed_provider.contexts[0]], ["system", "user", "user"])


if __name__ == "__main__":
    unittest.main()
