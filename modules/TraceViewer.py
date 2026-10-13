"""Tk trace timeline for validated observation-only SDK traces."""

from __future__ import annotations

from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any

from tibia_client.trace_view import (
    DEFAULT_PRESENTATION_LIMIT,
    TraceDocument,
    event_detail,
    export_session_trace,
    filter_events,
    load_trace_document,
    session_trace_document,
)


class TraceViewer:
    """Inspect a trace file or a snapshot of a session's in-memory events.

    ``parent`` may be a Tk widget or the project's small ``GUI`` wrapper.  A
    standalone Tk root is created when it is ``None``.  Trace payloads are only
    rendered as text; payload strings are never treated as paths or commands.
    """

    def __init__(self, parent=None, trace_path=None, session=None):
        self.session = session
        self.document: TraceDocument | None = None
        self._rows = {}
        self._load_results: queue.SimpleQueue = queue.SimpleQueue()
        self._load_serial = 0
        master = getattr(parent, "windowID", parent)
        if master is None:
            self.window = tk.Tk()
            self._owns_root = True
        else:
            self.window = tk.Toplevel(master)
            self._owns_root = False
        self.window.title("Trace Viewer")
        self.window.geometry("1120x720")
        self.window.minsize(800, 520)

        self.kind_var = tk.StringVar(master=self.window)
        self.owner_var = tk.StringVar(master=self.window)
        self.request_var = tk.StringVar(master=self.window)
        self.source_var = tk.StringVar(master=self.window, value="No trace loaded")
        self.stats_var = tk.StringVar(master=self.window, value="0 events")
        self.status_var = tk.StringVar(master=self.window, value="Open a trace to inspect it.")
        self._build()
        self.window.after(50, self._poll_load)

        if trace_path is not None:
            self.open_trace(trace_path)
        elif session is not None:
            self.refresh_session()

    def _build(self) -> None:
        outer = ttk.Frame(self.window, padding=10)
        outer.pack(fill="both", expand=True)

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Open trace...", command=self.choose_trace).pack(side="left")
        if self.session is not None:
            ttk.Button(toolbar, text="Refresh session", command=self.refresh_session).pack(
                side="left", padx=(6, 0)
            )
            ttk.Button(toolbar, text="Export session...", command=self.export_session).pack(
                side="left", padx=(6, 0)
            )
        ttk.Label(toolbar, textvariable=self.source_var).pack(side="left", padx=12)
        ttk.Label(toolbar, textvariable=self.stats_var).pack(side="right")

        filters = ttk.LabelFrame(outer, text="Filters", padding=7)
        filters.pack(fill="x", pady=(8, 8))
        ttk.Label(filters, text="Kind").grid(row=0, column=0, sticky="w")
        self.kind_box = ttk.Combobox(filters, textvariable=self.kind_var, width=16)
        self.kind_box.grid(row=0, column=1, padx=(5, 14), sticky="ew")
        ttk.Label(filters, text="Owner").grid(row=0, column=2, sticky="w")
        self.owner_box = ttk.Combobox(filters, textvariable=self.owner_var, width=22)
        self.owner_box.grid(row=0, column=3, padx=(5, 14), sticky="ew")
        ttk.Label(filters, text="Request / key / state").grid(row=0, column=4, sticky="w")
        request_entry = ttk.Entry(filters, textvariable=self.request_var, width=28)
        request_entry.grid(row=0, column=5, padx=(5, 8), sticky="ew")
        ttk.Button(filters, text="Apply", command=self.apply_filters).grid(row=0, column=6)
        ttk.Button(filters, text="Clear", command=self.clear_filters).grid(
            row=0, column=7, padx=(5, 0)
        )
        filters.columnconfigure(5, weight=1)
        for widget in (self.kind_box, self.owner_box, request_entry):
            widget.bind("<Return>", lambda _event: self.apply_filters())

        pane = ttk.Panedwindow(outer, orient="vertical")
        pane.pack(fill="both", expand=True)
        timeline_frame = ttk.Frame(pane)
        detail_frame = ttk.LabelFrame(pane, text="Selected event")
        pane.add(timeline_frame, weight=3)
        pane.add(detail_frame, weight=2)

        columns = ("sequence", "elapsed", "kind", "owner", "request", "summary")
        self.timeline = ttk.Treeview(
            timeline_frame, columns=columns, show="headings", selectmode="browse"
        )
        headings = {
            "sequence": "#",
            "elapsed": "Elapsed",
            "kind": "Kind",
            "owner": "Owner",
            "request": "Request",
            "summary": "Summary",
        }
        widths = {
            "sequence": 65,
            "elapsed": 85,
            "kind": 100,
            "owner": 130,
            "request": 210,
            "summary": 430,
        }
        for column in columns:
            self.timeline.heading(column, text=headings[column])
            self.timeline.column(
                column,
                width=widths[column],
                minwidth=45,
                stretch=column in ("owner", "request", "summary"),
            )
        yscroll = ttk.Scrollbar(timeline_frame, orient="vertical", command=self.timeline.yview)
        xscroll = ttk.Scrollbar(timeline_frame, orient="horizontal", command=self.timeline.xview)
        self.timeline.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.timeline.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        timeline_frame.rowconfigure(0, weight=1)
        timeline_frame.columnconfigure(0, weight=1)
        self.timeline.bind("<<TreeviewSelect>>", self._select_event)

        self.detail = tk.Text(
            detail_frame, wrap="none", height=12, padx=6, pady=6, state="disabled"
        )
        detail_y = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail.yview)
        detail_x = ttk.Scrollbar(detail_frame, orient="horizontal", command=self.detail.xview)
        self.detail.configure(yscrollcommand=detail_y.set, xscrollcommand=detail_x.set)
        self.detail.grid(row=0, column=0, sticky="nsew")
        detail_y.grid(row=0, column=1, sticky="ns")
        detail_x.grid(row=1, column=0, sticky="ew")
        detail_frame.rowconfigure(0, weight=1)
        detail_frame.columnconfigure(0, weight=1)

        ttk.Label(outer, textvariable=self.status_var, anchor="w").pack(
            fill="x", pady=(7, 0)
        )

    def run(self) -> None:
        """Run the standalone event loop; embedded callers keep their own loop."""
        if self._owns_root:
            self.window.mainloop()

    def choose_trace(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.window,
            title="Open trace",
            filetypes=(("Tibia trace", "*.jsonl"), ("All files", "*")),
        )
        if path:
            self.open_trace(path)

    def open_trace(self, path: str | Path) -> bool:
        """Start bounded validation off the Tk event thread."""
        self._load_serial += 1
        serial = self._load_serial
        selected = Path(path)
        self.status_var.set(f"Loading and validating {selected.name}...")
        threading.Thread(
            target=self._load_trace,
            args=(serial, selected),
            name="trace-viewer-loader",
            daemon=True,
        ).start()
        return True

    def _load_trace(self, serial: int, path: Path) -> None:
        try:
            document = load_trace_document(path)
            result = (serial, path, document, None)
        except (OSError, ValueError) as exc:
            result = (serial, path, None, exc)
        self._load_results.put(result)

    def _poll_load(self) -> None:
        try:
            while True:
                serial, path, document, error = self._load_results.get_nowait()
                if serial != self._load_serial:
                    continue
                if error is not None:
                    self.status_var.set(f"Could not open trace: {error}")
                else:
                    self._set_document(document, f"Trace: {path.name}")
        except queue.Empty:
            pass
        try:
            self.window.after(50, self._poll_load)
        except tk.TclError:
            pass

    def refresh_session(self) -> bool:
        if self.session is None:
            self.status_var.set("No session was provided.")
            return False
        # This explicit selection supersedes any older file load still running.
        self._load_serial += 1
        try:
            document = session_trace_document(self.session)
        except (OSError, ValueError, TypeError) as exc:
            self.status_var.set(f"Could not read session history: {exc}")
            return False
        self._set_document(document, "Live session history (snapshot)")
        return True

    def export_session(self) -> bool:
        if self.session is None:
            self.status_var.set("No session was provided.")
            return False
        path = filedialog.asksaveasfilename(
            parent=self.window,
            title="Export complete session trace",
            defaultextension=".jsonl",
            filetypes=(("Tibia trace", "*.jsonl"), ("All files", "*")),
        )
        if not path:
            return False
        try:
            count = export_session_trace(self.session, path)
        except (OSError, ValueError, TypeError) as exc:
            self.status_var.set(f"Could not export session: {exc}")
            return False
        self.status_var.set(f"Exported {count} events to {Path(path).name}.")
        return True

    def _set_document(self, document: TraceDocument, label: str) -> None:
        self.document = document
        self.source_var.set(label)
        stats = document.statistics
        self.stats_var.set(
            f"{stats.total_events} events  |  {stats.duration:.3f}s  |  "
            f"{stats.requests} requests  |  {stats.dispatches} dispatches  |  "
            f"{stats.failures} failures"
        )
        self.kind_box.configure(values=("", *document.kinds))
        self.owner_box.configure(values=("", *document.owners))
        self.apply_filters()

    def clear_filters(self) -> None:
        self.kind_var.set("")
        self.owner_var.set("")
        self.request_var.set("")
        self.apply_filters()

    def apply_filters(self) -> None:
        children = self.timeline.get_children()
        if children:
            self.timeline.delete(*children)
        self._rows.clear()
        self._show_detail("")
        if self.document is None:
            return
        try:
            filtered = filter_events(
                self.document.events,
                kind=self.kind_var.get(),
                owner=self.owner_var.get(),
                request=self.request_var.get(),
                limit=DEFAULT_PRESENTATION_LIMIT,
            )
        except ValueError as exc:
            self.status_var.set(f"Invalid filter: {exc}")
            return
        for row in filtered.rows:
            identity = str(row.event.sequence)
            self._rows[identity] = row.event
            self.timeline.insert(
                "",
                "end",
                iid=identity,
                values=(
                    row.event.sequence,
                    f"{row.elapsed:.3f}s",
                    row.event.kind,
                    row.event.owner,
                    row.request,
                    row.summary,
                ),
            )
        shown = len(filtered.rows)
        suffix = f" (first {shown} shown)" if filtered.truncated else ""
        self.status_var.set(f"{filtered.matched} matching event(s){suffix}.")

    def _select_event(self, _tk_event: Any = None) -> None:
        selected = self.timeline.selection()
        event = self._rows.get(selected[0]) if selected else None
        self._show_detail(event_detail(event) if event is not None else "")

    def _show_detail(self, value: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if value:
            self.detail.insert("1.0", value)
        self.detail.configure(state="disabled")


__all__ = ["TraceViewer"]
