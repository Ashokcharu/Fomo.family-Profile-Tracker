from __future__ import annotations

from app.chains.base import ChainAdapter


class BNBChainAdapter(ChainAdapter):
    chain_name = "bnb_chain"

    def normalize_address(self, value: str) -> str:
        return value.strip().lower()

    def parse_transaction_event(self, payload: dict) -> dict:
        return {
            "chain": self.chain_name,
            "event_type": payload.get("event_type", "unknown"),
            "contract_address": payload.get("contract_address"),
            "tx_hash": payload.get("tx_hash"),
            "wallet_address": payload.get("wallet_address"),
        }
