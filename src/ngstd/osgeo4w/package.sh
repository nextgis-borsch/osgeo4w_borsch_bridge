export P=ngstd
export B=next
export MAINTAINER=NextGIS
export BUILDDEPENDS="base gdal-devel openssl-devel python3-core python3-devel python3-pyqt5 python3-pyqt-builder python3-sip qt5-devel sentrynative-devel zlib-devel"
export PACKAGES="ngstd ngstd-devel python3-ngstd"

: ${REPO:=https://github.com/nextgis/lib_ngstd.git}
: ${REPO_REF:=master}
: ${SOURCE_DIR:=source}

export REPO REPO_REF SOURCE_DIR

source ../../../scripts/build-helpers

startlog

cd ..

if [ -d $SOURCE_DIR ]; then
	cd $SOURCE_DIR
	git config core.filemode false
	git fetch origin
	git clean -f
	git reset --hard
	git checkout -f $REPO_REF
else
	git clone $REPO --branch $REPO_REF --single-branch --depth 1 $SOURCE_DIR
	cd $SOURCE_DIR
fi

MAJOR=$(sed -ne 's/^#define NGLIB_MAJOR_VERSION[[:space:]]*\([0-9]*\)$/\1/p' src/core/version.h)
MINOR=$(sed -ne 's/^#define NGLIB_MINOR_VERSION[[:space:]]*\([0-9]*\)$/\1/p' src/core/version.h)
PATCH=$(sed -ne 's/^#define NGLIB_PATCH_NUMBER[[:space:]]*\([0-9]*\)$/\1/p' src/core/version.h)
V=$MAJOR.$MINOR.$PATCH

cd ../osgeo4w

availablepackageversions $P

if [ -n "$version_curr" ]; then
	build=$binary_curr

	if [ "$V" = "$version_curr" ]; then
		(( ++build ))
	fi
else
	build=1
fi

nextbinary

(
	set -e
	set -x

	fetchenv osgeo4w/bin/o4w_env.bat

	vsenv
	cmakeenv
	ninjaenv

	[ -n "$OSGEO4W_SKIP_CLEAN" ] || rm -rf build-$V
	rm -rf install
	mkdir -p build-$V install

	cd build-$V

	cmake -G Ninja \
		-D CMAKE_BUILD_TYPE=Release \
		-D CMAKE_INSTALL_PREFIX=../install/apps/$P \
		-D WITH_BINDINGS=ON \
		-D WITH_GDAL_EXTERNAL=ON \
		-D WITH_SENTRYNATIVE_EXTERNAL=ON \
		-D WITH_OpenSSL_EXTERNAL=ON \
		-D WITH_ZLIB_EXTERNAL=ON \
		-D WITH_Qt5_EXTERNAL=ON \
		-D Python3_EXECUTABLE=$(cygpath -am ../osgeo4w/bin/python3.exe) \
		../../$SOURCE_DIR

	cmake --build .
	cmake --build . --target install
	cmakefix ../install
)

mkdir -p install/apps/$PYTHON/Lib/site-packages
for site_packages_dir in install/apps/$P/lib/Python*/site-packages; do
	if [ -d $site_packages_dir ]; then
		cp -a $site_packages_dir/. install/apps/$PYTHON/Lib/site-packages/
	fi
done
rm -rf install/apps/$P/lib/Python*

mkdir -p install/etc/abi
cat <<EOF >install/etc/abi/$P-devel
$P
EOF

export R=$OSGEO4W_REP/x86_64/release/$P
mkdir -p $R/$P-devel $R/python3-$P

cat <<EOF >$R/setup.hint
sdesc: "NextGIS standard runtime library"
ldesc: "NextGIS standard runtime library"
maintainer: $MAINTAINER
category: Libs
requires: msvcrt2019 qt5-libs gdal sentrynative openssl
EOF

cat <<EOF >$R/$P-devel/setup.hint
sdesc: "NextGIS standard development files"
ldesc: "NextGIS standard development files"
maintainer: $MAINTAINER
category: Libs
requires: $P
external-source: $P
EOF

cat <<EOF >$R/python3-$P/setup.hint
sdesc: "NextGIS standard Python bindings"
ldesc: "NextGIS standard Python bindings"
maintainer: $MAINTAINER
category: Python
requires: $P python3-core python3-pyqt5 python3-sip
external-source: $P
EOF

appendversions $R/setup.hint
appendversions $R/$P-devel/setup.hint
appendversions $R/python3-$P/setup.hint

tar -C install -cjf $R/$P-$V-$B.tar.bz2 \
	--xform "s,apps/$P/bin,bin," \
	--xform "s,apps/$P/share/translations,share/translations," \
	apps/$P/bin \
	apps/$P/share/translations

tar -C install -cjf $R/$P-devel/$P-devel-$V-$B.tar.bz2 \
	--xform "s,apps/$P/include,include," \
	--xform "s,apps/$P/lib,lib," \
	--xform "s,apps/$P/share,share," \
	apps/$P/include \
	apps/$P/lib \
	apps/$P/share \
	etc/abi/$P-devel

tar -C install -cjf $R/python3-$P/python3-$P-$V-$B.tar.bz2 \
	--exclude="*.pyc" \
	--exclude="__pycache__" \
	apps/$PYTHON/Lib/site-packages

tar -C .. -cjf $R/$P-$V-$B-src.tar.bz2 \
	osgeo4w/package.sh

cp ../$SOURCE_DIR/LICENSE $R/$P-$V-$B.txt
cp ../$SOURCE_DIR/LICENSE $R/$P-devel/$P-devel-$V-$B.txt
cp ../$SOURCE_DIR/LICENSE $R/python3-$P/python3-$P-$V-$B.txt

endlog