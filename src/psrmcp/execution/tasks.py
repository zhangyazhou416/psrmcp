"""MCP Tasks extension adapter (DESIGN.md §6.3, TOOLS.md §1.4).

The official python SDK does not expose the Tasks extension yet. This module keeps
the capability probe in one place so tools can switch transparently once it lands;
until then every long job uses the job_get/job_cancel fallback path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TasksCapability:
    supported: bool = False
    reason: str = "Tasks extension not implemented in the current MCP SDK; falling back to job_get/job_cancel"

    def describe(self) -> str:
        return self.reason if not self.supported else "MCP Tasks extension available"
