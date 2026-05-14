# DeepFaceLive Docker (Linux GPU / RunPod)

CUDA 11.8 runtime image with GPU PyTorch and **onnxruntime-gpu**. Build context is the **repository root** (same layout as [`../macos`](../macos)).

## RunPod (or any host with NVIDIA Container Toolkit)

1. Clone this repo on the pod (or mount your fork).
2. Install/use Docker with GPU support (`docker run --gpus all` works).
3. From the repository root:

```bash
./build/linux/start.sh
```

`-s` skips rebuild. `-d DIR` sets the data directory (default `<repo>/data`).

## Stream output port (MPEG-TS / UDP)

[`start.sh`](start.sh) publishes **two** UDP ports by default:

| Env | Default | Role |
| :--- | :--- | :--- |
| `DFL_OUTPUT_STREAM_UDP_PORT` | `1234` | **Stream output** from the app (MPEG-TS push); match the Stream output panel. |
| `DFL_STREAM_PORT_UDP` | `18766` | **Network stream** input (FFmpeg listens for OBS / Larix UDP); must differ from the output port. |

They must not be the same port. Override either before `start.sh`:

```bash
export DFL_OUTPUT_STREAM_UDP_PORT=1234
export DFL_STREAM_PORT_UDP=18766
./build/linux/start.sh -s
```

## Display (GUI)

The container needs a working Qt/X11 display. On a pod with a desktop (e.g. `DISPLAY=:0`), you may only need:

```bash
export DFL_CONTAINER_DISPLAY=:0
./build/linux/start.sh -s
```

If `DISPLAY` is unset, `start.sh` defaults to `:0`. For SSH X11 forwarding, set `DFL_CONTAINER_DISPLAY` to match your session (for example `localhost:10.0`) and ensure the X socket / authorization match your environment.

## Cameras (optional, off by default)

RunPod has no host V4L devices; `start.sh` does **not** pass cameras unless you opt in:

```bash
export DFL_ENABLE_CAMERA_DEVICES=1
./build/linux/start.sh -s
```

When enabled, existing `/dev/video0` … `/dev/video3` are passed through with `--device`.

## Network stream (UDP / SRT) on RunPod

If you see **`Address already in use`** on **SRT** (often port **8888**), Jupyter is usually using **8888** — use **`DFL_STREAM_PORT_SRT`** (default **8890** in the app). For **UDP**, the default listen port is **18766** (avoids collisions with common services and stuck `ffmpeg` retries). If a port is still busy, pick another with **`DFL_STREAM_PORT_UDP`**, run **`pkill -9 ffmpeg`**, and ensure **Stream output** and **Network stream** use **different** ports. See [`../../NETWORK_STREAMING.md`](../../NETWORK_STREAMING.md).

## Requirements

- NVIDIA drivers on the host and [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) (RunPod GPU templates usually satisfy this).
- Docker build uses the repo copy on disk (no `git clone` inside the image).
