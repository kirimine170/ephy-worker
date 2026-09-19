"""Render the canonical report deterministically，without a final generation call."""

from __future__ import annotations

import json
import re
from collections import Counter
from urllib.parse import quote, urlsplit, urlunsplit

from .schema import Claim, Evidence, Report, Source

_STATUS = {
    "supported_primary": "一次資料で支持（独立検証ではありません）",
    "corroborated": "確認済みの複数独立起点が支持",
    "conflicting": "矛盾が未解決",
    "insufficient": "根拠不足・未確認",
    "refuted": "該当条件で反証",
    "inference": "確認済み主張からの推論",
}
_RELATION = {"supports": "支持", "contradicts": "反対", "context_only": "背景のみ"}
_STATE = {
    "running": "実行中",
    "completed": "処理終了",
    "partial": "部分終了",
    "failed": "失敗",
    "cancelled": "取消",
}
_ANSWERABILITY = {"sufficient": "回答可能", "limited": "限定的", "unresolved": "未解決"}


def _text(value: object) -> str:
    """Keep generated text and source titles inert inside Markdown."""
    value = str(value).replace("\r", " ").replace("\n", " ")
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", value)


def _source_link(source: Source, label: str, page: int | None = None) -> str:
    url = source.final_url or source.requested_url
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            return _text(label) + "（URLをリンクできません）"
        if any(ord(char) < 32 or ord(char) == 127 for char in url):
            return _text(label) + "（URLをリンクできません）"
        if page is not None:
            if page < 1:
                return _text(label) + "（物理ページ番号が不正）"
            parsed = parsed._replace(fragment=f"page={page}")
        safe_url = quote(urlunsplit(parsed), safe=":/?#[]@!$&'*+,;=%")
        return f"[{_text(label)}](<{safe_url}>)"
    except ValueError:
        return _text(label) + "（URLをリンクできません）"


def _evidence_is_real(item: Evidence, sources: dict[str, Source], passage_counts: Counter) -> bool:
    source = sources.get(item.source_id)
    if source is None or source.status not in {"ok", "partial"}:
        return False
    if item.passage_id not in source.read_passage_ids or passage_counts[item.passage_id] != 1:
        return False
    passage = next(
        (p for p in source.passages if p.passage_id == item.passage_id and p.source_id == item.source_id),
        None,
    )
    if passage is None or passage.page != item.page or not item.quote.strip():
        return False
    start = item.quote_start - passage.start
    end = item.quote_end - passage.start
    return 0 <= start < end <= len(passage.text) and passage.text[start:end] == item.quote


def render_markdown(report: Report) -> str:
    """Use claim IDs for every conclusion and only Worker-resolved citation URLs."""
    source_counts = Counter(s.source_id for s in report.sources)
    sources = {s.source_id: s for s in report.sources if source_counts[s.source_id] == 1}
    evidence_counts = Counter(e.evidence_id for e in report.evidence)
    passage_counts = Counter(p.passage_id for s in report.sources for p in s.passages)
    evidence = {
        e.evidence_id: e
        for e in report.evidence
        if evidence_counts[e.evidence_id] == 1 and _evidence_is_real(e, sources, passage_counts)
    }
    claim_counts = Counter(c.claim_id for c in report.claims)
    claims = {c.claim_id: c for c in report.claims if claim_counts[c.claim_id] == 1}

    def claim_evidence(claim: Claim) -> list[Evidence]:
        return [evidence[key] for key in claim.evidence_ids if key in evidence]

    def qualifying(claim: Claim) -> list[Evidence]:
        """Checked，conditions-matching evidence with the relation the status
        rests on；empty for every other status．"""
        items = [e for e in claim_evidence(claim) if e.checked and e.conditions_match]
        if claim.status in {"supported_primary", "corroborated"}:
            return [e for e in items if e.relation == "supports"]
        if claim.status == "refuted":
            return [e for e in items if e.relation == "contradicts"]
        return []

    def citations(claim: Claim) -> str:
        # Unlabelled citations beside a confirmed conclusion show only the
        # qualifying evidence．Rejected context stays visibly labelled in the
        # detailed evidence list，not next to the conclusion．
        items = qualifying(claim) if confirmed(claim) else claim_evidence(claim)
        entries = []
        for item in items:
            source = sources[item.source_id]
            label = f"{source.source_id} / {item.passage_id}"
            if item.page is not None:
                label += f" / 物理p.{item.page}"
            entries.append(_source_link(source, label, item.page))
        return "，".join(dict.fromkeys(entries))

    def confirmed(claim: Claim) -> bool:
        return claim.checked and claim_counts[claim.claim_id] == 1 and bool(qualifying(claim))

    lines = [
        "# 調査レポート",
        "",
        f"質問：{_text(report.question)}",
        "",
        f"Job ID：{_text(report.job_id)}  ",
        f"Profile：{_text(report.profile_id)}  ",
        f"状態：{_STATE[report.state]} ／ 回答範囲：{_ANSWERABILITY[report.answerability]}  ",
        f"停止理由：{_text(report.stop_reason)}  ",
        f"作成日時：{_text(report.created_at)}  ",
        f"終了日時：{_text(report.finished_at or '未終了')}",
        "",
        "## 結論と確認範囲",
        "",
    ]
    major = [c for c in report.claims if c.importance == "major" and confirmed(c)]
    if major:
        for claim in major:
            statement = (
                f"仮説「{_text(claim.text)}」は該当条件で反証されています．"
                if claim.status == "refuted"
                else _text(claim.text)
            )
            lines.append(
                f"- **{_text(claim.claim_id)}／{_STATUS[claim.status]}**：{statement} {citations(claim)}"
            )
            if claim.conditions:
                lines.append("  適用条件：" + _text(claim.conditions))
    else:
        lines.append("確認済みの主要主張はありません．以下の候補・根拠不足・取得状態を参照してください．")
    if report.state != "completed":
        lines.extend(["", "調査は完了していません．保存時点で検査済みの情報を示しています．"])
    lines.extend(
        [
            "",
            (
                "本文の引用の実在はコードで検査し，意味・条件は別のモデル照合で点検しています．"
                "モデルによる照合は利用者による確認ではありません．"
            ),
            "",
            "## 主張と根拠",
            "",
        ]
    )
    if not report.claims:
        lines.append("検証対象の主張を取得できませんでした．")
    quotation_budget: Counter[str] = Counter()
    for claim in report.claims:
        lines.extend([f"### {_text(claim.claim_id)}：{_STATUS[claim.status]}", ""])
        prefix = "検証対象：" if claim.checked else "未照合の候補："
        lines.extend(
            [
                f"{prefix}{_text(claim.text)} {citations(claim)}".rstrip(),
                "",
                f"判定理由：{_text(claim.reason)}  ",
                f"照合段階：{'実施済み' if claim.checked else '未完了'}",
            ]
        )
        if claim.conditions:
            lines.extend(["", f"適用条件・日時・version：{_text(claim.conditions)}"])
        if claim.inference_from:
            refs = [key for key in claim.inference_from if key in claims and confirmed(claims[key])]
            lines.extend(
                ["", "推論元の確認済みclaim ID：" + ("，".join(map(_text, refs)) or "解決できません．")]
            )
        valid_items = claim_evidence(claim)
        if len(valid_items) != len(claim.evidence_ids):
            lines.extend(
                [
                    "",
                    "整合性警告：実在する読取済み本文へ解決できない根拠IDがあり，該当引用は表示していません．",
                ]
            )
        if not valid_items:
            lines.extend(["", "取得・読取済み本文から表示できる根拠はありません．"])
        for item in valid_items:
            label = f"{item.evidence_id}：{_RELATION[item.relation]} ／ {item.source_id} / {item.passage_id}"
            if item.page is not None:
                label += f" / 物理p.{item.page}"
            lines.extend(["", "- " + _source_link(sources[item.source_id], label, item.page)])
            available = min(180, 700 - quotation_budget[item.source_id])
            if available > 0:
                excerpt = item.quote[:available]
                quotation_budget[item.source_id] += len(excerpt)
                if len(excerpt) < len(item.quote):
                    excerpt += "…（引用の表示を省略）"
                lines.extend(["", f"> {_text(excerpt)}"])
            else:
                lines.extend(
                    ["", "引用の表示量上限に達しました．根拠位置は上記リンクとreport.jsonに記録しています．"]
                )
            condition = (
                "未照合" if item.conditions_match is None else "一致" if item.conditions_match else "不一致"
            )
            lines.extend(
                [
                    "",
                    (
                        f"  本文内文字位置：{item.quote_start}–{item.quote_end} ／ 条件：{condition} ／ "
                        f"意味の照合：{'済み' if item.checked else '未完了'}"
                    ),
                ]
            )
            if item.conditions:
                lines.append(f"  根拠の適用条件：{_text(item.conditions)}")
            if item.check_reason:
                lines.append(f"  照合理由：{_text(item.check_reason)}")
        if claim.uncertainty:
            lines.extend(["", "未確認点・条件差：", ""])
            lines.extend(f"- {_text(value)}" for value in claim.uncertainty)
        lines.append("")
    lines.extend(["## 矛盾・未確認点", ""])
    unresolved = [c for c in report.claims if c.status in {"conflicting", "insufficient"} or not c.checked]
    if unresolved:
        lines.extend(f"- {_text(c.claim_id)}：{_STATUS[c.status]}．{_text(c.reason)}" for c in unresolved)
    if report.gaps:
        lines.extend(f"- {_text(value)}" for value in report.gaps)
    if not unresolved and not report.gaps:
        lines.append("記録された未解決項目はありません．未知の問題がないことを保証するものではありません．")
    lines.extend(["", "## 資料と読取範囲", ""])
    for source in report.sources:
        label = f"{source.source_id}：{source.title or source.final_url or source.requested_url}"
        lines.extend(
            [
                "- " + _source_link(source, label),
                "  発見経路："
                + (
                    "実行者が指定した補足公開資料（検索による発見ではありません）"
                    if source.supplied
                    else "検索結果から取得"
                ),
                f"  取得状態：{_text(source.status)} ／ 種別：{source.kind} ／ HTTP：{source.http_status if source.http_status is not None else '不明'}  ",
                f"  取得日時：{_text(source.fetched_at)} ／ 本文hash：{_text(source.body_hash or '未取得')}  ",
                f"  抽出passage数：{len(source.passages)} ／ モデルへ送信したpassage数：{len(set(source.read_passage_ids))}",
            ]
        )
        if source.final_url and source.final_url != source.requested_url:
            requested = source.model_copy(update={"final_url": None})
            lines.append(
                "  URLの遷移：" + _source_link(requested, "要求URL") + " → " + _source_link(source, "最終URL")
            )
        read = [p for p in source.passages if p.passage_id in source.read_passage_ids]
        if read:
            ranges = [
                f"{p.passage_id}（{'物理p.' + str(p.page) + '／' if p.page is not None else ''}{p.start}–{p.end}）"
                for p in read
            ]
            lines.append("  読取範囲：" + _text("，".join(ranges)))
        if source.kind == "pdf":
            lines.append(
                f"  PDF物理ページ数：{source.page_count if source.page_count is not None else '不明'}．"
                "紙面に印刷されたページ番号とは区別しています．"
            )
            if source.page_statuses:
                lines.append(
                    "  ページ抽出状態："
                    + _text(
                        "，".join(
                            f"物理p.{key}={value}"
                            for key, value in sorted(
                                source.page_statuses.items(), key=lambda item: (len(item[0]), item[0])
                            )
                        )
                    )
                )
        if source.error:
            lines.append("  取得・抽出の失敗：" + _text(source.error))
        lines.extend("  制限：" + _text(value) for value in source.limitations)
        lines.append("")
    if not report.sources:
        lines.append("取得資料はありません．")
    lines.extend(
        [
            (
                "検索snippetは資料選択用であり，本文の根拠として扱っていません．"
                "PDFのOCR・図表画像理解・高精度な表構造復元は行っていません．"
            ),
            "",
            "## 起点の関係",
            "",
        ]
    )
    for group in report.origin_groups:
        lines.append(
            f"- {_text(group.group_id)}：{_text('，'.join(group.source_ids))} ／ 独立性：{group.independence}"
        )
        lines.extend("  " + _text(reason) for reason in group.reasons)
    if not report.origin_groups:
        lines.append("起点の関係は未確認です．")
    lines.extend(["", "URL数・domain数・検索engine数は独立した根拠数を意味しません．", "", "## 実行記録", ""])
    lines.append(
        f"検索query数：{len(report.queries)} ／ round数：{len(report.rounds)} ／ 取得資料数：{len(report.sources)}"
    )
    if report.activity_summary:
        lines.extend(["", _text(report.activity_summary)])
    if report.failures:
        lines.extend(["", "記録された失敗：", ""])
        lines.extend(
            "- " + _text(json.dumps(failure, ensure_ascii=False, sort_keys=True))
            for failure in report.failures
        )
    lines.extend(
        [
            "",
            "機械可読な正本はreport.jsonです．呼出し回数・経過時間・実model ID等はmetrics.jsonを参照してください．",
            "",
        ]
    )
    return "\n".join(lines)
