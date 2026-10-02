"""Bounded record delivery with cursors based on the delivered prefix.

Offsets address the deterministic matching-record order, not character ranges.
The caller must keep its query and frozen snapshot unchanged between pages.
"""

import json


def record_chars(record: dict) -> int:
    """Canonical JSON record characters charged by the observation budget."""
    return len(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def page_result(records: list[dict], offset: int, limit: int) -> dict:
    """Return one page of already filtered/ordered public records."""
    selected = records[offset:offset + limit]
    end = offset + len(selected)
    next_offset = end if end < len(records) else None
    return {"ok": True, "records": selected,
            "page": {"offset": offset, "total_matches": len(records),
                     "returned_count": len(selected), "next_offset": next_offset},
            "truncated": next_offset is not None}


def _prefix(result: dict, count: int, reason: str | None) -> dict:
    bounded = {**result, "records": result.get("records", [])[:count]}
    page = result.get("page")
    if isinstance(page, dict):
        end = page["offset"] + count
        bounded["page"] = {**page, "returned_count": count,
                           "next_offset": end if end < page["total_matches"] else None}
    if reason:
        bounded["truncated"] = True
        bounded["limit_reason"] = reason
        if reason.startswith("observation_"):
            bounded["budget_exhausted"] = True
        if not count:
            budget = reason.startswith("observation_")
            bounded["ok"] = False
            bounded["error"] = {
                "code": "observation_budget_exhausted" if budget else "result_too_large",
                "message": ("The next matching record exceeds the remaining observation budget. "
                            "No record was delivered; this is not an empty search result."
                            if budget else
                            "The next matching record exceeds the response character limit. "
                            "No record was delivered; this is not an empty search result."),
            }
    return bounded


def bound_result(result: dict, max_chars: int = 12000, *,
                 max_records: int | None = None, max_record_chars: int | None = None) -> dict:
    """Keep a contiguous record prefix and recompute its continuation cursor.

    ``max_chars`` limits the complete JSON response where a record can fit;
    an error/control envelope may exceed an exceptionally small response limit.
    Optional observation budgets charge only canonical record JSON. Metadata,
    errors and cursors are not observations and are never charged as records.
    A wholly blocked response is an explicit error, not a successful no-match.
    """
    if not result.get("ok"):
        return result
    records = result.get("records", [])
    count, chars, reason = 0, 0, None
    for record in records:
        if max_records is not None and count >= max_records:
            reason = "observation_record_budget"
            break
        size = record_chars(record)
        if max_record_chars is not None and chars + size > max_record_chars:
            reason = "observation_character_budget"
            break
        count += 1
        chars += size
    bounded = _prefix(result, count, reason)
    while count and len(json.dumps(bounded, ensure_ascii=False)) > max_chars:
        count -= 1
        reason = "result_character_limit"
        bounded = _prefix(result, count, reason)
        # A response limit can become tighter than an observation limit.
        # Rebuild from the original result so stale budget flags are not kept.
    return bounded
