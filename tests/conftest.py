"""Suite-wide safety: no test may reach a live coordinator."""
import pytest


# --------------------------------------------------------------------------------------------------
# ⛔ THE SUITE NEVER QUEUES INTO PRODUCTION (remote-submit M2, 2026-09-18).
# --------------------------------------------------------------------------------------------------
# The tower devcontainer now carries RUNQ_TRANSPORT=api and the client credentials in its
# ENVIRONMENT, so every `runq` the tests spawn as a subprocess inherits them — and talks to the LIVE
# coordinator instead of the temp registry the test just built. Caught the hour the seal landed: five
# tests in test_runq.py exited 3, which is the live API refusing a DUPLICATE. Nothing was inserted only
# because the config hashes collided with an acceptance run; a unique one would have queued real work
# on the real fleet.
#
# So the transport is forced LOCAL for the whole suite. A test that wants the API transport sets it
# itself with monkeypatch (test_runq_api_transport does), which runs after this and wins.
@pytest.fixture(autouse=True)
def _never_queue_into_production(monkeypatch):
    monkeypatch.setenv("RUNQ_TRANSPORT", "local")
    for var in ("COORD_API_URL", "COORD_API_CA", "COORD_API_CERT", "COORD_API_KEY"):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------------------------------
# Tests that cannot mean anything outside the project this coordinator was extracted from.
# --------------------------------------------------------------------------------------------------
# They are skipped WITH A REASON rather than deleted, so the test files stay identical to the ones
# the live coordinator is developed against and the gap stays visible in every run's summary.
NOT_HERE = {
    "tests/test_box_pause.py::TestLaneFootprintCalibration::"
    "test_shipped_configs_parse_and_declare_no_hand_set_lane":
        "pins one site's own capacity schedules (configs/capacity/*.json); schedules are site data",
    "tests/test_box_pause.py::TestLaneFootprintCalibration::test_the_fleet_only_desktop_has_NO_schedule":
        "pins one site's own capacity schedules (configs/capacity/*.json); schedules are site data",
    "tests/test_dispatcher.py::TestAFailedFinalPullMustNotDiscardAHeldCheckpoint::"
    "test_the_loader_this_relies_on_really_falls_back_to_prev":
        "exercises a trainer-side checkpoint loader that is not part of this repository",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        reason = NOT_HERE.get(item.nodeid)
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))
