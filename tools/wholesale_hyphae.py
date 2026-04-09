#!/usr/bin/env python3
"""Start a dedicated Hyphae instance for the wholesale RE knowledge corpus."""
import os
import sys

# Add Hyphae source to path
sys.path.insert(0, "/home/om/Desktop/Hyphae/hyphae/src")

DB_PATH = os.path.expanduser("~/.hyphae/wholesale.db")
PORT = 8102

# Patch the default before importing
import hyphae
hyphae.DEFAULT_DB = DB_PATH

# Also patch the server's get_hyphae to use our DB
from hyphae.server import app
import hyphae.server as server_module

_instance = None
def get_wholesale_hyphae():
    global _instance
    if _instance is None:
        _instance = hyphae.Hyphae(db_path=DB_PATH)
    return _instance

server_module.get_hyphae = get_wholesale_hyphae

if __name__ == "__main__":
    import uvicorn
    print(f"Starting Wholesale Hyphae on port {PORT}")
    print(f"Database: {DB_PATH}")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
