"""Structured, auditable hypotheses; this is a workflow constraint, not a truth oracle."""

from .report_audit import audit_report

CAUSES = ["connection_pool_exhaustion", "dependency_timeout", "bad_configuration", "healthy", "insufficient_evidence"]
INSTRUCTIONS = """
Use record_hypothesis to maintain a compact investigation ledger, not a private reasoning transcript.
Each entry states a falsifiable explanation, observed supporting/contradicting evidence,
and a concrete next check. Update entries as new evidence arrives. A diagnosis requires
one supported hypothesis matching its category and another distinct category ruled out
using observed incident evidence. Runbook guidance alone cannot establish an incident cause.
If you cannot distinguish alternatives, submit insufficient_evidence rather than inventing
counterevidence. The ledger checks provenance and workflow; you must assess causality.
The healthy category means observed normal operation, not merely missing telemetry.
"""

HYPOTHESIS_SCHEMA = {
    "type": "function", "function": {
        "name": "record_hypothesis",
        "description": "Create or revise a falsifiable incident hypothesis with observed evidence.",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "maxLength": 40},
                "cause_code": {"type": "string", "enum": CAUSES},
                "statement": {"type": "string", "maxLength": 800},
                "status": {"type": "string", "enum": ["open", "supported", "ruled_out"]},
                "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                "contradicting_evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                "next_check": {"type": "string", "maxLength": 800},
            },
            "required": ["id", "cause_code", "statement", "status", "supporting_evidence_ids", "contradicting_evidence_ids", "next_check"],
        },
    },
}


def record_hypothesis(arguments, observed, hypotheses):
    def reject(message):
        return {"ok": False, "error": {"code": "invalid_hypothesis", "message": message}}

    fields = HYPOTHESIS_SCHEMA["function"]["parameters"]["properties"]
    if not isinstance(arguments, dict) or set(arguments) != set(fields):
        return reject("fields must match the hypothesis schema")
    for key, limit in (("id", 40), ("statement", 800), ("next_check", 800)):
        if not isinstance(arguments[key], str) or not arguments[key].strip() or len(arguments[key]) > limit:
            return reject(f"invalid {key}")
    if arguments["cause_code"] not in CAUSES or arguments["status"] not in ("open", "supported", "ruled_out"):
        return reject("invalid category or status")
    if arguments["id"] not in hypotheses and len(hypotheses) >= 12:
        return reject("at most 12 hypotheses per run")
    for key in ("supporting_evidence_ids", "contradicting_evidence_ids"):
        ids = arguments[key]
        if not isinstance(ids, list) or len(ids) > 12 or any(not isinstance(item, str) for item in ids):
            return reject(f"invalid {key}")
        if len(ids) != len(set(ids)) or any(item not in observed for item in ids):
            return reject("evidence must be distinct IDs already returned by tools")
    support, contradict = arguments["supporting_evidence_ids"], arguments["contradicting_evidence_ids"]
    if set(support) & set(contradict):
        return reject("an observation cannot both support and contradict the same entry")
    required = support if arguments["status"] == "supported" else contradict if arguments["status"] == "ruled_out" else None
    if required is not None and not any(observed[item].get("kind") in ("metric", "log", "change") for item in required):
        return reject("decided hypotheses require incident evidence, not only runbooks or a service listing")
    audit = audit_report({"root_cause": arguments["statement"],
                          "evidence_ids": list(dict.fromkeys(support + contradict))}, observed)
    if audit["issues"]:
        return {"ok": False, "error": {"code": "invalid_hypothesis", "message": "Correct or qualify the numeric claim.",
                                       "issues": audit["issues"]}}
    # Copy lists so callers cannot mutate an accepted checkpoint entry by reference.
    hypotheses[arguments["id"]] = {**arguments, "supporting_evidence_ids": list(support), "contradicting_evidence_ids": list(contradict)}
    return {"ok": True, "hypotheses": list(hypotheses.values())}


def diagnosis_gate(report, hypotheses):
    if report["status"] == "insufficient_evidence":
        return None  # Do not force invented counterevidence to permit abstention.
    chosen = [item for item in hypotheses.values()
              if item["status"] == "supported" and item["cause_code"] == report["cause_code"]]
    if not chosen:
        return "record a supported hypothesis for the selected cause before diagnosing"
    citations = set(report["evidence_ids"])
    if not any(set(item["supporting_evidence_ids"]).issubset(citations) for item in chosen):
        return "the report must cite the selected hypothesis's supporting evidence"
    alternatives = [item for item in hypotheses.values()
                    if item["status"] == "ruled_out" and item["cause_code"] != report["cause_code"]
                    and item["cause_code"] != "insufficient_evidence"]
    if not alternatives:
        return "investigate and record counterevidence for a competing cause, or abstain if unresolved"
    return None
