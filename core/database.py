import os
import gzip
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


PLUGIN_NAME = "astrbot_plugin_daily_vocaloid"
PLUGIN_DIR = Path(
    os.environ.get(
        "DAILY_VOCALOID_PLUGIN_DIR",
        str(Path("/AstrBot/data/plugins") / PLUGIN_NAME),
    )
)
PLUGIN_DATA_DIR = Path(
    os.environ.get(
        "DAILY_VOCALOID_PLUGIN_DATA_DIR",
        str(Path("/AstrBot/data/plugin_data") / PLUGIN_NAME),
    )
)
COVER_DIR = PLUGIN_DATA_DIR / "covers"
LEGACY_VOCADB_DUMP_DIR = PLUGIN_DIR / "source" / "vocadb_dump"

CATALOG_GENERATION = "vocawiki_jp_vcpedia_zh_hall_v1"


def _resolve_db_path():
    env_path = os.environ.get("DAILY_VOCALOID_DB_PATH")
    if env_path:
        return env_path

    persistent_db = PLUGIN_DATA_DIR / "songs.db"
    plugin_db = PLUGIN_DIR / "songs.db"

    if persistent_db.exists():
        return str(persistent_db)
    seed_db = PLUGIN_DIR / "seed" / "songs.db.gz"
    if seed_db.exists():
        persistent_db.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(seed_db, "rb") as source, persistent_db.open("wb") as target:
            shutil.copyfileobj(source, target)
        return str(persistent_db)
    if plugin_db.exists():
        return str(plugin_db)
    return str(persistent_db)


DB_PATH = _resolve_db_path()


SONG_COLUMNS = {
    "title_jp": "TEXT",
    "title_zh": "TEXT",
    "producer": "TEXT",
    "vocal": "TEXT",
    "publish_date": "TEXT",
    "bvid": "TEXT",
    "bilibili_url": "TEXT",
    "cover_url": "TEXT",
    "pool_type": "TEXT DEFAULT 'excluded'",
    "available": "INTEGER DEFAULT 0",
    "source": "TEXT DEFAULT 'manual'",
    "imported_at": "TEXT",
    "cover_local": "TEXT",
    "bilibili_verified": "INTEGER DEFAULT 0",
    "bilibili_verified_at": "TEXT",
    "bilibili_view": "INTEGER DEFAULT 0",
    "bilibili_like": "INTEGER DEFAULT 0",
    "bilibili_favorite": "INTEGER DEFAULT 0",
    "bilibili_coin": "INTEGER DEFAULT 0",
    "bilibili_status": "TEXT DEFAULT 'pending'",
    "bilibili_attempts": "INTEGER DEFAULT 0",
    "bilibili_last_attempt_at": "TEXT",
    "bilibili_match_title": "TEXT",
    "bilibili_match_author": "TEXT",
    "bilibili_last_error": "TEXT",
    "manual_override": "INTEGER DEFAULT 0",
    "catalog_language": "TEXT",
    "catalog_engine": "TEXT",
    "catalog_level": "TEXT DEFAULT 'hall'",
    "catalog_source_page": "TEXT",
    "catalog_source_title": "TEXT",
    "catalog_article_url": "TEXT",
    "catalog_synced_at": "TEXT",
}


RECOMMEND_COLUMNS = """
    id,
    title_jp,
    title_zh,
    producer,
    vocal,
    publish_date,
    bvid,
    bilibili_url,
    cover_url,
    pool_type,
    available,
    source,
    imported_at,
    cover_local,
    bilibili_verified,
    bilibili_verified_at,
    bilibili_view,
    bilibili_like,
    bilibili_favorite,
    bilibili_coin,
    bilibili_status,
    bilibili_attempts,
    bilibili_last_attempt_at,
    bilibili_match_title,
    bilibili_match_author,
    bilibili_last_error,
    manual_override,
    catalog_language,
    catalog_engine,
    catalog_level,
    catalog_source_page,
    catalog_source_title,
    catalog_article_url
"""


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn, table):
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _ensure_song_columns(conn):
    existing = _table_columns(conn, "songs")
    for name, ddl in SONG_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE songs ADD COLUMN {name} {ddl}")


def _wipe_legacy_catalog(conn):
    row = conn.execute(
        "SELECT value FROM plugin_meta WHERE key='catalog_generation'"
    ).fetchone()
    current = row[0] if row else None

    if current == CATALOG_GENERATION:
        return False

    # User requested a completely clean catalogue when switching sources.
    # Important: do NOT delete anything inside the live plugin directory here.
    # AstrBot may watch that directory and reload the plugin while files are
    # being removed, which can make WebUI API requests fail with "Failed to
    # fetch". We clear runtime data here, and ship a separate stopped-service
    # cleanup script for the old 350 MB VocaDB dump.
    conn.execute("DELETE FROM recommend_history")
    conn.execute("DELETE FROM songs")
    conn.execute("DELETE FROM plugin_meta")

    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO plugin_meta(key,value,updated_at)
        VALUES('catalog_generation',?,?)
        """,
        (CATALOG_GENERATION, now),
    )
    conn.execute(
        """
        INSERT INTO plugin_meta(key,value,updated_at)
        VALUES('catalog_reset_at',?,?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO plugin_meta(key,value,updated_at)
        VALUES('legacy_dump_cleanup_required','1',?)
        """,
        (now,),
    )
    conn.commit()

    # Covers are runtime cache data, so clearing them here is safe.
    if COVER_DIR.exists():
        shutil.rmtree(COVER_DIR, ignore_errors=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)

    return True


def init_database():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS songs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_jp TEXT,
            title_zh TEXT,
            producer TEXT,
            vocal TEXT,
            publish_date TEXT,
            bvid TEXT,
            bilibili_url TEXT,
            cover_url TEXT,
            pool_type TEXT DEFAULT 'excluded',
            available INTEGER DEFAULT 0,
            source TEXT DEFAULT 'manual',
            imported_at TEXT,
            cover_local TEXT,
            bilibili_verified INTEGER DEFAULT 0,
            bilibili_verified_at TEXT,
            bilibili_view INTEGER DEFAULT 0,
            bilibili_like INTEGER DEFAULT 0,
            bilibili_favorite INTEGER DEFAULT 0,
            bilibili_coin INTEGER DEFAULT 0,
            bilibili_status TEXT DEFAULT 'pending',
            bilibili_attempts INTEGER DEFAULT 0,
            bilibili_last_attempt_at TEXT,
            bilibili_match_title TEXT,
            bilibili_match_author TEXT,
            bilibili_last_error TEXT,
            manual_override INTEGER DEFAULT 0,
            catalog_language TEXT,
            catalog_engine TEXT,
            catalog_level TEXT DEFAULT 'hall',
            catalog_source_page TEXT,
            catalog_source_title TEXT,
            catalog_article_url TEXT,
            catalog_synced_at TEXT
        )
        """
    )
    _ensure_song_columns(conn)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS recommend_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT,
            song_id INTEGER,
            recommended_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_meta(
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
        """
    )

    _wipe_legacy_catalog(conn)

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_songs_source "
        "ON songs(source)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_songs_bvid "
        "ON songs(bvid)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_songs_language "
        "ON songs(catalog_language)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_songs_level "
        "ON songs(catalog_level)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_songs_pool_available "
        "ON songs(pool_type,available)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_group_time "
        "ON recommend_history(group_id,recommended_at)"
    )

    conn.commit()
    conn.close()
    recalculate_pools()


def get_history(group_id):
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT song_id
            FROM recommend_history
            WHERE group_id=?
              AND datetime(recommended_at) > datetime('now','-7 day')
            """,
            (group_id,),
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def save_history(group_id, song_id):
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO recommend_history(group_id,song_id)
            VALUES(?,?)
            """,
            (group_id, song_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_unavailable(song_id):
    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE songs
            SET available=0,
                pool_type='excluded'
            WHERE id=?
              AND COALESCE(manual_override,0)=0
            """,
            (int(song_id),),
        )
        conn.commit()
    finally:
        conn.close()


def _parse_date(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10])
    except ValueError:
        return None


def _auto_pool(row):
    published = _parse_date(row["publish_date"])
    if published and published >= datetime.now() - timedelta(days=365):
        return "new"

    level = str(row["catalog_level"] or "hall").lower()
    if level in {"legend", "myth"}:
        return "hot"
    return "normal"


def _is_catalog_source_sql():
    return (
        "(source LIKE '%vocawiki%' OR source LIKE '%vcpedia%')"
    )


def recalculate_pools():
    conn = get_conn()
    try:
        conn.execute(
            """
            UPDATE songs
            SET available=0,
                pool_type='excluded'
            WHERE COALESCE(manual_override,0)=0
            """
        )

        # v2.3 admission rule:
        # The curated wiki entry supplies the identity/BV and the cover.
        # Bilibili API patrol is optional and no longer gates recommendation.
        rows = conn.execute(
            """
            SELECT *
            FROM songs
            WHERE (
                    source LIKE '%vocawiki%'
                    OR source LIKE '%vcpedia%'
                  )
              AND COALESCE(manual_override,0)=0
              AND (
                    (
                        COALESCE(TRIM(bvid),'')<>''
                        AND LOWER(COALESCE(TRIM(bilibili_url),'')) LIKE
                            '%bilibili.com/video/bv%'
                    )
                    OR LOWER(COALESCE(TRIM(bilibili_url),'')) LIKE
                        '%bilibili.com/video/av%'
                  )
              AND (
                    COALESCE(TRIM(cover_url),'') LIKE 'http%'
                    OR COALESCE(TRIM(cover_local),'')<>''
                  )
              AND catalog_level IN ('hall','legend','myth')
            """
        ).fetchall()

        for row in rows:
            pool = _auto_pool(row)
            conn.execute(
                """
                UPDATE songs
                SET pool_type=?,
                    available=1
                WHERE id=?
                """,
                (pool, row["id"]),
            )

        conn.commit()

        return {
            pool: conn.execute(
                "SELECT COUNT(*) FROM songs WHERE pool_type=? AND available=1",
                (pool,),
            ).fetchone()[0]
            for pool in ("hot", "normal", "new")
        }
    finally:
        conn.close()


def get_pool(pool_type):
    conn = get_conn()
    try:
        return conn.execute(
            f"""
            SELECT {RECOMMEND_COLUMNS}
            FROM songs
            WHERE pool_type=?
              AND available=1
              AND (
                    (
                        COALESCE(TRIM(bvid),'')<>''
                        AND LOWER(COALESCE(TRIM(bilibili_url),'')) LIKE
                            '%bilibili.com/video/bv%'
                    )
                    OR LOWER(COALESCE(TRIM(bilibili_url),'')) LIKE
                        '%bilibili.com/video/av%'
                  )
              AND (
                    COALESCE(manual_override,0)=1
                    OR (
                        (
                            source LIKE '%vocawiki%'
                            OR source LIKE '%vcpedia%'
                        )
                        AND (
                            COALESCE(TRIM(cover_url),'') LIKE 'http%'
                            OR COALESCE(TRIM(cover_local),'')<>''
                        )
                        AND catalog_level IN ('hall','legend','myth')
                    )
                  )
            ORDER BY
                COALESCE(manual_override,0) DESC,
                bilibili_view DESC,
                id DESC
            """,
            (pool_type,),
        ).fetchall()
    finally:
        conn.close()


def set_meta(pool_type):
    conn = get_conn()
    try:
        return conn.execute(
            f"""
            SELECT {RECOMMEND_COLUMNS}
            FROM songs
            WHERE pool_type=?
              AND available=1
              AND COALESCE(TRIM(bvid),'')<>''
              AND COALESCE(TRIM(bilibili_url),'') LIKE
                  'https://www.bilibili.com/video/BV%'
              AND (
                    COALESCE(manual_override,0)=1
                    OR (
                        source IN ('vocawiki','vcpedia')
                        AND bilibili_status='matched'
                        AND bilibili_verified=1
                        AND COALESCE(TRIM(cover_local),'')<>''
                        AND catalog_level IN ('hall','legend','myth')
                    )
                  )
            ORDER BY
                COALESCE(manual_override,0) DESC,
                bilibili_view DESC,
                id DESC
            """,
            (pool_type,),
        ).fetchall()
    finally:
        conn.close()


def set_meta(key, value):
    conn = get_conn()
    try:
        now = datetime.now().isoformat()
        conn.execute(
            """
            INSERT INTO plugin_meta(key,value,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (str(key), str(value), now),
        )
        conn.commit()
    finally:
        conn.close()


def get_meta(key, default=None):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM plugin_meta WHERE key=?",
            (str(key),),
        ).fetchone()
        return row[0] if row else default
    finally:
        conn.close()
