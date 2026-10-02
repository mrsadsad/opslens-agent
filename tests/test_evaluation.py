"""Scoring invariants: an attractive report must not hide missing evidence."""

import copy
import unittest

from opslens.evaluation import (
    evaluate_runs, paired_bootstrap_difference, percentile, score_run, wilson_interval,
)


def make_label(cause="dependency_timeout", required=None, split="dev"):
    return {"cause_code": cause, "required_evidence": ["metric", "log"]
            if required is None else required, "split": split}


def make_state(case_id="case-1", cause="dependency_timeout", citations=None):
    return {
        "case_id": case_id,
        "provider": "test-model",
        "status": "completed",
        "report": {
            "status": "insufficient_evidence" if cause == "insufficient_evidence" else "diagnosed",
            "cause_code": cause,
            "root_cause": "Downstream requests exceeded the configured timeout.",
            "evidence_ids": ["metric", "log"] if citations is None else citations,
            "explanation": "Both observations identify the same failing dependency.",
            "next_steps": ["Have an operator verify downstream health."],
        },
        "observed": {"metric": {"id": "metric"}, "log": {"id": "log"}},
        "steps": 3,
        "tool_calls": 4,
        "model_seconds": 0.5,
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        "usage_complete": True,
    }


class ScoreRunTests(unittest.TestCase):
    def test_successful_diagnosis_and_inputs_are_unchanged(self):
        state, label = make_state(), make_label()
        original = copy.deepcopy((state, label))
        score = score_run(state, label)
        self.assertTrue(score["completed"])
        self.assertTrue(score["cause_match"])
        self.assertTrue(score["task_success"])
        self.assertEqual(score["required_evidence_recall"], 1.0)
        self.assertEqual(score["citation_traceability"], 1.0)
        self.assertIsNone(score["abstention_correct"])
        self.assertEqual((state, label), original)

    def test_fabricated_citation_invalidates_otherwise_correct_diagnosis(self):
        state = make_state(citations=["metric", "log", "invented"])
        score = score_run(state, make_label())
        self.assertTrue(score["cause_match"])
        self.assertEqual(score["required_evidence_recall"], 1.0)
        self.assertAlmostEqual(score["citation_traceability"], 2 / 3)
        self.assertEqual(score["untraceable_evidence_ids"], ["invented"])
        self.assertFalse(score["report_valid"])
        self.assertFalse(score["task_success"])

    def test_guessing_required_id_without_observing_it_gets_no_recall_credit(self):
        state = make_state()
        del state["observed"]["log"]
        score = score_run(state, make_label())
        self.assertEqual(score["required_evidence_recall"], 0.5)
        self.assertEqual(score["missing_evidence_ids"], ["log"])
        self.assertFalse(score["task_success"])

    def test_missing_required_evidence_fails_despite_traceable_citations(self):
        score = score_run(make_state(citations=["metric"]), make_label())
        self.assertEqual(score["citation_traceability"], 1.0)
        self.assertEqual(score["required_evidence_recall"], 0.5)
        self.assertFalse(score["task_success"])

    def test_no_report_is_a_failure_even_if_status_claims_completion(self):
        state = make_state()
        state["report"] = None
        score = score_run(state, make_label())
        self.assertFalse(score["completed"])
        self.assertFalse(score["cause_match"])
        self.assertFalse(score["task_success"])
        self.assertEqual(score["required_evidence_recall"], 0.0)
        self.assertIsNone(score["citation_traceability"])

    def test_budget_exhausted_run_cannot_succeed_with_a_present_report(self):
        state = make_state()
        state["status"] = "budget_exhausted"
        score = score_run(state, make_label())
        self.assertTrue(score["cause_match"])
        self.assertFalse(score["completed"])
        self.assertFalse(score["task_success"])

    def test_empty_citations_do_not_get_perfect_traceability(self):
        score = score_run(make_state(citations=[]), make_label())
        self.assertIsNone(score["citation_traceability"])
        self.assertEqual(score["required_evidence_recall"], 0.0)
        self.assertFalse(score["task_success"])

    def test_correct_abstention_needs_required_evidence_for_task_success(self):
        state = make_state(cause="insufficient_evidence")
        label = make_label(cause="insufficient_evidence")
        score = score_run(state, label)
        self.assertTrue(score["abstention_correct"])
        self.assertTrue(score["task_success"])
        state["report"]["evidence_ids"] = []
        score = score_run(state, label)
        self.assertTrue(score["abstention_correct"])
        self.assertIsNone(score["citation_traceability"])
        self.assertEqual(score["required_evidence_recall"], 0.0)
        self.assertFalse(score["task_success"])

    def test_wrong_abstention_fails(self):
        score = score_run(make_state(cause="insufficient_evidence"), make_label())
        self.assertFalse(score["abstention_correct"])
        self.assertFalse(score["task_success"])

    def test_empty_required_evidence_is_explicit_and_not_a_division_by_zero(self):
        score = score_run(make_state(cause="insufficient_evidence", citations=[]),
                          make_label(cause="insufficient_evidence", required=[]))
        self.assertIsNone(score["required_evidence_recall"])
        self.assertIsNone(score["citation_traceability"])
        self.assertTrue(score["task_success"])

    def test_disagreeing_report_status_and_cause_cannot_succeed(self):
        state = make_state()
        state["report"]["status"] = "insufficient_evidence"
        score = score_run(state, make_label())
        self.assertTrue(score["cause_match"])
        self.assertFalse(score["report_valid"])
        self.assertFalse(score["task_success"])

    def test_required_root_service_prevents_category_only_success(self):
        state, label = make_state(), make_label()
        label["root_service"] = "payments"
        state["observed"]["metric"]["service"] = "checkout"
        state["observed"]["log"]["service"] = "payments"
        missing = score_run(state, label)
        self.assertFalse(missing["root_service_match"])
        self.assertFalse(missing["task_success"])
        state["report"]["root_service"] = "checkout"
        wrong = score_run(state, label)
        self.assertTrue(wrong["report_valid"])
        self.assertTrue(wrong["cause_match"])
        self.assertFalse(wrong["root_service_match"])
        self.assertEqual(wrong["failure_category"], "wrong_root_service")
        state["report"]["root_service"] = "payments"
        right = score_run(state, label)
        self.assertTrue(right["root_service_match"])
        self.assertTrue(right["task_success"])

    def test_absent_or_null_component_label_is_not_scored(self):
        state, label = make_state(), make_label()
        self.assertIsNone(score_run(state, label)["root_service_match"])
        label["root_service"] = None
        self.assertIsNone(score_run(state, label)["root_service_match"])
        self.assertTrue(score_run(state, label)["task_success"])

    def test_failure_taxonomy_distinguishes_missing_evidence_and_wrong_cause(self):
        score = score_run(make_state(citations=["metric"]), make_label())
        self.assertEqual(score["failure_category"], "missing_required_evidence")
        score = score_run(make_state(cause="bad_configuration"), make_label())
        self.assertEqual(score["failure_category"], "wrong_cause")


class EvaluateRunsTests(unittest.TestCase):
    def test_empty_runs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "nonempty"):
            evaluate_runs([], {})

    def test_duplicate_case_runs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            evaluate_runs([make_state(), make_state()], {"case-1": make_label()})

    def test_unknown_case_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            evaluate_runs([make_state()], {})

    def test_aggregate_includes_failures_and_splits(self):
        succeeded = make_state()
        failed = make_state("case-2")
        failed["status"] = "provider_error"
        failed["report"] = None
        failed["usage_complete"] = False
        result = evaluate_runs([succeeded, failed], {
            "case-1": make_label(), "case-2": make_label(split="challenge")})
        self.assertEqual(result["n"], 2)
        self.assertEqual(result["aggregate"]["completion_rate"], 0.5)
        self.assertEqual(result["aggregate"]["task_success_rate"], 0.5)
        self.assertEqual(result["aggregate"]["required_evidence_recall"], 0.5)
        self.assertEqual(result["aggregate"]["n_without_citations"], 1)
        self.assertIsNone(result["aggregate"]["abstention_correct_rate"])
        self.assertEqual(result["by_split"]["dev"]["task_success_rate"], 1.0)
        self.assertEqual(result["by_split"]["challenge"]["task_success_rate"], 0.0)
        self.assertEqual(result["usage"]["total_tokens"], 240)
        self.assertEqual(result["aggregate"]["total_model_seconds"], 1.0)
        self.assertEqual(result["aggregate"]["mean_tool_calls"], 4.0)
        self.assertFalse(result["usage_complete"])

    def test_scripted_demo_warning_survives_mixed_providers(self):
        demo = make_state("demo-case")
        demo["provider"] = "scripted-demo"
        result = evaluate_runs([make_state(), demo], {
            "case-1": make_label(), "demo-case": make_label()})
        self.assertTrue(result["scripted_demo"])
        self.assertEqual(result["provider"], "mixed")
        self.assertIn("DEMO ONLY", result["warnings"][0])
        self.assertIn("NOT a model-capability benchmark", result["warnings"][0])
        self.assertTrue(result["per_case"][1]["warnings"])

    def test_missing_usage_cannot_be_reported_as_complete(self):
        state = make_state()
        del state["usage"]["total_tokens"]
        result = evaluate_runs([state], {"case-1": make_label()})
        self.assertFalse(result["usage_complete"])
        self.assertEqual(result["usage"]["prompt_tokens"], 100)

    def test_abstention_rate_uses_abstentions_not_ordinary_diagnoses(self):
        states = [make_state(), make_state("case-2", cause="insufficient_evidence"),
                  make_state("case-3", cause="insufficient_evidence")]
        result = evaluate_runs(states, {"case-1": make_label(),
                                       "case-2": make_label(cause="insufficient_evidence"),
                                       "case-3": make_label()})
        self.assertEqual(result["aggregate"]["n_abstained"], 2)
        self.assertEqual(result["aggregate"]["abstention_correct_rate"], 0.5)
        self.assertAlmostEqual(result["aggregate"]["task_success_rate"], 2 / 3)

    def test_component_metric_denominator_excludes_unlabeled_cases(self):
        states = [make_state(), make_state("case-2")]
        labeled = make_label()
        labeled["root_service"] = "payments"
        result = evaluate_runs(states, {"case-1": labeled, "case-2": make_label()})
        self.assertEqual(result["aggregate"]["n_with_root_service_label"], 1)
        self.assertEqual(result["aggregate"]["root_service_match_rate"], 0.0)
        self.assertEqual(result["aggregate"]["task_success_rate"], 0.5)

    def test_censored_and_invalid_saved_states_do_not_get_wilson_intervals(self):
        for status in ("global_budget_exhausted", "interrupted", "experiment_invalid", "skipped_invalid"):
            with self.subTest(status=status):
                state = make_state()
                state["status"], state["report"] = status, None
                result = evaluate_runs([state], {"case-1": make_label()})
                self.assertEqual(result["per_case"][0]["failure_category"], status)
                self.assertIsNone(result["aggregate"]["task_success_wilson_95"])


class StatisticsTests(unittest.TestCase):
    def test_wilson_does_not_claim_certainty_at_extreme_small_samples(self):
        lower, upper = wilson_interval(0, 1)
        self.assertAlmostEqual(lower, 0.0)
        self.assertGreater(upper, 0.79)
        lower, upper = wilson_interval(1, 1)
        self.assertLess(lower, 0.21)
        self.assertAlmostEqual(upper, 1.0)
        self.assertIsNone(wilson_interval(0, 0))
        with self.assertRaises(ValueError):
            wilson_interval(2, 1)

    def test_percentiles_include_failed_trial_latencies_without_smoothing(self):
        self.assertEqual(percentile([10.0, 0.0], 0.5), 5.0)
        self.assertEqual(percentile([10.0, 0.0], 0.95), 9.5)
        self.assertEqual(percentile([4.0], 0.95), 4.0)
        self.assertIsNone(percentile([], 0.95))

    def test_paired_bootstrap_is_reproducible_and_collapses_repeated_family(self):
        pairs = [{"group_id": "family-a", "a": True, "b": False}] * 20
        pairs += [{"group_id": "family-b", "a": False, "b": True}]
        first = paired_bootstrap_difference(pairs, seed=17, samples=1000)
        self.assertEqual(first, paired_bootstrap_difference(pairs, seed=17, samples=1000))
        self.assertEqual(first["n_pairs"], 21)
        self.assertEqual(first["n_groups"], 2)
        self.assertEqual(first["difference"], 0.0)
        self.assertEqual(first["ci_95"], [-1.0, 1.0])

    def test_repeats_do_not_create_an_interval_from_one_independent_group(self):
        result = paired_bootstrap_difference([{"group_id": "one-family", "a": 1, "b": 0}] * 30)
        self.assertEqual(result["difference"], 1.0)
        self.assertEqual(result["n_groups"], 1)
        self.assertIsNone(result["ci_95"])


if __name__ == "__main__":
    unittest.main()
