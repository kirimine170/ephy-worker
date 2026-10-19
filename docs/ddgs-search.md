# DDGS検索の実験経路

`uv sync --locked --extra ddgs`でoptional依存を導入します．DDGS 9.16.0，primp 2.0.1，click 8.5.0を固定します．例は`configs/worker.ddgs.example.yaml`です．backendとregionは必須で，duckduckgo／brave／mojeek／google／yahooの単一backendを明示します．自動選択，fallback，retry，認証情報，proxy，redirect，fingerprint切替は使いません．無料検索元の制限や利用可能性は保証しません．

短命の子process内でDDGSのpayload生成とHTML parserを使います．固定primp profile（chrome_146／windows），TLS検証，既知endpoint，応答streamのbyte上限を使い，実HTTP要求ごとに親がBudgetを消費して許可します．GoogleのランダムUA，Yahooのランダムtracking pathは送信しません．親はdeadline／取消時に実interpreterの子をkillしてwaitします．RSSは既存parserと同様のpoll監視であり，OSの瞬間的なmemory上限ではありません．child stderrと外部例外本文は記録しません．空結果と解析失敗を厳密に区別できないため，空結果は`ddgs_indeterminate_results`で停止します．403／429／challenge／timeoutも成功0件へ変換しません．

共通`request_policy.sqlite_path`へGit外の絶対pathを設定すると，成功CandidateとcooldownをSQLiteへ保存します．query本文は保存せずSHA-256をkeyに使い，provider／endpoint／credential identity／backend／region／version／言語／件数で結果を分離します．privacyとJob時間を検査してからfresh cacheを返すため，cooldown中も保存済み結果を再利用できます．query_idは呼出しごとに再束縛し，discovered_atを維持します．TTL，entry数，serialized byte数，DB page数，50msのDB lock待機に上限があります．破損，schema不一致，不安全pathは明示エラーで停止し，外部再検索しません．SQLite操作ごとにconnectionを閉じます．同一process内の同時検索はcoalesceしますが，異なるprocessのcache missはcoalesceしません．

実測例（公開query JSONと出力はGit外に配置）：

```text
python scripts/benchmark_search.py --config C:/private/ddgs.yaml --queries C:/private/public-queries.json --output C:/private/ddgs-smoke.json --limit 20 --interval 3
```

query JSONは`id`，`language`（ja／en），`query`，`region`（jp-jp／us-en）を持つ最大100件の配列です．固定入力のSHA-256，query本文，provider／backend，結果URL，遅延，実要求回数，cache無効，失敗理由を記録します．cacheとfallbackを無効にし，直列実行します．403／429／challengeを受けたらbackendを停止し，残りをskippedとしてattempted分母から除きます．20件の疎通後，拒否がない場合だけ100件へ進みます．成功率は取得成功を示し，関連性／一次資料の網羅性を自動採点しません．SearXNGが起動していない場合は比較不能として別記します．本文snapshot再生，Tavily fallbackは今回含みません．キー付きTavilyの無料枠確認とPAYG拒否は変更しません．
