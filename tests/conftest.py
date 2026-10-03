import pytest

from calendar_clients.write_lock import WRITE_LOCK


@pytest.fixture(autouse=True)
def _hold_write_lock(request):
    """Most tests call write methods directly, as the tool holding the
    lock would -- so each test holds WRITE_LOCK by default. A test marked
    `without_write_lock` doesn't, to check what happens then."""
    if request.node.get_closest_marker("without_write_lock"):
        yield
        return
    with WRITE_LOCK:
        yield
