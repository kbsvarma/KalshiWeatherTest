from __future__ import annotations

import json

from kalshi_weather.domain.enums import KillSwitchScope
from kalshi_weather.governance import activate_kill_switch
from kalshi_weather.storage import SQLiteStateStore
from kalshi_weather.utils.serde import to_jsonable


def main() -> None:
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    kill = activate_kill_switch(KillSwitchScope.GLOBAL, reason="manual_operator_stop")
    store.save_kill_switch(scope=kill.scope.value, payload=to_jsonable(kill))
    print(json.dumps(to_jsonable(kill), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
