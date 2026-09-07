#!/usr/bin/env bash
# Sovereign Agentic AI Workbench - Model Gateway (llama.cpp Fallback Backend)
# Hardware Target: RTX 4060 (8GB VRAM) + 16GB System RAM
# Model: GPT-OSS 20B MXFP4 GGUF

MODEL_PATH="/run/media/santhankumar/New Volume/models/gpt-oss-20b/openai_gpt-oss-20b-MXFP4.gguf"

echo "=========================================================="
echo " Starting llama.cpp Model Gateway for GPT-OSS 20B MoE"
echo " Model: $MODEL_PATH"
echo " MoE Strategy: --n-gpu-layers 99 --cpu-moe"
echo " Endpoint: http://127.0.0.1:8080/v1"
echo "=========================================================="

llama-server \
  --model "$MODEL_PATH" \
  --host 127.0.0.1 \
  --port 8080 \
  -ngl 99 \
  --cpu-moe \
  -c 8192 \
  -fa \
  --temp 0.6 \
  --top-p 0.95
