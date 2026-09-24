# Coding executorとevaluation harness

## Scope

この機能は`ephy-worker`単体でcoding modelを比較するためのMVPです．Runtime，Managerの`web.collect`契約，Karteには接続しません．対象repositoryにはcandidate patchだけを生成し，自動commit，merge，push，deploy，worker再起動は行いません．`ephy-worker`自身も通常のtarget repositoryとして指定できます．

通常のJob実行はmodel weightを取得せず，組込み`mock` profileはmodel processもnetworkも使用しません．ローカル評価環境の構築手順はこの文書と設定exampleに固定し，GGUFの取得は別途明示的に行います．

## Execution flow

```text
CodingJob
  → profileとrepository/base revisionの解決
  → worker所有のdetached temporary Git worktree
  → FakePiRunnerまたはPiRpcRunner
  → candidate patchの取得
  → agentから独立したvalidation command
  → result.json，patch.diff，agent.log，validation.log
  → temporary worktreeの回収
```

`PiRpcRunner`はPiのheadless RPC interfaceである`pi --mode rpc --no-session`を起動し，LF区切りJSONLで`prompt`を送り，`agent_settled`までeventを読みます．TUIのscreen scrapingは行いません．Piの公式RPC仕様は[RPC Mode](https://pi.dev/docs/latest/rpc)を参照してください．

Pi固有の引数，event，model selectionはadapter内に閉じています．workerはagentの完了自己申告やtest成功自己申告を合否に使わず，candidate patchを取得した後に`validation_command`を別processで実行します．validation commandはagent promptへ含めないため，将来のhidden validationへ差し替えられます．

## Safety boundary

- source checkoutのdirty stateをreset，checkout，cleanしない．
- base revisionからdetached worktreeを作り，agentとvalidationのcwdをそのworktreeへ固定する．
- source checkoutへcommit，merge，pushを行わない．fixture repositoryの基点作成だけはworker所有の一時repositoryへcommitする．
- timeout／cancel時はPiまたはvalidationのprocess groupをTERM，猶予後KILLで回収する．
- patchとchanged filesを回収してからworktreeを削除する．Gitが削除を拒否した場合は診断用にworker所有pathを`result.json`へ残す．
- artifact rootは既存`JobStore`と同じくGit checkout外だけを許可する．既定は`~/.local/state/ephy-worker/eval/runs`で，`EPHY_WORKER_EVAL_DIR`または`--output-dir`で変更できる．
- Pi event logからthinking／reasoningとcredential名のfieldを除外する．

現在の作業ツリーをcoding対象にするときは，Jobへ`"source_state": "working-tree"`を指定する．workerはtracked fileの現在内容と，`snapshot_untracked_paths`へ明示した未追跡fileだけをGit外の一時repositoryへコピーし，そのbaselineからcandidate patchを作る．無視対象file，symbolic link，20 MiBを超えるsource snapshotは受け付けない．`source-manifest.json`へ各fileのSHA-256とsource HEADを残す．元のcheckoutのindex，branch，fileは変更しない．

候補patchは自動適用しない．採用前に`coding verify-source <artifact-dir> --repository <source-checkout>`で`source-manifest.json`のfile hashと現在のsource内容を照合し，`patch.diff`をreviewしたうえで別途適用する．sourceが変わっていればexit 1と差分file名を返す．

macOSで`"macos_sandbox": "write-guard"`を指定すると，Piと独立validationの両processを`sandbox-exec`で起動し，元のrepository rootと`macos_protected_roots`で指定した追加directoryへの書込みをOSで拒否する．例えばEphy workspace全体を守るには，その絶対pathを`macos_protected_roots`へ指定する．一時worktreeが保護directory内にあるJobは失敗させる．`--dry-run`には適用するprofileと保護rootが表示される．macOS以外，または`sandbox-exec`を適用できない環境では`sandbox_unavailable`として失敗し，無保護で再実行しない．既定は`off`なので，macの自己改善Jobには明示的に指定する．

```json
{
  "source_state": "working-tree",
  "macos_sandbox": "write-guard",
  "macos_protected_roots": ["/absolute/path/to/Ephy_Project"]
}
```

これは保護rootへの**書込み制限**であり，読み取り，保護root外への書込み，networkを制限しない．`sandbox-exec`のprofile言語は[Appleの担当者による説明](https://developer.apple.com/forums/thread/661939)でも第三者向けに文書化されていないとされるため，無人で任意の生成codeを走らせる最終的な隔離境界にはしない．その用途ではDocker等の独立した実行環境を別途検証する．

`network_policy`はJob schema，prompt，子process環境へ伝播します．`mock`の`offline`はnetworkを使用しません．実Pi processに対するportableなOS network namespaceやcontainer sandboxはこのMVPに含まれないため，実Piの`offline`は強制的なnetwork遮断ではありません．networkの強制隔離が必要な環境では別途検証済みcontainer／OS sandboxを使用してください．

## Model profiles

coding profileはresearch用`ModelProfile`と分けています．`profile_id`，`provider`，`model_id`，`server_type`，`context_window`，`reasoning_level`，`quantization`，任意の`endpoint`／`notes`を保持します．API keyは保存せず，Piまたはprovider側の既存設定から解決します．

組込みprofileは`mock`と`qwen3-coder-30b-a3b`です．mac向けcodingの既定経路はEphy Runtimeのcode endpointと同じllama.cpp `127.0.0.1:8083/v1`で，Piへ公開するcontextは32Kです．設定は[`configs/coding-models.example.yaml`](../configs/coding-models.example.yaml)と[`configs/pi-models.llama-cpp.example.json`](../configs/pi-models.llama-cpp.example.json)にあります．example catalogには追加で，共有router `127.0.0.1:8084/v1`を使うQwen3-Coder 30BとQwen3 8Bのprofileを置いた．routerのmodel pathはRuntime側の`configs/llama-cpp-router.ini`で管理する．Pi設定の`apiKey`はlocal provider認識用のplaceholderです．`--offline`はPiのremote catalog更新を止め，local endpointへのrequestは継続します．

旧backendの設定例と実行経路は削除した．過去の[測定記録](coding-model-validation.md)は履歴としてのみ残し，現行llama.cppの測定値として扱わない．

## CLI

mock smoke suiteは追加設定なしで実行できます．suiteは6カテゴリを各1件含み，実行ごとに小さなfixture Git repositoryを一時生成します．各taskの60秒上限はApple Silicon上のlocal model初回loadと複数tool callを含める値であり，mock runnerの時間ではありません．

```text
uv run python -m ephy_worker eval run --suite smoke --model mock
uv run python -m ephy_worker eval run --suite smoke --model mock --repeat 3 --output-dir /absolute/git-external/eval-runs
```

任意のJobは[example](../configs/coding-job.example.json)をGit外へコピーし，絶対repository path，goal，validation commandを設定します．`--dry-run`はworktreeを作らず，profile，repository，base revision，Pi invocation，validation planを検証します．

ephy-worker自身を対象にする場合も同じ`coding run`を使う．未コミットのcoding実装を含めるなら`source_state`を`working-tree`とし，必要な未追跡code／testを`snapshot_untracked_paths`へ個別列挙する．`base_revision`は現在のHEADを指定する．`validation_command`には，一時worktreeで実行できる絶対pathのPythonと`PYTHONPATH=src`を使える．

```text
uv run python -m ephy_worker coding run /absolute/private/coding-job.json --dry-run
uv run python -m ephy_worker coding run /absolute/private/coding-job.json --output-dir /absolute/git-external/eval-runs
uv run python -m ephy_worker coding verify-source /absolute/git-external/eval-runs/<run-id> --repository /absolute/path/to/ephy-worker
```

## Result and artifacts

1 executionごとに固有directoryを作り，次を保存します．

| File | Contents |
|---|---|
| `result.json` | run／task／job ID，model，base／result revision，status，validation，metrics，changed files，diagnostic |
| `patch.diff` | base revisionからのbinary対応Git diff．未追跡新規fileも含む |
| `agent.log` | sanitized Pi JSONL eventまたはdeterministic mock log |
| `validation.log` | 独立validationのstdout／stderr |
| `source-manifest.json` | working-tree snapshot時のみ．source HEADとcopyしたfileのSHA-256 |

Pi/providerが返さないturn，tool call，retry，token metricは`null`を許容します．`files_changed`，diff addition／deletion，wall timeはworkerが算出します．

failure codeは`pi_executable_unavailable`，`model_unavailable`，`model_endpoint_unavailable`，`invalid_model_profile`，`repository_unavailable`，`invalid_revision`，`worktree_creation_failure`，`pi_rpc_failure`，`sandbox_unavailable`，`execution_timeout`，`cancelled`，`validation_failure`，`internal_worker_failure`を区別します．

## macOSでllama.cppを準備する手順

既存の8083はEphy Runtimeと共有する単一モデルserverなので停止・再設定しない．複数モデルを共通のPi設定から切り替えるには，別ポート8084にllama.cpp routerを起動する．現行workspaceに存在するQwen3-Coder 30BとQwen3 8Bをpresetから読むため，model weightの移動やdownloadは不要である．preset内の相対pathは`ephy-runtime` repository rootを基準とする．他の環境ではpathを修正する．

このhostのRuntime checkout内binaryは旧配置先を`LC_RPATH`に保持するため，専用スクリプトが`DYLD_LIBRARY_PATH`を設定して起動する．詳細は[macOS初期検証](macos-self-improvement-validation.md)に記録した．

```bash
cd /Users/kirimine170/Desktop/Ephy_Project/ephy-workspace/ephy-runtime
./scripts/start_llama_router.sh
```

`--model`／`-m`を指定しないことがrouter modeの条件である．既定のmodel autoloadは残すので，headless Piがmodel IDを指定するとそのmodelが読み込まれる．`--models-max 1`は8084内の同時load数だけを抑え，8083の既存modelには影響しない．切替時はload時間がかかり，coding Jobのtimeoutにも含まれる．8083と8084で同じCoder modelを同時にloadすると，別processなのでメモリ使用量が増える．[llama.cpp router仕様](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#using-multiple-models)．routerを単体起動したterminalは開いたままにして，別terminalで確認する．Runtimeの`./scripts/start_backend_stack.sh`でもrouterを含めて起動し，管理対象のrouterなら`./scripts/stop_backend_stack.sh`で停止できる．手動起動済みの互換routerは再利用するが，一括停止では止めない．

```bash
curl -fsS http://127.0.0.1:8084/models | jq -r '.data[].id'
```

このhostの共通Pi設定`~/.pi/agent/models.json`には`llama_router` providerを追加し，旧providerを除去した．既存の`llama_cpp` 8083設定は維持した．設定を上書きせずに更新するため，変更前のbackupを`~/.pi/agent/models.json.backup.*`へ置いた．他のhostでは[`pi-models.llama-cpp.example.json`](../configs/pi-models.llama-cpp.example.json)から各providerだけを既存`models.json`へmergeする．[Pi設定directoryの仕様](https://pi.dev/docs/latest/configuration)．別terminalで次を実行する．

```bash
pi --offline --list-models llama_router
pi --offline --provider llama_router --model qwen3-8b-q6-k --no-session -p '一文で応答してください．'
pi --offline --provider llama_router --model qwen3-8b-q6-k --no-session
```

最後の行は対話型Piを起動する．その前の`-p`は短文probeであり，最初のmodel loadが発生する．別のmodelを使うときは`--model qwen3-coder-30b-a3b`へ替えるか，Pi内の`/model`で切り替える．Piが見せるIDとrouterの`/models`のIDが一致することを確認する．[Piのmodel選択](https://pi.dev/docs/latest/models)．

ephy-workerから比較評価する場合は，共通Pi設定のままrouter profileを指定する．成果物はGit checkout外へ置く．任意のcoding Jobなら`coding run ... --dry-run`で設定を確認できる．

```bash
uv run python -m ephy_worker eval run \
  --suite smoke \
  --model qwen3-8b-q6-k-router \
  --profiles configs/coding-models.example.yaml \
  --output-dir /private/tmp/ephy-coding-eval
```

Qwen3-Coderを試すには`--model qwen3-coder-30b-a3b-router`へ替える．既存8083の単一モデル経路は`qwen3-coder-30b-a3b`のままで利用できる．`coding run ... --dry-run --profiles ...`ではrepository，base revision，Pi invocationをmodel起動前に検査できる．

model ranking，自動best-model選択，実model benchmark，SWE-bench，fine tuning，Runtime連携は別Goalです．
