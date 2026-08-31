from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .contracts import (
    AffinityExtractor,
    BusinessFeatureExtractor,
    ContextFusionProvider,
    FeatureComparator,
    Judge,
    QueryParser,
)


@dataclass(slots=True)
class BusinessPlugin:
    """Endpoint-scoped business strategies; runtime resources remain service-scoped."""

    endpoint_id: str
    parser: QueryParser[Any]
    feature_extractor: BusinessFeatureExtractor
    feature_comparator: FeatureComparator
    fusion: ContextFusionProvider
    judge: Judge
    affinity_extractor: AffinityExtractor | None = None
    context_fingerprint: str | None = None

    def fusion_fingerprint(self) -> str:
        if self.context_fingerprint:
            return self.context_fingerprint
        explicit = getattr(self.fusion, "fingerprint", None)
        if explicit:
            return str(explicit)
        identity = f"{type(self.fusion).__module__}.{type(self.fusion).__qualname__}"
        return hashlib.sha256(identity.encode()).hexdigest()


class BusinessPluginRegistry:
    """Resolve exact endpoint plugins with an explicitly configurable default fallback."""

    def __init__(
        self,
        default_plugin: BusinessPlugin | None = None,
        *,
        allow_default_plugin: bool = True,
    ) -> None:
        self._plugins: dict[str, BusinessPlugin] = {}
        self.default_plugin = default_plugin
        self.allow_default_plugin = allow_default_plugin
        self._revision = 0

    def register(self, plugin: BusinessPlugin) -> None:
        if plugin.endpoint_id == "*":
            if self.default_plugin is not None:
                raise ValueError("default BusinessPlugin is already registered")
            self.default_plugin = plugin
        elif plugin.endpoint_id in self._plugins:
            raise ValueError(f"BusinessPlugin already registered for {plugin.endpoint_id}")
        else:
            self._plugins[plugin.endpoint_id] = plugin
        self._revision += 1

    def get(self, endpoint_id: str) -> BusinessPlugin:
        plugin = self._plugins.get(endpoint_id)
        if plugin is not None:
            return plugin
        if self.allow_default_plugin and self.default_plugin is not None:
            return self.default_plugin
        raise KeyError(f"no BusinessPlugin registered for endpoint {endpoint_id}")

    def contains(self, endpoint_id: str) -> bool:
        return endpoint_id in self._plugins

    def resolve(self, endpoint_id: str) -> ContextFusionProvider:
        return self.get(endpoint_id).fusion

    @property
    def fingerprint(self) -> str:
        entries = [
            f"{endpoint}:{plugin.fusion_fingerprint()}"
            for endpoint, plugin in sorted(self._plugins.items())
        ]
        if self.allow_default_plugin and self.default_plugin is not None:
            entries.append(f"*:{self.default_plugin.fusion_fingerprint()}")
        entries.append(f"revision:{self._revision}")
        return hashlib.sha256("|".join(entries).encode()).hexdigest()
