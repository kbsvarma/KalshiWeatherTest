from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, Sequence

from .kalshi_private import (
    DEMO_ROOT_URL,
    PROD_ROOT_URL,
    KalshiAuthError,
    KalshiCredentials,
    KalshiPrivateClient,
    KalshiSigner,
)


PROD_WSS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
DEMO_WSS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"


class KalshiWebSocketError(RuntimeError):
    """Raised when the Kalshi WebSocket client cannot stream safely."""


@dataclass(frozen=True, slots=True)
class SubscriptionSpec:
    channels: tuple[str, ...]
    market_tickers: tuple[str, ...] = ()


def build_subscribe_command(spec: SubscriptionSpec, request_id: int) -> dict[str, Any]:
    params: dict[str, Any] = {"channels": list(spec.channels)}
    if spec.market_tickers:
        params["market_tickers"] = list(spec.market_tickers)
    return {
        "id": request_id,
        "cmd": "subscribe",
        "params": params,
    }


class KalshiWebSocketClient:
    def __init__(
        self,
        credentials: KalshiCredentials,
        signer: KalshiSigner | None = None,
        ws_url: str | None = None,
    ) -> None:
        self.credentials = credentials
        self.signer = signer or KalshiSigner(
            access_key=credentials.access_key,
            private_key_path=credentials.private_key_path,
        )
        self.ws_url = ws_url or (
            DEMO_WSS_URL if credentials.root_url == DEMO_ROOT_URL else PROD_WSS_URL
        )

    @classmethod
    def from_env(cls) -> "KalshiWebSocketClient":
        private_client = KalshiPrivateClient.from_env()
        return cls(private_client.credentials, signer=private_client.signer)

    def build_headers(self) -> dict[str, str]:
        return self.signer.build_headers("GET", "/trade-api/ws/v2")

    async def stream(
        self,
        subscription_specs: Sequence[SubscriptionSpec],
        *,
        max_messages: int | None = None,
        idle_timeout_seconds: float = 10.0,
    ) -> AsyncIterator[Mapping[str, Any]]:
        try:
            import websockets
        except ModuleNotFoundError as exc:
            raise KalshiWebSocketError(
                "websockets package is required for Kalshi streaming"
            ) from exc

        headers = self.build_headers()
        request_id = 1
        try:
            async with websockets.connect(
                self.ws_url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=20,
                max_queue=None,
            ) as websocket:
                for spec in subscription_specs:
                    await websocket.send(json.dumps(build_subscribe_command(spec, request_id), sort_keys=True))
                    request_id += 1

                message_count = 0
                while True:
                    try:
                        raw_message = await asyncio.wait_for(
                            websocket.recv(),
                            timeout=idle_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        break
                    payload = json.loads(raw_message)
                    yield payload
                    message_count += 1
                    if max_messages is not None and message_count >= max_messages:
                        break
        except KalshiAuthError:
            raise
        except Exception as exc:
            raise KalshiWebSocketError(str(exc)) from exc
