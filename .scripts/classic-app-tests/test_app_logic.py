"""Logic checks that the boot test can't see (Streamlit only runs app.py once a browser
session connects). Runs the script with Streamlit's AppTest and a fake WorkspaceClient.
"""

import py_compile
from unittest import mock

import pytest
from streamlit.testing.v1 import AppTest

from test_smoke import CLASSIC_TEMPLATES, REPO_ROOT


@pytest.mark.parametrize("template", CLASSIC_TEMPLATES)
def test_python_compiles(template):
    for path in (REPO_ROOT / template).rglob("*.py"):
        if ".venv" not in path.parts:
            py_compile.compile(str(path), doraise=True)


def test_files_app_rejects_paths_outside_volume(monkeypatch):
    monkeypatch.setenv("DATABRICKS_VOLUME_PATH", "/Volumes/main/default/uploads")
    with mock.patch("databricks.sdk.WorkspaceClient") as client_cls:
        client = client_cls.return_value
        client.files.list_directory_contents.return_value = []
        at = AppTest.from_file(str(REPO_ROOT / "streamlit-files-app/app.py")).run()
        client.files.list_directory_contents.assert_called_with("/Volumes/main/default/uploads")

        client.files.list_directory_contents.reset_mock()
        at.text_input[0].set_value("../../../etc").run()

        assert any("outside the volume" in e.value for e in at.error)
        client.files.list_directory_contents.assert_not_called()
