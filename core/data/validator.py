from datetime import datetime, timedelta
import pandas as pd

class StaleDataError(Exception):
    pass

def assert_not_stale(df: pd.DataFrame, ticker: str):
    """
    Validates that the last price update is within recent trading days
    to prevent agents from hallucinating on outdated info.
    """
    if df.empty:
        raise StaleDataError(f"No market data available for {ticker}")
    
    last_date = df.index[-1]
    
    # If index is tz-aware, make it naive for simple comparison
    if last_date.tz is not None:
        last_date = last_date.tz_localize(None)
        
    now = datetime.now()
    # Allow 4 days (to cover long weekends/holidays)
    if (now - last_date).days > 7:
        raise StaleDataError(f"Market data for {ticker} is stale. Last date: {last_date.strftime('%Y-%m-%d')}")
