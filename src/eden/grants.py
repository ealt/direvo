"""Helpers for transient cross-scope file grants."""

from __future__ import annotations

from pathlib import Path

from .models import FilePermissionGrant


def create_grant_symlinks(
    grants: tuple[FilePermissionGrant, ...],
    *,
    actor: str,
    source_root: Path,
    target_root: Path,
    skip_existing: bool = False,
) -> list[Path]:
    """Create symlinks for grants that target a specific actor."""
    created: list[Path] = []
    for grant in grants:
        if grant.actor != actor:
            continue
        source = source_root / grant.path
        target = target_root / grant.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            if target.resolve() == source.resolve():
                continue
            if skip_existing:
                continue
            raise RuntimeError(f"Grant target already exists: {target}")
        if target.exists():
            if skip_existing:
                continue
            raise RuntimeError(f"Grant target already exists: {target}")
        target.symlink_to(source)
        created.append(target)
    return created


def remove_grant_symlinks(paths: list[Path], *, target_root: Path) -> None:
    """Remove created grant symlinks and prune empty parent directories."""
    for path in reversed(paths):
        if path.is_symlink():
            path.unlink()
        current = path.parent
        while current != target_root and current.exists():
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
