$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$PiExecutable = if ($env:EPHY_PI_EXECUTABLE) { $env:EPHY_PI_EXECUTABLE } else { "pi" }
$Provider = if ($env:EPHY_PI_MODEL_PROVIDER) { $env:EPHY_PI_MODEL_PROVIDER } else { "llama-local" }
$ReasoningModel = if ($env:EPHY_PI_REASONING_MODEL) { $env:EPHY_PI_REASONING_MODEL } else { "Qwen3.8-27B-UD-Q4_K_M" }
$CoderModel = if ($env:EPHY_PI_CODER_MODEL) { $env:EPHY_PI_CODER_MODEL } else { "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M" }
$Models = "$Provider/$ReasoningModel,$Provider/$CoderModel"

$env:EPHY_PI_MODEL_ROUTER = "1"

& $PiExecutable `
    --no-extensions `
    --extension (Join-Path $Root "tools/pi-local/ephy-compaction-recovery.ts") `
    --extension (Join-Path $Root "tools/pi-local/ephy-model-router.ts") `
    --provider $Provider `
    --model $ReasoningModel `
    --models $Models `
    @args
exit $LASTEXITCODE
