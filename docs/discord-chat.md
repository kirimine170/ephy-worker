# Discord mention adapter のローカル試作

現在の入口は，Discordの候補から選んだ実botメンションを先頭に付けた公開情報の質問である．`@ephy 質問` を既存ResearchExecutorへ渡す．通常投稿を仕事として実行せず，前turnを踏まえる継続会話もまだ実装していない．[worker #26](https://github.com/kirimine170/ephy-worker/issues/26) のSheets取得と，[runtime #94](https://github.com/kirimine170/ephy-runtime/issues/94) の会話・承認を置き換えない．

## 作業・返信と読取り能力の分離

- 実際のbot user IDが，生本文の先頭 `<@ID>` または `<@!ID>` とSDKのmentions一覧の両方にある場合だけを受け付ける．文字列`@ephy`，everyone，role，返信由来のmention，code内mentionでは起動しない．初期版は明示mentionがあってもreply形式を拒否する．
- bot自身，他bot，webhook，DM，別guild，不許可userからは実行も返信もしない．編集・システム投稿は対象外である．
- mentionモードの `allowed_channel_ids` は作業・返信先の単一テストchannelを指定する．現在の指定は `1560630456070967386` である．その外の明示mentionも実行・返信しない．親カテゴリからの許可継承はない．
- Gatewayの受信範囲はDiscord側のbot権限に従う．`read_message_content=false` の初期版は非メンション通常本文の読取りを実装済みとは扱わない．botがmentionされた本文はprivileged intentなしでも届く公式例外を利用する．
- 将来 `read_message_content=true` を設定し，Developer PortalでもMessage Content intentを有効化すると，botに許可されたchannelの通常本文を受信できる．ただし現在のhandlerは非mention通常投稿を無視し，保存，Karte登録，外部転送，タスク化をしない．履歴取得toolも未実装である．
- `max_messages=None` でSDKのmessage本文cacheを無効にする．全履歴の走査・一括保存を行わない．明示された作業の本文だけを，既存Workerの公開情報用privacy検査を通したうえで設定済み検索・モデルへ渡す．

通常メッセージ返信は**テストchannelへの通常投稿**であり，ephemeralではない．mentionsを無効化しembedを抑制する．秘密や業務の生会話を入力しない．

## 起動に必要な設定

Application IDは `1560577412540932136`，テストchannelは `1560630456070967386` である．guild ID，操作するDiscord user ID，実運用のWorker YAMLとprofileは利用者が指定する．空のuser許可リスト，guild ID 0，wildcardを全許可として扱わない．

依存は専用venvへ導入済みである．確認時はPython 3.12.14，discord.py 2.7.1，aiohttp 3.14.3であった．別の場所へ移すときは `uv sync --locked --extra discord --python 3.12` を使う．共有モデル／Strataの設定や起動を変更しない．

Git外の起動補助は `E:\ephy-work\discord-launch\Start-EphyDiscord.ps1` である．初回Checkでguild ID・操作user ID・実worker設定path・profileを対話入力し，tokenを含まない `discord.toml` を同じdirectoryへ作る．既存設定は上書きしない．テスト用の `127.0.0.1:9` fixture設定を実運用に流用しない．

```powershell
& 'E:\ephy-work\discord-launch\Start-EphyDiscord.ps1' -Mode Check
# After non-secret settings pass, the user may run:
& 'E:\ephy-work\discord-launch\Start-EphyDiscord.ps1' -Mode Run
# If the file is elsewhere:
& 'E:\ephy-work\discord-launch\Start-EphyDiscord.ps1' -Mode Run -TokenPath 'C:\actual\token-file.txt'
```

launcherの既定token候補は `C:\Users\ephy\token\ephy_discord_token.txt` である．**ユーザーがRunを実行した場合だけ**そのfileを読み，`ProcessStartInfo.EnvironmentVariables["EPHY_DISCORD_BOT_TOKEN"]` としてPython子プロセスだけへ渡す．token値をコマンド引数，TOML，ログ，別fileへ保存せず，親PowerShellの環境変数や永続環境変数を変更しない．起動直後にStartInfoの値と一時参照を消し，子終了時にその環境も失われる．元のtoken fileの保存場所やACLは変更しない．

agentが行ったのはファイルの存在・サイズの確認と合成tokenのmock検証だけであり，実token内容の読取り・実認証・実接続は行っていない．Checkはtoken fileを読まず，通信，Bot token解決，Job作成をしない．profileの実endpoint／modelの稼働やDiscord権限を検証した結果ではない．

直接CLIを使う場合も，tokenは既存どおり `EPHY_DISCORD_BOT_TOKEN` からだけ解決する．`discord check --config ...` はoffline，`discord run --config ... --connect` はlive操作である．mentionモードにSlash登録は不要であり，`discord register` はtokenを読む前に拒否する．旧Slash互換を使う場合だけ `entrypoint="slash"` として旧コマンドを明示利用する．

## Discord側の最低要件とintent

mention版ではGateway intentsのGUILDSとGUILD_MESSAGESを使い，初期版のMESSAGE_CONTENTは無効にする．Message Contentの公式例外によりbot本人へのmention本文は取得できる．members／presenceのprivileged intent，Administrator，履歴取得権限は初期版に要求しない．通常本文をすべて受信する場合は，コード側のflagとDeveloper Portal側のMessage Content設定の両方が必要である．設定変更は利用者が行う．

botにはテストchannelの実効 `View Channel` と `Send Messages` が必要である．招待URLの `permissions=0` は追加権限を要求しないという意味で，既存の@everyone等から権限を継承していれば動作する．この作業ではDiscord側の権限を確認・変更していない．mentionだけを使うため `applications.commands` scopeやSlash登録は必須ではない．Gateway受信のため公開port／tunnelやInteractions Endpoint URLも不要である．

## 取消・終了・失敗

`@ephy cancel` は，同じguild／channelで同じ本人が開始した実行だけを取り消す．受付IDを明示する `@ephy cancel 受付ID` も使える．実際のbotを候補から選び，先頭でmentionする．取消の大文字小文字は区別しない．不正なcancel形式を調査として実行しない．

一度に1件，待ち行列は作らずbusyを返す．質問は1000文字，実行は既定300秒・最大600秒，返信はUTF-16で1800単位ずつ最大5通に制限し，超過を明示する．公開情報だけを使い，profile，URL，tool権限，shellをDiscord本文から指定できない．

同じ実行への取消は異なるmessage IDでも1回に限定し，取消開始時にadapterの期限を解除する．shutdownも同じ取消経路と共有終了taskを使い，重複要求・待機timeoutからexecutor cleanupへ再取消を送らない．Ctrl+Cでbotの子プロセスを停止する．launcherは終了待ちのあとも残る場合だけ自分が起動したbot子プロセスを停止し，共有モデルサービスは停止しない．実Discord接続中のWindows console Ctrl+Cはまだ未検証であり，強制停止時の成果物保存は保証しない．

SQLite台帳はmessage／interaction ID，guild／channel／user ID，status，Job IDだけを保存し，token，質問，返信，例外本文を保存しない．OS lockで同じstate directoryの複数起動を拒否する．同じIDは再起動後も再実行しない．crash途中のreceived／runningはinterruptedとして残し，自動resume／再送しない．台帳は1万件で新規要求を拒否する．

Discord向けReportのcopyでは，GitHub／Discord／Tavily等の形式と既知bot tokenをMarkdown変換前に除去し，分割前・SDK送信直前にもMarkdownのescapeを考慮して検査する．Git外の正本Reportは投影で変更しない．完全な機密分類やprompt injection検出を保証しない．研究成果物には明示作業の質問・取得資料・回答がGit外に保存される．

## 元botの照合と検証範囲

添付ZIP `discord-reader-mcp-0.1.0.zip` は80,765 bytes，SHA-256 `57f0da3c2440eccf1178d55045ca521ad1936f3afdf442f763ac58baf8104c92` を正式取得して確認した．依存はPython 3.11+，mcp>=1.9,<2，httpx>=0.27，pytest>=8である．環境変数token・GET専用REST・階層allowlist・429/5xx retry・TTL cache・cursor paging・9件のoffline testを静的照合した．ライセンス宣言は見つからず，ソースはコピーしていない．秘密形式や長い秘密定数の検査は該当なしだったが，完全な機密分類を保証しない．

本試作はdiscord.pyにGateway／HTTPのbucket・global rate limit・429処理を任せる．adapter自身は曖昧な送信失敗を自動再試行せずdelivery_failedとする．SDK内部retryを含むexactly-once配信は保証しない．fake SDK・mock HTTP／processと実executor fixtureを使う自己checkであり，実Discordの権限，接続，再接続，実429や実モデル能力の合格ではない．独立レビュー済みの旧bytesを新しいmention差分の承認として流用しない．

## 全channel通常投稿のread-only拡張案（未実装）

Message Contentを明示的に有効化した後，Discord権限で見えるchannelをread対象とし，作業・返信先の単一channel許可とは別のread-only adapterを設ける案である．履歴が必要なときだけ固定Discord REST GETを使い，guild／ユーザー認可，件数・期間・bytes・timeoutを制限し，取得元message IDと時刻を付ける．一括巡回・全履歴保存をしない．この読取toolは現在まだ実装していない．

全通常投稿の本文を自動でKarte，RAG，外部クラウドや検索へ送らない．他channelから取得した内容をテストchannelへ出す場合も，実行者の明示要求・必要部分・機密確認を別に行う．将来の会話／承認と保存はRuntimeの責務である．

公式仕様：[Message Content例外とintents](https://docs.discord.com/developers/events/gateway#message-content-intent)，[channel permissions](https://docs.discord.com/developers/topics/permissions)，[SDK](https://discordpy.readthedocs.io/en/stable/api.html)．

## 継続会話に必要な最小追加案（未実装）

現在の機能は公開情報の単発調査入口であり，前の発言を踏まえる会話はできない．`@ephy chat` と `@ephy reset` を追加し，会話の状態と認可をRuntimeへ置く案を次の最小候補とする．今回このAPI，履歴，コマンドは実装していない．

現行Runtimeの `ChatCompletionRequest.messages` は履歴を受け取れる．しかし `apps/gateway/routes.py` の205行以降では，fast modeでも251–264行でlocal RAG検索を行い，取得結果を326行以降でpromptへ組み込む．`source_scope="none"` や `rag_required=false` を送るだけでは検索を無効化できない．`RequestMetadata` に参照を確実に禁止するfieldは現在存在しない．固定fast modeは `ModelRouter._resolve_mode` によって選べるが，RAG無効化とは別である．

最小の実装単位は次のとおりとする．

1. **Discord側は既存transportを再利用する．** worker内の薄い `RuntimeChatClient` が `@ephy chat` とresetを，利用者が固定した数値loopbackのRuntime URLへだけ渡す．proxy・redirect・URL／model／metadataの本文指定は拒否する．Bot tokenをRuntimeへ渡さず，既存 `/ephy-research` だけをResearchExecutorへ送る．
2. **Runtime側に限定会話入口を追加する．** 例として `POST /v1/discord/chat` と `POST /v1/discord/reset` を定義し，request ID，アプリ／guild／channel／user ID，本文のみを型付きで受ける．Runtimeでもserver側のallowlistを照合する．Discord本文からsystem／tool messageやモデル，source scopeを受け付けない．既存Gatewayの会話処理を共有する小さなserviceを抽出し，同じmodel routingとpersona処理を使う．新しいモデルproviderやGatewayを作らない．
3. **参照禁止をRuntimeが強制する．** 例として `RequestMetadata.grounding: Literal["default", "none"]` を追加し，専用入口は常に `none` と固定する．この指定ではlocal RAG／Karte／embedding／Web検索の呼出しをすべて0件にし，scope指定との矛盾は拒否する．`grounding` は提案fieldであり，現行APIへ送るだけでは機能しない．model routeは `fast`，`stream=false`，Web検索は無効に固定する．Runtimeの公開可能なpersonaを利用者が選び，private profileや取得資料をDiscordへ公開する設定を暗黙に引き継がない．
4. **履歴はRuntimeのメモリ内だけに保持する．** keyはアプリ／guild／channel／user IDの組とし，他userや場所へ共有しない．初回は最大6往復，本文合計12,000文字，最大100session，20分TTLの上限とする．固定modelの実context上限にsystem prompt・回答予約分・履歴が収まることもRuntimeで事前検査する．上限を超える場合は黙って会話を削らずresetを求める．restart／resetで消去し，Karteや学習datasetへ自動保存しない．会計用companyの分離・承認はruntime #94の別scopeである．
5. **失敗時は会話を進めない．** 1sessionにつき1要求，request IDで重複生成を防ぎ，取消とresetを同じownerに限定する．timeout／拒否／送信不明のturnを成功履歴として追加しない．未確定turnが残る場合は自動再生成をせず，状態確認またはresetを求める．モデルやRuntimeが不在なら固定エラーを返し，別モデル・クラウド・検索へfallbackしない．

想定差分はworkerの新規 `runtime_chat.py`，既存mention入口／設定／テスト，Runtimeの限定route・小さなsession service・metadata schema・テストに限定する．Runtimeとの実接続を追加した段階で `.ephy/project.yaml` の直接関係も更新する．本試作では未実装依存として登録しない．既存Runtimeの起動状態や設定，認証，公開範囲は今回変更・実検査していない．

オフライン受入条件は，架空の2往復で前turnの引継ぎを確認し，user／channel越境，reset／TTL／上限，重複，取消，Runtime不在，例外・token非露出を検証することとする．Runtime mockではRAG／Karte／embedding／Webのstubを呼ばれると失敗する設定にし，`grounding=none` が参照を確実に止めることを確認する．未知fieldを送るだけの見かけ上の無効化を合格にしない．実Discordと実モデルの確認はこの後の別gateである．

根拠：[Runtime Chat route](https://github.com/kirimine170/ephy-runtime/blob/main/apps/gateway/routes.py#L205)，[request schema](https://github.com/kirimine170/ephy-runtime/blob/main/packages/llm_runtime/schemas.py)，[model router](https://github.com/kirimine170/ephy-runtime/blob/main/packages/router_core/router.py)，[persona処理](https://github.com/kirimine170/ephy-runtime/blob/main/packages/prompt_core/loader.py)．取得時のblob SHAと該当行の抜粋はGit外の監査資料へ固定している．
