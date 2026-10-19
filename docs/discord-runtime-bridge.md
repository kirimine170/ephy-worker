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

まず`POST /v1/web-search/plan`へ公開の質問を送り，`allow`の場合だけ`web_search=true`と同じplan IDを付けてchatを開始します．`confirm`／`block`は検索していないことを明示して終了し，自動承認はしません．chatには`Prefer: respond-async`を明示してHTTP202への対応を通知し，202の場合は同じpayload／plan ID／headerを繰り返して再開します．このheaderを送らない既存のRuntime clientは通常のcompletion／SSE接続を保ったまま待機します．pending／finalのJobとplanの不一致，失敗，接続断，未完のstreamを成功にはしません．

最終生成はSSEです．`delta.content`だけを集め，`reasoning_content`やroute／source eventを会話本文へ混ぜません．完了reasonと`[DONE]`を確認してから最終回答を送ります．thinking tagを除去し，不完全な内部出力を拒否します．streamには600秒のread idle timeoutがあり，活動が続く生成に経過時間の締切は設けません．検索のpending回数，stream byte数，回答長は別に制限します．意味上の反復を判定するモデルやloop watchdogは追加していません．

直接返信の参照先は，このtransportが同じguild／channel／userへ送った最終回答だけです．過去回答は最大1200 UTF-16単位，24時間以内，最大128件を保持します．質問の参照用データとして同じplan queryとchatへ渡すため，Runtime plannerも主題を解決できます．Managerへ送るのはRuntimeが許可した書換え後の検索語だけです．Discordの履歴取得や参照先のfetch，返信先の再帰探索はしません．

## 状態と失敗

SQLiteへmessage IDをHTTPより前に確保し，並行配信や再起動後の重複を実行しません．確保後のcrashや返信の成否不明も自動再送しません．request IDは最大10000件を保持し，上限で新規受理を停止します．古いIDを削除して重複実行を許すpruneは行いません．stateを削除するとこの保証を失います．生の利用者質問やtokenをledgerへ保存しません．Windowsでは親directoryのACLを継承するため，利用者専用のlocal directoryを使います．

同時調査は最大2件です．取消時はローカルtaskとHTTP接続を解放し，Runtimeへ取消をbest effortで送ります．検証済みの`allow` planでpending回数の上限に達した場合や，chatの通信・protocol・回答検査が失敗した場合も，同じplanの取消を1回試みてから元の失敗を返します．取消requestが失敗しても元の失敗理由を置き換えず，新しいplanやJobを作り直しません．正常完了や未承認・不正なplanには取消を送りません．遠隔Workerの停止完了をtransportだけで保証するものではありません．失敗は固定した短い説明で返し，例外本文やHTTP response本文をlogへ出しません．全てのDiscord返信で`allowed_mentions=none`とembed抑制を指定し，UTF-16で1800単位以内に制限します．

offline testsはSDKの有無によらず許可，pending再開，SSE，重複，返信contextと失敗を確認します．実Discord投稿や本番Bot restartはtestに含めません．
