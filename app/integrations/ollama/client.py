from __future__ import annotations

from typing import Any

import httpx


class OllamaClient:
    """Small synchronous client for a local Ollama server."""

    def __init__(self, base_url: str | None = None, model: str | None = None, num_threads: int = 2):
        self.base_url = (base_url or "http://localhost:11434").rstrip("/")
        self.model = model or "qwen3:8b"
        self.num_threads = max(1, int(num_threads))

    def is_available(self) -> bool:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=2.0)
            response.raise_for_status()
            models = response.json().get("models", [])
            return any(model.get("name") == self.model for model in models)
        except (httpx.HTTPError, ValueError, TypeError):
            return False

    def generate(self, prompt: str) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                    "think": False,
                    "keep_alive": 0,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 80,
                        "num_ctx": 4096,
                        "num_thread": self.num_threads,
                    },
                },
                timeout=httpx.Timeout(300.0, connect=3.0),
            )
            response.raise_for_status()
            body = response.json()
            return {"status": "ok", "response": body.get("response", "")}
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return {"status": "unavailable", "message": str(exc)}
