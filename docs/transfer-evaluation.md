# Skill／toolの課題間転用評価セット

## 実装済みの範囲

`src/ephy_worker/fixtures/coding/transfer/`に，既存smokeとは別のsynthetic taskを6件置く．開発用3件とheldout 3件は，task IDと初期sourceが重複しない．数値境界，列の重複意味，欠損と値の区別という3つの問題群を両splitに含める．単なるtask数の増加ではなく，開発時の手順や補助を別の契約へ持ち込むための小さな測定interfaceである．

| 問題群 | development | heldout |
|---|---|---|
| 数値境界 | 端数を含むbatch数 | 上下限へのclamp |
| 列の意味 | 最初の出現順の一意化 | 隣接する重複だけの除去 |
| 欠損と値 | 空文字のoptional整数変換 | falsey値を保持するkey lookup |

heldoutは公開synthetic fixtureの運用上の分割であり，秘密の試験や学習混入防止の保証ではない．開発者はfixtureと正解controlを閲覧できる．実測では補助の改善をdevelopmentだけで行い，補助と評価定義のhashをfreezeした後にheldoutを一度評価する．heldout結果を見て補助を直した場合，その結果はdevelopment扱いにし，新しい未使用task集合で次の評価を設計する．

今回の実装は通常のcloud上でのfixture／checker／比較interfaceの整備である．gpt-oss planner → Qwen implementer → 独立検証 → fresh gpt-oss auditという正式な自己改善workflowは実行していない．実モデル性能，転用効果，formal audit，review_ready，merge_readyはいずれも未実証である．既存governanceの変更や例外化は行わない．

## 3つの比較条件

- `none`：課題のgoal，編集可能file，初期implementationだけを渡す．
- `skill`：同じ入力に，一般的な境界／意味保持の修正手順を追加する．
- `tool`：同じ入力に，受け取ったPython sourceをAST解析するread-only toolを追加する．toolはfile pathを受け取らず，sourceの実行，file読書き，networkを行わない．新しいMCP serverは作らない．JSON envelopeは1,048,576文字以内として完全に読み取り，超過時は切詰めず明示拒否する．decoded sourceには別途65,536 UTF-8 bytes上限を適用し，JSON escapeによる増加をsource量へ混同しない．

同一model設定，同一task，environment，budget，repeat indexで比較する．variant以外のhashが変わる比較は認めない．skillとtoolを同時に追加する条件はこの版に含めない．補助の文字数やtool往復も共通context／token／time budgetに含める．反復ごとに条件順を回転するが，完全な無作為化やserver warm-up制御を代替しない．

`comparison_plan()`は明示されたenvironmentとbudgetからbalanced planを返すだけで，modelを起動せず，budgetを強制しない．environmentは`platform`，`python`，`dependencies`，`executor`の非空文字列を必須とする．budgetの`timeout_seconds`はfixtureと一致させる．追加のturn／tool-call／token上限はcallerが定義・強制し，観測値を別途残す．未接続のrunner制御を実装済みと見なしてはならない．

model-visibleな`request`には正解control，checker，検査assertion，他splitの課題を含めない．`FrozenTransferSuite.suite`はcontroller用で，`CodingFixture.mock_changes`に既知good controlを含むため，そのままmodelへ渡してはならない．実際の候補repositoryを用意する際も，モデルの閲覧範囲をrequestのfilesと許可toolだけへ限定する必要がある．既存`eval run --suite smoke`をこのplanの実モデル実行経路として流用したと主張しない．

## 固定checkerとnegative controls

`freeze_transfer_suite()`はsuite，checker，skill，tool，評価harnessのbytesを固定する．検査前後にそのidentityを照合する．checkerのidentityには外置checker，harness，各taskの固定test内容を含める．検査定義はcandidate repositoryの外へ展開し，candidate内のtestを書き換えてもそのtestは実行しない．

`validate_candidate()`は，既存の固定checker／scope制約の方式を踏襲し，fixture初期状態からの完全なfile差分を検査する．candidateは`fixture_repository(fixture)`のcontext内で作り，そのcontext内でimplementationを変更して検査する．内部では既存`create_fixture_repository()`と`CodingFixture`を再利用する．編集前のcandidate rootとGit metadataのbaselineは親processのimmutable memoryへ保存し，fixture全体のhash，正規化したallocation path，device／inodeと結び付ける．candidate内のmanifestやGit設定からbaselineを作り直さず，context終了時に破棄する．信頼されたbaselineがない外部directoryや，fixture／allocationが一致しないcandidateは，Git metadataの有無によらずchecker実行前にscope不合格とする．

test／追加file／削除fileの変更，symlink，hardlink，Windows reparse pointを拒否する．ignoredな追加fileもscope検査の対象である．candidate rootを表す`.`と，`.git`自身および全subdirectory／fileのpath，type，permission，bytes，device／inodeもscope，candidate hash，検査前後のintegrityへ含める．hook／config変更，空directoryの追加・削除，同じbytesを持つmetadata file／directoryの置換も許可しない．candidate rootを同じpathの別directoryへ置き換えた場合も，元のchild objectがすべて同じでも検査の戻り値をintegrity不合格とする．rootのtype／reparse検査はchildの走査前に行い，リンクとhardlinkは外部のbytesを読む前に拒否する．Windows属性はバックアップなどで更新されるvolatileなARCHIVE bitだけを除外し，readonlyを含む他の属性を固定する．creation／access／modify timestampはhashへ含めない．このcandidate hashはallocation identityを含むため，別allocation間で同じsource内容だけを比較するhashではない．通常のimplementation fileについては，従来どおりbytesとexecutable bitsを固定する．

checker実行中のcandidate変化，checker copy変化，fixtureや補助の変化を不合格とする．検査中にmetadataがリンクや非regular fileへ変わった場合もintegrity不合格とし，予期しないfilesystem errorは握り潰さない．checkerから成功exitだけが返っても，期待件数を含む構造化結果がなければ不合格になる．

`run_controls()`は各taskについて次を確認する．modelを呼ばず，synthetic fixture以外のsourceを入力しない．

1. 既知good candidateを受理する．
2. 未修正candidateを拒否する．
3. 関数が欠けた誤実装を拒否する．
4. 正しい実装でもtest assertionを削除したcandidateを拒否する．
5. 正しい実装でも無関係fileを追加したcandidateを拒否する．

検査の単体testにはchecker改変，検査中のcandidate改変，exit 0だけの偽成功，tool入力不正，split／条件不正，timeout／環境不備も含む．既存の`evaluation.py`，`coding_cli.py`，`scripts/validate_self_improvement.py`，governanceは変更しない．既存repository全体の採用gateや監査gateの代替でもない．

このcheckerはsandboxではない．候補Python codeを子process内でimportするため，任意の敵対的codeの安全な実行，network遮断，transient改変と復元，結果偽造への完全な耐性は保証しない．現在は信頼されたsynthetic controlsにだけ使う．実モデルcandidateの実行前には別途OSレベルの隔離，権限境界，出力制限，process-tree timeout，予算強制，provenanceのrunner側記録を検証する必要がある．

## 実行例

追加model，資格情報，download，外部APIは不要である．repository依存が揃った同じPython環境を使う．

```bash
PYTHONPATH=src python -m ephy_worker.transfer_evaluation controls
PYTHONPATH=src python -m pytest -q tests/test_transfer_evaluation.py
ruff check src/ephy_worker/transfer_evaluation.py src/ephy_worker/fixtures/coding/transfer tests/test_transfer_evaluation.py
python scripts/validate_repository.py
git diff --check
```

plan作成はPython APIで行う．例のenvironmentは説明用であり，実行時には実際のversion／lock／runner情報で置き換える．

```python
from ephy_worker.coding_profiles import load_coding_profiles
from ephy_worker.transfer_evaluation import comparison_plan, freeze_transfer_suite

frozen = freeze_transfer_suite()
profile = load_coding_profiles()["mock"]
environment = {"platform": "synthetic-linux", "python": "3.12",
               "dependencies": "actual-lock-sha256", "executor": "planned-adapter-v1"}
budget = {"timeout_seconds": 10, "max_turns": 8, "max_tool_calls": 16}
plan = comparison_plan(frozen, split="development", profile=profile,
                       environment=environment, budget=budget, repeat=3)
assert plan["transfer_effect"] == "unmeasured"
```

## 比較基盤とのinterface

`benchmark.make_run()`へ渡すfixtureは`frozen.task(task_id).fixture`，suite ID／SHA-256／checker SHA-256はplanの同名field，variantは各rowの`variant`を使う．profile，environment，budgetはplan作成時にfreezeした同じobjectの値を使い，`variant_label`と`repeat_index`を対応させる．各rowのcondition hashはbenchmark側と同じcanonical JSON形式で算出する．

`CodingResult`は実際のrunner記録を使う．`validate_candidate()`の固定checker／scope／integrity結果が失敗した場合，candidate内testの成功だけで成功recordを作らない．checker証跡のcandidate SHA-256と最終候補，result，patch identityをrunnerで結び付ける．現在のmoduleはこの観測の自動attestationまでは提供しない．

```python
from ephy_worker.benchmark import compare_runs, make_run

# result は実際のrunner出力．synthetic／mock／liveを混在させない．
record = make_run(
    result, profile=profile, fixture=frozen.task(row["task_id"]).fixture,
    suite_id=row["suite_id"], suite_sha256=row["suite_sha256"],
    checker_sha256=row["checker_sha256"], environment=environment, budget=budget,
    variant=row["variant"], variant_label=row["variant_label"],
    repeat_index=row["repeat_index"], evidence_kind="synthetic",
)
# 全条件の同一task／同一repeat集合が揃ってから集計する．
summary = compare_runs(records, axis="variant")
```

developmentとheldoutは別々に集計する．taskごとの成功率，成功時／全試行の時間中央値，未実行validation，失敗分類を分けて示し，単一総合点や自動採用判断を作らない．小さなsynthetic setでの結果を実業務全般へ一般化しない．controlの全合格はcheckerの回帰確認であり，skill／toolの有効性の証拠ではない．
