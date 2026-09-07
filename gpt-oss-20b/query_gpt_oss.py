#!/usr/bin/env python3
"""
Sovereign Agentic AI Workbench - GPT-OSS 20B Inference Client
Connects to local Ollama server running GPT-OSS 20B MXFP4 on New Volume.
Uses OpenAI Harmony raw channel prompting for instant, non-repetitive responses.
"""

import sys
import requests

# Ensure proper UTF-8 handling on Windows stdout
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gpt-oss-20b:latest"

def ask_gpt_oss(prompt_text: str, system_prompt: str = "You are a helpful AI assistant.") -> str:
    # OpenAI Harmony raw prompt format
    formatted_prompt = (
        f"<|start|>system<|message|>{system_prompt}<|end|>\n"
        f"<|start|>user<|message|>{prompt_text}<|end|>\n"
        f"<|start|>assistant<|channel|>final<|message|>"
    )
    
    payload = {
        "model": MODEL_NAME,
        "prompt": formatted_prompt,
        "raw": True,
        "stream": False,
        "options": {
            "temperature": 0.6,
            "top_p": 0.95,
            "num_predict": 1024,
            "stop": ["<|end|>", "<|return|>"]
        }
    }
    
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        return data.get("response", "").strip()
    except Exception as e:
        return f"Error connecting to GPT-OSS 20B: {e}"

if __name__ == "__main__":
    if len(sys.argv) > 1:
        user_query = " ".join(sys.argv[1:])
    else:
        user_query = "Explain Quantum Computing in 2 concise bullet points."
    
    print(f"\nUser Query: {user_query}\n")
    print("--- GPT-OSS 20B Response ---")
    answer = ask_gpt_oss(user_query)
    print(answer)
    print("----------------------------\n")
