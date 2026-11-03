import threading
import time

import pytest
from fastapi.testclient import TestClient

import activity
import jobs
import main
from trading import sim


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    jobs._waiting.clear()
    jobs._current = None
    main._hits.clear()  # the per-visitor rate limit must not leak between tests
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TRADING_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", "pw-for-tests")
    monkeypatch.setattr(main, "fetch_price_history", lambda t: None)  # every ticker "exists"
    monkeypatch.setattr(sim, "_fetch", lambda s: 100.0)
    sim._cache.clear()
    yield
    jobs._waiting.clear()
    _wait_for(lambda: jobs._current is None)  # no job may outlive its test (the worker is a global thread)


def _wait_for(cond, seconds: float = 10.0) -> None:
    end = time.monotonic() + seconds
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


@pytest.fixture
def gate():
    """Pipeline jobs block on this until the test ends, so the queue stays in a known state."""
    g = threading.Event()
    yield g
    g.set()


def _wait_idle(c: TestClient) -> dict:
    for _ in range(100):
        live = c.get("/api/live").json()
        if not live["running"] and not live["queue"]["items"]:
            return live
        time.sleep(0.1)
    raise AssertionError("queue did not drain")


def test_visitor_queues_an_analysis_and_it_is_saved(fake_llm, fake_data):
    c = TestClient(main.app)
    r = c.post("/api/queue", json={"ticker": "fake"})
    assert r.status_code == 200 and r.json()["position"] == 1
    live = _wait_idle(c)
    assert live["last"]["ok"] is True and live["last"]["label"] == "FAKE"

    rows = c.get("/api/analyses?ticker=fake").json()["analyses"]
    assert len(rows) == 1 and rows[0]["rating"] == "Overweight"
    rec = c.get(f"/api/analyses/{rows[0]['id']}").json()
    names = [s["name"] for s in rec["stages"]]
    assert names[-1] == "Portfolio Manager" and names.count("Bull Researcher") == 2  # two debate rounds
    assert c.get("/api/analyses/nope").status_code == 404
    assert c.get("/api/history/fake").json()["history"]


def test_queue_is_capped_and_tells_whats_ahead(monkeypatch, gate):
    monkeypatch.setattr("graph.trading_graph.run_analysis", lambda t: gate.wait(30))
    c = TestClient(main.app)
    jobs.submit("analysis", "user", ticker="AAA", ip="1.1.1.0")
    _wait_for(lambda: jobs._current is not None)  # the worker picked AAA up and blocks in it
    for i, t in enumerate(["BBB", "CCC", "DDD"], 1):  # three wait
        jobs.submit("analysis", "user", ticker=t, ip=f"1.1.1.{i}")
    r = c.post("/api/queue", json={"ticker": "EEE"})
    assert r.status_code == 429 and "queue is full" in r.json()["error"]
    labels = [i["label"] for i in r.json()["queue"]["items"]]
    assert labels == ["AAA", "BBB", "CCC", "DDD"] and r.json()["queue"]["items"][0]["running"]
    assert c.post("/api/queue", json={"ticker": "bbb"}).status_code in (409, 429)
    assert c.post("/api/queue", json={"ticker": "not a ticker"}).status_code == 400


def test_one_waiting_analysis_per_visitor(monkeypatch, gate):
    monkeypatch.setattr("graph.trading_graph.run_analysis", lambda t: gate.wait(30))
    jobs.submit("analysis", "user", ticker="AAA", ip="9.9.9.9")
    _wait_for(lambda: jobs._current is not None)
    jobs.submit("analysis", "user", ticker="BBB", ip="9.9.9.9")  # AAA is running, BBB waits
    with pytest.raises(jobs.Refused):
        jobs.submit("analysis", "user", ticker="CCC", ip="9.9.9.9")


def test_no_public_way_to_run_the_daily_cycle_or_old_job_endpoints():
    c = TestClient(main.app)
    for path in ("/api/daily-run", "/api/analyze"):
        assert c.post(path, json={"ticker": "AAPL"}).status_code in (404, 405)
    for path in ("/api/admin/cash", "/api/admin/sell", "/api/admin/daily", "/api/admin/cancel"):
        assert c.post(path, json={"amount": 5, "symbol": "AAPL", "execute": True, "id": "x"}).status_code == 401
    assert c.get("/api/admin/overview").status_code == 401


def test_admin_login_cash_and_sell():
    c = TestClient(main.app)
    assert c.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    assert c.post("/api/admin/login", json={"password": "pw-for-tests"}).status_code == 200

    start = c.get("/api/admin/overview").json()["portfolio"]
    assert start["cash"] == 100_000 and start["mode"] == "simulated paper account"
    assert c.post("/api/admin/cash", json={"amount": 5000}).json()["cash"] == 105_000
    assert c.post("/api/admin/cash", json={"amount": -5}).status_code == 422

    b = sim.SimBroker()
    from trading.types import ProposedOrder
    b.place_order(ProposedOrder("AAPL", "buy", notional=1000, entry_price=100))
    ov = c.get("/api/admin/overview").json()["portfolio"]
    assert [p["symbol"] for p in ov["positions"]] == ["AAPL"] and ov["deposited"] == 105_000
    assert c.post("/api/admin/sell", json={"symbol": "MSFT"}).status_code == 400
    assert c.post("/api/admin/sell", json={"symbol": "aapl"}).json()["sold"] == ["AAPL"]
    after = c.get("/api/portfolio").json()
    assert after["positions"] == [] and after["cash"] == pytest.approx(105_000)
    assert [e["type"] for e in after["ledger"]][:2] == ["sell", "buy"]

    assert c.post("/api/admin/logout").status_code == 200
    assert c.get("/api/admin/overview").status_code == 401


def test_admin_login_locks_out_after_repeated_failures():
    import admin
    admin._fails.clear()
    c = TestClient(main.app)
    codes = [c.post("/api/admin/login", json={"password": "x"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    admin._fails.clear()


def test_public_ui_does_not_mention_admin_and_admin_page_is_served():
    c = TestClient(main.app)
    for f in ("index.html", "app.js", "style.css", "charts.js", "flow.js"):
        assert "admin" not in c.get("/" + f).text.lower()
    r = c.get("/admin")
    assert r.status_code == 200 and "noindex" in r.headers["x-robots-tag"]


def test_live_snapshot_marks_running_stages():
    activity.job_start("analysis", "X")
    activity.analysis_start("X", ["Fundamental Analyst", "Technical Analyst", "Sentiment Analyst", "News Analyst", "Bull Researcher"])
    activity.analysis_stage("Technical Analyst", "out")
    states = {s["name"]: s["state"] for s in activity.snapshot()["analysis"]["stages"]}
    assert states["Technical Analyst"] == "done" and states["Fundamental Analyst"] == "running" and states["Bull Researcher"] == "pending"
    activity.analysis_end()
    activity.job_end(True, True, "ok")
    assert activity.busy() is False


def test_schedule_endpoint_and_catch_up(monkeypatch):
    c = TestClient(main.app)
    assert c.get("/api/schedule").json()["rule"].endswith("America/New_York")
    assert "AAPL" in c.get("/api/watchlist").json()["tickers"]
    queued = []
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: queued.append((a, k)))
    from datetime import datetime
    monkeypatch.setattr(main, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: datetime(2026, 11, 3, 18, 0, tzinfo=tz)), "fromisoformat": datetime.fromisoformat}))
    main._catch_up()  # a Tuesday after 16:30 ET with no run recorded
    assert queued and queued[0][0][:2] == ("daily", "schedule")


def test_dashboard_lists_the_transactions_the_account_took():
    from trading.types import ProposedOrder
    c = TestClient(main.app)
    b = sim.SimBroker()
    b.deposit(500)
    b.place_order(ProposedOrder("AAPL", "buy", notional=1000, entry_price=100, reason="momentum"))
    b.place_order(ProposedOrder("AAPL", "sell", reason="thesis broke"))
    tx = c.get("/api/portfolio").json()["transactions"]
    assert [t["type"] for t in tx] == ["sell", "buy", "deposit"]
    sell, buy, dep = tx
    assert buy["qty"] == pytest.approx(10) and buy["amount"] == 1000 and buy["note"] == "momentum"
    assert sell["amount"] == pytest.approx(1000) and sell["pnl"] == 0 and sell["note"] == "thesis broke"
    assert dep["amount"] == 500 and dep["symbol"] is None


def test_alpaca_fills_become_transactions():
    from datetime import datetime, timezone
    from types import SimpleNamespace as NS
    from trading.broker import AlpacaBroker
    from trading.view import recent_transactions

    t = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    fills = [NS(filled_qty="2", filled_at=t, filled_avg_price="50", symbol="MSFT", side=NS(value="sell"), order_type=NS(value="stop")),
             NS(filled_qty="0", filled_at=None, filled_avg_price=None, symbol="X", side=NS(value="buy"), order_type=NS(value="market"))]
    broker = AlpacaBroker("k", "s")
    broker.trading_client = NS(get_orders=lambda req: fills)
    tx = recent_transactions(broker)
    assert len(tx) == 1 and tx[0]["type"] == "sell" and tx[0]["amount"] == 100 and tx[0]["note"] == "stop-loss hit"


def _daily_at(minutes_from_now: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.now(jobs.ET) + timedelta(minutes=minutes_from_now)).strftime("%H:%M")


def test_visitors_are_locked_out_while_the_daily_cycle_runs(monkeypatch, gate):
    monkeypatch.setattr("trading.daily_run.run_daily", lambda execute=False: gate.wait(30))
    c = TestClient(main.app)
    jobs.submit("daily", "admin")
    _wait_for(lambda: jobs._current is not None)
    r = c.post("/api/queue", json={"ticker": "AAA"})
    assert r.status_code == 423 and "daily cycle is running" in r.json()["error"]
    assert c.get("/api/live").json()["queue"]["lock"]["kind"] == "daily"
    gate.set()
    _wait_for(lambda: jobs._current is None)
    assert jobs.lock() is None


def test_visitors_are_locked_out_in_the_hour_before_the_daily_cycle(monkeypatch):
    monkeypatch.setattr("graph.trading_graph.run_analysis", lambda t: None)
    monkeypatch.setenv("ENABLE_SCHEDULER", "1")
    monkeypatch.setenv("DAILY_DAYS", "mon-sun")
    c = TestClient(main.app)
    monkeypatch.setenv("DAILY_AT", _daily_at(90))
    assert c.post("/api/queue", json={"ticker": "AAA"}).status_code == 200  # 90 min out: still open
    _wait_idle(c)
    monkeypatch.setenv("DAILY_AT", _daily_at(45))
    r = c.post("/api/queue", json={"ticker": "BBB"})
    assert r.status_code == 423 and "closed for the 60 minutes" in r.json()["error"]
    assert c.get("/api/live").json()["queue"]["lock"]["kind"] == "window"
    with pytest.raises(jobs.Refused):
        jobs.submit("analysis", "user", ticker="BBB", ip="2.2.2.2")
    jobs.submit("analysis", "admin", ticker="BBB")  # the owner is not locked out
    _wait_idle(c)


def test_a_waiting_visitor_analysis_waits_for_the_daily_cycle(monkeypatch, gate):
    monkeypatch.setattr("graph.trading_graph.run_analysis", lambda t: gate.wait(30))
    monkeypatch.setenv("ENABLE_SCHEDULER", "1")
    monkeypatch.setenv("DAILY_DAYS", "mon-sun")
    monkeypatch.setenv("DAILY_AT", _daily_at(180))
    jobs.submit("analysis", "user", ticker="AAA", ip="3.3.3.1")
    _wait_for(lambda: jobs._current is not None)
    jobs.submit("analysis", "user", ticker="BBB", ip="3.3.3.2")
    monkeypatch.setenv("DAILY_AT", _daily_at(30))  # the lock window opens while BBB waits
    gate.set()  # AAA finishes
    _wait_for(lambda: jobs._current is None)
    time.sleep(0.3)
    assert jobs._current is None and [j["ticker"] for j in jobs._waiting] == ["BBB"]  # held, not started
    assert jobs.snapshot()["items"][0]["held"] is True
    monkeypatch.setenv("DAILY_AT", _daily_at(180))  # window over (the cycle ran)
    with jobs._lock:
        jobs._lock.notify()
    _wait_for(lambda: not jobs._waiting and jobs._current is None)
