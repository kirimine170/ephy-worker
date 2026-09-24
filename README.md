# ephy-worker

## Overview

認可された公開Web調査と隔離されたcoding Jobを実行する，Python 3.12–3.14向けCLIです．調査では質問を検索計画へ分け，SearXNGまたはTavilyで探索し，HTML／テキストPDFの本文から根拠を抽出して別contextで照合します．codingではtemporary Git worktree，Pi RPC adapter，独立validation，machine-readable artifactを用います．

## Role in the Ephy ecosystem

Workerは調査Job内の実行・予算・出典を所有します．目的・許可・結果の利用はRuntime，永続記憶はKarteの責務です．Runtimeの通常会話・音声設定や本番Karteには接続・変更しません．将来のUIはGo／Wailsを想定しています．

## Goals

- 異なる役割の検索語3–5本，初回4–6資料を目安に探索し，本文で照合する．
- source／passage／claim IDと引用の実在をコードで検査し，意味・条件・版・日時を別段階で照合する．
- 追加調査は理由付きで最大1roundに制限し，矛盾・未確認点・取得不能を保存する．
- 取消・timeout・上限時も検査済みの部分結果を保存する．
- coding modelをprofileで交換し，同じsynthetic taskを隔離worktreeと独立validationで評価する．

## Non-goals

UI，stdio daemon，Runtime／Karte実接続，自動再割当，中間resume，複数調査の並列化，OCR，JSブラウザ，認証回避は対象外です．coding機能の自動model weight取得，自動ranking，merge／push／deploy，自己更新も対象外です．

## Current status

Phase 1の調査処理に加え，Phase 2の分散収集経路を実装しています．実行したfixture・実モデル試験，未検証のOS／profile，検索基盤の制約は[Phase 1検証](docs/phase1-validation.md)と[Phase 2検証](docs/phase2-validation.md)に記載します．`completed`は処理完了を表し，すべての主張が真実だという保証ではありません．`answerability`で回答範囲を別記します．

Coding Evaluation MVPは，Pi RPC adapter，detached worktree，deterministic mock，独立validation，6カテゴリのoffline smoke suiteを実装しています．mac向けcodingの既定Pi backendはRuntimeのcode経路と同じllama.cppです．共有llama.cpp routerで複数GGUFを切り替えるprofileも用意しています．現在の未コミット内容も明示したfileだけ一時snapshotに含め，ephy-worker自身からcandidate patchを作れます．実行境界は[Coding executorとevaluation harness](docs/coding-evaluation.md)を参照してください．

対話型のlocal Piでは，Qwen3.8 ReasoningとQwen3 Coderを作業phaseに応じて自律切替するopt-in tool，token上限時のcompaction recovery，macOS／Windows共通の起動引数例を共有しています．managed self-improvement sessionではrouterを無効化し，既存のmodel identityとrole分離を維持します．構成，検証範囲，未検証範囲は[Pi自律model切替](docs/pi-autonomous-model-routing.md)を参照してください．

現環境ではSearXNGのCAPTCHAにより検索発見のlive経路は未検証です．既知の公開資料を補足したQwen実行ではHTML・PDF引用を採用できましたが，数値の精度差や比較表現の過大解釈を意味照合が見逃す例があり，調査品質の合格とは扱っていません．これを受けて`evidence.py`の`apply_review`にclaimと引用の決定的整合性チェック（数値の精度，比較表現の強さ）を追加しました．検査に落ちた引用はsupportとして数えず`context_only`として保存し，claimは残りの有効な根拠がその状態を満たさなくなった場合にのみ降格します．チェックは観測された失敗形状のみの狭い範囲をoffline testでカバーしており，live経路の再実行はまだ行っていません．その他の過大解釈の検出は同一モデルの別context照合と人の再確認に任せます．

無料枠向けのTavily検索providerを追加しました．無料プラン・使用量・残量を検索前に確認し，basic検索を最大10credit／Jobに制限します．利用者実行のdoctorで使用量API・実検索1回・Qwen接続の成功を確認しました．`paygo_limit`のnullは未報告として保持し，無料枠の残量で制限します．設定・`doctor --usage-only`の診断手順と検証範囲は[Tavily検証](docs/tavily-validation.md)を参照してください．

## Architecture

`config`／`models`／`search`／`fetch`／`extraction`／`evidence`／`research`／`report`／`store`をCLIから分離しています．Phase 2では`collector`／`manager`／`worker_service`を追加し，調査判断と資料収集を契約0.4で分けます．[Phase 1設計](docs/adr/0001-phase1-public-research.md)，[Phase 2設計](docs/adr/0003-phase2-distributed-collection.md)，[処理境界](docs/architecture.md)を参照してください．Workerはモデルにshell・ファイル・credential・URL取得のtoolを渡しません．

Coding経路は`coding_schema`／`coding_profiles`／`coding_executor`／`evaluation`を調査経路から分離します．Pi固有処理はRPC adapter内だけに置き，`CodingJob → worktree → Pi/mock → patch → validation → CodingResult`を構成します．

## Repository relationships

親projectは`ephy`です．直接依存repoはありません．将来のRuntime／Karte接続は未実装であり，現時点の`.ephy/project.yaml`へ実接続として宣言しません．

## Getting started

[uv](https://docs.astral.sh/uv/)を利用します．lockfileで依存を固定し，このrepo専用`.venv`を作ります．モデルウェイトのdownloadや他のPython環境の更新は行いません．

macOS／Linux：

```bash
uv sync --locked --python 3.12
mkdir -p "$HOME/ephy-worker-private"
cp configs/worker.example.yaml "$HOME/ephy-worker-private/worker.yaml"
```

PowerShell：

```powershell
uv sync --locked --python 3.12
New-Item -ItemType Directory -Force "$HOME/ephy-worker-private"
Copy-Item configs/worker.example.yaml "$HOME/ephy-worker-private/worker.yaml"
```

Git外の設定を編集し，稼働中サービスの実際のURL・model IDを確認してください．profileの`base_url`／`model_id`を直接記述する場合は，対応する`*_env`フィールドを削除します．API key本体は書かず，必要な場合だけ`api_key_env`へ環境変数名を設定します．`qwen-local`と`deepseek-local`はprofile名であり，実model IDではありません．一方の未設定profileは，もう一方の利用を妨げません．

```yaml
model_profiles:
  qwen-local:
    family: qwen
    base_url: http://127.0.0.1:8083/v1
    model_id: your-actual-model-id
    api: chat_completions
    output_mode: tool
    context_tokens: 32768
    max_output_tokens: 4096
    max_tokens_parameter: max_tokens
    schema_retries: 1
```

上記は形の例です．placeholderは実在IDへ置換し，`doctor`で確認します．`output_mode`は`tool`／`native_json`／`prompted_json`から実serverが対応するものを明示します．勝手なmode・model・有料APIへのfallbackはありません．serverの実context長を`context_tokens`へ設定します．`enable_thinking`やsamplingは必要なときだけWorkerのprofileへ明示し，共有serverの起動設定は変更しません．

同じコマンドをmacOS／LinuxとPowerShellで使えます．改行継続に依存しない1行の例です．

```text
uv run python -m ephy_worker doctor --config /absolute/private/worker.yaml --profile qwen-local --output-dir /absolute/private/research-output
uv run python -m ephy_worker research --config /absolute/private/worker.yaml --profile qwen-local --question "公開の技術比較質問" --output-dir /absolute/private/research-output
```

### Coding evaluation

実modelもPiも不要なmock smoke suiteです．artifact既定先はGit外の`~/.local/state/ephy-worker/eval/runs`です．

```text
uv run python -m ephy_worker eval run --suite smoke --model mock
uv run python -m ephy_worker coding run /absolute/private/coding-job.json --dry-run
```

Job example，custom profile，実Pi接続，failure code，artifact schemaは[Coding executorとevaluation harness](docs/coding-evaluation.md)を参照してください．

### Phase 2の分散収集

Manager，収集Worker，調査processは役割別の設定を使います．例をGit外へコピーし，絶対pathと環境変数を利用環境へ合わせます．credentialは十分長い乱数を各端末の環境変数に設定し，YAMLやshell historyへ値を直接書かないでください．Managerはloopbackにだけbindします．別PCからはSSH port forwarding等の暗号化tunnelで各端末の`127.0.0.1:8321`へ接続するか，検証済みのHTTPS reverse proxyを用意します．平文HTTPのLAN公開は既定で拒否します．

```text
cp configs/manager.example.yaml /absolute/private/manager.yaml
cp configs/collector-worker.example.yaml /absolute/private/collector.yaml
cp configs/research-remote.example.yaml /absolute/private/research-remote.yaml
uv run python -m ephy_worker manager run --config /absolute/private/manager.yaml
uv run python -m ephy_worker worker run --config /absolute/private/collector.yaml
uv run python -m ephy_worker manager workers --config /absolute/private/manager.yaml
uv run python -m ephy_worker research --config /absolute/private/research-remote.yaml --profile qwen-local --mode remote --question "公開の技術比較質問" --output-dir /absolute/private/research-output
```

調査processは検索語生成，候補選択，引用照合，追加round，レポート生成を担当します．収集Workerは`web.collect`の`search`と`extract`だけを実行し，LLMへ接続しません．指定した`target_worker_id`が未登録，offline，能力不一致の場合は待機理由を返し，localへ自動切替しません．

手動Job操作は次のとおりです．`submit`用JSONは[例](configs/job-search.example.json)をGit外へコピーし，`submit_key`を再送単位で固定します．同じkeyと同じ入力の再送は同じJobを返し，異なる入力は拒否されます．

```text
uv run python -m ephy_worker job submit --config /absolute/private/manager.yaml --file /absolute/private/job-search.json
uv run python -m ephy_worker job status --config /absolute/private/manager.yaml --job-id JOB_ID
uv run python -m ephy_worker job cancel --config /absolute/private/manager.yaml --job-id JOB_ID
uv run python -m ephy_worker job result --config /absolute/private/manager.yaml --job-id JOB_ID
uv run python -m ephy_worker job retry --config /absolute/private/manager.yaml --job-id JOB_ID
uv run python -m ephy_worker job delete --config /absolute/private/manager.yaml --job-id JOB_ID
```

各processは`Ctrl+C`で停止します．Manager停止前に調査processと収集Workerを止めます．Workerとの通信が切れた通常実行Jobはlease失効後に`lost`となり，遅延結果は拒否されます．取消要求中に通信が切れたJobは，停止確認がないため`cancel_requested`のまま残ります．状態と原因を確認してから明示retryしてください．自動再割当や中間resumeはありません．

ManagerのSQLiteとartifact directoryは同じPCのローカルdiskへ置き，ネットワーク共有しません．terminal Jobの成果物が不要になったら`job delete`でDB参照と管理対象ファイルを削除します．Managerのartifact directory内を個別に削除すると参照が壊れます．2台用の接続情報はManager側のtunnel到達先，両credential環境変数，収集側の検索provider URLまたはTavily key，調査側のLLM endpoint／model IDです．

Windowsでは絶対pathを例として`"C:\Users\you\ephy-worker-private\worker.yaml"`のように渡します．空白・日本語を含むpathは引用符で囲んでください．DeepSeek利用時は同じcommandのprofileだけを`deepseek-local`へ変更します．既知の公開PDFを補足する場合は`--source-url "https://example.org/paper.pdf"`を付けます．検索発見と実行者指定は区別して記録します．

`doctor`はSearXNGのJSON／設定engine／障害，model IDの存在，選択した型付き出力mode，出力先を検査します．CAPTCHA・403・429・timeoutを検索0件として扱いません．model discoveryもmodel request数に含みます．検索語は外部検索サービスへ送信されるため，完全オフラインではありません．公開質問だけを渡してください．

`Ctrl+C`で現在のJobを取消します．共有LLM／SearXNGをkillしません．client接続を閉じてもserver内部の推論が停止したかは未確認です．再実行は新しいJob directoryを作り，既存成果物を上書きしません．プロセスの強制killや停電では`status.json`が`running`のまま残る場合があり，成功を表しません．自動resumeはありません．

終了codeは`0=completed`，`2=partial`，`1=failed／設定エラー`，`130=cancelled`です．

出力先はGit checkout外を必須とします．Jobごとに次を保存します．

| ファイル | 用途 |
|---|---|
| `report.json` | 正本．検索候補，source／passage全文，引用位置・対応方法，claim，origin group，読取範囲，失敗，停止理由 |
| `report.md` | 正本から組み立てた日本語レポート．事実ごとに出典を表示 |
| `events.jsonl` | Job ID，連番，段階，検索・取得・照合・終了の記録 |
| `metrics.json` | 実model ID，request数，時間，使用量，version，制約．不明token数はnull |
| `status.json` | 最新Job状態．取消や失敗を区別 |
| `artifacts.json` | report JSON／MarkdownのSHA-256 |

不要なJob directoryは利用者が削除できます．POSIXではJob directoryを0700，ファイルを0600にします．Windowsでは親directoryのACLを継承するため，利用者専用directoryを選んでください．永続的な共有cacheは作りません．

PDFはContent-TypeとPDF magicを確認し，1始まりの**物理ページ番号**を保持します．抽出できた全文をpassage単位で検索し，後半ページも候補にします．`read_passage_ids`はモデルへ送った範囲です．抽出成功と全文の確認済みは異なります．画像混在ページはテキストだけ利用し，画像・OCR・図表の意味理解・高精度な表構造は未対応として記録します．暗号化・破損・読取不能・ページ数／文字数上限も区別します．

引用は保存した原文と完全に一致します．モデルがPDFの改行を空白へ変更した場合に限り，同じ送信済みpassage内の一意な空白正規化matchから原文を復元し，`quote_match=whitespace_normalized`と記録します．数値・単語・句読点の変更や別passageへの付替えは行いません．

同じ本文hashと明示的な転載元linkをorigin groupへ統合します．異なるdomainだけでは独立と判断しません．現Phase 1の自動group検出は独立性を`unknown`とする保守的方式であり，自動的に`corroborated`へ昇格しません．一次資料の意味上の支持は別モデルcontextで点検しますが，利用者による検証とは区別します．追加roundの結果は前roundの矛盾・根拠を消さずに保存します．

既定予算は10query／20検索request／12資料／36fetch request／40model request／900秒です．redirect・retryもrequestへ数えます．モデルのschema修正は各呼出し1回までです．実在しない引用の修正を最大1回だけ別に試み，全requestを共通上限に数えます．HTML 5MiB，PDF 20MiB，抽出20万文字，PDF100ページ，Job作業データ128MiBを上限にします．parserは別processで時間とRSSを監視し，上限時に回収します．macOS／WindowsのRSSはpoll監視であり，OSレベルの厳密な瞬間memory capではありません．

## Testing

外部LLM／検索サービス不要の共通fixtureです．

```bash
uv sync --locked
uv run python -m pytest -q
uv run python -m pytest -q tests/test_coding_executor.py tests/test_coding_evaluation.py
uv run python -m ephy_worker eval run --suite smoke --model mock --output-dir /absolute/git-external/eval-runs
uv run ruff check src tests/test_research.py tests/test_workflow_edges.py tests/test_cli.py tests/test_evidence.py tests/test_fetch.py tests/test_extraction.py tests/test_providers.py tests/test_tavily.py tests/test_manager.py tests/test_manager_http.py tests/test_worker_service.py tests/test_coding_executor.py tests/test_coding_evaluation.py
PYTHONPATH=src .venv/bin/python scripts/validate_phase2_processes.py
python3 scripts/validate_repository.py --check-sensitive-patterns
```

Windowsのrepository validationは`python scripts/validate_repository.py --check-sensitive-patterns`です．既存Linux CIを依存lockfileとoffline pytestへ接続し，新規3 OS matrixは追加していません．CIの実行結果はローカル実行と区別します．fixture合格は実モデルの調査能力を示しません．

## Security and data handling

公開fetchはHTTP(S)，DNS全解決結果，redirect先を検査し，接続IPを固定して再解決を防ぎます．private／loopback／link-local／metadata／IPv6変種，credential入りURLを拒否します．モデル／SearXNGは設定済みの別経路を使い，公開fetchの例外にはしません．TLS検証，環境proxy無効化，cookie無効化，展開後size制限があります．queryは書換え後にもsecret・private path等を検査します．完全な機密情報分類器ではないため，公開情報のみを入力してください．

[データ取扱方針](docs/security-and-data.md)も参照してください．API key・推論reasoning・私的会話・モデルウェイトをGitや成果物へ保存しません．外部tracingは有効化しません．

## Documentation

- [Architecture](docs/architecture.md)
- [Phase 1 ADR](docs/adr/0001-phase1-public-research.md)
- [Phase 1 validation](docs/phase1-validation.md)
- [Phase 2 distributed collection ADR](docs/adr/0003-phase2-distributed-collection.md)
- [Phase 2 validation](docs/phase2-validation.md)
- [Tavily free-plan validation](docs/tavily-validation.md)
- [Coding executor and evaluation](docs/coding-evaluation.md)
- [Local coding model validation](docs/coding-model-validation.md)
- [Pi autonomous model routing](docs/pi-autonomous-model-routing.md)
- [Repository relationships](docs/repository-relations.md)
- [Security and data handling](docs/security-and-data.md)

## License

このrepositoryのlicenseは未選択です．配布時に明示的に決定してください．
