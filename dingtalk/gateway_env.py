"""DingTalk env -> gateway config bridge (``PlatformEntry.env_overrides``)."""

from gateway.config import Platform
from gateway.platform_env import EnvCred

ENV_ENABLE_CREDENTIALS = ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET")
ENV_OVERRIDES = (
    EnvCred(
        Platform.DINGTALK, ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
        fixed=(("client_id", "DINGTALK_CLIENT_ID"), ("client_secret", "DINGTALK_CLIENT_SECRET")),
        home="DINGTALK_HOME_CHANNEL",
    ),
)
