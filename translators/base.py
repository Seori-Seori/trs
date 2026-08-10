from __future__ import annotations

from abc import ABC, abstractmethod


class TranslationTransportError(RuntimeError):
    pass


class Translator(ABC):
    @abstractmethod
    def translate(self, prompt: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        raise NotImplementedError
