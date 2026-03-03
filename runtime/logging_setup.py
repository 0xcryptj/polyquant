"""
Structured logging via structlog.

Development:  coloured pretty-print to stdout
Production:   newline-delimited JSON (--log-json) — ship to Loki / Datadog / CloudWatch

Also writes to artifacts/logs/runtime.log when the artifacts dir is writable.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

# Project root: runtime/logging_setup.py -> runtime/ -> project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_LOG = _PROJECT_ROOT / "artifacts" / "logs" / "runtime.log"


def setup_logging(level: str = "INFO", json_output: bool = False) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Silence noisy third-party libraries
    for noisy in (
        "httpx", "httpcore", "ccxt", "telegram", "apscheduler",
        "hpack", "uvicorn.access", "uvicorn.error", "websockets",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Bridge stdlib logging into structlog so third-party libs are captured too
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    # Add file handler for artifacts/logs/runtime.log (best-effort)
    try:
        _RUNTIME_LOG.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(_RUNTIME_LOG, encoding="utf-8")
        fh.setLevel(log_level)
        fh.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(fh)
    except OSError:
        pass

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        handlers=handlers,
        force=True,
    )
