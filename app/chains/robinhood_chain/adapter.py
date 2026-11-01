from __future__ import annotations

from app.chains.base import ChainAdapter


class RobinhoodChainAdapter(ChainAdapter):
    chain_name = "robinhood_chain"

    def normalize_address(self, value: str) -> str:
        return value.strip().lower()

    def parse_transaction_event(self, payload: dict) -> dict:
        return {
            "chain": self.chain_name,
            "event_type": payload.get("event_type", "unknown"),
            "wallet_address": payload.get("wallet_address"),
            "token_address": payload.get("token_address"),
            "quote_amount": payload.get("quote_amount"),
        }
