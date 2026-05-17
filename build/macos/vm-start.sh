#!/bin/bash
# ==============================================================================
# DeepFaceLive - macOS Lima VM Startup Script
# Professional, ultra-fast alternative to Docker on macOS using Lima VM.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SKIP_VM_START=0
FORCE_INSTALL=0
DATA_FOLDER="${REPO_ROOT}/data/"

usage() {
    printf "Usage: %s [-s] [-p] [-d /path/to/data]\n" "$(basename "$0")"
    printf "  -s  Skip starting Lima VM if already running\n"
    printf "  -p  Force reinstalling Python requirements\n"
    printf "  -d  Data directory on host (default: <repo>/data)\n"
}

printf "\n"
while getopts 'spd:h' opt; do
    case "$opt" in
        s) SKIP_VM_START=1; printf "Lima VM: Skip start option enabled (-s)\n" ;;
        p) FORCE_INSTALL=1; printf "Lima VM: Force requirements reinstall option enabled (-p)\n" ;;
        d) DATA_FOLDER="$OPTARG"; printf "Lima VM: Custom data folder specified: %s\n" "$DATA_FOLDER" ;;
        h) usage; exit 0 ;;
        ?) usage; exit 1 ;;
    esac
done
shift "$((OPTIND - 1))"
printf "\n"

# Verify that lima is installed on the host
if ! command -v limactl &>/dev/null; then
    printf "\033[0;31mError: limactl is not installed on your Mac.\033[0m\n" >&2
    printf "Please run: \033[0;32mbrew install lima\033[0m\n\n" >&2
    exit 1
fi

# Ensure data folder exists on host
mkdir -p "$DATA_FOLDER"

# Create dummy requirements file for stream_ingest if missing to prevent pip crashes
if [[ ! -f "${REPO_ROOT}/services/stream_ingest/requirements.txt" ]]; then
    mkdir -p "${REPO_ROOT}/services/stream_ingest"
    touch "${REPO_ROOT}/services/stream_ingest/requirements.txt"
fi

# Generate the actual lima.yaml from the template with dynamic REPO_ROOT mapping
TEMPLATE_FILE="${SCRIPT_DIR}/vm/lima.yaml.template"
TARGET_YAML="${SCRIPT_DIR}/vm/deepfacelive.yaml"

if [[ ! -f "$TEMPLATE_FILE" ]]; then
    printf "\033[0;31mError: Template file not found at %s\033[0m\n" "$TEMPLATE_FILE" >&2
    exit 1
fi

printf "Generating Lima VM configuration with repo root: %s...\n" "$REPO_ROOT"
mkdir -p "$(dirname "$TARGET_YAML")"
sed "s|REPO_ROOT_PLACEHOLDER|${REPO_ROOT}|g" "$TEMPLATE_FILE" > "$TARGET_YAML"

# Check the current status of the deepfacelive VM
VM_STATUS=$(limactl list deepfacelive --format '{{.Status}}' 2>/dev/null || echo "")

if [[ -z "$VM_STATUS" ]]; then
    printf "Instance 'deepfacelive' does not exist. Creating and starting VM (first run, this may take a few minutes)...\n"
    limactl start --tty=false "$TARGET_YAML"
elif [[ "$VM_STATUS" != "Running" ]]; then
    printf "Lima VM 'deepfacelive' is currently %s. Starting it...\n" "$VM_STATUS"
    limactl start deepfacelive
else
    printf "Lima VM 'deepfacelive' is already running.\n"
fi

# Enable X11 network clients permission on the host for XQuartz
printf "Configuring host X11 server permissions (xhost)...\n"
xhost +localhost 2>/dev/null || xhost + 127.0.0.1 2>/dev/null || xhost + 2>/dev/null || true

# Map the host data directory to guest path
# If data folder is inside the repo, map it to /work/DeepFaceLive/...
# If it's outside the repo (but in home directory), it has the identical path on guest
abs_data_folder="$(cd "$(dirname "$DATA_FOLDER")" && pwd)/$(basename "$DATA_FOLDER")"
DATA_FOLDER_GUEST="$abs_data_folder"

if [[ "$abs_data_folder" == "$REPO_ROOT"* ]]; then
    suffix="${abs_data_folder#$REPO_ROOT}"
    DATA_FOLDER_GUEST="/work/DeepFaceLive${suffix}"
fi

printf "Data directory guest path resolved to: %s\n" "$DATA_FOLDER_GUEST"

# Provision Python virtual environment and dependencies if missing or forced
VENV_EXISTS=1
if ! limactl shell deepfacelive test -d /work/DeepFaceLive/.venv-lima &>/dev/null; then
    VENV_EXISTS=0
fi

if [[ "$VENV_EXISTS" -eq 0 || "$FORCE_INSTALL" -eq 1 ]]; then
    printf "Provisioning/updating virtual environment (.venv-lima) inside the guest VM...\n"
    limactl shell deepfacelive bash -c "
      set -euo pipefail
      cd /work/DeepFaceLive
      if [ ! -d .venv-lima ]; then
          echo 'Creating python virtual environment...'
          python3 -m venv .venv-lima
      fi
      source .venv-lima/bin/activate
      echo 'Upgrades inside venv: pip'
      pip install --upgrade pip
      echo 'Installing requirements-cpu.txt inside VM...'
      pip install -r build/macos/requirements-cpu.txt
    "
fi

# Auto-detect X11 port on the macOS host (6000 => :0, 6001 => :1, etc.)
X11_PORT=$(lsof -n -i -P | grep LISTEN | grep -E "X11|XQuartz" | grep -oE '600[0-9]' | head -n 1 || echo 6000)
X11_DISP=$((X11_PORT - 6000))
printf "Detected macOS X11 server on port %s (Display :%s)\n" "$X11_PORT" "$X11_DISP"

# Launch DeepFaceLive inside the guest VM
printf "\n\033[0;32mLaunching DeepFaceLive inside Lima VM (DISPLAY=host.lima.internal:%s)...\033[0m\n\n" "$X11_DISP"

limactl shell deepfacelive bash -c "
  cd /work/DeepFaceLive
  source .venv-lima/bin/activate
  export DISPLAY=host.lima.internal:$X11_DISP
  # Re-enable QT xcb platform integration
  export QT_QPA_PLATFORM=xcb
  python main.py run DeepFaceLive --userdata-dir '$DATA_FOLDER_GUEST' --no-cuda
"
