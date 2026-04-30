export P=sentrynative
export B=next
export MAINTAINER=NextGIS
export BUILDDEPENDS="base curl-devel zlib-devel"
export PACKAGES="sentrynative sentrynative-devel"

: ${REPO:=https://github.com/nextgis-borsch/lib_sentrynative.git}
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

V=$(sed -ne 's/^#define SENTRY_SDK_VERSION "\([^"]*\)"$/\1/p' include/sentry.h)

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
		-D BUILD_SHARED_LIBS=ON \
		-D WITH_CURL_EXTERNAL=ON \
		-D CMAKE_INSTALL_SYSTEM_RUNTIME_LIBS_NO_WARNINGS=TRUE \
		../../$SOURCE_DIR

	cmake --build .
	cmake --build . --target install
	cmakefix ../install
)

export R=$OSGEO4W_REP/x86_64/release/$P
mkdir -p $R/$P-devel

mkdir -p install/etc/abi
cat <<EOF >install/etc/abi/$P-devel
$P
EOF

cat <<EOF >$R/setup.hint
sdesc: "Sentry native runtime"
ldesc: "Sentry native runtime"
maintainer: $MAINTAINER
category: Libs
requires: msvcrt2019 curl zlib
EOF

cat <<EOF >$R/$P-devel/setup.hint
sdesc: "Sentry native development files"
ldesc: "Sentry native development files"
maintainer: $MAINTAINER
category: Libs
requires: $P
external-source: $P
EOF

appendversions $R/setup.hint
appendversions $R/$P-devel/setup.hint

tar -C install -cjf $R/$P-$V-$B.tar.bz2 \
	--xform "s,apps/$P/bin,bin," \
	apps/$P/bin/sentrynative.dll \
	apps/$P/bin/crashpad_handler.exe

tar -C install -cjf $R/$P-devel/$P-devel-$V-$B.tar.bz2 \
	--xform "s,apps/$P/include,include," \
	--xform "s,apps/$P/lib,lib," \
	--xform "s,apps/$P/share,share," \
	apps/$P/include \
	apps/$P/lib \
	apps/$P/share \
	etc/abi/$P-devel

tar -C .. -cjf $R/$P-$V-$B-src.tar.bz2 \
	osgeo4w/package.sh

cp ../$SOURCE_DIR/LICENSE $R/$P-$V-$B.txt
cp ../$SOURCE_DIR/LICENSE $R/$P-devel/$P-devel-$V-$B.txt

endlog
