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

[`start.sh`](start.sh) publishes **one** UDP port so clients can receive **Stream output** MPEG-TS from the container. Default **1234**. Set the same port in the app’s Stream output settings.

Override before `start.sh`:

```bash
export DFL_OUTPUT_STREAM_UDP_PORT=2345
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

## Requirements

- NVIDIA drivers on the host and [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) (RunPod GPU templates usually satisfy this).
- Docker build uses the repo copy on disk (no `git clone` inside the image).
