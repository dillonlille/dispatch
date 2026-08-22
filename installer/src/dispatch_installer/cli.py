"""Public installer CLI for clone-based Dispatch installations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import NoReturn, cast

from .doctor import inspect_installation
from .layout import InstallLayout, InstallerError, installation_lock, read_installation
from .lifecycle import install_or_update, recover_incomplete_installation, repair_existing
from .setup import run_setup, selected_long_running_plugins
from .service import disable_plugin_service, enable_plugin_service, status_plugin_service
from .uninstall import plan_uninstall, uninstall


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise InstallerError("arguments_invalid", message)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="dispatch-installer")
    parser.add_argument("--dispatch-home", help="per-user Dispatch root; defaults to $HOME/.dispatch")
    parser.add_argument("--json", action="store_true", help="emit a JSON result envelope")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("layout", help="print the resolved installation layout")
    install = actions.add_parser("install", help="install a staged Dispatch clone")
    install.add_argument("--clone", type=Path, help="staged clone; omit to clone from GitHub")
    install.add_argument("--channel", choices=("stable", "dev"), default="stable")
    install.add_argument("--version", help="stable release tag")
    install.add_argument("--yes", action="store_true", help="confirm installation")
    update = actions.add_parser("update", help="update the installed clone")
    update.add_argument("--clone", type=Path, help="already cloned target; omit to clone from GitHub")
    update.add_argument("--channel", choices=("stable", "dev"))
    update.add_argument("--version", help="stable release tag")
    repair_parser = actions.add_parser("repair", help="repair the venv, launcher, and service")
    repair_parser.add_argument("--yes", action="store_true", help="confirm repair")
    channel = actions.add_parser("channel", aliases=("switch-channel",), help="switch stable/dev channel")
    channel.add_argument("channel", choices=("stable", "dev"))
    channel.add_argument("--clone", type=Path, help="already cloned target; omit to clone from GitHub")
    channel.add_argument("--version", help="stable release tag")
    actions.add_parser("doctor", help="inspect installation without changing it")
    actions.add_parser("verify", help="verify a complete installation")
    actions.add_parser(
        "recover",
        help="remove unrecorded managed paths left by a crashed first install",
    )
    setup = actions.add_parser("setup", help="install selected built-in plugins")
    setup.add_argument("--plugin", action="append", default=[])
    setup.add_argument("--list", action="store_true")
    setup.add_argument("--yes", action="store_true")
    harness_setup = actions.add_parser(
        "harness-setup",
        help="select, install, and configure an agent harness profile",
    )
    harness_setup.add_argument("--yes", action="store_true", help="run non-interactively with defaults")
    harness_setup.add_argument(
        "--install-harness",
        action="store_true",
        help="authorize installing the harness if it is absent (supply-chain action)",
    )
    plugin_service = actions.add_parser("plugin-service", help="operate an exactly generated plugin service")
    plugin_service.add_argument("operation", choices=("status", "enable", "disable"))
    plugin_service.add_argument("plugin_id")
    remove = actions.add_parser("uninstall", help="remove Dispatch files by mode or category")
    remove.add_argument("--plan", action="store_true", help="print the removal plan without mutating")
    remove.add_argument("--mode", choices=("standard", "complete", "custom"), help="standard keeps durable data; complete removes everything; custom selects categories")
    remove.add_argument("--with", dest="with_category", action="append", metavar="CATEGORY", help="custom mode: remove exactly this category (repeatable)")
    remove.add_argument("--without", action="append", metavar="CATEGORY", help="preset modes: keep this category (repeatable)")
    remove.add_argument("--delete-secrets", action="store_true", help="confirm removing the secrets category in custom mode")
    remove.add_argument("--purge", action="store_true", help="historical alias for --mode complete")
    remove.add_argument("--yes", action="store_true", help="confirm uninstall without prompting")
    return parser


def _emit(ok: bool, action: str, status: str, data: object, error: dict[str, str] | None = None) -> None:
    print(json.dumps({"ok": ok, "action": action, "status": status, "data": data, "error": error}, sort_keys=True))


def _prompt_uninstall_mode(input_fn=input) -> tuple[str, list[str], list[str]]:
    """Interactive mode chooser: preset pick or category multi-select."""
    from .categories import CATEGORY_NAMES, DURABLE_CATEGORIES, dependency_notes
    from .interactive import multi_select_menu
    from .ui import select_menu, status_line

    choice = select_menu(
        "Choose an uninstall mode",
        [
            ("standard", "remove code and runtime, keep configuration, data, logs"),
            ("complete", "remove everything Dispatch created, including data"),
            ("custom", "choose exactly which categories to remove"),
        ],
        recommended="standard",
        hint="Enter to continue",
        input_fn=input_fn,
    )
    if choice is None:
        raise InstallerError(
            "confirmation_required",
            "interactive selection requires a terminal; pass --mode with --yes instead",
        )
    if choice == 0:
        return ("standard", [], [])
    if choice == 1:
        return ("complete", [], [])
    options = [
        (
            name,
            f"{name}{' (durable)' if name in DURABLE_CATEGORIES else ''}",
        )
        for name in sorted(CATEGORY_NAMES)
    ]
    indices = multi_select_menu(
        "Select categories to remove",
        options,
        preselected=[],
        hint="Space toggles, Enter confirms",
    )
    if indices is None:
        raise InstallerError(
            "confirmation_required",
            "category selection requires a terminal; pass --with/--without with --yes instead",
        )
    include = [options[index][0] for index in indices]
    if not include:
        raise InstallerError("uninstall_selection_required", "no categories were selected")
    for note in dependency_notes(frozenset(include)):
        print(status_line("warn", note))
    return ("custom", include, [])


def _confirm_delete_secrets(input_fn=input) -> bool:
    """Typed confirmation for removing the secrets category."""
    try:
        answer = input_fn("  Type 'delete secrets' to confirm: ").strip()
    except EOFError:
        return False
    return answer == "delete secrets"


def _confirm_interactive_uninstall(
    layout: InstallLayout,
    mode: str,
    include: list[str],
    exclude: list[str],
    *,
    input_fn=input,
) -> None:
    """Show the exact plan and require a typed confirmation before mutating."""
    from .ui import dim, status_line

    result = plan_uninstall(layout, mode=mode, include=include, exclude=exclude)
    removals = sorted(str(path) for path in cast(list[object], result.get("remove", [])))
    preserved = sorted(str(path) for path in cast(list[object], result.get("preserve", [])))
    notes = sorted(str(note) for note in cast(list[object], result.get("notes", [])))
    print(f"\n  About to remove ({result['mode']}):")
    for path in removals:
        print(f"    - {path}")
    if preserved:
        print(f"  {dim('Keeping:')}")
        for path in preserved:
            print(f"    {dim(f'+ {path}')}")
    for note in notes:
        print(status_line("warn", note))
    for blocker in result["blockers"]:
        print(status_line("fail", str(blocker)))
    try:
        answer = input_fn("  Type 'uninstall' to continue: ").strip()
    except EOFError:
        answer = ""
    if answer != "uninstall":
        raise InstallerError("confirmation_required", "uninstall was not confirmed")


def _lifecycle(layout: InstallLayout, args: argparse.Namespace) -> dict[str, object]:
    if args.action == "install":
        if not args.yes:
            raise InstallerError("confirmation_required", "install requires --yes")
        return install_or_update(layout, channel=args.channel, version=args.version, source=args.clone)
    if args.action == "update":
        record = read_installation(layout)
        if record is None:
            raise InstallerError("installation_missing", "Dispatch is not installed")
        channel = args.channel or str(record["channel"])
        return install_or_update(
            layout,
            channel=channel,
            version=args.version,
            source=args.clone,
            update_current=True,
        )
    if args.action in {"channel", "switch-channel"}:
        if read_installation(layout) is None:
            raise InstallerError("installation_missing", "Dispatch is not installed")
        return install_or_update(layout, channel=args.channel, version=args.version, source=args.clone)
    if args.action == "repair":
        if not args.yes:
            raise InstallerError("confirmation_required", "repair requires --yes")
        return repair_existing(layout)
    raise InstallerError("action_unknown", "unsupported lifecycle action")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = None if argv is None else list(argv)
    if arguments is not None and "--json" in arguments and any(
        value in {"-h", "--help"} for value in arguments
    ):
        _emit(
            True,
            "help",
            "ready",
            {"usage": parser.format_usage().strip(), "contains_secrets": False},
        )
        return 0
    args = None
    try:
        args = parser.parse_args(arguments)
        layout = InstallLayout.from_environment(dispatch_home=args.dispatch_home)
        if args.action == "layout":
            _emit(True, "layout", "ready", {"layout": layout.as_dict()})
            return 0
        if args.action in {"install", "update", "repair", "channel", "switch-channel"}:
            result = _lifecycle(layout, args)
            cleanup_error = result.get("cleanup_error_code")
            if cleanup_error is not None:
                _emit(
                    False,
                    args.action,
                    str(result.get("status", "error")),
                    result,
                    {
                        "code": str(cleanup_error),
                        "message": "activation committed but obsolete generation cleanup is incomplete",
                    },
                )
                return 1
            _emit(True, args.action, str(result.get("status", "ready")), result)
            return 0
        if args.action in {"doctor", "verify"}:
            result = inspect_installation(layout)
            _emit(bool(result["ok"]), args.action, str(result["status"]), result)
            return 0 if result["ok"] else 1
        if args.action == "recover":
            with installation_lock(layout):
                result = recover_incomplete_installation(layout)
            _emit(True, "recover", str(result.get("status", "recovered")), result)
            return 0
        if args.action == "setup":
            with installation_lock(layout):
                return run_setup(
                    layout,
                    [*(f"--plugin={plugin}" for plugin in args.plugin), *( ["--list"] if args.list else []), *( ["--yes"] if args.yes or args.json else [])],
                    human=not args.json and not args.yes,
                )
        if args.action == "harness-setup":
            from .harness_setup import run_harness_setup

            with installation_lock(layout):
                result = run_harness_setup(
                    layout,
                    human=not args.json and not args.yes,
                    allow_install=args.install_harness,
                )
            pending = result.pending
            _emit(
                not pending,
                "harness-setup",
                "pending_requirements" if pending else str(result.as_dict().get("profile") and "ready" or "skipped"),
                result.as_dict(),
                {"code": "harness_pending", "message": "harness setup has pending requirements"} if pending else None,
            )
            return 0 if not pending else 1
        if args.action == "plugin-service":
            if read_installation(layout) is None:
                raise InstallerError("installation_missing", "Dispatch is not installed")
            if args.plugin_id not in selected_long_running_plugins(layout):
                raise InstallerError(
                    "plugin_service_not_selected",
                    "plugin service must be selected and declared long-running",
                )
            if args.operation == "status":
                result = status_plugin_service(layout, args.plugin_id)
            else:
                with installation_lock(layout):
                    result = (
                        enable_plugin_service(layout, args.plugin_id)
                        if args.operation == "enable"
                        else disable_plugin_service(layout, args.plugin_id)
                    )
            ready = result.get("status") not in {"unsafe", "incomplete", "missing"}
            _emit(ready, "plugin-service", str(result.get("status", "error")), result)
            return 0 if ready else 1
        if args.action == "uninstall":
            if args.plan and args.yes:
                raise InstallerError("uninstall_arguments", "--plan and --yes cannot be combined")
            include: list[str] = list(args.with_category or [])
            exclude: list[str] = list(args.without or [])
            mode = args.mode
            if mode is None:
                if include or exclude or args.delete_secrets:
                    raise InstallerError(
                        "uninstall_arguments",
                        "--with/--without/--delete-secrets require --mode",
                    )
                if args.purge:
                    mode = "complete"
            kwargs = {
                "mode": mode,
                "include": include,
                "exclude": exclude,
                "secrets_confirmed": bool(args.delete_secrets),
            }
            if args.plan:
                result = plan_uninstall(layout, **kwargs)
                _emit(not result["blockers"], "uninstall", str(result["status"]), result)
                return 0 if not result["blockers"] else 2
            if not args.yes:
                if mode is None:
                    mode, include, exclude = _prompt_uninstall_mode()
                    kwargs = {
                        "mode": mode,
                        "include": include,
                        "exclude": exclude,
                        "secrets_confirmed": bool(args.delete_secrets),
                    }
                    if mode == "custom" and "secrets" in include and not (
                        args.delete_secrets
                        or _confirm_delete_secrets(input)
                    ):
                        include = [category for category in include if category != "secrets"]
                        kwargs["include"] = include
                        kwargs["secrets_confirmed"] = False
                _confirm_interactive_uninstall(layout, mode, include, exclude, input_fn=input)
            elif mode == "custom" and "secrets" in include and not args.delete_secrets:
                raise InstallerError(
                    "uninstall_secrets_unconfirmed",
                    "removing secrets requires explicit confirmation (--delete-secrets)",
                )
            if not include and mode == "custom":
                raise InstallerError("uninstall_selection_required", "no categories were selected")
            result = uninstall(layout, purge=args.purge and mode is None, **kwargs)
            _emit(True, "uninstall", str(result["status"]), result)
            return 0
        raise InstallerError("action_unknown", "unknown installer action")
    except EOFError:
        action = args.action if args is not None else "unknown"
        _emit(False, str(action), "error", {}, {"code": "input_unavailable", "message": "interactive input is unavailable"})
        return 1
    except InstallerError as exc:
        action = args.action if args is not None else "unknown"
        _emit(False, str(action), "error", {}, {"code": exc.code, "message": str(exc)[:512]})
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        action = args.action if args is not None else "unknown"
        _emit(False, str(action), "error", {}, {"code": "installer_failed", "message": str(exc)[:512]})
        return 1


__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
