"""CLI: python run.py AAPL"""
import sys
import time

from dotenv import load_dotenv

load_dotenv()

import observability as obs
from graph.trading_graph import run_analysis

def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python run.py TICKER")
        return 2
    start = time.time()
    obs.init_tracing()
    try:
        state = run_analysis(
            sys.argv[1],
            on_progress=lambda stage, done, total: print(f"[{done}/{total}] {stage} ({time.time() - start:.0f}s)", flush=True),
        )
    except Exception as e:
        print(f"error: {str(e).splitlines()[0] if str(e) else type(e).__name__}")
        return 1
    d = state["final_decision"]
    print(f"\n=== {state['ticker']} {state['trade_date']} (last ${state['last_price']:.2f}) ===")
    print(f"Rating: {d['rating']}  Target: {d['target_price']}  Stop: {d['stop_loss']}  Size: {d['position_size']}%")
    from agents.base import usage
    print(f"LLM usage: {usage['calls']} calls, {usage['tokens']} tokens")
    print(f"\n{d['justification']}\n\nPast context: {state['past_context']}")
    return 0

if __name__ == "__main__":
    code = main()
    obs.shutdown()
    sys.exit(code)
