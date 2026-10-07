use std::{env, sync::Arc, time::Duration};

use axum::{Json, extract::State, http::StatusCode};
use serde_json::{Value, json};
use tokio_postgres::{Config, NoTls};

use crate::{AppError, AppState};

// Fixed scope: no caller-controlled SQL, database, server name, or date range.
const REPORT_QUERY: &str = "SELECT report FROM public.daily_resource_reports \
    WHERE report_date >= (now() AT TIME ZONE 'Asia/Tokyo')::date - 7 \
      AND report_date < (now() AT TIME ZONE 'Asia/Tokyo')::date \
    ORDER BY report_date DESC, server ASC LIMIT 350";

pub(crate) fn configuration() -> Result<Option<Config>, String> {
    let Ok(host) = env::var("REPORTS_DB_HOST") else {
        return Ok(None);
    };
    if host.trim().is_empty() {
        return Err("REPORTS_DB_HOST cannot be empty".into());
    }
    let port = env::var("REPORTS_DB_PORT").unwrap_or_else(|_| "5433".into())
        .parse::<u16>().map_err(|_| "REPORTS_DB_PORT must be a port number")?;
    let mut config = Config::new();
    config.host(&host).port(port)
        .dbname("resource_reports")
        .user("resource_report_reader")
        .connect_timeout(Duration::from_secs(3));
    if let Ok(password) = env::var("REPORTS_DB_PASSWORD") {
        config.password(password);
    }
    // This deployment uses the existing private-cluster YSQL endpoint without TLS.
    Ok(Some(config))
}

pub(crate) async fn list(State(state): State<Arc<AppState>>) -> Result<Json<Value>, AppError> {
    let Some(config) = &state.reports_database else {
        return Err(AppError {
            status: StatusCode::SERVICE_UNAVAILABLE,
            message: "日次レポートの接続設定がまだありません".into(),
        });
    };
    tokio::time::timeout(Duration::from_secs(8), query(config))
        .await
        .map_err(|_| report_error())?
}

fn report_error() -> AppError {
    // Do not include database errors or configuration in client responses or logs.
    tracing::warn!("resource report database request failed");
    AppError {
        status: StatusCode::SERVICE_UNAVAILABLE,
        message: "日次レポートを取得できません。しばらくしてから再読み込みしてください".into(),
    }
}

async fn query(config: &Config) -> Result<Json<Value>, AppError> {
    let (client, connection) = config.connect(NoTls).await.map_err(|_| report_error())?;
    // Drive the connection in the same future so a request timeout cancels both sides.
    let fetch = async {
        let rows = client.query(REPORT_QUERY, &[]).await.map_err(|_| report_error())?;
        let reports: Vec<Value> = rows.iter().map(|row| row.get(0)).collect();
        Ok(Json(json!({ "reports": reports, "timezone": "Asia/Tokyo", "retention_days": 7 })))
    };
    tokio::pin!(connection);
    tokio::select! {
        result = fetch => result,
        _ = &mut connection => Err(report_error()),
    }
}
