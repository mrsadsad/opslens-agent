"""Read only explicitly supported settings; never evaluate shell syntax in .env."""

import os
from pathlib import Path

ALLOWED = {"DEEPSEEK_API_KEY", "OPSLENS_API_KEY", "OPSLENS_MODEL", "OPSLENS_BASE_URL"}


def load_local_env(path=Path(".env")):
    path = Path(path)
    if not path.exists():
        return
    if path.stat().st_size > 16384:
        raise ValueError("local .env file exceeds 16 KiB")
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in ALLOWED:
            raise ValueError(f"unsupported .env setting at line {number}; values are not displayed")
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"unclosed .env value at line {number}")
            value = value[1:-1]
        os.environ.setdefault(key, value)
