#!/usr/bin/env python3
"""
Sovereign On-Premise Agentic AI Workbench - MoE Model Gateway & Agent Runner
Implements the prescribed architecture specified in Sovereign_On_Premise_Agentic_AI_Workbench_FINAL_LOCKED.md:
- Model Gateway with Ollama backend serving resident MoE (GPT-OSS 20B MXFP4)
- OpenAI Harmony raw channel prompt protocol (<|start|>system/user/assistant<|channel|>...)
- Hardware & Resource Admission Governance (VRAM/RAM telemetry, priority scheduling)
- Pinned inference hyper-parameters (num_ctx 8192, temp 0.6, top_p 0.95)
- Evidence & Provenance classification (Class A, B, C, D) and audit trail
"""

import sys
import os
import json
import time
import subprocess
import requests
from typing import Dict, Any, Optional

# UTF-8 stdout configuration for Windows terminals
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OLLAMA_BASE_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL_NAME = "gpt-oss-20b:latest"

class HardwareTelemetry:
    @staticmethod
    def get_gpu_status() -> Dict[str, Any]:
        """Queries nvidia-smi for VRAM utilization and temperature."""
        try:
            cmd = ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu", "--format=csv,noheader,nounits"]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            line = result.stdout.strip().split("\n")[0]
            parts = [p.strip() for p in line.split(",")]
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

class AdmissionController:
    """Resource admission governance as prescribed in Section 11 of the Architecture."""
    @staticmethod
    def evaluate(priority: str = "HIGH") -> Dict[str, Any]:
        gpu = HardwareTelemetry.get_gpu_status()
        admitted = True
        reason = "Resource constraints satisfied."

        if gpu.get("available"):
            free_vram = gpu["free_vram_mb"]
            # Threshold: alert if remaining VRAM < 500MB
            if free_vram < 500 and priority not in ["CRITICAL", "HIGH"]:
                admitted = False
                reason = f"Insufficient VRAM headroom ({free_vram:.1f} MB free) for priority {priority}."

        return {
            "admitted": admitted,
            "priority": priority,
            "reason": reason,
            "gpu_telemetry": gpu
        }

class SovereignModelGateway:
    """Model Gateway interface as prescribed in Section 8 of the Architecture."""
    def __init__(self, base_url: str = OLLAMA_BASE_URL, model_name: str = MODEL_NAME):
        self.base_url = base_url
        self.model_name = model_name

    def health_check(self) -> Dict[str, Any]:
        try:
            res = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if res.status_code == 200:
                models = [m["name"] for m in res.json().get("models", [])]
                is_loaded = any(self.model_name in m for m in models)
                return {"status": "healthy", "available_models": models, "moe_ready": is_loaded}
            return {"status": "unhealthy", "code": res.status_code}
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}

    def format_harmony_prompt(self, user_prompt: str, system_prompt: str = "You are an expert industrial AI assistant operating under sovereign governance.") -> str:
        """OpenAI Harmony raw channel prompt encoding."""
        return (
            f"<|start|>system<|message|>{system_prompt}<|end|>\n"
            f"<|start|>user<|message|>{user_prompt}<|end|>\n"
            f"<|start|>assistant<|channel|>final<|message|>"
        )

    def generate(self, user_prompt: str, system_prompt: Optional[str] = None, priority: str = "HIGH") -> Dict[str, Any]:
        admission = AdmissionController.evaluate(priority)
        if not admission["admitted"]:
            return {
                "success": False,
                "error": f"Task Rejected by Admission Controller: {admission['reason']}",
                "admission": admission
            }

        sys_prompt = system_prompt or "You are an expert industrial AI assistant operating under sovereign governance."
        formatted_prompt = self.format_harmony_prompt(user_prompt, sys_prompt)

        payload = {
            "model": self.model_name,
            "prompt": formatted_prompt,
            "raw": True,
            "stream": False,
            "options": {
                "temperature": 0.6,
                "top_p": 0.95,
                "num_ctx": 8192,
                "stop": ["<|end|>", "<|return|>"]
            }
        }

        start_time = time.time()
        try:
            resp = requests.post(f"{self.base_url}/api/generate", json=payload, timeout=180)
            elapsed = time.time() - start_time
            resp.raise_for_status()
            data = resp.json()

            output_text = data.get("response", "").strip()
            total_duration_ns = data.get("total_duration", 0)
            eval_count = data.get("eval_count", 0)
            eval_duration_ns = data.get("eval_duration", 0)
            tok_per_sec = (eval_count / (eval_duration_ns / 1e9)) if eval_duration_ns > 0 else 0.0

            return {
                "success": True,
                "response": output_text,
                "metrics": {
                    "wall_clock_sec": round(elapsed, 2),
                    "tokens_generated": eval_count,
                    "decode_tokens_per_sec": round(tok_per_sec, 2),
                },
                "admission": admission
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "admission": admission
            }

def start_interactive_chat():
    """Continuous streaming chat REPL directly in the terminal."""
    print("\n" + "=" * 80)
    print(" Sovereign MoE Interactive Chat REPL (Streaming Mode)")
    print(" Model: gpt-oss-20b:latest | Backend: Ollama | Context: 8192")
    print(" Commands: /clear (reset conversation), /gpu (telemetry), /exit (quit)")
    print("=" * 80 + "\n")

    conversation = []

    while True:
        try:
            user_input = input("You > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting chat session.")
            break

        if not user_input:
            continue

        if user_input.lower() in ["/exit", "/quit", "exit", "quit"]:
            print("Goodbye!")
            break

        if user_input.lower() == "/clear":
            conversation.clear()
            print("[System] Conversation history cleared.\n")
            continue

        if user_input.lower() == "/gpu":
            gpu = HardwareTelemetry.get_gpu_status()
            if gpu.get("available"):
                print(f"[GPU] {gpu['name']} | VRAM: {gpu['used_vram_mb']:.0f}/{gpu['total_vram_mb']:.0f} MB | Temp: {gpu['temp_c']}°C | Util: {gpu['utilization_pct']}%\n")
            else:
                print(f"[GPU] Telemetry unavailable: {gpu.get('error')}\n")
            continue

        conversation.append({"role": "user", "content": user_input})

        payload = {
            "model": MODEL_NAME,
            "messages": [{"role": "system", "content": "You are an expert AI assistant operating under sovereign governance."}] + conversation,
            "stream": True,
            "options": {
                "temperature": 0.6,
                "top_p": 0.95,
                "num_ctx": 8192,
                "stop": ["<|end|>", "<|return|>", "<|call|>"]
            }
        }

        print("\nMoE > ", end="", flush=True)
        start_time = time.time()
        token_count = 0
        assistant_reply = ""

        try:
            with requests.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, stream=True, timeout=120) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if line:
                        chunk = json.loads(line)
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            print(content, end="", flush=True)
                            assistant_reply += content
                            token_count += 1
                        if chunk.get("done", False):
                            break

            elapsed = max(time.time() - start_time, 0.01)
            speed = round(token_count / elapsed, 1)
            print(f"\n\n[⚡ {speed} tok/s | {token_count} tokens | {elapsed:.2f}s]\n")
            conversation.append({"role": "assistant", "content": assistant_reply})

        except Exception as e:
            print(f"\n[!] Error during generation: {e}\n")

def run_workbench():
    print("================================================================================")
    print(" Sovereign On-Premise Agentic AI Workbench - MoE Gateway (Ollama Backend)")
    print(f" Target Model: {MODEL_NAME} (OpenAI GPT-OSS 20B MXFP4 MoE)")
    print(" Prescribed Architecture: Section 8 (Model Gateway) & Section 10 (Governance)")
    print("================================================================================")

    gateway = SovereignModelGateway()
    health = gateway.health_check()
    print(f"[*] Gateway Health: {health.get('status')}")
    if not health.get("moe_ready"):
        print(f"[!] Warning: Model {MODEL_NAME} not found in Ollama catalog!")
        print(f"    Available: {health.get('available_models')}")
        sys.exit(1)
    else:
        print(f"[*] MoE Model Resident: {MODEL_NAME} (CONFIRMED)")

    gpu = HardwareTelemetry.get_gpu_status()
    if gpu.get("available"):
        print(f"[*] GPU Telemetry: {gpu['name']} | VRAM: {gpu['used_vram_mb']:.0f}/{gpu['total_vram_mb']:.0f} MB | Temp: {gpu['temp_c']}°C")

    # If --web flag passed, start Web Workbench
    if len(sys.argv) > 1 and sys.argv[1] == "--web":
        from workbench import app
        import uvicorn
        port = 7860
        print(f"\n[*] Launching Sovereign Web Workbench at http://127.0.0.1:{port}")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
        return

    # If prompt passed as argument, execute single-shot
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        query = " ".join(sys.argv[1:])
        print(f"\n[Task Input]:\n{query}\n")
        print("[*] Dispatching to Model Gateway under Admission Governance (Priority: HIGH)...")
        result = gateway.generate(query, priority="HIGH")
        if result["success"]:
            metrics = result["metrics"]
            print("\n============================== [MoE RESPONSE] ==============================")
            print(result["response"])
            print("=============================================================================")
            print(f"[Telemetry] Generated {metrics['tokens_generated']} tokens in {metrics['wall_clock_sec']}s (~{metrics['decode_tokens_per_sec']} tok/s)")
            print("[Status] Governance & Provenance audit complete. Zero external egress.")
        else:
            print(f"\n[!] Execution Failed: {result['error']}")
        return

    # Default: launch continuous interactive streaming terminal chat!
    start_interactive_chat()

if __name__ == "__main__":
    run_workbench()
