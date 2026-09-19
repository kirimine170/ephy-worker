# Phase 1 検証記録

実施日：2026-09-14．変更repositoryは`ephy-worker`のみ．baseは`8dde2087daaf1bc7c4d18f80d58aa8063faf17d7`，branchは`codex/spike-worker-web-research`，worktree名は`ephy-worker-web-research`．remote HEADも同じSHAであることを読取確認した．

## 実装とfixture

CLIのdoctor／research，3–5query，候補選択，HTML／テキストPDF，別contextでの意味照合，最大1round追加，JSON正本からのMarkdown生成を実装した．ネットワーク不要のfixtureで，以下を確認した．

- HTMLとPDFのsource／passage／物理ページ／引用位置を解決する．PDF後半ページも選択する．
- 画像PDF，画像混在，暗号化，破損，403，抽出失敗，ページ／文字／memory／時間上限を成功へ変換しない．
- 同じ本文hash／明示的転載元を同じ起点へまとめ，異なるdomainだけでは独立としない．
- 架空ID・引用・未送信passageを拒否し，条件不一致・反証・矛盾を保持する．
- 追加roundで旧矛盾を消さず，取消時にも旧引用を保持する．
- DNS全結果とredirect先を検査し，private宛先・IPv6変種・rebinding・圧縮爆弾を拒否する．テストのためのprivate例外はない．
- 429，schema retry，retryで増えたcontextの上限，timeout，モデル通信取消を確認する．
- 実CLI subprocessへSIGINTを送り，終了code130，cancelled成果物，request0を確認する．対象parserを停止し，共有serverをkillしない．
- 日本語・空白path，UTF-8，Git内出力拒否，新Job作成，既存成果物非上書きを確認する．

offline pytestは163件が11.92秒で成功した．既存repository toolingのunittest12件，ruff check／format，uv sync --locked，repository validationとsensitive-pattern scanも成功した．fixtureは構造・制御の検査であり，実モデルの調査能力の合格とは区別する．

## 実環境とmodel probe

| 項目 | 実測／確認内容 |
|---|---|
| OS | macOS arm64，Python3.12.9で実行 |
| Model ID | `qwen3-coder-30b-a3b`，family=qwen |
| 推論server | llama.cpp `b9872-665892536`，既存のlocalhost:8083 |
| 量子化 | serverの`/models`表示は`Q4_K - Medium`．profileでは`Q4_K_M`と記録 |
| context | `/models`・`/props`・`/slots`で32768を確認 |
| 重みrevision | 不明．ウェイトを新規取得していない |
| chat template | 稼働serverのdefault templateが存在することを確認．templateの完全なhash／revisionは未確認 |
| Worker sampling | temperature0.2，max_output4096．その他はserver既定，thinking指定なし |
| 最終出力mode | `prompted_json`．ネイティブtool対応合格とは表示しない |
| SearXNG | `2026.7.13+9e25585ae`，既存localhost:8888 |
| engine | 有効engineはDuckDuckGoのみ．CAPTCHA障害を明示確認 |

Pydantic AI1.107.5，Pydantic2.13.5，openai3.13.0，Trafilatura2.2.0，pypdf6.18.1，aiohttp3.14.3等をuv.lockへ固定した．

最終のprompted JSON doctorはbasic／実Extraction schemaの両probeに成功した（model3request，3.882秒）．SearXNGのCAPTCHAがあるためdoctor全体のokはfalseである．

最初の単純tool probeは成功したが，本番Extraction schemaでは`Failed to initialize samplers: failed to parse grammar`というHTTP400を確認した．doctorに実Extraction schemaのprobeを追加し，単純probeとworkflow schemaを区別した．その後Workerのprivate profileを明示的にprompted JSONへ変更した．サーバー起動設定や通常会話設定は変更していない．

## 公開実行の範囲

固定した問いは，2025年4月公開Qwen3-30B-A3Bの総／活性化パラメータ数，thinking切替，ベンチマークの推論条件・制約である．2025年7月版との混同を避ける条件を付けた．

通常の検索からの実行は`research-d62ed9a43c61467893a4b7385d67664f`として保存した．検索4request，fetch0，model1，7.503秒で`failed/no_readable_sources`となった．検索障害を0件の正常検索へ変換していない．

独立して本文処理を検証するため，実行者が既知の公開資料を補足した．これは検索で発見できた資料ではない．

- [Qwen公式発表HTML](https://qwenlm.github.io/blog/qwen3/)
- [Qwen3 Technical Report PDF](https://arxiv.org/pdf/2505.09388)
- [Qwen3-30B-A3B model card HTML](https://huggingface.co/Qwen/Qwen3-30B-A3B)

初回補足実行はtool grammarエラーを記録した．prompted JSONでの次の実行`research-58a3e53dc5c44115b2ae2bdca4033161`は，HTML2件と35ページPDFを実取得し，94.653秒，検索8／fetch3／model3requestで`partial/no_new_evidence`として保存した．PDFは画像未読のためpartial，92passagesへ抽出した．物理30ページもモデルの読取範囲に含まれた．2件のHTML根拠を採用し，5件の引用不一致を拒否した．PDF根拠の採用はこの実行では達成していない．

引用を原文の短い連続箇所へ限定する指示と，最大1回の有限な引用修正を追加した．次のJobでは4件のHTML引用を採用したが，PDFの改行差が残った．Codexの本文監査は数値の丸め精度差と，無根拠な「資料に存在しない」というreasonも指摘した．これを受け，同じpassageの空白差のみを一意に原文へ復元する処理，数値精度の保持指示，根拠なしclaimの理由をコードで留保する処理を追加した．最新の補足実行と主要claim照合は最終確認欄に記載する．

成果物はGit外の`local-data/worker-research/reports/<Job ID>/`に保存した．同じ親directoryに`worker.yaml`とdoctor記録がある．private設定・取得本文・実行logはGitへ追加していない．

## 未検証・残る制約

検索から資料を発見する完全なlive経路は，SearXNG／DuckDuckGoのCAPTCHAにより未検証である．補足資料による部分実行を完全なlive合格へ数えない．別engineの有効化，CAPTCHA回避，新規search server，有料APIへの切替は行っていない．検索基盤を復旧後，同じprivate configで補足URLなしのresearch commandを再実行できる．

DeepSeekの実機は未検証．`EPHY_DEEPSEEK_BASE_URL`と`EPHY_DEEPSEEK_MODEL_ID`，認証が必要ならAPI key環境変数名を設定し，doctor後に同じresearchを実行する．mockをDeepSeek合格の代わりにしない．

WindowsとLinuxの実行は未検証．既存Linux CIはoffline suiteへ接続したが，今回pushしていないためCIの実行結果はない．新規3 OS matrixは対象外．macOS以外のCtrl+C／parser回収／path処理を実機合格とは報告しない．replay／baseline／両モデル比較もPhase1の対象外であり未実施．利用者によるclaim確認は未実施．

OCR，PDF図表画像，表の高精度構造復元，動的ページ，認証ページ，独立性の高度な推定は未対応．意味照合は同じmodelの別contextであり，人の照合と区別する．query privacy検査はパターン検査であり，公開質問だけを入力する前提である．共有server内部の推論取消は未確認．

## 最終確認

最終補足Jobは`research-22cd85b7fa5b4792a73a8aa38109905d`である．174.717秒，検索8／fetch3／model4request，input41285／output4252tokenを記録し，`partial/no_new_evidence`，終了code2となった．初回5queryと理由付き追加3queryはすべてCAPTCHAで失敗した．指定したHTML2件と35物理ページのPDF1件を実取得し，24passages（PDF20，HTML各2）をモデルへ渡した．PDFでは物理30ページも読取範囲へ含め，物理13・15ページの2引用を採用した．採用した5引用すべてについて，保存した原文と文字offsetが完全に一致することを別途コードで確認した．PDF2引用の対応方法は`whitespace_normalized`であり，保存引用には元の改行を保持している．

以下はCodexによる主要5claimの本文照合であり，利用者による確認ではない．モデルの`checked`は照合処理を通った意味であり，この監査への合格を示さない．

| claim | モデル判定 | Codexの確認 |
|---|---|---|
| R0-c001：総300億・活性化33億 | supported_primary | 不適切．S1は30.5B／3.3B，S3は30B／3Bであり，精度の違う値を混ぜている．reasonの30.5Bとも不一致．資料ごとの数値と差を残す必要がある |
| R0-c002：enable_thinkingのTrue／False切替 | supported_primary | 引用はTrueと切替コメントまでであり，Falseの明示部分が不足．取得済みS1の後続passageにはFalseがあるが，今回の読取範囲外であり，その部分を確認済みとできない |
| R0-c003：モード別temperature／top-p／top-k | supported_primary | 引用したPDF物理13ページと6数値は一致する．ただしpresence penalty，評価時の長さ上限等の条件を網羅していないため，推論条件全体の説明としては限定的 |
| R0-c004：QwQ-32Bと同等または優れる | supported_primary | 過大な解釈．本文はhighly competitiveとし，特に推論系benchmarkという限定がある．引用だけでは同等以上の一般化を支持できない |
| R0-c005：non-thinkingでQwen2.5-32B-Instructより優れる | insufficient | 本文に一致する引用を採用できず，未確認として残す制御は妥当．資料全体に記載がないという意味ではない |

数値精度の保持指示を追加しても，この実モデルではc001とc004の意味照合の見逃しが残った．また一部のgapsには，読取不足を資料自体の情報不足として表す記述が残る．したがって，引用の実在確認とHTML／PDFの部分実行は確認できたが，自動判定した4件を正しい主張4件として数えず，調査品質を合格とは報告しない．数値・比較の強さ・条件の網羅性は利用者による再確認が必要である．別contextの同一モデル照合に残る限界として記録する．

詳細な独立監査は同Jobの`evaluation.md`に保存した．これはCodexの評価用sidecarであり，自動生成した正本・Markdownを後から書き換えていない．`artifacts.json`と照合したSHA-256は，`report.json=3845ce9eb05f0c0ec06c65ddf3fdd0d2e333b0d1a53df92fb7c58cf6516b143a`，`report.md=fc1f681d00cafe7f7d45a4208483152c634e5a9c478acd4f0cc11e4e21db45a8`で一致した．

再実行はworktreeで以下を実行する．保存先と設定はGit外である．検索基盤復旧後の検索発見試験には，`--source-url`を追加しない．

```bash
uv run python -m ephy_worker doctor --config ../../local-data/worker-research/worker.yaml --profile qwen-local --output-dir ../../local-data/worker-research/reports
uv run python -m ephy_worker research --config ../../local-data/worker-research/worker.yaml --profile qwen-local --output-dir ../../local-data/worker-research/reports --question "2025年4月公開のQwen3-30B-A3Bについて，総パラメータ数と活性化パラメータ数，thinkingとnon-thinkingの切替方法，ベンチマークの推論条件と制約を公式資料と技術レポートから整理してください．2025年7月版とは区別してください．"
```

## Phase 1後の強化（claim・引用の決定的整合性）

上記監査のc001（精度の違う値の混合）とc004（比較表現の強化）を受け，`evidence.py`の`apply_review`に決定的整合性チェックを追加した．reviewがsupportsとして条件一致と判定した各evidenceについて，claim本文と引用を次の2点で検査する．

1. 数値の精度：隣接する単位（B，M，％，億，万，倍など）を持つ数値だけを対象に，同一の単位のあいだだけ桁列を比較し，片方向のprefix関係になる場合（例：`30B`対`30.5B`，`3.3B`対`3B`）にその引用をsupportとして数えない．claim側の数値の値と単位が引用にそのまま現れる場合は，他の数値が混ざっていても指摘しない．単位のない数値（日付，version，裸の整数），model名やhyphenated複合語内の数値（Qwen3-30B-A3B，GPT-4），ASCII単位の直後にASCII文字が続く表記（`3beta`，`3months`）は対象外である．
2. 比較表現の強さ：同等・同等以上・優位の3段階の固定語彙で，claimの表現が引用より強い場合（例：引用がhighly competitiveでclaimが同等または優れる）にその引用をsupportとして数えない．同等または優れる・同等または優れているのような複合表現は完全形として登録し，全markerを収集したうえで最長一致で重なりを解決するため，内包される語で過大に評価しない．否定は選択された完全な表現にのみ適用するため，否定された複合表現（同等または優れるわけではない）は肯定markerを残さずlevel 0となる．否定は比較述語に直接付着した明示的な形の有限パターン集合だけ数える（英語は述語の直前にdoes not，did not，cannot，can not，never，not，no，without，n'tが続く形，日本語は直後の〜ない・〜ません・わけではないなど，および直前の非）．汎用の英語解析ではないため，no doubt ... outperformsやnot only outperformsは否定と扱わない．単独の無・不も否定と扱わないため，無条件で上回るや不具合修正後は上回るは誤検出しない．

検査が問題を検出してもclaimは書き換えない．該当evidenceのrelationはその場で`context_only`へ変更されて背景として保存され，詳細は`check_reason`へ追記されて`uncertainty`へも記録し，claimの`reason`へ検査の追記を行う．claimは残りの有効な根拠がその状態を満たさなくなった場合にのみ降格される．支持がすべて除外された場合は`insufficient`となり，複数のsupportsの一部だけが問題のある場合は残りの支持でclaimを維持できる（例：30.5Bと30Bが混在して30.5B側だけが除外されても，30B側の支持でsupported_primaryを維持）．検査は1つのclaimのすべてのsupportsへ個別に適用するため，資料ごとに精度の異なる値が混ざって支持がすべて除外されたc001型のclaimは降格する．

このチェックは上記Qwen実行の監査で観測された失敗形状のみを対象とする狭い範囲であり，汎用の数値・比較セマンティクスではない．異なる値の検出（95対90），条件・benchmark範囲の省略，言い換えによる強化は依然として同一モデルの別context照合と人の再確認に依存する．決定の範囲と代替案は[ADR-0002](adr/0002-claim-citation-consistency.md)に記載する．offline test suite（`tests/test_evidence.py`）に，検出される失敗形状と，忠実な数値・比較表現を誤検出しないための偽陽性保護テストを追加した．live経路の再実行は未実施であり，本強化の適用後も上記Qwen実行の調査品質評価は変更していない．
