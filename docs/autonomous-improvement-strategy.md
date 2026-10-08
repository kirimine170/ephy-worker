# ephy-workerの自律改善戦略

## 調査の結論と証拠の範囲

調査日：2026-10-08．対象sourceは`d36960b5b524deb393e562c7329068503395620c`である．
既存のPi adapter，固定checker，比較interface，有限campaignを再利用し，まず「完全な計画を返せたか」と「予定した全試行を同じ予算で実行したか」を外側で判定する小さな変更を推奨する．
その後に，凍結したskill／toolの効果を未使用課題へ転用できるか測る．
現時点で，compact-output候補，context packet，skill／tool／MCPによる実モデルの改善は実証されていない．

この文書はCodexによるsource調査と設計提案であり，指定モデルのformal Pi Jobを実行した成果ではない．
実モデル呼出し，新しいtrial，Strataの操作，既存lockや予算の変更は行っていない．
文書公開は性能改善の採用を意味せず，既存[governance](system-development-governance.md)と[評価契約](../.agents/skills/ephy-worker-self-improvement/references/eval-contract.md)を変更しない．

| 証拠区分 | 本文での扱い | 言えないこと |
|---|---|---|
| sourceで確認した実装 | 対象commitのcodeと対応文書へlinkする | 現在の稼働serviceでも同じ設定が成立している |
| 公開PRに記録された結果 | PR番号，確認日，対象headを区別する | PR本文だけで独立した実行証拠が揃う |
| 依頼時の既往報告 | 新規測定と分け，報告された範囲だけを述べる | raw証跡の再検証や欠けた数値の補完 |
| offline control | 配送，拒否，parser，checkerの挙動を示す | local LLMの計画能力，正答率，転用効果 |
| 今後の提案 | 仮説，scope，受入条件を示す | 実装済み，実行許可済み，採用済み |

Piの参照snapshotは公式repositoryの`v1.1.0`，commit `abe508e1b89912adde45528136c3221eb69acdd7`である．
repo内の過去記録に現れる`0.85.1`／`0.86.1`とは区別する．
今回，稼働中Pi binaryのversionやhashは取得しておらず，最新仕様との実環境互換性を証明しない．
公式文書は可変のlatest URLではなく，以下のtag付きsourceを基準に読む．

- [SDK](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/sdk.md)／[RPC](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/rpc.md)．
- [Skills](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/skills.md)／[Extensions](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/extensions.md)．
- [MCP](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/mcp.md)／[Compaction](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/compaction.md)／[Security](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/security.md)．

## 現行基盤と過去の到達点

[README](../README.md)にあるcoding経路は，`CodingJob → worktree → Pi/mock → patch → 独立validation → CodingResult`である．
[PiRpcRunner](../src/ephy_worker/coding_executor.py)は既にPythonからPi subprocessを制御し，JSONLでrequestとeventを扱い，`agent_settled`を待つ．
TUI操作や別のagent loopを作り直す必要はない．
通常のcoding executorと，[formal runtime](../src/ephy_worker/formal_runtime.py)のmanaged JSON print経路は別interfaceであり，同じ安全保証を持つとは扱わない．

| 基盤 | sourceで確認した範囲 | 改善測定までの不足 |
|---|---|---|
| [正式proposal runner](formal-pi-runtime.md) | role，scope，policy ack，固定check，hash，stage順序を検査する | 実行codeのverifier sandboxが未接続で，対象はMarkdown |
| [既存Strata profile](strata-proposal-runtime.md) | 単一Markdown，別planner／implementer，zero repair，外部review待ちで停止する | formal audit完了や自動採用を表さない |
| [stage guard](../tools/pi-local/formal-stage-guard.ts) | admitted request，総output，応答cap，model identityを検査する | provider usageの真実性やserver側取消を単独では証明しない |
| [benchmark](fixed-benchmark-comparison.md) | model／variant比較の固定条件とtask／repeat整合を検査する | 予算強制や事前plan全件の実行attestationではない |
| [transfer評価](transfer-evaluation.md) | 公開synthetic 6課題，3 development／3 heldout，外置checkerとcontrols | 実モデル転用は未測定で，checker自体はsandboxではない |
| [campaign](../src/ephy_worker/formal_campaign.py) | plan／verifier freeze，lock，有限期限，停止，中断Job非再実行 | 成功proposalを累積的改善や採用へ変換しない |

[PR20](https://github.com/kirimine170/ephy-worker/pull/20)の公開説明は，8 requestの試行をread-onlyで点検した結果として，ack 1回と全文read 7回が成功し，最終計画を得られなかったと記録する．
promptに固定capを明示したことと，モデルがcap内で計画を完成できたことは別の判定である．
[PR21](https://github.com/kirimine170/ephy-worker/pull/21)の予算記述は将来の試行案であり，その設定によるlive成功を示さない．
その数値を新しい既定予算や実行許可として転記しない．

依頼時の既往報告では，出力上限275／1024 token条件で完全な計画を返せなかった試行と，compact-output候補のoffline testが報告されている．
これらを今回再実行していない．
実際の`finish_reason`，usage，serverに適用されたcap，timeoutを照合できていないため，上限値だけからtoken exhaustionを失敗原因と断定しない．
compact-output候補が短い応答を生成できること，構造化された計画が完全であること，課題を正しく解決することを分けて測る必要がある．
offline合格を実モデルの改善へ読み替えない．

[PR24](https://github.com/kirimine170/ephy-worker/pull/24)は2026-10-08確認時点でopen／unmergedであり，対象headは`f9b65b9a678c38716b46ddc894a9c478e62d2964`である．
context packetは2–16 file，fileごと32 KiB以下，合計64 KiB以下という提案で，公開説明の検証範囲はofflineの配送・拒否controlである．
bytes上限はtoken fitや応答cap内での完成を保証しない．
現行mainの保証や，完全な計画，実モデル正答率の改善として扱わない．
採用条件を満たした場合にだけ，既存配送方式との比較候補にできる．

## 自律改善を一つの失敗から進める

一般的な能力不足の診断から資産検索・獲得までを自動で完了するend-to-end経路は，今回のsourceや実測で確認できていない．
次の流れを，既存Job／proposal境界を保つcontrollerの設計案とする．

1. 失敗を「mandatory readでrequest消費」「応答capで未完」「要求の誤解」「tool情報不足」「環境・checker不備」に分ける．観測値，未知値，失敗stageを残し，agentの説明だけで原因を確定しない．
2. capability gapに対して，検証した既存skill，tool，公式文書を検索する．metadata広告と全文readを分け，検索結果の出典・hash・適用条件を記録する．
3. 再利用・設定変更で解けるか判断し，必要なら一つだけのbounded candidateを提案する．新toolを増やす前に，既存readやAST補助で必要情報を得られるか調べる．
4. 固定したoffline controlsで，完成性，意味保持，越境拒否，false-success拒否を確認する．環境不備はcandidate repairにせず停止する．
5. 別途許可された将来のlive比較で，同model・等予算のpaired条件を測り，candidate freeze後に未使用heldoutへ進む．効果が不明なら未実証の候補として保存する．
6. 最終candidateと失敗例を来歴付きarchiveへ残し，現在headのCI・独立reviewと人間の判断を経て再利用可能skillへ昇格する．検索・生成・報告受領から採用へ自動遷移しない．

archiveには既存Job保存と[Karte consumer](karte-experiment-consumer.md)の証拠identity境界を再利用する．
このconsumerはsynthetic adapterで，停止済みJobのartifactを検査する境界であり，実モデル運転やproduction連携，formal合格，採用を提供しない．
新しいmemory DBを先に作らず，private evidenceを一般skillへ混入させない．
proposerやarchive自体の効果は，基礎測定が成立した後の別ablation課題とする．

## 経験を条件付きlessonとreview済みsnapshotへ変える

以下の経験蓄積とskill化は未実装の設計案である．
run証拠から，「どの条件では何が有効だったか」というlessonを提案し，既存Karte reviewを経たsnapshotだけを将来の測定入力にする．
一度の成功から無条件の規則を作らず，失敗例，counterexample，未測定範囲を残す．

| 保存する対象 | 内容 | 与えない権限 |
|---|---|---|
| immutable observation | 実行条件，結果，停止理由，証拠ref／hash | 原文の上書きや成功への再label |
| conditional lesson | 適用条件，仮説，反例，元証拠ref，review状態 | evaluator変更，tool追加，自動activation |
| tested skill candidate | lessonから作った手順，version／hash，許可resource，固定評価結果 | 未reviewの命令実行，model／scope拡大，自己採用 |

Karteの参照sourceは2026-10-08確認のcommit `0ff51235bf9682b241d8878d8c96b9448b9fc125`で固定する．
既存[reviewed outbox](https://github.com/kirimine170/Karte/blob/0ff51235bf9682b241d8878d8c96b9448b9fc125/architecture/KARTE_EPHY_OUTBOX.md)はproposalを人間がaccept／編集／rejectし，canonicalへの保存をKarteが扱う境界である．
この保存判断を，Worker candidateのformal合格やskill採用へ読み替えない．
[halted report v0.1](https://github.com/kirimine170/Karte/blob/0ff51235bf9682b241d8878d8c96b9448b9fc125/architecture/KARTE_EPHY_EXPERIMENT_REPORT_V01.md)のfoundationは停止実験の証拠保存とproposal返却を行うもので，汎用の成功・lesson・skill schemaではない．
将来のlesson型は新しいversion／typeとして設計し，停止理由や既存v0.1の意味を維持する．

別の[synthetic producer v1](https://github.com/kirimine170/Karte/blob/0ff51235bf9682b241d8878d8c96b9448b9fc125/architecture/KARTE_EXPERIMENT_PRODUCER_V1.md)には`prepare`／`publish`／`status`とpendingへの配送が実装されている．
ただし`synthetic_only: true`を要求し，`state=experiment`，`verification=unverified`を固定し，report acceptance後も`adopted=false`である．
この既存adapterを「未実装」とは扱わず，実モデルからlessonを作りproductionで再利用する経路は，今回参照したsourceの範囲では成立を確認できていないと区別する．

[privacy／provenance policy](https://github.com/kirimine170/Karte/blob/0ff51235bf9682b241d8878d8c96b9448b9fc125/architecture/adr/ADR-0004-personal-context-privacy-provenance-policy.md)では`search`／`read`／`propose`／`review`／`export`／`learn`を別capabilityとして判定する．
`learn`は既定付与されず，readできる証拠をそのまま再利用lessonへ蓄積してよいという許可にはならない．
source provenanceは出典追跡であり，hashが一致しても本文は信頼済み命令ではない．
lessonの保存・export・学習利用には各操作のpolicyを再確認し，private証拠を公開skillへ持ち込まない．

将来のexporterはapproved lessonのdoc identity，bytes，policy／review version，適用条件をdeterministic snapshotへ固定する．
run開始時にsnapshotをpinし，実行途中のreview変更でcontextを差し替えない．
取消・権限失効は元sourceからderived lesson／export／cacheへ伝播させ，次のactivationとrunを拒否する設計が必要である．
実行中への影響は契約の停止条件で扱い，古い承認済みsnapshotを無期限の権限にしない．
candidate自身にevaluatorやactivationを作らせず，controllerが許可snapshotとtool ceilingを決める．

最初の検証はdeterministic exporterとsynthetic outbox adapterのoffline controlsで行う．
改変，review欠落，重複配送，revocation，private混入を拒否し，既存のreceipt／bundle bindingを維持する．
次に，未使用heldoutで「retrievalなし／review済みlesson retrieval」を同予算で比較する．
効果が確認できてから，同じlessonの自然言語skillと実行workflowを別variantとして比較する．
実行workflowにはOS隔離と副作用controlを追加し，lessonの文章上の有用性だけでcode実行を許可しない．

## Piを境界の明確なharnessとして使う

### Python RPC adapterを継続する

公式SDKはNode.js／Bun内でsessionを組み込む入口であり，Python controllerには既存RPC adapterが自然である．
将来のUI統合など，明確な要件が生じるまでは新しいSDK bridgeやagent loopを増やさない．
subprocess分離は言語間interfaceであり，OS sandboxの代替ではない．[SDK](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/sdk.md)，[Security](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/security.md)．

v1.1.0 RPCでは`prompt`へのsuccessは開始，queue，extensionによる処理を表し，Job成功ではない．
`agent_end`の後にもretry，compaction，follow-upがあり得るため，最終settledと独立checkerの結果を分ける．
既存通常adapterは既に`agent_settled`を待つが，今後のruntime更新は稼働binaryを固定し，旧・新event fixtureを通す互換性提案として扱う．
未知eventや不足した終端を成功へ補完せず，version既定更新やsilent fallbackを行わない．[RPC](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/rpc.md)．

adapterの受入条件は，LF区切りJSONL，request ID照合，stdoutの継続消費，stderrの別回収，EOF，invalid JSON，extension error，cancelを独立に検査することである．
待機timeoutと取消も分ける．公式[RpcClient source](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/src/modes/rpc/rpc-client.ts)では，`waitForIdle`／`collectEvents`のtimeoutはPromise拒否であり，自動abortの保証ではない．
controllerは所有Piへのabort要求と外側のdeadline，process回収を別々に記録する必要がある．

### Skillは必要な内容だけを，検証したidentityで渡す

[Agent Skills仕様](https://agentskills.io/specification)と[client実装ガイド](https://agentskills.io/client-implementation/adding-skills-support)は，metadataの広告，必要時の本文，追加resourceという段階的な読込みを示す．
通常作業ではname／descriptionから選び，必要な`SKILL.md`と参照fileを読む方式がcontext消費を抑える候補になる．
ただしmandatory governanceの全文配送とackは省略せず，「短くする」を根拠に契約本文を切り詰めない．

測定するvariantには，skill名だけでなく本文と参照resourceのraw-byte SHA-256，探索root，許可path，公開tool，読み込んだbytesを固定する．
discoverされたこと，metadataを見たこと，grepしたこと，全文がprovider contextへ配送されたことを別eventにする．
既存governanceの全文read判定を，単なる「読んだ」という回答で置き換えない．
optional resourceの追加にも同じcontext／time予算を使い，未宣言resourceや過大fileは拒否する．

最初のskill仮説は，長い万能手順を増やすことではなく，「境界条件を列挙し，変更前後の意味を比較する」という一つの短い手順でよい．
skillなし条件との比較で，完全計画，正答，request，tool往復，時間を別々に示す．
skill自己編集と採用判断を同じsessionで行わず，改善候補は未適用のままfreezeする．

### Tool／MCPはverifier隔離の後に限定導入する

現行[transfer tool](transfer-evaluation.md)はsource文字列のAST解析であり，file pathやnetworkを受け取らない．
このread-only補助をMCP化する前に，既存tool interfaceで情報の追加自体が有効かを調べる．
code候補の検証を安全に隔離できない間は，managed Markdown profileへtool／MCP実装候補を混入させない．

v1.1.0にはMCP接続があるが，CLIでbuiltin toolを絞る`--tools read`だけではMCP toolは除去されない．
configured serverはmodelのtool callより前に接続し得るため，将来のversion契約でdiscovery無効化または明示allowlistを検証する．
server，tool，schema，exposure mode，credential境界を明示的にallowlistへ固定する．
既存MCP adapterや`/mcp`で充足できる要件に重複clientを作らず，SDK利用時のfactory／extension bindingも別の互換性checkに含める．[Pi MCP](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/mcp.md)．

MCPのread-only等のannotationは非信頼serverではhintであり，権限の根拠ではない．
実際のtool実装，呼出し先，副作用，path／network制約をcontrollerで検査する．
schemaとprotocol revisionも固定し，追加toolの自動公開，副作用のある呼出しの自動retryを許さない．[MCP Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)，[ToolAnnotations](https://modelcontextprotocol.io/specification/2026-07-28/schema#toolannotations)．

## 同じ予算で改善を比較する

### 比較する変数と計測する費用を分ける

skill／toolの因果効果を測るときは，同じmodel，quantization，context，sampling，hardware，server，Pi，checker，taskを使い，variantだけを変える．
model比較の場合はmodel設定全体が差分であり，weightsだけの効果とは呼ばない．
各taskに同じtrial数を事前宣言し，実行順，warm-up／cache条件，concurrency，乱数設定を固定または均衡化して記録する．
予算を超えた候補に追加turnを与えて成功まで続ける比較は行わない．

| 計測項目 | 共通上限へ含めるもの | 別に保存する観測値 |
|---|---|---|
| generation request | ack，読取後の推論，計画，実装，retry，repair，要約・compaction | admitted，送信済み，完了，拒否，失敗 |
| token／context | policy，skill，tool schema／result，packet，履歴，最終回答 | input，output，cache，reasoningのprovider別定義と未知値 |
| tool利用 | discovery，read，補助tool，MCP往復と再試行 | toolごとの回数，bytes，時間，失敗，副作用 |
| wall time | queue，model load，tool，推論，検証，停止待ち | 全試行時間と成功試行時間 |
| optimizer | 仮説生成，candidate選択，試行の読込み，修正推論 | targetモデルとは別の費用台帳と全体合計 |

総output予算と1応答のcapは独立である．
総量が残っていても一つの完全な計画に必要な応答長が不足すれば失敗する．
反対に応答capだけを揃えても，request数や全体tokenが異なれば等予算比較にならない．
capを変更する実験ではcap自体を独立variantとして固定し，実行可能性の比較とskill効果の比較を混ぜない．
欠けたusageを0にせずunknownとし，必要な計測が欠ける比較はinconclusiveにする．

既存stage guardはadmitted generationを送信前に数え，残総量と応答capの小さい方をpayloadへ入れる．
報告usageの超過や`length`を停止条件とし，request境界のsynthetic transport controlがある．
health確認はgeneration requestとは別分類だが，service接触とwall timeには記録する．
benchmarkのbudget fieldはmetadataであり，この実行時強制の証拠へ代用しない．[Strata runtime](strata-proposal-runtime.md)，[benchmark](fixed-benchmark-comparison.md)．

### 予定した全試行を照合する

[benchmark code](../src/ephy_worker/benchmark.py)は提供record間のtask／repeat gridを検査する．
全variantから同じ失敗trialを落とすと検出できないため，coverageは`supplied_records_only`である．
事前の`task × variant × repeat × split`全件planをcontroller外置artifactとしてfreezeする．各rowは実行前に未開始とし，最終照合では完了，失敗，取消，未実行を明示したterminal記録を一つずつ対応させる追加gateを提案する．
欠落recordを成功・失敗へ推定変換せず，plan不一致として拒否する．

pipeline成功率，独立checker正答率，完全計画率，未実行validation，環境失敗，停止理由を別々に報告する．
cap失敗も分母に残し，truncationや例外を削除して成功へ置き換えない．追加人間指示と探索を含む費用も保存する．
環境失敗はpipeline分母に保持し，モデルの誤答とは区別する．
速い失敗をlatency改善に数えず，全試行と成功時の時間を並べる．
少数課題の偶然を総合点で隠さず，taskごとの結果と不確実性を示す．
反復数，成功条件，選択規則は結果を見る前に固定する．

## Held-outと来歴を評価の外側で管理する

既存3 development／3 heldoutは公開synthetic setであり，運用上の分割である．
controller用suiteにはgood controlが含まれるため，suite全体をmodelへ渡さず，task requestと許可sourceだけを渡す．
optimizerからheldout task，checker，正解control，結果を隠し，developmentで選んだ一つのcandidateのhashと選択規則をfreezeしてからheldoutへ進む．
その結果を見て選び直したcandidateには，新しい未使用評価集合が必要になる．[transfer評価](transfer-evaluation.md)．

公開課題をheldoutと呼ぶだけでは学習混入を排除できない．
OpenAIはSWE-bench Verifiedのtest設計とcontaminationの問題を報告しており，既知benchmarkの見かけ上の成功率だけに依存する評価は避ける．
まず既存synthetic setでinterfaceを確認し，その後に要求に対応した新規課題群をcontroller管理下で作り，splitと利用履歴を固定する．[OpenAI公式報告](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)．

最終candidateに対して，次の来歴をrunnerが観測し，modelの自己申告から作らない．

- base／source tree，完全patch，candidate snapshot，checker，suite，split，plan，選択規則のhash．
- policy，skillと参照resource，prompt，tool schema／実装，MCP設定，Pi，runner，依存lockのhash．
- 実model／server／設定／hardwareの観測identityと，証明できない範囲．
- roleごとのprocess／session，request，tool trace，meter，時刻，終了理由，workflow順序．
- 最終独立検証のpatch binding，外部review対象head，変更後に無効となった旧結果．

raw-byte hashとcanonical JSON hashの用途を分ける．
hash一致は与えられたbytesの一致を示すが，model weights，usageの真実性，metadataの正しさ，全試行の実在を単独で証明しない．
既存Strataの観測identityはprocess開始，実行binary／設定，API model／contextに結び付くdeploymentの証跡である．GPU内weightsやKV表現の証明ではなく，未取得のweight hashはunknownのまま保持する．
公開文書にはsanitizedな結論と検査範囲を残し，credential，個人情報，raw会話，私的log／bundle，local pathやservice情報を入れない．
必要な非公開証拠は既存のGit外保存方式に従う．[governance](system-development-governance.md)，[データ方針](security-and-data.md)．

## 完成性から反復変更へ評価を広げる

以下は追加の評価設計であり，今回実行したtestや新しいtrial予算ではない．
出力を短くしただけの候補から，実行の正しさ，別課題への転用，障害時の停止，変更を重ねる保守性へ段階的に進む．
上位段階の合格は，下位段階の証拠を省略する理由にならない．

### 失敗の結果と原因推定を分ける

| 分類 | 独立に照合する観測 | 分類から推定しないこと |
|---|---|---|
| transport failure | HTTP／RPC event，接続，EOF，schema，provider error | モデルが課題を理解できない |
| timeout | 元deadline，経過時間，取消要求，process／server停止確認 | token上限が原因だった |
| token exhaustion | finish reasonとusage，送信cap，server適用capの整合 | capを増やせば正答する |
| invalid syntax | 完全に保存した出力への固定parser結果 | parserを緩めれば内容も正しい |
| incomplete plan | 最終出力の必須field，要求との対応，未完marker | 非空の計画や長い文章なら完成している |
| invalid action | role／tool／引数／path／副作用の契約違反 | 結果がよければ違反が消える |
| executable incorrect | 外置checkerで要求挙動が失敗する | 環境不備や未実行testも誤答である |
| regression | baselineで通った挙動がcandidateで失敗する | 目標の修正成功だけで採用できる |
| verified success | 完成性，要求，回帰，scope，来歴の必要gateが最終patchに一致する | formal auditやmergeの別gateも通過した |

複数の失敗がある場合は，最初のstageと併発した事実を残す．
一次原因が不明ならunknownにし，agentが書いた原因説明から観測済みlabelへ昇格しない．

### 一回の正答から安全な反復へ進む

完成性checkerを通した後は，失敗を直す`FAIL_TO_PASS`と既存成功を守る`PASS_TO_PASS`の両方を固定する．
[SWE-bench公式harness](https://www.swebench.com/SWE-bench/api/harness/)の解決・維持の区別を小さなrepo課題へ適用する提案であり，SWE-bench全体を導入したという意味ではない．
同じ原因，template，派生修正をdevelopmentとheldoutへ分散させず，family単位でgroup splitする．
既存公開synthetic splitはそのままの意味で保持し，新しいgroup splitを成立済みと扱わない．

最終patchをcandidate外でcleanなbaseへ適用し，candidateが変更できないevaluatorで再検証する．
test skip，zero collected，assertion削除，偽exit 0，結果file偽造をnegative controlへ含める．
candidateとevaluatorを同じ探索で最適化せず，hidden結果を見て修正する回数も選択予算へ数える．
一つの課題の反復を別々の独立課題として数えず，task単位のpaired win／loss／tieと回帰を示す．

`pass@k`はk候補のうち少なくとも一つが正しい指標であり，`pass^k`は同じ課題のk回すべてで成功する信頼性の指標である．
後者は[τ-bench](https://arxiv.org/html/2406.12045v1)の考え方を参考にし，taskごとの反復結果から評価する．
全課題を混ぜた平均成功率のk乗で代用せず，失敗feedbackを使ったretryを独立sampleと呼ばない．
false-success，人間の追加指示，失敗も含む費用を並べ，探索費用と運用時費用，input／output／reasoningの既知・未知を分ける．
観測した事故が0件でも，未検査の障害や副作用が起こらない証明にはならない．

次の段階ではprovider切断，読取失敗，証拠欠落，重複結果，process中断，disk不足などのfault injectionを固定する．
想定した危険を検出してsafe stopできた場合は，停止したという理由だけでtest失敗にしない．
逆に危険状態のままcompletedへ進むfalse-successを拒否する．
その後，最初の成功実装へ仕様変更を順次与え，各段階の正答，回帰，scope，費用，変更来歴を検査する．
[SlopCodeBench公式source](https://github.com/SprocketLab/slop-code-bench)の反復仕様変更は設計の参考であり，ephyでの結果やlocal LLMの劣化率を示さない．
長時間動いたこと自体を難しい課題の成功と数えない．[METR公式説明](https://metr.org/time-horizons/)のtime horizonは人間の課題所要時間を基準とした難度であり，agentの連続稼働時間ではない．

## 長時間処理の復旧と資源境界

### 対話のcompactionとcampaign resumeを区別する

Piのsession永続化とcompactionは会話contextの継続機構である．
sessionを復元するときはSessionManagerの永続記録を正本とし，memory上のmessagesを書き換えただけで復旧したとはしない．
extensionの初期化・shutdownは重複しても安全である必要がある．[Compaction](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/compaction.md)，[Extensions](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/extensions.md)．

repoの[ephy-compaction-recovery](../tools/pi-local/ephy-compaction-recovery.ts)はLLMを呼ばないbounded checkpointと履歴抜粋を作る．
抜粋は切り詰め得るため，policy全文やimmutable evidenceを代替しない．
対話復帰ではGit状態と実diffを再確認する．
managed Strata profileではautomatic retry／compactionが無効に固定されており，対話用の復旧extensionをそのまま混ぜない．
将来要約推論を使う別profileを設計する場合も，そのrequestとtokenを予算へ数える．

campaign resumeは同じplanと保存されたverifier freezeを照合し，元の絶対deadlineを保持する．
中断Jobの実装，検証，監査を再実行せず，証拠を保持して失敗へ分類する．
特に中断したStrata Jobは基盤失敗としてcampaignを止める．
lockのmetadataは所有権ではなく，kernel-held lockが重複運転を拒否する．
freeze欠落を新しい環境で再測定して埋めず，完了campaignへtrialを追加しない．[campaign source](../src/ephy_worker/formal_campaign.py)，[Strata runtime](strata-proposal-runtime.md)．

### 資源の上限は外側で強制し，保証の強さを記録する

| 境界 | 現行確認事項 | 次に必要なcontrol |
|---|---|---|
| time／requests／output | formal stageとJobに有限上限がある | ack，tool，retry，要約を含む全経路で独立meterと照合 |
| RAM／disk／logs | formal runtimeはRSSと空きRAM／diskをpoll監視し，command captureのlog bytesを上限と比較する | 瞬間上限との違い，disk満杯，過大event，証拠保存失敗 |
| process | formal runtimeに所有process tree停止，通常RPCのPOSIXにgroup回収がある | 親先行終了，子孫残存，Windows通常RPC，各OSの取消 |
| tool／network／file | managed profileにroleとpath制約がある | code verifierのOS隔離，拒否された越境／通信の独立観測 |
| server推論 | client停止と応答capがある | server側取消の確認，確認不能時の残仕事の分類 |

通常RPCのWindows回収をformal runtimeと同じtree保証と見なさない．
また，Pi公式[bash tool](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/src/core/tools/bash.ts)には既定timeoutがなく，MCPのtimeoutはprogressで延長され得る．
toolごとのtimeoutだけに頼らず，外側のJob絶対deadline，出力bytes，tool-call数，process treeの境界を持つ．
OS container／sandboxの隔離controlを通るまで任意code候補を実行しない．

取消は，取消要求，Pi／tool processの停止，接続close，server推論の停止確認を別eventにする．
clientが閉じてもserver処理停止が未確認なら，停止済みと報告しない．
既存Strataをkill／reloadせず，新しいgenerationを止め，既存の応答capと契約期限を守る．
本調査は既存service，lock，停止条件，trial予算を操作する許可を追加しない．

## 既存Managerから限定executorへ広げる

[Phase 2実装](phase2-validation.md)には単一SQLite Manager，pull型Worker，attempt／lease token，stale result拒否，submit／resultの再送対策，残予算内の明示retryがある．
これらを新しい機能として提案せず，[Manager source](../src/ephy_worker/manager.py)と既存Job契約を再利用する．
現在の`web.collect` Workerはsearch／extractだけを担当し，明示`target_worker_id`へ割り当てる．
[WorkerService](../src/ephy_worker/worker_service.py)はconcurrency 1で登録・実行するが，登録schema全体が1だけを許すわけではない．
自動再割当や途中resumeは実装範囲外で，記録された単一PCのfixture合格は，異機種の実機分散運転を証明しない．

将来のexecutorは，research，coding，build，physicalを別kindとschemaで宣言する案とする．
generic shell commandをqueueへ入れず，kindごとに許可入力，実行binary／tool，filesystem／network，resource，副作用，取消・結果契約を固定する．
inference役とCPU verification役を分離する案も，必要能力をschemaへ表す設計として扱う．
現実の端末名・specによる配置は決めず，弱いnodeで全benchmarkを処理できるという容量保証も置かない．

lease tokenは古い結果の採用を拒否するが，lease失効だけでは外部推論や機器操作が停止しない．
再割当前に，旧attemptの停止確認，downstream fencing，operation単位のidempotencyをkind別に検証する提案とする．
受信側が古い世代の要求を拒否するという[Chubby論文§2.4](https://storage.googleapis.com/gweb-research2023-media/pubtools/4444.pdf)の原則を参考にし，Managerでの結果拒否とdownstreamでの動作拒否を区別する．
外部serviceや機器がfencingを受け付けない場合は，自動再割当せずunknown副作用として止める．
接続断後の結果を除外できても，重複した動作やresource消費が消えたとは報告しない．

既存Managerにはroot lineageでchild予約を数える境界があり，usageが不明なlost Jobのretryは残予算不明として拒否する．
将来の新executorにもこの方式を拡張し，予約を原子的に照合して元deadlineを維持する．
未知usageを0へ戻さず，予約を保持またはunknown消費として扱う．既存の残予算計算を未知消費の回復へ読み替えない．
network断，Manager再起動，lease切れ，遅延結果，重複副作用，budget同時予約をsynthetic fault controlsで検査してから実機評価を設計する．

framework導入より先に，現在のManagerでkindと境界を一つずつ確認する．
workflow／scheduler frameworkを使っても，sandbox，device fencing，server取消，予算meterの保証が自動で得られるとは扱わない．
[Ray公式resource説明](https://docs.ray.io/en/latest/ray-core/scheduling/resources.html#physical-resources-and-logical-resources)でもlogical resourceは主にschedule時のadmissionに使われ，実際のCPU／memory使用量の上限とは区別される．導入案や配置計画ではない．
この拡張は未実装の設計案であり，既存lock，試行上限，service設定，運転権限を変更しない．

## 研究から使う設計原則

[SWE-agent](https://arxiv.org/html/2405.15793v1)はtool interfaceと履歴処理がagentの振る舞いに影響することを比較する．
ephyではtool出力の形やskillを一つずつ変える仮説へ使えるが，GPT-4 Turboの数値をlocal LLMの改善量へ転用できない．
[Agentless](https://arxiv.org/html/2407.01489v2)のlocalization → repair → validationという固定段階は，探索的agentに対する小さいbaselineを設計する参考になる．
論文間の平均費用差は，同じモデル・予算での因果比較ではない．

[HarnessOpt-Bench](https://arxiv.org/html/2608.06301v1)のtarget model／environment／verifier固定，development／validation／test分離，外側のmeterとcandidate version管理を参考にする．
同論文のoptimizer推論はuncappedであるため，ephyの等予算評価にはoptimizerとtargetの別台帳および全体上限が追加で必要になる．
[Darwin Gödel Machine](https://arxiv.org/html/2505.22954v1)のcheckerを狙うobjective hacking例は，checkerの成功signalだけで採用しない理由になる．
失敗candidateも保存する原則は使うが，大規模な進化探索を現在の小さい検証基盤へ導入する根拠にはしない．

## 次に実装する小さな候補

以下は別々の変更仮説である．同じJobにまとめず，一つの固定scope，外置checker，negative control，独立検証で扱う．
優先するのは1と2のoffline作業である．trusted synthetic controlを超えて生成codeを実行する変更には，verifier隔離の確認を先行させる．
この文書は実装や追加live予算を承認せず，新しいformal契約または明示されたCodex作業scopeが必要である．
lesson exporter，障害・反復変更評価，分散executorは後続の独立仮説として扱い，以下の完成性と計測の候補へまとめて実装しない．

| 候補 | 仮説とscope | offline受入条件 | 将来のlive比較 |
|---|---|---|---|
| 1．全trial照合sidecar | benchmarkを再利用し，事前planと結果の全件照合だけを追加する | 全variantの同一trial欠落，重複，split／hash drift，未実行隠蔽を拒否し，既存集計を維持する | 実行前freezeしたmatrixと独立meterを使い，環境失敗も残す |
| 2．計画完成checker | plannerの最終出力に仮説，scope，禁止事項，受入条件，検証方針を要求する | ackだけ，readだけ，capで途切れた回答，field欠落を拒否し，短い完全計画を受理する | 同じ実modelと総予算で既存prompt対compact-outputを比較する |
| 3．Skill activation manifest | 宣言済みskillの広告・全文配送・参照resourceを区別するsidecarを追加する | hash drift，metadataだけの全文read主張，未宣言resource，過大入力，checker／他split混入を拒否する | none対短いskillをdevelopmentで比較し，一候補のfreeze後に未使用heldoutへ進む |
| 4．Pi取消・互換性control | 既存RPC adapterのlifecycleと所有process回収を検証する | fake Piでhandled，aborted，終端欠落，retry／follow-up／settled，過大出力，親先行終了，子孫回収を確認する | 許可した一つのversionでbounded pilotを行い，default upgradeを伴わない |

候補2の構造checkは計画の意味の正しさを証明しない．
現行planner-only経路の最終plan受入は非空であることを確認しており，構造・意味の完全性checkerが成立済みとは扱わない．[formal runtime source](../src/ephy_worker/formal_runtime.py)．
独立reviewで要求とplanの一致を点検し，実装後の課題正答は別checkerで測る．
PR24 packetを候補2へ組み込む場合も，merge／採用と互換性checkの後に独立variantとして固定する．
packetとcompact-outputを同時に変えて，どちらの効果か不明な比較を作らない．
候補4は静的に確認したcoverageの検査提案であり，handled／abortedによる実モデル障害が観測されたという主張ではない．
tool／MCP転用runnerの接続は，OS隔離，権限，外置checker，false-success controlを通した後の別候補とする．

将来のlive実行は，既存trialの再開や予約の流用で始めない．
source，モデル役割，実行環境，許可tool，全trial matrix，各上限，選択・停止規則を固定した新しい契約を作る．
offline controls → 許可されたbounded pilot → 同条件のdevelopment比較 → candidate freeze → 未使用heldout → 最終独立検証・reviewの順に進む．
pilotが基盤失敗ならrepairへ渡さず停止し，予算不足でも同じJobの上限を増やさない．

採用根拠は，最終candidateに結び付いた来歴，完全な試行記録，固定条件での実測，未使用課題への結果，現在headのCIと独立reviewである．
offline control，説明文，古いreview，実モデルの自己申告だけで改善や採用を宣言しない．
通常PRの作成後も，新しいpushは以前のCI／reviewを無効にし，mergeには別の明示判断が必要である．
