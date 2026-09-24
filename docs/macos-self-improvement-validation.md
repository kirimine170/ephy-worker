# macOS自己改善の初期検証

2026-09-23に，ephy-workerのcoding Jobをephy-worker自身へ向けた．元checkoutにはcoding機能の未コミット変更があるため，`source_state: working-tree`を使用した．tracked fileの現在内容と明示したuntracked fileを一時Git repositoryへcopyし，候補patchを元checkoutへ自動適用しない構成である．

## llama.cpp接続

Ephy Runtimeのcode profileと同じ`127.0.0.1:8083/v1`をPiの`llama_cpp` providerへ設定した．Qwen3-Coder-30B-A3BのGGUFを`qwen3-coder-30b-a3b` aliasで起動し，`/v1/models`とPi短文probeの双方で接続を確認した．このhostでは`labo/llama.cpp`の実行可能buildを使用した．Runtime checkout内の`llama-server` binaryは旧pathを`LC_RPATH`に保持しており，単体実行はdylib解決で失敗した．そのbinaryもbuild/binを`DYLD_LIBRARY_PATH`へ指定すると`--version`は成功したが，Runtime repository自体には変更を加えていない．

既定のcoding profileはllama.cppへ切り替えた．旧backendの測定値は比較履歴として別fileに保持した．その後，旧backendの設定・Pi providerを廃止し，llama.cpp routerを共有Pi設定へ追加した．

## 自己改善Job

macOSでPi親processの終了後に子tool processが残る問題を対象にした．working-tree snapshot，Pi RPC，candidate patch，独立pytest，typed result，source manifestを一通り実行した．

| 試行 | 接続 | 独立validation | 判断 |
|---|---|---|---|
| 初回 | Ollama GPT-OSS | agent 300秒timeout，patchなし | 棄却 |
| 2回目 | llama.cpp Qwen3-Coder | 子PIDを検証しない不完全なテストが失敗 | 棄却 |
| feedback後 | llama.cpp Qwen3-Coder | 子を別sessionへ移す誤ったテストが失敗 | 棄却 |

生成したpatchとlogは`/private/tmp/ephy-worker-self-improvement`に保存した．このdirectoryは一時領域である．modelの完了自己申告は成功判定に使っていない．

2件のcandidateが不合格だったため，process-group回収は人手で修正した．親processが既に終了していても同じgroupへTERMを送り，残存groupには猶予後KILLを送る．回帰テストは親が先に終了する場合とPiの`agent_settled`を受け取る場合の両方で，実際の子PID消滅を確認する．

候補採用前のread-only確認として`coding verify-source`を追加した．source manifestのHEADとfile hashを現在のcheckoutへ照合し，変更や欠落，新規fileとの衝突があればexit 1を返す．最終確認では全287テスト，変更対象のRuff，`git diff --check`，repository validatorが成功した．

## 残る境界

初回試験時はmacOSの強制filesystem sandboxを接続していなかった．Piへ渡す「worktree内だけを編集」の指示と，temporary worktreeだけでは，Pi processのcheckout外への書込みをOSレベルで禁止できなかった．その後，coding Jobに任意指定の`sandbox-exec`書込みguardを追加した．元のrepositoryと指定した追加rootを保護し，Piとvalidationの双方に適用する．これは書込み範囲だけの初期防御層であり，無人自己更新やpatch自動適用は依然として有効化していない．

このhostには`sandbox-exec`とDocker CLIがあるが，Docker daemonは停止中である．現行Codex実行環境内での`sandbox-exec`試験は`sandbox_apply: Operation not permitted`になった．macOS側の権限で再実行するとprofile適用に成功し，一時directoryへの書込みを実際に拒否した．coding Jobのguardも同じ制約を受けるため，Codex内部sandboxでは失敗するが，適切なmacOS host processから起動すれば利用できる．

追加した統合テストは，fake Piと独立validationを実際に`sandbox-exec`下で起動し，元repositoryと追加保護directoryへの書込みが拒否されることを確認した．実際のllama.cpp接続Piによるsandbox付き自己改善Jobは未実行である．
