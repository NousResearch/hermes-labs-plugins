"""Copilot / GitHub Models provider profile.

Core routes GPT-5+/Codex -> codex_responses and Claude -> anthropic_messages;
this profile covers the chat_completions remainder: editor attribution headers
(copilot_default_headers()) and catalog-gated GitHub Models reasoning.
"""

from typing import Any

from providers.base import ProviderProfile

from .auth_rows import AUTH_ROW_FIELDS
from .catalog import CATALOG_FIELDS, HOSTNAMES, METADATA_HOSTNAMES, MODELS_DEV_ID, CopilotCatalogMixin
from .credentials import CopilotCredentialsMixin
from .picker import PICKER_FIELDS
from .responses import RESPONSES_PROFILE_FIELDS, CopilotResponsesMixin
from .setup import SETUP_FIELDS
from .wire import CopilotWireMixin


class CopilotProfile(CopilotWireMixin, CopilotResponsesMixin, CopilotCatalogMixin, CopilotCredentialsMixin, ProviderProfile):
    """GitHub Copilot / GitHub Models — editor headers + reasoning."""

    def build_api_kwargs_extras(
        self, *, model: str | None = None, reasoning_config: dict | None = None,
        supports_reasoning: bool = False, **ctx,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not (supports_reasoning and model):
            return {}, {}
        try:
            from .catalog import clamp_github_reasoning_effort, github_model_reasoning_efforts

            supported = github_model_reasoning_efforts(model)
            if not supported:
                return {}, {}
            if not reasoning_config:
                return {"reasoning": {"effort": "medium"}}, {}
            # Never drop straight to medium, which inverted the ladder (ultra < high). See #74295.
            effort = clamp_github_reasoning_effort(reasoning_config.get("effort"), supported)
            return {"reasoning": {"effort": effort}}, {}
        except Exception:
            return {}, {}

    def get_usage_cost(self, model: str, usage: Any, **context: Any) -> Any | None:
        # The amount GitHub reports in ``copilot_usage`` when core carried it into ``usage.raw_usage``
        # (``_usage_field_kwargs`` below), else an estimate at the vendor's list rate (copilot/pricing.py). This
        # profile also answers the github-models aliases (models.github.ai), whose billing may differ. Today's
        # hook receives only (model, usage), so the notes name no product; a core that also passes the request's
        # ``base_url`` lets GitHub Models calls skip the Copilot-rate estimate (a reported amount still counts).
        try:
            from .catalog import _is_github_models_host
            from .pricing import copilot_usage_cost
        except Exception:  # a broken pricing module must never break a turn
            return None
        return copilot_usage_cost(model, usage, estimate=not _is_github_models_host(context.get("base_url")))


def _usage_field_kwargs() -> dict[str, Any]:
    """Ask core to carry ``copilot_usage`` into ``usage.raw_usage`` when it supports
    ``ProviderProfile.usage_response_fields``; an older core gets nothing and keeps the estimate."""
    import dataclasses

    if any(f.name == "usage_response_fields" for f in dataclasses.fields(ProviderProfile)):
        return {"usage_response_fields": ("copilot_usage",)}
    return {}


copilot = CopilotProfile(
    name="copilot", aliases=("github-copilot", "github-models", "github-model", "github"),
    env_vars=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"), base_url="https://api.githubcopilot.com",
    auth_type="copilot", ambient_credentials=True, **RESPONSES_PROFILE_FIELDS, **SETUP_FIELDS,
    hostnames=HOSTNAMES, metadata_hostnames=METADATA_HOSTNAMES, models_dev_id=MODELS_DEV_ID, **CATALOG_FIELDS,
    **PICKER_FIELDS, **AUTH_ROW_FIELDS, **_usage_field_kwargs(),
)
