from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from statistics import fmean
from typing import Any, DefaultDict, Generic, Mapping, TypeVar

from kalshi_weather.domain.enums import SourceHealthState


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RawPayloadRecord:
    source_name: str
    source_endpoint: str
    request_params: Mapping[str, Any]
    transport_status: int | str
    payload_hash: str
    parser_version: str
    ingest_time: datetime
    event_time: datetime | None
    payload: bytes | str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedEnvelope(Generic[T]):
    raw_payload_id: str
    normalized_at: datetime
    record: T
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceHealthSnapshot:
    source_name: str
    state: SourceHealthState
    last_success_at: datetime | None
    recent_success_rate: float
    freshness_seconds: float | None
    timing_drift_seconds_avg: float | None
    sequence_gap_count: int
    schema_hash: str | None
    notes: tuple[str, ...] = ()


def calculate_timing_drift_seconds(
    event_time: datetime | None, ingest_time: datetime
) -> float | None:
    if event_time is None:
        return None
    return (ingest_time - event_time).total_seconds()


class TimingDriftMonitor:
    def __init__(self, maxlen: int = 256) -> None:
        self._drifts: DefaultDict[str, deque[float]] = defaultdict(lambda: deque(maxlen=maxlen))

    def record(
        self,
        source_name: str,
        event_time: datetime | None,
        ingest_time: datetime,
    ) -> float | None:
        drift = calculate_timing_drift_seconds(event_time, ingest_time)
        if drift is not None:
            self._drifts[source_name].append(drift)
        return drift

    def average(self, source_name: str) -> float | None:
        drifts = self._drifts.get(source_name)
        if not drifts:
            return None
        return fmean(drifts)

    def should_widen_uncertainty(
        self,
        source_name: str,
        warning_seconds: float,
    ) -> bool:
        average = self.average(source_name)
        return average is not None and average >= warning_seconds


class IngestionAdapter(ABC, Generic[T]):
    source_name: str
    parser_version: str

    @abstractmethod
    def fetch_raw(self) -> RawPayloadRecord:
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw_payload: RawPayloadRecord) -> tuple[NormalizedEnvelope[T], ...]:
        raise NotImplementedError
