# 開発と運用

Rust 2024 edition の RustFS / NATS 動作確認アプリです。依存関係は `Cargo.lock` に固定し、Dockerfile と同じ Rust 1.98.0 で `cargo test --locked` を実行してください。

アプリは固定 bucket `test` と固定 subject `test.codex-hello-server` だけを利用します。認証値は環境変数で受け取り、Kubernetes では既存 Secret を参照します。秘密鍵、トークン、認証値、kubeconfig をコード・ドキュメント・ログに保存しないでください。

`codex-hello-server.yaml` は `default/codex-hello-server` の Deployment と Service を定義します。NodePort 30800、3 replicas、公開パス `/codex-hello-server` は現在の Nginx 構成に合わせています。変更時は YAML の前提条件と README も更新してください。クラスタを変更する操作はユーザーの依頼がある場合にだけ行います。

main のイメージを意図的に可変の `ghcr.io/uiui611/hello-private-server:main` として使用し、`Always` で取得します。GitHub Actions はビルド・公開後に OIDC webhook を一度通知し、通知失敗は警告として扱います。受信側の認証・妥当性検証を送信スクリプトへ重複させないでください。
