"""Single logging entry point: route everything through loguru.

Pipecat logs exclusively via loguru, so the whole app standardizes on it too.
`setup_logging()` configures one console sink + one rotating file sink, and
installs an `InterceptHandler` so stdlib `logging` records (uvicorn, the Google
API client, aiohttp, …) are funnelled into the same loguru stream — one format,
one level, one place to configure.
"""

from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path

from loguru import logger

from config import settings

# session_id is bound per-call via `logger.contextualize` (see bot.run_bot);
# "-" is the default shown for lines emitted outside a voice session.
CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<magenta>{extra[session_id]}</magenta> | "
    "<level>{message}</level>"
)
FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[session_id]} | "
    "{name}:{function}:{line} - {message}"
)

# stdlib loggers that install their own handlers and so need explicit rerouting.
_STDLIB_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "aiohttp.access")

# Third-party loggers that flood the stream — aioice emits one INFO line per ICE
# candidate pair (hundreds per connection), and the WebRTC / low-level HTTP /
# websocket stack becomes overwhelming at DEBUG. Clamp them to WARNING at the
# source so they never bury the app's own logs, regardless of LOG_LEVEL.
_NOISY_LOGGERS = (
    "aioice",
    "aiortc",
    "websockets",
    "httpcore",
    "urllib3",
    "googleapiclient",
)


class InterceptHandler(logging.Handler):
    """Redirect stdlib `logging` records into loguru (the canonical recipe)."""

    def emit(self, record: logging.LogRecord) -> None:
        # Map the stdlib level to loguru's equivalent name when one exists.
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Walk back to the frame that issued the log so loguru reports the real
        # caller (name:function:line) instead of this handler.
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging() -> None:
    """Configure loguru sinks and reroute stdlib logging. Call once at startup."""
    level = settings.log_level.upper()

    logger.remove()  # drop pipecat/loguru's default stderr sink
    # Default session_id for any line not emitted inside a bound call context.
    logger.configure(extra={"session_id": "-"})

    logger.add(sys.stderr, level=level, format=CONSOLE_FORMAT, enqueue=True)

    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_path,
        level=level,
        format=FILE_FORMAT,
        rotation=settings.log_rotation,
        retention=settings.log_retention,
        enqueue=True,       # safe across threads/async tasks
        backtrace=True,
        diagnose=False,     # don't leak local variables into log files
    )

    # Funnel the stdlib root + libraries' own loggers into loguru.
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in _STDLIB_LOGGERS:
        std_logger = logging.getLogger(name)
        std_logger.handlers = [InterceptHandler()]
        std_logger.propagate = False

    # Silence the flood-prone libraries even when the app runs at DEBUG.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
