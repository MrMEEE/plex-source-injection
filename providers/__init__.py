"""Pluggable external metadata/streaming providers."""

from .base import (
    BaseProvider,
    ExternalTrack,
    ProviderConfigurationError,
    ProviderError,
    ProviderSetting,
    is_external_id,
    make_external_id,
    parse_external_id,
)
from .registry import ProviderRegistry, discover_providers, register_provider

__all__ = [
    "BaseProvider",
    "ExternalTrack",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderSetting",
    "ProviderRegistry",
    "discover_providers",
    "is_external_id",
    "make_external_id",
    "parse_external_id",
    "register_provider",
]
