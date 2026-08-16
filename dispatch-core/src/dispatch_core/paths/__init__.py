"""Small, non-mutating path resolver for per-user Dispatch installations."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re


class PathConfigError(ValueError):
    """A configured path is relative, unsafe, or outside its declared boundary."""


def _contains_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
        if not current.exists():
            return False
    return False


_OWNER = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_ROOT_ENV = {
    "config": "DISPATCH_CONFIG_ROOT",
    "data": "DISPATCH_DATA_ROOT",
    "state": "DISPATCH_STATE_ROOT",
    "cache": "DISPATCH_CACHE_ROOT",
    "runtime": "DISPATCH_RUNTIME_ROOT",
}
_DISPATCH_HOME_ENV = "DISPATCH_HOME"


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
    """Resolve an absolute physical path and require it to remain below root."""
    boundary = _absolute(root, f"{label} root")
    candidate = _absolute(value, label)
    if candidate == boundary or not _is_within(candidate, boundary):
        raise PathConfigError(f"{label} is outside its declared root")
    return candidate


def _source_code_root() -> Path:
    candidate = Path(__file__).resolve().parents[4]
    source = candidate / "dispatch-core" / "src" / "dispatch_core" / "paths"
    if not source.is_dir():
        raise PathConfigError("DISPATCH_CODE_ROOT is required outside the source checkout")
    return candidate


@dataclass(frozen=True)
class DispatchPaths:
    """Resolved source and private per-user roots; construction never creates paths."""

    home: Path
    code: Path
    config: Path
    data: Path
    state: Path
    cache: Path
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
        code_path = _absolute(code_root or env.get("DISPATCH_CODE_ROOT") or _source_code_root(), "DISPATCH_CODE_ROOT")
        if not code_path.is_dir():
            raise PathConfigError("DISPATCH_CODE_ROOT must be an existing directory")

        dispatch_home = _absolute(
            env.get(_DISPATCH_HOME_ENV) or home_path / ".dispatch",
            _DISPATCH_HOME_ENV,
        )

        def dispatch_root(kind: str) -> Path:
            explicit = env.get(_ROOT_ENV[kind])
            if explicit:
                return _absolute(explicit, _ROOT_ENV[kind])
            return _child(dispatch_home, kind)

        config = dispatch_root("config")
        data = dispatch_root("data")
        state = dispatch_root("state")
        cache = dispatch_root("cache")
        if env.get("DISPATCH_RUNTIME_ROOT"):
            runtime = _absolute(env["DISPATCH_RUNTIME_ROOT"], "DISPATCH_RUNTIME_ROOT")
        elif env.get("XDG_RUNTIME_DIR"):
            runtime = _child(_absolute(env["XDG_RUNTIME_DIR"], "XDG_RUNTIME_DIR"), "dispatch")
        else:
            runtime = _child(dispatch_home, "runtime")

        primary = {"config": config, "data": data, "state": state, "cache": cache, "runtime": runtime}
        items = list(primary.items())
        for index, (left_label, left) in enumerate(items):
            for right_label, right in items[index + 1 :]:
                if left == right or _is_within(left, right) or _is_within(right, left):
                    raise PathConfigError(f"{left_label} and {right_label} roots cannot overlap")
        for label, private_root in primary.items():
            if private_root == code_path or _is_within(private_root, code_path):
                raise PathConfigError(f"{label} root cannot be inside the source checkout")

        override = env.get("DISPATCH_BUILD_OUTPUT")
        build_output_override = _absolute(override, "DISPATCH_BUILD_OUTPUT") if override else None
        if build_output_override and (build_output_override == code_path or _is_within(build_output_override, code_path)):
            raise PathConfigError("DISPATCH_BUILD_OUTPUT cannot be inside the source checkout")

        return cls(
            home=home_path,
            code=code_path,
            config=config,
            data=data,
            state=state,
            cache=cache,
            runtime=runtime,
            build_output_override=build_output_override,
        )

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
            "DISPATCH_CODE_ROOT": self.code,
            "DISPATCH_CONFIG_ROOT": self.config,
            "DISPATCH_DATA_ROOT": self.data,
            "DISPATCH_STATE_ROOT": self.state,
            "DISPATCH_CACHE_ROOT": self.cache,
            "DISPATCH_RUNTIME_ROOT": self.runtime,
        }
        return {name: str(path) for name, path in values.items()}

    def owner_environment(self, owner: str) -> dict[str, str]:
        if not _OWNER.fullmatch(owner):
            raise PathConfigError("owner must be a lowercase Dispatch slug")
        prefix = owner.replace("-", "_").upper()
        values = self.as_environment()
        values.update(
            {
                "DISPATCH_OWNER_ID": owner,
                "DISPATCH_OWNER_CONFIG_ROOT": str(self.owner_root("config", owner)),
                "DISPATCH_OWNER_DATA_ROOT": str(self.owner_root("data", owner)),
                "DISPATCH_OWNER_STATE_ROOT": str(self.owner_root("state", owner)),
                "DISPATCH_OWNER_CACHE_ROOT": str(self.owner_root("cache", owner)),
                "DISPATCH_OWNER_RUNTIME_ROOT": str(self.owner_root("runtime", owner)),
                f"DISPATCH_{prefix}_CONFIG_ROOT": str(self.owner_root("config", owner)),
                f"DISPATCH_{prefix}_DATA_ROOT": str(self.owner_root("data", owner)),
                f"DISPATCH_{prefix}_STATE_ROOT": str(self.owner_root("state", owner)),
            }
        )
        return values
