//! Portfolio mode: account + positions, recent daily runs, and dry-run / execute triggers.

use crate::analyze::rating_color;
use crate::DataMsg;
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph, Row, Table, Wrap},
    Frame,
};
use reqwest::Client;
use serde_json::Value;
use std::time::{Duration, Instant};
use tokio::{sync::mpsc::UnboundedSender, task::JoinHandle};

const REFRESH_EVERY: Duration = Duration::from_secs(30);
const REQ_TIMEOUT: Duration = Duration::from_secs(20);

/// Messages from background tasks. Run messages carry the run id (stale runs are ignored).
pub enum PortfolioMsg {
    Portfolio(Result<Value, String>),
    Trades(Result<Value, String>),
    RunStatus(u64, String),
    RunDone(u64, Value),
    RunFailed(u64, String),
}

/// What the main loop should do after a key press.
pub enum Action {
    None,
    Quit,
    Leave,
}

struct ActiveRun {
    execute: bool,
    started: Instant,
    message: String,
}

pub struct PortfolioState {
    portfolio: Option<Value>,
    trades: Option<Value>,
    portfolio_err: Option<String>,
    trades_err: Option<String>,
    run_err: Option<String>,
    notice: Option<String>,
    pending: u8,
    last_refresh: Option<Instant>,
    run: Option<ActiveRun>,
    run_id: u64,
    handle: Option<JoinHandle<()>>,
    confirm: bool,
    scroll: usize,
}

impl PortfolioState {
    pub fn new() -> Self {
        PortfolioState {
            portfolio: None,
            trades: None,
            portfolio_err: None,
            trades_err: None,
            run_err: None,
            notice: None,
            pending: 0,
            last_refresh: None,
            run: None,
            run_id: 0,
            handle: None,
            confirm: false,
            scroll: 0,
        }
    }

    /// Called when the mode is entered.
    pub fn enter(&mut self, client: &Client, tx: &UnboundedSender<DataMsg>) {
        self.confirm = false;
        self.refresh(client, tx, true);
    }

    /// Called every loop iteration while in this mode: periodic portfolio refresh.
    pub fn tick(&mut self, client: &Client, tx: &UnboundedSender<DataMsg>) {
        let due = self.last_refresh.map_or(true, |t| t.elapsed() >= REFRESH_EVERY);
        if due && self.pending == 0 {
            self.refresh(client, tx, false);
        }
    }

    fn refresh(&mut self, client: &Client, tx: &UnboundedSender<DataMsg>, with_trades: bool) {
        if self.pending > 0 {
            return;
        }
        self.last_refresh = Some(Instant::now());
        self.pending += 1;
        tokio::spawn(fetch(client.clone(), tx.clone(), "/api/portfolio", PortfolioMsg::Portfolio));
        if with_trades {
            self.pending += 1;
            tokio::spawn(fetch(client.clone(), tx.clone(), "/api/trades?limit=20", PortfolioMsg::Trades));
        }
    }

    pub fn on_msg(&mut self, msg: PortfolioMsg, client: &Client, tx: &UnboundedSender<DataMsg>) {
        match msg {
            PortfolioMsg::Portfolio(r) => {
                self.pending = self.pending.saturating_sub(1);
                match r {
                    Ok(v) => {
                        self.portfolio = Some(v);
                        self.portfolio_err = None;
                    }
                    Err(e) => self.portfolio_err = Some(format!("Portfolio: {e}")),
                }
            }
            PortfolioMsg::Trades(r) => {
                self.pending = self.pending.saturating_sub(1);
                match r {
                    Ok(v) => {
                        self.trades = Some(v);
                        self.trades_err = None;
                    }
                    Err(e) => self.trades_err = Some(format!("Trades: {e}")),
                }
            }
            PortfolioMsg::RunStatus(id, m) => {
                if let (true, Some(run)) = (id == self.run_id, self.run.as_mut()) {
                    if !m.is_empty() {
                        run.message = m;
                    }
                }
            }
            PortfolioMsg::RunDone(id, rec) => {
                if id != self.run_id || self.run.is_none() {
                    return;
                }
                let tag = run_tag(self.run.take().map_or(false, |r| r.execute));
                let summary = rec["summary"].as_str().unwrap_or("");
                self.notice = Some(format!("{tag} complete. {summary}"));
                self.pending = 0;
                self.refresh(client, tx, true);
            }
            PortfolioMsg::RunFailed(id, e) => {
                if id != self.run_id || self.run.is_none() {
                    return;
                }
                let tag = run_tag(self.run.take().map_or(false, |r| r.execute));
                self.run_err = Some(format!("{tag} failed: {e}"));
                self.pending = 0;
                self.refresh(client, tx, true);
            }
        }
    }

    fn start_run(&mut self, execute: bool, client: &Client, tx: &UnboundedSender<DataMsg>) {
        if self.run.is_some() {
            return;
        }
        if let Some(h) = self.handle.take() {
            h.abort();
        }
        self.run_id += 1;
        self.run_err = None;
        self.notice = None;
        self.run = Some(ActiveRun { execute, started: Instant::now(), message: "Submitting job...".into() });
        self.handle = Some(tokio::spawn(run_task(client.clone(), tx.clone(), self.run_id, execute)));
    }
}

fn run_tag(execute: bool) -> &'static str {
    if execute { "EXECUTE run" } else { "DRY run" }
}

fn net_err(e: &reqwest::Error) -> String {
    if e.is_connect() || e.is_timeout() {
        format!("Backend not reachable at {}. Is the server running? ({e})", crate::api_base())
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

async fn get_json(client: &Client, path: &str) -> Result<Value, String> {
    match client.get(format!("{}{path}", crate::api_base())).timeout(REQ_TIMEOUT).send().await {
        Err(e) => Err(net_err(&e)),
        Ok(r) if !r.status().is_success() => Err(http_err(r).await),
        Ok(r) => r.json::<Value>().await.map_err(|e| format!("Bad response from {path}: {e}")),
    }
}

async fn fetch(client: Client, tx: UnboundedSender<DataMsg>, path: &'static str, wrap: fn(Result<Value, String>) -> PortfolioMsg) {
    let r = get_json(&client, path).await;
    let _ = tx.send(DataMsg::Portfolio(wrap(r)));
}

async fn run_task(client: Client, tx: UnboundedSender<DataMsg>, id: u64, execute: bool) {
    let send = |m: PortfolioMsg| {
        let _ = tx.send(DataMsg::Portfolio(m));
    };
    let resp = client
        .post(format!("{}/api/daily-run?execute={execute}", crate::api_base()))
        .timeout(REQ_TIMEOUT)
        .send()
        .await;
    let job_id = match resp {
        Err(e) => return send(PortfolioMsg::RunFailed(id, net_err(&e))),
        Ok(r) if !r.status().is_success() => return send(PortfolioMsg::RunFailed(id, http_err(r).await)),
        Ok(r) => match r.json::<Value>().await {
            Ok(v) => v["job_id"].as_str().unwrap_or("").to_string(),
            Err(e) => return send(PortfolioMsg::RunFailed(id, format!("Bad response from /api/daily-run: {e}"))),
        },
    };
    if job_id.is_empty() {
        return send(PortfolioMsg::RunFailed(id, "Backend returned no job_id".into()));
    }

    let mut fails = 0;
    loop {
        tokio::time::sleep(Duration::from_secs(1)).await;
        let st = match get_json(&client, &format!("/api/status/{job_id}")).await {
            Ok(v) => {
                fails = 0;
                v
            }
            Err(e) => {
                // runs last minutes: tolerate a few blips before giving up
                fails += 1;
                if fails >= 5 {
                    return send(PortfolioMsg::RunFailed(id, e));
                }
                continue;
            }
        };
        let msg = st["message"].as_str().unwrap_or("").to_string();
        match st["state"].as_str().unwrap_or("") {
            "done" => {
                return match get_json(&client, &format!("/api/result/{job_id}")).await {
                    Ok(v) => send(PortfolioMsg::RunDone(id, v)),
                    Err(e) => send(PortfolioMsg::RunFailed(id, e)),
                };
            }
            "error" => {
                return send(PortfolioMsg::RunFailed(id, if msg.is_empty() { "Daily run failed".into() } else { msg }));
            }
            _ => send(PortfolioMsg::RunStatus(id, msg)),
        }
    }
}

pub fn handle_key(st: &mut PortfolioState, key: KeyEvent, client: &Client, tx: &UnboundedSender<DataMsg>) -> Action {
    if st.confirm {
        st.confirm = false;
        if key.code == KeyCode::Char('y') {
            st.start_run(true, client, tx);
        }
        return Action::None;
    }
    match key.code {
        KeyCode::Char('q') => return Action::Quit,
        KeyCode::Esc | KeyCode::Char('p') => return Action::Leave,
        KeyCode::Char('r') => st.refresh(client, tx, true),
        KeyCode::Char('d') => st.start_run(false, client, tx),
        KeyCode::Char('x') => st.confirm = st.run.is_none(),
        KeyCode::Down | KeyCode::Char('j') => st.scroll = st.scroll.saturating_add(1),
        KeyCode::Up | KeyCode::Char('k') => st.scroll = st.scroll.saturating_sub(1),
        KeyCode::PageDown => st.scroll = st.scroll.saturating_add(10),
        KeyCode::PageUp => st.scroll = st.scroll.saturating_sub(10),
        KeyCode::Home => st.scroll = 0,
        _ => {}
    }
    Action::None
}

// ---------- formatting helpers ----------

fn group(int_part: &str) -> String {
    let mut out = String::new();
    for (i, c) in int_part.chars().enumerate() {
        if i > 0 && (int_part.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

fn money(n: f64) -> String {
    let s = format!("{:.2}", n.abs());
    let (i, d) = s.split_once('.').unwrap_or((&s, "00"));
    let sign = if n < 0.0 && n.abs() >= 0.005 { "-" } else { "" };
    format!("{sign}${}.{d}", group(i))
}

fn opt_money(v: &Value) -> Option<String> {
    v.as_f64().map(money)
}

fn money_or(v: &Value) -> String {
    opt_money(v).unwrap_or_else(|| "n/a".into())
}

fn qty(v: &Value) -> String {
    match v.as_f64() {
        Some(n) if (n - n.round()).abs() < 1e-9 => format!("{n:.0}"),
        Some(n) => format!("{n:.4}").trim_end_matches('0').to_string(),
        None => "n/a".into(),
    }
}

fn pnl_color(n: f64) -> Color {
    if n > 0.0 {
        Color::Green
    } else if n < 0.0 {
        Color::Red
    } else {
        Color::Reset
    }
}

fn str_of<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}

fn wrap_text(text: &str, width: usize) -> Vec<String> {
    let width = width.max(8);
    let mut out = Vec::new();
    for para in text.lines() {
        let mut cur = String::new();
        let mut len = 0;
        for word in para.split_whitespace() {
            let wl = word.chars().count();
            if wl > width {
                if len > 0 {
                    out.push(std::mem::take(&mut cur));
                    len = 0;
                }
                let chars: Vec<char> = word.chars().collect();
                for c in chars.chunks(width) {
                    out.push(c.iter().collect());
                }
            } else if len == 0 {
                cur.push_str(word);
                len = wl;
            } else if len + 1 + wl <= width {
                cur.push(' ');
                cur.push_str(word);
                len += 1 + wl;
            } else {
                out.push(std::mem::take(&mut cur));
                cur.push_str(word);
                len = wl;
            }
        }
        if len > 0 || para.trim().is_empty() {
            out.push(cur);
        }
    }
    if out.is_empty() {
        out.push(String::new());
    }
    out
}

/// Pre-wrapped line builder so the scroll range is exact.
struct Lines {
    width: usize,
    lines: Vec<Line<'static>>,
}

impl Lines {
    fn push(&mut self, spans: Vec<Span<'static>>) {
        self.lines.push(Line::from(spans));
    }

    fn text(&mut self, indent: usize, text: &str, style: Style) {
        for l in wrap_text(text, self.width.saturating_sub(indent)) {
            self.lines.push(Line::from(Span::styled(format!("{}{l}", " ".repeat(indent)), style)));
        }
    }
}

fn order_head(o: &Value) -> String {
    let mut s = format!("{} {}", str_of(o, "side").to_uppercase(), str_of(o, "symbol"));
    if let Some(n) = opt_money(&o["notional"]) {
        s.push_str(&format!("  {n}"));
    }
    if let Some(n) = opt_money(&o["stop_loss"]) {
        s.push_str(&format!("  stop {n}"));
    }
    if let Some(n) = opt_money(&o["take_profit"]) {
        s.push_str(&format!("  take {n}"));
    }
    s
}

fn run_lines(runs: &[Value], width: usize) -> Vec<Line<'static>> {
    let dim = Style::default().fg(Color::DarkGray);
    let bold = Style::default().add_modifier(Modifier::BOLD);
    let mut l = Lines { width, lines: Vec::new() };
    for r in runs {
        let time: String = str_of(r, "time").chars().take(19).collect::<String>().replace('T', " ");
        let executed = r["executed"].as_bool().unwrap_or(false);
        let (tag, tag_style) = if executed {
            (" EXEC ", Style::default().fg(Color::Black).bg(Color::Magenta).add_modifier(Modifier::BOLD))
        } else {
            (" DRY ", Style::default().fg(Color::Black).bg(Color::Cyan).add_modifier(Modifier::BOLD))
        };
        l.push(vec![
            Span::styled(time, bold),
            Span::raw("  "),
            Span::styled(tag, tag_style),
            Span::styled(
                format!("  equity {}  cash {}", money_or(&r["equity"]), money_or(&r["cash"])),
                dim,
            ),
        ]);
        let cands: Vec<&str> = r["candidates"].as_array().map_or(vec![], |a| a.iter().filter_map(|s| s.as_str()).collect());
        l.text(2, &format!("Candidates: {}", if cands.is_empty() { "(none)".into() } else { cands.join(", ") }), Style::default());

        if let Some(d) = r["decisions"].as_object().filter(|d| !d.is_empty()) {
            l.text(2, "Decisions:", Style::default().add_modifier(Modifier::UNDERLINED));
            for (sym, dec) in d {
                let rating = str_of(dec, "rating");
                l.push(vec![
                    Span::raw(format!("    {sym:<6} ")),
                    Span::styled(
                        format!("{:<12}", if rating.is_empty() { "?" } else { rating }),
                        Style::default().fg(rating_color(rating)).add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(
                        format!("target {}  stop {}", money_or(&dec["target_price"]), money_or(&dec["stop_loss"])),
                        dim,
                    ),
                ]);
                let j = str_of(dec, "justification");
                if !j.is_empty() {
                    l.text(6, j, dim);
                }
            }
        }
        let summary = str_of(r, "summary");
        if !summary.is_empty() {
            l.text(2, &format!("Summary: {summary}"), Style::default().fg(Color::Cyan));
        }

        let approved = r["approved"].as_array().cloned().unwrap_or_default();
        if !approved.is_empty() {
            l.text(2, "Approved orders:", Style::default().add_modifier(Modifier::UNDERLINED));
            for o in &approved {
                l.text(4, &format!("+ {}", order_head(o)), Style::default().fg(Color::Green));
                let why = str_of(o, "reason");
                if !why.is_empty() {
                    l.text(6, why, dim);
                }
            }
            if !executed {
                l.text(4, "(dry run: no orders were sent)", dim);
            }
        }
        let rejected = r["rejected"].as_array().cloned().unwrap_or_default();
        if !rejected.is_empty() {
            l.text(2, "Rejected orders:", Style::default().add_modifier(Modifier::UNDERLINED));
            for o in &rejected {
                l.text(4, &format!("x {}", order_head(o)), Style::default().fg(Color::Red));
                let why = str_of(o, "reason");
                l.text(6, if why.is_empty() { "(no reason given)" } else { why }, Style::default().fg(Color::Yellow));
            }
        }
        let fills = r["fills"].as_array().cloned().unwrap_or_default();
        if !fills.is_empty() {
            l.text(2, "Fills:", Style::default().add_modifier(Modifier::UNDERLINED));
            for fl in &fills {
                let who = format!("{} {}", str_of(fl, "side").to_uppercase(), str_of(fl, "symbol"));
                match fl["error"].as_str() {
                    Some(e) => l.text(4, &format!("FAIL {who}: {e}"), Style::default().fg(Color::Red)),
                    None => l.text(4, &format!("OK   {who}  order {}", str_of(fl, "order_id")), Style::default().fg(Color::Green)),
                }
            }
        }
        if let Some(errs) = r["errors"].as_object().filter(|e| !e.is_empty()) {
            l.text(2, "Errors:", Style::default().add_modifier(Modifier::UNDERLINED));
            for (sym, e) in errs {
                let msg = e.as_str().map_or_else(|| e.to_string(), |s| s.to_string());
                l.text(4, &format!("{sym}: {msg}"), Style::default().fg(Color::Red));
            }
        }
        l.push(vec![Span::styled("-".repeat(width.min(60)), dim)]);
    }
    l.lines
}

// ---------- drawing ----------

fn error_block(f: &mut Frame, area: Rect, lines: &[String]) {
    let text: Vec<Line> = lines.iter().map(|s| Line::from(s.as_str())).collect();
    let p = Paragraph::new(text)
        .style(Style::default().fg(Color::Red))
        .wrap(Wrap { trim: true })
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Red))
                .title(" Error "),
        );
    f.render_widget(p, area);
}

pub fn draw(f: &mut Frame, area: Rect, st: &mut PortfolioState) {
    let mut errs: Vec<String> = Vec::new();
    errs.extend(st.portfolio_err.clone());
    errs.extend(st.trades_err.clone());
    errs.extend(st.run_err.clone());
    let w = area.width.saturating_sub(2).max(1) as usize;
    let err_rows: usize = errs.iter().map(|e| e.chars().count().div_ceil(w).max(1)).sum();
    let err_h = if errs.is_empty() { 0 } else { err_rows.min(6) as u16 + 2 };

    let n_pos = st.portfolio.as_ref().and_then(|p| p["positions"].as_array()).map_or(0, |a| a.len());
    let pos_h = (n_pos.clamp(1, 6) as u16) + 3;

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(4),
            Constraint::Length(pos_h),
            Constraint::Length(4),
            Constraint::Length(err_h),
            Constraint::Min(4),
        ])
        .split(area);

    draw_header(f, rows[0], st);
    draw_positions(f, rows[1], st);
    draw_status(f, rows[2], st);
    if err_h > 0 {
        error_block(f, rows[3], &errs);
    }
    draw_runs(f, rows[4], st);
}

fn draw_header(f: &mut Frame, area: Rect, st: &PortfolioState) {
    let block = Block::default().borders(Borders::ALL).title(" Portfolio ");
    let Some(p) = st.portfolio.as_ref() else {
        let msg = if st.portfolio_err.is_some() { "(unavailable)" } else { "Loading portfolio..." };
        f.render_widget(Paragraph::new(msg).block(block), area);
        return;
    };
    let equity = p["equity"].as_f64();
    let peak = p["peak_equity"].as_f64();
    let dd = p["drawdown_pct"].as_f64().unwrap_or(0.0);
    let bold = Style::default().add_modifier(Modifier::BOLD);
    let dim = Style::default().fg(Color::DarkGray);
    let line1 = Line::from(vec![
        Span::styled("Mode ", dim),
        Span::styled(str_of(p, "mode").to_string(), Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        Span::styled("   Equity ", dim),
        Span::styled(money_or(&p["equity"]), bold),
        Span::styled("   Cash ", dim),
        Span::styled(money_or(&p["cash"]), bold),
    ]);
    let (delta, color) = match (equity, peak) {
        (Some(e), Some(pk)) => (e - pk, if dd > 0.005 { Color::Red } else { Color::Green }),
        _ => (0.0, Color::Reset),
    };
    let sign = if delta > 0.005 { "+" } else { "" };
    let line2 = Line::from(vec![
        Span::styled("Peak ", dim),
        Span::styled(money_or(&p["peak_equity"]), bold),
        Span::styled("   P&L vs peak ", dim),
        Span::styled(format!("{sign}{}", money(delta)), Style::default().fg(color).add_modifier(Modifier::BOLD)),
        Span::styled("   Drawdown ", dim),
        Span::styled(format!("{dd:.2}%"), Style::default().fg(color).add_modifier(Modifier::BOLD)),
    ]);
    f.render_widget(Paragraph::new(vec![line1, line2]).block(block), area);
}

fn draw_positions(f: &mut Frame, area: Rect, st: &PortfolioState) {
    let positions = st.portfolio.as_ref().and_then(|p| p["positions"].as_array());
    let n = positions.map_or(0, |a| a.len());
    let title = format!(" Positions ({n}) ");
    let block = Block::default().borders(Borders::ALL).title(title);
    let Some(positions) = positions else {
        f.render_widget(Paragraph::new("").block(block), area);
        return;
    };
    if positions.is_empty() {
        f.render_widget(Paragraph::new("No open positions.").block(block), area);
        return;
    }
    let head = Row::new(["Symbol", "Qty", "Avg entry", "Price", "Mkt value", "P&L %"])
        .style(Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD));
    let visible = area.height.saturating_sub(3) as usize;
    let rows: Vec<Row> = positions
        .iter()
        .take(visible)
        .map(|p| {
            let pnl = p["pnl_pct"].as_f64();
            let pnl_txt = pnl.map_or("n/a".to_string(), |n| format!("{n:+.2}%"));
            Row::new(vec![
                ratatui::widgets::Cell::from(str_of(p, "symbol").to_string()),
                ratatui::widgets::Cell::from(qty(&p["qty"])),
                ratatui::widgets::Cell::from(money_or(&p["avg_entry"])),
                ratatui::widgets::Cell::from(money_or(&p["price"])),
                ratatui::widgets::Cell::from(money_or(&p["market_value"])),
                ratatui::widgets::Cell::from(pnl_txt).style(Style::default().fg(pnl.map_or(Color::Reset, pnl_color))),
            ])
        })
        .collect();
    let widths = [
        Constraint::Length(10),
        Constraint::Length(12),
        Constraint::Length(14),
        Constraint::Length(14),
        Constraint::Length(16),
        Constraint::Length(10),
    ];
    f.render_widget(Table::new(rows, widths).header(head).block(block), area);
}

fn draw_status(f: &mut Frame, area: Rect, st: &PortfolioState) {
    let help = " r refresh | d dry-run | x execute | j/k scroll | p/Esc back | q quit ";
    if st.confirm {
        let p = Paragraph::new(vec![
            Line::from(Span::styled(
                "EXECUTE RUN: orders WILL BE SENT to the Alpaca PAPER trading account.",
                Style::default().fg(Color::Black).bg(Color::Yellow).add_modifier(Modifier::BOLD),
            )),
            Line::from(Span::styled(
                "Type y to confirm; any other key cancels.",
                Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD),
            )),
        ])
        .block(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(Color::Yellow))
                .title(" Confirm "),
        );
        f.render_widget(p, area);
        return;
    }
    let lines = if let Some(run) = &st.run {
        let s = run.started.elapsed().as_secs();
        vec![
            Line::from(Span::styled(
                format!("{} running ({}:{:02})", run_tag(run.execute), s / 60, s % 60),
                Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD),
            )),
            Line::from(run.message.clone()),
        ]
    } else {
        let ago = st.last_refresh.map_or("never".to_string(), |t| format!("{}s ago", t.elapsed().as_secs()));
        let mut second = format!("Refreshed {ago} (auto every {}s)", REFRESH_EVERY.as_secs());
        if st.pending > 0 {
            second.push_str("  refreshing...");
        }
        let first = match &st.notice {
            Some(n) => Line::from(Span::styled(n.clone(), Style::default().fg(Color::Green))),
            None => Line::from("Idle."),
        };
        vec![first, Line::from(Span::styled(second, Style::default().fg(Color::DarkGray)))]
    };
    f.render_widget(
        Paragraph::new(lines).block(Block::default().borders(Borders::ALL).title(help)),
        area,
    );
}

fn draw_runs(f: &mut Frame, area: Rect, st: &mut PortfolioState) {
    let inner_w = area.width.saturating_sub(2) as usize;
    let inner_h = area.height.saturating_sub(2) as usize;
    let runs = st.trades.as_ref().and_then(|t| t["runs"].as_array());
    let (n_runs, lines) = match runs {
        None => (
            0,
            vec![Line::from(if st.trades_err.is_some() { "(unavailable)" } else { "Loading runs..." })],
        ),
        Some(r) if r.is_empty() => (0, vec![Line::from("No runs logged yet. Press d for a dry run.")]),
        Some(r) => (r.len(), run_lines(r, inner_w)),
    };
    let max = lines.len().saturating_sub(inner_h);
    st.scroll = st.scroll.min(max);
    let shown: Vec<Line> = lines.into_iter().skip(st.scroll).take(inner_h).collect();
    let more = if max > 0 { format!(" [{}/{}]", st.scroll + 1, max + 1) } else { String::new() };
    f.render_widget(
        Paragraph::new(shown).block(
            Block::default()
                .borders(Borders::ALL)
                .title(format!(" Recent runs ({n_runs}){more} ")),
        ),
        area,
    );
}
