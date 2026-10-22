from __future__ import annotations

from app.chains.base import ChainAdapter


class MonadAdapter(ChainAdapter):
    chain_name = "monad"

    def normalize_address(self, value: str) -> str:
        return value.strip().lower()

    def parse_transaction_event(self, payload: dict) -> dict:
        return {
            "chain": self.chain_name,
            "event_type": payload.get("event_type", "unknown"),
            "tx_hash": payload.get("tx_hash"),
            "wallet_address": payload.get("wallet_address"),
            "token_address": payload.get("token_address"),
        }
