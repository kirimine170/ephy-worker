# Changelog

All notable changes to this repository are documented in this file．

## Unreleased

### Fixed

- macOSでPi親processが先に終了した後も，同じprocess groupの子tool processをTERM／猶予後KILLで回収する．
- TavilyのResearcher accountで`paygo_limit=null`が返る場合の誤停止を修正．無料枠の残量検査を保持し，項目別診断と検索・モデル呼出しを行わない`doctor --usage-only`を追加．

### Added

- mac向けcodingの既定Pi backendとして，Runtime code endpointと同じllama.cpp profileを追加．共有routerで複数GGUFを選ぶprofileも追加．
- 未コミットのtracked fileと明示したuntracked fileを一時Git repositoryへcopyするworking-tree snapshot経路，source manifest，元checkoutを変更しない自己改善候補生成．
- candidate patch採用前にsource HEADとfile hashを再照合する`coding verify-source` CLI．
- local coding model向けPi custom provider設定，64 GB host用32K context profile，量子化を明示したmodel準備・評価手順．
- Pi headless RPC adapter，coding専用model profile／Job／result schema，detached Git worktree，独立validation，typed failure診断．
- model不要のdeterministic mock runner，6カテゴリのoffline coding smoke suite，CLI，patch／log／result artifact，timeout／cancel時の子process回収．
- Tavilyの無料プラン向け検索provider，事前使用量確認，basic固定・無料枠残量・最大10creditの制御，検証用設定とoffline試験．
- Worker Phase 1のdoctor／research CLI，専用lockfile，公開HTML／物理ページ単位PDFの取得・抽出・照合．
- 出典・引用実在・転載起点・矛盾を保持するJSON／Markdown成果物と，有限追加検索・Ctrl+C・timeout制御．
- 公開fetchのDNS固定・redirect検査・容量制限と，独立parserの時間・RSS制限．
- Offline fixture，CLI subprocess試験，実モデル接続と部分調査の検証記録．

- Initial language-independent Ephy repository template．
- Project metadata schema，initializer，validator，tests，and GitHub collaboration templates．
