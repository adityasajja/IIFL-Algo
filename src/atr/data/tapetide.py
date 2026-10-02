"""Tapetide Indian Stock Research and Market Intel client.

Exposes fundamental ratios, FII/DII flow statistics, technical screeners,
and end-of-day options chains using the configured Tapetide MCP token.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from loguru import logger

TAPETIDE_MCP_URL = "https://mcp.tapetide.com/mcp"


class TapetideClient:
    """Client for querying Tapetide MCP research tools over Streamable HTTP JSON-RPC."""

    def __init__(self, token: str | None = None, timeout: float = 15.0) -> None:
        self.token = token or os.getenv("TAPETIDE_TOKEN") or ""
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call any Tapetide MCP tool by name with arguments."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments or {},
            },
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                res = client.post(TAPETIDE_MCP_URL, headers=self._headers, json=payload)
                if res.status_code != 200:
                    logger.warning("Tapetide MCP call failed: HTTP {} -> {}", res.status_code, res.text[:200])
                    return None
                data = res.json()
                if "error" in data:
                    logger.warning("Tapetide MCP error: {}", data["error"])
                    return None
                result = data.get("result", {})
                content_list = result.get("content", [])
                if content_list and content_list[0].get("type") == "text":
                    text = content_list[0].get("text", "")
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text
                return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error invoking Tapetide tool {}: {}", name, exc)
            return None

    # Convenience wrappers for algorithmic trading workflows:
    def get_market_pulse(self) -> dict[str, Any] | None:
        """Market overview including FII/DII institutional activity and sector performance."""
        return self.call_tool("get_market_pulse")

    def get_fii_dii_flows(self) -> dict[str, Any] | None:
        """Historical and recent foreign/domestic institutional net flows."""
        return self.call_tool("get_fii_dii_flows")

    def get_option_chain(self, symbol: str = "NIFTY") -> dict[str, Any] | None:
        """End-of-day option chain snapshot with open interest and Greeks."""
        return self.call_tool("get_option_chain", {"symbol": symbol.upper()})

    def screen_stocks(self, conditions: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """Fundamental and technical screening against NSE/BSE universe."""
        return self.call_tool("screen_stocks", {"conditions": conditions or []})

    def get_company_profile(self, symbol: str) -> dict[str, Any] | None:
        """Fetch company fundamentals, valuation multiples, and business profile."""
        return self.call_tool("get_company_profile", {"symbol": symbol.upper()})
