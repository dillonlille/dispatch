"""Uninstall modes and category selections."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dispatch_installer.categories import (
    dependency_notes,
    resolve_selection,
)
from dispatch_installer.cli import (
    _confirm_delete_secrets,
    _prompt_uninstall_mode,
    main,
)
from dispatch_installer.layout import InstallerError
from dispatch_installer.uninstall import plan_uninstall


def _strings(value: object) -> list[str]:
    assert isinstance(value, list)
    return [str(item) for item in value]


def make_layout(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700, parents=True)
    from dispatch_installer.layout import InstallLayout

    return InstallLayout.from_environment({"HOME": str(home)})


def seeded_layout(tmp_path: Path):
    """A prepared layout with one file in every private root."""
    layout = make_layout(tmp_path)
    layout.prepare()
    for name in ("config", "secrets", "data", "state", "logs", "cache"):
        (getattr(layout, name) / "keep.txt").write_text(name, encoding="utf-8")
    return layout


# ---------------------------------------------------------------------------
# Selection resolution
# ---------------------------------------------------------------------------


def test_standard_and_complete_presets() -> None:
    assert resolve_selection("standard") == frozenset(
        {"code", "runtime", "services", "launcher", "cache"}
    )
    assert resolve_selection("complete", secrets_confirmed=False) == frozenset(
        {
            "code", "runtime", "services", "launcher", "cache",
            "logs", "state", "data", "config", "secrets",
        }
    )


def test_custom_include_selects_exactly_those_categories() -> None:
    assert resolve_selection("custom", include=("logs", "data")) == frozenset({"logs", "data"})
    assert resolve_selection("custom", exclude=("secrets",)) == frozenset(
        {
            "code", "runtime", "services", "launcher", "cache",
            "logs", "state", "data", "config",
        }
    )


def test_custom_without_selection_is_rejected() -> None:
    with pytest.raises(InstallerError) as error:
        resolve_selection("custom")
    assert error.value.code == "uninstall_selection_required"


def test_custom_include_and_exclude_are_mutually_exclusive() -> None:
    with pytest.raises(InstallerError) as error:
        resolve_selection("custom", include=("logs",), exclude=("cache",))
    assert error.value.code == "uninstall_arguments"


def test_preset_modes_accept_exclusions_but_not_additions() -> None:
    assert resolve_selection("standard", exclude=("cache",)) == frozenset(
        {"code", "runtime", "services", "launcher"}
    )
    with pytest.raises(InstallerError) as error:
        resolve_selection("standard", include=("logs",))
    assert error.value.code == "uninstall_arguments"


def test_unknown_names_are_rejected() -> None:
    with pytest.raises(InstallerError) as category_error:
        resolve_selection("custom", include=("database",))
    assert category_error.value.code == "uninstall_category_unknown"
    with pytest.raises(InstallerError) as mode_error:
        resolve_selection("nuclear")
    assert mode_error.value.code == "uninstall_mode_unknown"


def test_custom_secrets_require_explicit_confirmation() -> None:
    with pytest.raises(InstallerError) as error:
        resolve_selection("custom", include=("secrets",))
    assert error.value.code == "uninstall_secrets_unconfirmed"
    assert resolve_selection("custom", include=("secrets",), secrets_confirmed=True) == frozenset(
        {"secrets"}
    )
    # Presets carry their own documented contracts.
    assert "secrets" in resolve_selection("complete")


def test_dependency_notes_explain_partial_states() -> None:
    notes = dependency_notes(frozenset({"code"}))
    assert any("runtime" in note for note in notes)
    assert any("services" in note for note in notes)
    assert any("cache" in note for note in dependency_notes(frozenset({"cache"})))
    assert any("state" in note for note in dependency_notes(frozenset({"state", "logs"})))
    # Full and standard selections carry no informational notes.
    full = frozenset(
        {"code", "runtime", "services", "launcher", "cache", "logs", "state", "data", "config", "secrets"}
    )
    assert dependency_notes(full) == []
    assert dependency_notes(frozenset({"code", "runtime", "services", "launcher"})) == []


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_plan_custom_subset_lists_only_selected_roots(tmp_path: Path) -> None:
    layout = seeded_layout(tmp_path)
    plan = plan_uninstall(layout, mode="custom", include=["logs", "data"])
    assert plan["status"] == "planned"
    assert plan["mode"] == "custom"
    assert _strings(plan["selection"]) == ["data", "logs"]
    assert _strings(plan["remove"]) == sorted(str(getattr(layout, name)) for name in ("logs", "data"))
    preserved = set(_strings(plan["preserve"]))
    for name in ("config", "secrets", "state"):
        assert str(getattr(layout, name)) in preserved
    assert plan["blockers"] == []
    assert isinstance(plan["notes"], list)


def test_plan_custom_state_targets_internal_root(tmp_path: Path) -> None:
    layout = seeded_layout(tmp_path)
    layout.browser_installation_record.parent.mkdir(parents=True, exist_ok=True)
    layout.browser_installation_record.write_text("{}", encoding="utf-8")
    plan = plan_uninstall(layout, mode="custom", include=["state"])
    # Staging the state root covers the tree; the disposable browser
    # installation record beneath it is validated and reported explicitly.
    assert _strings(plan["remove"]) == sorted(
        [str(layout.state), str(layout.browser_installation_record)]
    )


def test_plan_complete_alias_matches_purge_envelope(tmp_path: Path) -> None:
    layout = seeded_layout(tmp_path)
    alias = plan_uninstall(layout, purge=True)
    explicit = plan_uninstall(layout, mode="complete")
    assert alias["mode"] == "purge"
    assert explicit["mode"] == "complete"
    assert alias["selection"] == explicit["selection"] == sorted(_strings(explicit["selection"]))
    assert alias["remove"] == explicit["remove"]
    # The complete selection removes the entire home including secrets.
    assert str(layout.dispatch_home) in _strings(explicit["remove"])
    assert str(layout.secrets) not in _strings(explicit.get("preserve", []))


def test_plan_standard_matches_historical_keep_data_contract(tmp_path: Path) -> None:
    layout = seeded_layout(tmp_path)
    plan = plan_uninstall(layout, mode="standard")
    removed = set(_strings(plan["remove"]))
    assert str(layout.dispatch_home) not in removed
    for name in ("config", "secrets", "data", "state", "logs"):
        assert str(getattr(layout, name)) not in removed
    assert plan["preserve"]
    historical = plan_uninstall(make_layout(tmp_path / "second-home"))
    assert historical["mode"] == "standard"


def test_plan_standard_accepts_exclusions(tmp_path: Path) -> None:
    layout = seeded_layout(tmp_path)
    plan = plan_uninstall(layout, mode="standard", exclude=["cache"])
    assert str(layout.cache) not in _strings(plan["remove"])
    assert str(layout.cache) in _strings(plan["preserve"])


def test_plan_custom_code_requires_provenance(tmp_path: Path) -> None:
    layout = seeded_layout(tmp_path)
    plan = plan_uninstall(layout, mode="custom", include=["code"])
    assert plan["status"] == "blocked"
    assert any("provenance" in str(item) for item in plan["blockers"])
    untouched = plan_uninstall(layout, mode="custom", include=["logs"])
    assert untouched["status"] != "blocked"


def test_plan_envelope_keeps_stable_keys(tmp_path: Path) -> None:
    layout = seeded_layout(tmp_path)
    plan = plan_uninstall(layout, mode="standard")
    assert set(plan) >= {
        "schema_version",
        "status",
        "mode",
        "remove",
        "preserve",
        "system_dependencies",
        "hermes",
        "blockers",
        "selection",
        "notes",
    }


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------


def test_uninstall_custom_removes_only_selected_durable_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = seeded_layout(tmp_path)
    monkeypatch.setenv("HOME", str(layout.home))
    result = main(
        [
            "--dispatch-home",
            str(layout.dispatch_home),
            "--json",
            "uninstall",
            "--mode",
            "custom",
            "--with",
            "logs",
            "--with",
            "data",
            "--delete-secrets",
            "--yes",
        ]
    )
    assert result == 0
    assert not layout.logs.exists()
    assert not layout.data.exists()
    for name in ("config", "secrets", "state", "cache"):
        assert (getattr(layout, name) / "keep.txt").exists()
    assert not layout.clone.exists()


def test_uninstall_custom_leaves_units_and_launcher_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = seeded_layout(tmp_path)
    monkeypatch.setenv("HOME", str(layout.home))
    unit = layout.service_directory / "dispatch.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\nDescription=sentinel\n", encoding="utf-8")
    launcher = layout.command_path
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\nsentinel\n", encoding="utf-8")
    result = main(
        [
            "--dispatch-home",
            str(layout.dispatch_home),
            "--json",
            "uninstall",
            "--mode",
            "custom",
            "--with",
            "cache",
            "--yes",
        ]
    )
    assert result == 0
    assert unit.read_text(encoding="utf-8").startswith("[Unit]")
    assert launcher.exists()
    assert not layout.cache.exists()
    assert (layout.logs / "keep.txt").exists()


def test_cli_rejects_secrets_without_delete_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    layout = seeded_layout(tmp_path)
    monkeypatch.setenv("HOME", str(layout.home))
    result = main(
        [
            "--dispatch-home",
            str(layout.dispatch_home),
            "--json",
            "uninstall",
            "--mode",
            "custom",
            "--with",
            "secrets",
            "--yes",
        ]
    )
    assert result == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"]["code"] == "uninstall_secrets_unconfirmed"


def test_cli_custom_without_categories_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    layout = seeded_layout(tmp_path)
    monkeypatch.setenv("HOME", str(layout.home))
    result = main(
        [
            "--dispatch-home",
            str(layout.dispatch_home),
            "--json",
            "uninstall",
            "--mode",
            "custom",
            "--yes",
        ]
    )
    assert result == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"]["code"] == "uninstall_selection_required"


def test_cli_with_requires_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    layout = seeded_layout(tmp_path)
    monkeypatch.setenv("HOME", str(layout.home))
    result = main(
        [
            "--dispatch-home",
            str(layout.dispatch_home),
            "--json",
            "uninstall",
            "--with",
            "logs",
            "--plan",
        ]
    )
    assert result == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"]["code"] == "uninstall_arguments"


def test_cli_plan_reports_mode_for_aliases(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    layout = seeded_layout(tmp_path)
    monkeypatch.setenv("HOME", str(layout.home))
    # A seeded-but-record-less installation is blocked by design; the plan
    # still reports which mode was requested.
    assert main(["--dispatch-home", str(layout.dispatch_home), "--json", "uninstall", "--plan"]) == 2
    blocked = json.loads(capsys.readouterr().out.strip())["data"]
    assert blocked["mode"] == "standard"
    assert blocked["status"] == "blocked"
    assert (
        main(["--dispatch-home", str(layout.dispatch_home), "--json", "uninstall", "--purge", "--plan"])
        == 2
    )
    assert json.loads(capsys.readouterr().out.strip())["data"]["mode"] == "complete"
    assert (
        main(
            [
                "--dispatch-home",
                str(layout.dispatch_home),
                "--json",
                "uninstall",
                "--mode",
                "custom",
                "--with",
                "logs",
                "--plan",
            ]
        )
        == 0
    )
    custom = json.loads(capsys.readouterr().out.strip())
    assert custom["ok"] is True
    assert custom["data"]["selection"] == ["logs"]
    assert custom["data"]["status"] == "planned"


def test_prompt_falls_back_to_explicit_flags_off_tty() -> None:
    with pytest.raises(InstallerError) as error:
        _prompt_uninstall_mode(input_fn=lambda _prompt: "1")
    assert error.value.code == "confirmation_required"


def test_typed_secrets_confirmation() -> None:
    assert _confirm_delete_secrets(input_fn=lambda _prompt: "delete secrets") is True
    assert _confirm_delete_secrets(input_fn=lambda _prompt: "no") is False
