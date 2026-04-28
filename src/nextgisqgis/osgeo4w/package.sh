#!/usr/bin/env bash

export P=nextgisqgis
export BUILDDEPENDS="expat-devel fcgi-devel proj-devel gdal-devel qt5-oci sqlite3-devel geos-devel gsl-devel libiconv-devel libzip-devel libspatialindex-devel python3-pip python3-pyqt5 python3-sip python3-pyqt-builder python3-devel python3-qscintilla python3-nose2 python3-future python3-pyyaml python3-mock python3-six qca-devel qscintilla-devel qt5-devel qwt-devel libspatialite-devel oci-devel qtkeychain-devel zlib-devel opencl-devel exiv2-devel protobuf-devel python3-setuptools zstd-devel qtwebkit-devel libpq-devel libxml2-devel hdf5-devel hdf5-tools netcdf-devel pdal pdal-devel grass draco-devel python3-oauthlib ngstd-devel sentrynative-devel"
export REPO=https://github.com/nextgis/nextgisqgis.git
export CHECKOUT_DIR=nextgisqgis
export EXTRA_CMAKE_ARGS="-D WITH_NGSTD_EXTERNAL=ON"
export PACKAGES="$P $P-common $P-deps $P-devel $P-full $P-full-free $P-full-grids $P-grass-plugin $P-oracle-provider $P-pdb $P-server"

source ../../qgis-ltr/osgeo4w/package.sh