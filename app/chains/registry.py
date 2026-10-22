from __future__ import annotations

from app.chains.base import ChainAdapter


class ChainRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ChainAdapter] = {}

    def register(self, adapter: ChainAdapter) -> None:
        self._adapters[adapter.chain_name] = adapter

    def get(self, chain_name: str) -> ChainAdapter | None:
        return self._adapters.get(chain_name)

    def list_enabled(self) -> list[str]:
        return sorted(self._adapters)


chain_registry = ChainRegistry()
