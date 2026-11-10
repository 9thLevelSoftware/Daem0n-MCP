"""Native graph input failures must not leave a waiting child process."""

import time
from unittest.mock import patch

import pytest

from daem0nmcp.graph.leiden import (
    LeidenConfig,
    LeidenExecutionError,
    run_leiden_bounded,
)


def test_native_request_rejection_precedes_process_creation():
    with (
        patch("daem0nmcp.graph.leiden._MAX_REQUEST_BYTES", 1),
        patch("daem0nmcp.graph.leiden.subprocess.Popen") as launch,
        pytest.raises(LeidenExecutionError, match="TASK_REQUIRED"),
    ):
        run_leiden_bounded(
            ("one", "two"),
            (("one", "two"),),
            LeidenConfig(),
            cancelled=None,
            deadline=time.monotonic() + 5,
        )
    launch.assert_not_called()
