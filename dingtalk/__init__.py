"""dingtalk — DingTalk (钉钉) gateway platform for Hermes Agent (Hermes Labs plugin).

Formerly built into hermes-agent core (``plugins/platforms/dingtalk`` and ``hermes_cli/dingtalk_auth.py``).
Everything a migrated install relies on is unchanged: platform name ``dingtalk``, the ``DINGTALK_*`` env
vars and the ``platforms.dingtalk`` config keys.

Wiring goes only through the public plugin API (``ctx.register_platform``), and ``register()`` does
nothing on a core that still bundles DingTalk.
"""

from .register import register

__all__ = ["register"]
