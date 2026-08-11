from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from prisma.enums import SubscriptionTier

from backend.util.cache import cached
from backend.util.clients import get_database_manager_async_client
from backend.util.settings import BehaveAs, Settings


class Entitlement(str, Enum):
    CODEX_SUBSCRIPTION_TRANSPORT = "codex_subscription_transport"


@dataclass(frozen=True)
class EntitlementPolicy:
    minimum_tier: SubscriptionTier
    allow_local: bool = False


ENTITLEMENT_POLICIES: Mapping[Entitlement, EntitlementPolicy] = MappingProxyType(
    {
        Entitlement.CODEX_SUBSCRIPTION_TRANSPORT: EntitlementPolicy(
            minimum_tier=SubscriptionTier.MAX,
            allow_local=True,
        ),
    }
)

_TIER_ORDER = (
    SubscriptionTier.NO_TIER,
    SubscriptionTier.BASIC,
    SubscriptionTier.PRO,
    SubscriptionTier.MAX,
    SubscriptionTier.BUSINESS,
    SubscriptionTier.ENTERPRISE,
)
_TIER_RANK = {tier: rank for rank, tier in enumerate(_TIER_ORDER)}

settings = Settings()


class EntitlementRequiredError(Exception):
    def __init__(self, entitlement: Entitlement, minimum_tier: SubscriptionTier):
        self.entitlement = entitlement
        self.minimum_tier = minimum_tier
        super().__init__(
            f"{entitlement.value} requires a {minimum_tier.value} plan or higher"
        )


class _EntitlementUserNotFoundError(Exception):
    pass


@cached(maxsize=1000, ttl_seconds=300, shared_cache=True)
async def _fetch_entitlement_user_subscription_tier(
    user_id: str,
) -> SubscriptionTier:
    """Resolve and cache an authoritative tier through DatabaseManager.

    Only successful lookups are cached. Missing users and transient failures
    raise, so neither can poison the shared cache.
    """
    try:
        tier = await get_database_manager_async_client().get_user_subscription_tier(
            user_id
        )
    except ValueError as exc:
        raise _EntitlementUserNotFoundError(user_id) from exc
    return SubscriptionTier(tier)


async def _get_user_subscription_tier(user_id: str) -> SubscriptionTier:
    try:
        return await _fetch_entitlement_user_subscription_tier(user_id)
    except _EntitlementUserNotFoundError:
        return SubscriptionTier.NO_TIER


def invalidate_user_entitlement_cache(user_id: str) -> None:
    _fetch_entitlement_user_subscription_tier.cache_delete(user_id)


async def has_entitlement(user_id: str, entitlement: Entitlement) -> bool:
    """Check one centralized entitlement policy against the user's DB tier."""
    policy = ENTITLEMENT_POLICIES[entitlement]
    if policy.allow_local and settings.config.behave_as == BehaveAs.LOCAL:
        return True

    tier = await _get_user_subscription_tier(user_id)
    return _TIER_RANK[tier] >= _TIER_RANK[policy.minimum_tier]


async def require_entitlement(user_id: str, entitlement: Entitlement) -> None:
    """Raise when the centralized entitlement policy is not satisfied."""
    policy = ENTITLEMENT_POLICIES[entitlement]
    if not await has_entitlement(user_id, entitlement):
        raise EntitlementRequiredError(entitlement, policy.minimum_tier)
