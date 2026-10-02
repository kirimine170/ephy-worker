# Managed Piの正式proposal runner

今回の基盤実装は，ユーザーが明示したCodexによる修正・PR・merge依頼として作成する．gpt-oss／Qwenで実装した正式Jobではなく，過去コードをQwenの成果へ認定し直さない．CIとPRレビューを通すbootstrapの基盤修正と，このrunnerで新規実行するモデルのproposalを区別する．

## 入口と実装範囲

既存labの`run-background-job.ps1`と対応するsmoke testを`tools/pi-local/windows/`へversion管理用に取り込んだ．既存v1は挙動を保持し，`schemaVersion: 2`だけを`ephy_worker.formal_runtime`へdispatchする．v1の`audit_pending`を正式合格へ昇格させない．managed Piの`background_job_submit`は，controllerが事前固定したspecの実在pathとSHA-256を受け取り，既存のUI確認後にv2を提出できる．対話用`tools/pi-local/run.ps1`はこの入口ではない．

正式v2はclean detached worktree，固定file／semantic scope，外部checkerとcontrol，5種類のcheck，runtimeとmodelのhash，時間・output token・request・RAM・空き容量・log上限，最大2回のrepairを起動前に検証する．gpt-oss planner，Qwen implementer，runner独立検証，freeze，fresh gpt-oss auditorを別process／sessionで順に実行する．実装者にはshellを与えず，file toolのwriteを正規化した許可pathへ限定し，範囲外path・hard link・role違反・wrong model・予算超過を遮断してsessionを停止する．既存governance gateは最後に読み込み，system配送とexactなack前の依頼隔離を保持する．必須runtimeの実在pathとhashも配送する．

最初の対応profileは`docs/`または`.agents/skills/`内のMarkdown proposalだけである．検証時に変更プログラムを実行するsandboxは未接続であり，Python・tool・MCPの実行コード変更を許可すると，checker経由でfile toolの境界を超えられるため，このprofileでは拒否する．OSの権限・security設定を変更して制限を外さない．文書の合格はskillの別課題への実効性を証明せず，転用測定は別に固定した課題と条件が必要である．

canonical auditの22 artifact，3 schema，完全性markerを再利用する．runnerがbundle path・実bytes・SHA-256を検証し，監査前のintegrity attestationを固定する．schema-validなaudit inputとsystem envelopeを配送し，auditorは凍結bundle以外を読めない．監査出力のschema，最終patch binding，引用artifactのidentity，candidate／bundle無変更と実model／tool traceを外側で検証する．invalid JSONは同じ監査runで修正させない．すべて合格した`ACCEPT_PROPOSAL`だけが未適用`review_ready`になる．

audit contractのA／B／C各必須項目へ対応する25個の固定check IDをsystem envelopeへ配送し，省略・重複・架空IDを拒否する．governance gateの隣接runtimeから配送される監査契約とschemaも正本と同じhashへ固定する．モデルmanifestはrouter presetの実pathと全shardに結び付け，各stage前後に実bytesを再検証する．確認画面はagentの要約ではなく固定specの課題・scope・検査・モデル・上限を表示する．別途承認されたIntegration roleの手動applyでは`candidate.patch`を選び，監査binding，実session／trace，現在candidateをread-onlyで再検証し，確認後にも再確認する．

routerの起動・unload・RAM解放待ちの後，load要求直前にモデルを再hashする．independent verifierのidentityは申告値からコピーせず，実行中Python，controller source，解決されたcommand executable，固定commandと実環境から導出し，期待値と照合する．commandの実path・hash・PID・argv・環境hashを外側で保存し，freezeはその観測済みidentityを必須とする．Integration時は監査結果JSONをraw出力，session最後のassistant message，stdout最後のmessage_endへ照合する．attestationだけのhash更新で判定を置換できるとは扱わない．

Windows回帰fixtureはPythonで実際のRPC subprocessを起動し，POSIX shebangの実行可能性に依存しない．source snapshotのcleanupは割り当てた一時領域だけを対象にし，WindowsでGitが生成した独立ファイルのread-only属性のみを解除する．ACL拒否，hard link，領域外targetは再試行で迂回せず失敗を保存する．OSのACLやsecurity設定は変更しない．CIはLinuxとWindowsの両方で既存検査を実行する．

## 有限の再帰実行

`python -m ephy_worker.formal_campaign --plan <controller-owned-plan.json> --state <new-state-directory> --execute-authorized`は，ユーザーの明示的な実行依頼を受けたtrusted controller用入口である．agentには公開しない．spec配列，最大連続失敗，campaign全体の期限を固定する．上限は20件／24時間で，controllerのkernel-held lockで重複運転を拒否する．各Jobは新しいIDとclean worktreeを持つ．model serverも所有processだけを起動・停止し，他のPi／llama／Strata processまたは占有portがあれば停止する．

`--resume`は同じplan identityの記録だけを再開する．中断したJobは証拠を残して失敗と記録し，同じcandidateの実装・検証・監査を再実行しない．完了済みcampaignは追加実行しない．`STOP`またはJobの`cancel.request`で停止できる．結果を自動apply／commit／push／mergeせず，成功proposal，失敗，仮説なし，未実装を別の`outcome`へ記録する．

## 検証

`tests/test_formal_runtime.py`は実schemaと不正controlを使って，path escape，hard link，bundle改変，stale audit，架空引用，偽PASS，不完全契約，untracked差分，重複process lock，ack前read，wrong model，中断Jobの再実行を検証する．`tests/formal-stage-guard-smoke.mjs`はtool hookを実行してrole・path・token・requestの制限を検証する．既存回帰・governance smokeは保持する．synthetic test合格と実LLM実行の結果を混同しない．
