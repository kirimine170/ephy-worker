[Ephy Worker managed Pi governance bootstrap v1]

このリポジトリの開発規約は，Policy ID `ephy.system-development-governance.v1`です．正本は`docs/system-development-governance.md`です．

managed Piの正式な実装または監査では，candidate外のrunnerが同policyのfreeze済みsnapshotを，SHA-256，末尾marker，session nonce，roleとともに`development_governance` system sectionへ注入しなければなりません．

最初に，注入されたpolicy全文とenvelopeを確認し，表示された値をそのまま`governance_ack` toolへ一度だけ返してください．正しいackとrunner指定contextの配送結果が受理されるまで，元の依頼への回答，書込み，shell，delegation，background submissionを開始してはなりません．受理後に`governance_ack`を再度呼びません．policy本文，SHA-256，末尾marker，nonce，role，`governance_ack` toolのいずれかが欠ける場合は，正式な作業として続行せず，不足項目を報告してください．

ackはpolicyの配送とidentity受領を示すだけです．各roleのscope，read-only制約，proposal-only境界，固定gate，独立性を解除しません．

END-OF-EPHY-PI-GOVERNANCE-BOOTSTRAP-V1
