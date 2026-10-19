"""Nonmodal, plain-text Karte browsing, isolated from worker lifecycle receipts."""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from tkinter import ttk

from .inspection import InspectionError, KarteReader


@dataclass(frozen=True)
class InspectionResult:
    value: object = None
    error: str | None = None


class InspectionRequest:
    """One bounded I/O request at a time; only the Tk thread consumes results."""

    def __init__(self):
        self._queue = queue.Queue(maxsize=1)
        self._generation = 0
        self.busy = False
        self.closed = False

    def submit(self, operation: Callable[[], object]) -> bool:
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
            self._queue.put((generation, result))

        threading.Thread(target=run, name="desktop-readonly-inspection", daemon=True).start()
        return True

    def poll(self) -> InspectionResult | None:
        try:
            generation, result = self._queue.get_nowait()
        except queue.Empty:
            return None
        self.busy = False
        if self.closed or generation != self._generation:
            return None
        return result

    def invalidate(self, *, close=False):
        self._generation += 1
        self.closed = self.closed or close


def inspection_error_text(code: str) -> str:
    return f"読み取りに失敗しました（{code}）．設定・CLI・保存先を確認して再度操作してください．"


class RecordsModel:
    """Read-only search and page state. Never creates an observer or worker."""

    def __init__(self, config, *, reader=None):
        self.reader = reader if reader is not None else KarteReader(config)
        self.request = InspectionRequest()
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

    def poll(self) -> bool:
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

    def close(self):
        self.request.invalidate(close=True)
        self.records = []
        self.page = None
        self.task_id = None
        self._pending = None
        self.offset = 0
        self.previous_offsets = []
        self.message = ""


class RecordsWindow:
    """Local read-only window: no HTML renderer, link handlers, or write actions."""

    def __init__(self, parent, config, *, on_close):
        self.model = RecordsModel(config)
        self._on_close = on_close
        self._closed = False
        self.window = tk.Toplevel(parent)
        self.window.title("Karte 記録を読む · 読み取り専用")
        self.window.geometry("1000x680")
        self.window.minsize(760, 480)
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
        ttk.Label(pages, text="task / events · JSON 表示 · リンクは開きません").pack(side="left")
        detail_frame = ttk.Frame(outer)
        detail_frame.pack(fill="both", expand=True)
        self.detail = tk.Text(detail_frame, wrap="word", state="disabled", font=("Consolas", 10))
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll = ttk.Scrollbar(detail_frame, command=self.detail.yview)
        detail_scroll.pack(side="right", fill="y")
        self.detail.configure(yscrollcommand=detail_scroll.set)
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

    def _poll(self):
        if self._closed:
            return
        pending_search = self.model._pending and self.model._pending[0] == "search"
        if self.model.poll():
            self._render(rebuild=bool(pending_search))
        self.window.after(80, self._poll)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.model.close()
        self.window.destroy()
        self._on_close()
