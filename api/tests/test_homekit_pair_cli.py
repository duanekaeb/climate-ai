"""scripts/pair_ecobee.py with a fake controller: prompts, saves encrypted to the DB, no files."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("aiohomekit")

from climate.collector.homekit_service import secret_key
from climate.sources import homekit as hk
from climate.store import secrets
from climate.store.db import session_scope
from climate.store.orm import HomekitDevice, Unit
from tests.test_homekit_fakes import SECRET_LTSK, FakeController, FakeDevice

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pair_ecobee.py"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("pair_ecobee", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def scripted(answers: list[str]):
    prompts: list[str] = []

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        return answers.pop(0)

    return input_fn, prompts


async def test_cli_pairs_and_saves_encrypted(cli, db, tmp_path):
    dev = FakeDevice()
    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=lambda _p: FakeController([dev]))
    input_fn, prompts = scripted(["12345678", " 123-45-678 "])
    out: list[str] = []
    args = cli.parse_args(["hallway", "AA:BB:CC:DD:EE:01", "--unit", "main"])
    rc = await cli.run(args, bridge=bridge, input_fn=input_fn, out=out.append)
    assert rc == 0, out
    assert len(prompts) == 2 and hk.BAD_CODE_FORMAT_MSG in out  # re-prompted on the bad format
    assert dev.log[-1] == "close_temporary"
    with session_scope() as s:
        data = secrets.get_secret_json(s, secret_key("hallway"))
        row = s.get(HomekitDevice, dev.id)
        assert data is not None and data["iOSDeviceLTSK"] == SECRET_LTSK
        assert (row.alias, row.unit_key, row.pairing_state, row.model) == ("hallway", "main", "paired", "ECB501")
        assert s.get(Unit, "main").homekit_device_id == dev.id
    # never a plain pairing file next to the charmap cache
    assert sorted(p.name for p in (tmp_path / "hk").iterdir()) == ["charmap.json"]
    assert not any(SECRET_LTSK in line for line in out)


async def test_cli_refuses_an_alias_that_is_already_saved(cli, db, tmp_path):
    with session_scope() as s:
        secrets.put_secret_json(s, secret_key("hallway"), {"x": 1})
    dev = FakeDevice()
    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=lambda _p: FakeController([dev]))
    out: list[str] = []
    args = cli.parse_args(["hallway", "aa:bb:cc:dd:ee:01", "--unit", "main"])
    rc = await cli.run(args, bridge=bridge, input_fn=lambda _p: "123-45-678", out=out.append)
    assert rc == 2 and "already saved" in out[0]
    assert dev.log == [] and not bridge.started


async def test_cli_reports_wrong_code(cli, db, tmp_path):
    dev = FakeDevice()
    bridge = hk.HomekitBridge(tmp_path / "hk", controller_factory=lambda _p: FakeController([dev]))
    out: list[str] = []
    args = cli.parse_args(["hallway", "aa:bb:cc:dd:ee:01", "--unit", "main"])
    rc = await cli.run(args, bridge=bridge, input_fn=lambda _p: "999-99-999", out=out.append)
    assert rc == 1 and out[-1] == f"error: {hk.WRONG_CODE_MSG}"
    with session_scope() as s:
        assert secrets.get_secret_json(s, secret_key("hallway")) is None


def test_cli_validates_arguments(cli):
    with pytest.raises(SystemExit):
        cli.parse_args(["Hallway!", "aa:bb:cc:dd:ee:01", "--unit", "main"])
    with pytest.raises(SystemExit):
        cli.parse_args(["hallway", "not-a-device", "--unit", "main"])
    with pytest.raises(SystemExit):
        cli.parse_args(["hallway", "aa:bb:cc:dd:ee:01", "--unit", "garage"])
