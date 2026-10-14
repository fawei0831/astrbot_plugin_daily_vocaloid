import asyncio
import re
from datetime import datetime
from pathlib import Path

import aiohttp

from .database import get_conn, recalculate_pools, set_meta
from .cover_cache import save_cover, touch_cover


VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
COVER_DIR = Path(
    "/AstrBot/data/plugin_data/astrbot_plugin_daily_vocaloid/covers"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
REQUEST_INTERVAL = 0.85
FOREIGN_VERIFY_RATIO = 0.70
VERIFY_CHUNK_SIZE = 20
VERIFY_CHUNK_COOLDOWN = 8
NETWORK_BACKOFF = (3, 8, 20)
RATE_LIMIT_WAIT = 60


class BilibiliApiError(RuntimeError):
    def __init__(self, message, code=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class BilibiliProtectionError(BilibiliApiError):
    pass


class RateLimiter:
    def __init__(self, interval=REQUEST_INTERVAL):
        self.interval = interval
        self.lock = asyncio.Lock()
        self.next_time = 0.0

    async def wait(self):
        loop = asyncio.get_running_loop()
        async with self.lock:
            now = loop.time()
            if now < self.next_time:
                await asyncio.sleep(self.next_time - now)
            self.next_time = loop.time() + self.interval


_limiter = RateLimiter()


def _extract_bvid(value):
    match = re.search(r"(BV[0-9A-Za-z]{8,})", str(value or ""), re.I)
    return match.group(1) if match else ""


def _extract_aid(value):
    text = str(value or "")
    match = re.search(r"(?:^|[/\s])av(\d+)(?:$|[/?#\s])", text, re.I)
    if not match:
        match = re.search(r"[?&]aid=(\d+)", text, re.I)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _normalize_image_url(url):
    url = str(url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


async def _get_json(session, url, params):
    await _limiter.wait()

    try:
        async with session.get(
            url,
            params=params,
            headers={"Referer": "https://www.bilibili.com/"},
            allow_redirects=True,
        ) as response:
            body = await response.text()
            status = int(response.status)

            if status in {412, 429}:
                raise BilibiliProtectionError(
                    f"Bilibili HTTP {status}，触发访问保护",
                    http_status=status,
                )

            if status >= 400:
                raise BilibiliApiError(
                    f"Bilibili HTTP {status}",
                    http_status=status,
                )

            try:
                data = await response.json(content_type=None)
            except Exception as exc:
                raise BilibiliApiError(
                    f"Bilibili returned non-JSON: {body[:120]}"
                ) from exc

    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise

    code = data.get("code", 0)
    if code in {-412, -429}:
        raise BilibiliProtectionError(
            f"Bilibili API error {code}: "
            f"{data.get('message') or data.get('msg') or '访问保护'}",
            code=code,
        )

    if code != 0:
        raise BilibiliApiError(
            f"Bilibili API error {code}: "
            f"{data.get('message') or data.get('msg') or 'unknown'}",
            code=code,
        )

    return data


async def _view_video(session, bvid=None, aid=None):
    if bvid:
        params = {"bvid": bvid}
    elif aid:
        params = {"aid": int(aid)}
    else:
        return None

    data = await _get_json(session, VIEW_URL, params)
    video = data.get("data") or {}
    found_bvid = str(video.get("bvid") or bvid or "").strip()

    if not found_bvid:
        return None

    stat = video.get("stat") or {}
    return {
        "bvid": found_bvid,
        "url": f"https://www.bilibili.com/video/{found_bvid}",
        "cover_url": _normalize_image_url(video.get("pic")),
        "title": str(video.get("title") or ""),
        "author": str((video.get("owner") or {}).get("name") or ""),
        "view": int(stat.get("view") or 0),
        "like": int(stat.get("like") or 0),
        "favorite": int(stat.get("favorite") or 0),
        "coin": int(stat.get("coin") or 0),
    }


async def _cache_cover(session, bvid, cover_url):
    cover_url = _normalize_image_url(cover_url)
    if not bvid or not cover_url:
        return ""

    COVER_DIR.mkdir(parents=True, exist_ok=True)
    await _limiter.wait()

    try:
        async with session.get(
            cover_url,
            headers={"Referer": "https://www.bilibili.com/"},
            allow_redirects=True,
        ) as response:
            status = int(response.status)

            if status in {412, 429}:
                raise BilibiliProtectionError(
                    f"Bilibili cover HTTP {status}，触发访问保护",
                    http_status=status,
                )

            if status >= 400:
                return ""

            content_type = str(
                response.headers.get("Content-Type") or ""
            ).lower()

            if "png" in content_type:
                suffix = ".png"
            elif "webp" in content_type:
                suffix = ".webp"
            else:
                suffix = ".jpg"

            data = await response.read()
            if len(data) < 1024:
                return ""

            path = COVER_DIR / f"{bvid}{suffix}"
            cached = save_cover(path, data)
            return str(cached) if cached else ""

    except BilibiliProtectionError:
        raise
    except Exception:
        return ""


async def prepare_cover(bvid, cover_url, cover_local=""):
    # 缓存被清理后按需补回，不修改歌曲状态或推荐历史。
    path = Path(cover_local) if cover_local else None
    if path is not None and path.is_file():
        if path.parent.resolve() == COVER_DIR.resolve():
            touch_cover(path)
        return str(path)
    for suffix in ('.jpg', '.jpeg', '.png', '.webp'):
        cached = COVER_DIR / (bvid + suffix)
        if cached.is_file():
            touch_cover(cached)
            return str(cached)
    if not cover_url:
        return ""
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=12),
    ) as session:
        return await _cache_cover(session, bvid, cover_url)


def _level_from_views(views):
    views = int(views or 0)
    if views >= 10_000_000:
        return "myth"
    if views >= 1_000_000:
        return "legend"
    return "hall"


def _query_targets(conn, status_sql, language, limit):
    if limit <= 0:
        return []

    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM songs
            WHERE source IN (
                'vocawiki',
                'vcpedia',
                'vcpedia+vocawiki',
                'vocawiki+vcpedia'
            )
              AND COALESCE(manual_override,0)=0
              AND {status_sql}
              AND catalog_language=?
              AND (
                    COALESCE(TRIM(bvid),'')<>''
                    OR COALESCE(TRIM(bilibili_url),'')<>''
                  )
            ORDER BY
                date(publish_date) DESC,
                id DESC
            LIMIT ?
            """,
            (language, int(limit)),
        ).fetchall()
    ]


def _load_targets(limit, retry=False):
    """
    Normal verification only reads pending rows, so a successful 'matched'
    song is never selected again. Retry mode only reads failed rows.

    Each batch is filled approximately 70% Japanese/foreign + 30% Chinese.
    If one side temporarily lacks enough rows, the other side fills the
    remainder so the requested batch size is still useful.
    """
    conn = get_conn()
    try:
        status_sql = (
            "bilibili_status IN ('error','unmatched')"
            if retry
            else "COALESCE(bilibili_status,'pending')='pending'"
        )

        limit = max(1, int(limit))
        foreign_target = int(round(limit * FOREIGN_VERIFY_RATIO))
        foreign_target = min(max(foreign_target, 0), limit)
        chinese_target = limit - foreign_target

        foreign = _query_targets(
            conn,
            status_sql,
            "foreign",
            foreign_target,
        )
        chinese = _query_targets(
            conn,
            status_sql,
            "zh",
            chinese_target,
        )

        selected_ids = {row["id"] for row in foreign + chinese}
        remaining = limit - len(foreign) - len(chinese)

        # Fill shortage from either side, while excluding rows already selected.
        if remaining > 0:
            placeholders = ",".join("?" for _ in selected_ids)
            exclude_sql = (
                f"AND id NOT IN ({placeholders})"
                if selected_ids
                else ""
            )
            params = list(selected_ids)

            rows = conn.execute(
                f"""
                SELECT *
                FROM songs
                WHERE source IN (
                    'vocawiki',
                    'vcpedia',
                    'vcpedia+vocawiki',
                    'vocawiki+vcpedia'
                )
                  AND COALESCE(manual_override,0)=0
                  AND {status_sql}
                  AND (
                        COALESCE(TRIM(bvid),'')<>''
                        OR COALESCE(TRIM(bilibili_url),'')<>''
                      )
                  {exclude_sql}
                ORDER BY
                    CASE
                        WHEN catalog_language='foreign' THEN 0
                        ELSE 1
                    END,
                    date(publish_date) DESC,
                    id DESC
                LIMIT ?
                """,
                [*params, remaining],
            ).fetchall()

            for row in rows:
                item = dict(row)
                if item.get("catalog_language") == "zh":
                    chinese.append(item)
                else:
                    foreign.append(item)

        # Interleave rather than verifying all 14 foreign then all 6 Chinese.
        result = []
        f = list(foreign)
        z = list(chinese)

        while f or z:
            # 7:3 cadence over each 10-slot block.
            pattern = (
                "foreign", "foreign", "zh",
                "foreign", "foreign", "zh",
                "foreign", "foreign", "zh",
                "foreign",
            )
            for language in pattern:
                bucket = f if language == "foreign" else z
                if bucket:
                    result.append(bucket.pop(0))
                elif f:
                    result.append(f.pop(0))
                elif z:
                    result.append(z.pop(0))

                if len(result) >= limit:
                    return result

        return result
    finally:
        conn.close()


def _mark_error(song_id, error):
    conn = get_conn()
    try:
        now = datetime.now().isoformat()
        conn.execute(
            """
            UPDATE songs
            SET bilibili_status='error',
                bilibili_attempts=COALESCE(bilibili_attempts,0)+1,
                bilibili_last_attempt_at=?,
                bilibili_last_error=?
            WHERE id=?
            """,
            (now, str(error)[:500], int(song_id)),
        )
        conn.commit()
    finally:
        conn.close()


async def _save_verified(session, song, video):
    cover_local = await _cache_cover(
        session,
        video["bvid"],
        video.get("cover_url"),
    )

    now = datetime.now().isoformat()
    level = _level_from_views(video.get("view"))

    status = "matched" if cover_local else "error"
    error = "" if cover_local else "视频已验证，但封面缓存失败"

    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE songs
            SET bvid=?,
                bilibili_url=?,
                cover_url=?,
                cover_local=?,
                bilibili_verified=1,
                bilibili_verified_at=?,
                bilibili_view=?,
                bilibili_like=?,
                bilibili_favorite=?,
                bilibili_coin=?,
                bilibili_status=?,
                bilibili_attempts=COALESCE(bilibili_attempts,0)+1,
                bilibili_last_attempt_at=?,
                bilibili_match_title=?,
                bilibili_match_author=?,
                bilibili_last_error=?,
                catalog_level=?
            WHERE id=?
            """,
            (
                video["bvid"],
                video["url"],
                video.get("cover_url") or "",
                cover_local,
                now,
                int(video.get("view") or 0),
                int(video.get("like") or 0),
                int(video.get("favorite") or 0),
                int(video.get("coin") or 0),
                status,
                now,
                video.get("title") or "",
                video.get("author") or "",
                error,
                level,
                song["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return bool(cover_local)


async def _view_with_backoff(
    session,
    *,
    bvid=None,
    aid=None,
    progress_callback=None,
):
    last_error = None

    for attempt in range(len(NETWORK_BACKOFF) + 1):
        try:
            return await _view_video(
                session,
                bvid=bvid,
                aid=aid,
            )
        except BilibiliProtectionError:
            raise
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            BilibiliApiError,
        ) as exc:
            last_error = exc

            # Non-protection API errors such as "video does not exist" should
            # not be hammered repeatedly.
            if (
                isinstance(exc, BilibiliApiError)
                and exc.code not in (None, -1)
                and exc.http_status not in (None, 500, 502, 503, 504)
            ):
                raise

            if attempt >= len(NETWORK_BACKOFF):
                break

            wait_seconds = NETWORK_BACKOFF[attempt]
            if progress_callback:
                progress_callback({
                    "throttle_state": "network_backoff",
                    "wait_seconds": wait_seconds,
                    "last_error": (
                        f"网络异常，{wait_seconds} 秒后重试：{exc}"
                    ),
                })
            await asyncio.sleep(wait_seconds)

    raise BilibiliApiError(
        f"Bilibili 请求失败：{last_error}"
    )


async def verify_catalog_bilibili(
    limit=20,
    retry=False,
    progress_callback=None,
):
    try:
        limit = min(max(int(limit), 1), 500)
    except (TypeError, ValueError):
        limit = 20

    targets = _load_targets(limit, retry=bool(retry))

    target_foreign = sum(
        1
        for row in targets
        if row.get("catalog_language") != "zh"
    )
    target_chinese = len(targets) - target_foreign

    state = {
        "running": True,
        "target": len(targets),
        "target_foreign": target_foreign,
        "target_chinese": target_chinese,
        "processed": 0,
        "matched": 0,
        "matched_foreign": 0,
        "matched_chinese": 0,
        "errors": 0,
        "current": "",
        "last_error": "",
        "retry": bool(retry),
        "throttle_state": "normal",
        "wait_seconds": REQUEST_INTERVAL,
        "stopped_by_protection": False,
    }

    timeout = aiohttp.ClientTimeout(total=18)
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
        },
    ) as session:
        for index, song in enumerate(targets):
            state["current"] = (
                song.get("title_zh")
                or song.get("title_jp")
                or ""
            )
            state["throttle_state"] = "normal"
            state["wait_seconds"] = REQUEST_INTERVAL

            try:
                bvid = (
                    _extract_bvid(song.get("bvid"))
                    or _extract_bvid(song.get("bilibili_url"))
                )
                aid = None if bvid else _extract_aid(
                    song.get("bilibili_url")
                )

                video = await _view_with_backoff(
                    session,
                    bvid=bvid or None,
                    aid=aid,
                    progress_callback=progress_callback,
                )

                if not video:
                    raise BilibiliApiError(
                        "百科中的 Bilibili 链接无法解析"
                    )

                if await _save_verified(session, song, video):
                    state["matched"] += 1
                    if song.get("catalog_language") == "zh":
                        state["matched_chinese"] += 1
                    else:
                        state["matched_foreign"] += 1
                else:
                    state["errors"] += 1
                    state["last_error"] = "封面缓存失败"

            except BilibiliProtectionError as exc:
                # One conservative wait is useful in case it is a temporary
                # protection response. Do not keep retrying indefinitely.
                state["throttle_state"] = "rate_limited"
                state["wait_seconds"] = RATE_LIMIT_WAIT
                state["last_error"] = str(exc)

                if progress_callback:
                    progress_callback(dict(state))

                await asyncio.sleep(RATE_LIMIT_WAIT)

                # Stop the run after the cooldown. The current row remains
                # pending (or error in retry mode) and can be safely resumed
                # later, so we don't burn through thousands of requests.
                state["stopped_by_protection"] = True
                state["throttle_state"] = "protection_stop"
                state["wait_seconds"] = 0
                break

            except Exception as exc:
                _mark_error(song["id"], exc)
                state["errors"] += 1
                state["last_error"] = str(exc)

            state["processed"] += 1

            if progress_callback:
                progress_callback(dict(state))

            # A short rest every 20 songs keeps long runs from becoming one
            # uninterrupted request stream.
            if (
                (index + 1) % VERIFY_CHUNK_SIZE == 0
                and index + 1 < len(targets)
            ):
                state["throttle_state"] = "chunk_cooldown"
                state["wait_seconds"] = VERIFY_CHUNK_COOLDOWN
                if progress_callback:
                    progress_callback(dict(state))
                await asyncio.sleep(VERIFY_CHUNK_COOLDOWN)

    state["running"] = False
    state["current"] = ""
    state["pools"] = recalculate_pools()

    conn = get_conn()
    try:
        state["pending"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM songs
            WHERE COALESCE(manual_override,0)=0
              AND COALESCE(bilibili_status,'pending')='pending'
            """
        ).fetchone()[0]
        state["pending_foreign"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM songs
            WHERE COALESCE(manual_override,0)=0
              AND COALESCE(bilibili_status,'pending')='pending'
              AND catalog_language='foreign'
            """
        ).fetchone()[0]
        state["pending_chinese"] = conn.execute(
            """
            SELECT COUNT(*)
            FROM songs
            WHERE COALESCE(manual_override,0)=0
              AND COALESCE(bilibili_status,'pending')='pending'
              AND catalog_language='zh'
            """
        ).fetchone()[0]
    finally:
        conn.close()

    set_meta(
        "last_bilibili_verify",
        datetime.now().isoformat(),
    )
    return state


async def prepare_manual_bilibili(bvid="", bilibili_url="", cover_url=""):
    found_bvid = _extract_bvid(bvid) or _extract_bvid(bilibili_url)
    if not found_bvid:
        return {
            "ok": False,
            "error": "人工启用至少需要有效 BV 或 Bilibili 链接。",
        }

    result = {
        "ok": True,
        "bvid": found_bvid,
        "url": f"https://www.bilibili.com/video/{found_bvid}",
        "cover_url": _normalize_image_url(cover_url),
        "cover_local": "",
        "verified": 0,
        "view": 0,
        "like": 0,
        "favorite": 0,
        "coin": 0,
        "title": "",
        "author": "",
        "warning": "",
    }

    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    ) as session:
        try:
            video = await _view_video(session, bvid=found_bvid)
        except Exception as exc:
            video = None
            result["warning"] = (
                "已保存人工锁定；Bilibili 详情暂时无法验证：" + str(exc)
            )

        if video:
            result.update({
                "bvid": video["bvid"],
                "url": video["url"],
                "cover_url": video.get("cover_url") or result["cover_url"],
                "verified": 1,
                "view": video.get("view") or 0,
                "like": video.get("like") or 0,
                "favorite": video.get("favorite") or 0,
                "coin": video.get("coin") or 0,
                "title": video.get("title") or "",
                "author": video.get("author") or "",
            })

        if result["cover_url"]:
            result["cover_local"] = await _cache_cover(
                session,
                result["bvid"],
                result["cover_url"],
            )

    return result
