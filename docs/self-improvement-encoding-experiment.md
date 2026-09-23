# 自己改善実験：Windows CLI encoding接続確認（2026-09-23）

## 結論

1件のencoding課題について，固定した評価定義`self-improvement-gate-v2`＋`windows-cli-encoding-v3`で，HEAD `bd62feda41eeca867e8a98c0b99802a54fbf07d8`のbaselineは既知の`UnicodeDecodeError: 'cp932'`で失敗し，Piが作ったcandidateは同一コマンドで合格した．独立の挙動確認，repository validation，Ruff，変更範囲，`git diff --check`も合格し，statusは`review_ready`である．これは評価経路の接続確認であり，自己改善skillの利用やQwen subagentの効果を示すものではない．測定時のcandidateは未commit・未applyだったが，後続のGitHub整理で合格差分だけを本repositoryへ適用した．

## 基盤としての変更（Codex）

- 既存の独立gateである`scripts/validate_self_improvement.py`を拡張し，選択Python，baseline／candidateのGit rootとHEAD，fixture byte，対象source import先，必要module／file，Ruffを測定前に確認する．各測定は該当worktreeを`cwd`とし，その`src`を`PYTHONPATH`の先頭に置く．`PIP_NO_INDEX=1`，`UV_OFFLINE=1`で自動DLに依存しない．`PYTHONUTF8`／`PYTHONIOENCODING`は両側から除外した．候補を走らせる前に既知のbaseline失敗文字列を確認し，再現できなければcandidateを`unexecuted`とする．
- 同じargv，fixture，HEAD，Pythonを両側で使用する．`status`は既存Job語彙の`failed`（環境不足），`verification_failed`（不合格），`timed_out`，`cancelled`，`review_ready`を用い，`reason_code`と`baseline_state`／`candidate_state`で未実行と失敗を区別する．
- 固定挙動確認`scripts/check_encoding_behavior.py`をcandidate worktree外から実行する．元のtestの2つのCLI probeとassertion，skipなしをASTで照合し，UTF-8の厳密な全文decode以外の変更，decode errorの握り潰し，内容切詰めを拒否する．CLIを実行してhelpの内容とmissing-profileの拒否も独立に確認する．`tests/test_check_encoding_behavior.py`にmutation probeを追加した．
- 基盤の関連テストは`26 passed`，Ruffと`scripts/validate_repository.py`は合格．元からdirtyだったskill／文書と未commitのvalidator／testは破棄・commitしていない．

## 固定した評価条件

| 項目 | 条件 |
| --- | --- |
| 評価定義 | gate `self-improvement-gate-v2`（SHA-256 `2bf2cbc81c76b2361fe09f1b6055f866db6a58fbbb92bf7a77bff17c1a033d68`），挙動確認`windows-cli-encoding-v3`（SHA-256 `31d210462c8b3da9585f530f9d2e8fe65f4ac3e36f3a8a86ef4190b690fd0eab`） |
| HEAD | baseline／candidateとも`bd62feda41eeca867e8a98c0b99802a54fbf07d8`から作成した別worktree |
| Python／依存 | `ephy-worker/.venv/Scripts/python.exe`（3.12），`pytest`，`yaml`，Ruff 0.16.7，両worktreeの`src/ephy_worker`を個別にimport確認 |
| fixture／変更範囲 | `pyproject.toml`の同一byte（SHA-256 `e071db180cf70c9fddbbc3b93151fd9fab65bf5c23903a63672d9ed7ce9a30ae`），変更許可は`tests/test_cli.py`のみ |
| correctness | 両側で同じ`python.exe -m pytest -q tests/test_cli.py::test_cli_help_and_missing_profile -p no:cacheprovider --basetemp <許可済み専用パス>`．`PYTHONUTF8`／`PYTHONIOENCODING`は未設定．baselineには`UnicodeDecodeError`と`cp932`を要求 |
| モデル／ハーネス | Pi 0.86.1，gpt-oss-20b-MXFP4 lead，llama.cpp local endpoint，32K context．Qwen3-Coder-Next-Q4_K_Mはloadedだが，この試行では呼ばれなかった |

## 結果と証拠

| 対象 | correctness | 独立gate | 判定 |
| --- | --- | --- | --- |
| baseline | 2026-09-23 11:56:51～52 JST，exit 1，CP932 decode失敗 | 固定挙動自体は破壊されていない | 既知失敗を再現 |
| candidate（Piによる2行の`encoding="utf-8"`追加） | 2026-09-23 11:56:52～54 JST，exit 0 | fixed behavior／repository／Ruff／scope／diff checkすべてpass | `review_ready`，未apply |

機械判定は`../.pi-dual-runtime/audits/encoding-v3-result.json`，事前baseline確認は`../.pi-dual-runtime/audits/encoding-v3-preflight.json`，Piイベントは`../.pi-dual-runtime/audits/encoding-pi-v3-20260923.jsonl`，candidate差分は`../.pi-dual-runtime/worktrees/encoding-candidate-v3-20260923`にローカル保存した（これらの生ログはGitHubへ含めない）．評価側スクリプトのhashはPi実行前後で不変．Pi自身の`pytest`呼出しはbash側で`command not found`（exit 127）であり，Piによるtest成功ではない．合格はCodexが後からコードを直した結果ではなく，固定済み独立gateによる再実行結果である．

skillはPiのsystem contextに`ephy-worker-self-improvement`として掲載されたが，イベントログに本文のreadは0件，Qwen `subagent`呼出しも0件だった．従って自動発見の表示までは確認，読込・利用・別Jobへの効果は未確認．Piの最終文面や自己評価は合否に使っていない．

評価定義を作る途中の試行は成功率の分母から除外した．最初のPi起動はWindows引数引用の誤りで課題文が分割されたため中止し，その隔離worktreeとログを保存した．この誤起動が作った未追跡の`reportlab/`内3ファイルも，元の作業領域へは移さず隔離worktreeに残している．次のcandidateは固定check v1の過度な制限で採点対象外とした．v2で生成されたcandidateはtarget test自体はpassしたが，未使用変数によるRuff不合格であり，v3確定前のため最終比較には含めない．最終のv3試行は1件・1回の予備測定で，汎用能力やskill効果の証明ではない．

## 未統合・残存事項

既存のPi background runner`../run-background-job.ps1`はephy-workerリポジトリ外にあり，今回は変更していない．同runnerはworktree作成後すぐPiを起動し，Python／worktree source／依存／一時領域のpreflightをagent開始前に行わない．正式な`/bg`経路へ組み込むには，worktree作成直後にリポジトリ内の評価preflightを呼び，選択Python・`PYTHONPATH=<worktree>/src`・offline設定・書込可能なpytest一時領域を検証してからPiを始める変更が必要である．現時点のHEAD-based worktreeには未commitのvalidatorと固定checkは存在しないため，そこにある前提で参照してはならない．

PiのbashではPython／pytestが見つからず，agent内の自己検証は未実行だった．Qwenへの委譲とskill読込も確認できない．実験時にはcandidate patchの適用，merge，push，deployを行わなかった．
