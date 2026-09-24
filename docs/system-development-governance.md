# ephy-worker システム開発ガバナンス

- Policy ID：`ephy.system-development-governance.v1`
- Version：`1.2.0`
- Status：active

この文書は，ephy-worker自身の変更，評価基盤，実行runner，prompt，skill，checkerを含むシステム開発の正本です．個別の作業指示より優先し，上位のsystem／developer指示とユーザーが明示した権限境界には従います．

## 正しさの定義

正しさとは，コードが動くことやテストが通ることだけではありません．次の積集合が成立した状態です．

```text
固定された要求
AND 限定された差分
AND 再現可能な検証証拠
AND 指定どおりの実行経路
AND 最終candidateに対する独立監査
```

agentの「完了した」「問題ない」「tests passed」という説明は承認ではありません．runnerが全gateを確認した状態だけを完了とします．

## システム開発の基本原則

- 要求，利用者，信頼境界，失敗時の影響を，実装より先に明らかにします．曖昧さを偶然の仕様にしません．
- 症状を隠す変更ではなく，観測された根本原因に対する最小で一貫した変更を選びます．既存の安定した仕組みを再利用または設定して解ける場合，新しい機構を増やしません．
- 最小権限，secure-by-default，fail-closedを原則とします．silent fallback，例外の握り潰し，データ欠損・置換による見かけ上の成功を許しません．
- interfaceと既存利用者を守ります．互換性を壊す必要がある場合は，影響，移行経路，rollback条件を明示します．
- 重要な挙動をtestと独立した観測証拠で確認します．test自体やcheckerも誤る前提で，既知の不正candidateを拒否できるcontrolを持ちます．
- 運用可能性を設計に含めます．状態，入力，出力，失敗理由，version，変更主体を追跡でき，停止・再試行・rollbackが可能でなければなりません．
- 現在実装されている保証と，将来の提案を明確に分けます．未実装のgateを，文書だけで成立したものとして報告しません．

## 規範語

- MUST／必須：満たせない場合は，そのJobを続行してはなりません．
- MUST NOT／禁止：実行した場合は，そのcandidateを不合格にします．
- SHOULD／原則：逸脱には明示的な理由と証拠が必要です．
- MAY／任意：他の必須条件と権限境界を損なわない範囲で許可します．

## 1．契約を実装より先に固定する

実装前に，次を機械可読なJob契約として固定し，hashを記録しなければなりません．

- Job ID，base commit，cleanなcandidate worktree．
- 変更可能なfile scopeと，許可されたsemantic scope．
- 要求，禁止事項，受入条件，固定checker，evaluation contract．
- Python，Ruff，依存関係，offline条件，cwd，`PYTHONPATH`，locale，UTF-8設定，一時領域を含むenvironment contract．
- planner，implementer，verifier，auditorの期待modelとrole．
- timeout，repair上限，停止条件，proposal-onlyか否か．
- 必須policy，skill，prompt，schemaのversionとSHA-256．

一つでも未確定または矛盾している場合は，`contract_incomplete`として停止します．実行中に契約を変更する必要が生じた場合，同じJobを継続せず，新しいJobとして開始します．

## 2．役割と権限を分離する

### Controller／runner

- stage順序，権限，環境，hash，状態遷移を機械的に強制します．
- 実model，process，session，tool traceを外側から記録します．
- LLMの自己申告を実行証拠として代用しません．

### Planner／manager

- 要求，scope，リスク，実装方針，検証方法を整理します．
- candidateを変更してはなりません．

### Implementer

- 指定されたcleanな隔離worktreeだけを変更します．
- 自己checkはできますが，独立検証や自分の成果の承認を代行しません．

### Independent verifier

- 実装process終了後，固定済みcommandとcheckerを同一のenvironment contractで実行します．
- candidateをrepairしません．

### Auditor

- 独立検証後の最終diffと証拠だけを監査します．
- candidateへの書込み権限，shell，network，subagentを持ちません．
- 実装sessionと同じsessionを再利用しません．

### Integration／release operator

- formal proposal workflowと監査対象Jobの外側にある別roleです．
- 原則として`review_ready` artifactを入力とし，ユーザーが対象と操作を明示的に承認した後だけ，apply，commit，tag，push，PR作成・更新，merge，release，deploy，外部通知を扱えます．
- formal auditが未接続のbootstrap期間に限り，ユーザーが外部PRレビュー経路を明示的に選んだ場合，machine gateと監査前workflow gateを通過してfreezeされたproposalを，専用branchへのapply，commit，push，PR作成・更新まで運べます．これは`review transport`であり，formal audit完了，`review_ready`，採用，mergeのいずれも意味しません．
- `review transport`では，元candidateのpatch SHA-256，PR head commit，対象scope，検証結果をPRへ結び付けます．現在のPR headに対する必須CIとCodex Reviewが完了し，未解決のblocking findingがない場合だけ`external_pr_review_ready`候補にできます．新しいpushは以前のCIとreviewを無効にします．
- 承認された操作，対象repository／branch／remote，実行結果を記録し，監査済みpatchから差分が生じた場合は，その監査を採用根拠として流用しません．

正式な自己改善Jobでは，plannerとauditorをgpt-oss，implementerをQwenに固定します．leadが小さな修正を直接編集して代替してはなりません．Integration／release operatorはこのJobのstageや兼任roleではなく，proposal stop後に別の明示的な承認がある場合だけ開始できます．formal lead，implementer，verifier，auditorへ承認後操作の権限を追加して代用してはなりません．

## 3．実行順序をrunnerが保証する

正式な順序は次です．`attempt`は初回実装を1回目とし，candidate由来の失敗だけを固定上限までrepairできます．

```text
preflight
→ gpt-oss lead plan
→ attempt 1..N {
    Qwen implementation or repair
    → independent verification
    → candidate-origin failureかつ残回数ありなら次のattempt
  }
→ freeze final candidate and audit bundle
→ fresh gpt-oss audit
→ proposal stop
```

各attemptの中でQwenより前に独立検証を実行したり，独立検証後にQwen以外がrepairしたり，最終attempt後に検証せず監査へ進んだりすることは`workflow_failed`です．基盤不具合，環境不一致，wrong model，invalid command，証拠欠落はattemptを増やさず停止します．auditorが見るpatch SHA-256と，最終独立検証が測定したpatch SHA-256は一致しなければなりません．監査後にcandidateが1 byteでも変われば，検証と監査は無効です．

## 4．隔離とscopeを守る

- 実装は，指定base commitから作成したcleanなdetached worktreeでのみ行います．
- main checkout，既存candidate，旧Jobを直接変更または再利用しません．
- 開始時にtrackedまたはuntrackedの差分があれば，`candidate_not_clean`として停止します．
- file scopeとsemantic scopeの両方を満たす必要があります．
- 許可ファイル内でも，課題と無関係なcleanup，format，命名変更，コメント変更，fixture変更，assertion緩和は禁止します．
- formatterやgeneratorが広範囲を変更した場合も，自動的には許容しません．
- untracked filesを含む完全な変更一覧を検査します．

## 5．環境と検証を固定する

- baseline，worker self-check，runner check，独立再検証は，同じenvironment contractを使用します．
- `cwd`と`PYTHONPATH`だけを，測定対象のworktreeへ対応させます．
- baseline failure，candidate failure，既知の非対象failure，environment failureを分けて記録します．
- Python，Ruff，依存関係，locale，bootstrap，一時領域などの不備を，candidateのrepairへ渡してはなりません．
- 実際に成立していないcommand，unavailableなtool，未実行のcheckをPASSとして扱いません．
- test合格だけを要求適合の根拠にしません．scope，意味保持，workflow，auditも独立した必須gateです．
- skip／xfail追加，assertion削除，例外握り潰し，入力の欠損・置換，checker回避は禁止します．
- checkerと評価定義は実モデル実行前にcontrol candidateで検証し，実験中に上書きしません．

candidate由来と機械的に判定できる失敗だけを，固定回数のrepair対象にできます．基盤不具合，環境不一致，wrong model，invalid command，証拠欠落はrepairせず停止します．

## 6．必要な証拠を保存する

最低限，次をimmutable artifactとして保存します．

- base commit，開始時Git状態，final diff，changed-files manifest．
- `patch_sha256`とcandidate snapshot manifest SHA-256．
- governance，skill，task，evaluation，checker，environment，prompt，schemaの各SHA-256．
- 各stageの期待modelと，runnerが観測したmodel，artifact manifest，runtime，invocation config．
- stageの開始・終了時刻，順序，process／session ID，tool trace．
- command，cwd，環境契約，exit code，stdout，stderr．
- baseline，target tests，full suite，Ruff，repository validation，scope，fixed checkerの結果．
- audit input／evidence manifest／bundle integrity attestation／audit result／execution attestationの各SHA-256．
- candidateの監査前後のsnapshot SHA-256．

証拠は最終patchと結び付けます．古いcandidate，別環境，手動実行，hashのない出力，agentの説明だけによる結果は流用しません．

## 7．policyを必ず配送し，受領前の変更を禁止する

「必ず読ませる」は，モデルの自発的なfile readや「読みました」という文章を待つことではありません．runnerは次を実施します．

1. candidate外の正本snapshotを読み，raw bytesのSHA-256を計算します．
2. policy本文，Policy ID，SHA-256，末尾marker，Jobごとのnonceを各stageのsystem payloadへ強制注入します．
3. 初期toolを`governance_ack`だけに制限し，ack前にはfile readを含む他のtoolを公開しません．policy本文はtoolで取得させるのではなく，system payloadへ完全な形で注入します．初回provider callでは元のユーザー依頼と過去会話を非破壊的に退避し，system messageとack専用の最小messageだけを渡します．元の依頼はack結果の検証後に復元します．
4. agentがPolicy ID，SHA-256，末尾marker，nonce，roleを構造化して`governance_ack`へ返した場合だけ受領候補とします．ack messageに含まれた文章，thinking，兄弟tool callは保存前に除去し，exactなack tool call一つだけを実行します．文章によるack，値違い，追加field，ackなしの回答は，安定したfailure markerへ置換し，そのsessionを恒久停止します．
5. lead／plannerでは，ack実行時にrunner指定のskill，evaluation contract，audit contract，audit prompt，実装境界文書をraw bytesから再読込し，開始時のbyte数とSHA-256に一致することを確認します．これらは切り詰めず，一つのmodel-visible tool resultとして配送します．50 KiBまたは2000行を超える場合，truncationせず停止します．最終`tool_result` hookがそのcontentの完全一致を確認するまで，他のtoolを公開しません．
6. ackと必須contextの検証後だけ，そのroleに許可されたtoolを公開し，`governance_ack`自体は非公開に戻します．auditor roleではack後に`read`，`grep`，`find`，`ls`だけを公開します．managed runtimeがrepository外にある場合は，runnerとsmoke testの実在path，byte数，SHA-256をsystem-controlled metadataとして渡し，架空のrepository内pathで代替させません．
7. ack前，必須context検証中，role ceiling外のtool callを`tool_call` gateでも遮断します．gateは他のfeature extensionより後に読み込み，最終provider payloadと最終tool resultを検査します．
8. system delivery，ack，必須context配送，解禁tool，最初の変更可能toolの順序をrunnerの証跡へ残します．

policy，prompt，skill，contractの欠落，hash不一致，policy／audit contract／audit promptに定義された末尾markerの欠落，truncation疑い，ack不成立があれば，推測して続けません．

この仕組みが保証するのは，policyがsystem payloadへ配送され，そのidentityを構造化して受領し，必須文書の検証済みbytesがprovider contextへ渡され，それ以前には他のtoolを実行できず，未受領の最終assistant messageがfailureへ置換されたことです．raw streaming deltaの瞬間的な非表示や，モデル内部の主観的な「理解」，配送内容に従った計画の正しさは保証しません．計画，candidate，workflowは別の固定checkerと独立監査で判定します．

## 8．監査を独立したread-only判定にする

監査は，freshなgpt-oss processで，freeze済みaudit bundleだけを対象にします．監査promptは[独立監査契約](../.agents/skills/ephy-worker-self-improvement/references/audit-contract.md)に従い，[audit input schema](../.agents/skills/ephy-worker-self-improvement/references/audit-input.schema.json)と[evidence manifest schema](../.agents/skills/ephy-worker-self-improvement/references/evidence-manifest.schema.json)に適合したbundleを受け取り，結果を[audit result schema](../.agents/skills/ephy-worker-self-improvement/references/audit-result.schema.json)へ適合させます．

runnerは監査processのack前には`governance_ack`だけを与え，ack後には`read`，`grep`，`find`，`ls`だけを与えます．特に`bash`／`powershell`，`edit`，`write`，network，subagentを与えません．

artifactの実byte数，SHA-256，bundle-relative pathの正規化，path escape，symlink escape，manifest schema適合は，auditorではなくrunnerがfreeze後に再計算・検査し，bundle integrity attestationとして固定します．runnerはmanifestとattestationをbindするaudit inputをその後に作成し，audit input schema適合を検証してから，audit input，evidence manifest，attestation，audit prompt，result schemaのSHA-256とaudit input検証結果をsystem-controlled envelopeへ注入します．auditorはSHA-256を自力で計算したと申告せず，envelope，attestation，manifestの値が一致することと，参照した内容の意味を監査します．

監査は次を別々に判定します．

- evidence integrity：artifact，hash，binding，完全性．
- candidate correctness：要求，scope，意味保持，固定gate．
- workflow validity：role，model，stage順序，独立性，proposal stop．

必須証拠が読めない場合は`INCONCLUSIVE`です．確認済みの違反があれば`REJECT_PROPOSAL`です．すべての必須checkがPASSの場合だけ`ACCEPT_PROPOSAL`を返せます．`ACCEPT_PROPOSAL`は未適用proposalとしてレビュー可能という意味であり，commit，push，PR，mergeの許可ではありません．

gpt-oss自身のmodel identity，tool制限，無変更性，JSON Schema適合は，監査後にrunnerが外側からexecution attestationとして固定します．bundle integrity attestationは監査前，execution attestationは監査後に作成し，両者を代用しません．invalid JSONを同じ監査runで修正させません．

## 9．proposal-onlyの境界

proposal-onlyで許される変更は，隔離candidate内の未適用差分と，runner管理下の証拠生成だけです．次は禁止します．

- commit，tag，push，PR作成・更新，merge．
- main checkoutへのapply，release，deploy．
- 外部サービスへの書込み・通知．
- 有料API，追加download，保存済み評価定義や旧証跡の上書き．

これらは，proposal stop後の別タスクとして，ユーザーが対象と操作を明示的に許可した場合だけ，Integration／release operatorが実行できます．formal auditが未接続のbootstrap期間には，前節の`review transport`だけをformal audit前に実行できますが，proposal Job自身の禁止事項は変わりません．formal lead，implementer，verifier，auditorは，その承認を自分のrole権限の拡張として扱いません．

## 10．fail-closed条件

次はrepairせず停止します．

- governance，skill，task，contract，checkerの未配布，identity未受領，hash不一致．
- policy／audit contract／audit promptの末尾marker欠落，truncation疑い，governance ack不成立．
- dirtyまたはwrong-base candidate，main checkoutへの直接編集．
- environment／preflight不合格．
- model・role不一致，stage順序違反，lead直接編集．
- checker／evaluation definitionの欠落・変更．
- file scopeまたはsemantic scope違反．
- invalid verification command，必須証拠欠落，検証不能．
- stale verification，古いpatchへのaudit，監査後のcandidate変更．
- auditorによる書込み，禁止tool利用，schema不適合．
- 指示間の重大な矛盾．

停止した試行は失敗例として保持し，条件をその場で変更して成功扱いにしません．

## 完了式

ここで`pre_audit_workflow_gate`は，固定済み契約に従ってpreflight，gpt-oss plan，Qwen実装，独立検証，final candidate freezeまでのrole，model，順序，hash bindingが成立した状態を表す．formal audit，audit execution attestation，`review_ready`は含まない．

```text
review_ready =
  machine_gate
  AND workflow_gate
  AND audit_decision == ACCEPT_PROPOSAL
  AND audit_execution_attestation
  AND all_bound_hashes_match
  AND candidate_unchanged_after_audit

external_pr_review_ready =
  machine_gate
  AND pre_audit_workflow_gate
  AND frozen_candidate_bound_to_pr_head
  AND current_pr_head_ci_passed
  AND codex_review_completed_on_current_pr_head
  AND no_unresolved_blocking_findings

merge_ready =
  explicit_human_merge_approval
  AND (review_ready OR external_pr_review_ready)
```

`external_pr_review_ready`は，formal gpt-oss auditや`review_ready`を実行済みと見なす状態ではありません．Codex Reviewはtest，branch protection，required approvalを代替せず，merge権限も持ちません．いずれの経路でも自動適用は行わず，採用とmergeには対象を指定した明示的な人間の判断が必要です．

END-OF-EPHY-SYSTEM-DEVELOPMENT-GOVERNANCE-V1
