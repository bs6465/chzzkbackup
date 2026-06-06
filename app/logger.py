from __future__ import annotations

import logging

from .db import db


class SQLiteLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            db.add_log(record.levelname, self.format(record))
        except Exception:
            pass


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("chzzkbackup")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler = SQLiteLogHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    logger.propagate = False
    return logger


logger = setup_logging()
