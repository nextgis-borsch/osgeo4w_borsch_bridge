#!/bin/bash

set -e
set -o pipefail

export PATH=/bin:/usr/bin

: ${OSGEO4W_VERBOSE:=1}
: ${OSGEO4W_QUIET:=0}

log() {
	echo "$(date +"%Y-%m-%d %H:%M:%S"): $*"
}

if [ -z "$OSGEO4W_REP" ]; then
	b=$(git branch --show-current)
	case $b in
	master)
		export OSGEO4W_REP=$PWD
		cd $OSGEO4W_REP
		;;

	*)
		export OSGEO4W_REP=$TEMP/repo-$b
		export OSGEO4W_SKIP_UPLOAD=1
		mkdir -p "$OSGEO4W_REP"
		;;
	esac
fi

: ${OSGEO4W_SKIP_UPLOAD:=1}
: ${OSGEO4W_SKIP_CLEAN:=1}
: ${OSGEO4W_BUILD_RDEPS:=1}
: ${OSGEO4W_CONTINUE_BUILD:=0}
: ${OSGEO4W_SKIP_MASTER_REPO:=0}

export OSGEO4W_REP OSGEO4W_SKIP_UPLOAD

build() {
	bash package.sh
}

[ -f .buildenv ] && source .buildenv

if [ "$TX_TOKEN" = "none" ]; then TX_TOKEN=; fi
if [ "$OSGEO4W_SKIP_UPLOAD" = "0" ]; then OSGEO4W_SKIP_UPLOAD=; fi
if [ "$OSGEO4W_SKIP_CLEAN" = "0" ]; then OSGEO4W_SKIP_CLEAN=; fi
if [ "$OSGEO4W_BUILD_RDEPS" = "0" ]; then OSGEO4W_BUILD_RDEPS=; fi
if [ "$OSGEO4W_CONTINUE_BUILD" = "0" ]; then OSGEO4W_CONTINUE_BUILD=; fi
if [ "$OSGEO4W_SKIP_MASTER_REPO" = "0" ]; then OSGEO4W_SKIP_MASTER_REPO=; fi

# build in order
log "Resolving build order for arguments: $*"
PKGS=$(perl scripts/build-inorder.pl "$@" | paste -d" " -s)

if [ -z "$OSGEO4W_BUILD_RDEPS" ]; then
	# but only requested packages
	PKGS=$(fgrep -xf <(tr ' ' '\n' <<<"$@") <(tr ' ' '\n' <<<"$PKGS"))
fi

PKG_COUNT=$(tr ' ' '\n' <<<"$PKGS" | sed '/^$/d' | wc -l)

log "Repository root: $OSGEO4W_REP"
log "Working directory: $PWD"
log "Build order resolved: $PKGS"
log "Build count: $PKG_COUNT"

[ -z "$OSGEO4W_SKIP_UPLOAD" ] || log "Not uploading"
[ -n "$OSGEO4W_BUILD_RDEPS" ] || log "Not building reverse dependencies"
[ -z "$OSGEO4W_SKIP_CLEAN" ] || log "Skipping clean phases"
[ -z "$OSGEO4W_CONTINUE_BUILD" ] || log "Continuing on build failures"
[ -z "$OSGEO4W_SKIP_MASTER_REPO" ] || log "Skipping master repository dependencies"

ok=1
P=$PWD
built=
current_index=0
for i in $PKGS; do
	d=${i#-}
	current_index=$(( current_index + 1 ))

	if [ -f $P/tmp/$d.done ]; then
		log "[$current_index/$PKG_COUNT] $d ALREADY DONE"
		continue
	fi

	cd src/$d/osgeo4w

	log "[$current_index/$PKG_COUNT] $d BUILDING"

	mkdir -p $P/tmp
	if build; then
		log "[$current_index/$PKG_COUNT] $d SUCCEEDED"
		built="${built} $d"
		touch $P/tmp/$i.done
	else
		r=$?
		log "[$current_index/$PKG_COUNT] $d FAILED WITH $r"
		[ "$OSGEO4W_CONTINUE_BUILD" ] || { touch $P/tmp/$i.fail; exit 1; }
		ok=0
	fi

	cd ../../..
done

[ -z "$GITHUB_ENV" ] || echo "BUILT_PKGS=${built# }" >>$GITHUB_ENV

[ -n "$built" ] && log "Built packages: ${built# }"
[ "$ok" ] && log "Build pipeline finished"

[ "$ok" ] || exit 1
