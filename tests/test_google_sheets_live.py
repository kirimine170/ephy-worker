"""Live validation is a separate, explicitly authorized human opt-in．"""

import os

import pytest


def test_explicit_live_metadata():
    if os.environ.get("EPHY_SHEETS_LIVE") != "1":
        pytest.skip("live_validation_pending: set EPHY_SHEETS_LIVE=1 only after manual API/Viewer/ADC setup")
    configured = os.environ.get("EPHY_SHEETS_LIVE_CONFIG")
    if not configured or not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        pytest.skip("live_validation_pending: explicit external config and service-account ADC required")
    from ephy_worker.config import load_config
    from ephy_worker.google_sheets import SheetsAdapter

    config = load_config(configured)
    assert config.google_sheets is not None
    adapter = SheetsAdapter(config.google_sheets)
    try:
        for source in ("ephy-backlog", "karte-backlog"):
            result = adapter.execute(source)
            assert result.status == "ok", result.diagnostic
            assert result.tabs
    finally:
        adapter.close()
