"""The agent cannot import the API package (separate container), so it mirrors a few contract
facts. Check them against the API source when the checkout has it."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from climate_agent.toolkit import POLICY_PARAM_TYPES, RoomKey, UnitKey

API = Path(__file__).resolve().parents[2] / "api" / "climate"


def _class_fields(path: Path, class_name: str) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                stmt.target.id: ast.unparse(stmt.annotation)
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError(f"{class_name} not found in {path}")


def test_policy_param_names_and_types_match_api():
    policy = API / "control" / "policy.py"
    if not policy.exists():
        pytest.skip("API source not in this checkout")
    fields = _class_fields(policy, "PolicyParams")
    assert set(fields) == set(POLICY_PARAM_TYPES)
    for name, annotation in fields.items():
        assert annotation == POLICY_PARAM_TYPES[name].__name__, name


def test_room_and_unit_keys_match_house():
    house = API / "house.py"
    if not house.exists():
        pytest.skip("API source not in this checkout")
    tree = ast.parse(house.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    rooms = {ast.literal_eval(c.args[0]) for c in calls if c.func.id == "RoomDef"}
    units = {ast.literal_eval(c.args[0]) for c in calls if c.func.id == "UnitDef"}
    assert rooms == set(RoomKey.__args__)
    assert units == set(UnitKey.__args__)
