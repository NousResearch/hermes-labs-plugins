"""Profile clones (the plugin's registered entry, conftest): every YUANBAO_* key is the bot's channel identity, so a channel-less clone strips them all."""

import pytest

_KEYS = ("YUANBAO_APP_ID", "YUANBAO_APP_KEY", "YUANBAO_APP_SECRET", "YUANBAO_BOT_ID", "YUANBAO_WS_URL",
         "YUANBAO_API_DOMAIN", "YUANBAO_HOME_CHANNEL", "YUANBAO_DM_POLICY", "YUANBAO_ALLOWED_USERS")


@pytest.mark.plugin_registry
@pytest.mark.parametrize("env", [
    "YUANBAO_APP_KEY=legacy-id\nYUANBAO_APP_SECRET=s3cret\n",  # legacy credential name core still ran
    "YUANBAO_APP_ID=app\n",                                    # incomplete set: never ran the adapter
    "",
])
def test_clone_strips_every_yuanbao_key(tmp_path, env):
    profile_channels = pytest.importorskip("hermes_cli.profile_channels")
    index_cls = getattr(profile_channels, "ChannelKeyIndex", None)
    if index_cls is None:
        pytest.skip("this Hermes core has no profile-clone channel index")
    source = tmp_path / "source"
    source.mkdir()
    (source / ".env").write_text(env)
    index = index_cls(source)
    assert {key: index.platform_for(key) for key in _KEYS} == dict.fromkeys(_KEYS, "yuanbao")
