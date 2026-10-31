mod analyze;
mod portfolio;

use analyze::{Action, AnalyzeMsg, AnalyzeState};
use portfolio::{PortfolioMsg, PortfolioState};
use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyModifiers},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::{Backend, CrosstermBackend},
    layout::{Constraint, Direction, Layout},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph, Wrap, Tabs},
    Terminal,
};
use reqwest::Client;
use std::{error::Error, io, time::Duration};
use serde_json::Value;
use tokio::sync::mpsc;

/// Backend address: API_URL env var (set to http://api:8000 in Docker), default local.
pub fn api_base() -> String {
    std::env::var("API_URL").unwrap_or_else(|_| "http://127.0.0.1:8000".to_string())
}

enum InputMode {
    Normal,
    Editing,
}

enum ViewMode {
    RawData,
    AgentSummary,
}

#[derive(Clone, Copy, PartialEq)]
enum DataTab {
    All,
    Fundamentals,
    Technicals,
    News,
    Sentiment,
    Macro,
}

impl DataTab {
    fn next(self) -> Self {
        match self {
            DataTab::All => DataTab::Fundamentals,
            DataTab::Fundamentals => DataTab::Technicals,
            DataTab::Technicals => DataTab::News,
            DataTab::News => DataTab::Sentiment,
            DataTab::Sentiment => DataTab::Macro,
            DataTab::Macro => DataTab::All,
        }
    }
    
    fn title(&self) -> &'static str {
        match self {
            DataTab::All => "All Data",
            DataTab::Fundamentals => "Fundamentals",
            DataTab::Technicals => "Technicals (Price)",
            DataTab::News => "Recent News",
            DataTab::Sentiment => "Sentiment (Socials)",
            DataTab::Macro => "Macro & Events",
        }
    }
}

pub enum DataMsg {
    Fundamentals(Value),
    Technicals(Value),
    News(Value),
    Sentiment(Value),
    Macro(Value),
    Error(String),
    Analyze(u64, AnalyzeMsg),
    Portfolio(PortfolioMsg),
}

struct App {
    input: String,
    input_mode: InputMode,
    view_mode: ViewMode,
    active_tab: DataTab,
    scroll: u16,
    data_msg: String,
    analyzing: bool,
    analyze: AnalyzeState,
    portfolio_mode: bool,
    portfolio: PortfolioState,
    
    // Concurrent Data State
    fundamentals: Option<Value>,
    technicals: Option<Value>,
    news: Option<Value>,
    sentiment: Option<Value>,
    macro_data: Option<Value>,
    
    loading_fundamentals: bool,
    loading_technicals: bool,
    loading_news: bool,
    loading_sentiment: bool,
    loading_macro: bool,
}

impl App {
    fn new() -> App {
        App {
            input: String::new(),
            input_mode: InputMode::Normal,
            view_mode: ViewMode::RawData,
            active_tab: DataTab::All,
            scroll: 0,
            data_msg: String::from("Type a ticker and press Enter to fetch data."),
            analyzing: false,
            analyze: AnalyzeState::new(),
            portfolio_mode: false,
            portfolio: PortfolioState::new(),
            
            fundamentals: None,
            technicals: None,
            news: None,
            sentiment: None,
            macro_data: None,
            
            loading_fundamentals: false,
            loading_technicals: false,
            loading_news: false,
            loading_sentiment: false,
            loading_macro: false,
        }
    }

    fn reset_data(&mut self) {
        self.fundamentals = None;
        self.technicals = None;
        self.news = None;
        self.sentiment = None;
        self.macro_data = None;
        
        self.loading_fundamentals = true;
        self.loading_technicals = true;
        self.loading_news = true;
        self.loading_sentiment = true;
        self.loading_macro = true;
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let mut app = App::new();
    let res = run_app(&mut terminal, &mut app).await;

    disable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        LeaveAlternateScreen,
        DisableMouseCapture
    )?;
    terminal.show_cursor()?;

    if let Err(err) = res {
        println!("{:?}", err)
    }

    Ok(())
}

async fn run_app<B: Backend>(terminal: &mut Terminal<B>, app: &mut App) -> io::Result<()> {
    let (tx, mut rx) = mpsc::unbounded_channel::<DataMsg>();
    let client = Client::new();

    loop {
        // Handle incoming background data messages
        while let Ok(msg) = rx.try_recv() {
            match msg {
                DataMsg::Fundamentals(val) => {
                    app.fundamentals = Some(val);
                    app.loading_fundamentals = false;
                },
                DataMsg::Technicals(val) => {
                    app.technicals = Some(val);
                    app.loading_technicals = false;
                },
                DataMsg::News(val) => {
                    app.news = Some(val);
                    app.loading_news = false;
                },
                DataMsg::Sentiment(val) => {
                    app.sentiment = Some(val);
                    app.loading_sentiment = false;
                },
                DataMsg::Macro(val) => {
                    app.macro_data = Some(val);
                    app.loading_macro = false;
                },
                DataMsg::Analyze(id, m) => app.analyze.on_msg(id, m),
                DataMsg::Portfolio(m) => app.portfolio.on_msg(m, &client, &tx),
                DataMsg::Error(err) => {
                    app.data_msg = err;
                    // Could set all loading to false here, but let's let them resolve
                }
            }
        }

        if app.portfolio_mode {
            app.portfolio.tick(&client, &tx);
        }

        terminal.draw(|f| ui(f, &mut *app))?;

        if crossterm::event::poll(Duration::from_millis(50))? {
            if let Event::Key(key) = event::read()? {
                if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
                    return Ok(());
                }
                if app.portfolio_mode {
                    match portfolio::handle_key(&mut app.portfolio, key, &client, &tx) {
                        portfolio::Action::Quit => return Ok(()),
                        portfolio::Action::Leave => app.portfolio_mode = false,
                        portfolio::Action::None => {}
                    }
                    continue;
                }
                if app.analyzing {
                    match analyze::handle_key(&mut app.analyze, key, &client, &tx) {
                        Action::Quit => return Ok(()),
                        Action::LeaveAnalyze => app.analyzing = false,
                        Action::None => {}
                    }
                    continue;
                }
                match app.input_mode {
                    InputMode::Normal => match key.code {
                        KeyCode::Char('e') => {
                            app.input_mode = InputMode::Editing;
                        }
                        KeyCode::Char('q') => {
                            return Ok(());
                        }
                        KeyCode::Char('a') => {
                            app.analyzing = true;
                        }
                        KeyCode::Char('p') => {
                            app.portfolio_mode = true;
                            app.portfolio.enter(&client, &tx);
                        }
                        KeyCode::Char('v') => {
                            app.view_mode = match app.view_mode {
                                ViewMode::RawData => ViewMode::AgentSummary,
                                ViewMode::AgentSummary => ViewMode::RawData,
                            };
                        }
                        KeyCode::Tab => {
                            app.active_tab = app.active_tab.next();
                            app.scroll = 0;
                        }
                        KeyCode::Down | KeyCode::Char('j') => {
                            app.scroll = app.scroll.saturating_add(1);
                        }
                        KeyCode::Up | KeyCode::Char('k') => {
                            app.scroll = app.scroll.saturating_sub(1);
                        }
                        KeyCode::PageDown => {
                            app.scroll = app.scroll.saturating_add(10);
                        }
                        KeyCode::PageUp => {
                            app.scroll = app.scroll.saturating_sub(10);
                        }
                        _ => {}
                    },
                    InputMode::Editing => match key.code {
                        KeyCode::Enter => {
                            let ticker = app.input.clone();
                            if !ticker.is_empty() {
                                app.input_mode = InputMode::Normal;
                                app.reset_data();
                                app.scroll = 0;
                                app.data_msg = String::from("Fetching concurrent data...");
                                
                                // Spawn background tasks
                                let t = ticker.clone();
                                let c = client.clone();
                                let tx_f = tx.clone();
                                tokio::spawn(async move {
                                    if let Ok(resp) = c.get(format!("{}/api/data/fundamentals/{}", api_base(), t)).send().await {
                                        if let Ok(json) = resp.json::<Value>().await {
                                            let _ = tx_f.send(DataMsg::Fundamentals(json));
                                        }
                                    }
                                });

                                let t = ticker.clone();
                                let c = client.clone();
                                let tx_t = tx.clone();
                                tokio::spawn(async move {
                                    if let Ok(resp) = c.get(format!("{}/api/data/technicals/{}", api_base(), t)).send().await {
                                        if let Ok(json) = resp.json::<Value>().await {
                                            let _ = tx_t.send(DataMsg::Technicals(json));
                                        }
                                    }
                                });

                                let t = ticker.clone();
                                let c = client.clone();
                                let tx_n = tx.clone();
                                tokio::spawn(async move {
                                    if let Ok(resp) = c.get(format!("{}/api/data/news/{}", api_base(), t)).send().await {
                                        if let Ok(json) = resp.json::<Value>().await {
                                            let _ = tx_n.send(DataMsg::News(json));
                                        }
                                    }
                                });

                                let t = ticker.clone();
                                let c = client.clone();
                                let tx_s = tx.clone();
                                tokio::spawn(async move {
                                    if let Ok(resp) = c.get(format!("{}/api/data/sentiment/{}", api_base(), t)).send().await {
                                        if let Ok(json) = resp.json::<Value>().await {
                                            let _ = tx_s.send(DataMsg::Sentiment(json));
                                        }
                                    }
                                });

                                let c = client.clone();
                                let tx_m = tx.clone();
                                tokio::spawn(async move {
                                    if let Ok(resp) = c.get(format!("{}/api/data/macro", api_base())).send().await {
                                        if let Ok(json) = resp.json::<Value>().await {
                                            let _ = tx_m.send(DataMsg::Macro(json));
                                        }
                                    }
                                });
                            }
                        }
                        KeyCode::Char(c) => {
                            app.input.push(c);
                        }
                        KeyCode::Backspace => {
                            app.input.pop();
                        }
                        KeyCode::Esc => {
                            app.input_mode = InputMode::Normal;
                        }
                        _ => {}
                    },
                }
            }
        }
    }
}

fn format_json_content(app: &App) -> String {
    match app.active_tab {
        DataTab::All => {
            let mut all_json = serde_json::Map::new();
            if let Some(f) = &app.fundamentals { all_json.insert("fundamentals".to_string(), f.clone()); }
            if let Some(t) = &app.technicals { all_json.insert("technicals".to_string(), t.clone()); }
            if let Some(n) = &app.news { all_json.insert("news".to_string(), n.clone()); }
            if let Some(s) = &app.sentiment { all_json.insert("sentiment".to_string(), s.clone()); }
            if let Some(m) = &app.macro_data { all_json.insert("macro".to_string(), m.clone()); }
            
            if all_json.is_empty() {
                if app.loading_fundamentals || app.loading_technicals {
                    return "Loading all data...".to_string();
                } else {
                    return app.data_msg.clone();
                }
            }
            serde_json::to_string_pretty(&Value::Object(all_json)).unwrap_or_else(|_| "Failed".to_string())
        },
        DataTab::Fundamentals => {
            if app.loading_fundamentals {
                "Loading Fundamentals...".to_string()
            } else if let Some(val) = &app.fundamentals {
                serde_json::to_string_pretty(val).unwrap_or_else(|_| "Failed to format JSON".to_string())
            } else {
                "No Data".to_string()
            }
        },
        DataTab::Technicals => {
            if app.loading_technicals {
                "Loading Technicals...".to_string()
            } else if let Some(val) = &app.technicals {
                serde_json::to_string_pretty(val).unwrap_or_else(|_| "Failed to format JSON".to_string())
            } else {
                "No Data".to_string()
            }
        },
        DataTab::News => {
            if app.loading_news {
                "Loading News...".to_string()
            } else if let Some(val) = &app.news {
                serde_json::to_string_pretty(val).unwrap_or_else(|_| "Failed to format JSON".to_string())
            } else {
                "No Data".to_string()
            }
        },
        DataTab::Sentiment => {
            if app.loading_sentiment {
                "Loading Sentiment (This may take longer due to rate limits)...".to_string()
            } else if let Some(val) = &app.sentiment {
                serde_json::to_string_pretty(val).unwrap_or_else(|_| "Failed to format JSON".to_string())
            } else {
                "No Data".to_string()
            }
        },
        DataTab::Macro => {
            if app.loading_macro {
                "Loading Macro...".to_string()
            } else if let Some(val) = &app.macro_data {
                serde_json::to_string_pretty(val).unwrap_or_else(|_| "Failed to format JSON".to_string())
            } else {
                "No Data".to_string()
            }
        },
    }
}

fn ui(f: &mut ratatui::Frame, app: &mut App) {
    if app.portfolio_mode {
        let area = f.size().inner(&ratatui::layout::Margin { vertical: 1, horizontal: 1 });
        portfolio::draw(f, area, &mut app.portfolio);
        return;
    }
    if app.analyzing {
        let area = f.size().inner(&ratatui::layout::Margin { vertical: 1, horizontal: 1 });
        analyze::draw(f, area, &mut app.analyze);
        return;
    }
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .margin(1)
        .constraints(
            [
                Constraint::Length(3), // Search bar
                Constraint::Length(3), // Controls
                Constraint::Length(3), // Tabs
                Constraint::Min(1),    // Main content
            ]
            .as_ref(),
        )
        .split(f.size());

    // --- Search Bar ---
    let input_title = match app.input_mode {
        InputMode::Normal => "Search Ticker (Press 'e' to edit, 'a' Analyze, 'p' Portfolio, 'q' quit)",
        InputMode::Editing => "Searching (Press Enter to search, Esc to cancel)",
    };
    
    let input_style = match app.input_mode {
        InputMode::Normal => Style::default(),
        InputMode::Editing => Style::default().fg(Color::Yellow),
    };
    
    let input = Paragraph::new(app.input.as_str())
        .style(input_style)
        .block(Block::default().borders(Borders::ALL).title(input_title));
    f.render_widget(input, chunks[0]);

    match app.input_mode {
        InputMode::Normal => {}
        InputMode::Editing => {
            f.set_cursor(chunks[0].x + app.input.len() as u16 + 1, chunks[0].y + 1);
        }
    }

    // --- Controls ---
    let view_text = match app.view_mode {
        ViewMode::RawData => "[*] Raw Data View   [ ] Agent Summary (Disabled)",
        ViewMode::AgentSummary => "[ ] Raw Data View   [*] Agent Summary (Disabled)",
    };
    let toggle_help = "  (Press 'a' Analyze | 'p' Portfolio | 'v' to toggle view | 'j/k' or 'Down/Up' to scroll | 'Tab' to change data section)";
    let controls = Paragraph::new(Line::from(vec![
        Span::styled(view_text, Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        Span::raw(toggle_help),
    ])).block(Block::default().borders(Borders::ALL).title("Controls"));
    f.render_widget(controls, chunks[1]);

    // --- Tabs (Data Sections) ---
    // Helper to conditionally append a loading spinner/indicator to the tab title
    let mut tab_titles: Vec<Line> = Vec::new();
    
    let mut all_title = DataTab::All.title().to_string();
    if app.loading_fundamentals || app.loading_technicals || app.loading_news || app.loading_sentiment || app.loading_macro {
        all_title.push_str(" [Loading...]");
    }
    tab_titles.push(Line::from(all_title));
    
    let mut fund_title = DataTab::Fundamentals.title().to_string();
    if app.loading_fundamentals { fund_title.push_str(" [Loading...]"); }
    tab_titles.push(Line::from(fund_title));

    let mut tech_title = DataTab::Technicals.title().to_string();
    if app.loading_technicals { tech_title.push_str(" [Loading...]"); }
    tab_titles.push(Line::from(tech_title));

    let mut news_title = DataTab::News.title().to_string();
    if app.loading_news { news_title.push_str(" [Loading...]"); }
    tab_titles.push(Line::from(news_title));

    let mut sent_title = DataTab::Sentiment.title().to_string();
    if app.loading_sentiment { sent_title.push_str(" [Loading...]"); }
    tab_titles.push(Line::from(sent_title));

    let mut macro_title = DataTab::Macro.title().to_string();
    if app.loading_macro { macro_title.push_str(" [Loading...]"); }
    tab_titles.push(Line::from(macro_title));
    
    let active_index = match app.active_tab {
        DataTab::All => 0,
        DataTab::Fundamentals => 1,
        DataTab::Technicals => 2,
        DataTab::News => 3,
        DataTab::Sentiment => 4,
        DataTab::Macro => 5,
    };

    let tabs = Tabs::new(tab_titles)
        .block(Block::default().borders(Borders::ALL).title("Data Sections (Async)"))
        .select(active_index)
        .style(Style::default().fg(Color::White))
        .highlight_style(Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD));
    f.render_widget(tabs, chunks[2]);

    // --- Main Content ---
    let main_title = match app.view_mode {
        ViewMode::RawData => format!("Raw Scraped Data: {}", app.active_tab.title()),
        ViewMode::AgentSummary => "Agent Summary (COMING SOON)".to_string(),
    };
    
    let content = match app.view_mode {
        ViewMode::RawData => format_json_content(app),
        ViewMode::AgentSummary => {
            "Agent execution is currently disabled.\nPress 'v' to switch back to Raw Data view.".to_string()
        }
    };
    
    let data_paragraph = Paragraph::new(content)
        .block(Block::default().borders(Borders::ALL).title(main_title))
        .scroll((app.scroll, 0))
        .wrap(Wrap { trim: true });
        
    f.render_widget(data_paragraph, chunks[3]);
}
