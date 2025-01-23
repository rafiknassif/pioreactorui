# -*- coding: utf-8 -*-
"""
this contains shared data for both huey and the flask app

"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from dotenv import dotenv_values
from huey import SqliteHuey


def is_testing_env():
    return os.environ.get("TESTING") == "1"


CACHE_DIR = Path(tempfile.gettempdir()) / "pioreactor_cache"

env = dotenv_values(".env", verbose=True)

try:
    huey = SqliteHuey(filename=CACHE_DIR / "huey.db")
except sqlite3.OperationalError:
    raise IOError(f'Unable to open huey.db at {CACHE_DIR / "huey.db"}')
