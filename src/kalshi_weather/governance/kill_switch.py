from __future__ import annotations

from datetime import datetime, timezone

from kalshi_weather.domain.enums import KillSwitchScope
from kalshi_weather.domain.models import KillSwitchState


def activate_kill_switch(
    scope: KillSwitchScope,
    reason: str,
    sticky: bool = True,
) -> KillSwitchState:
    return KillSwitchState(
        scope=scope,
        state="ACTIVE",
        triggered_at=datetime.now(timezone.utc),
        trigger_reason=reason,
        cleared_at=None,
        cleared_by=None,
        sticky_flag=sticky,
    )


def clear_kill_switch(existing: KillSwitchState, cleared_by: str) -> KillSwitchState:
    return KillSwitchState(
        scope=existing.scope,
        state="CLEARED",
        triggered_at=existing.triggered_at,
        trigger_reason=existing.trigger_reason,
        cleared_at=datetime.now(timezone.utc),
        cleared_by=cleared_by,
        sticky_flag=existing.sticky_flag,
    )
