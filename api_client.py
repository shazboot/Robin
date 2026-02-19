import base64
import datetime
import json
from typing import Any, Dict, Optional
import uuid
import requests
from nacl.signing import SigningKey

from config import API_KEY, BASE64_PRIVATE_KEY, API_VERSION


class CryptoAPITrading:
    def __init__(self):
        self.api_key = API_KEY
        private_key_seed = base64.b64decode(BASE64_PRIVATE_KEY)
        self.private_key = SigningKey(private_key_seed)
        self.base_url = "https://trading.robinhood.com"
        self.api_version = API_VERSION
        self._account_number = None  # cached for v2

    def _path(self, endpoint: str) -> str:
        return f"/api/{self.api_version}/crypto/{endpoint}"

    @staticmethod
    def _get_current_timestamp() -> int:
        return int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp())

    @staticmethod
    def _build_query_params(key: str, *args: Optional[str]) -> str:
        if not args:
            return ""
        params = [f"{key}={arg}" for arg in args]
        return "?" + "&".join(params)

    def _get_authorization_header(
        self, method: str, path: str, body: str, timestamp: int
    ) -> Dict[str, str]:
        message_to_sign = f"{self.api_key}{timestamp}{path}{method}{body}"
        signed = self.private_key.sign(message_to_sign.encode("utf-8"))
        return {
            "x-api-key": self.api_key,
            "x-signature": base64.b64encode(signed.signature).decode("utf-8"),
            "x-timestamp": str(timestamp),
        }

    def make_api_request(self, method: str, path: str, body: str = "") -> Any:
        timestamp = self._get_current_timestamp()
        headers = self._get_authorization_header(method, path, body, timestamp)
        url = self.base_url + path

        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=10)
            elif method == "POST":
                response = requests.post(
                    url,
                    headers=headers,
                    json=json.loads(body) if body else None,
                    timeout=10,
                )
            else:
                raise ValueError(f"Unsupported method: {method}")

            if response.status_code >= 400:
                print(f"HTTP Error {response.status_code}: {response.text}")
            return response.json()
        except requests.RequestException as e:
            print(f"Error making API request: {e}")
            return None

    # ---- Account ----

    def get_account(self) -> Any:
        if self.api_version == "v2":
            path = self._path("trading/accounts/")
            result = self.make_api_request("GET", path)
            if result and "results" in result and result["results"]:
                self._account_number = result["results"][0]["account_number"]
            return result
        else:
            path = self._path("trading/accounts/")
            return self.make_api_request("GET", path)

    def get_account_number(self) -> str:
        if self._account_number:
            return self._account_number
        account = self.get_account()
        if self.api_version == "v2":
            return self._account_number
        return account.get("account_number", "")

    # ---- Market Data ----

    def get_best_bid_ask(self, *symbols: Optional[str]) -> Any:
        query_params = self._build_query_params("symbol", *symbols)
        path = self._path(f"marketdata/best_bid_ask/{query_params}")
        return self.make_api_request("GET", path)

    def get_estimated_price(self, symbol: str, side: str, quantity: str) -> Any:
        if self.api_version == "v2":
            path = self._path(
                f"trading/estimated_price/?symbol={symbol}&side={side}&quantity={quantity}"
            )
        else:
            path = self._path(
                f"marketdata/estimated_price/?symbol={symbol}&side={side}&quantity={quantity}"
            )
        return self.make_api_request("GET", path)

    # ---- Trading ----

    def get_trading_pairs(self, *symbols: Optional[str]) -> Any:
        query_params = self._build_query_params("symbol", *symbols)
        path = self._path(f"trading/trading_pairs/{query_params}")
        return self.make_api_request("GET", path)

    def get_holdings(self, *asset_codes: Optional[str]) -> Any:
        if self.api_version == "v2":
            acct = self.get_account_number()
            params = f"?account_number={acct}"
            if asset_codes:
                for code in asset_codes:
                    params += f"&asset_code={code}"
            path = self._path(f"trading/holdings/{params}")
        else:
            query_params = self._build_query_params("asset_code", *asset_codes)
            path = self._path(f"trading/holdings/{query_params}")
        return self.make_api_request("GET", path)

    def place_order(
        self,
        side: str,
        order_type: str,
        symbol: str,
        order_config: Dict[str, str],
    ) -> Any:
        body = {
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "type": order_type,
            "symbol": symbol,
            f"{order_type}_order_config": order_config,
        }
        if self.api_version == "v2":
            acct = self.get_account_number()
            path = self._path(f"trading/orders/?account_number={acct}")
        else:
            path = self._path("trading/orders/")
        return self.make_api_request("POST", path, json.dumps(body))

    def cancel_order(self, order_id: str) -> Any:
        path = self._path(f"trading/orders/{order_id}/cancel/")
        return self.make_api_request("POST", path)

    def get_order(self, order_id: str) -> Any:
        if self.api_version == "v2":
            acct = self.get_account_number()
            path = self._path(f"trading/orders/{order_id}/?account_number={acct}")
        else:
            path = self._path(f"trading/orders/{order_id}/")
        return self.make_api_request("GET", path)

    def get_orders(self) -> Any:
        if self.api_version == "v2":
            acct = self.get_account_number()
            path = self._path(f"trading/orders/?account_number={acct}")
        else:
            path = self._path("trading/orders/")
        return self.make_api_request("GET", path)
