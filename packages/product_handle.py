import abc
import csv
import glob
import os

# from astropy.io.votable import from_table
from collections import OrderedDict
from os import PathLike
from pathlib import Path
from typing import List, Union
import shutil

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import tables_io

FilePath = Union[str, "PathLike[str]"]
Column = Union[str, int]
OpInt = Union[int, None]
OpStr = Union[str, None]


def create_output(
    data,
    cwd_dir,
    output_root_dir,
    output_dir,
    output_name,
    extension,
    ra_column="ra",
    dec_column="dec",
    temp_dir=None,
    client=None,
    logger=None,
    hats_size_threshold_mb=200,
):
    """Create output file

    Args:
        data (pandas.dataframe):
        output_root_dir (str): output root dir
        output_dir (str): output dir
        output_name (str): output name
        extension (str): output extension

    Return:
        str: output filepath
    """

    extension = extension.lower()
    outputfile = str(Path(output_dir, f"{output_name}.{extension}"))
    output_full_file = str(Path(output_root_dir, outputfile).resolve())
    output_temp = str(Path(cwd_dir, f"{output_name}.{extension}"))

    match extension:
        case "csv":
            if hasattr(data, "compute") and callable(data.compute):
                data = data.compute()
            data.to_csv(output_temp)
        case "fits" | "fit" | "hf5" | "hdf5" | "h5" | "pq" | "parquet":
            if hasattr(data, "compute") and callable(data.compute):
                data = data.compute()
            tables_io.write(data, output_temp, extension)
        case "hats":
            _write_hats_output(
                data,
                output_temp,
                ra_column=ra_column,
                dec_column=dec_column,
                temp_dir=temp_dir,
                client=client,
                logger=logger,
                size_threshold_mb=hats_size_threshold_mb,
            )
        case "votable":
            raise NotImplementedError("VOTable not implemented")
            # outputfile = str(Path(output_dir, f"{output_name}.xml").resolve())
            # table = tables_io.convert(data, tables_io.types.AP_TABLE)
            # votable = from_table(table)
            # votable.to_xml(outputfile)
        case _:
            raise ValueError(
                f"The {extension} extension is not valid for output file from this pipeline."
            )

    # Copy the output temporary to the output full dir
    if extension == "hats":
        src = Path(output_temp)
        dst = Path(output_full_file)
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
            else:
                dst.unlink(missing_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(output_temp, output_full_file)

    return outputfile


def _write_hats_output(
    data,
    output_dir: str,
    ra_column: str = "ra",
    dec_column: str = "dec",
    *,
    temp_dir=None,
    client=None,
    logger=None,
    size_threshold_mb: int = 200,
):
    """Persist output dataframe as a HATS catalog directory.

    This helper uses lsdb runtime APIs with compatibility fallbacks because
    writer method names may vary across versions. Lazy/large dataframe outputs
    are staged as parquet and imported with hats-import to avoid loading the
    whole product in memory.
    """
    output_path = Path(output_dir)

    if output_path.exists():
        if output_path.is_dir():
            shutil.rmtree(output_path, ignore_errors=True)
        else:
            output_path.unlink(missing_ok=True)

    try:
        import lsdb
    except Exception as error:
        raise RuntimeError(
            "lsdb is required to export HATS output in training_set_maker."
        ) from error

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not hasattr(data, "columns") or not {ra_column, dec_column}.issubset(set(data.columns)):
        raise ValueError(
            f"HATS output requires '{ra_column}' and '{dec_column}' columns in the final data."
        )

    # LSDB catalog-like outputs can be written lazily by LSDB itself.
    if (
        hasattr(data, "write_catalog")
        and callable(getattr(data, "write_catalog"))
    ) or (hasattr(data, "to_hats") and callable(getattr(data, "to_hats"))):
        _write_catalog_like_to_hats(data, output_path)
        return

    if _is_dask_dataframe(data):
        _log_info(
            logger,
            "Writing lazy HATS output via parquet staging before threshold-based HATS build: %s",
            output_path,
        )
        parquet_path = _stage_hats_output_parquet(data, output_path, temp_dir, logger)
        build_collection_with_retry(
            parquet_path=parquet_path,
            output_path=output_path,
            output_artifact_name=output_path.name,
            catalog_artifact_name="catalog",
            margin_artifact_name=_margin_artifact_name(),
            client=client,
            logger=logger,
            ra_column=ra_column,
            dec_column=dec_column,
            try_margin=True,
            size_threshold_mb=size_threshold_mb,
        )
        return

    data_size_mb = _dataframe_memory_mb(data)
    if data_size_mb > size_threshold_mb:
        _log_info(
            logger,
            "HATS output dataframe is %.1f MB; using parquet staging and hats-import: %s",
            data_size_mb,
            output_path,
        )
        parquet_path = _stage_hats_output_parquet(data, output_path, temp_dir, logger)
        build_collection_with_retry(
            parquet_path=parquet_path,
            output_path=output_path,
            output_artifact_name=output_path.name,
            catalog_artifact_name="catalog",
            margin_artifact_name=_margin_artifact_name(),
            client=client,
            logger=logger,
            ra_column=ra_column,
            dec_column=dec_column,
            try_margin=True,
            size_threshold_mb=0,
        )
        return

    catalog = lsdb.from_dataframe(
        data,
        catalog_name="catalog",
        use_pyarrow_types=False,
        ra_column=ra_column,
        dec_column=dec_column,
    )
    _write_catalog_like_to_hats(catalog, output_path)
    _normalize_collection_margin_name(
        output_path,
        old_margin_name="catalog_5arcs",
        new_margin_name=_margin_artifact_name(),
    )


def _is_dask_dataframe(data):
    return hasattr(data, "to_parquet") and hasattr(data, "map_partitions")


def _dataframe_memory_mb(data):
    try:
        return float(data.memory_usage(deep=True).sum()) / 1024**2
    except Exception:
        return float("inf")


def _margin_artifact_name(margin_threshold: float = 5.0) -> str:
    threshold = int(margin_threshold) if float(margin_threshold).is_integer() else margin_threshold
    return f"margin_{threshold}arcs"


def _normalize_collection_margin_name(
    collection_path: Path,
    old_margin_name: str,
    new_margin_name: str,
):
    """Rename LSDB-generated margin directory and update collection metadata."""
    old_margin_path = collection_path / old_margin_name
    new_margin_path = collection_path / new_margin_name

    if old_margin_path.is_dir() and old_margin_path != new_margin_path:
        if new_margin_path.exists():
            shutil.rmtree(new_margin_path, ignore_errors=True)
        old_margin_path.rename(new_margin_path)

    properties_path = collection_path / "collection.properties"
    if not properties_path.exists():
        return

    text = properties_path.read_text(encoding="utf-8")
    text = text.replace(f"all_margins={old_margin_name}", f"all_margins={new_margin_name}")
    text = text.replace(f"default_margin={old_margin_name}", f"default_margin={new_margin_name}")
    properties_path.write_text(text, encoding="utf-8")


def _stage_hats_output_parquet(data, output_path: Path, temp_dir, logger):
    """Write final output as parquet parts for hats-import without full materialization."""
    staging_root = Path(temp_dir) if temp_dir else output_path.parent / "temp"
    parquet_path = staging_root / f"{output_path.stem}_hats_parquet"

    if parquet_path.exists():
        shutil.rmtree(parquet_path, ignore_errors=True)
    parquet_path.mkdir(parents=True, exist_ok=True)

    _log_info(logger, "Staging HATS output parquet at %s", parquet_path)
    if _is_dask_dataframe(data):
        data.to_parquet(
            str(parquet_path),
            engine="pyarrow",
            write_index=False,
            overwrite=True,
        )
    else:
        table = pa.Table.from_pandas(data.reset_index(drop=True), preserve_index=False)
        pq.write_table(table, parquet_path / "part-0.parquet")

    return parquet_path


def build_collection_with_retry(
    parquet_path,
    logger=None,
    client=None,
    try_margin: bool = True,
    *,
    size_threshold_mb: int = 200,
    output_path=None,
    output_artifact_name: str | None = None,
    catalog_artifact_name: str | None = None,
    margin_artifact_name: str | None = None,
    margin_threshold: float = 5.0,
    ra_column: str = "ra",
    dec_column: str = "dec",
) -> str:
    """Build a HATS collection from parquet using LSDB for small data and hats-import for large data."""
    parquet_path = Path(parquet_path).resolve()
    if output_path is not None:
        collection_path = Path(output_path).resolve()
        parent_dir = collection_path.parent
        collection_artifact_name = output_artifact_name or collection_path.name
        catalog_artifact_name = catalog_artifact_name or collection_path.stem
    else:
        parent_dir = parquet_path.parent
        collection_artifact_name = output_artifact_name or f"{parquet_path.name}_hats"
        catalog_artifact_name = catalog_artifact_name or collection_artifact_name
        collection_path = parent_dir / collection_artifact_name
    margin_artifact_name = margin_artifact_name or f"{catalog_artifact_name}_{int(margin_threshold)}arcs"

    base_path = parquet_path / "base"
    pattern = str(base_path / "*.parquet") if base_path.exists() else str(parquet_path / "*.parquet")
    in_file_paths = sorted(glob.glob(pattern))
    if not in_file_paths:
        raise ValueError(f"No Parquet files found at '{parquet_path}'")

    try:
        total_size_mb = sum(os.path.getsize(path) for path in in_file_paths) / 1024**2
    except Exception:
        total_size_mb = float("inf")

    if total_size_mb <= size_threshold_mb:
        try:
            import lsdb

            _log_info(
                logger,
                "Small catalog (%.1f MB). Building collection via LSDB fast path -> %s",
                total_size_mb,
                collection_path,
            )
            pdf_list = [pd.read_parquet(path) for path in in_file_paths]
            dfp = (
                pd.concat(pdf_list, ignore_index=True)
                if len(pdf_list) > 1
                else pdf_list[0]
            )
            catalog = lsdb.from_dataframe(
                dfp,
                catalog_name=catalog_artifact_name,
                ra_column=ra_column,
                dec_column=dec_column,
                use_pyarrow_types=True,
            )
            catalog.write_catalog(str(collection_path), as_collection=True, overwrite=True)
            _normalize_collection_margin_name(
                collection_path,
                old_margin_name=f"{catalog_artifact_name}_{int(margin_threshold)}arcs",
                new_margin_name=margin_artifact_name,
            )
            _log_info(logger, "Finished collection fast-path: %s", collection_path)
            return str(collection_path)
        except Exception as error:
            _log_warning(
                logger,
                "Collection fast-path failed for %s (%s: %s). Falling back to hats-import.",
                collection_artifact_name,
                type(error).__name__,
                error,
            )

    try:
        from hats_import import CollectionArguments
        from hats_import.collection.run_import import run as run_collection_import
    except Exception as error:
        raise RuntimeError(
            "hats-import is required to export large HATS output in training_set_maker."
        ) from error

    def _clean_partial():
        try:
            if collection_path.is_dir():
                shutil.rmtree(collection_path, ignore_errors=True)
        except Exception as error:
            _log_warning(logger, "Failed to remove partial '%s': %s", collection_path, error)

    _clean_partial()

    def _make_args(with_margin: bool):
        args = CollectionArguments(
            output_artifact_name=collection_artifact_name,
            output_path=str(parent_dir),
            resume=False,
        ).catalog(
            input_file_list=in_file_paths,
            file_reader="parquet",
            output_artifact_name=catalog_artifact_name,
            ra_column=ra_column,
            dec_column=dec_column,
        )
        if with_margin:
            args = args.add_margin(
                margin_threshold=margin_threshold,
                output_artifact_name=margin_artifact_name,
                is_default=True,
            )
        return args

    if try_margin:
        try:
            _log_info(
                logger,
                "Building collection WITH margin using hats-import: %s",
                collection_artifact_name,
            )
            run_collection_import(_make_args(with_margin=True), client)
            return str(collection_path)
        except Exception as error:
            _log_warning(logger, "WITH margin failed: %s. Retrying WITHOUT margin...", error)
            _clean_partial()

    try:
        _log_info(
            logger,
            "Building collection WITHOUT margin using hats-import: %s",
            collection_artifact_name,
        )
        run_collection_import(_make_args(with_margin=False), client)
    except Exception as error:
        _clean_partial()
        raise RuntimeError(f"Failed to build collection '{collection_artifact_name}': {error}") from error

    if not collection_path.is_dir():
        raise RuntimeError(f"hats-import did not create expected HATS output: {collection_path}")

    return str(collection_path)


def _log_info(logger, message, *args):
    if logger is not None:
        try:
            logger.info(message, *args)
            return
        except Exception:
            pass


def _log_warning(logger, message, *args):
    if logger is not None:
        try:
            logger.warning(message, *args)
            return
        except Exception:
            pass


def _write_catalog_like_to_hats(catalog_like, output_path: Path):
    """Write an LSDB catalog-like object as HATS."""
    write_catalog = getattr(catalog_like, "write_catalog", None)
    to_hats = getattr(catalog_like, "to_hats", None)

    attempts = []
    if callable(write_catalog):
        attempts.extend(
            [
                lambda: write_catalog(str(output_path), as_collection=True, overwrite=True),
                lambda: write_catalog(str(output_path), overwrite=True),
                lambda: write_catalog(str(output_path)),
            ]
        )
    if callable(to_hats):
        attempts.extend(
            [
                lambda: to_hats(
                    str(output_path),
                    catalog_name=output_path.stem,
                    overwrite=True,
                    progress_bar=False,
                    as_collection=False,
                ),
                lambda: to_hats(
                    str(output_path),
                    catalog_name=output_path.stem,
                    overwrite=True,
                    progress_bar=False,
                ),
                lambda: to_hats(str(output_path), overwrite=True, progress_bar=False),
                lambda: to_hats(str(output_path)),
            ]
        )

    if not attempts:
        raise RuntimeError("Could not find a compatible lsdb HATS writer in this environment.")

    last_error = None
    for run in attempts:
        try:
            result = run()
            if hasattr(result, "compute") and callable(result.compute):
                result.compute()
            return
        except TypeError as error:
            last_error = error
            continue
        except Exception as error:
            last_error = error
            continue

    raise RuntimeError(
        f"Could not write HATS output with available lsdb writer method: {last_error}"
    )


class NotTableError(TypeError):
    pass


class ProductHandle:
    def df_from_file(self, filepath: PathLike, **kwargs) -> pd.DataFrame:
        """TODO: Descrever essa função
        OBS: é possivel utilizar todos os argumentos da função pandas.read_csv()
        descritos aqui: https://pandas.pydata.org/docs/reference/api/pandas.read_csv.html

        Args:
            filepath (PathLike): _description_

        Returns:
            pd.DataFrame: _description_
        """
        return FileHandle(filepath).to_df(**kwargs)


class FileHandle(object):
    handle = None
    extension = None

    def __init__(self, filepath: PathLike):
        fp = Path(filepath)

        # Identificar o formato do arquivo
        self.extension = fp.suffix

        # Instancia o Handle de acordo com o tipo de arquivo
        match self.extension:
            case ".csv":
                self.handle = CsvHandle(fp)
            case ".fits" | ".fit" | ".hf5" | ".hdf5" | ".h5" | ".pq" | ".parquet":
                self.handle = TableIOHandle(fp)
            case ".zip" | ".tar" | ".gz":
                self.handle = CompressedHandle(fp)
            case _:
                # Try TxtHandle
                self.handle = TxtHandle(fp)
                # message = f"The {self.extension} extension has not yet been implemented"
                # raise NotImplementedError(message)

    def to_df(self, **kwargs):
        return self.handle.to_df(**kwargs)


class BaseHandle(object):
    filepath = None

    def __init__(self, filepath: PathLike):
        self.filepath = filepath

    @abc.abstractmethod
    def to_df(self, **kwargs) -> pd.DataFrame:
        raise NotImplementedError


class CsvHandle(BaseHandle):
    delimiter = str
    has_hd = bool  # True se o arquivo CSV possuir Headers na primeira linha.
    # Lista com nome das colunas que podem ser str ou int.
    column_names = [Column]

    def __init__(self, filepath: PathLike):
        super().__init__(filepath)

        self.delimiter = self.get_delimiter()
        self.has_hd = self.has_header()
        self.column_names = self.get_column_names()

    def to_df(self, **kwargs) -> pd.DataFrame:
        # Check for header parameter
        if "header" in kwargs:
            raise Exception(
                "It is not possible to use the header argument in the df_from_file() method when the file is a .csv file."
            )

        if "delimiter" in kwargs:
            raise Exception(
                "It is not possible to use the delimiter argument in the df_from_file() method when the file is a .csv file."
            )

        if self.has_hd:
            df = pd.read_csv(
                self.filepath, header=0, delimiter=self.delimiter, **kwargs
            )
        else:
            df = pd.read_csv(
                self.filepath,
                header=None,
                names=self.column_names,
                delimiter=self.delimiter,
                **kwargs,
            )
        return df

    def has_header(self) -> bool:
        """Verifica se o csv tem Header.
        Usa dois metodos diferentes para checar.
        - vai considerar que tem Header se tiver pelo menos uma coluna como string.
        - vai considerar que tem Header se os tipos de dados forem diferentes na primeira linha.
        - Retorna True se ambos os metodos retornarem True.
        - Retorna False se um dos metodos retornar False.
        Args:
            path (_type_): _description_

        Returns:
            bool: True se o csv possuir Headers na primeira linha.
        """
        temp = []

        # Method: open csv twice considering with header and without header,
        # if the data types are the same in both times it probably doesn't have header.
        # https://stackoverflow.com/questions/53100598/can-pandas-auto-recognize-if-header-is-present/53101192#53101192
        df = pd.read_csv(self.filepath, header=None, delimiter=self.delimiter, nrows=20)
        df_header = pd.read_csv(self.filepath, delimiter=self.delimiter, nrows=20)
        if tuple(df.dtypes) != tuple(df_header.dtypes):
            temp.append(True)
        else:
            temp.append(False)

        # Method: Check if at least one in first Line is String.
        # https://stackoverflow.com/questions/38360496/pandas-read-csv-without-knowing-whether-header-is-present/62217716#62217716
        if any(df.iloc[0].apply(lambda x: isinstance(x, str))):
            temp.append(True)
        else:
            temp.append(False)
        return all(temp)

    def get_column_names(self) -> List[Column]:
        df = pd.read_csv(self.filepath, header=None, delimiter=self.delimiter, nrows=5)

        if self.has_hd:
            df = df[1:].reset_index(drop=True).rename(columns=df.iloc[0])
            columns = df.columns.tolist()
        else:
            # Cria nomes para as colunas de forma sequencial [0...len(headers)]
            # o Resultado é uma lista de str: ['0', ...,'10',...]
            columns = [str(i) for i in [*range(0, len(df.iloc[0]))]]

        return columns

    def get_delimiter(self):
        """Get delimiter from file

        Returns:
            str: delimiter
        """
        with open(self.filepath, "r") as csvfile:
            dt = csvfile.readline().replace("\n", "")
            delimiter = csv.Sniffer().sniff(dt).delimiter

        return delimiter


class TableIOHandle(BaseHandle):
    def __init__(self, filepath: PathLike):
        super().__init__(filepath)

    def to_df(self, **kwargs) -> pd.DataFrame:
        # Le o arquivo utilizando o metodo read da tables_io
        # O retorno é um astropy table.
        df = tables_io.read(self.filepath, tables_io.types.AP_TABLE)

        # Trata arquivo com mais de uma tabela, pegando a primeira.
        if isinstance(df, OrderedDict):
            df = df[list(df.keys())[0]]

        # Converte o astropy table para pandas.Dataframe
        try:
            df = df.to_pandas()
        except ValueError as _:
            df = tables_io.read(self.filepath, tables_io.types.PD_DATAFRAME)

        return df


class CompressedHandle(BaseHandle):
    def __init__(self, filepath: PathLike):
        super().__init__(filepath)

    def to_df(self, **kwargs) -> pd.DataFrame:
        raise NotTableError(
            "It is not possible to return a dataframe from this file type."
        )


class TxtHandle(BaseHandle):
    delimiter = str
    has_hd = bool
    skiprows = int
    # Lista com nome das colunas que podem ser str ou int.
    column_names = [Column]

    def __init__(self, filepath: PathLike):
        super().__init__(filepath)

        self.delimiter = self.get_delimiter()

        self.delim_whitespace = False
        if self.delimiter == None:
            self.delim_whitespace = True

        self.skiprows = self.count_skiprows()

        self.has_hd = self.has_header()

        self.column_names = self.get_column_names()

    def to_df(self, **kwargs) -> pd.DataFrame:
        # Try read table using numpy
        # Ignore all lines start with #
        # ignores all initial lines that after the split have only string.
        # Acept space as delimiter.
        # Does not accept string values.
        return pd.read_csv(
            self.filepath,
            delimiter=self.delimiter,
            skiprows=self.skiprows,
            names=self.column_names,
            header=None,
            delim_whitespace=self.delim_whitespace,
        )

    def has_header(self) -> bool:
        if self.skiprows == 1:
            return True
        else:
            return False

    def get_column_names(self) -> List[Column]:
        # Tenta ler o nome das colunas apenas para arquivos onde a primeira linha é toda de string.
        if self.has_hd:
            with open(self.filepath, "r") as fp:
                line = fp.readline().replace("\r\n", "")
                if line.startswith("#"):
                    line = line.strip("#").strip()

                columns = [str(i).strip() for i in line.split(self.delimiter)]
        else:
            # Cria nomes para as colunas de forma sequencial [0...len(headers)]
            # o Resultado é uma lista de str: ['0', ...,'10',...]
            df = pd.read_csv(
                self.filepath,
                delimiter=self.delimiter,
                skiprows=self.skiprows,
                header=None,
                delim_whitespace=self.delim_whitespace,
            )
            columns = [str(i) for i in [*range(0, len(df.iloc[0]))]]
        return columns

    def get_delimiter(self):
        """Get delimiter from file

        Returns:
            str: delimiter
        """
        with open(self.filepath, "r") as csvfile:
            dt = csvfile.readline().replace("\n", "")
            delimiter = csv.Sniffer().sniff(dt).delimiter

        if delimiter.isspace():
            return None

        return delimiter

    def count_skiprows(
        self,
    ):
        count = 0
        with open(self.filepath, "r") as f:
            for line in f:
                if line.startswith("#") or self.is_line_str(line.strip("\r\n")):
                    count += 1
                else:
                    break
        return count

    def is_line_str(self, line):
        return all(
            (
                isinstance(el, str) and not el.isnumeric()
                for el in line.split(self.delimiter)
            )
        )
