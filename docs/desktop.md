# Ephy Worker Console

Windows 向けの 3 ペイン GUI です．入力，実行統計，生成テキストとイベントを同じ画面で確認します．Python 標準の Tk を使い，既存の `CodingExecutor` と `PiRpcRunner` を起動します．モデル API へ直接質問するチャットではありません．

この GUI の実タスクは，指定した Git revision の隔離 worktree を読む `planner` に限定します．Pi に公開する tool は `read` と `governance_ack` だけで，同梱の `navigation_guard.ts` が読み取り範囲を検査します．任意 shell，コード編集，任意の Pi 起動引数，merge，Strata の起動・停止は提供しません．通常の coding CLI や formal self-improvement workflow を置き換えるものではありません．

## 起動

配布された `EphyWorkerConsole` フォルダー全体を展開してください．`EphyWorkerConsole.exe` だけを移動せず，隣の `_internal` を残します．Python は配布物に含まれます．実タスクには別途 Git，Pi，利用者が接続先を確認したローカル Strata が必要です．画面の build ID と同梱の `build-info.json` で配布版を確認できます．

合成デモはモデル呼出し不要です．固定 fixture を同じ `CodingExecutor` に通し，合成イベントを表示します．入力欄を操作できますが，生成内容は fixture であり，入力に対する実モデルの回答ではありません．

```powershell
.\EphyWorkerConsole.exe --demo
```

実接続用には同梱の `desktop.example.json` を Git 管理外の利用者専用フォルダーへコピーし，実際の path，model ID，endpoint に置き換えます．provider は `strata-local` を使います．例のままでは実行できません．

```powershell
.\EphyWorkerConsole.exe --config 'C:\path\to\private\desktop.json'
```

ソースからは，依存を準備した Python で同じ入口を使います．

```powershell
python -m ephy_worker.desktop.app --demo
python -m ephy_worker.desktop.app --config 'C:\path\to\private\desktop.json'
```

## 設定

JSON の未知のキーは拒否します．相対 path は設定ファイルの所在を基準に解決します．設定に秘密情報を直接記述せず，実行記録とともに公開物へ含めないでください．

| キー | 内容 |
|---|---|
| `repository_path` | 読み取り対象の既存 Git repository．必須 |
| `base_revision` | 読む revision．既定は `HEAD`．未コミット変更を自動では取り込まない |
| `pi_executable` | Pi 実行ファイル．既定は `pi` |
| `provider`／`model_id` | provider は `strata-local`．model ID は接続先で確認した実際の値 |
| `endpoint` | ローカル Strata の `http://127.0.0.1:<port>/v1`．認証情報や query を含めない |
| `context_window` | 接続先モデルで確認した context の上限 |
| `timeout_seconds` | 実タスクの制限秒数．省略時は 240．1〜1200 の JSON 整数のみ |
| `artifact_root` | Git 管理外の保存先．省略時は `%LOCALAPPDATA%\EphyWorkerConsole\runs` |
| `models_file` | `null` または省略のみ．provider は固定の同梱正本で登録し，利用者の model 定義を読み込まない |
| `karte_binary`／`karte_root` | opt-in の Karte worklog CLI と保存先．両方を指定したときだけ連携する |

実タスクの上限は model request 8 回，総出力 8192 token，1 応答 2048 token，retry 0 回です．wall time は `timeout_seconds` で設定し，同じ秒数を worker と governance stage の制限へ渡します．初回の governance 確認も予算に含みます．GUI は独立した Pi agent directory を使い，通常の extension／skill／prompt／theme／context 自動探索を無効にします．固定の内部 extension を明示して読み込み，共有 Pi 設定や既存の Strata profile を更新しません．

例えば利用者専用 JSON に `"timeout_seconds": 600` を指定し，既存の `--config` で読み込みます．`0`，負数，`1201` 以上，`600.0`，`"600"`，`true`，`null` などは拒否し，省略時だけ 240 秒を使います．接続設定欄に有効な秒数と上限 1200 秒を表示します．読込みに失敗すると以前の設定を解除し，実タスクを開始できません．合成デモの制限は別の固定 300 秒で，読み込んだ実接続設定の値は使いません．

### Governance の実行契約

同梱する正本の `strata-provider`，`formal-stage-guard`，GUI の `navigation_guard`，正本の `governance-gate` の順に extension を読み込みます．gate は最後に置き，正本を変更せず使います．provider は request 前に Strata の health と model ID／context の一致を確認します．

最初の model request は policy と固定契約に対する `governance_ack` のためのものです．元の入力タスクは ack を確認した後に送ります．ホストは最終 RPC tool result の ack と実行 context を検査し，ack 後の assistant 本文，停止・拒否・trace 違反がないこと，開始前後の正本 hash 一致を確認します．ack が欠落する場合や，gate の失敗 marker，hard stop，禁止 tool などを観測した場合は成功扱いにしません．画面に生成本文が出たことだけで契約成立とは判定しません．

policy，planner／監査契約，provider／guard／stop／gate の正本 10 ファイルを `governance_runtime` に元 repository の相対 path を保って収録します．`manifest.json` の path・byte 数・SHA-256 とコードに固定した正本 hash を照合し，run ごとのコピーも検査します．この限定的な planner 実行と確認処理は，正式な自己改善 Job の実装者・監査者分離や採用手順の成立を意味しません．

### 信頼する設定と実行ファイル

実行には，利用者が確認した設定 JSON，Git／Pi／Karte の実行ファイル，対象 repository とそのローカル Git 設定を使ってください．`PATH` から選ばれる実行ファイルもこの前提に含みます．この JSON は第三者から届いた設定を安全評価して実行する形式ではありません．キーや URL の検査だけで，指定された実行ファイル・repository の安全性を保証しません．

読み取り専用 guard は Pi のモデルに公開する tool の境界です．既存 worker と同様，Git の worktree 作成や状態確認では，元 repository の `post-checkout` hook，`core.fsmonitor` などの Git helper が guard の外で動く場合があります．GUI はこれらを無効化する OS sandbox ではありません．対象 repository のローカル Git 設定・hook と，実行ファイルを事前に確認したうえで利用してください．

### Pi の起動前検査

通常の `.pi` ディレクトリと，その中の通常ファイルは許可します．選択 revision の worktree から `.pi` までの path と，`.pi` 内の全項目をホスト側で検査し，symbolic link，junction を含む reparse point，複数 hardlink を持つ通常ファイル，特殊ファイル，探索エラーがある場合は Pi を起動しません．Unicode 正規化で変わる root や，`.pi` の探索上限 10000 項目を超える場合も拒否します．`.pi` 自体が存在してディレクトリでない場合も拒否します．`.pi/commands` は通常のディレクトリ・ファイル・リンク・切れたリンクを含め，どの種類でも存在すれば拒否します．

Pi の project 設定移行は extension guard より前に `.pi/commands` を rename する場合があるため，この移行を起動前に防ぎます．通常の `.pi/prompts`・`.pi/extensions` などの自動読込みは，固定した起動 flag と独立した空の trust 設定で無効にします．この移行と起動 flag の挙動を確認した対象は Pi 0.86.1 です．別の Pi version・実行ファイルの同等性は未検証であり，信頼できる実行ファイルを選ぶ必要があります．既存 repository の `.pi` は保持し，検査を通る revision を選択してください．前述の Git hook・ローカル Git 設定の信頼前提も必要です．

## 操作と状態

1. 合成デモか実接続かを画面で確認し，入力欄に調べたい内容を書きます．
2. 開始すると `starting` を経て `running` になります．同じ画面で実行中の二重送信を拒否します．
3. 生成本文とイベントを随時表示します．切断後の再接続は同じ run の表示を復帰する操作であり，入力を再送しません．
4. 取消は実行中の worker／Pi に要求します．取消要求中と完了・失敗・取消済みを区別し，ウィンドウを閉じる際も実行を終える処理を行います．

Pi stream の切断は成功扱いにせず，失敗状態と理由を表示します．共有推論サービスが実際に計算を停止したことは GUI の取消表示だけでは確認できません．新しい実行は新しい run であり，中断した生成の途中再開ではありません．

Windows では，起動した Pi とその子孫を専用 Job Object で管理します．Pi を一時停止状態で作成し，所有境界へ登録してから実行を進めます．登録できなければ prompt を送信せず失敗とします．通常完了，取消，timeout，RPC 異常のいずれも所有する process tree を回収し，process 終了と pipe の読取り待ちにも上限を設けます．回収を確認できない場合は完了扱いにしません．この境界は `PiRpcRunner` が起動する Pi の tree に適用します．共有 Strata はこの tree に含まれず，クライアントを取り消した後にサーバ側の計算が停止したという保証はありません．

同時実行の制限は 1 GUI instance あたり 1 run です．別の GUI を起動すると，個別の worktree と Pi 設定で別 run を並行して実行できます．共有するモデルサービスへの全体的な排他・queue・負荷制御はありません．同じ共有モデルへ複数 GUI から送る場合は，モデルの処理能力を共有します．同一 GUI の二重送信拒否や表示の再接続は，別 GUI のタスク投入を制限する機能ではありません．

統計は取得できた値だけを表示し，未取得は `—` または未取得の表示を残します．ホスト CPU／RAM は `psutil`，ホストの NVIDIA GPU／VRAM は利用可能な場合だけ固定の `nvidia-smi` query から取得します．GPU の値は device 全体であり，worker による占有量ではありません．取得不能を 0 に置き換えません．

process CPU／RSS は起動した Pi child だけを対象とし，GUI，Pi の child，Strata の process を合算しません．process CPU は 1 logical core を 100% とするため 100% を超える場合があります．PID と開始時刻を確認し，終了済み・再利用された PID を監視し続けません．token 数は確定 assistant message の usage を集計します．token/秒は provider が `timings.predicted_per_second` を返した場合だけ表示し，token 数を総経過時間で割って推定しません．標準 Pi の usage だけでは速度は未取得です．経過時間は送信受付から cleanup 完了までの単調増加時計で測り，完了後は固定します．純粋なモデル生成時間とは異なります．

## Karte と保存

Karte は observer CLI による任意連携です．記録の出所は `host_observation` で，GUI ホストが観測した開始・終了を記録します．モデル自身の報告や全 tool 履歴の記録ではありません．`off`（未設定），`unsaved`（未保存／保存未確認），`saved`（保存済み），`saved_views_pending`（原本保存済み／閲覧用生成待ち），`failed`（保存失敗）と待機件数を区別します．`recorded=true` かつ `views_ready=false` は保存失敗と混同しません．終了 append は `outcome` を使います．worker の処理成功だけでは人による確認・検証完了を宣言せず，Karte の終了 outcome は `paused` とします．プロンプト，tool 引数，生成本文を Karte の observer 記録へ自動転記しません．常用環境への登録・適用済みを意味する表示ではありません．

observer event は送信前にローカルの送信待ち queue へ永続化します．queue 保存用の短い lock と CLI 配送用の lease を分け，別 GUI が送信中でも event を保存します．配送 lease の競合時は `unsaved`／`delivery_busy` と待機件数を返し，保存した event を破棄しません．再送は同じ event ID と入力を保ちます．queue の容量上限は 4096 件／5 MiB であり，容量不足や disk 保存失敗は保存成功として扱いません．

別 GUI が配送を終えても，この GUI が当該 event の保存 receipt を確認していない場合は，`unsaved`／`record_ack_unavailable`（保存未確認）を返します．他の process の保存結果を推測して `saved` と表示しません．

自動 flush は，Karte 連携を設定した実 run の開始・終了の `record` 時だけです．1 回の flush は最大 32 件，CLI 1 回の timeout は 5 秒です．未保存 event や閲覧用生成待ちを保持しても，idle 中，表示の再接続，GUI の起動だけでは自動再送しません．flush を完了するまで無制限に反復する機能や，GUI 専用の再送ボタンもありません．後続の実 run の開始・終了で再び配送を試みます．閉じる際は run の終了処理を待ちますが，すべての待機 event の保存完了を保証するものではありません．合成デモは設定済みの Karte にも接続しません．

worker のローカル artifact には，実行内容，ログ，生成結果などが含まれ得ます．保存先は利用者専用の Git 管理外フォルダーにしてください．画面や evidence を共有する場合も，実タスクの入力・本文・path を事前に確認します．配布用 screenshot は合成デモで作ります．

## Windows ビルド

Python 3.12 と Tcl/Tk を持つ Windows 環境でビルドします．PyInstaller は **6.16.0** に固定します．既存 worker の仮想環境を変更せず，別の build 用仮想環境へ PyInstaller を用意し，`uv.lock` に一致する worker 依存を使ってください．ビルドスクリプト自体は download や install を行いません．

以下の path は例です．`OutputRoot` は source checkout の外に置きます．別の読み取り専用 `site-packages` を利用する場合だけ `DependencyPath` を指定します．

```powershell
.\scripts\build_desktop.ps1 `
  -Python 'C:\path\to\isolated-build-venv\Scripts\python.exe' `
  -DependencyPath 'C:\path\to\worker-venv\Lib\site-packages' `
  -OutputRoot 'C:\path\to\artifacts'
```

スクリプトは既存出力を削除・上書きせず，新しい build ID の下に次を作ります．

| path | 内容 |
|---|---|
| `dist/EphyWorkerConsole/` | フォルダー全体を配布する Windows アプリ |
| `dist/EphyWorkerConsole/build-info.json` | app version，build ID，Git revision，変更有無，Python／PyInstaller version，source／governance manifest hash |
| `dist/EphyWorkerConsole/_internal/ephy_worker/desktop/governance_runtime/` | dot directory を含む正本 10 ファイルと `manifest.json` |
| `evidence/source-manifest.json` | 未追跡の GUI source，guard，正本 bundle と manifest，配布説明を含むビルド入力の相対 path，byte 数，SHA-256 |
| `evidence/governance-runtime-manifest.json` | 同梱正本 10 ファイルの相対 path，byte 数，SHA-256 |
| `evidence/artifact-manifest.json` | 配布ファイルの相対 path，byte 数，SHA-256 |
| `evidence/dependency-manifest.json` | ビルド時に選択した環境の distribution 名と version．ローカル path は含めない |
| `evidence/pyinstaller.log` | ビルド出力．環境の絶対 path を含むため公開前に確認する |

EXE の Windows file version は `pyproject.toml` の version と一致します．`build-info.json` は build ID と source identity を補います．これは入力と生成物を追跡する仕組みであり，異なる環境で EXE の bytes が同一になる保証ではありません．ビルド前後の source bundle と，ビルド後の `_internal` にある bundle を同じ固定 hash 検査へ通します．manifest の書換えだけで別の正本を承認しません．ビルド中に既存入力が変更された場合は失敗し，新しい build ID で再ビルドします．

## 検証の区別

配布物の起動・合成描画を確認する入口です．実モデルや通常 Karte の接続試験にはなりません．

```powershell
.\EphyWorkerConsole.exe --demo `
  --smoke-test 'C:\path\to\evidence\desktop-smoke.json' `
  --screenshot 'C:\path\to\evidence\desktop-demo.png'
```

ソースの状態遷移試験は `tests/test_desktop*.py`，既存 executor との整合は `tests/test_coding_executor.py` が対象です．二重送信，取消，切断・再接続，stream 中断，閉じる動作を確認します．実際の実施結果は配布時の evidence と照合してください．未実行の実 Pi／モデル接続，GPU 取得，Karte 保存を合成試験の成功から推定しません．

この GUI のビルド・テストは，formal self-improvement の指定モデル実装・独立監査，`review_ready`，採用，merge の成立を宣言するものではありません．
