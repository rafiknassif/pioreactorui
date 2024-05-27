# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import socket
import sqlite3
import typing as t
from datetime import datetime
from datetime import timezone
from logging import handlers

import paho.mqtt.client as mqtt
from flask import Flask
from flask import g
from paho.mqtt.enums import CallbackAPIVersion

from config import env
from config import config
from version import __version__

NAME = "pioreactorui"
VERSION = __version__
HOSTNAME = socket.gethostname()
LOG_TOPIC = f"pioreactor/{HOSTNAME}/$experiment/logs/ui"


# set up logging
logger = logging.getLogger(NAME)
logger.setLevel(logging.DEBUG)

file_handler = handlers.WatchedFileHandler(env["UI_LOG_LOCATION"])
file_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)-2s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
)
logger.addHandler(file_handler)


logger.debug(f"Starting {NAME}={VERSION} on {HOSTNAME}...")
logger.debug(f".env={dict(env)}")

app = Flask(NAME)

# connect to MQTT server
logger.debug("Starting MQTT client")

client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)
client.username_pw_set(
    config.get("mqtt", "username", fallback="pioreactor"),
    config.get("mqtt", "password", fallback="raspberry"),
)
client.connect(
        host=config.get("mqtt", "broker_address", fallback="localhost"),
        port=config.getint("mqtt", "broker_port", fallback=1883)
)
client.loop_start()

## UTILS


def msg_to_JSON(msg: str, task: str, level: str) -> str:
    return json.dumps(
        {
            "message": msg.strip(),
            "task": task,
            "source": "ui",
            "level": level,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )


def publish_to_log(msg: str, task: str, level="DEBUG") -> None:
    client.publish(LOG_TOPIC, msg_to_JSON(msg, task, level))


def publish_to_experiment_log(msg: str, experiment: str, task: str, level="DEBUG") -> None:
    client.publish(f"pioreactor/{HOSTNAME}/{experiment}/logs/ui", msg_to_JSON(msg, task, level))


def publish_to_error_log(msg, task: str) -> None:
    logger.error(msg)
    if isinstance(msg, str):
        publish_to_log(msg, task, "ERROR")
    else:
        publish_to_log(json.dumps(msg), task, "ERROR")


def _make_dicts(cursor, row) -> dict:
    return dict((cursor.description[idx][0], value) for idx, value in enumerate(row))


def _get_db_connection():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(config['storage']["database"])
        db.row_factory = _make_dicts
        db.execute("PRAGMA foreign_keys = 1")

    return db


def query_db(
    query: str, args=(), one: bool = False
) -> t.Optional[list[dict[str, t.Any]] | dict[str, t.Any]]:
    cur = _get_db_connection().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv


def modify_db(statement: str, args=()) -> int:
    con = _get_db_connection()
    cur = con.cursor()
    try:
        cur.execute(statement, args)
        con.commit()
    except sqlite3.IntegrityError:
        return 0
    except Exception as e:
        print(e)
        con.rollback()  # TODO: test
        raise e
    finally:
        row_changes = cur.rowcount
        cur.close()
    return row_changes
