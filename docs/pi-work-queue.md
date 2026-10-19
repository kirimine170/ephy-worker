# Piの長い作業票を小さな実装単位で実行する

探索用campaign controllerは，schemaVersion 2の契約にある各taskのstepsを，固定順序の作業キューへ展開する．長い作業票の全体目的と共通制約を保持し，現在の小項目だけをgpt-ossの計画，Qwenの実装，固定check，gpt-ossのreviewへ渡す．実装は`tools/pi-local/ephy-campaign-guard.ts`にある．

## 契約

schemaVersion 1の既存契約は引き続き読める．schemaVersion 2では，rootに`goal`と`constraints`，各taskに`steps`を指定する．task本文は各stepの共通制約，step本文は一つの受入条件に対応する具体的な変更とする．分解は実行前に固定し，契約全体のSHA-256へ結び付ける．

```json
{
  "schemaVersion": 2,
  "goal": "二つのreport読込みを厳密なUTF-8にする",
  "constraints": "assertionとfixtureを維持し，未適用proposalで停止する",
  "tasks": [{
    "id": 1,
    "title": "report読込み",
    "instruction": "二つの受入条件を順番に完了する",
    "allowedFiles": ["tests/test_research.py", "tests/test_tavily.py"],
    "maxRepairs": 1,
    "checks": ["両方の条件を確認する固定command定義"],
    "steps": [{
      "id": "research",
      "title": "researchの読込み",
      "instruction": "対象関数のreport読込みへ厳密なUTF-8を指定する",
      "allowedFiles": ["tests/test_research.py"],
      "checks": ["この小項目の固定command定義"]
    }, {
      "id": "tavily",
      "title": "tavilyの読込み",
      "instruction": "対象関数のreport読込みへ厳密なUTF-8を指定する",
      "allowedFiles": ["tests/test_tavily.py"],
      "checks": ["この小項目の固定command定義"]
    }]
  }]
}
```

上記は構造の説明用であり，実行可能な完全な契約ではない．checkは`name`，`executable`，`args`，`timeoutMs`を持つobjectにする．完全な生成例は`tools/pi-local/prepare-campaign-test.mjs`を参照する．base，worktree，モデル，作業票hash，controller hash，全体上限，finalChecksなどの既存必須fieldも必要になる．

各stepのfile scopeは親taskの部分集合に限定する．空のsteps，重複ID，scope拡張，固定checkの欠落は起動前に拒否する．各stepへ実行用の連番IDを割り当て，元task IDとstep IDとの対応を保存する．親taskのchecksは最後のstepで実行し，全step終了後にはcampaignのfinalChecksも実行する．

## 進捗とcontext

`work-progress.json`へ全項目と状態を保存し，`campaign-state.json`へ現在位置，完了・未完了ID，実際に観測した編集数，復帰回数を保存する．モデルの完了申告だけでは先へ進まない．差分，check，reviewの全てが成立した小項目だけを`verified_and_reviewed`にする．

schemaVersion 2ではphase切替前の通常tool履歴を次の役割へ持ち越さず，現在のphaseのtool対話を維持する．元のuser指示，system指示，governanceの受領結果や必須bundleは保持する．compactionで切替境界が消えても，現在の作業，計画，完了ID，検証結果はcontrollerのstateから再配送する．編集・読込みの実測counterは進捗fileと復帰案内に保持し，毎回system prefixを書き換えない．

Piのcustom messageはproviderへuser messageとして変換されるため，状態説明を毎回tool結果の後ろへ置かない．送信直前に，元の最初のsystemメッセージを保持したうえで，現在の役割・作業・次の操作をその中へ加える．最終payloadの役割，公開tool，状態説明hashを記録する．この経路はOpenAI互換のmessages配列を持つproviderを対象とする．

各phaseの作業履歴の前へ，現在の役割と次のtool操作だけを示す短い引き継ぎrequestを置く．phase内に後から届くuserの再案内は，それ以前のassistant返答より後ろに保つ．全userメッセージを先頭へ集めて時系列を崩してはならない．最初の送信system／user promptを証拠directoryへ保存するため，そのdirectoryはprivateな実行記録として扱い，Gitへ追加しない．

`before_agent_start`でsystem prompt全文を固定しない．Pi自身によるtool説明の更新を保ち，計画後にedit/writeが公開されたことをモデルにも知らせる．

schemaVersion 2のreviewには，検証した累積diff，checkの実行結果とstdout／stderrを直接渡す．review役であることを先頭に明記し，`review_task`に`patchSha256`と`reviewedFiles`を要求する．送信payloadへ証拠が入ったこと，返答のhash・file集合，現在candidateのhashが一致しなければ先へ進めない．final checks後にもreview済みpatchとの一致を確認する．24 KiBを超えるreview証拠は切り捨てず停止する．累積diffも含む上限なので，大きな成果物はcampaign自体を複数proposalへ分けてから実行する．これは証拠の配送・同一性のgateであり，モデルの判断内容の正しさまでは保証しない．

## 有限回の復帰

- `maxReadOnlyToolCalls`：実際の変更がないまま続くread/searchの回数．
- `maxRepeatedReads`：同じtoolと引数による再読込み回数．
- `maxProgressRecoveries`：読込みループと変更ゼロ提出に対する再案内の合計上限．
- `maxProtocolCorrections`：実装中の重複start，既知の後続項目への先走り編集，review判定欄の省略に対する訂正上限．

回数を超えた操作には，実測進捗と現在の小項目を返す．上限を使い切れば失敗として保存・停止する．実際にfile bytesが変わった場合だけread/searchのカウンタを戻し，同じ内容のwriteでは戻さない．復帰回数は変更しても戻さず，次の小項目に進んだ場合にだけ初期化する．

計画のread/search上限に達したら，公開toolをcampaign controlへ絞り，計画提出へ誘導する．その後に禁止されたreadを実行しようとした場合は停止する．

現在項目に実編集があり，schemaVersion 2の後続項目にだけ許可されたfileへ書こうとした場合，その書込みは実行せず，現在項目の提出へ誘導する．訂正回数を使い切るか，キュー外のfileへ書こうとした場合は従来どおり停止する．後続fileの権限や完了扱いを先に与えることはない．

reviewの判定欄が省略された場合も，未承認のまま明示的なPASS／FAIL／INCONCLUSIVEを要求する．controllerがPASSを補完することはない．hash・file不一致や明示的な不合格はこの訂正で通さない．

## 検証と適用範囲

smoke testは，複数小項目の固定check・review完了，元taskとの対応，空提出後の実編集，compaction後の進捗保持，末尾tool結果の保持，最初のsystemメッセージへの配送，読込みcounterによるsystem prefix変更の抑制，review証拠の未配送・hash／file不一致の拒否，no-op writeによる上限回避の拒否を確認する．

実Pi用の準備scriptは，指定baseから新しいdetached worktreeと証拠directoryを作成し，controllerとchecker／probeの入力copyを保存する．既存の試行は再利用しない．二つの実在するUTF-8修正を含む長い作業票，stepごとの意味保持checker，3 caseのpytest，正常・不正UTF-8・semantic-negativeの最終probeを固定する．Windowsのlauncherは`-Headless`で同じPi経路をJSON modeで実行できる．

共有editable venvが管理側checkoutをimportしないよう，pytestの起動時にcandidateの`src`を`sys.path`と子process用`PYTHONPATH`へ固定し，実際の`ephy_worker.__file__`も検査・記録する．

この仕組みは固定済み小項目を実行する探索用controllerであり，任意の自然言語から漏れのない分解を自動生成する機能ではない．分解時に作業票の受入条件が全てstepsへ対応していることを確認する必要がある．同一session内のgpt-oss reviewはformalな独立監査ではなく，Pi process停止後の無人修復・再起動も対象外である．
