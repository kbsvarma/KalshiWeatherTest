from __future__ import annotations

import unittest

from kalshi_weather.clients.nws import NwsClimateClient


class NwsClimateClientTest(unittest.TestCase):
    def test_extract_cli_text_from_weather_gov_html(self) -> None:
        html = """
        <!DOCTYPE html>
        <html>
          <body>
            <pre class="glossaryProduct">
084
CDUS43 KLOT 060635
CLIMDW

CLIMATE REPORT
NATIONAL WEATHER SERVICE CHICAGO IL
            </pre>
          </body>
        </html>
        """
        text = NwsClimateClient._extract_cli_text(html)
        self.assertIn("CLIMDW", text)
        self.assertIn("CLIMATE REPORT", text)
        self.assertNotIn("<pre", text)

    def test_extract_cli_text_passthrough_for_plain_payload(self) -> None:
        payload = "CDUS41 KOKX 060224\nCLINYC\n\nCLIMATE REPORT\n"
        self.assertEqual(NwsClimateClient._extract_cli_text(payload), payload)


if __name__ == "__main__":
    unittest.main()
