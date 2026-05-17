from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from kalshi_weather.clients.kalshi_private import KalshiPrivateClient


class KalshiAuthEnvTest(unittest.TestCase):
    def test_from_env_accepts_api_key_id_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            key_path = os.path.join(tempdir, "dummy.pem")
            with open(key_path, "w", encoding="utf-8") as handle:
                handle.write("-----BEGIN PRIVATE KEY-----\nTEST\n-----END PRIVATE KEY-----\n")
            env = {
                "KALSHI_API_KEY_ID": "alias-key",
                "KALSHI_PRIVATE_KEY_PATH": key_path,
                "KALSHI_ENV": "prod",
            }
            with patch.dict(os.environ, env, clear=True):
                client = KalshiPrivateClient.from_env()
            self.assertEqual(client.credentials.access_key, "alias-key")
            self.assertEqual(str(client.credentials.private_key_path), key_path)


if __name__ == "__main__":
    unittest.main()
