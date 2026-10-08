# Opt-in planner context packet

この補助packetは，controllerが明示選択した小さな全文をplanner promptへ追加する．既定では無効であり，`planner_context`を持たない契約のpromptと実行経路は従来どおりである．追加tool，retriever，model呼出し，実験予算，採用権限は導入しない．

既存governance gateは変更しない．policyのsystem配送，exact ACK，5文書の全文配送と検証を維持する．packet内の`already_delivered`は，その6文書のpath／SHA-256／byte数を参照するmetadataであり，要約でも代替配送でもない．rootのAGENTS.mdとREADME.mdは`additional_required`として両方の全文を配送する．task contextはcontrollerがpath，hash，byte数，`additional_required`または`optional`を明示したfileだけである．他の適用可能な必須readは引き続き必要であり，packetの選択は要件の完全性を自動判定しない．

## Frozen input

新しい契約にだけ，次のoptional fieldを含める．値は送信前にcontrollerが固定する．`source_revision`はJobの`baseRevision`とcandidate HEADに一致する40桁のcommitである．hashとbyte数はそのcommitのraw blobから取得する．以下は形だけの例であり，値を実fileへ合わせる．

```json
{
  "planner_context": {
    "version": 1,
    "source_revision": "0123456789012345678901234567890123456789",
    "max_packet_bytes": 65536,
    "files": [
      {"path": "AGENTS.md", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bytes": 1234, "category": "additional_required"},
      {"path": "README.md", "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "bytes": 2345, "category": "additional_required"},
      {"path": "docs/example.md", "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "bytes": 345, "category": "optional"}
    ]
  }
}
```

source scopeはrootのAGENTS.md／README.md，docs・.agents/skills・.pi/prompts配下のMarkdown，src/ephy_worker・tests配下のPythonに限定する．configs，credential directory，Git metadata，任意pathは読まない．Gitにcommitされたregular blobとcandidate raw bytesの一致を検査し，link／reparse point／hard link，path escape，重複path，未知field，invalid UTF-8，欠落，hash／byte数不一致を拒否する．既存全文配送の文書を再選択できない．

選択数はAGENTS／READMEを含め2–16個，各fileは1–32768 bytes，packetのUTF-8 JSON全体は明示上限以下かつ最大65536 bytesである．既存配送文書のmetadataとJSON escapingもpacket上限へ数える．全文はUnicode，BOM，改行を保持したJSON text fieldとして渡す．全文の要約，silent truncation，optional fileの黙示省略は行わない．一つでも不成立ならplanner stage前に`GateFailure`で拒否し，部分packetや架空の`NO_HYPOTHESIS`を返さない．packetはfile順序によらず決定的であり，Job directoryの`planner-context-packet.json`へ保存し，promptへSHA-256とserialized byte数を添える．

packet helperは既存のformal_runtime.py内に置き，既存のsealed bootstrapとcontroller pinを使う．追加moduleやlauncher権限は不要である．invocation identityは有効なpacket選択を追加でbindする．進行中またはfreeze済みのJob／trialへfieldを追加せず，新しい比較契約を別に固定する．

## Validation and limits

synthetic offline testsは，packet／promptの完全性，UTF-8，scope，source identity，サイズ，重複，欠落，determinism，stage前拒否，既定promptのbyte一致を検査する．これらは実modelの遵守，計画品質，性能向上を示さない．全文配送は入力tokenを増やす．model別token量，推論時間，request削減，成功率への影響は未測定であり，`model_token_impact`はnullで記録する．応答token capで計画が切れる失敗をpacketだけで解消すると主張しない．この差分は総予算も応答ごとの上限も変更しない．新しい比較予算はこの実装から認可されない．

既存captureのmetrics抽出は本差分に含めない．比較時は同じ固定task，総budget，応答token capで完了率，成功あたりrequest数，入力／出力token，推論と前後処理の時間を測定する必要がある．本実装は既存trial，Strata service，resource lock，旧captureを変更しない．
