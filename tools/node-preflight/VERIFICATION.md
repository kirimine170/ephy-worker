# 検証記録

## 対象

- 独立した `ephy-node-preflight` version 0.1.0
- upstream参照：`kirimine170/ephy-worker` commit `0f6c3cb7a272bc67e2a0a0dc828e579df6823d98`
- 確認日：2026-10-01 UTC
- 実行環境：Linux x86_64，Python 3.12.14，Python標準ライブラリのみ
- この記録は補助CLIの検証であり，ephy-workerの正式managed Pi Job，正式監査，review_ready，運用合格ではない

## 実行した確認

### Unit test

```text
python3 -B -m unittest discover -s tests -v
```

51 testsが通過した．今回のLinux実行ではskipなし．以下を含む．

- JSONの重複キー／NaN／不正型／過大／欠損／不整合値の拒否
- unavailable／unknown／not_checkedの区別，profile別要件，Pythonとpackage version境界
- synthetic sampleでhost probeを呼ばないこと，診断出力にpathを出さないこと
- POSIXのsource descriptor相対探索，symlink／FIFO拒否，容量上限，CRLF正規化，source差分
- Windows RAM API，Windows sampleのnative handle読取／reparse拒否，macOS RAM APIのmock
- Windows source検証のfail-closed，metadata Version欠落，引数エラーのredaction
- moduleにnetwork／subprocess／任意実行／書込APIを持ち込まない静的control

初期の自己checkでは，静的controlが無害な `platform.system()` を禁止APIと誤認して1件失敗した．ownerを識別するcontrolへ修正し，再実行した．検証側はこれを実装上のnetwork／process起動と扱っていない．独立レビューで指摘されたsource path探索のraceとmetadata欠落を修正し，関連testを追加した．

### 実際のLinux host probe

```text
python3 -B node_preflight.py --workspace . --worker-source ../ephy-source-reference --profile collect --profile test --profile render --format both
```

exit 1．失敗ではなく，前提不足を正しくblockした診断結果である．参照source directoryにはGitHub connectorで上記commitから取得した固定3ファイルだけを配置した．checkout全体や実worker実行の確認ではない．このsource本文は配布ZIPに含まない．

- OS：Linux x86_64
- Python：3.12.14
- 論理CPU／affinity：9／9．quotaは未確認
- RAM総量：約9.73 GiB．測定時の利用可能量：約7.89 GiB
- 測定workspaceの空き容量：約29.65 GiB
- git／uv／node／npm／pdftoppm／pdftotextはPATH上に存在．このCLIでは実行していない
- 固定3 sourceの正規化SHA-256は参照値と一致
- collect：pydantic-ai-slim／trafilaturaのmetadataがないためblocked
- test：上記に加えpytest／pytest-asyncio／ruffのmetadataがないためblocked
- render：ローカル前提の観測はpreflight_passed．実renderは未実行
- 全profileでdispatch_eligibleはfalse，advertised_capabilitiesは空

測定値は瞬間値であり，将来の空き容量，性能，利用可能量を保証しない．不足packageのインストールは行っていない．

### Sample／CLI確認

- `--help` を実行し，日本語の使用方法とexit codeを確認
- 同梱3 sampleを3 profile同時指定で実行し，3件すべてのJSONが `*.expected-report.json` と一致した
- sampleの実exit codeは，順に0／1／3だった
- distribution ZIPを新しい一時directoryへ展開し，manifestの全11ファイルのSHA-256を照合した
- 展開したZIPからunit test 51件を再実行し，すべて通過した．3 sampleの出力／exit codeも再び一致した
- 別の読み取り専用確認でも51 tests通過，依存範囲／source hash／契約参照の一致を確認した．指摘された問題の修正後に，残るblocking findingはなかった．これは正式managed Pi監査ではない

検証した `node_preflight.py` のSHA-256：`263a86b46120c77c25be4d5922c56f633090d9d19f3d2c5e486e7ae7eec6149a`．配布ZIPの各file hashは `MANIFEST.sha256` に記載した．manifest自身のhashは自己参照のため含めない．

## 未実行／未検証

- Windows／macOS実機上のCLIとtest
- 完全なephy-worker checkoutでのpytest，Ruff，`scripts/validate_repository.py`，正式governance workflow
- 全source commit identity，lockfile解決，packageのimport，tool version／正常動作
- Manager／search／LLM接続，認証，SSH／TLS設定，Worker登録，2台間の実Job
- PDF生成／画像render，GPU，モデル容量，sandbox，resource quota

repo validationはこの独立成果物のリポジトリ形式に対応しないため，合格済みと扱わない．未実行項目をsampleやmockで代替していない．
