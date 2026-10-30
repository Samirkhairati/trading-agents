//! Analyze mode: ticker -> POST /api/analyze -> poll /api/status -> show /api/result.

use crate::DataMsg;
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    layout::{Alignment, Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Gauge, Paragraph, Tabs, Wrap},
    Frame,
};
use reqwest::Client;
use serde_json::Value;
use std::time::Duration;
use tokio::{sync::mpsc::UnboundedSender, task::JoinHandle};

const BASE: &str = "http://127.0.0.1:8000";

const TABS: [&str; 7] = [
    "1 Fundamentals",
    "2 Technicals",
    "3 Sentiment",
    "4 Macro",
    "5 Debate",
    "6 Plan+Trader",
    "7 Risk",
];

/// Messages from the background job task; the u64 is the run id (stale runs are ignored).
pub enum AnalyzeMsg {
    Status(Value),
    Result(Value),
    Failed(String),
}

#[derive(PartialEq)]
enum Phase {
    Input,
    Running,
    Done,
    Failed,
}

/// What the main loop should do after a key press.
pub enum Action {
    None,
    Quit,
    LeaveAnalyze,
}

pub struct AnalyzeState {
    input: String,
    phase: Phase,
    run_id: u64,
    handle: Option<JoinHandle<()>>,
    ticker: String,
    stage: String,
    message: String,
    done: u64,
    total: u64,
    error: String,
    result: Option<Value>,
    tab: usize,
    scroll: u16,
}

impl AnalyzeState {
    pub fn new() -> Self {
        AnalyzeState {
            input: String::new(),
            phase: Phase::Input,
            run_id: 0,
            handle: None,
            ticker: String::new(),
            stage: String::new(),
            message: String::new(),
            done: 0,
            total: 0,
            error: String::new(),
            result: None,
            tab: 0,
            scroll: 0,
        }
    }

    fn cancel(&mut self) {
        if let Some(h) = self.handle.take() {
            h.abort();
        }
    }

    pub fn on_msg(&mut self, id: u64, msg: AnalyzeMsg) {
        if id != self.run_id || self.phase != Phase::Running {
            return;
        }
        match msg {
            AnalyzeMsg::Status(v) => {
                self.stage = str_of(&v, "stage");
                self.message = str_of(&v, "message");
                self.done = v["done"].as_u64().unwrap_or(self.done);
                self.total = v["total"].as_u64().unwrap_or(self.total);
            }
            AnalyzeMsg::Result(v) => {
                self.result = Some(v);
                self.phase = Phase::Done;
                self.tab = 0;
                self.scroll = 0;
            }
            AnalyzeMsg::Failed(e) => {
                self.error = e;
                self.phase = Phase::Failed;
            }
        }
    }

    fn start(&mut self, client: &Client, tx: &UnboundedSender<DataMsg>) {
        let ticker = self.input.trim().to_uppercase();
        if ticker.is_empty() {
            return;
        }
        self.cancel();
        self.run_id += 1;
        self.ticker = ticker.clone();
        self.phase = Phase::Running;
        self.stage = "Starting".into();
        self.message = "Submitting job...".into();
        self.done = 0;
        self.total = 0;
        self.result = None;
        self.error.clear();
        self.tab = 0;
        self.scroll = 0;
        self.handle = Some(tokio::spawn(job_task(client.clone(), tx.clone(), self.run_id, ticker)));
    }
}

fn str_of(v: &Value, key: &str) -> String {
    v[key].as_str().unwrap_or("").to_string()
}

fn net_err(e: &reqwest::Error) -> String {
    if e.is_connect() || e.is_timeout() {
        format!("Backend not reachable at {BASE}. Is the server running? ({e})")
    } else {
        format!("Request failed: {e}")
    }
}

/// Turn a non-2xx response into a short message (uses FastAPI's `detail` if present).
async fn http_err(resp: reqwest::Response) -> String {
    let code = resp.status();
    let body = resp.json::<Value>().await.unwrap_or(Value::Null);
    match body["detail"].as_str() {
        Some(d) => format!("Backend error {code}: {d}"),
        None => format!("Backend error {code}"),
    }
}

async fn job_task(client: Client, tx: UnboundedSender<DataMsg>, id: u64, ticker: String) {
    let send = |m: AnalyzeMsg| {
        let _ = tx.send(DataMsg::Analyze(id, m));
    };
    let resp = client
        .post(format!("{BASE}/api/analyze"))
        .json(&serde_json::json!({ "ticker": ticker }))
        .send()
        .await;
    let job_id = match resp {
        Err(e) => return send(AnalyzeMsg::Failed(net_err(&e))),
        Ok(r) if !r.status().is_success() => return send(AnalyzeMsg::Failed(http_err(r).await)),
        Ok(r) => match r.json::<Value>().await {
            Ok(v) => v["job_id"].as_str().unwrap_or("").to_string(),
            Err(e) => return send(AnalyzeMsg::Failed(format!("Bad response from /api/analyze: {e}"))),
        },
    };
    if job_id.is_empty() {
        return send(AnalyzeMsg::Failed("Backend returned no job_id".into()));
    }

    loop {
        tokio::time::sleep(Duration::from_secs(1)).await;
        let st = match client.get(format!("{BASE}/api/status/{job_id}")).send().await {
            Err(e) => return send(AnalyzeMsg::Failed(net_err(&e))),
            Ok(r) if !r.status().is_success() => return send(AnalyzeMsg::Failed(http_err(r).await)),
            Ok(r) => match r.json::<Value>().await {
                Ok(v) => v,
                Err(e) => return send(AnalyzeMsg::Failed(format!("Bad status response: {e}"))),
            },
        };
        match st["state"].as_str().unwrap_or("") {
            "done" => {
                send(AnalyzeMsg::Status(st));
                let res = match client.get(format!("{BASE}/api/result/{job_id}")).send().await {
                    Err(e) => return send(AnalyzeMsg::Failed(net_err(&e))),
                    Ok(r) if !r.status().is_success() => return send(AnalyzeMsg::Failed(http_err(r).await)),
                    Ok(r) => r.json::<Value>().await,
                };
                return match res {
                    Ok(v) => send(AnalyzeMsg::Result(v)),
                    Err(e) => send(AnalyzeMsg::Failed(format!("Bad result response: {e}"))),
                };
            }
            "error" => {
                let m = str_of(&st, "message");
                return send(AnalyzeMsg::Failed(if m.is_empty() { "Analysis failed".into() } else { m }));
            }
            _ => send(AnalyzeMsg::Status(st)),
        }
    }
}

pub fn handle_key(st: &mut AnalyzeState, key: KeyEvent, client: &Client, tx: &UnboundedSender<DataMsg>) -> Action {
    if st.phase == Phase::Input {
        match key.code {
            KeyCode::Enter => st.start(client, tx),
            KeyCode::Char(c) => st.input.push(c),
            KeyCode::Backspace => {
                st.input.pop();
            }
            KeyCode::Esc => return Action::LeaveAnalyze,
            _ => {}
        }
        return Action::None;
    }
    match key.code {
        KeyCode::Char('q') => return Action::Quit,
        KeyCode::Char('a') => {
            st.cancel();
            return Action::LeaveAnalyze;
        }
        KeyCode::Esc => {
            st.cancel();
            st.phase = Phase::Input;
        }
        _ if st.phase != Phase::Done => {}
        KeyCode::Right | KeyCode::Char('l') => {
            st.tab = (st.tab + 1) % TABS.len();
            st.scroll = 0;
        }
        KeyCode::Left | KeyCode::Char('h') => {
            st.tab = (st.tab + TABS.len() - 1) % TABS.len();
            st.scroll = 0;
        }
        KeyCode::Char(c @ '1'..='7') => {
            st.tab = c as usize - '1' as usize;
            st.scroll = 0;
        }
        KeyCode::Down | KeyCode::Char('j') => st.scroll = st.scroll.saturating_add(1),
        KeyCode::Up | KeyCode::Char('k') => st.scroll = st.scroll.saturating_sub(1),
        KeyCode::PageDown => st.scroll = st.scroll.saturating_add(10),
        KeyCode::PageUp => st.scroll = st.scroll.saturating_sub(10),
        _ => {}
    }
    Action::None
}

pub fn rating_color(rating: &str) -> Color {
    match rating.to_lowercase().as_str() {
        "buy" | "overweight" => Color::Green,
        "hold" => Color::Yellow,
        "underweight" | "sell" => Color::Red,
        _ => Color::Gray,
    }
}

fn num(v: &Value, prefix: &str, suffix: &str) -> String {
    match v.as_f64() {
        Some(n) => format!("{prefix}{n:.2}{suffix}"),
        None => "n/a".into(),
    }
}

fn join(v: &Value) -> String {
    match v.as_array() {
        Some(a) => a.iter().filter_map(|s| s.as_str()).collect::<Vec<_>>().join("\n\n---\n\n"),
        None => String::new(),
    }
}

fn tab_text(r: &Value, tab: usize) -> String {
    let text = match tab {
        0 => str_of(r, "fundamentals_report"),
        1 => str_of(r, "market_report"),
        2 => str_of(r, "sentiment_report"),
        3 => str_of(r, "news_report"),
        4 => join(&r["debate_history"]),
        5 => {
            let past = str_of(r, "past_context");
            let mut s = format!(
                "== Research Manager plan ==\n{}\n\n== Trader ==\n{}",
                str_of(r, "investment_plan"),
                str_of(r, "trader_plan")
            );
            if !past.is_empty() {
                s.push_str(&format!("\n\n== Past decisions ==\n{past}"));
            }
            s
        }
        _ => join(&r["risk_history"]),
    };
    if text.trim().is_empty() { "(no data)".into() } else { text }
}

fn fact(label: &str, value: String) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("{label:<8}"), Style::default().fg(Color::DarkGray)),
        Span::styled(value, Style::default().add_modifier(Modifier::BOLD)),
    ])
}

fn error_block(f: &mut Frame, area: Rect, msg: &str) {
    let p = Paragraph::new(msg.to_string())
        .style(Style::default().fg(Color::Red))
        .wrap(Wrap { trim: true })
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Red))
                .title(" Error (Esc: try again) "),
        );
    f.render_widget(p, area);
}

pub fn draw(f: &mut Frame, area: Rect, st: &mut AnalyzeState) {
    let top = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(3), Constraint::Min(1)])
        .split(area);

    let (title, style) = if st.phase == Phase::Input {
        (" Analyze ticker (Enter: run, Esc: raw-data view) ", Style::default().fg(Color::Yellow))
    } else {
        (" Analyze ticker (Esc: new ticker, a: raw-data view, q: quit) ", Style::default())
    };
    let input = Paragraph::new(st.input.as_str())
        .style(style)
        .block(Block::default().borders(Borders::ALL).title(title));
    f.render_widget(input, top[0]);
    if st.phase == Phase::Input {
        f.set_cursor(top[0].x + st.input.chars().count() as u16 + 1, top[0].y + 1);
    }
    let body = top[1];

    match st.phase {
        Phase::Input => {
            let help = "Type a ticker (e.g. AAPL) and press Enter.\nAnalysis runs the full agent pipeline and takes 1-3 minutes.";
            f.render_widget(
                Paragraph::new(help).block(Block::default().borders(Borders::ALL).title(" Analyze ")),
                body,
            );
        }
        Phase::Failed => error_block(f, body, &st.error),
        Phase::Running => draw_progress(f, body, st),
        Phase::Done => draw_result(f, body, st),
    }
}

fn draw_progress(f: &mut Frame, area: Rect, st: &AnalyzeState) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(3), Constraint::Min(1)])
        .split(area);
    let ratio = if st.total == 0 { 0.0 } else { (st.done as f64 / st.total as f64).min(1.0) };
    let label = format!("{}/{}  {}", st.done, st.total, st.stage);
    let gauge = Gauge::default()
        .block(Block::default().borders(Borders::ALL).title(format!(" Analyzing {} ", st.ticker)))
        .gauge_style(Style::default().fg(Color::Cyan))
        .ratio(ratio)
        .label(label);
    f.render_widget(gauge, rows[0]);
    f.render_widget(
        Paragraph::new(st.message.as_str())
            .wrap(Wrap { trim: true })
            .block(Block::default().borders(Borders::ALL).title(" Status ")),
        rows[1],
    );
}

fn draw_result(f: &mut Frame, area: Rect, st: &mut AnalyzeState) {
    let Some(r) = st.result.as_ref() else { return };
    let d = &r["final_decision"];
    let rating = d["rating"].as_str().unwrap_or("?").to_string();
    let color = rating_color(&rating);

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(7),
            Constraint::Length(6),
            Constraint::Length(3),
            Constraint::Min(3),
        ])
        .split(area);
    let head = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(28), Constraint::Min(20)])
        .split(rows[0]);

    let big = Paragraph::new(vec![
        Line::from(""),
        Line::from(Span::styled(
            format!("  {}  ", rating.to_uppercase()),
            Style::default().fg(Color::Black).bg(color).add_modifier(Modifier::BOLD),
        )),
    ])
    .alignment(Alignment::Center)
    .block(Block::default().borders(Borders::ALL).border_style(Style::default().fg(color)).title(" Rating "));
    f.render_widget(big, head[0]);

    let ticker = r["ticker"].as_str().unwrap_or(&st.ticker);
    let facts = vec![
        fact("Ticker", format!("{ticker}  ({})", str_of(r, "trade_date"))),
        fact("Price", num(&r["last_price"], "$", "")),
        fact("Target", num(&d["target_price"], "$", "")),
        fact("Stop", num(&d["stop_loss"], "$", "")),
        fact("Size", num(&d["position_size"], "", "%")),
    ];
    f.render_widget(
        Paragraph::new(facts).block(Block::default().borders(Borders::ALL).title(" Decision ")),
        head[1],
    );

    let just = d["justification"].as_str().unwrap_or("(none)");
    f.render_widget(
        Paragraph::new(just)
            .wrap(Wrap { trim: true })
            .block(Block::default().borders(Borders::ALL).title(" Justification ")),
        rows[1],
    );

    let tabs = Tabs::new(TABS.iter().map(|t| Line::from(*t)).collect::<Vec<_>>())
        .block(Block::default().borders(Borders::ALL).title(" Reports (</>/1-7: switch, j/k/PgUp/PgDn: scroll) "))
        .select(st.tab)
        .highlight_style(Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD));
    f.render_widget(tabs, rows[2]);

    let text = tab_text(r, st.tab);
    // Clamp scroll using an estimate of the wrapped line count.
    let width = rows[3].width.saturating_sub(2).max(1) as usize;
    let est: usize = text.lines().map(|l| l.chars().count().max(1).div_ceil(width)).sum();
    let est = est + est / 16 + 1; // word wrap needs more lines than a char count
    let max = est.saturating_sub(rows[3].height.saturating_sub(2) as usize).min(u16::MAX as usize) as u16;
    st.scroll = st.scroll.min(max);
    f.render_widget(
        Paragraph::new(text)
            .wrap(Wrap { trim: false })
            .scroll((st.scroll, 0))
            .block(Block::default().borders(Borders::ALL).title(format!(" {} ", &TABS[st.tab][2..]))),
        rows[3],
    );
}
