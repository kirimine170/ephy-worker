"""Offline acceptance checks for quotation provenance and conservative review."""

import pytest

from ephy_worker.evidence import apply_review, group_origins, select_passages, validate_extraction
from ephy_worker.report import render_markdown
from ephy_worker.schema import (
    Claim,
    ClaimCheck,
    ClaimProposal,
    EvidenceCheck,
    EvidenceProposal,
    Extraction,
    OriginGroup,
    Passage,
    Report,
    Review,
    Source,
)


def source(
    source_id="s1", text="Release 2 supports streaming only on Linux.", *, page=None, read=True, **kwargs
):
    passage = Passage(
        passage_id=f"{source_id}p1", source_id=source_id, text=text, page=page, start=40, end=40 + len(text)
    )
    return Source(
        source_id=source_id,
        requested_url=f"https://{source_id}.example/document",
        kind="pdf" if page else "html",
        status="ok",
        passages=[passage],
        read_passage_ids=[passage.passage_id] if read else [],
        **kwargs,
    )


def extract(sources, *, relation="supports", text="Release 2 supports streaming on Linux."):
    proposals = [
        EvidenceProposal(
            source_id=s.source_id,
            passage_id=s.passages[0].passage_id,
            quote=s.passages[0].text,
            relation=relation,
        )
        for s in sources
    ]
    return validate_extraction(Extraction(claims=[ClaimProposal(text=text, evidence=proposals)]), sources)


def check(
    claim, evidence, *, status="supported_primary", match=True, primary=True, relations=None, refs=None
):
    return ClaimCheck(
        claim_id=claim.claim_id,
        status=status,
        reason="本文の対象versionとLinuxの条件を照合した．",
        evidence_checks=[
            EvidenceCheck(
                evidence_id=e.evidence_id,
                relation=relations[i] if relations else e.relation,
                conditions_match=match,
                is_primary=primary,
                reason="Release 2かつLinuxに限定された本文．",
            )
            for i, e in enumerate(evidence)
        ],
        inference_from=refs or [],
    )


def test_exact_pdf_quote_has_worker_ids_and_physical_page_offsets():
    src = source(text="Introduction. Only release 2 is supported.", page=73)
    extraction = Extraction(
        claims=[
            ClaimProposal(
                text="Release 2 is supported.",
                evidence=[
                    EvidenceProposal(
                        source_id="s1",
                        passage_id="s1p1",
                        quote="Only release 2 is supported.",
                        relation="supports",
                    )
                ],
            )
        ]
    )
    claims, evidence, errors = validate_extraction(extraction, [src], id_prefix="r2-")
    assert errors == []
    assert claims[0].claim_id == "r2-c001"
    assert claims[0].status == "insufficient"
    assert not claims[0].checked
    assert evidence[0].evidence_id == "r2-e001"
    assert evidence[0].page == 73
    assert evidence[0].quote_start == 54
    assert evidence[0].quote_end == 82


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "invented"},
        {"passage_id": "invented"},
        {"quote": "Release 2 supports all platforms."},
        {"quote": "release 2 supports streaming only on Linux."},
        {"quote": "   "},
    ],
)
def test_fabricated_or_nonexact_evidence_is_rejected(changes):
    src = source()
    proposal = {
        "source_id": "s1",
        "passage_id": "s1p1",
        "quote": src.passages[0].text,
        "relation": "supports",
    }
    proposal.update(changes)
    claims, evidence, errors = validate_extraction(
        Extraction(claims=[ClaimProposal(text="A proposed fact.", evidence=[EvidenceProposal(**proposal)])]),
        [src],
    )
    assert not evidence
    assert errors
    assert claims[0].status == "insufficient"
    assert claims[0].uncertainty


def test_unread_passage_and_other_source_passage_rejected():
    unread = source(read=False)
    _claims, evidence, errors = extract([unread])
    assert not evidence and "読取範囲" in errors[0]
    other = source("s2")
    extraction = Extraction(
        claims=[
            ClaimProposal(
                text="A proposed fact.",
                evidence=[
                    EvidenceProposal(
                        source_id="s1", passage_id="s2p1", quote=other.passages[0].text, relation="supports"
                    )
                ],
            )
        ]
    )
    _, evidence, errors = validate_extraction(extraction, [unread, other])
    assert not evidence and "属していません" in errors[0]


@pytest.mark.parametrize("status", ["blocked", "http_error", "image_only", "extraction_failed"])
def test_unavailable_body_cannot_confirm_snippet_claim(status):
    src = source()
    src.status = status
    claims, evidence, errors = extract([src])
    reviewed = apply_review(
        claims, evidence, Review(checks=[check(claims[0], evidence)]), group_origins([src])
    )
    assert not evidence and errors
    assert reviewed[0].status == "insufficient"


def test_partial_pdf_body_can_support_only_actually_read_page():
    src = source(page=8)
    src.status = "partial"
    src.page_statuses = {"1": "image_only", "8": "ok"}
    claims, evidence, errors = extract([src])
    assert not errors
    result = apply_review(claims, evidence, Review(checks=[check(claims[0], evidence)]), group_origins([src]))
    assert result[0].status == "supported_primary"


def test_ranking_searches_pdf_late_pages_and_preserves_source_diversity():
    pdf = source(page=1)
    pdf.passages = [
        Passage(
            passage_id=f"s1p{page}",
            source_id="s1",
            page=page,
            text="Background introduction unrelated to streaming."
            if page != 80
            else "Release 2 streaming limitations: Linux only.",
        )
        for page in range(1, 91)
    ]
    html = source("s2", "Streaming specification of release 2.")
    pdf.read_passage_ids = []
    selected = select_passages([pdf, html], "release 2 Linux limitations", 2)
    assert selected[0].page == 80
    assert {p.source_id for p in selected} == {"s1", "s2"}
    assert pdf.read_passage_ids == []
    assert select_passages([pdf], "Linux limitations", 1)[0].page == 80


def test_japanese_ranking_and_zero_budget():
    src = source(text="前書きと歴史．")
    src.passages.append(
        Passage(passage_id="s1p2", source_id="s1", text="取消機能の制約は接続の終了のみである．")
    )
    assert select_passages([src], "取消機能の制約", 1)[0].passage_id == "s1p2"
    assert select_passages([src], "取消", 0) == []


def test_duplicate_hash_and_explicit_origin_links_merge_transitively():
    original = source("s1", body_hash="same")
    copy = source("s2", body_hash="same")
    repost = source("s3", origin_urls=[copy.requested_url + "#attribution"])
    groups = group_origins([original, copy, repost])
    assert len(groups) == 1
    assert groups[0].source_ids == ["s1", "s2", "s3"]
    assert groups[0].independence == "unknown"
    assert any("hash" in reason for reason in groups[0].reasons)


def test_common_original_link_groups_copy_domains_without_original_fetched():
    sources = [source(f"s{i}", origin_urls=["https://original.example/research"]) for i in range(1, 4)]
    assert len(group_origins(sources)) == 1


def test_domain_or_different_hash_never_confirms_independence():
    first = source("s1", body_hash="first")
    second = source("s2", body_hash="second")
    second.requested_url = "https://s1.example/different-research"
    third = source("s3", body_hash="third")
    groups = group_origins([first, second, third])
    assert len(groups) == 3
    assert all(g.independence == "unknown" for g in groups)


def test_conditions_mismatch_cannot_become_confirmed_support():
    src = source()
    claims, evidence, _ = extract([src], text="Release 1 supports streaming on Windows.")
    reviewed = apply_review(claims, evidence, Review(checks=[check(claims[0], evidence, match=False)]), [])
    assert reviewed[0].checked
    assert reviewed[0].status == "insufficient"
    assert any("不一致" in value for value in reviewed[0].uncertainty)
    assert evidence[0].checked and evidence[0].conditions_match is False


def test_primary_review_is_required_beyond_exact_quotation():
    src = source()
    claims, evidence, _ = extract([src])
    assert claims[0].status == "insufficient"
    reviewed = apply_review(claims, evidence, Review(checks=[check(claims[0], evidence, primary=False)]), [])
    assert reviewed[0].status == "insufficient"
    reviewed = apply_review(claims, evidence, Review(checks=[check(claims[0], evidence)]), [])
    assert reviewed[0].status == "supported_primary"
    assert evidence[0].is_primary is True


@pytest.mark.parametrize("mode", ["missing", "duplicate", "fabricated", "unknown_claim"])
def test_semantic_review_requires_exact_id_coverage(mode):
    claims, evidence, _ = extract([source()])
    item = check(claims[0], evidence)
    if mode == "missing":
        item.evidence_checks = []
    elif mode == "duplicate":
        item.evidence_checks *= 2
    elif mode == "fabricated":
        item.evidence_checks[0].evidence_id = "e999"
    else:
        item.claim_id = "c999"
    reviewed = apply_review(claims, evidence, Review(checks=[item]), [])
    assert reviewed[0].status == "insufficient"
    assert not reviewed[0].checked
    assert not evidence[0].checked


def test_corroboration_requires_multiple_confirmed_origins():
    sources = [source("s1"), source("s2")]
    claims, evidence, _ = extract(sources)
    review = Review(checks=[check(claims[0], evidence, status="corroborated", primary=False)])
    groups = group_origins(sources)
    assert apply_review(claims, evidence, review, groups)[0].status == "insufficient"
    for group in groups:
        group.independence = "confirmed"
    assert apply_review(claims, evidence, review, groups)[0].status == "corroborated"
    merged = [
        OriginGroup(group_id="o1", source_ids=["s1", "s2"], reasons=["同じ起点．"], independence="confirmed")
    ]
    assert apply_review(claims, evidence, review, merged)[0].status == "insufficient"
    groups.append(groups[0].model_copy(deep=True))
    assert apply_review(claims, evidence, review, groups)[0].status == "insufficient"


def test_unresolved_contradiction_overrides_model_majority_or_primary_vote():
    sources = [source("s1"), source("s2"), source("s3", "Release 2 does not support streaming on Linux.")]
    claims, evidence, _ = extract(sources)
    review = Review(checks=[check(claims[0], evidence, relations=["supports", "supports", "contradicts"])])
    reviewed = apply_review(claims, evidence, review, group_origins(sources))
    assert reviewed[0].status == "conflicting"
    assert evidence[2].relation == "contradicts"
    report = Report(
        job_id="test",
        question="公開の質問",
        profile_id="fixture",
        sources=sources,
        evidence=evidence,
        claims=reviewed,
        state="completed",
    )
    md = render_markdown(report)
    assert "矛盾が未解決" in md
    assert "e003：反対" in md
    assert all(s.requested_url in md for s in sources)


def test_refutation_requires_matching_contradiction():
    claims, evidence, _ = extract([source()], relation="contradicts")
    review = Review(checks=[check(claims[0], evidence, status="refuted")])
    assert apply_review(claims, evidence, review, [])[0].status == "refuted"


def test_inference_requires_checked_supported_root_claims_and_preserves_order():
    claims, evidence, _ = extract([source()])
    inferred = Claim(claim_id="c002", text="Linuxの条件を先に確認することを提案する．")
    checks = [check(claims[0], evidence), check(inferred, [], status="inference", refs=["c001"])]
    result = apply_review([inferred, *claims], evidence, Review(checks=checks), [])
    assert result[0].status == "inference"
    assert result[0].inference_from == ["c001"]
    for refs in [["invented"], ["c002"], [], ["c001", "c001"]]:
        checks[1].inference_from = refs
        assert (
            apply_review([*claims, inferred], evidence, Review(checks=checks), [])[1].status == "insufficient"
        )
    checks[1].inference_from = ["c001"]
    checks[0].evidence_checks[0].conditions_match = False
    assert apply_review([*claims, inferred], evidence, Review(checks=checks), [])[1].status == "insufficient"


def test_markdown_is_deterministic_links_physical_page_and_shows_partial_failures():
    src = source(page=80)
    src.kind = "pdf"
    src.page_count = 90
    src.status = "partial"
    src.page_statuses = {"1": "image_only", "80": "ok"}
    src.limitations = ["画像ページのOCRは未実施．"]
    claims, evidence, _ = extract([src])
    claims = apply_review(claims, evidence, Review(checks=[check(claims[0], evidence)]), [])
    report = Report(
        job_id="fixture",
        question="条件は何か",
        profile_id="test",
        sources=[src],
        evidence=evidence,
        claims=claims,
        state="partial",
        stop_reason="fetch_requests",
        gaps=["全ページは未確認．"],
        failures=[{"stage": "fetch", "status": 403}],
    )
    rendered = render_markdown(report)
    assert render_markdown(report) == rendered
    assert "#page=80" in rendered and "物理p.80" in rendered
    assert "部分終了" in rendered and "fetch\\_requests" in rendered
    assert "image\\_only" in rendered and "403" in rendered
    assert "全ページは未確認" in rendered
    assert "モデルへ送信したpassage数：1" in rendered
    assert "独立検証ではありません" in rendered


def test_renderer_does_not_invent_citations_or_promote_unchecked_summary():
    src = source()
    claims, evidence, _ = extract([src])
    claims[0].status = "supported_primary"
    report = Report(
        job_id="fixture", question="質問", profile_id="test", sources=[src], claims=claims, evidence=evidence
    )
    rendered = render_markdown(report)
    assert "確認済みの主要主張はありません" in rendered
    assert "未照合の候補" in rendered
    evidence[0].source_id = "invented"
    rendered = render_markdown(report)
    assert "整合性警告" in rendered
    assert "> Release 2" not in rendered


def test_renderer_escapes_untrusted_markdown_and_avoids_non_http_links():
    src = source()
    src.title = "[claim](javascript:alert(1)) <script>"
    src.final_url = "javascript:alert(1)"
    report = Report(job_id="fixture", question="[click](file:///secret)", profile_id="test", sources=[src])
    rendered = render_markdown(report)
    assert "&lt;script&gt;" in rendered
    assert "[claim](javascript:" not in rendered
    assert "URLをリンクできません" in rendered


def test_renderer_marks_supplemental_pdf_and_redirect_provenance():
    src = source(page=20, supplied=True)
    src.final_url = "https://cdn.example/research.pdf"
    report = Report(job_id="fixture", question="質問", profile_id="test", sources=[src])
    rendered = render_markdown(report)
    assert "実行者が指定した補足公開資料" in rendered
    assert "検索による発見ではありません" in rendered
    assert src.requested_url in rendered and src.final_url in rendered
    assert "要求URL" in rendered and "最終URL" in rendered


def test_evidence_shared_between_different_claim_reviews_is_rejected():
    claims, evidence, _ = extract([source()])
    second = claims[0].model_copy(update={"claim_id": "c002"}, deep=True)
    review = Review(checks=[check(claims[0], evidence), check(second, evidence, relations=["contradicts"])])
    result = apply_review([*claims, second], evidence, review, [])
    assert all(c.status == "insufficient" and not c.checked for c in result)
    assert not evidence[0].checked


def test_whitespace_alignment_saves_original_pdf_text_and_offsets():
    text = "Preface. Temperature 0.6,\n\t top-p  0.95 and top-k 20. Ending."
    src = source(text=text, page=13)
    proposal = "Temperature 0.6, top-p 0.95 and top-k 20."
    claims, evidence, errors = validate_extraction(
        Extraction(
            claims=[
                ClaimProposal(
                    text="Sampling values.",
                    evidence=[
                        EvidenceProposal(
                            source_id="s1", passage_id="s1p1", quote=proposal, relation="supports"
                        )
                    ],
                )
            ]
        ),
        [src],
    )
    assert not errors and claims[0].evidence_ids
    item = evidence[0]
    assert item.quote == "Temperature 0.6,\n\t top-p  0.95 and top-k 20."
    assert item.quote != proposal and item.quote_match == "whitespace_normalized"
    assert text[item.quote_start - 40 : item.quote_end - 40] == item.quote
    assert item.page == 13


@pytest.mark.parametrize(
    "proposal",
    [
        "Temperature 0.7, top-p 0.95.",
        "Temperature 0.6; top-p 0.95.",
        "temperature 0.6, top-p 0.95.",
        "Temperature 0.6, topp 0.95.",
    ],
)
def test_whitespace_alignment_never_repairs_numbers_punctuation_case_or_hyphens(proposal):
    src = source(text="Temperature 0.6,\n top-p 0.95.", page=13)
    _, evidence, errors = validate_extraction(
        Extraction(
            claims=[
                ClaimProposal(
                    text="Sampling values.",
                    evidence=[
                        EvidenceProposal(
                            source_id="s1", passage_id="s1p1", quote=proposal, relation="supports"
                        )
                    ],
                )
            ]
        ),
        [src],
    )
    assert not evidence and errors


def test_ambiguous_whitespace_alignment_and_oversized_recovered_quote_rejected():
    for text, quote in [
        ("value\n 0.6; value\t0.6;", "value 0.6"),
        ("value" + "\n" * 500 + "0.6", "value 0.6"),
    ]:
        src = source(text=text, page=13)
        _, evidence, errors = validate_extraction(
            Extraction(
                claims=[
                    ClaimProposal(
                        text="Value.",
                        evidence=[
                            EvidenceProposal(
                                source_id="s1", passage_id="s1p1", quote=quote, relation="supports"
                            )
                        ],
                    )
                ]
            ),
            [src],
        )
        assert not evidence and errors


def test_whitespace_alignment_never_reanchors_to_another_passage():
    src = source(text="A different paragraph.", page=12)
    src.passages.append(Passage(source_id="s1", passage_id="s1p2", text="Temperature\n 0.6.", page=13))
    src.read_passage_ids.append("s1p2")
    _, evidence, errors = validate_extraction(
        Extraction(
            claims=[
                ClaimProposal(
                    text="Temperature.",
                    evidence=[
                        EvidenceProposal(
                            source_id="s1", passage_id="s1p1", quote="Temperature 0.6.", relation="supports"
                        )
                    ],
                )
            ]
        ),
        [src],
    )
    assert not evidence and errors


def test_no_evidence_cannot_repeat_model_claim_of_absence():
    claim = Claim(claim_id="c001", text="Sampling values are specified.")
    review = Review(
        checks=[ClaimCheck(claim_id="c001", status="insufficient", reason="資料には記載が存在しない．")]
    )
    result = apply_review([claim], [], review, [])
    assert result[0].status == "insufficient" and not result[0].checked
    assert "資料全体の記載の有無は判定していません" in result[0].reason
    assert "記載が存在しない" not in result[0].reason
    rendered = render_markdown(Report(job_id="fixture", question="質問", profile_id="fixture", claims=result))
    assert "記載が存在しない" not in rendered
