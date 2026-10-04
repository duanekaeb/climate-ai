"""Routes: see docs/BUILD.md (API) and climate.api.schemas for the bodies."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["setup"])
