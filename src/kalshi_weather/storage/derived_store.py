from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DerivedAnalyticsStore:
    def __init__(self, root: Path | str = "data/derived") -> None:
        self.root = Path(root)

    def write_provider_reliability(self, city_id: str, payload: dict[str, Any]) -> Path:
        path = self.root / "provider_reliability" / f"{city_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def read_provider_reliability(self, city_id: str) -> dict[str, Any] | None:
        path = self.root / "provider_reliability" / f"{city_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
