"""Frozen, paired policy experiments with bounded observations and model calls.

Policies receive only incident tools and a provider. Manifest labels stay in
the orchestration/scoring layer and are never included in model messages.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import re
import threading
import time
import uuid

from . import __version__
from .evaluation import (
    DEMO_WARNING, METRIC_NOTES, TOKEN_FIELDS, _validate_label,
    paired_bootstrap_difference, percentile, score_run, wilson_interval,
)
from .providers import ChatProvider, ProviderError
from .pagination import bound_result, record_chars
from .runtime import AgentRuntime, REPORT_SCHEMA, SYSTEM_PROMPT, validate_report
from .report_audit import audit_report
from .store import RunStore
from .tools import IncidentTools


POLICIES = ("single-shot", "fixed-workflow", "react", "investigator")
POLICY_DESCRIPTIONS = {
    "single-shot": "Broad deterministic collection over all discovered services, then one diagnosis call.",
    "fixed-workflow": "Metrics/changes for all services, alert-derived literal log/runbook searches, then one diagnosis call.",
    "react": "Model chooses tools iteratively within the same source and observation limits.",
    "investigator": "Iterative investigation with runtime hypothesis bookkeeping; structural checks are not semantic judging.",
}
DEFAULT_OBSERVATION_BUDGET = {"max_chars": 24000, "max_records": 100}
MAX_CONTEXT_CHARS = 48000
MAX_TOTAL_TOKENS = 60000
BASELINE_PROMPT = (
    SYSTEM_PROMPT + "\nThis is a one-call baseline. Evidence has been collected by a "
    "deterministic retrieval policy, subject to a budget. No further retrieval is "
    "available. Submit exactly one submit_diagnosis call using only supplied record IDs."
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_manifest(manifest_path: str | Path) -> dict:
    """Validate hashes and group separation before creating or calling a model.

    Paths are relative to the manifest, not to the process working directory.
    The returned object belongs to the evaluator; do not pass it to policies.
    """
    path = Path(manifest_path).resolve()
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("benchmark manifest requires schema_version=1")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest cases must be a nonempty list")
    budget = manifest.get("observation_budget", DEFAULT_OBSERVATION_BUDGET)
    if not isinstance(budget, dict) or set(budget) != {"max_chars", "max_records"}:
        raise ValueError("observation_budget requires max_chars and max_records")
    for field in ("max_chars", "max_records"):
        if type(budget[field]) is not int or budget[field] < 1:
            raise ValueError(f"observation_budget {field} must be a positive integer")
    normalized, seen, groups = [], set(), {}
    for entry in cases:
        if not isinstance(entry, dict):
            raise ValueError("manifest cases must be objects")
        case_id, group_id = entry.get("case_id"), entry.get("group_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("manifest case_id must be nonempty and unique")
        if not isinstance(group_id, str) or not group_id:
            raise ValueError("manifest cases require a nonempty group_id")
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            raise ValueError("manifest cases require a path")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError("manifest cases require a SHA-256 digest")
        case_path = (path.parent / entry["path"]).resolve()
        case_bytes = case_path.read_bytes()
        if _sha256(case_bytes) != digest.lower():
            raise ValueError(f"snapshot hash mismatch: {case_id}")
        snapshot = json.loads(case_bytes)
        if not isinstance(snapshot, dict) or snapshot.get("case_id") != case_id:
            raise ValueError(f"snapshot case_id mismatch: {case_id}")
        IncidentTools._validate_snapshot(snapshot)
        if type(snapshot.get("synthetic")) is not bool:
            raise ValueError("snapshot must explicitly declare synthetic true or false")
        source_kind = "synthetic" if snapshot["synthetic"] else "live_capture"
        if entry.get("source_kind", source_kind) != source_kind:
            raise ValueError(f"source_kind disagrees with snapshot: {case_id}")
        label = entry.get("label")
        required = _validate_label(label)
        if label.get("root_service") is not None and label["root_service"] not in snapshot.get("services", []):
            raise ValueError(f"label root_service absent from snapshot services: {case_id}")
        evidence_ids = {record.get("id") for record in snapshot.get("records", [])
                        if isinstance(record, dict) and isinstance(record.get("id"), str)}
        if not required.issubset(evidence_ids):
            raise ValueError(f"label references evidence absent from snapshot: {case_id}")
        if any(not set(group).issubset(evidence_ids) for group in label.get("evidence_groups", [])):
            raise ValueError(f"label evidence group references absent records: {case_id}")
        split = label["split"]
        if group_id in groups and groups[group_id] != split:
            raise ValueError(f"group_id crosses splits: {group_id}")
        groups[group_id] = split
        seen.add(case_id)
        item = {
            "case_id": case_id, "group_id": group_id, "path": case_path,
            "sha256": digest.lower(), "label": dict(label), "source_kind": source_kind,
            "source": entry.get("source", snapshot.get("source", source_kind)),
            "raw_snapshot": case_bytes,
        }
        if entry.get("provenance_path"):
            provenance_path = (path.parent / entry["provenance_path"]).resolve()
            item["raw_provenance"] = provenance_path.read_bytes()
            item["provenance_sha256"] = _sha256(item["raw_provenance"])
        normalized.append(item)
    return {
        "schema_version": 1,
        "dataset_version": manifest.get("dataset_version", manifest.get("version", "unspecified")),
        "original_manifest_sha256": _sha256(raw),
        "observation_budget": dict(budget),
        "cases": normalized,
    }


def validate_manifest(manifest_path: str | Path) -> dict:
    """JSON-safe dry-run inventory; creates no provider and performs no I/O writes."""
    manifest = load_manifest(manifest_path)
    return {
        "case_count": len(manifest["cases"]),
        "groups": sorted({case["group_id"] for case in manifest["cases"]}),
        "source_kinds": sorted({case["source_kind"] for case in manifest["cases"]}),
        "splits": sorted({case["label"]["split"] for case in manifest["cases"]}),
        "manifest_sha256": manifest["original_manifest_sha256"],
        "dataset_version": manifest["dataset_version"],
        "observation_budget": manifest["observation_budget"],
    }


class ModelCallBudget:
    """Shared atomic reservation; unsuccessful model attempts also consume it."""

    def __init__(self, limit: int):
        if type(limit) is not int or limit < 0:
            raise ValueError("max_model_calls must be a nonnegative integer")
        self.limit, self.used = limit, 0
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.limit - self.used

    def reserve(self) -> None:
        with self._lock:
            if self.used >= self.limit:
                raise ProviderError("global_model_call_budget_exhausted")
            self.used += 1


class BudgetedProvider:
    def __init__(self, provider, budget: ModelCallBudget):
        self.provider, self.budget = provider, budget
        self.name = provider.name
        self.model_calls = 0
        self.scripted_demo = (
            getattr(provider, "is_scripted", False) is True or "scripted-demo" in self.name.lower()
        )
        # Built-in providers otherwise retry internally, escaping an outer
        # complete-call count. A benchmark reserves once for each HTTP attempt.
        if isinstance(provider, ChatProvider):
            provider.max_attempts = 1

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        self.budget.reserve()
        self.model_calls += 1
        return self.provider.complete(messages, tools)


class BudgetedTools:
    """The same delivered-evidence envelope for every policy.

    Characters count canonical JSON record payloads (Unicode code points),
    including duplicates. Alerts, schemas and control/error envelopes are not
    evidence and are outside this budget. Token usage remains separately measured.
    """

    def __init__(self, tools, max_chars: int = 24000, max_records: int = 100):
        if type(max_chars) is not int or type(max_records) is not int or min(max_chars, max_records) < 1:
            raise ValueError("observation budgets must be positive integers")
        self.tools = tools
        self.case_id, self.alert = tools.case_id, tools.alert
        self.fingerprint, self.schemas = tools.fingerprint, tools.schemas
        self.source = getattr(tools, "source", "unknown")
        self.max_chars, self.max_records = max_chars, max_records
        self.chars, self.records, self.requests = 0, 0, 0
        self.unique_ids = set()

    def execute(self, name: str, arguments: dict) -> dict:
        self.requests += 1
        result = self.tools.execute(name, arguments)
        if not result.get("ok"):
            return result
        # Preserve a contiguous prefix: skipping a large record would make the
        # continuation offset jump over evidence the model never received.
        # Stay below the runtime's 12000-character cap so all charged records
        # are delivered, including pagination and limit metadata.
        bounded = bound_result(result, 10000, max_records=self.max_records - self.records,
                               max_record_chars=self.max_chars - self.chars)
        for record in bounded["records"]:
            self.chars += record_chars(record)
            self.records += 1
            self.unique_ids.add(record["id"])
        return bounded

    def measurements(self) -> dict:
        return {"max_chars": self.max_chars, "max_records": self.max_records,
                "delivered_chars": self.chars, "delivered_records": self.records,
                "unique_records": len(self.unique_ids), "retrieval_calls": self.requests,
                "counting": "canonical JSON record characters; duplicates count again"}


def _event(state: dict, kind: str, **fields) -> None:
    state["events"].append({"seq": len(state["events"]) + 1,
                            "time": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields})


def _new_state(case_id: str, fingerprint: str, provider: str, policy: str) -> dict:
    return {
        "run_id": uuid.uuid4().hex, "case_id": case_id, "case_fingerprint": fingerprint,
        "provider": provider, "policy": policy, "status": "running", "steps": 0,
        "tool_calls": 0, "model_seconds": 0.0, "observed": {}, "report": None,
        "messages": [], "events": [], "pending_model_request": False,
        "usage": {key: 0 for key in TOKEN_FIELDS}, "usage_complete": True,
    }


def _alert_terms(alert: str, services: list[str]) -> list[str]:
    """Small frozen keyword policy, not an LLM or an oracle over target labels."""
    terms = []
    lowered = alert.casefold()
    for term in ("timeout", "latency", "error", "5xx", "pool", "configuration", "unavailable"):
        if term in lowered:
            terms.append(term)
    if not terms:
        tokens = re.findall(r"[A-Za-z][A-Za-z_-]{3,}", alert)
        stopwords = {"synthetic", "snapshot", "diagnose", "from", "this", "only", *services}
        terms = [token.casefold() for token in tokens if token.casefold() not in stopwords]
    return list(dict.fromkeys(terms))[:2] or ["error"]


def _run_baseline(tools: BudgetedTools, provider: BudgetedProvider, store: RunStore,
                  policy: str, max_tool_calls: int, state: dict) -> dict:
    # Mutate the caller's state too, so a tool failure or interrupt cannot lose
    # the observations/counters collected before the exception.
    state["max_steps"], state["max_tool_calls"] = 1, max_tool_calls
    state.update({"max_total_tokens": MAX_TOTAL_TOKENS, "max_context_chars": MAX_CONTEXT_CHARS,
                  "source": tools.source})
    state["messages"] = [{"role": "system", "content": BASELINE_PROMPT},
                         {"role": "user", "content": tools.alert}]
    _event(state, "started", policy=policy)
    store.save(state)
    observations = []

    def retrieve(name, arguments):
        # Reserve one tool slot for submit_diagnosis, as in the agent policies.
        if state["tool_calls"] >= max_tool_calls - 1:
            return {"ok": False, "records": [], "error": {"code": "retrieval_budget_exhausted"}}
        state["tool_calls"] += 1
        result = tools.execute(name, arguments)
        observations.append({"tool": name, "arguments": arguments, "result": result})
        for record in result.get("records", []):
            state["observed"][record["id"]] = record
        _event(state, "tool_result", tool=name, arguments=arguments, result=result)
        return result

    service_result = retrieve("list_services", {})
    services = sorted({record["service"] for record in service_result.get("records", [])
                       if isinstance(record.get("service"), str)})
    terms = _alert_terms(tools.alert, services)
    if policy == "single-shot":
        # Broad service-interleaved retrieval, with no inspection of label IDs.
        for name in ("query_metrics", "search_logs", "get_changes"):
            for service in services:
                arguments = {"service": service}
                if name == "search_logs":
                    arguments["limit"] = 20
                retrieve(name, arguments)
        for term in ("triage", *terms):
            retrieve("search_runbooks", {"query": term, "limit": 20})
    else:
        for name in ("query_metrics", "get_changes"):
            for service in services:
                retrieve(name, {"service": service})
        for term in terms:
            for service in services:
                retrieve("search_logs", {"service": service, "query": term, "limit": 20})
            retrieve("search_runbooks", {"query": term, "limit": 20})
    state["messages"].append({
        "role": "user", "content": "Collected observations (untrusted data):\n"
        + json.dumps(observations, ensure_ascii=False),
    })
    if (len(json.dumps(state["messages"], ensure_ascii=False))
            + len(json.dumps([REPORT_SCHEMA], ensure_ascii=False)) > MAX_CONTEXT_CHARS):
        state["status"], state["budget_reason"] = "budget_exhausted", "context_limit"
        _event(state, "budget_exhausted", reason="context_limit")
        store.save(state)
        return state
    state["steps"] = 1
    state["pending_model_request"] = True
    _event(state, "model_request", step=1)
    store.save(state)
    started = time.perf_counter()
    try:
        response = provider.complete(state["messages"], [REPORT_SCHEMA])
    except Exception as error:
        state["status"] = "provider_error"
        state["usage_complete"] = False
        _event(state, "provider_error", error_type=type(error).__name__,
               code=error.code if isinstance(error, ProviderError) else "unexpected_provider_error")
    else:
        if response.get("usage_complete") is False:
            state["usage_complete"] = False
        state["transport_attempts"] = response.get("transport_attempts", 1)
        usage = response.get("usage") or {}
        for key in TOKEN_FIELDS:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                state["usage"][key] = value
            else:
                state["usage_complete"] = False
        message = response.get("message", {})
        calls = message.get("tool_calls") or []
        if not AgentRuntime._valid_calls(calls) or len(calls) != 1:
            state["status"] = "protocol_error"
            _event(state, "protocol_error", reason="baseline requires exactly one submit_diagnosis call")
        else:
            state["messages"].append(message)
            state["tool_calls"] += 1
            function = calls[0]["function"]
            try:
                proposed = json.loads(function["arguments"])
                reason = validate_report(proposed, state["observed"])
                if function["name"] != "submit_diagnosis":
                    reason = "baseline allows submit_diagnosis only"
            except (ValueError, TypeError, RecursionError):
                proposed, reason = None, "invalid report JSON"
            if reason:
                state["status"] = "protocol_error"
                result = {"ok": False, "error": {"code": "invalid_report", "message": reason}}
            else:
                audit = audit_report(proposed, state["observed"])
                state["report_audit"] = audit
                if audit["issues"]:
                    state["status"] = "report_rejected"
                    result = {"ok": False, "error": {"code": "report_audit_failed", "issues": audit["issues"]}}
                else:
                    state["report"], state["status"] = proposed, "completed"
                    result = {"ok": True, "accepted": True}
            _event(state, "tool_result", tool=function["name"], arguments=proposed, result=result)
    finally:
        state["model_seconds"] += time.perf_counter() - started
        state["pending_model_request"] = False
        store.save(state)
    return state


def _summarize_policy(rows: list[dict], inference_block_reason: str | None = None) -> dict:
    scores = [row["score"] for row in rows]
    n = len(rows)
    succeeded = sum(score["task_success"] for score in scores)
    attempted = [row for row in rows if row["attempted"]]
    times = [row["wall_seconds"] for row in attempted]
    complete_usage = all(score["usage_complete"] for score in scores)
    group_ids = {row["group_id"] for row in rows}
    counts = Counter(score["failure_category"] for score in scores if score["failure_category"])
    components = [score["root_service_match"] for score in scores if score["root_service_match"] is not None]
    return {
        "n": n, "n_attempted": len(attempted), "n_success": succeeded,
        "n_structural_success": sum(score.get("structural_task_success", score["task_success"]) for score in scores),
        "n_numeric_audit_flagged": sum(score.get("numeric_audit_passed") is False or score.get("numeric_rejection_count", 0) > 0 for score in scores),
        "numeric_rejection_count": sum(score.get("numeric_rejection_count", 0) for score in scores),
        "task_success_rate": succeeded / n,
        "task_success_wilson_95": None if inference_block_reason else wilson_interval(succeeded, n),
        "inference_block_reason": inference_block_reason,
        "cause_match_rate": sum(score["cause_match"] for score in scores) / n,
        "n_with_root_service_label": len(components),
        "root_service_match_rate": sum(components) / len(components) if components else None,
        "wilson_independent_trial_layout": len(group_ids) == n and not inference_block_reason,
        "wilson_note": (f"Interval suppressed: {inference_block_reason}. Scheduled outcomes remain in the denominator."
                        if inference_block_reason else
                        "Descriptive only when repeats or related cases share group_id; not independent trials."),
        "failure_taxonomy": dict(sorted(counts.items())),
        "model_calls": sum(row["model_calls"] for row in rows),
        "latency_seconds": {"n": len(times), "p50": percentile(times, 0.5),
                            "p95": percentile(times, 0.95),
                            "scope": "wall time of attempted runs, including failed runs; excludes unattempted rows"},
        "reported_tokens": {key: sum(score["usage"][key] for score in scores) for key in TOKEN_FIELDS},
        "usage_complete": complete_usage,
        "reported_tokens_mean_per_scheduled_run": sum(score["usage"]["total_tokens"] for score in scores) / n,
        "currency_cost": None,
        "cost_note": "Tokens only; no invented monetary cost. Missing usage produces partial, lower-bound totals.",
        "mean_tool_calls": sum(score["tool_calls"] for score in scores) / n,
        "retrieval_calls": sum(score.get("retrieval_calls", 0) for score in scores),
        "empty_retrieval_calls": sum(score.get("empty_retrieval_calls", 0) for score in scores),
        "unsupported_phase_calls": sum(score.get("unsupported_phase_calls", 0) for score in scores),
        "numeric_repair_requests": sum(score.get("numeric_repair_requests", 0) for score in scores),
        "mean_observation_chars": sum(row["observation_budget"]["delivered_chars"] for row in rows) / n,
        "mean_observation_records": sum(row["observation_budget"]["delivered_records"] for row in rows) / n,
    }


def summarize_attempts(rows: list[dict], policies: list[str], invalid_reason: str | None = None) -> list[dict]:
    """Keep source/split strata separate and pair every scheduled attempt."""
    # A global protocol violation invalidates earlier completed strata too.
    # Keep this information in the row index so re-summarizing saved outcomes
    # cannot accidentally restore intervals removed by the initial runner.
    invalid_reason = invalid_reason or next((row.get("invalid_reason") for row in rows
                                             if row.get("invalid_reason")), None)
    strata = []
    for source_kind, split in sorted({(row["source_kind"], row["split"]) for row in rows}):
        selected = [row for row in rows if (row["source_kind"], row["split"]) == (source_kind, split)]
        censored = any(row["score"]["failure_category"] in ("global_budget_exhausted", "interrupted")
                       for row in selected)
        inference_block_reason = invalid_reason or ("budget_or_interruption_censored" if censored else None)
        index = {}
        for row in selected:
            key = row["case_id"], row["repeat"], row["policy"]
            if key in index:
                raise ValueError("duplicate benchmark case/repeat/policy; best-of selection is forbidden")
            index[key] = row
        pair_keys = sorted({(row["case_id"], row["repeat"]) for row in selected})
        if any((case, repeat, policy) not in index
               for case, repeat in pair_keys for policy in policies):
            raise ValueError("benchmark comparisons require all scheduled policy/case/repeat outcomes")
        comparisons = []
        for first, second in itertools.combinations(policies, 2):
            pairs = []
            for case_id, repeat in pair_keys:
                a, b = index[(case_id, repeat, first)], index[(case_id, repeat, second)]
                pairs.append({"group_id": a["group_id"], "a": a["score"]["task_success"],
                              "b": b["score"]["task_success"]})
            comparison = paired_bootstrap_difference(pairs)
            comparison.update({"policy_a": first, "policy_b": second,
                               "direction": "policy_a minus policy_b", "budget_or_interrupt_censored": censored,
                               "inference_valid": inference_block_reason is None and comparison["n_groups"] >= 2,
                               "inference_block_reason": inference_block_reason or
                               ("insufficient_independent_groups" if comparison["n_groups"] < 2 else None)})
            if inference_block_reason:
                comparison["ci_95"] = None
                comparison["limitation"] = f"No inferential interval: {inference_block_reason}."
            comparisons.append(comparison)
        strata.append({
            "source_kind": source_kind, "split": split,
            "n_cases": len({row["case_id"] for row in selected}),
            "n_groups": len({row["group_id"] for row in selected}),
            "inference_block_reason": inference_block_reason,
            "policies": {policy: _summarize_policy([row for row in selected if row["policy"] == policy],
                                                    inference_block_reason)
                         for policy in policies},
            "paired_comparisons": comparisons,
        })
    return strata


def execute_benchmark(
    manifest_path, output_dir, provider_factory, policies: list[str], repeats: int = 1,
    max_steps: int = 12, max_tool_calls: int = 24, max_model_calls: int = 60,
) -> dict:
    """Execute the full grid once, preserving failures rather than picking winners.

    ``provider_factory()`` must return a fresh provider with ``name`` and
    ``complete(messages, tools)``. Custom providers must issue at most one model
    request per complete() and mark protocol-only fixtures ``is_scripted=True``.
    No automatic rerun/resume or best-of reduction is performed by this command.
    """
    if not isinstance(policies, list) or not policies or any(policy not in POLICIES for policy in policies):
        raise ValueError(f"policies must be a nonempty list chosen from {POLICIES}")
    if len(set(policies)) != len(policies):
        raise ValueError("policies must not contain duplicates")
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    if type(max_steps) is not int or not 1 <= max_steps <= 100:
        raise ValueError("max_steps must be 1..100")
    if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= 200:
        raise ValueError("max_tool_calls must be 1..200")
    budget = ModelCallBudget(max_model_calls)
    manifest = load_manifest(manifest_path)
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("benchmark output directory must be empty; previous attempts must not be overwritten")
    output.mkdir(parents=True, exist_ok=True)
    frozen = {key: value for key, value in manifest.items() if key != "cases"}
    frozen["cases"] = []
    for index, entry in enumerate(manifest["cases"], 1):
        relative = f"cases/case-{index:04d}.json"
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(entry["raw_snapshot"])
        case = {key: value for key, value in entry.items()
                if key not in ("raw_snapshot", "raw_provenance", "path")}
        case["path"] = relative
        if "raw_provenance" in entry:
            provenance = f"cases/provenance-{index:04d}.json"
            (output / provenance).write_bytes(entry["raw_provenance"])
            case["provenance_path"] = provenance
        frozen["cases"].append(case)
    _write_json(output / "manifest.json", frozen)
    metadata = {
        "schema_version": 1, "benchmark_id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(), "opslens_version": __version__,
        "dataset_version": manifest["dataset_version"],
        "manifest_sha256": _sha256((output / "manifest.json").read_bytes()),
        "source_hashes": {path.name: _sha256(path.read_bytes())
                          for path in sorted(Path(__file__).parent.glob("*.py"))},
        "policies": list(policies), "policy_descriptions": {p: POLICY_DESCRIPTIONS[p] for p in policies},
        "repeats": repeats, "max_steps": max_steps, "max_tool_calls": max_tool_calls,
        "max_model_calls": max_model_calls, "observation_budget": manifest["observation_budget"],
        "max_context_chars": MAX_CONTEXT_CHARS, "max_total_tokens": MAX_TOTAL_TOKENS,
        "order": "case/repeat order; policy order rotates by case index plus repeat index",
        "provider_contract": "One model request per complete(); built-in HTTP retries disabled.",
        "label_boundary": "Labels used for manifest validation/scoring only; policies receive tools, never labels.",
    }
    _write_json(output / "experiment.json", metadata)
    store = RunStore(output / "checkpoints.sqlite3")
    rows, provider_names = [], set()
    interrupted = False
    expected_provider, invalid_reason, invalid_details = None, None, None
    for case_index, entry in enumerate(frozen["cases"]):
        for repeat in range(1, repeats + 1):
            offset = (case_index + repeat - 1) % len(policies)
            ordered_policies = policies[offset:] + policies[:offset]
            for policy in ordered_policies:
                attempt_id = f"attempt-{len(rows) + 1:05d}"
                directory = output / "attempts" / attempt_id
                directory.mkdir(parents=True, exist_ok=True)
                started = time.perf_counter()
                provider = tools = None
                attempted = False
                state = _new_state(entry["case_id"], entry["sha256"],
                                   expected_provider or "unattempted", policy)
                try:
                    if invalid_reason:
                        state["status"] = "skipped_invalid"
                        _event(state, "skipped_invalid", code=invalid_reason)
                    elif interrupted:
                        state["status"] = "interrupted"
                    elif budget.remaining == 0:
                        state["status"] = "global_budget_exhausted"
                    else:
                        attempted = True
                        provider = BudgetedProvider(provider_factory(), budget)
                        provider_names.add(provider.name)
                        state["provider"] = provider.name
                        if expected_provider is None:
                            expected_provider = provider.name
                        if provider.name != expected_provider:
                            invalid_reason = "provider_identity_changed"
                            invalid_details = {"expected_provider": expected_provider,
                                               "observed_provider": provider.name, "attempt_id": attempt_id}
                            state["status"] = "experiment_invalid"
                            _event(state, "experiment_invalid", code=invalid_reason, **invalid_details)
                        else:
                            tools = BudgetedTools(IncidentTools(output / entry["path"]), **manifest["observation_budget"])
                            if policy in ("react", "investigator"):
                                runtime = AgentRuntime(tools, provider, store, policy=policy)
                                state = runtime.start(max_steps=max_steps, max_tool_calls=max_tool_calls,
                                                      max_total_tokens=MAX_TOTAL_TOKENS, max_context_chars=MAX_CONTEXT_CHARS)
                                state = runtime.run(state)
                            else:
                                state = _run_baseline(tools, provider, store, policy, max_tool_calls, state)
                except KeyboardInterrupt:
                    interrupted = True
                    state["status"] = "interrupted"
                    state["usage_complete"] = False
                    _event(state, "interrupted", reason="user interrupt; remaining scheduled outcomes retained")
                except Exception as error:
                    # Preserve only safe error type/code, never keys or raw provider bodies.
                    state["status"] = "execution_error" if provider and provider.model_calls else "setup_error"
                    state["usage_complete"] = False
                    _event(state, "execution_error", error_type=type(error).__name__,
                           code=error.code if isinstance(error, ProviderError) else "unexpected_execution_error")
                if any(event.get("code") == "global_model_call_budget_exhausted" for event in state["events"]):
                    state["status"] = "global_budget_exhausted"
                state.update({"policy": policy, "group_id": entry["group_id"], "repeat": repeat,
                              "source_kind": entry["source_kind"], "attempt_id": attempt_id,
                              "attempted": attempted,
                              "invalid_reason": invalid_reason,
                              "model_calls": provider.model_calls if provider else 0,
                              "scripted_demo": provider.scripted_demo if provider else False,
                              "wall_seconds": time.perf_counter() - started})
                observation = tools.measurements() if tools else {
                    **manifest["observation_budget"], "delivered_chars": 0,
                    "delivered_records": 0, "unique_records": 0, "retrieval_calls": 0,
                }
                state["observation_budget"] = observation
                store.save(state)
                # Only now are evaluator labels combined with a completed attempt.
                score = score_run(state, entry["label"])
                public_state = {key: value for key, value in state.items() if key != "messages"}
                _write_json(directory / "state.json", public_state)
                _write_json(directory / "score.json", score)
                (directory / "trace.jsonl").write_text(
                    "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in state["events"]), encoding="utf-8")
                row = {key: state[key] for key in ("attempt_id", "attempted", "invalid_reason", "case_id", "group_id", "repeat", "policy",
                                                   "source_kind", "model_calls", "wall_seconds", "observation_budget")}
                row.update({"split": entry["label"]["split"], "score": score,
                            "state_path": f"attempts/{attempt_id}/state.json",
                            "score_path": f"attempts/{attempt_id}/score.json"})
                rows.append(row)
                # Write progress after EVERY outcome, including failures and skipped rows.
                _write_json(output / "attempts.json", {"attempts": rows})
    scripted = any(row["score"]["scripted_demo"] for row in rows)
    censored = any(row["score"]["failure_category"] in ("global_budget_exhausted", "interrupted") for row in rows)
    warnings = [DEMO_WARNING] if scripted else []
    if censored:
        warnings.append("INCOMPLETE EXPERIMENT: global budget/interruption censored scheduled outcomes; Wilson and paired intervals are suppressed for affected strata.")
    if invalid_reason:
        warnings.append("INVALID EXPERIMENT: provider identity changed. Further model requests stopped; all Wilson and paired intervals are suppressed.")
    warnings.append("Synthetic fixtures and local fault captures are reported separately; neither represents production incident diversity.")
    summary = {
        **metadata, "n_cases": len(frozen["cases"]), "n_attempts": len(rows),
        "provider_names": sorted(provider_names), "model_calls_used": budget.used,
        "scripted_demo": scripted, "experiment_complete": not censored and invalid_reason is None,
        "experiment_valid": invalid_reason is None, "invalid_reason": invalid_reason, "invalid_details": invalid_details,
        "interpretation": "invalid_experiment" if invalid_reason else "not_executed" if budget.used == 0 else
                          "protocol_demo_only" if scripted else "model_experiment_requires_review",
        "warnings": warnings, "metric_notes": dict(METRIC_NOTES),
        "strata": summarize_attempts(rows, policies, invalid_reason),
        "output_dir": str(output),
    }
    _write_json(output / "summary.json", summary)
    return summary
