# 知識・cache・調査終了の実験

この実装は未接続の実験モジュールと手動CLIです．稼働中Bot，既存Report，Discord応答，起動設定，依存関係は変更しません．正式Piの実装・監査や本番採用を成立させるものではありません．

## 回答経路

一般的で安定した説明は，型付きのroute proposalと短いknowledge draftを利用できます．出力のoriginは`model_knowledge`，`externally_verified`は常にfalseです．知識draftからClaimやEvidenceを作成したり，checked状態を付与したりしません．confidenceはモデルの自己申告であり，正しさの証明には使いません．低いconfidenceのproposalは，保守的に資料を必要とする経路へ進めます．

現在・最新，出典指定，医療等の重要判断，型番・価格等の具体的で未確認の質問には，決定的なguardを優先します．guardは有限の語と質問形を検査します．未知の言い換えをすべて検出する分類器ではなく，false positiveもあり得ます．固定fixtureの合格は，この有限集合での方針検査です．

資料が必要な場合，質問が完全一致するin-memory cacheを調べます．policy identity，as-of，現在時刻，TTL，canonical Reportのsnapshot hashを照合します．過去のReportや取得時刻を新しいas-ofだけで延命できません．未完了，不十分，失敗，gap，矛盾，時刻不正，未知の取得元，紹介文だけのReportは再利用しません．cacheの拒否理由は`cache_code`へ記録し，researchへ進めます．

取得元のHTTP状態，本文種別，hash，extractor，byte数，取得時刻，URLのpublic性・credential・秘密の形式，read passageと引用spanも確認します．URL検査はネットワーク解決を行いません．Reportのdeep copy上で，記録済みEvidenceCheck／ClaimCheckから既存`apply_review`を再適用し，一次資料による支持または確認済みの独立した複数起点の条件を検査します．その後，既存`verified_facts`のpositive fact／span gateを通します．canonical Reportは変更しません．

これは取得byteの真正性や意味の正しさを新たに証明する処理ではありません．引用の存在だけでは主張への意味的含意を証明できず，記録済みの照合判断が誤る可能性は残ります．cacheは一時メモリだけに保持し，実ユーザーのcache，会話，key，Bot tokenは読みません．

## 調査終了のreplay

検索・取得は公開合成Reportのラウンド列として再生します．新しいlive Web検索loopではありません．主要subquestionのcoverageはpositive verified fact IDへの参照に限定し，未知IDや未確認claimの参照を拒否します．coverageの意味的な対応そのものはfixtureの前提であり，モデルが自動的に正しく割り当てた証拠にはしません．

gapや重大な矛盾が残る場合は，予算と新しい事実がある範囲で次のラウンドへ進みます．3ラウンド目で不足が解消する例も含みます．事実本文と条件が重複するラウンドが2回続くとpartialです．時間・モデル回数・ラウンド上限，fixture枯渇，エラーでもpartialになり，sufficientとは区別します．cancelは十分性や予算より優先します．最大8ラウンド，モデル回数6，300秒までのreplay設定を検証します．

## 実行と結果

既定はオフラインです．`--cases`には固定された公開合成`evaluation.json`を指定します．そのbyte hashが異なる入力は拒否します．`--output`にはGit checkout外の絶対パスを指定し，親directoryは事前に作成します．既存出力，symlink，junctionを拒否します．

```powershell
$env:PYTHONPATH = 'E:\ephy-work\worker-answer-routing-lab-20261018\src'
& 'E:\ephy-work\discord-chat-20261016\.venv\Scripts\python.exe' scripts/benchmark_answer_routing.py --cases 'E:\ephy-work\worker-answer-routing-lab-evidence-20261018\evaluation.json' --output 'E:\ephy-work\worker-answer-routing-lab-evidence-20261018\offline-example.json'
```

オフラインroutingでは全質問に同一の楽観的proposalを与え，質問に基づくguardを検査します．expected labelは評価側だけに保持し，routerの入力には渡しません．結果は`mode=offline`，`passed`，`failed`，`model_requests=0`，各rowのexpected／actualを記録します．これは知識回答の品質測定ではありません．

独立検証・read-only reviewの後に，実験operatorだけが次のopt-in probeを実行します．実装者のself-checkと通常のtestsはmockを使います．

```powershell
& 'E:\ephy-work\discord-chat-20261016\.venv\Scripts\python.exe' scripts/benchmark_answer_routing.py --cases 'E:\ephy-work\worker-answer-routing-lab-evidence-20261018\evaluation.json' --output 'E:\ephy-work\worker-answer-routing-lab-evidence-20261018\live-example.json' --live-model --base-url 'http://127.0.0.1:18080/v1' --model-id 'Qwen3-Coder-Next-Q4_K_M'
```

endpointとmodel identityは上記の値に完全一致させ，応答model IDも検査します．既存`ModelRunner`を`prompted_json`，`max_tokens=512`，context 16384，schema retries 0で再利用します．discovery 1回，routingと短い知識draft 3回，公開合成資料の3主張を一括ラベル判定する1回を直列に実行します．物理requestはdiscoveryを含め最大5，各45秒，全体300秒です．有料API，download，service再起動，model unload，設定変更は行いません．

資料判定promptにはsource本文と主張本文の配列だけを渡し，gold labelやcase IDを渡しません．モデルからはsupported／contradicted／not_establishedの3ラベルだけを受け取ります．知識draftも型・長さ・有限の安全検査を通したものだけを保存します．思考過程，raw response，不正な出力本文や例外詳細は保存せず，失敗はcodeとして記録します．この安全検査もあらゆる秘密形式や不正表現を網羅する保証ではありません．

live結果では実行成功を`execution_ok`，routing・資料ラベル・知識keyword補助検査を各指標および`passed`／`failed`で区別します．未取得の期待結果は失敗へ数えます．知識keywordの一致は弱い補助検査であり，知識回答の正確性や十分性の証明ではありません．CLIのexit 0も実Webの正しさや本番適用の承認を意味しません．

今回のWindows pytest環境では，candidate外の専用`clock-runtime`をPYTHONPATHへ加え，固定hashの`clock_contract`を`-p clock_contract`で読み込みます．これはProactorの早期wake許容幅だけを0へ設定する環境契約です．元のmonotonic時計，deadline，既存tests，assertion，skip，アプリケーション方針は変更しません．evidence全体はPYTHONPATHへ加えません．
