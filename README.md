# hello-private-server

RustFS と NATS の動作確認用 Web アプリです。アプリと Kubernetes リソースの名前は `codex-hello-server` です。

- RustFS: 固定 bucket `test` へテキストを書き込み、直後に読み戻します。初回書き込み時に bucket を作成します。
- NATS: 固定 subject `test.codex-hello-server` を購読し、メッセージを publish して受信します。
- 日次レポート: Ubuntu ホストと VM の CPU・メモリ・スワップ・ディスク残量を日別に比較します。集計日は日本時間で、直近7日分を表示します。

保存先や subject を変更する API はありません。入力サイズは 64 KiB までです。NATS は Core NATS の publish/subscribe を利用し、JetStream の設定は変更しません。

## ビルドと自動更新

`main` への push で GitHub Actions が Linux amd64 イメージをビルドし、GHCR に `main` とコミット SHA のタグで公開します。pull request ではビルドだけを実行します。`workflow_dispatch` による main の手動実行も利用できます。

公開後に GitHub OIDC トークンで `https://deploy.mizu-mizu.info/v1/deployments` へ `hello-private-server` の更新を通知します。受信サービスが `default/codex-hello-server` の Pod を再作成し、各 Pod は `ghcr.io/uiui611/hello-private-server:main` を取得します。通知は一度だけ行い、失敗は警告として扱います。Actions は Pod の更新完了を待ちません。

GitHub Actions は `GITHUB_TOKEN` で GHCR に公開し、OIDC で通知するため、追加のリポジトリ Secret は不要です。初回は GHCR パッケージを public にし、受信サービスへこのリポジトリとサービス ID の対応を登録してください。

ローカルでのビルドとテストは次のとおりです。Dockerfile は Rust 1.98.0 を使用し、依存関係は `Cargo.lock` に固定しています。

```sh
cargo test --locked
docker build -t hello-private-server:local .
```

## Kubernetes

`codex-hello-server.yaml` は `default` Namespace に、3 replicas の Deployment と NodePort 30800 の Service を定義します。次の依存サービスと Secret を先に用意してください。認証値はリポジトリに保存しません。

| 依存サービス | Secret | 必須キー |
| --- | --- | --- |
| RustFS (`rustfs:9000`) | `rustfs-credentials` | `RUSTFS_ACCESS_KEY`, `RUSTFS_SECRET_KEY` |
| NATS (`nats:4222`) | `nats-auth` | `username`, `password` |
| YugabyteDB (`yb-tservers:5433`) | `resource-reports-reader` | `password` |

日次レポートには専用 DB `resource_reports` と SELECT のみを許可した `resource_report_reader` を使用します。[監視サービスの導入手順](monitoring/README.md)を先に実施してください。Web は `REPORTS_DB_HOST`、`REPORTS_DB_PORT`（既定5433）、`REPORTS_DB_PASSWORD` から接続します。`REPORTS_DB_HOST` が未設定の場合、既存機能は動作し、レポート欄には未設定の案内を表示します。DB 障害時もレポート API だけが503となり、プロセスのヘルスチェックには影響しません。

```sh
kubectl apply --dry-run=server -n default -f codex-hello-server.yaml
kubectl apply -n default -f codex-hello-server.yaml
kubectl rollout status deployment/codex-hello-server -n default
```

既存 Nginx 経由の URL は `https://ubuntu.home.arpa/codex-hello-server/` です。アプリ自体には認証がないため、NodePort はプライベート LAN で利用し、インターネットから直接到達できるようにしないでください。

マニフェストは `APP_BASE_PATH=/codex-hello-server` を設定しています。Nginx からもこのプレフィックスを保持して転送してください。別の公開パスを使う場合は環境変数とプロキシ設定を合わせて変更します。空文字または `/` ならルートで公開されます。

## API

- `GET <base-path>/healthz`: アプリプロセスのヘルスチェック
- `GET <base-path>/api/status`: RustFS と NATS の接続確認
- `GET <base-path>/api/reports`: 直近7つの完了日のレポート。固定範囲の読み取り専用 API（最大350件）です。
- `POST <base-path>/api/rustfs`: `{"filename":"hello.txt","content":"hello"}`
- `POST <base-path>/api/nats`: `{"message":"hello"}`
