"""Offline tests for the Joao Telegram trade alerts. No test reads the real
~/.env.shared, writes the real data/ directory or touches the network."""
import io
import json
import urllib.error
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types

import monitor
import telegram_notifications as tg
import trade_alerts as ta
import twitter_digest as tw
from storage import ledger_lock

BOT_ID = "-".join(["fake", "bot", "id"])
CHAT = "-1000000000001"          # fake id, never the real channel


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "SHARED_ENV", str(tmp_path / "no-such-env"))
    monkeypatch.setattr(ta, "STATE_PATH", str(tmp_path / "state.json"))
    for key in (tg.TOKEN_KEY, tg.CHAT_KEY, tg.ENABLED_KEY, tg.PLANS_KEY):
        monkeypatch.delenv(key, raising=False)


def ev(asset="BTC", direction="short", action="open", stance="own_current",
       evidence="I decided to enter SHORT", timeframe="", reason="", prices=()):
    return {"asset": asset, "direction": direction, "action": action, "stance": stance,
            "timeframe": timeframe, "evidence": evidence, "reason": reason,
            "stated_prices": list(prices)}


def rec(tid, created, events, text="post text"):
    return {"tweet_id": str(tid), "created_at": created, "url": f"https://x.com/j/status/{tid}",
            "text": text, "trade_events": events}


# --- telegram sender ---------------------------------------------------------
class Resp:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, payload):
    return urllib.error.HTTPError("https://api.telegram.org/botX/sendMessage", code, "x", {},
                                  io.BytesIO(json.dumps(payload).encode()))


def test_send_success_returns_message_id_and_plain_text():
    seen = []

    def opener(req, timeout):
        seen.append(json.loads(req.data))
        return Resp({"ok": True, "result": {"message_id": 77}})
    assert tg.send_message(BOT_ID, CHAT, "hello <b>x</b>", opener=opener) == 77
    assert "parse_mode" not in seen[0] and seen[0]["chat_id"] == CHAT


def test_send_429_waits_retry_after_then_succeeds():
    calls, naps = [], []

    def opener(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise http_error(429, {"description": "slow", "parameters": {"retry_after": 3}})
        return Resp({"ok": True, "result": {"message_id": 5}})
    assert tg.send_message(BOT_ID, CHAT, "t", opener=opener, sleep=naps.append) == 5
    assert naps == [3]


def test_send_long_rate_limit_is_left_for_next_run():
    naps = []

    def opener(req, timeout):
        raise http_error(429, {"parameters": {"retry_after": 3600}})
    with pytest.raises(tg.SendError) as exc:
        tg.send_message(BOT_ID, CHAT, "t", opener=opener, sleep=naps.append)
    assert exc.value.transient and naps == []          # never sleeps an hour inside the cron run


def test_send_permanent_error_is_not_retried_and_token_is_scrubbed():
    calls = []

    def opener(req, timeout):
        calls.append(1)
        raise http_error(400, {"description": f"Bad Request: chat not found {BOT_ID}"})
    with pytest.raises(tg.SendError) as exc:
        tg.send_message(BOT_ID, CHAT, "t", opener=opener, sleep=lambda s: None)
    assert not exc.value.transient and len(calls) == 1
    assert BOT_ID not in str(exc.value)


def test_send_network_errors_retry_then_raise_transient():
    def opener(req, timeout):
        raise urllib.error.URLError(f"boom {BOT_ID}")
    with pytest.raises(tg.SendError) as exc:
        tg.send_message(BOT_ID, CHAT, "t", opener=opener, sleep=lambda s: None, attempts=3)
    assert exc.value.transient and BOT_ID not in str(exc.value)


def test_send_rejects_ok_false():
    with pytest.raises(tg.SendError):
        tg.send_message(BOT_ID, CHAT, "t", opener=lambda r, timeout: Resp({"ok": False}),
                        sleep=lambda s: None, attempts=1)


def test_env_lookup_prefers_environment_then_shared_file(tmp_path):
    path = tmp_path / "env"
    path.write_text(f'# c\nexport {tg.TOKEN_KEY}="{BOT_ID}"\n{tg.CHAT_KEY}={CHAT}\n{tg.ENABLED_KEY}=1\n')
    assert tg.get_config(str(path), environ={}) == (BOT_ID, CHAT)
    assert tg.get_config(str(path), environ={tg.CHAT_KEY: "-9"})[1] == "-9"
    assert tg.flag(tg.ENABLED_KEY, str(path), {}) and not tg.flag(tg.PLANS_KEY, str(path), {})
    with pytest.raises(tg.ConfigError):
        tg.get_config(str(tmp_path / "missing"), environ={})


# --- extraction --------------------------------------------------------------
class FakeClient:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = 0
        self.models = self

    def generate_content(self, **kw):
        self.calls += 1
        item = self.payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=json.dumps(item), usage_metadata=SimpleNamespace(
            prompt_token_count=10, candidates_token_count=20, thoughts_token_count=0,
            tool_use_prompt_token_count=0))


class Tally:
    def __init__(self):
        self.oks = self.fails = 0

    def ok(self):
        self.oks += 1

    def fail(self):
        self.fails += 1


def extract(client, text, tally=None):
    return ta.extract_events(client, text, "2026-09-23", model="m",
                             thinking=monitor.GEMINI_THINKING, tally=tally or Tally(),
                             types=genai_types, errors=genai_errors)


def test_evidence_must_be_a_verbatim_quote():
    text = "I'm SHORT   in the short term\non BTC, ETH!"
    assert ta.evidence_in_text("I'm SHORT in the short term", text)
    assert ta.evidence_in_text("I’m short ... on BTC", text)
    assert not ta.evidence_in_text("I am short on BTC", text)
    assert not ta.evidence_in_text("", text)


def test_extract_normalizes_assets_and_keeps_verified_events():
    text = "I decided to enter SHORT positions on Bitcoin"
    client = FakeClient({"events": [ev(asset="$Bitcoin", evidence="decided to enter SHORT"),
                                    ev(asset="solana:So111", evidence="enter SHORT")]})
    tally = Tally()
    events, rejected, i, o = extract(client, text, tally)
    assert [e["asset"] for e in events] == ["BTC", ""] and rejected == [] and (i, o) == (10, 20)
    assert tally.oks == 1


def test_extract_retries_unverified_evidence_then_rejects_it():
    text = "I decided to enter SHORT positions"
    bad = {"events": [ev(evidence="invented words")]}
    client = FakeClient(bad, bad)
    events, rejected, *_ = extract(client, text)
    assert client.calls == 2 and events == [] and len(rejected) == 1


def test_extract_second_attempt_can_recover():
    text = "I decided to enter SHORT positions"
    client = FakeClient({"events": [ev(evidence="invented")]},
                        {"events": [ev(evidence="decided to enter SHORT")]})
    events, rejected, *_ = extract(client, text)
    assert len(events) == 1 and rejected == []


def test_extract_api_error_and_bad_json_mean_unclassified():
    tally = Tally()
    err = genai_errors.APIError(500, {"error": {"message": "x"}})
    assert extract(FakeClient(err), "t", tally) is None
    assert extract(FakeClient({"nope": 1}), "t", tally) is None
    assert tally.fails == 2


def test_extract_empty_events_is_a_valid_classification():
    events, rejected, *_ = extract(FakeClient({"events": []}), "market analysis")
    assert events == [] and rejected == []


# --- decide ------------------------------------------------------------------
def test_decide_rules():
    pos = {}
    assert ta.decide(ev(action="existing", asset=""), pos, False) == (False, "existing_no_asset")
    assert ta.decide(ev(action="existing"), pos, False) == (True, "existing")
    assert ta.decide(ev(action="existing"), pos, False) == (False, "existing_repeat")
    assert ta.decide(ev(action="add"), pos, False) == (True, "add")
    assert ta.decide(ev(action="reduce"), pos, False) == (True, "reduce")
    assert pos["BTC:short"]["status"] == "open"
    assert ta.decide(ev(action="close"), pos, False) == (True, "close")
    assert pos["BTC:short"]["status"] == "closed"
    assert ta.decide(ev(action="existing"), pos, False) == (True, "existing")   # reopened -> new info
    assert ta.decide(ev(action="open", asset=""), pos, False) == (True, "open")
    assert ta.decide(ev(action="close", asset=""), pos, False) == (True, "close")
    assert ta.decide(ev(stance="own_past_result"), pos, True) == (False, "retrospective")
    assert ta.decide(ev(stance="own_plan", direction="long"), pos, False) == (False, "plan_off")
    assert ta.decide(ev(stance="own_plan", direction="long"), pos, True) == (True, "plan")
    assert "BTC:long" not in pos                                   # a plan is not a position


def test_opposite_direction_is_not_a_repeat():
    pos = {"BTC:short": {"status": "open"}}
    assert ta.decide(ev(direction="long", action="existing"), pos, False) == (True, "existing")


# --- message -----------------------------------------------------------------
def test_seven_assets_in_one_post_make_one_message():
    events = [ev(asset=a, timeframe="short term", reason="Szerkezeti gyengeség",
                 evidence="I'm SHORT in the short term") for a in
              ("BTC", "ETH", "HYPE", "SOL", "DOGE", "ZEC", "TAKE")]
    text = ta.render_message("https://x.com/j/status/1", "2026-09-23T15:52:09+00:00", events)
    assert text.count("Eszköz:") == 1 and "BTC, ETH, HYPE, SOL, DOGE, ZEC, TAKE" in text
    assert text.startswith("Joao Wedson - SHORT: új belépés")
    assert "Poszt: 2026-09-23 15:52 UTC" in text and "nincs megadva" in text
    assert text.endswith("https://x.com/j/status/1")


def test_message_without_asset_says_so_and_plan_is_labelled():
    text = ta.render_message("u", "2026-11-12T16:02:00+00:00",
                             [ev(asset="", action="open"),
                              ev(asset="BTC", direction="long", stance="own_plan", prices=["$60k"])])
    assert text.startswith("Joao Wedson - több pozícióközlés")
    assert "nincs megnevezve a posztban" in text
    assert "LONG: terv (még nem nyitott pozíció)" in text and "$60k" in text


def test_partially_named_assets_are_flagged():
    text = ta.render_message("u", "2026-11-12T16:02:00+00:00", [ev(asset="BTC"), ev(asset="")])
    assert "meg nem nevezett" in text


# --- state -------------------------------------------------------------------
def test_state_load_distinguishes_missing_from_corrupt(tmp_path):
    p = tmp_path / "s.json"
    assert ta.load_state(str(p)) is None
    p.write_text("{broken")
    with pytest.raises(ta.AlertStateError):
        ta.load_state(str(p))
    p.write_text(json.dumps({"version": 1}))
    with pytest.raises(ta.AlertStateError):
        ta.load_state(str(p))
    assert p.read_text() == json.dumps({"version": 1})              # untouched


def test_new_state_seeds_and_validates():
    state = ta.new_state(["1", 2], seeds=["btc:short", "$ETH:short"])
    assert set(state["seen"]) == {"1", "2"} and set(state["positions"]) == {"BTC:short", "ETH:short"}
    with pytest.raises(ValueError):
        ta.new_state([], seeds=["BTC:sideways"])


# --- ingest ------------------------------------------------------------------
def test_baseline_and_old_records_never_alert():
    state = ta.new_state(["1"])
    records = [rec(1, "2026-09-23T15:00:00+00:00", [ev()]),
               {"tweet_id": "2", "created_at": "2026-09-24T00:00:00+00:00", "text": "old, no field"}]
    assert ta.ingest(state, records, enabled=True, plans_enabled=False) == 0
    assert state["queue"] == [] and "2" not in state["seen"]


def test_disabled_keeps_a_shadow_log_and_tracks_positions_but_queues_nothing():
    state = ta.new_state([])
    n = ta.ingest(state, [rec(1, "2026-09-23T15:00:00+00:00", [ev()]),
                          rec(2, "2026-09-24T15:00:00+00:00", [ev(action="existing")])],
                  enabled=False, plans_enabled=False)
    assert n == 0 and state["queue"] == [] and len(state["shadow"]) == 1
    assert state["seen"] == {"1": "shadow", "2": "no_alert"}          # repeat suppressed even off


def test_enabling_later_cannot_send_a_backlog():
    state = ta.new_state([])
    ta.ingest(state, [rec(1, "2026-09-23T15:00:00+00:00", [ev()])], enabled=False, plans_enabled=False)
    ta.ingest(state, [rec(1, "2026-09-23T15:00:00+00:00", [ev()])], enabled=True, plans_enabled=False)
    assert state["queue"] == []


def test_force_ids_are_marked_not_queued():
    state = ta.new_state([])
    ta.ingest(state, [rec(9, "2026-09-23T15:00:00+00:00", [ev()])], enabled=True,
              plans_enabled=False, force_ids={"9"})
    assert state["queue"] == [] and state["seen"]["9"] == "force"


def test_chronological_order_and_repeat_suppression():
    state = ta.new_state([])
    records = [rec(3, "2026-11-14T15:19:00+00:00", [ev(action="existing")]),
               rec(1, "2026-06-27T15:14:00+00:00", [ev(action="existing")]),
               rec(2, "2026-09-04T13:28:00+00:00", [ev(action="existing")])]
    assert ta.ingest(state, records, enabled=True, plans_enabled=False) == 1
    assert [q["id"] for q in state["queue"]] == ["1"]
    assert state["seen"] == {"1": "queued", "2": "no_alert", "3": "no_alert"}


# --- dispatch ----------------------------------------------------------------
def init_state(path, ids=(), seeds=()):
    from storage import write_json_atomic
    write_json_atomic(path, ta.new_state(list(ids), seeds))


def dispatch(records, path, sender=None, enabled=True, **kw):
    env = {tg.ENABLED_KEY: "1" if enabled else "0", tg.TOKEN_KEY: BOT_ID, tg.CHAT_KEY: CHAT}
    return ta.dispatch(records, state_path=path, environ=env, sender=sender, **kw)


def test_enabled_without_state_is_a_hard_error_and_creates_nothing(tmp_path):
    path = str(tmp_path / "state.json")
    with pytest.raises(ta.AlertStateError, match="init"):
        dispatch([rec(1, "2026-09-23T15:00:00+00:00", [ev()])], path, sender=Mock())
    assert not (tmp_path / "state.json").exists()


def test_disabled_without_or_with_corrupt_state_is_quiet_and_untouched(tmp_path, capsys):
    path = tmp_path / "state.json"
    assert dispatch([rec(1, "2026-09-23T15:00:00+00:00", [ev()])], str(path), enabled=False) is None
    path.write_text("{broken")
    assert dispatch([], str(path), enabled=False) is None
    assert path.read_text() == "{broken"
    with pytest.raises(ta.AlertStateError):
        dispatch([], str(path), sender=Mock())
    assert path.read_text() == "{broken"


def test_dry_run_never_touches_state_or_sends(tmp_path):
    path = tmp_path / "state.json"
    init_state(str(path))
    before = path.read_text()
    sender = Mock()
    assert dispatch([rec(1, "2026-09-23T15:00:00+00:00", [ev()])], str(path), sender=sender,
                    dry_run=True) is None
    assert path.read_text() == before and sender.call_count == 0


def test_send_once_then_idempotent_rerun(tmp_path):
    path = str(tmp_path / "state.json")
    init_state(path)
    sender = Mock(return_value=4242)
    records = [rec(1, "2026-09-23T15:52:00+00:00", [ev(asset=a) for a in ("BTC", "ETH")])]
    assert dispatch(records, path, sender=sender) == {"queued": 1, "sent": 1, "failed": 0}
    assert dispatch(records, path, sender=sender) == {"queued": 0, "sent": 0, "failed": 0}
    assert sender.call_count == 1
    item = ta.load_state(path)["queue"][0]
    assert item["status"] == "sent" and item["message_id"] == 4242 and item["sent_at"]


def test_queue_is_durable_before_sending_and_transient_failure_retries(tmp_path):
    path = str(tmp_path / "state.json")
    init_state(path)
    records = [rec(1, "2026-09-23T15:52:00+00:00", [ev()])]

    def boom(text):
        assert ta.load_state(path)["queue"][0]["status"] == "pending"   # saved before the send
        raise tg.SendError("net down", transient=True)
    with pytest.raises(ta.AlertStateError):
        dispatch(records, path, sender=boom)
    item = ta.load_state(path)["queue"][0]
    assert item["status"] == "pending" and item["attempts"] == 1
    sender = Mock(return_value=1)
    assert dispatch([], path, sender=sender)["sent"] == 1              # no new posts: pending still goes out
    assert ta.load_state(path)["queue"][0]["status"] == "sent"


def test_transient_failure_keeps_order(tmp_path):
    path = str(tmp_path / "state.json")
    init_state(path)
    records = [rec(1, "2026-09-23T15:00:00+00:00", [ev(asset="BTC")]),
               rec(2, "2026-09-24T15:00:00+00:00", [ev(asset="ETH")])]
    sender = Mock(side_effect=tg.SendError("x", transient=True))
    with pytest.raises(ta.AlertStateError):
        dispatch(records, path, sender=sender)
    assert sender.call_count == 1                                       # 2nd not attempted before the 1st
    assert [q["status"] for q in ta.load_state(path)["queue"]] == ["pending", "pending"]


def test_permanent_failure_is_marked_failed_and_not_retried_forever(tmp_path):
    path = str(tmp_path / "state.json")
    init_state(path)
    sender = Mock(side_effect=tg.SendError("chat not found", transient=False))
    with pytest.raises(ta.AlertStateError):
        dispatch([rec(1, "2026-09-23T15:00:00+00:00", [ev()])], path, sender=sender)
    assert ta.load_state(path)["queue"][0]["status"] == "failed"
    assert dispatch([], path, sender=sender)["failed"] == 0              # not retried, not noisy
    assert sender.call_count == 1


def test_attempt_cap_turns_pending_into_failed(tmp_path):
    path = str(tmp_path / "state.json")
    init_state(path)
    sender = Mock(side_effect=tg.SendError("x", transient=True))
    for _ in range(ta.MAX_SEND_ATTEMPTS):
        with pytest.raises(ta.AlertStateError):
            dispatch([rec(1, "2026-09-23T15:00:00+00:00", [ev()])], path, sender=sender)
    assert ta.load_state(path)["queue"][0]["status"] == "failed"


def test_overlapping_runs_cannot_both_send(tmp_path):
    path = str(tmp_path / "state.json")
    init_state(path)
    sender = Mock()
    with ledger_lock(path):
        with pytest.raises(RuntimeError, match="Another writer"):
            dispatch([rec(1, "2026-09-23T15:00:00+00:00", [ev()])], path, sender=sender)
    assert sender.call_count == 0


def test_force_run_never_sends(tmp_path):
    path = str(tmp_path / "state.json")
    init_state(path)
    sender = Mock()
    dispatch([rec(7, "2026-09-23T15:00:00+00:00", [ev()])], path, sender=sender, force_ids={"7"})
    assert sender.call_count == 0 and ta.load_state(path)["seen"]["7"] == "force"


# --- twitter_digest integration ----------------------------------------------
def analysis_value():
    return {"overall_sentiment": "bearish", "market_view": "Valid prose.", "key_levels": [],
            "top_themes": ["theme"], "summary": "Valid prose."}


class ProcessClient(FakeClient):
    """Answers the analysis call and the trade-events call by schema."""

    def __init__(self, events=None, fail_events=False):
        super().__init__()
        self.events, self.fail_events = events or [], fail_events

    def generate_content(self, **kw):
        self.calls += 1
        schema = kw["config"].response_json_schema
        if schema is ta.EVENT_SCHEMA:
            if self.fail_events:
                raise genai_errors.APIError(500, {"error": {"message": "x"}})
            payload = {"events": self.events}
        else:
            payload = analysis_value()
        return SimpleNamespace(text=json.dumps(payload), usage_metadata=SimpleNamespace(
            prompt_token_count=1, candidates_token_count=1, thoughts_token_count=0,
            tool_use_prompt_token_count=0))


def raw_tweet(tid, text):
    return {"id": tid, "text": text, "createdAt": "Wed Sep 23 15:52:00 +0000 2026",
            "lang": "en", "url": f"https://x.com/joao_wedson/status/{tid}"}


def test_joao_process_adds_trade_events_other_feeds_do_not(monkeypatch):
    monkeypatch.setattr(tw, "_normalize_getxapi", lambda t: {
        "id": str(t["id"]), "text": t["text"], "created_at": "2026-09-23T15:52:00+00:00",
        "media": [], "author": "joao_wedson"})
    text = "I'm SHORT in the short term on BTC"
    client = ProcessClient(events=[ev(evidence="I'm SHORT in the short term")])
    records, *_ = tw.process([raw_tweet("1", text)], client, tw.FEEDS["joao_wedson"], allow_vision=False)
    assert records[0]["trade_events"][0]["asset"] == "BTC"
    client2 = ProcessClient()
    records2, *_ = tw.process([raw_tweet("2", text)], client2, tw.FEEDS["ki_young_ju"], allow_vision=False)
    assert "trade_events" not in records2[0] and client2.calls == 1


def test_failed_classification_leaves_the_post_unseen(monkeypatch):
    monkeypatch.setattr(tw, "_normalize_getxapi", lambda t: {
        "id": str(t["id"]), "text": t["text"], "created_at": "2026-09-23T15:52:00+00:00",
        "media": [], "author": "joao_wedson"})
    records, *_ = tw.process([raw_tweet("1", "I'm SHORT BTC")], ProcessClient(fail_events=True),
                             tw.FEEDS["joao_wedson"], allow_vision=False)
    assert records == []


def test_run_feed_dispatches_even_when_there_are_no_new_posts(tmp_path, monkeypatch):
    summaries = tmp_path / "s.json"
    summaries.write_text(json.dumps([rec(1, "2026-09-23T15:52:00+00:00", [])]))
    feed = replace(tw.FEEDS["joao_wedson"], summaries_file=str(summaries), current_view_file=None)
    monkeypatch.setattr(tw, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(tw, "fetch_posts", lambda *a: ([], 1))
    monkeypatch.setattr(tw, "refresh_current_view", lambda *a, **k: None)
    dispatched = Mock()
    monkeypatch.setattr(tw.trade_alerts, "dispatch", dispatched)
    args = SimpleNamespace(force=None, limit=None, dry_run=False, no_vision=True)
    tw.run_feed(feed, object(), args)
    assert dispatched.call_count == 1 and dispatched.call_args.kwargs["dry_run"] is False
    other = replace(tw.FEEDS["ki_young_ju"], summaries_file=str(summaries), current_view_file=None)
    tw.run_feed(other, object(), args)
    assert dispatched.call_count == 1                                  # other feeds never dispatch


def test_alert_failure_does_not_skip_the_current_view_refresh(tmp_path, monkeypatch):
    summaries = tmp_path / "s.json"
    summaries.write_text(json.dumps([rec(1, "2026-09-23T15:52:00+00:00", [])]))
    feed = replace(tw.FEEDS["joao_wedson"], summaries_file=str(summaries), current_view_file=None)
    monkeypatch.setattr(tw, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(tw, "fetch_posts", lambda *a: ([], 1))
    refresh = Mock()
    monkeypatch.setattr(tw, "refresh_current_view", refresh)
    monkeypatch.setattr(tw.trade_alerts, "dispatch", Mock(side_effect=ta.AlertStateError("x")))
    args = SimpleNamespace(force=None, limit=None, dry_run=False, no_vision=True)
    with pytest.raises(ta.AlertStateError):
        tw.run_feed(feed, object(), args)
    assert refresh.call_count == 1
