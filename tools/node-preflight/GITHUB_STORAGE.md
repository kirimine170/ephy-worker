# GitHub保管版の位置付け

このdirectoryは，既に作成した独立版 `ephy-node-preflight` 0.1.0のsourceをGitHubへ一元保管するためのものです．**未検証・採用不可のDraft PR**として公開し，通常のWorker実装や正式なself-improvement candidateへの採用を意味しません．

今回に限り，正式な検証・監査が未完了でも保管／レビュー用Draft PRへ置くことについて，利用者の明示的な承認を得ています．governance本文，既存のquality gate，Worker API，config，依存関係，CIの定義は変更しません．`review_ready`，`external_pr_review_ready`，formal audit完了を主張せず，merge／release／deployはこの保管作業の対象外です．

## 保存した内容

- 最初の保管commitでは配布済み独立版をbyte単位で保存しました．その後，Codex ReviewのP1に対応し，Python prereleaseの判定，static controlのimport／call allowlistと読取専用open判定，回帰test，関連説明を限定修正しています．合成sampleと期待JSONは変更していません．
- `MANIFEST.sha256` は，元配布版と同じ11ファイルについて，現在の修正後bytesを照合するhashです．この追加説明はmanifestの対象ではありません．元配布版のidentityは以下のSHA-256と最初の保管commitで保持します．
- `SCOPE.json` のpublicationと `VERIFICATION.md` は，GitHub移送前の独立版を作成・検証した時点の記録です．現在の保管場所と確認対象headは，このDraft PRとGitHubのcommitを参照してください．
- ZIP，Python cache，実行バイナリ，実端末のreport，credential，個人情報，会話記録は追加していません．sampleは合成データです．
- 元CLIのSHA-256：`263a86b46120c77c25be4d5922c56f633090d9d19f3d2c5e486e7ae7eec6149a`．
- 元配布ZIPのSHA-256：`08c12c38a0c997078e2fc4790a746869f605776a93085dcc464c4b120f8212d0`．ZIP本体はGitへ保存しません．
- 移送base：`0f6c3cb7a272bc67e2a0a0dc828e579df6823d98`．既存のresume修正PRとは別のbranch／PRです．

## repository内からの再現手順

repository rootから実行します．追加packageのインストールは不要です．

```sh
cd tools/node-preflight
python3 -B -m unittest discover -s tests -v
python3 -B node_preflight.py --sample samples/prerequisites-present.json --profile collect --profile test --profile render --format json
python3 -B node_preflight.py --sample samples/missing-dependencies.json --profile collect --profile test --profile render --format json
python3 -B node_preflight.py --sample samples/windows-unverified.json --profile collect --profile test --profile render --format json
```

sampleの期待exit codeは順に `0`／`1`／`3` です．出力JSONは対応する `*.expected-report.json` と比較できます．Windowsでは同じdirectoryで `python3` を `py -3.12` へ置き換えます．

repository rootでは既存の検査を実行します．

```sh
python3 -B scripts/validate_repository.py --check-sensitive-patterns
```

rootの既存pytest設定は `tests/` を対象とするため，この独立CLIの59 unit testは上記commandで別に実行する必要があります．既存CIは変更していません．

## 検証と未確認の境界

- Linux cloud／Python 3.12.14で，この独立CLIの59 unit testと，合成sample 3件のJSON・exit codeを再確認しています．alpha／beta／candidateの誤通過を拒否し，final releaseの挙動を保持します．固定import／call allowlist，from-import source，alias，open mode／flagsを検査し，HTTP通信，process起動，書込／削除／touch等の不正controlを実行せずASTで拒否します．
- GitHub移送先のbaseと追加後のsnapshotに対し，既存repository validatorとsecret-pattern scanを実行しています．
- Windows／macOSはAPI mockによるunit testのみで，実機確認ではありません．Windowsのsource attestationは意図的に `unknown` です．
- full Worker regression，実Worker登録／Job，2台間通信，formal Pi workflow gate，正式な独立監査は，独立CLIのtest結果で代替しません．
- PRのCIとCodex Reviewは，その時点の正確なheadで別に確認します．新しいpushがあれば以前の結果を新headの合格根拠にはしません．

詳しい利用方法と制約は [README](README.md)，独立版作成時の記録は [VERIFICATION](VERIFICATION.md) を参照してください．
