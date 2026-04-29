# NextGIS OSGeo4W Bridge

This repository is the Windows packaging bridge between the OSGeo4W build
layout and the historical NextGIS delivery stack based on borsch, repka and
Qt Installer Framework.

The bridge keeps the existing OSGeo4W build recipes and repository structure,
but adds a NextGIS-specific orchestration layer that can:

- build selected source packages or the whole tree;
- resolve changed packages since a git tag;
- hydrate build dependencies from repka-compatible artifacts instead of
  rebuilding them locally;
- convert OSGeo4W binary tarballs into borsch/repka-style repositories with
  `version.str` and compiler-tagged ZIP archives;
- generate QtIFW package metadata from the same source-of-truth mapping;
- dispatch MSI generation through the existing OSGeo4W MSI scripts.

## Layout

- `scripts/build.sh`, `scripts/build-inorder.pl`, `scripts/msis.sh`
  preserve the upstream OSGeo4W build flow.
- `scripts/nextgis.py` is the new entry point for NextGIS workflows.
- `nextgis/bridge/` contains the Python orchestration code.
- `nextgis/config/repositories.json` is the source-of-truth mapping for:
  - bridge source package name;
  - repka repository name;
  - borsch packet/archive name;
  - QtIFW component definitions and copy rules.
- `src/nextgisqgis/osgeo4w/package.sh` is a thin wrapper over `qgis-ltr`.
- `src/ngstd/osgeo4w/package.sh` and `src/sentrynative/osgeo4w/package.sh`
  add the missing NextGIS runtime packages.

## Packaging Model

OSGeo4W and repka use different artifact models.

OSGeo4W produces split binary tarballs, for example:

- `openjpeg`
- `openjpeg-devel`
- `openjpeg-tools`

The bridge repacks those outputs into a repka-compatible repository root:

- one repository directory such as `lib_openjpeg/`
- one `build/version.str`
- one runtime compiler-tagged ZIP archive such as
  `openjpeg-2.5.4-MSVC-19.38-64bit.zip`
- when OSGeo4W emits a `-devel` package, one additional compiler-tagged ZIP
  such as `openjpeg-devel-2.5.4-MSVC-19.38-64bit.zip`

This is why the mapping file is mandatory: one NextGIS repository may be built
from several OSGeo4W binary packages, and QtIFW components may consume only a
subset of the repacked files.

## Main Commands

Run all commands from the repository root.

Create a build snapshot:

```bash
python3 scripts/nextgis.py snapshot \
  --output nextgis/state/build-manifest.json
```

List packages changed since a tag:

```bash
python3 scripts/nextgis.py changes --tag v1.0.0
```

Convert finished OSGeo4W release tarballs into repka-compatible repositories:

```bash
python3 scripts/nextgis.py package \
  gdal openjpeg nextgisqgis ngstd sentrynative \
  --release-root x86_64/release \
  --artifacts-root nextgis/artifacts
```

Generate QtIFW overlay metadata from the repacked repositories:

```bash
python3 scripts/nextgis.py qtifw \
  --artifacts-root nextgis/artifacts \
  --output nextgis/qtifw
```

Build selected packages, hydrate dependencies from repka when the exact recipe
version is available, rebuild missing or outdated dependencies from source,
repack them for borsch/repka and generate QtIFW metadata in one run:

```bash
python3 scripts/nextgis.py build \
  nextgisqgis ngstd sentrynative \
  --repka-root https://rm.nextgis.com/repository/windows \
  --release-root x86_64/release \
  --artifacts-root nextgis/artifacts \
  --qtifw-output nextgis/qtifw \
  --snapshot-output nextgis/state/build-manifest.json
```

Build only packages changed since a tag and also build reverse dependencies:

```bash
python3 scripts/nextgis.py build \
  --changed-since-tag v1.0.0 \
  --build-reverse-dependencies
```

Build everything from source and ignore repka hydration completely:

```bash
python3 scripts/nextgis.py build --ignore-repka
```

Request MSI output through the legacy MSI pipeline:

```bash
python3 scripts/nextgis.py build \
  nextgisqgis \
  --packaging-backend both \
  --msi-mirror https://download.osgeo.org/osgeo4w/v2
```

## Source-of-Truth Rules

- OSGeo4W recipes remain responsible for compilation and split binary tarballs.
- `nextgis/config/repositories.json` is responsible for naming and packaging
  semantics outside OSGeo4W.
- QtIFW metadata must be generated from this bridge repository, not authored by
  hand in `nextgis_installer`.
- `nextgis_installer` should consume generated output or receive automated
  updates from CI.

## New Recipes

### nextgisqgis

`nextgisqgis` is implemented as a thin wrapper around the `qgis-ltr` recipe.
The wrapper changes the package name, checkout target and repository URL while
reusing the Qt5/LTR packaging logic.

The wrapper currently enables `WITH_NGSTD_EXTERNAL=ON` and adds the explicit
build dependencies required by the NextGIS fork.

### gdal

The `gdal` recipe keeps the existing OSGeo4W packaging logic, but the source is
fetched from the NextGIS fork instead of the upstream release tarball. Override
`REPO`, `REPO_REF` or `SOURCE_DIR` if a different fork or ref is needed.

### ngstd and sentrynative

These recipes create native OSGeo4W outputs for the missing NextGIS runtime
packages:

- runtime package;
- development package;
- Python bindings package for `ngstd`.

## Environment Notes

- The build recipes still assume the existing OSGeo4W and MSVC environment.
- `scripts/nextgis.py` is orchestration only. It does not replace
  `scripts/build.sh`; it drives it.
- Compiler-tagged ZIP names can be overridden with `--compiler-tag` or the
  `NEXTGIS_COMPILER_TAG` environment variable.
- Repka hydration can use either a local directory or an HTTP root containing
  generated repositories.

## Recommended Workflow

1. Build or update the required OSGeo4W packages.
2. Run `snapshot` and commit the manifest when you want reliable
   change-detection against future tags.
3. Run `package` or `build --packaging-backend borsch` to create repka-ready
   repositories.
4. Run `qtifw` or `build --qtifw-output ...` to generate installer metadata.
5. Publish the generated repositories and let CI open update PRs for
   `nextgis_installer` if needed.
