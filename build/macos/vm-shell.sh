#!/bin/bash
# ==============================================================================
# DeepFaceLive - macOS Lima VM Shell Access Script
# Instantly drops the developer into the Ubuntu guest VM at /work/DeepFaceLive.
# ==============================================================================
set -euo pipefail

# Verify that lima is installed on the host
if ! command -v limactl &>/dev/null; then
    printf "\033[0;31mError: limactl is not installed on your Mac.\033[0m\n" >&2
    printf "Please run: \033[0;32mbrew install lima\033[0m\n\n" >&2
    exit 1
fi

# Check if the VM is running
VM_STATUS=$(limactl list deepfacelive --format '{{.Status}}' 2>/dev/null || echo "")

if [[ "$VM_STATUS" != "Running" ]]; then
    printf "\033[0;33mWarning: Lima VM 'deepfacelive' is not running (status: '%s'). Starting it...\033[0m\n" "$VM_STATUS"
    limactl start deepfacelive
fi

printf "\033[0;32mEntering Lima VM 'deepfacelive' at /work/DeepFaceLive. Type 'exit' to return to macOS.\033[0m\n\n"

# Shell into the VM and automatically change directory to the mounted repository root
limactl shell deepfacelive bash -c "cd /work/DeepFaceLive && exec bash"
