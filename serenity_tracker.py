"""
Serenity Tracker
----------------
Runs once per day (via GitHub Actions). It:
  1. Fetches recent public X posts from a target account (via an Apify actor)
  2. Asks Gemini (free tier) to find stock tickers + stance in each post
  3. Saves results into data/history.json (growing archive)
  4. Rebuilds docs/index.html (the dashboard you open in Safari)

You never run this by hand. GitHub Actions runs it for you.
All secrets come from environment variables — nothing is hardcoded.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

# ----------------------------- configuration -----------------------------

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
APIFY_ACTOR_ID = os.environ.get("APIFY_ACTOR_ID", "")  # e.g. "someuser~their-actor"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TARGET_HANDLE = os.environ.get("TARGET_HANDLE", "aleabitoreddit")
MAX_POSTS = int(os.environ.get("MAX_POSTS", "50"))

HISTORY_PATH = "data/history.json"
DASHBOARD_PATH = "docs/index.html"


def fail(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def http_json(url, payload=None, timeout=180):
    """Small helper: POST (or GET) a URL, return parsed JSON."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ----------------------------- step 1: fetch posts -----------------------------

def fetch_posts():
    """Run the Apify actor and return a list of normalized posts."""
    if not APIFY_TOKEN or not APIFY_ACTOR_ID:
        fail("APIFY_TOKEN or APIFY_ACTOR_ID is missing (check your GitHub Secrets).")

    url = (
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}"
        f"/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    )

    # NOTE: every Apify actor expects slightly different input field names.
    # These are common ones; if your chosen actor uses different names,
    # check its "Input" tab on Apify and adjust here.
    actor_input = {
        "profile_urls": [f"@{TARGET_HANDLE}"],
        "search_query": f"from:{TARGET_HANDLE}",
        "search_sort": "Latest",
        "source_mode": "auto",
        "tweet_type": "all",
        "max_items": MAX_POSTS,
        "min_likes": 0,
        "min_replies": 0,
        "min_retweets": 0,
        "blue_verified_only": False,
        "verified_only": False,
        "has_hashtags": False,
        "has_images": False,
        "has_links": False,
        "has_mentions": False,
        "has_videos": False,
    }

    print(f"Fetching up to {MAX_POSTS} posts from @{TARGET_HANDLE} via Apify...")
    try:
        items = http_json(url, actor_input)
    except urllib.error.HTTPError as e:
        fail(f"Apify {e.code}: {e.read().decode()[:800]}")
    except Exception as e:
        fail(f"Apify request failed: {e}")


    if not isinstance(items, list):
        fail(f"Unexpected Apify response (not a list): {str(items)[:300]}")

    posts = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = it.get("text") or it.get("full_text") or it.get("tweet") or it.get("content") or ""
        url_ = it.get("url") or it.get("tweetUrl") or it.get("link") or ""
        date_ = it.get("created_at") or it.get("createdAt") or it.get("date") or it.get("timestamp") or ""
        if text.strip():
            posts.append({"text": text.strip(), "url": url_, "date": str(date_)})

    print(f"Got {len(posts)} posts with text.")
    return posts


# ----------------------------- step 2: classify with Gemini -----------------------------

CLASSIFY_PROMPT = """You are a strict data extractor. Below are public social media posts
from a stock market commentator. For EACH post, list every stock ticker
mentioned (like NVDA, TSMC, 005930.KS). For each ticker found, judge the
author's stance in that post: "bullish", "bearish", or "neutral".
If a post mentions no tickers, skip it.

Respond ONLY with a JSON array, no markdown, no explanation. Schema:
[{"post_index": 0, "ticker": "NVDA", "stance": "bullish", "note": "one short reason"}]

Posts:
"""


def classify_posts(posts):
    """Send posts to Gemini, get back ticker/stance rows."""
    if not GEMINI_API_KEY:
        fail("GEMINI_API_KEY is missing (check your GitHub Secrets).")
    if not posts:
        print("No posts to classify today.")
        return []

    numbered = "\n\n".join(f"[{i}] {p['text'][:600]}" for i, p in enumerate(posts))
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {"contents": [{"parts": [{"text": CLASSIFY_PROMPT + numbered}]}]}

    print(f"Classifying with {GEMINI_MODEL}...")
    try:
        resp = http_json(url, payload)
        raw = resp["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        fail(f"Gemini request failed: {e}")

    raw = re.sub(r"```json|```", "", raw).strip()
    try:
        rows = json.loads(raw)
    except Exception:
        fail(f"Could not parse Gemini output as JSON. Output was:\n{raw[:500]}")

    results = []
    for r in rows:
        try:
            idx = int(r["post_index"])
            if 0 <= idx < len(posts):
                results.append({
                    "ticker": str(r["ticker"]).upper().strip(),
                    "stance": str(r.get("stance", "neutral")).lower().strip(),
                    "note": str(r.get("note", ""))[:200],
                    "post_url": posts[idx]["url"],
                    "post_date": posts[idx]["date"],
                    "post_text": posts[idx]["text"][:280],
                })
        except Exception:
            continue

    print(f"Extracted {len(results)} ticker mentions.")
    return results


# ----------------------------- step 3: save history -----------------------------

def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return []


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)


def merge(history, new_rows):
    """Add new rows, skipping exact duplicates (same post + same ticker)."""
    seen = {(h.get("post_url"), h.get("ticker")) for h in history}
    added = 0
    today = datetime.now(timezone.utc).isoformat()
    for r in new_rows:
        key = (r["post_url"], r["ticker"])
        if key not in seen and r["post_url"]:
            r["recorded_at"] = today
            history.append(r)
            seen.add(key)
            added += 1
    print(f"Added {added} new mentions (skipped duplicates).")
    return history


# ----------------------------- step 4: build dashboard -----------------------------

def parse_when(row):
    """Best-effort date for a row (falls back to when we recorded it)."""
    for key in ("post_date", "recorded_at"):
        v = row.get(key, "")
        for fmt in (None,):  # try fromisoformat first
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception:
                pass
    return datetime.now(timezone.utc)


def summarize(history, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    table = {}
    for r in history:
        if parse_when(r) < cutoff:
            continue
        t = table.setdefault(r["ticker"], {"bullish": 0, "bearish": 0, "neutral": 0, "total": 0, "latest": ""})
        stance = r["stance"] if r["stance"] in t else "neutral"
        t[stance] += 1
        t["total"] += 1
        t["latest"] = r["stance"]
    return sorted(table.items(), key=lambda kv: -kv[1]["total"])


def build_dashboard(history):
    periods = [("7 days", 7), ("30 days", 30), ("90 days", 90)]
    tabs_html, tables_html = [], []
    for i, (label, days) in enumerate(periods):
        rows = summarize(history, days)
        body = "".join(
            f"<tr><td class='tk'>{t}</td><td>{d['total']}</td>"
            f"<td class='bull'>{d['bullish']}</td><td class='bear'>{d['bearish']}</td>"
            f"<td>{d['neutral']}</td><td class='{d['latest']}'>{d['latest']}</td></tr>"
            for t, d in rows
        ) or "<tr><td colspan='6'>No mentions in this period yet.</td></tr>"
        active = "active" if i == 0 else ""
        tabs_html.append(f"<button class='tab {active}' onclick=\"show({i})\">{label}</button>")
        tables_html.append(
            f"<table class='period {active}' id='p{i}'><thead><tr>"
            f"<th>Ticker</th><th>Mentions</th><th>Bullish</th><th>Bearish</th>"
            f"<th>Neutral</th><th>Latest stance</th></tr></thead><tbody>{body}</tbody></table>"
        )

    recent = sorted(history, key=parse_when, reverse=True)[:40]
    feed = "".join(
        f"<div class='card'><div class='card-top'><span class='tk'>{r['ticker']}</span>"
        f"<span class='badge {r['stance']}'>{r['stance']}</span></div>"
        f"<p>{r['post_text']}</p>"
        f"<div class='meta'>{r.get('note','')}"
        + (f" · <a href='{r['post_url']}'>original post</a>" if r.get("post_url") else "")
        + "</div></div>"
        for r in recent
    ) or "<p>Nothing recorded yet — check back after the first successful run.</p>"

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Serenity Tracker</title>
<style>
:root {{ --bg:#101418; --panel:#1a2027; --ink:#e8edf2; --dim:#8a97a5;
        --bull:#3ecf8e; --bear:#ff6b6b; --line:#2a323c; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
       font:16px/1.55 "Avenir Next","Segoe UI",sans-serif; padding:20px; }}
header {{ border-left:4px solid var(--bull); padding-left:14px; margin-bottom:24px; }}
h1 {{ margin:0; font-size:1.5rem; letter-spacing:.02em; }}
.sub {{ color:var(--dim); font-size:.85rem; }}
.tab {{ background:none; border:1px solid var(--line); color:var(--dim);
        padding:8px 16px; border-radius:20px; margin-right:8px; font-size:.9rem; }}
.tab.active {{ color:var(--ink); border-color:var(--bull); }}
table {{ width:100%; border-collapse:collapse; margin-top:16px; display:none; }}
table.active {{ display:table; }}
th,td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line);
         font-size:.92rem; }}
th {{ color:var(--dim); font-weight:600; font-size:.78rem;
     text-transform:uppercase; letter-spacing:.06em; }}
.tk {{ font-weight:700; }}
.bull,.bullish {{ color:var(--bull); }} .bear,.bearish {{ color:var(--bear); }}
.neutral {{ color:var(--dim); }}
h2 {{ margin-top:36px; font-size:1.05rem; color:var(--dim);
     text-transform:uppercase; letter-spacing:.08em; }}
.card {{ background:var(--panel); border:1px solid var(--line);
         border-radius:10px; padding:14px 16px; margin-top:12px; }}
.card-top {{ display:flex; justify-content:space-between; margin-bottom:6px; }}
.badge {{ font-size:.75rem; padding:2px 10px; border-radius:12px;
          border:1px solid currentColor; }}
.card p {{ margin:4px 0; font-size:.92rem; }}
.meta {{ color:var(--dim); font-size:.8rem; }}
a {{ color:var(--bull); }}
footer {{ margin-top:40px; color:var(--dim); font-size:.78rem; }}
</style></head><body>
<header><h1>Serenity Tracker</h1>
<div class="sub">Public posts of @{TARGET_HANDLE}, auto-classified daily · updated {updated}</div></header>
<div>{''.join(tabs_html)}</div>
{''.join(tables_html)}
<h2>Recent mentions</h2>
{feed}
<footer>Automated research interface for public posts. Not affiliated with the account owner. Not investment advice.</footer>
<script>
function show(n) {{
  document.querySelectorAll('.period').forEach((t,i)=>t.classList.toggle('active',i===n));
  document.querySelectorAll('.tab').forEach((b,i)=>b.classList.toggle('active',i===n));
}}
</script></body></html>"""
    os.makedirs(os.path.dirname(DASHBOARD_PATH), exist_ok=True)
    with open(DASHBOARD_PATH, "w") as f:
        f.write(html)
    print(f"Dashboard written to {DASHBOARD_PATH}")


# ----------------------------- main -----------------------------

if __name__ == "__main__":
    posts = fetch_posts()
    new_rows = classify_posts(posts)
    history = merge(load_history(), new_rows)
    save_history(history)
    build_dashboard(history)
    print("Done.")
