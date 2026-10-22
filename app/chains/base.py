from __future__ import annotations

from abc import ABC, abstractmethod


class ChainAdapter(ABC):
    """Base adapter for per-chain token and transaction parsing."""

    chain_name: str = "base"

    @abstractmethod
    def normalize_address(self, value: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def parse_transaction_event(self, payload: dict) -> dict:
        raise NotImplementedError
