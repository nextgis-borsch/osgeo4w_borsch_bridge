from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence


DEFAULT_CYGWIN_DIRNAME = "cygwin"
DEFAULT_BOOTSTRAP_DIRNAME = ".nextgis-bootstrap"
DEFAULT_CYGWIN_MIRROR = "http://cygwin.mirror.constant.com"
DEFAULT_CYGWIN_PACKAGES = (
    "bison,flex,poppler,doxygen,git,unzip,tar,diffutils,patch,curl,wget,"
    "flip,p7zip,make,osslsigncode,mingw64-x86_64-gcc-core,catdoc,enscript,"
    "mingw64-x86_64-binutils,perl-Data-UUID,ruby=2.6.4-1,perl-YAML-Tiny"
)
DEFAULT_SETUP_URL = "https://cygwin.com/setup-x86_64.exe"


@dataclass
class BootstrapOptions:
    mirror: str = DEFAULT_CYGWIN_MIRROR
    package_cache: Optional[Path] = None
    setup_executable: Optional[Path] = None
    force: bool = False


@dataclass
class CommandRuntime:
    root_dir: Path
    cygwin_root: Optional[Path] = None
    bash_executable: str = "bash"
    git_executable: str = "git"
    cygpath_executable: Optional[str] = None
    windows_sysdir: str = ""

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
        return command_name

    def build_environment(
        self,
        extra_environment: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        environment = os.environ.copy()
        home_dir = environment.get("HOME") or str(Path.home())
        Path(home_dir).mkdir(parents=True, exist_ok=True)
        environment["HOME"] = home_dir
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


def is_cygwin_root(path: Path) -> bool:
    resolved_path = path.resolve()
    return (
        (resolved_path / "bin" / "bash.exe").exists()
        and (resolved_path / "bin" / "git.exe").exists()
        and (resolved_path / "bin" / "cygpath.exe").exists()
    )


def detect_adjacent_cygwin(root_dir: Path) -> Optional[Path]:
    candidate = default_cygwin_root(root_dir)
    if is_cygwin_root(candidate):
        return candidate.resolve()
    return None


def detect_cygwin_from_path() -> Optional[Path]:
    for executable_name in ("bash.exe", "bash"):
        executable_path = shutil.which(executable_name)
        if executable_path is None:
            continue
        candidate = Path(executable_path).resolve().parent.parent
        if is_cygwin_root(candidate):
            return candidate
    return None


def query_windows_sysdir(cygpath_executable: Path) -> str:
    result = subprocess.run(
        [str(cygpath_executable), "-w", "--sysdir"],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def build_cygwin_runtime(root_dir: Path, cygwin_root: Path) -> CommandRuntime:
    cygpath_executable = cygwin_root / "bin" / "cygpath.exe"
    windows_sysdir = query_windows_sysdir(cygpath_executable)
    return CommandRuntime(
        root_dir=root_dir,
        cygwin_root=cygwin_root,
        bash_executable=str(cygwin_root / "bin" / "bash.exe"),
        git_executable=str(cygwin_root / "bin" / "git.exe"),
        cygpath_executable=str(cygpath_executable),
        windows_sysdir=windows_sysdir,
    )


def native_runtime(root_dir: Path) -> CommandRuntime:
    return CommandRuntime(root_dir=root_dir)


def download_file(url: str, destination: Path, force: bool) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        return destination
    with urllib.request.urlopen(url) as response:
        destination.write_bytes(response.read())
    return destination


def bootstrap_cygwin(
    root_dir: Path,
    cygwin_root: Path,
    options: BootstrapOptions,
) -> Path:
    if is_cygwin_root(cygwin_root) and not options.force:
        return cygwin_root.resolve()

    bootstrap_dir = default_bootstrap_dir(root_dir)
    setup_executable = options.setup_executable or (
        bootstrap_dir / "setup-x86_64.exe"
    )
    package_cache = options.package_cache or (bootstrap_dir / "cache")
    setup_executable = download_file(
        url=DEFAULT_SETUP_URL,
        destination=setup_executable,
        force=options.force,
    )
    package_cache.mkdir(parents=True, exist_ok=True)
    cygwin_root.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
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
        ],
        check=True,
    )
    if not is_cygwin_root(cygwin_root):
        raise RuntimeError(
            f"Cygwin bootstrap did not produce a valid installation at {cygwin_root}"
        )
    return cygwin_root.resolve()


def resolve_runtime(
    root_dir: Path,
    explicit_cygwin_root: Optional[Path] = None,
    bootstrap_if_missing: bool = False,
    bootstrap_options: Optional[BootstrapOptions] = None,
) -> CommandRuntime:
    if not is_windows_host():
        return native_runtime(root_dir)

    if explicit_cygwin_root is not None:
        resolved_root = explicit_cygwin_root.resolve()
        if not is_cygwin_root(resolved_root):
            raise FileNotFoundError(
                f"Explicit Cygwin root is not valid: {resolved_root}"
            )
        return build_cygwin_runtime(root_dir, resolved_root)

    detected_root = detect_adjacent_cygwin(root_dir)
    if detected_root is None:
        detected_root = detect_cygwin_from_path()
    if detected_root is not None:
        return build_cygwin_runtime(root_dir, detected_root)

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
    return build_cygwin_runtime(root_dir, installed_root)


def remap_command(
    args: Sequence[str],
    runtime: Optional[CommandRuntime],
) -> Sequence[str]:
    if not args or runtime is None:
        return args
    remapped_args = list(args)
    remapped_args[0] = runtime.resolve_command(remapped_args[0])
    return remapped_args