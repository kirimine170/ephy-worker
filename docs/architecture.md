# Worker architecture

CLIは設定と公開質問を受け取り，Job別directoryを作ります．research executorは計画→複数query→候補選択→本文取得→根拠抽出→別context照合→必要なら1round追加を実行します．modelは次の問いを提案できますが，network接続先・予算・path・段階順はコードが所有します．

`schema.py`はschema version0.1の正本です．unknown fieldを拒否するPydantic型を利用します．`models.py`は明示したChat Completions endpointにPydantic AIを接続し，schema出力と有限retryを扱います．`search.py`は設定済みSearXNGのみ，`fetch.py`は公開HTTP(S)のみを取得します．両者の接続経路は共有しません．

`extraction.py`は取得済みbytesを子processへ渡し，HTMLと物理ページ単位PDFを解析します．time／RSS上限と取消で対象parserだけを停止します．`evidence.py`は全抽出passageの語彙検索，origin統合，引用実在と照合IDの検査を行います．意味の支持を形式検証の合格と混同しません．reviewがsupportsとしたevidenceについて，claim本文と引用の決定的整合性（数値の精度，比較表現の強さ）を検査し，検査に落ちた引用は`context_only`として保存されsupportへ数えず，claimは残りの有効な根拠がその状態を満たさなくなった場合にのみ降格します（[ADR-0002](adr/0002-claim-citation-consistency.md)）．

`report.py`は検査済みのclaim IDからMarkdownを生成します．最終生成用LLMは呼びません．`store.py`はGit外Job directoryへJSON正本，レポート，events，metrics，hashをUTF-8で保存します．取消や上限でも検査済み部分を返します．プロセス強制kill後の自動復旧はPhase 1の範囲外です．

Runtimeが実行許可とJob全体の管理，WorkerがJob内の調査loop，Karteが永続記憶を所有します．将来のGo／Wails接続はWorkerの起動・入力・取消・成果物読取りを担い，検索処理を重複実装しません．現状UI／stdio protocol／Karte書込みはありません．

## Phase 2の収集境界

`research.py`は`Collector` interfaceを介して資料収集を行う．local modeでは`LocalCollector`が既存のsearch／fetch／extractionを同processで呼ぶ．remote modeでは`RemoteCollector`が契約0.4の`web.collect` JobをManagerへ投入する．`search` Jobはquery群から候補だけを返し，調査側が候補を選んだ後に`extract` JobがURL群を取得・抽出する．したがって，検索語生成，候補選択，evidence照合，claim検査，レポート生成は従来どおり調査側に残る．

Managerはloopback HTTP，単一SQLite connection，Git外artifact directoryを所有する．事前登録credentialに一致するWorkerだけが登録とpull claimを行える．RequesterとWorkerの操作権限は分ける．claimはSQLite transactionで直列化し，attemptとleaseを発行する．結果確定とartifact uploadはworker ID，attempt，lease，hash，size，job scopeを検査する．Manager再起動後はSQLiteからJob，attempt，結果参照を復元する．

Workerは`web.collect`能力だけを宣言し，検索providerとparserを持つがmodel profileを持たない．1 processあたり1 Jobを実行し，lease更新で取消と実行権喪失を観測する．Managerへの接続と公開page fetchは別clientであり，分散化しても公開fetchのprivate address拒否やredirect検査は変えない．詳細は[ADR-0003](adr/0003-phase2-distributed-collection.md)を参照する．
