# GitHub保管版の位置付け

このdirectoryは，既に作成した独立版 `ephy-node-preflight` 0.1.0のsourceをGitHubへ一元保管するためのものです．[PR #11](https://github.com/kirimine170/ephy-worker/pull/11)は，当初**未検証・採用不可のDraft PR**として公開し，その後，利用者が指定したCodex Review／P0・P1修正／CIの経路を経てsquash mergeされました．独立した補助CLIであり，Workerの登録能力や正式なself-improvement workflowの採用を意味しません．

利用者が保管／レビュー用Draft PRの公開と，Codex Review／P0・P1修正／CI後のmerge操作を明示的に承認したことは，実施された操作の事実として記録します．一方，[governance正本](../../docs/system-development-governance.md)の `merge_ready` は，明示的な人間の承認に加えて `review_ready` または `external_pr_review_ready` の成立を必要とします．ユーザー承認やCodex Review／CIの成功だけでは，`pre_audit_workflow_gate` を含む技術的・手続的readinessを満たしません．

PR #11と来歴文書修正[PR #12](https://github.com/kirimine170/ephy-worker/pull/12)のmerge時には，正式Pi workflow／`pre_audit_workflow_gate`／formal auditを完了しておらず，`review_ready`／`external_pr_review_ready` も成立していませんでした．したがって，これらのmergeはrepositoryの `merge_ready` 条件と採用契約を満たさない **governance deviation（手順逸脱）** として記録します．ユーザー承認によってquality gateやpermission boundaryに例外が成立した，または過去のmergeがgovernanceへ適合したとは主張しません．policy本文と既存gateは変更していません．Worker API，config，依存関係，CIの定義もこの補助CLIの保管では変更していません．release／deployは実施していません．

## 保存した内容

- 元配布版から，Codex ReviewのP1に対応し，Python prereleaseの判定，static controlの閉じた構文／import／member／call引数とreceiver契約，再束縛拒否，読取専用open判定，回帰test，関連説明を限定修正しています．合成sampleと期待JSONは変更していません．archiveとGit履歴の対応は次節に記載します．
- `MANIFEST.sha256` は，元配布版と同じ11ファイルについて，修正後bytesを照合するhashです．この追加説明はmanifestの対象ではありません．元配布版のidentityは以下のSHA-256で区別し，現在のsourceのhashやtest結果として流用しません．
- `SCOPE.json` のpublicationと `VERIFICATION.md` は，GitHub移送前の独立版を作成・検証した時点の記録です．元CLIの51 testと，GitHub修正後の65 testは対象bytesが異なります．現在の保管場所，変更履歴，確認対象commitは以下と各PRの記録を参照してください．
- ZIP，Python cache，実行バイナリ，実端末のreport，credential，個人情報，会話記録は追加していません．sampleは合成データです．
- 元CLIのSHA-256：`263a86b46120c77c25be4d5922c56f633090d9d19f3d2c5e486e7ae7eec6149a`．
- 元配布ZIPのSHA-256：`08c12c38a0c997078e2fc4790a746869f605776a93085dcc464c4b120f8212d0`．ZIP本体はGitへ保存しません．

## archive，旧branch，squash mergeの対応

- 独立版のupstream参照点と旧保管branchの開始baseは `0f6c3cb7a272bc67e2a0a0dc828e579df6823d98` です．これはPR #11の最終baseやmain上のsquash commitの親を表しません．
- [旧branchの最初の保管commit](https://github.com/kirimine170/ephy-worker/commit/c982844d265fc7db90f40985bcaaca47b617e613)は `c982844d265fc7db90f40985bcaaca47b617e613`，その親は上記 `0f6c3cb7a272bc67e2a0a0dc828e579df6823d98` です．元配布ZIPの12ファイルは，このcommitの `tools/node-preflight/` 内の対応ファイルとbyte単位で一致します．追加した本書はZIPに含まれません．この照合はarchiveの来歴を示し，現在のmain ancestryや修正後sourceの検証を代替しません．
- PR #11の最終baseは `146012483e63de19f97c655b730978221604de32`，[旧branchの最終head](https://github.com/kirimine170/ephy-worker/commit/005b8c141abb402073e3d5165b19c62cbc0c7b44)は `005b8c141abb402073e3d5165b19c62cbc0c7b44` です．旧branch側の保存／修正commit群は，このheadとPR履歴で参照する過去の記録です．
- mainへ入った実際の[squash merge commit](https://github.com/kirimine170/ephy-worker/commit/75e954512404107ca1b992fba3238a5b8f67119c)は `75e954512404107ca1b992fba3238a5b8f67119c`，唯一の親は `146012483e63de19f97c655b730978221604de32` です．旧branchの保存／修正commit群は，このsquash commitの祖先には含まれません．
- 旧最終headとsquash mergeのrepository treeは，ともに `9e7ce9c1eb19c5a277f792a0c5f1a6697cd2d12f` です．同じsnapshotである根拠はtree identityであり，commit identityや親子関係の同一性ではありません．merge時CLIのSHA-256は `61933858d57cfe587d039126a133d6679b51ca6bb193681d0525a424101f04fd` です．
- 後続PRのbase／head／diff／CI／reviewは，そのPR自身に結び付けて確認します．上記archiveや旧headの検証結果を，新しいheadの合格証拠として読み替えません．

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

rootの既存pytest設定は `tests/` を対象とするため，この独立CLIの65 unit testは上記commandで別に実行する必要があります．既存CIは変更していません．

## 検証と未確認の境界

- PR #11の旧最終head `005b8c141abb402073e3d5165b19c62cbc0c7b44` で，Linux cloud／Python 3.12.14による独立CLIの65 unit testと，合成sample 3件のJSON・exit codeを確認しました．alpha／beta／candidateの誤通過を拒否し，final releaseの挙動を保持します．固定された構文，import／member／call allowlist，from-import，callable再束縛，callback値，receiver binding，open mode／flagsの変更を検査し，HTTP通信，process起動，書込／削除／touch等の不正controlを実行せずASTで拒否します．CLIが使わないdecorator／metaclass／動的構文は許可しません．
- 旧最終headの[CI](https://github.com/kirimine170/ephy-worker/actions/runs/36888635393)と，squash merge `75e954512404107ca1b992fba3238a5b8f67119c` の[post-merge CI](https://github.com/kirimine170/ephy-worker/actions/runs/36890656405)は成功しています．repository validator／secret-pattern scanを含みます．この2つのcommitを区別して記録します．
- macOSは独立版作成時にはAPI mockのみでしたが，squash merge `75e954512404107ca1b992fba3238a5b8f67119c` で[初回実機CLI／test結果](https://github.com/kirimine170/ephy-worker/pull/11#issuecomment-5935971232)を追加確認しました．Windows実機は未確認で，Windowsのsource attestationは意図的に `unknown` です．
- full Worker regression，実Worker登録／Job，2台間通信，formal Pi workflow gate，正式な独立監査は，独立CLIのtest結果で代替しません．
- PRのCIとCodex Reviewは，その時点の正確なheadで別に確認します．新しいpushがあれば以前の結果を新headの合格根拠にはしません．

詳しい利用方法と制約は [README](README.md)，独立版作成時の記録は [VERIFICATION](VERIFICATION.md) を参照してください．
