from __future__ import annotations
from .gemini_parser import parse_query, SemanticParseError
from .models import StructuredQuery

QueryResolutionError = SemanticParseError

def resolve_query(user_query: str, **kwargs) -> StructuredQuery:
    """Compatibility entry point: Gemini parses semantics only."""
    return parse_query(user_query, **kwargs)
