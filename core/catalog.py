import asyncio
import html
import re
from collections import deque
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import aiohttp

from .database import get_conn, recalculate_pools, set_meta


USER_AGENT = (
    "Mozilla/5.0 (compatible; AstrBot-DailyVocaloid/2.3; "
    "Hall-of-Fame catalog cache)"
)
REQUEST_INTERVAL = 1.30
MAX_PAGES_PER_SOURCE = 180
NETWORK_BACKOFF = (3, 8, 20)
RATE_LIMIT_WAIT = 60
SERVICE_UNAVAILABLE_WAIT = 20


VOCAWIKI_BASE = "https://voca.wiki/"
VCPEDIA_BASE = "https://vcpedia.cn/"

# VocaWiki is the main source for Japanese/foreign Bilibili Hall-of-Fame
# entries. It explicitly delegates Chinese achievements to VCPedia on many
# engine pages, so VCPedia is used as the Chinese supplement.
VOCAWIKI_SEEDS = (
    "VOCALOID殿堂曲/bilibili投稿",
    "Synthesizer V殿堂曲/bilibili投稿",
    "UTAU殿堂曲/bilibili投稿",
    "CeVIO殿堂曲/bilibili投稿",
    "NEUTRINO殿堂曲/bilibili投稿",
    "VOICEVOX殿堂曲/bilibili投稿",
    "VoiSona殿堂曲/bilibili投稿",
    "New Type殿堂曲/bilibili投稿",
    "ACE殿堂曲",
    "VOICEPEAK殿堂曲",
)

VCPEDIA_SEEDS = (
    "VOCALOID中文殿堂曲",
    "Synthesizer V中文殿堂曲",
    "ACE中文殿堂曲",
    "UTAU中文殿堂曲",
    "X Studio殿堂曲",
    "Sharpkey殿堂曲",
    "MUTA殿堂曲",
    "DeepVocal殿堂曲",
    "DiffSinger殿堂曲",
    "VocalSharp殿堂曲",
    "袅袅虚拟歌手殿堂曲",
)

BLOCK_TAGS = {
    "address","article","aside","blockquote","br","dd","div","dl","dt",
    "fieldset","figcaption","figure","footer","form","h1","h2","h3","h4",
    "h5","h6","header","hr","li","main","nav","ol","p","pre","section",
    "table","tbody","td","tfoot","th","thead","tr","ul",
}

COVER_MARKERS = (
    "(翻)", "（翻）", "(翻唱)", "（翻唱）",
    "(cover)", "（cover）",
)


class CatalogError(RuntimeError):
    pass


class CatalogSourceBlocked(CatalogError):
    """Stop the current source instead of hammering it."""
    pass


def _clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _extract_bvid(value):
    match = re.search(r"(BV[0-9A-Za-z]{8,})", str(value or ""), re.I)
    return match.group(1) if match else ""


def _extract_aid(value):
    text = str(value or "")
    match = re.search(r"(?:^|[/\s])av(\d+)(?:$|[/?#\s])", text, re.I)
    if not match:
        match = re.search(r"[?&]aid=(\d+)", text, re.I)
    return match.group(1) if match else ""


def _video_key(url):
    bvid = _extract_bvid(url)
    if bvid:
        return "bv:" + bvid.lower()
    aid = _extract_aid(url)
    if aid:
        return "av:" + aid
    return str(url or "").strip().lower()


def _canonical_bili(url):
    bvid = _extract_bvid(url)
    if bvid:
        return bvid, f"https://www.bilibili.com/video/{bvid}"
    return "", str(url or "").strip()


def _is_cover(title):
    lowered = str(title or "").lower()
    return any(marker.lower() in lowered for marker in COVER_MARKERS)


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
        return unquote(
            parse_qs(parsed.query).get("title", [""])[0]
        ).strip()

    return path.lstrip("/").strip()


def _canonical_internal_url(href, base_url, domain):
    if not href:
        return ""

    absolute = urljoin(base_url, html.unescape(href))
    parsed = urlparse(absolute)

    if parsed.scheme not in {"http", "https"}:
        return ""
    if parsed.netloc.lower() not in {domain, "www." + domain}:
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

    return urljoin(
        f"https://{domain}/",
        quote(title, safe="/:_()·-. "),
    )


def _engine_from_title(page_title):
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
    return engine or "歌声合成"



def _normalize_cover_url(value, page_url):
    value = html.unescape(str(value or "")).strip()
    if not value:
        return ""

    # srcset can contain "url 1x, url2 2x"; prefer the first URL.
    if "," in value and (" 1x" in value or " 2x" in value or "w" in value):
        value = value.split(",", 1)[0].strip().split(" ", 1)[0].strip()

    if value.startswith("data:"):
        return ""
    if value.startswith("//"):
        value = "https:" + value
    else:
        value = urljoin(page_url, value)

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return ""

    lowered = value.lower()
    # Common UI decorations / tracking placeholders, not song covers.
    if any(
        token in lowered
        for token in (
            "logo",
            "favicon",
            "emoji",
            "spinner",
            "loading.gif",
            "1x1",
            "pixel",
            "avatar",
        )
    ):
        return ""

    return value


def _pick_image_attr(attrs):
    # MediaWiki themes and lazy loaders use several different attributes.
    for key in (
        "data-src",
        "data-original",
        "data-lazy-src",
        "data-echo",
        "src",
        "srcset",
    ):
        value = attrs.get(key)
        if value:
            return value
    return ""




LEVEL_RANK = {
    "hall": 1,
    "legend": 2,
    "myth": 3,
}


def _parse_play_count(text):
    """
    Parse common wiki display forms:
      101.2万
      1000万
      1.2亿
      1,234,567
    Returns an integer or 0 if the page does not expose a usable count.
    """
    text = _clean_text(text).replace(",", "")

    match = re.search(
        r"(?:播放(?:数量|量|数)?|再生(?:数|回数)?)\s*[：:]?\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*(亿|万)?",
        text,
        re.I,
    )
    if not match:
        return 0

    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return 0

    unit = match.group(2) or ""
    if unit == "亿":
        value *= 100_000_000
    elif unit == "万":
        value *= 10_000

    return int(value)


def _level_from_play_count(count):
    count = int(count or 0)
    if count >= 10_000_000:
        return "myth"
    if count >= 1_000_000:
        return "legend"
    return "hall"


def _max_level(left, right):
    left = str(left or "hall").lower()
    right = str(right or "hall").lower()

    if left not in LEVEL_RANK:
        left = "hall"
    if right not in LEVEL_RANK:
        right = "hall"

    return (
        left
        if LEVEL_RANK[left] >= LEVEL_RANK[right]
        else right
    )



def _extract_record(context, href, page_url, page_title, source, language, cover_url=""):
    lines = [
        _clean_text(x)
        for x in re.split(r"[\r\n]+", context)
        if _clean_text(x)
    ]
    if not lines:
        return None

    start = -1
    for idx in range(len(lines) - 1, -1, -1):
        if re.search(r"(?:曲目|歌曲名称)\s*[：:]", lines[idx]):
            start = idx
            break
    if start < 0:
        return None

    block = " \n".join(lines[start:])

    title_match = re.search(
        r"(?:曲目|歌曲名称)\s*[：:]\s*(.+?)"
        r"(?=\s+P主\s*[：:]|\s+UP主\s*[：:]|\nP主\s*[：:]|\nUP主\s*[：:])",
        block,
        re.S,
    )
    producer_match = re.search(
        r"(?:P主|UP主)\s*[：:]\s*(.+?)"
        r"(?=\s+投稿时间\s*[：:]|\n投稿时间\s*[：:])",
        block,
        re.S,
    )
    date_match = re.search(
        r"投稿时间\s*[：:]\s*(\d{4}-\d{1,2}-\d{1,2})",
        block,
    )

    play_count = _parse_play_count(block)
    parsed_level = _level_from_play_count(play_count)

    if not title_match or not producer_match or not date_match:
        return None

    title = _clean_text(title_match.group(1))
    producer = _clean_text(producer_match.group(1))
    publish_date = date_match.group(1)

    title = re.sub(r"\s*\^\[\d+\]\s*$", "", title).strip()

    if not title or not producer or _is_cover(title):
        return None

    bvid, bili_url = _canonical_bili(href)
    if not bvid and not _extract_aid(bili_url):
        return None

    return {
        "title": title,
        "producer": producer,
        "publish_date": publish_date,
        "bvid": bvid,
        "bilibili_url": bili_url,
        "cover_url": _normalize_cover_url(cover_url, page_url),
        "source": source,
        "language": language,
        "engine": _engine_from_title(page_title),
        "level": parsed_level,
        "wiki_play_count": play_count,
        "source_page": page_url,
        "source_title": page_title,
    }


class AchievementPageParser(HTMLParser):
    def __init__(self, page_url, page_title, source, language, domain):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.page_title = page_title
        self.source = source
        self.language = language
        self.domain = domain

        self.text = ""
        self.anchor_stack = []
        self.records = []
        self.internal_links = set()
        self.images = []

    def _append_text(self, text):
        if text:
            self.text += text

    def _last_record_marker(self):
        positions = [
            self.text.rfind("曲目："),
            self.text.rfind("曲目:"),
            self.text.rfind("歌曲名称："),
            self.text.rfind("歌曲名称:"),
        ]
        return max(positions)

    def _cover_for_record(self, record_marker):
        if record_marker < 0:
            return ""

        # The list pages place the song image directly before the textual
        # "曲目：" block. Prefer the nearest preceding image. The 1200-char
        # window avoids accidentally reusing a cover from the previous song.
        candidates = [
            item
            for item in self.images
            if item["position"] <= record_marker
            and record_marker - item["position"] <= 1200
            and item["url"]
        ]
        if not candidates:
            return ""

        return max(
            candidates,
            key=lambda item: item["position"],
        )["url"]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag in BLOCK_TAGS:
            self._append_text("\n")

        if tag == "img":
            raw = _pick_image_attr(attrs)
            cover = _normalize_cover_url(raw, self.page_url)
            if cover:
                self.images.append({
                    "position": len(self.text),
                    "url": cover,
                })

        if tag == "a":
            self.anchor_stack.append({
                "href": attrs.get("href", ""),
                "context_start": max(0, len(self.text) - 4000),
            })

    def handle_data(self, data):
        self._append_text(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor_stack:
            anchor = self.anchor_stack.pop()
            href = html.unescape(anchor["href"] or "")

            internal = _canonical_internal_url(
                href,
                self.page_url,
                self.domain,
            )
            if internal:
                title = _page_title_from_url(internal)

                if self.source == "vocawiki":
                    if (
                        "殿堂曲/bilibili投稿" in title
                        or title in {"ACE殿堂曲", "VOICEPEAK殿堂曲"}
                    ):
                        self.internal_links.add(internal)
                else:
                    if (
                        "中文殿堂曲" in title
                        or (
                            "殿堂曲" in title
                            and _engine_from_title(title) in {
                                "X Studio","Sharpkey","MUTA","DeepVocal",
                                "DiffSinger","VocalSharp","袅袅虚拟歌手",
                            }
                        )
                    ):
                        self.internal_links.add(internal)

            parsed = urlparse(urljoin(self.page_url, href))
            if parsed.netloc.lower() in {
                "www.bilibili.com",
                "bilibili.com",
                "m.bilibili.com",
            }:
                context = self.text[anchor["context_start"]:]
                record_marker = self._last_record_marker()
                cover_url = self._cover_for_record(record_marker)

                record = _extract_record(
                    context,
                    urljoin(self.page_url, href),
                    self.page_url,
                    self.page_title,
                    self.source,
                    self.language,
                    cover_url=cover_url,
                )
                if record:
                    self.records.append(record)

        if tag in BLOCK_TAGS:
            self._append_text("\n")


async def _fetch_html(session, url, progress_callback=None):
    """
    Conservative fetch policy for community wikis.

    - normal pacing is handled by the crawler (1.3 s between requests)
    - network/5xx retry: 3s -> 8s -> 20s
    - 429: wait Retry-After or 60s once; repeated 429 stops this source
    - 403: stop this source immediately
    - repeated 503: wait once, then stop this source
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.6",
    }

    rate_limit_seen = False
    unavailable_seen = False
    last_error = None

    for attempt in range(len(NETWORK_BACKOFF) + 1):
        try:
            async with session.get(
                url,
                headers=headers,
                allow_redirects=True,
            ) as response:
                body = await response.text(errors="replace")
                status = int(response.status)

                if status == 403:
                    raise CatalogSourceBlocked(
                        f"HTTP 403: {_page_title_from_url(url)}。"
                        "为避免继续触发站点保护，已停止当前来源。"
                    )

                if status == 429:
                    if rate_limit_seen:
                        raise CatalogSourceBlocked(
                            f"HTTP 429: {_page_title_from_url(url)}。"
                            "等待后再次限流，已停止当前来源。"
                        )

                    rate_limit_seen = True
                    retry_after = response.headers.get("Retry-After")
                    try:
                        wait_seconds = int(retry_after)
                    except (TypeError, ValueError):
                        wait_seconds = RATE_LIMIT_WAIT

                    wait_seconds = max(10, min(wait_seconds, 180))
                    if progress_callback:
                        progress_callback({
                            "throttle_state": "rate_limited",
                            "wait_seconds": wait_seconds,
                            "last_error": f"HTTP 429，暂停 {wait_seconds} 秒",
                        })
                    await asyncio.sleep(wait_seconds)
                    continue

                if status == 503:
                    if unavailable_seen:
                        raise CatalogSourceBlocked(
                            f"HTTP 503: {_page_title_from_url(url)}。"
                            "服务仍不可用，已停止当前来源。"
                        )

                    unavailable_seen = True
                    if progress_callback:
                        progress_callback({
                            "throttle_state": "service_unavailable",
                            "wait_seconds": SERVICE_UNAVAILABLE_WAIT,
                            "last_error": (
                                f"HTTP 503，暂停 {SERVICE_UNAVAILABLE_WAIT} 秒"
                            ),
                        })
                    await asyncio.sleep(SERVICE_UNAVAILABLE_WAIT)
                    continue

                if status >= 500:
                    raise CatalogError(
                        f"HTTP {status}: {_page_title_from_url(url)}"
                    )

                if status >= 400:
                    # Ordinary 4xx page misses are page-local errors; they are
                    # not retried and do not stop the entire source.
                    raise CatalogError(
                        f"HTTP {status}: {_page_title_from_url(url)}"
                    )

                return str(response.url), body

        except CatalogSourceBlocked:
            raise
        except CatalogError as exc:
            last_error = exc
            # Retry only server-side errors. Ordinary 404 etc. should return
            # immediately because waiting will not make the page appear.
            if "HTTP 5" not in str(exc):
                raise
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as exc:
            last_error = exc

        if attempt >= len(NETWORK_BACKOFF):
            break

        wait_seconds = NETWORK_BACKOFF[attempt]
        if progress_callback:
            progress_callback({
                "throttle_state": "backoff",
                "wait_seconds": wait_seconds,
                "last_error": (
                    f"网络错误，{wait_seconds} 秒后重试：{last_error}"
                ),
            })
        await asyncio.sleep(wait_seconds)

    raise CatalogError(
        f"请求失败：{_page_title_from_url(url)}：{last_error}"
    )


def _merge_record(old, new):
    if not old:
        return dict(new)

    result = dict(old)

    if not result.get("cover_url") and new.get("cover_url"):
        result["cover_url"] = new["cover_url"]

    result["level"] = _max_level(
        result.get("level"),
        new.get("level"),
    )
    result["wiki_play_count"] = max(
        int(result.get("wiki_play_count") or 0),
        int(new.get("wiki_play_count") or 0),
    )

    # Chinese classification wins if the same BV appears in both sources.
    if new.get("language") == "zh":
        result["language"] = "zh"

    sources = set(str(result.get("source") or "").split("+"))
    sources.add(str(new.get("source") or ""))
    result["source"] = "+".join(sorted(x for x in sources if x))

    engines = set(
        x.strip()
        for x in (
            str(result.get("engine") or "")
            + " / "
            + str(new.get("engine") or "")
        ).split("/")
        if x.strip()
    )
    result["engine"] = " / ".join(sorted(engines))

    source_titles = set(
        x.strip()
        for x in (
            str(result.get("source_title") or "")
            + " | "
            + str(new.get("source_title") or "")
        ).split("|")
        if x.strip()
    )
    result["source_title"] = " | ".join(sorted(source_titles))

    return result


def _split_joined(value, delimiter="+"):
    return {
        x.strip()
        for x in str(value or "").split(delimiter)
        if x.strip()
    }


def _merge_joined(left, right, delimiter="+"):
    values = _split_joined(left, delimiter)
    values.update(_split_joined(right, delimiter))
    return delimiter.join(sorted(values))


def _merge_engine_text(left, right):
    values = {
        x.strip()
        for x in (
            str(left or "") + " / " + str(right or "")
        ).split("/")
        if x.strip()
    }
    return " / ".join(sorted(values))


def _merge_source_titles(left, right):
    values = {
        x.strip()
        for x in (
            str(left or "") + " | " + str(right or "")
        ).split("|")
        if x.strip()
    }
    return " | ".join(sorted(values))


def _upsert_records(records, recalculate=True):
    """
    Persist parsed Hall-of-Fame records.

    v2.1.1 accidentally removed this function while rewriting _crawl_source,
    which meant the crawler could discover entries but every page failed when
    it attempted to write them. Keep this as a standalone function so both
    incremental page writes and the final cross-source consolidation use the
    same path.
    """
    now = datetime.now().isoformat()
    inserted = 0
    updated = 0

    conn = get_conn()
    try:
        for record in records:
            bvid = str(record.get("bvid") or "").strip()
            bili_url = str(record.get("bilibili_url") or "").strip()
            language = str(record.get("language") or "foreign").strip()
            title = str(record.get("title") or "").strip()
            source = str(record.get("source") or "").strip()

            existing = None

            if bvid:
                existing = conn.execute(
                    """
                    SELECT *
                    FROM songs
                    WHERE LOWER(COALESCE(bvid,''))=LOWER(?)
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (bvid,),
                ).fetchone()

            # Old av links may not yet have a BV until Bilibili verification.
            # Avoid duplicates by exact URL in that case.
            if existing is None and bili_url:
                existing = conn.execute(
                    """
                    SELECT *
                    FROM songs
                    WHERE COALESCE(TRIM(bilibili_url),'')=?
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (bili_url,),
                ).fetchone()

            if existing:
                existing_language = str(
                    existing["catalog_language"] or ""
                ).strip()
                merged_language = (
                    "zh"
                    if language == "zh" or existing_language == "zh"
                    else "foreign"
                )

                merged_source = _merge_joined(
                    existing["source"],
                    source,
                    "+",
                )
                merged_engine = _merge_engine_text(
                    existing["catalog_engine"],
                    record.get("engine"),
                )
                merged_source_title = _merge_source_titles(
                    existing["catalog_source_title"],
                    record.get("source_title"),
                )
                merged_level = _max_level(
                    existing["catalog_level"],
                    record.get("level"),
                )

                old_title_jp = str(existing["title_jp"] or "").strip()
                old_title_zh = str(existing["title_zh"] or "").strip()

                if language == "zh":
                    title_zh = title or old_title_zh
                    title_jp = old_title_jp
                else:
                    title_jp = title or old_title_jp
                    # Keep a display title for foreign records if no Chinese
                    # title exists; UI already prefers title_zh.
                    title_zh = old_title_zh or title

                producer = (
                    str(record.get("producer") or "").strip()
                    or str(existing["producer"] or "").strip()
                )
                publish_date = (
                    str(record.get("publish_date") or "").strip()
                    or str(existing["publish_date"] or "").strip()
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
                        cover_url=?,
                        source=?,
                        catalog_language=?,
                        catalog_engine=?,
                        catalog_level=?,
                        catalog_source_page=?,
                        catalog_source_title=?,
                        catalog_synced_at=?
                    WHERE id=?
                    """,
                    (
                        title_jp,
                        title_zh,
                        producer,
                        publish_date,
                        bvid or str(existing["bvid"] or ""),
                        bili_url or str(existing["bilibili_url"] or ""),
                        str(record.get("cover_url") or "").strip()
                        or str(existing["cover_url"] or ""),
                        merged_source,
                        merged_language,
                        merged_engine,
                        merged_level,
                        str(record.get("source_page") or "").strip()
                        or str(existing["catalog_source_page"] or ""),
                        merged_source_title,
                        now,
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
                    pool_type,
                    available,
                    source,
                    imported_at,
                    cover_local,
                    bilibili_verified,
                    bilibili_status,
                    manual_override,
                    catalog_language,
                    catalog_engine,
                    catalog_level,
                    catalog_source_page,
                    catalog_source_title,
                    catalog_synced_at
                )
                VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    title if language != "zh" else "",
                    title,
                    str(record.get("producer") or "").strip(),
                    "",
                    str(record.get("publish_date") or "").strip(),
                    bvid,
                    bili_url,
                    str(record.get("cover_url") or "").strip(),
                    "excluded",
                    0,
                    source,
                    now,
                    "",
                    0,
                    "pending",
                    0,
                    language,
                    str(record.get("engine") or "").strip(),
                    str(record.get("level") or "hall").strip() or "hall",
                    str(record.get("source_page") or "").strip(),
                    str(record.get("source_title") or "").strip(),
                    now,
                ),
            )
            inserted += 1

        conn.commit()
    finally:
        conn.close()

    if recalculate:
        recalculate_pools()

    return {
        "inserted": inserted,
        "updated": updated,
        "total_records": len(records),
    }


async def _crawl_source(
    session,
    *,
    source,
    language,
    base_url,
    domain,
    seeds,
    progress_callback=None,
):
    queue = deque()
    queued = set()
    seen = set()
    records = {}
    errors = []
    inserted = 0
    updated = 0
    blocked = False

    for title in seeds:
        url = urljoin(
            base_url,
            quote(title, safe="/:_()·-. "),
        )
        if url not in queued:
            queue.append(url)
            queued.add(url)

    while queue and len(seen) < MAX_PAGES_PER_SOURCE:
        url = queue.popleft()
        if url in seen:
            continue

        seen.add(url)
        page_title = _page_title_from_url(url)

        def relay_fetch(info):
            if progress_callback:
                payload = {
                    "current_source": source,
                    "current_page": page_title,
                    "pages_scanned": len(seen),
                    "pages_queued": len(queue),
                    "records_found": len(records),
                    "errors": len(errors),
                    "request_interval": REQUEST_INTERVAL,
                    "max_pages": MAX_PAGES_PER_SOURCE,
                }
                payload.update(info)
                progress_callback(payload)

        try:
            final_url, body = await _fetch_html(
                session,
                url,
                progress_callback=relay_fetch,
            )
            page_title = _page_title_from_url(final_url) or page_title

            parser = AchievementPageParser(
                final_url,
                page_title,
                source,
                language,
                domain,
            )
            parser.feed(body)
            parser.close()

            page_records = {}
            for record in parser.records:
                key = _video_key(record["bilibili_url"])
                page_records[key] = _merge_record(
                    page_records.get(key),
                    record,
                )
                records[key] = _merge_record(
                    records.get(key),
                    record,
                )

            # Persist each successful page immediately. If a later page hits
            # rate limiting, everything already parsed remains in SQLite and
            # the next sync continues by updating/deduplicating rather than
            # starting from zero.
            if page_records:
                page_result = _upsert_records(
                    list(page_records.values()),
                    recalculate=False,
                )
                inserted += int(page_result.get("inserted") or 0)
                updated += int(page_result.get("updated") or 0)

            for link in parser.internal_links:
                if link not in seen and link not in queued:
                    queue.append(link)
                    queued.add(link)

        except CatalogSourceBlocked as exc:
            blocked = True
            errors.append(f"{page_title}: {exc}")
            if progress_callback:
                progress_callback({
                    "current_source": source,
                    "current_page": page_title,
                    "pages_scanned": len(seen),
                    "pages_queued": len(queue),
                    "records_found": len(records),
                    "errors": len(errors),
                    "last_error": str(exc),
                    "throttle_state": "source_stopped",
                    "wait_seconds": 0,
                    "request_interval": REQUEST_INTERVAL,
                    "max_pages": MAX_PAGES_PER_SOURCE,
                })
            break

        except Exception as exc:
            errors.append(f"{page_title}: {exc}")

        if progress_callback:
            progress_callback({
                "current_source": source,
                "current_page": page_title,
                "pages_scanned": len(seen),
                "pages_queued": len(queue),
                "records_found": len(records),
                "errors": len(errors),
                "last_error": errors[-1] if errors else "",
                "throttle_state": "normal",
                "wait_seconds": REQUEST_INTERVAL,
                "request_interval": REQUEST_INTERVAL,
                "max_pages": MAX_PAGES_PER_SOURCE,
            })

        await asyncio.sleep(REQUEST_INTERVAL)

    return {
        "records": list(records.values()),
        "pages_scanned": len(seen),
        "errors": errors,
        "blocked": blocked,
        "inserted": inserted,
        "updated": updated,
    }


async def sync_catalog(progress_callback=None):
    timeout = aiohttp.ClientTimeout(total=45)
    headers = {"User-Agent": USER_AGENT}

    state = {
        "running": True,
        "current_source": "",
        "current_page": "",
        "pages_scanned": 0,
        "pages_queued": 0,
        "records_found": 0,
        "vocawiki_records": 0,
        "vcpedia_records": 0,
        "errors": 0,
        "last_error": "",
        "throttle_state": "normal",
        "wait_seconds": REQUEST_INTERVAL,
        "request_interval": REQUEST_INTERVAL,
        "max_pages": MAX_PAGES_PER_SOURCE,
    }

    def relay(info):
        state.update(info)
        if progress_callback:
            try:
                progress_callback(dict(state))
            except Exception:
                pass

    async with aiohttp.ClientSession(
        timeout=timeout,
        headers=headers,
    ) as session:
        vw = await _crawl_source(
            session,
            source="vocawiki",
            language="foreign",
            base_url=VOCAWIKI_BASE,
            domain="voca.wiki",
            seeds=VOCAWIKI_SEEDS,
            progress_callback=relay,
        )
        state["vocawiki_records"] = len(vw["records"])

        # A protection response on one source does not prevent the other source
        # from being attempted. Each source has its own conservative stop.
        vc = await _crawl_source(
            session,
            source="vcpedia",
            language="zh",
            base_url=VCPEDIA_BASE,
            domain="vcpedia.cn",
            seeds=VCPEDIA_SEEDS,
            progress_callback=relay,
        )
        state["vcpedia_records"] = len(vc["records"])

    # Consolidate the two in-memory source sets once more. This is a
    # database-only pass (no additional network requests) and guarantees that
    # the same BV appearing in both wikis becomes one merged row.
    merged = {}
    for record in vw["records"] + vc["records"]:
        key = _video_key(record["bilibili_url"])
        merged[key] = _merge_record(
            merged.get(key),
            record,
        )

    _upsert_records(
        list(merged.values()),
        recalculate=False,
    )

    # Recalculate recommendation pools once after all incremental/final writes.
    recalculate_pools()

    # Count the actual deduplicated rows persisted in the new catalogue.
    conn = get_conn()
    try:
        total_records = conn.execute(
            """
            SELECT COUNT(*)
            FROM songs
            WHERE source LIKE '%vocawiki%'
               OR source LIKE '%vcpedia%'
            """
        ).fetchone()[0]
    finally:
        conn.close()

    errors = vw["errors"] + vc["errors"]
    now = datetime.now().isoformat()
    set_meta("last_catalog_sync", now)

    state.update({
        "running": False,
        "current_source": "",
        "current_page": "",
        "records_found": total_records,
        "total_records": total_records,
        "inserted": int(vw.get("inserted") or 0) + int(vc.get("inserted") or 0),
        "updated": int(vw.get("updated") or 0) + int(vc.get("updated") or 0),
        "errors": len(errors),
        "last_error": errors[-1] if errors else "",
        "error_samples": errors[:8],
        "last_sync": now,
        "vocawiki_blocked": bool(vw.get("blocked")),
        "vcpedia_blocked": bool(vc.get("blocked")),
        "throttle_state": "done",
        "wait_seconds": 0,
    })
    return state
