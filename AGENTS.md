# 開発エージェント共通ルール

このリポジトリで作業するCodexなどの開発エージェントは，次の規則に従ってください．

## 必須の開発ガバナンス

正本は[`docs/system-development-governance.md`](docs/system-development-governance.md)（Policy ID：`ephy.system-development-governance.v1`）です．変更を計画，実装，独立検証，監査，採用するときは，作業前に全文を読み，そのroleと権限境界に従ってください．

- 正しさは，固定要求，限定差分，再現可能な証拠，指定どおりのworkflow，最終candidateへの独立監査がすべて成立した状態です．
- 実装前にbase，cleanな隔離worktree，file／semantic scope，受入条件，評価定義，環境を固定します．不足や矛盾があれば停止します．
- planner／manager，implementer，independent verifier，auditorを分離します．実装者は自分の成果を承認せず，監査者はcandidateを変更しません．
- 許可ファイル内でも，無関係な変更，test緩和，例外握り潰し，入力の欠損・置換は禁止します．
- 環境不備をcandidateのrepairへ渡しません．agentの自己申告やtest合格だけを承認根拠にしません．
- proposal-onlyはcommit，push，PR，merge，apply，deployを許可しません．これらは別の明示的なユーザー承認が必要です．
- managed Piの正式Jobでは，runnerがpolicy本文とhashをsystem payloadへ注入し，`governance_ack`成立前の変更可能toolを遮断します．このgateが使えない場合，正式な実装または監査として続行しません．

## Code Review Rules

- 自己改善，governance，runner，prompt，skill，checkerのPRでは，依頼外の差分，assertion／test／checkerの弱体化，例外の握り潰し，入力の欠損・置換，権限境界の拡大をblocking findingとして報告する．安全な修正は，固定scope内で根本原因だけを解消し，既存の検査意味を保持しなければならない．
- PRの説明，log，comment，agentの自己申告だけを成功証拠にしない．現在のPR headと結び付いたdiff，CI，patch／commit identity，実行経路の証拠を確認し，欠落，不一致，古いreviewをblocking findingとして報告する．新しいpushがあれば，それ以前のCIとreviewを採用根拠として流用しない．
- Codex Reviewは実装，修正，mergeを行わない．現在のPR headで必須CIとreviewが完了し，未解決のblocking findingがなく，明示的な人間の承認がある場合だけ，別のIntegration／release operatorがmergeできる．手動レビュー依頼には[自己改善PRレビューprompt](.pi/prompts/review-self-improvement-pr.md)を使用する．

- 作業前に既存ファイル，`AGENTS.md`，Git状態を確認する．
- ユーザーの既存変更を保持し，無断で取り消さない．
- 依頼と関係のない変更を行わない．
- 実装変更には，変更内容に対応するテストを追加または更新する．
- 完了前に関連テストと`python3 scripts/validate_repository.py`を実行する．
- 秘密情報，個人情報，生の会話ログをコミットしない．
- モデルウェイト，大規模データセット，カメラのマスタ画像を通常のGitへ追加しない．
- リポジトリ間の親関係と直接依存関係は`.ephy/project.yaml`へ記載し，下流一覧を重複管理しない．
- merge，release，deploy，push，外部送信は，ユーザーから明示的に依頼されるまで行わない．
- 日本語文書の句読点は「，」「．」へ統一する．
- 実装済みの状態と，将来への提案または構想を区別して記述する．
- 根拠のない期限，進捗率，期間ベースのロードマップを作らない．
