#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SKIP_BUILD=0
DATA_FOLDER="${REPO_ROOT}/data/"
declare CAM0 CAM1 CAM2 CAM3

usage() {
    printf "Usage: %s [-s] [-d /path/to/data] [-c]\n" "$(basename "$0")"
    printf "  -s  Skip docker build\n  -d  Data dir (default: <repo>/data)\n  -c  Pass /dev/video* if present\n"
    printf "Stream output (MPEG-TS UDP): publish host port = container port (default 1234). Override with DFL_OUTPUT_STREAM_UDP_PORT.\n"
    printf "Network stream (incoming UDP listen): publish port (default 18766). Override with DFL_STREAM_PORT_UDP. Must differ from DFL_OUTPUT_STREAM_UDP_PORT.\n"
}

printf "\n"
while getopts 'scd:h' opt; do
    case "$opt" in
        s) SKIP_BUILD=1; printf "Skipping docker build (-s)\n" ;;
        c)
            test -e /dev/video0 && CAM0="--device=/dev/video0:/dev/video0"
            test -e /dev/video1 && CAM1=--device=/dev/video1:/dev/video1
            test -e /dev/video2 && CAM2=--device=/dev/video2:/dev/video2
            test -e /dev/video3 && CAM3=--device=/dev/video3:/dev/video3
            ;;
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
    docker build -f "$SCRIPT_DIR/Dockerfile" -t deepfacelive-macos "$REPO_ROOT"
else
    if ! docker image inspect deepfacelive-macos &>/dev/null; then
        printf "No image 'deepfacelive-macos'. Run without -s once.\n" >&2
        exit 1
    fi
fi

P_OUT="${DFL_OUTPUT_STREAM_UDP_PORT:-1234}"
P_IN="${DFL_STREAM_PORT_UDP:-18766}"
if [[ "$P_IN" == "$P_OUT" ]]; then
    printf "Ports must differ: DFL_STREAM_PORT_UDP=%s and DFL_OUTPUT_STREAM_UDP_PORT=%s\n" "$P_IN" "$P_OUT" >&2
    exit 1
fi
printf '\nStream output (MPEG-TS / UDP): host port %s → container %s (match Stream output in the app).\n' "$P_OUT" "$P_OUT"
printf 'Network stream (UDP listen): host port %s → container %s (match DFL_STREAM_PORT_UDP).\n' "$P_IN" "$P_IN"
P_FRAME="${DFL_WEBRTC_FRAME_PORT:-8766}"
printf 'WebRTC frame bridge (TCP): host port %s → container (set DFL_WEBRTC_FRAME_HOST=host.docker.internal:%s in container).\n\n' "$P_FRAME" "$P_FRAME"

# Auto-detect X11 port (6000 => :0, 6001 => :1, etc.)
X11_PORT=$(lsof -n -i -P | grep LISTEN | grep -E "X11|XQuartz" | grep -oE '600[0-9]' | head -n 1 || echo 6000)
X11_DISP=$((X11_PORT - 6000))
printf "Detected X11 display :%s (port %s)\n" "$X11_DISP" "$X11_PORT"

docker run --ipc=host \
    -e "DISPLAY=${DFL_CONTAINER_DISPLAY:-host.docker.internal:$X11_DISP}" \
    -e "DFL_OUTPUT_STREAM_UDP_PORT=${P_OUT}" \
    -e "DFL_STREAM_PORT_UDP=${P_IN}" \
    -e "DFL_WEBRTC_FRAME_HOST=host.docker.internal:${P_FRAME}" \
    -p "${P_OUT}:${P_OUT}/udp" \
    -p "${P_IN}:${P_IN}/udp" \
    -p "${P_FRAME}:${P_FRAME}/tcp" \
    -v /tmp/.X11-unix:/tmp/.X11-unix \
    -v "$DATA_FOLDER:/data/" \
    $CAM0 $CAM1 $CAM2 $CAM3 \
    --rm -it deepfacelive-macos
