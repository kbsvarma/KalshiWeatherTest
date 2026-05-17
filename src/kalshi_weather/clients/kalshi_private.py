from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import time
from typing import Any, Mapping

from .http import HttpRequestError, http_request_json


API_PREFIX = "/trade-api/v2"
PROD_ROOT_URL = "https://api.elections.kalshi.com"
DEMO_ROOT_URL = "https://demo-api.kalshi.co"


class KalshiAuthError(RuntimeError):
    """Raised when signed authenticated requests cannot be prepared safely."""


class KalshiPrivateClientError(RuntimeError):
    """Raised when authenticated Kalshi requests fail."""


@dataclass(frozen=True, slots=True)
class KalshiCredentials:
    access_key: str
    private_key_path: Path
    root_url: str
    subaccount: int = 0


class KalshiSigner:
    def __init__(self, access_key: str, private_key_path: Path | str) -> None:
        self.access_key = access_key
        self.private_key_path = Path(private_key_path)

    def sign_text(self, text: str) -> str:
        if not self.private_key_path.exists():
            raise KalshiAuthError(f"missing Kalshi private key: {self.private_key_path}")
        try:
            return self._sign_with_cryptography(text)
        except ModuleNotFoundError:
            return self._sign_with_openssl(text)

    def build_headers(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        timestamp_ms = timestamp_ms or int(time.time() * 1000)
        path_without_query = path.split("?")[0]
        message = f"{timestamp_ms}{method.upper()}{path_without_query}"
        return {
            "KALSHI-ACCESS-KEY": self.access_key,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": self.sign_text(message),
        }

    def _sign_with_cryptography(self, text: str) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        private_key = serialization.load_pem_private_key(
            self.private_key_path.read_bytes(),
            password=None,
        )
        signature = private_key.sign(
            text.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _sign_with_openssl(self, text: str) -> str:
        try:
            result = subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-sigopt",
                    "rsa_padding_mode:pss",
                    "-sigopt",
                    "rsa_pss_saltlen:digest",
                    "-sign",
                    str(self.private_key_path),
                ],
                input=text.encode("utf-8"),
                capture_output=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise KalshiAuthError(
                "cryptography is unavailable and openssl is not installed; cannot sign Kalshi requests"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise KalshiAuthError(
                f"openssl failed while signing Kalshi request: {exc.stderr.decode('utf-8', errors='replace')}"
            ) from exc
        return base64.b64encode(result.stdout).decode("utf-8")


class KalshiPrivateClient:
    def __init__(self, credentials: KalshiCredentials, signer: KalshiSigner | None = None) -> None:
        self.credentials = credentials
        self.signer = signer or KalshiSigner(
            access_key=credentials.access_key,
            private_key_path=credentials.private_key_path,
        )

    @classmethod
    def from_env(cls) -> "KalshiPrivateClient":
        access_key = os.environ.get("KALSHI_ACCESS_KEY") or os.environ.get("KALSHI_API_KEY_ID")
        private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        environment = os.environ.get("KALSHI_ENV", "prod").lower()
        subaccount = int(os.environ.get("KALSHI_SUBACCOUNT", "0"))
        if not access_key or not private_key_path:
            raise KalshiAuthError(
                "KALSHI_ACCESS_KEY (or KALSHI_API_KEY_ID) and KALSHI_PRIVATE_KEY_PATH must be set for authenticated requests"
            )
        root_url = DEMO_ROOT_URL if environment == "demo" else PROD_ROOT_URL
        return cls(
            KalshiCredentials(
                access_key=access_key,
                private_key_path=Path(private_key_path),
                root_url=root_url,
                subaccount=subaccount,
            )
        )

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        api_path = f"{API_PREFIX}{path}"
        try:
            headers = self.signer.build_headers(method=method, path=api_path)
            return http_request_json(
                f"{self.credentials.root_url}{api_path}",
                method=method,
                params=params,
                headers=headers,
                payload=payload,
            )
        except (KalshiAuthError, HttpRequestError) as exc:
            raise KalshiPrivateClientError(str(exc)) from exc

    def create_order(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.request_json("POST", "/portfolio/orders", payload=payload)

    def list_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return self.request_json("GET", "/portfolio/orders", params=params)

    def get_order(self, order_id: str) -> Mapping[str, Any]:
        return self.request_json("GET", f"/portfolio/orders/{order_id}")

    def cancel_order(self, order_id: str) -> Mapping[str, Any]:
        params = {"subaccount": self.credentials.subaccount}
        return self.request_json("DELETE", f"/portfolio/orders/{order_id}", params=params)

    def amend_order(self, order_id: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.request_json("POST", f"/portfolio/orders/{order_id}/amend", payload=payload)

    def get_user_data_timestamp(self) -> Mapping[str, Any]:
        return self.request_json("GET", "/exchange/user_data_timestamp")
