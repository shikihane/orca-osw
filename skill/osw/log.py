from __future__ import annotations

import logging
import sys

_configured = False


def setup_logging(verbose: bool = False) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    level = logging.DEBUG if verbose else logging.INFO

    try:
        from rich.logging import RichHandler

        handler = RichHandler(
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            tracebacks_show_locals=verbose,
            markup=True,
        )
    except ImportError:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    logging.basicConfig(level=level, handlers=[handler], force=True)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"osw.{name}")
