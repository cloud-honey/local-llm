#!/bin/zsh
# oMLX 네이티브 qwen4_exp 경로 시험용 실행 스크립트 (2026-09-17, OMLX_0.6.4_UPGRADE_RUNBOOK.md 4단계).
# 기본 `serve` 와의 차이: jedisct1 커스텀 로더(omlx_support/*.py, sitecustomize)와 모델 폴더의
# .mlx-runtime(mlx 0.32.1)을 PYTHONPATH 에서 뺀다 → 앱 번들 mlx 0.32.0 + omlx 네이티브
# mlx_vlm_qwen4_exp_compat + 커스텀 Metal 커널이 그대로 살아난다. 서버 플래그는 동일.
# 활성화/복귀는 omlx_upgrade_helper.sh native-on / native-off 로만 할 것 (serve 백업 관리).
set -euo pipefail

support_root=${0:A:h}
model_root=${support_root:h}
models_root=${model_root:h}
base_path=${OMLX_BASE_PATH:-$model_root/.omlx}
cache_dir=${OMLX_CACHE_DIR:-$base_path/cache}
api_key=${OMLX_API_KEY:-omlx}
port=${OMLX_PORT:-8766}
host=${OMLX_HOST:-0.0.0.0}
app_resources=${OMLX_APP_RESOURCES:-/Applications/oMLX.app/Contents/Resources}
cpython_root=$app_resources/Python/cpython-3.11
mlx_site=$app_resources/Python/framework-mlx-base/lib/python3.11/site-packages

mkdir -p "$cache_dir"
export OMLX_QWEN4_PLE_MODE=mmap
export OMLX_QWEN4_PLE_MODEL_PATH="$model_root"
export PYTHONHOME=$cpython_root
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$app_resources:$mlx_site"   # support_root / .mlx-runtime 제외가 핵심

exec "$cpython_root/bin/python3" -m omlx.cli serve \
  --model-dir "$models_root" \
  --host "$host" \
  --port "$port" \
  --max-concurrent-requests 1 \
  --memory-guard balanced \
  --paged-ssd-cache-dir "$cache_dir" \
  --paged-ssd-cache-max-size 128GB \
  --hot-cache-max-size 0 \
  --initial-cache-blocks 1 \
  --no-hf-cache \
  --base-path "$base_path" \
  --api-key "$api_key"
