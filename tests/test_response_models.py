"""Foundation tests for typed response models (strict mode, extra-key filtering, 500 handler)."""

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from trackseerr.api import response_models
from trackseerr.api.app import response_validation_error_response


def test_strict_mode_enabled_under_tests():
    assert response_models.ApiModel.model_config["extra"] == "forbid"


def test_extra_key_rejected_in_strict_mode():
    class Item(response_models.ApiModel):
        a: int

    with pytest.raises(ValidationError):
        Item.model_validate({"a": 1, "secret": "x"})


def test_extra_key_dropped_when_not_strict():
    # Fresh interpreter with the flag unset: reloading the module in-process would create a second
    # ApiModel class and make issubclass checks elsewhere depend on test order.
    code = (
        "from trackseerr.api.response_models import ApiModel\n"
        "class Item(ApiModel):\n"
        "    a: int\n"
        "assert ApiModel.model_config['extra'] == 'ignore'\n"
        "assert Item.model_validate({'a': 1, 'secret': 'x'}).model_dump() == {'a': 1}\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "TRACKSEERR_STRICT_RESPONSES"}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent), env.get("PYTHONPATH")]))
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_handler_returns_500_and_logs_without_values(caplog):
    app = FastAPI()
    app.add_exception_handler(ResponseValidationError, response_validation_error_response)

    class Out(response_models.ApiModel):
        n: int

    @app.get("/bad", response_model=Out)
    def bad():
        return {"n": "SENSITIVE-VALUE-123"}

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR):
        r = client.get("/bad")
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal response error"}
    text = caplog.text
    assert "GET /bad" in text and "n" in text and "int_parsing" in text
    assert "SENSITIVE-VALUE-123" not in text
