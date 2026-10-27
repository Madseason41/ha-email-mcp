"""End-to-end tests open sockets to GreenMail and the add-on.

The Home Assistant pytest plugin (pulled in by the integration-test
requirements) disables sockets in ``pytest_runtest_setup``. Re-enable them
for every e2e test. Module-scoped fixtures also enable sockets themselves,
because they run before this function-scoped fixture.
"""

from __future__ import annotations

import pytest

try:
    import pytest_socket
except ImportError:  # only present when the Home Assistant test plugin is installed
    pytest_socket = None


@pytest.fixture(autouse=True)
def _allow_network():
    if pytest_socket is not None:
        pytest_socket.enable_socket()
    yield
