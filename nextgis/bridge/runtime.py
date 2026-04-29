from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import urllib.request

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


DEFAULT_CYGWIN_DIRNAME = "cygwin"
DEFAULT_BOOTSTRAP_DIRNAME = ".nextgis-bootstrap"
DEFAULT_CYGWIN_MIRROR = "https://mirrors.kernel.org/sourceware/cygwin/"
DEFAULT_CYGWIN_PACKAGES = (
    "bison,flex,poppler,doxygen,git,unzip,tar,diffutils,patch,curl,wget,"
    "flip,p7zip,make,osslsigncode,mingw64-x86_64-gcc-core,catdoc,enscript,"
    "mingw64-x86_64-binutils,perl-Data-UUID,ruby=2.6.4-1,perl-YAML-Tiny"
)
DEFAULT_SETUP_URL = "https://cygwin.com/setup-x86_64.exe"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
CYGCHECK_ROW_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<version>\S+)\s+(?P<status>.+)$"
)


LOGGER = logging.getLogger("osgeo4w_borsch_bridge.runtime")
RUNTIME_PROGRESS_ENABLED = True
ACTIVE_PROXY: Optional[str] = None


@dataclass
class BootstrapOptions:
    mirror: str = DEFAULT_CYGWIN_MIRROR
    package_cache: Optional[Path] = None
    setup_executable: Optional[Path] = None
    proxy: Optional[str] = None
    force: bool = False


@dataclass
class CommandRuntime:
    root_dir: Path
    cygwin_root: Optional[Path] = None
    bash_executable: str = "bash"
    git_executable: str = "git"
    cygpath_executable: Optional[str] = None
    cygcheck_executable: Optional[str] = None
    windows_sysdir: str = ""
    proxy: Optional[str] = None

    @property
    def uses_cygwin(self) -> bool:
        return self.cygwin_root is not None

    def resolve_command(self, command_name: str) -> str:
        if not self.uses_cygwin:
            return command_name
        if command_name == "bash":
            return self.bash_executable
        if command_name == "git":
            return self.git_executable
        if command_name == "cygpath" and self.cygpath_executable:
            return self.cygpath_executable
        if command_name == "cygcheck" and self.cygcheck_executable:
            return self.cygcheck_executable
        return command_name

    def build_environment(
        self,
        extra_environment: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        environment = os.environ.copy()
        home_dir = environment.get("HOME") or str(Path.home())
        Path(home_dir).mkdir(parents=True, exist_ok=True)
        environment["HOME"] = home_dir
        environment.update(build_proxy_environment(self.proxy))
        if self.uses_cygwin and self.cygwin_root is not None:
            preferred_paths = [
                str(self.cygwin_root / "bin"),
                str(self.cygwin_root / "usr" / "bin"),
            ]
            if self.windows_sysdir:
                preferred_paths.append(self.windows_sysdir)
            existing_paths = [
                item for item in environment.get("PATH", "").split(os.pathsep)
                if item
            ]
            filtered_paths = [
                item for item in existing_paths if item not in preferred_paths
            ]
            environment["PATH"] = os.pathsep.join(
                preferred_paths + filtered_paths
            )
        if extra_environment:
            environment.update(extra_environment)
        return environment

    def ensure_safe_directory(self, directory: Path) -> None:
        subprocess.run(
            [
                self.resolve_command("git"),
                "config",
                "--global",
                "--add",
                "safe.directory",
                str(directory),
            ],
            cwd=str(directory),
            env=self.build_environment(),
            text=True,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


ACTIVE_RUNTIME: Optional[CommandRuntime] = None


def set_runtime_progress_enabled(enabled: bool) -> None:
    global RUNTIME_PROGRESS_ENABLED
    RUNTIME_PROGRESS_ENABLED = enabled


def normalize_proxy(proxy: Optional[str]) -> Optional[str]:
    if proxy is None:
        return None
    normalized = proxy.strip()
    if not normalized:
        return None
    return normalized


def set_active_proxy(proxy: Optional[str]) -> None:
    global ACTIVE_PROXY
    ACTIVE_PROXY = normalize_proxy(proxy)


def get_active_proxy() -> Optional[str]:
    return ACTIVE_PROXY


def build_proxy_environment(proxy: Optional[str] = None) -> Dict[str, str]:
    effective_proxy = normalize_proxy(proxy)
    if effective_proxy is None:
        effective_proxy = get_active_proxy()
    if effective_proxy is None:
        return {}
    return {
        "ALL_PROXY": effective_proxy,
        "HTTP_PROXY": effective_proxy,
        "HTTPS_PROXY": effective_proxy,
        "all_proxy": effective_proxy,
        "http_proxy": effective_proxy,
        "https_proxy": effective_proxy,
    }


def open_url(
    url: str,
    proxy: Optional[str] = None,
):
    proxy_url = normalize_proxy(proxy)
    if proxy_url is None:
        proxy_url = get_active_proxy()
    if proxy_url is None:
        return urllib.request.urlopen(url)
    proxy_handler = urllib.request.ProxyHandler(
        {
            "http": proxy_url,
            "https": proxy_url,
        }
    )
    opener = urllib.request.build_opener(proxy_handler)
    return opener.open(url)


def set_active_runtime(runtime: Optional[CommandRuntime]) -> None:
    global ACTIVE_RUNTIME
    ACTIVE_RUNTIME = runtime


def get_active_runtime() -> Optional[CommandRuntime]:
    return ACTIVE_RUNTIME


def is_windows_host() -> bool:
    return os.name == "nt"


def default_cygwin_root(root_dir: Path) -> Path:
    return root_dir / DEFAULT_CYGWIN_DIRNAME


def default_bootstrap_dir(root_dir: Path) -> Path:
    return root_dir / DEFAULT_BOOTSTRAP_DIRNAME


def required_cygwin_packages() -> List[Tuple[str, Optional[str]]]:
    packages: List[Tuple[str, Optional[str]]] = []
    for package_spec in DEFAULT_CYGWIN_PACKAGES.split(","):
        stripped_spec = package_spec.strip()
        if not stripped_spec:
            continue
        package_name, separator, version = stripped_spec.partition("=")
        packages.append((package_name, version if separator else None))
    return packages


def build_cygwin_probe_environment(cygwin_root: Path) -> Dict[str, str]:
    environment = os.environ.copy()
    preferred_paths = [
        str(cygwin_root / "bin"),
        str(cygwin_root / "usr" / "bin"),
    ]
    existing_paths = [
        item for item in environment.get("PATH", "").split(os.pathsep) if item
    ]
    filtered_paths = [
        item for item in existing_paths if item not in preferred_paths
    ]
    environment["PATH"] = os.pathsep.join(preferred_paths + filtered_paths)
    return environment


def parse_cygcheck_output(output_text: str) -> Dict[str, Tuple[str, str]]:
    package_statuses: Dict[str, Tuple[str, str]] = {}
    for line in output_text.splitlines():
        stripped_line = line.strip()
        if not stripped_line:
            continue
        if stripped_line.startswith("Package"):
            continue
        match = CYGCHECK_ROW_RE.match(stripped_line)
        if match is None:
            continue
        package_statuses[match.group("name")] = (
            match.group("version"),
            match.group("status").strip(),
        )
    return package_statuses


def has_required_cygwin_packages(
    cygwin_root: Path,
    cygcheck_executable: Path,
) -> bool:
    required_packages = required_cygwin_packages()
    package_names = [package_name for package_name, _version in required_packages]
    result = subprocess.run(
        [str(cygcheck_executable), "-c", *package_names],
        text=True,
        capture_output=True,
        check=False,
        env=build_cygwin_probe_environment(cygwin_root),
    )
    if result.stderr.strip():
        LOGGER.debug(f"cygcheck stderr: {result.stderr.strip()}")
    package_statuses = parse_cygcheck_output(result.stdout)
    for package_name, expected_version in required_packages:
        package_data = package_statuses.get(package_name)
        if package_data is None:
            LOGGER.debug(
                f"Cygwin package is missing from cygcheck output: {package_name}"
            )
            return False
        installed_version, install_status = package_data
        if install_status.upper() != "OK":
            LOGGER.debug(
                "Cygwin package is not in OK state: "
                f"{package_name} ({installed_version}, {install_status})"
            )
            return False
        if expected_version is not None and installed_version != expected_version:
            LOGGER.debug(
                "Cygwin package version mismatch: "
                f"{package_name} expected {expected_version}, "
                f"got {installed_version}"
            )
            return False
    return result.returncode == 0


def is_cygwin_root(path: Path) -> bool:
    resolved_path = path.resolve()
    cygpath_executable = resolved_path / "bin" / "cygpath.exe"
    cygcheck_executable = resolved_path / "bin" / "cygcheck.exe"
    if not cygpath_executable.exists():
        LOGGER.debug(f"Cygwin root check failed, cygpath is missing: {resolved_path}")
        return False
    if not cygcheck_executable.exists():
        LOGGER.debug(
            f"Cygwin root check failed, cygcheck is missing: {resolved_path}"
        )
        return False
    return has_required_cygwin_packages(resolved_path, cygcheck_executable)


def detect_adjacent_cygwin(root_dir: Path) -> Optional[Path]:
    candidate = default_cygwin_root(root_dir)
    LOGGER.debug(f"Checking adjacent Cygwin runtime at {candidate}")
    if is_cygwin_root(candidate):
        LOGGER.info(f"Detected adjacent Cygwin runtime at {candidate.resolve()}")
        return candidate.resolve()
    return None


def detect_cygwin_root_from_launcher(executable_path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            [
                str(executable_path),
                "-c",
                "cygpath --windows /usr/bin",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        LOGGER.debug(
            f"Failed to resolve Cygwin root from launcher {executable_path}"
        )
        return None

    bin_path = Path(result.stdout.strip())
    candidate = bin_path.parent
    if is_cygwin_root(candidate):
        return candidate.resolve()
    return None


def detect_cygwin_from_path() -> Optional[Path]:
    for executable_name in ("bash.exe", "bash"):
        executable_path = shutil.which(executable_name)
        if executable_path is None:
            LOGGER.debug(f"PATH lookup failed for {executable_name}")
            continue
        candidate = Path(executable_path).resolve().parent.parent
        LOGGER.debug(
            f"Checking Cygwin runtime from PATH via {executable_name}: {candidate}"
        )
        if is_cygwin_root(candidate):
            LOGGER.info(
                f"Detected Cygwin runtime from PATH via {executable_name}: {candidate}"
            )
            return candidate

    for executable_name in ("cygwin.exe", "cygwin"):
        executable_path = shutil.which(executable_name)
        if executable_path is None:
            LOGGER.debug(f"PATH lookup failed for {executable_name}")
            continue
        LOGGER.debug(
            f"Checking Cygwin launcher from PATH: {executable_path}"
        )
        candidate = detect_cygwin_root_from_launcher(Path(executable_path))
        if candidate is not None:
            LOGGER.info(
                f"Detected Cygwin runtime from PATH via {executable_name}: {candidate}"
            )
            return candidate
    return None


def query_windows_sysdir(cygpath_executable: Path) -> str:
    cygwin_root = cygpath_executable.parent.parent
    result = subprocess.run(
        [str(cygpath_executable), "-w", "--sysdir"],
        text=True,
        capture_output=True,
        check=True,
        env=build_cygwin_probe_environment(cygwin_root),
    )
    return result.stdout.strip()


def build_cygwin_runtime(
    root_dir: Path,
    cygwin_root: Path,
    proxy: Optional[str] = None,
) -> CommandRuntime:
    cygpath_executable = cygwin_root / "bin" / "cygpath.exe"
    windows_sysdir = query_windows_sysdir(cygpath_executable)
    return CommandRuntime(
        root_dir=root_dir,
        cygwin_root=cygwin_root,
        bash_executable=str(cygwin_root / "bin" / "bash.exe"),
        git_executable=str(cygwin_root / "bin" / "git.exe"),
        cygpath_executable=str(cygpath_executable),
        cygcheck_executable=str(cygwin_root / "bin" / "cygcheck.exe"),
        windows_sysdir=windows_sysdir,
        proxy=normalize_proxy(proxy),
    )


def native_runtime(
    root_dir: Path,
    proxy: Optional[str] = None,
) -> CommandRuntime:
    return CommandRuntime(root_dir=root_dir, proxy=normalize_proxy(proxy))


def unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def download_file(
    url: str,
    destination: Path,
    force: bool,
    proxy: Optional[str] = None,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        LOGGER.info(f"Reusing downloaded file {destination}")
        return destination

    temp_destination = destination.with_suffix(destination.suffix + ".part")
    downloaded_size = 0
    last_reported_percent = -1
    last_report_time = 0.0
    LOGGER.info(f"Downloading {url} -> {destination}")

    try:
        with open_url(url, proxy=proxy) as response:
            total_size_text = response.headers.get("Content-Length", "")
            total_size = int(total_size_text) if total_size_text.isdigit() else 0
            with temp_destination.open("wb") as file_handle:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    file_handle.write(chunk)
                    downloaded_size += len(chunk)

                    if not RUNTIME_PROGRESS_ENABLED:
                        continue

                    current_time = time.monotonic()
                    if total_size > 0:
                        percent = int(downloaded_size * 100 / total_size)
                        if percent >= last_reported_percent + 5 or percent == 100:
                            LOGGER.info(
                                f"Download progress: {percent}% "
                                f"({downloaded_size}/{total_size} bytes)"
                            )
                            last_reported_percent = percent
                    elif current_time - last_report_time >= 2.0:
                        LOGGER.info(
                            "Download progress: "
                            f"{downloaded_size // (1024 * 1024)} MiB"
                        )
                        last_report_time = current_time
    except KeyboardInterrupt:
        LOGGER.info("Download cancelled by user")
        unlink_if_exists(temp_destination)
        raise
    except Exception:
        unlink_if_exists(temp_destination)
        raise

    temp_destination.replace(destination)
    LOGGER.info(f"Download completed: {destination}")
    return destination


def run_bootstrap_installer(
    command: Sequence[str],
    environment: Optional[Dict[str, str]] = None,
) -> None:
    LOGGER.info(
        f"Starting Cygwin installer: {' '.join(str(item) for item in command)}"
    )
    merged_environment = os.environ.copy()
    if environment:
        merged_environment.update(environment)
    process = subprocess.Popen(list(command), env=merged_environment)
    try:
        process.wait()
    except KeyboardInterrupt:
        LOGGER.info("Cygwin installation cancelled by user")
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, list(command))


def bootstrap_cygwin(
    root_dir: Path,
    cygwin_root: Path,
    options: BootstrapOptions,
) -> Path:
    if is_cygwin_root(cygwin_root) and not options.force:
        LOGGER.info(
            f"Reusing existing Cygwin installation at {cygwin_root.resolve()}"
        )
        return cygwin_root.resolve()

    bootstrap_dir = default_bootstrap_dir(root_dir)
    setup_executable = options.setup_executable or (
        bootstrap_dir / "setup-x86_64.exe"
    )
    package_cache = options.package_cache or (bootstrap_dir / "cache")
    LOGGER.info(f"Cygwin runtime was not found. Bootstrapping into {cygwin_root}")
    LOGGER.info(f"Cygwin mirror: {options.mirror}")
    LOGGER.info(f"Cygwin package cache: {package_cache}")
    if options.proxy:
        LOGGER.info("Cygwin proxy: configured")
    setup_executable = download_file(
        url=DEFAULT_SETUP_URL,
        destination=setup_executable,
        force=options.force,
        proxy=options.proxy,
    )
    package_cache.mkdir(parents=True, exist_ok=True)
    cygwin_root.parent.mkdir(parents=True, exist_ok=True)
    install_command = [
        str(setup_executable),
        "-qnNdOW",
        "-R",
        str(cygwin_root),
        "-s",
        options.mirror,
        "-l",
        str(package_cache),
        "-P",
        DEFAULT_CYGWIN_PACKAGES,
    ]
    if options.proxy:
        install_command.extend(["--proxy", options.proxy])
    run_bootstrap_installer(
        install_command,
        environment=build_proxy_environment(options.proxy),
    )
    if not is_cygwin_root(cygwin_root):
        raise RuntimeError(
            f"Cygwin bootstrap did not produce a valid installation at {cygwin_root}"
        )
    LOGGER.info(f"Cygwin installation completed: {cygwin_root.resolve()}")
    return cygwin_root.resolve()


def resolve_runtime(
    root_dir: Path,
    explicit_cygwin_root: Optional[Path] = None,
    bootstrap_if_missing: bool = False,
    bootstrap_options: Optional[BootstrapOptions] = None,
) -> CommandRuntime:
    proxy = bootstrap_options.proxy if bootstrap_options is not None else None
    if not is_windows_host():
        return native_runtime(root_dir, proxy=proxy)

    if explicit_cygwin_root is not None:
        resolved_root = explicit_cygwin_root.resolve()
        if not is_cygwin_root(resolved_root):
            raise FileNotFoundError(
                f"Explicit Cygwin root is not valid: {resolved_root}"
            )
        LOGGER.info(f"Using explicit Cygwin root: {resolved_root}")
        return build_cygwin_runtime(root_dir, resolved_root, proxy=proxy)

    detected_root = detect_adjacent_cygwin(root_dir)
    if detected_root is None:
        LOGGER.debug("Adjacent Cygwin runtime was not found")
        detected_root = detect_cygwin_from_path()
    if detected_root is not None:
        return build_cygwin_runtime(root_dir, detected_root, proxy=proxy)

    LOGGER.debug("Cygwin runtime was not found in PATH")

    if not bootstrap_if_missing:
        raise FileNotFoundError(
            "Cygwin was not found next to the repository and was not found in PATH. "
            "Run the bootstrap command first or pass --cygwin-root."
        )

    installed_root = bootstrap_cygwin(
        root_dir=root_dir,
        cygwin_root=default_cygwin_root(root_dir),
        options=bootstrap_options or BootstrapOptions(),
    )
    return build_cygwin_runtime(root_dir, installed_root, proxy=proxy)


def remap_command(
    args: Sequence[str],
    runtime: Optional[CommandRuntime],
) -> Sequence[str]:
    if not args or runtime is None:
        return args
    remapped_args = list(args)
    remapped_args[0] = runtime.resolve_command(remapped_args[0])
    return remapped_args