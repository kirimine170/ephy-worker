"""Deterministic passage selection and conservative provenance checks.

An exact quotation check establishes existence only．Semantic support is supplied
by the separate review stage，and independent origins require separate evidence．
A supporting quote is also checked deterministically against the claim text
(see ``_support_conflicts``)．A quote that drops or adds the decimal precision
of a same-unit quantity the claim states，or that is weaker than the comparison
the claim asserts，is persisted as ``context_only`` with the deterministic
rejection details and is not counted as support．The claim is downgraded only
when the remaining valid evidence no longer satisfies its status．
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from urllib.parse import urlsplit, urlunsplit

from .schema import Claim, Evidence, Extraction, OriginGroup, Passage, Review, Source

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "was",
        "what",
        "which",
        "with",
    ]
)
_USABLE = {"ok", "partial"}
_SUPPORTED = {"supported_primary", "corroborated"}


def _tokens(text: str) -> Counter[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    words = [word for word in re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", text) if word not in _STOPWORDS]
    for run in re.findall(r"[\u3040-\u30ff\u3400-\u9fff]+", text):
        words.extend(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            words.append(run)
    return Counter(words)


def select_passages(sources: list[Source], question: str, max_passages: int) -> list[Passage]:
    """Rank every extracted passage，then reserve coverage across source documents.

    This does not mark passages read．The caller records only the IDs actually
    submitted to the model，after applying its context budget．PDF page order does
    not constrain this search，so late-page conditions can outrank introductions．
    """
    if max_passages <= 0:
        return []
    passages = [
        p
        for s in sources
        if s.status in _USABLE
        for p in s.passages
        if p.source_id == s.source_id and p.text.strip()
    ]
    if not passages:
        return []
    terms = _tokens(question)
    tokenized = [_tokens(p.text) for p in passages]
    frequencies: Counter[str] = Counter()
    for tokens in tokenized:
        frequencies.update(terms.keys() & tokens.keys())
    total = len(passages)
    scores = []
    for index, tokens in enumerate(tokenized):
        length = sum(tokens.values())
        score = sum(
            (1 + math.log(tokens[t])) * math.log(1 + total / (1 + frequencies[t])) for t in terms if tokens[t]
        ) / (1 + math.log1p(length) / 8)
        scores.append((score, index))
    ranked = [index for _, index in sorted(scores, key=lambda item: (-item[0], item[1]))]
    # One strongest passage per source first，in relevance order．No domain-based
    # inference about independence is made by this diversity heuristic．
    selected: list[int] = []
    covered: set[str] = set()
    for index in ranked:
        source_id = passages[index].source_id
        if source_id not in covered:
            selected.append(index)
            covered.add(source_id)
        if len(selected) >= max_passages:
            break
    selected_set = set(selected)
    for index in ranked:
        if len(selected) >= max_passages:
            break
        if index not in selected_set:
            selected.append(index)
            selected_set.add(index)
    return [passages[index] for index in selected]


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _quote_span(text: str, quote: str) -> tuple[int, int, str] | None:
    """Map only a unique whitespace variant to a contiguous，unmodified source span."""
    if not quote.strip():
        return None
    exact = text.find(quote)
    if exact >= 0:
        return exact, exact + len(quote), "exact"
    needle = re.sub(r"\s+", " ", quote).strip()
    # A character map preserves source offsets across PDF line wraps．No case，
    # punctuation，hyphenation，numeric or cross-passage repair is permitted．
    chunks = list(re.finditer(r"\s+|\S", text))
    normalized = "".join(" " if match.group().isspace() else match.group() for match in chunks)
    first = normalized.find(needle)
    if first < 0 or normalized.find(needle, first + 1) >= 0:
        return None
    start = chunks[first].start()
    end = chunks[first + len(needle) - 1].end()
    if end - start > 500:
        return None
    return start, end, "whitespace_normalized"


def validate_extraction(
    extraction: Extraction, sources: list[Source], *, id_prefix: str = ""
) -> tuple[list[Claim], list[Evidence], list[str]]:
    """Issue Worker IDs and store only real quotes from the specified read passage.

    A unique whitespace-only alignment may recover a PDF quote's original span．
    The stored quote itself always exactly matches the original extracted text．
    """
    source_counts = Counter(s.source_id for s in sources)
    source_map = {s.source_id: s for s in sources if source_counts[s.source_id] == 1}
    passage_counts = Counter(p.passage_id for s in sources for p in s.passages)
    claims: list[Claim] = []
    evidence: list[Evidence] = []
    errors: list[str] = []
    for number, proposal in enumerate(extraction.claims, 1):
        claim_id = f"{id_prefix}c{number:03d}"
        claim = Claim(
            claim_id=claim_id,
            text=proposal.text,
            importance=proposal.importance,
            conditions=proposal.conditions,
            uncertainty=list(proposal.uncertainty),
        )
        for item in proposal.evidence:
            source = source_map.get(item.source_id)
            rejection = None
            passage = None
            span = None
            if source is None:
                rejection = "source IDが取得資料と一意に対応しません．"
            elif source.status not in _USABLE:
                rejection = f"本文を利用できない取得状態です（{source.status}）．"
            elif passage_counts[item.passage_id] != 1:
                rejection = "passage IDが抽出本文と一意に対応しません．"
            else:
                passage = next(
                    (
                        p
                        for p in source.passages
                        if p.passage_id == item.passage_id and p.source_id == source.source_id
                    ),
                    None,
                )
                if passage is None:
                    rejection = "passage IDが指定sourceに属していません．"
                elif item.passage_id not in source.read_passage_ids:
                    rejection = "モデルへ送信した読取範囲に含まれません．"
                else:
                    span = _quote_span(passage.text, item.quote)
                    if span is None:
                        rejection = "引用が該当passageの本文に一致せず，空白差だけの一意な対応もありません．"
            if rejection:
                message = f"{claim_id} / {item.source_id} / {item.passage_id}: {rejection}"
                errors.append(message)
                claim.uncertainty.append(message)
                continue
            assert passage is not None
            assert span is not None
            start, end, quote_match = span
            quote_start = passage.start + start
            item_id = f"{id_prefix}e{len(evidence) + 1:03d}"
            proposal_data = item.model_dump()
            proposal_data["quote"] = passage.text[start:end]
            evidence.append(
                Evidence(
                    **proposal_data,
                    evidence_id=item_id,
                    page=passage.page,
                    quote_start=quote_start,
                    quote_end=passage.start + end,
                    quote_match=quote_match,
                )
            )
            claim.evidence_ids.append(item_id)
        if not claim.evidence_ids:
            claim.reason = "取得・読取済み本文に一致する根拠がありません．"
        claims.append(claim)
    return claims, evidence, errors


def _origin_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username:
            return None
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port and not (
            (parsed.scheme.lower() == "https" and port == 443)
            or (parsed.scheme.lower() == "http" and port == 80)
        ):
            host += f":{port}"
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))
    except ValueError:
        return None


def group_origins(sources: list[Source]) -> list[OriginGroup]:
    """Merge exact copies and explicit origin links，never domains or model votes.

    Different hashes or different origin URLs do not prove independence．This
    minimal detector deliberately leaves every group's independence unknown．
    """
    parents = list(range(len(sources)))
    relations: list[tuple[int, int, str]] = []

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    urls = [
        {
            url
            for value in [s.requested_url, s.final_url, *s.origin_urls]
            if value and (url := _origin_url(value))
        }
        for s in sources
    ]
    for left, source in enumerate(sources):
        for right in range(left):
            other = sources[right]
            reasons = []
            if source.body_hash and source.body_hash == other.body_hash:
                reasons.append("取得本文hashが一致．")
            overlap = urls[left] & urls[right]
            if overlap:
                reasons.append("要求URL・最終URL・明示された起点URLが共通: " + ", ".join(sorted(overlap)))
            if reasons:
                parents[root(left)] = root(right)
                relations.extend((left, right, reason) for reason in reasons)
    groups: dict[int, list[int]] = {}
    for index in range(len(sources)):
        groups.setdefault(root(index), []).append(index)
    output = []
    for indexes in groups.values():
        reasons = [reason for left, right, reason in relations if left in indexes and right in indexes]
        reasons.append("資料の起点の独立性を確認する情報が不足しているためunknown．")
        origins = sorted(
            {url for index in indexes for value in sources[index].origin_urls if (url := _origin_url(value))}
        )
        output.append(
            OriginGroup(
                group_id=f"o{len(output) + 1:03d}",
                source_ids=[sources[index].source_id for index in indexes],
                origin_urls=origins,
                reasons=_unique(reasons),
                independence="unknown",
            )
        )
    return output


# Deterministic claim/quote consistency checks (semantic hardening)．
# They cover only the failure shapes observed in the Phase 1 Qwen validation，
# not general numeric or comparative semantics．
_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?")
_QUANTITY_UNIT = re.compile(r"億万|百万|倍|%|[KMGTP]?B|ms|km|kg|[KMGTP]|兆|億|万|千|[smx]", re.IGNORECASE)
_HYPHENS = frozenset({"-", "‑", "–", "―"})

# Comparison expressions ordered by asserted strength: 2 = equality，
# 3 = equal-or-better，4 = superiority．
_COMPARISON_LEVELS: dict[int, tuple[str, ...]] = {
    2: ("同等", "匹敵", "equal", "equivalent", "on par", "comparable"),
    3: (
        "同等以上",
        "同等または優れる",
        "同等または優れている",
        "同等かそれ以上",
        "equal to or better",
        "equal or better",
        "matches or exceeds",
    ),
    4: (
        "優れる",
        "優位",
        "上回る",
        "勝る",
        "凌ぐ",
        "outperform",
        "surpass",
        "better than",
        "higher than",
        "state-of-the-art",
        "state of the art",
    ),
}
# Finite explicit English negation patterns attached directly to the comparison
# predicate．This is not general English parsing：doubt or emphasis phrases
# such as "no doubt it outperforms" or "not only outperforms" stay positive．
_NEGATION_EN = re.compile(r"\b(?:does\s+not|did\s+not|can\s+not|cannot|without|never|not|no)\s+$|n't\s+$")
_NEGATION_EN_WINDOW = 12
# Explicit Japanese negation forms attached to the marker itself．Standalone 無
# or 不 are never negation: 無条件 (unconditionally) and 不具合 (defect) keep
# the comparison positive．
_NEGATION_JA = (
    "わけではない",
    "わけでもない",
    "わけがない",
    "ではない",
    "でない",
    "にすぎない",
    "しない",
    "ない",
    "ません",
    "なく",
    "ず",
)


def _quantities(text: str) -> list[tuple[str, str]]:
    """Return (normalized decimal core, unit) pairs for quantity numbers in ``text``．

    A quantity number is a digit run with an adjacent unit (B，M，%，億，万，倍，…)．
    The unit is retained so that exact value-and-unit matches can protect a claim
    quantity and precision is compared only between the same unit (no unit
    conversion)．Digits embedded in identifiers (Qwen3，A3B，v2) or in hyphenated
    compounds (Qwen3-30B-A3B，GPT-4) are not quantity numbers，so model names，
    versions，and dates never take part in the check．An ASCII unit must end at a
    boundary: ``3beta`` and ``3months`` are identifiers，while ``3ms``，``300M``，
    ``95%``，and ``3倍`` remain quantities．
    """
    normalized = unicodedata.normalize("NFKC", text)
    quantities: list[tuple[str, str]] = []
    for match in _NUMBER_TOKEN.finditer(normalized):
        start, end = match.span()
        before = normalized[start - 1] if start > 0 else ""
        if (before.isascii() and before.isalnum()) or before in _HYPHENS:
            continue
        unit = _QUANTITY_UNIT.match(normalized, end)
        if unit is None:
            continue
        after = normalized[unit.end() : unit.end() + 1]
        if after in _HYPHENS:
            continue
        # A digit or letter right after an ASCII unit means the unit is part of a
        # longer identifier (3beta，3months)．Japanese units carry no such boundary．
        if unit.group().isascii() and after.isascii() and after.isalnum():
            continue
        core = match.group()
        if "." in core:
            core = core.rstrip("0").rstrip(".")
        quantities.append((core, unit.group()))
    return quantities


def _precision_conflict(claim_core: str, quote_core: str) -> bool:
    """True when one core is the other with decimal digits added or dropped．

    ``30`` vs ``30.5`` (the claim rounds 30.5B to 30B) and ``3.3`` vs ``3``
    (the claim adds precision the quote does not state) both conflict．
    Unrelated values such as ``30`` vs ``3.3`` or ``95`` vs ``90`` are outside
    this narrow check and are left to the semantic review stage．
    """
    if claim_core == quote_core:
        return False
    return quote_core.startswith(claim_core + ".") or claim_core.startswith(quote_core + ".")


def _marker_spans(text: str, marker: str) -> list[tuple[int, int]]:
    # ASCII markers also match common inflections (outperforms，surpassed，…)
    # without reaching unrelated words such as "equality"．
    pattern = rf"\b{re.escape(marker)}(?:s|es|ed|ing|d)?\b" if marker.isascii() else re.escape(marker)
    return [match.span() for match in re.finditer(pattern, text)]


def _negated(text: str, span: tuple[int, int]) -> bool:
    """True when a narrow explicit negation attaches to the marker span．

    English: a finite pattern set directly before the predicate (does not，
    did not，cannot，can not，never，not，no，without，n't)．This is not
    general English parsing，so "no doubt it outperforms" stays positive．
    Japanese: an explicit form attached within two characters after the
    marker (上回らない，匹敵しない，同等ではない，上回るわけではない)，or the
    prefix 非 directly before it (非同等)．Unrelated 無 or 不 never count．
    """
    start, end = span
    before = text[max(0, start - _NEGATION_EN_WINDOW) : start]
    if _NEGATION_EN.search(before):
        return True
    if start > 0 and text[start - 1] == "非":
        return True
    tail = text[end : end + 6]
    return any(tail[offset:].startswith(_NEGATION_JA) for offset in range(3))


def _comparison_level(text: str) -> int:
    """Return the strongest tracked comparison expression level in ``text``．

    0 means no tracked expression．All marker spans are collected first and
    overlaps are resolved by preferring the longest span，so the complete
    composites 同等または優れる／同等または優れている and "matches or exceeds"
    keep level 3 instead of being raised by a nested marker．Negation is then
    applied only to the selected complete span，so a negated composite removes
    its nested 同等／優れる markers as well．
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    spans: list[tuple[int, int, int]] = []
    for level, markers in _COMPARISON_LEVELS.items():
        for marker in markers:
            for span in _marker_spans(normalized, marker):
                spans.append((span[0], span[1], level))
    spans.sort(key=lambda item: (item[0] - item[1], item[0]))
    chosen: list[tuple[int, int, int]] = []
    for item in spans:
        if all(item[1] <= start or item[0] >= end for start, end, _ in chosen):
            chosen.append(item)
    kept = [level for start, end, level in chosen if not _negated(normalized, (start, end))]
    return max(kept, default=0)


def _support_conflicts(claim_text: str, quote_text: str) -> list[str]:
    """Deterministic consistency problems between a claim and its support quote．

    Returns human-readable details．An empty result does not assert that the
    quote fully supports the claim．
    """
    details: list[str] = []
    claim_quantities = _quantities(claim_text)
    quote_quantities = _quantities(quote_text)
    if claim_quantities and quote_quantities:
        quote_exact = {(core, unit.casefold()) for core, unit in quote_quantities}
        for core, unit in claim_quantities:
            # An exact value-and-unit match in the quote protects this claim
            # quantity from unrelated quote quantities．
            if (core, unit.casefold()) in quote_exact:
                continue
            for quote_core, quote_unit in quote_quantities:
                if quote_unit.casefold() != unit.casefold():
                    continue
                if _precision_conflict(core, quote_core):
                    details.append(
                        f"数値の精度が一致しません（claim「{core}{unit}」に対し引用は「{quote_core}{quote_unit}」）．"
                        "引用の数値を丸めたり精度を加えたりしたclaimはsupportとして扱いません．"
                    )
    claim_level = _comparison_level(claim_text)
    if claim_level and claim_level > _comparison_level(quote_text):
        details.append(
            "claimが引用より強い比較表現（同等・優位など）を使用しています．"
            "引用が同じ以上の強さの比較を示さないため，supportとして扱いません．"
        )
    return list(dict.fromkeys(details))


def apply_review(
    claims: list[Claim], evidence: list[Evidence], review: Review, groups: list[OriginGroup]
) -> list[Claim]:
    """Apply a separate semantic review only when its ID coverage is exact.

    Evidence is updated in place with the accepted check's relation and reason．
    Claim states are additionally gated in code，especially corroboration and
    inference．A review request cannot override missing or conflicting evidence．
    """
    claim_ids = Counter(c.claim_id for c in claims)
    evidence_ids = Counter(e.evidence_id for e in evidence)
    evidence_owners = Counter(key for claim in claims for key in claim.evidence_ids)
    evidence_map = {e.evidence_id: e for e in evidence if evidence_ids[e.evidence_id] == 1}
    check_ids = Counter(c.claim_id for c in review.checks)
    check_map = {c.claim_id: c for c in review.checks if check_ids[c.claim_id] == 1}
    global_error = any(key not in claim_ids or count != 1 for key, count in check_ids.items())
    group_ids = Counter(g.group_id for g in groups)
    memberships: dict[str, list[OriginGroup]] = {}
    for group in groups:
        for source_id in group.source_ids:
            memberships.setdefault(source_id, []).append(group)
    for item in evidence:
        item.checked = False
        item.check_reason = None
        item.conditions_match = None
        item.is_primary = None
    result: list[Claim] = []
    for original in claims:
        claim = original.model_copy(deep=True)
        claim.status = "insufficient"
        claim.checked = False
        claim.inference_from = []
        check = check_map.get(claim.claim_id)
        if global_error or claim_ids[claim.claim_id] != 1 or check is None:
            claim.reason = "照合結果のclaim IDが不明・重複・欠落しているため採用できません．"
            result.append(claim)
            continue
        checked_ids = [item.evidence_id for item in check.evidence_checks]
        if (
            len(checked_ids) != len(set(checked_ids))
            or len(claim.evidence_ids) != len(set(claim.evidence_ids))
            or set(checked_ids) != set(claim.evidence_ids)
            or any(item_id not in evidence_map or evidence_owners[item_id] != 1 for item_id in checked_ids)
        ):
            claim.reason = "照合結果がclaimの全evidence IDと一意に対応しないため採用できません．"
            result.append(claim)
            continue
        if not claim.evidence_ids and check.status != "inference":
            claim.reason = (
                "今回の読取範囲で，本文に一致する有効な引用を確保できませんでした．"
                "資料全体の記載の有無は判定していません．"
            )
            result.append(claim)
            continue
        claim.checked = True
        claim.reason = check.reason
        claim.conditions = check.conditions or claim.conditions
        claim.uncertainty = _unique([*claim.uncertainty, *check.uncertainty])
        supporting: list[Evidence] = []
        contradicting: list[Evidence] = []
        support_conflicts: list[str] = []
        for item_check in check.evidence_checks:
            item = evidence_map[item_check.evidence_id]
            item.relation = item_check.relation
            item.checked = True
            item.check_reason = item_check.reason
            item.conditions_match = item_check.conditions_match
            item.is_primary = item_check.is_primary
            if not item_check.conditions_match:
                claim.uncertainty.append(
                    f"{item.evidence_id}: 条件・日時・version等が不一致．{item_check.reason}"
                )
            elif item_check.relation == "supports":
                details = _support_conflicts(claim.text, item.quote)
                if details:
                    # The deterministic guard overrides the review relation so the
                    # item is persisted and rendered as context，not support．
                    item.relation = "context_only"
                    item.check_reason = f"{item_check.reason}{''.join(details)}"
                    for detail in details:
                        claim.uncertainty.append(f"{item.evidence_id}: {detail}")
                    support_conflicts.extend(details)
                else:
                    supporting.append(item)
            elif item_check.relation == "contradicts":
                contradicting.append(item)
        if support_conflicts:
            claim.reason += (
                " 決定的整合性チェックでclaimと引用の間に数値の精度差または比較表現の強さの差を検出したため，"
                "該当のevidenceをsupportとして計りません．"
            )
        primary = any(item.is_primary for item in supporting)
        confirmed = set()
        for item in supporting:
            member = memberships.get(item.source_id, [])
            if (
                len(member) == 1
                and group_ids[member[0].group_id] == 1
                and member[0].independence == "confirmed"
            ):
                confirmed.add(member[0].group_id)
        if supporting and contradicting:
            claim.status = "conflicting"
            claim.reason += " 条件が一致する支持根拠と反対根拠があり，矛盾を未解決として保持します．"
        elif check.status == "inference":
            claim.inference_from = list(check.inference_from)
            # Resolve against checked source claims after all regular checks．
        elif check.status == "refuted" and contradicting and not supporting:
            claim.status = "refuted"
        elif check.status in _SUPPORTED and supporting and not contradicting:
            if check.status == "corroborated" and len(confirmed) >= 2:
                claim.status = "corroborated"
            elif primary:
                claim.status = "supported_primary"
                if check.status == "corroborated":
                    claim.reason += " 独立した複数起点は未確認であり，一次資料による支持までとします．"
            else:
                claim.reason += " 一次資料による支持または複数の確認済み独立起点が不足しています．"
        elif check.status != "insufficient":
            claim.reason += " 指定状態を満たす条件一致の根拠が不足しています．"
        result.append(claim)
    resolved = {c.claim_id: c for c in result}
    for claim in result:
        check = check_map.get(claim.claim_id)
        if not claim.checked or check is None or check.status != "inference" or claim.status == "conflicting":
            continue
        refs = claim.inference_from
        if (
            refs
            and len(refs) == len(set(refs))
            and claim.claim_id not in refs
            and all(
                ref in resolved
                and resolved[ref].checked
                and resolved[ref].status in _SUPPORTED
                and resolved[ref].evidence_ids
                for ref in refs
            )
        ):
            claim.status = "inference"
        else:
            claim.inference_from = []
            claim.reason += " 推論元が確認済みの支持されたclaimに解決できません．"
    return result
