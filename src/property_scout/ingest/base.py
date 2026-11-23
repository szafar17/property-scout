"""Abstract source interface and a small registry/factory.

Every MLS adapter implements :class:`BaseListingSource` and yields normalized
:class:`~property_scout.models.Listing` objects, so the orchestrator never
needs to know whether the data came from Trestle OData, a RETS feed, or a
local JSON fixture.
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Iterable, Iterator

from property_scout.config import FilterCriteria
from property_scout.models import Listing


class BaseListingSource(abc.ABC):
    """Contract for a listing source adapter.

    Implementations should perform any auth lazily (on first ``fetch_active``)
    and translate the source's native records into :class:`Listing` objects via
    their own ``_normalize`` step.
    """

    #: Short, stable name used by the registry (e.g. ``"hcmls"``).
    name: str = "base"

    @abc.abstractmethod
    def fetch_active(self, criteria: FilterCriteria) -> Iterator[Listing]:
        """Yield active listings matching ``criteria`` for this source.

        Adapters should push as much of the filtering as possible to the remote
        query (price, status, county) and let the pipeline handle the rest.
        """
        raise NotImplementedError

    def healthcheck(self) -> bool:
        """Cheap liveness probe. Override to hit the source's auth endpoint."""
        return True


# --------------------------------------------------------------------------- #
# Registry / factory                                                           #
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, Callable[..., BaseListingSource]] = {}


def register_source(name: str) -> Callable[[Callable[..., BaseListingSource]],
                                           Callable[..., BaseListingSource]]:
    """Class decorator registering a source factory under ``name``."""

    def _wrap(factory: Callable[..., BaseListingSource]) -> Callable[..., BaseListingSource]:
        _REGISTRY[name] = factory
        return factory

    return _wrap


def build_source(name: str, **kwargs) -> BaseListingSource:
    """Instantiate a registered source by name.

    Raises:
        KeyError: if ``name`` was never registered (import the adapter module
            first so its ``@register_source`` decorator has run).
    """
    if name not in _REGISTRY:
        raise KeyError(f"Unknown source {name!r}; registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


def available_sources() -> list[str]:
    """Names of all currently-registered sources."""
    return sorted(_REGISTRY)


def fetch_all(sources: Iterable[BaseListingSource], criteria: FilterCriteria) -> list[Listing]:
    """Fetch active listings from every source, concatenated.

    A failure in one source is allowed to raise; the orchestrator decides
    whether a partial run is acceptable.
    """
    listings: list[Listing] = []
    for src in sources:
        listings.extend(src.fetch_active(criteria))
    return listings
