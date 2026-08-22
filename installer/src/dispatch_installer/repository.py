"""Published-release resolution and bounded Git checkout operations."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from .layout import InstallerError

REPOSITORY_URL = "https://github.com/dillonlille/dispatch.git"
REPOSITORY_API = "https://api.github.com/repos/dillonlille/dispatch/releases?per_page=100"
REPOSITORY_GITHUB_API = "https://api.github.com/repos/dillonlille/dispatch"
DEVELOPMENT_BRANCH = "main"
LEGACY_DEVELOPMENT_BRANCH = "dev"
DEVELOPMENT_REFS = frozenset({DEVELOPMENT_BRANCH, LEGACY_DEVELOPMENT_BRANCH})
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]


def run_command(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    visible = "install-deps" in command
    return subprocess.run(command, cwd=cwd, check=False, capture_output=not visible, text=True)


def _checked(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    run: RunCommand = run_command,
) -> subprocess.CompletedProcess[str]:
    completed = run(command, cwd)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:256]
        raise InstallerError("git_failed", detail or f"command failed: {command[0]}")
    return completed


def validate_ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref or _TAG.fullmatch(ref) is None or ref.startswith("-"):
        raise InstallerError("ref_invalid", "repository ref is invalid")
    return ref


def _published_releases(*, opener=urlopen) -> list[dict[str, object]]:
    request = Request(
        REPOSITORY_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "dispatch-installer"},
    )
    try:
        with opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise InstallerError("release_lookup_failed", "could not resolve published GitHub releases") from exc
    if not isinstance(payload, list):
        raise InstallerError("release_lookup_invalid", "GitHub release response is not a list")
    releases = [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("draft") is False
        and item.get("prerelease") is False
        and isinstance(item.get("tag_name"), str)
        and _TAG.fullmatch(item["tag_name"])
    ]
    if not releases:
        raise InstallerError("release_not_found", "no published stable GitHub release was found")
    releases.sort(
        key=lambda item: str(item.get("published_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return releases


def resolve_latest_release(*, opener=urlopen) -> str:
    return str(_published_releases(opener=opener)[0]["tag_name"])


def resolve_published_release(tag: str, *, opener=urlopen) -> str:
    tag = validate_ref(tag)
    if not any(item["tag_name"] == tag for item in _published_releases(opener=opener)):
        raise InstallerError(
            "release_not_published",
            "the requested tag is not a published stable GitHub release",
        )
    return tag


def _github_object(url: str, *, opener=urlopen) -> dict[str, object]:
    request = Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "dispatch-installer"},
    )
    try:
        with opener(request, timeout=20) as response:
            raw = response.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise InstallerError("repository_authority_invalid", "canonical repository response is too large")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
        raise InstallerError("repository_authority_unavailable", "canonical repository authority is unavailable") from exc
    if not isinstance(payload, dict):
        raise InstallerError("repository_authority_invalid", "canonical repository authority returned invalid data")
    return payload


def canonical_record_has_remote_authority(record: dict[str, object], *, opener=urlopen) -> bool:
    """Bind an installation record to GitHub before destructive removal."""

    channel = record.get("channel")
    ref = record.get("ref")
    commit = record.get("commit")
    if channel not in {"stable", "dev"} or not isinstance(ref, str) or not isinstance(commit, str):
        return False
    if channel == "dev":
        if ref not in DEVELOPMENT_REFS:
            return False
        encoded_commit = quote(commit, safe="")
        encoded_ref = quote(ref, safe="")
        payload = _github_object(
            f"{REPOSITORY_GITHUB_API}/compare/{encoded_commit}...{encoded_ref}",
            opener=opener,
        )
        base = payload.get("base_commit")
        merge_base = payload.get("merge_base_commit")
        return (
            payload.get("status") in {"ahead", "identical"}
            and isinstance(base, dict)
            and base.get("sha") == commit
            and isinstance(merge_base, dict)
            and merge_base.get("sha") == commit
        )
    tag = validate_ref(ref)
    if resolve_published_release(tag, opener=opener) != tag:
        return False
    payload = _github_object(
        f"{REPOSITORY_GITHUB_API}/commits/{quote(tag, safe='')}",
        opener=opener,
    )
    return payload.get("sha") == commit


def clone_repository(
    destination: Path,
    *,
    channel: str,
    ref: str,
    run: RunCommand = run_command,
) -> Path:
    if channel not in {"stable", "dev"}:
        raise InstallerError("channel_invalid", "channel must be stable or dev")
    if channel == "dev" and ref != DEVELOPMENT_BRANCH:
        raise InstallerError("dev_ref_invalid", "the dev channel must track the main branch")
    ref = validate_ref(ref)
    if destination.exists() or destination.is_symlink():
        raise InstallerError("clone_destination_exists", f"clone destination already exists: {destination}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = ["git", "clone"]
    if channel == "stable":
        command.extend(("--no-checkout", "--depth", "1", REPOSITORY_URL, str(destination)))
    else:
        command.extend(("--single-branch", "--branch", ref, REPOSITORY_URL, str(destination)))
    _checked(tuple(command), run=run)
    if channel == "stable":
        _checked(("git", "-C", str(destination), "fetch", "--depth", "1", "origin", "tag", ref), run=run)
        _checked(("git", "-C", str(destination), "checkout", "--detach", f"refs/tags/{ref}"), run=run)
    return destination


def assert_checkout_clean(clone: Path, *, run: RunCommand = run_command) -> None:
    status = _checked(
        (
            "git",
            "-C",
            str(clone),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored=matching",
        ),
        run=run,
    )
    if status.stdout.strip():
        raise InstallerError("clone_dirty", "the Dispatch checkout contains local or ignored files")


def current_commit(clone: Path, *, run: RunCommand = run_command) -> str:
    completed = _checked(("git", "-C", str(clone), "rev-parse", "HEAD"), run=run)
    commit = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
        raise InstallerError("commit_invalid", "clone did not report a valid commit")
    return commit.lower()


def local_channel_drift(clone: Path, record: dict[str, object] | None) -> dict[str, int] | None:
    """Count commits between HEAD and the locally cached channel tip.

    Read-only and fully offline: it consults only ``refs/remotes/origin/<ref>``
    as of the last fetch/update, using the same hardened git argv as
    :func:`local_checkout_matches_record`. Returns ``None`` whenever drift is
    not measurable (missing record/ref/remote tracking ref, or any git
    failure) — doctor treats ``None`` as "no information", never as an error.
    """

    if record is None:
        return None
    ref = str(record.get("ref", ""))
    if not ref:
        return None
    metadata = clone / ".git"
    if clone.is_symlink() or not clone.is_dir() or metadata.is_symlink() or not metadata.is_dir():
        return None
    base = (
        "git",
        "--no-optional-locks",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.ext.allow=never",
        "-C",
        str(clone),
    )

    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                (*base, *arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return subprocess.CompletedProcess((), 1, "", "")

    head = invoke("rev-parse", "--verify", "HEAD^{commit}")
    remote = invoke("rev-parse", "--verify", f"refs/remotes/origin/{ref}^{{commit}}")
    if head.returncode != 0 or remote.returncode != 0:
        return None
    head_commit = head.stdout.strip().lower()
    remote_commit = remote.stdout.strip().lower()
    if re.fullmatch(r"[0-9a-f]{40,64}", head_commit) is None or re.fullmatch(r"[0-9a-f]{40,64}", remote_commit) is None:
        return None
    if head_commit == remote_commit:
        return {"behind": 0, "ahead": 0}
    counts = invoke("rev-list", "--left-right", "--count", f"{head_commit}...{remote_commit}")
    if counts.returncode != 0:
        return None
    columns = counts.stdout.split()
    if len(columns) != 2 or not all(column.isdigit() for column in columns):
        return None
    return {"behind": int(columns[1]), "ahead": int(columns[0])}


def local_checkout_matches_record(clone: Path, record: dict[str, object] | None) -> bool:
    """Validate local checkout identity without contacting or trusting a remote."""
    metadata = clone / ".git"
    if clone.is_symlink() or not clone.is_dir() or metadata.is_symlink() or not metadata.is_dir():
        return False
    try:
        details = metadata.stat(follow_symlinks=False)
    except OSError:
        return False
    if (
        details.st_uid != os.geteuid()
        or not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        return False
    base = (
        "git",
        "--no-optional-locks",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.ext.allow=never",
        "-C",
        str(clone),
    )

    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (*base, *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    try:
        top = invoke("rev-parse", "--show-toplevel")
        head = invoke("rev-parse", "--verify", "HEAD^{commit}")
        dirty = invoke(
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored=matching",
        )
        if (
            top.returncode != 0
            or head.returncode != 0
            or dirty.returncode != 0
            or Path(top.stdout.strip()).resolve(strict=True) != clone.resolve(strict=True)
            or dirty.stdout.strip()
        ):
            return False
        commit = head.stdout.strip().lower()
        if record is not None and commit != record.get("commit"):
            return False
        if record is None:
            return True
        channel = record.get("channel")
        ref = str(record.get("ref", ""))
        remote_url = invoke("config", "--get", "remote.origin.url")
        if remote_url.returncode != 0 or remote_url.stdout.strip() != REPOSITORY_URL:
            return False
        branch = invoke("symbolic-ref", "--quiet", "--short", "HEAD")
        if channel == "dev":
            if ref not in DEVELOPMENT_REFS:
                return False
            authority = invoke(
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{ref}^{{commit}}",
            )
            if branch.returncode != 0 or branch.stdout.strip() != ref:
                return False
            branch_remote = invoke(
                "config",
                "--get",
                f"branch.{ref}.remote",
            )
            branch_merge = invoke(
                "config",
                "--get",
                f"branch.{ref}.merge",
            )
            if (
                branch_remote.returncode != 0
                or branch_remote.stdout.strip() != "origin"
                or branch_merge.returncode != 0
                or branch_merge.stdout.strip() != f"refs/heads/{ref}"
            ):
                return False
        else:
            authority = invoke("rev-parse", "--verify", f"refs/tags/{ref}^{{commit}}")
            if branch.returncode != 1:
                return False
        return authority.returncode == 0 and authority.stdout.strip().lower() == commit
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return False


def _resolved_commit(clone: Path, revision: str, *, run: RunCommand) -> str:
    completed = _checked(("git", "-C", str(clone), "rev-parse", revision), run=run)
    commit = completed.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise InstallerError("commit_invalid", "clone did not report a valid authority commit")
    return commit


def verify_checkout_authority(
    clone: Path,
    *,
    channel: str,
    ref: str,
    run: RunCommand = run_command,
) -> str:
    """Verify a staged checkout against GitHub before it can be promoted."""

    metadata = clone / ".git"
    if clone.is_symlink() or not clone.is_dir() or metadata.is_symlink() or not metadata.is_dir():
        raise InstallerError("clone_missing", "Dispatch clone is missing or unsafe")
    assert_checkout_clean(clone, run=run)
    head = current_commit(clone, run=run)
    if channel == "stable":
        ref = validate_ref(ref)
        _checked(("git", "-C", str(clone), "fetch", "--depth", "1", REPOSITORY_URL, "tag", ref), run=run)
        expected = _resolved_commit(clone, f"refs/tags/{ref}^{{commit}}", run=run)
        branch = run(("git", "-C", str(clone), "symbolic-ref", "--quiet", "--short", "HEAD"), None)
        if branch.returncode == 0 or head != expected:
            raise InstallerError("stable_authority_invalid", "stable checkout is not detached at its published tag")
        if branch.returncode != 1:
            raise InstallerError("stable_authority_invalid", "stable checkout attachment could not be verified")
    elif channel == "dev":
        if ref != DEVELOPMENT_BRANCH:
            raise InstallerError("dev_ref_invalid", "the dev channel must track the main branch")
        shallow = _checked(
            ("git", "-C", str(clone), "rev-parse", "--is-shallow-repository"),
            run=run,
        )
        if shallow.stdout.strip() != "false":
            raise InstallerError("dev_history_incomplete", "the dev checkout must contain complete history")
        _checked(
            ("git", "-C", str(clone), "fetch", REPOSITORY_URL, f"refs/heads/{DEVELOPMENT_BRANCH}"),
            run=run,
        )
        expected = _resolved_commit(clone, "FETCH_HEAD", run=run)
        branch = _checked(
            ("git", "-C", str(clone), "symbolic-ref", "--quiet", "--short", "HEAD"),
            run=run,
        )
        branch_remote = _checked(
            (
                "git",
                "-C",
                str(clone),
                "config",
                "--get",
                f"branch.{DEVELOPMENT_BRANCH}.remote",
            ),
            run=run,
        )
        branch_merge = _checked(
            (
                "git",
                "-C",
                str(clone),
                "config",
                "--get",
                f"branch.{DEVELOPMENT_BRANCH}.merge",
            ),
            run=run,
        )
        if (
            branch.stdout.strip() != DEVELOPMENT_BRANCH
            or branch_remote.stdout.strip() != "origin"
            or branch_merge.stdout.strip() != f"refs/heads/{DEVELOPMENT_BRANCH}"
            or head != expected
        ):
            raise InstallerError(
                "dev_authority_invalid",
                "dev checkout must exactly track origin/main from the canonical GitHub branch",
            )
    else:
        raise InstallerError("channel_invalid", "channel must be stable or dev")
    return head


__all__ = [
    "DEVELOPMENT_BRANCH",
    "DEVELOPMENT_REFS",
    "LEGACY_DEVELOPMENT_BRANCH",
    "REPOSITORY_API",
    "REPOSITORY_URL",
    "assert_checkout_clean",
    "clone_repository",
    "current_commit",
    "local_channel_drift",
    "local_checkout_matches_record",
    "resolve_latest_release",
    "resolve_published_release",
    "run_command",
    "validate_ref",
    "verify_checkout_authority",
]
