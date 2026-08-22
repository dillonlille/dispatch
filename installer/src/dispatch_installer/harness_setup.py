"""Interactive harness setup flow for Dispatch's setup wizard.

Wires the UI kit and the harness module into a six-step experience:
selection, detection, (optional) installation, profile creation,
model/provider choice, reasoning level, and a verification summary.

Headless runs never prompt: they either complete from declared state or
return structured pending requirements. Credentials are never collected
here — Codex OAuth is delegated to Hermes' own device-code flow.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from .harness import (
    HARNESS_CATALOG,
    DetectionResult,
    HarnessSpec,
    detect_harness,
    install_harness,
    load_selection,
    write_selection,
)
from .layout import InstallLayout, InstallerError
from .ui import select_menu, status_line, step_header, summary_divider

PROFILE_NAME = "dispatch-operations"
_CODEX_MODELS = (
    ("gpt-5.6-luna", "Balanced speed and depth."),
    ("gpt-5.6-sol", "Most thorough reasoning."),
    ("gpt-5.6-terra", "Fastest responses."),
)
_RECOMMENDED_MODEL = "gpt-5.6-luna"
_REASONING_LEVELS = (
    ("high", "Deep reasoning for complex operations."),
    ("xhigh", "Maximum reasoning; slower responses."),
    ("medium", "Balanced thinking time."),
    ("low", "Snappy responses, lighter tasks."),
)
_RECOMMENDED_REASONING = "high"
_HERMES_COMMAND_TIMEOUT = 120.0


@dataclass(slots=True)
class HarnessSetupResult:
    selected: bool = False
    harness_id: str = ""
    version: str = ""
    profile: str = ""
    model: str = ""
    reasoning: str = ""
    pending: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "selected": self.selected,
            "harness": self.harness_id,
            "version": self.version,
            "profile": self.profile,
            "model": self.model,
            "reasoning": self.reasoning,
            "pending_requirements": self.pending,
            "contains_secrets": False,
        }


def _hermes_command(
    launcher: str,
    arguments: tuple[str, ...],
    *,
    timeout: float = _HERMES_COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            (launcher, *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallerError("harness_command_failed", f"harness command failed: {arguments[0]}") from exc


def _profile_exists(launcher: str, name: str) -> bool:
    import sys as _sys

    completed = _sys.modules[__name__]._hermes_command(launcher, ("profile", "list"))
    if completed.returncode != 0:
        return False
    return any(
        line.strip().startswith(name) or f" {name} " in line
        for line in (completed.stdout or "").splitlines()
    )


def _ensure_profile(spec: HarnessSpec, detection: DetectionResult, result: HarnessSetupResult) -> None:
    launcher = spec.launcher
    if _profile_exists(launcher, PROFILE_NAME):
        result.profile = PROFILE_NAME
        return
    completed = _hermes_command(launcher, ("profile", "create", PROFILE_NAME, "--no-skills", "--no-alias"))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:256]
        raise InstallerError("harness_profile_failed", f"could not create the {PROFILE_NAME} profile: {detail}")
    result.profile = PROFILE_NAME


def _write_profile_config(spec: HarnessSpec, detection: DetectionResult, model: str, reasoning: str) -> None:
    """Set model/provider/reasoning through Hermes' own config CLI."""
    launcher = spec.launcher
    pairs = (
        ("model.default", model),
        ("model.provider", "openai-codex"),
        ("agent.reasoning_effort", reasoning),
    )
    for key, value in pairs:
        completed = _hermes_command(launcher, ("config", "set", key, value, "--profile", PROFILE_NAME))
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:256]
            raise InstallerError(
                "harness_config_failed",
                f"could not set {key} on the {PROFILE_NAME} profile: {detail}",
            )


def _codex_pending() -> dict[str, str]:
    return {
        "requirement": "codex_authentication",
        "action": f"run: hermes auth add openai-codex --profile {PROFILE_NAME}",
    }


def _auth_status_pending(spec: HarnessSpec) -> list[dict[str, str]]:
    completed = _hermes_command(spec.launcher, ("auth", "status", "openai-codex", "--profile", PROFILE_NAME))
    output = (completed.stdout or "").lower()
    if "logged out" in output or "not authenticated" in output or completed.returncode != 0:
        return [_codex_pending()]
    return []


def run_harness_setup(
    layout: InstallLayout,
    *,
    human: bool,
    allow_install: bool = False,
    input_fn=input,
) -> HarnessSetupResult:
    """Run the interactive harness flow; headless runs skip prompting."""
    result = HarnessSetupResult()
    existing = load_selection(layout.config)
    print(step_header(1, 6, "Harness"))

    options = [
        (spec.id, spec.description)
        for spec in HARNESS_CATALOG.values()
    ]
    options.append(("none", "Core-only install. Run dispatch setup anytime."))
    index: int | None = None
    if existing is not None:
        print(status_line("ok", f"Harness already selected: {existing}"))
        index = 0 if existing != "none" else len(options) - 1
    elif human:
        index = select_menu(
            "Select harness",
            options,
            recommended=HARNESS_CATALOG["hermes"].id,
            hint="↑↓ move · enter select",
            input_fn=input_fn,
            interactive=True,
        )
        if index is None:
            print(status_line("warn", "No selection made; skipping harness setup"))
            return result
    else:
        print(status_line("warn", "Non-interactive run without a recorded selection; skipping harness setup"))
        return result

    chosen = options[index][0]
    if chosen == "none":
        print(status_line("ok", "Continuing Core-only"))
        return result

    spec = HARNESS_CATALOG[chosen]
    result.selected = True
    result.harness_id = spec.id

    # Step 2 — detect / install
    print(step_header(2, 6, f"Detecting {spec.display_name}"))
    detection = detect_harness(spec)
    if detection.status == "absent":
        print(status_line("warn", f"{spec.display_name} is not installed"))
        should_install = allow_install
        if human and not should_install:
            answer_index = select_menu(
                f"Install {spec.display_name} now?",
                [("yes", "Runs the official installer with verified checksum."), ("no", "Skip harness setup.")],
                recommended="yes",
                input_fn=input_fn,
                interactive=True,
            )
            should_install = answer_index == 0
        if not should_install:
            result.pending.append({
                "requirement": "harness_install",
                "action": f"re-run dispatch setup, or install {spec.display_name} manually",
            })
            return result
        detection = install_harness(spec, allow_install=True)
    if detection.status != "ready":
        raise InstallerError("harness_unhealthy", f"{spec.display_name}: {detection.detail}")
    print(status_line("ok", f"{spec.display_name} {detection.version}", detection.home))
    result.version = detection.version
    write_selection(layout.config, spec, detection)

    # Step 3 — profile
    print(step_header(3, 6, "Profile"))
    _ensure_profile(spec, detection, result)
    print(status_line("ok", f"Profile ready: {result.profile}"))

    # Step 4 — model/provider
    print(step_header(4, 6, "Model"))
    model = _RECOMMENDED_MODEL
    if human:
        model_index = select_menu(
            f"Default model ({spec.launcher} auth)",
            list(_CODEX_MODELS),
            recommended=_RECOMMENDED_MODEL,
            input_fn=input_fn,
            interactive=True,
        )
        if model_index is not None:
            model = _CODEX_MODELS[model_index][0]
    else:
        print(status_line("ok", f"Using default: {model}"))
    result.model = model

    # Step 5 — reasoning
    print(step_header(5, 6, "Reasoning"))
    reasoning = _RECOMMENDED_REASONING
    if human:
        reasoning_index = select_menu(
            "Reasoning level",
            list(_REASONING_LEVELS),
            recommended=_RECOMMENDED_REASONING,
            input_fn=input_fn,
            interactive=True,
        )
        if reasoning_index is not None:
            reasoning = _REASONING_LEVELS[reasoning_index][0]
    else:
        print(status_line("ok", f"Using default: {reasoning}"))
    result.reasoning = reasoning

    _write_profile_config(spec, detection, model, reasoning)
    print(status_line("ok", f"{model} · openai-codex · reasoning {reasoning}"))

    # Step 6 — verification
    print(step_header(6, 6, "Verification"))
    print(summary_divider())
    print(status_line("ok", f"{spec.display_name} {result.version}", "installed"))
    print(status_line("ok", f"Profile {result.profile}", "ready"))
    print(status_line("ok", "Model", f"{result.model} · openai-codex"))
    print(status_line("ok", "Reasoning", result.reasoning))
    result.pending.extend(_auth_status_pending(spec))
    if result.pending:
        for item in result.pending:
            print(status_line("warn", "Codex sign-in pending", item.get("action", "")))
    else:
        print(status_line("ok", "Codex authentication", "active"))
    print(summary_divider())
    return result


__all__ = [
    "HarnessSetupResult",
    "PROFILE_NAME",
    "run_harness_setup",
]
