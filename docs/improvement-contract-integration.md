# P2 合成契約接続の準備

この差分は，一つの合成ケースについて，既存の計画完了 checker／保存出力 adapter，Capability Gap の native adapter，Evaluation Discovery，全 trial 照合を接続するテストの準備です．実モデル評価，運用接続，採用，正式監査は行いません．

Discovery，corrected Trial，計画 checker／adapter，Gap native adapter は local P2 合成接続向けに受入れ済みで，固定 15 file を専用 candidate へ exact-byte 搬送しました．実 API を呼ぶ一件の接続，lineage record，部品の負例，停止済み campaign の resume control はテストに記述済みです．依存受入れは task root の `evidence/accepted-dependencies-v3.json`，native 搬送は `evidence/native-transport-v3-result.json` に対応します．実行結果は task root の `evidence/verification-runs/sol-integration-v1/summary.json` を別証跡の予定保存先とし，その存在や合否をこの文書からは主張しません．この test file 自体は統合合格の証拠ではありません．skip／xfail，空 assertion，仮 adapter，native API の mock は追加していません．

## 固定する一件

入力は `tests/fixtures/improvement_integration/planner_gap_case_v1.json` 一件です．候補は `explicit-plan-requirement-map` 一つで，baseline と candidate を各一 trial，repeat は 1 とします．model descriptor，task，suite，checker，environment descriptor，30 秒の budget は共通で，variant identity と供給する合成出力だけを変えます．30 秒と wall time は合成 metadata であり，実時間の測定や予算遵守の証明ではありません．モデル呼出し，検索，追加 repair は 0 です．

主ケースの baseline は，読取可能な plan JSON から mandatory な `fixed` check の参照だけが欠けた計画です．既存 checker の `check_coverage`／`incomplete` を予定します．candidate は要求・受入条件・固定検証を明示した構造化計画です．`ACK` は `invalid_input` の別負例に残し，planning-contract failure へ変換しません．入力の requirements／prohibitions は fixture に明記し，自由文から後付け抽出しません．fixture の既存 key `pending_connection` は互換性のため保持し，受入れ済み native source／patch／development contract／v3 受入れ契約と外部実行結果参照を記録します．preliminary output shape の識別子と `unverified_candidate_example` は過去の例示 metadata として保持し，受入れ済み依存 pin や実行結果の代用にはしません．`native_inputs` の三つの context は baseline 構造，candidate 構造，全 trial accounting を区別する caller envelope identity であり，producer の job／run ID ではありません．同一 context に存在する conflict を分割して隠す手段として使いません．

fixture の `invented_result` は独立した架空の `CodingResult` 記録です．計画 checker の `completed` を task validation の成否へ変換しません．計画に記載した check の argv，coding task の argv，mock descriptor，source URL は一切実行・取得しません．

## 依存の境界

| 依存 | 固定識別子 | 固定依存の扱い |
|---|---|---|
| 共通 main | `01bf02afc755fef12c752cc8638dd19f20e8d033` | 専用 clean worktree の base |
| Evaluation Discovery | source `aa57b90858789cf5a49fa21f773a1875fe8c749424a1a7661726e6921b82d153`，patch `3401fecb21bdd6a96d28df5c09e11fd72a8e07ea0db3518625f1a67f2958db38` | 受入済み bytes を搬送，元 3 file は無変更 |
| Corrected Trial | source `b8e99e86e42bcb737f9cedc4afc9538a7029b1eae889d5e136809e03bb38622e`，patch `2ad980fe31bce4f5ba6bbcf8d01f2e0b577f0ca87e340be0b4527a5c46e89455` | 受入済み bytes を搬送，schema version は plan／ledger とも 1 |
| Plan checker | source `d8d4c2c47a97cdaac946c5ff5f4ae5cee1ea1bebcec96c0e8b2d616350b45180` | local P2 合成接続向け受入れ済み |
| Saved-plan adapter | source `52a8be641b32db11596f708c657d83ec7ce3c6de4eafdae3a732422a8b0c1ddf`，combined patch `b61db92784fedff39625e1c09dc1ae91191e695c23d0008872b542eaff08c97b` | 受入れ契約 `656478c94fb9b08f0c75efb9a983ee14a92952f79d4a57d247a86875d92edb7d`．owner の passing gate は別凍結 v3 replica に対応．P2 の実行結果は外部結果証跡を参照 |
| Gap native adapter | source `cadf8d0aa1a1ff23e03e65394e8b35a37965731bc244c0a5e193c7dd54f6c4ac`，patch `ad7a2a87df2e69348fe39d1d538dc207e5067045d41ff35d6ac3143224b3e789`，development contract `72e078ce87556b59c2fda4f4c9641236023674737d47aa91c0f543579034553e` | local P2 合成接続向け受入れ契約 `2483b4db4e48ef0f0af326f4399d0485d13aa9cf77df97b62e66170e5a0b3204`．固定 4 file の搬送結果 `7f55d52648ae1beb49fafbbf490516f8ac8b2406003c8f03175879fd195529e7`．schema／ruleset は固定済み，実行結果は外部結果証跡を参照 |

upstream source，test，doc をこの統合差分で編集しません．固定 15 file の輸送は受入れ契約と exact-byte 搬送証跡に結び付け，このメタデータ確定ではその bytes を変更しません．全 trial の完成済み全回帰をこの準備のために二重実行しません．計画 adapter の旧 v2 raw-index interval failure は未解決のまま保存され，v3 replica の合格へ置き換えません．旧 Gap core は native から import されず，この統合でも使用しないため搬送しません．

## 保持する根拠

保存計画は owner API `check_planner_only_output` の 5 入力を準備します．raw completion contract hash，expected job ID，canonical runtime contract hash は入力を破壊する負例より先に保持します．結果の top-level `completed`，`source_bindings_valid`，全 source bytes/hash，terminal JSONL line，checker へ渡した再直列化 message，original text の hash を区別します．nested checker の success だけを完了と読み替えません．`structural_only`，`checks_executed=false` と semantic/workflow/provenance/adoption の未評価状態を保持します．保存出力の検査結果を job／capture 元 directory へ書き込みません．

Gap は受入れ済み native adapter だけで接続します．native P0 evidence を `synthetic_observations` へ付け替える shim は作りません．計画の構造不足は未検証の workflow planning gap に限定し，model の能力不足や task validation failure と同一視しません．接続 API は `derive_native_gap_candidates(manifest_bytes, reports_by_id, *, expected_manifest_sha256)` です．`ephy.capability-native-manifest.v1` の actual report raw hash／length，family 別 intended pin，`source_origin=test_fixture` を固定します．出力は `ephy.capability-native-gap-report.v1`，`native-p0-report-projection.v1`，`caller_pinned_native_reports` を保持し，候補と仮説は `unverified` のままです．Gap candidate ID／supporting observation IDs／evidence IDs，native manifest／report hash，Research Report raw hash／job ID と claim selection を test-local `ephy.synthetic-improvement-lineage.v1` record へ保存する準備をしました．これは caller が与えた連結であり，Research Report が Gap の原因を診断したという保証ではありません．native evidence の actual raw report digest／size／schema／family／source origin，canonical JSON で hash 化した artifact／context ID，intended と observed の binding，reported source／terminal／checker／text commitments を入力 bytes と照合する assertion を追加しました．同一 context と同一 intended pins の二つの保存報告を最初から構成する独立 control では，planning check の passed／failed 両観測を保持し，`conflicting_observations` を出し，候補を抑止する予定です．主ケースの baseline／candidate は別 context と明示し，個々の観測を残します．test-local lineage assertion は native の candidate → observation → evidence → raw report と，実 Discovery の claim → source／passage／quote の両側を保持済み hash に結び付けます．同じ helper を positive／negative で使い，negative は保持後の submitted copy だけを変えます．生産用の新しい判定 API ではありません．

Discovery は完全な `Report` を決定的に直列化した実 bytes の SHA-256 と job ID を外部期待値に渡します．選択は claim ID のみで行い，一つの候補を `proposed` のまま保持します．source／passage／claim／evidence ID，実 passage／quote UTF-8 hash，引用 offset を確認します．`checked=true` だけでは support にしません．`Report.gaps` は調査上の情報不足として保持し，Capability Gap の代用にしません．version，license，必要資源，既存実装との差分は未確認のままです．外部 query の構築・送信，code 実行，install，MCP 有効化はありません．

Trial は結果の生成前に二つの予定 run ID と全 Conditions から `TrialPlan` を作り，`plan_sha256` を保持します．その後，合成 raw `CodingResult` と既存 `make_run` の出力を `record_execution` に渡し，ledger を固定して `execution_ledger_sha256` を別保持します．負例はその後の submitted copy を変更し，期待 pin を更新しません．raw JSON bytes hash と canonical plan／ledger／BenchmarkRun／CodingResult fingerprint は別物です．正常な合成記録でも `execution_verified=false` のままで，実行や source authenticity は証明されません．

## 既存 campaign の再利用

専用 temporary directory に合成の停止済み checkpoint，plan，verifier freeze を保存し，実 `formal_campaign.run_campaign(..., resume=True)` を呼ぶ control を準備しました．plan と freeze の検査は mock しません．既存の `exclusive_lock` も test-local path に限って実行します．`submit`，runner constructor，`freeze_submission_identity` は呼ばれれば失敗する sentinel に置換し，次の Job や identity 再採取に進めないことを検査します．

停止状態の保存，`planner_stopped` が proposal-valid でないこと，plan の変更，freeze 欠落・改変の拒否を確認する予定です．さらに，初期状態から freeze の内部 plan binding または identity 件数だけが不整合で，retained hash はその initial bytes と一致する二つの独立 control を追加しました．元の hash-tamper control は保持し，改変済み submitted checkpoint に合わせて pin を再計算しません．ここで作る freeze は合成 control record であり，実 verifier identity の認証証拠ではありません．新 scheduler／framework は追加せず，campaign 実装も変更しません．

## 合否と残作業

| 観測 | この作業で言えること |
|---|---|
| 全 native binding が成立し baseline は incomplete，candidate は complete | 合成ケースで期待した構造差を観測．実モデル改善は未評価 |
| 正しい binding の candidate が構造条件に不合格 | 合成条件内で不合格．能力一般の判定ではない |
| 未実行，欠落，不一致，unsupported native schema，未知の検証 | 検証不能．改善へ昇格しない |
| 計画が `no_hypothesis` | 独立した upstream 結果を保持し，通常失敗や能力不足へ変換しない |
| P0 `ok=true`／記録整合性／合成 test pass | 実行証明，改善確認，正式監査，採用のいずれでもない |

実際の `improvement_confirmed` はこの作業では到達不能です．別途承認された実行，trusted recorder と実予算・model の証拠，固定 validation の実行，完全な比較，適用される formal review が必要です．test 内に別の合否判定 framework は作りません．

最終 fixture／source snapshot と固定 command の凍結，対象統合と関連回帰，Ruff，repository validation，diff check，前後不変性の独立検証，fresh reviewer による同一差分と証拠のレビューは，candidate 外の証跡へ分離します．一件の実 API 接続，plan adapter の terminal／no-hypothesis controls，native raw report／manifest の改変拒否，詳細 native binding，同一 check conflict，lineage 不一致はコードに記述済みです．実際の実行結果と合否は task root の `evidence/verification-runs/sol-integration-v1/summary.json` に保存された record と，その最終 fixture／source／patch bindings を確認してください．参照先の存在や将来の合格はここでは主張しません．その検証も local synthetic verification の範囲であり，formal `review_ready`，commit，push，PR，merge，採用の証拠へ読み替えません．
