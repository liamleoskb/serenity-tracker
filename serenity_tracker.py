"""
Market Voices Tracker — budget-guarded build
============================================
Changes from the previous version:
  * GEMINI_MODEL default is now gemini-3.6-flash (2.5-flash was retired)
  * tracks 3 accounts (gmpnavi and TheObserverLee removed)
  * LOOKBACK_DAYS default 1 — the smallest useful window
  * NEW: a spend ledger in data/spend.json plus a pre-flight budget check.
    The run is REFUSED before any Apify call if the remaining budget could
    not absorb a worst-case run. This makes overspending structurally
    impossible rather than merely unlikely.
  * NEW: same-day guard — a second run on the same UTC date is refused
    unless FORCE_RUN=1, so an accidental double-click cannot double-charge.

Cost model (Apify free plan, from the actor's published pricing):
    $3.00 per 1,000 tweets + $0.006 run-start fee
    worst case per run = 100 tweets = $0.306
    observed for these 3 accounts ≈ 18 tweets/day ≈ $0.06/run

IMPORTANT: the ledger records ESTIMATES, not Apify's actual billing.
Check console.apify.com → Billing periodically and correct the ledger by
editing data/spend.json if the figures drift.

Built against apify.com/altimis/scweet:
  max_items is global per run · free guardrails 1,000 tweets/day, 10 runs/day,
  60s between runs · unknown input keys rejected · zero-result runs trigger
  escalating cooldowns, so this script never auto-retries.

Secrets: APIFY_TOKEN, APIFY_ACTOR_ID, GEMINI_API_KEY
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ============================ configuration ============================

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
APIFY_ACTOR_ID = os.environ.get("APIFY_ACTOR_ID", "altimis~scweet").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()

TARGET_HANDLES = [h.strip().lstrip("@") for h in os.environ.get(
    "TARGET_HANDLES", "aleabitoreddit,NURadu_,Alisvolatprop12"
).split(",") if h.strip()]

MAX_ITEMS = max(int(os.environ.get("MAX_ITEMS", "100")), 100)   # 100 is the floor
LOOKBACK_DAYS = max(int(os.environ.get("LOOKBACK_DAYS", "1")), 1)

SEARCH_SORT = os.environ.get("SEARCH_SORT", "Latest").strip()
if SEARCH_SORT not in ("Top", "Latest"):
    SEARCH_SORT = "Latest"

TWEET_TYPE = os.environ.get("TWEET_TYPE", "exclude_retweets").strip()

# --- budget control ---
BUDGET_USD = float(os.environ.get("BUDGET_USD", "1.70"))
COST_PER_1K = 3.00          # free-plan tweet rate
RUN_START_FEE = 0.006
WORST_CASE_RUN = MAX_ITEMS / 1000 * COST_PER_1K + RUN_START_FEE   # $0.306
FORCE_RUN = os.environ.get("FORCE_RUN", "").strip() == "1"

BATCH_SIZE = 25

HISTORY_PATH = "data/history.json"
SPEND_PATH = "data/spend.json"
STATUS_PATH = "data/last_run.json"
DASHBOARD_PATH = "docs/index.html"

VALID_STANCES = ("bullish", "bearish", "neutral")


def log(msg):
    print(msg, flush=True)


def http_json(url, payload=None, timeout=600):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip() else None


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def safe_url(u):
    u = str(u or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


# ============================ spend ledger ============================

def load_spend():
    """{'total': float, 'runs': [{'date','tweets','cost'}], 'budget': float}"""
    if os.path.exists(SPEND_PATH):
        try:
            with open(SPEND_PATH, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "total" in d:
                d.setdefault("runs", [])
                return d
        except Exception as e:
            log(f"WARNING: spend.json unreadable ({e}); treating spend as 0.")
    return {"total": 0.0, "runs": [], "budget": BUDGET_USD}


def save_spend(spend):
    os.makedirs(os.path.dirname(SPEND_PATH), exist_ok=True)
    spend["runs"] = spend.get("runs", [])[-60:]      # keep the last 60 runs
    spend["budget"] = BUDGET_USD
    tmp = SPEND_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(spend, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SPEND_PATH)


def budget_check(spend):
    """
    Decide whether fetching is allowed. Returns (allowed, reason).
    Reserves the worst-case run cost, so the budget can never be exceeded
    even if every account suddenly posts to the cap.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    spent = float(spend.get("total", 0.0))
    remaining = BUDGET_USD - spent

    log(f"Budget   : ${spent:.3f} spent of ${BUDGET_USD:.2f} "
        f"· ${remaining:.3f} remaining")

    if not FORCE_RUN:
        for r in spend.get("runs", []):
            if r.get("date") == today:
                return False, (f"Already fetched today ({today}), costing "
                               f"${r.get('cost', 0):.3f}. Skipping to avoid "
                               f"double-charging. Set FORCE_RUN=1 to override.")

    if remaining < WORST_CASE_RUN:
        return False, (f"Budget guard: ${remaining:.3f} left, but a run could "
                       f"cost up to ${WORST_CASE_RUN:.3f} in the worst case. "
                       f"Refusing to fetch. Raise BUDGET_USD after your Apify "
                       f"credit resets, or top up your account.")

    runs_left = int(remaining // max(WORST_CASE_RUN, 0.001))
    log(f"           at worst case, {runs_left} more run(s) fit in budget")
    return True, ""


# ============================ step 1: fetch ============================

def build_actor_input():
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
             ).strftime("%Y-%m-%d")
    return {
        "source_mode": "search",
        "search_query": " OR ".join(f"from:{h}" for h in TARGET_HANDLES),
        "search_sort": SEARCH_SORT,
        "tweet_type": TWEET_TYPE,
        "since": since,
        "max_items": MAX_ITEMS,
    }


def extract_post(item):
    if not isinstance(item, dict):
        return None

    text = ""
    for key in ("text", "full_text", "fullText", "content", "rawContent"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    if not text:
        nested = item.get("tweet")
        if isinstance(nested, dict):
            for key in ("text", "full_text", "content"):
                v = nested.get(key)
                if isinstance(v, str) and v.strip():
                    text = v.strip()
                    break
    if not text:
        return None

    author = ""
    for key in ("handle", "username", "userName", "screen_name"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            author = v.strip().lstrip("@")
            break
    if not author:
        user = item.get("user")
        if isinstance(user, dict):
            for key in ("handle", "username", "screen_name"):
                v = user.get(key)
                if isinstance(v, str) and v.strip():
                    author = v.strip().lstrip("@")
                    break

    url = ""
    for key in ("tweet_url", "tweetUrl", "url", "link", "permalink"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            url = v.strip()
            break
    if not url:
        tid = item.get("id") or item.get("id_str") or item.get("conversation_id")
        if tid and author:
            url = f"https://x.com/{author}/status/{tid}"
        elif tid:
            url = f"https://x.com/i/status/{tid}"

    date = ""
    for key in ("created_at", "createdAt", "collected_at_utc", "date", "timestamp"):
        v = item.get(key)
        if v:
            date = str(v)
            break

    return {"text": text, "url": safe_url(url), "date": date, "author": author}


def fetch_posts(spend):
    """ONE Apify run, budget-gated. Returns (posts, notes, cost)."""
    if not APIFY_TOKEN or not APIFY_ACTOR_ID:
        log("ERROR: APIFY_TOKEN or APIFY_ACTOR_ID missing.")
        return [], ["Apify credentials missing — check GitHub Secrets."], 0.0

    allowed, reason = budget_check(spend)
    if not allowed:
        log(f"\nFETCH SKIPPED — {reason}")
        return [], [reason], 0.0

    actor_input = build_actor_input()
    log(f"\nQuery    : {actor_input['search_query']}")
    log(f"Window   : since {actor_input['since']} ({LOOKBACK_DAYS}d) · "
        f"sort {actor_input['search_sort']} · {actor_input['tweet_type']}")
    log(f"Cap      : {actor_input['max_items']} tweets TOTAL "
        f"(worst case ${WORST_CASE_RUN:.3f})")
    log("Apify use: 1 run\n")

    url = (f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}"
           f"/run-sync-get-dataset-items?token={APIFY_TOKEN}")

    try:
        items = http_json(url, actor_input)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            body = ""
        log(f"Apify HTTP {e.code}: {body}")
        hint = {400: " (an input field was rejected)",
                401: " (token invalid)",
                403: " (actor not enabled on your account)"}.get(e.code, "")
        # A failed run may still incur the start fee.
        return [], [f"Apify returned HTTP {e.code}{hint}"], RUN_START_FEE
    except Exception as e:
        log(f"Apify request failed: {e}")
        return [], [f"Apify request failed: {e}"], RUN_START_FEE

    if not isinstance(items, list):
        log(f"Unexpected response: {type(items).__name__} {str(items)[:200]}")
        return [], ["Apify returned an unexpected response shape."], RUN_START_FEE

    log(f"Raw items returned: {len(items)}")
    if items and isinstance(items[0], dict):
        log(f"Field names: {sorted(items[0].keys())[:25]}")

    if not items:
        log("\nZero tweets returned. Likely causes:")
        log("  1. Daily run limit — apify.com run log says "
            "'Daily run limit reached (n/10)'.")
        log("  2. A cooldown from earlier zero-result runs; re-running "
            "lengthens it.")
        log("  3. Nothing posted inside the window.")
        return [], ["No tweets returned — check the Apify run log before "
                    "retrying."], RUN_START_FEE

    posts, unattributed = [], 0
    for it in items:
        p = extract_post(it)
        if not p:
            continue
        if not p["author"]:
            p["author"] = "unknown"
            unattributed += 1
        posts.append(p)

    per_author = {}
    for p in posts:
        per_author[p["author"]] = per_author.get(p["author"], 0) + 1

    log(f"\nUsable posts: {len(posts)}")
    for a, n in sorted(per_author.items(), key=lambda kv: -kv[1]):
        log(f"  @{a}: {n}")
    if unattributed:
        log(f"  ({unattributed} posts carried no author field)")

    cost = len(posts) / 1000 * COST_PER_1K + RUN_START_FEE
    log(f"\nEstimated cost this run: ${cost:.3f}")

    notes = [f"Fetched {len(posts)} posts · est. ${cost:.3f}"]

    wanted = {h.lower() for h in TARGET_HANDLES}
    missing = wanted - {a.lower() for a in per_author}
    if missing:
        notes.append("Nothing in this window from: "
                     + ", ".join("@" + m for m in sorted(missing)))

    if len(items) >= MAX_ITEMS:
        if len(per_author) > 1:
            top, count = max(per_author.items(), key=lambda kv: kv[1])
            if count > MAX_ITEMS * 0.6:
                msg = (f"Cap reached and @{top} used {count}/{len(posts)} of it — "
                       f"other accounts under-sampled.")
                log(f"WARNING: {msg}")
                notes.append(msg)
        log(f"Note: hit the {MAX_ITEMS}-tweet cap; older posts not fetched.")

    return posts, notes, cost


# ============================ step 2: classify ============================

CLASSIFY_PROMPT = """You are a strict data extractor. Below are public social media
posts from stock market commentators. For EACH post, list every stock ticker
mentioned (e.g. NVDA, TSM, 005930.KS). For each ticker, judge the author's stance
in that post: "bullish", "bearish", or "neutral".

Rules:
- Skip posts with no ticker. Never invent a ticker.
- Use the exchange-suffixed symbol for non-US listings (005930.KS for Samsung
  Electronics, 2330.TW for TSMC's Taiwan listing).
- If the author is quoting or describing someone else's view, mark "neutral".

Respond ONLY with a JSON array. No markdown fences, no commentary. Schema:
[{"post_index": 0, "ticker": "NVDA", "stance": "bullish", "note": "short reason"}]

Posts:
"""


def parse_gemini_json(raw):
    if not raw:
        return []
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except Exception:
        pass
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(cleaned[start:end + 1])
            return data if isinstance(data, list) else []
        except Exception:
            pass
    log(f"  could not parse Gemini output: {cleaned[:200]}")
    return []


def classify_batch(batch, offset, model):
    numbered = "\n\n".join(
        f"[{i + offset}] {p['text'][:600]}" for i, p in enumerate(batch))
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={GEMINI_API_KEY}")
    payload = {"contents": [{"parts": [{"text": CLASSIFY_PROMPT + numbered}]}]}

    for attempt in (1, 2):
        try:
            resp = http_json(url, payload)
            parts = resp["candidates"][0]["content"]["parts"]
            return parse_gemini_json("".join(p.get("text", "") for p in parts))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                body = ""
            log(f"  Gemini HTTP {e.code}: {body[:200]}")
            if e.code == 404:
                # Google names the replacement model in the error text.
                m = re.search(r"models/([\w.\-]+)", body.split("use", 1)[-1])
                if m and m.group(1) != model:
                    raise ModelRetired(m.group(1))
                raise ModelRetired("")
            if e.code == 429 and attempt == 1:
                log("  rate limited; waiting 20s for one retry")
                time.sleep(20)
                continue
            return []
        except ModelRetired:
            raise
        except Exception as e:
            log(f"  Gemini call failed: {e}")
            return []
    return []


class ModelRetired(Exception):
    """Raised when Gemini reports the model is gone, carrying the successor."""
    def __init__(self, successor):
        self.successor = successor
        super().__init__(successor)


def clean_ticker(t):
    t = str(t).upper().strip().lstrip("$").strip()
    return t if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", t) else ""


def classify_posts(posts):
    if not posts:
        log("No posts to classify.")
        return [], []
    if not GEMINI_API_KEY:
        log("ERROR: GEMINI_API_KEY missing.")
        return [], ["Gemini key missing — check GitHub Secrets."]

    model = GEMINI_MODEL
    notes = []
    raw_rows = []
    start = 0
    while start < len(posts):
        batch = posts[start:start + BATCH_SIZE]
        log(f"Classifying posts {start}–{start + len(batch) - 1} with {model}...")
        try:
            raw_rows.extend(classify_batch(batch, start, model))
        except ModelRetired as e:
            if e.successor and e.successor != model:
                log(f"  model retired; switching to {e.successor} and retrying")
                notes.append(f"Gemini model {model} is retired — used "
                             f"{e.successor}. Update GEMINI_MODEL in the workflow.")
                model = e.successor
                continue          # retry this same batch with the new model
            log("  model retired and no successor named; aborting classification.")
            notes.append(f"Gemini model {model} is retired. Set GEMINI_MODEL "
                         f"in the workflow to a current model.")
            break
        start += BATCH_SIZE

    results = []
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        try:
            idx = int(r.get("post_index", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(posts)):
            continue
        ticker = clean_ticker(r.get("ticker", ""))
        if not ticker:
            continue
        stance = str(r.get("stance", "neutral")).lower().strip()
        if stance not in VALID_STANCES:
            stance = "neutral"
        p = posts[idx]
        results.append({
            "ticker": ticker, "stance": stance,
            "note": str(r.get("note", ""))[:200],
            "author": p.get("author", ""),
            "post_url": p.get("url", ""),
            "post_date": p.get("date", ""),
            "post_text": p.get("text", "")[:280],
        })

    log(f"Extracted {len(results)} ticker mentions.")
    if posts and not results and not notes:
        notes.append("Posts were fetched but contained no stock tickers.")
    return results, notes


# ============================ step 3: history ============================

def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        log(f"WARNING: history.json unreadable ({e}); backed up, starting fresh.")
        try:
            os.replace(HISTORY_PATH, HISTORY_PATH + ".broken")
        except Exception:
            pass
        return []


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORY_PATH)


def dedupe_key(row):
    return (row.get("post_url") or row.get("post_text", "")[:80],
            row.get("ticker", ""))


def merge(history, new_rows):
    seen = {dedupe_key(h) for h in history}
    added = 0
    now = datetime.now(timezone.utc).isoformat()
    for r in new_rows:
        k = dedupe_key(r)
        if k in seen:
            continue
        r["recorded_at"] = now
        history.append(r)
        seen.add(k)
        added += 1
    log(f"Added {added} new mentions ({len(new_rows) - added} already on file).")
    return history


def write_status(posts_count, added_count, notes, spend):
    os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump({"when": datetime.now(timezone.utc).isoformat(),
                   "posts_fetched": posts_count,
                   "mentions_added": added_count,
                   "spent_total": round(spend.get("total", 0.0), 4),
                   "budget": BUDGET_USD,
                   "notes": notes}, f, ensure_ascii=False, indent=1)


# ============================ step 4: dashboard ============================

def parse_when(row):
    for key in ("post_date", "recorded_at"):
        v = str(row.get(key, "")).strip()
        if not v:
            continue
        try:
            return datetime.fromisoformat(
                v.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(v, fmt)
                return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                        ).astimezone(timezone.utc)
            except Exception:
                continue
        if v.isdigit() and len(v) >= 10:
            try:
                return datetime.fromtimestamp(int(v[:10]), tz=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def summarize(history, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    table = {}
    for r in history:
        if parse_when(r) < cutoff:
            continue
        t = table.setdefault(r.get("ticker", "?"), {
            "bullish": 0, "bearish": 0, "neutral": 0,
            "total": 0, "voices": set(), "latest": "neutral", "latest_at": None})
        stance = r.get("stance", "neutral")
        if stance not in VALID_STANCES:
            stance = "neutral"
        t[stance] += 1
        t["total"] += 1
        if r.get("author"):
            t["voices"].add(r["author"])
        when = parse_when(r)
        if t["latest_at"] is None or when > t["latest_at"]:
            t["latest_at"], t["latest"] = when, stance
    return sorted(table.items(), key=lambda kv: (-kv[1]["total"], kv[0]))


def build_dashboard(history, notes, spend):
    periods = [("7 days", 7), ("30 days", 30), ("90 days", 90)]
    tabs, tables = [], []
    for i, (label, days) in enumerate(periods):
        rows = summarize(history, days)
        body = "".join(
            f"<tr><td class='tk'>{esc(t)}</td><td>{d['total']}</td>"
            f"<td>{len(d['voices'])}</td>"
            f"<td class='bullish'>{d['bullish']}</td>"
            f"<td class='bearish'>{d['bearish']}</td>"
            f"<td class='neutral'>{d['neutral']}</td>"
            f"<td class='{d['latest']}'>{d['latest']}</td></tr>"
            for t, d in rows
        ) or "<tr><td colspan='7'>No mentions in this period yet.</td></tr>"
        active = "active" if i == 0 else ""
        tabs.append(f"<button class='tab {active}' onclick='show({i})'>{label}</button>")
        tables.append(
            f"<table class='period {active}'><thead><tr><th>Ticker</th>"
            f"<th>Mentions</th><th>Voices</th><th>Bull</th><th>Bear</th>"
            f"<th>Neutral</th><th>Latest</th></tr></thead>"
            f"<tbody>{body}</tbody></table>")

    recent = sorted(history, key=parse_when, reverse=True)[:60]
    cards = []
    for r in recent:
        u = safe_url(r.get("post_url", ""))
        link = f" · <a href='{esc(u)}'>original post</a>" if u else ""
        cards.append(
            f"<div class='card'><div class='card-top'>"
            f"<span><span class='tk'>{esc(r.get('ticker',''))}</span>"
            f"<span class='who'>@{esc(r.get('author',''))}</span></span>"
            f"<span class='badge {r.get('stance','neutral')}'>"
            f"{esc(r.get('stance',''))}</span></div>"
            f"<p>{esc(r.get('post_text',''))}</p>"
            f"<div class='meta'>{esc(r.get('note',''))}{link}</div></div>")
    feed = "".join(cards) or (
        "<p class='meta'>Nothing recorded yet. The Actions log explains why.</p>")

    spent = float(spend.get("total", 0.0))
    remaining = BUDGET_USD - spent
    pct = min(max(spent / BUDGET_USD * 100 if BUDGET_USD else 0, 0), 100)
    bar = (f"<div class='budget'><div class='brow'><span>Apify spend "
           f"(estimated)</span><span>${spent:.2f} of ${BUDGET_USD:.2f}</span></div>"
           f"<div class='track'><div class='fill' style='width:{pct:.0f}%'></div></div>"
           f"<div class='meta'>${remaining:.2f} remaining · verify against "
           f"console.apify.com → Billing</div></div>")

    note_html = ""
    if notes:
        note_html = ("<div class='notice'><strong>Last run</strong><ul>"
                     + "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul></div>")

    watched = ", ".join("@" + esc(h) for h in TARGET_HANDLES)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Voices Tracker</title><style>
:root {{ --bg:#0f1317; --panel:#181f26; --ink:#e9eef3; --dim:#8895a3;
  --bull:#3ecf8e; --bear:#ff6b6b; --line:#28313b; --warn:#f0b429; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); padding:20px;
  font:16px/1.55 "Avenir Next","Segoe UI",system-ui,sans-serif; }}
header {{ border-left:4px solid var(--bull); padding-left:14px; margin-bottom:18px; }}
h1 {{ margin:0; font-size:1.45rem; }}
h2 {{ margin-top:34px; font-size:.95rem; color:var(--dim);
  text-transform:uppercase; letter-spacing:.08em; }}
.sub, .meta {{ color:var(--dim); font-size:.8rem; }}
.sub {{ margin-top:4px; }}
nav a {{ font-size:.85rem; }}
.budget {{ background:var(--panel); border:1px solid var(--line);
  border-radius:10px; padding:12px 14px; margin:16px 0; }}
.brow {{ display:flex; justify-content:space-between; font-size:.85rem;
  margin-bottom:8px; }}
.track {{ height:6px; background:var(--line); border-radius:3px; overflow:hidden; }}
.fill {{ height:100%; background:var(--warn); }}
.notice {{ background:rgba(240,180,41,.08); border:1px solid var(--warn);
  border-radius:8px; padding:10px 14px; margin:16px 0; font-size:.85rem; }}
.notice ul {{ margin:6px 0 0 18px; padding:0; }}
.tab {{ background:none; border:1px solid var(--line); color:var(--dim);
  padding:8px 16px; border-radius:20px; margin:0 8px 8px 0; font-size:.9rem; }}
.tab.active {{ color:var(--ink); border-color:var(--bull); }}
table {{ width:100%; border-collapse:collapse; margin-top:14px; display:none; }}
table.active {{ display:table; }}
th,td {{ text-align:left; padding:9px 6px; border-bottom:1px solid var(--line);
  font-size:.9rem; }}
th {{ color:var(--dim); font-size:.72rem; text-transform:uppercase;
  letter-spacing:.06em; }}
.tk {{ font-weight:700; }}
.who {{ color:var(--dim); font-size:.8rem; margin-left:8px; }}
.bullish {{ color:var(--bull); }} .bearish {{ color:var(--bear); }}
.neutral {{ color:var(--dim); }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:13px 15px; margin-top:11px; }}
.card-top {{ display:flex; justify-content:space-between; align-items:center;
  margin-bottom:6px; gap:10px; }}
.badge {{ font-size:.72rem; padding:2px 10px; border-radius:12px;
  border:1px solid currentColor; white-space:nowrap; }}
.card p {{ margin:4px 0; font-size:.9rem; }}
a {{ color:var(--bull); }}
footer {{ margin-top:38px; color:var(--dim); font-size:.76rem; line-height:1.6; }}
</style></head><body>
<header><h1>Market Voices Tracker</h1>
<div class="sub">Watching {watched} · updated {updated} ·
{len(history)} mentions on file</div></header>
<nav><a href="analysis.html">Consensus &amp; track record →</a></nav>
{bar}
{note_html}
<div style="margin-top:16px">{''.join(tabs)}</div>
{''.join(tables)}
<h2>Recent mentions</h2>
{feed}
<footer>Automated research interface for public posts. Not affiliated with any
tracked account. Classifications are AI-generated and often wrong — always read
the original post before relying on anything here. Not investment advice.</footer>
<script>
function show(n) {{
  document.querySelectorAll('.period').forEach((t,i)=>t.classList.toggle('active',i===n));
  document.querySelectorAll('.tab').forEach((b,i)=>b.classList.toggle('active',i===n));
}}
</script></body></html>"""

    os.makedirs(os.path.dirname(DASHBOARD_PATH), exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Dashboard written to {DASHBOARD_PATH}")


# ============================ main ============================

def main():
    log(f"Tracking {len(TARGET_HANDLES)} account(s): "
        f"{', '.join('@' + h for h in TARGET_HANDLES)}")
    log(f"Model    : {GEMINI_MODEL}")

    spend = load_spend()
    history = load_history()
    log(f"History  : {len(history)} mentions on file")

    posts, fetch_notes, cost = fetch_posts(spend)

    if cost > 0:
        spend["total"] = float(spend.get("total", 0.0)) + cost
        spend.setdefault("runs", []).append({
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "tweets": len(posts),
            "cost": round(cost, 4),
        })
        save_spend(spend)
        log(f"Ledger   : ${spend['total']:.3f} of ${BUDGET_USD:.2f} used")

    rows, class_notes = classify_posts(posts)
    before = len(history)
    history = merge(history, rows)
    added = len(history) - before

    save_history(history)
    notes = fetch_notes + class_notes
    write_status(len(posts), added, notes, spend)
    build_dashboard(history, notes, spend)
    log("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(0)
