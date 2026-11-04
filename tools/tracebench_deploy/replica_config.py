from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import normalize_service_name

DEFAULT_REPLICA_COUNT = 1


class ReplayConfigError(RuntimeError):
    """Raised when the replica configuration cannot be parsed."""


@dataclass
class ReplicaConfig:
    default: int = DEFAULT_REPLICA_COUNT
    overrides: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ReplicaConfig":
        data = data or {}
        default = int(data.get("default", DEFAULT_REPLICA_COUNT))
        overrides = {
            normalize_service_name(str(name)): int(value)
            for name, value in data.get("overrides", {}).items()
        }
        for name, value in [("default", default), *overrides.items()]:
            if value < 1:
                raise ReplayConfigError(f"replica count for {name} must be >= 1, got {value}")
        return cls(default=default, overrides=overrides)

    @classmethod
    def from_file(cls, path: Path) -> "ReplicaConfig":
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ReplayConfigError(f"Failed to parse replica config {path}: {exc}") from exc
        return cls.from_dict(data)

    def count_for(self, service_name: str) -> int:
        return self.overrides.get(service_name, self.default)
