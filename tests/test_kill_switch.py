from __future__ import annotations

import unittest

from kalshi_weather.domain.enums import KillSwitchScope
from kalshi_weather.governance.kill_switch import activate_kill_switch, clear_kill_switch


class KillSwitchTest(unittest.TestCase):
    def test_activate_and_clear(self) -> None:
        kill = activate_kill_switch(KillSwitchScope.GLOBAL, "test")
        self.assertEqual(kill.state, "ACTIVE")
        cleared = clear_kill_switch(kill, "tester")
        self.assertEqual(cleared.state, "CLEARED")


if __name__ == "__main__":
    unittest.main()
