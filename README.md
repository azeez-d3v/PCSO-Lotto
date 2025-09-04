# PCSO Lotto Results API (Async + Redis)

FastAPI service that scrapes official Philippine Charity Sweepstakes Office (PCSO) lotto draw results on‑demand, adds Redis (Upstash) caching, pagination, and robust date validation.

## Features

* Async scraping with `curl_cffi` + `selectolax` (fast HTML parsing)
* Smart caching in Upstash Redis (hidden ASP.NET event fields + query results)
* Date range normalization with safe defaults (2015-01-01 → today Asia/Manila)
* Pagination (page/per_page) with bounded page size (max 50)
* Lightweight rate limiting via outbound semaphore (limits concurrent upstream requests)
* Typed responses via Pydantic models

## Tech Stack

FastAPI, asyncio, curl_cffi, selectolax, Upstash Redis (`upstash-redis` / `upstash_redis`), Pydantic, uv / pip.

## Environment Variables (.env)

Create a `.env` file (or set in Railway) with at least:

```bash
UPSTASH_REDIS_REST_URL=https://<your-upstash-endpoint>
UPSTASH_REDIS_REST_TOKEN=<your-upstash-rest-token>
```

The code calls `Redis.from_env()` so defaults follow the official Upstash variable names.

## Install & Run (Local)

Using uv (preferred if `uv` is installed):

```bash
uv sync
uv run fastapi run main:app --port 8000
```

Using pip:

```bash
python -m venv .venv
./.venv/Scripts/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

## Endpoint

`GET /lotto-results`

Returns paginated lotto draw records.

## Query Parameters

| Name | Type | Constraints | Default Behavior | Example |
|------|------|-------------|------------------|---------|
| start_month | str | Full month name | Defaults to Jan 2015 if any start_* missing | September |
| start_day | int | 1–31 | ″ | 1 |
| start_year | int | 2015–current | ″ | 2024 |
| end_month | str | Full month name | Defaults to today (Asia/Manila) if any end_* missing | September |
| end_day | int | 1–31 | ″ | 4 |
| end_year | int | 2015–current | ″ | 2025 |
| page | int | >=1 | 1 | 1 |
| per_page | int | 1–50 | 50 | 25 |

If the range yields no rows a 404 is returned. Page overflow returns 400.

## Response (Success)

```json
{
    "success": true,
    "message": "Lotto results retrieved successfully.",
    "total_results": 123,
    "total_pages": 3,
    "current_page": 1,
    "per_page": 50,
    "elapsed_seconds": 0.237,
    "start_date": "September 1, 2025",
    "end_date": "September 4, 2025",
    "results": [
        {
            "game": "Ultra Lotto 6/58",
            "combination": "01-12-23-34-45-56",
            "draw_date": "09/04/2025",
            "jackpot_php": "₱49,500,000.00",
            "winners": "0"
        }
    ]
}
```

## Error Shape

```json
{
    "success": false,
    "message": "<detail>"
}
```

## Caching Strategy

| Item | Key Pattern | TTL (s) | Notes |
|------|-------------|---------|-------|
| Hidden event fields | pcso:event_fields | 30 | Required ASP.NET viewstate data |
| Result set | pcso:results:{start}:{end}:game0 | 60 | Short TTL to keep data fresh |

## Rate / Load Safety

Outbound requests to the PCSO site are wrapped by an `asyncio.Semaphore(8)` limiting parallelism to 8 to reduce server stress.

## Deployment (Railway)

`railway.json` present. Add environment variables in Railway dashboard and deploy. Default start command can be (adjust as needed):

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Disclaimer

Not affiliated with PCSO. Data is scraped from the public website;
