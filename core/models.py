from dataclasses import dataclass

from typing import Optional



@dataclass

class Song:

    id: int

    title_jp: str

    title_zh: str

    producer: str

    vocal: str

    publish_date: str

    bilibili_url: str

    cover_url: Optional[str] = None

    pool_type: str = "normal"



@dataclass

class RecommendResult:

    song: Song

    pool_label: str

