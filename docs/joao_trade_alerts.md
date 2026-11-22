# Joao Wedson trade alerts (Telegram)

Analysis only. Reads what @joao_wedson writes about his OWN long/short
positions and posts one Telegram message per relevant post to a private channel.
Nothing is ever traded.

## How it works
Inside the existing daily `twitter_digest.py` run (no extra X fetch):

1. `trade_alerts.extract_events()` - one small dedicated Gemini call per Joao
   post (separate from the shared analysis call on purpose: a long multi-asset
   reply cannot truncate the post summary). Result is stored on the record as
   `trade_events`. A failed classification leaves the post unseen (retried the
   next day), exactly like a failed text analysis. Every event carries a verbatim
   quote; events whose quote is not literally in the post are dropped
   (`trade_events_rejected`).
2. `trade_alerts.decide()` applies fixed rules against a small position book:
   - own new entry / add / reduce / close -> alert
   - own "existing" position -> alert only the first time per asset+direction;
     an "existing" with no named asset never alerts
   - plans -> alert only with `JOAO_ALERT_PLANS_ENABLED`
   - retrospective result posts, whales/retail/market positioning -> never
3. One durable queue item per post (saved BEFORE sending), one message per post
   (a 7-asset post is one message). Delivery is at-least-once: a lost HTTP reply
   after a successful send can duplicate one message.

## Position cards in images (2026-11-21)
The event call also receives the post's images (large, up to 4). A Binance/Hyperliquid
position card in his post counts as his own position (`source: "image"`, usually `existing`).
An image event is accepted only if the independent level read (`joao_levels`, own call and
schema) found the same own entry (same asset and side, price within 0.5%); the message then
carries the level's exact price. If they disagree the levels are re-read up to twice
(`twitter_digest.LEVEL_RECHECKS`); still no agreement = no alert (kept in
`trade_events_rejected`). Replay on the known card posts: 10-08 post 8/8 entries, 6 negative
controls (heatmaps, ETF charts) 0 events.

## Files
- `trade_alerts.py` - schema, decision rules, state, dispatch, CLI
- `telegram_notifications.py` - stdlib sender; reads credentials from the
  environment or `~/.env.shared` only (never this project's `.env`)
- `data/joao_alert_state.json` (gitignored) - baseline ids, position book,
  queue, shadow log
- keys: `TELEGRAM_BOT_TOKEN`, `JOAO_TELEGRAM_CHAT_ID`,
  `JOAO_TRADE_ALERTS_ENABLED`, `JOAO_ALERT_PLANS_ENABLED`

## Operating it
    python trade_alerts.py init --seed BTC:short   # ONCE; refuses if state exists
    python trade_alerts.py status                  # counts, positions, last shadow messages
    python trade_alerts.py replay                  # chronological dry replay of the whole ledger, no send
    python trade_alerts.py send-test               # one marked test message

- Flag off (default): posts are still classified; the would-be message is kept in
  `state["shadow"]` and nothing is sent. Review it with `status`, then switch the
  flag on.
- Flag on and the state file missing or corrupt: the run fails loudly after the
  ledger is written. It is never re-initialized automatically.
- `--dry-run` and `--force` runs never send.

## Replay result (2026-11-15, 547 archived posts, ~$0.20)
3 would-be messages in total: BTC existing short (06-27), TAKE short (09-23
15:18) and the 7-asset short package (09-23 15:52). The "still holding"/"since
$97K" repeats, Bitfinex whale post, ONDO/TAKE result posts, the conditional BTC
long and generic boasts produced nothing.
