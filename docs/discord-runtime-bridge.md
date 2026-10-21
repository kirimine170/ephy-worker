# DiscordからRuntimeへのtransport

`discord-runtime`はopt-inの独立したDiscord入口です．Discordの受信と返信だけを担当し，検索計画，許可，固定Workerへの収集Job，根拠の抽出と最終回答はRuntimeへ委譲します．既存のresearch CLIや稼働中Botの設定は変更しません．この実装の追加だけでは本番接続を開始しません．

## 準備と実行

```text
uv sync --locked --extra discord
ephy-worker discord-runtime check --config /absolute/private/discord-runtime.toml
ephy-worker discord-runtime run --config /absolute/private/discord-runtime.toml --connect
```

[設定例](../configs/discord-runtime.example.toml)をGit外へコピーし，application／guild／channel／user IDと絶対`state_dir`を指定します．空の許可リスト，未知の設定field，Git内またはlink経由のstate directoryは拒否します．tokenは`token_env`で指定した環境変数だけから読みます．設定や成果物へtoken本体を書きません．`check`は設定の構造とpathを確認するだけで，Discord，Runtime，モデル，検索へ接続せず，tokenも読みません．`run`は`--connect`を必須にします．

Runtime URLはliteralのloopback IPだけを許可します．環境proxyとredirectは利用しません．Manager URL，収集Worker ID，providerとcredentialはRuntime側の設定です．Discord本文からそれらを指定・変更できません．Runtime側はWorker providerの設定とManager／収集Workerを別途起動してください．既存のManager／collector設定例とRuntimeのbridge文書を利用します．

通常のbot mentionにはMessage Content Intentは不要です．mentionを含まないBotへの直接返信を読む場合は，`read_message_content=true`とし，Discord Developer PortalでもMessage Content Intentを有効にします．この権限がない場合はmentionで質問してください．Bot／webhook／system／everyone mention，許可外のguild／channel／userは処理しません．

## 会話と許可の境界

受理後すぐに「分かりました，調べてみますね．」と返信し，120秒たっても終わらなければ「もう少し調べるのに時間がかかりそうです．」を1回返信します．最終回答まで利用者の続行操作は不要です．一人称を「わたし」とし，事実と意見を分けて意見を添え，不確実性を明記するsystem messageをRuntimeへ渡します．自然言語の品質はRuntime側のモデルにも依存します．

まず`POST /v1/web-search/plan`へ公開の質問を送り，`allow`の場合は`web_search=true`と同じplan IDを付けてchatを開始します．`auto_approve_search`は既定で`false`であり，`confirm`は検索していないことを明示して終了します．信頼したlocal設定で`auto_approve_search=true`を明示した場合だけ，検証済みplan IDを`POST /v1/web-search/approve`へ送り，HTTP200，`status=approved`，同じplan IDを確認してからchatを開始します．承認に失敗した場合や承認responseが不正な場合はchatも取消も送りません．`block`は設定にかかわらず拒否し，承認もchatも送りません．この設定は厳密なbooleanであり，起動後やDiscord本文から変更できません．Runtimeのglobalな許可policyは変更しません．chatには`Prefer: respond-async`を明示してHTTP202への対応を通知し，202の場合は同じpayload／plan ID／headerを繰り返して再開します．このheaderを送らない既存のRuntime clientは通常のcompletion／SSE接続を保ったまま待機します．pending／finalのJobとplanの不一致，失敗，接続断，未完のstreamを成功にはしません．

最終生成はSSEです．`delta.content`だけを集め，`reasoning_content`やroute／source eventを会話本文へ混ぜません．完了reasonと`[DONE]`を確認してから最終回答を送ります．thinking tagを除去し，不完全な内部出力を拒否します．streamには600秒のread idle timeoutがあり，活動が続く生成に経過時間の締切は設けません．検索のpending回数，stream byte数，回答長は別に制限します．意味上の反復を判定するモデルやloop watchdogは追加していません．

最終回答は，受信時の最大32768文字と秘密情報・内部出力・制御文字の検査を維持したまま，全体をsanitizeします．1800 UTF-16単位を超える回答も末尾を省略せず，段落，改行，文末を優先して分割し，順番に全て送ります．絵文字の途中では分割せず，通常のMarkdown code fence内で分割するときは閉じる行と再開する行を補います．開始・終了の区切り行が上限に収まらない場合や，継続用の補助行によって本文に上限の半分（通常900 UTF-16単位）を確保できない長いfenceは，元の文字列として分割し，全ての本文を保持します．補助行の繰り返しによって送信数が膨らむことを防ぎます．それ以外の本文は保持します．モデルには必要な事実や説明を残し，可能なら1800文字以内で簡潔に答えるよう促します．

直接返信の参照先は，このtransportが同じguild／channel／userへ送った最終回答の各messageです．送信に成功してIDを受け取った部分を，それぞれ独立した返信先として記録します．過去回答は各部分につき最大1200 UTF-16単位，24時間以内，全体で最大128件を保持し，既知のcredentialをredactします．全回答をDiscordへ届ける範囲と，後続の質問へ渡す参照contextの保持範囲は別です．新しい質問だけを従来の厳格な公開text検査（最大4000文字）へ通してから，planの`query`へそのまま指定します．過去回答は別のoptional `context`として渡し，chatでもassistantの参照用データとして保持します．chatの最新user本文は新しい質問そのものです．過去回答がない場合は従来どおり`query`だけを送ります．

既に公開した回答のcode fenceや公開連絡先・pathの例を，新しい質問の個人情報として再判定しません．Runtimeは過去回答を命令や権限として扱わず，必要なredaction後にlocalな検索語書換えの参考だけに使います．書換え後の検索語はRuntimeの同じprivacy／許可検査を必須とし，Managerへ送るのは許可された検索語だけです．生の過去回答をManagerへ渡しません．過去回答によって承認権限は変わらず，`confirm`の自動承認には前述の明示設定が必要であり，`block`は常に拒否します．Discordの履歴取得や参照先のfetch，返信先の再帰探索はしません．

## 状態と失敗

許可ユーザーの連続メッセージは，共有する永続FIFOキューへ受信順に保存します．待機中も受付と順番待ちの通知を返し，処理開始時に受付通知を繰り返しません．待機件数は最大128件です．挿入前に，既存request IDと未実行の待機依頼を合わせて10000件以内に収まることも確認します．満杯の場合は新規受理を断り，待機中の依頼を削除しません．

BridgeLedgerのSQLite inboxには，復旧に必要な待機中の質問本文と返信参照用の過去回答をlocalに保存します．再起動後，Runtimeへの実行開始を記録していない依頼はFIFOで再開します．HTTPより前にmessage IDを確保した依頼は，処理が途中で止まると実行結果が不明なため自動再実行せず，結果を確認できない旨を通知します．最終回答は最初のchunkの送信前に配送終了を記録し，返信の成否不明や部分送信も自動再送しません．

終了したinboxの質問本文と過去回答は空にし，重複判定用のIDは直近10000件まで保持します．既存の終了行も起動時に本文を空にします．実行開始を記録するrequest ID台帳は最大10000件を保持し，上限で新規受理を停止します．この台帳の古いIDをpruneして再実行を許すことはありません．stateを削除すると重複防止の記録を失います．tokenはledgerへ保存しません．private stateはGit外の利用者専用local directoryへ置きます．POSIXでは新規state directoryを0700で作成し，SQLiteを0600とします．Windowsでは親directoryのACLを継承するため，保存先のACLを利用者に限定します．

分割した最終回答の途中で送信に失敗した場合も，同じrequestを自動再実行・再送しません．それまでに送信・記録できた部分への直接返信は参照できますが，未送信の残りを配信済みとは扱いません．

会話・修正依頼の分類は，許可ユーザー全員で共有するキューから1件ずつ順番に処理します．取消時はローカルtaskとHTTP接続を解放し，Runtimeへ取消をbest effortで送ります．検証済みの`allow` planまたは明示設定による承認を検証済みの`confirm` planでpending回数の上限に達した場合や，chatの通信・protocol・回答検査が失敗した場合も，同じplanの取消を1回試みてから元の失敗を返します．取消requestが失敗しても元の失敗理由を置き換えず，新しいplanやJobを作り直しません．正常完了や未承認・不正なplanには取消を送りません．遠隔Workerの停止完了をtransportだけで保証するものではありません．失敗は固定した短い説明で返し，例外本文やHTTP response本文をlogへ出しません．全てのDiscord返信で`allowed_mentions=none`とembed抑制を指定し，UTF-16で1800単位以内に制限します．

offline testsはSDKの有無によらず許可，pending再開，SSE，重複，返信contextと失敗を確認します．実Discord投稿や本番Bot restartはtestに含めません．
## 所有者からの自然文による修正依頼

`owner_repair_config`を省略した場合，既存の調査・会話だけを実行します．有効にする場合は，Discord設定へ次を追加します．

```toml
owner_repair_config = "E:/ephy-work/discord-launch/owner-repair.json"
```

実際のDiscord author IDが`425581535748423680`の場合だけ，Runtimeのローカル会話APIで自然文を分類します．接頭辞は不要です．製品の実装・動作・設定の修正と，通常の質問や回答内容の訂正を区別します．分類結果は`{"action":"repair"}`または`{"action":"conversation"}`の厳密なJSONだけを受理します．表示名，引用された会話，model出力は権限を付与しません．他の許可ユーザーは従来の調査・会話を利用します．分類失敗時は修正も外部検索も実行しません．修正の分類を公開検索の検査より先に行い，私的なコード修正依頼を外部の検索語へ変換しません．

controllerはBotと別processで起動します．`state_dir`にSQLiteの依頼，stage，結果，送信outboxを保持し，Bot／Runtimeを再起動しても依頼を失いません．実行中のmodel stageでcontrollerが落ちた場合は，証拠を残して停止します．同じDiscord message IDの依頼を勝手に再実装しません．送信開始後の通信切断は` sending `状態を保存し，重複送信を避けるため自動再送しません．この曖昧な状態の配送確認は運用側で行います．

controller設定の例です．pathとSHA-256は運用者が配置した実在ファイルへ置き換えます．Discord本文やmodel出力からこの設定，repository，command，remoteを作成しません．

```json
{
  "state_dir": "E:/ephy-work/discord-launch/data/bridge/owner-repair",
  "guild_id": 1539224877209485392,
  "channel_ids": [1560630456070967386],
  "python": "E:/ephy-work/discord-chat-20261016/.venv/Scripts/python.exe",
  "git": "C:/Users/ephy/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/git/cmd/git.exe",
  "gh": "C:/Program Files/GitHub CLI/gh.exe",
  "powershell": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
  "release_adapter": "E:/ephy-work/discord-launch/Invoke-OwnerRepairRelease.ps1",
  "release_adapter_sha256": "<64 hexadecimal characters>",
  "poll_seconds": 5,
  "repositories": {
    "ephy-worker": {
      "source": "E:/ephy-work/worker-production-20261019",
      "production": "E:/ephy-work/worker-production-20261019",
      "origin": "https://github.com/kirimine170/ephy-worker.git",
      "github_repository": "kirimine170/ephy-worker",
      "branch": "main",
      "allowed_prefixes": ["src/", "tests/", "docs/"],
      "required_checks": ["<actual required GitHub Actions check name>"],
      "formal_template": "E:/ephy-work/discord-launch/owner-worker-template.json",
      "formal_template_sha256": "<64 hexadecimal characters>"
    }
  }
}
```

`ephy-runtime`も同じ形式で登録できます．許可remoteは`kirimine170/ephy-worker`と`kirimine170/ephy-runtime`のHTTPS remoteに固定します．Runtimeでは必要に応じて`packages/`と`apps/`をprefixへ指定します．controller，governance，検証・採用gate，秘密情報，隠し設定は自動修正scopeへ含めません．これらを変更する依頼は通常の自律修正Jobへ流さず，別の運用scopeとして扱います．

`formal_template`は運用者が固定した`{"runtime":{...},"contract":{...}}`です．runtimeには既存modelのidentity，managed Pi，provider，governance gate，worker agent，resource lockなど正式runnerの全設定を含めます．contractには予算，5本の固定argv検証，runtime pinsと明示的な実行profile／sandbox descriptorを含めます．discoveryはこのcommandを変更しません．scopeと受入checkerの提案を読み取り専用のlocal model呼出しで作り，known_good／unchanged／wrong_answer／test_weakening／scope_escapeの5つの実測controlを通してから契約をfreezeします．checkerはcandidateと別の読み取り専用領域に置き，実行は選択済みのnative sandboxだけを使います．sandboxの保証範囲と観測されたloopback制約は[実行profile文書](discord-owner-repair-execution.md)で確認します．

scope選択は，既存local modelへ厳密なJSON Schemaで「repository／allowed_files／semantic_scope」または「read_moreだけ」の二つの形を指定します．不正な形，既存test不足，追加codeの確認待ちには，前回の実出力（feedbackは最大8192 UTF-8 byte），不足内容と既存test候補を返し，元の依頼を維持して再試行します．暫定上限は30回であり，実測で妥当性を確立した値ではありません．各回の出力，失敗理由，選択結果と回数はprivate stateへ保存します．受理済みの追加codeは保持し，byte予算が満杯なら自動追加を止めます．実際のcode確認，許可scope，既存test，sourceと権限の検査は回数を増やしても省略しません．上限到達は成功扱いにせず失敗を通知します．

discoveryは既存file全体をcontrolの出力として再生成させず，scope内のpathごとに`[{"old":"完全一致する一意な部分文字列","new":"置換後"}]`または`[{"append":"追加test"}]`という小さな編集を受け取ります．controllerが元のsourceへ展開し，曖昧な一致，scope外，未知fieldを拒否します．既存のassertionを残した回帰testの追加を必須にします．選択したmodelの`apply-template`と`tokenize`で実際の入力token数を測り，context上限からその入力とtemplate差分用の64 tokenを引いた残りを出力へ割り当てます．4096 tokenは固定の生成上限ではなく，生成開始に必要な最小空き容量です．残りが4096 token未満なら生成前に停止し，入力を切り詰めて続行しません．出力が実際のcontext上限まで続いて不完全になった場合も，JSONや生成完了条件を緩めて受理せず，生のresponseと失敗をprivateに保持します．

checker／controlの提案も暫定30回まで修正を試みます．各回を`control-attempts/attempt-NN`へ分け，元の提案，checker，5本の実測結果を保持します．model由来のJSON・構文・編集・control結果不一致だけを，前回の提案（最大8192 UTF-8 byte）と実際のexit／stderr末尾などを次の回へ渡して修正します．既定の閉じたDSLでは，追加testの注釈やdecoratorは禁止のままです．native profileの許可範囲は以下に記載します．受入checkerは単体scriptとしてassertionを実行する必要があり，呼び出されないpytest関数を定義するだけではbaseline拒否を満たしません．5本すべての期待結果が揃うまで契約をfreezeしません．環境・model identity・native capability・実行証跡の異常はmodel修正へ流さず停止します．成功した回のcheckerと依存fileを最終契約へpinし，`frozen-spec.json`は従来どおり依頼の実行directory直下へ保存します．

checker提案がJSON・構文・編集検査で失敗して実測へ進めなかった場合も，直近の5本の実測診断は元のattempt番号とchecker hashを付けた過去の助言として保持します．今回の結果と混同せず，成功には今回のcheckerによる5本すべての実測を要求します．失敗したchecker本文がbyte単位で同じまま5回続いた場合は，停止せず，次のpromptで大きな前回提案本文の再掲を止め，実測結果から実際のcandidate APIを使った別の検証方針を促します．元の依頼とsourceは維持し，省いた提案の全文・byte数・hashはprivate artifactへ残します．checkerが変わるか有効な本文がなくなれば連続回数をリセットします．5回は今回の実測から置いた暫定の方針転換値であり，全体の提案上限30回とは別です．この回復実験の成立だけで，自律的な機能実装が最後まで成功したとは扱いません．

運用者が明示した`native_windows_code_v1`では，既存のdescriptor検証とcodex／python／ACL helper／capabilityの4つのpin一致を確認してから，そのdescriptorとcapabilityのhashを読み取り専用checker guardへ固定します．この場合だけ，追加testの通常のsync／async本文，既存fixture引数，本文内import，class・method・filesystem呼出しを許可します．Path／Noneなどの不活性な注釈と，既存pytestの裸のasyncio markerを扱えます．元のtest本文・rawの各行とその順序・AST，scope，新規module／plugin制限は維持し，追加top-level実行文・import・fixture／hook定義，任意decorator・default・実行する注釈は拒否します．それ以外のprofileは従来の閉じたDSLのままです．model／candidateはこの許可を選べず，実行は同じnative隔離を必須とし，host実行へ切り替えません．この本文検査を汎用Python sandboxとは見なしません．

native profileのtest追加は，末尾へのappendだけでなく，元のtop-level文の間への新しいtest／helper関数の挿入を扱います．guardとmodelが従来から使う`read_text`の改行正規化後の表現で，元の各行を同じ内容・順序で残し，元のASTも同じ順序で残ることを確認します．削除・置換・並べ替えや任意のtop-level実行文は許可しません．新たな物理的CRLF／LFの一致条件は追加しません．元のASTノードに対応する関数・class・decoratorのsource範囲は，本文内や末尾の字下げcommentを含めて同じ内容で保ちます．ノード間の元の空白・commentも対応する区間内で保持し，追加関数の本文にある同じ行で代用しません．実行対象は元の関数名にないすべての新しい`test_`関数から選ぶため，途中への挿入もpytestのcase結果検査を受けます．既定DSLは従来どおりAST prefixを保つ末尾追加だけです．初回と修正時のpromptには，選択済みの既存testから得たimport文とsource pathを短い助言として渡し，`packages.karte_core`のような実際のimport表記を参照させます．sourceや依頼の代用，import fallbackやpath変更には使わず，実行成功の証明とも扱いません．

元の先頭ASTノードより前への関数挿入は拒否し，module docstringが先頭から移動して`__doc__`が変わることを防ぎます．docstringやimportなどの元のprologueを保った後の挿入は，従来どおり扱います．

nativeの挿入位置は，元の最後の非関数top-level文と，定義時の評価が不活性だと確認できない元の関数headerより後に限定します．これにより，追加関数名が既存の代入やdefault／decorator／即時annotation評価へ先に見えることを防ぎます．literal定数のdefault，既存の不活性annotation，裸のpytest asyncio markerを扱い，future annotationsは遅延評価として扱います．その境界より後の関数間挿入は維持し，任意のruntime意味同値性や同一process内の証跡偽装の解決は保証しません．

native profileでは，静的guardと意味的な受入checkerが通った後，元のtest ASTとの差分から追加されたtop-levelの`test_`関数だけをcontrollerが指定し，既存のpytestで実行します．収集された全caseにsetup／call／teardownのpassedが必要で，skip・xfail・失敗・deselection・対象の欠落は成功にしません．pytest本体と実行関数，既存TEMPのpathをcandidate import／受入checkerより先に保持し，書込みはそのTEMP配下の一時directoryだけへ向け，candidate／checkerへcacheを作りません．このchecker内では画面capture pluginを無効にし，stdout／stderrは外側のnative runnerで記録します．そのためcapture plugin由来のcapsys／capfd fixtureはこの追加test検査では利用できません．元の全suiteはこのchecker内で再実行せず，別の登録済み検査に任せます．追加testがないunchanged controlでは，従来どおり同じ意味的checkerだけでbaselineを判定します．この実行結果検査は動的なskip表記も扱いますが，同一process内の悪意あるPythonによる証跡偽装が不可能だとは保証せず，既知のcompletion-record偽装の制約を解消したとは扱いません．

checkerへ渡す`argv[1]`は読み取り専用のcandidate source／import rootであり，checker自身のdirectoryも読み取り専用です．protocolやtestの書き込みデータは，既存の`TEMP`／`TMP`／`TMPDIR`に従う`tempfile.TemporaryDirectory()`へ置くよう，初回と修正feedbackの両方で指定します．candidate直下の`.mdsys`は書き込み先にしません．実際のcandidate APIを呼んだ結果を同じassertionで比較し，known_goodは実装修正と回帰testを含むよう指示します．自己生成したJSONだけを検査しても機能の証明にはならず，この指示だけで意味的な妥当性を機械的に証明したとは扱いません．追加testのskip／xfail禁止は，直接属性に加え，pytestの別名import・importorskip・unittestのSkipTestなどの明示的な表記も拒否します．通常のnative test本文と既定DSLは維持し，汎用Python安全性の保証には読み替えません．

scope選択の再試行対象は，完了したmodel本文の厳密JSON解析エラーと，明示したscope／read_more検証エラーだけです．modelのfield型は選択エラーとして明示検査し，環境設定・endpoint protocol・source読込の一般的なValueError／TypeErrorや未知の失敗を30回の再試行へ変換しません．元の原因を保持して停止します．skip禁止にはunittestの`TestCase.skipTest()`も含みます．

固定checkerのargvは`["{python}","-I","{checker}","{candidate}"]`とします．`-I`でcandidate由来の`ast.py`などによる標準libraryの乗っ取りを防ぎ，scope／assertion／authority guardを通過した後だけcandidateのimport pathを追加します．control実行も同じargvを使います．

依存関係の異なるrepositoryには，各templateの`runtime.python`へ対応する検証用Pythonを固定します．契約のverifier identity freeze，formal runner，採用直前のproposal再検証は，その実際のPythonを使った別processで実行します．現在のcontrollerのPythonを別の検証環境だと申告しません．childのimport pathは固定Worker governance rootの`src`だけとし，native Gitのpathを固定してcredential環境を除去します．Runtimeのcontrolと受入checkerには対象candidate rootもimport pathとして渡します．

`discord_runtime.py`では`safe_final`，`split_discord_text`，`bounded_text`の本文とliteralの`PERSONA`だけを自動修正の候補にできます．関数signatureとその他の全top-level ASTをchecker外の固定guardへ保存し，所有者認証，設定，token，受理・分類・実行routeの変更を拒否します．reply helperへdynamic import，global操作，dunder accessなどを追加する変更も拒否し，既存test／assertionと独立レビューの検査を維持します．

reply helperのfree nameは純粋なbuiltin，固定した文字列／正規表現と検査済みのreply helperだけに限定します．`os`，`httpx`，`Path`，`asyncio`，`sqlite3`，`psutil`などの権限を持つglobalやそのalias／closureを参照できません．nested functionとcomprehensionのlocal scopeを区別し，正規表現moduleの操作とdata methodも明示した集合だけを許可します．

既存test moduleは，imports，fixtures，decorators，function bodyを含む元のtop-level ASTを変更できません．既定DSLでは末尾への一意な新しいtest／helper functionの追加だけを受理し，native profileでは元のraw行とASTの順序を保つtop-level間への関数挿入も受理し，既存bindingやtest名の上書き，skip／xfailの追加を拒否します．assertionを`if False`へ移す変更も元のASTが変わるため拒否します．追加したtestの妥当性は固定checkerと独立レビューでも確認します．

正式なplanner，Pi implementer，独立検証，freshな読み取り専用監査を通過したproposalだけを別roleのrelease adapterへ渡します．同じ最終headの必須CI，独立Codex Review，未解決review threadがないことを機械的に確認するまでmergeしません．review権限やサービスが利用できなければ`review_pending`で待機します．GitHub credentialはrelease subprocessだけで取得し，modelとcheckerの環境へ渡しません．

接続済み所有者のGitHubからreviewを依頼する場合だけ，運用者が`review_adapter`，`review_adapter_sha256`，`review_adapter_python`，`review_adapter_python_sha256`，`review_adapter_config`，`review_adapter_config_sha256`の6項目をすべて指定します．pathは絶対path，pinは実在する固定fileのSHA-256です．未指定時は既存のGitHub CLI認証で依頼します．host callbackのargvは`[python,"-I",adapter,"--config",config,"--request",request]`に固定し，modelやDiscord本文からcommandを作りません．callbackのconfigは`{"schema":"ephy.github-review-relay-config.v1","codex_executable":"<absolute pinned Codex executable>","codex_sha256":"<actual SHA-256>"}`です．

relay requestは`ephy.owner-review-request.v1`と`repair_id`，登録済み`repository`，`pr_number`，`head_sha`だけです．公式App Serverのstdio RPCでephemeral／read-only threadを作り，接続済みGitHubの実profileが`kirimine170`／`78987919`であること，PRが登録repository内のopen・main向け・同一repositoryのheadであることを確認します．model turn，shell，承認自動応答は使いません．固定の`@codex review`文面を投稿し，所有者による同じheadのmarker／本文を読み戻してから依頼済みとします．依頼本文に生のtaskや会話は含めません．pin変更，別所有者，head変更，未知response，承認要求，投稿結果不明は停止または`review_pending`となります．再試行時は所有者の同一markerで重複を防ぎます．このrelayはreview依頼だけを行い，CI，独立Codex Review，未解決finding，merge，本番healthのgateを変更しません．

PRには，検証commandと実測exit／安全な出力，base／head／treeとfile hash，checker source，公開baseに対するcontrol差分と展開後hash，policyと観測済みACK／session identity，独立監査のcheck内容とbindingを公開します．元のDiscord依頼，過去回答，prompt，CoT，tool本文は含めません．public goalとcheckerを含む公開資料に秘密情報や私的pathが残る場合は投稿前に停止します．公開reportはpathを正規化した派生物であることを明記し，private raw artifactのhashと混同しません．長いreportはUTF-8境界を保って複数commentへ完全に分割し，report自身のSHA-256と順序を固定します．検証hashや自己申告だけで外部reviewを依頼しません．

運用release callbackは固定SHA-256のPowerShellへ`-RepairRequest <absolute-json-path>`を渡します．request schemaは`ephy.owner-repair-release.v1`で，`repair_id`，`owner_user_id`，`repository`，`deployed_commit`，`pr_number`，`head_sha`を含みます．callbackは既存modelを必須とし，modelの起動・停止・unloadをせず，本番反映，対象serviceの再起動，health，model identity不変を実測します．stdoutは`{"deployed_commit":"<merged SHA>","healthy":true,"model_identity_unchanged":true}`だけを返します．成功確認前に完了通知を作成しません．

Botの`state_dir/repair-drain`へdeploymentごとのnonceを保存し，存在する間は新規処理と新しいoutbox chunkの送信を始めません．`repair-active.json`には`pid`，`created_at`（実processのUNIX開始時刻），`active`，`updated_at`，Botが現在観測した`drain_token`を保存します．同じevent loopのwatcherが1秒ごとに更新します．release callbackはPIDと開始時刻，今回のnonceへの新しいackを照合し，生成とoutbox送信の両方が0になるのを待ってからBotを再起動します．古いidle snapshotは採用しません．すでに受理した生成や送信は最後まで待ち，controller自体は停止しません．

```powershell
python -m ephy_worker.owner_repair --config E:/ephy-work/discord-launch/owner-repair.json
```

設定と永続storeを検証し，singleton lockを取得した後に`EPHY_OWNER_REPAIR_CONTROLLER_READY`を出力します．生の依頼とsourceを含むprivate stateやdiscovery／checker記録はGitへ追加しません．
