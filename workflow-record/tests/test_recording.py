"""Recording model: secret masking (both layers), normalization, assembly, CDP routing, save/list.

Ported from hermes-agent PR #72199's tests/hermes_cli/test_record.py, minus the tests that
read the recorder JS source (the live-browser test covers the in-page behaviour instead).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from workflow_record_plugin.recording import (
    BINDING_NAME,
    RECORDING_VERSION,
    build_recording,
    handle_cdp_message,
    is_secret_placeholder,
    list_recordings,
    mask_value,
    normalize_event,
    recordings_dir,
    save_recording,
    selector_for,
)


class TestMaskValue:
    def test_password_input_type_is_masked(self):
        assert mask_value("hunter2", input_type="password", name="pw") == "{SECRET:pw}"

    def test_masked_value_never_contains_the_raw_value(self):
        assert "hunter2" not in mask_value("hunter2", input_type="password", name="login-password")

    @pytest.mark.parametrize("ac", ["current-password", "new-password", "one-time-code", "cc-number", "cc-csc"])
    def test_secret_autocomplete_is_masked(self, ac):
        assert mask_value("s3cr3t", autocomplete=ac, name="f") == "{SECRET:f}"

    @pytest.mark.parametrize("ac", [
        "username current-password",       # a token list naming the secret among others
        "section-login new-password",
        "one-time-code username",
        "cc-number cc-csc",
        "  current-password  ",             # stray whitespace is still the token
        "Current-Password",                 # the attribute is case-insensitive
    ])
    def test_a_secret_autocomplete_token_in_a_list_is_masked(self, ac):
        # ``autocomplete`` is a space-separated token list: a field that names a secret among
        # other tokens is a secret field too, and its value must never reach the recording.
        assert mask_value("s3cr3t", autocomplete=ac, name="f") == "{SECRET:f}"

    @pytest.mark.parametrize("ac", [
        "username email",                  # no secret token
        "current-password-extra",           # not the token itself
        "shipping address",
    ])
    def test_a_list_without_a_secret_token_is_not_masked(self, ac):
        assert mask_value("alice@example.com", autocomplete=ac, name="user") == "alice@example.com"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX modes only")
    def test_a_saved_recording_is_readable_by_its_owner_only(self, tmp_path):
        # A recording holds what the user typed (a password the masking missed, an API key in a
        # plain field, the prompts), so the file and its directory must not be readable by another
        # local account.
        from workflow_record_plugin.recording import save_recording

        path = save_recording({"version": 1, "steps": []}, "demo", directory=tmp_path / "recordings")

        assert path.stat().st_mode & 0o077 == 0, oct(path.stat().st_mode)
        assert path.parent.stat().st_mode & 0o077 == 0, oct(path.parent.stat().st_mode)

    def test_plain_value_passes_through(self):
        assert mask_value("alice@example.com", input_type="email", name="user") == "alice@example.com"

    def test_existing_placeholder_is_not_double_wrapped(self):
        assert mask_value("{SECRET:pw}", input_type="password", name="pw") == "{SECRET:pw}"

    def test_nameless_field_falls_back_to_type(self):
        assert mask_value("x", input_type="password", name="") == "{SECRET:password}"

    def test_the_recorder_js_shares_the_python_secret_rule(self):
        # The JS decides whether to read a field's value at all, and the Python pass masks again.
        # Both use the one pattern, so a secret cannot slip through one of them.
        from workflow_record_plugin.recording import RECORDER_JS

        assert "SECRET_AUTOCOMPLETE.test(autocompleteOf(el))" in RECORDER_JS
        assert "SECRET_AUTOCOMPLETE.includes(autocompleteOf(el))" not in RECORDER_JS

    def test_is_secret_placeholder(self):
        assert is_secret_placeholder("{SECRET:pw}")
        assert not is_secret_placeholder("hunter2")
        assert not is_secret_placeholder(None)


class TestRecorderSelectorEscaping:
    """Attribute-hook selectors must survive values containing quotes."""

    @staticmethod
    def _helper() -> str:
        import re

        from workflow_record_plugin.recording import RECORDER_JS

        match = re.search(r"^  const cssAttr = .*;$", RECORDER_JS, re.M)
        assert match, "cssAttr helper missing from the recorder JS"
        return match.group(0)

    @pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
    def test_quotes_and_backslashes_are_escaped(self):
        script = (
            self._helper()
            + "\nconsole.log(JSON.stringify(cssAttr('He said \"hi\" \\\\ 100%')));\n"
        )
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert json.loads(out) == 'He said \\"hi\\" \\\\ 100%'

    def test_every_attribute_hook_goes_through_the_helper(self):
        from workflow_record_plugin.recording import RECORDER_JS

        # three uses (data-testid, name, aria-label), all through the helper
        assert RECORDER_JS.count("cssAttr(") == 3
        assert "const cssAttr =" in RECORDER_JS
        assert '="${name}"' not in RECORDER_JS
        assert '="${aria}"' not in RECORDER_JS
        assert '="${testId}"' not in RECORDER_JS


class TestSelectorFallback:
    def test_explicit_selector_wins(self):
        assert selector_for({"selector": "#login > button", "tag": "button"}) == "#login > button"

    def test_tag_and_text(self):
        sel = selector_for({"selector": "", "tag": "button", "text": "Sign in"})
        assert "button" in sel and "Sign in" in sel

    def test_bare_tag(self):
        assert selector_for({"tag": "input"}) == "input"

    def test_never_empty(self):
        assert selector_for({}) == "*"


class TestNormalizeEvent:
    def test_click(self):
        step = normalize_event({"t": 1000, "type": "click", "selector": "#buy", "tag": "button", "text": "Buy now"})
        assert step == {"t": 1000.0, "type": "click", "selector": "#buy", "text": "Buy now"}

    def test_input_masks_a_password_the_page_failed_to_mask(self):
        step = normalize_event({"t": 2000, "type": "input", "selector": "input[name=pw]",
                                "inputType": "password", "name": "pw", "value": "hunter2"})
        assert step["value"] == "{SECRET:pw}"
        assert "hunter2" not in json.dumps(step)

    def test_input_masks_on_reported_autocomplete_hint(self):
        # A text-typed OTP field: only the autocomplete hint marks it secret.
        step = normalize_event({"t": 1, "type": "input", "selector": "#otp", "inputType": "text",
                                "autocomplete": "one-time-code", "name": "otp", "value": "123456"})
        assert step["value"] == "{SECRET:otp}"

    def test_click_on_a_secret_field_drops_its_text(self):
        step = normalize_event({"t": 1, "type": "click", "selector": "#pw", "tag": "input",
                                "inputType": "password", "text": "hunter2"})
        assert "text" not in step and "hunter2" not in json.dumps(step)

    def test_click_on_a_field_without_a_reported_type_drops_its_text(self):
        # An older recorder build that copied el.value into click text and sent no inputType.
        step = normalize_event({"t": 1, "type": "click", "selector": "#pw", "tag": "input", "text": "hunter2"})
        assert "hunter2" not in json.dumps(step)

    def test_click_on_an_input_button_keeps_its_label(self):
        step = normalize_event({"t": 1, "type": "click", "selector": "#go", "tag": "input",
                                "inputType": "submit", "text": "Sign in"})
        assert step["text"] == "Sign in"

    def test_input_keeps_plain_value(self):
        step = normalize_event({"t": 3, "type": "input", "selector": "#user", "inputType": "text", "value": "alice"})
        assert step["value"] == "alice"

    def test_enter(self):
        assert normalize_event({"t": 5, "type": "enter", "selector": "#pw"}) == {"t": 5.0, "type": "enter", "selector": "#pw"}

    def test_navigate(self):
        step = normalize_event({"t": 9, "type": "navigate", "url": "https://x.test/home"})
        assert step == {"t": 9.0, "type": "navigate", "url": "https://x.test/home"}

    def test_navigate_without_url_is_dropped(self):
        assert normalize_event({"t": 9, "type": "navigate"}) is None

    def test_manual(self):
        step = normalize_event({"t": 1, "type": "manual", "text": "clicked the login button"})
        assert step == {"t": 1.0, "type": "manual", "text": "clicked the login button"}

    def test_unknown_type_is_dropped(self):
        assert normalize_event({"t": 1, "type": "scroll"}) is None

    def test_non_dict_is_dropped(self):
        assert normalize_event("nope") is None
        assert normalize_event(None) is None

    def test_bad_timestamp_defaults_to_zero(self):
        assert normalize_event({"t": "junk", "type": "enter", "selector": "#a"})["t"] == 0.0


class TestBuildRecording:
    @staticmethod
    def _events():
        # Out of order, epoch-ms timestamps like the recorder ships.
        return [
            {"t": 1753500002000, "type": "input", "selector": "#user", "inputType": "text", "value": "alice"},
            {"t": 1753500001000, "type": "click", "selector": "#login", "text": "Login"},
            {"t": 1753500003000, "type": "navigate", "url": "https://x.test/home"},
            {"t": 1753500002500, "type": "bogus"},
        ]

    def test_schema_shape(self):
        rec = build_recording("https://x.test/", "2026-07-26T00:00:00+00:00", self._events())
        assert set(rec) == {"version", "started_at", "url", "steps"}
        assert rec["version"] == RECORDING_VERSION
        assert rec["url"] == "https://x.test/"
        assert rec["started_at"] == "2026-07-26T00:00:00+00:00"
        assert all("t" in s and "type" in s for s in rec["steps"])

    def test_steps_ordered_and_rebased_to_seconds(self):
        steps = build_recording("u", "s", self._events())["steps"]
        assert [s["type"] for s in steps] == ["click", "input", "navigate"]
        assert [s["t"] for s in steps] == [0.0, pytest.approx(1.0), pytest.approx(2.0)]

    def test_relative_timestamps_pass_through(self):
        steps = build_recording("", "s", [{"t": 1.0, "type": "manual", "text": "a"},
                                          {"t": 2.0, "type": "manual", "text": "b"}])["steps"]
        assert [s["t"] for s in steps] == [0.0, 1.0]

    def test_unknown_events_are_dropped_not_fatal(self):
        assert len(build_recording("u", "s", self._events())["steps"]) == 3

    def test_empty_events(self):
        assert build_recording("u", "s", [])["steps"] == []

    def test_recording_never_contains_a_raw_password(self):
        events = self._events() + [{"t": 1753500004000, "type": "input", "selector": "#pw",
                                    "inputType": "password", "name": "pw", "value": "raw-secret-value"}]
        rec = build_recording("u", "s", events)
        assert "raw-secret-value" not in json.dumps(rec)
        assert any(s.get("value") == "{SECRET:pw}" for s in rec["steps"])


def _binding(payload) -> dict:
    return {"method": "Runtime.bindingCalled", "params": {"name": BINDING_NAME, "payload": payload}}


class TestHandleCdpMessage:
    def test_binding_payload_is_buffered(self):
        events = []
        payload = {"t": 1, "type": "click", "selector": "#a"}
        handle_cdp_message(_binding(json.dumps(payload)), events)
        assert events == [payload]

    def test_other_bindings_are_ignored(self):
        events = []
        handle_cdp_message({"method": "Runtime.bindingCalled", "params": {"name": "other", "payload": "{}"}}, events)
        assert events == []

    def test_malformed_payload_is_ignored(self):
        events = []
        handle_cdp_message(_binding("{not json"), events)
        assert events == []

    def test_top_frame_navigation_becomes_a_navigate_event(self):
        events = []
        handle_cdp_message({"method": "Page.frameNavigated", "params": {"frame": {"url": "https://x.test/next"}}}, events)
        assert [(e["type"], e["url"]) for e in events] == [("navigate", "https://x.test/next")]

    def test_subframe_navigation_is_ignored(self):
        events = []
        handle_cdp_message({"method": "Page.frameNavigated",
                            "params": {"frame": {"url": "https://ads.test/", "parentId": "F2"}}}, events)
        assert events == []

    @pytest.mark.parametrize("url", ["about:blank", "chrome://newtab", "devtools://x", "chrome-error://chromewebdata/"])
    def test_internal_urls_are_ignored(self, url):
        events = []
        handle_cdp_message({"method": "Page.frameNavigated", "params": {"frame": {"url": url}}}, events)
        assert events == []

    def test_unrelated_methods_are_ignored(self):
        events = []
        handle_cdp_message({"method": "Network.requestWillBeSent", "params": {}}, events)
        assert events == []


class TestSaveAndList:
    def test_default_dir_follows_hermes_home(self, hermes_home):
        assert recordings_dir() == hermes_home / "recordings"

    def test_save_and_list_roundtrip(self, hermes_home):
        rec = build_recording("https://x.test/", "2026-07-26T00:00:00+00:00",
                              [{"t": 1, "type": "click", "selector": "#a", "text": "Go"}])
        path = save_recording(rec, "My Flow!!")
        assert path.parent == hermes_home / "recordings"
        assert path.name.startswith("my-flow-")
        assert json.loads(path.read_text(encoding="utf-8")) == rec
        listed = list_recordings()
        assert [(r["name"], r["steps"], r["url"]) for r in listed] == [(path.name, 1, "https://x.test/")]

    def test_same_second_saves_do_not_overwrite(self, tmp_path):
        rec = build_recording("u", "s", [])
        first = save_recording(rec, "flow", directory=tmp_path)
        second = save_recording(rec, "flow", directory=tmp_path)
        assert first != second and first.exists() and second.exists()

    def test_unreadable_file_is_listed_as_unreadable(self, tmp_path):
        (tmp_path / "broken.json").write_text("{nope", encoding="utf-8")
        assert list_recordings(directory=tmp_path)[0]["error"] == "unreadable"

    def test_list_missing_dir(self, tmp_path):
        assert list_recordings(directory=tmp_path / "nope") == []

    def test_slug_cannot_escape_the_directory(self, tmp_path):
        path = save_recording(build_recording("u", "s", []), "../../etc/passwd", directory=tmp_path)
        assert path.parent == tmp_path
