#!/usr/bin/env bash
# Sovereign Agentic AI Workbench - Model Gateway (FreeToken Backend)
# Hardware Target: RTX 4060 (8GB VRAM) + 16GB System RAM
# Model: GPT-OSS 20B MXFP4 GGUF

MODEL_PATH="/run/media/santhankumar/New Volume/models/gpt-oss-20b/openai_gpt-oss-20b-MXFP4.gguf"
VENV_PATH="/run/media/santhankumar/New Volume/freetoken_env"

if [ -f "$VENV_PATH/bin/activate" ]; then
    source "$VENV_PATH/bin/activate"
fi

echo "=========================================================="
echo " Starting FreeToken Model Gateway for GPT-OSS 20B MoE"
echo " Model: $MODEL_PATH"
echo " MoE Backend: offload (GPU Cache + RAM Offload)"
echo " Endpoint: http://127.0.0.1:8080/v1"
echo "=========================================================="

ft serve \
  --model "$MODEL_PATH" \
  --served-model-name "gpt-oss-20b" \
  --host 127.0.0.1 \
  --port 8080 \
  --moe-backend offload \
  --moe-cache-auto \
  --tool-call-parser gpt_oss \
  --reasoning-parser gpt_oss \
  --num-tokens 8192
