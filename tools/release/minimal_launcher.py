"""Self-contained, foreground Windows runtime; UI/API do not wait for R5/OTP."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path, PurePosixPath
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]


def open_when_api_ready(stopped):
    while not stopped.is_set():
        try:
            with urlopen("http://127.0.0.1:8000/api/health", timeout=0.5) as response:
                if response.status == 200:
                    webbrowser.open("http://127.0.0.1:5173")
                    return
        except OSError:
            pass
        stopped.wait(0.2)


def verify(root, full=True):
    manifest = json.loads((root / "MINIMAL_RUNTIME.json").read_text(encoding="utf-8"))
    started = time.perf_counter()
    rows = [
        row for row in manifest["files"] if full or row["category"] in ("code", "provenance", "web")
    ]
    required = {row["path"] for row in rows}
    directories = {""}
    for name in required:
        parts = PurePosixPath(name)
        if parts.is_absolute() or ".." in parts.parts or "\\" in name or ":" in name:
            raise ValueError("Runtime member escaped package")
        directories.update(str(parent) for parent in parts.parents if str(parent) != ".")
    sizes = {}
    pending = [(root, "")]
    while pending:
        folder, prefix = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                name = prefix + "/" + entry.name if prefix else entry.name
                if name not in required and name not in directories:
                    continue
                stat = entry.stat(follow_symlinks=False)
                if getattr(stat, "st_file_attributes", 0) & 0x400 or entry.is_symlink():
                    raise ValueError(f"Runtime reparse member: {name}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), name))
                else:
                    sizes[name] = stat.st_size
    for row in rows:
        path = root / row["path"]
        if sizes.get(row["path"]) != row["bytes"]:
            raise ValueError(f"Runtime member missing or wrong size: {row['path']}")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != row["sha256"]:
            raise ValueError(f"Runtime differs: {row['path']}")
    for name, origin in manifest["aliases"].items() if full else []:
        path, source = root / name, root / origin
        if not path.resolve().is_relative_to(root) or not source.resolve().is_relative_to(root):
            raise ValueError("Alias escaped package")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, path)  # Same-volume NTFS alias, no duplicated input bytes.
        elif not os.path.samefile(source, path):
            raise ValueError(f"Working input must be a package-local hard link: {name}")
    print(
        json.dumps(
            {"event": "runtime_validation", "full": full, "seconds": time.perf_counter() - started}
        ),
        flush=True,
    )
    return manifest


def child(module):
    """Trace Python file use; forbid accidental development-cache fallbacks."""
    import runpy

    root = ROOT
    manifest = json.loads((root / "MINIMAL_RUNTIME.json").read_text(encoding="utf-8"))
    forbidden = str(Path(manifest["source"]).parent.resolve()).casefold()
    output = (root / ".state" / f"files-{os.getpid()}.jsonl").open("a", encoding="utf-8")
    seen = set()
    guard = threading.local()

    def audit(event, args):
        if event not in ("open", "sqlite3.connect") or getattr(guard, "active", False):
            return
        if not args or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        guard.active = True
        try:
            name = os.fsdecode(args[0])
            path = str(Path(name.removeprefix("file:").split("?", 1)[0]).resolve())
            if path.casefold().startswith(forbidden + os.sep):
                raise PermissionError("Development .cache access forbidden in experiment")
            if (
                path.startswith(str(root))
                and path not in seen
                and ("data" + os.sep in path or ".cache" + os.sep in path)
            ):
                seen.add(path)
                output.write(json.dumps({"event": event, "path": path}) + "\n")
                output.flush()
        finally:
            guard.active = False

    sys.addaudithook(audit)
    try:
        with (Path(manifest["source"]) / "RUNTIME_KIT.json").open("rb"):
            raise RuntimeError("Development-cache isolation probe unexpectedly succeeded")
    except PermissionError:
        output.write(json.dumps({"source_cache_probe": "blocked"}) + "\n")
        output.flush()
    sys.argv = [module]
    if module == "web-api":
        import asyncio

        import uvicorn
        from starlette.staticfiles import StaticFiles

        from services.api.app import app

        app.mount("/", StaticFiles(directory=root / "apps/web/dist", html=True))
        config = uvicorn.Config(app, host="127.0.0.1", port=8000, access_log=False)
        sockets = [config.bind_socket()]
        try:
            sockets.append(
                uvicorn.Config(app, host="127.0.0.1", port=5173, access_log=False).bind_socket()
            )
            asyncio.run(uvicorn.Server(config).serve(sockets=sockets))
        finally:
            for item in sockets:
                item.close()
    else:
        runpy.run_module(module, run_name="__main__")


def launch(headless=False):
    start = time.perf_counter()
    verify(ROOT, full=False)
    for port in (5173, 8000, 8800, 8810):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise ValueError(
                    f"Port {port} is occupied; stop its existing service first"
                ) from error
    state = ROOT / ".state"
    state.mkdir(exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(state)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    children, logs = [], []
    names, exited = {}, set()
    stopped = threading.Event()
    java = ROOT / ".cache/router/jdk-25.0.4.1+1/bin/java.exe"
    commands = {
        "api": [sys.executable, "-B", "-m", "tools.release.minimal_launcher", "--child", "web-api"],
        "native": [
            sys.executable,
            "-B",
            "-m",
            "tools.release.minimal_launcher",
            "--child",
            "tools.router.coverage_worker",
        ],
        "otp": [
            str(java),
            "-Xmx6G",
            "-jar",
            str(ROOT / ".cache/router/otp-shaded-2.10.0.jar"),
            "--load",
            "--bindAddress",
            "127.0.0.1",
            "--port",
            "8800",
            str(ROOT / ".cache/router/otp-inputs"),
        ],
    }
    try:

        def start_service(name):
            log = (state / f"{name}.log").open("w", encoding="utf-8")
            logs.append(log)
            process = subprocess.Popen(
                commands[name],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            children.append(process)
            names[process.pid] = name

        start_service("api")

        def start_routers():
            try:
                verify(ROOT)
                if stopped.is_set():
                    return
                start_service("native")
                start_service("otp")
            except Exception as error:
                (state / "router-start-error.txt").write_text(str(error), encoding="utf-8")
                print(f"Routers unavailable: {error}", flush=True)

        threading.Thread(target=start_routers, daemon=True).start()
        (state / "processes.json").write_text(
            json.dumps(
                {
                    "launcher": os.getpid(),
                    "children": [p.pid for p in children],
                    "api_spawn_seconds": time.perf_counter() - start,
                }
            ),
            encoding="utf-8",
        )
        print(
            "http://127.0.0.1:5173 - map/API start independently; "
            "routers prepare in parallel. Type stop + Enter.",
            flush=True,
        )
        if not headless:
            threading.Thread(target=open_when_api_ready, args=(stopped,), daemon=True).start()

        def console():
            for line in sys.stdin:
                if line.strip().lower() == "stop":
                    stopped.set()
                    return

        threading.Thread(target=console, daemon=True).start()
        while not stopped.wait(0.5):
            for process in children:
                if process.poll() is not None:
                    name = names[process.pid]
                    if name == "api":
                        raise RuntimeError("API exited; see .state/api.log")
                    if name not in exited:
                        exited.add(name)
                        print(
                            f"{name} unavailable; map/API remain running. See .state logs.",
                            flush=True,
                        )
    finally:
        stopped.set()
        for process in children:
            if process.poll() is None:
                process.terminate()
        for process in children:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        for log in logs:
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if args.child:
        child(args.child)
    elif args.check:
        print(json.dumps({"files": len(verify(ROOT)["files"]), "verified": True}))
    else:
        try:
            launch(args.headless)
        except KeyboardInterrupt:
            pass
