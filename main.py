import asyncio
import calendar
import json
import time
from math import ceil
from datetime import date, datetime
from typing import Optional, Dict, Any, Tuple

import pytz
from fastapi import FastAPI, Query, HTTPException
from pydantic import BaseModel
from selectolax.parser import HTMLParser
from curl_cffi import requests
from upstash_redis.asyncio import Redis
from dotenv import load_dotenv

# --------------------
# Constants & config
# --------------------
BASE_URL = "https://www.pcso.gov.ph/SearchLottoResult.aspx"

HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://www.pcso.gov.ph",
    "referer": BASE_URL,
}

MIN_START_DATE = date(2015, 1, 1)
MAX_END_DATE = datetime.now(pytz.timezone("Asia/Manila")).date()

# Month lookup optimization
MONTH_MAP = {m: i for i, m in enumerate(calendar.month_name) if m}

# Redis (Upstash)
load_dotenv()  # Load environment variables from .env file
redis = Redis.from_env()

# Caching TTLs
EVENT_TTL = 30         # seconds for hidden ASP.NET fields
RESULT_TTL = 60        # seconds for full query results

# Limit concurrent outbound requests to PCSO
OUTBOUND_SEMAPHORE = asyncio.Semaphore(8)

app = FastAPI(title="PCSO Lotto Results API (Async + Redis)", version="2.0.0")


# --------------------
# Models
# --------------------
class LottoResult(BaseModel):
    game: str
    combination: str
    draw_date: str
    jackpot_php: str
    winners: str


class SuccessResponse(BaseModel):
    success: bool
    message: str
    total_results: int
    total_pages: int
    current_page: int
    per_page: int
    elapsed_seconds: float
    start_date: str
    end_date: str
    results: list[LottoResult]


class ErrorResponse(BaseModel):
    success: bool
    message: str


# --------------------
# Helpers
# --------------------
def format_human_date(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def validate_and_resolve_dates(
    start_month: Optional[str],
    start_day: Optional[int],
    start_year: Optional[int],
    end_month: Optional[str],
    end_day: Optional[int],
    end_year: Optional[int],
) -> Tuple[date, date, str, int, int, str, int, int]:
    # Apply defaults
    if start_month is None or start_day is None or start_year is None:
        start_month = MIN_START_DATE.strftime("%B")
        start_day = MIN_START_DATE.day
        start_year = MIN_START_DATE.year
    if end_month is None or end_day is None or end_year is None:
        end_month = MAX_END_DATE.strftime("%B")
        end_day = MAX_END_DATE.day
        end_year = MAX_END_DATE.year

    # Month validation
    if start_month not in MONTH_MAP or end_month not in MONTH_MAP:
        raise HTTPException(status_code=400, detail="Invalid month name provided.")

    # Date object validation
    try:
        start_dt = date(start_year, MONTH_MAP[start_month], start_day)
        end_dt = date(end_year, MONTH_MAP[end_month], end_day)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid day for the given month/year.")

    # Range enforcement
    if start_dt < MIN_START_DATE:
        raise HTTPException(
            status_code=400,
            detail=f"Start date cannot be earlier than {format_human_date(MIN_START_DATE)}."
        )
    if end_dt > MAX_END_DATE:
        raise HTTPException(
            status_code=400,
            detail=f"End date cannot be later than {format_human_date(MAX_END_DATE)} (today in Manila)."
        )
    if end_dt < start_dt:
        raise HTTPException(status_code=400, detail="End date cannot be earlier than start date.")

    return (
        start_dt, end_dt,
        start_month, start_day, start_year,
        end_month, end_day, end_year
    )


def make_result_cache_key(start_dt: date, end_dt: date) -> str:
    # Game is fixed to "0" (All) in this version; include if you later expose it
    return f"pcso:results:{start_dt.isoformat()}:{end_dt.isoformat()}:game0"


def event_cache_key() -> str:
    return "pcso:event_fields"


# --------------------
# Scraper (async)
# --------------------
async def get_event_fields_async(session: requests.AsyncSession) -> Dict[str, str]:
    # Try Redis cache first
    cache = await redis.get(event_cache_key())
    if cache:
        try:
            fields = json.loads(cache)
            if isinstance(fields, dict) and fields:
                return fields
        except Exception:
            pass  # fall through to refresh

    async with OUTBOUND_SEMAPHORE:
        r = await session.get(BASE_URL, headers=HEADERS)
    r.raise_for_status()
    tree = HTMLParser(r.text)

    fields: Dict[str, str] = {}
    for name in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION"]:
        node = tree.css_first(f'input[name="{name}"]')
        if node:
            fields[name] = node.attributes.get("value", "")

    if not fields:
        raise ValueError("Failed to extract hidden event fields.")

    # Cache in Redis
    await redis.set(event_cache_key(), json.dumps(fields), ex=EVENT_TTL)
    return fields


async def scrape_lotto_results_async(
    start_month: str, start_day: int, start_year: int,
    end_month: str, end_day: int, end_year: int,
) -> Tuple[list[LottoResult], int]:
    # Check result cache
    start_dt = date(start_year, MONTH_MAP[start_month], start_day)
    end_dt = date(end_year, MONTH_MAP[end_month], end_day)
    rkey = make_result_cache_key(start_dt, end_dt)

    cached = await redis.get(rkey)
    if cached:
        try:
            payload = json.loads(cached)
            rows = payload.get("rows", [])
            total_rows = payload.get("total_rows", len(rows))
            # Rehydrate to Pydantic
            results = [LottoResult(**row) for row in rows]
            return results, total_rows
        except Exception:
            pass  # ignore cache error and proceed to scrape

    async with requests.AsyncSession() as s:
        event_fields = await get_event_fields_async(s)

        form = {
            **event_fields,
            "ctl00$ctl00$cphContainer$cpContent$ddlStartMonth": start_month,
            "ctl00$ctl00$cphContainer$cpContent$ddlStartDate": str(start_day),
            "ctl00$ctl00$cphContainer$cpContent$ddlStartYear": str(start_year),
            "ctl00$ctl00$cphContainer$cpContent$ddlEndMonth": end_month,
            "ctl00$ctl00$cphContainer$cpContent$ddlEndDay": str(end_day),
            "ctl00$ctl00$cphContainer$cpContent$ddlEndYear": str(end_year),
            "ctl00$ctl00$cphContainer$cpContent$ddlSelectGame": "0",  # All
            "ctl00$ctl00$cphContainer$cpContent$btnSearch": "Search Lotto",
        }

        async with OUTBOUND_SEMAPHORE:
            r = await s.post(BASE_URL, headers=HEADERS, data=form)
        r.raise_for_status()

    tree = HTMLParser(r.text)
    table = tree.css_first("table.search-lotto-result-table")
    if not table:
        return [], 0

    # Optional schema check (defensive)
    header_tr = table.css_first("tr")
    expected_headers = ["LOTTO GAME", "COMBINATIONS", "DRAW DATE", "JACKPOT (PHP)", "WINNERS"]
    if header_tr:
        headers = [th.text(strip=True) for th in header_tr.css("th")]
        if headers != expected_headers:
            raise ValueError("Unexpected table structure from PCSO site.")

    body_rows = table.css("tr")[1:]
    total_rows = len(body_rows)

    results: list[LottoResult] = []
    for row in body_rows:
        cols = [td.text(strip=True) for td in row.css("td")]
        if cols and len(cols) == 5:
            results.append(LottoResult(
                game=cols[0],
                combination=cols[1],
                draw_date=cols[2],
                jackpot_php=cols[3],
                winners=cols[4],
            ))

    # Cache parsed results (compact JSON)
    try:
        to_store = {
            "rows": [r.model_dump() for r in results],
            "total_rows": total_rows,
        }
        await redis.set(rkey, json.dumps(to_store), ex=RESULT_TTL)
    except Exception:
        pass  # cache failure should not break the request

    return results, total_rows


# --------------------
# API Endpoint (async)
# --------------------
@app.get(
    "/lotto-results",
    response_model=SuccessResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
)
async def get_lotto_results(
    start_month: Optional[str] = Query(None, example="September"),
    start_day: Optional[int] = Query(None, ge=1, le=31, example=2),
    start_year: Optional[int] = Query(None, ge=1900, le=2100, example=2025),
    end_month: Optional[str] = Query(None, example="September"),
    end_day: Optional[int] = Query(None, ge=1, le=31, example=4),
    end_year: Optional[int] = Query(None, ge=1900, le=2100, example=2025),
    page: int = Query(1, ge=1, example=1),
    per_page: int = Query(50, ge=1, le=50, example=50),
):
    # Validate and resolve dates with defaults
    (
        start_dt, end_dt,
        start_month, start_day, start_year,
        end_month, end_day, end_year
    ) = validate_and_resolve_dates(
        start_month, start_day, start_year,
        end_month, end_day, end_year
    )

    start_time = time.perf_counter()

    try:
        all_results, total_rows = await scrape_lotto_results_async(
            start_month, start_day, start_year,
            end_month, end_day, end_year,
        )
    except requests.RequestsError as re:
        raise HTTPException(status_code=502, detail=f"Network error contacting PCSO: {re}")
    except ValueError as ve:
        raise HTTPException(status_code=500, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")

    if total_rows == 0 or not all_results:
        raise HTTPException(status_code=404, detail="No results found for the given parameters.")

    total_pages = max(ceil(total_rows / per_page), 1)
    if page > total_pages:
        raise HTTPException(status_code=400, detail="Page number exceeds total pages.")

    start_idx = (page - 1) * per_page
    end_idx = min(start_idx + per_page, total_rows)
    paginated = all_results[start_idx:end_idx]

    elapsed = time.perf_counter() - start_time

    return SuccessResponse(
        success=True,
        message="Lotto results retrieved successfully.",
        total_results=total_rows,
        total_pages=total_pages,
        current_page=page,
        per_page=per_page,
        elapsed_seconds=round(elapsed, 3),
        start_date=format_human_date(start_dt),
        end_date=format_human_date(end_dt),
        results=paginated,
    )
