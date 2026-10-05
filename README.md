# PZ Server Training Set Maker

Creates customized training and validation/test sets using a compilation of spectroscopic redshifts and LSST photometric data.


## Acknowledgements

Software developed and delivered as part of the in-kind contribution program BRA-LIN, from LIneA to the Rubin Observatory's LSST. An overview of this and other contributions is available [here](https://linea-it.github.io/pz-lsst-inkind-doc/). The pipelines take advantage of the software support layer developed by LINCC, available as Python libraries: [hats](https://github.com/astronomy-commons/hats), [hats-import](https://github.com/astronomy-commons/hats-import) and [lsdb](https://github.com/astronomy-commons/lsdb).


## Tests

### Test data

This repository currently contains a basic dataset, for testing purposes only. The ideal is to connect the pipelines to systems with access to a larger datasets.

### Install

The only requirement is to have `micromamba` available in `PATH`:

```bash
git clone https://github.com/linea-it/pzserver_training_set_maker && cd pzserver_training_set_maker
./setup.sh
source env.sh
```

To install the pipeline at once:

```bash
./install.sh
```

The `setup.sh` will suggest a directory where the pipelines and datasets are installed, type 'yes' to confirm or 'no' to configure the desired path in each case with the respective environment variables and then run again `setup.sh`.

The installation script creates the `pipe_tsm` environment with `micromamba`.

By default the scripts use `MAMBA_ROOT_PREFIX="$HOME/.micromamba"`. On a Slurm cluster, point this variable to a persistent location visible to the jobs if needed:

```bash
export MAMBA_ROOT_PREFIX=/path/to/shared/or/persistent/micromamba
```

## HATS configuration

The pipeline accepts the effective `science_catalogs` configuration as the
`param.hats_config` object in its process configuration. When this object is
present and non-empty, TSM uses it to generate the HATS catalog.

At runtime, TSM writes the object to `science_catalogs_hats_config.yaml` inside
the process directory because the current `science_catalogs` API requires a
file argument. This file is an execution artifact; the submitted object remains
the source of truth. Relative catalog and dustmap paths are resolved against
`inputs.dataset.path`.

If `param.hats_config` is absent or empty, `inputs.dataset.path` must point
directly to an existing HATS catalog. TSM does not discover YAML files or derive
legacy HATS paths from the photometric parameters.

## Run a pipeline

To execute, simply:

```bash
# execute training set maker
mkdir process001
./run.sh config.yaml process001
```
