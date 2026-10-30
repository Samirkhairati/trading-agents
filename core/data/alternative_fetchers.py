import json
import logging
import time
import re
import html as html_mod
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import pandas as pd
import io
import ssl

# Fix for macOS Python SSL Certificate issue
ssl._create_default_https_context = ssl._create_unverified_context

logger = logging.getLogger(__name__)

USER_AGENT = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"

# ---------------------------------------------------------
# Reddit (RSS)
# ---------------------------------------------------------
def fetch_reddit_posts(ticker: str, limit: int = 5) -> str:
    """
    Fetch recent Reddit posts mentioning the ticker via public RSS.
    Fails soft: 2 attempts, short backoff.
    """
    subreddits = ["wallstreetbets", "stocks", "investing"]
    blocks = []
    MAX_RETRIES = 2

    def _strip_html(content: str) -> str:
        if not content:
            return ""
        # Reddit wraps selftext between SC_OFF / SC_ON markers
        if "<!-- SC_OFF -->" in content and "<!-- SC_ON -->" in content:
            content = content.split("<!-- SC_OFF -->")[1].split("<!-- SC_ON -->")[0]
        text = re.sub(r"<[^>]+>", " ", content)
        return " ".join(html_mod.unescape(text).split())

    for i, sub in enumerate(subreddits):
        if i > 0:
            time.sleep(1.0)  # Pace between subreddits

        qs = urllib.parse.urlencode({
            "q": ticker, "restrict_sr": "on",
            "sort": "new", "t": "week", "limit": limit
        })
        url = f"https://www.reddit.com/r/{sub}/search.rss?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        raw_xml = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw_xml = resp.read()
                break  # success
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # Exponential backoff: 5, 10, 20, 40, 80, 160s
                    wait = 2.0 * (2.0 ** (attempt - 1))
                    try:
                        if e.headers and "Retry-After" in e.headers:
                            wait = min(max(wait, float(e.headers["Retry-After"])), 3.0)
                    except (ValueError, TypeError):
                        pass
                    if attempt < MAX_RETRIES:
                        logger.warning(
                            f"Reddit 429 for r/{sub} (attempt {attempt}/{MAX_RETRIES}). "
                            f"Sleeping {wait:.0f}s..."
                        )
                        time.sleep(wait)
                    else:
                        logger.warning(f"Reddit r/{sub}: giving up after {MAX_RETRIES} attempts.")
                else:
                    logger.warning(f"Failed to fetch Reddit r/{sub} for {ticker}: HTTP {e.code}")
                    break
            except Exception as e:
                logger.warning(f"Failed to fetch Reddit r/{sub} for {ticker}: {e}")
                break

        if raw_xml is None:
            blocks.append(f"r/{sub}: <fetch failed after {MAX_RETRIES} attempts>")
            continue

        try:
            root = ET.fromstring(raw_xml)
            posts = []
            atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", atom_ns)[:limit]:
                title_el = entry.find("atom:title", atom_ns)
                content_el = entry.find("atom:content", atom_ns)
                published_el = entry.find("atom:published", atom_ns)

                title = (title_el.text or "").strip()
                body = _strip_html(content_el.text if content_el is not None else "")
                if len(body) > 240:
                    body = body[:240] + "…"

                date_str = ""
                if published_el is not None and published_el.text:
                    date_str = published_el.text[:10]

                entry_text = f"  [{date_str}] {title}"
                if body:
                    entry_text += f"\n    {body}"
                posts.append(entry_text)

            if posts:
                blocks.append(f"r/{sub} ({len(posts)} posts mentioning {ticker.upper()}):\n" + "\n".join(posts))
            else:
                blocks.append(f"r/{sub}: <no posts found mentioning {ticker.upper()} in the past 7 days>")
        except ET.ParseError as e:
            logger.warning(f"Failed to parse Reddit RSS for r/{sub}: {e}")
            blocks.append(f"r/{sub}: <parse error>")

    return "\n\n".join(blocks)

# ---------------------------------------------------------
# StockTwits
# ---------------------------------------------------------
def fetch_stocktwits_messages(ticker: str, limit: int = 30) -> str:
    """Fetch recent StockTwits messages for the ticker via public JSON endpoint."""
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker.upper()}.json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            
        messages = data.get("messages", [])
        if not messages:
            return f"<no StockTwits messages found for ${ticker.upper()}>"
            
        bullish = bearish = unlabeled = 0
        lines = []
        for m in messages[:limit]:
            sentiment = (m.get("entities", {}).get("sentiment") or {}).get("basic")
            body = (m.get("body") or "").replace("\n", " ").strip()
            
            if sentiment == "Bullish":
                bullish += 1
                tag = "Bullish"
            elif sentiment == "Bearish":
                bearish += 1
                tag = "Bearish"
            else:
                unlabeled += 1
                tag = "no-label"
                
            lines.append(f"[{tag}] {body[:150]}...")
            
        summary = f"StockTwits Sentiment -> Bullish: {bullish}, Bearish: {bearish}, Unlabeled: {unlabeled}\n"
        return summary + "\n".join(lines)
    except Exception as e:
        logger.warning(f"Failed to fetch StockTwits for {ticker}: {e}")
        return f"<stocktwits unavailable>"

# ---------------------------------------------------------
# FRED
# ---------------------------------------------------------
def fetch_fred_macro_data() -> str:
    """Fetch core macro indicators (CPI, Unemployment, Fed Funds) via FRED CSV downloads."""
    indicators = {
        "CPIAUCSL": "Inflation (CPI)",
        "UNRATE": "Unemployment Rate",
        "FEDFUNDS": "Federal Funds Rate"
    }
    
    blocks = []
    for symbol, name in indicators.items():
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={symbol}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                df = pd.read_csv(io.BytesIO(resp.read()))
                # Get the last 3 data points
                tail = df.tail(3).to_string(index=False)
                blocks.append(f"--- {name} ({symbol}) ---\n{tail}")
        except Exception as e:
            logger.warning(f"Failed to fetch FRED data for {symbol}: {e}")
            
    if not blocks:
        return "<fred macro data unavailable>"
    return "\n\n".join(blocks)

# ---------------------------------------------------------
# Polymarket
# ---------------------------------------------------------
def fetch_polymarket_odds(topic: str = "interest rates") -> str:
    """Fetch prediction market odds using Polymarket Gamma API."""
    url = f"https://gamma-api.polymarket.com/public-search?q={urllib.parse.quote(topic)}&limit_per_type=5"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            
        now = datetime.now(timezone.utc)
        lines = []
        
        for event in data.get("events", []):
            for m in event.get("markets", []):
                if m.get("closed"):
                    continue
                    
                prices = m.get("outcomePrices")
                outcomes = m.get("outcomes")
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                        outcomes = json.loads(outcomes)
                    except:
                        continue
                        
                if not prices or not outcomes:
                    continue
                    
                try:
                    prob = float(prices[0])
                    label = outcomes[0]
                    lines.append(f"- {m.get('question')} -> {label}: {prob:.0%} implied probability")
                except:
                    continue
                    
        if not lines:
            return f"<no open polymarket predictions for {topic}>"
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Failed to fetch Polymarket data: {e}")
        return "<polymarket unavailable>"
