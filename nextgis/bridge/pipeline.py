from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Dict, IO, Iterable, List, Optional, Sequence, Set, Tuple

from .config import BridgeConfiguration, QtIfwComponent
from .runtime import (
    DEFAULT_CYGWIN_MIRROR,
    BootstrapOptions,
    bootstrap_cygwin,
    default_cygwin_root,
    get_active_runtime,
    open_url,
    is_windows_host,
    remap_command,
    resolve_runtime,
    set_active_runtime,
    set_active_proxy,
    set_runtime_progress_enabled,
)

try:
    from tqdm import tqdm as tqdm_module  # type: ignore[import-not-found]
except ImportError:
    tqdm_module = None


ASSIGNMENT_RE = re.compile(r"^export\s+(?P<name>[A-Z_]+)=(?P<value>.+)$")
DEFAULT_ASSIGNMENT_RE = re.compile(
	r"^:\s+\$\{(?P<name>[A-Z_]+):=(?P<value>.*)\}$"
)
CL_VERSION_RE = re.compile(r"Version\s+(?P<major>\d+)\.(?P<minor>\d+)")
PACKAGE_VERSION_RE = re.compile(
    r"^(?P<version>.+)-(?P<binary>\d+|next|tbd)$"
)
COMPILER_BANNER_RE = re.compile(r"Compiler banner:\s*(?P<banner>.+)")


@dataclass
class LoggingSettings:
    quiet: bool = False
    progress_enabled: bool = False
    heartbeat_seconds: int = 60


LOGGING_SETTINGS = LoggingSettings()
LOGGER_NAMESPACE = "osgeo4w_borsch_bridge"
LOGGER = logging.getLogger(f"{LOGGER_NAMESPACE}.pipeline")
PROCESS_LOGGER = logging.getLogger(f"{LOGGER_NAMESPACE}.process")
SHELL_LOG_PREFIX = "\x1eOSGEO4W_BRIDGE_LOG\x1f"


class TqdmLoggingHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            output_stream = sys.stderr if record.levelno >= logging.ERROR else sys.stdout
            if LOGGING_SETTINGS.progress_enabled and tqdm_module is not None:
                tqdm_module.write(message, file=output_stream)
            else:
                stream = output_stream
                stream.write(message + self.terminator)
                self.flush()
        except Exception:
            self.handleError(record)


@dataclass
class SourceRecipe:
    name: str
    package_script: Path
    package_names: List[str]
    build_depends: List[str]

    @property
    def osgeo4w_dir(self) -> Path:
        return self.package_script.parent


@dataclass
class BuildSnapshot:
    config_digest: str
    packages: Dict[str, Dict[str, object]]


class WorkspaceModel:
    def __init__(
        self,
        root_dir: Path,
        recipes: Dict[str, SourceRecipe],
    ) -> None:
        self.root_dir = root_dir
        self.recipes = recipes
        self.package_to_source = self._build_package_index()
        self.forward_dependencies = self._build_forward_dependencies()
        self.reverse_dependencies = self._build_reverse_dependencies()

    @classmethod
    def discover(cls, root_dir: Path) -> "WorkspaceModel":
        recipes: Dict[str, SourceRecipe] = {}
        for package_script in sorted(root_dir.glob("src/*/osgeo4w/package.sh")):
            recipe = parse_recipe(package_script)
            recipes[recipe.name] = recipe
        return cls(root_dir=root_dir, recipes=recipes)

    def resolve_source_name(self, name: str) -> str:
        if name in self.recipes:
            return name
        if name in self.package_to_source:
            return self.package_to_source[name]
        raise KeyError(f"Unknown package or source recipe: {name}")

    def source_names(self) -> List[str]:
        return sorted(self.recipes)

    def build_targets(
        self,
        names: Iterable[str],
        include_reverse_dependencies: bool,
        disabled: Set[str],
    ) -> List[str]:
        source_names = {self.resolve_source_name(name) for name in names}
        filtered_names = {name for name in source_names if name not in disabled}
        if not include_reverse_dependencies:
            return sorted(filtered_names)
        result = set(filtered_names)
        queue = list(filtered_names)
        while queue:
            current_name = queue.pop(0)
            for dependent_name in self.reverse_dependencies.get(
                current_name,
                set(),
            ):
                if dependent_name in disabled or dependent_name in result:
                    continue
                result.add(dependent_name)
                queue.append(dependent_name)
        return sorted(result)

    def bootstrap_sources(
        self,
        target_names: Iterable[str],
        disabled: Set[str],
    ) -> List[str]:
        source_names = {self.resolve_source_name(name) for name in target_names}
        result: Set[str] = set()
        queue = list(source_names)
        while queue:
            current_name = queue.pop(0)
            for dependency_name in self.forward_dependencies.get(
                current_name,
                set(),
            ):
                if dependency_name in disabled or dependency_name in source_names:
                    continue
                if dependency_name in result:
                    continue
                result.add(dependency_name)
                queue.append(dependency_name)
        return sorted(result)

    def snapshot(
        self,
        configuration: BridgeConfiguration,
    ) -> BuildSnapshot:
        package_data: Dict[str, Dict[str, object]] = {}
        config_digest = sha256_text(
            json.dumps(configuration.raw_data, sort_keys=True)
        )
        for source_name, recipe in self.recipes.items():
            payload = configuration.relevant_payload(source_name)
            digest = sha256_text(json.dumps(payload, sort_keys=True))
            for file_path in sorted(recipe.osgeo4w_dir.rglob("*")):
                if file_path.is_file():
                    digest = sha256_chain(digest, sha256_file(file_path))
            package_data[source_name] = {
                "digest": digest,
                "package_names": recipe.package_names,
                "build_depends": recipe.build_depends,
            }
        return BuildSnapshot(config_digest=config_digest, packages=package_data)

    def _build_package_index(self) -> Dict[str, str]:
        index: Dict[str, str] = {}
        for source_name, recipe in self.recipes.items():
            for package_name in recipe.package_names:
                index[package_name] = source_name
        return index

    def _build_forward_dependencies(self) -> Dict[str, Set[str]]:
        forward_dependencies: Dict[str, Set[str]] = {
            source_name: set() for source_name in self.recipes
        }
        for source_name, recipe in self.recipes.items():
            for package_name in recipe.build_depends:
                dependency_source = self.package_to_source.get(package_name)
                if dependency_source is None or dependency_source == source_name:
                    continue
                forward_dependencies[source_name].add(dependency_source)
        return forward_dependencies

    def _build_reverse_dependencies(self) -> Dict[str, Set[str]]:
        reverse_dependencies: Dict[str, Set[str]] = {
            source_name: set() for source_name in self.recipes
        }
        for source_name, dependencies in self.forward_dependencies.items():
            for dependency_name in dependencies:
                reverse_dependencies[dependency_name].add(source_name)
        return reverse_dependencies


def parse_recipe(package_script: Path) -> SourceRecipe:
    exports: Dict[str, str] = {}
    with package_script.open("r", encoding="utf-8") as file_handle:
        for line in file_handle:
            stripped_line = line.strip()
            match = ASSIGNMENT_RE.match(stripped_line)
            if match is None:
                match = DEFAULT_ASSIGNMENT_RE.match(stripped_line)
            if match is None:
                continue
            name = match.group("name")
            if name not in {"P", "PACKAGES", "BUILDDEPENDS"}:
                continue
            value = match.group("value").strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            if value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            value = expand_shell_variables(value, exports)
            exports[name] = value
    build_depends = split_export(exports.get("BUILDDEPENDS", ""))
    if build_depends == ["none"]:
        build_depends = []
    return SourceRecipe(
        name=package_script.parent.parent.name,
        package_script=package_script,
        package_names=split_export(exports.get("PACKAGES", "")),
        build_depends=build_depends,
    )


def split_export(value: str) -> List[str]:
    if not value:
        return []
    return [token for token in value.split() if token]


def expand_shell_variables(
    value: str,
    variables: Dict[str, str],
) -> str:
    for key, replacement in variables.items():
        value = value.replace(f"${{{key}}}", replacement)
        value = value.replace(f"${key}", replacement)
    return value


def sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_chain(first_value: str, second_value: str) -> str:
    return sha256_text(f"{first_value}:{second_value}")


def configure_logging(args: argparse.Namespace) -> None:
    log_level = getattr(logging, str(args.log_level).upper())
    LOGGING_SETTINGS.quiet = bool(args.quiet)
    LOGGING_SETTINGS.progress_enabled = bool(
        not args.quiet
        and not args.no_progress
        and tqdm_module is not None
        and sys.stdout.isatty()
    )
    LOGGING_SETTINGS.heartbeat_seconds = int(args.heartbeat_seconds)
    set_runtime_progress_enabled(not args.quiet and not args.no_progress)

    root_logger = logging.getLogger(LOGGER_NAMESPACE)
    root_logger.handlers.clear()
    root_logger.propagate = False
    root_logger.setLevel(log_level)

    handler = TqdmLoggingHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s: [%(name)s] %(levelname)s %(message)s")
    )
    root_logger.addHandler(handler)

    for logger_name in (
        LOGGER_NAMESPACE,
        f"{LOGGER_NAMESPACE}.pipeline",
        f"{LOGGER_NAMESPACE}.runtime",
        f"{LOGGER_NAMESPACE}.process",
    ):
        current_logger = logging.getLogger(logger_name)
        current_logger.setLevel(log_level)
        current_logger.propagate = logger_name != LOGGER_NAMESPACE

    if LOGGING_SETTINGS.quiet:
        for logger_name in (
            LOGGER_NAMESPACE,
            f"{LOGGER_NAMESPACE}.pipeline",
            f"{LOGGER_NAMESPACE}.runtime",
            f"{LOGGER_NAMESPACE}.process",
        ):
            logging.getLogger(logger_name).disabled = True
    else:
        for logger_name in (
            LOGGER_NAMESPACE,
            f"{LOGGER_NAMESPACE}.pipeline",
            f"{LOGGER_NAMESPACE}.runtime",
            f"{LOGGER_NAMESPACE}.process",
        ):
            logging.getLogger(logger_name).disabled = False


def format_command(args: Sequence[str]) -> str:
    return " ".join(str(argument) for argument in args)


def first_output_line(output_text: str) -> str:
    for line in output_text.splitlines():
        stripped_line = line.strip()
        if stripped_line:
            return stripped_line
    return "unknown"


def iter_with_progress(
    items: Sequence[str],
    description: str,
) -> Iterable[str]:
    if not LOGGING_SETTINGS.progress_enabled:
        return items
    if tqdm_module is None:
        return items
    return tqdm_module(
        items,
        desc=description,
        unit="pkg",
        dynamic_ncols=True,
    )


def parse_shell_log_frame(
    line: str,
) -> Optional[Tuple[str, int, str]]:
    if not line.startswith(SHELL_LOG_PREFIX):
        return None
    payload = line[len(SHELL_LOG_PREFIX):].rstrip("\r\n")
    parts = payload.split("\x1f", 2)
    if len(parts) != 3:
        return None
    logger_name, level_name, message = parts
    if not logger_name:
        return None
    level = getattr(logging, level_name.upper(), logging.INFO)
    return logger_name, int(level), message


def stream_subprocess_output(
    args: Sequence[str],
    cwd: Path,
    merged_environment: Dict[str, str],
    description: str,
) -> subprocess.CompletedProcess[str]:
    if LOGGING_SETTINGS.quiet:
        completed_process = subprocess.run(
            list(args),
            cwd=str(cwd),
            env=merged_environment,
            text=True,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return subprocess.CompletedProcess(
            completed_process.args,
            completed_process.returncode,
            stdout="",
            stderr="",
        )

    process = subprocess.Popen(
        list(args),
        cwd=str(cwd),
        env=merged_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )
    output_queue: Queue[Tuple[str, Optional[str]]] = Queue()
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []
    stderr_buffer: List[str] = []

    def flush_stderr_buffer() -> None:
        if not stderr_buffer:
            return
        message = "".join(stderr_buffer).rstrip("\r\n")
        stderr_buffer.clear()
        if message:
            PROCESS_LOGGER.error(message)

    def read_stream(stream_name: str, stream: Optional[IO[str]]) -> None:
        if stream is None:
            output_queue.put((stream_name, None))
            return
        for line in stream:
            output_queue.put((stream_name, line))
        output_queue.put((stream_name, None))

    stdout_thread = threading.Thread(
        target=read_stream,
        args=("stdout", process.stdout),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=read_stream,
        args=("stderr", process.stderr),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    heartbeat_deadline = time.monotonic() + LOGGING_SETTINGS.heartbeat_seconds
    completed_streams = set()

    while len(completed_streams) < 2:
        try:
            stream_name, line = output_queue.get(timeout=1.0)
        except Empty:
            if process.poll() is not None and output_queue.empty():
                flush_stderr_buffer()
                break
            if time.monotonic() >= heartbeat_deadline:
                LOGGER.info(f"Still running: {description}")
                heartbeat_deadline = (
                    time.monotonic() + LOGGING_SETTINGS.heartbeat_seconds
                )
            continue

        if line is None:
            if stream_name == "stderr":
                flush_stderr_buffer()
            completed_streams.add(stream_name)
            continue

        heartbeat_deadline = (
            time.monotonic() + LOGGING_SETTINGS.heartbeat_seconds
        )
        shell_log_frame = parse_shell_log_frame(line)
        if shell_log_frame is not None:
            flush_stderr_buffer()
            logger_name, level, message = shell_log_frame
            logging.getLogger(logger_name).log(level, message)
            continue
        if stream_name == "stdout":
            flush_stderr_buffer()
            stdout_lines.append(line)
            sys.stdout.write(line)
            sys.stdout.flush()
            continue

        stderr_lines.append(line)
        stderr_buffer.append(line)

    flush_stderr_buffer()
    stdout_thread.join(timeout=0)
    stderr_thread.join(timeout=0)

    return_code = process.wait()
    stdout_text = "".join(stdout_lines)
    stderr_text = "".join(stderr_lines)
    if return_code != 0:
        raise subprocess.CalledProcessError(
            return_code,
            list(args),
            output=stdout_text,
            stderr=stderr_text,
        )
    return subprocess.CompletedProcess(
        list(args),
        return_code,
        stdout=stdout_text,
        stderr=stderr_text,
    )


def run_command(
    args: Sequence[str],
    cwd: Path,
    environment: Optional[Dict[str, str]] = None,
    capture_output: bool = False,
    description: str = "",
) -> subprocess.CompletedProcess[str]:
    runtime = get_active_runtime()
    merged_environment = os.environ.copy()
    if runtime is not None:
        runtime.ensure_safe_directory(cwd)
        merged_environment = runtime.build_environment(environment)
        args = remap_command(args, runtime)
    elif environment:
        merged_environment.update(environment)

    command_text = format_command(args)
    if description:
        LOGGER.info(f"{description}: {command_text}")
    elif not capture_output:
        LOGGER.info(f"Running command: {command_text}")

    if capture_output:
        return subprocess.run(
            list(args),
            cwd=str(cwd),
            env=merged_environment,
            text=True,
            check=True,
            capture_output=True,
        )

    return stream_subprocess_output(
        list(args),
        cwd=cwd,
        merged_environment=merged_environment,
        description=description or command_text,
    )


def capture_optional_command(
    args: Sequence[str],
    cwd: Path,
    environment: Optional[Dict[str, str]] = None,
) -> str:
    runtime = get_active_runtime()
    merged_environment = os.environ.copy()
    command_args = list(args)
    if runtime is not None:
        merged_environment = runtime.build_environment(environment)
        command_args = list(remap_command(command_args, runtime))
    elif environment:
        merged_environment.update(environment)

    try:
        completed_process = subprocess.run(
            command_args,
            cwd=str(cwd),
            env=merged_environment,
            text=True,
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        return "not found"

    output_text = "\n".join(
        part for part in [completed_process.stdout, completed_process.stderr] if part
    )
    return first_output_line(output_text) if output_text else "unknown"


def log_build_environment_summary(
    root_dir: Path,
    args: argparse.Namespace,
) -> None:
    effective_release_root = resolve_effective_release_root(root_dir, args)
    runtime = get_active_runtime()
    runtime_name = "native"
    runtime_root = "PATH"
    if runtime is not None and runtime.uses_cygwin and runtime.cygwin_root is not None:
        runtime_name = "cygwin"
        runtime_root = str(runtime.cygwin_root)

    LOGGER.info(f"Host OS: {platform.platform()}")
    LOGGER.info(f"Host Python: {platform.python_version()}")
    LOGGER.info(f"Execution runtime: {runtime_name} ({runtime_root})")
    LOGGER.info(
        "Progress bars: "
        f"{'enabled' if LOGGING_SETTINGS.progress_enabled else 'disabled'}"
    )
    LOGGER.info(
        "Paths: "
        f"release={effective_release_root}, "
        f"artifacts={args.artifacts_root.resolve()}"
    )

    bash_version = capture_optional_command(["bash", "--version"], root_dir)
    git_version = capture_optional_command(["git", "--version"], root_dir)
    LOGGER.info(f"Bash: {bash_version}")
    LOGGER.info(f"Git: {git_version}")

    if runtime is not None and runtime.uses_cygwin:
        cygwin_os = capture_optional_command(
            ["bash", "-lc", "uname -srvmo"],
            root_dir,
        )
        cygpath_version = capture_optional_command(
            ["cygpath", "--version"],
            root_dir,
        )
        LOGGER.info(f"Cygwin OS: {cygwin_os}")
        LOGGER.info(f"Cygwin tools: {cygpath_version}")

    compiler_banner = capture_optional_command(["cl"], root_dir)
    if compiler_banner in {"unknown", "not found"}:
        LOGGER.info(
            "Compiler: not yet available in the wrapper environment; "
            "recipe-level compiler details will be logged after vsenv()."
        )
    else:
        LOGGER.info(f"Compiler: {compiler_banner}")


def detect_compiler_tag() -> str:
    return detect_compiler_tag_for_root(None)


def detect_compiler_tag_for_root(root_dir: Optional[Path]) -> str:
    explicit_value = os.environ.get("NEXTGIS_COMPILER_TAG")
    if explicit_value:
        return explicit_value
    detected_tag = detect_compiler_tag_from_command(["cl"])
    if detected_tag is not None:
        return detected_tag
    if root_dir is not None:
        detected_tag = detect_compiler_tag_from_package_logs(root_dir)
        if detected_tag is not None:
            return detected_tag
    for compiler_path in iter_compiler_candidates(root_dir):
        detected_tag = detect_compiler_tag_from_command([str(compiler_path)])
        if detected_tag is not None:
            return detected_tag
    return "unknown-compiler"


def detect_compiler_tag_from_command(command: Sequence[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            list(command),
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    output = f"{result.stdout}\n{result.stderr}"
    return parse_compiler_tag(output)


def detect_compiler_tag_from_package_logs(root_dir: Path) -> Optional[str]:
    log_paths = sorted(
        root_dir.glob("src/*/osgeo4w/package.log*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for log_path in log_paths:
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        matches = COMPILER_BANNER_RE.findall(log_text)
        for banner in reversed(matches):
            detected_tag = parse_compiler_tag(banner)
            if detected_tag is not None:
                return detected_tag
    return None


def parse_compiler_tag(output: str) -> Optional[str]:
    match = CL_VERSION_RE.search(output)
    if match is None:
        return None
    normalized_output = output.lower()
    if "for x64" in normalized_output or "hostx64" in normalized_output:
        suffix = "64bit"
    elif "for x86" in normalized_output or "hostx86" in normalized_output:
        suffix = "32bit"
    else:
        suffix = (
            "64bit"
            if os.environ.get("Platform", "x64").lower() == "x64"
            else "32bit"
        )
    return f"MSVC-{match.group('major')}.{match.group('minor')}-{suffix}"


def iter_compiler_candidates(root_dir: Optional[Path]) -> Iterable[Path]:
    candidates: List[Path] = []
    seen_paths: Set[Path] = set()

    def append_candidate(path: Path) -> None:
        if path in seen_paths or not path.exists():
            return
        seen_paths.add(path)
        candidates.append(path)

    vctools_install_dir = os.environ.get("VCToolsInstallDir")
    if vctools_install_dir:
        for suffix in (
            Path("bin/HostX64/x64/cl.exe"),
            Path("bin/HostX86/x86/cl.exe"),
        ):
            append_candidate(Path(vctools_install_dir) / suffix)

    compiler_roots: List[Path] = []
    if root_dir is not None:
        compiler_roots.extend(
            [
                root_dir / "scripts" / "vs2022" / "VC",
                root_dir / "scripts" / "vs2019" / "VC",
            ]
        )

    for base_dir in (
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
    ):
        if not base_dir:
            continue
        for year in ("2022", "2019"):
            for edition in (
                "Community",
                "Professional",
                "Enterprise",
                "BuildTools",
            ):
                compiler_roots.append(
                    Path(base_dir)
                    / "Microsoft Visual Studio"
                    / year
                    / edition
                    / "VC"
                )

    for compiler_root in compiler_roots:
        for compiler_path in sorted(
            compiler_root.glob("Tools/MSVC/*/bin/HostX64/x64/cl.exe"),
            reverse=True,
        ):
            append_candidate(compiler_path)
        for compiler_path in sorted(
            compiler_root.glob("Tools/MSVC/*/bin/HostX86/x86/cl.exe"),
            reverse=True,
        ):
            append_candidate(compiler_path)

    return candidates


def default_osgeo4w_repo(root_dir: Path) -> Path:
    current_branch = capture_optional_command(
        ["git", "branch", "--show-current"],
        root_dir,
    )
    if current_branch == "master":
        return root_dir
    temp_root = Path(os.environ.get("TEMP", tempfile.gettempdir()))
    return temp_root / f"repo-{current_branch}"


def resolve_osgeo4w_repo(root_dir: Path, args: argparse.Namespace) -> Path:
    if getattr(args, "osgeo4w_repo", None) is not None:
        return args.osgeo4w_repo.resolve()
    configured_repo = os.environ.get("OSGEO4W_REP")
    if configured_repo:
        return Path(configured_repo).resolve()
    resolved_release_root = (
        args.release_root.resolve()
        if args.release_root.is_absolute()
        else (root_dir / args.release_root).resolve()
    )
    if resolved_release_root.parts[-2:] == ("x86_64", "release"):
        return resolved_release_root.parent.parent
    return default_osgeo4w_repo(root_dir)


def resolve_effective_release_root(
    root_dir: Path,
    args: argparse.Namespace,
) -> Path:
    if args.release_root.is_absolute():
        return args.release_root.resolve()
    if getattr(args, "osgeo4w_repo", None) is not None:
        return (args.osgeo4w_repo.resolve() / args.release_root).resolve()
    configured_repo = os.environ.get("OSGEO4W_REP")
    if configured_repo:
        return (Path(configured_repo).resolve() / args.release_root).resolve()
    return (root_dir / args.release_root).resolve()


def parse_archive_version(package_name: str, archive_name: str) -> str:
    prefix = f"{package_name}-"
    if archive_name.endswith(".tar.bz2"):
        archive_name = archive_name[:-8]
    if not archive_name.startswith(prefix):
        raise ValueError(f"Unexpected archive name: {archive_name}")
    remainder = archive_name[len(prefix):]
    match = PACKAGE_VERSION_RE.match(remainder)
    if match is None:
        return remainder
    return match.group("version")


def locate_archive_base(repo_root: Path) -> str:
    version_file = repo_root / "build" / "version.str"
    lines = version_file.read_text(encoding="utf-8").splitlines()
    if len(lines) < 3:
        raise ValueError(f"Malformed version.str in {version_file}")
    return lines[2].strip()


def write_version_file(repo_root: Path, version: str, archive_base: str) -> None:
    build_dir = repo_root / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    version_file = build_dir / "version.str"
    version_file.write_text(
        f"{version}\n{timestamp}\n{archive_base}",
        encoding="utf-8",
    )


def extract_tarball(archive_path: Path, destination_dir: Path) -> None:
    with tarfile.open(archive_path, "r:bz2") as archive_handle:
        archive_handle.extractall(path=destination_dir)


def copy_directory_contents(source_dir: Path, destination_dir: Path) -> None:
    for file_path in sorted(source_dir.rglob("*")):
        if file_path.is_dir():
            continue
        relative_path = file_path.relative_to(source_dir)
        target_path = destination_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target_path)


def should_skip_merge_path(relative_path: Path) -> bool:
    parts = relative_path.parts
    if len(parts) >= 2 and parts[0] == "etc" and parts[1] in {
        "setup",
        "postinstall",
        "preremove",
    }:
        return True
    if parts and parts[0] in {"usr", "var"}:
        return True
    return False


def stage_osgeo4w_merge(install_root: Path, stage_root: Path) -> None:
    for file_path in sorted(install_root.rglob("*")):
        if file_path.is_dir():
            continue
        relative_path = file_path.relative_to(install_root)
        if should_skip_merge_path(relative_path):
            continue
        target_path = stage_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target_path)


def locate_python_root(install_root: Path, configured_root: str) -> Path:
    apps_dir = install_root / "apps"
    if apps_dir.exists():
        python_directories = sorted(apps_dir.glob("Python*"))
        if python_directories:
            return python_directories[0]
    return install_root / configured_root


def stage_python_site_package(
    install_root: Path,
    stage_root: Path,
    python_root: str,
) -> None:
    source_python_root = locate_python_root(install_root, python_root)
    site_packages_dir = source_python_root / "Lib" / "site-packages"
    scripts_dir = source_python_root / "Scripts"
    if site_packages_dir.exists():
        copy_directory_contents(
            site_packages_dir,
            stage_root / "Lib" / "site-packages",
        )
    if scripts_dir.exists():
        copy_directory_contents(scripts_dir, stage_root / "Scripts")


def create_zip_archive(
    stage_root: Path,
    zip_path: Path,
    archive_base: str,
) -> None:
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as zip_handle:
        for file_path in sorted(stage_root.rglob("*")):
            if file_path.is_dir():
                continue
            relative_path = file_path.relative_to(stage_root)
            archive_path = Path(archive_base) / relative_path
            zip_handle.write(file_path, archive_path.as_posix())


def package_source_recipe(
    recipe: SourceRecipe,
    configuration: BridgeConfiguration,
    release_root: Path,
    artifacts_root: Path,
    compiler_tag: str,
) -> Path:
    descriptor = configuration.describe(recipe.name, recipe.package_names)
    archives = find_release_archives(recipe, release_root)
    primary_package = recipe.package_names[0]
    version = parse_archive_version(
        primary_package,
        archives[primary_package].name,
    )
    archive_base = f"{descriptor.packet_name}-{version}-{compiler_tag}"
    repo_root = artifacts_root / descriptor.repo_name
    if repo_root.exists():
        shutil.rmtree(repo_root)
    repo_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{recipe.name}-stage-") as temp_name:
        temporary_dir = Path(temp_name)
        install_root = temporary_dir / "install"
        stage_root = temporary_dir / "stage"
        install_root.mkdir(parents=True, exist_ok=True)
        stage_root.mkdir(parents=True, exist_ok=True)
        for archive_path in archives.values():
            extract_tarball(archive_path, install_root)
        if descriptor.packaging_class == "python-site-package":
            stage_python_site_package(
                install_root=install_root,
                stage_root=stage_root,
                python_root=configuration.python_root,
            )
        else:
            stage_osgeo4w_merge(install_root=install_root, stage_root=stage_root)
        build_dir = repo_root / "build"
        build_dir.mkdir(parents=True, exist_ok=True)
        create_zip_archive(
            stage_root=stage_root,
            zip_path=build_dir / f"{archive_base}.zip",
            archive_base=archive_base,
        )
        copy_directory_contents(stage_root, repo_root)
    write_version_file(
        repo_root=repo_root,
        version=version,
        archive_base=archive_base,
    )
    write_repository_metadata(
        repo_root=repo_root,
        recipe=recipe,
        repo_name=descriptor.repo_name,
        packet_name=descriptor.packet_name,
        packaging_class=descriptor.packaging_class,
        version=version,
        archive_base=archive_base,
    )
    return repo_root


def write_repository_metadata(
    repo_root: Path,
    recipe: SourceRecipe,
    repo_name: str,
    packet_name: str,
    packaging_class: str,
    version: str,
    archive_base: str,
) -> None:
    metadata_path = repo_root / "build" / "metadata.json"
    metadata = {
        "source_name": recipe.name,
        "package_names": recipe.package_names,
        "repo_name": repo_name,
        "packet_name": packet_name,
        "packaging_class": packaging_class,
        "version": version,
        "archive_base": archive_base,
    }
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def find_release_archives(
    recipe: SourceRecipe,
    release_root: Path,
) -> Dict[str, Path]:
    archives: Dict[str, Path] = {}
    for package_name in recipe.package_names:
        candidates = [
            path
            for path in release_root.glob(f"**/{package_name}-*.tar.bz2")
            if not path.name.endswith("-src.tar.bz2")
        ]
        exact_candidates = [
            path for path in candidates if path.parent.name == package_name
        ]
        if exact_candidates:
            candidates = exact_candidates
        if not candidates:
            raise FileNotFoundError(
                f"No release archive found for {package_name} under {release_root}"
            )
        candidates.sort(key=lambda path: path.stat().st_mtime)
        archives[package_name] = candidates[-1]
    return archives


def load_snapshot_from_tag(
    root_dir: Path,
    tag_name: str,
) -> Optional[BuildSnapshot]:
    try:
        result = run_command(
            ["git", "show", f"{tag_name}:nextgis/state/build-manifest.json"],
            cwd=root_dir,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        return None
    payload = json.loads(result.stdout)
    return BuildSnapshot(
        config_digest=str(payload["config_digest"]),
        packages=dict(payload["packages"]),
    )


def write_snapshot_file(snapshot: BuildSnapshot, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "config_digest": snapshot.config_digest,
                "packages": snapshot.packages,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def changed_packages_since_tag(
    root_dir: Path,
    workspace: WorkspaceModel,
    configuration: BridgeConfiguration,
    tag_name: str,
) -> List[str]:
    current_snapshot = workspace.snapshot(configuration)
    previous_snapshot = load_snapshot_from_tag(root_dir, tag_name)
    if previous_snapshot is None:
        return changed_packages_from_git_diff(root_dir, workspace, tag_name)
    changed_names: List[str] = []
    for source_name in workspace.source_names():
        current_payload = current_snapshot.packages[source_name]
        previous_payload = previous_snapshot.packages.get(source_name)
        if previous_payload is None:
            changed_names.append(source_name)
            continue
        if current_payload["digest"] != previous_payload.get("digest"):
            changed_names.append(source_name)
    return changed_names


def changed_packages_from_git_diff(
    root_dir: Path,
    workspace: WorkspaceModel,
    tag_name: str,
) -> List[str]:
    result = run_command(
        ["git", "diff", "--name-only", f"{tag_name}..HEAD"],
        cwd=root_dir,
        capture_output=True,
    )
    changed_files = [
        line.strip() for line in result.stdout.splitlines() if line.strip()
    ]
    changed_names: Set[str] = set()
    for file_name in changed_files:
        path = Path(file_name)
        if len(path.parts) >= 4 and path.parts[0] == "src" and path.parts[2] == "osgeo4w":
            changed_names.add(path.parts[1])
            continue
        if path.parts[:2] == ("nextgis", "config"):
            return workspace.source_names()
    return sorted(changed_names)


def open_zip_reader(
    artifacts_root: str,
    repo_name: str,
) -> Tuple[zipfile.ZipFile, io.BytesIO]:
    if re.match(r"^https?://", artifacts_root):
        version_url = f"{artifacts_root.rstrip('/')}/{repo_name}/build/version.str"
        with open_url(version_url) as response:
            version_lines = response.read().decode("utf-8").splitlines()
        archive_base = version_lines[2].strip()
        archive_url = (
            f"{artifacts_root.rstrip('/')}/{repo_name}/build/{archive_base}.zip"
        )
        with open_url(archive_url) as response:
            payload = io.BytesIO(response.read())
        return zipfile.ZipFile(payload), payload
    repo_root = Path(artifacts_root) / repo_name
    archive_base = locate_archive_base(repo_root)
    archive_path = repo_root / "build" / f"{archive_base}.zip"
    return zipfile.ZipFile(archive_path), io.BytesIO()


def hydrate_source_recipe(
    recipe: SourceRecipe,
    configuration: BridgeConfiguration,
    artifacts_root: str,
    install_root: Path,
) -> None:
    descriptor = configuration.describe(recipe.name, recipe.package_names)
    zip_handle, _payload = open_zip_reader(artifacts_root, descriptor.repo_name)
    with zip_handle:
        names = [
            name for name in zip_handle.namelist() if name and not name.endswith("/")
        ]
        top_level_prefix = ""
        if names:
            top_level_prefix = names[0].split("/", 1)[0]
        for member_name in names:
            relative_name = member_name
            if top_level_prefix and member_name.startswith(f"{top_level_prefix}/"):
                relative_name = member_name[len(top_level_prefix) + 1 :]
            if not relative_name:
                continue
            target_path = map_hydrated_path(
                packaging_class=descriptor.packaging_class,
                configuration=configuration,
                install_root=install_root,
                relative_name=relative_name,
            )
            if target_path is None:
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zip_handle.open(member_name) as source_handle:
                with target_path.open("wb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle)
    write_hydration_markers(install_root=install_root, recipe=recipe)


def map_hydrated_path(
    packaging_class: str,
    configuration: BridgeConfiguration,
    install_root: Path,
    relative_name: str,
) -> Optional[Path]:
    relative_path = Path(relative_name)
    if packaging_class == "python-site-package":
        python_root = locate_python_root(install_root, configuration.python_root)
        if relative_name.startswith("Lib/site-packages/"):
            suffix = relative_path.relative_to(Path("Lib/site-packages"))
            return python_root / "Lib" / "site-packages" / suffix
        if relative_name.startswith("Scripts/"):
            suffix = relative_path.relative_to(Path("Scripts"))
            return python_root / "Scripts" / suffix
        return None
    return install_root / relative_path


def write_hydration_markers(install_root: Path, recipe: SourceRecipe) -> None:
    setup_dir = install_root / "etc" / "setup"
    setup_dir.mkdir(parents=True, exist_ok=True)
    for package_name in recipe.package_names:
        marker_path = setup_dir / f"{package_name}.lst.gz"
        with gzip.open(marker_path, "wt", encoding="utf-8") as file_handle:
            file_handle.write(f"hydrated:{recipe.name}\n")


def generate_qtifw_overlay(
    workspace: WorkspaceModel,
    configuration: BridgeConfiguration,
    artifacts_root: Path,
    output_root: Path,
    selected_names: Iterable[str],
) -> None:
    if output_root.exists():
        shutil.rmtree(output_root)
    packages_root = output_root / "packages"
    packages_root.mkdir(parents=True, exist_ok=True)
    selected_name_list = sorted(selected_names)
    for source_name in iter_with_progress(selected_name_list, "qtifw"):
        recipe = workspace.recipes[source_name]
        descriptor = configuration.describe(source_name, recipe.package_names)
        repo_root = artifacts_root / descriptor.repo_name
        version_text, release_date = read_version_file(repo_root)
        for component in descriptor.qtifw_components:
            write_qtifw_component(
                packages_root=packages_root,
                component=component,
                repo_name=descriptor.repo_name,
                version_text=version_text,
                release_date=release_date,
            )


def read_version_file(repo_root: Path) -> Tuple[str, str]:
    version_file = repo_root / "build" / "version.str"
    lines = version_file.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise ValueError(f"Malformed version file: {version_file}")
    release_date = lines[1].split(" ", 1)[0]
    return lines[0], release_date


def write_qtifw_component(
    packages_root: Path,
    component: QtIfwComponent,
    repo_name: str,
    version_text: str,
    release_date: str,
) -> None:
    component_dir = packages_root / component.component_id
    meta_dir = component_dir / "meta"
    data_dir = component_dir / "data"
    meta_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "package.xml").write_text(
        build_meta_xml(
            component=component,
            version_text=version_text,
            release_date=release_date,
        ),
        encoding="utf-8",
    )
    (data_dir / "package.xml").write_text(
        build_data_xml(component=component, repo_name=repo_name),
        encoding="utf-8",
    )


def build_meta_xml(
    component: QtIfwComponent,
    version_text: str,
    release_date: str,
) -> str:
    lines = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
        "<Package>",
        f"    <DisplayName>{xml_escape(component.display_name)}</DisplayName>",
        f"    <Description>{xml_escape(component.description)}</Description>",
        f"    <Name>{xml_escape(component.component_id)}</Name>",
        f"    <Version>{xml_escape(version_text)}</Version>",
        f"    <ReleaseDate>{xml_escape(release_date)}</ReleaseDate>",
    ]
    if component.default:
        lines.append("    <Default>true</Default>")
    if component.dependencies:
        dependencies = ",".join(component.dependencies)
        lines.append(
            f"    <Dependencies>{xml_escape(dependencies)}</Dependencies>"
        )
    if component.replaces:
        replaces = ",".join(component.replaces)
        lines.append(f"    <Replaces>{xml_escape(replaces)}</Replaces>")
    if component.update_text:
        lines.append(
            f"    <UpdateText>{xml_escape(component.update_text)}</UpdateText>"
        )
    lines.append("</Package>")
    return "\n".join(lines) + "\n"


def build_data_xml(component: QtIfwComponent, repo_name: str) -> str:
    lines = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
        f"<Package root=\"{xml_escape(repo_name)}\">",
        "    <win>",
    ]
    for copy_rule in component.copy_rules:
        lines.append(
            "        <path "
            f"src=\"{xml_escape(copy_rule.source)}\" "
            f"dst=\"{xml_escape(copy_rule.destination)}\"/>"
        )
    lines.extend(["    </win>", "</Package>"])
    return "\n".join(lines) + "\n"


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_path_for_runtime(path: Path) -> str:
    runtime = get_active_runtime()
    resolved_path = path.resolve()
    if (
        runtime is None
        or not runtime.uses_cygwin
        or runtime.cygpath_executable is None
    ):
        return str(resolved_path)
    try:
        result = subprocess.run(
            [runtime.cygpath_executable, "-u", str(resolved_path)],
            text=True,
            capture_output=True,
            check=True,
            env=runtime.build_environment(),
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return str(resolved_path)
    converted_path = result.stdout.strip()
    return converted_path or str(resolved_path)


def build_environment(args: argparse.Namespace) -> Dict[str, str]:
    root_dir = args.root.resolve()
    environment: Dict[str, str] = {}
    environment["OSGEO4W_BRIDGE_LOG_PROTOCOL"] = "1"
    if not args.build_reverse_dependencies:
        environment["OSGEO4W_BUILD_RDEPS"] = "0"
    if args.continue_on_error:
        environment["OSGEO4W_CONTINUE_BUILD"] = "1"
    environment["OSGEO4W_REP"] = format_path_for_runtime(
        resolve_osgeo4w_repo(root_dir, args)
    )
    if args.quiet:
        environment["OSGEO4W_QUIET"] = "1"
    return environment


def ensure_osgeo4w_root(root_dir: Path) -> Path:
    osgeo4w_root = root_dir / "osgeo4w"
    osgeo4w_root.mkdir(parents=True, exist_ok=True)
    return osgeo4w_root


def build_bootstrap_options(args: argparse.Namespace) -> BootstrapOptions:
    return BootstrapOptions(
        mirror=args.cygwin_mirror,
        package_cache=args.cygwin_cache.resolve()
        if args.cygwin_cache is not None
        else None,
        setup_executable=args.cygwin_setup.resolve()
        if args.cygwin_setup is not None
        else None,
        proxy=args.proxy,
        force=getattr(args, "force", False),
    )


def activate_runtime(
    args: argparse.Namespace,
    bootstrap_if_missing: bool,
) -> None:
    runtime = resolve_runtime(
        root_dir=args.root.resolve(),
        explicit_cygwin_root=args.cygwin_root.resolve()
        if args.cygwin_root is not None
        else None,
        bootstrap_if_missing=bootstrap_if_missing,
        bootstrap_options=build_bootstrap_options(args),
    )
    set_active_runtime(runtime)
    runtime_name = "native"
    runtime_root = "PATH"
    if runtime.uses_cygwin and runtime.cygwin_root is not None:
        runtime_name = "cygwin"
        runtime_root = str(runtime.cygwin_root)
    LOGGER.info(
        f"Runtime={runtime_name}, root={runtime_root}, workspace={args.root.resolve()}"
    )


def bootstrap_command(args: argparse.Namespace) -> int:
    root_dir = args.root.resolve()
    if not is_windows_host():
        LOGGER.info("Cygwin bootstrap is only required on Windows hosts.")
        return 0
    set_active_runtime(None)
    target_root = (
        args.cygwin_root.resolve()
        if args.cygwin_root is not None
        else default_cygwin_root(root_dir)
    )
    LOGGER.info(f"Bootstrapping Cygwin into {target_root}")
    installed_root = bootstrap_cygwin(
        root_dir=root_dir,
        cygwin_root=target_root,
        options=build_bootstrap_options(args),
    )
    LOGGER.info(f"Cygwin runtime is ready at {installed_root}")
    print(installed_root)
    return 0


def build_command(args: argparse.Namespace) -> int:
    activate_runtime(args, bootstrap_if_missing=True)
    root_dir = args.root.resolve()
    effective_release_root = resolve_effective_release_root(root_dir, args)
    log_build_environment_summary(root_dir, args)
    configuration = BridgeConfiguration.load(args.config.resolve())
    workspace = WorkspaceModel.discover(root_dir)
    disabled = {workspace.resolve_source_name(name) for name in args.disable}
    requested_names = resolve_requested_packages(
        args=args,
        root_dir=root_dir,
        workspace=workspace,
        configuration=configuration,
    )
    build_targets = workspace.build_targets(
        names=requested_names,
        include_reverse_dependencies=args.build_reverse_dependencies,
        disabled=disabled,
    )
    LOGGER.info(
        f"Build plan contains {len(build_targets)} packages; backend={args.packaging_backend}"
    )
    if build_targets:
        LOGGER.info(f"Build targets: {' '.join(build_targets)}")
    environment = build_environment(args)
    if args.repka_root and not args.ignore_repka:
        bootstrap_names = workspace.bootstrap_sources(
            target_names=build_targets,
            disabled=disabled,
        )
        osgeo4w_root = ensure_osgeo4w_root(root_dir)
        LOGGER.info(
            f"Hydrating {len(bootstrap_names)} dependency repositories into {osgeo4w_root}"
        )
        for source_name in iter_with_progress(
            bootstrap_names,
            "hydrate",
        ):
            hydrate_source_recipe(
                recipe=workspace.recipes[source_name],
                configuration=configuration,
                artifacts_root=args.repka_root,
                install_root=osgeo4w_root,
            )
        environment["OSGEO4W_SKIP_UPDATE"] = "1"
        environment["OSGEO4W_SKIP_MASTER_REPO"] = "1"
    build_arguments = build_targets + [f"{name}-" for name in sorted(disabled)]
    if build_arguments:
        run_command(
            ["bash", "scripts/build.sh", *build_arguments],
            cwd=root_dir,
            environment=environment,
            description="Executing OSGeo4W build pipeline",
        )
    if args.snapshot_output:
        LOGGER.info(f"Writing snapshot to {args.snapshot_output.resolve()}")
        snapshot = workspace.snapshot(configuration)
        write_snapshot_file(snapshot, args.snapshot_output.resolve())
    if args.packaging_backend in {"borsch", "both"}:
        compiler_tag = args.compiler_tag or detect_compiler_tag_for_root(root_dir)
        LOGGER.info(f"Packaging repka artifacts with compiler tag {compiler_tag}")
        package_selected_sources(
            workspace=workspace,
            configuration=configuration,
            release_root=effective_release_root,
            artifacts_root=args.artifacts_root.resolve(),
            source_names=build_targets,
            compiler_tag=compiler_tag,
        )
        if args.qtifw_output:
            LOGGER.info(
                f"Generating QtIFW overlay into {args.qtifw_output.resolve()}"
            )
            generate_qtifw_overlay(
                workspace=workspace,
                configuration=configuration,
                artifacts_root=args.artifacts_root.resolve(),
                output_root=args.qtifw_output.resolve(),
                selected_names=build_targets,
            )
    if args.packaging_backend in {"msi", "both"}:
        package_msi(
            root_dir=root_dir,
            workspace=workspace,
            selected_names=build_targets,
            mirror=args.msi_mirror,
        )
    return 0


def package_selected_sources(
    workspace: WorkspaceModel,
    configuration: BridgeConfiguration,
    release_root: Path,
    artifacts_root: Path,
    source_names: Iterable[str],
    compiler_tag: str,
) -> None:
    source_name_list = sorted(source_names)
    for source_name in iter_with_progress(source_name_list, "package"):
        recipe = workspace.recipes[source_name]
        LOGGER.info(f"Packaging source recipe {source_name}")
        package_source_recipe(
            recipe=recipe,
            configuration=configuration,
            release_root=release_root,
            artifacts_root=artifacts_root,
            compiler_tag=compiler_tag,
        )


def package_msi(
    root_dir: Path,
    workspace: WorkspaceModel,
    selected_names: Iterable[str],
    mirror: str,
) -> None:
    msi_sources = []
    for source_name in selected_names:
        recipe = workspace.recipes[source_name]
        if (
            f"{source_name}-full" in recipe.package_names
            and f"{source_name}-full-grids" in recipe.package_names
        ):
            msi_sources.append(source_name)
    if not msi_sources:
        return
    environment = os.environ.copy()
    environment["PKGS"] = " ".join(sorted(msi_sources))
    if mirror:
        environment["mirror"] = mirror
    LOGGER.info(f"Building MSI packages for: {' '.join(sorted(msi_sources))}")
    run_command(
        ["bash", "scripts/msis.sh"],
        cwd=root_dir,
        environment=environment,
        description="Executing MSI packaging pipeline",
    )


def resolve_requested_packages(
    args: argparse.Namespace,
    root_dir: Path,
    workspace: WorkspaceModel,
    configuration: BridgeConfiguration,
) -> List[str]:
    if args.changed_since_tag:
        changed_names = changed_packages_since_tag(
            root_dir=root_dir,
            workspace=workspace,
            configuration=configuration,
            tag_name=args.changed_since_tag,
        )
        if args.packages:
            requested = {
                workspace.resolve_source_name(name) for name in args.packages
            }
            return sorted(requested.intersection(changed_names))
        return changed_names
    if args.packages:
        return sorted({workspace.resolve_source_name(name) for name in args.packages})
    return workspace.source_names()


def snapshot_command(args: argparse.Namespace) -> int:
    root_dir = args.root.resolve()
    configuration = BridgeConfiguration.load(args.config.resolve())
    workspace = WorkspaceModel.discover(root_dir)
    LOGGER.info(
        f"Generating snapshot for {len(workspace.recipes)} source recipes"
    )
    snapshot = workspace.snapshot(configuration)
    write_snapshot_file(snapshot, args.output.resolve())
    LOGGER.info(f"Snapshot written to {args.output.resolve()}")
    return 0


def changes_command(args: argparse.Namespace) -> int:
    activate_runtime(args, bootstrap_if_missing=True)
    root_dir = args.root.resolve()
    configuration = BridgeConfiguration.load(args.config.resolve())
    workspace = WorkspaceModel.discover(root_dir)
    changed_names = changed_packages_since_tag(
        root_dir=root_dir,
        workspace=workspace,
        configuration=configuration,
        tag_name=args.tag,
    )
    LOGGER.info(
        f"Detected {len(changed_names)} changed packages since tag {args.tag}"
    )
    if args.output_format == "json":
        print(json.dumps(changed_names, indent=2))
    else:
        for source_name in changed_names:
            print(source_name)
    return 0


def package_command(args: argparse.Namespace) -> int:
    root_dir = args.root.resolve()
    configuration = BridgeConfiguration.load(args.config.resolve())
    workspace = WorkspaceModel.discover(root_dir)
    if args.packages:
        source_names = sorted(
            {workspace.resolve_source_name(name) for name in args.packages}
        )
    else:
        source_names = workspace.source_names()
    compiler_tag = args.compiler_tag or detect_compiler_tag_for_root(root_dir)
    LOGGER.info(
        f"Packaging {len(source_names)} repositories from {args.release_root.resolve()}"
    )
    package_selected_sources(
        workspace=workspace,
        configuration=configuration,
        release_root=args.release_root.resolve(),
        artifacts_root=args.artifacts_root.resolve(),
        source_names=source_names,
        compiler_tag=compiler_tag,
    )
    return 0


def qtifw_command(args: argparse.Namespace) -> int:
    root_dir = args.root.resolve()
    configuration = BridgeConfiguration.load(args.config.resolve())
    workspace = WorkspaceModel.discover(root_dir)
    if args.packages:
        source_names = {
            workspace.resolve_source_name(name) for name in args.packages
        }
    else:
        source_names = {
            recipe.name
            for recipe in workspace.recipes.values()
            if (
                args.artifacts_root
                / configuration.describe(recipe.name, recipe.package_names).repo_name
            ).exists()
        }
    LOGGER.info(
        f"Generating QtIFW metadata for {len(source_names)} repositories"
    )
    generate_qtifw_overlay(
        workspace=workspace,
        configuration=configuration,
        artifacts_root=args.artifacts_root.resolve(),
        output_root=args.output.resolve(),
        selected_names=sorted(source_names),
    )
    return 0


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
        "--force",
        action="store_true",
        help="Redownload setup-x86_64.exe before running the installer.",
    )
    bootstrap_parser.set_defaults(handler=bootstrap_command)

    snapshot_parser = subparsers.add_parser("snapshot", help="Write build snapshot.")
    snapshot_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Snapshot file path.",
    )
    snapshot_parser.set_defaults(handler=snapshot_command)

    changes_parser = subparsers.add_parser(
        "changes",
        help="List changed packages since a git tag.",
    )
    changes_parser.add_argument("--tag", required=True, help="Git tag name.")
    changes_parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Output format.",
    )
    changes_parser.set_defaults(handler=changes_command)

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
        "--release-root",
        type=Path,
        default=Path("x86_64/release"),
        help="OSGeo4W release root.",
    )
    package_parser.add_argument(
        "--artifacts-root",
        type=Path,
        required=True,
        help="Output root for repka-compatible repositories.",
    )
    package_parser.add_argument(
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
        "--artifacts-root",
        type=Path,
        required=True,
        help="Repka-compatible artifacts root.",
    )
    qtifw_parser.add_argument(
        "--output",
        type=Path,
        required=True,
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
        "--disable",
        action="append",
        default=[],
        help="Disable a source recipe or binary package from the build graph.",
    )
    build_parser_obj.add_argument(
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
        "--release-root",
        type=Path,
        default=Path("x86_64/release"),
        help="OSGeo4W release root.",
    )
    build_parser_obj.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("nextgis/artifacts"),
        help="Output root for repka-compatible repositories.",
    )
    build_parser_obj.add_argument(
        "--qtifw-output",
        type=Path,
        help="Output directory for generated QtIFW overlay.",
    )
    build_parser_obj.add_argument(
        "--snapshot-output",
        type=Path,
        help="Write a build snapshot after the build.",
    )
    build_parser_obj.add_argument(
        "--packaging-backend",
        choices=["none", "borsch", "msi", "both"],
        default="borsch",
        help="Packaging backend selection.",
    )
    build_parser_obj.add_argument(
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
        "--msi-mirror",
        default="",
        help="Mirror path or URL passed to scripts/msis.sh.",
    )
    build_parser_obj.set_defaults(handler=build_command)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    set_active_proxy(args.proxy)
    configure_logging(args)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())