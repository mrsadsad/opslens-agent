"""Conservative, deterministic checks for three known report failure classes.

This is NOT a semantic or causal correctness judge. It never reads labels,
changes a report, or calls a model. Empty issues mean only that these rules did
not match. Cross-clause associations and denominator/percentile arguments are
review flags, not proof that the underlying diagnosis is false.

Numeric pairs must be explicit transitions inside one clause. A local claim can
produce an arithmetic error; a cross-clause claim needs a shared textual subject
and one distinct candidate transition, and produces only a review flag. Units
are deliberately limited. Unrecognized units, ambiguous multiple transitions,
implicit numbers and general prose arithmetic are outside coverage.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException
import hashlib
from pathlib import Path
import re


AUDIT_VERSION = "1.0.0"
# Include BOTH constants in runtime definition hashes. A forgotten version bump
# must not silently allow a checkpoint to resume under different audit rules.
AUDIT_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?"
_UNIT = (
    r"milliseconds?|seconds?|minutes?|hours?|requests?|connections?|"
    r"KiB|MiB|GiB|KB|MB|GB|ms|s|秒|毫秒|分钟|小时|万|千|%|％|个|条|次"
)
_LEFT = rf"(?<![A-Za-z0-9_.:])(?P<old>{_NUMBER})\s*(?P<old_unit>{_UNIT})?\s*"
_RIGHT = rf"\s*(?P<new>{_NUMBER})\s*(?P<new_unit>{_UNIT})?(?![\d.])"
_TRANSITIONS = [
    re.compile(_LEFT + r"(?:→|->|⇒|=>|降(?:低)?(?:到|至|为)|减少(?:到|至)|缩减(?:到|至)|"
               r"升(?:高)?(?:到|至|为)|增加(?:到|至)|增长(?:到|至)|变为|变成)" + _RIGHT, re.I),
    re.compile(r"(?:从|由|\bfrom\s+)" + _LEFT
               + r"(?:降|降低|下降|减|减少|缩减|升|升高|增加|增长|调整|变更|变)?\s*(?:到|至|为|\bto\b)"
               + _RIGHT, re.I),
]
_HALF = re.compile(
    r"砍半|减半|減半|折半|(?:减少|降低|下降|削减)(?:了)?一半|降至(?:原来(?:的)?)?一半|"
    r"\bhalv(?:ed|ing|es)\b|\bcut\s+(?:in|by)\s+half\b|\breduc(?:ed|tion)\s+by\s+half\b", re.I)
_PERCENT_CHANGE = re.compile(
    rf"(?P<direction>减少|降低|下降|缩减|削减|增加|增长|上升|提高|"
    rf"decreased?|reduced?|dropped?|increased?|grew)(?:了|约|大约|\s|by)*"
    rf"(?P<percent>{_NUMBER})\s*[%％]", re.I)
_SENTENCE_BREAK = re.compile(r"[。！？!?；;\n]+|\.(?!\d)")
_CLAUSE_BREAK = re.compile(r"，|(?<!\d),|,(?!\d)|、|(?:但是|而且|并且|同时|另外|而)|\b(?:and|but|whereas|while)\b", re.I)
_NEGATION = re.compile(
    r"不是|并非|并不|不应|不要|不得|不能|不可|无法|未能|不支持|不代表|不成立|不等于|不一致|不相符|"
    r"缺乏依据|尚无依据|无依据|未证实|未经证实|错误地|错误说法|"
    r"\b(?:not|cannot|can't|shouldn't|mustn't|never|unsupported|invalid)\b|\bno\s+evidence\b", re.I)
_APPROXIMATE = re.compile(r"约|大约|近似|近乎|几乎|\b(?:about|roughly|approximately|nearly)\b", re.I)
_P95 = re.compile(r"p\s*95(?:[_-][A-Za-z_]+)?|95(?:th)?\s*percentile|95\s*(?:百分位|分位)", re.I)
_ADDITION = re.compile(r"加上|相加|之和|求和|\+|\b(?:plus|sum|summed|adding|added|add)\b", re.I)
_P95_INFERENCE = re.compile(r"相符|吻合|等于|得到|得出|推断|推得|因此|总延迟|端到端|整体|"
                            r"\b(?:equals?|gives?|therefore|overall|end.to.end|matches?|consistent)\b|=", re.I)
_COUNT = re.compile(rf"{_NUMBER}\s*(?:(?:个|条|次)\s*)?(?:错误|失败|异常)|"
                    rf"{_NUMBER}\s*(?:个)?\s*请求[^。；;]{{0,18}}(?:错误|失败)|"
                    rf"{_NUMBER}\s+(?:errors?|failures?|failed\s+requests?)\b|"
                    rf"\b(?:errors?|failures?)\b[^.;]{{0,35}}{_NUMBER}\s+requests?\b", re.I)
_RATE = re.compile(rf"{_NUMBER}\s*[%％]")
_AGREEMENT = re.compile(r"一致|吻合|相符|印证|证明|"
                        r"\b(?:matches?|consistent\s+with|aligns?\s+with|agrees?\s+with)\b", re.I)
_WINDOW = re.compile(r"(?:同一|相同)(?:个|统计|时间)*(?:窗口|分钟|小时|时段)|"
                     r"\bsame\s+(?:time\s+|one.minute\s+)?(?:window|minute|interval)\b", re.I)
_POPULATION = re.compile(r"(?:同一|相同)(?:个)?(?:路由|接口|服务|请求范围|请求集合|统计口径|口径|分母)|"
                         r"\bsame\s+(?:(?:request|traffic)\s+)?(?:population|scope|route|denominator)\b", re.I)
_DENOMINATOR = re.compile(rf"(?:总请求数?|请求总数|分母|total\s+requests?|denominator)\s*(?:为|是|[:=])?\s*{_NUMBER}|"
                          rf"{_NUMBER}\s*(?:个|次)?\s*(?:总请求|total\s+requests?)", re.I)

# These normalize explicit unit spellings, not semantic metric names.
_UNITS = {
    "ms": ("time", Decimal(1)), "millisecond": ("time", Decimal(1)),
    "milliseconds": ("time", Decimal(1)), "毫秒": ("time", Decimal(1)),
    "s": ("time", Decimal(1000)), "second": ("time", Decimal(1000)),
    "seconds": ("time", Decimal(1000)), "秒": ("time", Decimal(1000)),
    "minute": ("time", Decimal(60000)), "minutes": ("time", Decimal(60000)),
    "分钟": ("time", Decimal(60000)), "hour": ("time", Decimal(3600000)),
    "hours": ("time", Decimal(3600000)), "小时": ("time", Decimal(3600000)),
    "%": ("proportion", Decimal("0.01")), "％": ("proportion", Decimal("0.01")),
    "kb": ("bytes", Decimal(1000)), "mb": ("bytes", Decimal(1000000)), "gb": ("bytes", Decimal(1000000000)),
    "kib": ("bytes", Decimal(1024)), "mib": ("bytes", Decimal(1048576)), "gib": ("bytes", Decimal(1073741824)),
    "千": ("number", Decimal(1000)), "万": ("number", Decimal(10000)),
    "个": ("count", Decimal(1)), "条": ("count", Decimal(1)), "次": ("count", Decimal(1)),
    "request": ("requests", Decimal(1)), "requests": ("requests", Decimal(1)),
    "connection": ("connections", Decimal(1)), "connections": ("connections", Decimal(1)),
    "": ("number", Decimal(1)),
}
_SUBJECT_STOP = {"from", "to", "the", "a", "an", "is", "was", "has", "been", "by", "and", "therefore",
                 "decreased", "reduced", "increased", "capacity", "value", "then", "which", "that"}


def _number(value: str) -> Decimal:
    # Reports are untrusted input. Decimal accepts enormous exponents, which
    # could otherwise overflow during unit conversion or ratio computation.
    compact = value.replace(",", "")
    if len(compact) > 48:
        raise ValueError("numeric literal exceeds audit bounds")
    number = Decimal(compact)
    if not number.is_finite() or not -18 <= number.adjusted() <= 18:
        raise ValueError("numeric magnitude exceeds audit bounds")
    return number


def _unsupported_numeric_expression(text: str) -> bool:
    for sentence in _SENTENCE_BREAK.split(text):
        for clause in _CLAUSE_BREAK.split(sentence):
            literals = [match[field] for pattern in _TRANSITIONS for match in pattern.finditer(clause)
                        for field in ("old", "new")]
            literals.extend(match["percent"] for match in _PERCENT_CHANGE.finditer(clause))
            for literal in literals:
                try:
                    _number(literal)
                except (DecimalException, ValueError):
                    return True
    return False


def _subjects(prefix: str) -> set[str]:
    """Lexical association only; no arbitrary subject inference across clauses."""
    subjects = {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", prefix)
                if word.lower() not in _SUBJECT_STOP}
    chinese = re.sub(r"^(?:因此|所以|说明是|说明|可见|其中|该|这个|这次)", "", prefix.strip())
    chinese = re.sub(r"(?:从|由|被|把|已|已经|的容量|容量|数值|数目|数量|值|容量被)$", "", chinese)
    subjects.update(re.findall(r"[\u4e00-\u9fff]{2,16}", chinese))
    if re.search(r"DB_POOL_MAX|pool[_ -]?(?:max|capacity|limit)|池容量|连接池上限|池上限", prefix, re.I):
        subjects.add("metric:pool_capacity")
    return subjects


def _negated(text: str, start: int, end: int) -> bool:
    # Limit negation to this assertion's neighbourhood. A later unrelated
    # warning must not erase an earlier arithmetic contradiction.
    return _NEGATION.search(text[max(0, start - 70):min(len(text), end + 40)]) is not None


def _transitions(text: str) -> list[dict]:
    transitions, seen = [], set()
    for sentence_index, sentence in enumerate(_SENTENCE_BREAK.split(text)):
        for clause_index, clause in enumerate(_CLAUSE_BREAK.split(sentence)):
            for pattern in _TRANSITIONS:
                for match in pattern.finditer(clause):
                    key = (sentence_index, clause_index, match.start("old"), match.start("new"))
                    if key in seen:
                        continue
                    seen.add(key)
                    old_unit, new_unit = match["old_unit"] or "", match["new_unit"] or ""
                    # 千/万 modify their own numeral, unlike a shared physical
                    # unit: "1千 -> 500" means 1000 -> 500, not 1000 -> 500000.
                    first_unit = (old_unit or (new_unit if new_unit not in {"千", "万"} else "")).lower()
                    second_unit = (new_unit or (old_unit if old_unit not in {"千", "万"} else "")).lower()
                    first_dimension, first_scale = _UNITS[first_unit]
                    second_dimension, second_scale = _UNITS[second_unit]
                    if first_dimension != second_dimension:
                        continue
                    try:
                        old, new = _number(match["old"]) * first_scale, _number(match["new"]) * second_scale
                    except (DecimalException, ValueError):
                        continue
                    if old <= 0 or new < 0:
                        continue  # Signed/zero baselines need domain interpretation.
                    transitions.append({
                        "old": old, "new": new, "dimension": first_dimension,
                        "display": f"{match['old']}{match['old_unit'] or ''}→{match['new']}{match['new_unit'] or ''}",
                        "sentence": sentence_index, "clause": clause_index,
                        "subjects": _subjects(clause[:match.start("old")]),
                    })
    return transitions


def _claims(clause: str):
    for match in _HALF.finditer(clause):
        yield match, Decimal("0.5"), "decrease"
    for match in _PERCENT_CHANGE.finditer(clause):
        direction = "increase" if re.match(r"增加|增长|上升|提高|increase|grew", match["direction"], re.I) else "decrease"
        try:
            fraction = _number(match["percent"]) / 100
        except (DecimalException, ValueError):
            continue
        yield match, fraction, direction


def _distinct(transitions: list[dict]) -> list[dict]:
    # The same explicit change may appear in root_cause and explanation.
    by_value = {}
    for transition in transitions:
        key = transition["old"], transition["new"], transition["dimension"]
        by_value.setdefault(key, transition)
    return list(by_value.values())


def _select_transition(transitions: list[dict], sentence_index: int, clause_index: int,
                       clause: str, claim, all_transitions: list[dict]):
    local = _distinct([item for item in transitions if item["sentence"] == sentence_index and item["clause"] == clause_index])
    if len(local) == 1:
        return local[0], "error"
    if local:
        return None, None  # Do not guess which of multiple changes a claim describes.
    prefix = clause[:claim.start()].strip()
    subjects = _subjects(prefix)
    previous = _distinct([item for item in transitions if item["sentence"] == sentence_index
                          and item["clause"] == clause_index - 1])
    if len(previous) == 1 and re.fullmatch(
        r"(?:因此|所以|即|也就是|相当于|which\s+is|therefore|thus|a\s+)?\s*"
        r"(?:其|该)?(?:容量|数值|数目|数量|值)?(?:已|被|是)?\s*", prefix, re.I
    ):
        return previous[0], "error"
    if not subjects:
        return None, None
    candidates = _distinct([item for item in all_transitions if subjects & item["subjects"]])
    return (candidates[0], "review") if len(candidates) == 1 else (None, None)


def _record_sources(report: dict, observed) -> list[tuple[str, str]]:
    if not isinstance(observed, dict):
        return []
    ids = report.get("evidence_ids", [])
    if not isinstance(ids, list):
        return []
    result, seen = [], set()
    for record_id in ids:
        if not isinstance(record_id, str) or record_id in seen:
            continue
        seen.add(record_id)
        record = observed.get(record_id)
        if isinstance(record, dict) and isinstance(record.get("content"), str):
            result.append((record_id, record["content"]))
    return result


def audit_report(report, observed) -> dict:
    """Return issues without changing report/observations or checking gold labels.

    error: a locally linked explicit relative-change claim contradicts arithmetic
    beyond rounding tolerance (2 percentage points, 5 for approximate wording).
    review: association, common counting scope, or percentile inference requires
    human verification. Rules intentionally prefer missing an ambiguous claim to
    creating a definitive arithmetic error from unrelated numbers.
    """
    if not isinstance(report, dict):
        return {"version": AUDIT_VERSION, "issues": [{
            "code": "unsupported_audit_input", "severity": "review",
            "message": "报告不是结构化对象，无法执行这些确定性审计规则。", "evidence_ids": [],
        }], "requires_review": True}
    texts = [report[key] for key in ("root_cause", "explanation") if isinstance(report.get(key), str)]
    if isinstance(report.get("next_steps"), list):
        texts.extend(item for item in report["next_steps"] if isinstance(item, str))
    sources = _record_sources(report, observed)
    transition_sets = [_transitions(text) for text in texts]
    all_transitions = [item for transitions in transition_sets for item in transitions]
    source_transitions = [(record_id, _transitions(content)) for record_id, content in sources]
    issues = []

    def add(code, severity, message, evidence_ids):
        issue = {"code": code, "severity": severity, "message": message, "evidence_ids": sorted(set(evidence_ids))}
        if issue not in issues:
            issues.append(issue)

    for text, transitions in zip(texts, transition_sets):
        if _unsupported_numeric_expression(text):
            add("numeric_range_unchecked", "review",
                "数值转变或比例声明超出本审计的数值长度/指数范围，未执行算术判定；需单独核实。", [])
        for sentence_index, sentence in enumerate(_SENTENCE_BREAK.split(text)):
            for clause_index, clause in enumerate(_CLAUSE_BREAK.split(sentence)):
                for claim, claimed_fraction, direction in _claims(clause):
                    if _negated(clause, claim.start(), claim.end()):
                        continue
                    change, severity = _select_transition(transitions, sentence_index, clause_index, clause, claim, all_transitions)
                    if change is None:
                        continue
                    actual = ((change["new"] - change["old"]) if direction == "increase" else
                              (change["old"] - change["new"])) / change["old"]
                    tolerance = Decimal("0.05") if _APPROXIMATE.search(clause) else Decimal("0.02")
                    if abs(actual - claimed_fraction) <= tolerance:
                        continue
                    evidence_ids = [record_id for record_id, candidates in source_transitions if any(
                        candidate["old"] == change["old"] and candidate["new"] == change["new"]
                        and candidate["dimension"] == change["dimension"] for candidate in candidates)]
                    description = "增加" if direction == "increase" else "减少"
                    message = (f"明确转变 {change['display']} 按相同量纲计算为{description} {actual * 100:g}%，"
                               f"与“{claim.group()}”所称 {claimed_fraction * 100:g}% 不符。")
                    if severity == "review":
                        message += "转变与比例说法跨子句且仅有一个同主题候选，需要复核它们是否指同一量。"
                    add("relative_change_conflict", severity, message, evidence_ids)

            # Count/rate equivalence is not established by mentioning two
            # plausible-looking numbers. Require explicit shared scope AND a
            # denominator in this argument; do not infer them from other prose.
            agreement = _AGREEMENT.search(sentence)
            if (_COUNT.search(sentence) and _RATE.search(sentence) and agreement
                    and not _negated(sentence, agreement.start(), agreement.end())
                    and not (_WINDOW.search(sentence) and _POPULATION.search(sentence) and _DENOMINATOR.search(sentence))):
                evidence_ids = [record_id for record_id, content in sources
                                if _COUNT.search(content) or re.search(r"requests/s|\brps\b|请求(?:速率|量|总数)", content, re.I)
                                or (_RATE.search(content) and re.search(r"5xx|error[_ ]rate|failure[_ ]rate|错误率|失败率", content, re.I))]
                add("count_rate_scope_unverified", "review",
                    "把错误数量与错误率称为一致，但该论述没有同时明确共同时间窗口、请求范围及总请求分母；"
                    "请补充可追溯口径，或明确目前无法核对一致性。", evidence_ids)

            addition = _ADDITION.search(sentence)
            if (len(_P95.findall(sentence)) >= 2 and addition and _P95_INFERENCE.search(sentence)
                    and not _negated(sentence, addition.start(), addition.end())
                    and not re.search(r"(?:固定|恒定|constant|deterministic)\s*(?:的|为|at|is|:|=)?\s*" + _NUMBER, sentence, re.I)):
                evidence_ids = [record_id for record_id, content in sources if _P95.search(content)]
                add("p95_aggregation_unverified", "review",
                    "论述用分量 p95 与其他耗时相加解释整体 p95；分位数通常不可直接相加。"
                    "请依据同一请求的耗时求和后重新计算分位数，或明确缺少这样的证据。", evidence_ids)

    return {"version": AUDIT_VERSION, "issues": issues, "requires_review": bool(issues)}
