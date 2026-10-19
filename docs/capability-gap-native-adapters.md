# P0保存reportからのCapability Gap射影

## 境界

`ephy_worker.capability_gap_native.derive_native_gap_candidates(manifest_bytes, reports_by_id, *, expected_manifest_sha256)` は標準ライブラリだけを使う純関数です．固定済みP0 reportのraw bytesを，callerが別に保持するmanifest SHA-256へ拘束し，観測・未知・未検証の調査候補へ射影します．元のsynthetic専用APIへ変換せず，P0 checker，実モデル，検索，ファイル入出力を実行しません．CLIやruntime接続は追加していません．

この局所実装は，固定したP0のschemaを読めることを示します．P0依存側の最終受入れ，元source bytesの再検査，producerの真正性，実行，意味上の正しさ，実モデル改善，正式workflow，採用は評価しません．hash一致は，callerが指したreportを読んだことだけを保証します．source_originもcallerの申告です．

## 入力

manifestはUTF-8 JSONで，次の正確なfieldを持ちます．

```json
{
  "schema": "ephy.capability-native-manifest.v1",
  "artifacts": [{
    "artifact_id": "report-1",
    "context_id": "comparison-1",
    "source_family": "plan_completion",
    "source_origin": "test_fixture",
    "sha256": "<report raw bytes SHA-256>",
    "byte_length": 123,
    "expected": {"contract_sha256": "<intended completion-contract raw SHA-256>"}
  }]
}
```

上のhashと長さは説明用です．callerは実bytesの正確な値を入れ，manifestのraw SHA-256を別に保持してAPIへ渡します．report自身から信頼するpinを作り直してはいけません．`reports_by_id` はartifact_idからbytesへのdictで，manifestと完全に同じID集合が必要です．artifact IDとcontext IDはASCII英数字始まりの英数字・dot・underscore・hyphen，長さ1–64です．同じartifact IDまたは同じreport raw hashの重複を拒否します．

| source_family | native schema | expectedの正確なfield |
|---|---|---|
| trial_reconciliation | schema_version整数1 | plan_id，plan_sha256，execution_ledger_sha256，axis，evidence_kind |
| plan_completion | ephy.plan-completion-result.v1 | contract_sha256 |
| planner_completion | ephy.planner-completion-evidence.v1 | job_id，job_contract_sha256，contract_sha256 |

source_originは`test_fixture`または`saved_artifact`です．trialのevidence_kindは`synthetic/mock/live`をそのまま保持し，planでは`not_reported`とします．context_idはcaller envelopeのIDであり，元JobのIDとは扱いません．

manifestは131072 bytes，reportは1048576 bytes，artifact数は1–16，JSONは深さ32，node数32768，文字列8192文字を上限とします．重複JSON key，NaN/Infinity，boolとintの読み替え，未知schema，fieldの不足・追加，矛盾するassuranceを拒否します．失敗は`NativeGapInputError`で，`code`と例外本文に安定したcodeだけを返します．入力全体を検査してから出力を作るため，部分成功はありません．

## 出力とhashの意味

出力のtop-level fieldはschema，ruleset，evidence_basis，assurance，manifest_sha256，assessment_status，evidence，observations，unknowns，gap_candidates，cause_hypothesesです．schemaは`ephy.capability-native-gap-report.v1`，rulesetは`native-p0-report-projection.v1`，evidence_basisは`caller_pinned_native_reports`，assessment_statusは`reported_evidence_only`です．

assuranceは`raw_report_bindings_verified=true`，`source_bytes_rechecked=false`です．producer_provenance，execution，semantic_correctness，real_model_improvement，formal_adoptionはすべて`not_assessed`です．

evidenceにはevidence_id，artifact_id_sha256，context_id_sha256，source_family，source_origin，native_schema，raw_report_sha256，raw_report_byte_length，evidence_kind，bindings，reported_commitmentsを保存します．ID参照のSHA-256は，UTF-8・sort_keys・compact separatorsでcanonical JSON化した値のdigestです．raw fileのdigestとは区別します．bindingsの各値は`{intended, observed, comparison}`で，comparisonは`match/mismatch/unavailable`です．私的なplan_id/job_idはhashだけにします．nullを空hashへ置換しません．

| family | reported_commitmentsのfield |
|---|---|
| trial | canonical_plan_sha256，canonical_execution_ledger_sha256，comparison_available，canonical_comparison_subobject_sha256 |
| core | raw_completion_contract_sha256，raw_response_sha256，raw_plan_text_sha256 |
| saved planner | canonical_runtime_contract_sha256，snapshot_sha256，raw_source_files，raw_terminal_record_sha256，terminal_line，reserialized_checker_input_sha256，raw_original_text_sha256，lead_plan_representation，nested_core |

raw_source_filesは固定5ファイルの`{sha256, bytes}`です．nested_coreはstatus，passed，contract_sha256，response_sha256，plan_sha256を保存します．これらは上流reportに書かれた値であり，adapterがその元bytesを読んだという意味ではありません．trial comparisonは外形だけを検査し，canonical subobject SHAとavailabilityに留めます．集計を個別run statusや能力に読み替えません．

observationは`observation_id/evidence_id/check_key/category/code/stage/status/observation_basis/diagnostic_codes/detail_refs`を持ちます．observation_basisは常に`reported`です．detail_refsは私的なID・field等のhash参照だけです．raw診断path/message，集計label，failure文字列をコピーしません．unknownはcode，observation_ids，evidence_ids，detail_refsを持ち，矛盾の場合はcheck_keyも持ちます．

gap候補のfieldはcandidate_id，category，investigation_area，status，supporting_observation_ids，contradicting_observation_ids，evidence_idsです．cause仮説はhypothesis_id，candidate_id，cause_codeと同じstatus・証拠参照を持ちます．いずれもstatusは`unverified`です．同じcaller contextとintended contractの合格・不合格が競合する場合，両方の観測と`conflicting_observations`を残して，そのcheckの候補生成を抑制します．異なるcontextは合算しません．

## 判定の扱い

- core complete，またはsource_bindings_valid=trueのsaved planner completeは，workflowの構造合格だけを記録します．
- source bindingが成立したincompleteだけが，`planning_contract`という未検証調査候補になります．その候補には失敗した観測IDと証拠IDを結び付けます．
- legacy ack等がinvalid_inputになった場合はevidence issueと計画構造unknownです．P2でworkflow候補を用いるbaselineには，source_bindings_valid=trueかつstatus=incompleteのreportが必要です．
- NO_HYPOTHESISは独立のunknownとして保持し，能力不足を診断しません．
- invalid_sourceはsource binding失敗です．nested checker.passed=trueでも計画構造をunknownとします．途中まで設定されたhash，null，intended pinとの不一致を保存します．
- invalid_contract/invalid_inputでは，欠けたhashや異なるhashを不明・不一致として保持します．構造合格へ補完しません．
- trialのokは記録照合だけです．missing，duplicate，mismatch，未実行のIDを順序と件数を保って記録します．validation未実行や明示的not_executedはunknownです．環境原因，モデル能力，失敗runのstatus，時間，得点を作りません．
- Windows writer表現はexact_utf8とwindows_write_textを区別し，原text，保存file，terminal record，再serializeしたchecker inputのhashを分けます．

## 検証

`tests/fixtures/capability_gap/native-adapters/cases_v1.json` はsource_origin=test_fixtureの手書きreport形と，P0が保存した合成CLI reportのexact UTF-8 bytesを同梱します．後者は元raw SHA-256，byte数，別保存caller pin，保存元basenameを保持します．実モデルの実行結果ではありません．同fixtureに準備済み20項目と対応test名を収録しています．テストはreport射影，外部pin，改変，重複，partial failure，unknown，privacy，矛盾，resource limitを検査します．元P0のlast-record選択や契約照合を再実装・再実行した証明にはしません．

```text
python -B -m pytest -q -ra --junitxml=<candidate外の証拠path> -p no:cacheprovider tests/test_capability_gap_native.py
python -B -m ruff check --no-cache src/ephy_worker/capability_gap_native.py tests/test_capability_gap_native.py
```

実行時は固定環境契約に従い，PYTHONPATHを測定候補のsrcへ，TEMP/TMPを専用証拠領域へ設定します．この文書は検証結果の宣言ではありません．最終候補のhash，実command，独立検証，fresh reviewは候補外の引渡し証拠で確認します．
