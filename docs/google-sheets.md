# Google Sheets Read-only MVP

`ephy-worker google-sheets`は，明示的に許可したSpreadsheetだけをService Accountで読み取ります．公開Web用`Source`，`web.collect`，Manager，Coding Jobへ認証情報やセル本文を配送しません．このMVPは人間が明示的に承認したCodex実装であり，現行Markdown限定のformal runnerが自律獲得した能力や，formal gate／audit完了とは扱いません．

## 人間によるセットアップ

1. Google Cloud projectでGoogle Sheets APIを有効にし，専用Service Accountを用意します．Workspace管理者権限とDomain-wide delegationは使用しません．
2. 対象のEphy／Karteバックログ2件だけを，そのService Accountのメールアドレスへ手動でViewerとして共有します．Workerは共有権限を変更しません．Workspaceポリシーが共有を拒否した場合はここで停止します．ユーザーOAuthへの切替が必要であることを運用者へ報告し，別主体や共有変更による迂回をしません．ユーザーOAuthは今回未実装です．
3. 必要な鍵の発行と配置は人間が行います．Git checkout外のアクセスを限定したdirectoryへService Account ADC JSONを配置し，`GOOGLE_APPLICATION_CREDENTIALS`に絶対pathを設定します．Codexは鍵を発行・取得・保存しません．
4. [設定例](../configs/google-sheets.example.yaml)をGit checkout外へコピーします．`sources`の`ephy-backlog`／`karte-backlog`を実IDへ置き換え，同じ2 IDを`allowed_spreadsheet_ids`へ登録します．実ID入り設定と結果はGitへ追加しません．

ADCは明示したローカルService Accountファイルだけを受理します．通常の`gcloud auth application-default login`によるuser ADC，外部identity，cloud metadata，ambient認証主体へのfallbackは使用しません．公式`google-auth`がService Account署名とtoken交換を行い，scopeは`https://www.googleapis.com/auth/spreadsheets.readonly`の1件です．追加scopeとdelegated subjectを含む認証設定を拒否し，token endpointはGoogleの固定HTTPS URLだけを許可します．SpreadsheetのIAM／共有設定はOAuth scopeと別に必要です．

PowerShellの実行例です．以下のdirectoryは事前に人間が作成し，Git checkout外に置いてください．

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS = 'C:\private\ephy-worker\service-account.json'
uv run ephy-worker google-sheets doctor --config C:\private\ephy-worker\google-sheets.yaml
uv run ephy-worker google-sheets list-tabs --config C:\private\ephy-worker\google-sheets.yaml --source ephy-backlog
uv run ephy-worker google-sheets read --config C:\private\ephy-worker\google-sheets.yaml --source ephy-backlog --tab '人間が選んだシート名' --range 'A1:Z200' --output C:\private\ephy-worker\results\ephy-backlog.json
uv run ephy-worker google-sheets list-tabs --config C:\private\ephy-worker\google-sheets.yaml --source karte-backlog
```

`doctor`は指定source，未指定なら設定内の全sourceについてmetadata取得を試みます．allowlistを最初に検査し，認証・API接続の失敗はexit 1です．metadata一覧から人間がtabを選び，`read`で有限範囲を明示します．`list-tabs`と`doctor`はmetadata JSONだけをstdoutへ出します．`read`は結果JSONを指定fileへ保存し，stdoutにはstatus／diagnostic／request数だけを出します．セル本文をconsoleへ出しません．

## 範囲と結果

範囲はtabに相対的な大文字A1記法の単一cellまたは有限rectangleです．例は`A1`，`B3:D6`です．`A:A`，`1:200`，別tab指定，named range，`$A$1`，逆順rangeは拒否します．cell上限超過はcredential読込みとnetworkより前に拒否します．metadataでgridサイズを確認し，範囲外へのreadは拒否します．非grid sheetも一覧には含めますが，そのセル取得は拒否します．

`includeGridData=false`と限定`fields`でmetadataを取得し，その後`values.get`を`FORMATTED_VALUE`／`ROWS`で実行します．応答rangeが要求と一致しない場合は失敗です．先頭行をheader扱いせず，文字列・数値・booleanをGoogleの返却型のまま保持し，日付や数式の意味を推測しません．数式はAPIが返す表示結果を取得します．

専用`google-sheets-v1` JSONは，`source_id`，`spreadsheet_id`，`spreadsheet_title`，`sheet_id`，`sheet_title`，`range`，`source_url`，`retrieved_at`，`rows`，`status`，`diagnostic`を持ちます．`rows[].row`と`rows[].cells[].row`／`column`は実際の1始まり位置です．APIが省略した末尾の空cell／空行は要求rectangle内だけ`null`で補い，APIが明示した空文字列は空文字列のまま保持します．`limitations`へ取得範囲外が未確認であることを記録します．

セル本文は非信頼データです．結果には`content_trust: untrusted_data_do_not_execute`を付けます．シート内の命令，credential要求，URL，コードをWorkerが実行する経路はありません．JSONを後からagentへ渡す利用者も，本文を操作指示として扱わないでください．

## 予算と失敗

設定例の既定値は8 request，10,000 cell，1応答2 MiB，operation合計4 MiB，1 requestの実時間15秒，operation60秒です．token交換・metadata・values・retryは同じrequest予算を消費します．network timeoutはDNS／接続／header／slow-drip本文を含む実時間で制限します．credential fileは64 KiB以内に制限し，公式認証ライブラリの読込み前後にもoperation期限を確認します．JSON解析後，metadata・行・cellの正規化中，成功状態への確定直前にも期限を検査し，CPU処理で期限を超えた結果を成功として公開しません．

Sheets GETの429／一時的5xxのみ最大2回，0.5／1秒のbackoffでretryします．待機もoperation予算内に収めます．auth token交換は自動retryせず，401／403／404／通信障害もretryしません．redirect，環境proxy，圧縮応答を許可せず，認証headerをGoogleの固定Sheets endpoint以外へ送りません．HTTP error本文と第三者例外本文はdiagnosticに含めません．

主なdiagnosticは次のとおりです．

| code | 意味 |
| --- | --- |
| `credential_missing` | 明示したService Account ADCがない |
| `service_account_required` | user ADCなど対象外の認証方式 |
| `authentication_rejected` | 401，認証拒否 |
| `access_denied_or_hidden` | 403，権限不足または存在秘匿 |
| `not_found_or_hidden` | 404，不存在または存在秘匿 |
| `rate_limited` | 有限retry後も429 |
| `network_error` | 通信失敗 |
| `request_timeout`／`operation_timeout` | request／operation期限 |
| `cell_limit`／`request_limit`／`response_byte_limit` | 予算上限 |
| `response_range_mismatch` | 要求と応答の取得範囲不一致 |

403／404だけからファイル不存在と権限不足を断定しません．認証失敗や上限で無効な状態を成功扱いしません．API開始後の失敗では`status: failed`，`rows: []`，未取得可能性を示す`limitations`を保存します．入力・allowlist・出力pathが不正な場合は取得を開始せず，結果fileを作りません．

出力親directoryは事前作成が必要です．全Git checkout内とsymlink／junction経路，既存fileを拒否します．同一directoryの一時fileへ完全なJSONを書いてflush／fsync後，exclusive hard linkで公開します．既存fileを置換しません．hard link非対応のfilesystemでは安全な出版を代替せず失敗します．取消／例外時は一時fileを削除し，不完全なJSONを正式fileへ公開しません．SIGINT／SIGTERMはexit 130です．強制終了や停電で残った`.tmp`は正式結果ではありません．POSIXの一時fileは0600です．Windowsでは人間がdirectory ACLを限定してください．

## 検証状態とlive opt-in

mock／synthetic testsは実Google APIを使いません．複数tab，ragged／空行，日本語，位置・表示値，allowlist，認証方式，HTTP失敗，有限retry，deadline，byte上限，range，上書き／取消，本文とcredentialのconsole漏洩防止を確認します．既存Web／Coding／Managerの全回帰suiteは別途同じ環境で実行してください．

実2件の認証接続とセル内容の検証は，人間によるセットアップ後まで`live_validation_pending`です．live testは通常，理由を付けてskipします．metadataの明示opt-inは次です．

```powershell
$env:EPHY_SHEETS_LIVE = '1'
$env:EPHY_SHEETS_LIVE_CONFIG = 'C:\private\ephy-worker\google-sheets.yaml'
uv run python -m pytest -q -rs tests/test_google_sheets_live.py
```

このlive testは2 aliasのmetadataだけを確認します．各tabと取得範囲は人間が一覧を確認して選び，両バックログを`read`で別々の新規JSONへ保存して，元の表示値と行・列を照合します．実接続成功をmockから推定しません．実データ，設定，token，秘密鍵をGit／CI logへ保存しません．

Drive検索，Docs／Slides，Spreadsheet編集，共有変更，polling，永続cache，OAuth併行実装，分散認証Job，Runtime／Karte取込みは今回未実装です．
