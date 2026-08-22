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
        # Exporter OUTPUT_DATA_DIR == Parser EXPORT_DIR (the unified hand-off).
        return self.data_root / "exports"

    @property
    def parsed_dir(self) -> Path:
        return self.data_root / "parsed"

    @property
    def textures_dir(self) -> Path:
        return self.data_root / "textures"

    # --- Linux mapper runtime paths --------------------------------------
    @property
    def wine_prefix(self) -> Path:
        return self.wrf_root / "prefix"

    @property
    def proton_path(self) -> Path:
        return self.wrf_root / "proton" / "GE-Proton10-34-WRF-TLS"
