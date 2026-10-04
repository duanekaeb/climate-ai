"""Export the API contract (climate.api.schemas) as one JSON Schema document.

The web app turns it into TypeScript: ``cd web && npm run gen:types``.
"""

from __future__ import annotations

import inspect
import json
import sys

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from climate.api import schemas


def _strip_titles(node, top_level: bool = False) -> None:
    """Drop per-property titles so the generator doesn't emit an alias type for each field."""
    if isinstance(node, dict):
        if not top_level:
            node.pop("title", None)
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for sub in value.values():
                    _strip_titles(sub)
            elif isinstance(value, (dict, list)):
                _strip_titles(value)
    elif isinstance(node, list):
        for item in node:
            _strip_titles(item)


def main() -> None:
    models = [
        obj
        for _, obj in inspect.getmembers(schemas, inspect.isclass)
        if issubclass(obj, BaseModel) and obj is not BaseModel
    ]
    _, top = models_json_schema([(m, "serialization") for m in models], ref_template="#/$defs/{model}")
    defs = top.get("$defs", {})
    for name, d in defs.items():
        _strip_titles(d, top_level=True)
        # Responses always carry every field; mark them required so TS types aren't optional.
        # Request bodies (``*Body``, ``*Update``) keep pydantic's defaults-are-optional semantics.
        if d.get("type") == "object" and "properties" in d and not name.endswith(("Body", "Update")):
            d["required"] = sorted(d["properties"])
    root = {
        "title": "ApiTypes",
        "type": "object",
        "properties": {name: {"$ref": f"#/$defs/{name}"} for name in sorted(defs)},
        "additionalProperties": False,
        "$defs": defs,
    }
    json.dump(root, sys.stdout, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
