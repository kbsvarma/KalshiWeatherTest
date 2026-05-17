from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from kalshi_weather.ingestion.contracts import RawPayloadRecord


@dataclass(frozen=True, slots=True)
class StoredRawPayload:
    raw_payload_id: str
    metadata_path: Path
    payload_path: Path


class FileRawStore:
    def __init__(self, root: Path | str = "data/raw") -> None:
        self.root = Path(root)

    def write(self, raw_record: RawPayloadRecord) -> StoredRawPayload:
        raw_payload_id = uuid4().hex
        ingest_date = raw_record.ingest_time.strftime("%Y-%m-%d")
        source_dir = self.root / raw_record.source_name / ingest_date / raw_payload_id
        source_dir.mkdir(parents=True, exist_ok=True)

        payload_hash = self._payload_hash(raw_record.payload)
        payload_suffix = "json" if isinstance(raw_record.payload, str) else "bin"
        payload_path = source_dir / f"payload.{payload_suffix}"
        metadata_path = source_dir / "metadata.json"

        if isinstance(raw_record.payload, bytes):
            payload_path.write_bytes(raw_record.payload)
        else:
            payload_path.write_text(raw_record.payload, encoding="utf-8")

        metadata = asdict(raw_record)
        metadata["ingest_time"] = raw_record.ingest_time.isoformat()
        metadata["event_time"] = (
            raw_record.event_time.isoformat() if raw_record.event_time else None
        )
        metadata["payload"] = None
        metadata["raw_payload_id"] = raw_payload_id
        metadata["payload_hash"] = payload_hash
        metadata["payload_path"] = str(payload_path)

        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return StoredRawPayload(
            raw_payload_id=raw_payload_id,
            metadata_path=metadata_path,
            payload_path=payload_path,
        )

    @staticmethod
    def _payload_hash(payload: str | bytes) -> str:
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        return sha256(data).hexdigest()
