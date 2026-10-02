"""JSON parsing for untrusted model responses and tool arguments."""

import json
import math


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number exceeds finite float range")
    return parsed


def strict_loads(value):
    """Reject NaN/Infinity extensions and finite literals that overflow to inf.

    This keeps parsed values compatible with the checkpoint store's strict JSON
    serializer. Strings containing these words remain ordinary strings.
    """
    return json.loads(value, parse_constant=_reject_constant, parse_float=_finite_float)
