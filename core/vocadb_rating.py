import asyncio
from datetime import datetime

import aiohttp

from .database import (
    get_conn,
    recalculate_pools,
    set_meta,
    VOCADB_CANDIDATE_MAX_RANK,
)


VOCADB_SONGS_URL = "https://vocadb.net/api/songs"
USER_AGENT = (
    "AstrBot-DailyVocaloid/1.5.1 "
    "(VocaDB rating cache; contact via plugin owner)"
)
PAGE_SIZE = 50


class VocaDbApiError(RuntimeError):
    pass


def ensure_vocadb_rating_schema():
    # Rating columns are part of database.py's main schema from v1.5.3 onward.
    # Keep this function for compatibility with main.py, but do not run DDL
    # during ordinary WebUI requests.
    return True


def _api_params(start=0, max_results=PAGE_SIZE, min_score=None):
    params = {
        "songTypes": "Original",
        "status": "Finished",
        "start": int(start),
        "maxResults": min(max(int(max_results), 1), PAGE_SIZE),
        "sort": "RatingScore",
        "lang": "Japanese",
        "getTotalCount": "true" if int(start) == 0 else "false",
    }
    if min_score is not None:
        params["minScore"] = int(min_score)
    return params


async def fetch_rating_page(start=0, max_results=PAGE_SIZE, min_score=None):
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/json,*/*",
    }

    params = _api_params(
        start=start,
        max_results=max_results,
        min_score=min_score,
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:
        async with session.get(
            VOCADB_SONGS_URL,
            params=params,
            allow_redirects=True,
        ) as response:
            body = await response.text()

            if response.status >= 400:
                hint = ""
                lower = body.lower()
                if (
                    "cloudflare" in lower
                    or "just a moment" in lower
                    or response.status in (403, 429, 503)
                ):
                    hint = " (likely Cloudflare / edge protection)"
                raise VocaDbApiError(
                    f"VocaDB HTTP {response.status}{hint}"
                )

            try:
                data = await response.json(content_type=None)
            except Exception as exc:
                sample = body[:160].replace("\n", " ")
                raise VocaDbApiError(
                    f"VocaDB returned non-JSON data: {sample}"
                ) from exc

    if not isinstance(data, dict):
        raise VocaDbApiError("VocaDB returned an unexpected response")

    items = data.get("items") or []
    if not isinstance(items, list):
        raise VocaDbApiError("VocaDB response has no items list")

    return {
        "items": items,
        "total_count": data.get("totalCount"),
        "start": int(start),
    }


def _safe_int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def import_rating_items(items, rank_start=1):
    """
    Cache RatingScore from VocaDB into EXISTING local songs.

    No songs are created here. This module only enriches rows already imported
    from the local VocaDB dump.
    """
    try:
        rank_start = max(1, int(rank_start))
    except (TypeError, ValueError):
        rank_start = 1

    if not isinstance(items, list):
        raise ValueError("items must be a list")

    now = datetime.now().isoformat()
    matched_local = 0
    missing_local = 0
    scored = 0
    zero_score = 0

    conn = get_conn()
    try:
        for offset, item in enumerate(items):
            if not isinstance(item, dict):
                continue

            vocadb_id = item.get("id")
            if vocadb_id is None:
                continue

            try:
                rating_score = float(item.get("ratingScore") or 0)
            except (TypeError, ValueError):
                rating_score = 0.0

            favorited_times = _safe_int(item.get("favoritedTimes"))
            rank = rank_start + offset

            cur = conn.execute(
                """
                UPDATE songs
                SET rating_score=?,
                    vocadb_rank=?,
                    vocadb_favorited_times=?,
                    vocadb_rating_synced_at=?
                WHERE source='vocadb'
                  AND vocadb_id=?
                """,
                (
                    rating_score,
                    rank,
                    favorited_times,
                    now,
                    str(vocadb_id),
                ),
            )

            if cur.rowcount:
                matched_local += 1
                if rating_score > 0:
                    scored += 1
                else:
                    zero_score += 1
            else:
                missing_local += 1

        conn.commit()
    finally:
        conn.close()

    set_meta("last_vocadb_rating_sync", now)
    recalculate_pools()

    return {
        "ok": True,
        "received": len(items),
        "matched_local": matched_local,
        "missing_local": missing_local,
        "scored": scored,
        "zero_score": zero_score,
        "rank_start": rank_start,
    }


async def test_server_access():
    try:
        page = await fetch_rating_page(start=0, max_results=3)
        sample = []
        for item in page["items"][:3]:
            sample.append({
                "id": item.get("id"),
                "name": item.get("name") or item.get("defaultName") or "",
                "ratingScore": item.get("ratingScore"),
                "favoritedTimes": item.get("favoritedTimes"),
            })

        return {
            "ok": True,
            "route": "server",
            "sample": sample,
            "total_count": page.get("total_count"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "route": "server",
            "error": str(exc),
        }


async def sync_server_ratings(limit=500):
    try:
        limit = min(max(int(limit), 1), 2000)
    except (TypeError, ValueError):
        limit = 500

    result = {
        "ok": True,
        "route": "server",
        "requested": limit,
        "received": 0,
        "matched_local": 0,
        "missing_local": 0,
        "scored": 0,
        "zero_score": 0,
        "pages": 0,
    }

    start = 0
    while start < limit:
        size = min(PAGE_SIZE, limit - start)
        try:
            page = await fetch_rating_page(
                start=start,
                max_results=size,
            )
        except Exception as exc:
            if result["received"] == 0:
                return {
                    "ok": False,
                    "route": "server",
                    "error": str(exc),
                }

            result["partial"] = True
            result["error"] = str(exc)
            break

        items = page["items"]
        if not items:
            break

        imported = import_rating_items(
            items,
            rank_start=start + 1,
        )

        result["received"] += len(items)
        result["matched_local"] += imported["matched_local"]
        result["missing_local"] += imported["missing_local"]
        result["scored"] += imported["scored"]
        result["zero_score"] += imported["zero_score"]
        result["pages"] += 1

        start += len(items)
        if len(items) < size:
            break

        # Be polite to VocaDB. The public API documentation explicitly asks
        # clients to cache and avoid unnecessary request volume.
        await asyncio.sleep(0.35)

    recalculate_pools()
    return result


def rating_stats():
    ensure_vocadb_rating_schema()
    conn = get_conn()
    try:
        return {
            "vocadb_scored": conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE source='vocadb'
                  AND COALESCE(rating_score,0)>0
                """
            ).fetchone()[0],
            "vocadb_ranked": conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE source='vocadb'
                  AND vocadb_rank IS NOT NULL
                """
            ).fetchone()[0],
            "vocadb_candidates": conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE source='vocadb'
                  AND vocadb_rank BETWEEN 1 AND ?
                """,
                (VOCADB_CANDIDATE_MAX_RANK,),
            ).fetchone()[0],
            "candidate_max_rank": VOCADB_CANDIDATE_MAX_RANK,
            "best_rating": conn.execute(
                """
                SELECT COALESCE(MAX(rating_score),0)
                FROM songs
                WHERE source='vocadb'
                """
            ).fetchone()[0],
        }
    finally:
        conn.close()
