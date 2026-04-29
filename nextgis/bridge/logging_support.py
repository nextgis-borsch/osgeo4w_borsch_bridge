from __future__ import annotations

import argparse
import logging
import sys

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

from .runtime import set_runtime_progress_enabled

try:
    from tqdm import tqdm as tqdm_module  # type: ignore[import-not-found]
except ImportError:
    tqdm_module = None


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
            output_stream = (
                sys.stderr if record.levelno >= logging.ERROR else sys.stdout
            )
            if LOGGING_SETTINGS.progress_enabled and tqdm_module is not None:
                tqdm_module.write(message, file=output_stream)
            else:
                output_stream.write(message + self.terminator)
                self.flush()
        except Exception:
            self.handleError(record)


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

    logger_names = (
        LOGGER_NAMESPACE,
        f"{LOGGER_NAMESPACE}.pipeline",
        f"{LOGGER_NAMESPACE}.runtime",
        f"{LOGGER_NAMESPACE}.process",
    )
    for logger_name in logger_names:
        current_logger = logging.getLogger(logger_name)
        current_logger.setLevel(log_level)
        current_logger.propagate = logger_name != LOGGER_NAMESPACE

    for logger_name in logger_names:
        logging.getLogger(logger_name).disabled = LOGGING_SETTINGS.quiet


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