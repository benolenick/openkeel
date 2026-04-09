"""OpenKeel Bridge — local WebSocket server exposing terminal I/O.

Each OpenKeel GUI window starts a bridge server on a random port.
A registration file is written to ~/.openkeel/sessions/ so that
the Automaite desktop agent can discover and latch onto it.

Protocol (mirrors Automaite relay agent protocol):
  - Server sends binary frames: raw PTY output bytes
  - Server sends JSON:  {"type": "meta", "profile": ..., "mode": ..., "stats": {...}}
  - Client sends JSON:  {"type": "input", "data": "..."}
  - Client sends JSON:  {"type": "resize", "cols": N, "rows": N}
  - Client sends JSON:  {"type": "meta_request"}  -> server replies with meta
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from pathlib import Path

logger = logging.getLogger("openkeel.bridge")

SESSIONS_DIR = Path.home() / ".openkeel" / "sessions"


class BridgeServer:
    """Async WebSocket server that bridges a TerminalWidget to remote clients."""

    def __init__(self, terminal_widget, get_meta_fn=None):
        """
        Args:
            terminal_widget: The TerminalWidget whose PTY we expose.
            get_meta_fn: Callable returning dict of governance metadata.
        """
        self._terminal = terminal_widget
        self._get_meta = get_meta_fn or (lambda: {})
        self._session_id = str(uuid.uuid4())
        self._clients: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._port: int = 0
        self._reg_path: Path | None = None

        # Hook into the terminal's output signal
        self._terminal.data_received.connect(self._on_terminal_output)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> int:
        """Start the bridge server in a background thread. Returns the port."""
        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop, args=(ready,), daemon=True,
            name=f"bridge-{self._session_id[:8]}",
        )
        self._thread.start()
        ready.wait(timeout=5)
        if self._port:
            self._write_registration()
        return self._port

    def stop(self):
        """Shut down the server and remove registration."""
        self._remove_registration()
        if self._loop:
            # Close all connected clients so the agent gets a clean disconnect
            async def _close_all():
                for ws in list(self._clients):
                    try:
                        await ws.close()
                    except Exception:
                        pass
                if self._server:
                    self._server.close()
                self._loop.stop()
            try:
                asyncio.run_coroutine_threadsafe(_close_all(), self._loop)
            except Exception:
                # Loop may already be stopped
                pass

    # ---- internal ----

    def _run_loop(self, ready: threading.Event):
        try:
            import websockets
            import websockets.asyncio.server
        except ImportError:
            logger.error("websockets not installed — bridge disabled")
            ready.set()
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _start():
            self._server = await websockets.asyncio.server.serve(
                self._handle_client,
                "127.0.0.1", 0,  # port 0 = OS picks a free port
            )
            # Get the actual port
            for sock in self._server.sockets:
                addr = sock.getsockname()
                self._port = addr[1]
                break
            logger.info(
                "Bridge server listening on 127.0.0.1:%d (session %s)",
                self._port, self._session_id[:8],
            )
            ready.set()
            await self._server.serve_forever()

        try:
            self._loop.run_until_complete(_start())
        except Exception:
            logger.debug("Bridge server loop ended")
        finally:
            ready.set()

    async def _handle_client(self, ws):
        self._clients.add(ws)
        logger.info("Bridge client connected (%d total)", len(self._clients))

        # Send initial meta
        try:
            meta = self._get_meta()
            meta["type"] = "meta"
            meta["session_id"] = self._session_id
            await ws.send(json.dumps(meta))
        except Exception:
            pass

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue

                msg_type = msg.get("type")

                if msg_type == "input":
                    data = msg.get("data", "")
                    if data and self._terminal._pty and self._terminal._pty.isalive():
                        self._terminal._pty.write(data)

                elif msg_type == "resize":
                    cols = max(1, min(msg.get("cols", 80), 500))
                    rows = max(1, min(msg.get("rows", 24), 200))
                    self._terminal._cols = cols
                    self._terminal._rows = rows
                    self._terminal._screen.resize(rows, cols)
                    if self._terminal._pty and self._terminal._pty.isalive():
                        self._terminal._pty.setwinsize(rows, cols)

                elif msg_type == "meta_request":
                    meta = self._get_meta()
                    meta["type"] = "meta"
                    meta["session_id"] = self._session_id
                    await ws.send(json.dumps(meta))

        except Exception:
            pass
        finally:
            self._clients.discard(ws)
            logger.info("Bridge client disconnected (%d remaining)", len(self._clients))

    def _on_terminal_output(self, data: str):
        """Called from Qt thread when terminal produces output."""
        if not self._clients or not self._loop:
            return
        raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data

        async def _broadcast(payload):
            dead = set()
            for ws in self._clients:
                try:
                    await ws.send(payload)
                except Exception:
                    dead.add(ws)
            self._clients -= dead

        try:
            asyncio.run_coroutine_threadsafe(_broadcast(raw), self._loop)
        except Exception:
            pass

    def _write_registration(self):
        """Write a JSON file so Automaite can discover this session."""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._reg_path = SESSIONS_DIR / f"{self._session_id}.json"
        import os
        reg = {
            "session_id": self._session_id,
            "port": self._port,
            "pid": os.getpid(),
            "label": "OpenKeel Terminal",
        }
        # Add current governance metadata
        try:
            reg.update(self._get_meta())
        except Exception:
            pass
        self._reg_path.write_text(json.dumps(reg, indent=2), encoding="utf-8")
        logger.info("Registration written: %s", self._reg_path)

    def _remove_registration(self):
        """Remove the registration file on shutdown."""
        if self._reg_path and self._reg_path.exists():
            try:
                self._reg_path.unlink()
                logger.info("Registration removed: %s", self._reg_path)
            except Exception:
                pass
