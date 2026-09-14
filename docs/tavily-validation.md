# Tavily無料枠での検索検証

2026-09-14．利用者の「無料枠がある検索APIに合わせて検証実装してほしい」という追加依頼に基づく変更である．Phase 1の検索adapterへTavilyを追加し，HTML／PDF取得・照合，モデル設定，最大1roundの追加調査は既存実装を利用する．SearXNGとの切替は設定で明示し，自動fallbackはしない．

## 選定

確認時点の[Tavily公式料金](https://docs.tavily.com/documentation/api-credits)では，Researcher無料プランは毎月1,000credit，カード登録不要，basic検索は1requestあたり1creditである．[Brave公式料金](https://brave.com/search/api/)にも月5ドル分の無料creditがあるが，カード登録を求めるため，今回はTavilyを選んだ．無料枠の条件は変更され得るため，利用時のdashboard表示を確認する．

## 実装した範囲

- `search.provider: tavily`を明示した場合だけ`https://api.tavily.com`へ接続する．SearXNGの起動設定は変更しない．
- APIキーは`api_key_env`で指定した環境変数から読み，Authorization headerだけへ付ける．URL・JSON body・設定・成果物へキー本体を書かない．別hostへの設定とredirectを拒否する．
- 各検索の直前に[GET /usage](https://docs.tavily.com/documentation/api-reference/endpoint/usage)でaccount情報を確認する．認識するプラン名は`Researcher`／`Free`，`paygo_limit=0`かつ`paygo_usage=0`，`0 < plan_limit <= 1000`，残り1credit以上を必須にする．値の欠落や未知の形式は未確認として停止する．課金設定の変更は行わない．
- [POST /search](https://docs.tavily.com/documentation/api-reference/endpoint/search)は`basic`，`auto_parameters=false`，`include_answer=false`，`include_raw_content=false`，`include_images=false`で固定する．追加料金のあるExtract／Crawl／Research endpointは使わない．
- APIの再試行は0回．認証・quota・429・timeout・不正応答時は同Jobの以降の検索を停止する．使用量確認も共通の`search_requests`へ数える．timeoutや取消で結果不明の検索も送信前に1creditを予約し，未知の使用量を0と表示しない．
- `max_credits_per_job`は最大10．同Job内では遅延した使用量応答で予約済みcreditを復活させない．設定例のquery上限は8なので，通常のresearchは最大8回のbasic検索となる．doctorの公開検索probeは別に1回となる．
- 返されたURL・snippetは候補選択専用であり，支持根拠にはしない．本文の実取得・PDF物理ページ・引用offset・意味照合は既存処理を使う．

月間の消費量はTavily account側で管理され，ほかのアプリやJobの消費も含まれる．Workerはaccountのusage応答とJob内上限を確認するが，請求台帳を持つものではない．dashboardで無料プランと従量課金無効を維持することが前提となる．

## 実行

[Tavily dashboard](https://app.tavily.com/)で無料accountとAPIキーを用意する．accountの従量課金を有効化する必要はない．APIキーはチャットへ貼らず，実行するterminalへ入力する．

以下はこのMacのworktreeと用意済みprivate設定を使う例である．同じterminalで順に実行する．`read`行はmacOS標準のzsh用で，入力を画面やshell履歴に残さない．

```zsh
cd /Users/kirimine170/Desktop/Ephy_Project/ephy-workspace/ephy-worker-web-research
uv sync --locked
read -rs 'TAVILY_API_KEY?Tavily APIキー: '
export TAVILY_API_KEY
echo
EPHY_TEST_CONFIG="/Users/kirimine170/Desktop/Ephy_Project/local-data/worker-research/worker-tavily.yaml"
EPHY_TEST_OUTPUT="/Users/kirimine170/Desktop/Ephy_Project/local-data/worker-research/reports"
uv run python -m ephy_worker doctor --config "$EPHY_TEST_CONFIG" --profile qwen-local --output-dir "$EPHY_TEST_OUTPUT"
```

doctorはQwen接続とTavilyの無料プラン／検索を検査する．`search.ok=true`と`profiles.qwen-local.ok=true`を確認してから調査する．

```zsh
uv run python -m ephy_worker research \
  --config "$EPHY_TEST_CONFIG" \
  --profile qwen-local \
  --output-dir "$EPHY_TEST_OUTPUT" \
  --question "2025年4月公開のQwen3-30B-A3Bについて，総パラメータ数と活性化パラメータ数，thinkingとnon-thinkingの切替方法，ベンチマークの推論条件と制約を公式資料と技術レポートから整理してください．2025年7月版とは区別してください．"
```

検索発見を検証するため，この例に`--source-url`は付けていない．適切なPDFを検索できなければ，既存CLIの`--source-url`で補足した実行を別Jobとして試せる．その場合は検索発見と補足資料を区別して記録する．

APIキー未設定では明示的な設定エラーとなり，researchのJobは作成しない．`tavily_free_plan_required`／`tavily_paygo_must_be_disabled`／`tavily_free_allowance_unverified`の場合はdashboardと契約・usage応答を確認する．無料枠を確認できない状態で制約を緩める必要はない．

終了時は`Ctrl+C`，使い終わったキーをterminal環境から取り除く場合は`unset TAVILY_API_KEY`を使う．成果物の読み方はREADMEと共通である．`metrics.json`の`search`へ予約credit，provider報告credit，不明件数，accountの使用量確認結果を保存する．

ほかの環境では`configs/worker.tavily.example.yaml`をGit外へコピーし，既存modelのURL／実IDと実context長を設定する．Windows・Linuxでの今回の変更の実機試験は未実施である．

## 検証結果

APIキー未設定のためTavilyへの実通信・実課金状態・実検索品質は未検証であり，実機完了とは扱わない．mock HTTPで契約・残量検査，送信先固定，basic要求，認証・quota・redirect・サイズ・timeout・取消・未知usage・架空snippet非採用，CLIの未設定エラーを確認した．追加36件を含む全199件のoffline pytestが12.11秒で成功した．後続roundでcredit上限に達しても，前roundの根拠を保持した部分レポートを保存することを確認した．doctorの選択providerとusage表示もmockで確認した．

ruff check／format，repository validationとsensitive-pattern scan，git diffの空白検査も成功した．依存追加はなく，既存lockfileと専用環境を使用した．既存SearXNG設定は保持し，privateな`worker-tavily.yaml`は別ファイルとして作成した．

この変更は検索経路の追加であり，前回のQwenによる数値精度・比較表現の意味照合漏れを修正したものではない．実検索が通った後も，主要claimは[Phase 1検証記録](phase1-validation.md)と同じ基準で本文と照合する．
