# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import signal
from logging import handlers
from shlex import join
from subprocess import check_call as run_and_check_call
from subprocess import DEVNULL
from subprocess import Popen
from subprocess import run
from subprocess import STDOUT
from typing import Any

from pioreactor.config import config
from pioreactor.mureq import HTTPException
from pioreactor.pubsub import get_from
from pioreactor.pubsub import post_into
from pioreactor.utils.networking import resolve_to_address

from .config import cache
from .config import CACHE_DIR
from .config import env
from .config import huey
from .config import is_testing_env

# this is a hack to get around us not cleaning up / tracking Popen processes. We effectively ignore
# what they do. Note that since this LOC is at the top of this module, PioreactorUI also is affected by
# it. https://stackoverflow.com/questions/16807603/python-non-blocking-non-defunct-process and https://www.philipson.co.il/post/a-subprocess-bug-nah/
signal.signal(signal.SIGCHLD, signal.SIG_IGN)

logger = logging.getLogger("huey.consumer")
logger.setLevel(logging.INFO)

file_handler = handlers.WatchedFileHandler(
    config.get("logging", "ui_log_file", fallback="/var/log/pioreactor.log")
)
file_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)-2s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
)
logger.addHandler(file_handler)

if not is_testing_env():
    PIO_EXECUTABLE = "/usr/local/bin/pio"
    PIOS_EXECUTABLE = "/usr/local/bin/pios"
else:
    PIO_EXECUTABLE = env.get("PIO_EXECUTABLE")
    PIOS_EXECUTABLE = env.get("PIOS_EXECUTABLE")

ALLOWED_ENV = (
    "EXPERIMENT",
    "JOB_SOURCE",
    "TESTING",
    "HOSTNAME",
    "HARDWARE",
    "ACTIVE",
    "PIO_VERSION",
    "FIRMWARE",
    "ACTIVE",
    "DEBUG",
)


@huey.on_startup()
def initialized():
    logger.info("Starting Huey consumer...")
    logger.info(f"Cache directory = {CACHE_DIR}")


@huey.task()
def pio_run(*args: str, env: dict[str, str] = {}) -> bool:
    # for long running pio run jobs where we don't care about the output / status
    command = ("nohup", PIO_EXECUTABLE, "run") + args
    env = {k: v for k, v in (env or {}).items() if k in ALLOWED_ENV}
    logger.info(f"Executing `{join(command)}`, {env=}")
    Popen(
        command, start_new_session=True, env=dict(os.environ) | env, stdout=DEVNULL, stderr=STDOUT
    )
    return True


@huey.task()
def add_new_pioreactor(new_pioreactor_name: str, version: str, model: str) -> tuple[bool, str]:
    # CPU heavy
    logger.info(f"Adding new pioreactor {new_pioreactor_name}, {model} {version}")
    result = run(
        [PIO_EXECUTABLE, "workers", "add", new_pioreactor_name, "-v", version, "-m", model],
        capture_output=True,
        text=True,
    )
    cache.evict("config")
    if result.returncode != 0:
        return False, str(result.stderr.strip())
    else:
        return True, str(result.stderr.strip())


@huey.task()
def update_app_across_cluster() -> bool:
    # CPU heavy / IO heavy
    logger.info("Updating app on leader")
    update_app_on_leader = ["pio", "update", "app"]
    run_and_check_call(update_app_on_leader)
    cache.evict("app")

    logger.info("Updating app and ui on workers")
    update_app_across_all_workers = [PIOS_EXECUTABLE, "update", "-y"]
    run(update_app_across_all_workers)
    return True


@huey.task()
def update_app_from_release_archive_across_cluster(archive_location: str) -> bool:
    logger.info(f"Updating app on leader from {archive_location}")
    update_app_on_leader = ["pio", "update", "app", "--source", archive_location]
    run_and_check_call(update_app_on_leader)
    # remove bits if success
    cache.evict("app")

    logger.info(f"Updating app and ui on workers from {archive_location}")
    distribute_archive_to_workers = [PIOS_EXECUTABLE, "cp", archive_location, "-y"]
    run(distribute_archive_to_workers)

    update_app_across_all_workers = [PIOS_EXECUTABLE, "update", "--source", archive_location, "-y"]
    run(update_app_across_all_workers)

    return True


@huey.task()
def update_app_from_release_archive_on_specific_pioreactors(
    archive_location: str, pioreactors: list[str]
) -> bool:
    units_cli: tuple[str, ...] = sum((("--units", p) for p in pioreactors), tuple())

    logger.info(f"Updating app and ui on unit {pioreactors} from {archive_location}")
    distribute_archive_to_workers = [PIOS_EXECUTABLE, "cp", archive_location, "-y", *units_cli]
    run(distribute_archive_to_workers)

    update_app_across_all_workers = [
        PIOS_EXECUTABLE,
        "update",
        "--source",
        archive_location,
        "-y",
        *units_cli,
    ]
    run(update_app_across_all_workers)

    return True


@huey.task()
def pio(*args: str, env: dict[str, str] = {}) -> tuple[bool, str]:
    logger.info(f'Executing `{join(("pio",) + args)}`, {env=}')
    result = run(
        (PIO_EXECUTABLE,) + args, capture_output=True, text=True, env=dict(os.environ) | env
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    else:
        return True, result.stdout.strip()


@huey.task()
@huey.lock_task("export-data-lock")
def pio_run_export_experiment_data(*args: str, env: dict[str, str] = {}) -> tuple[bool, str]:
    logger.info(f'Executing `{join(("pio", "run", "export_experiment_data") + args)}`, {env=}')
    result = run(
        (PIO_EXECUTABLE, "run", "export_experiment_data") + args,
        capture_output=True,
        text=True,
        env=dict(os.environ) | env,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    else:
        return True, result.stdout.strip()


@huey.task()
def pio_kill(*args: str, env: dict[str, str] = {}) -> bool:
    logger.info(f'Executing `{join(("pio", "kill") + args)}`, {env=}')
    result = run((PIO_EXECUTABLE, "kill") + args, env=dict(os.environ) | env)
    return result.returncode == 0


@huey.task()
@huey.lock_task("plugins-lock")
def pio_plugins(*args: str, env: dict[str, str] = {}) -> bool:
    # install / uninstall only
    assert args[0] in ("install", "uninstall")
    logger.info(f'Executing `{join(("pio", "plugins") + args)}`, {env=}')
    result = run((PIO_EXECUTABLE, "plugins") + args, env=dict(os.environ) | env)
    return result.returncode == 0


@huey.task()
@huey.lock_task("update-lock")
def pio_update_app(*args: str, env: dict[str, str] = {}) -> bool:
    logger.info(f'Executing `{join(("pio", "update", "app") + args)}`, {env=}')
    result = run((PIO_EXECUTABLE, "update", "app") + args, env=dict(os.environ) | env)
    return result.returncode == 0


@huey.task()
@huey.lock_task("update-lock")
def pio_update(*args: str, env: dict[str, str] = {}) -> bool:
    logger.info(f'Executing `{join(("pio", "update") + args)}`, {env=}')
    run((PIO_EXECUTABLE, "update") + args, env=dict(os.environ) | env)
    # this always returns >0 because it kills huey, I think, so just return true
    return True


@huey.task()
@huey.lock_task("update-lock")
def pio_update_ui(*args: str, env: dict[str, str] = {}) -> bool:
    logger.info(f'Executing `{join(("pio", "update", "ui") + args)}`, {env=}')
    run((PIO_EXECUTABLE, "update", "ui") + args, env=dict(os.environ) | env)
    # this always returns >0 because it kills huey, I think, so just return true
    return True


@huey.task()
def rm(path: str) -> bool:
    logger.info(f"Deleting {path}.")
    result = run(["rm", path])
    return result.returncode == 0


@huey.task()
def shutdown() -> bool:
    logger.info("Shutting down now")
    result = run(["sudo", "shutdown", "-h", "now"])
    return result.returncode == 0


@huey.task()
def reboot() -> bool:
    logger.info("Rebooting now")
    result = run(["sudo", "reboot"])
    return result.returncode == 0


@huey.task()
def pios(*args: str, env: dict[str, str] = {}) -> tuple[bool, str]:
    logger.info(f'Executing `{join(("pios",) + args + ("-y",))}`, {env=}')
    result = run(
        (PIOS_EXECUTABLE,) + args + ("-y",),
        capture_output=True,
        text=True,
        env=dict(os.environ) | env,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    else:
        return True, result.stdout.strip()


@huey.task()
def save_file(path: str, content: str) -> bool:
    try:
        with open(path, "w") as f:
            f.write(content)
        return True
    except Exception as e:
        logger.error(e)
        return False


@huey.task()
def write_config_and_sync(config_path: str, text: str, units: str, flags: str) -> tuple[bool, str]:
    try:
        with open(config_path, "w") as f:
            f.write(text)

        result = run(
            (PIOS_EXECUTABLE, "sync-configs", "--units", units, flags),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise Exception(result.stderr.strip())

        return (True, "")

    except Exception as e:
        logger.error(str(e))
        return (False, "Could not sync configs to all Pioreactors.")


@huey.task()
def post_worker(worker: str, endpoint: str, json: dict | None = None) -> tuple[str, Any]:
    try:
        r = post_into(resolve_to_address(worker), endpoint, json=json, timeout=1)
        r.raise_for_status()
        return worker, r.json()
    except HTTPException:
        logger.error(f"Could not post to {worker}'s endpoint {endpoint}. Check connection?")
        return worker, None


@huey.task()
def multicast_post_across_cluster(
    endpoint: str, workers: list[str], json: dict | None = None
) -> dict[str, Any]:
    # this function "consumes" one huey thread waiting fyi
    assert endpoint.startswith("/unit_api")

    tasks = post_worker.map(((worker, endpoint, json) for worker in workers))

    return {worker: response for (worker, response) in tasks.get(blocking=True)}


@huey.task()
def get_worker(worker: str, endpoint: str, json: dict | None = None) -> tuple[str, Any]:
    try:
        r = get_from(resolve_to_address(worker), endpoint, json=json, timeout=1)
        r.raise_for_status()
        return worker, r.json()
    except HTTPException:
        logger.error(f"Could not get from {worker}'s endpoint {endpoint}. Check connection?")
        return worker, None


@huey.task()
def multicast_get_across_cluster(
    endpoint: str, workers: list[str], json: dict | None = None
) -> dict[str, Any]:
    # this function "consumes" one huey thread waiting fyi
    assert endpoint.startswith("/unit_api")

    tasks = get_worker.map(((worker, endpoint, json) for worker in workers))

    return {worker: response for (worker, response) in tasks.get(blocking=True)}
