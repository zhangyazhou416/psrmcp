"""ID generation for artifacts, jobs and sessions (TOOLS.md §1.2 patterns)."""

from __future__ import annotations

import secrets
import time


def _suffix(n_bytes: int = 8) -> str:
    return secrets.token_hex(n_bytes)


def new_artifact_id() -> str:
    return f"art_{time.strftime('%Y%m%d')}{_suffix(6)}"


def new_job_id() -> str:
    return f"job_{time.strftime('%Y%m%d')}{_suffix(6)}"


def new_session_id() -> str:
    return f"ses_{time.strftime('%Y%m%dT%H%M%S')}_{_suffix(4)}"
