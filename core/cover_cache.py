"""Only evict generated BV cover images; never touch catalogues or history."""
import os
import re
from pathlib import Path

MAX_CACHE_BYTES = 50 * 1024 * 1024
IMAGE_NAME = re.compile(r'BV[0-9A-Za-z]{10}\.(?:jpg|jpeg|png|webp)\Z', re.I)


def prune_cache(directory, limit=MAX_CACHE_BYTES):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        return 0
    files = []
    for path in directory.iterdir():
        if path.is_symlink() or not IMAGE_NAME.fullmatch(path.name):
            continue
        try:
            if path.is_file():
                stat = path.stat()
                files.append((stat.st_mtime_ns, path.name, path, stat.st_size))
        except FileNotFoundError:
            pass
    total = sum(row[3] for row in files)
    for _, _, path, size in sorted(files):
        if total <= limit:
            break
        try:
            path.unlink()
            total -= size
        except FileNotFoundError:
            total -= size
    return total


def touch_cover(path):
    try:
        os.utime(path, None)
    except FileNotFoundError:
        pass


def save_cover(path, content, limit=MAX_CACHE_BYTES):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink() or not IMAGE_NAME.fullmatch(path.name) or len(content) > limit:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    touch_cover(path)
    prune_cache(path.parent, limit)
    return path if path.is_file() else None
