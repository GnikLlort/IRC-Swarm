"""Logging setup: rotating files + secret redaction.

Architecture
------------
* one rotating ``manager.log`` handler is attached to the **root** logger, so
  every module's records are captured there (proxy, irc_client, manager, ...);
* each bot additionally gets its own rotating ``botNNN.log`` handler that only
  carries that bot's records;
* a redaction filter on the root scrubs any registered secret (proxy password,
  IRC password) and inline ``user:pass@`` credentials before a line is written.

Security contract: secrets are only ever stored in private fields and are also
defensively scrubbed here so they can never appear in any log file.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from logging.handlers import RotatingFileHandler
from typing import Dict, Optional

_CRED_RE = re.compile(r"(?P<scheme>[a-z0-9]+://)(?P<user>[^:@/\s]+):(?P<pass>[^@/\s]+)@",
                      re.IGNORECASE)

_secret_lock = threading.Lock()
_secrets: set = set()


def register_secret(secret: Optional[str]) -> None:
    """Register a credential that must never appear in a log line."""
    if not secret:
        return
    with _secret_lock:
        _secrets.add(str(secret))


def redact(text: str) -> str:
    """Strip registered secrets and inline URL passwords from ``text``."""
    if not text:
        return text
    try:
        for s in list(_secrets):
            if s and s in text:
                text = text.replace(s, "****")
        text = _CRED_RE.sub(r"\g<scheme>\g<user>:****@", text)
    except Exception:  # pragma: no cover - logging must never fail
        return text
    return text


class _RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            clean = redact(record.getMessage())
            record.msg = clean
            record.args = None
        except Exception:  # pragma: no cover
            pass
        return True


def _fmt() -> logging.Formatter:
    return logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )


def _file_handler(log_dir: str, filename: str, level: int) -> RotatingFileHandler:
    path = os.path.join(log_dir, filename)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(_fmt())
    handler.setLevel(level)
    handler.addFilter(_RedactionFilter())
    return handler


class LoggingFacility:
    """Central point that owns all logging configuration for the process."""

    def __init__(self, log_dir: str = "logs", level: str = "INFO") -> None:
        self.log_dir = log_dir
        self.level = logging.getLevelName(level) if isinstance(level, str) else level
        self._bot_loggers: Dict[str, logging.Logger] = {}
        self._lock = threading.Lock()
        os.makedirs(log_dir, exist_ok=True)

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addFilter(_RedactionFilter())
        self._attach_root_handler()

    # ------------------------------------------------------------------ #
    def _attach_root_handler(self) -> None:
        # remove any previous manager handler we installed
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, RotatingFileHandler) and getattr(h, "_irmgr", False):
                try:
                    h.close()
                except Exception:
                    pass
                root.removeHandler(h)
        handler = _file_handler(self.log_dir, "manager.log", self.level)
        setattr(handler, "_irmgr", True)  # marker so reconfigure can find it
        root.addHandler(handler)

    def manager_logger(self, name: str = "manager") -> logging.Logger:
        # root handler already captures this; just return the named logger.
        return logging.getLogger(name)

    def console_logger(self, name: str = "console") -> logging.Logger:
        logger = logging.getLogger(name)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            logger.propagate = False
        return logger

    def bot_logger(self, bot_id: str) -> logging.Logger:
        with self._lock:
            if bot_id not in self._bot_loggers:
                logger = logging.getLogger(f"ircbot.{bot_id}")
                logger.setLevel(self.level)
                logger.addHandler(_file_handler(self.log_dir, f"{bot_id}.log",
                                                self.level))
                logger.propagate = True   # also lands in manager.log
                self._bot_loggers[bot_id] = logger
            return self._bot_loggers[bot_id]

    def reconfigure(self, log_dir: str, level: str) -> None:
        self.log_dir = log_dir
        self.level = logging.getLevelName(level) if isinstance(level, str) else level
        os.makedirs(log_dir, exist_ok=True)
        self._attach_root_handler()
        with self._lock:
            for logger in self._bot_loggers.values():
                for h in list(logger.handlers):
                    try:
                        h.close()
                    except Exception:
                        pass
                    logger.removeHandler(h)
                logger.addHandler(_file_handler(self.log_dir,
                                                f"{logger.name.rsplit('.',1)[-1]}.log",
                                                self.level))


_facility: Optional[LoggingFacility] = None


def setup(log_dir: str = "logs", level: str = "INFO") -> LoggingFacility:
    global _facility
    if _facility is None:
        _facility = LoggingFacility(log_dir=log_dir, level=level)
    else:
        _facility.reconfigure(log_dir, level)
    return _facility


def facility() -> LoggingFacility:
    if _facility is None:
        return setup()
    return _facility
