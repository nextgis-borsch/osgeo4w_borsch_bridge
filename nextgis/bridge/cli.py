from __future__ import annotations

import argparse
import subprocess

from pathlib import Path
from typing import Optional, Sequence

from .logging_support import LOGGER, configure_logging
from .pipeline import (
    bootstrap_command,
    build_command,
    changes_command,
    clean_command,
    package_command,
    qtifw_command,
    snapshot_command,
)
from .runtime import DEFAULT_CYGWIN_MIRROR, set_active_proxy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NextGIS bridge orchestration for OSGeo4W, repka and QtIFW."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("nextgis/config/repositories.json"),
        help="Path to NextGIS bridge configuration.",
    )
    parser.add_argument(
        "--cygwin-root",
        type=Path,
        help="Explicit Cygwin root used for bash, git and related tools on Windows.",
    )
    parser.add_argument(
        "--cygwin-mirror",
        default=DEFAULT_CYGWIN_MIRROR,
        help="Cygwin mirror used when bootstrap installs a local runtime.",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="Proxy URL used for network requests and Cygwin bootstrap.",
    )
    parser.add_argument(
        "--cygwin-cache",
        type=Path,
        help="Directory used as the local Cygwin package cache.",
    )
    parser.add_argument(
        "--cygwin-setup",
        type=Path,
        help="Path to a cached setup-x86_64.exe executable.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress wrapper logs and child process output.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Logging level for wrapper diagnostics.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable tqdm progress bars even when tqdm is available.",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=int,
        default=60,
        help="Emit a heartbeat message if a child process stays silent for N seconds.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Install a local Cygwin runtime for NextGIS bridge commands.",
    )
    bootstrap_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Redownload setup-x86_64.exe before running the installer.",
    )
    bootstrap_parser.set_defaults(handler=bootstrap_command)

    snapshot_parser = subparsers.add_parser("snapshot", help="Write build snapshot.")
    snapshot_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("nextgis/state/build-manifest.json"),
        help="Snapshot file path.",
    )
    snapshot_parser.set_defaults(handler=snapshot_command)

    changes_parser = subparsers.add_parser(
        "changes",
        help="List changed packages since a git tag.",
    )
    changes_parser.add_argument("-t", "--tag", required=True, help="Git tag name.")
    changes_parser.add_argument(
        "-f",
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format.",
    )
    changes_parser.set_defaults(handler=changes_command)

    clean_parser = subparsers.add_parser(
        "clean",
        help="Remove untracked build outputs from the repository.",
    )
    clean_parser.add_argument(
        "-s",
        "--src",
        action="store_true",
        help="Clean untracked files under src/.",
    )
    clean_parser.add_argument(
        "-a",
        "--artifacts",
        action="store_true",
        help="Clean tmp/, x86_64/ and nextgis/artifacts/.",
    )
    clean_parser.add_argument(
        "-f",
        "--full",
        action="store_true",
        help="Clean untracked files across the entire repository.",
    )
    clean_parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would be removed without deleting it.",
    )
    clean_parser.set_defaults(handler=clean_command)

    package_parser = subparsers.add_parser(
        "package",
        help="Convert OSGeo4W artifacts into repka-compatible packages.",
    )
    package_parser.add_argument(
        "packages",
        nargs="*",
        help="Source package names or binary package names.",
    )
    package_parser.add_argument(
        "-r",
        "--release-root",
        type=Path,
        default=Path("x86_64/release"),
        help="OSGeo4W release root.",
    )
    package_parser.add_argument(
        "-a",
        "--artifacts-root",
        type=Path,
        default=Path("nextgis/artifacts"),
        help="Output root for repka-compatible repositories.",
    )
    package_parser.add_argument(
        "-c",
        "--compiler-tag",
        default="",
        help="Explicit compiler tag used in archive names.",
    )
    package_parser.set_defaults(handler=package_command)

    qtifw_parser = subparsers.add_parser(
        "qtifw",
        help="Generate QtIFW package metadata overlay.",
    )
    qtifw_parser.add_argument(
        "packages",
        nargs="*",
        help="Source package names or binary package names.",
    )
    qtifw_parser.add_argument(
        "-r",
        "--release-root",
        type=Path,
        default=Path("x86_64/release"),
        help="OSGeo4W release root used for setup.hint and license metadata.",
    )
    qtifw_parser.add_argument(
        "-a",
        "--artifacts-root",
        type=Path,
        default=Path("nextgis/artifacts"),
        help="Repka-compatible artifacts root.",
    )
    qtifw_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("nextgis/qtifw"),
        help="Output directory for generated QtIFW overlay.",
    )
    qtifw_parser.set_defaults(handler=qtifw_command)

    build_parser_obj = subparsers.add_parser(
        "build",
        help="Build packages and optionally package them for repka or MSI.",
    )
    build_parser_obj.add_argument(
        "packages",
        nargs="*",
        help="Source package names or binary package names.",
    )
    build_parser_obj.add_argument(
        "-d",
        "--disable",
        action="append",
        default=[],
        help="Disable a source recipe or binary package from the build graph.",
    )
    build_parser_obj.add_argument(
        "-t",
        "--changed-since-tag",
        default="",
        help="Build only packages changed since the provided tag.",
    )
    build_parser_obj.add_argument(
        "--build-reverse-dependencies",
        action="store_true",
        help="Build reverse dependencies of selected packages.",
    )
    build_parser_obj.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue the build if a package fails.",
    )
    build_parser_obj.add_argument(
        "-r",
        "--release-root",
        type=Path,
        default=Path("x86_64/release"),
        help="OSGeo4W release root.",
    )
    build_parser_obj.add_argument(
        "-a",
        "--artifacts-root",
        type=Path,
        default=Path("nextgis/artifacts"),
        help="Output root for repka-compatible repositories.",
    )
    build_parser_obj.add_argument(
        "-q",
        "--qtifw-output",
        type=Path,
        help="Output directory for generated QtIFW overlay.",
    )
    build_parser_obj.add_argument(
        "-s",
        "--snapshot-output",
        type=Path,
        help="Write a build snapshot after the build.",
    )
    build_parser_obj.add_argument(
        "-p",
        "--packaging-backend",
        choices=["none", "borsch", "msi", "both"],
        default="borsch",
        help="Packaging backend selection.",
    )
    build_parser_obj.add_argument(
        "-k",
        "--repka-root",
        default="",
        help="Local path or HTTP root with repka-compatible artifacts.",
    )
    build_parser_obj.add_argument(
        "--ignore-repka",
        action="store_true",
        help="Ignore repka artifacts even if repka root is provided.",
    )
    build_parser_obj.add_argument(
        "--allow-osgeo4w-deps",
        action="store_true",
        help=(
            "Allow unresolved external dependencies to be fetched from "
            "OSGeo4W via osgeo4w-setup.exe."
        ),
    )
    build_parser_obj.add_argument(
        "-c",
        "--compiler-tag",
        default="",
        help="Explicit compiler tag used in archive names.",
    )
    build_parser_obj.add_argument(
        "--osgeo4w-repo",
        type=Path,
        help="Explicit local OSGeo4W repository path.",
    )
    build_parser_obj.add_argument(
        "-m",
        "--msi-mirror",
        default="",
        help="Mirror path or URL passed to scripts/msis.sh.",
    )
    build_parser_obj.set_defaults(handler=build_command)
    return parser


def validate_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    if args.command == "clean" and not any(
        [args.src, args.artifacts, args.full]
    ):
        parser.error("clean requires at least one scope: -s, -a or -f")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    set_active_proxy(args.proxy)
    configure_logging(args)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        LOGGER.error("Operation cancelled by user")
        return 130
    except subprocess.CalledProcessError as error:
        command_text = "unknown command"
        if error.cmd:
            command_text = " ".join(str(part) for part in error.cmd)
        LOGGER.error(
            f"Command failed with exit code {error.returncode}: {command_text}"
        )
        return error.returncode or 1
    except Exception as error:
        LOGGER.error(str(error))
        return 1
