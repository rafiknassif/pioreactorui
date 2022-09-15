# -*- coding: utf-8 -*-
from __future__ import annotations

import glob
import os
import re
import sqlite3
import subprocess

from flask import g
from flask import jsonify
from flask import request
from flask import Response
from yaml import CLoader as Loader  # type: ignore
from yaml import load as yaml_load  # type: ignore

import tasks
from app import app
from app import client
from app import config
from app import insert_into_db
from app import publish_to_error_log
from app import query_db
from app import logger

## PIOREACTOR CONTROL


@app.route("/api/stop_all", methods=["POST"])
def stop_all():
    """Kills all jobs"""
    result = subprocess.run(["pios", "kill", "--all-jobs", "-y"], capture_output=True)

    if result.returncode != 0:
        publish_to_error_log(result.stdout, "stop_all")
        publish_to_error_log(result.stderr, "stop_all")
        return Response(status=500)

    return Response(status=200)


@app.route("/api/stop/<job>/<unit>", methods=["POST"])
def stop_job_on_unit(job, unit):
    """Kills specified job on unit"""

    jobs_to_kill_over_MQTT = ["add_media", "add_alt_media", "remove_waste"]

    if job in jobs_to_kill_over_MQTT:
        client.publish(f"pioreactor/{unit}/$experiment/{job}/$state/set", "disconnected", qos=2)

    else:
        result = subprocess.run(["pios", "kill", job, "-y", "--units", unit], capture_output=True)

        if result.returncode != 0:
            publish_to_error_log(result.stdout, "stop_all")
            publish_to_error_log(result.stderr, "stop_all")
            return Response(status=500)

    return Response(status=200)


@app.route("/api/run/<job>/<unit>", methods=["POST"])
def run_job_on_unit(job, unit):
    """Runs specified job on unit"""

    # client = connection to mqtt server

    json_string = request.body

    client.publish(f"pioreactor/{unit}/$experiment/run/{job}", json_string, qos=2)

    return Response(status=200)


@app.route("/api/reboot/<unit>", methods=["POST"])
def reboot_unit(unit):
    """Reboots unit"""  # should return a 0
    result = subprocess.run(["pios", "reboot", "-y", "--units", unit], capture_output=True)

    if result.returncode != 0:
        publish_to_error_log(result.stdout, "reboot")
        publish_to_error_log(result.stderr, "reboot")
        return Response(status=500)

    return Response(status=200)


## DATA FOR CARDS ON OVERVIEW


@app.route("/api/recent_logs", methods=["GET"])
def recent_logs():
    """Shows event logs from all units"""
    args = request.args
    if "min_level" in args:
        min_level = args["min_level"]
    else:
        min_level = "INFO"

    if min_level == "DEBUG":
        level_string = '(level == "ERROR" or level == "WARNING" or level == "NOTICE" or level == "INFO" or level == "DEBUG")'
    elif min_level == "INFO":
        level_string = (
            '(level == "ERROR" or level == "NOTICE" or level == "INFO" or level == "WARNING")'
        )
    elif min_level == "WARNING":
        level_string = '(level == "ERROR" or level == "WARNING")'
    elif min_level == "ERROR":
        level_string = '(level == "ERROR")'
    else:
        level_string = (
            '(level == "ERROR" or level == "NOTICE" or level == "INFO" or level == "WARNING")'
        )

    try:
        recent_logs = query_db(
            f"SELECT l.timestamp, level=='ERROR'as is_error, level=='WARNING' as is_warning, level=='NOTICE' as is_notice, l.pioreactor_unit, message, task FROM logs AS l LEFT JOIN latest_experiment AS le ON (le.experiment = l.experiment OR l.experiment=?) WHERE {level_string} AND l.timestamp >= MAX(strftime('%Y-%m-%dT%H:%M:%S', datetime('now', '-24 hours')), le.created_at) ORDER BY l.timestamp DESC LIMIT 50;",
            ("'$experiment'",),
        )
    except Exception as e:
        publish_to_error_log(str(e), "recent_logs")
        return Response(status=500)

    return jsonify(recent_logs)


@app.route("/api/time_series/growth_rates/<experiment>", methods=["GET"])
def growth_rates(experiment):
    """Gets growth rates for all units"""
    args = request.args
    filter_mod_n = args.get("filter_mod_n", 100)

    try:
        growth_rates = query_db(
            "SELECT json_object('series', json_group_array(unit), 'data', json_group_array(json(data))) as result FROM (SELECT pioreactor_unit as unit, json_group_array(json_object('x', timestamp, 'y', round(rate, 5))) as data FROM growth_rates WHERE experiment=? AND ((ROWID * 0.61803398875) - cast(ROWID * 0.61803398875 as int) < 1.0/?) GROUP BY 1);",
            (experiment, filter_mod_n),
            one=True,
        )

    except Exception as e:
        publish_to_error_log(str(e), "growth_rates")
        return Response(status=400)

    return growth_rates["result"]


@app.route("/api/time_series/temperature_readings/<experiment>", methods=["GET"])
def temperature_readings(experiment):
    """Gets temperature readings for all units"""
    args = request.args
    filter_mod_n = args.get("filter_mod_n", 100)

    try:
        temperature_readings = query_db(
            "SELECT json_object('series', json_group_array(unit), 'data', json_group_array(json(data))) as result FROM (SELECT pioreactor_unit as unit, json_group_array(json_object('x', timestamp, 'y', round(temperature_c, 2))) as data FROM temperature_readings WHERE experiment=? AND ((ROWID * 0.61803398875) - cast(ROWID * 0.61803398875 as int) < 1.0/?) GROUP BY 1);",
            (experiment, filter_mod_n),
            one=True,
        )

    except Exception as e:
        publish_to_error_log(str(e), "temperature_readings")
        return Response(status=400)

    return temperature_readings["result"]


@app.route("/api/time_series/od_readings_filtered/<experiment>", methods=["GET"])
def od_readings_filtered(experiment):
    """Gets normalized od for all units"""
    args = request.args
    filter_mod_n = args.get("filter_mod_n", 100)
    lookback = float(args.get("lookback", 4))

    try:
        filtered_od_readings = query_db(
            "SELECT json_object('series', json_group_array(unit), 'data', json_group_array(json(data))) as result FROM (SELECT pioreactor_unit as unit, json_group_array(json_object('x', timestamp, 'y', round(normalized_od_reading, 7))) as data FROM od_readings_filtered WHERE experiment=? AND ((ROWID * 0.61803398875) - cast(ROWID * 0.61803398875 as int) < 1.0/?) AND timestamp > strftime('%Y-%m-%dT%H:%M:%S', datetime('now',?)) GROUP BY 1);",
            (experiment, filter_mod_n, f"-{lookback} hours"),
            one=True,
        )

    except Exception as e:
        publish_to_error_log(str(e), "od_readings_filtered")
        return Response(status=400)

    return filtered_od_readings["result"]


@app.route("/api/time_series/od_readings/<experiment>", methods=["GET"])
def od_readings(experiment):
    """Gets raw od for all units"""
    args = request.args
    filter_mod_n = args.get("filter_mod_n", 100)
    lookback = float(args.get("lookback", 4))

    try:
        raw_od_readings = query_db(
            "SELECT json_object('series', json_group_array(unit), 'data', json_group_array(json(data))) as result FROM (SELECT pioreactor_unit || '-' || channel as unit, json_group_array(json_object('x', timestamp, 'y', round(od_reading, 7))) as data FROM od_readings WHERE experiment=? AND ((ROWID * 0.61803398875) - cast(ROWID * 0.61803398875 as int) < 1.0/?) and timestamp > strftime('%Y-%m-%dT%H:%M:%S', datetime('now', ?)) GROUP BY 1);",
            (experiment, filter_mod_n, f"-{lookback} hours"),
            one=True,
        )

    except Exception as e:
        publish_to_error_log(str(e), "od_readings")
        return Response(status=400)

    return raw_od_readings["result"]


@app.route("/api/time_series/alt_media_fraction/<experiment>", methods=["GET"])
def alt_media_fraction(experiment):
    """get fraction of alt media added to vial"""

    try:
        alt_media_fraction_ = query_db(
            "SELECT json_object('series', json_group_array(unit), 'data', json_group_array(json(data))) as result FROM (SELECT pioreactor_unit as unit, json_group_array(json_object('x', timestamp, 'y', round(alt_media_fraction, 7))) as data FROM alt_media_fractions WHERE experiment=? GROUP BY 1);",
            (experiment,),
            one=True,
        )

    except Exception as e:
        publish_to_error_log(str(e), "alt_media_fractions")
        return Response(status=400)

    return alt_media_fraction_["result"]


@app.route("/api/recent_media_rates", methods=["GET"])
def recent_media_rates():
    """Shows amount of added media per unit"""
    ## this one confusing
    hours = 3

    try:
        rows = query_db(
            "SELECT d.pioreactor_unit, SUM(CASE WHEN event='add_media' THEN volume_change_ml ELSE 0 END) / ? AS media_rate, SUM(CASE WHEN event='add_alt_media' THEN volume_change_ml ELSE 0 END) / ? AS alt_media_rate FROM dosing_events AS d JOIN latest_experiment USING (experiment) WHERE datetime(d.timestamp) >= datetime('now', '-? hours') AND event IN ('add_alt_media', 'add_media') AND source_of_event LIKE 'dosing_automation%' GROUP BY d.pioreactor_unit;",
            (hours, hours),
        )

        json_result = {}
        aggregate = {"altMediaRate": 0.0, "mediaRate": 0.0}
        for row in rows:
            json_result[row["pioreactor_unit"]] = {
                "alt_media_rate": row["alt_media_rate"],
                "media_rate": row["media_rate"],
            }
            aggregate["media_rate"] = aggregate["media_rate"] + row["media_rate"]
            aggregate["alt_media_rate"] = aggregate["alt_media_rate"] + row["alt_media_rate"]

        json_result["all"] = aggregate
        return jsonify(json_result)

    except Exception as e:
        publish_to_error_log(str(e), "recent_media_rates")
        return Response(status=400)


## CALIBRATIONS


@app.route("/api/calibrations/<pioreactor_unit>/<calibration_type>", methods=["GET"])
def get_unit_calibrations(pioreactor_unit, calibration_type):

    try:
        unit_calibration = query_db(
            "SELECT * FROM calibrations WHERE type=? AND pioreactor_unit=?",
            (calibration_type, pioreactor_unit),
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_unit_calibrations")
        return Response(status=400)

    return jsonify(unit_calibration)


## PLUGINS


@app.route("/api/get_installed_plugins", methods=["GET"])
def list_installed_plugins():

    result = subprocess.run(["pio", "list-plugins", "--json"], capture_output=True)

    if result.returncode != 0:
        publish_to_error_log(str(result.stdout), "get_installed_plugins")
        publish_to_error_log(str(result.stderr), "get_installed_plugins")
        return []

    else:
        return result.stdout


@app.route("/api/install_plugin", methods=["POST"])
def install_plugin():

    body = request.get_json()

    result = subprocess.run(["pios", "install-plugin", body["plugin_name"]], capture_output=True)

    if result.returncode != 0:
        publish_to_error_log(result.stdout, "install_plugin")
        publish_to_error_log(result.stderr, "install_plugin")
        return Response(status=500)

    return Response(status=200)


@app.route("/api/uninstall_plugin", methods=["POST"])
def uninstall_plugin():

    body = request.get_json()  # dictionary of data that the client sends

    result = subprocess.run(["pios", "uninstall-plugin", body["plugin_name"]], capture_output=True)

    if result.returncode != 0:
        publish_to_error_log(result.stdout, "uninstall-plugin")
        publish_to_error_log(result.stderr, "uninstall_plugin")
        return Response(status=500)

    return Response(status=200)


## MISC


@app.route("/api/contrib/automations/<automation_type>", methods=["GET"])
def get_automation_contrib(automation_type):
    """TODO: dosing doesn't do anything..."""
    try:
        automation_path = os.path.join(config["CONTRIB_FOLDER"], "automations", automation_type)

        files = sorted(
            glob.glob(automation_path + "/*.y[a]ml")
        )  # list of strings, where strings rep. paths to  yaml files

        automations = []  # list of dict

        for file in files:
            with open(file, "rb") as file_stream:
                automations.append(
                    yaml_load(file_stream.read(), Loader=Loader)
                )  # read returns string, safe_load converts to python object == dict

        return jsonify(automations)

    except Exception as e:
        publish_to_error_log(str(e), "get_automation_contrib")
        return Response(status=400)


@app.route("/api/contrib/jobs", methods=["GET"])
def get_job_contrib():
    try:
        job_path = os.path.join(config["CONTRIB_FOLDER"], "jobs")

        files = sorted(
            glob.glob(job_path + "/*.y[a]ml")
        )  # list of strings, where strings rep. paths to  yaml files

        jobs = []  # list of dict

        for file in files:
            with open(file, "rb") as file_stream:
                jobs.append(
                    yaml_load(file_stream.read(), Loader=Loader)
                )  # read returns string, safe_load converts to python object == dict

        return jsonify(jobs)

    except Exception as e:
        publish_to_error_log(str(e), "get_job_contrib")
        return Response(status=400)


@app.route("/api/update_app", methods=["POST"])
def update_app():
    tasks.update_app()
    return 200


@app.route("/api/get_app_version", methods=["GET"])
def get_app_version():
    result = subprocess.run(
        ["python", "-c", "import pioreactor; print(pioreactor.__version__)"], capture_output=True
    )
    if result.returncode != 0:
        publish_to_error_log(result.stdout, "get_app_version")
        publish_to_error_log(result.stderr, "get_app_version")
        return Response(status=500)
    return result.stdout.strip()


@app.route("/api/export_datasets", methods=["POST"])
def export_datasets():
    return


@app.route("/api/get_experiments", methods=["GET"])
def get_experiments():
    try:
        experiments = query_db(
            "SELECT experiment, created_at, description FROM experiments ORDER BY created_at DESC;"
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_experiments")
        return Response(status=400)

    return jsonify(experiments)


@app.route("/api/get_latest_experiment", methods=["GET"])
def get_latest_experiment():
    try:
        latest_experiment = query_db(
            "SELECT experiment, created_at, description, media_used, organism_used, delta_hours FROM latest_experiment",
            one=True,
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_latest_experiment")
        return Response(status=400)

    return jsonify(latest_experiment)


@app.route("/api/get_current_unit_labels", methods=["GET"])
def get_current_unit_labels():
    try:
        current_unit_labels = query_db(
            "SELECT r.pioreactor_unit as unit, r.label FROM pioreactor_unit_labels AS r JOIN latest_experiment USING (experiment);"
        )

        keyed_by_unit = {d["unit"]: d["label"] for d in current_unit_labels}

        return jsonify(keyed_by_unit)

    except Exception as e:
        publish_to_error_log(str(e), "get_current_unit_labels")
        return Response(status=400)


@app.route("/api/update_current_unit_labels", methods=["POST"])
def update_current_unit_labels():

    body = request.get_json()

    unit = body["unit"]
    label = body["label"]

    latest_experiment_dict = query_db("SELECT experiment FROM latest_experiment", one=True)

    latest_experiment = latest_experiment_dict["experiment"]

    try:
        insert_into_db(
            "INSERT OR REPLACE INTO pioreactor_unit_labels (label, experiment, pioreactor_unit, created_at) VALUES ((?), (?), (?), strftime('%Y-%m-%dT%H:%M:%S', datetime('now')) ) ON CONFLICT(experiment, pioreactor_unit) DO UPDATE SET label=excluded.label, created_at=strftime('%Y-%m-%dT%H:%M:%S', datetime('now'))",
            (label, latest_experiment, unit),
        )

    except Exception as e:
        publish_to_error_log(str(e), "update_current_unit_labels")
        return Response(status=400)

    # client.publish(f"pioreactor/{unit}/{latest_experiment}/unit_label", label, retain=True)

    return Response(status=200)


@app.route("/api/get_historical_organisms_used", methods=["GET"])
def get_historical_organisms_used():
    try:
        historical_organisms = query_db(
            'SELECT DISTINCT organism_used as key FROM experiments WHERE NOT (organism_used IS NULL OR organism_used == "") ORDER BY created_at DESC;'
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_historical_organisms_used_used")
        return Response(status=400)

    return jsonify(historical_organisms)


@app.route("/api/get_historical_media_used", methods=["GET"])
def get_historical_media_used():
    try:
        historical_media = query_db(
            'SELECT DISTINCT media_used as key FROM experiments WHERE NOT (media_used IS NULL OR media_used == "") ORDER BY created_at DESC;'
        )

    except Exception as e:
        publish_to_error_log(str(e), "get_historical_organisms_used_used")
        return Response(status=400)

    return jsonify(historical_media)


@app.route("/api/create_experiment", methods=["POST"])
def create_experiment():

    body = request.get_json()

    try:
        insert_into_db(
            "INSERT INTO experiments (created_at, experiment, description, media_used, organism_used) VALUES (?,?,?,?,?)",
            (
                body["created_at"],
                body["experiment"],
                body.get("description"),
                body.get("media_used"),
                body.get("organism_used"),
            ),
        )

        client.publish("pioreactor/latest_experiment", body["experiment"], qos=2, retain=True)
        return Response(status=200)

    except sqlite3.IntegrityError as e:
        publish_to_error_log(str(e), "create_experiment")
        return Response(status=400)
    except Exception as e:
        print(e)
        publish_to_error_log(str(e), "create_experiment")
        return Response(status=400)


@app.route("/api/update_experiment_desc", methods=["POST"])
def update_experiment_description():
    body = request.get_json()
    try:
        insert_into_db(
            "UPDATE experiments SET description = (?) WHERE experiment=(?)",
            (body["description"], body["experiment"]),
        )
        return Response(status=200)

    except Exception as e:
        publish_to_error_log(str(e), "update_experiment_desc")
        return Response(status=500)


@app.route("/api/add_new_pioreactor", methods=["POST"])
def add_new_pioreactor():


    new_name = request.get_json()["newPioreactorName"]
    try:
        result = tasks.add_new_pioreactor(new_name)
    except Exception as e:
        print(e)
        logger.error(str(e))
        return {"msg": e}, Response(status=500)

    try:
        status, msg = result(blocking=True, timeout=30)
    except Exception:
        status, msg = False, "Timed out"

    if status:
        return Response(status=200)
    else:
        publish_to_error_log(msg, "add_new_pioreactor")
        return {"msg": msg}, 500


## CONFIG CONTROL


@app.route("/api/get_config/<filename>", methods=["GET"])
def get_config_of_file(filename):
    """get a specific config.ini file in the .pioreactor folder"""
    try:
        specific_config_path = os.path.join(config["CONFIG_INI_FOLDER"], filename)

        with open(specific_config_path) as file_stream:
            return file_stream.read()

    except Exception as e:
        publish_to_error_log(str(e), "get_config_of_file")
        return Response(status=400)


@app.route("/api/get_configs", methods=["GET"])
def get_list_all_configs():
    """get a list of all config.ini files in the .pioreactor folder"""
    try:
        config_path = config["CONFIG_INI_FOLDER"]

        list_config_files = []

        for file in os.listdir(config_path):
            if file.endswith(".ini"):
                list_config_files.append(file)

        return list_config_files

    except Exception as e:
        publish_to_error_log(str(e), "get_list_all_contrib")
        return Response(status=400)


@app.route("/api/delete_config", methods=["POST"])
def delete_config():
    """TODO: make this http DELETE"""

    body = request.get_json()

    config_path = os.path.join(
        config["CONFIG_INI_FOLDER"], body["filename"]
    )  # where is this filename coming from?

    result = subprocess.run(["rm", config_path], capture_output=True)

    if result.returncode != 0:
        publish_to_error_log(result.stdout, "delete_config")
        publish_to_error_log(result.stderr, "delete_config")
        return Response(status=400)

    else:
        return Response(status=200)


@app.route("/api/save_new_config", methods=["POST"])
def save_new_config():
    """if the config file is unit specific, we only need to run sync-config on that unit."""

    body = request.get_json()

    regex = re.compile(r"/config_?(.*)?\.ini/")
    filename = body["filename"]

    if regex.match(filename)[1] != "":
        units = regex.match(filename)[1]
        flags = ["--specific"]

    else:
        units = "$broadcast"
        flags = ["--shared"]

    try:
        config_path = os.path.join(config["CONFIG_INI_FOLDER"], body["filename"])

        with open(config_path, "w") as f:
            f.write(body["code"])

    except Exception as e:
        publish_to_error_log(str(e), "save_new_config")
        return Response(status=500)

    result = subprocess.run(["pios", "sync-configs", "--units", units] + flags, capture_output=True)

    if result.returncode != 0:
        publish_to_error_log(result.stdout, "save_new_config")
        publish_to_error_log(result.stderr, "save_new_config")
        return Response(status=500)

    return Response(status=200)


@app.errorhandler(404)
def not_found(e):
    try:
        return app.send_static_file("index.html")
    except Exception:
        return "Not found! Missing index.html?", 404


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()
