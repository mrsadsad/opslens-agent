"""Offline label-based scoring; labels never enter the agent's investigation.

Cause matching is literal category matching, not a judgment of the explanation.
Citation traceability only checks that cited IDs were observed. It does not prove
that the cited text supports a claim. Empty citation sets have undefined (None)
traceability, and abstention still needs all evidence required by its label.
"""

import math
import random
from collections import Counter

from .runtime import validate_report
from .report_audit import audit_report


TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
SPLITS = ("train", "dev", "test", "challenge")
DEMO_WARNING = (
    "DEMO ONLY: scripted-demo runs exercise the software and scoring pipeline; "
    "these scores are NOT a model-capability benchmark."
)
METRIC_NOTES = {
    "cause_match": "Exact cause_code equality; explanation semantics are not scored.",
    "root_service_match": "Exact component equality when the label specifies root_service; None otherwise.",
    "required_evidence_recall": (
        "Fraction of label-required IDs both cited and observed; None if the label "
        "requires no IDs. Aggregate is a mean over cases with required IDs."
    ),
    "citation_traceability": (
        "Fraction of distinct cited IDs present in observed; None for no citations. "
        "Aggregate is weighted by citation count. This is NOT semantic support accuracy."
    ),
    "abstention_correct": (
        "For abstaining reports only: the label and reported cause both equal "
        "insufficient_evidence. None otherwise. Evidence coverage is checked separately "
        "by task_success, including on insufficient_evidence cases."
    ),
    "task_success": (
        "Completed valid report, exact cause and (when labeled) root_service match, all required evidence cited and "
        "observed, and no unobserved citations. A diagnosis requires citations. "
        "Every evidence_groups group must have an observed citation, and no known numeric audit issue may remain. "
        "An empty-citation abstention can pass only if its label requires no evidence. This is not full semantic correctness."
    ),
    "numeric_audit": "Conservative rules for known numeric risks; zero findings does not certify all narrative claims.",
    "numeric_rejection_count": "Rejected diagnosis or hypothesis submissions with numeric findings; includes repaired and ultimately failed runs.",
    "evidence_groups_recall": "Fraction of evaluator-defined alternative-evidence groups with at least one observed citation; all groups are required.",
    "usage": "Reported token totals; partial totals are marked usage_complete=false.",
}


def _validate_label(label: dict) -> set[str]:
    if not isinstance(label, dict):
        raise ValueError("label must be a dictionary")
    if not isinstance(label.get("cause_code"), str) or not label["cause_code"].strip():
        raise ValueError("label requires a nonempty cause_code")
    if label.get("split") not in SPLITS:
        raise ValueError("label split must be train, dev, test or challenge")
    required = label.get("required_evidence")
    if not isinstance(required, list) or any(
        not isinstance(item, str) or not item.strip() for item in required
    ):
        raise ValueError("required_evidence must be a list of nonempty record IDs")
    if len(required) != len(set(required)):
        raise ValueError("required_evidence must not contain duplicate IDs")
    root_service = label.get("root_service")
    if root_service is not None and (not isinstance(root_service, str) or not root_service.strip()):
        raise ValueError("label root_service must be a nonempty service name or null")
    groups = label.get("evidence_groups", [])
    if not isinstance(groups, list) or len(groups) > 30:
        raise ValueError("evidence_groups must be a list of up to 30 alternative-ID lists")
    for group in groups:
        if (not isinstance(group, list) or not group or len(group) > 30
                or any(not isinstance(item, str) or not item.strip() for item in group)
                or len(group) != len(set(group))):
            raise ValueError("each evidence group must contain distinct nonempty IDs")
    return set(required)


def _usage(state: dict) -> tuple[dict, bool]:
    source = state.get("usage")
    source = source if isinstance(source, dict) else {}
    complete = state.get("usage_complete") is True
    result = {}
    for key in TOKEN_FIELDS:
        value = source.get(key)
        if type(value) is int and value >= 0:
            result[key] = value
        else:
            result[key] = 0
            complete = False
    return result, complete


def _nonnegative_number(state: dict, key: str) -> int | float:
    value = state.get(key, 0)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{key} must be a finite nonnegative number")
    return value


def score_run(state: dict, label: dict) -> dict:
    """Score one recorded run against an evaluator-only label.

    A missing or invalid report cannot succeed. Key evidence must actually have
    been observed, so guessing label IDs does not earn evidence-recall credit.
    """
    if not isinstance(state, dict):
        raise ValueError("state must be a dictionary")
    case_id = state.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("state requires a nonempty case_id")
    required = _validate_label(label)
    observed = state.get("observed", {})
    if not isinstance(observed, dict):
        raise ValueError("observed must be a dictionary")
    report = state.get("report")
    has_report = isinstance(report, dict)
    report_error = validate_report(report, observed) if has_report else "no report"
    report = report if has_report else {}
    raw_citations = report.get("evidence_ids", [])
    # Runtime rejects malformed reports. Preserve that rejection when scoring
    # imported snapshots, while still exposing metrics for well-formed IDs.
    citations = {
        item for item in raw_citations if isinstance(item, str)
    } if isinstance(raw_citations, list) else set()
    traceable = citations.intersection(observed)
    covered = required.intersection(traceable)
    missing = required - covered
    untraceable = citations - traceable
    completed = state.get("status") == "completed" and has_report
    cause_match = has_report and report.get("cause_code") == label["cause_code"]
    root_service_match = (has_report and report.get("root_service") == label["root_service"]
                          if label.get("root_service") is not None else None)
    abstained = has_report and report.get("status") == "insufficient_evidence"
    abstention_correct = (
        label["cause_code"] == "insufficient_evidence" and cause_match
    ) if abstained else None
    usage, usage_complete = _usage(state)
    provider = state.get("provider", "unknown")
    if not isinstance(provider, str) or not provider:
        provider = "unknown"
    scripted_demo = state.get("scripted_demo") is True or "scripted-demo" in provider.lower()
    groups = label.get("evidence_groups", [])
    missing_groups = [group for group in groups if not traceable.intersection(group)]
    audit = audit_report(report, observed) if has_report else None
    structural_success = (completed and report_error is None and cause_match and root_service_match is not False
                          and not missing and not untraceable and not missing_groups)
    query_events = [event for event in state.get("events", [])
                    if event.get("kind") == "tool_result"
                    and event.get("tool") in ("list_services", "query_metrics", "search_logs", "get_changes",
                                               "search_runbooks", "get_evidence", "get_trace")]
    numeric_rejections = sum(
        event.get("kind") == "tool_result" and
        (event.get("result", {}).get("error", {}).get("code") == "report_audit_failed" or
         (event.get("result", {}).get("error", {}).get("code") == "invalid_hypothesis"
          and bool(event.get("result", {}).get("error", {}).get("issues"))))
        for event in state.get("events", []))
    score = {
        "case_id": case_id,
        "split": label["split"],
        "provider": provider,
        "scripted_demo": scripted_demo,
        "warnings": [DEMO_WARNING] if scripted_demo else [],
        "run_status": state.get("status"),
        "completed": completed,
        "report_valid": report_error is None,
        "report_error": report_error,
        "cause_match": cause_match,
        "root_service_match": root_service_match,
        "required_evidence_recall": len(covered) / len(required) if required else None,
        "required_evidence_count": len(required),
        "covered_evidence_count": len(covered),
        "missing_evidence_ids": sorted(missing),
        "citation_traceability": len(traceable) / len(citations) if citations else None,
        "citation_count": len(citations),
        "traceable_citation_count": len(traceable),
        "untraceable_evidence_ids": sorted(untraceable),
        "abstained": abstained,
        "abstention_correct": abstention_correct,
        "evidence_group_count": len(groups),
        "missing_evidence_groups": missing_groups,
        "evidence_groups_recall": (len(groups) - len(missing_groups)) / len(groups) if groups else None,
        "structural_task_success": bool(structural_success),
        "numeric_audit": audit,
        "numeric_audit_passed": not audit["issues"] if audit is not None else None,
        "numeric_rejection_count": numeric_rejections,
        "task_success": bool(structural_success and audit is not None and not audit["issues"]),
        "retrieval_calls": len(query_events),
        "empty_retrieval_calls": sum(event.get("result", {}).get("ok") is True
                                     and event.get("result", {}).get("records") == [] for event in query_events),
        "unsupported_phase_calls": sum(event.get("result", {}).get("error", {}).get("code") == "unsupported_phase"
                                       for event in query_events),
        "numeric_repair_requests": sum(event.get("result", {}).get("error", {}).get("code") == "report_audit_failed"
                                       for event in state.get("events", [])),
        "steps": _nonnegative_number(state, "steps"),
        "tool_calls": _nonnegative_number(state, "tool_calls"),
        "model_seconds": _nonnegative_number(state, "model_seconds"),
        "usage": usage,
        "usage_complete": usage_complete,
    }
    score["failure_category"] = classify_failure(state, score)
    return score


def classify_failure(state: dict, score: dict) -> str | None:
    """One exclusive, operational category per attempt; never an LLM judgment."""
    if score["task_success"]:
        return None
    status = state.get("status")
    if status in ("global_budget_exhausted", "interrupted", "setup_error", "experiment_invalid", "skipped_invalid"):
        return status
    if status in ("budget_exhausted", "token_budget_exhausted"):
        return "run_budget_exhausted"
    if status == "provider_error":
        return "provider_error"
    if status in ("protocol_error", "tool_error", "execution_error"):
        return status
    if not isinstance(state.get("report"), dict):
        # Rejected report proposals are still failures even though the runtime
        # correctly prevents them from becoming accepted reports.
        for event in reversed(state.get("events", [])):
            code = event.get("result", {}).get("error", {}).get("code")
            if code in ("invalid_report", "report_audit_failed"):
                return code
        return "no_report"
    if score["untraceable_evidence_ids"]:
        return "untraceable_citation"
    if not score["report_valid"]:
        return "invalid_report"
    if not score["cause_match"]:
        return "incorrect_abstention" if score["abstained"] else "wrong_cause"
    if score["root_service_match"] is False:
        return "wrong_root_service"
    if score["missing_evidence_ids"]:
        return "missing_required_evidence"
    if score.get("missing_evidence_groups"):
        return "missing_evidence_group"
    if score.get("numeric_audit_passed") is False:
        return "report_audit_failed"
    return "incomplete_run"


def _aggregate(scores: list[dict]) -> dict:
    n = len(scores)
    n_completed = sum(score["completed"] for score in scores)
    n_cause_matched = sum(score["cause_match"] for score in scores)
    n_succeeded = sum(score["task_success"] for score in scores)
    interval_blocked = any(score["failure_category"] in (
        "global_budget_exhausted", "interrupted", "experiment_invalid", "skipped_invalid"
    ) for score in scores)
    abstentions = [score for score in scores if score["abstained"]]
    n_correct_abstentions = sum(score["abstention_correct"] for score in abstentions)
    recalls = [score["required_evidence_recall"] for score in scores
               if score["required_evidence_recall"] is not None]
    component_matches = [score["root_service_match"] for score in scores
                         if score["root_service_match"] is not None]
    citations = sum(score["citation_count"] for score in scores)
    traced = sum(score["traceable_citation_count"] for score in scores)
    result = {
        "n": n,
        "n_completed": n_completed,
        "completion_rate": n_completed / n,
        "n_cause_matched": n_cause_matched,
        "cause_match_rate": n_cause_matched / n,
        "n_with_root_service_label": len(component_matches),
        "root_service_match_rate": sum(component_matches) / len(component_matches) if component_matches else None,
        "required_evidence_recall": sum(recalls) / len(recalls) if recalls else None,
        "n_with_required_evidence": len(recalls),
        "citation_traceability": traced / citations if citations else None,
        "citation_count": citations,
        "traceable_citation_count": traced,
        "n_without_citations": sum(score["citation_count"] == 0 for score in scores),
        "n_abstained": len(abstentions),
        "n_correct_abstentions": n_correct_abstentions,
        "abstention_correct_rate": (
            n_correct_abstentions / len(abstentions) if abstentions else None
        ),
        "n_task_success": n_succeeded,
        "n_structural_success": sum(score["structural_task_success"] for score in scores),
        "n_numeric_audit_flagged": sum(score["numeric_audit_passed"] is False or score["numeric_rejection_count"] > 0 for score in scores),
        "numeric_rejection_count": sum(score["numeric_rejection_count"] for score in scores),
        "task_success_rate": n_succeeded / n,
        "task_success_wilson_95": None if interval_blocked else wilson_interval(n_succeeded, n),
        "wilson_note": "Suppressed: censored or invalid experiment outcomes." if interval_blocked else
                       "Assumes independent Bernoulli trials; related cases may violate this assumption.",
        "failure_taxonomy": dict(sorted(Counter(
            score["failure_category"] for score in scores if score["failure_category"]
        ).items())),
    }
    for key in ("steps", "tool_calls", "model_seconds"):
        total = sum(score[key] for score in scores)
        result[f"total_{key}"] = total
        result[f"mean_{key}"] = total / n
    return result


def evaluate_runs(states: list[dict], labels: dict) -> dict:
    """Score a nonempty, explicitly selected set of unique known cases.

    Refuse duplicate cases instead of implicitly picking the best attempt. To
    compare providers or repeat trials, evaluate each complete trial separately.
    """
    if not isinstance(states, list) or not states:
        raise ValueError("states must be a nonempty list")
    if not isinstance(labels, dict):
        raise ValueError("labels must map case IDs to labels")
    seen = set()
    scores = []
    for state in states:
        if not isinstance(state, dict):
            raise ValueError("each state must be a dictionary")
        case_id = state.get("case_id")
        if not isinstance(case_id, str) or case_id not in labels:
            raise ValueError(f"unknown case_id: {case_id!r}")
        if case_id in seen:
            raise ValueError(f"duplicate case_id: {case_id!r}")
        seen.add(case_id)
        scores.append(score_run(state, labels[case_id]))
    providers = sorted({score["provider"] for score in scores})
    scripted_demo = any(score["scripted_demo"] for score in scores)
    warnings = [DEMO_WARNING] if scripted_demo else []
    if len(providers) > 1:
        warnings.append("MIXED PROVIDERS: aggregate scores do not describe one model or policy.")
    return {
        "n": len(scores),
        "per_case": scores,
        "aggregate": _aggregate(scores),
        "by_split": {
            split: _aggregate([score for score in scores if score["split"] == split])
            for split in SPLITS
            if any(score["split"] == split for score in scores)
        },
        "provider": providers[0] if len(providers) == 1 else "mixed",
        "providers": providers,
        "usage": {key: sum(score["usage"][key] for score in scores) for key in TOKEN_FIELDS},
        "usage_complete": all(score["usage_complete"] for score in scores),
        "scripted_demo": scripted_demo,
        "warnings": warnings,
        "metric_notes": dict(METRIC_NOTES),
    }


def percentile(values: list[float], quantile: float) -> float | None:
    """Linearly interpolated sample percentile (R-7 / common default method)."""
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def wilson_interval(successes: int, n: int) -> list[float] | None:
    """95% Wilson score interval; independent Bernoulli trials are assumed."""
    if type(n) is not int or type(successes) is not int or not 0 <= successes <= n:
        raise ValueError("Wilson interval requires 0 <= integer successes <= integer n")
    if n == 0:
        return None
    z = 1.959963984540054
    proportion = successes / n
    denominator = 1 + z * z / n
    center = (proportion + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / n + z * z / (4 * n * n)) / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def paired_bootstrap_difference(
    pairs: list[dict], *, seed: int = 0, samples: int = 2000,
) -> dict:
    """Paired cluster bootstrap of success A minus B, resampling group IDs.

    Input rows are {group_id, a, b}. Repeated cases and variants within one
    group are averaged before resampling; they are not independent samples.
    The estimand weights groups equally, irrespective of their case counts.
    """
    if type(samples) is not int or samples < 100:
        raise ValueError("bootstrap requires at least 100 samples")
    if not pairs:
        raise ValueError("paired bootstrap requires at least one pair")
    grouped = {}
    for row in pairs:
        if not isinstance(row.get("group_id"), str) or not row["group_id"]:
            raise ValueError("each bootstrap pair requires a group_id")
        if type(row.get("a")) not in (bool, int) or row["a"] not in (0, 1):
            raise ValueError("bootstrap outcomes must be binary")
        if type(row.get("b")) not in (bool, int) or row["b"] not in (0, 1):
            raise ValueError("bootstrap outcomes must be binary")
        grouped.setdefault(row["group_id"], []).append(int(row["a"]) - int(row["b"]))
    means = [sum(grouped[key]) / len(grouped[key]) for key in sorted(grouped)]
    estimate = sum(means) / len(means)
    interval = None
    if len(means) >= 2:
        rng = random.Random(seed)
        draws = [sum(rng.choice(means) for _ in means) / len(means) for _ in range(samples)]
        interval = [percentile(draws, 0.025), percentile(draws, 0.975)]
    return {
        "difference": estimate,
        "ci_95": interval,
        "n_pairs": len(pairs),
        "n_groups": len(means),
        "bootstrap_samples": samples,
        "seed": seed,
        "estimand": "mean of within-group paired success differences, equal group weights",
        "method": "paired cluster percentile bootstrap over group_id",
        "limitation": "No interval with fewer than two groups; a tiny group count gives weak inference.",
    }
