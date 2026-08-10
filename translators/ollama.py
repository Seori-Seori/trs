from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from core.config import OllamaConfig

from .base import Translator, TranslationTransportError


class OllamaTranslator(Translator):
    def __init__(self, config: OllamaConfig) -> None:
        self.base_url = config.base_url.rstrip("/")
        self.model = config.model
        self.timeout_seconds = config.timeout_seconds

    def _request(
        self,
        endpoint: str,
        *,
        payload: dict[str, Any] | None = None,
        method: str | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            f"{self.base_url}{endpoint}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise TranslationTransportError(
                f"Ollama HTTP {exc.code} at {endpoint}: {detail[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TranslationTransportError(
                f"Cannot connect to Ollama at {self.base_url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise TranslationTransportError(
                f"Ollama request timed out after {self.timeout_seconds} seconds"
            ) from exc

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TranslationTransportError("Ollama returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise TranslationTransportError("Ollama returned a non-object JSON response")
        return decoded

    def health_check(self) -> bool:
        try:
            response = self._request("/api/tags", method="GET")
        except TranslationTransportError:
            return False
        models = response.get("models", [])
        if not isinstance(models, list):
            return False
        return any(
            isinstance(item, dict)
            and (item.get("name") == self.model or item.get("model") == self.model)
            for item in models
        )

    def translate(self, prompt: str) -> str:
        response = self._request(
            "/api/generate",
            method="POST",
            payload={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "top_k": 20,
                    "top_p": 0.6,
                    "repeat_penalty": 1.05,
                    "temperature": 0.7,
                },
            },
        )
        value = response.get("response")
        if not isinstance(value, str):
            error = response.get("error")
            if error:
                raise TranslationTransportError(f"Ollama generation failed: {error}")
            raise TranslationTransportError("Ollama response is missing string field 'response'")
        return value
