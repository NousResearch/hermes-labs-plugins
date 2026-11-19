"""strip_cron_wrapper follows both cron wrapper shapes core has shipped (#66)."""

from __future__ import annotations

import pytest

from yuanbao_plugin.adapter import MessageSender

PAYLOAD = "Daily report\n\n-------------\nmarkdown rule above is payload, not wrapper"


def _header_first(content: str) -> str:
    # cron/scheduler_delivery.py _deliver_result before hermes-agent#99452
    return (
        "Cronjob Response: daily\n(job_id: abc123)\n-------------\n\n"
        f"{content}\n\n"
        'To stop or manage this job, send me a new message (e.g. "stop reminder daily").'
    )


def _footer(content: str) -> str:
    # cron/scheduler_delivery.py _deliver_result after hermes-agent#99452
    return f"{content}\n\n-------------\nCronjob Response: daily\n(job_id: abc123)"


@pytest.mark.parametrize("wrap", [_header_first, _footer], ids=["header-first", "footer"])
def test_wrapper_is_stripped_to_the_payload(wrap):
    assert MessageSender.strip_cron_wrapper(wrap(PAYLOAD)) == PAYLOAD


@pytest.mark.parametrize("text", [
    PAYLOAD,
    "notes\n\n-------------\nCronjob Response: is a phrase I quote here\nand more text",
    _footer(PAYLOAD) + "\nuser kept typing",
])
def test_non_wrapper_text_is_left_unchanged(text):
    assert MessageSender.strip_cron_wrapper(text) == text
