# ephy-node-preflight

複数PCへ `ephy-worker` を配置する前に，端末ごとのローカル前提条件を比較する，読み取り専用の小さなCLIです．Python標準ライブラリだけで動作します．自動インストール，サービス起動，Worker登録，通信確認，Job実行は行いません．

**現段階では独立した補助ツールです．ephy-workerへの統合，正式なmanaged Pi workflow，採用承認は未実施です．**

## 既存doctorとの違い

既存の `ephy-worker doctor` は，設定した検索provider／モデルに接続し，型付き出力や一時ファイルの書込みを確認します．このCLIは，それ以前の「何が存在し，何が不足し，何が未確認か」をオフラインで整理します．doctorを置換しません．

参考にした正本は `kirimine170/ephy-worker` の commit `0f6c3cb7a272bc67e2a0a0dc828e579df6823d98` です．

- [既存doctor](https://github.com/kirimine170/ephy-worker/blob/0f6c3cb7a272bc67e2a0a0dc828e579df6823d98/src/ephy_worker/diagnostics.py)
- [分散契約](https://github.com/kirimine170/ephy-worker/blob/0f6c3cb7a272bc67e2a0a0dc828e579df6823d98/src/ephy_worker/contracts.py)
- [Worker登録処理](https://github.com/kirimine170/ephy-worker/blob/0f6c3cb7a272bc67e2a0a0dc828e579df6823d98/src/ephy_worker/worker_service.py)
- [Python／依存version](https://github.com/kirimine170/ephy-worker/blob/0f6c3cb7a272bc67e2a0a0dc828e579df6823d98/pyproject.toml)
- [Phase 2実機検証の範囲](https://github.com/kirimine170/ephy-worker/blob/0f6c3cb7a272bc67e2a0a0dc828e579df6823d98/docs/phase2-validation.md)
- [システム開発ガバナンス](https://github.com/kirimine170/ephy-worker/blob/0f6c3cb7a272bc67e2a0a0dc828e579df6823d98/docs/system-development-governance.md)

既存の分散能力は `web.collect`，契約 `0.4`，mode `search`／`extract`，Worker同時実行数 `1` です．このツールは新しい登録能力を作らず，結果の `advertised_capabilities` は常に空，`dispatch_eligible` は常に `false` です．upstreamのコード本文は同梱せず，契約上の事実と照合hashを参照しています．

## すぐ試す

Python 3.12–3.14を使用してください．追加packageのインストールは不要です．ZIPをローカルdirectoryへ展開し，そのdirectoryから実行します．`-B` はPython bytecode cacheの生成を抑えます．

### Linux／macOS

```bash
python3 -B node_preflight.py --workspace . --profile collect
python3 -B node_preflight.py --workspace /absolute/local/workspace --worker-source /absolute/local/ephy-worker --profile collect --profile test --format both
```

2行目の `--worker-source` は省略可能です．Linux／macOSでは既存sourceの固定3ファイルを照合します．sourceを指定しない場合，collect/testはsource確認を `not_checked` とします．shellの行末継続記法は不要です．

### Windows PowerShell

```powershell
py -3.12 -B .\node_preflight.py --workspace . --profile collect --profile test --format both
py -3.12 -B .\node_preflight.py --sample .\samples\prerequisites-present.json --profile collect --profile test --profile render --format both
```

`py` launcherがない環境では，使用するPython 3.12–3.14の `python` commandへ置き換えます．ツールが調べるpackageは，**実行しているPython環境**のものです．ephy-workerの既存 `.venv` を診断したい場合は，そのPython実行ファイルで起動してください．新規環境の作成や `uv sync` はこのCLIから行いません．

**Windowsのsource hash確認は未対応です．** Python標準APIだけではdirectory handle基準の安全な子path探索を共通実装できないため，`--worker-source` を指定しても `unknown / unsupported_platform` とします．junction／reparse pointを辿って確認済みとする代替動作は行いません．OS，CPU，RAM，disk，package，PATHの測定は別に行います．Windowsのcollect/testはsource確認が残るため，ほかの条件がすべて揃っていても `incomplete` になります．sampleでの通過は実機確認ではありません．

### JSONの保存と比較

```bash
python3 -B node_preflight.py --workspace . --profile collect --format json > node-report.json
python3 -B node_preflight.py --workspace . --profile render --min-free-gib 5 --min-available-gib 2 --format both > node-report.json
```

`both` はJSONだけをstdout，短いsummaryをstderrへ出します．ファイル保存は利用者のshellによる明示的なリダイレクトです．CLI自身には出力ファイルの書込み処理がありません．PowerShell 5系のリダイレクトはUTF-16等になる場合があるため，保存後の利用先でUTF-8が必要ならshell側の文字encodingを確認してください．

`--min-free-gib` の既定値は計画上の **1 GiB** です．`--min-available-gib` は既定では設けません．いずれも利用者の計画用閾値であり，ephy-workerや個別Jobの必要容量を実測・保証した値ではありません．

## Profile

| profile | 調べるローカル前提 | それだけでは分からないこと |
|---|---|---|
| collect | Python範囲，workspace，disk，固定source，正本の直接runtime依存のmetadata version | 検索provider／Manager設定，credential，TLS／tunnel，実Job |
| test | collectの項目，GitのPATH存在，正本のdev依存metadata | test結果，Git実行，detached worktree，sandbox，正式workflow |
| render | Python範囲，workspace，disk，reportlab metadata，pdftoppm／pdftotextのPATH存在 | 実際のPDF生成／変換，フォント，日本語表示，成果物品質 |

`test` と `render` はこの補助CLIのローカルprofile名です．ephy-workerの分散能力ではありません．`render` はPDF／reportの確認作業を想定し，3D／GPU renderingやLLM推論を意味しません．collectにGPU，Git，Node，npm，モデルを必須としていません．

## 結果の読み方

観測値は固定キーの `{status, value, reason}` です．

- `observed`：そのAPIで値を取得した．存在と動作は別です
- `unavailable`：指定対象やpackageが見つからないなど，欠落を観測した
- `unknown`：API失敗，未対応OS機能，解釈できないmetadataなどで判断できない
- `not_checked`：明示的に未検査．例：`--worker-source` なし

非 `observed` の `value` は必ず `null` です．欠落を `0` や成功へ置き換えません．

| overall／profile status | 意味 | exit code |
|---|---|---|
| preflight_passed | 選択profileのローカル前提の観測だけが通過した | 0 |
| blocked | 少なくとも一つの前提が不足／範囲外／source差分あり | 1 |
| incomplete | 明確な不足はないが，必要な項目がunknown／not_checked | 3 |
| 入力／診断エラー | 固定形式に違反，読めない入力など | 2 |
| 取消 | KeyboardInterrupt | 130 |

複数profileでは `blocked` を優先し，次に `incomplete` とします．**exit 0でもWorkerが運用可能とは主張しません．** 実行していない項目はreport末尾の `not_checked` に残ります．sourceが更新されてhashが異なる場合は，互換性が壊れたと断定せず，この固定referenceでは未承認の差分としてblockします．

## 測定範囲とプライバシー

読み取る対象は以下に限定します．

- OSの種類，正規化architecture，Python version，論理CPU数，可能なOSのCPU affinity数
- Linuxの `/proc/meminfo` の固定2項目，Windowsの `GlobalMemoryStatusEx`，macOSの `hw.memsize`
- 明示したworkspace directoryのmetadata，`os.access` のaccess hint，そのfilesystemの空き容量
- 固定6 command名のPATH存在確認：git，uv，node，npm，pdftoppm，pdftotext．実行しません
- 固定12 distributionのversion metadata．package本文をimportしません
- `--worker-source` 指定時だけ，固定3 sourceファイルの内容hash．設定や `.git` を読みません

sourceは各ファイル128 KiBまで，sampleは64 KiBまでです．POSIXのsource探索は保持したdirectory descriptorに相対的に `O_NOFOLLOW`／`O_NONBLOCK` を使い，各要素を開きます．source rootの最終要素がsymlinkの場合も確認を拒否します．CRLFだけをLFへ正規化してhashを計算します．Windows sampleはnative handleで最終要素のreparse pointと非disk／非regular対象を拒否します．これらは許可された通常ファイルの限定読取りであり，任意に変更され続けるfilesystemを完全に固定するsandboxではありません．

reportにはhostname，ユーザー名，workspace／実行ファイルの絶対path，credential，環境変数一覧，source本文を含めません．CLI引数エラーも固定codeにします．tool検索ではPATHを参照しますが，その内容を出力しません．network client，subprocess，model download，SSH設定変更，private driveの再帰scan，package install，ファイル変更は実装していません．

ローカルのworkspace／sample／source／Python環境を指定してください．OSが提供するnetwork mount，mapped drive，PATHやPython metadata上のnetwork filesystemまで，このCLIは判定・遮断しません．明示的なnetwork requestを開始しないことと，OSのfilesystem I/Oが完全にローカルであることは別です．通常の信頼できるPython環境から使用してください．

## 正確さの限界

- 実行済みの実機確認は今回のLinux cloudだけです．Windows／macOSはunit testでAPIをmockした範囲です
- CPU数，RAM，diskはhost／filesystemから見える値です．container cgroup，VM，scheduler，quota，競合負荷による実際の配分量を保証しません
- macOSの利用可能RAMは，圧縮／回収可能pageの意味を推定しないため `unknown` です．RAM下限を指定した場合は `incomplete` になります
- `os.access` は書込試験ではありません．ACL，race，read-only mount，Git外出力先というrepo要件は別に確認してください
- PATH存在はtool versionや正常実行を証明しません．distribution metadataもimport成功，binary ABI，lockfile一致，extras／推移的依存の解決を証明しません
- prerelease／dev／local versionなど単純な数値releaseでないmetadataは，比較を推測せず `unknown` にします
- Python実行環境自体がalpha／beta／release candidateの場合も，数値だけでfinal releaseと扱わず `unknown / non_release_version` とします．ほかの必須条件が揃っていても `incomplete` になります
- sourceの固定3ファイル一致は，checkout全体のcommitや，実際にimportされるworker sourceを証明しません
- GPUの有無，driver，VRAM，LLM model容量を測定せず，GPU名から実行能力を推定しません
- 2台の実通信，OS別停止処理，実検索，引用品質，coding隔離，formal governance gateは未検証です

## 再現用sampleとtest

同梱sampleはすべて架空の固定データで，このPC／利用者の実機情報ではありません．

- `samples/prerequisites-present.json`：全profileのローカル前提が揃った合成例．exit 0
- `samples/missing-dependencies.json`：packageとPDF tool不足の合成例．exit 1
- `samples/windows-unverified.json`：Windows source未確認の合成例．exit 3
- 対応する `*.expected-report.json` は3 profile同時指定時の正規化出力です

```bash
python3 -B node_preflight.py --sample samples/prerequisites-present.json --profile collect --profile test --profile render --format json
python3 -B -m unittest discover -s tests -v
```

sample modeはhost probeを呼びません．未知キー，欠損キー，重複JSONキー，NaN／Infinity，不正な型，負の容量，不整合容量，過大入力を拒否します．sampleにcommandやpathを埋め込んで実行させる機能はありません．

unit testは一時directoryとfixtureを作ります．この点は，読み取り専用の診断CLI自体と異なります．POSIX descriptor探索だけのtestは，Windows上では明示skipされる設計です．今回のLinux testではskipはありませんでした．

static controlは，レビュー済みのimport／call allowlistと，読取専用open mode／flagsを確認します．HTTP client，process，書込み，削除，touch，alias等の不正controlはASTとして検査し，実行しません．これは固定されたsourceに対する回帰検査であり，任意のPython programを安全に実行するsandboxや，正式な監査の代替ではありません．新しいAPIにはcheckerとsourceのレビューが必要です．

## 次の統合作業

1. 使用予定の各PCの既存Python環境からこのCLIを実行し，不足と未確認を確認する
2. Windows／macOS実機でCLIとtestを実行し，Windowsの安全なsource検証方法は別途設計・検証する
3. source referenceを更新するときは，契約，依存範囲，hashの根拠をレビューして更新する．自動追従させない
4. repoへ統合する場合はephy-workerのガバナンスに沿った別candidateと検証を用意する．このZIPは正式workflowの代わりにならない
5. 本来のdoctor，暗号化tunnel／TLS，2台間のfixture collect，取消／切断／再試行を個別に実行して確認する

本ツールは以上を自動実行しません．Runtime／Karteには触れません．今回の実行結果と未実行項目は `VERIFICATION.md` を参照してください．
