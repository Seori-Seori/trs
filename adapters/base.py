from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from core.segment import Segment


class Adapter(ABC):
    @abstractmethod
    def load(self, path: str | Path) -> tuple[Any, list[Segment]]:
        raise NotImplementedError

    @abstractmethod
    def save(self, document: Any, segments: list[Segment], output_path: str | Path) -> Path:
        raise NotImplementedError

    @abstractmethod
    def backup(self, path: str | Path, backup_dir: str | Path | None = None) -> Path:
        raise NotImplementedError
