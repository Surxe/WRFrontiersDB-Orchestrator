"""Resolve sibling repos and derive every stage path from a single WRF_ROOT.

This is the single source of truth that keeps the Exporter's output and the
Parser's input from drifting apart: both sides come from the same derivation
here, so they can never be pointed at different directories by hand.
"""

from __future__ import annotations

from pathlib import Path


class Repos:
    """Locations derived from WRF_ROOT and REPOS_DIR."""

    def __init__(self, wrf_root: Path, repos_dir: Path) -> None:
        self.wrf_root = Path(wrf_root)
        self.repos_dir = Path(repos_dir)

    # --- sibling repositories --------------------------------------------
    @property
    def exporter_dir(self) -> Path:
        return self.repos_dir / "WRFrontiers-Exporter"

    @property
    def parser_dir(self) -> Path:
        return self.repos_dir / "WRFrontiersDB-Parser"

    @property
    def site_dir(self) -> Path:
        return self.repos_dir / "WRFrontiersDB-Site"

    @property
    def data_dir(self) -> Path:
        return self.repos_dir / "WRFrontiersDB-Data"

    def venv_python(self, repo_dir: Path) -> Path:
        """The repo's own virtualenv interpreter."""
        return repo_dir / ".venv" / "bin" / "python"

    # --- derived data paths (under WRF_ROOT/data) ------------------------
    @property
    def data_root(self) -> Path:
        return self.wrf_root / "data"

    @property
    def steam_download_dir(self) -> Path:
        return self.data_root / "steam-download"

    def mapper_file(self, game_version: str) -> Path:
        # Dated per patch, e.g. .../data/mapper/2026-08-22.usmap
        return self.data_root / "mapper" / f"{game_version}.usmap"

    @property
    def exports_dir(self) -> Path:
        # DEPRECATED: use exports_dir_for_version(game_version) instead.
        # Kept for backwards compat, but versioned dirs are the new pattern (t-0122).
        return self.data_root / "exports"

    def exports_dir_for_version(self, game_version: str) -> Path:
        # Dated per patch, e.g. .../data/exports/2026-08-22
        return self.data_root / "exports" / game_version

    @property
    def parsed_dir(self) -> Path:
        # DEPRECATED: use parsed_dir_for_version(game_version) instead.
        return self.data_root / "parsed"

    def parsed_dir_for_version(self, game_version: str) -> Path:
        # Dated per patch, e.g. .../data/parsed/2026-08-22
        return self.data_root / "parsed" / game_version

    @property
    def textures_dir(self) -> Path:
        # DEPRECATED: use textures_dir_for_version(game_version) instead.
        return self.data_root / "textures"

    def textures_dir_for_version(self, game_version: str) -> Path:
        # Dated per patch, e.g. .../data/textures/2026-08-22
        return self.data_root / "textures" / game_version

    def prune_old_versions(self, keep: int = 2) -> None:
        """Delete old version directories, keeping only the newest `keep` patches.

        Applied to exports, parsed, textures, and mapper after a successful patch
        run. Sorts by directory name (ISO date string); deletes oldest, keeping
        newest. A no-op if fewer than keep+1 versions exist.
        """
        import shutil
        for subdir in ["exports", "parsed", "textures", "mapper"]:
            parent = self.data_root / subdir
            if not parent.is_dir():
                continue
            # List version dirs (ISO date strings like 2026-08-22)
            versions = sorted([d.name for d in parent.iterdir() if d.is_dir()])
            to_delete = versions[:-keep]  # All but the last `keep`
            for v in to_delete:
                shutil.rmtree(parent / v, ignore_errors=False)

    # --- Linux mapper runtime paths --------------------------------------
    @property
    def wine_prefix(self) -> Path:
        return self.wrf_root / "prefix"

    @property
    def proton_path(self) -> Path:
        return self.wrf_root / "proton" / "GE-Proton10-34-WRF-TLS"
