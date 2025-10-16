# Get X/Twitter User Tweets for a Specific Month (JSON Export)

Fetch all posts (“tweets”) for one or more X/Twitter users within a given **UTC month**, then save the results to JSON.  
The script paginates carefully, backs off on rate-limits, and avoids wasting requests (useful on the free 100-request/month plan).

## Quick Start

```bash
# 1) Python deps
pip install requests

# 2) Run
python x_month_export.py --bearer-token YOUR_TOKEN --usernames jack elon --month 2024-08
```

> Replace `x_month_export.py` with the actual filename in this repo if different.

## Required Inputs

- **Bearer token** (X API OAuth2 app-only):  
  pass with `--bearer-token` **or** via env var `X_BEARER_TOKEN`
- **Username(s)** (no `@`): `--usernames jack elon`
- **Month (UTC)** in `YYYY-MM`: `--month 2024-08`

If any of these are missing, the script will exit.

## What It Does

- Looks up user ID(s) via `GET /2/users/by/username/:username`
- Paginates `GET /2/users/:id/tweets` between month start/end (UTC)
- Respects `Retry-After`/rate limits with exponential backoff
- Saves **incremental progress** per page to `posts_<username>_<YYYY-MM>.partial.json`
- Writes final JSON to `posts_<username>_<YYYY-MM>.json`

## Usage

```bash
# Single user
python x_month_export.py --bearer-token $X_BEARER_TOKEN \
  --usernames jack \
  --month 2024-08

# Multiple users
python x_month_export.py --bearer-token $X_BEARER_TOKEN \
  --usernames jack elon nasa \
  --month 2024-08

# Include replies/retweets, custom outdir and page size (<=100)
python x_month_export.py --bearer-token $X_BEARER_TOKEN \
  --usernames nasa \
  --month 2024-08 \
  --include-replies --include-retweets \
  --outdir ./exports --per-page 100
```

### Flags

- `--bearer-token` (string) **required** if `X_BEARER_TOKEN` not set
- `--usernames` (one or more) **required**
- `--month` (`YYYY-MM`, UTC) **required**
- `--include-replies` (optional)
- `--include-retweets` (optional)
- `--outdir` (default `.`)
- `--per-page` (default `100`, max `100`)

## Output

Each run creates:

- `posts_<username>_<YYYY-MM>.json` → final payload:
  ```json
  {
    "username": "jack",
    "user_id": "12",
    "month": "2024-08",
    "start_time": "2024-08-01T00:00:00Z",
    "end_time": "2024-09-01T00:00:00Z",
    "count": 123,
    "posts": [ /* raw tweet objects from /2/users/:id/tweets */ ]
  }
  ```
- `posts_<username>_<YYYY-MM>.partial.json` → incremental checkpoint (kept for inspection)

## Notes & Limits

- **UTC window**: `--month` is interpreted in **UTC** (e.g., `2024-08` = `2024-08-01T00:00:00Z` through just before `2024-09-01T00:00:00Z`).
- **Rate limits**: Script backs off on `429/503` and minimizes wasted requests; still subject to your plan’s allowances.
- **Fields/expansions**: Requests include useful `tweet.fields`, `user.fields`, `expansions`, and `media.fields`. Adjust in code if needed.

## Environment

- Python 3.8+  
- `requests`

```bash
pip install requests
```

## Disclaimer

You are responsible for complying with X’s Developer Policy, terms, and applicable laws. API availability, endpoints, and rate limits may change.
