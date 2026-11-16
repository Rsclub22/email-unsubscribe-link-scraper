"""Settings of the agent image that the service relies on. Runs where the Dockerfile is (not inside the image)."""

import json
import pathlib
import re

import pytest

DOCKERFILE = pathlib.Path(__file__).resolve().parents[1] / "Dockerfile"


@pytest.mark.skipif(not DOCKERFILE.exists(), reason="the image does not contain its Dockerfile")
def test_gunicorn_keeps_a_thread_for_health_while_a_case_runs():
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", DOCKERFILE.read_text(), re.MULTILINE).group(1))
    assert cmd[cmd.index("--workers") + 1] == "1"          # LOCK in server.py serialises cases within one process
    assert int(cmd[cmd.index("--threads") + 1]) >= 2       # a second thread answers /health during a case
