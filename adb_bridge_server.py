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
import re
import shlex
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


_ADB_PREFIX_RE = re.compile(r"^\s*adb(\.exe)?\s+", re.IGNORECASE)


def is_host_command(line: str) -> bool:
    """
    True for commands meant to run ON THE MAC against the device (adb devices,
    adb reboot, adb tcpip, adb install, adb push/pull, ...), as opposed to
    commands meant to run INSIDE the device's shell (getprop, pm, dumpsys...).

    Once a persistent `adb -s <serial> shell` session is attached, that
    session IS the device's shell -- there is no `adb` binary inside it to
    call. A card labeled "adb reboot" has to be dispatched as a fresh host
    subprocess, not written into the shell's stdin.
    """
    return bool(_ADB_PREFIX_RE.match(line))


async def run_host_command(serial: str | None, line: str, websocket):
    """
    Run a host-side `adb ...` command as its own subprocess (not through the
    persistent shell) and stream its output back over the websocket as it
    arrives, same "output" message shape as the persistent-shell path.
    """
    try:
        tokens = shlex.split(line)
    except ValueError as e:
        await websocket.send(json.dumps({"type": "error", "message": f"couldn't parse command: {e}"}))
        return

    # tokens[0] is "adb" (or "adb.exe") -- drop it, we invoke ADB_BIN directly.
    rest = tokens[1:]

    # Commands that address the whole adb server rather than one device
    # (e.g. `adb devices`) shouldn't get a `-s <serial>` inserted.
    no_serial_needed = {"devices", "kill-server", "start-server", "connect", "disconnect"}
    if serial and rest and rest[0] not in no_serial_needed:
        args = ["-s", serial] + rest
    else:
        args = rest

    if not ADB_BIN:
        await websocket.send(json.dumps({"type": "error", "message": "adb binary not available on this machine"}))
        return

    await websocket.send(json.dumps({"type": "output", "line": f"[host] adb {' '.join(args)}"}))

    try:
        proc = await asyncio.create_subprocess_exec(
            ADB_BIN, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            text = raw_line.decode(errors="replace").rstrip("\n")
            if text:
                await websocket.send(json.dumps({"type": "output", "line": text}))
        await proc.wait()
        if proc.returncode != 0:
            await websocket.send(json.dumps({"type": "output", "line": f"[host] exited with code {proc.returncode}"}))
    except Exception as e:
        await websocket.send(json.dumps({"type": "error", "message": str(e)}))


async def handler(websocket):
    peer = websocket.remote_address
    print(f"[connect] browser tab connected from {peer}")
    shell: PersistentShell | None = None
    loop = asyncio.get_event_loop()

    def push_output(line: str):
        asyncio.run_coroutine_threadsafe(
            websocket.send(json.dumps({"type": "output", "line": line})), loop
        )

    try:
        async for raw in websocket:
            print(f"[recv] {raw}")
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
                line = msg.get("line", "")
                if is_host_command(line):
                    # Runs on the Mac against the device -- independent of
                    # whether a persistent shell is currently attached.
                    serial = shell.serial if shell else msg.get("serial")
                    await run_host_command(serial, line, websocket)
                    continue
                if not shell:
                    await websocket.send(json.dumps({"type": "error", "message": "not connected"}))
                    continue
                try:
                    shell.send(line)
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
        print(f"[disconnect] browser tab {peer} disconnected")
        if shell:
            shell.stop()


def startup_self_check():
    print("=" * 60)
    print("ADB BRIDGE — STARTUP SELF-CHECK")
    print("=" * 60)

    print(f"Python:     {sys.version.split()[0]}")
    try:
        import websockets as _ws
        print(f"websockets: {_ws.__version__}")
    except Exception as e:
        print(f"websockets: ERROR reading version ({e})")

    print(f"adb binary: {ADB_BIN or 'NOT FOUND — see note below'}")
    if not ADB_BIN:
        print(
            "  -> No 'adb' found on PATH, next to this script, or in common SDK\n"
            "     install locations. Install Android platform-tools and either\n"
            "     add it to PATH or drop adb(.exe) next to this script, then\n"
            "     restart this server."
        )
        print("=" * 60)
        return

    try:
        result = subprocess.run(
            [ADB_BIN, "devices", "-l"], capture_output=True, text=True, timeout=5
        )
        print("`adb devices -l` output:")
        for line in result.stdout.splitlines():
            print(f"    {line}")
        if result.stderr.strip():
            print("  stderr:")
            for line in result.stderr.splitlines():
                print(f"    {line}")
        devices = list_devices()
        if not devices:
            print(
                "  -> No authorized devices right now. Cable plugged in and it's a\n"
                "     data cable (not charge-only)? USB debugging enabled in the\n"
                "     headset's Settings > System > Developer? If a device shows\n"
                "     up above as 'unauthorized', put the headset on — there's a\n"
                "     prompt waiting to be accepted in-headset."
            )
        else:
            print(f"  -> {len(devices)} device(s) ready: {devices}")
    except Exception as e:
        print(f"  -> Failed to run 'adb devices': {e}")

    print("=" * 60)


async def main():
    startup_self_check()
    print(f"Starting WebSocket server on ws://{HOST}:{PORT} ...")
    try:
        async with websockets.serve(handler, HOST, PORT):
            print(f"Listening. Leave this window open — connect from the browser page now.")
            print(f"Every message the page sends/receives will be logged below.")
            print("-" * 60)
            await asyncio.Future()
    except OSError as e:
        print(f"FAILED TO START: {e}")
        if "address already in use" in str(e).lower() or getattr(e, "errno", None) == 48:
            print(
                f"  -> Port {PORT} is already taken — probably another copy of this\n"
                f"     script already running in a different terminal/window. Close\n"
                f"     that one (or reuse it instead of starting a second)."
            )
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
