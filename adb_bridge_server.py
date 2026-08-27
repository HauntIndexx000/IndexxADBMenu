#!/usr/bin/env python3
"""
adb_bridge_server.py

Local companion process that lets a webpage drive ADB.

Why this exists: browsers have no raw TCP/USB-ADB transport for JS to use
directly (see explanation given alongside this file). This process is the
thing that actually holds the ADB connection -- using the same persistent-
shell pattern as the tkinter dash tool -- and exposes it to the page over
a plain localhost WebSocket. The page never touches ADB directly; it just
sends/receives JSON over ws://127.0.0.1:8765.

Run this locally (same machine the headset/emulator is plugged into or
reachable via TCP/IP ADB), then open bridge_client.html in a browser.

Protocol (JSON messages over the WebSocket):
  Client -> Server:
    {"type": "devices"}                          -> list connected devices
    {"type": "connect", "serial": "<id-or-ip>"}   -> attach persistent shell
    {"type": "cmd", "line": "ls /sdcard"}         -> run a shell command
    {"type": "disconnect"}

  Server -> Client:
    {"type": "devices", "devices": [...]}
    {"type": "connected", "serial": "..."}
    {"type": "output", "line": "..."}
    {"type": "error", "message": "..."}
"""

import asyncio
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import websockets

HOST = "127.0.0.1"
PORT = 8765


def find_adb() -> str:
    """Same discovery order as the tkinter tool: local dir, PATH, common SDK paths."""
    local = Path(__file__).parent / ("adb.exe" if sys.platform == "win32" else "adb")
    if local.exists():
        return str(local)

    on_path = shutil.which("adb")
    if on_path:
        return on_path

    common = [
        Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe",
        Path.home() / "Library/Android/sdk/platform-tools/adb",
        Path.home() / "Android/Sdk/platform-tools/adb",
        Path("/usr/bin/adb"),
        Path("/usr/local/bin/adb"),
    ]
    for c in common:
        if c.exists():
            return str(c)

    raise FileNotFoundError(
        "adb binary not found. Install platform-tools or place adb next to this script."
    )


ADB_BIN = None
try:
    ADB_BIN = find_adb()
except FileNotFoundError as e:
    print(f"[warn] {e}", file=sys.stderr)


def list_devices() -> list[str]:
    if not ADB_BIN:
        return []
    result = subprocess.run([ADB_BIN, "devices"], capture_output=True, text=True, timeout=5)
    devices = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if line and "\tdevice" in line:
            devices.append(line.split("\t")[0])
    return devices


class PersistentShell:
    """
    Wraps `adb -s <serial> shell` as a long-lived subprocess with a stdin
    pipe, so we don't spawn a new adb process per command (same rationale
    as the dash-controller tool: avoids event-stream / latency overhead
    on repeated invocations).
    """

    def __init__(self, serial: str, on_output):
        self.serial = serial
        self.on_output = on_output
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self):
        if not ADB_BIN:
            raise RuntimeError("adb binary not available on this machine")
        self.proc = subprocess.Popen(
            [ADB_BIN, "-s", self.serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            if self._stop.is_set():
                break
            self.on_output(line.rstrip("\n"))

    def send(self, line: str):
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("shell not started")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def stop(self):
        self._stop.set()
        if self.proc:
            try:
                self.proc.stdin.write("exit\n")
                self.proc.stdin.flush()
            except Exception:
                pass
            self.proc.terminate()


async def handler(websocket):
    shell: PersistentShell | None = None
    loop = asyncio.get_event_loop()

    def push_output(line: str):
        asyncio.run_coroutine_threadsafe(
            websocket.send(json.dumps({"type": "output", "line": line})), loop
        )

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({"type": "error", "message": "bad json"}))
                continue

            mtype = msg.get("type")

            if mtype == "devices":
                try:
                    devices = list_devices()
                    await websocket.send(json.dumps({"type": "devices", "devices": devices}))
                except Exception as e:
                    await websocket.send(json.dumps({"type": "error", "message": str(e)}))

            elif mtype == "connect":
                serial = msg.get("serial")
                if not serial:
                    await websocket.send(json.dumps({"type": "error", "message": "no serial given"}))
                    continue
                if shell:
                    shell.stop()
                shell = PersistentShell(serial, push_output)
                try:
                    shell.start()
                    await websocket.send(json.dumps({"type": "connected", "serial": serial}))
                except Exception as e:
                    await websocket.send(json.dumps({"type": "error", "message": str(e)}))
                    shell = None

            elif mtype == "cmd":
                if not shell:
                    await websocket.send(json.dumps({"type": "error", "message": "not connected"}))
                    continue
                try:
                    shell.send(msg.get("line", ""))
                except Exception as e:
                    await websocket.send(json.dumps({"type": "error", "message": str(e)}))

            elif mtype == "disconnect":
                if shell:
                    shell.stop()
                    shell = None
                await websocket.send(json.dumps({"type": "disconnected"}))

            else:
                await websocket.send(json.dumps({"type": "error", "message": f"unknown type {mtype}"}))
    finally:
        if shell:
            shell.stop()


async def main():
    print(f"adb bridge listening on ws://{HOST}:{PORT}")
    print(f"adb binary: {ADB_BIN or 'NOT FOUND'}")
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
