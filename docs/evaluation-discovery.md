# Evaluation Discovery の offline 契約

`evaluation_discovery.py` は，既存 Research Executor が保存した Report 0.1 の実 bytes と，呼出側が明示する claim ID の選択を，未採用の評価候補へ正規化する純関数 adapter です．既存の検索，取得，本文抽出，意味照合を再実装しません．この段階は合成 Report による検証だけを対象とし，実モデルの改善や実際の候補の有効性を示しません．

## API

```python
from ephy_worker.evaluation_discovery import normalize_evaluation_candidates

result = normalize_evaluation_candidates(
    report_bytes,
    expected_report_sha256=expected_hash,
    expected_job_id=expected_job,
    selections=[
        {
            "candidate_id": "parsing-evaluation",
            "claims_by_field": {
                "name": ["c001"],
                "evaluation_method": ["c002"],
                "applicability": ["c003"],
            },
        }
    ],
)
canonical_bytes = result.output_bytes
canonical_bytes_sha256 = result.sha256
output = result.to_dict()  # 独立した可変 copy
```

`report_bytes` は実際に読み込んだ UTF-8 JSON の `bytes`，hash はその bytes の小文字 SHA-256 です．再serializeした Report の hash へ置換しません．この API は IO を行わず，ファイルの存在や永続化を保証しません．呼出側が後で保存する場合は，保存先から実 bytes を再読込し，`result.sha256` と一致することを検査する必要があります．戻り値の hash は canonical output bytes の hash であり，将来の保存済み artifact の検証済み hash ではありません．

`expected_job_id` と Report の job ID は完全一致を要求します．`schema_version=0.1`，`workflow_version=phase1-v1`，状態 `completed` または `partial` のみを受け付けます．`Report.model_dump(mode="json")` が保存する全 field を，子 model の default field まで明示した表現を要求します．欠落した日時や metadata を schema default から補いません．既存 prose validator が値を変更する非正規化入力も拒否します．JSON の空白や key 順序は自由ですが，hash はその実 bytes に結び付きます．

`CandidateSelection` は strict schema です．候補 ID と既存 claim ID の選択だけを受け取り，任意の metadata 値は受け取りません．重複候補 ID，空の選択，不明 field，不明／重複 claim ID は拒否します．同じ claim を複数の field に明示的に選択することは可能です．

## 候補と不明値

出力契約は `ephy.evaluation-discovery.v1` です．候補と全体の状態は常に `proposed` です．候補は次の全 field を持ちます．

| Field | 内容 |
| --- | --- |
| `name` | 候補を記述した claim |
| `version` | 対象版を記述した claim |
| `license` | license を記述した claim |
| `applicability` | 適用条件を記述した claim |
| `evaluation_method` | 評価方法を記述した claim |
| `required_resources` | 必要資源を記述した claim |
| `existing_implementation_delta` | 既存実装との差分を記述した claim |
| `improvement_hypothesis` | 改善仮説を記述した claim |
| `unresolved_matters` | 未確認事項を記述した claim |

各 field の `value` は，選択された `Claim.text` の**全文**の配列です．部分抽出，書換え，推測はしません．未選択 field は `value=null`，`status=unknown`，`reason_codes=["not_selected"]`，空の claims になります．支持されない選択は削除せず，全文，条件，不確実性，元の状態と理由を `unresolved` として保持します．version や license がない場合に既定値を補いません．

`report_supported` は保存済み Report の claim に対する限定的な支持検査を通過したという意味です．呼出側がその claim を適切な metadata field に分類したこと，候補への関連性，license 適合性，安全性，評価妥当性，改善効果を証明しません．`field_assignment_status=caller_selected_not_semantically_verified` を明示し，分類と適用判断は人または独立評価へ残します．同様に Report hash は bytes identity を示すだけで，Report の作成者や起点の真正性を認証しません．

## 根拠 binding と支持検査

各選択 claim は Report SHA-256 と source／passage／claim／evidence ID に結び付きます．binding は passage と quote の UTF-8 bytes から実計算した SHA-256，文字 offset，page，引用全文，条件，source の読取制約を保持します．offset は既存 Report と同じ Unicode 文字 offset であり，UTF-8 byte offset ではありません．`quote_start/end` は passage の `start` を基準に本文へ対応させます．`whitespace_normalized` の印があっても，保存された quote は元 passage の exact slice と一致しなければなりません．

`Source.body_hash` は取得後に抽出した全文 text の UTF-8 hash として宣言された値です．完全な抽出全文 bytes は入力されないため，adapter はその hash を検証しません．`declared_source_body_hash` と `source_body_hash_status=unverified_complete_extracted_body_not_supplied` として明示し，passage hash と区別します．Source の URL などの未選択 metadata は Report ID と bytes hash を介して参照できます．新たな URL 取得はありません．

既存 `group_origins` が同じ URL，宣言された本文 hash，共有 origin URL から同一起点とする資料を，別々の confirmed group とする場合は，該当する corroboration を `known_shared_origin` で未解決にします．異なる URL や hash だけから独立性を新たに認定しません．

構造検査は全 Report を対象とし，ID の一意性，source と passage の所有関係，read membership の参照整合性，evidence の単一 claim 所有，origin membership，quote の実在と区間・page を確認します．構造上壊れた Report は部分的に採用しません．

選択 claim の支持には，さらに source 状態 `ok/partial`，実際の read membership，claim と evidence の checked flag と理由，`conditions_match=true`，一次性の明示，支持された元の claim 状態を要求します．これだけでは支持とせず，copy に対して既存 `evidence.apply_review` を再利用し，支持／反対関係，条件，primary／独立起点による corroboration，数値精度と比較表現の既存 guard を適用します．元の `corroborated` が `supported_primary` へ降格する場合も，強い元の状態を黙って置換せず未解決理由を残します．条件不一致や未読などが一つでもある選択 claim は保守的に未解決とします．

元の `conflicting/refuted/insufficient/inference` を支持へ昇格しません．replay は新しい意味照合ではなく，保存済み review の構造と既存の決定的 guard の再確認です．意味照合が見逃した誤りや偽造された Report の真正性を判定する機能ではありません．

## 安全境界とエラー

`DiscoveryInputError.code` と例外文字列は固定 code のみです．JSON 重複 key，NaN／Infinity／指数 overflow，invalid UTF-8／surrogate，型 coercion，不明 schema，欠落 field，hash・job 不一致，曖昧な ID／所有関係，不正引用，無効な選択を fail closed で拒否します．入力の私的 path，log，validation error 全文を例外文字列へ転記しません．

`Report.gaps` は `information_gaps` として原文を保持するだけです．抽出・照合の不足や引用検証エラーを含み得るため，能力不足，capability gap，実装欠陥へ読み替えません．出力自体にも私的な入力が含まれ得るため，公開用 sanitizer として使わないでください．検索 query の生成・外部送信経路はありません．

この module は外部検索，追加モデル呼出し，未知 source のコード実行，install，MCP 有効化，候補採用を行いません．JobStore／ResearchExecutor／GUI／Strata と接続しません．P0 と Gap の契約・検証が成立する前の運用接続は範囲外です．出力は決定的な canonical JSON（key sort，compact separator，UTF-8，末尾 LF）であり，入力を変更しません．合成テストの合格は自己検証の結果であり，独立監査や実モデル改善の合格ではありません．

## 合成検証

`tests/test_evaluation_discovery.py` は提案・未知 metadata・全文 binding・hash・不変性の正常系に加え，偽造 checked，改変 quote，重複 ID，evidence 共有，未読／条件不一致，conflicting／refuted，primary／corroboration，既存数値／比較 guard，field 間の根拠混同，schema coercion，重複 JSON key，非有限値，私的入力を含む安全な例外の負例を検査します．実検索，実モデル，実 dataset，改善効果の評価は行っていません．
