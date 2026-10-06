# ephyへの作業票：controllerの検証同一性と環境故障の分離

## 位置付けと境界

これは承認済みの探索用proposal実験であり，正式な独立監査ではない．管理側が固定したテストを使い，gpt-ossが計画・レビュー，Qwenが実装する．稼働中controllerは管理側の元ファイルであり，編集するcandidate内の同名ファイルとは別物である．candidate版をPi extensionとして読み込んだり，runtimeへコピーしたりしない．

変更できるのは，隔離candidateの`tools/pi-local/ephy-campaign-guard.ts`だけ．テスト，作業票，契約，評価pack，policy，launcher，model設定，skill，依存関係は変更しない．shell／networkを使わない．commit，push，PR，merge，applyは禁止．

## 作業1：各checkと候補の同一性

`submit_implementation`／`runChecks`付近を確認し，次を最小差分で実装する．

- 固定checkの実行前後にcandidateを測定する．複数checkがある場合，最後だけでなく，各checkの境界で照合する．親taskのcheckも同じ扱いにする．
- patch SHA-256，変更file集合，file bytes hashを照合する．許可内の変更，許可外の変更，untracked追加のどれも見逃さない．
- 変化を検出したら`candidate_changed_during_checks`で停止し，後続check，repair，reviewへ進まない．repair回数は増やさない．成功終了でも候補不合格終了でも，改変検出を優先する．
- 「チェック後の候補」を新たな検証済み候補に置き換えてはならない．before／afterと実行結果を証拠へ残す．既存のreview時・final checks後の改変拒否も維持する．

読込む中心は`WorkspaceSnapshot`，`workspaceSnapshot`，`runChecks`，`submit_implementation`．関連しないtool，context，role，queue処理は変更しない．

## 作業2：環境・基盤故障をrepairしない

`CheckContract`，`parseCheck`，`runChecks`，失敗後のrepair分岐に，以下の固定契約を追加する．

- `candidateFailureExitCodes?: number[]`をcheck単位の任意fieldにする．省略または空配列では，非zero終了をcandidate failureと判定しない．
- 指定可能なのは10〜125の重複しない整数．0，1，2，126，127，負数，256，小数，文字列，nullは起動前に拒否し，errorにfield名を含める．通常の汎用exit 1を修正能力の判定へ流用しない．
- このfieldは固定checkerが予約したcandidate failure用exit codeだけを宣言する．今回はfixtureの10を使う．既存pytest／Ruffへ自動的にexit 1のrepair許可を付けない．実checkerの対応付け・wrapper追加は別作業である．
- **正常に実行・終了し，candidateも不変で，宣言済みcodeに一致した場合だけ**，既存上限内のrepairを許す．stdoutの文章やモデルの申告は分類に使わない．
- 未分類の非zero終了，実行ファイル欠落，spawn／exec例外，kill／timeoutは`verification_environment_failed`で証拠を保存して停止する．原因不明を含む「検証不能」の保守的な分類であり，必ず環境故障と診断できたという意味ではない．例外をtool errorのまま逃がさず，terminal stateとshutdownを残す．
- `killed=true`はexit 0や宣言済みcodeでも不合格．基盤停止でrepair回数を増やさない．候補不合格のrepair上限を使い切った場合は従来の`fixed_check_failed`を維持する．

## 固定評価と提出

管理側の`pi-campaign-repair-acceptance.mjs`は，candidateのcontrollerを一時fixture内でロードして検査する．identity group，environment group，既存campaign smokeを使用する．テストはcandidate外に固定され，外側controllerの固定commandが実行する．Qwenによる自己申告やテスト編集では合格にならない．

各小項目で計画 → Qwen実装 → `submit_implementation` → 固定検証 → gpt-ossレビューを行う．現在の項目以外へ進まない．管理側で安全なrepair経路を事前検証した外側controllerを使う．各小項目は初回＋最大2回のrepair，全体60分を固定上限とする．固定wrapperのexit 10だけをcandidate由来の失敗とし，その検証出力を受けて同じscopeで修正し，再提出する．環境故障・検証不能，改変検出，上限到達は証拠付きの人間への確認待ちで終了する．同一試行の契約を変更して続行しない．この上限は，candidate controllerのrepair挙動をfixtureで検査することとは別である．

runtimeの安全性修正は管理側の成果であり，Qwenの成果に数えない．candidateは旧評価時の元controller bytesから始める場合があり，そのhashをmanifestへ明記する．旧失敗candidateを採用・再利用したり，runtimeの完成コードをcandidateへコピーしたりしない．モデルが`action=stop`を要求した場合は，人間の中断指示とは区別して確認待ちとする．ユーザーがPiへ直接`EPHY_STOP`と入力した場合だけ明示的な中断として記録する．

最終提出では，変更箇所，固定検証結果，未解決事項を記載し，未適用proposalで停止する．同一sessionのレビューを正式な独立監査と呼ばない．採用には管理側の再検証と別セッション／PRの独立レビューが必要である．

## 今回は行わないこと

全体期限の外側watchdog，checker／probeの本体実行経路の固定，Windows既存4件のtest failure，model変更，skill追加，自然言語の自動分解，継続運用・一晩放置は対象外．監督下で実行し，反応が止まった場合は手動停止して証拠を管理側へ渡す．
