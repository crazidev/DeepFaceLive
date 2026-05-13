#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SKIP_BUILD=0
DATA_FOLDER="${REPO_ROOT}/data/"

usage() {
    printf "Usage: %s [-s] [-d /path/to/data]\n" "$(basename "$0")"
    printf "  -s  Skip docker build\n"
    printf "  -d  Data dir (default: <repo>/data)\n"
    printf "GPU: requires NVIDIA Container Toolkit (--gpus all). Typical on RunPod.\n"
    printf "Stream output (MPEG-TS UDP): publish host port = container port (default 1234). Override with DFL_OUTPUT_STREAM_UDP_PORT.\n"
    printf "Display: defaults to host \$DISPLAY. Override with DFL_CONTAINER_DISPLAY (e.g. :0 on a pod desktop).\n"
    printf "Cameras: off by default (RunPod). Set DFL_ENABLE_CAMERA_DEVICES=1 to pass /dev/video0..3 when present.\n"
}

camera_docker_args() {
    local v="${DFL_ENABLE_CAMERA_DEVICES:-}"
    case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) ;;
        *) return 0 ;;
    esac
    printf 'Camera passthrough enabled (DFL_ENABLE_CAMERA_DEVICES=%s)\n' "$v" >&2
    [[ -e /dev/video0 ]] && printf '%s ' --device=/dev/video0:/dev/video0
    [[ -e /dev/video1 ]] && printf '%s ' --device=/dev/video1:/dev/video1
    [[ -e /dev/video2 ]] && printf '%s ' --device=/dev/video2:/dev/video2
    [[ -e /dev/video3 ]] && printf '%s ' --device=/dev/video3:/dev/video3
}

printf "\n"
while getopts 'sd:h' opt; do
    case "$opt" in
        s) SKIP_BUILD=1; printf "Skipping docker build (-s)\n" ;;
        d) DATA_FOLDER="$OPTARG"; printf "Data folder: %s\n" "$DATA_FOLDER" ;;
        h) usage; exit 0 ;;
        ?) usage; exit 1 ;;
    esac
done
shift "$((OPTIND - 1))"
printf "\n"

mkdir -p "$DATA_FOLDER"

xhost +localhost 2>/dev/null || xhost + 127.0.0.1 2>/dev/null || xhost + 2>/dev/null || true

if [[ "$SKIP_BUILD" -eq 0 ]]; then
    docker build -f "$SCRIPT_DIR/Dockerfile" -t deepfacelive-linux "$REPO_ROOT"
else
    if ! docker image inspect deepfacelive-linux &>/dev/null; then
        printf "No image 'deepfacelive-linux'. Run without -s once.\n" >&2
        exit 1
    fi
fi

P_OUT="${DFL_OUTPUT_STREAM_UDP_PORT:-1234}"
printf '\nStream output (MPEG-TS / UDP): host port %s → container %s (match Stream output port in the app).\n\n' "$P_OUT" "$P_OUT"

DFL_DISP="${DFL_CONTAINER_DISPLAY:-${DISPLAY:-:0}}"
printf "Using DISPLAY=%s (set DFL_CONTAINER_DISPLAY to override)\n" "$DFL_DISP"

CAMERA_ARGS="$(camera_docker_args)"

docker run --ipc=host --gpus all \
    -e "DISPLAY=$DFL_DISP" \
    -p "${P_OUT}:${P_OUT}/udp" \
    -v /tmp/.X11-unix:/tmp/.X11-unix \
    -v "$DATA_FOLDER:/data/" \
    $CAMERA_ARGS \
    --rm -it deepfacelive-linux
