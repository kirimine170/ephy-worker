# Dockerを使わない検索

`search.provider: duckduckgo_html` はDuckDuckGoの公開HTML検索へ直接GETする実装です．Docker，WSL，検索APIキー，追加依存関係は不要です．モデルAPIは引き続き別途必要です．設定例は `configs/worker.duckduckgo.example.yaml` にあります．既存YAMLの `search` を次に置き換え，モデル設定は維持できます．

```yaml
search:
  provider: duckduckgo_html
```

接続先は `https://html.duckduckgo.com/html/` に固定され，URL・APIキーの上書きは拒否します．`engine` は `duckduckgo`，`language` は `auto` のみです．既存の `timeout_seconds`，`retries`，`response_bytes`，`max_results` とジョブの検索回数・時間予算を適用します．検索語を送信する前に既存の秘密情報・個人情報検査を実行します．プロキシ環境変数，redirect，自動的な別検索への切替は利用しません．識別用User-Agentは `ephy-worker/0.1` です．

この経路は商用検索APIの可用性保証を持ちません．公開HTMLの変更，ネットワーク制限，CAPTCHA，rate limitによって使えなくなる場合があります．202・CAPTCHAは `duckduckgo_challenge`，429は `duckduckgo_rate_limited`，redirectは `duckduckgo_redirect_refused` として明示的に失敗します．これらを迂回・自動再試行しません．timeout・接続エラー・502/503/504のみ設定回数以内で再試行し，すべて予算へ計上します．未知のHTMLや壊れた結果を「検索結果0件」に置換しません．明示的なno-results要素があるページのみ空結果を返します．

`engine_health()` は実装が設定されていることだけを返し，`network_probe: false`・`availability: not_probed` と表示します．実際の接続確認には `python -m ephy_worker doctor --config <YAMLパス>` を実行します．doctorは公開検索語 `Python documentation` で検索し，HTML経路では `json_api: false` と実際の結果数を返します．doctorの成功は将来の検索成功を保証しません．

取得結果にはURL，title，snippet，query ID，engine，rank，発見時刻を記録します．DuckDuckGoの既知hostの `/l/?uddg=` だけを展開し，外部の同名パラメータは展開しません．HTTP(S)以外，認証付きURL，local host名，private・loopbackなど非公開IP literalは拒否します．結果URLへの本文アクセスは既存fetch処理の安全性検査に従います．この検索段階では結果hostのDNS解決・本文取得は行いません．

SearXNG・Tavilyは既存の明示設定で引き続き使用できます．切替にはYAMLでproviderを指定し直し，Workerを再起動してください．

アカウント不要の公式API経路 `tavily_keyless` と，任意の要求間隔・cache・challenge休止の共通制御も利用できます．[検索providerの設定](search-providers.md)を参照してください．
