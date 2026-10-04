"""Static checks over the web app's copy of the policy schema (no database, no Node).

The public web bundle must not carry the whole API schema: blocking /api/openapi.json at the
gateway means nothing if every request and response shape ships in a JavaScript chunk. So
policyMeta.ts imports only ``$defs.PolicyParams``, copied into policyParams.schema.json, and
this test keeps that copy identical to web/src/api/schema.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"
SLIM = WEB_SRC / "components" / "model" / "policyParams.schema.json"
REGEN = "cd web && npm run gen:types"  # writes types.ts and policyParams.schema.json from schema.json


def test_policy_params_copy_matches_the_generated_schema():
    full = json.loads((WEB_SRC / "api" / "schema.json").read_text(encoding="utf-8"))
    slim = json.loads(SLIM.read_text(encoding="utf-8"))
    assert slim == full["$defs"]["PolicyParams"], f"policyParams.schema.json is stale; regenerate it: {REGEN}"


def test_no_web_source_imports_the_full_schema():
    imports = re.compile(r"""(?:from|import)\s*\(?\s*['"][^'"]*api/schema\.json['"]""")
    offenders = [
        str(path.relative_to(ROOT))
        for path in WEB_SRC.rglob("*")
        if path.suffix in (".ts", ".vue", ".js") and imports.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"these would bundle the whole API schema into the public web app: {offenders}"
    meta = (WEB_SRC / "components" / "model" / "policyMeta.ts").read_text(encoding="utf-8")
    assert "from './policyParams.schema.json'" in meta
