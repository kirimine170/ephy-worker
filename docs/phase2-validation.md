# Phase 2検証記録

## 実装範囲

契約0.4の`web.collect`，Local／Remote Collector，SQLite Manager，pull型Worker，lease，取消，明示retry，artifact転送，CLI操作を実装した．収集WorkerとManagerはmodel profileなしで起動できる．既存local researchは既定経路として残る．

## 2026-09-21に確認した範囲

- 環境：macOS，Python 3.14，単一PC．
- offline pytest：Manager core，HTTP認証，明示target，原子的claim，lease，取消，再送，artifact hash／scope，Manager再起動，Workerのsearch／extract，Local／Remote対応を確認した．
- `PYTHONPATH=src .venv/bin/python scripts/validate_phase2_processes.py`を実行した．Manager，model-less Worker，検証processの3 processを分け，loopback HTTPで登録，Job投入，claim，lease，search／extract，`result-pack.json` artifact，結果確定を確認した．両Jobは`completed`で，報告されたmodel requestは0だった．検索・本文は決定的fixtureであり，外部Webのlive検証ではない．
- 収集Workerのfixture実行ではmodel requestが0であることを確認した．
- Phase 1を含む全offline test，ruff，repository validationを実行対象とする．

## 未検証

- 物理的に異なる2台間の接続，TLS終端または暗号化tunnelの実機確認．
- Windows／Linux実機．CLIはPOSIX signalがない環境でも起動できるが，サービス停止操作は未検証．
- 実SearXNG／Tavilyを使ったremote検索と，実HTML／テキストPDFを含むremote researchのlive完走．
- Manager process停止中のWorker通信断からの運用復旧．自動再割当と中間resumeは実装範囲外．

同一PCの結果を2台実機合格とは扱わない．live検索の成功と，引用の実在，意味上の支持も別に判定する．
