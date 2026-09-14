# Worker Phase 1 architecture

CLIは設定と公開質問を受け取り，Job別directoryを作ります．research executorは計画→複数query→候補選択→本文取得→根拠抽出→別context照合→必要なら1round追加を実行します．modelは次の問いを提案できますが，network接続先・予算・path・段階順はコードが所有します．

`schema.py`はschema version0.1の正本です．unknown fieldを拒否するPydantic型を利用します．`models.py`は明示したChat Completions endpointにPydantic AIを接続し，schema出力と有限retryを扱います．`search.py`は設定済みSearXNGのみ，`fetch.py`は公開HTTP(S)のみを取得します．両者の接続経路は共有しません．

`extraction.py`は取得済みbytesを子processへ渡し，HTMLと物理ページ単位PDFを解析します．time／RSS上限と取消で対象parserだけを停止します．`evidence.py`は全抽出passageの語彙検索，origin統合，引用実在と照合IDの検査を行います．意味の支持を形式検証の合格と混同しません．

`report.py`は検査済みのclaim IDからMarkdownを生成します．最終生成用LLMは呼びません．`store.py`はGit外Job directoryへJSON正本，レポート，events，metrics，hashをUTF-8で保存します．取消や上限でも検査済み部分を返します．プロセス強制kill後の自動復旧はPhase 1の範囲外です．

Runtimeが実行許可とJob全体の管理，WorkerがJob内の調査loop，Karteが永続記憶を所有します．将来のGo／Wails接続はWorkerの起動・入力・取消・成果物読取りを担い，検索処理を重複実装しません．現状UI／stdio protocol／Karte書込みはありません．
