"""Provider registry: discovery, instantiation and ratingKey dispatch."""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import TYPE_CHECKING, Iterable, TypeVar

from .base import (
    PROVIDER_PREFIX_RE,
    BaseProvider,
    ProviderConfigurationError,
    parse_external_id,
)

if TYPE_CHECKING:
    from config import Settings

logger = logging.getLogger(__name__)

P = TypeVar("P", bound=type[BaseProvider])

# name -> provider class
_PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {}
_INTERNAL_MODULES = {"base", "registry"}


def register_provider(cls: P) -> P:
    """Class decorator registering a :class:`BaseProvider` implementation."""
    if not (inspect.isclass(cls) and issubclass(cls, BaseProvider)):
        raise TypeError(f"{cls!r} is not a BaseProvider subclass")
    if inspect.isabstract(cls):
        missing = ", ".join(sorted(cls.__abstractmethods__))
        raise TypeError(f"{cls.__name__} does not implement: {missing}")
    for attr in ("name", "prefix", "display_name"):
        value = getattr(cls, attr, None)
        if not isinstance(value, str) or not value:
            raise TypeError(f"{cls.__name__} must define a non-empty string '{attr}'")
    if not PROVIDER_PREFIX_RE.match(cls.prefix):
        raise TypeError(f"{cls.__name__}.prefix must be lowercase alphanumeric: {cls.prefix!r}")
    name = cls.name.lower()
    for other in _PROVIDER_CLASSES.values():
        if other is cls:
            continue
        if other.name.lower() == name:
            raise ValueError(f"Provider name {name!r} already registered by {other.__name__}")
        if other.prefix == cls.prefix:
            raise ValueError(f"Provider prefix {cls.prefix!r} already used by {other.__name__}")
    _PROVIDER_CLASSES[name] = cls
    return cls


def unregister_provider(name: str) -> None:
    _PROVIDER_CLASSES.pop(name.lower(), None)


def discover_providers() -> dict[str, type[BaseProvider]]:
    """Import every module in the ``providers`` package so they self-register."""
    package = importlib.import_module(__package__)
    for module in pkgutil.iter_modules(package.__path__):
        if module.name in _INTERNAL_MODULES or module.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__package__}.{module.name}")
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to import provider module %s", module.name)
    return dict(_PROVIDER_CLASSES)


def available_providers() -> dict[str, type[BaseProvider]]:
    return dict(_PROVIDER_CLASSES)


class ProviderRegistry:
    """Holds the active (enabled and configured) provider instances."""

    def __init__(self, providers: Iterable[BaseProvider] = ()) -> None:
        self._by_name: dict[str, BaseProvider] = {}
        self._by_prefix: dict[str, BaseProvider] = {}
        for provider in providers:
            self.add(provider)

    def add(self, provider: BaseProvider) -> None:
        if provider.prefix in self._by_prefix:
            raise ValueError(f"Duplicate provider prefix {provider.prefix!r}")
        self._by_name[provider.name.lower()] = provider
        self._by_prefix[provider.prefix] = provider

    @classmethod
    def from_settings(cls, settings: "Settings") -> "ProviderRegistry":
        classes = discover_providers()
        registry = cls()
        for name in settings.enabled_providers:
            provider_cls = classes.get(name.lower())
            if provider_cls is None:
                logger.warning(
                    "Unknown provider %r in ENABLED_PROVIDERS (available: %s)",
                    name,
                    ", ".join(sorted(classes)) or "none",
                )
                continue
            try:
                registry.add(provider_cls(settings))
            except ProviderConfigurationError as exc:
                logger.warning("Provider %r disabled: %s", name, exc)
            else:
                logger.info("Provider %r enabled (prefix %r)", name, provider_cls.prefix)
        return registry

    @property
    def providers(self) -> list[BaseProvider]:
        return list(self._by_name.values())

    def get(self, name: str) -> BaseProvider | None:
        return self._by_name.get(name.lower())

    def get_by_prefix(self, prefix: str) -> BaseProvider | None:
        return self._by_prefix.get(prefix)

    def resolve(self, external_id: str) -> tuple[BaseProvider, str]:
        """Return the provider and item ID for a synthetic ratingKey.

        Raises ``KeyError`` if no enabled provider owns the prefix and
        ``ValueError`` if the ratingKey is malformed.
        """
        prefix, item_id = parse_external_id(external_id)
        provider = self._by_prefix.get(prefix)
        if provider is None:
            raise KeyError(f"No enabled provider for prefix {prefix!r}")
        return provider, item_id

    def __len__(self) -> int:
        return len(self._by_name)
