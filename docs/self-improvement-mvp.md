# ephy-worker自己改善MVP

## 実装済みの範囲

このrepositoryには，ephy-worker自体の改善依頼に限定して発火するrepo-local skill，評価契約，開発ガバナンス，独立監査契約，監査prompt，入出力schemaを追加する．これらは変更を自動採用するsystemではなく，proposal-only loopの指示と機械可読な境界を定義する．

現行のWindows runnerが接続済みなのは，gpt-ossによる調整，Qwen実装，既存の独立check，証拠保存，`audit_pending`停止までである．agent起動前に全契約をfreezeして環境不備を分離するformal preflightと，freshかつread-onlyのgpt-oss auditor起動，audit bundle検証，result schema検証，監査後execution attestation，`review_ready`判定は未接続である．したがって，次は実装済みの実行結果ではなく，接続後にrunnerが保証する完成形の順序である．

また，現行Qwen processの`cwd`は隔離worktreeへ固定されるが，汎用`bash`／`powershell`／`edit`／`write`はOS-levelのpath jailではない．絶対pathや`..`によるworktree外アクセスを機械的に遮断できるworkspace-root限定toolまたはsandboxが接続されるまでは，scope制約を完全に強制済みとは扱わず，正式な自己改善経路の完成を宣言しない．現在のgovernance gateが保証するのは，policy全文とidentityのsystem配送，ack前の元依頼隔離，exactなack，lead／planner用必須文書bytesの再hash・一括配送・最終tool result照合，違反sessionの恒久停止，role別tool ceilingである．配送内容に従った計画の正しさ，workspace-root confinement，formal preflight，formal audit接続は未保証である．

1. gpt-oss leadが改善仮説を1件だけ定義し，対象fileと受入条件を固定する．
2. 変更前のbaselineを，固定したcommandとfixtureで記録する．
3. Qwen workerが隔離worktreeで，委譲された仮説だけを実装する．
4. 同じcommandとfixtureでcandidateを測定する．
5. candidate由来のhard gateに失敗した場合，Windows runnerは同じ仮説，対象範囲，fixture，検証commandを保持したまま最大2回のQwen修正と再検証を要求できる．各試行のpatch fingerprintと検証logを残し，同じ状態への逆戻りや検証command自体の不備を検出したら停止する．
6. 最終attemptの独立検証後，freshかつread-onlyのgpt-oss auditorが，freeze済みの最終diffと検証証拠を監査する．
7. 最終的に不合格の候補は採用せず，patchとlogを監査用に残す．合格した候補も自動適用せず，人間またはCodexが適用を判断する．

skillは役割分担，単一仮説，同条件比較，停止条件，監査証跡を定義する．background jobの起動，隔離worktree，patch生成などの実行機構はWindows lab環境が提供し，このrepositoryのskill自体はそれらを再実装しない．

## 役割分担

- gpt-oss planner：要件整理，仮説と評価条件の確定，baseline記録．candidateは編集しない．
- Qwen worker：限定された候補の実装と，scope内のtest修正．
- independent verifier：Qwen終了後に固定checkを実行し，candidateを変更しない．
- fresh gpt-oss auditor：最終patchと証拠をread-onlyで監査し，構造化判定を返す．契約とpromptは定義済みだが，現行runnerへの起動接続は未実装．
- 人間またはCodex reviewer：review済みpatchを採用するか判断する．
- Integration／release operator：formal proposal workflow外の別roleとして，明示的なユーザー承認後だけapply，commit，push，PR，merge等の指定操作を行う．leadやauditorとは兼任しない．

## 評価軸

初期MVPでは，次の各軸を個別に記録し，複合scoreを作らない．

- correctness：指定testまたはfixtureの終了code．
- regression gate：repository validation，lint，`git diff --check`．
- scope：依頼外変更の有無．
- latency：job開始から`review_ready`までのwall-clock．
- intervention：job提出後に必要となった人間の追加指示回数．

## 停止条件と監査

同条件比較ができない，必要なhard gateが失敗する，scope外変更がある，または必要toolchainがなく検証を実行できない場合は，候補を採用せず停止する．未実行の検証を合格扱いしない．

`scripts/validate_self_improvement.py`はbaselineとcandidateの異なるGit作業ツリーを受け取り，同一のcommandと同一byteのfixtureを測る独立したhard gateである．JSON証跡に両測定値，対象差分，correctness，repository validation，Ruff，`git diff --check`，scopeの結果を分けて出力する．baseline失敗は改善の根拠になり得るが，candidateのhard gate失敗は合格にならない．これは修正の実行機構ではなく，判定を誤認させないための検証機構である．
Python taskでは選択interpreter，worktreeごとの`src` import，必要module／file，offline条件，固定HEADを測定前に確認できる．baselineに期待する失敗を明示すれば，別原因の失敗でcandidateの改善を判定しない．課題固有の固定挙動確認は両worktreeの外に置く．Windows encoding課題での条件・結果は`docs/self-improvement-encoding-experiment.md`に記録した．ただしPi background runnerはこのrepositoryの外にあり，agent起動前の自動preflight統合は未実装である．
Ruffは宣言した対象範囲のPython fileを検査し，対象fileを証跡に残す．対象のPython fileがない場合はRuffの利用可否のみ確認し，lintを「非該当」と記録する．既存の範囲外lint負債を候補の新規regressionと混同しないためである．

監査接続後は，runnerがaudit bundleのpath，size，SHA-256，schema適合を検証し，freshなgpt-oss auditorが最終candidateの内容，workflow証拠，runner attestationをread-onlyで評価する．監査結果だけでなく，実model，tool制限，candidate無変更，result schema適合をrunnerが監査後にattestして初めて`review_ready`を判定する．

## Codex PR review bridge

formal auditが未接続のbootstrap期間には，proposal Jobを終了した後，明示的に承認されたIntegration／release operatorがfreeze済みcandidateを専用PRへ運び，Codex Reviewを独立reviewerとして使える．この経路は，`AGENTS.md`の`Code Review Rules`と[自己改善PRレビューprompt](../.pi/prompts/review-self-improvement-pr.md)を使用し，元patch SHA-256，PR head commit，scope，検証結果を結び付ける．現在のPR headに対する必須CIとCodex Reviewが完了し，未解決のblocking findingがないことを確認する．新しいpush後はCIとreviewをやり直す．

これはreviewを得るためのbridgeであり，未実装のformal gpt-oss audit，execution attestation，runnerによる`review_ready`を実行済みとは扱わない．Codex Reviewは自動修正やmergeを行わず，最終的なmergeには対象を指定した明示的な人間の承認を必要とする．

## 将来構想

最優先の未実装項目は，agent起動前のformal preflightと，`audit_pending`からformal audit，execution attestation，`review_ready`までをrunnerへ接続することである．security評価の拡張とcost最適化は後続課題であり，このMVPの評価軸や自動化範囲へ含めない．自動採用，自動再起動，provider追加，Python機能変更，無限の自己書換えloopも実装済みではない．今後それらを検討する場合も，別の明示的な仮説とauthorizationを必要とする．
