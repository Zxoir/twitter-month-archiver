import os
import sys
import time
import json
import argparse
import calendar
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import requests

API_BASE = "https://api.x.com/2"

def iso_month_bounds(year: int, month: int):
    """Return UTC ISO-8601 start_time and end_time for the given month."""
    # Start at 00:00:00 on the 1st, end is first second of the next month
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc) + timedelta(seconds=1)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")

def backoff_sleep(resp: requests.Response, default_sec: int = 60):
    """Sleep respecting X-RateLimit or Retry-After headers when present."""
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            time.sleep(int(retry_after))
            return
        except ValueError:
            pass
    # fall back to simple backoff
    time.sleep(default_sec)

def fetch_user_id(bearer: str, username: str) -> Optional[str]:
    url = f"{API_BASE}/users/by/username/{username}"
    headers = {"Authorization": f"Bearer {bearer}"}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 200:
        data = r.json()
        return data.get("data", {}).get("id")
    elif r.status_code in (429, 503):
        backoff_sleep(r, default_sec=60)
        return fetch_user_id(bearer, username)
    else:
        print(f"[WARN] Failed to look up @{username}: {r.status_code} {r.text}", file=sys.stderr)
        return None

def fetch_user_posts_for_month(
    bearer: str,
    user_id: str,
    start_time_iso: str,
    end_time_iso: str,
    include_replies: bool = True,
    include_retweets: bool = True,
    max_per_request: int = 100,
    verbose: bool = True,
    # NEW: path to save incremental progress after each page (optional)
    incremental_save_path: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Paginate through /2/users/:id/tweets bounded by start_time/end_time.
    Returns a list of post objects as returned by the API.

    Added behaviors:
    - Save incrementally per page (if incremental_save_path provided).
    - Stop when fewer than max_results are returned for a page.
    - Stop when the oldest tweet in the page is older than start_time.
    - Track repeated next_token or empty data and stop.
    - Use meta.result_count to short-circuit when 0.
    """
    url = f"{API_BASE}/users/{user_id}/tweets"
    headers = {"Authorization": f"Bearer {bearer}"}

    excludes = []
    if not include_replies:
        excludes.append("replies")
    if not include_retweets:
        excludes.append("retweets")

    params = {
        "start_time": start_time_iso,
        "end_time": end_time_iso,
        "max_results": max_per_request,  # up to 100 for timelines
        "tweet.fields": ",".join([
            "id",
            "text",
            "created_at",
            "public_metrics",
            "lang",
            "possibly_sensitive",
            "source",
            "in_reply_to_user_id",
            "referenced_tweets",
            "attachments",
            "entities"
        ]),
        "expansions": ",".join([
            "author_id",
            "attachments.media_keys",
            "referenced_tweets.id"
        ]),
        "user.fields": "id,name,username,verified,created_at",
        "media.fields": "media_key,type,url,width,height,alt_text"
    }
    if excludes:
        params["exclude"] = ",".join(excludes)

    all_posts: List[Dict[str, Any]] = []
    next_token: Optional[str] = None

    # NEW: for repeated token detection
    seen_tokens = set()

    # Precompute window bounds for comparisons
    window_start_dt = datetime.fromisoformat(start_time_iso.replace("Z", "+00:00"))
    window_end_dt = datetime.fromisoformat(end_time_iso.replace("Z", "+00:00"))

    page_idx = 0

    while True:
        if next_token:
            params["pagination_token"] = next_token
        else:
            params.pop("pagination_token", None)

        r = requests.get(url, headers=headers, params=params, timeout=60)

        if r.status_code == 200:
            payload = r.json()
            posts = payload.get("data", []) or []
            meta = payload.get("meta", {}) or {}
            includes = payload.get("includes", {}) or {}

            # NEW: meta.result_count can indicate emptiness ahead of time
            result_count = meta.get("result_count", 0)

            page_idx += 1
            got = len(posts)
            if verbose:
                print(f"Fetched {got} posts (page {page_idx}). next_token={meta.get('next_token')} result_count={result_count}")

            # Append raw page data
            all_posts.extend(posts)

            # NEW: incremental save after every page
            if incremental_save_path:
                try:
                    # Save a compact progress payload (safe if interrupted)
                    prog_payload = {
                        "user_id": user_id,
                        "start_time": start_time_iso,
                        "end_time": end_time_iso,
                        "page": page_idx,
                        "count_so_far": len(all_posts),
                        "meta": meta,
                        "includes": includes,  # latest includes; not strictly cumulative
                        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "posts_so_far": all_posts,
                    }
                    with open(incremental_save_path, "w", encoding="utf-8") as f:
                        json.dump(prog_payload, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    if verbose:
                        print(f"[WARN] Failed incremental save to {incremental_save_path}: {e}", file=sys.stderr)

            # NEW: stopping conditions

            # 1) Empty data or meta.result_count == 0 => stop
            if not posts or result_count == 0:
                if verbose:
                    print("[STOP] Empty page or result_count==0.")
                break

            # 2) Fewer than max_results => likely last page, stop
            if got < params["max_results"]:
                if verbose:
                    print(f"[STOP] Page returned fewer ({got}) than max_results ({params['max_results']}).")
                # Note: still processed/kept this page; now stop.
                break

            # 3) Oldest tweet < start_time => we've paged past the window, stop
            try:
                oldest_dt = min(
                    (datetime.fromisoformat(p.get("created_at").replace("Z", "+00:00"))
                     for p in posts if p.get("created_at")),
                    default=None
                )
                if oldest_dt is not None and oldest_dt < window_start_dt:
                    if verbose:
                        print(f"[STOP] Oldest tweet on this page ({oldest_dt.isoformat()}) < start_time ({window_start_dt.isoformat()}).")
                    break
            except Exception as e:
                # If parsing fails, ignore this condition and continue on other guards
                if verbose:
                    print(f"[WARN] Failed to compute oldest tweet time: {e}", file=sys.stderr)

            # Continue pagination
            new_token = meta.get("next_token")

            # 4) Track repeated next_token (loop protection)
            if new_token is None:
                if verbose:
                    print("[STOP] No next_token present.")
                break
            if new_token in seen_tokens:
                if verbose:
                    print("[STOP] Repeated next_token detected; stopping to avoid loop.")
                break
            seen_tokens.add(new_token)
            next_token = new_token

        elif r.status_code in (429, 503):
            if verbose:
                print("[INFO] Rate limited; backing off...", file=sys.stderr)
            backoff_sleep(r, default_sec=60)
            # loop continues; incremental file already has progress
            continue
        else:
            print(f"[ERROR] Fetch failed: {r.status_code} {r.text}", file=sys.stderr)
            break

    # Keep only posts whose created_at is inside the month window (guardrail)
    def in_window(p):
        ts = p.get("created_at")
        if not ts:
            return True
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return window_start_dt <= dt < window_end_dt

    return [p for p in all_posts if in_window(p)]

def save_json(filename: str, obj: Any):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def main():
    parser = argparse.ArgumentParser(description="Save all X posts for specific account(s) for a given month to JSON.")
    parser.add_argument("--bearer-token", default=os.getenv("X_BEARER_TOKEN"), help="OAuth 2.0 app-only Bearer token")
    parser.add_argument("--usernames", required=True, nargs="+", help="One or more X usernames without @")
    parser.add_argument("--month", required=True, help="Target month in YYYY-MM (UTC)")
    parser.add_argument("--include-replies", action="store_true", help="Include replies")
    parser.add_argument("--include-retweets", action="store_true", help="Include Retweets")
    parser.add_argument("--outdir", default=".", help="Output directory")
    parser.add_argument("--per-page", type=int, default=100, help="max_results per page (<=100)")
    args = parser.parse_args()

    if not args.bearer_token:
        print("Provide a Bearer token via --bearer-token or X_BEARER_TOKEN env var.", file=sys.stderr)
        sys.exit(1)

    year, month = map(int, args.month.split("-"))
    start_iso, end_iso = iso_month_bounds(year, month)

    os.makedirs(args.outdir, exist_ok=True)

    for username in args.usernames:
        uid = fetch_user_id(args.bearer_token, username)
        if not uid:
            continue

        print(f"== @{username} (id {uid}) | {start_iso} to {end_iso} ==")

        # NEW: incremental progress filepath for this user+month
        incremental_path = os.path.join(args.outdir, f"posts_{username}_{args.month}.partial.json")

        posts = fetch_user_posts_for_month(
            bearer=args.bearer_token,
            user_id=uid,
            start_time_iso=start_iso,
            end_time_iso=end_iso,
            include_replies=args.include_replies,
            include_retweets=args.include_retweets,
            max_per_request=min(max(args.per_page, 10), 100),
            incremental_save_path=incremental_path,  # enable incremental saves
        )

        outpath = os.path.join(args.outdir, f"posts_{username}_{args.month}.json")
        payload = {
            "username": username,
            "user_id": uid,
            "month": args.month,
            "start_time": start_iso,
            "end_time": end_iso,
            "count": len(posts),
            "posts": posts
        }
        save_json(outpath, payload)
        print(f"Saved {len(posts)} posts to {outpath}")

        # Optional: leave the partial file as a journal of progress.
        # If you prefer to remove it after success, uncomment below:
        # try:
        #     os.remove(incremental_path)
        # except OSError:
        #     pass

if __name__ == "__main__":
    import __main__ as _m
    _m.main()
