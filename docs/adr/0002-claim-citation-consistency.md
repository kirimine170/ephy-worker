# ADR-0002：claimと引用の決定的整合性チェック

## Status

Accepted．2026-09-18のPhase 1後強化として実装した．

## Context

Phase 1のQwen補足実行の監査（[検証記録](../phase1-validation.md)の最終確認欄）で，意味照合が通ったclaimに2種類の繰り返し失敗があった．

- c001型：引用の数値を丸めて精度の違う値を混合する（例：総30.5B／アクティブ3.3Bの引用に対し，総30B・アクティブ3.3Bと主張）．
- c004型：比較表現を強化する（例：highly competitiveという引用に対し，同等または優れると主張）．

`apply_review`は既に引用の実在と照合IDを検査し，reviewがsupportsと条件一致を判定したevidenceをsupportとして数える．しかしclaim本文と引用本文の意味的一致さは同一モデルの別context照合に依存しており，その見逃しを決定論的に防ぐ手段がなかった．claimを黙って修正するのではなく，不確かな支持を降格させたい．

## Decision

`evidence.py`の`apply_review`で，reviewがsupportsとして条件一致を判定した各evidenceについて，claim本文と引用を2つの狭い決定的検査で照合する．

1. 数値の精度：NFKC正規化後，隣接する単位（B，M，％，億，万，倍など）を持つ数値を単位ごとに取り出し，同一の単位のあいだだけ桁列を比較して，片方向のdecimal prefix関係（`30B`対`30.5B`，`3.3B`対`3B`）にある場合，その引用をsupportとして数えない．claim側の数値の値と単位が引用にそのまま現れる場合は，他の数値が混ざっていても指摘しない．model名・version・日付混入を防ぐため，ASCII文字やhyphenに挟まれた数値，単位のない数値，ASCII単位の直後にASCII文字が続く表記（`3beta`，`3months`など）は対象外とする．
2. 比較表現の強さ：同等（2）・同等以上（3）・優位（4）の3段階の固定語彙（日本語と英語）で，claimの最大レベルが引用の最大レベルより高い場合，その引用をsupportとして数えない．ASCII語は一般的な屈折（outperforms，surpassedなど）も含む．同等または優れる・同等または優れているのような複合表現は完全形として登録し，全marker spanを収集したうえで最長一致で重なりを解決する．否定は選択された完全なspanにのみ適用するため，否定された複合表現（例：同等または優れるわけではない）は内包される同等・優れるを残さずlevel 0となる．否定は比較述語に直接付着した明示的な形の有限パターン集合だけ数える．英語は述語の直前にdoes not，did not，cannot，can not，never，not，no，without，n'tが続く形，日本語は述語の直後に〜ない・〜ません・わけではないなどが付着する形と述語の直前の非（例：非同等）．これは汎用の英語解析ではないため，no doubt ... outperformsやnot only outperformsのような肯定表現は否定と扱わない．単独の無・不も否定と扱わないため，無条件で上回るや不具合修正後は上回るのような肯定表現は誤検出しない．

検査が問題を検出してもclaimは書き換えない．該当evidenceのrelationをその場で`context_only`へ変更して背景として保存し，`evidence_id`付きの詳細を`check_reason`へ追記して`uncertainty`へ記録し，claimの`reason`へ検査の追記を行う．支持がすべて除外された場合は`insufficient`へ降格し，一部だけが問題のある場合は残りの支持でclaimを維持できる．検査はclaimごとのevidence単位で適用する．

検査は`tests/test_evidence.py`で，検出される失敗形状（c001型・c004型）と，忠実な数値・比較表現・否定文・model名・日付を誤検出しない偽陽性保護をoffline testとして固定した．

## Consequences

- 数値の丸め・精度付けと，観測語彙内の比較強化は，意味照合の見逃しに依存せずにsupportから除外される．claimの降格は残りの有効な根拠がその状態を満たさなくなった場合にのみ発生する．
- 検査は観測された失敗形状のみに限定するため，異なる値（95対90），条件・benchmark範囲の省略，言い換えによる強化は検出しない．これらの最終確認には同一モデルの別context照合と人の再確認を続ける．
- 比較語彙と単位集合は固定リストであり，リスト外の表現（例：space区切りの英語数量詞）は対象外になる．必要になったら語彙を拡張し，その際は偽陽性保護テストを併せて追加する．
- `check_reason`，`uncertainty`，`reason`への追記と，却下evidenceの`context_only`保存により，降格の根拠はreportへ明示される．確認済みclaimの結論行・検証対象行の無ラベル引用は，その状態を満たすevidence（照合済み・条件一致のsupports，refutedならcontradicts）のみを列挙し，却下evidenceは詳細evidence一覧の`背景のみ`ラベルと照合理由で表示され続ける．既存のschemaとreport形状は変えない．
- live経路での効果は再実行による確認が未実施であり，本決定はoffline testの合格までを示す．

## Alternatives considered

- 意味照合の強化（より厳しいprompt／多context投票）：決定論的でなく，コストと非再現性が増えるため，今回の狭い失敗形状への第一手段としては不採用．
- claim側の数値を引用へ「修復」する：黙ってclaimを書き換えることは禁止された方針に反するため不採用．
- 汎用の数値・比較NLP解析：範囲が広く偽陽性の制御が難いため，観測形状のみの安全な部分集合に縮小した．
- 単位のない数値も対象にする：日付・version・裸整数の偽陽性が大きいため，単位必須とした．

## Related repositories

実装変更は`ephy-worker`のみ．Runtime／Karteへの変更とrepo間の依存追加はない．

## Date

2026-09-18．
