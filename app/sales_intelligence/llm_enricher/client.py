"""Cliente Anthropic singleton + IDs de modelo."""
import os
from anthropic import Anthropic
from functools import lru_cache

MODEL_HAIKU = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-6"


@lru_cache(maxsize=1)
def get_client() -> Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY ausente no .env")
    return Anthropic(api_key=key)
