from datetime import UTC, datetime

from app.db.models import SubscriptionStatus, User


def access_ends_at(user: User) -> datetime:
    candidates = [user.trial_ends_at]
    if user.vip_ends_at:
        candidates.append(user.vip_ends_at)
    return max(candidates)


def has_access(user: User, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return not user.is_access_disabled and access_ends_at(user) > now


def refresh_subscription_status(user: User, now: datetime | None = None) -> SubscriptionStatus:
    now = now or datetime.now(UTC)
    if user.is_access_disabled:
        user.subscription_status = SubscriptionStatus.disabled
    elif user.vip_ends_at and user.vip_ends_at > now:
        user.subscription_status = SubscriptionStatus.vip
    elif user.trial_ends_at > now:
        user.subscription_status = (
            SubscriptionStatus.referral if user.referral_bonus_granted_at else SubscriptionStatus.trial
        )
    else:
        user.subscription_status = SubscriptionStatus.expired
    return user.subscription_status
