"""yuanbao — Tencent Yuanbao (腾讯元宝) gateway platform + yb_* tools for Hermes Agent (Hermes Labs plugin).

Formerly built into hermes-agent core (``plugins/platforms/yuanbao`` and ``tools/yuanbao_tools.py``).
Everything a migrated install relies on is unchanged: platform name ``yuanbao``, toolset ``yuanbao``,
tools ``yb_query_group_info`` / ``yb_query_group_members`` / ``yb_send_dm`` / ``yb_search_sticker`` /
``yb_send_sticker``, the ``YUANBAO_*`` env vars and the ``platforms.yuanbao`` config keys.

Wiring goes only through the public plugin API (``ctx.register_platform`` / ``ctx.register_tool``), and
``register()`` does nothing on a core that still bundles Yuanbao.
"""

from .register import register

__all__ = ["register"]
