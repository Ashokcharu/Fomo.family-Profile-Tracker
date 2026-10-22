from app.chains.base_chain.adapter import BaseChainAdapter
from app.chains.bnb_chain.adapter import BNBChainAdapter
from app.chains.ethereum.adapter import EthereumAdapter
from app.chains.monad.adapter import MonadAdapter
from app.chains.registry import chain_registry
from app.chains.robinhood_chain.adapter import RobinhoodChainAdapter
from app.chains.solana.adapter import SolanaAdapter

for adapter in (
    SolanaAdapter(),
    RobinhoodChainAdapter(),
    BaseChainAdapter(),
    BNBChainAdapter(),
    EthereumAdapter(),
    MonadAdapter(),
):
    chain_registry.register(adapter)

__all__ = ["chain_registry"]
