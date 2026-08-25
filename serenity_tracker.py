"""
Multi-Account X Tracker (v3 — diagnostic build)
-----------------------------------------------
Same pipeline as before, but fetch_posts() now:
  * prints the raw Apify response shape (how many items, what fields)
  * tries several input formats until one returns data
  * reports exactly which format worked, so we can lock it in later

Secrets required: APIFY_TOKEN, APIFY_ACTOR_ID, GEMINI_API_KEY
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ----------------------------- configuration -----------------------------

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
APIFY_ACTOR_ID = os.environ.get("APIFY_ACTOR_ID", "altimis~scweet")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

TARGET_HANDLES = [h.strip().lstrip("@") for h in os.environ.get(
    "TARGET_HANDLES",
    "aleabitoreddit,gmpnavi,NURadu_,TheObserverLee,Alisvolatprop12"
).split(",") if h.strip()]

MAX_POSTS = int(os.environ.get("MAX_POSTS", "100"))  # actor minimum is 100

HISTORY_PATH = "data/history.json"
DASHBOARD_PATH = "docs/index.html"

# Set to "1" in the workflow to print raw API responses for debugging.
DEBUG = os.environ.get("DEBUG", "1") == "1"


def fail(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def http_json(url, payload=None, timeout=300):
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

def input_variants(handle):
    """
    Different actors want different field names. We try these in order
    and keep the first one that actually returns items.
    """
    return [
        ("search_query only", {
            "search_query": f"from:{handle}",
            "search_sort": "Latest",
            "source_mode": "auto",
            "tweet_type": "all",
            "max_items": MAX_POSTS,
        }),
        ("profile_urls (@handle)", {
            "profile_urls": [f"@{handle}"],
            "search_sort": "Latest",
            "source_mode": "auto",
            "tweet_type": "all",
            "max_items": MAX_POSTS,
        }),
        ("profile_urls (full URL)", {
            "profile_urls": [f"https://x.com/{handle}"],
            "search_sort": "Latest",
            "source_mode": "auto",
            "tweet_type": "all",
            "max_items": MAX_POSTS,
        }),
        ("both fields", {
            "profile_urls": [f"https://x.com/{handle}"],
            "search_query": f"from:{handle}",
            "search_sort": "Latest",
            "source_mode": "auto",
            "tweet_type": "all",
            "max_items": MAX_POSTS,
        }),
    ]


def extract_text(item):
    """Pull post text out of whatever field name the actor used."""
    for key in ("text", "full_text", "fullText", "tweet", "content",
                "tweet_text", "tweetText", "body", "rawContent", "renderedContent"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def extract_url(item):
    for key in ("url", "tweetUrl", "tweet_url", "link", "permalink", "twitterUrl"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Some actors give only an ID — build the URL from it.
    tid = item.get("id") or item.get("id_str") or item.get("tweet_id") or item.get("conversation_id")
    user = item.get("username") or item.get("userName") or item.get("screen_name")
    if tid and user:
        return f"https://x.com/{user}/status/{tid}"
    if tid:
        return f"https://x.com/i/status/{tid}"
    return ""


def extract_date(item):
    for key in ("created_at", "createdAt", "date", "timestamp", "time", "post_date"):
        v = item.get(key)
        if v:
            return str(v)
    return ""


def normalize(items, handle):
    """Turn raw actor output into our own post format."""
    posts = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = extract_text(it)
        if not text:
            continue
        posts.append({
            "text": text,
            "url": extract_url(it),
            "date": extract_date(it),
            "author": handle,
        })
    return posts


def run_actor(url, actor_input):
    """Call Apify. Returns (items, error_string)."""
    try:
        items = http_json(url, actor_input)
        return items, None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:300]
        except Exception:
            body = ""
        return None, f"HTTP {e.code} {body}"
    except Exception as e:
        return None, str(e)


def fetch_posts():
    if not APIFY_TOKEN or not APIFY_ACTOR_ID:
        fail("APIFY_TOKEN or APIFY_ACTOR_ID is missing (check GitHub Secrets).")

    url = (f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}"
           f"/run-sync-get-dataset-items?token={APIFY_TOKEN}")

    all_posts = []
    working_format = None   # once one format works, reuse it for the rest

    for handle in TARGET_HANDLES:
        print(f"\n=== @{handle} ===")
        variants = input_variants(handle)
        if working_format:
            # put the known-good format first
            variants.sort(key=lambda v: v[0] != working_format)

        got = []
        for label, actor_input in variants:
            print(f"  trying input format: {label}")
            items, err = run_actor(url, actor_input)

            if err:
                print(f"    failed: {err}")
                continue
            if not isinstance(items, list):
                print(f"    unexpected response type: {type(items).__name__} "
                      f"{str(items)[:200]}")
                continue

            print(f"    raw items returned: {len(items)}")
            if DEBUG and items:
                first = items[0] if isinstance(items[0], dict) else {}
                print(f"    first item field names: {sorted(first.keys())[:25]}")
                print(f"    first item sample: {json.dumps(first, ensure_ascii=False)[:400]}")

            posts = normalize(items, handle)
            print(f"    usable posts after parsing: {len(posts)}")

            if posts:
                got = posts
                working_format = label
                print(f"    ✓ format '{label}' works")
                break
            elif items:
                print("    ! items returned but no text field recognized — "
                      "see field names above")

        if not got:
            print(f"  no posts obtained for @{handle}")
        all_posts.extend(got)

    print(f"\nTotal posts collected: {len(all_posts)}")
    if working_format:
        print(f"Working input format: {working_format}")
    return all_posts


# ----------------------------- step 2: classify -----------------------------

CLASSIFY_PROMPT = """You are a strict data extractor. Below are public social media
posts from several stock market commentators. For EACH post, list every stock
ticker mentioned (e.g. NVDA, TSM, 005930.KS). For each ticker, judge the
author's stance in that post: "bullish", "bearish", or "neutral".
Skip posts with no tickers. Do not invent tickers.

Respond ONLY with a JSON array, no markdown fences, no commentary. Schema:
[{"post_index": 0, "ticker": "NVDA", "stance": "bullish", "note": "one short reason"}]

Posts:
"""


def classify_batch(batch, offset):
    numbered = "\n\n".join(f"[{i + offset}] {p['text'][:600]}" for i, p in enumerate(batch))
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    payload = {"contents": [{"parts": [{"text": CLASSIFY_PROMPT + numbered}]}]}
    try:
        resp = http_json(url, payload)
        raw = resp["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.HTTPError as e:
        print(f"  Gemini HTTP {e.code}: {e.read().decode()[:300]}")
        return []
    except Exception as e:
        print(f"  Gemini call failed: {e}")
        return []

    raw = re.sub(r"```json|```", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        print(f"  Could not parse Gemini output: {raw[:300]}")
        return []


def classify_posts(posts):
    if not GEMINI_API_KEY:
        fail("GEMINI_API_KEY is missing (check GitHub Secrets).")
    if not posts:
        print("No posts to classify.")
        return []

    rows = []
    for start in range(0, len(posts), 25):
        batch = posts[start:start + 25]
        print(f"Classifying posts {start}-{start + len(batch) - 1} with {GEMINI_MODEL}...")
        rows.extend(classify_batch(batch, start))

    results = []
    for r in rows:
        try:
            idx = int(r["post_index"])
            if 0 <= idx < len(posts):
                p = posts[idx]
                results.append({
                    "ticker": str(r["ticker"]).upper().strip(),
                    "stance": str(r.get("stance", "neutral")).lower().strip(),
                    "note": str(r.get("note", ""))[:200],
                    "author": p.get("author", ""),
                    "post_url": p["url"],
                    "post_date": p["date"],
                    "post_text": p["text"][:280],
                })
        except Exception:
            continue

    print(f"Extracted {len(results)} ticker mentions.")
    return results


# ----------------------------- step 3: history -----------------------------

def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH) as f:
                return json.load(f)
        except Exception:
            print("history.json unreadable; starting fresh.")
    return []


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)


def merge(history, new_rows):
    seen = {(h.get("post_url"), h.get("ticker")) for h in history}
    added = 0
    now = datetime.now(timezone.utc).isoformat()
    for r in new_rows:
        # fall back to text-based key if the actor gave no URL
        key = (r["post_url"] or r["post_text"][:60], r["ticker"])
        if key not in seen:
            r["recorded_at"] = now
            history.append(r)
            seen.add(key)
            added += 1
    print(f"Added {added} new mentions (duplicates skipped).")
    return history


# ----------------------------- step 4: dashboard -----------------------------

def parse_when(row):
    for key in ("post_date", "recorded_at"):
        v = str(row.get(key, ""))
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            continue
    return datetime.now(timezone.utc)


def summarize(history, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    table = {}
    for r in history:
        if parse_when(r) < cutoff:
            continue
        t = table.setdefault(r["ticker"], {
            "bullish": 0, "bearish": 0, "neutral": 0,
            "total": 0, "voices": set(), "latest": ""
        })
        stance = r["stance"] if r["stance"] in ("bullish", "bearish", "neutral") else "neutral"
        t[stance] += 1
        t["total"] += 1
        if r.get("author"):
            t["voices"].add(r["author"])
        t["latest"] = stance
    return sorted(table.items(), key=lambda kv: (-kv[1]["total"], kv[0]))


def build_dashboard(history):
    periods = [("7 days", 7), ("30 days", 30), ("90 days", 90)]
    tabs, tables = [], []
    for i, (label, days) in enumerate(periods):
        rows = summarize(history, days)
        body = "".join(
            f"<tr><td class='tk'>{t}</td><td>{d['total']}</td>"
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
            f"<table class='period {active}'><thead><tr><th>Ticker</th><th>Mentions</th>"
            f"<th>Voices</th><th>Bull</th><th>Bear</th><th>Neutral</th>"
            f"<th>Latest</th></tr></thead><tbody>{body}</tbody></table>"
        )

    recent = sorted(history, key=parse_when, reverse=True)[:60]
    feed = "".join(
        f"<div class='card'><div class='card-top'>"
        f"<span><span class='tk'>{r['ticker']}</span>"
        f"<span class='who'>@{r.get('author','')}</span></span>"
        f"<span class='badge {r['stance']}'>{r['stance']}</span></div>"
        f"<p>{r['post_text']}</p><div class='meta'>{r.get('note','')}"
        + (f" · <a href='{r['post_url']}'>original post</a>" if r.get("post_url") else "")
        + "</div></div>"
        for r in recent
    ) or "<p>Nothing recorded yet — check the Actions log for what the fetch returned.</p>"

    watched = ", ".join("@" + h for h in TARGET_HANDLES)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Voices Tracker</title><style>
:root {{ --bg:#0f1317; --panel:#181f26; --ink:#e9eef3; --dim:#8895a3;
  --bull:#3ecf8e; --bear:#ff6b6b; --line:#28313b; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); padding:20px;
  font:16px/1.55 "Avenir Next","Segoe UI",sans-serif; }}
header {{ border-left:4px solid var(--bull); padding-left:14px; margin-bottom:22px; }}
h1 {{ margin:0; font-size:1.45rem; }}
.sub {{ color:var(--dim); font-size:.82rem; margin-top:4px; }}
nav a {{ font-size:.85rem; }}
.tab {{ background:none; border:1px solid var(--line); color:var(--dim);
  padding:8px 16px; border-radius:20px; margin:0 8px 8px 0; font-size:.9rem; }}
.tab.active {{ color:var(--ink); border-color:var(--bull); }}
table {{ width:100%; border-collapse:collapse; margin-top:14px; display:none; }}
table.active {{ display:table; }}
th,td {{ text-align:left; padding:9px 6px; border-bottom:1px solid var(--line); font-size:.9rem; }}
th {{ color:var(--dim); font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; }}
.tk {{ font-weight:700; }}
.who {{ color:var(--dim); font-size:.8rem; margin-left:8px; }}
.bullish {{ color:var(--bull); }} .bearish {{ color:var(--bear); }} .neutral {{ color:var(--dim); }}
h2 {{ margin-top:34px; font-size:.95rem; color:var(--dim);
  text-transform:uppercase; letter-spacing:.08em; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:13px 15px; margin-top:11px; }}
.card-top {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }}
.badge {{ font-size:.72rem; padding:2px 10px; border-radius:12px; border:1px solid currentColor; }}
.card p {{ margin:4px 0; font-size:.9rem; }}
.meta {{ color:var(--dim); font-size:.78rem; }}
a {{ color:var(--bull); }}
footer {{ margin-top:38px; color:var(--dim); font-size:.76rem; line-height:1.6; }}
</style></head><body>
<header><h1>Market Voices Tracker</h1>
<div class="sub">Watching {watched} · updated {updated}</div></header>
<nav><a href="analysis.html">Consensus &amp; track record →</a></nav>
<div style="margin-top:16px">{''.join(tabs)}</div>
{''.join(tables)}
<h2>Recent mentions</h2>
{feed}
<footer>Automated research interface for public posts. Not affiliated with any tracked
account. Classifications are AI-generated and may be wrong — always read the original
post. Not investment advice.</footer>
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
    rows = classify_posts(posts)
    history = merge(load_history(), rows)
    save_history(history)
    build_dashboard(history)
    print("Done.")
