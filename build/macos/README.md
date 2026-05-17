# DeepFaceLive on macOS (CPU-only)

Two ways to run Linux locally: **Lima VM** (recommended for day-to-day dev) or **Docker**. See [`../linux/README.md`](../linux/README.md) for general notes.

## Ubuntu VM (Lima) — recommended for local testing

Persistent Ubuntu 22.04 VM with the repo mounted at `/work/DeepFaceLive`. Install system and Python deps **once**; then `git pull` and re-run — no Docker image rebuild.

**Prerequisites**

1. [Lima](https://github.com/lima-vm/lima): `brew install lima`
2. [XQuartz](https://www.xquartz.org/): `brew install --cask xquartz` — log out/in once, open XQuartz, Preferences → Security → **Allow connections from network clients**
3. Before each session: `xhost +localhost` (or run `vm-start.sh`, which tries this for you)

**First run** (creates VM, `apt`, venv + pip — can take several minutes):

```bash
./build/macos/vm-start.sh
```

**Daily workflow**

```bash
./build/macos/vm-shell.sh          # optional: git pull, edit, debug
cd /work/DeepFaceLive && git pull
./build/macos/vm-start.sh -s       # -s skips limactl start if VM already up
```

Re-install Python deps after `requirements-cpu.txt` changes:

```bash
./build/macos/vm-start.sh -p
```

`-d DIR` sets userdata (default `<repo>/data`). Use a path under your home directory so Lima’s home mount is visible inside the VM.

UDP ports `1234` / `18766` are forwarded in [`vm/lima.yaml`](vm/lima.yaml). For other ports, edit `portForwards` and recreate or restart the VM.



Destroy the VM: `limactl delete deepfacelive`

If the first run used an older `lima.yaml` (`protocol` warning or missing repo), recreate once:

```bash
limactl delete deepfacelive
./build/macos/vm-start.sh
```

## Docker

CPU-only image for Docker Desktop on macOS.

```bash
./build/macos/start.sh
```

`-s` skips rebuild. `-d DIR` sets the data directory (default `<repo>/data`).

## Stream output port (MPEG-TS / UDP)

[`start.sh`](start.sh) publishes **two** UDP ports by default: **`DFL_OUTPUT_STREAM_UDP_PORT`** (default `1234`, stream output from the app) and **`DFL_STREAM_PORT_UDP`** (default `18766`, incoming network stream / OBS UDP). They must differ. Set `DFL_OUTPUT_STREAM_UDP_PORT` to match the Stream output panel.

```bash
export DFL_OUTPUT_STREAM_UDP_PORT=2345
./build/macos/start.sh -s
```

## Display

`DISPLAY` defaults to `host.docker.internal:0` (XQuartz). Override with `DFL_CONTAINER_DISPLAY` if needed.
