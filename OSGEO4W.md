# OSGeo4W Packaging Repository

This repository provides the build, packaging, and publication
infrastructure for the OSGeo4W v2 distribution on Windows.

Its purpose is to build a large collection of packages, resolve and encode
their dependencies, generate repository metadata, and optionally produce
profile-based installers on top of already built package sets.

## Overview

The repository is designed for workflows such as:

- building individual OSGeo4W packages from source;
- rebuilding complete dependency stacks for projects such as QGIS, GDAL,
  Qt, Python packages, and server-side components;
- producing a local package repository for testing;
- generating `setup.ini` and related repository metadata;
- creating installer wrappers for selected package profiles.

The repository is not centered around a single application. Its main output
is an OSGeo4W-compatible package repository containing package archives,
index files, and metadata.

## System Model

OSGeo4W is structured here as a package-oriented build system.

Each package is described by a recipe stored in:

```text
src/<name>/osgeo4w/package.sh
````

A recipe defines:

- which binary packages it produces;
- which packages are required for building;
- which version and revision are to be published.

Shared helper scripts in `scripts/build-helpers` provide the common build
infrastructure, including environment preparation, dependency installation,
toolchain setup, Python and pip integration, packaging helpers, and build
logging.

After a package is built, the repository infrastructure generates:

- package tarballs;
- `setup.hint` metadata files;
- repository index files such as `setup.ini`.

The resulting repository can be consumed directly by the OSGeo4W installer.

## Repository Layout

### Top-Level Structure

- `bootstrap.cmd`
  Windows entry point. Installs a minimal Cygwin environment and transfers
  control to the shell-based build workflow.

- `bootstrap.sh`
  Shell entry point. Initializes the working tree and starts the main build
  process.

- `scripts/`
  Shared infrastructure for build orchestration, dependency ordering,
  helper functions, repository metadata generation, upload workflows,
  validation, and installer generation.

- `src/`
  Package recipes. A directory typically corresponds to one upstream
  project or one logical package group.

## Build Flow

```text
bootstrap.cmd
  -> bootstrap.sh
    -> scripts/build.sh
      -> scripts/build-inorder.pl
        -> src/<pkg>/osgeo4w/package.sh
          -> scripts/build-helpers
            -> local OSGeo4W root for build dependencies
            -> x86_64/release/... tar.bz2 + setup.hint
      -> scripts/genini
        -> x86_64/setup.ini + x86_64/setup-lic.ini
```

## Packaging Unit

The primary unit in this repository is a package recipe rather than an
installer.

A single recipe may produce:

- one runtime package;
- runtime and development packages;
- runtime, tools, documentation, and QML packages;
- meta-packages containing only dependency declarations;
- profile packages aggregating existing dependencies.

This model is used extensively for large ecosystems such as Qt and QGIS,
where one recipe may emit multiple related binary packages.

## Package Build Lifecycle

Most `package.sh` files follow the same general structure:

1. Define core variables:

   - `P` — base package name;
   - `V` — upstream version;
   - `B` — binary revision or revision strategy;
   - `BUILDDEPENDS` — packages required in the build root;
   - `PACKAGES` — packages produced by the recipe.
2. Source `scripts/build-helpers`.
3. Call `startlog` to initialize the build environment and install
   dependencies.
4. Build or repackage upstream artifacts.
5. Generate `setup.hint` for each output package.
6. Place output archives into `x86_64/release/...`.
7. Call `endlog` to finalize the build and regenerate repository metadata.

## Recipe Categories

The repository contains several common recipe patterns.

### Native Source Builds

Used for C/C++ libraries and large applications such as Qt, GDAL, PDAL,
and QGIS.

Typical steps include:

- initializing the Visual Studio toolchain;
- configuring CMake and Ninja;
- wiring include and library paths against the local OSGeo4W root;
- building into a staging directory;
- splitting artifacts into runtime, development, symbols, plugins,
  documentation, and related outputs.

This approach is used when ABI compatibility and consistent integration with
the rest of the stack are required.

### Python Wheel Packaging

Used for many `python3-*` packages through the `packagewheel` helper from
`scripts/build-helpers`.

Typical workflow:

- use Python from the local OSGeo4W root;
- install the package into a staging environment with pip;
- include wheel-provided binaries when needed;
- generate OSGeo4W package archives and metadata.

This is the standard path for many Python dependencies that do not require
a complex native build process.

### Repackaging Prebuilt Binaries

Some recipes repackage upstream binary distributions instead of compiling
from source.

Typical examples include:

- `apache`;
- `node`;
- `swig`;
- `gs`.

In such cases, the recipe adapts the upstream layout to OSGeo4W conventions
and may add installation or removal hooks.

## Build Infrastructure

The following scripts form the core of the repository infrastructure.

| Tool                                         | Purpose                                                         |
| -------------------------------------------- | --------------------------------------------------------------- |
| `bootstrap.cmd`                              | Install minimal Cygwin tooling and start the build from Windows |
| `bootstrap.sh`                               | Initialize the checkout and invoke `scripts/build.sh`           |
| `scripts/build.sh`                           | Main build orchestrator                                         |
| `scripts/build-inorder.pl`                   | Resolve build order from dependency relationships               |
| `scripts/build-helpers`                      | Shared helper library for all recipes                           |
| `scripts/genini`                             | Generate `setup.ini` and `setup-lic.ini`                        |
| `scripts/regen.sh`                           | Regenerate repository metadata explicitly                       |
| `scripts/upload.sh`, `scripts/upload.pl`     | Publish locally built packages to the master repository         |
| `scripts/createmsi.pl`                       | Build MSI installers from selected package sets                 |
| `scripts/msis.sh`                            | Run MSI generation for standard profiles                        |
| `scripts/lint.sh`                            | Validate recipes and common conventions                         |
| `scripts/pippkg.py`                          | Support automation for Python package recipes                   |
| `scripts/update-outdated-python-packages.sh` | Refresh outdated Python package definitions                     |

## Usage

### Build a Single Package

From Windows:

```bat
bootstrap.cmd qgis-dev
```

From a shell environment:

```bash
bash bootstrap.sh qgis-dev
```

This process typically:

- determines the required recipes;
- resolves the dependency order;
- installs missing build dependencies into the local build root;
- produces a local package repository under `x86_64/`.

### Build Multiple Packages

```bash
bash bootstrap.sh gdal qt6 qgis-dev
```

The build system computes a valid order automatically and may also rebuild
reverse dependencies depending on configuration.

### Use a Local Repository for Testing

After a successful build, the `x86_64/` directory can be used as a local
package feed for the OSGeo4W installer.

This is the standard validation path before publication.

### Build MSI Installers

```bash
bash scripts/msis.sh qgis
```

This workflow creates an MSI installer from an already built package feed by
resolving the selected profile and packaging the resulting installation set.

## Output Artifacts

Typical outputs include:

- `x86_64/release/<pkg>/...tar.bz2`
  OSGeo4W package payloads;

- `x86_64/release/<pkg>/setup.hint`
  package metadata such as description, dependencies, categories,
  maintainer, and version information;

- `x86_64/setup.ini`
  main repository index for the OSGeo4W installer;

- `x86_64/setup-lic.ini`
  repository index including license and extended metadata;

- `tmp/`, `package.log`
  temporary files and build logs.

## Continuous Integration

The repository includes GitHub Actions workflows targeting
`windows-latest`.

These workflows support:

- building arbitrary package sets;
- enabling or disabling reverse dependency builds;
- skipping tests;
- continuing after partial failures;
- publishing the local `x86_64/` repository as a CI artifact;
- generating MSI installers when required.

The CI workflows follow the same packaging model as local builds.

## Delivery Layer Extensions

The repository is based on a package-oriented distribution model. Its native
strength lies in producing many small, composable packages with explicit
dependency metadata.

This model supports:

- dependency reuse across products;
- partial updates;
- separate delivery of runtime, development, plugins, and documentation
  components;
- transparent meta-package composition.

Additional delivery layers can be built on top of this model.

One existing example is `scripts/createmsi.pl`, which consumes repository
metadata, resolves dependencies for a selected profile, expands the package
set into a staging tree, and produces an installer artifact from that
snapshot.

The same general approach can be used for other installer technologies by:

1. generating or consuming a package repository snapshot;
2. resolving the dependency closure of a selected profile or meta-package;
3. expanding the resulting package set into a staging root;
4. packaging that staging root into a final installer format.

This makes it possible to use OSGeo4W as the canonical build and packaging
backend while exposing a different final delivery format for specific
distribution scenarios.

## Summary

This repository is a package build and publication system for the Windows
GIS stack based on the OSGeo4W model.

Its main characteristics are:

- reproducible builds across large dependency graphs;
- a unified package metadata and versioning model;
- local and publishable repository generation;
- support for profile-oriented installer creation on top of built package
  feeds.

The repository is centered on package recipes and repository generation
rather than on a single installer artifact.
