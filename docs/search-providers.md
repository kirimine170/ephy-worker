# 検索providerと要求の制御

検索語から共通の `Candidate` を返す境界を維持し，`search.provider` で検索先を明示します．本文取得と根拠の評価は既存のCollector／research処理が担当します．SearXNG，キー付きTavily，DuckDuckGo HTML，`tavily_keyless` を選べます．自動で別providerへ切り替えません．

## Tavily keyless

`configs/worker.tavily-keyless.example.yaml` が設定例です．[公式のkeyless仕様](https://docs.tavily.com/documentation/keyless)を2026-10-17に確認しました．アカウントとAPIキーは不要ですが，無料利用にはサービス側のrate limitがあります．制限値はこの実装で保証しません．

実装は `https://api.tavily.com/search` へのPOSTだけを行い，`X-Tavily-Access-Mode: keyless` を送ります．`TAVILY_API_KEY` が環境にあっても読み取らず，Authorizationを送信しません．接続先，APIキー参照，retryの上書きを拒否し，`/usage`，プロキシ環境変数，redirect，CAPTCHA解決，認証付き経路や有料経路へのfallbackを使いません．`basic`・`general`・`auto_parameters: false` を指定し，生成回答，raw content，画像を要求しません．

検索前に公開queryの秘密情報・個人情報検査を行います．結果数，レスポンスbyte数，要求時間，ジョブ時間，実HTTP要求回数を制限します．JSONの型，既知field，結果URLとprovenanceを検査し，未知の応答や自然言語の登録案内を検索成功や空結果に置換しません．429は `tavily_keyless_rate_limited` です．エラー本文や任意の案内文をlogへコピーしません．`engine_health()` は設定だけを確認します．接続確認は `python -m ephy_worker doctor --config <YAML>` が公開queryで実検索します．

## 任意のrequest_policy

`search.request_policy` を省略すると既存providerの挙動を維持します．指定すると，検索先共通のwrapperが次を行います．

| 設定 | 既定値 | 意味 |
| --- | --- | --- |
| `min_interval_seconds` | 2 | 同一backendの論理検索の開始間隔，0〜300秒 |
| `cache_ttl_seconds` | 300 | 成功結果の最大保持時間，0で無効，最大3600秒 |
| `cache_max_entries` | 128 | 同一backendのcache上限，1〜256件 |
| `cache_max_bytes` | 2097152 | UTF-8 JSONとして保持するcacheのbyte上限，1024〜16777216 |
| `cooldown_seconds` | 300 | challenge／429後の最低休止時間，最大86400秒 |

論理検索はproviderの `search()` 一回です．provider内部のusage確認や既存の接続エラーretryを含めて直列化しますが，内部HTTP一回ずつの間隔は制御しません．keylessはretryがなく，一回の論理検索が一回のHTTPです．wrapper自身は再試行しません．policy付きSearXNGは429をその場で返し，旧来の429 retryをしません．キー付きTavilyのfree allowance／PAYG guardは変更していません．

`engine_health()` は既存の実装へ直接委譲し，検索cache・開始間隔・休止の対象外です．keylessとDuckDuckGoのhealthはローカル確認です．SearXNGのconfig取得やキー付きTavilyのusage取得は，実HTTPとして各providerの既存予算へ計上します．

状態は同一process・event loop内で，provider，endpoint，engine，credential identityごとに共有します．結果数，language，policy設定が変わってもbackendの休止は引き継ぎます．異なる意味の結果はcacheを分けます．loopをまたぐlockは共有しません．一つのloopでbackendは最大32個とし，使用中または休止中の状態を追い出して制限を回避せず，満杯なら `search_policy_backend_limit` で拒否します．設定の異なるcache上限はアクセス時にその上限を適用します．

queryは検査後の完全一致をSHA-256でkey化し，生のqueryをcache keyへ保存しません．cacheはメモリ内だけに成功結果を保存し，LRU順に件数・byte上限を守ります．空の成功結果も対象です．cache hitではCandidateを複製し，現在のquery IDへ結び直します．発見時刻と検索元は元の値を保持します．毎回privacy検査と時間予算を確認し，hitや休止拒否に架空のHTTP回数を加算しません．古い結果で障害を隠すfallbackは行いません．

429とchallengeでは，同じbackendを後続ジョブでも休止します．対応providerが返したRetry-Afterと設定値の長い方を保持し，`search_provider_cooldown` と残り秒数を即座に返します．DuckDuckGo・SearXNG・keylessのHTTP Retry-Afterに対応します．既存キー付きTavilyはheaderを公開しないため，その429には設定の休止時間だけを使います．HTML本文から見つかったchallengeにも設定の休止時間を使います．休止全体をsleepしません．lock待ちと開始間隔の待機はキャンセルでき，残りジョブ時間内に終わらなければ `BudgetExceeded("job_timeout")` です．process再起動でcacheと休止状態は失われます．
