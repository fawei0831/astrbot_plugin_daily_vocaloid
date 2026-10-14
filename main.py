from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Star, register
from astrbot.api.web import error_response, json_response, request
import astrbot.api.message_components as Comp

import asyncio
import weakref
from astrbot.api import logger
import os
from datetime import datetime

from .core.database import (
    get_conn,
    init_database,
    mark_unavailable,
    recalculate_pools,
    save_history,
)
from .core.recommender import recommend
from .core.bilibili import (
    COVER_DIR,
    prepare_cover,
    prepare_manual_bilibili,
    verify_catalog_bilibili,
)
from .core.catalog import sync_catalog
from .core.cover_cache import prune_cache


PLUGIN_NAME = "astrbot_plugin_daily_vocaloid"
VALID_POOLS = {"hot", "normal", "new", "excluded"}


@register(
    PLUGIN_NAME,
    "JXMc",
    "今日术力口",
    "2.3.6",
)
class DailyVocaloid(Star):

    def __init__(self, context):
        super().__init__(context)
        init_database()
        prune_cache(COVER_DIR)

        context.register_web_api(
            f"/{PLUGIN_NAME}/health",
            self.web_health,
            ["GET"],
            "Daily Vocaloid backend health",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/stats",
            self.web_stats,
            ["GET"],
            "Daily Vocaloid statistics",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/songs",
            self.web_songs,
            ["GET"],
            "List Daily Vocaloid songs",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/songs/<song_id>/update",
            self.web_song_update,
            ["POST"],
            "Update song",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/songs/<song_id>/delete",
            self.web_song_delete,
            ["POST"],
            "Delete song",
        )

        context.register_web_api(
            f"/{PLUGIN_NAME}/catalog/sync",
            self.web_catalog_sync,
            ["POST"],
            "Sync VocaWiki Japanese/foreign and VCPedia Chinese Hall catalog",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/catalog/sync/status",
            self.web_catalog_sync_status,
            ["GET"],
            "Get catalog sync status",
        )

        context.register_web_api(
            f"/{PLUGIN_NAME}/bilibili/verify",
            self.web_bilibili_verify,
            ["POST"],
            "Verify exact catalog Bilibili links",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/bilibili/verify/status",
            self.web_bilibili_verify_status,
            ["GET"],
            "Get Bilibili verification status",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/bilibili/verify/cancel",
            self.web_bilibili_verify_cancel,
            ["POST"],
            "Cancel optional Bilibili patrol",
        )

        # 同一会话串行抽取、发送、记历史，避免并发请求抽到同一首。
        self._recommend_locks = weakref.WeakValueDictionary()

        self._catalog_job = None
        self._catalog_state = {
            "running": False,
            "current_source": "",
            "current_page": "",
            "pages_scanned": 0,
            "records_found": 0,
            "vocawiki_records": 0,
            "vcpedia_records": 0,
            "errors": 0,
            "last_error": "",
        }

        self._bili_job = None
        self._bili_state = {
            "running": False,
            "target": 0,
            "processed": 0,
            "matched": 0,
            "errors": 0,
            "current": "",
            "last_error": "",
        }


    @filter.command("今日术力口")
    async def daily_vocaloid(self, event: AstrMessageEvent):
        await self._recommend_to_event(event, None, "今日术力口")

    @filter.command("今日中术")
    async def daily_chinese(self, event: AstrMessageEvent):
        await self._recommend_to_event(event, "zh", "今日中术")

    @filter.command("今日日术")
    async def daily_japanese(self, event: AstrMessageEvent):
        await self._recommend_to_event(event, "foreign", "今日日术")

    async def _recommend_to_event(self, event, language, command_title):
        # 群聊保留原群ID以兼容旧历史；私聊按发送者ID隔离。
        group_id = str(event.get_group_id() or "")
        session_key = group_id or f"private:{event.get_sender_id()}"
        lock = self._recommend_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._recommend_locks[session_key] = lock
        async with lock:
            await self._send_recommendation(event, session_key, language, command_title)

    async def _send_recommendation(self, event, group_id, language, command_title):
        for _ in range(3):
            song = recommend(group_id, language=language)
            if not song:
                break

            song_id = song["id"]
            bvid = str(song["bvid"] or "")
            bilibili_url = str(song["bilibili_url"] or "")
            cover_local = str(song["cover_local"] or "")
            cover_url = str(song["cover_url"] or "")
            manual_override = int(song["manual_override"] or 0)

            lower_bili_url = bilibili_url.lower()
            link_ok = (
                (
                    bvid.startswith("BV")
                    and "bilibili.com/video/bv" in lower_bili_url
                )
                or "bilibili.com/video/av" in lower_bili_url
            )

            if manual_override:
                if not link_ok:
                    mark_unavailable(song_id)
                    continue
            else:
                trusted_source = (
                    "vocawiki" in str(song["source"] or "")
                    or "vcpedia" in str(song["source"] or "")
                )
                has_cover = (
                    (cover_local and os.path.isfile(cover_local))
                    or cover_url.startswith(("http://", "https://"))
                )
                if (
                    not trusted_source
                    or not link_ok
                    or not has_cover
                    or song["catalog_level"] not in (
                        "hall", "legend", "myth"
                    )
                ):
                    mark_unavailable(song_id)
                    continue

            title = song["title_zh"] or song["title_jp"] or "未知标题"
            producer = song["producer"] or "未知"
            engine = song["catalog_engine"] or song["vocal"] or "歌声合成"

            level_text = {
                "hall": "🏛️ 殿堂曲",
                "legend": "🔥 传说曲",
                "myth": "🌟 神话曲",
            }.get(song["catalog_level"], "🏛️ 殿堂曲")

            language_text = (
                "中文"
                if song["catalog_language"] == "zh"
                else "日语 / 外语"
            )

            pool_text = {
                "hot": "🔥 热门推荐",
                "normal": "🎵 殿堂推荐",
                "new": "🆕 新近殿堂",
            }.get(song["pool_type"], "🎵 推荐")

            msg = (
                f"🎵 {command_title}\n\n"
                f"《{title}》\n"
                f"P主：{producer}\n"
                f"引擎：{engine}\n"
                f"投稿：{song['publish_date'] or '未知'}\n"
                f"{level_text} · {language_text}\n"
                f"{pool_text}\n\n"
                f"▶ Bilibili：{bilibili_url}\n\n"
                "🎧 今天就听这个吧。"
            )

            # 直接等待平台发送完成，成功后才写入七天历史。
            try:
                try:
                    cover_local = await prepare_cover(bvid, cover_url, cover_local)
                except Exception:
                    logger.exception("今日术力口封面缓存失败，使用原封面地址")
                if cover_local and os.path.isfile(cover_local):
                    image = Comp.Image.fromFileSystem(cover_local)
                elif cover_url.startswith(("http://", "https://")):
                    image = Comp.Image.fromURL(cover_url)
                else:
                    image = None

                if image is not None:
                    try:
                        await event.send(event.chain_result([image, Comp.Plain(msg)]))
                    except Exception:
                        logger.exception("今日术力口封面发送失败，降级文字")
                        await event.send(event.plain_result(msg))
                else:
                    await event.send(event.plain_result(msg))
            except Exception:
                logger.exception("今日术力口发送失败，不记录推荐历史")
                return

            save_history(group_id, song_id)
            return

        await event.send(event.plain_result(
            f"🎵 {command_title}暂时没有可推荐的新歌曲。"
            "符合条件的歌曲可能已在最近7天推荐过，请稍后再试。"
        ))

    @filter.command("同步术力口")
    async def sync_command(self, event: AstrMessageEvent):
        yield event.plain_result(
            "🎵 当前自动曲库：VocaWiki 日语/外语殿堂 + "
            "VCPedia 中文殿堂。最低门槛为殿堂曲。"
            "请在管理页同步曲库。"
        )

    async def web_health(self):
        return json_response({
            "ok": True,
            "plugin": PLUGIN_NAME,
            "version": "2.3.3",
        })

    async def web_stats(self):
        conn = get_conn()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM songs"
            ).fetchone()[0]
            foreign = conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE catalog_language='foreign'
                """
            ).fetchone()[0]
            chinese = conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE catalog_language='zh'
                """
            ).fetchone()[0]
            available = conn.execute(
                "SELECT COUNT(*) FROM songs WHERE available=1"
            ).fetchone()[0]
            with_cover = conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE (
                        COALESCE(TRIM(cover_url),'') LIKE 'http%'
                        OR COALESCE(TRIM(cover_local),'')<>''
                      )
                """
            ).fetchone()[0]
            missing_cover = conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE (
                        COALESCE(TRIM(cover_url),'')=''
                        AND COALESCE(TRIM(cover_local),'')=''
                      )
                """
            ).fetchone()[0]
            verified = conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE bilibili_status='matched'
                  AND bilibili_verified=1
                  AND COALESCE(TRIM(cover_local),'')<>''
                """
            ).fetchone()[0]
            pending = conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE COALESCE(bilibili_status,'pending')='pending'
                """
            ).fetchone()[0]
            verified_foreign = conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE bilibili_status='matched'
                  AND bilibili_verified=1
                  AND COALESCE(TRIM(cover_local),'')<>''
                  AND catalog_language='foreign'
                """
            ).fetchone()[0]
            verified_chinese = conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE bilibili_status='matched'
                  AND bilibili_verified=1
                  AND COALESCE(TRIM(cover_local),'')<>''
                  AND catalog_language='zh'
                """
            ).fetchone()[0]
            pending_foreign = conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE COALESCE(bilibili_status,'pending')='pending'
                  AND catalog_language='foreign'
                """
            ).fetchone()[0]
            pending_chinese = conn.execute(
                """
                SELECT COUNT(*)
                FROM songs
                WHERE COALESCE(bilibili_status,'pending')='pending'
                  AND catalog_language='zh'
                """
            ).fetchone()[0]
            errors = conn.execute(
                "SELECT COUNT(*) FROM songs WHERE bilibili_status='error'"
            ).fetchone()[0]

            levels = {
                level: conn.execute(
                    "SELECT COUNT(*) FROM songs WHERE catalog_level=?",
                    (level,),
                ).fetchone()[0]
                for level in ("hall", "legend", "myth")
            }

            return json_response({
                "total": total,
                "foreign": foreign,
                "chinese": chinese,
                "available": available,
                "with_cover": with_cover,
                "missing_cover": missing_cover,
                "verified": verified,
                "verified_foreign": verified_foreign,
                "verified_chinese": verified_chinese,
                "pending": pending,
                "pending_foreign": pending_foreign,
                "pending_chinese": pending_chinese,
                "errors": errors,
                "levels": levels,
            })
        finally:
            conn.close()

    async def web_songs(self):
        q = (request.query.get("q") or "").strip()
        status = (request.query.get("status") or "").strip()
        language = (request.query.get("language") or "").strip()
        level = (request.query.get("level") or "").strip()
        source = (request.query.get("source") or "").strip()

        limit = request.query.get("limit", 50, type=int)
        offset = request.query.get("offset", 0, type=int)
        limit = min(max(limit or 50, 1), 200)
        offset = max(offset or 0, 0)

        where = ["1=1"]
        params = []

        if q:
            like = f"%{q}%"
            where.append(
                """
                (
                    title_jp LIKE ? OR title_zh LIKE ? OR
                    producer LIKE ? OR vocal LIKE ? OR
                    bvid LIKE ? OR catalog_engine LIKE ?
                )
                """
            )
            params.extend([like] * 6)

        if language:
            if language not in {"foreign", "zh"}:
                return error_response("invalid language", status_code=400)
            where.append("catalog_language=?")
            params.append(language)

        if level:
            if level not in {"hall", "legend", "myth"}:
                return error_response("invalid level", status_code=400)
            where.append("catalog_level=?")
            params.append(level)

        if source:
            if source not in {"vocawiki", "vcpedia"}:
                return error_response("invalid source", status_code=400)
            where.append("source LIKE ?")
            params.append(f"%{source}%")

        status_filters = {
            "enabled": "available=1",
            "cover": (
                "(COALESCE(TRIM(cover_url),'') LIKE 'http%' "
                "OR COALESCE(TRIM(cover_local),'')<>'')"
            ),
            "verified": (
                "bilibili_status='matched' AND bilibili_verified=1"
            ),
            "pending": "COALESCE(bilibili_status,'pending')='pending'",
            "error": "bilibili_status='error'",
            "manual": "COALESCE(manual_override,0)=1",
        }
        if status:
            clause = status_filters.get(status)
            if not clause:
                return error_response("invalid status", status_code=400)
            where.append(clause)

        where_sql = "WHERE " + " AND ".join(where)

        conn = get_conn()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM songs {where_sql}",
                params,
            ).fetchone()[0]

            rows = conn.execute(
                f"""
                SELECT *
                FROM songs
                {where_sql}
                ORDER BY
                    available DESC,
                    CASE catalog_level
                        WHEN 'myth' THEN 0
                        WHEN 'legend' THEN 1
                        ELSE 2
                    END,
                    CASE catalog_language
                        WHEN 'zh' THEN 0
                        ELSE 1
                    END,
                    bilibili_view DESC,
                    date(publish_date) DESC,
                    id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()

            return json_response({
                "items": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            })
        finally:
            conn.close()

    async def web_catalog_sync(self):
        if self._catalog_state.get("running"):
            return error_response(
                "catalog sync is already running",
                status_code=409,
            )

        self._catalog_state = {
            "running": True,
            "current_source": "",
            "current_page": "",
            "pages_scanned": 0,
            "records_found": 0,
            "vocawiki_records": 0,
            "vcpedia_records": 0,
            "errors": 0,
            "last_error": "",
        }

        def on_progress(info):
            self._catalog_state.update(info)
            self._catalog_state["running"] = True

        async def runner():
            try:
                result = await sync_catalog(
                    progress_callback=on_progress
                )
                self._catalog_state.update(result)
            except Exception as exc:
                self._catalog_state["errors"] = (
                    int(self._catalog_state.get("errors") or 0) + 1
                )
                self._catalog_state["last_error"] = str(exc)
            finally:
                self._catalog_state["running"] = False
                self._catalog_state["current_page"] = ""

        self._catalog_job = asyncio.create_task(runner())
        return json_response({"started": True})

    async def web_catalog_sync_status(self):
        return json_response(dict(self._catalog_state))

    async def web_bilibili_verify(self):
        if self._bili_state.get("running"):
            return error_response(
                "Bilibili verification is already running",
                status_code=409,
            )

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("invalid payload", status_code=400)

        try:
            limit = int(payload.get("limit", 20))
        except (TypeError, ValueError):
            return error_response("invalid limit", status_code=400)

        if limit < 1 or limit > 500:
            return error_response(
                "limit must be between 1 and 500",
                status_code=400,
            )

        retry = bool(payload.get("retry", False))

        self._bili_state = {
            "running": True,
            "target": limit,
            "processed": 0,
            "matched": 0,
            "errors": 0,
            "current": "",
            "last_error": "",
            "retry": retry,
        }

        def on_progress(info):
            self._bili_state.update(info)
            self._bili_state["running"] = True

        async def runner():
            try:
                result = await verify_catalog_bilibili(
                    limit=limit,
                    retry=retry,
                    progress_callback=on_progress,
                )
                self._bili_state.update(result)
            except asyncio.CancelledError:
                self._bili_state["cancelled"] = True
                self._bili_state["last_error"] = ""
            except Exception as exc:
                self._bili_state["errors"] = (
                    int(self._bili_state.get("errors") or 0) + 1
                )
                self._bili_state["last_error"] = str(exc)
            finally:
                self._bili_state["running"] = False
                self._bili_state["current"] = ""

        self._bili_job = asyncio.create_task(runner())
        return json_response({
            "started": True,
            "limit": limit,
            "retry": retry,
        })

    async def web_bilibili_verify_status(self):
        return json_response(dict(self._bili_state))

    async def web_bilibili_verify_cancel(self):
        if self._bili_job and not self._bili_job.done():
            self._bili_job.cancel()
            self._bili_state["cancelled"] = True
            self._bili_state["running"] = False
            self._bili_state["current"] = ""
            return json_response({
                "cancelled": True,
                "processed": int(
                    self._bili_state.get("processed") or 0
                ),
            })

        return json_response({
            "cancelled": False,
            "message": "当前没有正在运行的 Bilibili 巡检。",
        })

    async def web_song_update(self, song_id: str):
        try:
            song_id_int = int(song_id)
        except (TypeError, ValueError):
            return error_response("invalid song id", status_code=400)

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("invalid payload", status_code=400)

        conn = get_conn()
        try:
            current = conn.execute(
                "SELECT * FROM songs WHERE id=?",
                (song_id_int,),
            ).fetchone()
        finally:
            conn.close()

        if not current:
            return error_response("song not found", status_code=404)

        allowed = {
            "title_jp","title_zh","producer","vocal","publish_date",
            "bvid","bilibili_url","cover_url","pool_type",
            "available","manual_override",
        }
        updates = {
            k: v for k, v in payload.items()
            if k in allowed
        }

        if "pool_type" in updates and updates["pool_type"] not in VALID_POOLS:
            return error_response("invalid pool_type", status_code=400)

        for key in ("available", "manual_override"):
            if key in updates:
                value = updates[key]
                if isinstance(value, bool):
                    updates[key] = 1 if value else 0
                elif value in (0,1,"0","1"):
                    updates[key] = int(value)
                else:
                    return error_response(
                        f"{key} must be 0 or 1",
                        status_code=400,
                    )

        manual = int(
            updates.get("manual_override", current["manual_override"] or 0)
        )
        desired_available = int(
            updates.get("available", current["available"] or 0)
        )
        desired_pool = str(
            updates.get("pool_type", current["pool_type"] or "normal")
        )

        manual_info = None

        if manual and desired_available:
            if desired_pool == "excluded":
                return error_response(
                    "人工启用不能选择排除。",
                    status_code=400,
                )

            manual_info = await prepare_manual_bilibili(
                bvid=updates.get("bvid", current["bvid"] or ""),
                bilibili_url=updates.get(
                    "bilibili_url", current["bilibili_url"] or ""
                ),
                cover_url=updates.get(
                    "cover_url", current["cover_url"] or ""
                ),
            )

            if not manual_info.get("ok"):
                return error_response(
                    manual_info.get("error") or "人工启用失败",
                    status_code=400,
                )

            updates.update({
                "bvid": manual_info["bvid"],
                "bilibili_url": manual_info["url"],
                "cover_url": manual_info.get("cover_url") or "",
                "cover_local": manual_info.get("cover_local") or "",
                "bilibili_verified": int(
                    manual_info.get("verified") or 0
                ),
                "bilibili_verified_at": (
                    datetime.now().isoformat()
                    if manual_info.get("verified")
                    else current["bilibili_verified_at"]
                ),
                "bilibili_view": int(manual_info.get("view") or 0),
                "bilibili_like": int(manual_info.get("like") or 0),
                "bilibili_favorite": int(
                    manual_info.get("favorite") or 0
                ),
                "bilibili_coin": int(manual_info.get("coin") or 0),
                "bilibili_status": "matched",
                "manual_override": 1,
                "available": 1,
                "pool_type": desired_pool,
            })

        elif manual:
            updates["manual_override"] = 1
            updates["available"] = 0

        else:
            updates["manual_override"] = 0
            updates.pop("available", None)
            updates.pop("pool_type", None)

        assignments = ", ".join(f"{k}=?" for k in updates)
        params = list(updates.values()) + [song_id_int]

        conn = get_conn()
        try:
            conn.execute(
                f"UPDATE songs SET {assignments} WHERE id=?",
                params,
            )
            conn.commit()
        finally:
            conn.close()

        recalculate_pools()

        conn = get_conn()
        try:
            saved = conn.execute(
                """
                SELECT id,available,pool_type,manual_override,bvid,
                       cover_local,bilibili_verified
                FROM songs
                WHERE id=?
                """,
                (song_id_int,),
            ).fetchone()
        finally:
            conn.close()

        result = {
            "saved": True,
            "id": song_id_int,
            "available": int(saved["available"] or 0),
            "pool_type": saved["pool_type"],
            "manual_override": int(saved["manual_override"] or 0),
            "bvid": saved["bvid"] or "",
            "cover_cached": bool(saved["cover_local"]),
            "bilibili_verified": int(saved["bilibili_verified"] or 0),
        }

        if manual_info and manual_info.get("warning"):
            result["warning"] = manual_info["warning"]

        return json_response(result)

    async def web_song_delete(self, song_id: str):
        try:
            song_id_int = int(song_id)
        except (TypeError, ValueError):
            return error_response("invalid song id", status_code=400)

        conn = get_conn()
        try:
            conn.execute(
                "DELETE FROM recommend_history WHERE song_id=?",
                (song_id_int,),
            )
            cur = conn.execute(
                "DELETE FROM songs WHERE id=?",
                (song_id_int,),
            )
            if cur.rowcount == 0:
                return error_response("song not found", status_code=404)
            conn.commit()
        finally:
            conn.close()

        return json_response({
            "deleted": True,
            "id": song_id_int,
        })
