"""Fingerprint only the analytical records that contribute to this display."""

import hashlib
import sqlite3
from contextlib import closing
from functools import lru_cache


@lru_cache(maxsize=4)
def green_fingerprint(path, modified, size):
    digest = hashlib.sha256()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        for row in db.execute(
            "SELECT id,properties,geometry FROM areas "
            "WHERE json_extract(properties,'$.kind') IN ('park','wood') ORDER BY id"
        ):
            for value in row:
                data = value if isinstance(value, bytes) else str(value).encode()
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
    return digest.hexdigest()
