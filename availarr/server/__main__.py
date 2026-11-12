import logging
import os
import threading
import time

import uvicorn

from availarr.server.api import create_api
from availarr.server.config import load_server_config
from availarr.server.db import JobStore
from availarr.server.jobs import Jobs
from availarr.server.panel import create_panel
from availarr.server.qbit import QBit
from availarr.server.radarr import Radarr
from availarr.server.retention import tick
from availarr.server.sonarr import Sonarr


def build(cfg):
    qbit = QBit(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
    jobs = Jobs(JobStore(cfg.db_path), Radarr(cfg.radarr_url, cfg.radarr_key, cfg.radarr_root),
                Sonarr(cfg.sonarr_url, cfg.sonarr_key, cfg.sonarr_root), cfg, qbit=qbit)

    def retention_once():
        try:
            tick(jobs, qbit, time.time())
        except Exception:
            logging.exception("retention tick failed")

    return create_api(jobs, cfg), create_panel(jobs, qbit, cfg), retention_once


def main():
    logging.basicConfig(level=logging.INFO)
    cfg = load_server_config(os.environ.get("AVAILARR_CONFIG", "/config/server.toml"))
    api, panel, retention_once = build(cfg)

    def retention_loop():
        while True:
            retention_once()
            time.sleep(600)

    threading.Thread(target=uvicorn.Server(uvicorn.Config(panel, host="0.0.0.0", port=cfg.panel_port)).run,
                     daemon=True).start()
    threading.Thread(target=retention_loop, daemon=True).start()
    uvicorn.run(api, host="0.0.0.0", port=cfg.api_port)


if __name__ == "__main__":
    main()
