#!/bin/bash
set -Eeuo pipefail
set -o errtrace

ENV_NAME="pipe_tsm"
MICROMAMBA_BIN="${MICROMAMBA_BIN:-micromamba}"
MAMBA_ROOT_PREFIX="${MAMBA_ROOT_PREFIX:-$HOME/.micromamba}"

log() {
  local ts
  ts="$(date "+%Y-%m-%d %H:%M:%S")"
  echo "[$ts] $*"
}

trap '{
  code=$?
  log "❌ Fail (exit code: ${code})"
  exit $code
}' ERR

if [ $# -lt 1 ]; then
  echo "Usage: ./run.sh <config.yaml> [run_dir]"
  exit 1
fi

CONFIG_PATH="$1"
RUN_DIR="${2:-process001}"
mkdir -p "$RUN_DIR"

PIPE_BASE="$(cd "$(dirname "$0")" && pwd)"

# ---------------- Install Step ----------------
log "Ensuring environment is installed..."
bash "${PIPE_BASE}/install.sh"

# ---------------- Logs ----------------
LOGS_DIR="$RUN_DIR/process_info"
mkdir -p "$LOGS_DIR"
LOG_FILE="$LOGS_DIR/process.log"

exec > >(tee -a "$LOG_FILE") 2>&1

# ---------------- Run Pipeline ----------------
log "Running pipeline..."

"$MICROMAMBA_BIN" run --root-prefix "$MAMBA_ROOT_PREFIX" -n "$ENV_NAME" bash -c "
  export PATH=${PIPE_BASE}/scripts:\$PATH
  export PYTHONPATH=${PIPE_BASE}/packages:\$PYTHONPATH
  exec tsm-run \"\$1\" \"\$2\"
" bash "$CONFIG_PATH" "$RUN_DIR"

log "✅ Success (run dir: ${RUN_DIR})"
exit 0
