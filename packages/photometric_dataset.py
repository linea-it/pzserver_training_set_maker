"""Helpers to resolve and open the photometric dataset used by TSM."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable

import lsdb
import pyarrow.parquet as pq

from utils import load_yml


class PhotometricDatasetResolver:
    """Resolve the photometric dataset from science_catalogs or existing HATS inputs."""

    def __init__(
        self,
        *,
        inputs,
        param,
        cwd,
        logger,
        add_info: Callable[[str, object], None],
    ) -> None:
        self.inputs = inputs
        self.param = param
        self.cwd = cwd
        self.logger = logger
        self.add_info = add_info

    def resolve(self, client, selected_cols):
        """Resolve the photometric dataset from science_catalogs YAML or an existing HATS catalog."""
        dataset_base_path = Path(self.get_dataset_base_path())
        attempted_paths = []

        for candidate in self.get_science_catalog_config_candidates():
            attempted_paths.append(str(candidate))
            if candidate.is_file():
                self.logger.info(
                    "Resolved photometric dataset via science_catalogs config: %s",
                    candidate,
                )
                self.add_info("photometric_dataset_source", "science_catalogs")
                self.add_info("photometric_dataset_config", str(candidate))
                return self.build_dataset_from_science_catalogs(
                    config_path=candidate,
                    client=client,
                    selected_cols=selected_cols,
                )

        if dataset_base_path.exists() and self.is_hats_catalog_path(dataset_base_path):
            self.logger.info(
                "Resolved photometric dataset as direct HATS path: %s",
                dataset_base_path,
            )
            self.add_info("photometric_dataset_source", "hats")
            return self.open_hats_dataset(str(dataset_base_path), selected_cols)

        dataset_path = self.get_legacy_hats_path()
        attempted_paths.append(str(dataset_path))
        if os.path.exists(dataset_path):
            self.logger.info(
                "Resolved photometric dataset as legacy HATS path: %s",
                dataset_path,
            )
            self.add_info("photometric_dataset_source", "legacy_hats")
            return self.open_hats_dataset(dataset_path, selected_cols)

        self.logger.error(
            "Could not resolve photometric dataset. Tried: %s",
            attempted_paths,
        )
        raise FileNotFoundError(
            "Could not resolve photometric dataset. Tried: "
            + ", ".join(attempted_paths)
        )

    def get_legacy_hats_path(self):
        """Build the legacy derived HATS dataset path."""
        dataset_path = self.inputs.get("dataset").get("path")

        if self.param.get("use_absolute_lsdb_path", False):
            return str(dataset_path)

        flux_type = self.param.get("flux_type")
        convert_flux_to_mag = self.param.get("convert_flux_to_mag")
        flux_or_mag = "mag" if convert_flux_to_mag else "flux"
        dereddening = self.param.get("dereddening")
        return str(Path(dataset_path, flux_or_mag, flux_type, dereddening, "catalog"))

    def get_dataset_base_path(self):
        """Return the configured dataset base path without derived suffixes."""
        return str(self.inputs.get("dataset").get("path"))

    def get_science_catalog_config_candidates(self):
        """Return possible science_catalogs config paths for the current photometric mode."""
        dataset_base_path = Path(self.get_dataset_base_path())
        suffix = dataset_base_path.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            return [dataset_base_path]

        if self.param.get("use_absolute_lsdb_path", False):
            return []

        flux_type = self.param.get("flux_type")
        convert_flux_to_mag = self.param.get("convert_flux_to_mag")
        flux_or_mag = "mag" if convert_flux_to_mag else "flux"
        dereddening = self.param.get("dereddening")
        stem = f"{dataset_base_path.name}.{flux_or_mag}.{flux_type}.{dereddening}"
        return [
            dataset_base_path.parent / f"{stem}.yaml",
            dataset_base_path.parent / f"{stem}.yml",
        ]

    def build_dataset_from_science_catalogs(self, config_path, client, selected_cols):
        """Run science_catalogs from a YAML config and reopen the resulting HATS catalog lazily."""
        try:
            from science_catalogs import materialize_lsdb_catalog, prepare_catalog
        except ImportError as exc:
            raise RuntimeError(
                "science_catalogs is required when dataset.path resolves to a YAML config."
            ) from exc

        output_dir = self.get_science_catalogs_output_dir(config_path)
        open_kwargs = {}
        if selected_cols:
            open_kwargs["columns"] = selected_cols

        self.logger.info(
            "Building photometric dataset from science_catalogs config: %s -> %s",
            config_path,
            output_dir,
        )
        config = self.load_science_catalogs_config(config_path)
        prepared = prepare_catalog(str(config_path), config=config, client=client)
        result = materialize_lsdb_catalog(
            prepared,
            output_dir=str(output_dir),
            client=client,
            **open_kwargs,
        )
        self.logger.info("science_catalogs generated HATS dataset at: %s", result["path"])
        return result["data"], str(result["path"])

    def get_science_catalogs_output_dir(self, config_path):
        """Return a stable per-config directory for generated science_catalogs HATS outputs."""
        config_path = Path(config_path)
        digest = hashlib.sha1(str(config_path.resolve()).encode("utf-8")).hexdigest()[
            :8
        ]
        return Path(self.cwd, "temp", "science_catalogs", f"{config_path.stem}_{digest}")

    def load_science_catalogs_config(self, config_path):
        """Load a science_catalogs YAML and resolve relative paths against its parent directory."""
        config_path = Path(config_path)
        config = load_yml(str(config_path))
        config_dir = config_path.parent

        input_cfg = config.get("input", {})
        for key in ("catalog_path", "catalog_folder"):
            value = input_cfg.get(key)
            if (
                isinstance(value, str)
                and value
                and not Path(value).expanduser().is_absolute()
            ):
                input_cfg[key] = str((config_dir / value).resolve())

        dust_cfg = config.get("dust", {})
        dust_path = dust_cfg.get("path_to_dustmaps")
        if (
            isinstance(dust_path, str)
            and dust_path
            and not Path(dust_path).expanduser().is_absolute()
        ):
            dust_cfg["path_to_dustmaps"] = str((config_dir / dust_path).resolve())

        return config

    def open_hats_dataset(self, dataset_path, selected_cols):
        """Open an existing HATS dataset with optional column projection."""
        open_catalog_args = {}
        if selected_cols:
            open_catalog_args["columns"] = selected_cols
        else:
            open_catalog_args["columns"] = self.get_catalog_columns(dataset_path)

        return lsdb.open_catalog(dataset_path, **open_catalog_args), str(dataset_path)

    def is_hats_catalog_path(self, input_path):
        """Detect whether a path points to a HATS catalog directory."""
        path = Path(input_path)
        if not path.exists() or not path.is_dir():
            return False

        max_depth = 2
        candidates = [
            item
            for item in path.rglob("*")
            if item.is_file() and self.relative_depth(path, item) <= max_depth
        ]

        properties_candidates = [
            item
            for item in candidates
            if self.looks_like_properties_filename(item.name)
        ]
        if properties_candidates:
            return any(self.is_hats_properties_file(item) for item in properties_candidates)

        text_candidates = [item for item in candidates if self.looks_like_text_file(item)]
        return any(self.is_hats_properties_file(item) for item in text_candidates)

    def relative_depth(self, root_path, file_path):
        """Return the depth of a file relative to a root directory."""
        return len(file_path.relative_to(root_path).parts) - 1

    def looks_like_properties_filename(self, name):
        """Check whether a filename looks like a HATS properties file."""
        return str(name).lower().endswith("properties")

    def looks_like_text_file(self, path):
        """Check whether a file is worth inspecting as HATS metadata."""
        if self.looks_like_properties_filename(path.name):
            return True
        return path.suffix.lower() in {
            "",
            ".txt",
            ".cfg",
            ".conf",
            ".ini",
            ".yaml",
            ".yml",
        }

    def is_hats_properties_file(self, filepath):
        """Check whether a file contains HATS properties metadata."""
        try:
            with filepath.open("rb") as handle:
                text = handle.read(262144).decode("utf-8", errors="ignore")
        except Exception:
            return False
        return self.is_hats_properties_text(text)

    def is_hats_properties_text(self, text):
        """Check whether a text payload looks like HATS properties metadata."""
        hats_keys = {
            "catalog_name",
            "obs_collection",
            "hats_col_ra",
            "hats_col_dec",
            "hats_order",
            "hats_nrows",
            "hats_builder",
            "hats_version",
        }
        has_property_line = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip().lower()
            if not key:
                continue
            has_property_line = True
            if key.startswith("hats_") or key in hats_keys:
                return True
        return False if has_property_line else False

    def get_catalog_columns(self, dataset_path):
        """Get catalog columns excluding legacy HATS partition columns."""
        hats_partition_cols = {"Norder", "Dir", "Npix"}
        parquet_files = sorted(Path(dataset_path).rglob("*.parquet"))
        if not parquet_files:
            return []

        schema = pq.read_schema(parquet_files[0])
        columns = [name for name in schema.names if name not in hats_partition_cols]
        self.logger.debug("Opening catalog with projected columns: %s", columns)
        return columns
