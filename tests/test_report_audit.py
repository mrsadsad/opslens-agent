"""Deterministic report checks; no labels, credentials or model requests."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from opslens import report_audit
from opslens.report_audit import AUDIT_SOURCE_SHA256, AUDIT_VERSION, audit_report


ROOT = Path(__file__).resolve().parents[1]
SAVED_LIVE_RUN = ROOT / "runs" / "66ac5a39a7854edaa0572d3b61c18db6" / "result.json"
V03_SINGLE_SHOT = ROOT / "eval" / "results" / "v03" / "attempts" / "attempt-00001" / "state.json"
V03_SINGLE_SHOT_TRACE = V03_SINGLE_SHOT.with_name("trace.jsonl")


def check(text, observed=None, evidence_ids=None):
    return audit_report({"explanation": text, "evidence_ids": evidence_ids or []}, observed or {})


class RelativeChangeTests(unittest.TestCase):
    def test_required_integration_case_detects_contradiction(self):
        result = check("连接池从100降到20，容量砍半。")
        self.assertTrue(result["requires_review"])
        self.assertEqual(result["issues"][0]["code"], "relative_change_conflict")
        self.assertEqual(result["issues"][0]["severity"], "error")

    def test_different_wrong_changes_are_not_a_100_to_20_special_case(self):
        for text in ("连接池从120降到30，容量减半。", "容量从80降至60，因此减半。",
                     "容量由1.2降到0.2，被砍半。", "Capacity halved from 120 to 30.",
                     "容量 1,000→200，减半。", "容量120->30被砍半。"):
            with self.subTest(text=text):
                self.assertTrue(check(text)["requires_review"])

    def test_correct_halving_and_relative_percentages_pass(self):
        for text in ("连接池从100降到50，容量砍半。", "Capacity halved from 120 to 60.",
                     "容量80→40，减半。", "错误率从80%降到40%，减半。",
                     "连接池从100降到20，容量减少80%。", "容量100→125，增加25%。",
                     "容量120→61，约减半。"):
            with self.subTest(text=text):
                self.assertFalse(check(text)["requires_review"])

    def test_wrong_percentage_is_checked_as_an_explicit_claim(self):
        self.assertTrue(check("容量100→20，减少70%。")["requires_review"])
        self.assertTrue(check("容量100→125，增加50%。")["requires_review"])

    def test_unit_conversion_does_not_create_a_false_halving_error(self):
        for text in ("延迟从1秒降到500毫秒，减半。", "Latency halved from 1000ms to 0.5s.",
                     "容量从1MB降到500KB，减半。", "容量从1千降到500，减半。",
                     "容量从1万降到5千，减半。"):
            with self.subTest(text=text):
                self.assertFalse(check(text)["requires_review"])

    def test_two_unrelated_metrics_are_not_paired(self):
        for text in ("缓存从100降到20，队列容量减半。", "缓存从100降到20而队列容量减半。",
                     "Cache changed from 100 to 20 and queue halved.",
                     "旧连接数100，新错误数20，因此容量减半。",
                     "基线容量100。现在错误数20。容量减半。",
                     "缓存从100降到20，数据库从80降到40，数据库减半。"):
            with self.subTest(text=text):
                self.assertFalse(check(text)["requires_review"])

    def test_ambiguous_multiple_transitions_are_not_assigned_to_one_claim(self):
        self.assertFalse(check("容量100→20 / 80→40，说明各项减半。")["requires_review"])

    def test_negative_and_warning_statements_are_not_assertions(self):
        for text in ("100→20不是减半，而是减少80%。", "从100降到20，不能说容量砍半。",
                     "It was not halved from 100 to 20.", "不要把100→20写成减半。"):
            with self.subTest(text=text):
                self.assertFalse(check(text)["requires_review"])

    def test_cross_sentence_shared_subject_is_only_a_review(self):
        report = {"root_cause": "DB_POOL_MAX 100→20 后连接池不足。",
                  "explanation": "证据显示池容量被砍半。", "evidence_ids": ["change"]}
        observed = {"change": {"content": "DB_POOL_MAX 100 -> 20."}}
        result = audit_report(report, observed)
        self.assertEqual(result["issues"][0]["severity"], "review")
        self.assertEqual(result["issues"][0]["evidence_ids"], ["change"])

    def test_numeric_limits_cannot_crash_decimal_arithmetic(self):
        for text in ("容量从1e999999999降到20，减半。", "容量从1e-999999999降到20，减半。",
                     "容量从" + "9" * 4000 + "降到20，减半。", "容量100→20，减少1e999999999%。"):
            with self.subTest(text=text[:60]):
                result = check(text)
                self.assertTrue(result["requires_review"])
                self.assertIn("numeric_range_unchecked", [issue["code"] for issue in result["issues"]])


class ScopeAndPercentileTests(unittest.TestCase):
    def test_error_count_rate_agreement_without_scope_needs_review(self):
        for text in ("一分钟172条错误与18%的错误率一致。", "一分钟内172个请求出现错误，与18%的5xx比例一致。",
                     "172 errors are consistent with an 18% error rate."):
            with self.subTest(text=text):
                issues = check(text)["issues"]
                self.assertIn("count_rate_scope_unverified", [item["code"] for item in issues])
                self.assertTrue(all(item["severity"] == "review" for item in issues))

    def test_explicit_window_population_and_denominator_remove_scope_flag(self):
        for text in ("同一分钟、同一路由，总请求数1000，100条错误与10%的错误率一致。",
                     "In the same window and same request population with total requests=1000, "
                     "100 errors are consistent with a 10% error rate."):
            with self.subTest(text=text):
                self.assertNotIn("count_rate_scope_unverified", [item["code"] for item in check(text)["issues"]])

    def test_merely_listing_count_and_rate_or_warning_is_not_agreement(self):
        for text in ("日志有172条错误，告警错误率18%，统计范围待核实。",
                     "不能将172条错误与18%的错误率认定为一致。",
                     "172 errors are not consistent with an 18% error rate.",
                     "不要把172条错误说成证明18%的错误率。"):
            with self.subTest(text=text):
                self.assertFalse(check(text)["requires_review"])

    def test_adding_percentiles_to_infer_total_needs_review(self):
        for text in ("接口p95 2.8s与等待p95 2.3s加上后续处理耗时相符。",
                     "p95_A + p95_B equals overall p95.", "两阶段的p95 100ms和p95 200ms相加得到端到端p95 300ms。"):
            with self.subTest(text=text):
                self.assertIn("p95_aggregation_unverified", [item["code"] for item in check(text)["issues"]])

    def test_valid_percentile_operations_and_explicit_warnings_pass(self):
        for text in ("各阶段p95不能相加推导端到端p95。", "p95_A + p95_B is not equal to overall p95.",
                     "先对同一trace各阶段耗时相加，再对这些总耗时计算端到端p95。",
                     "接口p95=100ms，数据库p95=50ms，二者仅供分别观察。",
                     "总p95等于等待p95加上固定100ms处理时间。"):
            with self.subTest(text=text):
                self.assertFalse(check(text)["requires_review"])

    def test_unrelated_addition_is_not_a_percentile_derivation(self):
        self.assertFalse(check("接口p95=100ms，数据库p95=50ms；错误数1+2=3。")["requires_review"])

    def test_v03_rejected_sentence_explicitly_denies_percentile_addition(self):
        text = ("同时获取等待 p95 达 2300ms 与端到端 p95 从 180ms 升至 2800ms 在量级上一致"
                "（不将组件 p95 相加推断端到端值）。")
        self.assertFalse(check(text)["requires_review"])

    def test_percentile_negation_variants_are_local_denials(self):
        for text in (
            "不将组件 p95 相加推断端到端 p95。",
            "未将组件 p95 相加推断端到端 p95。",
            "不能把各阶段 p95 简单相加得到整体 p95。",
            "不要将不同服务 p95 直接相加得到端到端 p95。",
            "各组件 p95 未相加推断端到端 p95。",
            "p95 数值亦未相加推断端到端 p95。",
            "组件 p95 之和不等于端到端 p95。",
            "p95_A + p95_B cannot be used to infer overall p95.",
            "Do not directly add component p95_A and p95_B to infer overall p95.",
        ):
            with self.subTest(text=text):
                self.assertFalse(check(text)["requires_review"])

    def test_negating_an_unrelated_claim_does_not_hide_percentile_sum(self):
        for text in (
            "不是数据库故障，p95_A + p95_B 等于端到端 p95。",
            "不能证明数据库故障，但 p95_A + p95_B 得到整体 p95。",
            "这不是数据库问题且 p95_A + p95_B 等于端到端 p95。",
            "This is not a database problem, p95_A + p95_B equals overall p95.",
            "p95_A + p95_B equals overall p95, and this is not a database failure.",
            "不要将 p95 误认为均值，但 p95_A + p95_B 等于整体 p95。",
            "不将 p95_A 与 p95_B 相乘而是相加得到整体 p95。",
        ):
            with self.subTest(text=text):
                self.assertIn("p95_aggregation_unverified", [i["code"] for i in check(text)["issues"]])

    def test_first_negated_addition_does_not_hide_a_later_affirmative_sum(self):
        for text in (
            "不能把 p95_A 和 p95_B 相加，但 p95_C + p95_D 等于端到端 p95。",
            "p95_A + p95_B 不等于整体 p95，不过 p95_C + p95_D 等于整体 p95。",
            "Do not add p95_A and p95_B, but p95_C + p95_D equals overall p95.",
            "p95_A + p95_B 等于整体 p95，但不能把 p95_C 和 p95_D 相加。",
            "p95_A + p95_B equals overall p95 and p95_C + p95_D is not equal to overall p95.",
            "p95_A+p95_B等于总延迟p95以及p95_C+p95_D不等于整体p95。",
            "不是不能相加，p95_A + p95_B 等于端到端 p95。",
            "各阶段 p95 不是不能相加得到端到端 p95。",
        ):
            with self.subTest(text=text):
                self.assertIn("p95_aggregation_unverified", [i["code"] for i in check(text)["issues"]])


class ReplayAndApiTests(unittest.TestCase):
    @unittest.skipUnless(V03_SINGLE_SHOT.is_file() and V03_SINGLE_SHOT_TRACE.is_file(),
                         "frozen v03 export is not available")
    def test_v03_original_rejected_proposal_replay_does_not_rewrite_result(self):
        raw = V03_SINGLE_SHOT.read_bytes()
        trace_raw = V03_SINGLE_SHOT_TRACE.read_bytes()
        state = json.loads(raw)
        events = [json.loads(line) for line in trace_raw.splitlines() if line.strip()]
        proposal = next(e["arguments"] for e in events
                        if e.get("tool") == "submit_diagnosis")
        self.assertEqual(state["status"], "report_rejected")
        self.assertIsNone(state["report"])
        self.assertFalse(audit_report(proposal, state["observed"])["requires_review"])
        self.assertEqual(V03_SINGLE_SHOT.read_bytes(), raw)
        self.assertEqual(V03_SINGLE_SHOT_TRACE.read_bytes(), trace_raw)

    def test_self_contained_regression_for_the_three_original_error_classes(self):
        report = {"root_cause": "连接池缩减（DB_POOL_MAX 100→20）后容量不足。",
                  "explanation": "部署将DB_POOL_MAX从100降到20。指标显示池容量被砍半而非流量上涨。"
                                 "一分钟内172个请求出现错误，与18%的5xx比例一致。"
                                 "p95 2.8s与等待p95 2.3s加上后续处理耗时相符。",
                  "evidence_ids": ["change", "metric", "log"]}
        observed = {"change": {"content": "DB_POOL_MAX 100 -> 20."},
                    "metric": {"content": "240 requests/s; acquire p95=2300ms"},
                    "log": {"content": "Same error observed in 172 requests in the last minute."}}
        before = deepcopy((report, observed))
        result = audit_report(report, observed)
        self.assertEqual({issue["code"] for issue in result["issues"]},
                         {"relative_change_conflict", "count_rate_scope_unverified", "p95_aggregation_unverified"})
        rate_issue = next(issue for issue in result["issues"] if issue["code"] == "count_rate_scope_unverified")
        self.assertIn("log", rate_issue["evidence_ids"])
        self.assertEqual((report, observed), before)

    @unittest.skipUnless(SAVED_LIVE_RUN.is_file(), "original ignored live artifact is not available")
    def test_saved_original_live_artifact_replay_without_mutation(self):
        raw = SAVED_LIVE_RUN.read_bytes()
        state = json.loads(raw)
        result = audit_report(state["report"], state["observed"])
        self.assertEqual({issue["code"] for issue in result["issues"]},
                         {"relative_change_conflict", "count_rate_scope_unverified", "p95_aggregation_unverified"})
        self.assertTrue(all(issue["severity"] == "review" for issue in result["issues"]))
        self.assertEqual(SAVED_LIVE_RUN.read_bytes(), raw)

    def test_minimal_and_malformed_observation_records_do_not_crash(self):
        report = {"explanation": "从100降到20，减半。", "evidence_ids": ["a", "b", "c", "missing", None]}
        result = audit_report(report, {"a": {}, "b": None, "c": {"id": "c", "content": 100}})
        self.assertTrue(result["requires_review"])
        self.assertEqual(result["issues"][0]["evidence_ids"], [])
        self.assertFalse(audit_report({}, None)["requires_review"])
        self.assertTrue(audit_report(None, {})["requires_review"])

    def test_version_and_source_hash_make_rules_traceable(self):
        result = audit_report({}, {})
        self.assertEqual(result["version"], AUDIT_VERSION)
        self.assertEqual(AUDIT_SOURCE_SHA256, hashlib.sha256(Path(report_audit.__file__).read_bytes()).hexdigest())
        self.assertEqual(set(result), {"version", "issues", "requires_review"})


if __name__ == "__main__":
    unittest.main()
