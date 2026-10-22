from __future__ import annotations


class ModelInterface:
    """Replaceable interface for future local or remote model adapters."""

    def predict(self, *args, **kwargs):
        raise NotImplementedError("AI inference is intentionally disabled in this scaffold")
