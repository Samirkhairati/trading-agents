import yfinance as yf
import pandas as pd
from typing import List, Dict, Any
import logging

INDICATORS = [
    "close_50_sma", "close_200_sma", "close_10_ema",
    "macd", "macds", "macdh", "rsi",
    "boll", "boll_ub", "boll_lb", "atr", "vwma", "mfi",
]

def fetch_price_history(ticker: str, period: str = "1y") -> pd.DataFrame:
    """OHLCV history plus stockstats indicators. Raises on empty or stale data."""
    from stockstats import wrap
    from data.validator import assert_not_stale

    df = yf.Ticker(ticker).history(period=period)
    assert_not_stale(df, ticker)
    df = wrap(df.copy())
    for ind in INDICATORS:
        df[ind]  # triggers the calculation
    return pd.DataFrame(df)

def price_records(df: pd.DataFrame, days: int = 30) -> Dict[str, Any]:
    """Last `days` rows as {date: {column: value}} (JSON-safe, for the API)."""
    recent = df.tail(days).copy()
    recent.index = [d.strftime("%Y-%m-%d") for d in recent.index]
    return recent.astype(object).where(recent.notna(), None).to_dict(orient="index")

def format_price_report(df: pd.DataFrame, days: int = 15) -> str:
    """Compact text for the Technical Analyst: recent rows plus a latest-indicator summary."""
    cols = ["open", "high", "low", "close", "volume"] + [c for c in INDICATORS if c in df.columns]
    recent = df.tail(days)[cols].round(2)
    last = df.iloc[-1]
    summary = (
        f"Latest close: {last['close']:.2f} on {df.index[-1].strftime('%Y-%m-%d')}\n"
        f"50 SMA: {last['close_50_sma']:.2f}, 200 SMA: {last['close_200_sma']:.2f}, "
        f"RSI: {last['rsi']:.1f}, MACD hist: {last['macdh']:.3f}, ATR: {last['atr']:.2f}"
    )
    return f"{summary}\n\nLast {days} trading days:\n{recent.to_string()}"

def fetch_fundamentals(ticker: str) -> Dict[str, Any]:
    """
    Gets exactly the 28 fundamental fields that TradingAgents extracts, plus
    the most recent quarterly financial statements.
    """
    t = yf.Ticker(ticker)
    info = t.info
    
    if not info:
        return {"error": "No fundamentals available"}
        
    # Extract the exact 28 fields used by TradingAgents
    fields = {
        "Name": info.get("longName"),
        "Sector": info.get("sector"),
        "Industry": info.get("industry"),
        "Market Cap": info.get("marketCap"),
        "PE Ratio (TTM)": info.get("trailingPE"),
        "Forward PE": info.get("forwardPE"),
        "PEG Ratio": info.get("pegRatio"),
        "Price to Book": info.get("priceToBook"),
        "EPS (TTM)": info.get("trailingEps"),
        "Forward EPS": info.get("forwardEps"),
        "Dividend Yield": info.get("dividendYield"),
        "Beta": info.get("beta"),
        "52 Week High": info.get("fiftyTwoWeekHigh"),
        "52 Week Low": info.get("fiftyTwoWeekLow"),
        "50 Day Average": info.get("fiftyDayAverage"),
        "200 Day Average": info.get("twoHundredDayAverage"),
        "Revenue (TTM)": info.get("totalRevenue"),
        "Gross Profit": info.get("grossProfits"),
        "EBITDA": info.get("ebitda"),
        "Net Income": info.get("netIncomeToCommon"),
        "Profit Margin": info.get("profitMargins"),
        "Operating Margin": info.get("operatingMargins"),
        "Return on Equity": info.get("returnOnEquity"),
        "Return on Assets": info.get("returnOnAssets"),
        "Debt to Equity": info.get("debtToEquity"),
        "Current Ratio": info.get("currentRatio"),
        "Book Value": info.get("bookValue"),
        "Free Cash Flow": info.get("freeCashflow"),
    }
    
    # Filter out None values
    clean_fields = {k: v for k, v in fields.items() if v is not None}
    
    # Try to grab the most recent quarter for financial statements
    try:
        bs = t.quarterly_balance_sheet
        if not bs.empty:
            clean_fields["Recent_Quarterly_Balance_Sheet"] = bs.iloc[:, 0].to_dict()
            
        cf = t.quarterly_cashflow
        if not cf.empty:
            clean_fields["Recent_Quarterly_Cashflow"] = cf.iloc[:, 0].to_dict()
            
        is_stmt = t.quarterly_income_stmt
        if not is_stmt.empty:
            clean_fields["Recent_Quarterly_Income_Statement"] = is_stmt.iloc[:, 0].to_dict()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to fetch financial statements: {e}")
        
    return clean_fields

def fetch_news(ticker: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Gets the latest news headlines using Yahoo Finance RSS feed.
    """
    import urllib.request
    import xml.etree.ElementTree as ET
    
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    
    formatted_news = []
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            root = ET.fromstring(resp.read())
            
        for item in root.findall("./channel/item")[:limit]:
            title = item.find("title")
            link = item.find("link")
            pubDate = item.find("pubDate")
            desc = item.find("description")
            
            # Clean up the description to remove HTML if necessary
            desc_text = desc.text if desc is not None else ""
            if desc_text and "<" in desc_text:
                import re
                desc_text = re.sub(r'<[^>]+>', '', desc_text)
            
            formatted_news.append({
                "title": title.text if title is not None else "No Title",
                "publisher": "Yahoo Finance",
                "link": link.text if link is not None else "",
                "providerPublishTime": pubDate.text if pubDate is not None else "",
                "description": desc_text.strip()
            })
    except Exception as e:
        logging.getLogger(__name__).warning(f"Failed to fetch Yahoo RSS news for {ticker}: {e}")
        
    return formatted_news
