from __future__ import annotations

from enum import StrEnum


class RunMode(StrEnum):
    REPLAY = "REPLAY"
    SHADOW = "SHADOW"
    LIVE_READONLY = "LIVE_READONLY"
    LIVE_TRADE = "LIVE_TRADE"


class DecisionType(StrEnum):
    NO_TRADE = "NO_TRADE"
    WATCH = "WATCH"
    MAKER_ONLY = "MAKER_ONLY"
    TAKER_ALLOWED = "TAKER_ALLOWED"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    CANCEL_PENDING = "CANCEL_PENDING"
    HALT_CITY = "HALT_CITY"
    HALT_GLOBAL = "HALT_GLOBAL"


class QualificationState(StrEnum):
    UNMAPPED = "UNMAPPED"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    SHADOW_ONLY = "SHADOW_ONLY"
    SHADOW_QUALIFIED = "SHADOW_QUALIFIED"
    LIVE_PILOT = "LIVE_PILOT"
    DISABLED = "DISABLED"


class SettlementValidationStatus(StrEnum):
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    AMBIGUOUS = "AMBIGUOUS"
    BLOCKED = "BLOCKED"


class ReportStatus(StrEnum):
    PRELIMINARY = "PRELIMINARY"
    CANDIDATE_FINAL = "CANDIDATE_FINAL"
    FINALIZED = "FINALIZED"


class RiskState(StrEnum):
    ALLOW = "ALLOW"
    SOFT_BLOCK = "SOFT_BLOCK"
    HARD_HALT = "HARD_HALT"


class KillSwitchScope(StrEnum):
    GLOBAL = "GLOBAL"
    CITY = "CITY"


class SourceHealthState(StrEnum):
    HEALTHY = "HEALTHY"
    SOFT_STALE = "SOFT_STALE"
    HARD_STALE = "HARD_STALE"
    FAILED = "FAILED"
