#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PI_EXECUTABLE=${EPHY_PI_EXECUTABLE:-pi}
PROVIDER=${EPHY_PI_MODEL_PROVIDER:-llama-local}
REASONING_MODEL=${EPHY_PI_REASONING_MODEL:-Qwen3.8-27B-UD-Q4_K_M}
CODER_MODEL=${EPHY_PI_CODER_MODEL:-Qwen3-Coder-30B-A3B-Instruct-Q4_K_M}

export EPHY_PI_MODEL_ROUTER=1

exec "$PI_EXECUTABLE" \
  --no-extensions \
  --extension "$ROOT/tools/pi-local/ephy-compaction-recovery.ts" \
  --extension "$ROOT/tools/pi-local/ephy-model-router.ts" \
  --provider "$PROVIDER" \
  --model "$REASONING_MODEL" \
  --models "$PROVIDER/$REASONING_MODEL,$PROVIDER/$CODER_MODEL" \
  "$@"
