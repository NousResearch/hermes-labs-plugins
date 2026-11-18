"""#45: an expired Copilot Enterprise token returns a bare 403 forbidden."""

import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from copilot.credentials import CopilotCredentialsMixin

p = CopilotCredentialsMixin()


def test_403_forbidden_is_stale():
    assert p.is_stale_credential_error(status_code=403, message="HTTP 403: forbidden")


def test_403_permission_denied_is_stale():
    assert p.is_stale_credential_error(status_code=403, message="PermissionDeniedError: forbidden")


def test_other_403_is_not_stale():
    assert not p.is_stale_credential_error(status_code=403, message="rate limit exceeded")


def test_400_integrator_still_stale():
    assert p.is_stale_credential_error(status_code=400, message="model_not_available_for_integrator")


def test_400_other_not_stale():
    assert not p.is_stale_credential_error(status_code=400, message="bad request")


def test_notice_mentions_403():
    assert "403" in p.credential_refresh_notice(reason="stale")
