# T-098 ResearchJob v1 プロトコル設計案（Proposal）

**ステータス**：設計案（未実装・未受入・未採用）．本書はT-098のドキュメント設計提案のみであり，実行可能なcontract，schema，endpoint，checker，T-098完了，採用のいずれも主張しない．実行系とschema検証はすべてfuture workである．
**対象タスク**：T-098 ephy-worker protocol／job contractの設計．
**関連**：ADR-0003（既存 `web.collect` contract 0.4 との互換性維持）．

## 1. スコープと非目標

### スコープ
- `ResearchJob` v1 のJSONエンベロープ定義とライフサイクル（submit／lease／heartbeat／artifact／cancel／retry／audit）の設計．
- 既存の `web.collect` contract 0.4 との明確な分離と，同一Manager上での共存可能性の担保．
- 古いlease generation，古いrevision，取消済みJobによる書き込みを拒否するfencingの定義．
- 信頼済みManagerによるatomicな権限・状態検証と，Runtime側 Adoption の責任分界の定義．

### 非目標
- 実装コード，schema定義ファイル，endpointの作成，リポジトリへの実装変更，Runtime／Karteとの実統合．
- 既存 `web.collect` 0.4 の仕様変更や廃止（0.4 は現状の稼働契約であり，本書は将来設計として独立させる）．
- 認証・認可基盤の新設（既存のHTTP header credential運用を前提とする）．
- 分散合意形成や投票による真実性の決定（Workerは独立した証拠収集のみを行う）．

## 2. 既存 web.collect 0.4 との互換性

`src/ephy_worker/contracts.py` が定義する `web.collect` 0.4 は，`CONTRACT_VERSION = "0.4"`，`SubmitJob`（`submit_key`，`parent_job_id`，`input`，`target_worker_id`，`budget`，`deadline_seconds`）と，Managerが付加する `JobEnvelope`（`job_id`，`input_hash`，`created_at`），`Attempt` の `lease_token`，terminal状態 `completed`／`partial`／`failed`／`cancelled`／`lost` を持つ実装済み契約である．`src/ephy_worker/manager.py` は submit_key の一意制約と `submit_key_conflict`，`UPDATE ... WHERE state = 'queued'` によるatomicなlease claim，parentの残budget・残時間からのchild予算導出，queuedへのcancel即時確定とrunningへのcancel記録を実装している．

ResearchJob v1 はこの0.4とは別系統の将来プロトコルであり，`protocol_version` と `job_type` を明示することで混同を防ぐ．0.4 の既存フィールド・状態・挙動を変更せず，同一Manager上で job type を discriminator として共存させることを設計方針とする．本書の語彙（`submitted`，`leased`，`running`，`succeeded`，`failed`，`cancelled`，`lost`）は0.4の実装済み語彙とは独立であり，0.4側への読み替えは行わない．AdoptionはJob状態ではなく§5.7の別系統記録である．

## 3. 依存関係と未解決事項

本設計は以下の依存先が未確定であり，現状では「提案」であって実装可能な契約として扱わない．

- **T-033**：Runtime側のJobポリシー定義と結果採用（Adoption）ロジックの確定．
- **T-122**：共通の knowledge-lifecycle 契約（kirimine170/ephy issue 5）の確定．Karte連携仕様そのものではなく，知識のライフサイクル契約としての依存である点に注意する．
- **T-084**：mainブランチへの統合確認および既存ADRとの整合性検証．

## 4. バージョン付きJSONエンベロープ（提案）

すべての通信は `protocol_version` を含むJSONで行う．以下は **Managerがlease発行後にWorkerへ渡す leased envelope の合成例**（synthetic）であり，Submitの入力そのものではない．Submitの入力（immutableなSubmit入力）と，Managerが導出するleased envelopeを分離して扱う．識別子はすべて公開の合成例であり，実Job ID・実URL・資格情報は含まない．

```json
{
  "protocol_version": "1.0",
  "job_type": "research_job",
  "job_id": "rjob-synthetic-0001",
  "parent_job_id": null,
  "attempt_id": "att-synthetic-0001",
  "lease_generation": 1,
  "target_revision": "git-sha-synthetic-target",
  "policy_revision": "policy-hash-synthetic-policy",
  "submit_key": "synthetic-submit-key-0001",
  "created_at": "2026-10-15T01:00:00Z",
  "lease_expires_at": "2026-10-15T01:30:00Z",
  "payload": {
    "query_scope": "public_web_only",
    "constraints": {
      "max_results": 10,
      "allowed_domains": ["example.com"]
    }
  }
}
```

**注意**：`payload` 内には資格情報，プライベートなコンテキスト，ローカルパス，実スプレッドシートIDを含めない．資格情報はHTTP headerのみで伝達し，payload，artifact，ログへ書かない．`target_revision` と `policy_revision` は互いに比較せず，それぞれが独立に凍結済みの期待値と一致するかをManagerが検証する．

## 5. 操作と権限の設計（提案）

### 5.1 権威ある検証はManagerがatomicに強制する

認可，プロトコルversion，full-input hash，lease tuple（`job_id`／`attempt_id`／`lease_generation`），`target_revision` と `policy_revision` の独立検証，状態前置条件のすべては，信頼済み **Manager** が単一のatomicな操作として強制する．Workerは依頼者（requester）にすぎず，自分のcheckだけで権限を得たと扱わない．実行中のJobが保持する `target_revision` と `policy_revision` は，それぞれ現在の凍結済み期待値と別々に照合し，一方を他方と突き合わせる検証は行わない．いずれかが不一致なら実行も書き込みも拒否する（`revision_mismatch`）．

### 5.2 Submit と Idempotency（冪等）

- canonical full-input hash は，`protocol_version`，`job_type`，`target_revision`，`policy_revision`，scope，policy，budget，payload など，Submit時に確定する **すべてのimmutableな入力フィールド** を含む．後からManagerが割り当てる `attempt_id`，`lease_generation`，`job_id`，`created_at` は含めない．
- 同一 submit_key・同一 full input の再Submitは，既存Jobレコードを **現在の状態のまま** 返す．`submitted` へリセットしない．terminal状態のJobでも同じ認証済み同一入力Submitは既存viewを返すだけで，何も変更しない（positive case）．
- 同一 submit_key・異なるimmutable入力は `submit_key_conflict` として拒否する．
- Submitは信頼済みRuntime（または承認された依頼者）のみが発行できる．

### 5.3 Lease／Heartbeat／Artifact

- Workerがleaseをclaimすると，Managerは **atomicな単一のclaim操作** で `attempt_id` と `lease_generation` を発行する．有効なleaseがない `submitted` 作業に対しては，複数のclaim試行のうち高々1つだけが勝つ．2人目のclaimantを「単純に先行者を置換する」とは扱わず，`lease_conflict` で拒否する．
- heartbeat（lease renew）は有効期限内のみ延長可能で，期限切れ後の延長は不可（`lease_expired`）．期限切れはsweeperがJobを `lost` へ遷移させることがある．
- artifactは許可されたmedia typeとSHA-256・サイズ検証を受け，`job_id` と `attempt_id` に紐づく．
- lease claimが `running` へのatomic遷移を兼ねる場合，leased envelopeは追加のJob状態ではなく **view** である．状態グラフ上の状態はJobの実状態のみとする（§6）．

### 5.4 結果の冪等（Idempotency）とfencingの優先

- 新しい結果の受け付けは，**現在の `running` かつ有効leaseのattempt** からの送信だけである．受け付け前に，exactな `job_id`／`attempt_id`／`lease_generation` tuple，有効lease，独立に凍結された `target_revision`／`policy_revision` の一致，遷移を許す現在の状態をManagerがatomicに検証する．
- 一度受け付けた結果と **完全一致** する同一 `attempt_id` からの再送は，確定済みのreceiptを返すだけである．新しい取り込み，Adoption，状態変化は起こらない（positive case）．
- 同一receipt identityで **異なる内容** の結果は拒否（`result_conflict`）．
- fencingと取消は冪等より優先する．`cancelled` または `lost` のJobに対する結果送信は，古い `attempt_id`／`lease_generation` が一致しても，本文が過去と完全一致しても，常に拒否する（`stale_lease`／`job_terminal`）．

### 5.5 Cancel と finalize の競合

- Managerのcancel受理（`running` → `cancel_requested` のatomic barrier）は，それ以降の **新しい結果受け付けを直ちにfenceする**．Workerの停止確認（`cancel_requested` → `cancelled`）は後続の別イベントであり，両者を区別する．
- finalizeが先に勝った場合，Jobはterminal（`succeeded`／`failed`）になり，後続のcancelは既にterminalなviewを返すだけで，successを上書きできない．
- cancel受理が先に勝った場合，後続の success／failure finalize は拒否（`result_conflicts_with_cancel`）．cancel受理後の遅延結果は，古いID一致でも受け付けない．
- `submitted`（queued相当）へのcancelは即 `cancelled` とし，再queueへ戻さない．

### 5.6 Retry（信頼済みManager/Runtimeのみが発行）

- 自動再実行はない．Workerが自分自身へretryや新しい `attempt_id` を付与することは禁止する．
- 信頼済みRuntimeの承認を受けたretryは，Managerによる **新しいchild Jobの作成** である．新しい `job_id` に `parent_job_id` を設定し，新しいsubmit key，親の残budgetから導出した **残額のみ** のchild budget，親の残り時間内に収まるdeadlineを与える．親の `failed`／`lost`／`cancelled` レコードを `submitted` へ戻す操作は存在しない．
- child budgetの予約は，外部台帳（ledger）のatomicな予約として行い，並行または重複のretry要求が同じ残額を二重に消費できないようにする．retry要求のstable identityをchild submit keyへ束縛し，ネットワーク再送は同じchildを返し，別の子を作らない．
- 親のcore状態・入力・結果は不変であり，台帳とaudit recordはJobレコードとは別系統の記録とする．
- `cancelled` のJobは自動的にretryしない．child作成も信頼済みRuntimeの承認が必須である．
- 新しい `attempt_id` と `lease_generation` は，Managerだけがatomicなlease claim時に1度だけ発行する．Workerはどちらも発行しない．

### 5.7 Audit と Adoption の境界
- すべての状態遷移，lease発行，結果受領，cancel受理，retry child作成を監査ログへ記録する．ログへ資格情報を含めない．
- Runtime Adoption は，terminalなWorker Jobを `adopted` へ遷移させる操作ではなく，**別系統の信頼済みライフサイクル記録／判断** である．Adoption直前にも現在の `target_revision` と `policy_revision` を独立に再検証する．
- WorkerはKarteへ一切書き込まない．Karteおよび知識台帳への書き込みはRuntime側の責任とする．

## 6. 状態グラフと遷移表（提案）

### 6.1 1つのJobの状態グラフ

```mermaid
stateDiagram-v2
    [*] --> submitted: Submit（Managerがfull-input hash確定）
    submitted --> leased: lease claim成功（Managerがattempt_id／lease_generation発行）
    submitted --> cancelled: cancel受理（即terminal）
    submitted --> lost: deadline経過
    leased --> running: 実行開始（claimと同一atomic操作の場合はleasedはview）
    leased --> cancel_requested: cancel受理
    leased --> lost: lease期限切れ／実行開始前loss
    running --> succeeded: finalize（有効lease・revisions一致）
    running --> failed: finalize
    running --> cancel_requested: cancel受理（atomic barrier）
    running --> lost: lease期限切れ（sweeper）
    cancel_requested --> cancelled: Worker停止確認（status=cancelled）
    cancel_requested --> failed: Worker停止確認（status=failed）
    succeeded --> [*]
    failed --> [*]
    cancelled --> [*]
    lost --> [*]
```

- terminal状態（`succeeded`／`failed`／`cancelled`／`lost`）は終端であり，terminalから `submitted` へ戻るretry edgeはこのグラフに存在しない．
- retryはグラフ内のedgeではなく，信頼済みRuntime承認による **child Jobの新規作成**（別Jobの `[*] → submitted`）として表現する．
- lease claimが `running` へのatomic遷移を兼ねる実装では，`leased` は追加状態ではなくleased envelopeのviewとし，グラフ上は `submitted → running` の短縮と読み替えてよい．表とグラフで同じ意味を使う．
- Adoptionはグラフ外であり，terminal Jobとは別の信頼済みライフサイクル記録として扱う．

### 6.2 状態遷移表
| 操作（actor） | 前提条件 | 結果状態 | 不正・遅延遷移の扱い |
|---|---|---|---|
| Submit（信頼済みRuntime） | submit_key未使用，full-input hash確定 | submitted（新規） | 同一key・同一immutable入力：既存Jobを **現在の状態のまま** viewで返す（全状態，無変更）／同一key・異なる入力：submit_key_conflict拒否 |
| lease claim（Worker依頼，Manager発行） | submitted，有効active leaseなし，deadline前，revisions各々凍結期待値と一致 | leased／running（attempt_idとlease_generationをManagerが1度だけ発行） | 並行claimは高々1勝者，敗者はlease_conflict／revisions不一致はrevision_mismatch／terminalなら拒否 |
| heartbeat（Worker） | 現在のattempt，有効lease期限内 | 状態不変（lease延長） | 期限切れ後：lease_expired（延長不可，lost化されうる） |
| 結果finalize（Worker） | running，exact job／attempt／generation tuple，有効lease，revisions一致 | succeeded／failed | cancel受理後またはterminal後：stale_lease／job_terminal拒否（旧ID一致・同一本文でも不可）／完全一致再送：既存receiptのみ返す／同一receipt identityで異なる内容：result_conflict |
| cancel受理（Runtime→Manager） | submitted | cancelled（即terminal） | 同一入力Submitのview返却とreceipt再送のみ許容，他の新規変更操作は全て拒否 |
| cancel受理（Runtime→Manager） | leased／running | cancel_requested（atomic barrier，新結果を即fence） | 同上 |
| cancel受理（Runtime→Manager） | cancel_requested | cancel_requested（冪等） | 同上 |
| cancel受理（Runtime→Manager） | finalizeが先行しterminal | 既存terminal viewを返す（無変更） | successの上書きは不可 |
| Worker停止確認（Worker） | cancel_requested，現在のattempt | cancelled／failed | 有効leaseとtuple検証はfinalizeと同じ／fence後の新規結果は不可 |
| deadline／lease loss（Manager sweeper） | runningかつlease期限切れ，またはsubmittedかつdeadline経過 | lost | 以後の結果送信は旧ID一致でも拒否 |
| retry child作成（信頼済みRuntime承認，Manager実行） | 親がfailed／lostのterminal，残budget・残時間あり，stable retry identity | 新child Jobがsubmitted（parent_job_id設定，残額budget予約） | 親のstate／入力／結果は不変／台帳予約はatomicで二重消費なし／同一retry identityの再送は同一childを返す／cancelled親は対象外 |
| Adoption（Runtime） | Jobがterminal，revisions再検証PASS | Job状態は不変，別系統の採用記録のみ作成 | Jobをadoptedへ遷移させる操作は存在しない／WorkerのKarte書き込みは禁止 |

## 7. 計画する合成（synthetic）受入ケースと将来の実行可能作業

以下はすべて **計画段階の合成ケース** であり，実行もschema検証もされていない．識別子は公開の合成値のみを使う．

### 7.1 正常系（positive）
1. 同一submit_key・同一full inputの重複Submitが，terminal状態を含む任意状態で既存Jobの現在viewを無変更で返す（terminalを開かない）．
2. 信頼済みRuntime承認によるretry child作成：新 `job_id`＋`parent_job_id`＋新submit key＋残額child budgetでchildがsubmittedになり，親はterminal不変．
3. 同一retry要求identityのネットワーク再送が同一childを返し，別childを作らない．並行retryでも台帳予約が残額を二重に予約しない．
4. 受け付け済み結果と完全一致する再送が，取り込み・Adoption・状態変化なしに既存receiptのみ返す．
5. cancel受理がfinalizeより先：後続のsuccess finalizeは拒否され，Worker停止確認でcancelledが確定する．
6. finalizeがcancel受理より先：Jobはsucceededでterminal，後続cancelはterminal viewを返しsuccessを上書きしない．
7. 有効leaseの現在のattemptからの正常なfinalize／heartbeat／artifact受領．

### 7.2 拒否系（negative）
1. terminal Jobへの同一キー・異なるimmutable入力Submitと，terminal Jobへの新規変更操作（結果送信，lease claim，renew）のすべてが拒否され，terminalは不変．
2. 古い `attempt_id`／`lease_generation`，または `cancelled`／`lost` 後の遅延結果が，古いID一致・本文完全一致でも拒否されること．
3. 同一receipt identityで異なる内容の結果拒否（result_conflict）．
4. 有効leaseのないsubmitted作業への並行lease claimで勝者が高々1つで，敗者がlease_conflictになること．
5. `protocol_version` 不一致の拒否（protocol_mismatch）．
6. `target_revision` 不一致と `policy_revision` 不一致を別々のケースで検証し，互いとの比較では不一致を検出しないこと．
7. Worker自身によるretry，`attempt_id`／`lease_generation` の自己発行，Karte書き込みの拒否．
8. Workerの停止確認を伴わないlease期限切れがlostへ遷移し，以後の結果送信を拒否すること．

### 7.3 将来の実行可能作業（future work）
- JSON schemaの確定と，既存0.4スタブへの非干渉な共存テスト．
- 上記ケースの実executable testへの落とし込みと，Manager側atomic検証の独立検証．
- 外部台帳によるchild budget予約の永続化と復旧時の挙動確定．
- `policy_revision` のハッシュ生成方法，`lease_generation` の永続化，大規模artifactのストリーミングアップロードの可否．
- T-033／T-122／T-084の確定後のRuntime Adoption，knowledge-lifecycle契約との接続設計．

---
**注記**：本書はT-098の設計候補であり，承認・実装完了・採用を示すものではない．記載した動作の実行検証は§7.3の将来作業であり，現時点の実装済み証拠は存在しない．
