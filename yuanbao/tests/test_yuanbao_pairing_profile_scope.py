"""Yuanbao home-channel designation reads the OWNING profile's pairing whitelist.

A multiplex gateway serves a secondary profile through its own adapter instance; a DM sender
approved only on that profile (``hermes -p coder pairing approve yuanbao <id>``) must pass
``_sender_may_designate_home``, which used to consult the default profile's store.
"""

from unittest.mock import patch

import pytest

from yuanbao_plugin.adapter import AccessPolicy

from test_yuanbao_pipeline import make_adapter, make_ctx


def _pairing_adapter(owner=None):
    adapter = make_adapter()
    adapter.set_owner_profile(owner)
    adapter._access_policy = AccessPolicy(dm_policy="pairing", dm_allow_from=[], group_policy="pairing", group_allow_from=[])
    return adapter


def test_secondary_profile_uses_its_own_pairing_store():
    adapter = _pairing_adapter("coder")
    ctx = make_ctx(adapter=adapter, chat_type="dm", from_account="approved-on-coder")
    with patch("gateway.pairing.PairingStore") as store_cls:
        store_cls.return_value.is_approved.return_value = True
        assert adapter._sender_may_designate_home(ctx) is True
    store_cls.assert_called_once_with(profile="coder")


def test_default_profile_uses_the_global_store():
    adapter = _pairing_adapter(None)
    ctx = make_ctx(adapter=adapter, chat_type="dm", from_account="approved")
    with patch("gateway.pairing.PairingStore") as store_cls:
        store_cls.return_value.is_approved.return_value = True
        assert adapter._sender_may_designate_home(ctx) is True
    store_cls.assert_called_once_with()


@pytest.mark.parametrize("owner, expected", [("coder", {"profile": "coder"}), (None, {})])
def test_core_without_the_private_owner_helper(monkeypatch, owner, expected):
    """``_owner_transport_profile`` is private to core's base adapter; a core without it keeps the scoping."""
    from gateway.platforms.base import BasePlatformAdapter

    monkeypatch.delattr(BasePlatformAdapter, "_owner_transport_profile", raising=False)
    adapter = _pairing_adapter(owner)
    ctx = make_ctx(adapter=adapter, chat_type="dm", from_account="approved")
    with patch("gateway.pairing.PairingStore") as store_cls:
        store_cls.return_value.is_approved.return_value = True
        assert adapter._sender_may_designate_home(ctx) is True
    store_cls.assert_called_once_with(**expected)
