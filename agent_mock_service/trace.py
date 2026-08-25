from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping


class JsonlTraceWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: Mapping[str, Any]) -> None:
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":")) + "\n")


class MemoryTraceWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
    def write(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))
