import random

from .database import get_pool, get_history


# Every recommendation independently rolls language.
FOREIGN_LANGUAGE_RATE = 0.70

# Quality-tier target weights.
# Myth > Legend > Hall, as requested.
LEVEL_RATE = {
    "myth": 45,
    "legend": 35,
    "hall": 20,
}

ALL_POOLS = ("hot", "normal", "new")


def choose_language():
    return (
        "foreign"
        if random.random() < FOREIGN_LANGUAGE_RATE
        else "zh"
    )


def choose_level():
    r = random.randint(1, 100)
    total = 0

    for level in ("myth", "legend", "hall"):
        total += LEVEL_RATE[level]
        if r <= total:
            return level

    return "hall"


def _without_history(songs, history):
    history_set = set(history)
    return [
        song
        for song in songs
        if song["id"] not in history_set
    ]


def _filter_language(songs, language):
    if language == "zh":
        return [
            song
            for song in songs
            if song["catalog_language"] == "zh"
        ]

    return [
        song
        for song in songs
        if song["catalog_language"] != "zh"
    ]


def _songs_for(language, level, history=None):
    songs = []

    for pool in ALL_POOLS:
        songs.extend(get_pool(pool))

    # A song belongs to exactly one pool, but keep this defensive de-dup in case
    # a future DB migration changes that.
    unique = {}
    for song in songs:
        unique[song["id"]] = song

    filtered = [
        song
        for song in unique.values()
        if song["catalog_level"] == level
    ]
    filtered = _filter_language(filtered, language)

    if history is not None:
        filtered = _without_history(filtered, history)

    return filtered


def _fallback_levels(selected):
    if selected == "myth":
        return ("myth", "legend", "hall")
    if selected == "legend":
        return ("legend", "myth", "hall")
    return ("hall", "legend", "myth")


def _pick_language_level(language, selected_level, history):
    # First respect recent-history exclusion.
    for level in _fallback_levels(selected_level):
        songs = _songs_for(
            language,
            level,
            history=history,
        )
        if songs:
            return random.choice(songs)

    # 七天内不能重复：曲池耗尽也不解除历史限制。
    return None


def recommend(group_id, language=None):
    history = get_history(group_id)

    desired_language = language if language is not None else choose_language()
    desired_level = choose_level()

    picked = _pick_language_level(
        desired_language,
        desired_level,
        history,
    )
    if picked:
        return picked

    # 专项指令严格限定语言，耗尽时不跨语言兜底。
    if language is not None:
        return None

    # Only cross language if the 70/30 rolled side has no eligible songs.
    fallback_language = (
        "zh"
        if desired_language == "foreign"
        else "foreign"
    )

    return _pick_language_level(
        fallback_language,
        desired_level,
        history,
    )
