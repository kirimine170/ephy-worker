# 明示承認されたCodex bootstrapの限定採用経路

## Status

Proposed．この文書と関連5文書は整合案であり，activeな採用例外の成立，merge許可の追加，旧P1の解消を宣言しない．PR15／PR16のowner branchと状態，main，正式runnerは変更していない．draft PRへの公開と検証は，この案の独立レビューを得るための操作である．

## Context

ユーザーは，既存Flash NextとPiの接続，gpt-oss固定指定の見直し，必要な基盤のCodex実装，Codex ReviewでP0／P1を修正してからmergeする方針を明示している．この承認に基づく基盤開発と，runnerが指定modelへ実装させる一般の自己改善Jobを，同じ実装来歴として扱うことはできない．

実際の記録は，従来のpre-audit workflowを実施していないCodex bootstrapである．PR16の26ファイルは，25ファイルのCodex基盤変更と，実Piによる元のMarkdown guide 1ファイルからなる．後続のMarkdown候補は，基盤コードのPi実装来歴を証明しない．指定されたcloud modelはGPT6.1Solだが，実行APIがモデルIDを公開していないため，実際のcloud modelを検証済みとは記録しない．

現在のpolicy 1.3.0は，限定bootstrapをCodexが実装できることを認める一方，merge_readyをreview_readyまたはexternal_pr_review_readyに結び付けている．後者には対象patchのpre_audit_workflow_gateが含まれる．実装主体の承認と，この不足した来歴gateの成立は別の事実である．gpt-oss固定を外すことだけで，存在しない来歴を補完してはならない．

### 既存指示と評価対象の対応

| 既存指示 | この案での具体的な扱い | 追加で主張しないこと |
| --- | --- | --- |
| Flash NextとPiを接続する | 既存loopback serviceのmodel／context／PID／開始時刻／binary／設定hashを固定したexternal Strata実装を評価する | 新モデル取得，サービス再起動，設定／認証／FW変更 |
| gpt-oss固定指定を見直す | modelをJobのrole契約で固定し，既存profile既定値と今回の指定modelを区別する | model名の一致だけを独立性の証明にすること |
| 必要な基盤をCodexで実装する | 対象の基盤差分をCodex-authored bootstrapとして評価する | それをPi実装，正式監査済み，review_readyへ読み替えること |
| P0／P1修正後merge，P2以下は許容 | 最新対象headのCI／独立レビュー／blocking finding確認を採用判断へ結び付ける | standing操作承認だけで未実施gateがPASSになること |
| 一般の自己改善loopを維持する | formalとexternal proposalの既存gate，role，scope，停止条件をそのまま残す | 将来の任意candidateへの恒久的な免除 |

## Decision

提案は，今回の名前付きbootstrapだけを評価する独立した採用経路を明記し，通常の自己改善proposalの判定式を変更しないことである．既存承認を対象と操作の根拠として記録し，この案そのものを承認済み例外とは扱わない．

### 対象の固定とPR依存

| 対象 | 現在のidentity | 範囲 |
| --- | --- | --- |
| main | d1556460d6a3dcb8626fa1c5fd9ed8ed94f7c4db | 統合先 |
| PR15 | 440dc461234f5ae335a07c104644178cb430de31 | managed Pi基盤の23ファイル |
| PR16 | 875c4ba33bd186343997c946d7f9c60227fd811a | PR15に対する26ファイル |
| 両PRの最終source tree | ba24a78d777092d4f0203e16ab74c67e7335d662 | mainに対する40ファイル，39 Codex基盤＋元Pi guide 1 |

mainから40ファイルsource treeへのbinary／full-index patch SHA-256は3e5c2858ba36e402c0d78c40b86b66464e3959f50170c13211b72491c454b935である．このhashは文書整合前の基盤sourceを識別する．関連6文書を整合した新draftの完全patch／head／tree／scopeは，そのPR本文と新しいCI／レビュー入力へ別に結び付ける．古いhashやレビューを新headの合格へ流用しない．

推奨案は，mainをbaseとする別の専用branchで40ファイルをひとまとまりにレビューし，必要なpolicy整合文書だけを加えることである．PR15 owner branchへのpush／rebase，PR16の先行mergeでPR15を自動更新する操作は行わない．旧PR／P1は，新対象の独立レビューで同じ論点が解決した記録ができるまで保持する．旧PRを閉じることを，P1解消の代用にしない．

### 提案する採用条件

次のbootstrap_review_readyは未実装の提案名であり，現在のrunner stateではない．

```text
bootstrap_review_ready =
  named_bootstrap_scope_and_existing_authorization_bound
  AND codex_origin_and_missing_formal_provenance_disclosed
  AND current_full_patch_and_source_manifest_bound
  AND current_baseline_and_candidate_checks_passed
  AND current_pr_head_ci_passed
  AND independent_codex_review_on_full_current_scope
  AND no_unresolved_P0_or_P1_for_this_scope
  AND retained_live_evidence_identity_and_limits_verified
  AND general_formal_and_external_gates_unchanged
  AND historical_jobs_unchanged
```

別roleのIntegration／release operatorが，既存の対象／操作承認とこの限定採用条件を確認し，実行前のhead／base／patch hash，評価結果，実施操作を記録する．本人の実装完了宣言だけで採用しない．全範囲のP0／P1が解消しない場合はmergeを保留し，独立して実行可能な修正／検証を続ける．P2以下の許容はユーザー方針の範囲で記録する．branch protectionとrequired approvalは引き続き守る．

この条件を通常のformal proposalに追加しない．formalのreview_readyと，指定Pi実装までの来歴を要求するexternal_pr_review_readyは現行定義を維持する．formal_runtimeのintegration拒否，外部profileのexternal_review_pending終端，Markdown限定，OS verifier隔離前のcode／tool／MCP拒否を維持する．自動採用や任意repoへの転用を許可しない．

### 必要な整合変更

| 文書 | 提案する最小変更 |
| --- | --- |
| AGENTS.md | 今回の名前付きbootstrap評価経路とformal Jobを区別する参照を追加し，Code Review Rulesを維持する |
| docs/system-development-governance.md | この限定経路の対象／来歴の開示／評価条件を明記し，従来2経路の定義を変えずに採用判断との関係を説明する |
| self-improvement SKILL.md | Codex bootstrapをformal Pi stageの代替と扱わないことと，今回の別評価経路への参照を明記する |
| docs/formal-pi-runtime.md | PR15のCodex来歴と未実施formal gateを保持し，別経路による基盤採用と正式Job実行を区別する |
| docs/strata-proposal-runtime.md | 基盤採用と未適用Markdown候補の採用を区別し，既存runnerの拒否／制限を維持する |
| このADR | Proposedから先へ進むときに，既存承認との対応と独立レビュー結果を記録する |

この6文書の整合では，40ファイルの基盤差分にAGENTS.mdと新ADRが加わるため，main向け全範囲は42ファイルになる．残る4文書は既に40ファイルの範囲内である．実装前にこの正確な範囲を固定し，文書整合前sourceに対する変更をこの6文書だけへ限定する．一般のgateを実装するcode，tests，checker，schemas，依存関係，CIは整合前sourceとbyte単位で同じものを保持する．

### 既存証拠と新しい検証

公開875c4baのCI run 37131872421はWindows587／Linux590件を通過し，独立Codex Review 5970461894は重大な問題なしだった．ただし，PR16のレビュー対象diffはPR15をbaseとする26ファイルである．main向け40ファイル全体の新しいレビューとして流用しない．PR15にはpre-audit来歴について未解決P1が2件ある．

キャンセル修正には元のテスト条件を保持した400回の既定clock反復と，全体587件通過の証拠がある．修正済みsourceで3／3のMarkdown試行が5つのcheckを通過し，19分以内，18 requests／3321 output tokens，repair 0，候補未適用で終了した．前の15試行の1035保持ファイルは一致し，合計18／20で停止した．これを基盤コードのPi実装来歴や長期信頼性とは記録しない．

新しいheadには，main向け完全差分，dependency／lockの一致，固定したbaseline／candidate検証，両OSの全CI，main向け全範囲の独立Codex Reviewを必要とする．policy／skillを変える場合は新しいhashと配送controlを再検証する．既存live試験は元source／policy hashとのbindingを保持し，変更後headの実モデル実行として読み替えない．componentの一致と文書差分を明示した証拠評価を行う．レビューが新policyでのlive確認を必要と判断した場合は，その具体的な不足を示し，新しい有限予算なしに試行を追加しない．Proposedの段階では，これらの検証通過も別経路の最終採用やmerge-readyという判断を成立させない．

新規runtimeの免除flagやrunnerのaccepted状態追加はこの文書整合案に含めない．追加のcheckerが必要なら，schema／scope／hash／stale CI／review／P1／history mutationの拒否controlを先に定義し，一般formal integrationの既存拒否controlも同じ意味で実行する．

## Consequences

実装主体と検証の事実を保持したまま，ユーザーのbootstrap方針を評価可能な経路にできる．独立レビューは基盤全体とpolicy整合を同じmain向け対象で評価できる．PR15 ownerの作業を変更せずに依存をまとめられる．

新しい文書／policy hash，レビュー対象範囲，CIを確認する作業が必要になる．現在のpolicyから異なるのは，指定Piによる対象コード実装来歴を今回のCodex bootstrapへ要求しない別評価経路を定義する点である．一般の自主改善gateを変更せず，この点を新しい採用判断としてレビューする．旧来歴を後付けで合格にする方法は採らない．

## Alternatives considered

- 旧sourceをgpt-oss／Piで再実装したと記録する方法は，実行していない来歴の捏造なので採らない．
- 任意の自己改善candidateでpre_audit_workflow_gateを省略する方法は，今回の承認範囲を超えるため採らない．
- PR16をPR15 branchへ先行mergeする方法は，owner branchを変更し，PR15の新head CI／reviewを必要とするため推奨しない．
- gate不足を理由に設計／比較／修正準備まで停止する方法は，承認された作業を進めないため採らない．

## Related repositories

ephy-workerのみ．Runtime／Karteとの実接続や他repo変更はこの案の対象に含めない．

## Date

2026-10-03
