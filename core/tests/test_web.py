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
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TRADING_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", "pw-for-tests")
    monkeypatch.setattr(main, "fetch_price_history", lambda t: None)  # every ticker "exists"
    monkeypatch.setattr(sim, "_fetch", lambda s: 100.0)
    sim._cache.clear()


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


def test_queue_is_capped_and_tells_whats_ahead(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr("graph.trading_graph.run_analysis", lambda t: gate.wait(5))
    c = TestClient(main.app)
    jobs.submit("analysis", "user", ticker="AAA", ip="1.1.1.0")
    time.sleep(0.3)  # the worker picks AAA up and blocks in it
    for i, t in enumerate(["BBB", "CCC", "DDD"], 1):  # three wait
        jobs.submit("analysis", "user", ticker=t, ip=f"1.1.1.{i}")
    r = c.post("/api/queue", json={"ticker": "EEE"})
    assert r.status_code == 429 and "queue is full" in r.json()["error"]
    labels = [i["label"] for i in r.json()["queue"]["items"]]
    assert labels == ["AAA", "BBB", "CCC", "DDD"] and r.json()["queue"]["items"][0]["running"]
    assert c.post("/api/queue", json={"ticker": "bbb"}).status_code in (409, 429)
    assert c.post("/api/queue", json={"ticker": "not a ticker"}).status_code == 400
    gate.set()


def test_one_waiting_analysis_per_visitor(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr("graph.trading_graph.run_analysis", lambda t: gate.wait(5))
    jobs.submit("analysis", "user", ticker="AAA", ip="9.9.9.9")
    time.sleep(0.3)
    jobs.submit("analysis", "user", ticker="BBB", ip="9.9.9.9")  # AAA is running, BBB waits
    with pytest.raises(jobs.Refused):
        jobs.submit("analysis", "user", ticker="CCC", ip="9.9.9.9")
    gate.set()


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
