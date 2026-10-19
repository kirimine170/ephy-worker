"""Read-only viewer request ownership, pagination, and inert rendering contracts."""

import threading
from unittest.mock import Mock

import pytest

from ephy_worker.desktop.inspection import InspectionError
from ephy_worker.desktop.records import InspectionRequest, RecordsModel, RecordsWindow


@pytest.fixture
def deferred(monkeypatch):
    pending = []

    def thread(*, target, **_kwargs):
        pending.append(target)
        return Mock()

    monkeypatch.setattr("ephy_worker.desktop.records.threading.Thread", thread)
    return pending


def record(task_id="task-one"):
    return {"task_id": task_id, "title": f"Task {task_id}", "status": "failed",
            "last_recorded_at": "2026-10-10T00:00:00Z", "event_count": 23}


def page(task_id="task-one", offset=0, next_offset=10):
    return {"task": record(task_id), "objective": "Synthetic", "repository": "synthetic",
            "base_revision": "HEAD", "events": [{"sequence": n + 1, "text": f"event-{n + 1}"}
                                                 for n in range(offset, min(offset + 10, 23))],
            "next_offset": next_offset}


def finish(model, deferred):
    deferred.pop(0)()
    assert model.poll()


def loaded_model(deferred):
    # A strict reader exposes only official search/read; unexpected mutation calls fail immediately.
    reader = Mock(spec_set=["search", "read"])
    reader.search.return_value = [record(), record("task-two")]
    model = RecordsModel(None, reader=reader)
    assert model.refresh()
    finish(model, deferred)
    return model, reader


def test_io_runs_on_background_thread_and_requires_main_thread_poll():
    main_thread = threading.get_ident()
    done = threading.Event()
    identities = []
    request = InspectionRequest()

    def operation():
        identities.append(threading.get_ident())
        done.set()
        return "value"

    assert request.submit(operation)
    assert done.wait(2)
    # Completion never changes UI-facing busy until the main thread polls.
    assert request.busy
    result = request.poll()
    if result is None:  # The operation signals before the queue write; wait for its bounded thread to finish.
        for thread in threading.enumerate():
            if thread.name == "desktop-readonly-inspection":
                thread.join(2)
        result = request.poll()
    assert result.value == "value"
    assert identities != [main_thread]
    assert not request.busy


def test_invalidated_request_keeps_slot_until_completion_and_discards_stale_result(deferred):
    request = InspectionRequest()
    old, new = Mock(return_value="old"), Mock(return_value="new")
    assert request.submit(old)
    assert not request.submit(old)
    request.invalidate()
    assert not request.submit(new)
    deferred.pop(0)()
    assert request.poll() is None
    assert request.submit(new)
    assert request.poll() is None
    assert request.busy
    deferred.pop(0)()
    assert request.poll().value == "new"
    old.assert_called_once()
    new.assert_called_once()


def test_search_is_bounded_display_and_clears_stale_rows_on_error(deferred):
    model, reader = loaded_model(deferred)
    reader.search.return_value = [record(f"task-{n}") for n in range(20)]
    assert model.refresh()
    assert model.records == []
    assert not model.refresh()
    finish(model, deferred)
    assert len(model.records) == 20
    assert "全履歴" in model.message
    reader.search.side_effect = InspectionError("karte_timeout")
    assert model.refresh()
    finish(model, deferred)
    assert model.records == []
    assert model.page is None
    assert "karte_timeout" in model.message
    assert reader.search.call_count == 3


def test_pages_are_explicit_single_flight_and_reset_on_task_switch(deferred):
    model, reader = loaded_model(deferred)
    reader.read.side_effect = lambda task_id, offset: page(task_id, offset, offset + 10 if offset < 20 else -1)
    assert not model.select("not-in-search")
    assert model.select("task-one")
    assert not model.select("task-two")
    assert not model.next_page()
    finish(model, deferred)
    assert len(model.page["events"]) == 10
    assert not model.previous_page()
    assert model.next_page()
    assert model.page is None
    assert not model.next_page()
    finish(model, deferred)
    assert model.offset == 10
    assert model.previous_offsets == [0]
    assert model.next_page()
    finish(model, deferred)
    assert model.offset == 20
    assert len(model.page["events"]) == 3
    assert not model.next_page()
    assert model.previous_page()
    finish(model, deferred)
    assert model.offset == 10
    assert model.select("task-two")
    finish(model, deferred)
    assert model.task_id == "task-two"
    assert model.offset == 0
    assert model.previous_offsets == []
    assert reader.read.call_args_list[1].kwargs == {"offset": 10}


@pytest.mark.parametrize("code", ["karte_cli_missing", "karte_timeout", "karte_output_limit", "karte_cli_error",
                                 "karte_invalid_response", "karte_task_mismatch", "karte_invalid_page"])
def test_read_error_discards_previous_detail_and_navigation(deferred, code):
    model, reader = loaded_model(deferred)
    reader.read.return_value = page()
    assert model.select("task-one")
    finish(model, deferred)
    reader.read.side_effect = InspectionError(code)
    assert model.next_page()
    finish(model, deferred)
    assert model.page is None
    assert model.task_id is None
    assert model.previous_offsets == []
    assert not model.next_page()
    assert not model.previous_page()
    assert code in model.message
    assert len(model.records) == 2


@pytest.mark.parametrize("operation", ["search", "read"])
def test_close_and_reopen_rejects_old_window_completion(deferred, operation):
    model, reader = loaded_model(deferred)
    reader.read.return_value = page()
    assert model.refresh() if operation == "search" else model.select("task-one")
    model.close()
    reopened = RecordsModel(None, reader=reader)
    assert reopened.refresh()
    deferred.pop(0)()
    assert not model.poll()
    assert model.records == []
    assert model.page is None
    assert model.previous_offsets == []
    assert not model.refresh()
    assert reopened.request.busy
    assert reopened.records == []
    finish(reopened, deferred)
    assert len(reopened.records) == 2


def test_detail_renders_untrusted_links_html_and_paths_as_disabled_plain_text(deferred):
    model, reader = loaded_model(deferred)
    untrusted = '<script>alert(1)</script> https://example.invalid file:///private $(command)'
    detail = page()
    detail["events"][0]["text"] = untrusted
    reader.read.return_value = detail
    assert model.select("task-one")
    finish(model, deferred)
    window = RecordsWindow.__new__(RecordsWindow)
    window.model = model
    for name in ("table", "status", "detail", "refresh_button", "previous_button", "next_button",
                 "view_status", "view_url_value", "view_button", "view_identity"):
        setattr(window, name, Mock())
    window._render()
    rendered = window.detail.insert.call_args.args[1]
    assert untrusted in rendered
    window.detail.configure.assert_called_with(state="disabled")
    window.detail.bind.assert_not_called()
    window.table.configure.assert_called_with(selectmode="browse")
    assert model.next_page()
    window._render()
    window.table.configure.assert_called_with(selectmode="none")
    window.detail.delete.assert_called_with("1.0", "end")
    assert window.detail.insert.call_count == 1


def test_window_closure_stops_future_widget_updates(deferred):
    model, _reader = loaded_model(deferred)
    assert model.refresh()
    window = RecordsWindow.__new__(RecordsWindow)
    window.model = model
    window.window = Mock()
    window._on_close = Mock()
    window._closed = False
    window.close()
    deferred.pop(0)()
    window._poll()
    window.close()
    window.window.after.assert_not_called()
    window.window.destroy.assert_called_once()
    window._on_close.assert_called_once()


def render_result(task_id="task-one"):
    return {
        "schema_version": 1, "ok": True, "task_id": task_id,
        "doc_id": "f585b7a9-e749-4c0c-a142-e7e20d4ed61d",
        "relative_path": f"content/projects/synthetic/note/worklog-{task_id}.md",
        "source_sha256": "a" * 64, "markdown_sha256": "b" * 64,
        "renderer_commit": "30e5dfbab03f92e2eb7c8a67e17936579075fe88",
        "html": "<p>Synthetic derived document</p>", "metadata": {},
    }


def view_model(deferred):
    reader = Mock(spec_set=["search", "read"])
    reader.search.return_value = [record(), record("task-two")]
    reader.read.side_effect = lambda task_id, offset: page(task_id, offset, offset + 10 if offset < 20 else -1)
    renderer = Mock(spec_set=["render"])
    renderer.render.side_effect = render_result
    session = Mock(spec_set=["start", "close", "active"])
    session.start.return_value = "http://127.0.0.1:49152/" + "c" * 64 + "/"
    session.active = True
    factory = Mock(return_value=session)
    model = RecordsModel(None, reader=reader, renderer=renderer, session_factory=factory, clock=lambda: 100)
    assert model.refresh()
    finish(model, deferred)
    assert model.select("task-one")
    finish(model, deferred)
    return model, renderer, factory, session


def test_view_is_explicit_authorized_off_thread_before_url_and_browser_rereads(deferred):
    model, renderer, factory, session = view_model(deferred)
    original_page = model.page
    renderer.render.assert_not_called()
    factory.assert_not_called()
    assert model.create_view()
    assert model.view_url == ""
    assert model.view_identity is None
    renderer.render.assert_not_called()
    assert not model.create_view()
    finish(model, deferred)
    renderer.render.assert_called_once_with("task-one")
    factory.assert_called_once()
    assert factory.call_args.kwargs == {"ttl_seconds": 300}
    session.start.assert_called_once()
    assert model.view_url == session.start.return_value
    assert model.page is original_page
    assert "html" not in model.view_identity
    assert "metadata" not in model.view_identity
    assert model.view_identity["task_id"] == "task-one"
    assert "手動" in model.view_message
    read_view = factory.call_args.args[0]
    assert read_view()["task_id"] == "task-one"
    assert renderer.render.call_count == 2
    session.close.assert_not_called()


def test_view_requires_selected_loaded_record_and_never_runs_after_close(deferred):
    model, renderer, factory, _session = view_model(deferred)
    assert model.next_page()
    assert not model.create_view()
    finish(model, deferred)
    assert model.refresh()
    assert not model.create_view()
    finish(model, deferred)
    assert not model.create_view()
    model.close()
    assert not model.create_view()
    renderer.render.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize("action", ["select", "refresh", "next", "previous", "close"])
def test_record_navigation_revokes_active_view_and_clears_identity(deferred, action):
    model, _renderer, _factory, session = view_model(deferred)
    if action == "previous":
        assert model.next_page()
        finish(model, deferred)
    assert model.create_view()
    finish(model, deferred)
    if action == "select":
        assert model.select("task-two")
    elif action == "refresh":
        assert model.refresh()
    elif action == "next":
        assert model.next_page()
    elif action == "previous":
        assert model.previous_page()
    else:
        model.close()
    session.close.assert_called_once()
    assert model.view_url == ""
    assert model.view_identity is None
    assert model._view_session is None


@pytest.mark.parametrize("queued", [False, True])
def test_close_disposes_pending_or_queued_view_without_future_ui_poll(deferred, queued):
    model, _renderer, _factory, session = view_model(deferred)
    assert model.create_view()
    if queued:
        deferred.pop(0)()
    model.close()
    if not queued:
        deferred.pop(0)()
    session.close.assert_called_once()
    assert model.view_url == ""
    assert model.view_identity is None
    assert not model.poll()
    assert not model.create_view()


def test_selection_change_rejects_late_view_but_keeps_request_single_flight(deferred):
    model, renderer, _factory, session = view_model(deferred)
    assert model.create_view()
    assert model.select("task-two")
    # Complete the new ledger read first; the old render still owns its request slot.
    deferred.pop(1)()
    assert model.poll()
    assert model.task_id == "task-two"
    assert not model.create_view()
    deferred.pop(0)()
    # Dropping the stale result must still refresh the button when its busy slot is released.
    assert model.poll()
    session.close.assert_called_once()
    assert model.view_url == ""
    assert model.view_identity is None
    assert model.create_view()
    finish(model, deferred)
    assert model.view_identity["task_id"] == "task-two"
    assert renderer.render.call_args.args == ("task-two",)


@pytest.mark.parametrize("code", ["karte_renderer_view_missing", "karte_renderer_view_conflict",
                                 "karte_renderer_permission_denied", "karte_renderer_v2_conflict",
                                 "karte_renderer_helper_missing", "karte_output_limit"])
def test_derived_view_failure_retains_ledger_and_does_not_start_session(deferred, code):
    model, renderer, factory, _session = view_model(deferred)
    original_page = model.page
    renderer.render.side_effect = InspectionError(code)
    assert model.create_view()
    finish(model, deferred)
    assert model.page is original_page
    assert model.task_id == "task-one"
    assert code in model.view_message
    assert model.view_url == ""
    assert model.view_identity is None
    factory.assert_not_called()


def test_session_start_failure_closes_created_session_without_disclosing_raw_error(deferred):
    model, _renderer, _factory, session = view_model(deferred)
    session.start.side_effect = RuntimeError("private synthetic path and error")
    assert model.create_view()
    finish(model, deferred)
    session.close.assert_called_once()
    assert "inspection_failed" in model.view_message
    assert "private" not in model.view_message
    assert model.view_url == ""


def test_view_expiry_clears_url_and_explicit_recreation_revokes_prior_session(deferred):
    model, renderer, _factory, session = view_model(deferred)
    assert model.create_view()
    finish(model, deferred)
    assert model.create_view()
    session.close.assert_called_once()
    assert model.view_url == ""
    finish(model, deferred)
    assert renderer.render.call_count == 2
    model._clock = lambda: 400
    assert model.poll()
    assert model.view_url == ""
    assert model.view_identity is None
    assert "期限切れ" in model.view_message
    assert session.close.call_count == 2
    assert not model.poll()


def test_view_ui_is_selectable_text_without_opening_browser_or_writing_clipboard(deferred, monkeypatch):
    import subprocess
    import webbrowser

    browser = Mock(side_effect=AssertionError("browser must be manually opened"))
    process = Mock(side_effect=AssertionError("UI must not launch processes"))
    monkeypatch.setattr(webbrowser, "open", browser)
    monkeypatch.setattr(subprocess, "Popen", process)
    model, _renderer, _factory, _session = view_model(deferred)
    assert model.create_view()
    finish(model, deferred)
    window = RecordsWindow.__new__(RecordsWindow)
    window.model = model
    window.window = Mock()
    for name in ("table", "status", "detail", "refresh_button", "previous_button", "next_button",
                 "view_status", "view_url_value", "view_button", "view_identity"):
        setattr(window, name, Mock())
    window._render()
    window.view_url_value.set.assert_called_with(model.view_url)
    identity_text = window.view_identity.insert.call_args.args[1]
    assert "閲覧用 Markdown:" in identity_text
    assert "台帳 SHA-256:" in identity_text
    assert "30e5dfbab03f92e2eb7c8a67e17936579075fe88" in identity_text
    assert "<p>" not in identity_text
    window.view_identity.configure.assert_called_with(state="disabled")
    window.window.clipboard_clear.assert_not_called()
    window.window.clipboard_append.assert_not_called()
    browser.assert_not_called()
    process.assert_not_called()


def test_host_revocation_clears_still_visible_url_without_waiting_for_ui_deadline(deferred):
    model, _renderer, _factory, session = view_model(deferred)
    assert model.create_view()
    finish(model, deferred)
    session.active = False
    assert model.poll()
    assert model.view_url == ""
    assert model.view_identity is None
    session.close.assert_called_once()


def test_request_resource_is_disposed_when_close_races_background_completion():
    request = InspectionRequest()
    started, release, disposed = threading.Event(), threading.Event(), threading.Event()
    resource = object()
    seen = []

    def operation():
        started.set()
        assert release.wait(2)
        return resource

    def discard(value):
        seen.append(value)
        disposed.set()

    assert request.submit(operation, discard=discard)
    assert started.wait(2)
    request.invalidate(close=True)
    release.set()
    assert disposed.wait(2)
    assert seen == [resource]
    assert request.poll() is None
    assert not request.submit(operation, discard=discard)


def test_withdrawn_widgets_distinguish_projection_and_derived_view_with_inert_url(deferred, monkeypatch):
    import tkinter as tk

    from ephy_worker.desktop.records import VIEW_PROVENANCE

    model, _renderer, _factory, session = view_model(deferred)
    monkeypatch.setattr("ephy_worker.desktop.records.RecordsModel", lambda _config: model)
    root = tk.Tk()
    root.withdraw()
    window = None
    try:
        window = RecordsWindow(root, Mock(karte_root="synthetic-only"), on_close=Mock())
        window.window.withdraw()
        root.update_idletasks()
        finish(model, deferred)
        assert model.select("task-one")
        finish(model, deferred)
        window.create_view()
        finish(model, deferred)
        window._render()
        tabs = window.detail.master.master
        assert [tabs.tab(tab, "text") for tab in tabs.tabs()] == ["台帳 API（JSON）", "閲覧用 Markdown"]
        assert str(window.root_field.cget("state")) == "readonly"
        assert str(window.view_url_field.cget("state")) == "readonly"
        assert window.view_url_value.get() == session.start.return_value
        assert window.view_url_field.bind("<Button-1>") == ""
        assert str(window.view_identity.cget("state")) == "disabled"
        assert "既存ローカル worklog" in VIEW_PROVENANCE
        assert "公式固定版 KarteRenderer" in VIEW_PROVENANCE
        assert root.state() == "withdrawn"
        assert window.window.state() == "withdrawn"
    finally:
        if window is not None:
            window.close()
        root.destroy()
    session.close.assert_called_once()
