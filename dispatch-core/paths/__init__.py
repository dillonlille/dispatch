"""Path resolution for the cloned Dispatch application and private user data."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat


class PathConfigError(ValueError):
    """A configured path is relative, unsafe, or outside its boundary."""


_OWNER = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_ROOT_ENV = {
    "config": "DISPATCH_CONFIG_ROOT",
    "secrets": "DISPATCH_SECRETS_ROOT",
    "data": "DISPATCH_DATA_ROOT",
    "state": "DISPATCH_STATE_ROOT",
    "cache": "DISPATCH_CACHE_ROOT",
    "logs": "DISPATCH_LOGS_ROOT",
    "runtime": "DISPATCH_RUNTIME_ROOT",
}


def _contains_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
        if not current.exists():
            return False
    return False


def _absolute(value: str | Path, label: str) -> Path:
    text = str(value).strip()
    if not text:
        raise PathConfigError(f"{label} is empty")
    path = Path(text)
    if not path.is_absolute():
        raise PathConfigError(f"{label} must be absolute")
    if any(part in {".", ".."} for part in path.parts):
        raise PathConfigError(f"{label} contains traversal")
    if _contains_symlink(path):
        raise PathConfigError(f"{label} cannot use a symlink alias")
    return path.resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_private_root(path: Path, label: str) -> None:
    if not path.exists():
        return
    details = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_mode & 0o077
    ):
        raise PathConfigError(f"{label} must be a private user-owned directory")


def _child(root: Path, *parts: str) -> Path:
    relative = Path(*parts)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise PathConfigError("relative path is empty or unsafe")
    unresolved = root / relative
    if _contains_symlink(unresolved):
        raise PathConfigError("relative path cannot use a symlink alias")
    candidate = unresolved.resolve(strict=False)
    if not _is_within(candidate, root):
        raise PathConfigError("relative path escapes its declared root")
    return candidate


def require_within(value: str | Path, root: str | Path, label: str = "path") -> Path:
    boundary = _absolute(root, f"{label} root")
    candidate = _absolute(value, label)
    if candidate == boundary or not _is_within(candidate, boundary):
        raise PathConfigError(f"{label} is outside its declared root")
    return candidate


def _source_code_root() -> Path:
    candidate = Path(__file__).resolve().parents[2]
    if not (candidate / "dispatch-core" / "paths" / "__init__.py").is_file():
        raise PathConfigError("DISPATCH_CODE_ROOT is required outside a Dispatch checkout")
    return candidate


@dataclass(frozen=True)
class DispatchPaths:
    """Resolved checkout and per-user roots; construction does not create paths."""

    home: Path
    dispatch_home: Path
    code: Path
    config: Path
    secrets: Path
    data: Path
    state: Path
    cache: Path
    logs: Path
    runtime: Path
    build_output_override: Path | None = None

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        code_root: str | Path | None = None,
        home: str | Path | None = None,
    ) -> "DispatchPaths":
        env = dict(os.environ if environ is None else environ)
        home_path = _absolute(home or env.get("HOME") or Path.home(), "HOME")
        dispatch_home = _absolute(env.get("DISPATCH_HOME") or home_path / ".dispatch", "DISPATCH_HOME")
        _validate_private_root(dispatch_home, "DISPATCH_HOME")
        code_path = _absolute(code_root or env.get("DISPATCH_CODE_ROOT") or _source_code_root(), "DISPATCH_CODE_ROOT")
        if not code_path.is_dir():
            raise PathConfigError("DISPATCH_CODE_ROOT must be an existing directory")

        def private_root(kind: str) -> Path:
            explicit = env.get(_ROOT_ENV[kind])
            return _absolute(explicit, _ROOT_ENV[kind]) if explicit else _child(
                dispatch_home, "run" if kind == "runtime" else kind
            )

        roots = {kind: private_root(kind) for kind in _ROOT_ENV}
        for label, private in roots.items():
            _validate_private_root(private, _ROOT_ENV[label])
        items = list(roots.items())
        for index, (left_label, left) in enumerate(items):
            for right_label, right in items[index + 1 :]:
                if left == right or _is_within(left, right) or _is_within(right, left):
                    raise PathConfigError(f"{left_label} and {right_label} roots cannot overlap")
        for label, private in roots.items():
            if private == code_path or _is_within(private, code_path):
                raise PathConfigError(f"{label} root cannot be inside the source checkout")

        override = env.get("DISPATCH_BUILD_OUTPUT")
        build_output_override = _absolute(override, "DISPATCH_BUILD_OUTPUT") if override else None
        if build_output_override and (
            build_output_override == code_path or _is_within(build_output_override, code_path)
        ):
            raise PathConfigError("DISPATCH_BUILD_OUTPUT cannot be inside the source checkout")
        return cls(
            home=home_path,
            dispatch_home=dispatch_home,
            code=code_path,
            config=roots["config"],
            secrets=roots["secrets"],
            data=roots["data"],
            state=roots["state"],
            cache=roots["cache"],
            logs=roots["logs"],
            runtime=roots["runtime"],
            build_output_override=build_output_override,
        )

    @property
    def run(self) -> Path:
        return self.runtime

    def owner_root(self, kind: str, owner: str) -> Path:
        if kind not in _ROOT_ENV:
            raise PathConfigError(f"unknown root kind: {kind}")
        if not _OWNER.fullmatch(owner):
            raise PathConfigError("owner must be a lowercase Dispatch slug")
        return _child(getattr(self, kind), owner)

    def build_output(self, owner: str) -> Path:
        if not _OWNER.fullmatch(owner):
            raise PathConfigError("owner must be a lowercase Dispatch slug")
        return self.build_output_override or _child(self.cache, "build", owner)

    def as_environment(self) -> dict[str, str]:
        values = {
            "DISPATCH_HOME": self.dispatch_home,
            "DISPATCH_CODE_ROOT": self.code,
            "DISPATCH_CONFIG_ROOT": self.config,
            "DISPATCH_SECRETS_ROOT": self.secrets,
            "DISPATCH_DATA_ROOT": self.data,
            "DISPATCH_STATE_ROOT": self.state,
            "DISPATCH_CACHE_ROOT": self.cache,
            "DISPATCH_LOGS_ROOT": self.logs,
            "DISPATCH_RUNTIME_ROOT": self.runtime,
        }
        return {name: str(path) for name, path in values.items()}

    def owner_environment(self, owner: str) -> dict[str, str]:
        if not _OWNER.fullmatch(owner):
            raise PathConfigError("owner must be a lowercase Dispatch slug")
        prefix = owner.replace("-", "_").upper()
        values = self.as_environment()
        for kind in _ROOT_ENV:
            env_kind = "RUNTIME" if kind == "runtime" else kind.upper()
            value = str(self.owner_root(kind, owner))
            values[f"DISPATCH_OWNER_{env_kind}_ROOT"] = value
            values[f"DISPATCH_{prefix}_{env_kind}_ROOT"] = value
        values["DISPATCH_OWNER_ID"] = owner
        return values
