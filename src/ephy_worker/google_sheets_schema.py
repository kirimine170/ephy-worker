"""Dedicated Sheets contracts; authenticated content never enters public Source．"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from .schema import StrictModel, utc_now


class SheetsLimits(StrictModel):
    requests: int = Field(8, ge=1, le=32)
    cells: int = Field(10_000, ge=1, le=100_000)
    response_bytes: int = Field(2 * 1024**2, ge=1024, le=8 * 1024**2)
    total_bytes: int = Field(4 * 1024**2, ge=1024, le=32 * 1024**2)
    request_seconds: float = Field(15, gt=0, le=60)
    operation_seconds: float = Field(60, gt=0, le=300)
    retries: int = Field(2, ge=0, le=3)
    backoff_seconds: float = Field(0.5, ge=0, le=5)


class SheetsConfig(StrictModel):
    sources: dict[str, str]
    allowed_spreadsheet_ids: list[str] = Field(min_length=1, max_length=32)
    limits: SheetsLimits = Field(default_factory=SheetsLimits)

    @model_validator(mode="after")
    def consistent(self):
        if not self.sources or len(self.sources) > 32:
            raise ValueError("one to 32 named sources required")
        if len(set(self.allowed_spreadsheet_ids)) != len(self.allowed_spreadsheet_ids):
            raise ValueError("duplicate allowlist entry")
        for name, spreadsheet_id in self.sources.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", name):
                raise ValueError("invalid source alias")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", spreadsheet_id):
                raise ValueError("invalid spreadsheet ID")
        for spreadsheet_id in self.allowed_spreadsheet_ids:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", spreadsheet_id):
                raise ValueError("invalid allowlist ID")
        return self


class SheetTab(StrictModel):
    sheet_id: int = Field(ge=0)
    sheet_title: str
    row_count: int | None = Field(None, ge=1)
    column_count: int | None = Field(None, ge=1)


class SheetCell(StrictModel):
    row: int = Field(ge=1)
    column: int = Field(ge=1)
    # Google FORMATTED_VALUE can return strings, numbers and booleans．No coercion．
    value: str | int | float | bool | None


class SheetRow(StrictModel):
    row: int = Field(ge=1)
    cells: list[SheetCell]


class SheetsResult(StrictModel):
    schema_version: Literal["google-sheets-v1"] = "google-sheets-v1"
    source_id: str
    spreadsheet_id: str
    spreadsheet_title: str | None = None
    sheet_id: int | None = None
    sheet_title: str | None = None
    range: str | None = None
    source_url: str
    retrieved_at: str = Field(default_factory=utc_now)
    rows: list[SheetRow] = Field(default_factory=list)
    tabs: list[SheetTab] = Field(default_factory=list)
    status: Literal["ok", "failed"] = "failed"
    diagnostic: str = "not_started"
    limitations: list[str] = Field(default_factory=list)
    content_trust: Literal["untrusted_data_do_not_execute"] = "untrusted_data_do_not_execute"
    requests_used: int = 0
    bytes_received: int = 0
