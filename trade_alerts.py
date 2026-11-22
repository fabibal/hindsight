#!/usr/bin/env python3
"""
trade_alerts.py - Telegram alerts for @joao_wedson's OWN long/short positions.

ANALYSIS ONLY: this only reads what he writes and tells the owner. It never
places an order and nothing here is wired to a broker.

Flow (all inside the existing daily twitter_digest.py run, no extra X fetch):
  post text -> extract_events() (one small dedicated Gemini call per Joao post)
            -> stored on the record as `trade_events` in joao_summaries.json
            -> dispatch(): decide() per event against the known-position book,
               one durable queue item per post, one Telegram message per post.

Safety rules (see docs/joao_telegram_alerts_plan_2026-10-15.md):
  * init marks every post already in the ledger as baseline: no history flood.
  * while JOAO_TRADE_ALERTS_ENABLED is off, new posts are still classified and
    the would-be message is kept in state["shadow"] (nothing is sent).
  * flag on + state file missing/corrupt -> hard error, never a silent re-init.
  * dry-run and --force runs never send.
  * delivery is at-least-once: a lost HTTP reply after a successful send can
    produce one duplicate; the queue item is saved BEFORE sending.

  python trade_alerts.py init [--seed BTC:short ...]   # one-time baseline
  python trade_alerts.py status
  python trade_alerts.py replay [--ledger P] [--cache P]   # chronological dry replay, no send
  python trade_alerts.py send-test                        # one marked test message
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

from storage import ledger_lock, write_json_atomic
import telegram_notifications as tg

HOME = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HOME, "data")
STATE_PATH = os.path.join(DATA_DIR, "joao_alert_state.json")
LEDGER_PATH = os.path.join(DATA_DIR, "joao_summaries.json")

STATE_VERSION = 1
SHADOW_CAP = 300
MAX_SEND_ATTEMPTS = 10          # one attempt per daily run -> ~10 days before "failed"
EXTRACT_ATTEMPTS = 2
IMAGE_CAP = 4

# --- extraction ------------------------------------------------------------
EVENT_SYSTEM = (
    "You read ONE X post written by @joao_wedson (Joao Wedson, founder of "
    "Alphractal, a crypto analytics platform). Decide whether the post says that "
    "HE HIMSELF holds, opens, changes or closes a long or short position, or that "
    "he plans one. Return one event per asset. Most posts are pure market "
    "analysis: then return an empty events list.\n"
    "Only the author's OWN trading counts. Do NOT create an event for: positions "
    "of whales, retail, top traders, exchanges, 'the market' or any third party "
    "(long/short ratios, open interest, liquidation levels, Hyperliquid or "
    "Bitfinex positioning); product or platform promotion; generic boasts with no "
    "stated position ('my shorts rarely fail', 'those who followed my shorts did "
    "well'); questions to the audience; opinions about where price may go; other "
    "people's calls. The words long/short inside a market analysis are NOT his "
    "position. Never infer a position from a link or another post. A negation "
    "('I am not short') gives no event.\n"
    "IMAGES: the post may carry images. A Binance Futures / Hyperliquid POSITION "
    "CARD (shows the asset e.g. 'TAKEUSDT Perpetual', Short or Long, 'Entry Price', "
    "PnL %) attached to HIS post is HIS position: return one event per card with "
    "source 'image', stance 'own_current', action 'existing' (use 'open' / 'add' / "
    "'reduce' / 'close' only if the post TEXT says he just did that), asset from the "
    "card title ('TAKEUSDT' -> TAKE, 'BTCUSDC' -> BTC), direction from the card, "
    "stated_prices = ['entry <exact entry price printed on the card>'], evidence = "
    "'image N: <card title and side as printed>' (N = 1-based image number). Images "
    "may use European number format: '1.658,85' = 1658.85, '86.519,9' = 86519.9, "
    "'0,1986488' = 0.1986488. Do NOT create an event from: cards or screenshots of "
    "other traders, whales or exchanges; liquidation heatmaps; ordinary charts, "
    "arrows or buy/sell markers without a position card; mark prices. Events whose "
    "source is 'text' use only the text and a verbatim quote as evidence.\n"
    "Fields per event:\n"
    "- asset: ticker in capitals (BTC, ETH, SOL ...) as named in the post "
    "('Bitcoin' -> BTC). Empty string if the post does not name the asset. Never "
    "guess.\n"
    "- direction: 'long' or 'short'.\n"
    "- action: 'open' (he says he enters / has just entered a new position), "
    "'existing' (he says he is in a position that is already open, e.g. 'still "
    "holding', 'I am short since X'; an earlier increase mentioned as past is also "
    "'existing'), 'add' (he says he increases it now), 'reduce' (he says he trims "
    "it now), 'close' (he says he closes / has closed it now).\n"
    "- stance: 'own_current' (a position he holds or changes now), 'own_plan' (he "
    "intends or conditionally plans a later entry or change: 'I will buy in "
    "September', 'I will go long when ...'), 'own_past_result' (a report on a "
    "finished trade or earlier call and its result, with no statement that it is "
    "still held).\n"
    "- timeframe: the horizon exactly as stated ('short term', 'next 2 months'), "
    "else empty string.\n"
    "- source: 'text' (stated in the post text) or 'image' (read from a position "
    "card image, see IMAGES).\n"
    "- evidence: for source 'text' the exact words from the post, copied verbatim "
    "(at most 200 characters; use '...' between two copied fragments); for source "
    "'image' 'image N: <card title and side>'.\n"
    "- reason: one short sentence IN HUNGARIAN with the reason he gives, empty "
    "string if none.\n"
    "- stated_prices: any entry, stop or target price he states for this event, as "
    "short strings; empty list if none. Never invent one.\n"
    "Spot accumulation plans ('accumulate on every dip') are direction 'long', "
    "action 'open', stance 'own_plan'. If one statement names several assets, "
    "return one event per asset with the same evidence.\n"
    "Return ONLY valid JSON matching the schema. No markdown, no preamble."
)

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asset": {"type": "string"},
                    "direction": {"type": "string", "enum": ["long", "short"]},
                    "action": {"type": "string",
                               "enum": ["open", "existing", "add", "reduce", "close"]},
                    "stance": {"type": "string",
                               "enum": ["own_current", "own_plan", "own_past_result"]},
                    "timeframe": {"type": "string"},
                    "source": {"type": "string", "enum": ["text", "image"]},
                    "evidence": {"type": "string"},
                    "reason": {"type": "string"},
                    "stated_prices": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["asset", "direction", "action", "stance", "timeframe",
                             "source", "evidence", "reason", "stated_prices"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["events"],
    "additionalProperties": False,
}

_ALIASES = {"BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL", "DOGECOIN": "DOGE",
            "HYPERLIQUID": "HYPE", "ZCASH": "ZEC", "RIPPLE": "XRP"}


def normalize_asset(raw):
    s = (raw or "").strip().lstrip("$").strip()
    if not s or ":" in s or len(s) > 12 or not re.fullmatch(r"[A-Za-z0-9.\- ]+", s):
        return ""
    s = s.upper()
    return _ALIASES.get(s, s)


def _norm_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    text = (text.replace("’", "'").replace("‘", "'")
            .replace("“", '"').replace("”", '"'))
    return re.sub(r"\s+", " ", text).strip().casefold()


def evidence_in_text(evidence, text):
    """Every fragment (split on '...' / ellipsis) must occur verbatim in the post."""
    parts = [p for p in (_norm_text(p) for p in re.split(r"\.{3}|…", evidence or "")) if p]
    hay = _norm_text(text)
    return bool(parts) and all(p in hay for p in parts)


def extract_events(client, text, date, *, model, thinking, tally, types, errors, media_urls=()):
    """Dedicated Joao call. Returns (events, rejected, in_tok, out_tok) or None.

    None means "could not be classified" (API error / unparsable / garbled text):
    the caller leaves the post unseen so the next daily run retries it, exactly
    like a failed text analysis. Events whose evidence is not a verbatim quote
    after the last attempt are moved to `rejected` (kept for audit, never alerted).
    """
    import monitor
    from llm_support import parse_response, token_usage
    in_tok = out_tok = 0
    # Large images: position-card prices must be read exactly (small ones misread
    # digits in a 2026-11-21 test).
    imgs = [b for b in (monitor._image_block(u, size="large")
                        for u in list(media_urls or ())[:IMAGE_CAP]) if b is not None]
    user = (f"Posted by @joao_wedson (Joao Wedson)\nDate: {date}\nPost:\n{text}"
            + (f"\n\n(Images attached: {len(imgs)}, numbered 1..)" if imgs else ""))
    contents = [types.Part.from_text(text=user), *imgs] if imgs else user
    for attempt in range(1, EXTRACT_ATTEMPTS + 1):
        try:
            resp = client.models.generate_content(
                model=model, contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=EVENT_SYSTEM,
                    response_mime_type="application/json",
                    response_json_schema=EVENT_SCHEMA,
                    thinking_config=thinking,
                    max_output_tokens=3000))
        except errors.APIError as exc:
            print(f"  [trade events error] {type(exc).__name__}: {exc}", file=sys.stderr)
            tally.fail()
            return None
        i, o = token_usage(resp)
        in_tok, out_tok = in_tok + i, out_tok + o
        parsed = parse_response(resp, EVENT_SCHEMA, monitor._unescape_strings)
        if parsed is None:
            tally.fail()
            return None
        good, bad = [], []
        for ev in parsed["events"]:
            ev = dict(ev, asset=normalize_asset(ev["asset"]))
            if ev["source"] == "image":      # cross-checked against the level read later
                ok = bool(imgs) and ev["evidence"].strip().lower().startswith("image") \
                    and bool(ev["stated_prices"]) and bool(ev["asset"])
            else:
                ok = evidence_in_text(ev["evidence"], text)
            (good if ok else bad).append(ev)
        garbled = monitor._looks_mangled([e["reason"] for e in good])
        if (bad or garbled) and attempt < EXTRACT_ATTEMPTS:
            print("  [retry] trade events: unverified evidence or garbled text", file=sys.stderr)
            continue
        if garbled:
            tally.fail()
            print("  [give-up] trade events still garbled; post stays unseen", file=sys.stderr)
            return None
        tally.ok()
        if bad:
            print(f"  [trade events] {len(bad)} event(s) dropped: evidence is not a "
                  "verbatim quote", file=sys.stderr)
        return good, bad, in_tok, out_tok
    return None


def _price_numbers(strings):
    out = []
    for text in strings:
        for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(text)):
            try:
                out.append(float(m.group(0).replace(",", "")))
            except ValueError:
                pass
    return out


def reconcile_image_events(events, levels):
    """Image-sourced events are accepted only when the independent level
    extraction (joao_levels, own call + schema) read the same card: an own_entry
    on the same asset and side whose price matches the entry the event states
    (within 0.5%). The event's price is then replaced by the level's exact value.
    An image event on a (asset, side) that a text event already covers is dropped
    as a duplicate. Returns (accepted, rejected)."""
    text_keys = {_key(e) for e in events if e.get("source", "text") == "text"}
    accepted, rejected = [], []
    for ev in events:
        if ev.get("source", "text") == "text":
            accepted.append(ev)
            continue
        if _key(ev) in text_keys:
            rejected.append(dict(ev, rejected_reason="duplicate_of_text_event"))
            continue
        stated = _price_numbers(ev["stated_prices"])
        cands = [lv for lv in levels
                 if lv.get("kind") == "own_entry" and lv.get("direction") == ev["direction"]
                 and normalize_asset(lv.get("asset")) == ev["asset"]
                 and any(abs(lv["low"] - n) <= 0.005 * abs(lv["low"]) for n in stated)]
        # closest to the stated price; a card reading beats a text one on a tie
        match = min(cands, key=lambda lv: (min(abs(lv["low"] - n) / abs(lv["low"]) for n in stated),
                                           lv.get("source") != "chart"), default=None)
        if match is None:
            rejected.append(dict(ev, rejected_reason="no_matching_level_read"))
            continue
        accepted.append(dict(ev, stated_prices=[f"{ev['asset']} belépő {match['low']:.10g}"]))
    return accepted, rejected


# --- decision --------------------------------------------------------------
def _key(event):
    return f"{event['asset'] or '?'}:{event['direction']}"


def decide(event, positions, plans_enabled):
    """Return (alert: bool, label). Mutates `positions` only for alerted events.

    The only suppressions are: not-his-own/retrospective events, plans while the
    plans switch is off, and an `existing` repeat of a position we already know.
    An `existing` with no named asset NEVER alerts; an open/add/close with no
    named asset does (the message says the asset is not named).
    """
    stance, action, asset = event["stance"], event["action"], event["asset"]
    if stance == "own_past_result":
        return False, "retrospective"
    if stance == "own_plan":
        return (plans_enabled, "plan") if plans_enabled else (False, "plan_off")
    key = _key(event)
    book = positions.get(key)
    if action == "existing":
        if not asset:
            return False, "existing_no_asset"
        if book and book.get("status") == "open":
            return False, "existing_repeat"
        positions[key] = {"status": "open", "via": "existing"}
        return True, "existing"
    if action in ("open", "add"):
        if asset:
            positions[key] = {"status": "open", "via": action}
        return True, action
    if action == "reduce":
        return True, "reduce"
    if action == "close":
        if asset and key in positions:
            positions[key] = {"status": "closed", "via": "close"}
        return True, "close"
    return False, "unknown_action"


# --- message ---------------------------------------------------------------
_ACTION_HU = {"open": "új belépés", "existing": "meglévő pozíció közlése",
              "add": "pozíció növelése", "reduce": "pozíció csökkentése",
              "close": "pozíció zárása"}


def _fmt_time(iso):
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso or "ismeretlen idő"


def render_message(post_url, post_time, events):
    groups = {}
    for ev in events:
        groups.setdefault((ev["stance"], ev["action"], ev["direction"]), []).append(ev)
    blocks = []
    for (stance, action, direction), evs in groups.items():
        label = (f"{direction.upper()}: terv (még nem nyitott pozíció)"
                 if stance == "own_plan" else f"{direction.upper()}: {_ACTION_HU[action]}")
        assets = [e["asset"] for e in evs if e["asset"]]
        lines = [label,
                 "Eszköz: " + (", ".join(dict.fromkeys(assets)) if assets
                               else "nincs megnevezve a posztban")]
        if assets and len(assets) < len(evs):
            lines[-1] += " (és további, meg nem nevezett eszköz)"
        tfs = list(dict.fromkeys(e["timeframe"] for e in evs if e["timeframe"]))
        if tfs:
            lines.append("Időtáv: " + "; ".join(tfs[:2]))
        reason = next((e["reason"] for e in evs if e["reason"]), "")
        if reason:
            lines.append("Indok: " + reason)
        prices = list(dict.fromkeys(p for e in evs for p in e["stated_prices"]))
        lines.append("Megadott ár/stop/cél: " + ("; ".join(prices) if prices else "nincs megadva"))
        quote = evs[0]["evidence"].strip().replace("\n", " ")
        if evs[0].get("source") == "image":
            lines.append("Forrás: a poszt képe (pozíciókártya), nem a szöveg")
        else:
            lines.append(f'Idézet: "{quote[:200]}"')
        blocks.append("\n".join(lines))
    head = ("Joao Wedson - " + blocks[0].split("\n", 1)[0] if len(blocks) == 1
            else "Joao Wedson - több pozícióközlés egy posztban")
    if len(blocks) == 1:
        blocks[0] = blocks[0].split("\n", 1)[1]
    return (head + "\n\n" + "\n\n".join(blocks)
            + f"\n\nPoszt: {_fmt_time(post_time)}\nEredeti poszt: {post_url}")


# --- state -----------------------------------------------------------------
class AlertStateError(Exception):
    pass


def new_state(ledger_ids, seeds=(), now=None):
    now = now or datetime.now(timezone.utc).isoformat()
    positions = {}
    for seed in seeds:
        asset, _, direction = seed.partition(":")
        asset = normalize_asset(asset)
        if not asset or direction not in ("long", "short"):
            raise ValueError(f"bad seed {seed!r} (expected ASSET:long|short)")
        positions[f"{asset}:{direction}"] = {"status": "open", "via": "seed"}
    return {"version": STATE_VERSION, "initialized_at": now,
            "seen": {str(i): "baseline" for i in ledger_ids},
            "positions": positions, "queue": [], "shadow": []}


def load_state(path):
    """None if the file does not exist; AlertStateError if it exists but is bad."""
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise AlertStateError(f"alert state unreadable: {path} ({type(exc).__name__})") from None
    ok = (isinstance(state, dict) and state.get("version") == STATE_VERSION
          and isinstance(state.get("seen"), dict) and isinstance(state.get("positions"), dict)
          and isinstance(state.get("queue"), list) and isinstance(state.get("shadow"), list)
          and all(isinstance(q, dict) and q.get("status") in ("pending", "sent", "failed")
                  and isinstance(q.get("text"), str) for q in state["queue"]))
    if not ok:
        raise AlertStateError(f"alert state has an invalid shape: {path} "
                              "(never auto-reinitialized; fix or restore it)")
    return state


def ingest(state, records, *, enabled, plans_enabled, force_ids=frozenset(), now=None):
    """Classify not-yet-seen records (oldest first) against the position book.
    Pure with respect to IO. Records without a `trade_events` key (processed
    before this feature) are ignored and not marked."""
    now = now or datetime.now(timezone.utc).isoformat()
    new = [r for r in records if "trade_events" in r and str(r["tweet_id"]) not in state["seen"]]
    new.sort(key=lambda r: (r.get("created_at") or "", str(r["tweet_id"])))
    queued = 0
    for rec in new:
        tid = str(rec["tweet_id"])
        if tid in force_ids:
            state["seen"][tid] = "force"
            continue
        alerting = []
        for ev in rec["trade_events"]:
            fire, _label = decide(ev, state["positions"], plans_enabled)
            if fire:
                alerting.append(ev)
        if not alerting:
            state["seen"][tid] = "no_alert"
            continue
        text = render_message(rec.get("url") or f"https://x.com/joao_wedson/status/{tid}",
                              rec.get("created_at"), alerting)
        if enabled:
            state["seen"][tid] = "queued"
            state["queue"].append({"id": tid, "post_id": tid, "post_time": rec.get("created_at"),
                                   "text": text, "events": alerting, "status": "pending",
                                   "attempts": 0, "last_error": None, "message_id": None,
                                   "created_at": now, "sent_at": None})
            queued += 1
        else:
            state["seen"][tid] = "shadow"
            state["shadow"].append({"id": tid, "post_time": rec.get("created_at"), "text": text})
            del state["shadow"][:-SHADOW_CAP]
    return queued


def flush(state, send, save, *, now_fn=None):
    """Send pending items oldest first; `save()` persists after every change."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
    sent = failed = 0
    for item in state["queue"]:
        if item["status"] != "pending":
            continue
        try:
            item["message_id"] = send(item["text"])
        except tg.SendError as exc:
            item["attempts"] += 1
            item["last_error"] = str(exc)[:300]
            if not exc.transient or item["attempts"] >= MAX_SEND_ATTEMPTS:
                item["status"] = "failed"
            save()
            failed += 1
            if exc.transient:
                break            # keep order; the next daily run retries
            continue
        item["status"] = "sent"
        item["sent_at"] = now_fn()
        item["last_error"] = None
        save()
        sent += 1
    return sent, failed


def dispatch(records, *, dry_run=False, force_ids=frozenset(), state_path=None,
             env_path=None, environ=None, sender=None):
    """Called by twitter_digest after the ledger is written (and on the
    'no new posts' path so pending sends still go out)."""
    state_path = state_path or STATE_PATH
    if dry_run:
        return None
    enabled = tg.flag(tg.ENABLED_KEY, env_path, environ)
    plans = tg.flag(tg.PLANS_KEY, env_path, environ)
    with ledger_lock(state_path):
        try:
            state = load_state(state_path)
        except AlertStateError:
            if enabled:
                raise
            print("WARNING: joao alert state invalid; alerts are off, state untouched",
                  file=sys.stderr)
            return None
        if state is None:
            if enabled:
                raise AlertStateError("alerts are enabled but the state file is missing; run "
                                      "`python trade_alerts.py init` once (never auto-initialized)")
            return None

        def save():
            write_json_atomic(state_path, state)

        queued = ingest(state, records, enabled=enabled, plans_enabled=plans,
                        force_ids=set(force_ids or ()))
        save()                                   # queue is durable BEFORE any send
        sent = failed = 0
        if enabled:
            if sender is None:
                token, chat = tg.get_config(env_path, environ)
                sender = lambda text: tg.send_message(token, chat, text)   # noqa: E731
            sent, failed = flush(state, sender, save)
        print(f"joao alerts: enabled={enabled} queued={queued} sent={sent} "
              f"send_failures={failed} pending={sum(q['status'] == 'pending' for q in state['queue'])}")
        if failed:
            raise AlertStateError(f"{failed} alert send(s) failed this run; pending ones are "
                                  "retried on the next run (state kept)")
        return {"queued": queued, "sent": sent, "failed": failed}


# --- CLI -------------------------------------------------------------------
def _load_ledger(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _cmd_init(args):
    if os.path.exists(args.state):
        sys.exit(f"refusing: {args.state} already exists (never re-initialized)")
    ids = [r["tweet_id"] for r in _load_ledger(args.ledger)]
    state = new_state(ids, args.seed)
    os.makedirs(os.path.dirname(args.state), exist_ok=True)
    write_json_atomic(args.state, state)
    print(f"initialized: {len(ids)} baseline ids, positions seeded: {sorted(state['positions'])}")


def _cmd_status(args):
    state = load_state(args.state)
    if state is None:
        sys.exit("no state file (run init)")
    from collections import Counter
    print("enabled:", tg.flag(tg.ENABLED_KEY), "plans:", tg.flag(tg.PLANS_KEY))
    print("seen:", dict(Counter(state["seen"].values())))
    print("positions:", {k: v["status"] for k, v in state["positions"].items()})
    print("queue:", dict(Counter(q["status"] for q in state["queue"])))
    for q in state["queue"][-3:]:
        print(" ", q["id"], q["status"], q["attempts"], q["last_error"])
    print(f"shadow (would-be messages while disabled): {len(state['shadow'])}")
    for s in state["shadow"][-3:]:
        print("-----\n" + s["text"])


def _cmd_send_test(args):
    token, chat = tg.get_config()
    mid = tg.send_message(token, chat, "TESZT - Joao Wedson pozícióértesítő csatorna beállítva. "
                                       "Ez nem pozícióközlés.")
    print("sent test message_id", mid)


def _cmd_replay(args):
    from concurrent.futures import ThreadPoolExecutor
    from google import genai
    from google.genai import types, errors
    import monitor
    if args.env:
        monitor.load_env(args.env)
    records = sorted(_load_ledger(args.ledger), key=lambda r: (r["created_at"], r["tweet_id"]))
    cache = {}
    if os.path.exists(args.cache):
        cache = _load_ledger(args.cache)
    todo = [r for r in records if r["tweet_id"] not in cache]
    client = genai.Client()

    class Tally:
        ok = fail = staticmethod(lambda: None)

    def work(r):
        out = extract_events(client, r["text"], (r["created_at"] or "")[:10], model="gemini-3.7-flash",
                             thinking=monitor.GEMINI_THINKING, tally=Tally, types=types, errors=errors)
        return r["tweet_id"], out

    print(f"extracting {len(todo)} posts ({len(records) - len(todo)} cached)", flush=True)
    with ThreadPoolExecutor(args.workers) as pool:
        for n, (tid, out) in enumerate(pool.map(work, todo), 1):
            if out is not None:
                cache[tid] = {"events": out[0], "rejected": out[1], "in": out[2], "out": out[3]}
            if n % 25 == 0:
                write_json_atomic(args.cache, cache)
                print(f"  {n}/{len(todo)}", flush=True)
    write_json_atomic(args.cache, cache)
    state = new_state([])
    missing = 0
    sent = []
    for r in records:
        c = cache.get(r["tweet_id"])
        if c is None:
            missing += 1
            continue
        rec = dict(r, trade_events=c["events"])
        before = len(state["shadow"])
        ingest(state, [rec], enabled=False, plans_enabled=args.plans)
        if len(state["shadow"]) > before:
            sent.append((r, state["shadow"][-1]["text"]))
    print(f"\nreplay over {len(records)} posts: {len(sent)} would-be messages, "
          f"{missing} posts without classification, "
          f"in={sum(c['in'] for c in cache.values())} out={sum(c['out'] for c in cache.values())} tokens")
    for r, text in sent:
        print(f"\n===== {r['created_at'][:16]} {r['tweet_id']}\n{text}")
    rej = [(tid, c["rejected"]) for tid, c in cache.items() if c["rejected"]]
    print(f"\nposts with rejected (unverified) events: {len(rej)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "status", "replay", "send-test"):
        p = sub.add_parser(name)
        p.add_argument("--state", default=STATE_PATH)
        p.add_argument("--ledger", default=LEDGER_PATH)
        if name == "init":
            p.add_argument("--seed", action="append", default=[], metavar="ASSET:long|short")
        if name == "replay":
            p.add_argument("--cache", default=os.path.join(DATA_DIR, "joao_alert_replay_cache.json"))
            p.add_argument("--env", default=None, help="env file to load (GOOGLE_API_KEY)")
            p.add_argument("--workers", type=int, default=6)
            p.add_argument("--plans", action="store_true", help="also show plan messages")
    args = ap.parse_args()
    {"init": _cmd_init, "status": _cmd_status, "replay": _cmd_replay,
     "send-test": _cmd_send_test}[args.cmd](args)


if __name__ == "__main__":
    main()
