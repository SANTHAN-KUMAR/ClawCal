#!/usr/bin/env python3
"""
Sovereign On-Premise Agentic AI Workbench - Web Chat Interface
Prescribed Architecture Implementation (Section 6.1 User Workbench & Section 8 Model Gateway)
Hardware Target: RTX 4060 (8GB VRAM) + 16GB RAM
Backend: FastAPI + Ollama (gpt-oss-20b:latest MoE)
"""

import sys
import os
import json
import time
import subprocess
import requests
import uvicorn
from typing import List, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

app = FastAPI(title="Sovereign AI Workbench")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL_NAME = "gpt-oss-20b:latest"

def get_gpu_telemetry() -> Dict[str, Any]:
    try:
        cmd = ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu", "--format=csv,noheader,nounits"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        parts = [p.strip() for p in res.stdout.strip().split("\n")[0].split(",")]
        return {
            "available": True,
            "name": parts[0],
            "total_vram_mb": float(parts[1]),
            "used_vram_mb": float(parts[2]),
            "free_vram_mb": float(parts[3]),
            "temp_c": float(parts[4]),
            "utilization_pct": float(parts[5])
        }
    except Exception as e:
        return {"available": False, "error": str(e)}

@app.get("/api/telemetry")
async def telemetry_endpoint():
    gpu = get_gpu_telemetry()
    # Check Ollama health
    model_ready = False
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        if r.status_code == 200:
            models = [m["name"] for m in r.json().get("models", [])]
            model_ready = any(MODEL_NAME in m for m in models)
    except Exception:
        pass

    return {
        "gpu": gpu,
        "model": MODEL_NAME,
        "model_ready": model_ready,
        "backend": "Ollama / Local Sovereign Gateway",
        "egress": "0 bytes (Air-gapped)",
        "timestamp": time.time()
    }

@app.post("/api/chat")
async def chat_endpoint(req: Request):
    data = await req.json()
    messages = data.get("messages", [])
    system_prompt = data.get("system_prompt", "You are an expert industrial AI assistant operating under sovereign governance.")

    # Format multi-turn OpenAI Harmony raw prompt protocol
    formatted_prompt = f"<|start|>system<|message|>{system_prompt}<|end|>\n"
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "user":
            formatted_prompt += f"<|start|>user<|message|>{content}<|end|>\n"
        elif role == "assistant":
            formatted_prompt += f"<|start|>assistant<|channel|>final<|message|>{content}<|end|>\n"

    formatted_prompt += "<|start|>assistant<|channel|>final<|message|>"

    payload = {
        "model": MODEL_NAME,
        "prompt": formatted_prompt,
        "raw": True,
        "stream": True,
        "options": {
            "temperature": 0.6,
            "top_p": 0.95,
            "num_ctx": 8192,
            "stop": ["<|end|>", "<|return|>"]
        }
    }

    def generate_stream():
        start_time = time.time()
        token_count = 0
        try:
            with requests.post(f"{OLLAMA_URL}/api/generate", json=payload, stream=True, timeout=120) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        chunk = json.loads(line)
                        content = chunk.get("response", "")
                        if content:
                            token_count += 1
                            yield f"data: {json.dumps({'content': content})}\n\n"
                        if chunk.get("done", False):
                            elapsed = max(time.time() - start_time, 0.01)
                            speed = round(token_count / elapsed, 1)
                            yield f"data: {json.dumps({'done': True, 'tokens': token_count, 'elapsed': round(elapsed, 2), 'speed': speed})}\n\n"
                            break
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate_stream(), media_type="text/event-stream")

@app.get("/", response_class=FileResponse)
async def serve_workbench():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    return FileResponse(html_path)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 7860))
    print(f"================================================================================")
    print(f" Starting Sovereign On-Premise AI Workbench Interface")
    print(f" Web UI: http://127.0.0.1:{port}")
    print(f" MoE Model: {MODEL_NAME} | Local Egress: 0 bytes")
    print(f"================================================================================")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
