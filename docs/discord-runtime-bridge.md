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

最終回答は，受信時の最大32768文字と秘密情報・内部出力・制御文字の検査を維持したまま，全体をsanitizeします．1800 UTF-16単位を超える回答も末尾を省略せず，段落，改行，文末を優先して分割し，順番に全て送ります．絵文字の途中では分割せず，通常のMarkdown code fence内で分割するときは閉じる行と再開する行を補います．開始・終了の区切り行や継続用の補助行が上限に収まらない長いfenceは，元の文字列として分割し，全ての本文を保持します．それ以外の本文は保持します．モデルには必要な事実や説明を残し，可能なら1800文字以内で簡潔に答えるよう促します．

直接返信の参照先は，このtransportが同じguild／channel／userへ送った最終回答の各messageです．送信に成功してIDを受け取った部分を，それぞれ独立した返信先として記録します．過去回答は各部分につき最大1200 UTF-16単位，24時間以内，全体で最大128件を保持し，既知のcredentialをredactします．全回答をDiscordへ届ける範囲と，後続の質問へ渡す参照contextの保持範囲は別です．新しい質問だけを従来の厳格な公開text検査（最大4000文字）へ通してから，planの`query`へそのまま指定します．過去回答は別のoptional `context`として渡し，chatでもassistantの参照用データとして保持します．chatの最新user本文は新しい質問そのものです．過去回答がない場合は従来どおり`query`だけを送ります．

既に公開した回答のcode fenceや公開連絡先・pathの例を，新しい質問の個人情報として再判定しません．Runtimeは過去回答を命令や権限として扱わず，必要なredaction後にlocalな検索語書換えの参考だけに使います．書換え後の検索語はRuntimeの同じprivacy／許可検査を必須とし，Managerへ送るのは許可された検索語だけです．生の過去回答をManagerへ渡しません．過去回答によって承認権限は変わらず，`confirm`の自動承認には前述の明示設定が必要であり，`block`は常に拒否します．Discordの履歴取得や参照先のfetch，返信先の再帰探索はしません．

## 状態と失敗

SQLiteへmessage IDをHTTPより前に確保し，並行配信や再起動後の重複を実行しません．確保後のcrashや返信の成否不明も自動再送しません．request IDは最大10000件を保持し，上限で新規受理を停止します．古いIDを削除して重複実行を許すpruneは行いません．stateを削除するとこの保証を失います．生の利用者質問やtokenをledgerへ保存しません．Windowsでは親directoryのACLを継承するため，利用者専用のlocal directoryを使います．

分割した最終回答の途中で送信に失敗した場合も，同じrequestを自動再実行・再送しません．それまでに送信・記録できた部分への直接返信は参照できますが，未送信の残りを配信済みとは扱いません．

同時調査は最大2件です．取消時はローカルtaskとHTTP接続を解放し，Runtimeへ取消をbest effortで送ります．検証済みの`allow` planまたは明示設定による承認を検証済みの`confirm` planでpending回数の上限に達した場合や，chatの通信・protocol・回答検査が失敗した場合も，同じplanの取消を1回試みてから元の失敗を返します．取消requestが失敗しても元の失敗理由を置き換えず，新しいplanやJobを作り直しません．正常完了や未承認・不正なplanには取消を送りません．遠隔Workerの停止完了をtransportだけで保証するものではありません．失敗は固定した短い説明で返し，例外本文やHTTP response本文をlogへ出しません．全てのDiscord返信で`allowed_mentions=none`とembed抑制を指定し，UTF-16で1800単位以内に制限します．

offline testsはSDKの有無によらず許可，pending再開，SSE，重複，返信contextと失敗を確認します．実Discord投稿や本番Bot restartはtestに含めません．
