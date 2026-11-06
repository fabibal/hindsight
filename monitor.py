"""Shared helpers for the X / YouTube analysis digests (twitter_digest.py, youtube_monitor.py).

GetXAPI fetch + normalisation, Gemini model / price constants, the Hungarian
mangled-text detector and the LLM-outage tally. The trade-signal monitor that
used to live in this module (IncomeSharks / traderstewie extraction, positions,
IBKR-era plumbing) was removed on 2026-11-06; see git history before that date.
The file name is kept so the digests' `import monitor` lines stay unchanged.
"""

import gzip
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from google.genai import types as genai_types


HOME = os.path.dirname(os.path.abspath(__file__))


ENV_FILE = os.path.join(HOME, ".env")


GETXAPI_BASE = "https://api.getxapi.com"


GETXAPI_POSTS_PATH = "/twitter/user/tweets"


GETXAPI_COST_PER_CALL = 0.001        # $/call (~20 tweets/page)


# Text extraction. gemini-2.5-flash-lite replaced Haiku here 2026-07 (Anthropic
# credit balance depleted): cheap, stable (not the shut-down preview variant),
# and schema-constrained JSON via response_json_schema works the same way.
# 2026-07-13: briefly pinned to gemini-2.0-flash because 2.5-flash-lite is
# paid-tier-only and GOOGLE_API_KEY's project ("My First Project") had no
# linked Cloud Billing account (an AI Studio prepay credit balance alone does
# NOT enable paid tier) -- reverted back now that billing is linked.
# thinking_budget=0 is explicit even though 2.5 Flash-Lite defaults to thinking
# off, so a future Google default change can't silently eat max_output_tokens.
MODEL = "gemini-2.5-flash-lite"


EXTRACT_THINKING = genai_types.ThinkingConfig(thinking_budget=0)


EXTRACT_INPUT_PER_1M = 0.10          # $ / 1M input tokens


EXTRACT_OUTPUT_PER_1M = 0.40         # $ / 1M output tokens


# Vision model for chart-image analysis (Haiku 4.5 does NOT accept images).
# Only used for influencer tweets that carry a chart photo. Pricier per token,
# so it runs as an ADD-ON to the cheap Haiku text pass, never as a replacement.
# (Gemini Flash via Google AI Studio / google-genai — replaced Sonnet 5 here
# 2026-07: multimodal + json-schema structured output, no Anthropic vision spend.
# thinking_budget=0 mirrors the old "thinking disabled" behavior — Gemini Flash
# also thinks by default (medium), which would eat the tight max_output_tokens
# budgets meant for JSON only.)
# 2026-09-04: 3.5-flash -> 3.7-flash. Half the input / 2.4x cheaper output price
# (see GEMINI_*_PER_1M below), accepts thinking_budget=0 unchanged, and it is
# already the model youtube_monitor.py runs on. A/B'd on 4 real Glassnode chart
# posts before the swap: 4/4 identical chart_trend, 43% cheaper, and the
# summaries carried MORE concrete figures than 3.5-flash's. twitter_digest.MODEL
# moved in the same commit — the two share the price constants below, so they
# have to stay on the same model or the cost logs go wrong.
VISION_MODEL = "gemini-3.7-flash"


GEMINI_THINKING = genai_types.ThinkingConfig(thinking_budget=0)     # off


# Opt-in dynamic thinking for the dense, low-volume analysis reads where the
# reasoning pays off (YouTube/Cowen TA, Kendrick forecasts) — NOT vision, NOT the
# high-volume per-tweet feeds (ki/joao). Callers that use it MUST give
# max_output_tokens room for thinking + JSON or the json output truncates.
GEMINI_DEEP_THINKING = genai_types.ThinkingConfig(thinking_budget=-1)   # dynamic


GEMINI_INPUT_PER_1M = 0.75           # $ / 1M input tokens (gemini-3.7-flash)


GEMINI_OUTPUT_PER_1M = 3.75          # $ / 1M output tokens, incl. thinking tokens


MAX_VISION_IMAGES = 2                # cap images/tweet to bound vision cost


class _NoMediaRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError("Media redirects are not allowed")


def _image_block(url):
    """Download a Twitter media photo and return a Gemini image Part, or None
    on any failure. `name=small` keeps the download (and the vision token cost)
    modest while staying legible for chart levels."""
    try:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != "pbs.twimg.com"
                or parts.username or parts.password or parts.port not in (None, 443)):
            raise ValueError("Untrusted Twitter media URL")
        query = dict(urllib.parse.parse_qsl(parts.query))
        query["name"] = "small"
        url = urllib.parse.urlunsplit(parts._replace(
            query=urllib.parse.urlencode(query), fragment=""))
        req = urllib.request.Request(url,
                                     headers={"User-Agent": "Mozilla/5.0"})
        opener = urllib.request.build_opener(_NoMediaRedirects())
        with opener.open(req, timeout=15) as resp:
            raw = resp.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("Media exceeds 5 MiB limit")
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        elif raw.startswith(b"\xff\xd8\xff"):
            media_type = "image/jpeg"
        else:
            raise ValueError("Unsupported media content (expected PNG/JPEG)")
    except (urllib.error.URLError, OSError, ValueError, TypeError) as e:
        print(f"  [media error] {type(e).__name__}: {e}", file=sys.stderr)
        return None
    return genai_types.Part.from_bytes(data=raw, mime_type=media_type)


def _unescape_strings(obj):
    """Recursively html.unescape() every string in a parsed JSON value.
    Gemini occasionally emits HTML-named-entity text (e.g. "&eacute;" for
    "e") instead of the literal character -- harmless no-op on already-clean
    text, so applied unconditionally. Shared by twitter_digest.py and
    youtube_monitor.py, both of which get Hungarian free text back from
    gemini-3.5-flash."""
    if isinstance(obj, str):
        return html.unescape(obj)
    if isinstance(obj, list):
        return [_unescape_strings(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _unescape_strings(v) for k, v in obj.items()}
    return obj


def _first_sentence(text):
    """The leading sentence of a free-text field, whitespace-collapsed. Used by
    both digests as the stand-in when a CURRENT VIEW synthesis comes back
    without its one-line headline: the dashboard's Consensus panel shows that
    field on its own, so a blunt restatement of the stance carries more than a
    canned placeholder would."""
    s = " ".join((text or "").split())
    m = re.match(r".+?[.!?](?=\s|$)", s)
    return m.group(0) if m else s


# gemini-3.5-flash occasionally (probabilistically -- a retry of the
# identical call often comes back clean) mangles accented non-ASCII
# characters in long free-text fields. Observed forms, all specific to
# Hungarian-output feeds (twitter_digest.py, youtube_monitor.py):
#  - the UTF-8 continuation byte survives with its high bit stripped,
#    landing mid-word as stray punctuation: "veszteseg" -> "vesztes)g".
#  - a raw C0 control byte in its place: "n\x01gy".
#  - an ASCII escape-like marker + bare letter: "realiz'alt", "%evek".
#  - a bogus numeric-entity-shaped placeholder: "T#1;masz", "elt#3;rő".
#    Not a real HTML entity (no leading "&", so html.unescape() in
#    _unescape_strings correctly ignores it) and NOT a stable per-char
#    code -- the same digit stands in for different letters within one
#    string (#3; covers 'ó', 'í', 'ö', 'é' and 'á' in a single summary) --
#    so it's detected-for-retry like the others rather than decoded.
#  - one or more accented characters silently replaced by a literal TAB
#    (0x09) with no other artifact: "\ts" for "és", "mozg\ttlagot" for
#    "mozgóátlagot" (moving average, accusative). Found 2026-07-29 in
#    generate_current_view() output, undetected because _MANGLED_CTRL_RE
#    used to carve \x09/\x0a/\x0d out of the C0 range on the assumption
#    they were legitimate whitespace -- they are not: every field these
#    regexes guard is single-paragraph prose, so a tab/CR/LF mid-string is
#    exactly as unambiguous a corruption signal as any other control byte.
#  - the same silent replacement but with a plain SPACE (0x20) instead of a
#    TAB: "elm elt" for "elmúlt", "v rakoz   ll pontot" for "várakozó
#    álláspontot", " s" for "és". Found 2026-07-29 in daancrypto's
#    generate_current_view() output; widening _MANGLED_CTRL_RE above does
#    NOT cover it -- a space is not a control byte, and mid-string it is
#    indistinguishable from a word separator. What gives it away is the
#    stray one-letter word fragments it strews through the text, so that is
#    what _MANGLED_FRAG_RE matches: a lone lowercase letter other than
#    a/e/s, the only ones that legitimately stand alone in Hungarian or
#    English prose (verified: 0 hits across every existing ledger, 28 in the
#    corrupted file). The substitution never fires just once, so a real hit
#    always leaves several fragments even though any single one of them
#    could in principle be innocent.
#  - a "%" + 3 digits with no closing marker: "%141ll%141spontja" for
#    "álláspontja", "v%151gig" for "végig". Found 2026-08-02 in daancrypto's
#    generate_current_view() output. Neither existing alternative caught it:
#    "#\d{1,3};" needs the trailing ";", and letter-punct-letter needs a
#    LETTER after the "%", not a digit. Unlike the "#\d;" placeholder this one
#    is a stable per-char code (octal of the codepoint minus 128: á -> 141),
#    but it is still detected-for-retry rather than decoded -- one clean retry
#    is cheaper than trusting a reverse-engineered encoding. Must be anchored
#    to an ADJACENT LETTER: a bare "%\d{2,3}" also matches the Turkish
#    percent-first notation ("%70 olasılıkla", "%100 kârla") that @CelalKucuker
#    (removed 2026-08-27) used to tweet and monitor.py's own extraction quoted
#    back into trades.json (47 such hits in the existing ledger, vs 0 for the
#    anchored form).
# None of these are legitimate mid-word occurrences in Hungarian or English
# prose, so a hit is an unambiguous corruption signal.
_MANGLED_RE = re.compile(
    "[A-Za-zÀ-ÿ][)!:<#%][A-Za-zÀ-ÿ]"
    "|[A-Za-z]'[aeiouAEIOU]"
    "|#\\d{1,3};"
    "|[A-Za-zÀ-ÿ]%\\d{3}|%\\d{3}[A-Za-zÀ-ÿ]")


_MANGLED_CTRL_RE = re.compile(r"[\x00-\x1f]")   # full C0 range, no carve-outs


_MANGLED_FRAG_RE = re.compile(r"(?<!\S)[b-df-rt-z](?!\S)")  # lowercase except a/e/s


def _looks_mangled(obj):
    if isinstance(obj, str):
        return bool(_MANGLED_RE.search(obj) or _MANGLED_CTRL_RE.search(obj)
                    or _MANGLED_FRAG_RE.search(obj))
    if isinstance(obj, list):
        return any(_looks_mangled(v) for v in obj)
    if isinstance(obj, dict):
        return any(_looks_mangled(v) for v in obj.values())
    return False


# --- env / io --------------------------------------------------------------
def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --- GetXAPI fetch (alternative backend, ~100x cheaper) --------------------
def getxapi_get(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {os.environ['GETXAPI_KEY']}")
    # Request gzip: the uncompressed body (~25KB w/ Content-Length) is truncated
    # mid-stream by the upstream Caddy proxy (200 OK headers sent, then the body
    # connection drops -> IncompleteRead). The gzip body is ~6KB and chunked
    # (no Content-Length), so it survives the flaky link. Vary: Accept-Encoding.
    req.add_header("Accept-Encoding", "gzip")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


def _normalize_getxapi(tw):
    """Map a GetXAPI tweet to the official-API shape the rest of the code uses.

    Critically, GetXAPI's `createdAt` is the legacy Twitter format
    ('Mon Jun 01 00:46:36 +0000 2026'); convert it to ISO 8601 so downstream
    string-sorting and [:10] date slicing keep working."""
    created = tw.get("createdAt")
    try:
        created_iso = parsedate_to_datetime(created).astimezone(
            timezone.utc).isoformat() if created else None
    except (TypeError, ValueError):
        created_iso = None
    if created_iso is None:
        raise ValueError("GetXAPI tweet has a missing or invalid createdAt timestamp")
    # Photo URLs only (skip videos/gifs): used for chart-image vision analysis.
    media = [m.get("url") for m in (tw.get("media") or [])
             if m.get("type") == "photo" and m.get("url")]
    return {
        "id": str(tw.get("id")),
        # GetXAPI sometimes returns text HTML-escaped (e.g. "S&amp;P500");
        # unescape so it doesn't leak into the LLM prompt, the stored ledger,
        # and the dashboard.
        "text": html.unescape(tw.get("text", "")),
        "created_at": created_iso,
        "is_reply": tw.get("isReply"),
        # author handle of THIS tweet. tweets_and_replies returns the whole
        # conversation thread, so a reply may be authored by another user; we
        # filter on this so foreign replies aren't extracted as the account's
        # own signals (see foreign_author()).
        "author": (tw.get("author") or {}).get("userName"),
        "media": media,
        "reply_to_tweet_id": tw.get("inReplyToStatusId") or tw.get("inReplyToId"),
        "conversation_id": tw.get("conversationId"),
        "first_observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _tweet_page(data):
    """An HTTP 200 error envelope is an outage, not an empty timeline."""
    if not isinstance(data, dict) or not isinstance(data.get("tweets"), list):
        raise ValueError("GetXAPI response is missing a tweets array")
    batch = data["tweets"]
    if any(not isinstance(t, dict) or not str(t.get("id", "")).isdigit() for t in batch):
        raise ValueError("GetXAPI response contains an invalid tweet ID")
    return batch


class GeminiTally:
    """Run-wide tally of Gemini calls that succeeded vs ones whose APIError was
    swallowed at the call site.

    Every Gemini helper in this project catches APIError per call and returns
    None so one bad post/video cannot kill a whole run. The cost is that a TOTAL
    outage (depleted prepay credits, revoked key, denied project) looks exactly
    like a quiet run: nothing is written, nothing raises, and the only trace is
    per-item stderr noise nobody reads. Counting both outcomes lets a run decide
    at the end whether it was quiet or blind. Used by the digests via
    report_gemini_outage(); monitor.py's own check is inline on Interpreter."""

    def __init__(self):
        self.calls = 0
        self.errors = 0

    def ok(self):
        self.calls += 1

    def fail(self):
        self.errors += 1


# A run where every single call failed is unambiguous. A run that is merely
# mostly-failing is judged on rate, but only once enough calls have happened
# that the rate means something -- otherwise one flaky call on a 1-post night
# triggers a warning at 100%.
OUTAGE_MIN_CALLS = 5


OUTAGE_FAIL_RATE = 0.8


def report_gemini_outage(tally, who):
    """Log when a run's Gemini calls failed wholesale. Returns True if
    an outage was reported. Mirrors the inline guard monitor.main() runs after extraction."""
    total = tally.calls + tally.errors
    if not tally.errors:
        return False
    total_outage = not tally.calls
    mostly = total >= OUTAGE_MIN_CALLS and tally.errors / total >= OUTAGE_FAIL_RATE
    if not (total_outage or mostly):
        return False
    scope = f"all {tally.errors}" if total_outage else f"{tally.errors}/{total}"
    msg = (f"{who}: {scope} Gemini call(s) failed this run "
           f"-- check GOOGLE_API_KEY/quota")
    print(f"ERROR: {msg}", file=sys.stderr)
    return True
