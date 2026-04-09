#!/usr/bin/env python3
"""Dedicated Hyphae instance for the CS-papers scout corpus (port 8102)."""
import os
import sys

DB_PATH = os.path.expanduser("~/.hyphae/scout.db")
PORT = 8103

# Must be set BEFORE importing hyphae — this is the real isolation lever.
os.environ["HYPHAE_DB"] = DB_PATH

sys.path.insert(0, "/home/om/Desktop/Hyphae/hyphae/src")

import hyphae
hyphae.DEFAULT_DB = DB_PATH

from hyphae.server import app
import hyphae.server as server_module

_instance = None
def get_scout_hyphae():
    global _instance
    if _instance is None:
        _instance = hyphae.Hyphae(db_path=DB_PATH)
    return _instance

server_module.get_hyphae = get_scout_hyphae

if __name__ == "__main__":
    import uvicorn
    print(f"Starting Scout Hyphae on port {PORT}")
    print(f"Database: {DB_PATH}")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
