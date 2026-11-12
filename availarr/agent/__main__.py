import logging
import os
import threading
import time

import uvicorn
from fastapi import FastAPI

from availarr.agent import newznab, sabnzbd
from availarr.agent.b_arr import BArr
from availarr.agent.config import load_agent_config
from availarr.agent.db import AgentStore
from availarr.agent.transfer import ServerClient, step
from availarr.setup import run_setup


def build(cfg):
    store = AgentStore(cfg.db_path)
    server = ServerClient(cfg.server_url, cfg.token)
    b = BArr(cfg.radarr_url, cfg.radarr_key, cfg.sonarr_url, cfg.sonarr_key)
    app = FastAPI()
    app.include_router(newznab.router(cfg, server, b))
    app.include_router(sabnzbd.router(cfg, store, server))

    def step_once():
        try:
            step(store, server)
        except Exception:
            logging.exception("agent step failed")

    return app, step_once


def main():
    logging.basicConfig(level=logging.INFO)
    path = os.environ.get("AVAILARR_CONFIG", "/config/agent.toml")
    if not os.path.exists(path):  # first run: web setup on the agent port, then carry on
        run_setup("agent", path, load_agent_config, port=8789)
    cfg = load_agent_config(path)
    app, step_once = build(cfg)

    def loop():
        while True:
            step_once()
            time.sleep(cfg.poll_seconds)

    threading.Thread(target=loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=cfg.port)


if __name__ == "__main__":
    main()
