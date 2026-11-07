"""Preserve upstream's bound-state predicate while adding recent completions."""
import time

import pytest

from inbox_plugin import collectors
from inbox_seed import add_delegations


@pytest.mark.parametrize('include_completed', [False, True])
def test_live_state_values_remain_bound_with_optional_completion_window(hermes_home, monkeypatch, include_completed):
    now = time.time()
    add_delegations(hermes_home, [
        ('active', 'running', now, None, 'pending', 0, '{}'),
        ('closed', 'completed', now - 20, now, 'delivered', 1, '{}'),
    ])
    # A literal quote in a state value must never be interpolated into SQL.
    monkeypatch.setattr(collectors, 'LIVE_DELEGATION_STATES', ('running', "state with 'quotes'"))
    items, note = collectors.collect_delegations(hermes_home, include_completed=include_completed)
    assert note is None
    expected = {'active', 'closed'} if include_completed else {'active'}
    assert {item['id'] for item in items} == expected
