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
    for name in ("table", "status", "detail", "refresh_button", "previous_button", "next_button"):
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
