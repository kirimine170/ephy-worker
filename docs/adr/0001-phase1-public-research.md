# ADR-0001：Phase 1の公開Web調査executor

## Status

Accepted．利用者が指定したPhase1追補の範囲で採用・実装した．

## Context

ephy-worker main `8dde2087daaf1bc7c4d18f80d58aa8063faf17d7`はテンプレート状態だった．2026-09-14のPhase1追補を優先し，元MVPのdaemon／SQLite／resume／両モデル比較を今回へ戻さない．RuntimeのPython環境・通常会話・LLM起動設定は変更しない．

## Decision

Python 3.12以降，Pydantic AI，既存SearXNG，Trafilatura，pypdfを採用し，専用venvとuv.lockで依存を固定した．Runtimeの既存SearXNG HTTP形状を参考にした薄いadapterをWorkerへ実装し，隣接repoのsys.path変更や丸ごとcopyへ依存しない．

モデルはtool／native JSON Schema／prompted JSONをprofileで選択する．実schemaをprobeして固定し，hidden SDK retryを無効化する．単純tool probeだけでは本番schemaへの対応を宣言しない．同一モデルの別contextを照合段階に利用するが，独立情報源の数には数えない．引用のIDと実在を検査し，同じpassage内で空白差のみの一意な対応がある場合は元の原文とoffsetへ復元する．数値・単語・句読点・別passageの修正はしない．誤引用は最大1回の明示的な修正後にも再検査する．schema修正retryは各呼出し最大1回で，全HTTP requestを共通予算に数える．

全文store内を語彙検索するため，PDF末尾の条件も選択対象になる．queryに含まれる技術語も関連箇所検索へ使い，モデルのcontextに収まるまで低順位passageを減らす．送信したIDだけを読取範囲に記録する．未知の独立性はunknownに留め，corroboratedへ自動昇格しない．

公開fetchはaiohttpのresolverをchecked IPへ固定し，redirectごとにURL・DNSを再検査する．設定済みmodel／searchと別経路にしてSSRF例外を作らない．parserはspawn方式の子processに閉じ込め，時間・RSS・受信bytes・抽出量を制限する．

成果物はJob別file，report.jsonが正本である．追加roundの主張は別IDで保持し，以前の矛盾や未確認点を暗黙に消さない．前roundのclaim・evidenceも追加調査の参考contextへ渡す．Runtimeが許可と結果利用，WorkerがJob内の実行，Karteが永続記憶を所有する．

## Consequences

request上限とJob全体timeoutを共有し，部分結果は新しいLLM呼出しなしに生成する．共有serverを停止せず，対象parserとclient接続を閉じる．強制kill後のresumeは実装しない．macOS／WindowsのRSSは定期poll，LinuxはRLIMIT_ASも利用する．これはOSのsandbox全体の代用ではない．

実serverでは単純tool schemaが通っても本番Extraction schemaがgrammarエラーになったため，実試験のprofileは明示的にprompted JSONへ変更した．モデルの引用ミスは成功へ変換せず，照合できた範囲だけを返す．資料の独立性や意味の最終確認には人の確認が必要である．

## Alternatives considered

- Runtime内のsnippet要約流用：本文・PDFページ・照合段階を持たないため不採用．
- 汎用coding harness／LangGraph／vector DB：Phase1に不要な権限や状態を増やすため不採用．
- SQLite／常駐stdio：追補で後回しと指定されたためJob別fileへ縮小．
- 新規search server／有料API fallback：設定済みSearXNGの障害を明示して終了する．

## Related repositories

実装変更は`ephy-worker`のみ．Runtime／Karteとの実接続は未実装であり，repo間の実行時依存は追加していない．

## Date

2026-09-14．

参照した一次仕様：[Pydantic AI互換provider](https://pydantic.dev/docs/ai/models/openai/)，[型付き出力](https://pydantic.dev/docs/ai/core-concepts/output/)，[SearXNG Search API](https://docs.searxng.org/dev/search_api.html)，[Trafilatura](https://trafilatura.readthedocs.io/en/latest/usage-python.html)，[pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)．実行した版とprobe結果は検証記録を参照する．
