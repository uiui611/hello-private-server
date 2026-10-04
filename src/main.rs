use std::{env, sync::Arc, time::Duration};

use async_nats::ConnectOptions;
use aws_config::{BehaviorVersion, Region};
use aws_credential_types::Credentials;
use aws_sdk_s3::{Client as S3Client, config::Builder as S3ConfigBuilder, primitives::ByteStream};
use axum::{
    Json, Router,
    extract::State,
    http::StatusCode,
    response::{Html, IntoResponse, Response},
    routing::{get, post},
};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::net::TcpListener;
use tracing::{error, info};
use uuid::Uuid;

const TEST_BUCKET: &str = "test";
const TEST_SUBJECT: &str = "test.codex-hello-server";
const MAX_CONTENT_BYTES: usize = 64 * 1024;

#[derive(Clone)]
struct AppState {
    s3: S3Client,
    nats_url: String,
    nats_username: String,
    nats_password: String,
}

#[derive(Debug)]
struct AppError {
    status: StatusCode,
    message: String,
}

impl AppError {
    fn bad_request(message: impl Into<String>) -> Self {
        Self { status: StatusCode::BAD_REQUEST, message: message.into() }
    }

    fn dependency(service: &str, error: impl std::fmt::Display) -> Self {
        error!(service, error = %error, "dependency operation failed");
        Self {
            status: StatusCode::BAD_GATEWAY,
            message: format!("{service} の操作に失敗しました"),
        }
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({ "ok": false, "error": self.message }))).into_response()
    }
}

#[derive(Serialize)]
struct ServiceStatus {
    ok: bool,
    detail: String,
}

#[derive(Deserialize)]
struct RustfsRequest {
    filename: String,
    content: String,
}

#[derive(Deserialize)]
struct NatsRequest {
    message: String,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let state = Arc::new(build_state().await.unwrap_or_else(|error| {
        error!(%error, "invalid application configuration");
        std::process::exit(1);
    }));

    let base_path = normalize_base_path(
        &env::var("APP_BASE_PATH").unwrap_or_default(),
    ).unwrap_or_else(|error| {
        error!(%error, "invalid application configuration");
        std::process::exit(1);
    });
    let routes = Router::new()
        .route("/", get(index))
        .route("/healthz", get(healthz))
        .route("/api/status", get(status))
        .route("/api/rustfs", post(test_rustfs))
        .route("/api/nats", post(test_nats))
        .with_state(state);
    let app = if base_path.is_empty() {
        routes
    } else {
        Router::new()
            .route(&format!("{base_path}/"), get(index))
            .nest(&base_path, routes)
    };

    let address = env::var("LISTEN_ADDRESS").unwrap_or_else(|_| "0.0.0.0:8000".into());
    let listener = TcpListener::bind(&address).await.expect("failed to bind HTTP listener");
    info!(%address, %base_path, "codex hello server started");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("HTTP server failed");
}

async fn build_state() -> Result<AppState, String> {
    let endpoint = required_env("RUSTFS_ENDPOINT")?;
    let access_key = required_env("RUSTFS_ACCESS_KEY")?;
    let secret_key = required_env("RUSTFS_SECRET_KEY")?;
    let region_name = env::var("RUSTFS_REGION").unwrap_or_else(|_| "us-east-1".into());
    let credentials = Credentials::new(access_key, secret_key, None, None, "rustfs-secret");
    let shared = aws_config::defaults(BehaviorVersion::latest())
        .region(Region::new(region_name))
        .credentials_provider(credentials)
        .load()
        .await;
    let s3_config = S3ConfigBuilder::from(&shared)
        .endpoint_url(endpoint)
        .force_path_style(true)
        .build();

    Ok(AppState {
        s3: S3Client::from_conf(s3_config),
        nats_url: required_env("NATS_URL")?,
        nats_username: required_env("NATS_USERNAME")?,
        nats_password: required_env("NATS_PASSWORD")?,
    })
}

fn required_env(name: &str) -> Result<String, String> {
    env::var(name).map_err(|_| format!("environment variable {name} is required"))
}

fn normalize_base_path(value: &str) -> Result<String, String> {
    let value = value.trim();
    if value.is_empty() || value == "/" {
        return Ok(String::new());
    }
    if !value.starts_with('/')
        || value.contains('?')
        || value.contains('#')
        || value.split('/').any(|segment| segment == "." || segment == "..")
    {
        return Err("APP_BASE_PATH must be an absolute URL path without . or .. segments".into());
    }
    Ok(value.trim_end_matches('/').to_owned())
}

async fn index() -> Html<&'static str> {
    Html(include_str!("../static/index.html"))
}

async fn healthz() -> Json<Value> {
    Json(json!({ "ok": true }))
}

async fn status(State(state): State<Arc<AppState>>) -> Json<Value> {
    let rustfs = match state.s3.list_objects_v2().bucket(TEST_BUCKET).max_keys(1).send().await {
        Ok(_) => ServiceStatus { ok: true, detail: "/test に接続できました".into() },
        Err(error) if is_missing_bucket(&error.to_string()) => {
            ServiceStatus { ok: true, detail: "/test は初回書き込み時に作成されます".into() }
        }
        Err(error) => ServiceStatus { ok: false, detail: format!("接続エラー: {error}") },
    };

    let nats = match connect_nats(&state).await {
        Ok(client) => match client.flush().await {
            Ok(_) => ServiceStatus { ok: true, detail: TEST_SUBJECT.into() },
            Err(error) => ServiceStatus { ok: false, detail: format!("flush エラー: {error}") },
        },
        Err(error) => ServiceStatus { ok: false, detail: format!("接続エラー: {error}") },
    };

    Json(json!({ "rustfs": rustfs, "nats": nats }))
}

async fn test_rustfs(
    State(state): State<Arc<AppState>>,
    Json(request): Json<RustfsRequest>,
) -> Result<Json<Value>, AppError> {
    let filename = validate_filename(&request.filename)?;
    if request.content.len() > MAX_CONTENT_BYTES {
        return Err(AppError::bad_request("内容は 64 KiB 以下にしてください"));
    }

    ensure_test_bucket(&state.s3).await?;
    state.s3
        .put_object()
        .bucket(TEST_BUCKET)
        .key(&filename)
        .content_type("text/plain; charset=utf-8")
        .body(ByteStream::from(request.content.clone().into_bytes()))
        .send()
        .await
        .map_err(|error| AppError::dependency("RustFS", error))?;

    let downloaded = state.s3
        .get_object()
        .bucket(TEST_BUCKET)
        .key(&filename)
        .send()
        .await
        .map_err(|error| AppError::dependency("RustFS", error))?
        .body
        .collect()
        .await
        .map_err(|error| AppError::dependency("RustFS", error))?
        .into_bytes();

    Ok(Json(json!({
        "ok": true,
        "path": format!("/test/{filename}"),
        "content": String::from_utf8_lossy(&downloaded),
        "bytes": downloaded.len()
    })))
}

async fn ensure_test_bucket(client: &S3Client) -> Result<(), AppError> {
    if client.head_bucket().bucket(TEST_BUCKET).send().await.is_ok() {
        return Ok(());
    }
    match client.create_bucket().bucket(TEST_BUCKET).send().await {
        Ok(_) => Ok(()),
        Err(error) if is_existing_bucket(&error.to_string()) => Ok(()),
        Err(error) => Err(AppError::dependency("RustFS", error)),
    }
}

async fn test_nats(
    State(state): State<Arc<AppState>>,
    Json(request): Json<NatsRequest>,
) -> Result<Json<Value>, AppError> {
    if request.message.is_empty() || request.message.len() > MAX_CONTENT_BYTES {
        return Err(AppError::bad_request("メッセージは 1〜65536 bytes にしてください"));
    }

    let client = connect_nats(&state).await.map_err(|error| AppError::dependency("NATS", error))?;
    let mut subscriber = client
        .subscribe(TEST_SUBJECT)
        .await
        .map_err(|error| AppError::dependency("NATS", error))?;
    client.flush().await.map_err(|error| AppError::dependency("NATS", error))?;
    client
        .publish(TEST_SUBJECT, request.message.clone().into())
        .await
        .map_err(|error| AppError::dependency("NATS", error))?;
    client.flush().await.map_err(|error| AppError::dependency("NATS", error))?;

    let received = tokio::time::timeout(Duration::from_secs(3), subscriber.next())
        .await
        .map_err(|_| AppError::dependency("NATS", "3秒以内にメッセージを受信できませんでした"))?
        .ok_or_else(|| AppError::dependency("NATS", "購読が終了しました"))?;

    Ok(Json(json!({
        "ok": true,
        "subject": TEST_SUBJECT,
        "message": String::from_utf8_lossy(&received.payload)
    })))
}

async fn connect_nats(state: &AppState) -> Result<async_nats::Client, async_nats::ConnectError> {
    ConnectOptions::new()
        .user_and_password(state.nats_username.clone(), state.nats_password.clone())
        .connection_timeout(Duration::from_secs(3))
        .connect(&state.nats_url)
        .await
}

fn validate_filename(value: &str) -> Result<String, AppError> {
    let value = value.trim();
    if value.is_empty() {
        return Ok(format!("{}.txt", Uuid::new_v4()));
    }
    if value.len() > 120
        || value == "."
        || value == ".."
        || !value.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
    {
        return Err(AppError::bad_request("ファイル名には英数字、ピリオド、_、- のみ使用できます"));
    }
    Ok(value.to_owned())
}

fn is_missing_bucket(message: &str) -> bool {
    message.contains("NoSuchBucket") || message.contains("NotFound") || message.contains("404")
}

fn is_existing_bucket(message: &str) -> bool {
    message.contains("BucketAlreadyOwnedByYou") || message.contains("BucketAlreadyExists") || message.contains("409")
}

async fn shutdown_signal() {
    let ctrl_c = async { tokio::signal::ctrl_c().await.expect("failed to install Ctrl+C handler") };
    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! { _ = ctrl_c => {}, _ = terminate => {} }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filename_cannot_escape_test_bucket() {
        for invalid in ["../secret", "dir/file", "/test/file", "a b"] {
            assert!(validate_filename(invalid).is_err());
        }
        assert_eq!(validate_filename("sample.txt").unwrap(), "sample.txt");
    }

    #[test]
    fn base_path_is_normalized_and_validated() {
        assert_eq!(normalize_base_path("").unwrap(), "");
        assert_eq!(normalize_base_path("/").unwrap(), "");
        assert_eq!(normalize_base_path("/codex-hello-server/").unwrap(), "/codex-hello-server");
        for invalid in ["codex-hello-server", "/../secret", "/app?debug=1"] {
            assert!(normalize_base_path(invalid).is_err());
        }
    }
}
