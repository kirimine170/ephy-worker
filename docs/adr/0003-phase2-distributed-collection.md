# ADR-0003：pull型Managerによる資料収集の分離

- Status：Accepted
- Date：2026-09-21

## Context

Phase 1では，LLMによる調査判断と公開Webの検索・取得・抽出を同じprocessで実行していた．LLMを持たない別PCへ後者だけを移し，調査側の候補選択，意味照合，引用検査，追加round，レポート生成を維持する必要がある．別PCのSQLite共有，Workerへの着信，任意コード実行，予算の再付与は避ける．

## Decision

契約version `0.4`の`web.collect` Jobを導入し，入力modeを`search`と`extract`に限定する．Managerは単一process，ローカルSQLite，Git外artifact directoryで動作し，事前登録したWorkerがpullする．Jobは明示的な`target_worker_id`を持ち，Managerがattempt ID，lease token，有効期限を発行する．Workerは同時に1 Jobだけ実行し，LLMを呼ばない．

調査側は`Collector`境界を使う．`LocalCollector`は従来の検索・取得処理を同processで呼び，`RemoteCollector`は同じ入力をManagerへ送り，型付き`ResultEnvelope`を戻す．検索語生成，候補選択，引用照合は調査側に残す．子Jobの使用量と期限は親の残量から割り当て，追加roundごとに初期化しない．

credentialはHTTP headerにだけ載せ，設定には環境変数名を書く．Managerの初期bindはloopbackに限定する．別PCでは検証済みTLS終端またはSSH等の暗号化tunnelを利用する．非loopbackの平文HTTPは明示設定なしでは拒否する．公開ページのfetch制限は維持する．

artifactは一時ファイルへ受信し，job／attempt／lease，許可media type，bytes，SHA-256，個数上限を検査してから原子的に公開する．有効なattemptだけが結果を確定できる．同一submit keyと同一入力は同じJobを返し，異なる入力は拒否する．同一attemptの同一結果再送は成功し，異なる結果は拒否する．

取消要求中のJobはWorkerが`cancelled`結果を確定するまで`cancel_requested`に保つ．lease切れや通信断だけで停止確認済みとは扱わない．通常実行中のlease切れは`lost`とし，遅延結果を拒否する．自動再割当は行わない．

## Consequences

収集PCには検索・fetch・parser依存だけを設定でき，model profileは不要になる．Managerの再起動後もJob，attempt，結果，artifact参照はSQLiteから復元できる．一方，単一Managerは高可用ではなく，Worker停止後の再実行は利用者が明示的にretryする．2台間のTLSまたはtunnelは利用者の運用責務であり，本実装は証明書発行やSSH自動構築を行わない．

## Alternatives

- SQLiteファイルのネットワーク共有は，同時更新と障害復旧の境界が不明確になるため採用しない．
- ManagerからWorkerへpushする方式は，Worker側の着信設定を必要とするため採用しない．
- Redisや汎用workflow engineは，初期構成の運用負荷が要件を超えるため採用しない．
- 検索から照合までを収集Workerへ移す方式は，LLM不要という責務分離を崩すため採用しない．

## Related repositories

変更は`ephy-worker`内で完結する．Runtime，Karte，親projectとの実接続は追加しない．
