#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use tauri::{WebviewUrl, WebviewWindowBuilder};

/// 网关地址：环境变量 TAVILY_GATEWAY_URL 优先，默认本地 8000 端口
fn gateway_url() -> String {
    std::env::var("TAVILY_GATEWAY_URL")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "http://127.0.0.1:8000".to_string())
        .trim_end_matches('/')
        .to_string()
}

/// 探测网关 /health：手写最小 HTTP GET（仅支持 http），200 视为存活
fn probe_health(base: &str) -> bool {
    // 解析 http://host:port
    let rest = match base.strip_prefix("http://") {
        Some(r) => r,
        None => return false,
    };
    let host_port = rest.split('/').next().unwrap_or("");
    let mut parts = host_port.splitn(2, ':');
    let host = parts.next().unwrap_or("127.0.0.1");
    let port: u16 = parts.next().and_then(|p| p.parse().ok()).unwrap_or(80);

    let addr = format!("{}:{}", host, port);
    let sock: std::net::SocketAddr = match addr.parse() {
        Ok(a) => a,
        Err(_) => return false,
    };
    let stream = TcpStream::connect_timeout(&sock, Duration::from_millis(2500));
    let mut stream = match stream {
        Ok(s) => s,
        Err(_) => return false,
    };
    let req = format!("GET /health HTTP/1.0\r\nHost: {}\r\nConnection: close\r\n\r\n", host_port);
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let _ = stream.set_read_timeout(Some(Duration::from_millis(2500)));
    let mut buf = [0u8; 64];
    let n = stream.read(&mut buf).unwrap_or(0);
    let head = String::from_utf8_lossy(&buf[..n]);
    head.starts_with("HTTP/1.") && head.contains(" 200")
}

fn main() {
    let gw = gateway_url();
    let url = if probe_health(&gw) {
        WebviewUrl::External(format!("{}/ui/", gw).parse().expect("invalid gateway url"))
    } else {
        // 网关不可达：内置 fallback 页（可填网关地址重试，含启动指引）
        WebviewUrl::App("fallback.html".into())
    };

    tauri::Builder::default()
        .setup(move |app| {
            WebviewWindowBuilder::new(app, "main", url)
                .title("Tavily Pool Gateway")
                .inner_size(1280.0, 800.0)
                .min_inner_size(960.0, 620.0)
                .center()
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
