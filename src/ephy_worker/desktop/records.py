"""Nonmodal, plain-text Karte browsing, isolated from worker lifecycle receipts."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from tkinter import ttk

from .inspection import InspectionError, KarteReader
from .renderer import KarteRenderer, KarteViewSession

VIEW_TTL_SECONDS = 300
VIEW_PROVENANCE = "既存ローカル worklog の派生 Markdown / 公式固定版 KarteRenderer"


@dataclass(frozen=True)
class InspectionResult:
    value: object = None
    error: str | None = None


class InspectionRequest:
    """One bounded I/O request at a time; only the Tk thread consumes results."""

    def __init__(self):
        self._queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._generation = 0
        self.busy = False
        self.closed = False

    def submit(self, operation: Callable[[], object], *, discard=None) -> bool:
        with self._lock:
            if self.busy or self.closed:
                return False
            generation = self._generation
            self.busy = True

        def run():
            try:
                result = InspectionResult(value=operation())
            except InspectionError as exc:
                result = InspectionResult(error=exc.code)
            except Exception:  # noqa: BLE001 -- report failure without exposing CLI/private exception text.
                result = InspectionResult(error="inspection_failed")
            with self._lock:
                stale = self.closed or generation != self._generation
                if not self.closed:
                    self._queue.put_nowait((generation, InspectionResult() if stale else result,
                                           None if stale else discard))
            if stale and discard is not None and result.error is None:
                discard(result.value)

        threading.Thread(target=run, name="desktop-readonly-inspection", daemon=True).start()
        return True

    def poll(self) -> InspectionResult | None:
        with self._lock:
            try:
                generation, result, discard = self._queue.get_nowait()
            except queue.Empty:
                return None
            self.busy = False
            stale = self.closed or generation != self._generation
        if stale:
            if discard is not None and result.error is None:
                discard(result.value)
            return None
        return result

    def invalidate(self, *, close=False):
        with self._lock:
            self._generation += 1
            self.closed = self.closed or close
            try:
                _generation, result, discard = self._queue.get_nowait()
            except queue.Empty:
                return
            self.busy = False
        if discard is not None and result.error is None:
            discard(result.value)


def inspection_error_text(code: str) -> str:
    return f"読み取りに失敗しました（{code}）．設定・CLI・保存先を確認して再度操作してください．"


def view_error_text(code: str) -> str:
    explanations = {
        "karte_renderer_view_missing": "閲覧用 Markdown がありません．この画面では再生成しません．",
        "karte_renderer_view_conflict": "原本と閲覧用 Markdown が一致しません．既存データを保持しました．",
        "karte_renderer_permission_denied": "現在の設定では閲覧が許可されていません．",
        "karte_renderer_v2_conflict": "v2 記録はこの閲覧機能の対象外です．",
        "karte_renderer_helper_missing": "同梱の読取り用 helper が見つかりません．",
    }
    return f"閲覧 URL を作成できません（{code}）．" + explanations.get(
        code, "読取り用 helper と保存先を確認してください．")


@dataclass(frozen=True)
class ViewOffer:
    session: object
    url: str
    identity: dict
    expires_at: float


def _discard_view(offer: ViewOffer):
    offer.session.close()


def _open_default_browser(url: str):
    # Windows registered HTTP handler; no shell command or BROWSER environment lookup.
    os.startfile(url)


class RecordsModel:
    """Read-only search and page state. Never creates an observer or worker."""

    def __init__(self, config, *, reader=None, renderer=None, session_factory=None, clock=None, native_launcher=None):
        self.reader = reader if reader is not None else KarteReader(config)
        self._config = config
        self._renderer = renderer
        self._session_factory = session_factory or KarteViewSession
        self._clock = clock or time.monotonic
        self.request = InspectionRequest()
        self.view_request = InspectionRequest()
        self.native_request = InspectionRequest()
        self.native_launcher = native_launcher
        self.native_message = "選択した派生文書の編集可能なコピーを，専用データで開きます．原本への同期はありません．"
        self._native_task = None
        self._native_state = None
        self._auto_open_pending = False
        self.browser_state = "idle"
        self._owner_thread = threading.get_ident()
        self.view_url = ""
        self.view_identity: dict | None = None
        self.view_message = "選択した記録の既存 Markdown を読み取り，閲覧 URL を作成します．"
        self._view_session = None
        self._view_deadline = None
        self.records: list[dict] = []
        self.page: dict | None = None
        self.task_id: str | None = None
        self.offset = 0
        self.previous_offsets: list[int] = []
        self._pending = None
        self.message = "固定検索の結果を最大 20 件表示します．全履歴ではありません．"

    def refresh(self) -> bool:
        if not self.request.submit(self.reader.search):
            return False
        self._invalidate_view()
        self.records = []
        self.page = None
        self.task_id = None
        self.offset = 0
        self.previous_offsets = []
        self._pending = ("search", None, 0, [])
        self.message = "検索中…"
        return True

    def select(self, task_id: str) -> bool:
        if task_id not in {record["task_id"] for record in self.records}:
            return False
        return self._read(task_id, 0, [])

    def _read(self, task_id, offset, previous):
        if not self.request.submit(lambda: self.reader.read(task_id, offset=offset)):
            return False
        self._invalidate_view()
        self.page = None
        self._pending = ("read", task_id, offset, previous)
        self.message = "記録を読み取り中…"
        return True

    def next_page(self) -> bool:
        offset = self.page.get("next_offset") if self.page else None
        if offset is None or offset < 0 or self.task_id is None:
            return False
        return self._read(self.task_id, offset, [*self.previous_offsets, self.offset])

    def previous_page(self) -> bool:
        if not self.page or not self.previous_offsets or self.task_id is None:
            return False
        return self._read(self.task_id, self.previous_offsets[-1], self.previous_offsets[:-1])

    def create_view(self) -> bool:
        if (self.request.busy or self.view_request.busy or self.view_request.closed
                or self.page is None or self.task_id is None or self._view_is_current()):
            return False
        self._invalidate_view()
        task_id = self.task_id

        def prepare():
            renderer = self._renderer if self._renderer is not None else KarteRenderer(self._config)
            rendered = renderer.render(task_id)
            identity = {key: rendered[key] for key in (
                "task_id", "doc_id", "relative_path", "source_sha256", "markdown_sha256", "renderer_commit",
            )}
            session = self._session_factory(lambda: renderer.render(task_id), ttl_seconds=VIEW_TTL_SECONDS)
            try:
                url = session.start()
                return ViewOffer(session, url, identity, self._clock() + VIEW_TTL_SECONDS)
            except Exception:
                session.close()
                raise

        if not self.view_request.submit(prepare, discard=_discard_view):
            return False
        self.view_message = "閲覧権限と既存 Markdown を確認中…"
        return True

    def _view_is_current(self) -> bool:
        return (
            not self.view_request.closed and not self.request.busy and not self.view_request.busy
            and self.page is not None and self.task_id is not None
            and self.view_identity is not None and self.view_identity.get("task_id") == self.task_id
            and self._view_session is not None and self._view_session.active
            and self._view_deadline is not None and self._clock() < self._view_deadline
        )

    def open_view(self, opener, *, retry=False) -> bool:
        if threading.get_ident() != self._owner_thread:
            return False
        if not self._view_is_current() or self.browser_state == "opening":
            return False
        if retry:
            if self.browser_state != "failed":
                return False
        elif not self._auto_open_pending:
            return False
        self._auto_open_pending = False
        session, url = self._view_session, self.view_url
        self.browser_state = "opening"
        try:
            session.open_in_browser(url, opener)
        except Exception:  # noqa: BLE001 -- native errors may include private handler details.
            if self._view_session is session and self.view_url == url:
                self.browser_state = "failed"
                self.view_message = "既定ブラウザーへ送信できませんでした．有効な間は「ブラウザー起動を再試行」で再送できます．"
            return False
        if self._view_session is not session or self.view_url != url:
            return False
        self.browser_state = "dispatched"
        self.view_message = "既定ブラウザーへ URL を送信しました（表示完了は未確認）．最長 5 分有効で，読込み時に認可を再確認します．"
        return True

    def cancel_view(self):
        self._invalidate_view()
        self.view_message = "閲覧を取り消しました．必要なら新しく作成してください．"

    def create_native_snapshot(self) -> bool:
        launcher = self.native_launcher
        if (
            launcher is None or launcher.busy or launcher.active or self.native_request.busy
            or self.native_request.closed or self.request.busy or self.page is None or self.task_id is None
        ):
            return False
        task_id = self.task_id
        if not self.native_request.submit(lambda: launcher.prepare(self._config, task_id), discard=launcher.discard):
            return False
        self._native_task = task_id
        self.native_message = "現在の認可と選択した派生文書を確認中…"
        return True

    def _poll_native(self) -> bool:
        launcher = self.native_launcher
        before = self.native_request.busy
        result = self.native_request.poll()
        state = (launcher.busy, launcher.active) if launcher is not None else None
        changed = before != self.native_request.busy or state != self._native_state
        self._native_state = state
        if result is None:
            return changed
        if result.error:
            self.native_message = f"Karte のコピーを開けません（{result.error}）．元の記録は変更していません．"
        else:
            offer = result.value
            if (
                threading.get_ident() != self._owner_thread or self.request.busy or self.page is None
                or self.task_id != self._native_task or offer.task_id != self.task_id
            ):
                launcher.discard(offer)
                self.native_message = "選択が変わったため，Karte のコピー起動を取り消しました．"
            else:
                try:
                    launched = launcher.launch(offer)
                    self.native_message = (
                        f"Karte 本体へ起動を要求しました．コピー先: {launched.data_root}\n"
                        f"左の一覧で {launched.relative_path} を選んでください．編集はコピーだけに保存され，原本への同期はありません．"
                    )
                except InspectionError as exc:
                    self.native_message = f"Karte のコピーを開けません（{exc.code}）．別経路への切替えは行いません．"
                except Exception:  # noqa: BLE001 -- do not disclose process or private-path errors.
                    self.native_message = "Karte のコピーを開けません（native_karte_failed）．"
                finally:
                    launcher.discard(offer)
        self._native_task = None
        return True

    def _invalidate_view(self, *, close=False):
        self.native_request.invalidate(close=close)
        self._native_task = None
        self.native_message = "選択した派生文書のコピーを作成できます．原本への同期はありません．"
        self._auto_open_pending = False
        self.browser_state = "idle"
        self.view_request.invalidate(close=close)
        if self._view_session is not None:
            self._view_session.close()
        self._view_session = None
        self._view_deadline = None
        self.view_url = ""
        self.view_identity = None
        self.view_message = "閲覧 URL は未作成です．"

    def _poll_records(self) -> bool:
        result = self.request.poll()
        if result is None:
            return False
        kind, task_id, offset, previous = self._pending
        self._pending = None
        if result.error:
            self.page = None
            self.task_id = None
            self.previous_offsets = []
            self.offset = 0
            if kind == "search":
                self.records = []
            self.message = inspection_error_text(result.error)
        elif kind == "search":
            self.records = result.value
            self.message = f"検索結果 {len(self.records)} 件（最大 20 件）．全履歴・全体の最新順ではありません．"
        else:
            self.page = result.value
            self.task_id, self.offset, self.previous_offsets = task_id, offset, previous
            count = len(self.page["events"])
            page_range = f"{offset + 1}–{offset + count}" if count else "0"
            self.message = f"イベント {page_range}（1 ページ最大 10 件）．記録内容は文字列として表示します．"
        return True

    def poll(self) -> bool:
        changed = self._poll_records()
        view_was_busy = self.view_request.busy
        result = self.view_request.poll()
        changed = changed or view_was_busy != self.view_request.busy
        if result is not None:
            changed = True
            if result.error:
                self.view_message = view_error_text(result.error)
            else:
                offer = result.value
                self._view_session = offer.session
                self._view_deadline = offer.expires_at
                self.view_url = offer.url
                self.view_identity = offer.identity
                self._auto_open_pending = True
                self.browser_state = "pending"
                self.view_message = "閲覧 URL を作成しました．現在の選択と有効期限を確認してブラウザーへ送信します．"
        if self._view_session is not None and (
            not self._view_session.active or self._clock() >= self._view_deadline
        ):
            self._invalidate_view()
            self.view_message = "閲覧 URL は期限切れです．必要なら再作成してください．"
            changed = True
        return self._poll_native() or changed

    def close(self):
        self.request.invalidate(close=True)
        self._invalidate_view(close=True)
        self.records = []
        self.page = None
        self.task_id = None
        self._pending = None
        self.offset = 0
        self.previous_offsets = []
        self.message = ""


class RecordsWindow:
    """Ledger projection, current owned browser view, and authorized native copies."""

    def __init__(self, parent, config, *, on_close, native_launcher=None, browser_opener=None):
        self.model = RecordsModel(config, native_launcher=native_launcher)
        self._browser_opener = browser_opener or _open_default_browser
        self._on_close = on_close
        self._closed = False
        self.window = tk.Toplevel(parent)
        self.window.title("Karte 記録を読む · 読み取り専用")
        self.window.geometry("1000x780")
        self.window.minsize(760, 680)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(self.window, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="有効な Karte 保存先（選択・コピー可）").pack(anchor="w")
        self.root_value = tk.StringVar(self.window, value=str(config.karte_root))
        self.root_field = ttk.Entry(outer, textvariable=self.root_value, state="readonly")
        self.root_field.pack(fill="x", pady=(4, 10))
        self.refresh_button = ttk.Button(outer, text="固定検索を更新（最大 20 件）", command=self.refresh)
        self.refresh_button.pack(anchor="w")
        self.status = tk.StringVar(self.window)
        ttk.Label(outer, textvariable=self.status, wraplength=900).pack(fill="x", pady=8)
        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="x")
        self.table = ttk.Treeview(list_frame, columns=("title", "status", "count", "time"),
                                  show="headings", height=6, selectmode="browse")
        for key, title, width in (("title", "タスク", 350), ("status", "状態", 90),
                                   ("count", "件数", 65), ("time", "最終記録日時", 200)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=50)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_frame, command=self.table.yview)
        scrollbar.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind("<<TreeviewSelect>>", self._select)
        pages = ttk.Frame(outer)
        pages.pack(fill="x", pady=8)
        self.previous_button = ttk.Button(pages, text="前の 10 件", command=self.previous_page)
        self.previous_button.pack(side="left")
        self.next_button = ttk.Button(pages, text="次の 10 件", command=self.next_page)
        self.next_button.pack(side="left", padx=8)
        ttk.Label(pages, text="原本台帳の API 読取り結果（JSON・ページ表示）").pack(side="left")
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        detail_frame = ttk.Frame(tabs)
        tabs.add(detail_frame, text="台帳 API（JSON）")
        self.detail = tk.Text(detail_frame, wrap="word", state="disabled", font=("Consolas", 10))
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(detail_frame, command=self.detail.yview)
        detail_scroll.pack(side="right", fill="y")
        self.detail.configure(yscrollcommand=detail_scroll.set)
        view_frame = ttk.Frame(tabs, padding=10)
        tabs.add(view_frame, text="閲覧用 Markdown")
        ttk.Label(view_frame, text=VIEW_PROVENANCE, wraplength=850).pack(anchor="w")
        view_actions = ttk.Frame(view_frame)
        view_actions.pack(fill="x", pady=8)
        self.view_button = ttk.Button(view_actions, text="閲覧 URL を作成して開く", command=self.create_view)
        self.view_button.pack(side="left")
        self.retry_button = ttk.Button(view_actions, text="ブラウザー起動を再試行", command=self.retry_browser)
        self.retry_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(view_actions, text="閲覧を取り消す", command=self.cancel_view)
        self.cancel_button.pack(side="left")
        self.native_button = ttk.Button(view_frame, text="選択した記録を Karte で開く（コピー）", command=self.create_native_snapshot)
        self.native_button.pack(anchor="w", pady=4)
        self.native_status = tk.StringVar(self.window)
        ttk.Label(view_frame, textvariable=self.native_status, wraplength=850).pack(fill="x")
        ttk.Label(view_frame, text="コピーは編集可能で，原本へ同期しません．起動済みの Karte とコピーは，この画面を閉じても残ります．",
                  wraplength=850).pack(fill="x")
        self.view_status = tk.StringVar(self.window)
        ttk.Label(view_frame, textvariable=self.view_status, wraplength=850).pack(fill="x", pady=4)
        self.view_url_value = tk.StringVar(self.window)
        self.view_url_field = ttk.Entry(view_frame, textvariable=self.view_url_value, state="readonly")
        self.view_url_field.pack(fill="x", pady=4)
        ttk.Label(view_frame, text="画面を閉じる・選択を変えると URL を無効化します．"
                  "既にブラウザーへ表示・コピーした内容は消せません．", wraplength=850).pack(fill="x", pady=4)
        self.view_identity = tk.Text(view_frame, wrap="word", state="disabled", height=8, font=("Consolas", 9))
        self.view_identity.pack(fill="both", expand=True)
        self.refresh()
        self._poll()

    def refresh(self):
        if self.model.refresh():
            self._render(rebuild=True)

    def _select(self, _event=None):
        selected = self.table.selection()
        if selected and not self.model.request.busy:
            index = int(selected[0])
            if index < len(self.model.records) and self.model.select(self.model.records[index]["task_id"]):
                self._render()

    def next_page(self):
        if self.model.next_page():
            self._render()

    def previous_page(self):
        if self.model.previous_page():
            self._render()

    def create_view(self):
        if self.model.create_view():
            self._render()

    def retry_browser(self):
        if not self._closed:
            self.model.open_view(self._browser_opener, retry=True)
            if not self._closed:
                self._render()

    def cancel_view(self):
        if not self._closed:
            self.model.cancel_view()
            self._render()

    def create_native_snapshot(self):
        if not self._closed and self.model.create_native_snapshot():
            self._render()

    def _render(self, *, rebuild=False):
        if rebuild:
            self.table.delete(*self.table.get_children())
            for index, record in enumerate(self.model.records):
                self.table.insert("", "end", iid=str(index), values=(
                    record["title"], record["status"], record["event_count"], record["last_recorded_at"],
                ))
        self.status.set(self.model.message)
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if self.model.page is not None:
            self.detail.insert("1.0", json.dumps(self.model.page, ensure_ascii=False, indent=2))
        self.detail.configure(state="disabled")
        busy = self.model.request.busy
        self.table.configure(selectmode="none" if busy else "browse")
        self.refresh_button.state(["disabled"] if busy else ["!disabled"])
        self.previous_button.state(["!disabled"] if not busy and self.model.page and
                                   self.model.previous_offsets else ["disabled"])
        self.next_button.state(["!disabled"] if not busy and self.model.page and
                               self.model.page.get("next_offset", -1) >= 0 else ["disabled"])
        self.view_status.set(self.model.view_message)
        self.view_url_value.set(self.model.view_url)
        self.view_button.state(["!disabled"] if not busy and not self.model.view_request.busy
                               and self.model.page is not None and not self.model._view_is_current() else ["disabled"])
        self.retry_button.state(["!disabled"] if self.model._view_is_current()
                                and self.model.browser_state == "failed" else ["disabled"])
        self.cancel_button.state(["!disabled"] if self.model.view_request.busy or self.model.view_url
                                 or self.model.native_request.busy else ["disabled"])
        launcher = self.model.native_launcher
        self.native_button.state(["!disabled"] if launcher is not None and not launcher.busy and not launcher.active
                                 and not busy and not self.model.native_request.busy and self.model.page is not None
                                 else ["disabled"])
        self.native_status.set(self.model.native_message)
        self.view_identity.configure(state="normal")
        self.view_identity.delete("1.0", "end")
        if self.model.view_identity is not None:
            identity = self.model.view_identity
            self.view_identity.insert("1.0", "\n".join(
                f"{label}: {identity[key]}" for key, label in (
                    ("task_id", "作業 ID"), ("doc_id", "文書 ID"), ("relative_path", "閲覧用 Markdown"),
                    ("source_sha256", "台帳 SHA-256"), ("markdown_sha256", "Markdown SHA-256"),
                    ("renderer_commit", "KarteRenderer 固定版"),
                )
            ))
        self.view_identity.configure(state="disabled")

    def _poll(self):
        if self._closed:
            return
        pending_search = self.model._pending and self.model._pending[0] == "search"
        changed = self.model.poll()
        if self._closed:
            return
        if self.model._auto_open_pending:
            self.model.open_view(self._browser_opener)
            changed = True
        if self._closed:
            return
        if changed:
            self._render(rebuild=bool(pending_search))
        self.window.after(80, self._poll)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.model.close()
        self.window.destroy()
        self._on_close()
