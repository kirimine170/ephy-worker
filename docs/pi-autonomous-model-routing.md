# Pi自律model切替

このdirectoryは，macOSで実測したPi `0.85.1`とllama.cppによる対話型coding環境を，model weightや端末固有pathを含めずに再現するための構成である．Reasoning用Qwen3.8 27Bと実装用Qwen3 Coder 30Bを同じPi sessionへ登録し，`ephy_select_model` toolで次の推論を担当するmodelを切り替える．

この経路は対話型開発とsynthetic evaluation用である．`DUAL_GOVERNANCE_ROLE`が設定されたmanaged sessionではrouterを登録しない．`.pi/extensions/governance-gate.ts`を使う正式な自己改善，独立監査，Windows background runnerのmodel identityとrole分離は変更しない．`tools/pi-local/run.sh`と`run.ps1`はproject extensionの自動探索を無効にし，local routerとcompaction recoveryだけを明示的に読み込むため，managed Jobの起動には使用しない．

## 共有する構成

- `tools/pi-local/ephy-model-router.ts`：Reasoning／Coder切替tool，回数制限，cooldown，JSONL監査．
- `tools/pi-local/ephy-compaction-recovery.ts`：LLM summarizationがtoken上限で失敗した場合のlocal checkpointと`/recover`．
- `configs/pi-autonomous-models.example.json`：loopback llama.cpp routerと2 modelのPi catalog．
- `configs/pi-autonomous-settings.example.json`：thinking budgetとcompaction余白．
- `tools/pi-local/run.sh`／`run.ps1`：同じ引数契約の明示的起動script．

GGUF，llama.cpp binary，実行log，session，credentialはrepositoryへ含めない．例のAPI key `local`はloopback接続用の非secret placeholderである．

## llama.cpp router

2 modelを同時にmemoryへ置かず，要求時に載せ替える構成例である．`MODEL_DIRECTORY`直下へ各modelのGGUFを配置する．Qwen3.8で画像を使う場合は対応するprojectorも同じmodel directoryへ配置する．

```text
llama-server \
  --models-dir MODEL_DIRECTORY \
  --models-autoload \
  --models-max 1 \
  --jinja \
  --host 127.0.0.1 \
  --port 8080 \
  --gpu-layers all \
  --ctx-size 65536 \
  --parallel 1 \
  --flash-attn auto \
  --no-ui
```

Windowsでは同じ引数を`llama-server.exe`へ渡す．GPU backend，binary path，model pathはhostごとに設定する．`/health`が`200`を返し，`/v1/models`へ設定した2 model IDが現れることをPi起動前に確認する．

## Pi設定

既存のPi設定を上書きしないよう，専用directoryを作ってexampleをcopyする．

macOS／Linux：

```sh
export PI_CODING_AGENT_DIR="$HOME/.pi/ephy-worker-local"
mkdir -p "$PI_CODING_AGENT_DIR"
cp configs/pi-autonomous-models.example.json "$PI_CODING_AGENT_DIR/models.json"
cp configs/pi-autonomous-settings.example.json "$PI_CODING_AGENT_DIR/settings.json"
tools/pi-local/run.sh
```

Windows PowerShell：

```powershell
$env:PI_CODING_AGENT_DIR = Join-Path $HOME ".pi\ephy-worker-local"
New-Item -ItemType Directory -Force $env:PI_CODING_AGENT_DIR | Out-Null
Copy-Item configs\pi-autonomous-models.example.json (Join-Path $env:PI_CODING_AGENT_DIR "models.json")
Copy-Item configs\pi-autonomous-settings.example.json (Join-Path $env:PI_CODING_AGENT_DIR "settings.json")
.\tools\pi-local\run.ps1
```

Pi executableが`PATH`にない場合は，`EPHY_PI_EXECUTABLE`へbinary pathを設定する．provider名やmodel IDを変えた場合は，次の環境変数をPi catalogと一致させる．

```text
EPHY_PI_MODEL_PROVIDER
EPHY_PI_REASONING_MODEL
EPHY_PI_CODER_MODEL
```

## 切替contract

Piは次のsubstantial phaseに別modelが適する場合だけ`ephy_select_model`を呼ぶ．

- Reasoning：architecture，原因が曖昧な診断，security analysis，根拠を要する監査．
- Coder：まとまった実装，refactor，test-fix loop．
- 短い完了報告だけを理由に切り替えない．
- Coderのthinkingは`off`へ固定する．Reasoningは`off`／`minimal`／`low`／`medium`／`high`を選べる．
- 1 sessionの実切替は最大6回，同一phase内の即時再切替は拒否する．
- 任意model IDはtool引数に受け取らず，起動時に設定した2 modelだけを選べる．

現在の状態は`/model-route-status`で確認する．成功した切替とthinking変更は，既定ではGit管理外の`.ephy-worker/model-routing.jsonl`へ記録する．別pathを使う場合は`EPHY_PI_MODEL_ROUTER_AUDIT`を設定する．

## 実測範囲

2026-09-24にmacOS／Apple Metal／llama.cppの`--models-max 1`構成で確認した．Reasoningから開始したPiへ，未実装のPython集計関数と2件のunittestを渡した．Piは実装phaseでCoderへ切り替え，fileを編集して2件を成功させた後，最終確認でReasoningへ戻った．外部processから同じ2件を再実行して成功を確認し，JSONLには両方向の`model_switch`，model ID，thinking，理由，作業directoryが記録された．

Windows用起動scriptのargument contractとrouterのmanaged-session無効化はoffline smoke testで検査する．WindowsでのGGUF load，GPU inference，実modelによる自律切替は未検証である．model載せ替えには待ち時間が発生するため，短いphaseごとの切替には向かない．
