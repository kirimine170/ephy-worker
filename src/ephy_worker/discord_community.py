"""Local routing for explicitly enabled community observation requests．"""

from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple


class CommunityIntent(NamedTuple):
    operation: str
    period_days: int


def community_intent(query: str) -> CommunityIntent | None:
    text = unicodedata.normalize("NFKC", query).casefold()
    if not text.strip() or len(text) > 4000:
        return None
    context = bool(re.search(r"hacksick|この(?:サーバ|コミュニティ|チャンネル)|うちの|ここ(?:最近|の)|運営", text))
    # Specific community evidence questions are meaningful inside a configured guild．
    specific = bool(re.search(r"未解決|やり残|残っている(?:課題|タスク)|最近の(?:議論|話題|活動)|活動状況", text))
    if not (context or specific):
        return None
    period = 7 if re.search(r"7日|1週間|一週間|今週|先週|weekly", text) else 30
    for operation, pattern in (
        ("unresolved", r"未解決|課題|宿題|やり残|todo|タスク"),
        ("discussion", r"議論|話題|決定|合意|話して|会話|話し合"),
        ("activity", r"活動|活発|投稿|参加|発言|盛り上が|勢い|静か"),
        ("snapshot", r"構成|ロール|チャンネル一覧|全体像|どんなサーバ|人数|サーバ[ー]?情報"),
        ("report", r"状態|状況|どう|様子|まとめ|レポート|分析|改善|提案|運営|教えて|把握|確認"),
    ):
        if re.search(pattern, text):
            return CommunityIntent(operation, period)
    return None
