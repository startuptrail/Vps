import asyncio
import datetime as dt
import fcntl
import json
import os
import pty
import select
import shlex
import shutil
import signal
import subprocess
import struct
import termios
import threading
import time
from pathlib import Path

import psutil
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONSOLE_TOKEN = os.environ.get("CONSOLE_TOKEN", "")
FILE_ROOT = Path(os.environ.get("SPARECLOUD_FILE_ROOT", "/app/workspace")).resolve()
FILE_ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
apps = {}
apps_lock = threading.Lock()

app = FastAPI(title="SpareCloud Render Console")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def safe_token(candidate: str) -> bool:
    return bool(CONSOLE_TOKEN) and candidate == CONSOLE_TOKEN


def require_token(authorization: str | None = Header(default=None), x_console_token: str | None = Header(default=None)) -> None:
    candidate = x_console_token or (authorization[7:] if authorization and authorization.startswith("Bearer ") else "")
    if not safe_token(candidate):
        raise HTTPException(status_code=401, detail="Authentication required")


def confined_path(raw: str = "") -> Path:
    if "\x00" in raw:
        raise HTTPException(status_code=400, detail="Invalid path")
    candidate = (FILE_ROOT / raw).resolve()
    try:
        candidate.relative_to(FILE_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path is outside the workspace")
    return candidate


class FileAction(BaseModel):
    path: str = Field(min_length=0, max_length=2048)
    name: str | None = Field(default=None, max_length=255)


class FileSave(BaseModel):
    path: str = Field(min_length=1, max_length=2048)
    content: str = Field(max_length=2_000_000)


class AppSpec(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    command: str = Field(min_length=1, max_length=500)


def command_args(command: str) -> list[str]:
    try:
        args = shlex.split(command)
    except ValueError:
        raise HTTPException(400, "Invalid command")
    if not args or any("\x00" in part for part in args):
        raise HTTPException(400, "Invalid command")
    return args


def process_info(process: psutil.Process) -> dict:
    try:
        with process.oneshot():
            return {"pid": process.pid, "name": process.name(), "status": process.status(),
                    "cpu": round(process.cpu_percent(None), 1), "ram": process.memory_info().rss,
                    "started": dt.datetime.fromtimestamp(process.create_time(), dt.timezone.utc).isoformat(),
                    "command": " ".join(process.cmdline())[:300]}
    except (psutil.Error, PermissionError, OSError):
        return {}


def app_status(item: dict) -> dict:
    process = item.get("process")
    alive = bool(process and process.poll() is None)
    status = "Running" if alive else ("Crashed" if item.get("started") else "Stopped")
    return {"name": item["name"], "command": item["command"], "status": status,
            "pid": process.pid if alive else None, "uptime": round(time.time() - item["started"], 1) if alive else 0,
            "logs": item["logs"][-200:]}


def set_winsize(fd: int, rows: int, cols: int) -> None:
    rows = max(1, min(int(rows), 200))
    cols = max(1, min(int(cols), 400))
    packed = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


def child_shell() -> None:
    os.environ.setdefault("TERM", "xterm-256color")
    os.environ.setdefault("LANG", "C.UTF-8")
    os.environ["PS1"] = "\\[\\e[1;32m\\]root@sparecloud\\[\\e[0m\\]:\\w# "
    os.environ["HOME"] = "/root"
    os.chdir("/root")
    os.execv("/bin/bash", ["/bin/bash", "--login"])


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status(_: None = Depends(require_token)) -> JSONResponse:
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage(FILE_ROOT)
    net = psutil.net_io_counters()
    uptime = time.time() - psutil.boot_time()
    return JSONResponse({
        "online": True,
        "debian": Path("/etc/debian_version").read_text().strip() if Path("/etc/debian_version").exists() else "Unavailable",
        "hostname": os.uname().nodename,
        "os": " ".join(Path("/etc/os-release").read_text().splitlines()[:1]) if Path("/etc/os-release").exists() else "Unavailable",
        "arch": os.uname().machine, "kernel": os.uname().release, "python": os.sys.version.split()[0],
        "cpu": psutil.cpu_percent(None), "cores": psutil.cpu_count() or "Unavailable",
        "ram": {"percent": memory.percent, "used": memory.used, "total": memory.total},
        "disk": {"percent": round(disk.used / disk.total * 100, 1), "used": disk.used, "total": disk.total},
        "network": {"rx": net.bytes_recv, "tx": net.bytes_sent} if net else None,
        "uptime": uptime, "status": "Container running", "apps": sum(1 for x in apps.values() if x.get("process") and x["process"].poll() is None),
        "shell": "/bin/bash",
        "node": shutil.which("node") is not None,
    })


@app.get("/api/files")
def files(path: str = "", search: str = "", sort: str = "name", _: None = Depends(require_token)):
    folder = confined_path(path)
    if not folder.is_dir(): raise HTTPException(404, "Directory not found")
    entries = []
    for item in folder.iterdir():
        if search.lower() not in item.name.lower(): continue
        try: stat = item.stat()
        except OSError: continue
        entries.append({"name": item.name, "path": str(item.relative_to(FILE_ROOT)), "directory": item.is_dir(),
                        "size": stat.st_size if item.is_file() else 0, "modified": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat()})
    entries.sort(key=lambda x: (not x["directory"], x[sort] if sort in {"name", "size", "modified"} else x["name"]))
    return {"root": str(FILE_ROOT), "path": path, "entries": entries}


@app.get("/api/files/download")
def download(path: str, _: None = Depends(require_token)):
    target = confined_path(path)
    if not target.is_file(): raise HTTPException(404, "File not found")
    return FileResponse(target, filename=target.name)


@app.get("/api/files/read")
def read_file(path: str, _: None = Depends(require_token)):
    target = confined_path(path)
    if not target.is_file() or target.stat().st_size > 2_000_000: raise HTTPException(400, "Text file unavailable")
    try: content = target.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError): raise HTTPException(415, "Binary or unreadable file")
    return {"path": path, "content": content}


@app.post("/api/files/save")
def save_file(data: FileSave, _: None = Depends(require_token)):
    target = confined_path(data.path)
    if not target.is_file(): raise HTTPException(404, "File not found")
    target.write_text(data.content, encoding="utf-8")
    return {"saved": True}


@app.post("/api/files/folder")
def create_folder(data: FileAction, _: None = Depends(require_token)):
    target = confined_path(data.path) / (data.name or "")
    if not data.name or target.exists(): raise HTTPException(400, "Invalid or existing folder")
    target.mkdir()
    return {"created": True}


@app.post("/api/files/file")
def create_file(data: FileAction, _: None = Depends(require_token)):
    target = confined_path(data.path) / (data.name or "")
    if not data.name or target.exists(): raise HTTPException(400, "Invalid or existing file")
    target.touch()
    return {"created": True}


@app.post("/api/files/upload")
async def upload(path: str = Query(""), file: UploadFile = File(...), _: None = Depends(require_token)):
    target = confined_path(path) / Path(file.filename or "upload").name
    if target.exists(): raise HTTPException(409, "File exists")
    size = 0
    with target.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES: target.unlink(missing_ok=True); raise HTTPException(413, "Upload too large")
            output.write(chunk)
    return {"uploaded": True, "path": str(target.relative_to(FILE_ROOT))}


@app.delete("/api/files")
def delete_file(path: str, _: None = Depends(require_token)):
    target = confined_path(path)
    if target == FILE_ROOT or not target.exists(): raise HTTPException(404, "Not found")
    if target.is_dir(): shutil.rmtree(target)
    else: target.unlink()
    return {"deleted": True}


@app.post("/api/files/rename")
def rename_file(data: FileAction, _: None = Depends(require_token)):
    target = confined_path(data.path); destination = target.parent / (data.name or "")
    if not target.exists() or not data.name or destination.exists(): raise HTTPException(400, "Invalid rename")
    target.rename(destination)
    return {"renamed": True}


@app.get("/api/processes")
def processes(_: None = Depends(require_token)):
    return {"processes": [x for p in psutil.process_iter() if (x := process_info(p))]}


@app.post("/api/processes/{pid}/{action}")
def process_action(pid: int, action: str, _: None = Depends(require_token)):
    if action not in {"stop", "terminate", "kill"}: raise HTTPException(400, "Unsupported action")
    try: process = psutil.Process(pid); getattr(process, action)()
    except (psutil.Error, PermissionError, OSError): raise HTTPException(404, "Process unavailable")
    return {"ok": True}


@app.get("/api/apps")
def list_apps(_: None = Depends(require_token)):
    with apps_lock: return {"apps": [app_status(x) for x in apps.values()]}


@app.post("/api/apps")
def add_app(data: AppSpec, _: None = Depends(require_token)):
    with apps_lock:
        if data.name in apps: raise HTTPException(409, "App exists")
        apps[data.name] = {"name": data.name, "command": data.command, "process": None, "logs": [], "started": 0}
    return app_status(apps[data.name])


@app.post("/api/apps/{name}/{action}")
def app_action(name: str, action: str, _: None = Depends(require_token)):
    with apps_lock:
        item = apps.get(name)
        if not item: raise HTTPException(404, "App not found")
        if action in {"stop", "restart"} and item.get("process") and item["process"].poll() is None: item["process"].terminate()
        if action in {"start", "restart"}:
            item["logs"].append(f"[{dt.datetime.now().isoformat(timespec='seconds')}] starting {item['command']}")
            try:
                item["process"] = subprocess.Popen(command_args(item["command"]), cwd=FILE_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            except (OSError, HTTPException) as error:
                item["logs"].append(f"[{dt.datetime.now().isoformat(timespec='seconds')}] failed to start: {error}\n")
                item["process"] = None
            item["started"] = time.time()
            threading.Thread(target=lambda: item["logs"].extend(item["process"].stdout), daemon=True).start()
        return app_status(item)


@app.delete("/api/apps/{name}")
def remove_app(name: str, _: None = Depends(require_token)):
    with apps_lock:
        item = apps.pop(name, None)
        if not item: raise HTTPException(404, "App not found")
        if item.get("process") and item["process"].poll() is None: item["process"].terminate()
    return {"deleted": True}


@app.websocket("/ws/terminal")
async def terminal(websocket: WebSocket) -> None:
    await websocket.accept()

    try:
        auth = await asyncio.wait_for(websocket.receive_text(), timeout=10)
    except Exception:
        await websocket.close(code=1008, reason="Authentication timeout")
        return

    try:
        auth_data = json.loads(auth)
    except json.JSONDecodeError:
        await websocket.close(code=1008, reason="Invalid authentication payload")
        return

    if not safe_token(str(auth_data.get("token", ""))):
        await websocket.close(code=1008, reason="Invalid console token")
        return

    rows = int(auth_data.get("rows", 30) or 30)
    cols = int(auth_data.get("cols", 120) or 120)

    pid, fd = pty.fork()
    if pid == 0:
        child_shell()
        return

    try:
        set_winsize(fd, rows, cols)

        async def browser_to_pty() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"type": "input", "data": raw}

                kind = data.get("type")
                if kind == "input":
                    payload = str(data.get("data", "")).encode("utf-8", errors="ignore")
                    if payload:
                        os.write(fd, payload)
                elif kind == "resize":
                    try:
                        set_winsize(fd, int(data.get("rows", rows)), int(data.get("cols", cols)))
                    except (TypeError, ValueError, OSError):
                        pass

        async def pty_to_browser() -> None:
            loop = asyncio.get_running_loop()
            while True:
                try:
                    ready, _, _ = await loop.run_in_executor(None, select.select, [fd], [], [], 0.5)
                    if not ready:
                        try:
                            waited, _ = os.waitpid(pid, os.WNOHANG)
                        except ChildProcessError:
                            waited = pid
                        if waited == pid:
                            break
                        continue
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    await websocket.send_text(json.dumps({"type": "output", "data": data.decode("utf-8", errors="replace")}))
                except OSError:
                    break

        await websocket.send_text(json.dumps({
            "type": "ready",
            "message": "Authenticated. Connected to Debian 13 PTY.\r\n",
        }))

        sender = asyncio.create_task(browser_to_pty())
        receiver = asyncio.create_task(pty_to_browser())
        done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            try:
                task.result()
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            except Exception:
                pass

    finally:
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
