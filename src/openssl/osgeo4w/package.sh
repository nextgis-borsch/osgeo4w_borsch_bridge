export P=openssl
export V=3.0.20
export B=next
export MAINTAINER=JuergenFischer
export BUILDDEPENDS=none
export PACKAGES="openssl openssl-devel openssl-doc"

source ../../../scripts/build-helpers

startlog

[ -f $P-$V.tar.gz ] || wget https://github.com/openssl/openssl/releases/download/$P-$V/$P-$V.tar.gz
[ -d ../$P-$V ] || {
	tar -C .. -xzf $P-$V.tar.gz
	rm -f built tested installed
}

vsenv

if ! type -p perl >/dev/null; then
	log "perl was not found in PATH; install it via the configured Cygwin runtime"
	exit 1
fi

if ! type -p nasm >/dev/null; then
	log "nasm was not found in PATH; install it via the configured Cygwin runtime"
	exit 1
fi

log "Using perl from $(type -p perl)"
log "Using nasm from $(type -p nasm)"

(
	cd ../$P-$V

	if ! [ -f ../osgeo4w/built ]; then
		perl Configure VC-WIN64A --prefix=$(cygpath -aw ../osgeo4w/install) --openssldir=$(cygpath -aw ../osgeo4w/install/apps/openssl)

		nmake clean
		nmake
		touch ../osgeo4w/built
	fi

	if ! [ -f ../osgeo4w/tested ]; then
		nmake test
		touch ../osgeo4w/tested
	fi

	if ! [ -f ../osgeo4w/installed ]; then
		nmake install
		touch ../osgeo4w/installed
	fi

	cd ../osgeo4w
)

export R=$OSGEO4W_REP/x86_64/release/$P
mkdir -p $R/$P-devel $R/$P-doc install/etc/postinstall install/etc/ini install/apps/$P/certs

cat <<EOF >install/etc/postinstall/$P.bat
dllupdate -oite -copy -reboot "%OSGEO4W_ROOT%\\bin\\libcrypto-3-x64.dll"
dllupdate -oite -copy -reboot "%OSGEO4W_ROOT%\\bin\\libssl-3-x64.dll"
exit /b 0
EOF

cat <<EOF >install/etc/ini/$P.bat
set OPENSSL_ENGINES=%OSGEO4W_ROOT%\\lib\\engines-3
set SSL_CERT_FILE=%OSGEO4W_ROOT%\\bin\\curl-ca-bundle.crt
set SSL_CERT_DIR=%OSGEO4W_ROOT%\\apps\\$P\\certs
EOF

cat <<EOF >$R/setup.hint
sdesc: "OpenSSL Cryptography (Runtime)"
ldesc: "OpenSSL Cryptography (Runtime)"
category: Libs
requires: base msvcrt2019 curl-ca-bundle
maintainer: $MAINTAINER
EOF

cp ../$P-$V/LICENSE.txt $R/$P-$V-$B.txt

tar -C install -cjf $R/$P-$V-$B.tar.bz2 \
	apps/openssl/certs \
	bin/libcrypto-3-x64.dll \
	bin/libssl-3-x64.dll \
	lib/engines-3/capi.dll \
	lib/engines-3/padlock.dll \
	lib/engines-3/loader_attic.dll \
	lib/ossl-modules/legacy.dll \
	etc/postinstall/$P.bat \
	etc/ini/$P.bat

tar -C .. -cjf $R/$P-$V-$B-src.tar.bz2 \
	osgeo4w/package.sh

cp ../$P-$V/LICENSE.txt $R/$P-devel/$P-devel-$V-$B.txt

cat <<EOF >$R/$P-devel/setup.hint
sdesc: "OpenSSL Cryptography (Development)"
ldesc: "OpenSSL Cryptography (Development)"
category: Libs
requires: $P
external-source: $P
maintainer: $MAINTAINER
EOF

tar -C install -cjf $R/$P-devel/$P-devel-$V-$B.tar.bz2 \
	--exclude "apps/openssl/certs" \
	apps/openssl \
	bin/c_rehash.pl \
	bin/openssl.exe \
	include/openssl \
	lib/libcrypto.lib \
	lib/libssl.lib \
	bin/libcrypto-3-x64.pdb \
	bin/libssl-3-x64.pdb \
	lib/engines-3/capi.pdb \
	lib/engines-3/padlock.pdb \
	lib/engines-3/loader_attic.pdb \
	lib/ossl-modules/legacy.pdb \
	bin/openssl.pdb

cat <<EOF >$R/$P-doc/setup.hint
sdesc: "OpenSSL Cryptography (Documentation)"
ldesc: "OpenSSL Cryptography (Documentation)"
category: Libs
requires: $P
external-source: $P
maintainer: $MAINTAINER
EOF

tar -C install -cjf $R/$P-doc/$P-doc-$V-$B.tar.bz2 \
	--xform s,^html,apps/$P/html, \
	html

cp ../$P-$V/LICENSE.txt $R/$P-doc/$P-doc-$V-$B.txt

endlog
