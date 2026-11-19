"""Yuanbao env -> gateway config bridge (``PlatformEntry.env_enablement_fn``).

Core carried this as a ``gateway.platform_env.EnvCred`` row. That row needs a ``Platform`` member at
import time, which a plugin platform only has once it is registered, so the plugin seeds the same keys
through the public ``env_enablement_fn`` seam instead (the gateway enables the platform when
``is_connected`` sees App ID + App Secret, then merges this seed into ``platforms.yuanbao.extra``).
"""

from gateway.platforms._shared import get_scoped_secret, seed_extra_from_env

ENV_ENABLE_CREDENTIALS = ("YUANBAO_APP_ID", "YUANBAO_APP_SECRET")


def _strip_lower(value: str) -> str:
    return value.strip().lower()


_OPTIONAL_EXTRAS = (
    ("YUANBAO_BOT_ID", "bot_id", None), ("YUANBAO_WS_URL", "ws_url", None),
    ("YUANBAO_API_DOMAIN", "api_domain", None), ("YUANBAO_ROUTE_ENV", "route_env", None),
    ("YUANBAO_DM_POLICY", "dm_policy", _strip_lower), ("YUANBAO_DM_ALLOW_FROM", "dm_allow_from", None),
    ("YUANBAO_GROUP_POLICY", "group_policy", _strip_lower), ("YUANBAO_GROUP_ALLOW_FROM", "group_allow_from", None),
)


def env_enablement() -> dict:
    """``platforms.yuanbao.extra`` seed from env once App ID (or the legacy App Key) and App Secret are set.

    YUANBAO_APP_ID is preferred over the legacy YUANBAO_APP_KEY; ``YUANBAO_HOME_CHANNEL`` (+ ``_NAME``)
    becomes the home channel for cron ``deliver=yuanbao``.
    """
    app_id = str(get_scoped_secret("YUANBAO_APP_ID", "") or get_scoped_secret("YUANBAO_APP_KEY", "") or "")
    app_secret = str(get_scoped_secret("YUANBAO_APP_SECRET", "") or "")
    if not (app_id and app_secret):
        return {}
    return {"app_id": app_id, "app_secret": app_secret,
            **seed_extra_from_env(_OPTIONAL_EXTRAS, home_env="YUANBAO_HOME_CHANNEL")}


# Authorization env names (``PlatformEntry`` fields).
AUTHZ = {
    "allowed_users_env": "YUANBAO_ALLOWED_USERS",
    "allow_all_env": "YUANBAO_ALLOW_ALL_USERS",
    "own_policy_env": ("YUANBAO_DM_POLICY", "YUANBAO_GROUP_POLICY"),
}
