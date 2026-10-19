"""Explicit, bounded Google Sheets reads using service-account ADC．"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from .google_sheets_schema import SheetCell, SheetRow, SheetsConfig, SheetsResult, SheetTab
from .schema import utc_now

READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets/"
METADATA_FIELDS = "spreadsheetId,properties(title),sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"


class SheetsError(ValueError):
    """Stable codes only: never attach response text or credential exceptions．"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def external_path(path: Path, *, must_exist: bool = False) -> Path:
    """Reject linked ancestry and every Git checkout, including linked worktrees．"""
    absolute = path.expanduser().absolute()
    for parent in (absolute, *absolute.parents):
        if _linked(parent):
            raise SheetsError("linked_path_refused")
        if (parent / ".git").exists():
            raise SheetsError("path_must_be_outside_git")
    if must_exist and not absolute.is_file():
        raise SheetsError("credential_missing")
    return absolute


def output_path(path: Path) -> Path:
    path = external_path(path)
    if path.exists():
        raise SheetsError("output_exists")
    if not path.parent.is_dir():
        raise SheetsError("output_parent_missing")
    return path


def publish_json(path: Path, result: SheetsResult) -> None:
    """Same-directory staging then exclusive atomic hard-link publication．"""
    path = output_path(path)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            if os.name == "posix":
                os.chmod(temporary, 0o600)
            handle.write(result.model_dump_json(indent=2))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        output_path(path)  # Recheck ancestry and target immediately before publication．
        os.link(temporary, path)  # Fails if another writer has already published．
    finally:
        temporary.unlink(missing_ok=True)


def column_number(value: str) -> int:
    number = 0
    for char in value:
        number = number * 26 + ord(char) - ord("A") + 1
    return number


@dataclass(frozen=True)
class CellRange:
    text: str
    first_row: int
    first_column: int
    last_row: int
    last_column: int

    @property
    def width(self) -> int:
        return self.last_column - self.first_column + 1

    @property
    def height(self) -> int:
        return self.last_row - self.first_row + 1

    def qualified(self, tab: str) -> str:
        return "'" + tab.replace("'", "''") + "'!" + self.text


def parse_range(value: str, max_cells: int) -> CellRange:
    # Fixed finite cells only．Tab qualification belongs exclusively to --tab．
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})(?::([A-Z]{1,3})([1-9][0-9]{0,6}))?", value)
    if not match:
        raise SheetsError("invalid_range")
    first_column, first_row = column_number(match[1]), int(match[2])
    last_column = column_number(match[3] or match[1])
    last_row = int(match[4] or match[2])
    if first_column > last_column or first_row > last_row or last_column > 18278:
        raise SheetsError("invalid_range")
    result = CellRange(value, first_row, first_column, last_row, last_column)
    if result.width * result.height > max_cells:
        raise SheetsError("cell_limit")
    return result


def resolve_source(config: SheetsConfig, source: str) -> str:
    spreadsheet_id = config.sources.get(source)
    if spreadsheet_id is None:
        raise SheetsError("unknown_source")
    if spreadsheet_id not in config.allowed_spreadsheet_ids:
        raise SheetsError("spreadsheet_not_allowlisted")
    return spreadsheet_id


class SheetsAdapter:
    def __init__(self, config: SheetsConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.limits = config.limits
        self.transport = transport
        self.deadline = time.monotonic() + self.limits.operation_seconds
        self.requests = 0
        self.bytes = 0
        self.credentials = None

    def close(self) -> None:
        # Every request owns an async client so cancellation closes the active socket．
        pass

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SheetsError("operation_timeout")
        return remaining

    def _http(self, method: str, url: str, *, headers=None, body=None, params=None):
        if self.requests >= self.limits.requests:
            raise SheetsError("request_limit")
        self.requests += 1
        return asyncio.run(self._http_async(method, url, headers=headers, body=body, params=params))

    async def _http_async(self, method: str, url: str, *, headers=None, body=None, params=None):
        headers = dict(headers or {})
        headers["Accept-Encoding"] = "identity"
        try:
            # Wall time bounds DNS, headers and slow-drip body, not just idle socket reads．
            async with asyncio.timeout(min(self.limits.request_seconds, self.remaining())):
                async with httpx.AsyncClient(transport=self.transport, follow_redirects=False, trust_env=False) as client:
                    async with client.stream(
                        method, url, headers=headers, content=body, params=params,
                        timeout=min(self.limits.request_seconds, self.remaining()), follow_redirects=False,
                    ) as response:
                        return await self._response(response)
        except (TimeoutError, httpx.TimeoutException):
            raise SheetsError("request_timeout") from None
        except httpx.HTTPError:
            raise SheetsError("network_error") from None

    async def _response(self, response: httpx.Response):
        # Never read error bodies, which can echo credential material．
        if response.status_code != 200:
            return response.status_code, b"", {}
        if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            raise SheetsError("encoded_response_refused")
        content = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            self.remaining()
            self.bytes += len(chunk)
            if len(content) + len(chunk) > self.limits.response_bytes or self.bytes > self.limits.total_bytes:
                raise SheetsError("response_byte_limit")
            content.extend(chunk)
        self.remaining()
        return response.status_code, bytes(content), dict(response.headers)

    @staticmethod
    def _status(status: int) -> str:
        return {
            401: "authentication_rejected",
            403: "access_denied_or_hidden",
            404: "not_found_or_hidden",
            429: "rate_limited",
        }.get(status, "temporary_server_error" if 500 <= status < 600 else "http_error")

    def _auth_request(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        # Google auth's token retries are disabled by raising here before its retry loop．
        if url != TOKEN_URL or method != "POST":
            raise SheetsError("unsafe_auth_endpoint")
        status, data, response_headers = self._http(method, url, body=body, headers=headers)
        if status != 200:
            raise SheetsError(self._status(status))
        return AuthResponse(status, data, response_headers)

    def _load_credentials(self):
        self.remaining()
        configured = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not configured:
            raise SheetsError("credential_missing")
        path = external_path(Path(configured), must_exist=True)
        try:
            with path.open("rb") as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise SheetsError("credential_invalid")
            info = json.loads(raw)
            if not isinstance(info, dict) or info.get("type") != "service_account":
                raise SheetsError("service_account_required")
            if info.get("subject") or info.get("delegated_subject") or info.get("scopes"):
                raise SheetsError("credential_delegation_or_scopes_refused")
            if info.get("token_uri") != TOKEN_URL or info.get("universe_domain", "googleapis.com") != "googleapis.com":
                raise SheetsError("unsafe_auth_endpoint")
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_info(info, scopes=[READONLY_SCOPE])
        except SheetsError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise SheetsError("credential_invalid") from None
        self.remaining()
        return credentials

    def _token(self) -> str:
        if self.credentials is None:
            self.credentials = self._load_credentials()
        try:
            if not self.credentials.valid:
                self.credentials.refresh(self._auth_request)
        except SheetsError:
            raise
        except Exception:  # noqa: BLE001 — third-party auth errors can carry credentials．
            raise SheetsError("authentication_failed") from None
        self.remaining()
        token = self.credentials.token
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise SheetsError("authentication_failed")
        return token

    def _get(self, spreadsheet_id: str, suffix: str = "", *, params=None) -> dict:
        # Defense in depth before credential discovery or any request．
        if spreadsheet_id not in self.config.allowed_spreadsheet_ids:
            raise SheetsError("spreadsheet_not_allowlisted")
        token = self._token()
        url = SHEETS_URL + quote(spreadsheet_id, safe="") + suffix
        for attempt in range(self.limits.retries + 1):
            status, data, _ = self._http("GET", url, headers={"Authorization": f"Bearer {token}"}, params=params)
            if status == 200:
                try:
                    result = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    raise SheetsError("invalid_response") from None
                self.remaining()
                if not isinstance(result, dict):
                    raise SheetsError("invalid_response")
                return result
            if status != 429 and not 500 <= status < 600 or attempt == self.limits.retries:
                raise SheetsError(self._status(status))
            delay = self.limits.backoff_seconds * 2**attempt
            if delay >= self.remaining():
                raise SheetsError("operation_timeout")
            time.sleep(delay)
        raise SheetsError("http_error")

    def _metadata(self, spreadsheet_id: str) -> tuple[str, list[SheetTab]]:
        payload = self._get(spreadsheet_id, params={"includeGridData": "false", "fields": METADATA_FIELDS})
        try:
            if payload["spreadsheetId"] != spreadsheet_id or not isinstance(payload["properties"]["title"], str):
                raise ValueError
            title = payload["properties"]["title"]
            tabs = []
            for entry in payload["sheets"]:
                self.remaining()
                properties = entry["properties"]
                grid = properties.get("gridProperties", {})
                tabs.append(SheetTab.model_validate({
                    "sheet_id": properties["sheetId"], "sheet_title": properties["title"],
                    "row_count": grid.get("rowCount"),
                    "column_count": grid.get("columnCount"),
                }, strict=True))
            self.remaining()
            if len({tab.sheet_id for tab in tabs}) != len(tabs) or len({tab.sheet_title for tab in tabs}) != len(tabs):
                raise ValueError
            return title, tabs
        except SheetsError:
            raise
        except (ValueError, KeyError, TypeError):
            raise SheetsError("invalid_metadata_response") from None

    def execute(self, source: str, *, tab: str | None = None, cell_range: str | None = None) -> SheetsResult:
        spreadsheet_id = resolve_source(self.config, source)
        selected = parse_range(cell_range, self.limits.cells) if cell_range is not None else None
        if selected and (not isinstance(tab, str) or not tab or len(tab) > 100 or any(ord(c) < 32 for c in tab)):
            raise SheetsError("invalid_tab")
        result = SheetsResult(source_id=source, spreadsheet_id=spreadsheet_id,
                              source_url=f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}",
                              range=selected.qualified(tab) if selected else None)
        try:
            result.spreadsheet_title, result.tabs = self._metadata(spreadsheet_id)
            if selected:
                matched = [entry for entry in result.tabs if entry.sheet_title == tab]
                if len(matched) != 1:
                    raise SheetsError("tab_not_found")
                sheet = matched[0]
                result.sheet_id, result.sheet_title = sheet.sheet_id, sheet.sheet_title
                result.source_url += f"/edit#gid={sheet.sheet_id}&range={quote(selected.text, safe='')}"
                if sheet.row_count is None or sheet.column_count is None:
                    raise SheetsError("unsupported_sheet_grid")
                if selected.last_row > sheet.row_count or selected.last_column > sheet.column_count:
                    raise SheetsError("range_outside_grid")
                payload = self._get(spreadsheet_id, "/values/" + quote(selected.qualified(tab), safe=""), params={
                    "valueRenderOption": "FORMATTED_VALUE", "majorDimension": "ROWS",
                })
                result.rows = self._rows(payload, selected, tab, check_deadline=self.remaining)
                result.limitations = ["Only the explicitly requested range was retrieved; other cells are unexamined.",
                                      "Trailing empty cells omitted by the API are represented as null.",
                                      "FORMATTED_VALUE is preserved without header, date or numeric inference."]
            self.remaining()
            result.status, result.diagnostic = "ok", "connected"
        except SheetsError as exc:
            result.status, result.diagnostic = "failed", exc.code
            result.rows = []
            result.limitations = ["Requested data may be missing; no partial cell data is published."]
        result.retrieved_at = utc_now()
        result.requests_used, result.bytes_received = self.requests, self.bytes
        return result

    @staticmethod
    def _rows(payload: dict, selected: CellRange, tab: str, *, check_deadline=None) -> list[SheetRow]:
        response_range = payload.get("range")
        # API may omit quotes on simple titles, but must identify the exact requested rectangle．
        allowed_ranges = {selected.qualified(tab), tab + "!" + selected.text}
        # A single requested cell is normally returned as A1:A1．
        if ":" not in selected.text:
            allowed_ranges.update({selected.qualified(tab) + ":" + selected.text,
                                   tab + "!" + selected.text + ":" + selected.text})
        if response_range not in allowed_ranges:
            raise SheetsError("response_range_mismatch")
        if payload.get("majorDimension", "ROWS") != "ROWS":
            raise SheetsError("invalid_values_response")
        values = payload.get("values", [])
        if not isinstance(values, list) or len(values) > selected.height:
            raise SheetsError("invalid_values_response")
        rows = []
        for row_offset in range(selected.height):
            if check_deadline is not None:
                check_deadline()
            cells = values[row_offset] if row_offset < len(values) else []
            if not isinstance(cells, list) or len(cells) > selected.width:
                raise SheetsError("invalid_values_response")
            for value in cells:
                if check_deadline is not None:
                    check_deadline()
                if type(value) not in (str, int, float, bool) or isinstance(value, float) and not math.isfinite(value):
                    raise SheetsError("invalid_values_response")
            actual_row = selected.first_row + row_offset
            normalized_cells = []
            for column_offset in range(selected.width):
                if check_deadline is not None:
                    check_deadline()
                normalized_cells.append(SheetCell(
                    row=actual_row, column=selected.first_column + column_offset,
                    value=cells[column_offset] if column_offset < len(cells) else None,
                ))
            rows.append(SheetRow(row=actual_row, cells=normalized_cells))
        if check_deadline is not None:
            check_deadline()
        return rows


@dataclass
class AuthResponse:
    status: int
    data: bytes
    headers: dict
