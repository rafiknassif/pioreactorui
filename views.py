# -*- coding: utf-8 -*-
from __future__ import annotations

import configparser
import os
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from datetime import timezone
from pathlib import Path

from flask import g
from flask import jsonify
from flask import request
from flask import Response
from huey.exceptions import HueyException
from msgspec import DecodeError
from msgspec import ValidationError
from msgspec.json import decode as json_decode
from msgspec.json import encode as json_encode
from msgspec.yaml import decode as yaml_decode
from werkzeug.utils import secure_filename

import structs
import tasks as background_tasks
from app import app
from app import client
from app import modify_db
from app import publish_to_error_log
from app import publish_to_log
from app import query_db
from app import VERSION
from config import cache
from config import env


def scrub_to_valid(value: str):
    if value is None:
        raise ValueError()
    elif value.startswith("sqlite_"):
        raise ValueError()
    return "".join(chr for chr in value if (chr.isalnum() or chr == "_"))


def current_utc_datetime() -> datetime:
    # this is timezone aware.
    return datetime.now(timezone.utc)


def to_iso_format(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def current_utc_timestamp() -> str:
    # this is timezone aware.
    return to_iso_format(current_utc_datetime())


def is_valid_unix_filename(filename):
    return (
        bool(re.fullmatch(r"[a-zA-Z0-9._-]+", filename))
        and "/" not in filename
        and "\0" not in filename
    )


## PIOREACTOR CONTROL


@app.route("/api/stop_all", methods=["POST"])
def stop_all():
    """Kills all jobs"""
    background_tasks.pios("kill", "--all-jobs", "-y")
    return Response(status=202)


@app.route("/api/stop/<unit>/<job>", methods=["PATCH"])
def stop_job_on_unit(unit: str, job: str):
    """Kills specified job on unit"""

    jobs_to_kill_over_MQTT = {
        "add_media",
        "add_alt_media",
        "remove_waste",
        "circulate_media",
        "circulate_alt_media",
    }

    if job in jobs_to_kill_over_MQTT:
        msg = client.publish(
            f"pioreactor/{unit}/$experiment/{job}/$state/set", b"disconnected", qos=1
        )
        try:
            msg.wait_for_publish(timeout=1.0)
        except Exception:
            return Response(status=500)
    else:
        background_tasks.pios("kill", job, "-y", "--units", unit)

    return Response(status=202)


@app.route("/api/run/<unit>/<job>", methods=["PATCH"])
def run_job_on_unit(unit: str, job: str):
    """
    Runs specified job on unit.

    The body is passed to the CLI, and should look like:

    {
      "options": {
        "option1": "value1",
        "option2": "value2"
      },
      "args": ["arg1", "arg2"]
    }
    """
    try:
        client.publish(
            f"pioreactor/{unit}/$experiment/run/{job}",
            request.get_data() or r'{"options": {}, "args": []}',
            qos=1,
        )
    except Exception as e:
        publish_to_error_log(e, "run_job_on_unit")
        raise e

    return Response(status=202)


# @app.route("/api/run", methods=["GET"])
# def list_running_jobs_on_cluster(unit: str, job: str):
#     #TODO
#     active_jobs = []
#     def append(msg):
#         if msg.payload == b"ready":
#             active_jobs.append(msg)
#
#     client.message_callback_add(
#         f"pioreactor/+/+/+/$state", append
#     )
#
#     return Response(status=202)


@app.route("/api/reboot/<unit>", methods=["POST"])
def reboot_unit(unit: str):
    """Reboots unit"""
    background_tasks.pios("reboot", "-y", "--units", unit)
    return Response(status=202)


@app.route("/api/shutdown/<unit>", methods=["POST"])
def shutdown_unit(unit: str):
    """Shutdown unit"""
    background_tasks.pios("shutdown", "-y", "--units", unit)
    return Response(status=202)


## DATA FOR CARDS ON OVERVIEW


@app.route("/api/logs/recent", methods=["GET"])
def get_recent_logs():
    """Shows event logs from all units"""

    def get_level_string(min_level):
        levels = {
            "DEBUG": ["ERROR", "WARNING", "NOTICE", "INFO", "DEBUG"],
            "INFO": ["ERROR", "NOTICE", "INFO", "WARNING"],
            "WARNING": ["ERROR", "WARNING"],
            "ERROR": ["ERROR"],
        }

        selected_levels = levels.get(min_level, levels["INFO"])
        return " or ".join(f'level == "{level}"' for level in selected_levels)

    min_level = request.args.get("min_level", "INFO")
    level_string = "(" + get_level_string(min_level) + ")"

    try:
        recent_logs = query_db(
            f"SELECT l.timestamp, level=='ERROR'as is_error, level=='WARNING' as is_warning, level=='NOTICE' as is_notice, l.pioreactor_unit, message, task FROM logs AS l LEFT JOIN latest_experiment AS le ON (le.experiment = l.experiment OR l.experiment=?) WHERE {level_string} AND l.timestamp >= MAX(strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-24 hours')), le.created_at) ORDER BY l.timestamp DESC LIMIT 50;",
            ("$experiment",),
        )
    except Exception as e:
        publish_to_error_log(str(e), "get_recent_logs")
        return Response(status=500)

    return jsonify(recent_logs)


@app.route("/api/logs/<experiment>", methods=["GET"])
def get_logs(experiment):
    """Shows event logs from all units"""

    def get_level_string(min_level):
        levels = {
            "DEBUG": ["ERROR", "WARNING", "NOTICE", "INFO", "DEBUG"],
            "INFO": ["ERROR", "NOTICE", "INFO", "WARNING"],
            "WARNING": ["ERROR", "WARNING"],
            "ERROR": ["ERROR"],
        }

        selected_levels = levels.get(min_level, levels["INFO"])
        return " or ".join(f'level == "{level}"' for level in selected_levels)

    min_level = request.args.get("min_level", "INFO")
    level_string = "(" + get_level_string(min_level) + ")"

    try:
        recent_logs = query_db(
            f"SELECT l.timestamp, level=='ERROR'as is_error, level=='WARNING' as is_warning, level=='NOTICE' as is_notice, l.pioreactor_unit, message, task FROM logs AS l WHERE (le.experiment=? OR l.experiment=?) AND {level_string} AND l.timestamp >= MAX(strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-24 hours')), le.created_at) ORDER BY l.timestamp DESC LIMIT 50;",
            (
                experiment,
                "$experiment",
            ),
        )
    except Exception as e:
        publish_to_error_log(str(e), "get_logs")
        return Response(status=500)

    return jsonify(recent_logs)


@app.route("/api/time_series/growth_rates/<experiment>", methods=["GET"])
def get_growth_rates(experiment: str):
    """Gets growth rates for all units"""
    args = request.args
    filter_mod_n = float(args.get("filter_mod_N", 100.0))
    lookback = float(args.get("lookback", 4.0))

    try:
        growth_rates = query_db(
            """
            SELECT
                json_object('series', json_group_array(unit), 'data', json_group_array(json(data))) as result
            FROM (
                SELECT pioreactor_unit as unit,
                       json_group_array(json_object('x', timestamp, 'y', round(rate, 5))) as data
                FROM growth_rates
                WHERE experiment=? AND
                      ((ROWID * 0.61803398875) - cast(ROWID * 0.61803398875 as int) < 1.0/?) AND
                      timestamp > strftime('%Y-%m-%dT%H:%M:%S', datetime('now',?))
                GROUP BY 1
                );
            """,
            (experiment, filter_mod_n, f"-{lookback} hours"),
            one=True,
        )
        assert isinstance(growth_rates, dict)

    except Exception as e:
        publish_to_error_log(str(e), "get_growth_rates")
        return Response(status=400)

    return growth_rates["result"]


@app.route("/api/time_series/temperature_readings/<experiment>", methods=["GET"])
def get_temperature_readings(experiment: str):
    """Gets temperature readings for all units"""
    args = request.args
    filter_mod_n = float(args.get("filter_mod_N", 100.0))
    lookback = float(args.get("lookback", 4.0))

    try:
        temperature_readings = query_db(
            """
            SELECT json_object('series', json_group_array(unit), 'data', json_group_array(json(data))) as result
            FROM (
                SELECT
                    pioreactor_unit as unit,
                    json_group_array(json_object('x', timestamp, 'y', round(temperature_c, 2))) as data
                FROM temperature_readings
                WHERE experiment=? AND
                    ((ROWID * 0.61803398875) - cast(ROWID * 0.61803398875 as int) < 1.0/?) AND
                    timestamp > strftime('%Y-%m-%dT%H:%M:%S', datetime('now',?))
                GROUP BY 1
                );
            """,
            (experiment, filter_mod_n, f"-{lookback} hours"),
            one=True,
        )
        assert isinstance(temperature_readings, dict)

    except Exception as e:
        publish_to_error_log(str(e), "get_temperature_readings")
        return Response(status=400)

    return temperature_readings["result"]


@app.route("/api/time_series/od_readings_filtered/<experiment>", methods=["GET"])
def get_od_readings_filtered(experiment: str):
    """Gets normalized od for all units"""
    args = request.args
    filter_mod_n = float(args.get("filter_mod_N", 100.0))
    lookback = float(args.get("lookback", 4.0))

    try:
        filtered_od_readings = query_db(
            """
            SELECT
                json_object('series', json_group_array(unit), 'data', json_group_array(json(data))) as result
            FROM (
                SELECT
                    pioreactor_unit as unit,
                    json_group_array(json_object('x', timestamp, 'y', round(normalized_od_reading, 7))) as data
                FROM od_readings_filtered
                WHERE experiment=? AND
                    ((ROWID * 0.61803398875) - cast(ROWID * 0.61803398875 as int) < 1.0/?) AND
                    timestamp > strftime('%Y-%m-%dT%H:%M:%S', datetime('now',?))
                GROUP BY 1
                );
            """,
            (experiment, filter_mod_n, f"-{lookback} hours"),
            one=True,
        )
        assert isinstance(filtered_od_readings, dict)

    except Exception as e:
        publish_to_error_log(str(e), "get_od_readings_filtered")
        return Response(status=400)

    return filtered_od_readings["result"]


@app.route("/api/time_series/od_readings/<experiment>", methods=["GET"])
def get_od_readings(experiment: str):
    """Gets raw od for all units"""
    args = request.args
    filter_mod_n = float(args.get("filter_mod_N", 100.0))
    lookback = float(args.get("lookback", 4.0))

    try:
        raw_od_readings = query_db(
            """
            SELECT
                json_object('series', json_group_array(unit), 'data', json_group_array(json(data))) as result
            FROM (
                SELECT pioreactor_unit || '-' || channel as unit, json_group_array(json_object('x', timestamp, 'y', round(od_reading, 7))) as data
                FROM od_readings
                WHERE experiment=? AND
                    ((ROWID * 0.61803398875) - cast(ROWID * 0.61803398875 as int) < 1.0/?) AND
                    timestamp > strftime('%Y-%m-%dT%H:%M:%S', datetime('now', ?))
                GROUP BY 1
                );
            """,
            (experiment, filter_mod_n, f"-{lookback} hours"),
            one=True,
        )
        assert isinstance(raw_od_readings, dict)

    except Exception as e:
        publish_to_error_log(str(e), "get_od_readings")
        return Response(status=400)

    return raw_od_readings["result"]


@app.route("/api/time_series/<data_source>/<experiment>/<column>", methods=["GET"])
def get_fallback_time_series(data_source: str, experiment: str, column: str):
    args = request.args
    try:
        lookback = float(args.get("lookback", 4.0))
        data_source = scrub_to_valid(data_source)
        column = scrub_to_valid(column)
        r = query_db(
            f"SELECT json_object('series', json_group_array(unit), 'data', json_group_array(json(data))) as result FROM (SELECT pioreactor_unit as unit, json_group_array(json_object('x', timestamp, 'y', round({column}, 7))) as data FROM {data_source} WHERE experiment=? AND timestamp > strftime('%Y-%m-%dT%H:%M:%S', datetime('now',?)) and {column} IS NOT NULL GROUP BY 1);",
            (experiment, f"-{lookback} hours"),
            one=True,
        )
        assert isinstance(r, dict)

    except Exception as e:
        publish_to_error_log(str(e), "get_fallback_time_series")
        return Response(status=400)
    return r["result"]


@app.route("/api/media_rates/current", methods=["GET"])
def get_current_media_rates():
    """
    Shows amount of added media per unit. Note that it only consider values from a dosing automation (i.e. not manual dosing, which includes continously dose)

    """
    ## this one confusing

    try:
        rows = query_db(
            """
            SELECT
                d.pioreactor_unit,
                SUM(CASE WHEN event='add_media' THEN volume_change_ml ELSE 0 END) / 3 AS mediaRate,
                SUM(CASE WHEN event='add_alt_media' THEN volume_change_ml ELSE 0 END) / 3 AS altMediaRate
            FROM dosing_events AS d
            JOIN latest_experiment USING (experiment)
            WHERE
                datetime(d.timestamp) >= datetime('now', '-3 hours') AND
                event IN ('add_alt_media', 'add_media') AND
                source_of_event LIKE 'dosing_automation%'
            GROUP BY d.pioreactor_unit;
            """
        )

        json_result = {}
        aggregate = {"altMediaRate": 0.0, "mediaRate": 0.0}

        for row in rows:
            json_result[row["pioreactor_unit"]] = {
                "altMediaRate": row["altMediaRate"],
                "mediaRate": row["mediaRate"],
            }
            aggregate["mediaRate"] = aggregate["mediaRate"] + row["mediaRate"]
            aggregate["altMediaRate"] = aggregate["altMediaRate"] + row["altMediaRate"]

        json_result["all"] = aggregate
        return jsonify(json_result)

    except Exception as e:
        publish_to_error_log(str(e), "get_current_media_rates")
        return Response(status=500)


## CALIBRATIONS


@app.route("/api/calibrations/<pioreactor_unit>", methods=["GET"])
def get_available_calibrations_type_by_unit(pioreactor_unit: str):
    """
    {
        "types": [
            "temperature",
            "pH",
            "dissolved_oxygen",
            "conductivity"
        ]
    }
    """
    try:
        types = query_db(
            "SELECT DISTINCT type FROM calibrations WHERE pioreactor_unit=?",
            (pioreactor_unit),
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_available_calibrations_type_by_unit")
        return Response(status=500)

    return jsonify(types)


@app.route("/api/calibrations/<pioreactor_unit>/<calibration_type>", methods=["GET"])
def get_available_calibrations_of_type(pioreactor_unit: str, calibration_type: str):
    try:
        unit_calibration = query_db(
            "SELECT * FROM calibrations WHERE type=? AND pioreactor_unit=?",
            (calibration_type, pioreactor_unit),
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_available_calibrations_of_type")
        return Response(status=500)

    return jsonify(unit_calibration)


@app.route("/api/calibrations/<pioreactor_unit>/<calibration_type>/current", methods=["GET"])
def get_current_calibrations_of_type(pioreactor_unit: str, calibration_type: str):
    """
    retrieve the current calibration for type
    """
    try:
        r = query_db(
            "SELECT * FROM calibrations WHERE type=? AND pioreactor_unit=? AND is_current=1",
            (calibration_type, pioreactor_unit),
            one=True,
        )
        assert isinstance(r, dict)

        r["data"] = json_decode(r["data"])
        return jsonify(r)

    except Exception as e:
        publish_to_error_log(str(e), "get_current_calibrations_of_type")
        return Response(status=500)


@app.route(
    "/api/calibrations/<pioreactor_unit>/<calibration_type>/<calibration_name>", methods=["GET"]
)
def get_calibration_by_name(pioreactor_unit: str, calibration_type: str, calibration_name: str):
    """
    retrieve the calibration for type with name
    """
    try:
        r = query_db(
            "SELECT * FROM calibrations WHERE type=? AND pioreactor_unit=? AND name=?",
            (calibration_type, pioreactor_unit, calibration_name),
            one=True,
        )
        assert isinstance(r, dict)

        r["data"] = json_decode(r["data"])
        return jsonify(r)

    except Exception as e:
        publish_to_error_log(str(e), "get_calibration_by_name")
        return Response(status=500)


@app.route(
    "/api/calibrations/<pioreactor_unit>/<calibration_type>/<calibration_name>", methods=["PATCH"]
)
def patch_calibrations(pioreactor_unit: str, calibration_type: str, calibration_name: str):
    body = request.get_json()

    if "current" in body and body["current"] == 1:
        try:
            # does the new one exist in the database?
            existing_row = query_db(
                "SELECT * FROM calibrations WHERE pioreactor_unit=(?) AND type=(?) AND name=(?)",
                (pioreactor_unit, calibration_type, calibration_name),
                one=True,
            )
            if existing_row is None:
                publish_to_error_log(
                    f"calibration {calibration_name=}, {pioreactor_unit=}, {calibration_type=} doesn't exist in database.",
                    "patch_calibrations",
                )
                return Response(status=404)

            elif existing_row["is_current"] == 1:  # type: ignore
                # already current
                return Response(status=200)

            modify_db(
                "UPDATE calibrations SET is_current=0, set_to_current_at=NULL WHERE pioreactor_unit=(?) AND type=(?) AND is_current=1",
                (pioreactor_unit, calibration_type),
            )

            modify_db(
                "UPDATE calibrations SET is_current=1, set_to_current_at=CURRENT_TIMESTAMP WHERE pioreactor_unit=(?) AND type=(?) AND name=(?)",
                (pioreactor_unit, calibration_type, calibration_name),
            )
            return Response(status=200)

        except Exception as e:
            publish_to_error_log(str(e), "patch_calibrations")
            return Response(status=500)

    else:
        return Response(status=404)


@app.route("/api/calibrations", methods=["PUT"])
def create_or_update_new_calibrations():
    try:
        body = request.get_json()

        modify_db(
            "INSERT OR REPLACE INTO calibrations (pioreactor_unit, created_at, type, data, name, is_current, set_to_current_at) values (?, ?, ?, ?, ?, ?, ?)",
            (
                body["pioreactor_unit"],
                body["created_at"],
                body["type"],
                json_encode(
                    body
                ).decode(),  # keep it as a string, not bytes, probably equivalent to request.get_data(as_text=True)
                body["name"],
                0,
                None,
            ),
        )

        return Response(status=201)
    except KeyError as e:
        publish_to_error_log(str(e), "create_or_update_new_calibrations")
        return Response(status=400)
    except Exception as e:
        publish_to_error_log(str(e), "create_or_update_new_calibrations")
        return Response(status=500)


## PLUGINS


@app.route("/api/installed_plugins", methods=["GET"])
@cache.memoize(expire=15, tag="plugins")
def get_installed_plugins():
    result = background_tasks.pio("list-plugins", "--json")
    try:
        status, msg = result(blocking=True, timeout=120)
    except HueyException:
        status, msg = False, "Timed out."

    if not status:
        publish_to_error_log(msg, "installed_plugins")
        return jsonify([])
    else:
        # sometimes an error from a plugin will be printed. We just want to last line, the json bit.
        plugins_as_json = msg.split("\n")[-1]
        return plugins_as_json


@app.route("/api/upload", methods=["POST"])
def upload():
    if os.path.isfile(Path(env["DOT_PIOREACTOR"]) / "DISALLOW_UI_UPLOADS"):
        return Response(status=403)

    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]

    # If the user does not select a file, the browser submits an
    # empty file without a filename.
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400
    if file.content_length >= 30_000_000:  # 30mb?
        return jsonify({"error": "Too large"}), 400

    if file:
        filename = secure_filename(file.filename)
        save_path = os.path.join(tempfile.gettempdir(), filename)
        file.save(save_path)
        return jsonify({"message": "File successfully uploaded", "save_path": save_path}), 200


@app.route("/api/installed_plugins/<filename>", methods=["GET"])
def get_plugin(filename: str):
    """get a specific Python file in the .pioreactor/plugin folder"""
    # security bit: strip out any paths that may be attached, ex: ../../../root/bad
    file = Path(filename).name

    try:
        if Path(file).suffix != ".py":
            raise IOError("must provide a .py file")

        specific_plugin_path = Path(env["DOT_PIOREACTOR"]) / "plugins" / file
        return Response(
            response=specific_plugin_path.read_text(),
            status=200,
            mimetype="text/plain",
        )
    except IOError as e:
        publish_to_log(str(e), "get_plugin")
        return Response(status=404)
    except Exception as e:
        publish_to_error_log(str(e), "get_plugin")
        return Response(status=500)


@app.route("/api/alllow_ui_installs", methods=["GET"])
@cache.memoize(expire=10_000)
def able_to_install_plugins_from_ui():
    if os.path.isfile(Path(env["DOT_PIOREACTOR"]) / "DISALLOW_UI_INSTALLS"):
        return "false"
    else:
        return "true"


@app.route("/api/install_plugin", methods=["POST"])
def install_plugin():
    # there is a security problem here. See https://github.com/Pioreactor/pioreactor/issues/421
    if os.path.isfile(Path(env["DOT_PIOREACTOR"]) / "DISALLOW_UI_INSTALLS"):
        return Response(status=403)

    body = request.get_json()
    plugin_name = body["plugin_name"]

    background_tasks.pios_install_plugin(plugin_name)
    return Response(status=202)


@app.route("/api/uninstall_plugin", methods=["POST"])
def uninstall_plugin():
    body = request.get_json()
    background_tasks.pios_uninstall_plugin(body["plugin_name"])
    return Response(status=202)


## MISC


@app.route("/api/changelog", methods=["GET"])
def get_changelog():
    # not implemented yet

    return Response(status=500)

    try:
        # this is hardcoded and generally sucks
        changelog_path = Path("/usr/local/lib/python3.11/dist-packages/pioreactor/CHANGELOG.md")
        return Response(
            response=changelog_path.read_text(),
            status=200,
            mimetype="text/plain",
            headers={"Cache-Control": "public,max-age=30"},
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_changelog")
        return Response(status=400)


@app.route("/api/contrib/automations/<automation_type>", methods=["GET"])
@cache.memoize(expire=20, tag="plugins")
def get_automation_contrib(automation_type: str):
    # security to prevent possibly reading arbitrary file
    if automation_type not in {"temperature", "dosing", "led"}:
        return Response(status=400)

    try:
        automation_path_default = Path(env["WWW"]) / "contrib" / "automations" / automation_type
        automation_path_plugins = (
            Path(env["DOT_PIOREACTOR"])
            / "plugins"
            / "ui"
            / "contrib"
            / "automations"
            / automation_type
        )
        files = sorted(automation_path_default.glob("*.y*ml")) + sorted(
            automation_path_plugins.glob("*.y*ml")
        )

        # we dedup based on 'automation_name'.
        parsed_yaml = {}
        for file in files:
            try:
                decoded_yaml = yaml_decode(file.read_bytes(), type=structs.AutomationDescriptor)
                parsed_yaml[decoded_yaml.automation_name] = decoded_yaml
            except (ValidationError, DecodeError) as e:
                publish_to_error_log(
                    f"Yaml error in {Path(file).name}: {e}", "get_automation_contrib"
                )

        return Response(
            response=json_encode(list(parsed_yaml.values())),
            status=200,
            mimetype="application/json",
            headers={"Cache-Control": "public,max-age=6"},
        )
    except Exception as e:
        publish_to_error_log(str(e), "get_automation_contrib")
        return Response(status=400)


@app.route("/api/contrib/jobs", methods=["GET"])
@cache.memoize(expire=20, tag="plugins")
def get_job_contrib():
    try:
        job_path_default = Path(env["WWW"]) / "contrib" / "jobs"
        job_path_plugins = Path(env["DOT_PIOREACTOR"]) / "plugins" / "ui" / "contrib" / "jobs"
        files = sorted(job_path_default.glob("*.y*ml")) + sorted(job_path_plugins.glob("*.y*ml"))

        # we dedup based on 'job_name'.
        parsed_yaml = {}

        for file in files:
            try:
                decoded_yaml = yaml_decode(file.read_bytes(), type=structs.BackgroundJobDescriptor)
                parsed_yaml[decoded_yaml.job_name] = decoded_yaml
            except (ValidationError, DecodeError) as e:
                publish_to_error_log(f"Yaml error in {Path(file).name}: {e}", "get_job_contrib")

        return Response(
            response=json_encode(list(parsed_yaml.values())),
            status=200,
            mimetype="application/json",
            headers={"Cache-Control": "public,max-age=6"},
        )
    except Exception as e:
        publish_to_error_log(str(e), "get_job_contrib")
        return Response(status=400)


@app.route("/api/contrib/charts", methods=["GET"])
@cache.memoize(expire=20, tag="plugins")
def get_charts_contrib():
    try:
        chart_path_default = Path(env["WWW"]) / "contrib" / "charts"
        chart_path_plugins = Path(env["DOT_PIOREACTOR"]) / "plugins" / "ui" / "contrib" / "charts"
        files = sorted(chart_path_default.glob("*.y*ml")) + sorted(
            chart_path_plugins.glob("*.y*ml")
        )

        # we dedup based on chart 'chart_key'.
        parsed_yaml = {}
        for file in files:
            try:
                decoded_yaml = yaml_decode(file.read_bytes(), type=structs.ChartDescriptor)
                parsed_yaml[decoded_yaml.chart_key] = decoded_yaml
            except (ValidationError, DecodeError) as e:
                publish_to_error_log(f"Yaml error in {Path(file).name}: {e}", "get_charts_contrib")

        return Response(
            response=json_encode(list(parsed_yaml.values())),
            status=200,
            mimetype="application/json",
            headers={"Cache-Control": "public,max-age=6"},
        )
    except Exception as e:
        publish_to_error_log(str(e), "get_charts_contrib")
        return Response(status=400)


@app.route("/api/update_app", methods=["POST"])
def update_app():
    background_tasks.update_app()
    return Response(status=202)


@app.route("/api/update_app_to_develop", methods=["POST"])
def update_app_to_develop():
    background_tasks.update_app_to_develop()
    return Response(status=202)


@app.route("/api/update_app_from_release_archive", methods=["POST"])
def update_app_from_release_archive():
    body = request.get_json()
    release_archive_location = body["release_archive_location"]
    assert release_archive_location.endswith(".zip")
    background_tasks.update_app_from_release_archive(release_archive_location)
    return Response(status=202)


@app.route("/api/versions/app", methods=["GET"])
@cache.memoize(expire=60, tag="app")
def get_app_version():
    result = subprocess.run(
        ["python", "-c", "import pioreactor; print(pioreactor.__version__)"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        publish_to_error_log(result.stdout, "get_app_version")
        publish_to_error_log(result.stderr, "get_app_version")
        return Response(status=500)
    return Response(
        response=result.stdout.strip(),
        status=200,
        mimetype="text/plain",
        headers={"Cache-Control": "public,max-age=6"},
    )


@app.route("/api/versions/ui", methods=["GET"])
def get_ui_version():
    return VERSION


@app.route("/api/cluster_time", methods=["GET"])
def get_custer_time():
    result = background_tasks.get_time()
    timestamp = result(blocking=True, timeout=5)
    return Response(
        response=timestamp,
        status=200,
        mimetype="text/plain",
    )


@app.route("/api/cluster_time", methods=["POST"])
def set_cluster_time():
    # body = request.get_json()

    # timestamp = body["timestamp"]
    # not implemented
    return 500


@app.route("/api/export_datasets", methods=["POST"])
def export_datasets():
    body = request.get_json()

    cmd_tables = sum(
        [
            ["--tables", table_name]
            for (table_name, exporting) in body["datasetCheckbox"].items()
            if exporting
        ],
        [],
    )
    experiment_name = body["experimentSelection"]

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    if experiment_name == "<All experiments>":
        experiment_options = []
        filename = f"export_{timestamp}.zip"
    else:
        experiment_options = ["--experiment", experiment_name]

        _experiment_name = experiment_name
        chars = "\\`*_{}[]()>#+-.!$"
        for c in chars:
            _experiment_name = _experiment_name.replace(c, "_")

        filename = f"export_{_experiment_name}_{timestamp}.zip"

    filename_with_path = Path("/var/www/pioreactorui/static/exports") / filename
    result = background_tasks.pio(
        "run",
        "export_experiment_data",
        "--output",
        filename_with_path.as_posix(),
        *cmd_tables,
        *experiment_options,
    )
    try:
        status, msg = result(blocking=True, timeout=5 * 60)
    except HueyException:
        status, msg = False, "Timed out on export."
        publish_to_error_log(msg, "export_datasets")
        return {"result": status, "filename": None, "msg": msg}, 500

    if not status:
        publish_to_error_log(msg, "export_datasets")
        return {"result": status, "filename": None, "msg": msg}, 500

    return {"result": status, "filename": filename, "msg": msg}, 200


@app.route("/api/experiments", methods=["GET"])
@cache.memoize(expire=60, tag="experiments")
def get_experiments():
    try:
        response = jsonify(
            query_db(
                "SELECT experiment, created_at, description FROM experiments ORDER BY created_at DESC;"
            )
        )
        return response

    except Exception as e:
        publish_to_error_log(str(e), "get_experiments")
        return Response(status=500)


@app.route("/api/experiments", methods=["POST"])
def create_experiment():
    cache.evict("experiments")
    cache.evict("unit_labels")

    body = request.get_json()
    proposed_experiment_name = body["experiment"]

    if not proposed_experiment_name:
        return Response(status=404)
    elif proposed_experiment_name.lower() == "current":  # too much API rework
        return Response(status=404)
    elif (
        ("#" in proposed_experiment_name)
        or ("+" in proposed_experiment_name)
        or ("/" in proposed_experiment_name)
    ):
        return Response(status=404)

    try:
        modify_db(
            "INSERT INTO experiments (created_at, experiment, description, media_used, organism_used) VALUES (?,?,?,?,?)",
            (
                current_utc_timestamp(),
                body["experiment"],
                body.get("description"),
                body.get("mediaUsed"),
                body.get("organismUsed"),
            ),
        )
        publish_to_log(
            f"New experiment created: {body['experiment']}", "create_experiment", level="INFO"
        )
        return Response(status=201)

    except sqlite3.IntegrityError:
        return Response(status=409)
    except Exception as e:
        publish_to_error_log(str(e), "create_experiment")
        return Response(status=500)


@app.route("/api/experiments/latest", methods=["GET"])
@cache.memoize(expire=30, tag="experiments")
def get_latest_experiment():
    try:
        return Response(
            response=json_encode(
                query_db(
                    "SELECT experiment, created_at, description, media_used, organism_used, delta_hours FROM latest_experiment",
                    one=True,
                )
            ),
            status=200,
            headers={
                "Cache-Control": "public,max-age=2"
            },  # don't make this too high, as it caches description, which changes fast.
            mimetype="application/json",
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_latest_experiment")
        return Response(status=500)


@app.route("/api/unit_labels/<experiment>", methods=["GET"])
@cache.memoize(expire=30, tag="unit_labels")
def get_unit_labels(experiment):
    try:
        if experiment == "current":
            unit_labels = query_db(
                "SELECT r.pioreactor_unit as unit, r.label FROM pioreactor_unit_labels AS r JOIN latest_experiment USING (experiment);"
            )
        else:
            unit_labels = query_db(
                "SELECT r.pioreactor_unit as unit, r.label FROM pioreactor_unit_labels as r WHERE experiment=?;",
                (experiment,),
            )

        keyed_by_unit = {d["unit"]: d["label"] for d in unit_labels}

        return Response(
            response=json_encode(keyed_by_unit),
            status=200,
            headers={"Cache-Control": "public,max-age=6"},
            mimetype="application/json",
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_unit_labels")
        return Response(status=500)


@app.route("/api/unit_labels/current", methods=["PUT"])
def upsert_current_unit_labels():
    """
    Update or insert a new unit label for the current experiment.

    This API endpoint accepts a PUT request with a JSON body containing a "unit" and a "label".
    The "unit" is the identifier for the pioreactor and the "label" is the desired label for that unit.
    If the unit label for the current experiment already exists, it will be updated; otherwise, a new entry will be created.

    The response will be a status code of 201 if the operation is successful, and 400 if there was an error.


    JSON Request Body:
    {
        "unit": "<unit_identifier>",
        "label": "<new_label>"
    }

    Example usage:
    PUT /api/unit_labels/current
    {
        "unit": "unit1",
        "label": "new_label"
    }

    Returns:
    HTTP Response with status code 201 if successful, 400 if there was an error.

    Raises:
    Exception: Any error encountered during the database operation is published to the error log.
    """
    cache.evict("unit_labels")

    body = request.get_json()

    unit = body["unit"]
    label = body["label"]

    latest_experiment_dict = query_db("SELECT experiment FROM latest_experiment", one=True)

    latest_experiment = latest_experiment_dict["experiment"]

    try:
        modify_db(
            "INSERT OR REPLACE INTO pioreactor_unit_labels (label, experiment, pioreactor_unit, created_at) VALUES ((?), (?), (?), strftime('%Y-%m-%dT%H:%M:%S', datetime('now')) ) ON CONFLICT(experiment, pioreactor_unit) DO UPDATE SET label=excluded.label, created_at=strftime('%Y-%m-%dT%H:%M:%S', datetime('now'))",
            (label, latest_experiment, unit),
        )

    except Exception as e:
        publish_to_error_log(str(e), "upsert_current_unit_labels")
        return Response(status=400)

    return Response(status=201)


@app.route("/api/historical_organisms", methods=["GET"])
def get_historical_organisms_used():
    try:
        historical_organisms = query_db(
            'SELECT DISTINCT organism_used as key FROM experiments WHERE NOT (organism_used IS NULL OR organism_used == "") ORDER BY created_at DESC;'
        )

    except Exception as e:
        publish_to_error_log(str(e), "historical_organisms")
        return Response(status=500)

    return jsonify(historical_organisms)


@app.route("/api/historical_media", methods=["GET"])
def get_historical_media_used():
    try:
        historical_media = query_db(
            'SELECT DISTINCT media_used as key FROM experiments WHERE NOT (media_used IS NULL OR media_used == "") ORDER BY created_at DESC;'
        )

    except Exception as e:
        publish_to_error_log(str(e), "historical_media")
        return Response(status=500)

    return jsonify(historical_media)


@app.route("/api/experiments/<experiment>", methods=["PATCH"])
def update_experiment(experiment):
    cache.evict("experiments")

    body = request.get_json()
    try:
        if "description" in body:
            modify_db(
                "UPDATE experiments SET description = (?) WHERE experiment=(?)",
                (body["description"], experiment),
            )

        return Response(status=200)

    except Exception as e:
        publish_to_error_log(str(e), "update_experiment")
        return Response(status=500)


@app.route("/api/setup_worker_pioreactor", methods=["POST"])
def setup_worker_pioreactor():
    new_name = request.get_json()["newPioreactorName"]
    try:
        result = background_tasks.add_new_pioreactor(new_name)
    except Exception as e:
        publish_to_error_log(str(e), "setup_worker_pioreactor")
        return {"msg": str(e)}, 500

    try:
        status, msg = result(blocking=True, timeout=250)
    except HueyException:
        status, msg = False, "Timed out, see logs."

    if status:
        return Response(status=202)
    else:
        publish_to_error_log(msg, "setup_worker_pioreactor")
        return {"msg": msg}, 500


## CONFIG CONTROL


@app.route("/api/configs/<filename>", methods=["GET"])
@cache.memoize(expire=30, tag="config")
def get_config(filename: str):
    """get a specific config.ini file in the .pioreactor folder"""

    # security bit: strip out any paths that may be attached, ex: ../../../root/bad
    filename = Path(filename).name

    try:
        assert Path(filename).suffix == ".ini"

        specific_config_path = Path(env["DOT_PIOREACTOR"]) / filename
        return Response(
            response=specific_config_path.read_text(),
            status=200,
            mimetype="text/plain",
            headers={"Cache-Control": "public,max-age=6"},
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_config_of_file")
        return Response(status=400)


@app.route("/api/configs", methods=["GET"])
@cache.memoize(expire=60, tag="config")
def get_configs():
    """get a list of all config.ini files in the .pioreactor folder"""
    try:
        config_path = Path(env["DOT_PIOREACTOR"])
        return jsonify([file.name for file in sorted(config_path.glob("config*.ini"))])

    except Exception as e:
        publish_to_error_log(str(e), "get_configs")
        return Response(status=500)


@app.route("/api/configs/<filename>", methods=["DELETE"])
def delete_config(filename):
    cache.evict("config")
    filename = Path(filename).name  # remove any ../../ prefix stuff
    config_path = Path(env["DOT_PIOREACTOR"]) / filename

    background_tasks.rm(config_path)
    publish_to_log(f"Deleted config {filename}.", "delete_config")
    return Response(status=202)


@app.route("/api/configs/<filename>", methods=["PATCH"])
def update_config(filename):
    """if the config file is unit specific, we only need to run sync-config on that unit."""
    cache.evict("config")
    body = request.get_json()
    code = body["code"]

    if not filename.endswith(".ini"):
        return {"msg": "Incorrect filetype. Must be .ini."}, 400

    # security bit:
    # users could have filename look like ../../../../root/bad.txt
    # the below code will strip any paths.
    # General security risk here is ability to save arbitrary file to OS.
    filename = Path(filename).name

    # is the user editing a worker config or the global config?
    regex = re.compile(r"config_?(.*)?\.ini")
    if regex.match(filename)[1] != "":
        units = regex.match(filename)[1]
        flags = "--specific"
    else:
        units = "$broadcast"
        flags = "--shared"

    # General security risk here to save arbitrary file to OS.
    config_path = Path(env["DOT_PIOREACTOR"]) / filename

    # can the config actually be read? ex. no repeating sections, typos, etc.
    # filename is a string
    config = configparser.ConfigParser(allow_no_value=True)

    try:
        config.read_string(code)  # test parser

        # if editing config.ini (not a unit specific)
        # test to make sure we have minimal code to run pio commands
        if filename == "config.ini":
            assert config["cluster.topology"]
            assert config.get("cluster.topology", "leader_hostname")
            assert config.get("cluster.topology", "leader_address")

    except configparser.DuplicateSectionError as e:
        msg = f"Duplicate section [{e.section}] was found. Please fix and try again."
        publish_to_error_log(msg, "update_config")
        return {"msg": msg}, 400
    except configparser.DuplicateOptionError as e:
        msg = f"Duplicate option, `{e.option}`, was found in section [{e.section}]. Please fix and try again."
        publish_to_error_log(msg, "update_config")
        return {"msg": msg}, 400
    except configparser.ParsingError:
        msg = "Incorrect syntax. Please fix and try again."
        publish_to_error_log(msg, "update_config")
        return {"msg": msg}, 400
    except (AssertionError, configparser.NoSectionError, KeyError, TypeError):
        msg = "Missing required field(s) in [cluster.topology]: `leader_hostname` and/or `leader_address`. Please fix and try again."
        publish_to_error_log(msg, "update_config")
        return {"msg": msg}, 400
    except ValueError as e:
        msg = f"Error: {e}"
        publish_to_error_log(msg, "update_config")
        return {"msg": msg}, 400
    except Exception as e:
        publish_to_error_log(str(e), "update_config")
        msg = "Hm, something went wrong, check PioreactorUI logs."
        return {"msg": msg}, 500

    result = background_tasks.write_config_and_sync(config_path, code, units, flags)

    try:
        status, msg_or_exception = result(blocking=True, timeout=75)
    except HueyException:
        status, msg_or_exception = False, "sync-configs timed out."

    if not status:
        publish_to_error_log(msg_or_exception, "save_new_config")
        return {"msg": str(msg_or_exception)}, 500

    return Response(status=202)


@app.route("/api/historical_configs/<filename>", methods=["GET"])
@cache.memoize(expire=60, tag="config")
def get_historical_config_for(filename: str):
    try:
        configs_for_filename = query_db(
            "SELECT filename, timestamp, data FROM config_files_histories WHERE filename=? ORDER BY timestamp DESC",
            (filename,),
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_historical_config_for")
        return Response(status=400)

    return jsonify(configs_for_filename)


@app.route("/api/is_local_access_point_active", methods=["GET"])
@cache.memoize(expire=10_000)
def is_local_access_point_active():
    if os.path.isfile("/boot/firmware/local_access_point"):
        return "true"
    else:
        return "false"


### experiment profiles


@app.route("/api/contrib/experiment_profiles", methods=["POST"])
def create_experiment_profile():
    body = request.get_json()
    experiment_profile_body = body["body"]
    experiment_profile_filename = Path(body["filename"]).name

    # verify content
    try:
        yaml_decode(experiment_profile_body, type=structs.Profile)
    except Exception as e:
        msg = f"{e}"
        publish_to_error_log(msg, "create_experiment_profile")
        return {"msg": msg}, 400

    # verify file
    try:
        assert is_valid_unix_filename(experiment_profile_filename)
        assert experiment_profile_filename.endswith(
            ".yaml"
        ) or experiment_profile_filename.endswith(".yml")
    except Exception:
        msg = "Invalid filename"
        publish_to_error_log(msg, "create_experiment_profile")
        return {"msg": msg}, 400

    # save file to disk
    background_tasks.save_file(
        Path(env["DOT_PIOREACTOR"]) / "experiment_profiles" / experiment_profile_filename,
        experiment_profile_body,
    )

    return Response(status=200)


@app.route("/api/contrib/experiment_profiles", methods=["GET"])
def get_experiment_profiles():
    try:
        profile_path = Path(env["DOT_PIOREACTOR"]) / "experiment_profiles"
        files = sorted(profile_path.glob("*.y*ml"), key=os.path.getmtime, reverse=True)

        parsed_yaml = []
        for file in files:
            try:
                profile = yaml_decode(file.read_bytes(), type=structs.Profile)
                parsed_yaml.append({"experimentProfile": profile, "file": str(file)})
            except (ValidationError, DecodeError) as e:
                publish_to_error_log(
                    f"Yaml error in {Path(file).name}: {e}", "get_experiment_profiles"
                )

        return Response(
            response=json_encode(parsed_yaml),
            status=200,
            mimetype="application/json",
        )
    except Exception as e:
        publish_to_error_log(str(e), "get_experiment_profiles")
        return Response(status=400)


@app.route("/api/contrib/experiment_profiles/<filename>", methods=["GET"])
def get_experiment_profile(filename: str):
    file = Path(filename).name
    try:
        if not (Path(file).suffix == ".yaml" or Path(file).suffix == ".yml"):
            raise IOError("must provide a YAML file")

        specific_profile_path = Path(env["DOT_PIOREACTOR"]) / "experiment_profiles" / file
        return Response(
            response=specific_profile_path.read_text(),
            status=200,
            mimetype="text/plain",
        )
    except IOError as e:
        publish_to_log(str(e), "get_experiment_profile")
        return Response(status=404)
    except Exception as e:
        publish_to_error_log(str(e), "get_experiment_profile")
        return Response(status=500)


@app.route("/api/contrib/experiment_profiles/<filename>", methods=["DELETE"])
def delete_experiment_profile(filename: str):
    file = Path(filename).name
    try:
        if not (Path(file).suffix == ".yaml" or Path(file).suffix == ".yml"):
            raise IOError("must provide a YAML file")

        specific_profile_path = Path(env["DOT_PIOREACTOR"]) / "experiment_profiles" / file
        background_tasks.rm(specific_profile_path)
        publish_to_log(f"Deleted profile {filename}.", "delete_experiment_profile")
        return Response(status=200)
    except IOError as e:
        publish_to_log(str(e), "delete_experiment_profile")
        return Response(status=404)
    except Exception as e:
        publish_to_error_log(str(e), "delete_experiment_profile")
        return Response(status=500)


### FLASK META VIEWS


@app.errorhandler(404)
def not_found(e):
    try:
        return app.send_static_file("index.html")
    except Exception:
        return Response(status=404)


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()
