# SpareCloud

SpareCloud is an authenticated cloud console running inside a normal Render Docker container. It provides a real Python PTY and `/bin/bash` terminal, container-scoped files, process inspection, managed applications, captured logs, metrics, and system information.

## Architecture

- `server.py` contains FastAPI routes, authentication, PTY/WebSocket sessions, confined file operations, process inspection, and in-memory app supervision.
- `static/index.html` is the responsive console UI. Every terminal tab creates its own authenticated WebSocket and PTY.
- File operations are confined to `SPARECLOUD_FILE_ROOT` (default `/app/workspace`) using resolved-path and symlink escape protection.
- Metrics are read from the container's operating-system view through `psutil`; unavailable values are reported as `Unavailable`.

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export CONSOLE_TOKEN="a-long-random-local-secret"
./start.sh
```

The service binds to `0.0.0.0` and uses `$PORT` (default `10000`). Open `http://localhost:10000`.

## Docker and Render

```bash
docker build -t sparecloud-console .
docker run --rm -p 10000:10000 \
  -e CONSOLE_TOKEN="a-long-random-secret" sparecloud-console
```

Create a Render Web Service using Docker and configure `CONSOLE_TOKEN` as a secret environment variable. Render supplies `PORT`; the included startup command uses it without claiming host-level control.

## Security

`CONSOLE_TOKEN` is required at startup and is never returned by the API or embedded in frontend source. REST calls use `X-Console-Token`; WebSockets authenticate with their first message. Use HTTPS on public deployments. Uploads are limited to 25 MiB and text editing to UTF-8 files of 2 MiB. Managed commands are parsed without a shell and run with the container's permissions.

## Persistence and limitations

Managed app state and captured logs are in memory. Render may restart or recreate containers, so files, running processes, app state, and logs can be lost. The terminal prompt is `root@sparecloud:~#`, but that is only shell presentation: it is not a Render host shell or privileged VPS. Host/kernel operations, nested Docker/LXC, KVM, host networking, and unrestricted systemd are outside a normal Render service.

## Usage and troubleshooting

Authenticate, then use the sidebar to open Dashboard, Terminal, Files, Processes, Apps, Logs, System, or Settings. Add managed apps only for commands whose executable and files exist under the workspace. If startup rejects the configuration, set a strong `CONSOLE_TOKEN`; if system values are `Unavailable`, the container does not expose that operating-system source.
