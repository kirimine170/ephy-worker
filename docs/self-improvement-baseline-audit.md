# 自己改善実験の現状監査とベースライン

監査日：2026-09-23．対象は本repositoryのWindows作業tree．本報告はコード，skill，依存関係，設定を変更せず，既存実装，既存ログ，現在のoffline検証だけを監査した結果である．後続のencoding実験結果は`docs/self-improvement-encoding-experiment.md`に分離している．

## 冒頭要約

1. **今，実行で確認できていること**：ephy-worker本体は，公開Web調査を計画し，SearXNG／Tavily検索，公開HTML／PDF取得，根拠抽出，別context照合，最大1回の追加検索，成果物保存までを有限予算で実行する．Pi側では，gpt-oss leadがQwen workerへ隔離worktree内の実装を委譲し，patchと検証logを残すbackground jobが実行済みである．repo-local skillは次のPi sessionで発見され，別のread-only sessionで実際に読み込まれた．
2. **自己改善の経路として，まだつながっていない部分**：課題から不足能力を一般的に判定する機構，coding中の専用Web検索tool，調査結果から新しい道具を選択・実装するpolicy，獲得物を別Jobで実際に使って改善したことを示すtransfer評価は未確認である．既存のself-improvement skillはproposal-onlyの指示層であり，worker本体の自律機能ではない．
3. **現在の実測値**：Windowsで`PYTHONUTF8=1`を指定したoffline pytestは`276 passed，1 skipped，52.088秒`．指定なしでは`271 passed，5 failed，1 skipped，52.177秒`で，主因はCP932／UTF-8境界である．Phase 2の3-process fixtureはsearch／extractの両Jobを`completed`，model request 0まで確認したが，SQLite `manager.db-shm`のcleanupでexit 1となった．実モデルresearchはprivate configと`EPHY_*`設定が現在なく，新規実行条件を満たさないため未実行である．
4. **代表実行から分かったボトルネック**：最初のself-improvement JobはQwenがskill文書を作り適用まで進んだが，不足能力はJob入力で人間／leadが指定しておりworker自身の判定ではない．検証も主にscopeと`git diff --check`で，成果物の挙動を試すtestはなかった．次Jobでskillが発見されることは確認できたが，そのskillが自動的に一連の改善行動を起こしたという因果は未証明である．
5. **次の最小実験**：新しいPi sessionから，既にHEADへ入ったself-improvement skillの内容をJob promptへ再掲せず，Windows既定localeで再現する1件のencoding不具合を同一commandでbaseline／candidate比較する．skillの自動発見・読込，単一仮説，隔離実装，独立gate，非自動適用までを1件で確認する．runnerのPython pathだけを先に固定する．

## 1．対象repositoryとGit状態

### 事実

- 対象repository：本repositoryのWindows作業tree．remoteは`https://github.com/kirimine170/ephy-worker.git`．同じremoteのcleanな別cloneも存在するが，こちらは`main`の比較用であり，現行実験worktree群を所有していない．
- branch：`windows-self-improvement-mvp`．HEAD：`bd62feda41eeca867e8a98c0b99802a54fbf07d8`（`Add Windows self-improvement MVP contract`）．`main`／`origin/main`は`7adbbf3ca04fadbb82740e98a2bd9b29da8b33cf`．
- 監査開始時からdirtyである．追跡済み変更は`.agents/skills/ephy-worker-self-improvement/SKILL.md`，同`references/eval-contract.md`，`docs/self-improvement-mvp.md`．未追跡は`scripts/validate_self_improvement.py`と`tests/test_validate_self_improvement.py`．これら5件は既存作業であり，本監査は変更していない．
- HEADに含まれる自己改善機能はskill，評価契約，MVP説明の3文書だけである．独立validatorとそのtestは現在の作業treeにはあるが未コミットであり，HEADから作る新しい隔離worktreeには入らない．
- 適用指示は`AGENTS.md`．日本語句読点，dirty保持，test，repository validation，秘密情報禁止，自動merge／push／deploy禁止を確認した．

### 証拠

- `AGENTS.md`
- `.ephy/project.yaml`
- `.agents/skills/ephy-worker-self-improvement/SKILL.md`
- `.agents/skills/ephy-worker-self-improvement/references/eval-contract.md`
- `docs/self-improvement-mvp.md`
- `git branch -vv`，`git status --short --branch`，`git worktree list --porcelain`の2026-09-23実行結果

## 2．現在の実行構成

### 2.1 Codex，worker，推論環境の区別

| 層 | 確認結果 | 証拠／制約 |
|---|---|---|
| 本監査のCodex shell | Windows build 26200，x64，PowerShell 7.6.5．同じPCのworkspaceを読むが，Codexモデルの推論hostは到達不能であり不明 | shell実測．書込可能範囲は今回のworkspace等に制限され，外部networkも制限される |
| Windows host | Intel Core i9-11900K．RTX 3090 Ti 24564 MiB，driver 591.86．監査時GPU使用量17557 MiB | registry，`nvidia-smi`．物理RAM照会はAccess deniedで，本監査からは再確認不能．128 GBは利用者申告と`../benchmark.md`の過去記録 |
| ephy-worker本体 | 監査時にworker processなし．private worker configと`EPHY_*`／`TAVILY_*`環境変数なし | process一覧，環境変数名の有無だけを確認．秘密値は取得していない |
| Pi dual local | Pi 0.86.1，llama.cpp 0.4.1-dev build 11064／commit `a894dae93`．routerは`127.0.0.1:18080`でhealth ok | `../pi-windows-x64/pi.exe --version`，`../llama-cpp/llama-server.exe --version`，router API |

### 2.2 モデル構成

現在のPi routerで実際に`loaded`だったのは次の2モデルである．これはephy-workerの現在のprivate profileではなく，Pi coding harnessの構成である．

| 役割 | model | 量子化／配置 | context／出力上限 |
|---|---|---|---|
| lead | `gpt-oss-20b-MXFP4` | `MXFP4 MoE`，`n-gpu-layers=auto` | server 32768，Pi provider max output 8192 |
| implementation worker | `Qwen3-Coder-Next-Q4_K_M` | `Q4_K - Medium`，`cpu-moe=true`，`n-gpu-layers=auto` | server 32768，Pi provider max output 4096 |

両モデルともKV cacheはK/V `q8_0`，flash attention on，parallel 1である．router cacheには`ggml-org/Qwen3.8-27B-GGUF:Q4_K_M`も表示されたが`unloaded`であり，今回利用モデルに数えない．設定は`../dual-models.ini`，`../.pi-dual/extensions/dual-provider.ts`，実状態はrouterの`/v1/models`で確認した．

ephy-workerの過去の実モデル検証は別環境である．`docs/phase1-validation.md`によれば，2026-09-14のmacOS arm64で`qwen3-coder-30b-a3b`，Q4_K_M相当，llama.cpp，32768 context，prompted JSON，temperature 0.2，max output 4096を使った．現在のWindowsで同じprofileが構成済みである証拠はない．

### 2.3 harness，adapter，tool経路

- ephy-workerはPython 3.12–3.14向けCLI，package version 0.1.0．現在の`.venv`はPython 3.12.14，uv 0.12.17．主要依存は`pyproject.toml`／`uv.lock`で固定される．
- モデルadapterは`src/ephy_worker/models.py`のOpenAI互換Chat Completions＋Pydantic AI typed outputである．モデルへshell，file，credential，URL fetch toolを渡さない．`src/ephy_worker/research.py`のpromptにも「外部toolsはありません」と明記される．
- Web検索は`src/ephy_worker/search.py`のSearXNG，または`src/ephy_worker/tavily.py`のTavilyであり，Python orchestrationが検索を呼ぶ．モデルがcoding中にsearch toolを自由に呼ぶ方式ではない．HTML／PDF取得は`src/ephy_worker/fetch.py`が公開HTTP(S)だけをDNS固定，redirect再検査，容量制限付きで処理する．
- Pi lead／Qwen coding workerの標準toolはread，bash，edit，writeである．leadにはsubagentとbackground job toolもある．既存coding logに専用search／page toolの使用はなく，coding中のworker自身による管理されたWeb検索は**未実装または少なくとも未確認**である．bash経由の任意network利用可能性を専用検索能力の成功と数えない．
- test経路は`.venv\Scripts\python.exe -m pytest`，`.venv\Scripts\ruff.exe`，`scripts/validate_repository.py`，`scripts/validate_phase2_processes.py`．

### 2.4 skillの保存と次Jobへの反映

- project skillは`.agents/skills/ephy-worker-self-improvement/SKILL.md`に保存される．Pi 0.86.1はtrusted projectの`.agents/skills/`を起動時にscanし，name／descriptionをsystem promptへ入れ，必要時にfull `SKILL.md`をreadする．根拠は`../pi-windows-x64/docs/skills.md`．
- 実際に，2026-09-22T19:24:56Zの新しいPi sessionのsystem messageへskill名，description，absolute locationが入った．別の2026-09-22T15:30Z read-only sessionでは`SKILL.md`と`eval-contract.md`をreadした．
- ただし，後続の実装Jobがskillを自動readし，その指示によって動いた証拠はない．Job prompt自体が役割，仮説，gateを再掲していたため，skill利用との因果を分離できない．
- HEADに保存済みのskillは新しいworktreeでも利用できる．現在未コミットのvalidatorは新しいHEAD-based Jobへは反映されない．

### 2.5 timeout，retry，上限，権限境界

| 対象 | 実装済み上限 |
|---|---|
| ephy-worker research | 既定10 query，20 search request，12 source，36 fetch，40 model request，900秒．request 120秒，parser 20秒／512 MiB，HTML 5 MiB，PDF 20 MiB／100 page，Job cache 128 MiB |
| model schema | 1 callにつきschema retry最大1．引用修正も最大1回．全callは共通model request予算へ算入 |
| search | SearXNG retry最大1．Tavilyは課金不確実性のためretry 0，1 Job最大10 credit |
| research loop | 初回＋不足理由付き追加round最大1 |
| Pi background Job | whole-job timeout 5–480分，既定60分．同時active Job 1件．同一仮説のrepair既定2，設定可能0–3．timeout／cancelあり |
| Pi context | 32768．compactionはreserve 8192，recent 6000．明示的な総turn／総tool-call上限は確認できない |

ephy-workerの出力先は全Git checkout外が必須で，Job directoryを新規作成し既存成果物を上書きしない．Pi background jobはGit worktreeで隔離するが，Qwenにはbash／writeがあり，OS sandboxによるworktree外書込拒否を実装した証拠はない．安全境界は主にprompt，Git snapshot，scope gate，ユーザー確認である．自動commit，merge，push，deployは行わない．

## 3．能力のつながり

「実装」はコード／設定に経路があること，「実行確認」は実ログまたは今回の実行で通ったことを表す．

| 段階 | 実装 | 実行確認 | 判定 |
|---|---|---|---|
| 課題の受領 | ephy-worker CLIは`--question`，Pi Jobはtask／doneWhenを受ける | research実行記録，background Job 4件あり | 確認済み |
| 完了条件の把握 | background Job schemaにdoneWhen／verificationCommandsあり | Job recordへ保存され独立runnerが実行 | 確認済み |
| 現在使える能力の確認 | Pi promptはAGENTS，tools，available skillsを提示する | AGENTSとskill descriptionがsession system messageへ入った | 一部確認 |
| 不足能力の特定 | researchでは根拠不足を`gaps`とadditional queryへ変換する．generic coding能力gap evaluatorはない | 根拠gapから追加roundのmock／過去実行はある．自己改善Jobの仮説は外部入力 | research限定で確認，generic未実装 |
| インターネット調査 | ephy-worker researchのSearXNG／Tavily経路あり | 過去MacでTavily doctor成功，Qwen researchはSearXNG CAPTCHA．Pi coding workerの専用検索は未確認 | researchのみ一部確認 |
| 既存手段／新規実装／設定変更の選択 | 明示的policy実装なし．lead promptが判断を担う | 初回Jobでは新skill作成が指定済みで，自律選択ではない | 未確認 |
| 隔離環境で実行・検証 | Pi worktree，patch，verification log，timeout／cancel，ephy-worker offline fixtureあり | background Jobと今回のoffline testで確認 | 確認済みだがWindows runner不具合あり |
| 再利用可能な成果物へ保存 | repo-local skill，Git patch，Job log，research report／metrics | skillをHEADへ適用，report artifactの過去実行あり | 保存確認済み |
| 別Jobで読込・利用 | Piは次sessionでskillを発見可能 | discoveryと1回の明示readは確認．別Jobの成功への利用は未確認 | 部分確認 |

不足能力を文章で説明しただけのものは成功に数えていない．現状は「研究Job内の不足根拠→追加検索」と「coding Jobの明示課題→隔離実装」は存在するが，両者を一般的な能力獲得loopへ接続する実装はない．

## 4．既存評価と今回の測定

### 4.1 worker本体のunit／integration test

| 日時／対象 | command／条件 | 結果 | 分類 |
|---|---|---|---|
| 2026-09-23，HEAD＋既存dirty | `PYTHONUTF8=1`，`.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`，basetempはrepo外 | 276 passed，1 skipped，52.088秒 | 現在のoffline unit／integration．実モデル能力ではない |
| 同上 | `PYTHONUTF8`指定なしの同suite | 271 passed，5 failed，1 skipped，52.177秒 | 実行不合格．CP932 decode 4系統とmissing-Ruff subprocess出力decode 1件 |
| 同上 | Ruff `src tests scripts/validate_self_improvement.py` | 2件失敗．既存`tests/test_init_repository.py`と`tests/test_validate_repository.py`のimport順 | lint不合格．候補変更起因とは未判定 |
| 同上 | `scripts/validate_repository.py --check-sensitive-patterns`，`git diff --check` | 両方exit 0 | repository gate合格 |
| 同上 | `scripts/validate_phase2_processes.py` | search／extractの2 Jobはcompleted，3 process，model request 0．終了時SQLite `manager.db-shm` cleanupでexit 1，3.486秒 | 中間処理成功だがcommand全体は不合格 |

テスト実行前後で既存5ファイルのGit状態は変化していない．この報告書だけが本監査の追加物である．

### 4.2 Fake／Mockによる経路検証

- `tests/test_research.py`はFakeModel／FakeSearch／FakeFetcher／FakeCollectorでplan，追加round，失敗，取消，wall timeout，引用修正を検証する．
- `tests/test_tavily.py`はmock HTTPで無料枠，credit，timeout，秘密値非表示，full workflowを検証する．
- `tests/test_manager*.py`，`tests/test_worker_service.py`，`scripts/validate_phase2_processes.py`はManager／Worker／lease／artifact／search／extractをmodelなしで検証する．
- これらの合格を実モデルの課題解決能力またはWeb live完走の証明には数えない．

### 4.3 実モデルを使った過去のresearch評価

以下は`docs/phase1-validation.md`に記録された2026-09-14のmacOS／commit `8dde2087...`系の過去構成であり，現在のWindows構成の実測ではない．評価定義はreport schema 0.1と固定質問である．成果物本体は当時のGit外`local-data/worker-research/reports/<Job ID>/`にあり，このWindows workspaceからは到達確認できない．

| Job | 内容／完了条件 | 結果 | 時間／使用量 | 検索・追加情報 |
|---|---|---|---|---|
| `research-d62ed9a43c61467893a4b7385d67664f` | Qwen3-30B-A3Bの公式情報を検索から発見して照合 | failed，`no_readable_sources` | 7.503秒，search 4，fetch 0，model 1．token不明 | SearXNG CAPTCHA．人のURL補足なし |
| `research-58a3e53dc5c44115b2ae2bdca4033161` | 同質問を既知の公開資料3件で本文処理 | partial，`no_new_evidence`．HTML根拠採用，PDF根拠未達 | 94.653秒，search 8，fetch 3，model 3．token不明 | 人がQwen公式blog，arXiv PDF，Hugging Face model cardを追加 |
| `research-22cd85b7fa5b4792a73a8aa38109905d` | 引用修正後の同質問 | partial，`no_new_evidence`，exit 2．5引用は原文offset一致したが，Codex監査で主要5 claim中少なくとも2件に精度混同／比較過大解釈 | 174.717秒，search 8，fetch 3，model 4，input 41285／output 4252 token | 8検索は全てCAPTCHA．同じ3資料を人が補足．Codexの後付け監査はworker成績へ加点しない |

成功率は分母の定義がないため算出しない．上記3件だけなら，research全体の完遂は`0/3`，うち2件は補足URL付きpartialである．少数試行かつ過去構成なので汎用能力の証明ではない．

### 4.4 Pi自己改善background Job

| Job | base | 状態 | 証拠上の主因 |
|---|---|---|---|
| `bg-20260922144711-70a4d3` | `7adbbf3...` | applied | skill／契約／文書を作成．scopeとdiff checkは合格．Codex auditはinitial reject後にeligibleへ訂正し，人が適用 |
| `bg-20260922182343-bd6d4b` | `bd62fed...` | verification_failed | verification commandのCLI契約誤りとWindows実行形の不備．agentの成功文と独立結果が不一致 |
| `bg-20260923-self-improvement-rerun` | `bd62fed...`，dirty at submit | failed | gpt-oss response budget／compaction recoveryで編集前に停止 |
| `bg-20260923-self-improvement-rerun2` | `bd62fed...`，dirty at submit | cancelled | 4検証がoffline `uv`でPythonを発見できずexit 2．repair開始後にcancelし，patch／logを保持 |

`../.pi-dual-runtime/audits/self-gate-evidence.json`では，現在のdirty candidateに対する独立gateがcandidate test 13 passed，repository，Ruff，scope，diff checkすべてpassを記録する．ただしbaseline側はtest file不存在でexit 4であり，gpt-oss read-only audit自体はcompaction failureで完了していない．このため「candidate hard gateの機械判定」は確認できるが，「lead監査まで含む一続きの成功」には数えない．

## 5．代表実行の追跡

対象：`bg-20260922144711-70a4d3`．

| 対応 | 実際の証拠 |
|---|---|
| 要求 | `TASK.md`でephy-worker self-improvement MVP skill作成，3ファイル限定，Git statusを完了条件として明示 |
| workerが不足と判断した能力 | **該当なし**．skill不足はJob入力で既に決められており，worker自身が能力棚卸しから導いた判断ではない |
| 判断根拠 | 外部taskとdoneWhenのみ．generic gap analysis logなし |
| 利用した知識 | Qwenは`.ephy/project.yaml`と既存docsを読み，AGENTSをsystem contextで受領．専用Web検索なし |
| 操作／作成物 | gpt-ossがsubagentを1回呼び，Qwenが43 tool call（bash 30，read 8，write 4，edit 1），39 turnsで3文書を作成 |
| 独立検証 | runnerの`git status --short --untracked-files=all`と`git diff --check`はexit 0．挙動testはなし．Codex auditは一度判断訂正あり |
| 次Jobへ残ったもの | 3文書のpatchが適用され，後にcommit `bd62fed...`へ保存．後続Pi sessionでskill discoveryを確認 |

実行時間はJob作成からappliedまで約17分48秒．agent sessionは553.313秒．gpt-oss親sessionはinput 3051／output 570 token，tool call 1（subagent）．Qwenはinput 8764／output 5743 token，cache read 332236，39 turns，43 tool call．provider logにtokens/secはなく，VRAM／RAMもJob単位で記録されていないため不明である．追加の人間修正回数はJob logから一意に確定できない．

この実行のボトルネックは実装速度ではなく，仮説選定が外部指定であること，検証が成果物の存在／scope中心であること，そして次Jobでの利用を測っていないことである．

## 6．次の最小実験

### 提案

**既存skillの別Job reuseと，Windows locale不具合の単一仮説修正を1件で測る．**

### 評価したい不足能力と根拠

- 評価対象：前Jobで獲得・保存したrepo-local skillを新しいsessionが自動発見し，full skillを読み，明示的な改善workflowとして行動へ結びつける能力．
- 根拠：skill descriptionの次session提示と明示readは確認したが，後続実装Jobのpromptがskill内容を重複しており，skillの再利用が成功へ寄与したか判定できない．

### 課題入力

新しいPi sessionをcommit `bd62fed...`のclean worktreeで開始し，次だけを依頼する．skillの手順はtaskへ再掲しない．

> Windows既定localeで`tests/test_cli.py::test_cli_help_and_missing_profile`がUTF-8出力をCP932として読むため失敗する．この1件を，process全体の`PYTHONUTF8`設定へ依存せずlocale非依存にする自己改善proposalを作成してください．

### 環境・資源制約

- gpt-oss lead，Qwen worker，32768 context，lead medium，whole-job 60分，repair最大2．モデル／sampling／network条件をbaselineとcandidateで固定する．
- allowed fileはまず`tests/test_cli.py`だけ．必要性を証拠で示せないscope拡張は不合格．自動applyなし．
- baseline／candidateのcommandは同じ`.venv\Scripts\python.exe -m pytest -q tests/test_cli.py::test_cli_help_and_missing_profile -p no:cacheprovider`．`PYTHONUTF8`と`PYTHONIOENCODING`は未設定に固定する．
- private testと参照解はworkerのprompt，filesystem，検索対象から分離する．

### 客観的完了条件

1. 新sessionのsystem messageにskillがあり，agent logにfull `SKILL.md`のreadがある．
2. baselineは同じ既知のdecode failureを再現する．candidateは同一commandでexit 0．
3. repository validation，対象file Ruff，`git diff --check`，scopeがpassする．未実行gateはpass扱いしない．
4. candidateが環境変数，global locale，runner commandを変更してtestを隠していない．
5. logに仮説，根拠，既存手段で十分かの判断，baseline／candidate，追加指示回数を残す．

### 既知の参照解

- evaluatorの参照はPython 3.12.14 Library Reference「`subprocess`」の`subprocess.run`仕様．`encoding`／`errors`指定時は指定encodingでtext modeを開き，指定しない`text=True`は`io.TextIOWrapper`既定へ依存する．出典：`https://docs.python.org/3.12/library/subprocess.html`，版3.12.14，確認日2026-09-23．
- 参照実装はworkerへ渡さない．明示`encoding="utf-8"`等と異なる実装でも，固定commandとprivate variantが通り，locale環境の変更やdecode errorの握り潰しがなく，UTF-8内容が正確に検査されれば正解とする．

### 改善前後と再利用の確認

- 改善前後は同じcommit起点，同じmodel，context，timeout，検索条件，commandで比較する．
- 既存skillを作ったJobとは別の新sessionで今回の課題を行うこと自体がtransfer probeである．Job promptへskillのworkflowを再掲しない．skillを発見しただけでなく，read後に固定baseline，限定scope，独立gate，proposal-only停止まで実行した場合だけreuse成功とする．
- 次の別課題用に新しいskillやhelperを多数作ることは要求せず，加点もしない．既存手段で解けるとの判断を認める．

### 最低限必要な追加

runnerのoffline Python解決だけを修正する．具体的には既存`.venv\Scripts\python.exe`を直接使うか，`UV_PYTHON`を実在するPython 3.12.14へ固定する．直近Jobのようにtoolchain未実行なら，能力不足ではなく環境blockedとして終了する．coding中の専用Web検索toolの評価はこの1件へ混ぜず，別実験とする．

## 7．証拠の所在

- 設計／実装境界：`README.md`，`docs/architecture.md`，`docs/self-improvement-mvp.md`
- 過去のresearch評価：`docs/phase1-validation.md`，`docs/phase2-validation.md`，`docs/tavily-validation.md`
- 予算／設定：`src/ephy_worker/schema.py`，`src/ephy_worker/config.py`，`src/ephy_worker/budget.py`
- research接続：`src/ephy_worker/research.py`，`src/ephy_worker/models.py`，`src/ephy_worker/search.py`，`src/ephy_worker/tavily.py`，`src/ephy_worker/fetch.py`
- 成果物保存：`src/ephy_worker/store.py`
- Pi構成：`../start-dual-pi.ps1`，`../dual-models.ini`，`../.pi-dual/orchestrator.md`，`../.pi-dual/agents/qwen-worker.md`，`../pi-windows-x64/docs/skills.md`
- background Job record：`../.pi-dual-runtime/jobs/<Job ID>/job.json`，`TASK.md`，`agent.jsonl`，`verify-*.log`，`changes.patch`
- self-gate記録：`../.pi-dual-runtime/audits/self-gate-evidence.json`，`self-gate-audit.stderr.log`

## 判定

**事実**：限定されたresearch loop，Piの隔離実装・patch・gate，skillの永続化と次sessionでのdiscoveryは存在する．

**推測ではなく未確認とする事項**：genericな不足能力の自律判定，coding workerの管理されたWeb検索，調査から能力実装への選択，skillが別Jobの成功を改善するend-to-end transfer．

**提案**：上記1件のlocale修正Jobでskill reuseの因果とrunner境界を先に確かめる．今回は実装へ進まない．
