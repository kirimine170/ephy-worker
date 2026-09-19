# ephy-worker

## Overview

認可された公開Web調査を1プロセス・1Jobで実行する，Python 3.12–3.14向けCLIです．質問を検索計画へ分け，SearXNGまたはTavilyで探索し，HTML／テキストPDFの本文から根拠を抽出して別contextで照合します．`report.json`を正本として日本語Markdownを生成します．

## Role in the Ephy ecosystem

Workerは調査Job内の実行・予算・出典を所有します．目的・許可・結果の利用はRuntime，永続記憶はKarteの責務です．Runtimeの通常会話・音声設定や本番Karteには接続・変更しません．将来のUIはGo／Wailsを想定しています．

## Goals

- 異なる役割の検索語3–5本，初回4–6資料を目安に探索し，本文で照合する．
- source／passage／claim IDと引用の実在をコードで検査し，意味・条件・版・日時を別段階で照合する．
- 追加調査は理由付きで最大1roundに制限し，矛盾・未確認点・取得不能を保存する．
- 取消・timeout・上限時も検査済みの部分結果を保存する．

## Non-goals

UI，stdio daemon，Runtime／Karte実接続，SQLite，resume，同時Job，分散処理，OCR，JSブラウザ，認証回避，両モデル比較，汎用評価runnerはPhase 1に含めません．

## Current status

Phase 1実装です．実行したfixture・実モデル試験，未検証のOS／profile，検索基盤の制約は[検証記録](docs/phase1-validation.md)に記載します．`completed`は処理完了を表し，すべての主張が真実だという保証ではありません．`answerability`で回答範囲を別記します．

現環境ではSearXNGのCAPTCHAにより検索発見のlive経路は未検証です．既知の公開資料を補足したQwen実行ではHTML・PDF引用を採用できましたが，数値の精度差や比較表現の過大解釈を意味照合が見逃す例があり，調査品質の合格とは扱っていません．これを受けて`evidence.py`の`apply_review`にclaimと引用の決定的整合性チェック（数値の精度，比較表現の強さ）を追加しました．検査に落ちた引用はsupportとして数えず`context_only`として保存し，claimは残りの有効な根拠がその状態を満たさなくなった場合にのみ降格します．チェックは観測された失敗形状のみの狭い範囲をoffline testでカバーしており，live経路の再実行はまだ行っていません．その他の過大解釈の検出は同一モデルの別context照合と人の再確認に任せます．

無料枠向けのTavily検索providerを追加しました．無料プラン・使用量・残量を検索前に確認し，basic検索を最大10credit／Jobに制限します．利用者実行のdoctorで使用量API・実検索1回・Qwen接続の成功を確認しました．`paygo_limit`のnullは未報告として保持し，無料枠の残量で制限します．設定・`doctor --usage-only`の診断手順と検証範囲は[Tavily検証](docs/tavily-validation.md)を参照してください．

## Architecture

`config`／`models`／`search`／`fetch`／`extraction`／`evidence`／`research`／`report`／`store`をCLIから分離しています．[設計判断](docs/adr/0001-phase1-public-research.md)と[処理境界](docs/architecture.md)を参照してください．Workerはモデルにshell・ファイル・credential・URL取得のtoolを渡しません．

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
uv run ruff check src tests/test_research.py tests/test_workflow_edges.py tests/test_cli.py tests/test_evidence.py tests/test_fetch.py tests/test_extraction.py tests/test_providers.py tests/test_tavily.py
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
- [Tavily free-plan validation](docs/tavily-validation.md)
- [Repository relationships](docs/repository-relations.md)
- [Security and data handling](docs/security-and-data.md)

## License

このrepositoryのlicenseは未選択です．配布時に明示的に決定してください．
