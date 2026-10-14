import json
import os
import re
from datetime import datetime
from pathlib import Path

from .database import get_conn, init_database, set_meta, recalculate_pools


PLUGIN_NAME = "astrbot_plugin_daily_vocaloid"

# Your VocaDB dump is inside the plugin source tree, not plugin_data.
DUMP_ROOT = Path("/AstrBot/data/plugins") / PLUGIN_NAME / "source" / "vocadb_dump"
SONGS_DIR = DUMP_ROOT / "Songs"


VOICE_HINTS = (
    "初音ミク",
    "鏡音リン",
    "鏡音レン",
    "巡音ルカ",
    "MEIKO",
    "KAITO",
    "GUMI",
    "神威がくぽ",
    "IA",
    "ONE",
    "可不",
    "星界",
    "裏命",
    "狐子",
    "羽累",
    "重音テト",
    "歌愛ユキ",
    "flower",
    "v flower",
    "音街ウナ",
    "結月ゆかり",
    "紲星あかり",
    "東北きりたん",
    "ずんだもん",
    "Synthesizer V",
    "CeVIO",
    "Vocaloid",
    "UTAU",
)


def ensure_import_schema():
    init_database()
    set_meta("vocadb_dump_path", str(DUMP_ROOT))
    return True


def _safe_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _first_name(names, languages):
    if not isinstance(names, list):
        return ""

    wanted = {x.lower() for x in languages}
    for entry in names:
        if not isinstance(entry, dict):
            continue
        language = str(entry.get("language") or "").lower()
        value = str(entry.get("value") or "").strip()
        if value and language in wanted:
            return value
    return ""


def _extract_titles(item):
    names = item.get("names") or item.get("Names") or []

    jp = _first_name(names, ("Japanese",))
    zh = _first_name(
        names,
        (
            "ChineseSimplified",
            "ChineseTraditional",
            "Chinese",
        ),
    )
    en = _first_name(names, ("English", "Romaji", "Romanized"))

    fallback = str(
        item.get("defaultName")
        or item.get("name")
        or ""
    ).strip()

    title_jp = jp or fallback or en
    # Do not invent a Chinese translation. Use an existing localized/English name as fallback.
    title_zh = zh or en or fallback or title_jp

    return title_jp, title_zh


def _artist_name(artist):
    if not isinstance(artist, dict):
        return ""

    name = (
        artist.get("nameHint")
        or artist.get("name")
        or artist.get("defaultName")
    )

    if not name and isinstance(artist.get("artist"), dict):
        nested = artist["artist"]
        name = nested.get("name") or nested.get("defaultName")

    return str(name or "").strip()


def _looks_like_vocal(artist, name):
    text_parts = [
        str(artist.get("categories") or ""),
        str(artist.get("artistType") or ""),
        str(artist.get("roles") or ""),
        str(artist.get("role") or ""),
    ]
    text = " ".join(text_parts).lower()

    role_markers = (
        "vocalist",
        "vocaloid",
        "voicebank",
        "synthesizerv",
        "synthesizer v",
        "cevio",
        "utau",
        "voice synth",
    )
    if any(marker in text for marker in role_markers):
        return True

    name_lower = name.lower()
    return any(hint.lower() in name_lower for hint in VOICE_HINTS)


def _extract_artists(item):
    artists = item.get("artists") or item.get("Artists") or []
    producers = []
    vocals = []

    for artist in artists:
        if not isinstance(artist, dict):
            continue

        name = _artist_name(artist)
        if not name:
            continue

        if _looks_like_vocal(artist, name):
            if name not in vocals:
                vocals.append(name)
        else:
            if name not in producers:
                producers.append(name)

    if not producers:
        artist_string = str(item.get("artistString") or "").strip()
        if artist_string:
            producers.append(artist_string)

    return " / ".join(producers), " / ".join(vocals)


def _extract_bilibili(item):
    pvs = item.get("pvs") or item.get("PVs") or []

    for pv in pvs:
        if not isinstance(pv, dict):
            continue

        service = str(pv.get("service") or "").lower()
        if "bilibili" not in service:
            continue

        pv_id = str(
            pv.get("pvId")
            or pv.get("id")
            or pv.get("url")
            or ""
        ).strip()

        url = str(pv.get("url") or "").strip()
        bvid = ""

        match = re.search(r"(BV[0-9A-Za-z]+)", pv_id or url)
        if match:
            bvid = match.group(1)

        if not url:
            if bvid:
                url = f"https://www.bilibili.com/video/{bvid}"
            elif pv_id:
                if pv_id.startswith(("http://", "https://")):
                    url = pv_id
                else:
                    url = f"https://www.bilibili.com/video/{pv_id}"

        cover = str(
            pv.get("thumbUrl")
            or pv.get("thumbnail")
            or pv.get("imageUrl")
            or ""
        ).strip()

        return bvid, url, cover

    return "", "", ""



def _number_from(item, *keys):
    if not isinstance(item, dict):
        return 0.0

    for key in keys:
        if key in item:
            value = _safe_float(item.get(key))
            if value:
                return value

    for parent_key in ("stats", "Stats", "statistics", "Statistics"):
        nested = item.get(parent_key)
        if isinstance(nested, dict):
            for key in keys:
                if key in nested:
                    value = _safe_float(nested.get(key))
                    if value:
                        return value

    return 0.0


def _is_original_song(item):
    song_type = str(
        item.get("songType")
        or item.get("SongType")
        or ""
    ).strip()

    if song_type and song_type.lower() != "original":
        return False

    status = str(
        item.get("status")
        or item.get("Status")
        or ""
    ).strip().lower()

    if status == "draft":
        return False

    return True


def _normalize_song(item):
    if not isinstance(item, dict):
        return None

    if not _is_original_song(item):
        return None

    vocadb_id = (
        item.get("id")
        or item.get("Id")
        or item.get("vocadb_id")
    )
    if vocadb_id is None:
        return None

    title_jp, title_zh = _extract_titles(item)
    if not title_jp:
        return None

    producer, vocal = _extract_artists(item)
    if not vocal:
        return None

    bvid, bilibili_url, cover_url = _extract_bilibili(item)

    publish_date = str(
        item.get("publishDate")
        or item.get("PublishDate")
        or item.get("publish_date")
        or ""
    ).strip()
    if "T" in publish_date:
        publish_date = publish_date.split("T", 1)[0]

    rating_score = _number_from(
        item,
        "ratingScore",
        "RatingScore",
        "rating_score",
    )

    return {
        "vocadb_id": str(vocadb_id),
        "title_jp": title_jp,
        "title_zh": title_zh,
        "producer": producer,
        "vocal": vocal,
        "publish_date": publish_date,
        "bvid": bvid,
        "bilibili_url": bilibili_url,
        "cover_url": cover_url,
        "rating_score": rating_score,
        # Import = local cache only. Formal recommendation needs later
        # Bilibili verification and a successfully cached cover.
        "pool_type": "excluded",
        "available": 0,
        "source": "vocadb",
    }


def _upsert_song(conn, song):
    existing = conn.execute(
        "SELECT id FROM songs WHERE vocadb_id=? ORDER BY id LIMIT 1",
        (song["vocadb_id"],),
    ).fetchone()

    now = datetime.now().isoformat()

    if existing:
        conn.execute(
            """
            UPDATE songs
            SET title_jp=?,
                title_zh=?,
                producer=?,
                vocal=?,
                publish_date=?,
                rating_score=?,
                last_checked_at=?,
                source=?,
                imported_at=?
            WHERE id=?
            """,
            (
                song["title_jp"],
                song["title_zh"],
                song["producer"],
                song["vocal"],
                song["publish_date"],
                song["rating_score"],
                now,
                song["source"],
                now,
                existing[0],
            ),
        )
        return "updated"

    conn.execute(
        """
        INSERT INTO songs(
            vocadb_id,
            title_jp,
            title_zh,
            producer,
            vocal,
            publish_date,
            bvid,
            bilibili_url,
            cover_url,
            rating_score,
            pool_type,
            available,
            last_checked_at,
            source,
            imported_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            song["vocadb_id"],
            song["title_jp"],
            song["title_zh"],
            song["producer"],
            song["vocal"],
            song["publish_date"],
            song["bvid"],
            song["bilibili_url"],
            song["cover_url"],
            song["rating_score"],
            song["pool_type"],
            song["available"],
            now,
            song["source"],
            now,
        ),
    )
    return "inserted"


def _recalculate_pools(conn=None):
    return recalculate_pools()


def _import_items(items, recalc=True):
    ensure_import_schema()

    inserted = 0
    updated = 0
    skipped = 0

    conn = get_conn()
    try:
        for item in items:
            song = _normalize_song(item)
            if not song:
                skipped += 1
                continue

            action = _upsert_song(conn, song)
            if action == "inserted":
                inserted += 1
            else:
                updated += 1

        conn.commit()
    finally:
        conn.close()

    count = inserted + updated
    now = datetime.now().isoformat()

    set_meta("last_vocadb_sync", now)
    set_meta("last_vocadb_import_count", count)
    if recalc:
        recalculate_pools()

    return {
        "status": True,
        "count": count,
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "last_sync": now,
        "source": "vocadb",
    }


def _iter_song_objects(payload):
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return

    if not isinstance(payload, dict):
        return

    if isinstance(payload.get("items"), list):
        yield from _iter_song_objects(payload["items"])
        return

    if isinstance(payload.get("songs"), list):
        yield from _iter_song_objects(payload["songs"])
        return

    # A single VocaDB song object.
    if "id" in payload and (
        "names" in payload
        or "defaultName" in payload
        or "name" in payload
    ):
        yield payload
        return

    # Some dump shapes are dictionaries keyed by ID.
    for value in payload.values():
        if isinstance(value, dict) and "id" in value:
            yield value


def _file_sort_key(path):
    stem = path.stem
    try:
        return (0, int(stem))
    except ValueError:
        return (1, stem.lower())


def _load_json_file(path):
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        # Fallback for JSON-lines-like dumps.
        items = []
        with path.open("r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return items


def import_vocadb_dump(limit=500):
    """
    Import the NEXT unseen VocaDB songs.

    Old behavior restarted from Songs/0.json every time, so repeated syncs only
    updated the same first N rows. This version skips cached vocadb_id values and
    keeps scanning until it inserts the requested number of new valid songs.
    """
    ensure_import_schema()

    try:
        limit = int(limit) if limit not in (None, "", "all") else 0
    except (TypeError, ValueError):
        limit = 500

    if not SONGS_DIR.is_dir():
        return {
            "status": False,
            "count": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "msg": f"VocaDB Songs目录不存在: {SONGS_DIR}",
            "dump_path": str(DUMP_ROOT),
        }

    files = sorted(SONGS_DIR.glob("*.json"), key=_file_sort_key)
    if not files:
        return {
            "status": False,
            "count": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "msg": f"VocaDB Songs目录中没有JSON文件: {SONGS_DIR}",
            "dump_path": str(DUMP_ROOT),
        }

    conn = get_conn()
    try:
        existing_ids = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT vocadb_id
                FROM songs
                WHERE source='vocadb'
                  AND vocadb_id IS NOT NULL
                  AND TRIM(vocadb_id) <> ''
                """
            ).fetchall()
        }

        inserted = 0
        skipped = 0
        scanned = 0
        already_cached = 0
        exhausted = True

        for path in files:
            payload = _load_json_file(path)

            for item in _iter_song_objects(payload):
                scanned += 1

                raw_id = (
                    item.get("id")
                    or item.get("Id")
                    or item.get("vocadb_id")
                )
                if raw_id is not None and str(raw_id) in existing_ids:
                    already_cached += 1
                    continue

                song = _normalize_song(item)
                if not song:
                    skipped += 1
                    continue

                action = _upsert_song(conn, song)
                existing_ids.add(song["vocadb_id"])

                if action == "inserted":
                    inserted += 1

                if inserted and inserted % 200 == 0:
                    conn.commit()

                if limit > 0 and inserted >= limit:
                    exhausted = False
                    break

            if limit > 0 and inserted >= limit:
                break

        conn.commit()
    finally:
        conn.close()

    now = datetime.now().isoformat()
    set_meta("last_vocadb_sync", now)
    set_meta("last_vocadb_import_count", inserted)
    set_meta("vocadb_dump_path", str(DUMP_ROOT))
    recalculate_pools()

    return {
        "status": True,
        "count": inserted,
        "inserted": inserted,
        "updated": 0,
        "skipped": skipped,
        "scanned": scanned,
        "already_cached": already_cached,
        "exhausted": exhausted,
        "last_sync": now,
        "source": "vocadb",
        "dump_path": str(DUMP_ROOT),
    }


def import_vocadb_items(items=None, limit=None):
    """
    Compatibility entry used by the existing main.py WebUI endpoint.

    - If browser-fetched VocaDB items are supplied, import those.
    - If the payload is empty (or only contains a limit), read the local dump.
      This lets the existing WebUI endpoint move away from Cloudflare/API access
      without forcing a main.py rewrite.
    """
    if items is None:
        return import_vocadb_dump(limit or 500)

    if isinstance(items, dict):
        requested_limit = items.get("limit") or items.get("count") or limit

        if isinstance(items.get("items"), list):
            actual_items = items["items"]
            if actual_items:
                return _import_items(actual_items)
            return import_vocadb_dump(requested_limit or 500)

        # If this is a direct song object, import it.
        if "id" in items:
            return _import_items([items])

        # Payload with settings/limit but no data -> local dump.
        return import_vocadb_dump(requested_limit or 500)

    if isinstance(items, list):
        if items:
            return _import_items(items)
        return import_vocadb_dump(limit or 500)

    return import_vocadb_dump(limit or 500)
