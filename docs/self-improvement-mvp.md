# ephy-worker自己改善MVP

## 実装済みの範囲

このrepositoryには，ephy-worker自体の改善依頼に限定して発火するrepo-local skillと，候補を監査する評価契約を追加する．このMVPは，変更を自動採用するsystemではなく，次のproposal-only loopを一貫して実行するための指示層である．

1. gpt-oss leadが改善仮説を1件だけ定義し，対象fileと受入条件を固定する．
2. 変更前のbaselineを，固定したcommandとfixtureで記録する．
3. Qwen workerが隔離worktreeで，委譲された仮説だけを実装する．
4. 同じcommandとfixtureでcandidateを測定する．
5. gpt-oss leadが実際のdiffと検証結果を監査する．
6. hard gateに失敗した場合，Windows runnerは同じ仮説，対象範囲，fixture，検証commandを保持したまま最大2回の修正と再検証を要求できる．各試行のpatch fingerprintと検証logを残し，同じ状態への逆戻りや検証command自体の不備を検出したら停止する．
7. 最終的に不合格の候補は採用せず，patchとlogを監査用に残す．合格した候補も自動適用せず，人間またはCodexが適用を判断する．

skillは役割分担，単一仮説，同条件比較，停止条件，監査証跡を定義する．background jobの起動，隔離worktree，patch生成などの実行機構はWindows lab環境が提供し，このrepositoryのskill自体はそれらを再実装しない．

## 役割分担

- gpt-oss lead：要件整理，仮説と評価条件の確定，baseline記録，candidate監査，最終提案．
- Qwen worker：限定された候補の実装と，scope内のtest修正．
- 人間またはCodex：review済みpatchを適用するか判断する．

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

監査では，仮説，対象scope，baseline/candidateのcommand・fixture・終了code，実行済み検証，未実行検証と理由，Git status，diff要約，各評価軸，判断，残存riskを記録する．

## 将来構想

security評価の拡張とcost最適化は後続課題であり，このMVPの評価軸や自動化範囲へ含めない．自動採用，自動再起動，provider追加，Python機能変更，無限の自己書換えloopも実装済みではない．今後それらを検討する場合も，別の明示的な仮説とauthorizationを必要とする．
