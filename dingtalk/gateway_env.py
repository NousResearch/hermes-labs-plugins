"""DingTalk env -> gateway config bridge (``PlatformEntry.env_enablement_fn``).

Core carried this as a ``gateway.platform_env.EnvCred`` row. That row needs a ``Platform`` member at
import time, which a plugin platform only has once it is registered, so the plugin seeds the same keys
through the public ``env_enablement_fn`` seam instead (the gateway enables the platform when
``is_connected`` sees Client ID + Client Secret, then merges this seed into ``platforms.dingtalk.extra``).
"""

from gateway.platforms._shared import get_scoped_secret, seed_extra_from_env

ENV_ENABLE_CREDENTIALS = ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET")


def env_enablement() -> dict:
    """``platforms.dingtalk.extra`` seed from env once DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET are set.

    ``DINGTALK_HOME_CHANNEL`` (+ ``_NAME``) becomes the home channel for cron ``deliver=dingtalk``.
    """
    client_id = str(get_scoped_secret("DINGTALK_CLIENT_ID", "") or "")
    client_secret = str(get_scoped_secret("DINGTALK_CLIENT_SECRET", "") or "")
    if not (client_id and client_secret):
        return {}
    return {"client_id": client_id, "client_secret": client_secret,
            **seed_extra_from_env((), home_env="DINGTALK_HOME_CHANNEL")}


# Authorization env names (``PlatformEntry`` fields).
AUTHZ = {
    "allowed_users_env": "DINGTALK_ALLOWED_USERS",
    "allow_all_env": "DINGTALK_ALLOW_ALL_USERS",
    "allowlist_suppresses_pairing": True,
}
