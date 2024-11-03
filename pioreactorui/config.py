# -*- coding: utf-8 -*-
"""
this contains shared data for both huey and the flask app

"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from diskcache import Cache
from dotenv import dotenv_values
from huey import SqliteHuey


def is_testing_env():
    return os.environ.get("TESTING") == "1"


CACHE_DIR = Path(tempfile.gettempdir()) / "pioreactorui_cache"

env = dotenv_values(".env", verbose=True)

try:
    huey = SqliteHuey(filename=CACHE_DIR / "huey.db")
except sqlite3.OperationalError:
    raise IOError(f'Unable to open huey.db at {CACHE_DIR / "huey.db"}')


cache = Cache(
    directory=CACHE_DIR,
    tag_index=True,
    disk_min_file_size=2**16,
)
