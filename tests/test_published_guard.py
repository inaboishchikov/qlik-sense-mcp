"""A published app is never edited, and a check that cannot run blocks the edit.

Qlik accepts some changes to a published app and refuses others, so the
server does not leave the decision to it: every tool that changes an app asks
the repository whether the app is published first, and nothing is opened,
changed or uploaded when the answer is yes, missing or an error.
"""

import contextlib
import json
from types import SimpleNamespace

import pytest

from qlik_sense_mcp_server.tools import authoring as tools
from qlik_sense_mcp_server.tools import context


class Engine:
    """Records what the tools ask of Engine; any call here is a write that got through."""

    def __init__(self):
        self.calls = []

    @contextlib.contextmanager
    def transaction(self):
        yield

    def ensure_app(self, app_id, no_data=False):
        self.calls.append(("ensure_app", app_id))
        return 1

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args))
            return {}
        return call


def install(monkeypatch, answer):
    """`answer` is the app record, or an exception to raise."""
    engine = Engine()

    def get_app_by_id(app_id):
        if isinstance(answer, Exception):
            raise answer
        return answer

    uploads = []
    repo = SimpleNamespace(get_app_by_id=get_app_by_id,
                           upload_app_content=lambda *a: uploads.append(a) or {})
    monkeypatch.setattr(context, "engine_api", engine)
    monkeypatch.setattr(context, "repo_api", repo)
    return engine, uploads


# Every tool that changes an existing app, with arguments that would reach Engine.
WRITES = [
    ("set_app_script", dict(app_id="A", script="LOAD 1 AUTOGENERATE 1;")),
    ("patch_app_script", dict(app_id="A", find="a", replace="b")),
    ("reload_app", dict(app_id="A")),
    ("create_sheet", dict(app_id="A", title="S")),
    ("update_sheet", dict(app_id="A", sheet_id="s", title="T")),
    ("delete_sheet", dict(app_id="A", sheet_id="s")),
    ("add_sheet_object", dict(app_id="A", sheet_id="s", object_type="kpi", title="K",
                              measures=[{"expression": "Count(x)"}])),
    ("update_sheet_object", dict(app_id="A", object_id="o", title="T")),
    ("delete_sheet_object", dict(app_id="A", object_id="o")),
    ("set_app_options", dict(app_id="A", chart_animations=False)),
]


def run(name, kwargs):
    return json.loads(getattr(tools, name)(**kwargs))


@pytest.mark.parametrize("name,kwargs", WRITES)
class TestRefused:
    def test_a_published_app_is_not_touched(self, monkeypatch, name, kwargs):
        engine, _ = install(monkeypatch, {"published": True})
        out = run(name, kwargs)
        assert "published" in out["error"]
        assert engine.calls == []

    def test_an_unclear_answer_is_not_a_yes(self, monkeypatch, name, kwargs):
        engine, _ = install(monkeypatch, {"name": "no published field"})
        assert "error" in run(name, kwargs)
        assert engine.calls == []

    def test_a_failed_check_blocks_the_write(self, monkeypatch, name, kwargs):
        engine, _ = install(monkeypatch, RuntimeError("QRS is down"))
        out = run(name, kwargs)
        assert "Could not tell" in out["error"]
        assert engine.calls == []

    def test_the_refusal_carries_no_advice(self, monkeypatch, name, kwargs):
        install(monkeypatch, {"published": True})
        assert "hint" not in run(name, kwargs)


class TestAllowed:
    def test_an_unpublished_app_reaches_engine(self, monkeypatch):
        engine, _ = install(monkeypatch, {"published": False})
        tools.ensure_editable("A")
        assert tools._open_for_edit("A") == 1
        assert ("ensure_app", "A") in engine.calls


class TestUpload:
    def test_a_published_app_gets_no_upload(self, monkeypatch, tmp_path):
        _, uploads = install(monkeypatch, {"published": True})
        image = tmp_path / "x.png"
        image.write_bytes(b"d")
        out = json.loads(tools.upload_app_image(app_id="A", image_path=str(image)))
        assert "published" in out["error"]
        assert uploads == []

    def test_a_failed_check_gets_no_upload(self, monkeypatch, tmp_path):
        _, uploads = install(monkeypatch, OSError("down"))
        image = tmp_path / "x.png"
        image.write_bytes(b"d")
        assert "error" in json.loads(tools.upload_app_image(app_id="A", image_path=str(image)))
        assert uploads == []


class TestReadsAreNotGuarded:
    def test_options_can_be_read_from_a_published_app(self, monkeypatch):
        engine, _ = install(monkeypatch, {"published": True})
        tools.get_app_options(app_id="A")
        assert ("ensure_app", "A") in engine.calls
