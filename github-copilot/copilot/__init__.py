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

    def fetch_account_usage(
        self, *, base_url: str | None = None, api_key: str | None = None,
    ):
        from .usage import fetch_copilot_account_usage

        return fetch_copilot_account_usage(base_url=base_url, api_key=api_key)


copilot = CopilotProfile(
    name="copilot", aliases=("github-copilot", "github-models", "github-model", "github"),
    env_vars=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"), base_url="https://api.githubcopilot.com",
    auth_type="copilot", ambient_credentials=True, **RESPONSES_PROFILE_FIELDS, **SETUP_FIELDS,
    hostnames=HOSTNAMES, metadata_hostnames=METADATA_HOSTNAMES, models_dev_id=MODELS_DEV_ID, **CATALOG_FIELDS,
    **PICKER_FIELDS, **AUTH_ROW_FIELDS,
)
