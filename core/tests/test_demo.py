"""Seeded demo portfolio: files land in the demo dir, equity curve ends at today's equity, no network."""
from trading import demo, state as st


def test_seed_and_load(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_STATE_DIR", str(tmp_path))
    out = demo.seed(days=30, rng_seed=1)
    hist = st.load_state()["equity_history"]
    assert len(hist) == 30 and abs(hist[-1]["equity"] - out["equity"]) < 0.01
    broker = demo.load_broker()
    acct = broker.get_account()
    assert abs(acct.equity - out["equity"]) < 0.01 and len(broker.get_positions()) == 6
    assert 5 < acct.cash / acct.equity * 100 < 30
    assert len(st.get_runs()) == 3


def test_demo_state_dir(monkeypatch):
    monkeypatch.delenv("TRADING_STATE_DIR", raising=False)
    monkeypatch.setenv("DEMO_PORTFOLIO", "1")
    assert st._dir().name == "demo"
