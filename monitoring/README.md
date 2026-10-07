# 日次リソースレポート

Ubuntu ホスト上の systemd oneshot サービスです。常駐する監視プロセスは追加せず、ホストと VM の既存 sysstat 履歴を10分ごとに回収し、`df` によるローカルファイルシステムの残量・inode 使用率も保存します。ゲスト内の Python は SSH の標準入力で実行するため、ゲストへのファイル配置や追加パッケージは不要です。

## 前提と導入

- ホスト・ゲスト: Python 3、sysstat、10分間隔の `sysstat-collect.timer` が稼働していること。`sadf` の UTC JSON 出力を利用します。
- ホストの `mizu` ユーザーから各ゲストへ、既存 SSH 鍵と登録済み host key で接続できること。
- cp1 の既存 `kubectl` から `default/yb-tserver-0` の `yb-tserver` コンテナ内で `ysqlsh` を実行できること。
- 現環境の内部 YSQL はパスワード認証・TLS を強制していません。writer は既存の SSH / Kubernetes 管理接続を使います。YSQL の認証設定を変更した場合は、writer の認証も対応させてください。DB ポートをホストや LAN へ追加公開しません。

Ubuntu にこのディレクトリを転送してから、対象を確認して実行します。

```sh
cp config.example.json config.json
sh install.sh
systemctl list-timers 'resource-reports-*'
journalctl -u resource-reports-collect.service -u resource-reports-daily.service
```

`bootstrap.py` は専用 DB `resource_reports`、書き込み用・読み取り用の専用ロール、テーブル、Kubernetes Secret `resource-reports-reader` を作成します。パスワードは実行時に生成して直接 Secret に渡し、ソースやログへ出力しません。再実行では既存 reader Secret を使用します。既存アプリの Deployment はこのスクリプトでは変更しません。

## 保存と集計

- `/var/lib/resource-reports/samples.sqlite3` に最大約9日分の生データを保持します。権限は `mizu` のみ。DB の短い停止でも収集を続けます。
- 毎日 **00:15 Asia/Tokyo** に前日 00:00〜24:00 を集計し、YSQL `public.daily_resource_reports` に1サーバー1行を保存します。
- CPU 使用率は `100 - idle - iowait - steal`、平均は記録区間の秒数で重み付けします。I/O 待ちと steal は別項目です。最大値は記録区間平均の最大です。
- メモリ使用量は `MemTotal - MemAvailable`。平均・最大、最小利用可能量、スワップ最大使用量を保存します。ゲストのメモリ割り当てが変わらない現構成を前提に、取得時の MemTotal を sysstat 履歴に適用します。
- ディスクは `df` の一般ユーザー向け残量を使い、最新値・当日最小値・最大使用率・前日最後の計測との差を保存します。tmpfs、devtmpfs、squashfs、overlay、ネットワーク FS は対象外です。履歴がない日はディスク値を推定しません。
- 計測失敗したサーバーがあっても、他サーバーのデータは保存します。欠測は null / 収集率として表し、ゼロには置き換えません。
- 日次保存失敗は15分後に再試行します。保存完了していない直近7日の日付を再送し、主キー `(report_date, server)` への upsert で重複を防ぎます。
- DB は直近7つの完了日を保持し、8日以上前を日次処理で削除します。Web API も同じ日付範囲で絞ります。DB 停止中は物理削除が遅れる場合があります。
- 初回は既存の sysstat 履歴から取得できる期間を取り込みます。ディスク残量は導入時点からの記録です。

失敗した日の修正・再保存:

```sh
python3 /opt/resource-reports/monitor.py report --date YYYY-MM-DD --force
```

## 停止とテスト

```sh
sudo systemctl disable --now resource-reports-collect.timer resource-reports-daily.timer
sudo systemctl stop resource-reports-collect.service resource-reports-daily.service
cd monitoring
python3 -m unittest -v
```

停止してもローカル記録、DB、Secret は保持されます。SSH、DB 認証値、kubeconfig はリポジトリへ保存しないでください。
