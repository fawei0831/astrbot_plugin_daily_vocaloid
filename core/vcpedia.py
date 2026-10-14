import asyncio
import html
import re
from collections import deque
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import (
    parse_qs,
    quote,
    unquote,
    urljoin,
    urlparse,
    urlunparse,
)

import aiohttp

from .database import get_conn, recalculate_pools, set_meta


BASE_URL = "https://vcpedia.cn/"
INDEX_URL = urljoin(BASE_URL, quote("殿堂曲"))
USER_AGENT = (
    "Mozilla/5.0 (compatible; AstrBot-DailyVocaloid/2.0; "
    "+VCPedia Hall-of-Fame cache)"
)

MAX_PAGES = 220
REQUEST_INTERVAL_SECONDS = 0.16

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
    "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "ul",
}

CHINESE_DEFAULT_ENGINES = {
    "X Studio",
    "Sharpkey",
    "MUTA",
    "DeepVocal",
    "DiffSinger",
    "VocalSharp",
    "袅袅虚拟歌手",
}

EXPLICIT_SEEDS = (
    "VOCALOID中文殿堂曲",
    "VOCALOID殿堂曲/bilibili投稿",
    "Synthesizer V中文殿堂曲",
    "Synthesizer V外文殿堂曲",
    "ACE中文殿堂曲",
    "ACE外文殿堂曲",
    "UTAU中文殿堂曲",
    "UTAU外文殿堂曲",
    "X Studio殿堂曲",
    "Sharpkey殿堂曲",
    "MUTA殿堂曲",
    "DeepVocal殿堂曲",
    "DiffSinger殿堂曲",
    "VocalSharp殿堂曲",
    "袅袅虚拟歌手殿堂曲",
)


class VcpediaError(RuntimeError):
    pass


def _clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _extract_bvid(value):
    match = re.search(r"(BV[0-9A-Za-z]{8,})", str(value or ""), re.I)
    return match.group(1) if match else ""


def _extract_aid(value):
    match = re.search(r"(?:^|[/\s])av(\d+)(?:$|[/?#\s])", str(value or ""), re.I)
    if not match:
        match = re.search(r"[?&]aid=(\d+)", str(value or ""), re.I)
    if not match:
        return ""
    return match.group(1)


def _video_key(url):
    bvid = _extract_bvid(url)
    if bvid:
        return f"bv:{bvid.lower()}"
    aid = _extract_aid(url)
    if aid:
        return f"av:{aid}"
    return str(url or "").strip().lower()


def _canonical_bilibili(url):
    bvid = _extract_bvid(url)
    if bvid:
        return bvid, f"https://www.bilibili.com/video/{bvid}"
    return "", str(url or "").strip()


def _page_title_from_url(url):
    parsed = urlparse(url)
    path = unquote(parsed.path)

    for prefix in (
        "/zh-cn/", "/zh-hans/", "/zh-hant/", "/zh-tw/", "/zh-hk/",
    ):
        if path.startswith(prefix):
            path = "/" + path[len(prefix):]
            break

    if path.startswith("/index.php"):
        title = parse_qs(parsed.query).get("title", [""])[0]
        return unquote(title).strip()

    return path.lstrip("/").strip()


def _canonical_vcp_url(href, base_url=BASE_URL):
    if not href:
        return ""

    absolute = urljoin(base_url, html.unescape(href))
    parsed = urlparse(absolute)

    if parsed.scheme not in {"http", "https"}:
        return ""
    if parsed.netloc.lower() not in {"vcpedia.cn", "www.vcpedia.cn"}:
        return ""

    title = _page_title_from_url(absolute)
    if not title:
        return ""

    if any(
        bad in title
        for bad in (
            "Special:", "特殊:", "Template:", "模板:",
            "Category:", "分类:", "Talk:", "讨论:",
        )
    ):
        return ""

    # Skip non-Bilibili mirror branches to keep the source focused.
    lower_title = title.lower()
    if "niconico" in lower_title or "youtube" in lower_title:
        return ""

    if "殿堂曲" not in title:
        return ""

    return urljoin(BASE_URL, quote(title, safe="/:_()·-. "))


def _engine_and_language(page_title):
    title = str(page_title or "")

    engine = title
    for token in (
        "中文殿堂曲",
        "外文殿堂曲",
        "殿堂曲/bilibili投稿",
        "殿堂曲",
    ):
        if token in engine:
            engine = engine.split(token, 1)[0]
            break
    engine = engine.replace("_", " ").strip(" /")

    if not engine:
        engine = "歌声合成"

    if "中文" in title:
        language = "zh"
    elif "外文" in title or "bilibili投稿" in title:
        language = "foreign"
    elif engine in CHINESE_DEFAULT_ENGINES:
        language = "zh"
    else:
        language = "unknown"

    return engine, language


def _record_from_context(context, href, page_url, page_title):
    lines = [
        _clean_text(x)
        for x in re.split(r"[\r\n]+", context)
        if _clean_text(x)
    ]
    if not lines:
        return None

    # Work only with the block after the most recent "曲目".
    start = -1
    for idx in range(len(lines) - 1, -1, -1):
        if re.search(r"(?:曲目|歌曲名称)\s*[：:]", lines[idx]):
            start = idx
            break
    if start < 0:
        return None

    block = " \n".join(lines[start:])
    title_match = re.search(
        r"(?:曲目|歌曲名称)\s*[：:]\s*(.+?)(?=\s+UP主\s*[：:]|\nUP主\s*[：:])",
        block,
        re.S,
    )
    up_match = re.search(
        r"UP主\s*[：:]\s*(.+?)(?=\s+投稿时间\s*[：:]|\n投稿时间\s*[：:])",
        block,
        re.S,
    )
    date_match = re.search(
        r"投稿时间\s*[：:]\s*(\d{4}-\d{1,2}-\d{1,2})",
        block,
    )

    if not title_match or not up_match or not date_match:
        return None

    title = _clean_text(title_match.group(1))
    uploader = _clean_text(up_match.group(1))
    publish_date = date_match.group(1)

    # Remove common list annotations, but keep the actual title readable.
    title = re.sub(r"\s*\^\[\d+\]\s*$", "", title).strip()
    if not title or not uploader:
        return None

    bvid, bili_url = _canonical_bilibili(href)
    if not bvid and not _extract_aid(bili_url):
        return None

    engine, language = _engine_and_language(page_title)

    return {
        "title": title,
        "uploader": uploader,
        "publish_date": publish_date,
        "bvid": bvid,
        "bilibili_url": bili_url,
        "engine": engine,
        "language": language,
        "level": "hall",
        "source_page": page_url,
        "source_title": page_title,
    }


class VcpediaPageParser(HTMLParser):
    def __init__(self, page_url, page_title):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.page_title = page_title
        self.text = ""
        self.anchor_stack = []
        self.records = []
        self.internal_links = set()

    def _append_text(self, text):
        if not text:
            return
        self.text += text
        if len(self.text) > 12000:
            self.text = self.text[-12000:]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in BLOCK_TAGS:
            self._append_text("\n")

        if tag == "a":
            href = attrs.get("href", "")
            self.anchor_stack.append({
                "href": href,
                "text": "",
                "context_start": max(0, len(self.text) - 3500),
            })

    def handle_data(self, data):
        self._append_text(data)
        if self.anchor_stack:
            self.anchor_stack[-1]["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor_stack:
            anchor = self.anchor_stack.pop()
            href = html.unescape(anchor["href"] or "")

            internal = _canonical_vcp_url(href, self.page_url)
            if internal:
                self.internal_links.add(internal)

            parsed = urlparse(urljoin(self.page_url, href))
            if parsed.netloc.lower() in {
                "www.bilibili.com",
                "bilibili.com",
                "m.bilibili.com",
            }:
                context = self.text[anchor["context_start"]:]
                record = _record_from_context(
                    context,
                    urljoin(self.page_url, href),
                    self.page_url,
                    self.page_title,
                )
                if record:
                    self.records.append(record)

        if tag in BLOCK_TAGS:
            self._append_text("\n")


async def _fetch_html(session, url):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }
    async with session.get(
        url,
        headers=headers,
        allow_redirects=True,
    ) as response:
        body = await response.text(errors="replace")
        if response.status >= 400:
            raise VcpediaError(
                f"VCPedia HTTP {response.status}: {_page_title_from_url(url)}"
            )
        return str(response.url), body


def _merge_record(old, new):
    if not old:
        return dict(new)

    result = dict(old)

    if new.get("language") == "zh":
        result["language"] = "zh"
    elif result.get("language") in {"", "unknown", None}:
        result["language"] = new.get("language") or "unknown"

    engines = {
        x.strip()
        for x in (
            str(result.get("engine") or "") + "/" + str(new.get("engine") or "")
        ).split("/")
        if x.strip()
    }
    result["engine"] = " / ".join(sorted(engines))

    source_titles = {
        x.strip()
        for x in (
            str(result.get("source_title") or "")
            + " | "
            + str(new.get("source_title") or "")
        ).split("|")
        if x.strip()
    }
    result["source_title"] = " | ".join(sorted(source_titles))

    if not result.get("bvid") and new.get("bvid"):
        result["bvid"] = new["bvid"]
        result["bilibili_url"] = new["bilibili_url"]

    return result


def _upsert_records(records):
    now = datetime.now().isoformat()
    inserted = 0
    updated = 0

    conn = get_conn()
    try:
        for record in records:
            bvid = str(record.get("bvid") or "").strip()
            bili_url = str(record.get("bilibili_url") or "").strip()

            existing = None
            if bvid:
                existing = conn.execute(
                    """
                    SELECT *
                    FROM songs
                    WHERE LOWER(COALESCE(bvid,''))=LOWER(?)
                    ORDER BY
                        CASE WHEN source='vcpedia' THEN 0 ELSE 1 END,
                        id ASC
                    LIMIT 1
                    """,
                    (bvid,),
                ).fetchone()

            if existing:
                # If an older VocaDB/manual row already points at the exact same
                # BV, promote/merge that row instead of creating a duplicate.
                keep_matched = (
                    existing["bilibili_status"] == "matched"
                    and int(existing["bilibili_verified"] or 0) == 1
                    and bool(existing["cover_local"])
                )

                conn.execute(
                    """
                    UPDATE songs
                    SET title_jp=?,
                        title_zh=?,
                        producer=?,
                        publish_date=?,
                        bvid=?,
                        bilibili_url=?,
                        source='vcpedia',
                        imported_at=COALESCE(imported_at, ?),
                        vcp_engine=?,
                        vcp_language=?,
                        vcp_level=COALESCE(NULLIF(vcp_level,''),'hall'),
                        vcp_source_page=?,
                        vcp_source_title=?,
                        vcp_synced_at=?,
                        bilibili_status=CASE
                            WHEN ? THEN bilibili_status
                            ELSE 'pending'
                        END,
                        available=CASE
                            WHEN COALESCE(manual_override,0)=1 THEN available
                            WHEN ? THEN available
                            ELSE 0
                        END
                    WHERE id=?
                    """,
                    (
                        record["title"],
                        record["title"] if record["language"] == "zh" else "",
                        record["uploader"],
                        record["publish_date"],
                        bvid,
                        bili_url,
                        now,
                        record["engine"],
                        record["language"],
                        record["source_page"],
                        record["source_title"],
                        now,
                        1 if keep_matched else 0,
                        1 if keep_matched else 0,
                        existing["id"],
                    ),
                )
                updated += 1
                continue

            conn.execute(
                """
                INSERT INTO songs(
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
                    imported_at,
                    cover_local,
                    bilibili_verified,
                    bilibili_status,
                    pool_score,
                    manual_override,
                    vcp_engine,
                    vcp_language,
                    vcp_level,
                    vcp_source_page,
                    vcp_source_title,
                    vcp_synced_at
                )
                VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    record["title"],
                    record["title"] if record["language"] == "zh" else "",
                    record["uploader"],
                    "",
                    record["publish_date"],
                    bvid,
                    bili_url,
                    "",
                    0,
                    "excluded",
                    0,
                    None,
                    "vcpedia",
                    now,
                    "",
                    0,
                    "pending",
                    0,
                    0,
                    record["engine"],
                    record["language"],
                    "hall",
                    record["source_page"],
                    record["source_title"],
                    now,
                ),
            )
            inserted += 1

        conn.commit()
    finally:
        conn.close()

    recalculate_pools()
    set_meta("last_vcpedia_sync", now)

    return {
        "inserted": inserted,
        "updated": updated,
        "total_records": len(records),
        "last_sync": now,
    }


async def sync_vcpedia(progress_callback=None):
    """
    Crawl VCPedia's Hall-of-Fame index and its Hall-of-Fame subpages.

    We only follow VCPedia internal page titles containing "殿堂曲", and only
    import entries that already contain an explicit Bilibili video link.
    There is NO Bilibili title search here.
    """
    timeout = aiohttp.ClientTimeout(total=25)
    queue = deque()
    seen = set()
    queued = set()

    # Seed the general Hall-of-Fame directory plus explicit current engine roots
    # so Chinese catalogues are included even if the directory layout changes.
    seeds = [INDEX_URL] + [
        urljoin(BASE_URL, quote(title, safe="/:_()·-. "))
        for title in EXPLICIT_SEEDS
    ]
    for url in seeds:
        if url not in queued:
            queue.append(url)
            queued.add(url)

    records_by_key = {}
    errors = []

    state = {
        "running": True,
        "pages_scanned": 0,
        "pages_queued": len(queue),
        "records_found": 0,
        "current_page": "",
        "errors": 0,
        "last_error": "",
    }

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while queue and len(seen) < MAX_PAGES:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)

            page_title = _page_title_from_url(url)
            state["current_page"] = page_title

            try:
                final_url, body = await _fetch_html(session, url)
                page_title = _page_title_from_url(final_url) or page_title

                parser = VcpediaPageParser(final_url, page_title)
                parser.feed(body)
                parser.close()

                for record in parser.records:
                    key = _video_key(record["bilibili_url"])
                    records_by_key[key] = _merge_record(
                        records_by_key.get(key),
                        record,
                    )

                for link in parser.internal_links:
                    if link not in seen and link not in queued:
                        queue.append(link)
                        queued.add(link)

            except Exception as exc:
                errors.append(f"{page_title}: {exc}")
                state["errors"] += 1
                state["last_error"] = str(exc)

            state["pages_scanned"] = len(seen)
            state["pages_queued"] = len(queue)
            state["records_found"] = len(records_by_key)

            if progress_callback:
                try:
                    progress_callback(dict(state))
                except Exception:
                    pass

            await asyncio.sleep(REQUEST_INTERVAL_SECONDS)

    records = list(records_by_key.values())
    db_result = _upsert_records(records)

    state.update(db_result)
    state["running"] = False
    state["current_page"] = ""
    state["crawl_exhausted"] = not bool(queue)
    state["error_samples"] = errors[:5]
    return state
