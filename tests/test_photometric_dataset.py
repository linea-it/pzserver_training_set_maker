import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml


PACKAGES_DIR = Path(__file__).resolve().parents[1] / "packages"
sys.path.insert(0, str(PACKAGES_DIR))

sys.modules.setdefault("lsdb", types.ModuleType("lsdb"))
if "pyarrow" not in sys.modules:
    pyarrow = types.ModuleType("pyarrow")
    pyarrow.__path__ = []
    parquet = types.ModuleType("pyarrow.parquet")
    pyarrow.parquet = parquet
    sys.modules["pyarrow"] = pyarrow
    sys.modules["pyarrow.parquet"] = parquet

from photometric_dataset import PhotometricDatasetResolver


class PhotometricDatasetResolverTestCase(unittest.TestCase):
    def create_resolver(self, *, dataset_path, cwd, param):
        self.add_info = mock.Mock()
        return PhotometricDatasetResolver(
            inputs={"dataset": {"path": str(dataset_path)}},
            param=param,
            cwd=str(cwd),
            logger=mock.Mock(),
            add_info=self.add_info,
        )

    def test_submitted_hats_config_has_priority_and_is_materialized_at_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            release_path = temp_path / "dp1"
            process_path = temp_path / "process"
            release_path.mkdir()
            submitted_config = {
                "input": {
                    "catalog_folder": "catalogs/object",
                    "compute_magnitude": False,
                },
                "dust": {
                    "path_to_dustmaps": "../dustmaps",
                    "use_dustmap": "planck",
                },
                "collection": {"margin_threshold": 10.0},
            }
            resolver = self.create_resolver(
                dataset_path=release_path,
                cwd=process_path,
                param={"hats_config": submitted_config},
            )
            resolver.build_dataset_from_science_catalogs = mock.Mock(
                return_value=("catalog", "generated/path")
            )

            result = resolver.resolve(client="client", selected_cols=["objectId"])

            self.assertEqual(("catalog", "generated/path"), result)
            runtime_path = process_path / "science_catalogs_hats_config.yaml"
            resolver.build_dataset_from_science_catalogs.assert_called_once_with(
                config_path=runtime_path,
                client="client",
                selected_cols=["objectId"],
            )
            runtime_config = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
            self.assertEqual(
                str((release_path / "catalogs/object").resolve()),
                runtime_config["input"]["catalog_folder"],
            )
            self.assertEqual(
                str((release_path / "../dustmaps").resolve()),
                runtime_config["dust"]["path_to_dustmaps"],
            )
            self.assertEqual(
                "catalogs/object",
                submitted_config["input"]["catalog_folder"],
            )
            self.add_info.assert_any_call(
                "photometric_dataset_config_source",
                "param.hats_config",
            )

    def test_runtime_config_is_passed_to_science_catalogs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            release_path = temp_path / "dp1"
            release_path.mkdir()
            generated_path = temp_path / "generated"
            submitted_config = {
                "input": {"catalog_folder": "/datasets/dp1/catalog"},
                "dust": {"path_to_dustmaps": "/datasets/dustmaps"},
                "collection": {"margin_threshold": 10.0},
            }
            resolver = self.create_resolver(
                dataset_path=release_path,
                cwd=temp_path / "process",
                param={"hats_config": submitted_config},
            )
            science_catalogs = types.ModuleType("science_catalogs")
            science_catalogs.prepare_catalog = mock.Mock(
                return_value="prepared-catalog"
            )
            science_catalogs.materialize_lsdb_catalog = mock.Mock(
                return_value={"path": str(generated_path)}
            )

            with mock.patch.dict(
                sys.modules,
                {"science_catalogs": science_catalogs},
            ), mock.patch.object(
                sys.modules["lsdb"],
                "open_catalog",
                create=True,
                return_value="opened-catalog",
            ) as open_catalog:
                result = resolver.resolve(
                    client="client",
                    selected_cols=["objectId"],
                )

            runtime_path = temp_path / "process/science_catalogs_hats_config.yaml"
            science_catalogs.prepare_catalog.assert_called_once_with(
                str(runtime_path),
                config=submitted_config,
                client="client",
            )
            science_catalogs.materialize_lsdb_catalog.assert_called_once_with(
                "prepared-catalog",
                output_dir=str(
                    resolver.get_science_catalogs_output_dir(runtime_path)
                ),
                client="client",
                columns=["objectId"],
            )
            open_catalog.assert_called_once_with(
                str(generated_path),
                columns=["objectId"],
            )
            self.assertEqual(
                ("opened-catalog", str(generated_path)),
                result,
            )

    def test_empty_selected_cols_loads_all_materialized_catalog_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            release_path = temp_path / "dp1"
            release_path.mkdir()
            generated_path = temp_path / "generated"
            submitted_config = {
                "input": {"catalog_folder": "/datasets/dp1/catalog"},
            }
            resolver = self.create_resolver(
                dataset_path=release_path,
                cwd=temp_path / "process",
                param={"hats_config": submitted_config},
            )
            science_catalogs = types.ModuleType("science_catalogs")
            science_catalogs.prepare_catalog = mock.Mock(
                return_value="prepared-catalog"
            )
            science_catalogs.materialize_lsdb_catalog = mock.Mock(
                return_value={"path": str(generated_path)}
            )

            with mock.patch.dict(
                sys.modules,
                {"science_catalogs": science_catalogs},
            ), mock.patch.object(
                sys.modules["lsdb"],
                "open_catalog",
                create=True,
                return_value="opened-catalog",
            ) as open_catalog:
                resolver.resolve(client="client", selected_cols=[])

            runtime_path = temp_path / "process/science_catalogs_hats_config.yaml"
            science_catalogs.materialize_lsdb_catalog.assert_called_once_with(
                "prepared-catalog",
                output_dir=str(
                    resolver.get_science_catalogs_output_dir(runtime_path)
                ),
                client="client",
                columns="all",
            )
            open_catalog.assert_called_once_with(
                str(generated_path),
                columns="all",
            )

    def test_empty_hats_config_opens_direct_hats_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            hats_path = temp_path / "dp1_hats"
            hats_path.mkdir()
            (hats_path / "collection.properties").write_text(
                "hats_order=3\n",
                encoding="utf-8",
            )
            resolver = self.create_resolver(
                dataset_path=hats_path,
                cwd=temp_path / "process",
                param={"hats_config": {}},
            )

            with mock.patch.object(
                sys.modules["lsdb"],
                "open_catalog",
                create=True,
                return_value="opened-catalog",
            ) as open_catalog:
                result = resolver.resolve(client="client", selected_cols=["objectId"])

            self.assertEqual(("opened-catalog", str(hats_path)), result)
            open_catalog.assert_called_once_with(
                str(hats_path),
                columns=["objectId"],
            )
            self.assertFalse(
                (temp_path / "process/science_catalogs_hats_config.yaml").exists()
            )

    def test_empty_selected_cols_loads_all_direct_hats_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            hats_path = temp_path / "dp1_hats"
            hats_path.mkdir()
            (hats_path / "collection.properties").write_text(
                "hats_order=3\n",
                encoding="utf-8",
            )
            resolver = self.create_resolver(
                dataset_path=hats_path,
                cwd=temp_path / "process",
                param={"hats_config": {}},
            )

            with mock.patch.object(
                sys.modules["lsdb"],
                "open_catalog",
                create=True,
                return_value="opened-catalog",
            ) as open_catalog:
                resolver.resolve(client="client", selected_cols=[])

            open_catalog.assert_called_once_with(
                str(hats_path),
                columns="all",
            )

    def test_empty_hats_config_does_not_discover_yaml_or_derived_hats_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            release_path = temp_path / "dp1"
            release_path.mkdir()
            (temp_path / "dp1.mag.auto.sfd.yaml").write_text(
                "input:\n  catalog_folder: catalogs/object\n",
                encoding="utf-8",
            )
            derived_hats_path = release_path / "mag/auto/sfd/catalog"
            derived_hats_path.mkdir(parents=True)
            (derived_hats_path / "collection.properties").write_text(
                "hats_order=3\n",
                encoding="utf-8",
            )
            resolver = self.create_resolver(
                dataset_path=release_path,
                cwd=temp_path / "process",
                param={
                    "hats_config": {},
                    "flux_type": "auto",
                    "convert_flux_to_mag": True,
                    "dereddening": "sfd",
                },
            )

            with self.assertRaisesRegex(
                FileNotFoundError,
                "inputs.dataset.path is not a HATS catalog",
            ):
                resolver.resolve(client="client", selected_cols=["objectId"])

    def test_non_object_hats_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            resolver = self.create_resolver(
                dataset_path=temp_path / "dp1",
                cwd=temp_path / "process",
                param={"hats_config": []},
            )

            with self.assertRaisesRegex(
                ValueError,
                "param.hats_config must be an object",
            ):
                resolver.create_runtime_science_catalogs_config()
