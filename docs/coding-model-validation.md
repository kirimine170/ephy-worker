# Local coding model接続検証

この文書は廃止済みbackend経由で行った初期4 model比較の履歴である．設定例と実行経路は削除済みで，再現手順としては使用しない．現在のmac向けcoding経路はllama.cppであり，この表の数値をllama.cppの結果として再利用しない．

## 検証条件

2026-09-23に，Apple M2 Max／64 GB unified memoryのhostでPi `0.85.1`，Ollama `0.34.2`を使用した．model endpointは`http://127.0.0.1:11434/v1`，Pi contextは32K，最大出力は8Kである．Pi設定は`~/.pi/agent/models.json`へ0600で配置し，API keyや外部provider credentialは追加していない．

OllamaのOpenAI互換endpointへ`reasoning_effort=medium`を送ったGPT-OSS probeはHTTP 200で応答し，reasoning fieldを返した．このため，Ollama provider全体ではreasoning effortを無効としつつ，`gpt-oss:20b`だけをmodel-level overrideで有効化した．

比較対象repositoryのsource checkoutは変更せず，taskごとにdetached temporary worktreeを作成した．agent終了後のvalidationはPiから独立したprocessで実行し，patch，sanitized agent event，validation log，typed resultを`/private/tmp`配下へ保存した．このdirectoryは再起動やcleanupで消える一時成果物である．

## Provisioning

| Profile | Ollama model | Quantization | Local size | 状態 |
|---|---|---:|---:|---|
| `qwen3-coder-30b-a3b` | `qwen3-coder:30b` | Q4_K_M | 約18 GB | 接続・評価済み |
| `gpt-oss-20b` | `gpt-oss:20b` | MXFP4 | 約13 GB | 取得・接続・評価済み |
| `devstral-small-2-24b-instruct` | `devstral-small-2:latest` | Q4_K_M | 約15 GB | 取得・接続・評価済み |
| `qwen3-coder-next` | `hf.co/unsloth/Qwen3-Coder-Next-GGUF:Q3_K_M` | Q3_K_M | 約38 GB | 取得・接続・評価済み |

Qwen3 Coder Nextの公式Ollama Q4 buildは約52 GBであり，64 GB hostではOS，KV cache，Piを含む実行余裕が小さいため，初期比較には約38 GBのQ3 buildを選択した．Ollama `0.34.2`にはHugging Face CDNへのredirectを拒否する既知不具合があり，対象model IDに限り一回の`ollama pull --insecure`で回避した．恒久的なTLS緩和は設定していない．関連するupstream記録は[ollama/ollama#18526](https://github.com/ollama/ollama/issues/18526)である．

## Smoke結果

同じ6 taskを各modelで1回実行した初期測定である．1回だけの小規模fixture結果であり，model rankingとしては扱わない．

| Profile | Validation成功 | Agent wall time合計 | Tool calls | 失敗理由 |
|---|---:|---:|---:|---|
| `qwen3-coder-30b-a3b` | 5／6 | 118.356秒 | 40 | refactor taskで3 retry後もOllama streamが`finish_reason`なしに終了 |
| `gpt-oss-20b` | 6／6 | 64.902秒 | 30 | 失敗なし |
| `devstral-small-2-24b-instruct` | 5／6 | 209.897秒 | 44 | multi-file taskで空白正規化と末尾記号を誤りvalidation失敗 |
| `qwen3-coder-next` | 6／6 | 89.671秒 | 43 | 失敗なし |

Qwenの初回実測は20秒timeoutとtool-call書式揺れにより3／6だった．これを受け，local model用task timeoutを60秒へ変更し，Qwenのtemperatureを0.2へ固定した．また，Piが一時的なstream errorから自動retryして最終成功した場合もworkerが失敗扱いしていたため，terminal `agent_end`で成否を確定するようadapterを修正し，retry回帰試験を追加した．

GPT-OSSはreasoning effort override前のrunでは5／6，`medium`を実際に送る最終条件では6／6だった．各条件1 repeatのため，差をreasoning設定の効果とは断定しない．上表はmulti-file契約とrefactor helper名を明示した最終fixtureで再測定した値である．

Qwen3 Coder Nextは79.7B parameter，262K native contextのQ3_K_M buildとしてOllamaに登録され，Piの短文probeと6 taskの両方に成功した．今回の32K context条件では6 task合計44 turn，43 tool call，0 retryだった．

## Regression

- Coding executor／evaluation testは18件成功．
- repository全体は282件成功．
- 変更対象のRuffと`git diff --check`は成功．repository全体のRuffは，今回未変更の`tests/test_init_repository.py`と`tests/test_validate_repository.py`に既存のimport-order違反が2件あるため失敗する．
- Piの通常設定から4 modelが32K context／8K max outputとして列挙されることを確認．

次の判断には各modelを最低3 repeatで実行し，task別成功率，median wall time，stream／tool-call failure率，patch品質を分離して比較する必要がある．
