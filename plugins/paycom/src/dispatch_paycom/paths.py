"""Owner-scoped Paycom and Meal Break Gaps database paths."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping


class PathConfigError(ValueError):
    """A plugin path is not absolute, bounded, or owner-scoped."""


@dataclass(frozen=True)
class PaycomPaths:
    """Private database roots; Meal Break Gaps keeps independent ownership."""

    root: Path
    meal_root: Path
    roster: Path
    timecards: Path
    meals: Path
    identity: Path

    @classmethod
    def from_dispatch(cls, dispatch_paths: Any) -> "PaycomPaths":
        paycom_root = dispatch_paths.owner_root("data", "paycom")
        meal_root = dispatch_paths.owner_root("data", "meal-break-gaps")
        return cls(
            root=paycom_root,
            meal_root=meal_root,
            roster=paycom_root / "roster.sqlite3",
            timecards=paycom_root / "timecards.sqlite3",
            meals=meal_root / "meal-break-gaps.sqlite3",
            identity=paycom_root / "identity.sqlite3",
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        dispatch_paths: Any | None = None,
    ) -> "PaycomPaths":
        env = dict(os.environ if environ is None else environ)
        if dispatch_paths is None:
            import importlib

            core_paths = importlib.import_module("paths")
            dispatch_type = getattr(core_paths, "DispatchPaths", None)
            if dispatch_type is None:
                raise PathConfigError("DispatchPaths is unavailable")
            dispatch = dispatch_type.from_environment(env)
        else:
            dispatch = dispatch_paths
        return cls.from_dispatch(dispatch)

    def as_dict(self) -> dict[str, Path]:
        return {
            "roster": self.roster,
            "timecards": self.timecards,
            "meals": self.meals,
            "identity": self.identity,
        }


def _absolute_file(value: Any, label: str) -> Path:
    text = str(value).strip()
    path = Path(text)
    if not text or not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PathConfigError(f"{label} must be an absolute path without traversal")
    return Path(os.path.abspath(path))


def coerce_paths(value: PaycomPaths | Mapping[str, Any] | Any | None = None) -> PaycomPaths:
    if value is None:
        return PaycomPaths.from_environment()
    if isinstance(value, PaycomPaths):
        return value
    if not isinstance(value, Mapping) and callable(getattr(value, "owner_root", None)):
        return PaycomPaths.from_dispatch(value)
    if isinstance(value, Mapping):
        required = {"roster", "timecards", "meals", "identity"}
        if set(value) != required:
            raise PathConfigError("Paycom database paths must contain exactly roster, timecards, meals, and identity")
        paths = {name: _absolute_file(value[name], name) for name in required}
        paycom_parents = {paths[name].parent for name in ("roster", "timecards", "identity")}
        if len(paycom_parents) != 1:
            raise PathConfigError("Paycom-owned databases must share one owner root")
        return PaycomPaths(
            root=next(iter(paycom_parents)),
            meal_root=paths["meals"].parent,
            **paths,
        )
    raise PathConfigError("Unsupported Paycom path configuration")


def default_paths(environ: Mapping[str, str] | None = None) -> PaycomPaths:
    return PaycomPaths.from_environment(environ)


def resolve_paths(environ: Mapping[str, str] | None = None) -> PaycomPaths:
    return default_paths(environ)
