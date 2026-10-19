"""Windows-friendly Tk console over the existing bounded worker controller."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import queue
import re
import struct
import sys
import tempfile
import threading
import time
import tkinter as tk
import zlib
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import DesktopConfig, DesktopController
from .config import MAX_TIMEOUT_SECONDS
from .telemetry import MetricsSampler

BG = "#0a1120"
PANEL = "#111d30"
CARD = "#17263b"
FIELD = "#0d1829"
LINE = "#26394e"
TEXT = "#e6edf6"
MUTED = "#91a4bc"
TEAL = "#55dac5"
AMBER = "#f5c77c"
RED = "#f399a3"
FONT = "Yu Gothic UI"
MONO = "Consolas"
UNAVAILABLE = "— 未取得"
EXAMPLE_PROMPT = (
    "この公開リポジトリを読み取り専用で調べてください．\n\n"
    "1. 主なエントリーポイントと処理の流れ\n"
    "2. worker と Pi RPC の接続箇所\n"
    "3. 関連するテスト\n\n"
    "根拠のファイル名を示して要約してください．\n"
    "ファイルの変更やコマンド実行は行わないでください．"
)
STATE_LABELS = {
    "idle": "待機中", "starting": "開始中", "running": "実行中",
    "stopping": "停止中", "cancelling": "取消処理中", "cancelled": "取消済み", "completed": "完了",
    "failed": "失敗", "disconnected": "表示切断中", "reconnecting": "再接続中",
}
KARTE_LABELS = {
    "off": "未保存 · 設定なし", "saved": "保存済み", "saved_views_pending": "記録済み · 表示待ち",
    "unsaved": "未保存 / 未確認", "failed": "保存失敗",
}
KARTE_SCOPE = "開始・終了のみ／モデル自己申告・生成本文は未保存"


def build_identifier(path: Path | None = None) -> str:
    """Display only the public identifier from the packaged build metadata."""
    if path is None:
        if not getattr(sys, "frozen", False):
            return "source"
        path = Path(sys.executable).with_name("build-info.json")
    try:
        if path.stat().st_size > 16_384:
            return "build unavailable"
        value = json.loads(path.read_text(encoding="utf-8")).get("build_id")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return "build unavailable"


def karte_status_text(karte: dict) -> str:
    state = karte.get("state", "off")
    text = KARTE_LABELS.get(state, "未保存 / 未確認")
    if karte.get("pending"):
        text += f" · 保留 {karte['pending']}"
    return f"Karte ホスト観測：{text}"


def format_number(value: object, suffix: str = "", digits: int = 0) -> str:
    """Do not turn missing/non-finite measurements into plausible zeroes."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return UNAVAILABLE
    return f"{value:,.{digits}f}{suffix}"


def format_bytes(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return UNAVAILABLE
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GiB"
    return f"{value / 1024**2:.0f} MiB"


def format_elapsed(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return UNAVAILABLE
    minutes, seconds = divmod(max(0, int(value)), 60)
    return f"{minutes:02d}:{seconds:02d}"


def event_line(event: dict) -> str:
    """Render metadata only; raw tool arguments/results are deliberately omitted."""
    stamp = format_elapsed(event.get("at")) if isinstance(event.get("at"), (int, float)) else "--:--"
    kind = str(event.get("type") or event.get("kind") or "event")
    message = str(event.get("tool_name") or event.get("message") or "")
    message = " ".join(message.split())[:220]
    return f"{stamp}  {kind}" + (f"  ·  {message}" if message else "")


def _label(parent, text="", *, fg=TEXT, size=10, bold=False, bg=None, **kwargs):
    return tk.Label(
        parent, text=text, bg=bg or parent.cget("bg"), fg=fg,
        font=(FONT, size, "bold" if bold else "normal"), **kwargs,
    )


class WorkerConsole:
    """Tk widgets stay on the main thread; worker and telemetry run separately."""

    def __init__(self, root: tk.Tk, *, demo=False, config_path=None, controller=None, sampler=None,
                 demo_artifact_root=None):
        self.root = root
        self.controller = controller or DesktopController()
        self.config = None
        self.demo_artifact_root = demo_artifact_root or Path(tempfile.gettempdir()) / "EphyWorkerConsole-demo" / "runs"
        self._submitting = False
        self._closing = False
        self._destroyed = False
        self._displayed_text = ""
        self._displayed_events = ""
        self._last_run_id = None
        self._previous_state = None
        self._metrics_queue = queue.Queue(maxsize=1)
        self._metrics_stop = threading.Event()
        self._sample_pid = None
        self._last_snapshot = {}
        self.demo = tk.BooleanVar(root, value=demo)
        self.follow = tk.BooleanVar(root, value=True)
        self.notice = tk.StringVar(root, value="JSON 設定を読み込み，タスクを入力してください．")
        self.values: dict[str, tk.StringVar] = {}
        root.title(f"ephy · Worker Console [{build_identifier()}]")
        root.configure(bg=BG)
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1440, screen_width - 50)}x{min(980, screen_height - 80)}")
        root.minsize(min(1120, screen_width - 50), min(740, screen_height - 80))
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._style()
        self._build()
        self._mode_changed()
        if config_path:
            self.load_config(Path(config_path))
        self._sampler = sampler or MetricsSampler()
        self._metrics_thread = threading.Thread(target=self._sample_metrics, name="desktop-telemetry", daemon=True)
        self._metrics_thread.start()
        self._poll()
        root.bind("<Control-Return>", lambda _event: self._start())
        root.bind("<Escape>", lambda _event: self._cancel() if self.controller.active else None)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", font=(FONT, 10), background=CARD, foreground=TEXT,
                        bordercolor=LINE, padding=(12, 9), focusthickness=1, focuscolor=TEAL)
        style.map("TButton", background=[("active", "#263f55"), ("disabled", "#172337")],
                  foreground=[("disabled", "#586e86")])
        style.configure("Primary.TButton", background=TEAL, foreground=BG, font=(FONT, 11, "bold"))
        style.map("Primary.TButton", background=[("active", "#80efdd"), ("disabled", "#234b52")],
                  foreground=[("disabled", "#8aabaf")])
        style.configure("Stop.TButton", foreground=RED)
        style.configure("TCheckbutton", background=PANEL, foreground=MUTED, font=(FONT, 10))
        style.map("TCheckbutton", background=[("active", PANEL)], foreground=[("active", TEXT)])
        style.configure("Horizontal.TProgressbar", background=TEAL, troughcolor=LINE,
                        borderwidth=0, lightcolor=TEAL, darkcolor=TEAL)
        style.configure("Vertical.TScrollbar", background=CARD, troughcolor=FIELD, bordercolor=FIELD,
                        arrowcolor=MUTED)

    def _build(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=26, pady=(21, 15))
        brand = tk.Frame(header, bg=BG)
        brand.pack(side="left")
        _label(brand, "ephy", size=28, bold=True).pack(side="left")
        _label(brand, " /  WORKER CONSOLE", fg=MUTED, size=12).pack(side="left", padx=(4, 0), pady=(9, 0))
        self.mode_badge = _label(header, "LIVE WORKER", fg=TEAL, bg=CARD, size=10, bold=True, padx=13, pady=7)
        self.mode_badge.pack(side="right", padx=(12, 0))
        self.state_badge = _label(header, "● 待機中", fg=TEAL, size=12, bold=True)
        self.state_badge.pack(side="right")
        subheader = tk.Frame(self.root, bg=BG)
        subheader.pack(fill="x", padx=28, pady=(0, 15))
        _label(subheader, "入力から実行の様子まで，ひとつの画面で．", fg=MUTED, size=11).pack(side="left")
        karte_status = tk.Frame(subheader, bg=BG)
        karte_status.pack(side="right")
        self.karte_badge = _label(karte_status, karte_status_text({}), fg=MUTED, size=10)
        self.karte_badge.pack(anchor="e")
        _label(karte_status, KARTE_SCOPE, fg=MUTED, size=9).pack(anchor="e", pady=(3, 0))
        self.banner = _label(self.root, "", bg="#12323b", fg=TEAL, anchor="w", padx=16, pady=10)
        self.banner.pack(fill="x", padx=24, pady=(0, 14))
        self.panes = tk.PanedWindow(self.root, orient="horizontal", bg=BG, sashwidth=10,
                                   sashrelief="flat", bd=0, opaqueresize=True)
        self.panes.pack(fill="both", expand=True, padx=24)
        self.input_pane = tk.Frame(self.panes, bg=PANEL)
        metrics_panel = tk.Frame(self.panes, bg=PANEL)
        metrics_canvas = tk.Canvas(metrics_panel, bg=PANEL, highlightthickness=0, width=1)
        metrics_scrollbar = ttk.Scrollbar(metrics_panel, command=metrics_canvas.yview)
        metrics_scrollbar.pack(side="right", fill="y")
        metrics_canvas.pack(side="left", fill="both", expand=True)
        metrics_canvas.configure(yscrollcommand=metrics_scrollbar.set)
        self.metrics_pane = tk.Frame(metrics_canvas, bg=PANEL)
        metrics_window = metrics_canvas.create_window((0, 0), window=self.metrics_pane, anchor="nw")
        self.metrics_pane.bind("<Configure>", lambda _event: metrics_canvas.configure(
            scrollregion=metrics_canvas.bbox("all")))
        metrics_canvas.bind("<Configure>", lambda event: metrics_canvas.itemconfigure(metrics_window, width=event.width))
        self.output_pane = tk.Frame(self.panes, bg=PANEL)
        self.panes.add(self.input_pane, minsize=285, width=355, stretch="always")
        self.panes.add(metrics_panel, minsize=285, width=330, stretch="always")
        self.panes.add(self.output_pane, minsize=360, width=660, stretch="always")
        self._build_input()
        self._build_metrics()
        self._build_output()
        footer = tk.Frame(self.root, bg=BG)
        footer.pack(fill="x", padx=26, pady=(10, 13), side="bottom", before=self.panes)
        _label(footer, textvariable=self.notice, fg=MUTED, anchor="w", wraplength=1050).pack(side="left")
        _label(footer, "READ ONLY  /  v0.1", fg=MUTED, size=9).pack(side="right")

    def _heading(self, parent, number, title, subtitle):
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill="x", padx=18, pady=(17, 13))
        _label(frame, number, fg=TEAL, size=9, bold=True).pack(anchor="w")
        _label(frame, title, size=17, bold=True).pack(anchor="w", pady=(3, 3))
        _label(frame, subtitle, fg=MUTED, size=9).pack(anchor="w")

    def _build_input(self):
        parent = self.input_pane
        self._heading(parent, "01  /  INPUT", "タスクを送る", "CodingExecutor → Pi RPC")
        box = tk.Frame(parent, bg=CARD, padx=12, pady=11)
        box.pack(fill="x", padx=18)
        _label(box, "接続設定", fg=MUTED, size=9).pack(anchor="w")
        self.config_label = _label(box, "設定ファイル未選択", size=10, anchor="w", wraplength=280)
        self.config_label.pack(fill="x", pady=(4, 8))
        self.config_button = ttk.Button(box, text="JSON 設定を読み込む…", command=self._choose_config)
        self.config_button.pack(fill="x")
        self.config_summary = _label(box, "ローカル設定を明示して開始", fg=MUTED, size=9,
                                     anchor="w", justify="left", wraplength=280)
        self.config_summary.pack(fill="x", pady=(8, 0))
        self.timeout_label = _label(box, fg=MUTED, size=9, anchor="w", justify="left", wraplength=280)
        self.timeout_label.pack(fill="x", pady=(4, 0))
        # Reserve actions before allocating the remaining space to the editor.
        # This keeps cancel/reconnect reachable even at the minimum window size.
        bottom = tk.Frame(parent, bg=PANEL)
        bottom.pack(side="bottom", fill="x")
        _label(parent, "依頼内容", size=10, bold=True).pack(anchor="w", padx=18, pady=(17, 7))
        text_frame = tk.Frame(parent, bg=FIELD, highlightbackground=LINE, highlightthickness=1)
        text_frame.pack(fill="both", expand=True, padx=18)
        self.prompt = tk.Text(text_frame, wrap="word", height=9, width=1, bg=FIELD, fg=TEXT,
                              insertbackground=TEAL, selectbackground="#315d69", bd=0,
                              font=(FONT, 11), padx=12, pady=12, undo=True, spacing3=4)
        self.prompt.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(text_frame, command=self.prompt.yview)
        scroll.pack(side="right", fill="y")
        self.prompt.configure(yscrollcommand=scroll.set)
        self.prompt.insert("1.0", EXAMPLE_PROMPT)
        _label(bottom, "repository_navigation · read / governance_ack", fg=MUTED, size=8).pack(
            anchor="w", padx=18, pady=(6, 10))
        self.demo_toggle = ttk.Checkbutton(bottom, text="デモを使う（合成データ）",
                                           variable=self.demo, command=self._mode_changed)
        self.demo_toggle.pack(anchor="w", padx=15)
        actions = tk.Frame(bottom, bg=PANEL)
        actions.pack(fill="x", padx=18, pady=(12, 12))
        self.run_button = ttk.Button(actions, text="▶  実行する", style="Primary.TButton", command=self._start)
        self.run_button.pack(side="left", fill="x", expand=True)
        self.cancel_button = ttk.Button(actions, text="■  取消", style="Stop.TButton", command=self._cancel)
        self.cancel_button.pack(side="right", padx=(8, 0))
        connection = tk.Frame(bottom, bg=PANEL)
        connection.pack(fill="x", padx=18, pady=(0, 7))
        self.detach_button = ttk.Button(connection, text="表示を切断", command=self._disconnect)
        self.detach_button.pack(side="left", fill="x", expand=True)
        self.reconnect_button = ttk.Button(connection, text="再接続", command=self._reconnect)
        self.reconnect_button.pack(side="left", fill="x", expand=True, padx=(8, 0))
        _label(bottom, "表示切断中も実行は継続．再接続で同じ実行を表示．",
               fg=MUTED, size=8, wraplength=280, justify="left").pack(anchor="w", padx=18, pady=(0, 14))

    def _card(self, title):
        card = tk.Frame(self.metrics_pane, bg=CARD, padx=12, pady=10)
        card.pack(fill="x", padx=18, pady=(0, 10))
        _label(card, title, fg=MUTED, size=9, bold=True).pack(anchor="w", pady=(0, 5))
        return card

    def _metric(self, parent, key, title, *, size=11):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill="x", pady=2)
        _label(row, title, fg=MUTED, size=9).pack(side="left")
        var = tk.StringVar(self.root, value=UNAVAILABLE)
        self.values[key] = var
        _label(row, textvariable=var, size=size, bold=True).pack(side="right")

    def _build_metrics(self):
        self._heading(self.metrics_pane, "02  /  TELEMETRY", "使用率と実行統計", "取得元と単位を分けて表示")
        host = self._card("HOST  /  この Windows PC")
        self._metric(host, "host_cpu", "CPU", size=19)
        self.cpu_bar = ttk.Progressbar(host, mode="determinate", maximum=100)
        self.cpu_bar.pack(fill="x", pady=(5, 7))
        self._metric(host, "host_ram", "RAM")
        self._metric(host, "host_ram_percent", "使用率", size=10)
        proc = self._card("WORKER PROCESS  /  Pi")
        self._metric(proc, "worker_cpu", "CPU  ·  1 core = 100%")
        self._metric(proc, "worker_ram", "RSS")
        self._metric(proc, "pid", "PID", size=10)
        gpu = self._card("GPU  /  ホストのデバイス全体")
        self.gpu_name = _label(gpu, "デバイス情報を確認中", fg=MUTED, size=9,
                                anchor="w", justify="left", wraplength=260)
        self.gpu_name.pack(fill="x", pady=(0, 3))
        self._metric(gpu, "gpu", "GPU 使用率")
        self._metric(gpu, "vram", "VRAM")
        self.gpu_reason = _label(gpu, "", fg=MUTED, size=8, anchor="w", justify="left", wraplength=260)
        self.gpu_reason.pack(fill="x", pady=(4, 0))
        run = self._card("RUN  /  現在のタスク")
        grid = tk.Frame(run, bg=CARD)
        grid.pack(fill="x")
        for index, (key, title) in enumerate((
            ("elapsed", "経過時間"), ("input_tokens", "入力 tok"), ("output_tokens", "出力 tok"),
            ("rate", "生成速度"), ("turns", "Turns"), ("tools", "Tool calls"),
        )):
            tile = tk.Frame(grid, bg=CARD)
            tile.grid(row=index // 3, column=index % 3, sticky="nsew", pady=4)
            grid.columnconfigure(index % 3, weight=1, uniform="run")
            _label(tile, title, fg=MUTED, size=8).pack(anchor="w")
            self.values[key] = tk.StringVar(self.root, value=UNAVAILABLE)
            _label(tile, textvariable=self.values[key], size=10, bold=True).pack(anchor="w", pady=(3, 0))
        _label(run, "tok/s は provider の計測がある場合のみ．", fg=MUTED, size=8,
               wraplength=250, justify="left").pack(
            anchor="w", pady=(6, 0))
        self.sample_note = _label(self.metrics_pane, "— 未取得：未接続・未対応・初回サンプル", fg=MUTED,
                                  size=8, wraplength=290, justify="left", anchor="w")
        self.sample_note.pack(fill="x", padx=18, pady=(0, 12))

    def _build_output(self):
        self._heading(self.output_pane, "03  /  LIVE OUTPUT", "生成を見守る", "生成テキストとツールイベントを順次反映")
        controls = tk.Frame(self.output_pane, bg=PANEL)
        controls.pack(fill="x", padx=18, pady=(0, 8))
        self.run_label = _label(controls, "RUN  —", fg=MUTED, size=9)
        self.run_label.pack(side="left")
        ttk.Checkbutton(controls, text="自動スクロール", variable=self.follow).pack(side="right")
        panes = tk.PanedWindow(self.output_pane, orient="vertical", bg=PANEL, sashwidth=10, bd=0)
        panes.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        generation = tk.Frame(panes, bg=FIELD)
        events = tk.Frame(panes, bg=FIELD)
        panes.add(generation, minsize=155, stretch="always", height=345)
        panes.add(events, minsize=130, stretch="always")
        self.output = self._read_only_text(generation, font=(FONT, 11), height=12)
        self.output.tag_configure("placeholder", foreground=MUTED)
        self._write_text(self.output, "実行すると，ここに生成が表示されます．", "placeholder")
        log_header = tk.Frame(events, bg=CARD, padx=10, pady=7)
        log_header.pack(fill="x")
        _label(log_header, "EVENT STREAM", fg=TEAL, size=9, bold=True).pack(side="left")
        self.event_count = _label(log_header, "0 events", fg=MUTED, size=8)
        self.event_count.pack(side="right")
        self.event_log = self._read_only_text(events, font=(MONO, 9), height=8)
        self.event_log.tag_configure("log", foreground=MUTED)

    def _read_only_text(self, parent, *, font, height):
        text = tk.Text(parent, bg=FIELD, fg=TEXT, font=font, wrap="word", bd=0, width=1, height=height,
                       padx=14, pady=12, spacing3=4, selectbackground="#315d69", state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(parent, command=text.yview)
        scrollbar.pack(side="right", fill="y")
        text.configure(yscrollcommand=scrollbar.set)
        return text

    def _write_text(self, widget, text, tag=None, *, append=False):
        old_view = widget.yview()
        widget.configure(state="normal")
        if not append:
            widget.delete("1.0", "end")
        widget.insert("end", text, (tag,) if tag else ())
        widget.configure(state="disabled")
        if self.follow.get():
            widget.see("end")
        elif not append:
            widget.yview_moveto(old_view[0])

    def _choose_config(self):
        filename = filedialog.askopenfilename(parent=self.root, title="Worker のローカル設定",
                                              filetypes=[("JSON settings", "*.json")])
        if filename:
            self.load_config(Path(filename))

    def load_config(self, path: Path) -> bool:
        if self.controller.active or self._closing:
            return False
        try:
            config = DesktopConfig.from_file(path)
        except (OSError, ValueError, TypeError) as exc:
            self.config = None
            self.config_label.configure(text="設定を読み込めません", fg=RED)
            self.config_summary.configure(text="内容を確認して，再度読み込んでください．")
            self._refresh_timeout()
            self.notice.set(f"設定エラー: {exc}")
            return False
        self.config = config
        self.config_label.configure(text=path.name, fg=TEXT)
        self.config_summary.configure(text=f"{config.provider} / {config.model_id}\n{Path(config.repository_path).name}")
        self._refresh_timeout()
        self.notice.set("設定を読み込みました．実行ボタンで読み取り専用タスクを開始します．")
        return True

    def _refresh_timeout(self):
        if self.demo.get():
            text = "デモ実行制限：300 秒（固定）"
        else:
            value = f"{self.config.timeout_seconds} 秒" if self.config is not None else "未設定"
            text = f"実行制限：{value}（上限 {MAX_TIMEOUT_SECONDS} 秒）"
        self.timeout_label.configure(text=text)

    def _mode_changed(self):
        demo = self.demo.get()
        self.mode_badge.configure(text="SYNTHETIC DEMO" if demo else "LIVE WORKER", fg=AMBER if demo else TEAL)
        self.banner.configure(
            text=("DEMO  /  タスク・生成は合成データ．モデル呼出しなし．ホスト使用率は実測．" if demo else
                  "READ ONLY  /  既存 worker で実行するリポジトリ調査．ローカル設定を読み込んで開始．"),
            fg=AMBER if demo else TEAL, bg="#332a20" if demo else "#12323b",
        )
        self.run_button.configure(text="▶  デモを実行" if demo else "▶  実行する")
        self._refresh_timeout()

    def _start(self) -> bool:
        if self._submitting or self.controller.active or self._closing:
            return False
        prompt = self.prompt.get("1.0", "end-1c").strip()
        if not prompt:
            self.notice.set("依頼内容を入力してください．")
            self.prompt.focus_set()
            return False
        if not self.demo.get() and self.config is None:
            self.notice.set("実接続には JSON 設定が必要です．設定ファイルを読み込んでください．")
            return False
        self._submitting = True
        self.run_button.state(["disabled"])
        try:
            config = DesktopConfig(repository_path=Path.cwd(), artifact_root=self.demo_artifact_root) if self.demo.get() else self.config
            started = self.controller.start(config, prompt, demo=self.demo.get())
        except (OSError, ValueError, RuntimeError) as exc:
            self.notice.set(f"開始できません: {type(exc).__name__}: {exc}")
            return False
        finally:
            self._submitting = False
        if started:
            self.notice.set("合成タスクを開始しました．" if self.demo.get() else "worker の開始を待っています．")
        else:
            self.notice.set(str(self.controller.snapshot().get("error") or "タスクを開始できませんでした．"))
        self._render(self.controller.snapshot())
        return started

    def _cancel(self):
        if self.controller.active:
            self.controller.cancel()
            self.notice.set("取消を要求しました．worker の終了と片付けを待っています．")
            self.cancel_button.state(["disabled"])

    def _disconnect(self):
        self.controller.disconnect()
        self.notice.set("表示を切断しました．実行は継続しています．")
        self._render(self.controller.snapshot())

    def _reconnect(self):
        self.controller.reconnect()
        self.notice.set("同じ実行へ再接続しています．タスクの再送信は行いません．")
        self._render(self.controller.snapshot())

    def _sample_metrics(self):
        while not self._metrics_stop.is_set():
            try:
                metrics = self._sampler.sample(pid=self._sample_pid)
            except (OSError, ValueError, RuntimeError) as exc:
                metrics = {"unavailable_reasons": {"sampler": f"{type(exc).__name__}: {exc}"}}
            try:
                self._metrics_queue.put_nowait(metrics)
            except queue.Full:
                try:
                    self._metrics_queue.get_nowait()
                except queue.Empty:
                    pass
                self._metrics_queue.put_nowait(metrics)
            self._metrics_stop.wait(1.0)

    def _poll(self):
        if self._destroyed:
            return
        self.controller.drain()
        snapshot = self.controller.snapshot()
        self._last_snapshot = snapshot
        self._sample_pid = snapshot.get("worker_pid") if self.controller.active else None
        self._render(snapshot)
        try:
            self._render_metrics(self._metrics_queue.get_nowait())
        except queue.Empty:
            pass
        if self._closing and not self.controller.active and self.controller.close():
            self._destroy()
            return
        self.root.after(80, self._poll)

    def _render(self, snapshot):
        state = snapshot.get("state", "idle")
        execution_state = snapshot.get("execution_state", state)
        active = self.controller.active
        color = RED if state == "failed" else AMBER if state in {"stopping", "cancelling", "disconnected"} else TEAL
        state_text = STATE_LABELS.get(state, state)
        if state in {"disconnected", "reconnecting"}:
            state_text += f" / {STATE_LABELS.get(execution_state, execution_state)}"
        self.state_badge.configure(text=f"● {state_text}", fg=color)
        karte = snapshot.get("karte") or {}
        karte_state = karte.get("state", "off")
        self.karte_badge.configure(text=karte_status_text(karte), fg=TEAL if karte_state == "saved" else MUTED)
        run_id = snapshot.get("run_id")
        if run_id != self._last_run_id:
            self._displayed_text = ""
            self._displayed_events = ""
            self._write_text(self.output, "")
            self._write_text(self.event_log, "")
            self._last_run_id = run_id
        self.run_label.configure(text=f"RUN  {str(run_id)[-16:] if run_id else '—'}")
        connected = snapshot.get("connection", "connected") == "connected"
        if connected:
            text = snapshot.get("text") or ""
            if text != self._displayed_text:
                append = text.startswith(self._displayed_text) and bool(self._displayed_text)
                self._write_text(self.output, text[len(self._displayed_text):] if append else text, append=append)
                self._displayed_text = text
            events = snapshot.get("events") or []
            rendered = "\n".join(event_line(event) for event in events[-200:])
            if rendered != self._displayed_events:
                self._write_text(self.event_log, rendered, "log")
                self._displayed_events = rendered
                self.event_count.configure(text=f"{len(events)} events")
        for key, value in {
            "elapsed": format_elapsed(snapshot.get("elapsed_seconds")),
            "input_tokens": format_number(snapshot.get("input_tokens")),
            "output_tokens": format_number(snapshot.get("output_tokens")),
            "rate": format_number(snapshot.get("tokens_per_second"), " tok/s", 1),
            "turns": format_number(snapshot.get("turns")),
            "tools": format_number(snapshot.get("tool_calls")),
        }.items():
            self.values[key].set(value)
        locked = active or self._submitting or self._closing
        self.run_button.state(["disabled"] if locked else ["!disabled"])
        self.config_button.state(["disabled"] if locked else ["!disabled"])
        self.demo_toggle.state(["disabled"] if locked else ["!disabled"])
        self.prompt.configure(state="disabled" if locked else "normal")
        self.cancel_button.state(["!disabled"] if active and execution_state not in {"stopping", "cancelling"} else ["disabled"])
        self.detach_button.state(["!disabled"] if connected and run_id and not self._closing else ["disabled"])
        self.reconnect_button.state(["disabled"] if connected or self._closing else ["!disabled"])
        if snapshot.get("error"):
            self.notice.set(f"実行エラー: {snapshot['error']}")
        elif self._closing:
            self.notice.set("停止して終了します．worker の後処理が完了するまでお待ちください．")
        elif state in {"completed", "cancelled"} and not active and state != self._previous_state:
            self.notice.set("完了しました．生成とイベントを確認できます．" if state == "completed" else
                            "取消が完了しました．worker の後処理も終了しています．")
        self._previous_state = state

    def _render_metrics(self, metrics):
        cpu = metrics.get("host_cpu_percent")
        self.values["host_cpu"].set(format_number(cpu, "%", 1))
        self.cpu_bar.configure(value=cpu if isinstance(cpu, (int, float)) and math.isfinite(cpu) else 0)
        used, total = metrics.get("host_ram_used_bytes"), metrics.get("host_ram_total_bytes")
        self.values["host_ram"].set(f"{format_bytes(used)} / {format_bytes(total)}")
        self.values["host_ram_percent"].set(format_number(metrics.get("host_ram_percent"), "%", 1))
        self.values["worker_cpu"].set(format_number(metrics.get("worker_process_cpu_percent"), "%", 1))
        self.values["worker_ram"].set(format_bytes(metrics.get("worker_process_rss_bytes")))
        self.values["pid"].set(format_number(metrics.get("worker_process_pid")))
        gpus = metrics.get("gpu") or []
        if gpus:
            self.gpu_name.configure(text="\n".join(f"GPU {g['index']}  {g['name']}" for g in gpus))
            self.values["gpu"].set(" / ".join(format_number(g.get("utilization_percent"), "%", 0) for g in gpus))
            self.values["vram"].set(" / ".join(
                f"{format_bytes(g.get('vram_used_bytes'))} of {format_bytes(g.get('vram_total_bytes'))}" for g in gpus
            ))
            self.gpu_reason.configure(text="ホスト全体の値．worker 専用使用量ではありません．")
        else:
            reasons = metrics.get("unavailable_reasons") or {}
            self.gpu_name.configure(text="GPU 情報なし")
            self.values["gpu"].set(UNAVAILABLE)
            self.values["vram"].set(UNAVAILABLE)
            self.gpu_reason.configure(text=str(reasons.get("gpu") or "対応する計測元が利用できません．")[:180])
        if "sampler" in (metrics.get("unavailable_reasons") or {}):
            self.sample_note.configure(text=f"計測エラー: {metrics['unavailable_reasons']['sampler']}")

    def _on_close(self, *, confirm=True):
        if self._closing:
            return
        if self.controller.active and confirm and not messagebox.askyesno(
            "実行中のタスク", "タスクを停止し，後処理が終わってから閉じますか？", parent=self.root, default="no"
        ):
            return
        self._closing = True
        if self.controller.close():
            self._destroy()
        else:
            self.notice.set("停止して終了します．worker の後処理を待っています．")

    def _destroy(self):
        self._destroyed = True
        self._metrics_stop.set()
        self.root.destroy()


def capture_window(root: tk.Tk, path: Path) -> None:
    """Capture this window's client surface only, never the user's desktop."""
    if os.name != "nt":
        raise RuntimeError("Window screenshot is supported on Windows only")
    from ctypes import wintypes

    previous_topmost = root.attributes("-topmost")
    root.attributes("-topmost", True)
    root.lift()
    root.update()
    ctypes.windll.dwmapi.DwmFlush()
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetDC.argtypes, user32.GetDC.restype = [wintypes.HWND], wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.argtypes, gdi32.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes, gdi32.SelectObject.restype = [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    hwnd = root.winfo_id()
    width, height = root.winfo_width(), root.winfo_height()
    source = user32.GetDC(hwnd)
    memory = gdi32.CreateCompatibleDC(source)
    bitmap = gdi32.CreateCompatibleBitmap(source, width, height)
    previous = gdi32.SelectObject(memory, bitmap)
    try:
        # Tk's custom widgets do not implement PrintWindow completely. Read only
        # this opaque, topmost window's client DC after composition; no desktop DC
        # or rectangle outside the application's client surface is requested.
        if not gdi32.BitBlt(memory, 0, 0, width, height, source, 0, 0, 0x00CC0020):
            raise OSError("Own-window client capture failed")
        info = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        gdi32.SelectObject(memory, previous)
        if not gdi32.GetDIBits(memory, bitmap, 0, height, pixels, ctypes.create_string_buffer(info), 0):
            raise OSError("GetDIBits failed")
        raw = pixels.raw
        rows = bytearray()
        for y in range(height):
            rows.append(0)
            for x in range(width):
                offset = (y * width + x) * 4
                rows.extend((raw[offset + 2], raw[offset + 1], raw[offset]))

        def chunk(kind, body):
            return struct.pack("!I", len(body)) + kind + body + struct.pack("!I", zlib.crc32(kind + body))

        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b"")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
    finally:
        gdi32.SelectObject(memory, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(hwnd, source)
        root.attributes("-topmost", previous_topmost)


class GuiSmoke:
    """Exercise real Tk callbacks with the explicit synthetic worker adapter."""

    def __init__(self, app, evidence: Path, screenshot: Path | None):
        self.app, self.evidence, self.screenshot = app, evidence, screenshot
        self.results = {"mode": "synthetic_gui", "model_called": False, "checks": {}}
        self.stage = 0
        self.started = time.monotonic()
        self.run_id = None
        app.root.after(300, self.tick)

    def tick(self):
        app = self.app
        try:
            if time.monotonic() - self.started > 40:
                raise TimeoutError("GUI smoke exceeded 40 seconds")
            snapshot = app.controller.snapshot()
            checks = self.results["checks"]
            if self.stage == 2 and app.controller.active and app._displayed_text:
                checks["live_rendered_before_completion"] = bool(app.output.get("1.0", "end-1c"))
            if self.stage == 0:
                app.prompt.delete("1.0", "end")
                app.prompt.insert("1.0", EXAMPLE_PROMPT)
                checks["multiline_input"] = "\n" in app.prompt.get("1.0", "end-1c")
                checks["start"] = app._start()
                checks["run_button_locked"] = app.run_button.instate(["disabled"])
                self.run_id = app.controller.snapshot().get("run_id")
                checks["duplicate_rejected"] = not app._start()
                app._disconnect()
                checks["detached"] = app.controller.snapshot().get("connection") != "connected"
                self.stage = 1
            elif self.stage == 1:
                app._reconnect()
                self.stage = 2
            elif self.stage == 2 and not app.controller.active:
                checks["same_run_after_reconnect"] = snapshot.get("run_id") == self.run_id
                checks["completed"] = snapshot.get("execution_state", snapshot.get("state")) == "completed"
                checks["streamed_text"] = bool(snapshot.get("text"))
                checks["streamed_events"] = bool(snapshot.get("events"))
                checks.setdefault("live_rendered_before_completion", False)
                app._render(snapshot)
                previous_geometry = app.root.geometry()
                app.root.geometry("1120x740")
                app.root.update()
                client_bottom = app.root.winfo_rooty() + app.root.winfo_height()
                checks["minimum_size_actions_visible"] = all(
                    widget.winfo_viewable() and widget.winfo_rooty() + widget.winfo_height() <= client_bottom
                    for widget in (app.run_button, app.cancel_button, app.detach_button, app.reconnect_button)
                )
                app.root.geometry(previous_geometry)
                app.root.update()
                if self.screenshot:
                    capture_window(app.root, self.screenshot)
                    checks["own_window_screenshot"] = self.screenshot.exists()
                checks["second_start"] = app._start()
                app._cancel()
                self.stage = 3
            elif self.stage == 3 and not app.controller.active:
                checks["cancelled"] = snapshot.get("execution_state", snapshot.get("state")) == "cancelled"
                checks["close_test_start"] = app._start()
                app._on_close(confirm=False)
                checks["close_waits_for_worker"] = app._closing and not app._destroyed
                self.stage = 4
            elif self.stage == 4:
                if not app.controller.active:
                    checks["worker_stopped_on_close"] = True
                    self.finish()
                    return
        except Exception as exc:  # noqa: BLE001 -- smoke must persist unexpected failures as failed evidence.
            self.results["error"] = f"{type(exc).__name__}: {exc}"
            self.finish()
            return
        if not app._destroyed:
            app.root.after(100, self.tick)

    def finish(self):
        self.results["passed"] = not self.results.get("error") and all(self.results["checks"].values())
        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        self.evidence.write_text(json.dumps(self.results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not self.app._destroyed:
            self.app._on_close(confirm=False)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ephy-worker desktop console")
    parser.add_argument("--config", type=Path, help="Explicit local JSON settings")
    parser.add_argument("--demo", action="store_true", help="Use synthetic task adapter; no model calls")
    parser.add_argument("--smoke-test", type=Path, metavar="JSON", help="Run synthetic GUI self-test and save evidence")
    parser.add_argument("--screenshot", type=Path, metavar="PNG", help="Capture only the synthetic self-test window")
    args = parser.parse_args(argv)
    if args.screenshot and not args.smoke_test:
        parser.error("--screenshot requires --smoke-test to avoid capturing private task content")
    if args.smoke_test and args.config:
        parser.error("--smoke-test cannot load a private --config")
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass  # Older Windows versions use Tk's default DPI handling.
    root = tk.Tk()
    app = WorkerConsole(root, demo=args.demo or bool(args.smoke_test), config_path=args.config,
                        demo_artifact_root=args.smoke_test.parent / "gui-smoke-runs" if args.smoke_test else None)
    smoke = GuiSmoke(app, args.smoke_test, args.screenshot) if args.smoke_test else None
    root.mainloop()
    if smoke:
        if "passed" not in smoke.results:
            smoke.results["checks"]["worker_stopped_on_close"] = not app.controller.active
            smoke.results["checks"]["all_smoke_stages_reached"] = smoke.stage == 4
            smoke.finish()
        return 0 if smoke.results.get("passed") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
