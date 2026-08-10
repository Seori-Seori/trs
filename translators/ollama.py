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
                f"Ollama HTTP 오류 {exc.code} ({endpoint}): {detail[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TranslationTransportError(
                f"Ollama에 연결할 수 없습니다: {self.base_url} ({exc.reason})"
            ) from exc
        except TimeoutError as exc:
            raise TranslationTransportError(
                f"Ollama 요청 시간이 {self.timeout_seconds}초를 초과했습니다"
            ) from exc
        except OSError as exc:
            raise TranslationTransportError(
                f"Ollama 전송 중 연결 오류가 발생했습니다: {exc}"
            ) from exc

        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TranslationTransportError("Ollama가 올바른 JSON을 반환하지 않았습니다") from exc
        if not isinstance(decoded, dict):
            raise TranslationTransportError("Ollama JSON 응답이 객체 형식이 아닙니다")
        return decoded

    def health_check(self) -> bool:
        response = self._request("/api/tags", method="GET")
        models = response.get("models")
        if not isinstance(models, list):
            raise TranslationTransportError(
                "Ollama /api/tags 응답에 models 배열이 없습니다"
            )
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
                raise TranslationTransportError(f"Ollama 생성 실패: {error}")
            raise TranslationTransportError(
                "Ollama 응답에 문자열 response 필드가 없습니다"
            )
        return value
