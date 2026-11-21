"""Joao Wedson level ledger: every explicit price level / metric threshold in a
post's text AND images, labelled by kind. Analysis only, nothing is traded.

One dedicated Gemini call per post (flash, thinking off, LARGE images): a
2026-11-21 20-post test found this matches Pro (same levels, 0 invented numbers)
at 1/8 the cost; dynamic thinking cost 5x with no gain. The levels live on each
post record in data/joao_summaries.json (`levels`, `levels_rejected`);
data/joao_levels.json is a derived flat file rebuilt from it (for grading).

    python joao_levels.py backfill [--limit N] [--dry-run]   # posts without `levels`
    python joao_levels.py rebuild                            # derived file only
"""
import argparse
import os
import re
import sys

import trade_alerts
from storage import ledger_lock, load_ledger, write_json_atomic

HOME = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HOME, "data")
LEDGER_PATH = os.path.join(DATA_DIR, "joao_summaries.json")
LEVELS_PATH = os.path.join(DATA_DIR, "joao_levels.json")

MAX_IMAGES = 4
MAX_OUTPUT_TOKENS = 16000
ATTEMPTS = 2

KINDS = ["own_entry", "own_target", "own_stop", "own_invalidation", "support", "resistance",
         "liquidation_cluster_long", "liquidation_cluster_short", "onchain_cost_basis",
         "onchain_threshold", "positioning_avg_entry", "price_ref", "other"]
SCHEMA = {
    "type": "object",
    "properties": {"levels": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "asset": {"type": "string"},
            "kind": {"type": "string", "enum": KINDS},
            "direction": {"type": "string", "enum": ["long", "short", "none"]},
            "low": {"type": "number"},
            "high": {"type": ["number", "null"]},
            "source": {"type": "string", "enum": ["text", "chart"]},
            "evidence": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["asset", "kind", "direction", "low", "high", "source", "evidence", "note"]}}},
    "required": ["levels"]}

SYSTEM = """You extract every explicit price level or metric threshold from ONE X post by @joao_wedson (Joao Wedson, founder of Alphractal, on-chain/quant analyst who also trades his own positions), including numbers printed in the attached images.

SCOPE - output only these two groups, nothing else:
 (a) levels of Joao's OWN positions (own_entry / own_target / own_stop / own_invalidation), any asset;
 (b) levels for BTC (support, resistance, liquidation clusters, on-chain cost basis / thresholds, other). Never output price_ref or positioning_avg_entry, and no levels of other assets (ETH, SOL, PEPE ...) unless they are his own position levels.

Output one entry per level:
- asset: ticker (BTC, ETH, SOL, PEPE ...). Resolve from the post and chart titles; a token written as chain:0xADDRESS or chain:native is not the chain's own coin (e.g. ethereum:0x6982...is PEPE), use the chart title / text to name it. For a market-wide metric use TOTAL.
- kind: own_entry (entry price of a position Joao himself holds, e.g. 'Entry Price' on a position card), own_target, own_stop, own_invalidation, support, resistance, liquidation_cluster_long / _short (liquidation levels from Alphractal data), onchain_cost_basis (realized price, cost basis, balanced price, mean price ...), onchain_threshold (a metric level he says to watch, incl. non-price metric values such as a zero line), positioning_avg_entry (average entry prices of trader cohorts), price_ref (a stated price print, a prior high/low, or 'price went from X to Y'), other.
- direction: long/short for own_* kinds, else none.
- low / high: the number; high only for a stated range, else null. Use plain numbers: 87K = 87000, $0.004341 = 0.004341, 1.29M = 1290000. Keep the exact digits printed, do not round.
- source: text if the number is in the post text; chart if it is printed in an image and NOT in the text.
- evidence: for text, an exact verbatim quote from the post containing the number; for chart, which image (1-based) and which printed label/panel the number is read from.
- note: ONE short Hungarian sentence with the meaning of the level.

Rules:
- Images (Binance cards/charts) may use European number format: '1.658,85' = 1658.85 and '86.519,9' = 86519.9 (dot = thousands, comma = decimal); '0,1986488' = 0.1986488. Convert to a plain number and sanity-check it against the asset's price scale.
- Only numbers that are written in the text or printed as labels in an image. Never estimate a level from an unlabeled gridline, heatmap band or line position, never compute or infer a number.
- Include position cards (entry prices, not PnL percentages or mark prices), 'Key Levels' panels, last-price tags, cohort average entries.
- Do not output percentages of positioning, dollar amounts of OI/liquidation volume, dates, holdings, ratios or time spans as levels. Axis tick labels are not levels.
- Do not add levels the post does not support. If there are none, return an empty list."""

# Dashboard scope (user, 2026-11-21): his own positions + BTC levels only. Anything
# else is not shown, so it is not recorded.
OUT_OF_SCOPE_KINDS = {"price_ref", "positioning_avg_entry"}


def in_scope(lv):
    kind = str(lv.get("kind"))
    if kind.startswith("own_"):
        return True
    return str(lv.get("asset")).upper() == "BTC" and kind not in OUT_OF_SCOPE_KINDS


_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def _text_numbers(text):
    out = []
    for m in re.finditer(r"(?<![\w.])([-\u2212]?)(\d[\d,]*(?:\.\d+)?)\s?([kKmMbBtT])?\b", text or ""):
        try:
            v = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        sign = -1 if m.group(1) else 1
        out.append(sign * v)
        if m.group(3):
            out.append(sign * v * _MULT[m.group(3).lower()])
    if re.search(r"\bzero\b", text or "", re.I):
        out.append(0.0)
    return out


def _close(a, b):
    return abs(a) <= 0.02 if b == 0 else abs(a - b) / abs(b) <= 0.005


def _values(lv):
    return [v for v in (lv["low"], lv.get("high")) if v is not None]


def verify_text_level(lv, text):
    """A text-sourced level must quote the post verbatim AND its number must be in
    the text. (Chart-sourced numbers cannot be checked offline.)"""
    if not trade_alerts.evidence_in_text(lv["evidence"], text):
        return False
    nums = _text_numbers(text)
    return all(any(_close(v, n) for n in nums) for v in _values(lv))


def extract_levels(client, text, date, media_urls, *, model, thinking, tally, types, errors):
    """Returns (levels, rejected, in_tok, out_tok) or None (could not be read:
    the caller leaves the post unseen / un-leveled so it is retried)."""
    import monitor
    from llm_support import parse_response, token_usage
    imgs = [b for b in (monitor._image_block(u, size="large")
                        for u in (media_urls or [])[:MAX_IMAGES]) if b is not None]
    parts = [types.Part.from_text(
        text=f"Posted by @joao_wedson\nDate: {date}\nPost text:\n{text}\n\n"
             f"(Images attached: {len(imgs)}, numbered 1..)")] + imgs
    in_tok = out_tok = 0
    for attempt in range(1, ATTEMPTS + 1):
        try:
            resp = client.models.generate_content(
                model=model, contents=parts,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM, response_mime_type="application/json",
                    response_json_schema=SCHEMA, thinking_config=thinking,
                    max_output_tokens=MAX_OUTPUT_TOKENS))
        except errors.APIError as exc:
            print(f"  [levels error] {type(exc).__name__}: {exc}", file=sys.stderr)
            tally.fail()
            return None
        i, o = token_usage(resp)
        in_tok, out_tok = in_tok + i, out_tok + o
        parsed = parse_response(resp, SCHEMA, monitor._unescape_strings)
        if parsed is None:
            tally.fail()
            return None
        good, bad = [], []
        for lv in (x for x in parsed["levels"]
                   if in_scope(dict(x, asset=trade_alerts.normalize_asset(x["asset"]) or x["asset"].strip().upper()))):
            lv = dict(lv, asset=trade_alerts.normalize_asset(lv["asset"]) or lv["asset"].strip().upper())
            if lv["source"] == "text":
                ok = verify_text_level(lv, text)
            else:
                ok = bool(imgs)             # a chart reading needs a chart
            (good if ok else bad).append(lv)
        garbled = monitor._looks_mangled([lv["note"] for lv in good])
        if (bad or garbled) and attempt < ATTEMPTS:
            print("  [retry] levels: unverified evidence or garbled text", file=sys.stderr)
            continue
        if garbled:
            tally.fail()
            print("  [give-up] levels still garbled; post stays un-leveled", file=sys.stderr)
            return None
        tally.ok()
        if bad:
            print(f"  [levels] {len(bad)} level(s) dropped: not verifiable against the post",
                  file=sys.stderr)
        return good, bad, in_tok, out_tok
    return None


def flatten(records):
    rows = []
    for r in records:
        for lv in r.get("levels") or []:
            if not in_scope(lv):
                continue
            rows.append({"tweet_id": str(r["tweet_id"]), "posted_at": r.get("created_at"),
                         "url": r.get("url"), **lv})
    rows.sort(key=lambda x: (x["posted_at"] or "", x["tweet_id"]))
    return rows


def rebuild(records, path=LEVELS_PATH):
    rows = flatten(records)
    write_json_atomic(path, rows)
    return len(rows)


def backfill(limit=None, dry_run=False):
    import monitor
    from google import genai
    from google.genai import errors as genai_errors, types as genai_types
    import twitter_digest as td
    monitor.load_env(monitor.ENV_FILE)
    client = genai.Client()
    tally = monitor.GeminiTally()
    in_total = out_total = done = 0
    with ledger_lock(LEDGER_PATH):
        ledger = load_ledger(LEDGER_PATH, [])
        todo = [r for r in ledger if "levels" not in r]
        if limit is not None:
            todo = todo[:limit]
        print(f"{len(todo)} post(s) without levels (of {len(ledger)})")
        for n, r in enumerate(todo, 1):
            res = extract_levels(client, r.get("text", ""), (r.get("created_at") or "")[:10],
                                 r.get("media") or [], model=td.MODEL, thinking=monitor.GEMINI_THINKING,
                                 tally=tally, types=genai_types, errors=genai_errors)
            if res is None:
                print(f"  [{n}/{len(todo)}] {r['tweet_id']} failed; left for the next run", file=sys.stderr)
                continue
            r["levels"] = res[0]
            if res[1]:
                r["levels_rejected"] = res[1]
            in_total += res[2]; out_total += res[3]; done += 1
            if not dry_run and done % 25 == 0:      # checkpoint: a crash keeps the work
                write_json_atomic(LEDGER_PATH, ledger)
            print(f"  [{n}/{len(todo)}] {r['tweet_id']} {len(res[0])} level(s)", flush=True)
        cost = (in_total * monitor.GEMINI_INPUT_PER_1M + out_total * monitor.GEMINI_OUTPUT_PER_1M) / 1e6
        print(f"done {done}; tokens in={in_total} out={out_total} (${cost:.4f})")
        if dry_run:
            print("[dry-run] not writing")
            return
        write_json_atomic(LEDGER_PATH, ledger)
        print(f"levels file: {rebuild(ledger)} level(s) -> {LEVELS_PATH}")
    monitor.report_gemini_outage(tally, "joao_levels backfill")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill"); b.add_argument("--limit", type=int); b.add_argument("--dry-run", action="store_true")
    sub.add_parser("rebuild")
    a = ap.parse_args()
    if a.cmd == "backfill":
        backfill(a.limit, a.dry_run)
    else:
        with ledger_lock(LEDGER_PATH):
            print(rebuild(load_ledger(LEDGER_PATH, [])), "level(s)")


if __name__ == "__main__":
    main()
