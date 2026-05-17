from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from decimal import ROUND_CEILING
from uuid import uuid4

from kalshi_weather.clients import KalshiPrivateClient, KalshiPrivateClientError
from kalshi_weather.domain.enums import DecisionType, RunMode
from kalshi_weather.domain.models import EdgeEstimate, ExecutionPlan, StrategyDecisionExplanation


class LiveAdapterError(RuntimeError):
    """Raised when a live plan cannot be submitted safely."""


def build_execution_plan(
    explanation: StrategyDecisionExplanation,
    edge: EdgeEstimate,
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=uuid4().hex,
        action_type=explanation.final_decision,
        market_ticker=explanation.market_ticker,
        side=edge.side,
        quantity_fp=edge.quantity_fp,
        order_type="limit",
        limit_price_dollars=edge.p_market_exec,
        time_in_force="immediate_or_cancel",
        max_cost_dollars=(edge.p_market_exec * edge.quantity_fp) + edge.fee_cost,
        cancel_on_pause=True,
        maker_flag=False,
        rationale_codes=tuple(explanation.explanation_codes),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        run_mode=explanation.run_mode,
        decision_utc=explanation.as_of_time,
        max_age_seconds=45,
        portfolio_haircut=edge.portfolio_haircut,
        active_city_count=int(explanation.risk_summary.get("active_city_count") or 1),
    )


class ThinLiveAdapter:
    def __init__(
        self,
        api_base: str = "https://api.elections.kalshi.com/trade-api/v2",
        private_client: KalshiPrivateClient | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.private_client = private_client

    def prepare_order_payload(self, plan: ExecutionPlan) -> dict[str, object]:
        if plan.limit_price_dollars is None:
            raise LiveAdapterError("live plan requires a limit price")
        if plan.limit_price_dollars <= Decimal("0") or plan.limit_price_dollars >= Decimal("1"):
            raise LiveAdapterError("live plan requires a limit price strictly between 0 and 1 dollars")
        if plan.order_type != "limit":
            raise LiveAdapterError(f"unsupported order type: {plan.order_type}")
        payload: dict[str, object] = {
            "ticker": plan.market_ticker,
            "side": plan.side,
            "action": "buy",
            "count_fp": str(plan.quantity_fp),
            "time_in_force": plan.time_in_force,
            "client_order_id": plan.plan_id,
            "cancel_order_on_pause": plan.cancel_on_pause,
        }
        if plan.side == "yes":
            payload["yes_price_dollars"] = str(plan.limit_price_dollars)
        else:
            payload["no_price_dollars"] = str(plan.limit_price_dollars)
        if plan.max_cost_dollars is not None:
            payload["buy_max_cost"] = int(
                (plan.max_cost_dollars * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_CEILING)
            )
        if plan.expires_at is not None and plan.time_in_force != "immediate_or_cancel":
            payload["expiration_ts"] = int(plan.expires_at.timestamp())
        if self.private_client is not None and self.private_client.credentials.subaccount:
            payload["subaccount"] = self.private_client.credentials.subaccount
        return payload

    def submit_plan(
        self,
        plan: ExecutionPlan,
        live_gate_report: dict[str, object],
        active_kill_switch: bool = False,
        dry_run: bool = True,
    ) -> dict[str, object]:
        if active_kill_switch:
            raise LiveAdapterError("manual kill switch active")
        if not bool(live_gate_report.get("all_passed")):
            raise LiveAdapterError("live gates not passed")
        if plan.action_type != DecisionType.TAKER_ALLOWED:
            raise LiveAdapterError(f"unsupported live action: {plan.action_type.value}")
        decision_age = datetime.now(timezone.utc) - plan.decision_utc
        decision_age_seconds = max(0, int(decision_age.total_seconds()))
        if decision_age > timedelta(seconds=max(1, plan.max_age_seconds)):
            raise LiveAdapterError(
                f"stale_decision_abort:decision_age_seconds={decision_age_seconds}:market_ticker={plan.market_ticker}"
            )
        if plan.active_city_count > 1 and plan.portfolio_haircut <= Decimal("0"):
            raise LiveAdapterError("portfolio_haircut_required_for_multi_city_live")
        payload = self.prepare_order_payload(plan)
        if dry_run:
            return {
                "status": "DRY_RUN",
                "endpoint": f"{self.api_base}/portfolio/orders",
                "payload": payload,
            }
        if plan.run_mode != RunMode.LIVE_TRADE:
            raise LiveAdapterError("real submission requires run_mode LIVE_TRADE")
        if self.private_client is None:
            raise LiveAdapterError("no authenticated Kalshi client configured")
        try:
            order = self.private_client.create_order(payload)
        except KalshiPrivateClientError as exc:
            raise LiveAdapterError(str(exc)) from exc
        return {
            "status": "SUBMITTED",
            "endpoint": f"{self.api_base}/portfolio/orders",
            "payload": payload,
            "response": order,
        }

    def get_order(self, order_id: str) -> dict[str, object]:
        if self.private_client is None:
            raise LiveAdapterError("no authenticated Kalshi client configured")
        return dict(self.private_client.get_order(order_id))

    def list_resting_orders(self, market_ticker: str | None = None) -> dict[str, object]:
        if self.private_client is None:
            raise LiveAdapterError("no authenticated Kalshi client configured")
        return dict(self.private_client.list_orders(ticker=market_ticker, status="resting"))

    def cancel_order(self, order_id: str) -> dict[str, object]:
        if self.private_client is None:
            raise LiveAdapterError("no authenticated Kalshi client configured")
        return dict(self.private_client.cancel_order(order_id))
