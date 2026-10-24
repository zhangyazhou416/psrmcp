"""Schema contract: our Pydantic input schemas must agree with docs/TOOLS.md.

For every tool we compare, against the extracted TOOLS.md fixture:
- the set of properties (no missing / no extra),
- the required list,
- additionalProperties: false.
Enum/const value sets are compared where the fixture declares them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "toolsmd"
TOOL_NAMES = sorted(p.name.removesuffix(".input.json") for p in FIXTURES.glob("*.input.json"))


def test_nineteen_tools_present(registry):
    assert TOOL_NAMES == sorted(registry.names())
    assert len(TOOL_NAMES) == 19


def _fixture_schema(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.input.json").read_text(encoding="utf-8"))


def _our_schema(registry, name: str) -> dict:
    tool = {t.name: t for t in registry.mcp_tools()}[name]
    return tool.input_schema


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_input_schema_matches_toolsmd(registry, name):
    ours, theirs = _our_schema(registry, name), _fixture_schema(name)

    our_props = set(ours.get("properties", {}))
    their_props = set(theirs.get("properties", {}))
    assert our_props == their_props, f"{name}: property mismatch missing={their_props - our_props} extra={our_props - their_props}"

    assert sorted(ours.get("required", [])) == sorted(theirs.get("required", []))
    assert ours.get("additionalProperties") is False

    # Value sets for enums/consts must match wherever the contract declares them.
    # Optional fields render as anyOf[{...}, null]; unwrap to the value branch.
    def _value_branch(defn: dict) -> dict:
        if "anyOf" in defn:
            branches = [b for b in defn["anyOf"] if b.get("type") != "null"]
            if len(branches) == 1:
                return branches[0]
        return defn

    for prop, tdef in theirs.get("properties", {}).items():
        odef = _value_branch(ours["properties"][prop])
        for key in ("enum", "const"):
            if key in tdef:
                assert sorted(odef.get(key, [])) == sorted(tdef[key]), f"{name}.{prop}: {key} mismatch"
        if "minimum" in tdef:
            assert odef.get("minimum") == tdef["minimum"], f"{name}.{prop}: minimum"
        if "maximum" in tdef:
            assert odef.get("maximum") == tdef["maximum"], f"{name}.{prop}: maximum"


ANNOTATIONS_TABLE = [
    # tool, readOnly, destructive, idempotent, openWorld, long_task  (TOOLS.md 4)
    ("pulsar_environment", True, False, True, False, False),
    ("data_find", True, False, True, False, False),
    ("catalog_search", True, False, True, True, False),
    ("catalog_get", True, False, True, True, False),
    ("ephemeris_export", False, False, True, True, False),
    ("archive_inspect", True, False, True, False, False),
    ("archive_convert", False, False, True, False, False),
    ("archive_merge", False, False, True, False, True),
    ("dedisperse", False, False, True, False, True),
    ("rfi_scan", True, False, True, False, False),
    ("rfi_zap", False, False, True, False, False),
    ("profile_standardize", False, False, True, False, False),
    ("profile_measure", True, False, True, False, False),
    ("toa_extract", False, False, True, False, True),
    ("timing_fit", False, False, True, False, True),
    ("timing_diagnose", True, False, True, False, False),
    ("artifact_list", True, False, True, False, False),
    ("job_get", True, False, True, False, False),
    ("job_cancel", False, False, False, False, False),
]


def test_annotations_match_toolsmd(registry):
    specs = {s.name: s for s in registry._specs.values()}
    for name, ro, destr, idem, ow, long_task in ANNOTATIONS_TABLE:
        spec = specs[name]
        a = spec.annotations
        assert a.read_only_hint == ro, name
        assert a.destructive_hint == destr, name
        assert a.idempotent_hint == idem, name
        assert a.open_world_hint == ow, name
        assert spec.long_task is long_task, name
