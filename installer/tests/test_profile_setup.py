from __future__ import annotations

from contextlib import nullcontext
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from provider_catalog import provider_policy

import dispatch_installer.setup as setup_runtime
import dispatch_installer.launcher as launcher_runtime
import dispatch_installer.service as service_runtime
cli_runtime = importlib.import_module("dispatch_installer.cli")


class FakeAuthentication:
    def __init__(self, *, selected: str | None = None, profiles=None) -> None:
        self.selected = selected
        self._profiles = [] if profiles is None else list(profiles)
        self.enrolled: list[tuple[str, str, dict[str, str], str | None]] = []
        self.bound: list[tuple[str, str, str]] = []

    def profiles(self):
        return list(self._profiles)

    def compatible_profiles(self, provider):
        return []

    def profile_for_plugin(self, plugin_id: str, provider: str) -> str:
        if self.selected is None:
            raise RuntimeError("missing")
        return self.selected

    def providers(self):
        return (provider_policy("amazon-operations"),)

    def provider(self, value):
        return provider_policy(value)

    def enroll_profile(self, profile, provider, values, *, plugin_id=None):
        self.enrolled.append((profile, provider, dict(values), plugin_id))

    def bind_profile(self, profile, plugin_id, provider):
        self.bound.append((profile, plugin_id, provider))

    def retain_plugin_bindings(self, selected_plugins):
        self.selected_plugins = set(selected_plugins)


def patch_required_plugin(monkeypatch, manager: FakeAuthentication) -> None:
    monkeypatch.setattr(
        setup_runtime,
        "load_plugin_config",
        lambda _layout: {
            "plugins": [
                {
                    "id": "companion-bridge",
                    "required_profiles": [{"provider": "amazon-operations"}],
                }
            ]
        },
    )
    monkeypatch.setattr(setup_runtime, "_auth_manager_for_layout", lambda _layout: manager)


def test_noninteractive_setup_returns_pending_without_secret_prompt(monkeypatch, tmp_path: Path) -> None:
    manager = FakeAuthentication()
    patch_required_plugin(monkeypatch, manager)

    configured, pending = setup_runtime._setup_auth_profiles(tmp_path, ["companion-bridge"], human=False)

    assert configured == []
    assert pending == [
        {
            "plugin": "companion-bridge",
            "action": "run dispatch setup interactively to create or select a profile",
        }
    ]
    assert manager.enrolled == []


def test_interactive_setup_chains_profile_creation_after_plugin_selection(monkeypatch, tmp_path: Path) -> None:
    manager = FakeAuthentication()
    patch_required_plugin(monkeypatch, manager)
    answers = iter(["amazon-work"])
    secrets = iter(["synthetic-user", "synthetic-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(secrets))

    configured, pending = setup_runtime._setup_auth_profiles(tmp_path, ["companion-bridge"], human=True)

    assert pending == []
    assert configured == [
        {
            "plugin": "companion-bridge",
            "profile": "amazon-work",
            "type": "amazon",
            "status": "enrolled",
        }
    ]
    assert manager.enrolled == [
        (
            "amazon-work",
            "amazon-operations",
            {"username": "synthetic-user", "password": "synthetic-password"},
            "companion-bridge",
        )
    ]


def test_interactive_setup_reprompts_on_profile_collision_before_secret_prompts(monkeypatch, tmp_path: Path) -> None:
    layout: Any = tmp_path
    manager = FakeAuthentication(
        profiles=[{"profile": "amazon-work", "type": "amazon", "status": "enrolled"}]
    )
    patch_required_plugin(monkeypatch, manager)
    # The taken name is rejected with a re-ask; enrollment proceeds with the
    # second answer instead of failing the whole setup run.
    answers = iter(["amazon-work", "amazon-work-alt"])
    secrets = iter(["synthetic-user", "synthetic-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(secrets))

    configured, pending = setup_runtime._setup_auth_profiles(
        layout,
        ["companion-bridge"],
        human=True,
    )
    assert pending == []
    assert configured[0]["profile"] == "amazon-work-alt"
    assert manager.enrolled == [
        (
            "amazon-work-alt",
            "amazon-operations",
            {"username": "synthetic-user", "password": "synthetic-password"},
            "companion-bridge",
        )
    ]


def test_interactive_setup_reprompts_on_invalid_profile_name(monkeypatch, tmp_path: Path) -> None:
    layout: Any = tmp_path
    manager = FakeAuthentication()
    patch_required_plugin(monkeypatch, manager)
    # Uppercase/underscore/space names violate the Dispatch slug rule; the
    # wizard explains the rule and asks again instead of dying with an
    # opaque invalid_auth_request error.
    answers = iter(["Amazon Work!", "amazon_work", "-nope", "amazon-work"])
    secrets = iter(["synthetic-user", "synthetic-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(secrets))

    configured, pending = setup_runtime._setup_auth_profiles(layout, ["companion-bridge"], human=True)

    assert pending == []
    assert configured[0]["profile"] == "amazon-work"
    assert manager.enrolled[0][0] == "amazon-work"


def test_interactive_setup_preserves_enrollment_failure_details(monkeypatch, tmp_path: Path) -> None:
    layout: Any = tmp_path
    manager = FakeAuthentication()
    patch_required_plugin(monkeypatch, manager)

    class _CodedEnrollmentError(RuntimeError):
        def __init__(self, message: str, code: str) -> None:
            super().__init__(message)
            self.code = code

    def fail_enrollment(profile, provider, values, *, plugin_id=None):
        raise _CodedEnrollmentError(
            "credential account is already enrolled as another profile",
            "profile_exists",
        )

    manager.enroll_profile = fail_enrollment
    answers = iter(["amazon-work"])
    secrets = iter(["synthetic-user", "synthetic-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(secrets))

    with pytest.raises(setup_runtime.InstallerError) as failure:
        setup_runtime._setup_auth_profiles(layout, ["companion-bridge"], human=True)
    assert failure.value.code == "profile_exists"
    assert "already enrolled as another profile" in str(failure.value)


def test_deselection_drops_plugin_bindings_without_prompting(monkeypatch, tmp_path: Path) -> None:
    manager = FakeAuthentication(selected="amazon-work")
    patch_required_plugin(monkeypatch, manager)

    configured, pending = setup_runtime._setup_auth_profiles(tmp_path, [], human=False)  # type: ignore[arg-type]

    assert configured == []
    assert pending == []
    assert manager.selected_plugins == set()


def test_launcher_forwards_json_mode_as_noninteractive_to_core(monkeypatch) -> None:
    observed = {}
    layout = object()
    monkeypatch.setattr(launcher_runtime.InstallLayout, "from_environment", lambda: layout)
    monkeypatch.setattr(launcher_runtime, "_prepare_core_environment", lambda value: observed.setdefault("layout", value))

    class Interface:
        @staticmethod
        def main(arguments, *, prog, interactive):
            observed.update(arguments=arguments, prog=prog, interactive=interactive)
            return 0

    monkeypatch.setattr(launcher_runtime.importlib, "import_module", lambda _name: Interface)

    assert launcher_runtime._run_core(["auth", "add", "profile"], interactive=False) == 0
    assert observed == {
        "layout": layout,
        "arguments": ["auth", "add", "profile"],
        "prog": "dispatch",
        "interactive": False,
    }


def test_launcher_and_setup_json_help_are_structured(capsys) -> None:
    assert launcher_runtime.main(["--json", "--help"]) == 0
    launcher_help = capsys.readouterr()
    assert launcher_help.err == ""
    assert json.loads(launcher_help.out)["action"] == "help"

    assert cli_runtime.main(["--json", "setup", "--help"]) == 0
    setup_help = capsys.readouterr()
    assert setup_help.err == ""
    assert json.loads(setup_help.out)["action"] == "help"


def test_yes_setup_is_noninteractive_and_never_authorizes_secret_prompts(monkeypatch) -> None:
    observed = {}
    layout = object()
    monkeypatch.setattr(cli_runtime.InstallLayout, "from_environment", lambda **_kwargs: layout)
    monkeypatch.setattr(cli_runtime, "installation_lock", lambda value: nullcontext(value))

    def run_setup(value, arguments, *, human):
        observed.update(layout=value, arguments=arguments, human=human)
        return 1

    monkeypatch.setattr(cli_runtime, "run_setup", run_setup)

    assert cli_runtime.main(["setup", "--plugin", "paycom", "--yes"]) == 1
    assert observed["layout"] is layout
    assert observed["human"] is False


def test_interactive_setup_renders_a_profile_only_summary(monkeypatch, capsys) -> None:
    layout: Any = object()
    monkeypatch.setattr(setup_runtime, "available_plugins", lambda _layout: ["companion-bridge"])
    monkeypatch.setattr(
        setup_runtime,
        "configure_plugins",
        lambda _layout, selected, run: {"status": "complete", "selected_plugins": selected},
    )
    monkeypatch.setattr(
        setup_runtime,
        "_setup_auth_profiles",
        lambda _layout, selected, human: (
            [{"plugin": "companion-bridge", "profile": "amazon-main", "type": "amazon", "status": "enrolled"}],
            [],
        ),
    )

    assert setup_runtime.run_setup(  # type: ignore[arg-type]
        layout,
        ["--plugin=companion-bridge", "--yes"],
        human=True,
    ) == 0
    output = capsys.readouterr().out
    assert "Dispatch setup complete" in output
    assert "amazon-main (enrolled, not yet verified)" in output
    assert "amazon-operations" not in output
    assert "site_packages" not in output


def test_service_preflight_binds_one_compatible_legacy_profile() -> None:
    class Manager:
        def __init__(self):
            self.bound = []

        def profile_for_plugin(self, _plugin_id, _provider):
            raise RuntimeError("legacy profile is not bound yet")

        def compatible_profiles(self, provider):
            assert provider == "amazon-operations"
            return [{"profile": "amazon-main", "status": "enrolled"}]

        def bind_profile(self, profile, plugin_id, provider):
            self.bound.append((profile, plugin_id, provider))

    manager = Manager()
    service_runtime._require_selected_auth_profile(
        manager,
        "companion-bridge",
        "amazon-operations",
    )
    assert manager.bound == [("amazon-main", "companion-bridge", "amazon-operations")]


def _patch_two_compatible_profiles(monkeypatch) -> FakeAuthentication:
    """Manager whose reuse picker offers two compatible profiles."""

    class TwoProfileAuthentication(FakeAuthentication):
        def compatible_profiles(self, provider):
            return [
                {"profile": "amazon-main", "status": "enrolled"},
                {"profile": "amazon-alt", "status": "enrolled"},
            ]

    manager = TwoProfileAuthentication()
    patch_required_plugin(monkeypatch, manager)
    return manager


@pytest.mark.parametrize("answer", ["0", "-1", "99", "banana"])
def test_reuse_picker_reasks_on_out_of_range_answers_and_never_binds_wrong_profile(
    monkeypatch,
    tmp_path: Path,
    answer: str,
    capsys,
) -> None:
    # Python's negative indexing used to turn "0" into the LAST compatible
    # profile and "-1" into the second-to-last — a silent wrong-profile
    # binding. Out-of-range answers now re-ask (audit L-1 consistency) and
    # can never bind a profile the user did not select.
    manager = _patch_two_compatible_profiles(monkeypatch)
    answers = iter([answer, "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    configured, pending = setup_runtime._setup_auth_profiles(tmp_path, ["companion-bridge"], human=True)  # type: ignore[arg-type]

    assert pending == []
    assert configured[0]["profile"] == "amazon-alt"
    assert manager.bound == [("amazon-alt", "companion-bridge", "amazon-operations")]
    assert "or 'c' to create" in capsys.readouterr().err


def test_reuse_picker_eof_still_exits_cleanly(monkeypatch, tmp_path: Path) -> None:
    # The re-ask loop must not trap piped/CI runs: EOF propagates and
    # run_setup converts it to the structured input_unavailable error.
    manager = _patch_two_compatible_profiles(monkeypatch)

    def exhausted_input(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", exhausted_input)

    with pytest.raises(EOFError):
        setup_runtime._setup_auth_profiles(tmp_path, ["companion-bridge"], human=True)  # type: ignore[arg-type]
    assert manager.bound == []


def test_reuse_picker_still_accepts_valid_row_numbers(monkeypatch, tmp_path: Path) -> None:
    manager = _patch_two_compatible_profiles(monkeypatch)
    answers = iter(["2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    configured, pending = setup_runtime._setup_auth_profiles(tmp_path, ["companion-bridge"], human=True)  # type: ignore[arg-type]

    assert pending == []
    assert configured == [
        {
            "plugin": "companion-bridge",
            "profile": "amazon-alt",
            "type": "amazon",
            "status": "enrolled",
        }
    ]
    assert manager.bound == [("amazon-alt", "companion-bridge", "amazon-operations")]


def test_wizard_slug_rule_matches_core_enrollment_rule():
    # Audit hygiene: setup.py validates profile names up front with its own
    # _PROFILE_NAME, while Core's enroll_profile enforces its own _SLUG +
    # length rule. If either side drifts, the wizard accepts names Core
    # rejects (or vice versa) and users hit the wall mid-run. Pin them
    # together.
    import authentication as auth_module

    assert setup_runtime._PROFILE_NAME.pattern == auth_module._SLUG.pattern
    # The wizard's acceptance predicate is Core's exact rule:
    # slug pattern AND at most 63 chars.
    def wizard_accepts(name: str) -> bool:
        return len(name) <= 63 and setup_runtime._PROFILE_NAME.fullmatch(name) is not None

    assert not wizard_accepts("a" * 64)
    assert not wizard_accepts("Bad Name")
    assert wizard_accepts("amazon-work")


def _patch_raw_config(monkeypatch, manager, plugins_config) -> None:
    monkeypatch.setattr(setup_runtime, "load_plugin_config", lambda _layout: {"plugins": plugins_config})
    monkeypatch.setattr(setup_runtime, "_auth_manager_for_layout", lambda _layout: manager)


def test_malformed_required_profiles_become_pending_not_silence(monkeypatch, tmp_path: Path) -> None:
    # Audit L-2: a SELECTED plugin whose manifest entry is malformed used to
    # vanish silently — zero prompts, zero pendings — failing only later
    # behind a generic service-enable error. It must surface as a pending.
    manager = FakeAuthentication()
    _patch_raw_config(
        monkeypatch,
        manager,
        [{"id": "companion-bridge", "required_profiles": "amazon-operations"}],
    )

    configured, pending = setup_runtime._setup_auth_profiles(tmp_path, ["companion-bridge"], human=False)  # type: ignore[arg-type]

    assert configured == []
    assert len(pending) == 1
    assert pending[0]["plugin"] == "companion-bridge"
    assert "fix required_profiles" in pending[0]["action"]
    assert manager.enrolled == []


def test_malformed_provider_entry_is_reported_alongside_valid_ones(monkeypatch, tmp_path: Path) -> None:
    # Audit L-2: one garbage entry inside required_profiles used to be
    # skipped without a trace while the valid ones proceeded.
    manager = FakeAuthentication()
    _patch_raw_config(
        monkeypatch,
        manager,
        [
            {
                "id": "companion-bridge",
                "required_profiles": [{"provider": "amazon-operations"}, "garbage-entry"],
            }
        ],
    )

    configured, pending = setup_runtime._setup_auth_profiles(tmp_path, ["companion-bridge"], human=False)  # type: ignore[arg-type]

    # The valid requirement still flows into the headless bind attempt
    # (FakeAuthentication has no binding -> pending), plus one explicit
    # malformed-entry pending.
    actions = [item["action"] for item in pending]
    assert any("fix the provider entry" in action for action in actions)
    assert any("run dispatch setup interactively" in action for action in actions)


class PickyProviderAuthentication(FakeAuthentication):
    """Resolves only amazon-operations; anything else is an unknown realm."""

    def provider(self, value):
        if value != "amazon-operations":
            raise RuntimeError("unknown_auth_realm")
        return provider_policy(value)


def test_unknown_provider_degrades_to_pending_and_valid_plugins_still_enroll(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Audit L-3: one plugin declaring an unknown provider used to kill the
    # ENTIRE auth stage after installs were committed, so perfectly good
    # plugins never reached enrollment. Now the broken one degrades to a
    # pending and the healthy one enrolls normally.
    manager = PickyProviderAuthentication()
    _patch_raw_config(
        monkeypatch,
        manager,
        [
            {"id": "broken-bridge", "required_profiles": [{"provider": "ghost-realm"}]},
            {"id": "companion-bridge", "required_profiles": [{"provider": "amazon-operations"}]},
        ],
    )
    answers = iter(["amazon-work"])
    secrets = iter(["synthetic-user", "synthetic-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(secrets))

    configured, pending = setup_runtime._setup_auth_profiles(
        tmp_path, ["broken-bridge", "companion-bridge"], human=True  # type: ignore[arg-type]
    )

    assert [item["plugin"] for item in configured] == ["companion-bridge"]
    assert manager.enrolled == [
        (
            "amazon-work",
            "amazon-operations",
            {"username": "synthetic-user", "password": "synthetic-password"},
            "companion-bridge",
        )
    ]
    assert len(pending) == 1
    assert pending[0]["plugin"] == "broken-bridge"
    assert "dispatch setup again" in pending[0]["action"]


def test_empty_credential_values_reprompt_instead_of_failing_late(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    # Audit L-7: empty values used to pass the prompt loop and only blow up
    # in enroll_profile AFTER every prompt completed, forcing a full re-run.
    manager = FakeAuthentication()
    patch_required_plugin(monkeypatch, manager)
    answers = iter(["amazon-work"])
    secrets = iter(["", "synthetic-user", "", "synthetic-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(secrets))

    configured, pending = setup_runtime._setup_auth_profiles(tmp_path, ["companion-bridge"], human=True)  # type: ignore[arg-type]

    assert pending == []
    assert configured[0]["profile"] == "amazon-work"
    assert manager.enrolled == [
        (
            "amazon-work",
            "amazon-operations",
            {"username": "synthetic-user", "password": "synthetic-password"},
            "companion-bridge",
        )
    ]
    assert "is required" in capsys.readouterr().err


def test_keyring_downgrade_notice_shown_only_when_unavailable(monkeypatch, tmp_path: Path, capsys) -> None:
    # Audit L-6: enrollment on a host whose OS keyring is unreachable must
    # SAY SO before collecting credentials, instead of silently downgrading
    # to a disk-backed vault key.
    import sys
    import types

    manager = FakeAuthentication()
    patch_required_plugin(monkeypatch, manager)
    answers = iter(["amazon-work"])
    secrets = iter(["synthetic-user", "synthetic-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(secrets))

    unavailable = types.ModuleType("authentication.keyring")
    unavailable.available = lambda: False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "authentication.keyring", unavailable)
    setup_runtime._setup_auth_profiles(tmp_path, ["companion-bridge"], human=True)  # type: ignore[arg-type]
    assert "No OS keyring available" in capsys.readouterr().out

    available = types.ModuleType("authentication.keyring")
    available.available = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "authentication.keyring", available)
    answers = iter(["amazon-work-2"])
    secrets = iter(["synthetic-user", "synthetic-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(secrets))
    setup_runtime._setup_auth_profiles(tmp_path, ["companion-bridge"], human=True)  # type: ignore[arg-type]
    assert "No OS keyring available" not in capsys.readouterr().out
