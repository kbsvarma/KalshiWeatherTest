from __future__ import annotations

from decimal import Decimal
import unittest

from kalshi_weather.domain.models import ShadowPosition
from kalshi_weather.portfolio import build_static_correlation_matrix, portfolio_risk_units


class PortfolioTest(unittest.TestCase):
    def test_portfolio_risk_units_single_city(self) -> None:
        position = ShadowPosition(
            city_id="nyc",
            market_ticker="M1",
            side="yes",
            open_quantity_fp=Decimal("1"),
            avg_cost_dollars=Decimal("0.5"),
            cumulative_fees_dollars=Decimal("0"),
            mark_pnl_dollars=Decimal("0"),
            settled_pnl_dollars=Decimal("0"),
            lifecycle_status="OPEN",
        )
        matrix = build_static_correlation_matrix(["nyc"])
        risk = portfolio_risk_units([("nyc", position)], matrix)
        self.assertEqual(risk, Decimal("1.0"))

    def test_static_correlation_priors_allow_cross_coast_pairing(self) -> None:
        nyc_position = ShadowPosition(
            city_id="nyc",
            market_ticker="M1",
            side="yes",
            open_quantity_fp=Decimal("1"),
            avg_cost_dollars=Decimal("0.5"),
            cumulative_fees_dollars=Decimal("0"),
            mark_pnl_dollars=Decimal("0"),
            settled_pnl_dollars=Decimal("0"),
            lifecycle_status="OPEN",
        )
        lax_position = ShadowPosition(
            city_id="lax",
            market_ticker="M2",
            side="no",
            open_quantity_fp=Decimal("1"),
            avg_cost_dollars=Decimal("0.4"),
            cumulative_fees_dollars=Decimal("0"),
            mark_pnl_dollars=Decimal("0"),
            settled_pnl_dollars=Decimal("0"),
            lifecycle_status="OPEN",
        )
        chi_position = ShadowPosition(
            city_id="chi",
            market_ticker="M3",
            side="yes",
            open_quantity_fp=Decimal("1"),
            avg_cost_dollars=Decimal("0.6"),
            cumulative_fees_dollars=Decimal("0"),
            mark_pnl_dollars=Decimal("0"),
            settled_pnl_dollars=Decimal("0"),
            lifecycle_status="OPEN",
        )
        matrix = build_static_correlation_matrix(["nyc", "lax", "chi"])
        self.assertLess(matrix[("nyc", "lax")], matrix[("nyc", "chi")])
        cross_coast_risk = portfolio_risk_units(
            [("nyc", nyc_position), ("lax", lax_position)],
            matrix,
        )
        midwest_risk = portfolio_risk_units(
            [("nyc", nyc_position), ("chi", chi_position)],
            matrix,
        )
        self.assertLess(cross_coast_risk, Decimal("1.5"))
        self.assertGreater(midwest_risk, cross_coast_risk)


if __name__ == "__main__":
    unittest.main()
