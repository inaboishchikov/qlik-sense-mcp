"""App settings: theme, menus, animation, sheet title style, thumbnails.

Where these live was found by reading a real app after changing them in
the options dialog: a hidden `appprops` object, plus `qThumbnail` in the
app properties. The fake Engine below keeps both.
"""

import json
from types import SimpleNamespace

import pytest

from qlik_sense_mcp_server.engine import app_options as ao
from qlik_sense_mcp_server.tools import authoring as tools
from qlik_sense_mcp_server.tools import context
from tests.test_authoring import FakeEngine, _sheet


class TestBuildChanges:
    def test_flags_and_text_map_to_the_stored_keys(self):
        changes, thumb = ao.build_app_option_changes(
            theme="Dark Theme", rtl=True, chart_animations=False,
            sheet_title_color="#000000")
        assert changes == {"theme": "Dark Theme", "rtl": True, "chartAnimations": False,
                           "sheetTitleColor": "#000000"}
        assert thumb is None

    def test_a_menu_is_stored_as_its_opposite(self):
        """In the dialog a menu is on when its `disable...` key is false."""
        on, _ = ao.build_app_option_changes(hover_menu=True, context_menu=True)
        off, _ = ao.build_app_option_changes(hover_menu=False, context_menu=False)
        assert on == {"disableCellNavMenu": False, "disableCellContextMenu": False}
        assert off == {"disableCellNavMenu": True, "disableCellContextMenu": True}

    def test_what_is_not_given_is_not_touched(self):
        assert ao.build_app_option_changes(theme=None, rtl=None) == ({}, None)

    def test_the_thumbnail_is_returned_apart(self):
        changes, thumb = ao.build_app_option_changes(thumbnail_url="/appcontent/a/b.png")
        assert changes == {} and thumb == "/appcontent/a/b.png"

    def test_an_empty_thumbnail_means_clear(self):
        assert ao.build_app_option_changes(thumbnail_url="")[1] == ""

    def test_a_logo_is_a_static_content_url(self):
        changes, _ = ao.build_app_option_changes(sheet_logo_url="/appcontent/a/l.png")
        assert changes["sheetLogoThumbnail"] == {
            "qStaticContentUrlDef": {"qUrl": "/appcontent/a/l.png"}}
        assert ao.build_app_option_changes(sheet_logo_url="")[0]["sheetLogoThumbnail"] == {
            "qStaticContentUrlDef": {}}

    @pytest.mark.parametrize("kwargs", [
        {"rtl": "yes"}, {"hover_menu": 1}, {"theme": ""}, {"theme": "  "}, {"theme": 5},
        {"thumbnail_url": "https://evil.example/x.png"}, {"thumbnail_url": "x.png"},
        {"thumbnail_url": "/appcontent/../secret"}, {"sheet_logo_url": "//host/x"},
        {"nonsense": True},
    ])
    def test_bad_input_is_refused(self, kwargs):
        with pytest.raises(ValueError):
            ao.build_app_option_changes(**kwargs)

    def test_reading_inverts_the_menus_back(self):
        stored = {"theme": "Dark Theme", "disableCellNavMenu": True,
                  "disableCellContextMenu": False, "chartAnimations": True, "rtl": False,
                  "sheetTitleColor": ""}
        read = ao.read_app_options(stored)
        assert read["hover_menu"] is False and read["context_menu"] is True
        assert read["theme"] == "Dark Theme" and read["chart_animations"] is True

    def test_what_is_written_reads_back_the_same(self):
        changes, _ = ao.build_app_option_changes(hover_menu=False, context_menu=True, rtl=True)
        read = ao.read_app_options(changes)
        assert read == {"hover_menu": False, "context_menu": True, "rtl": True}


class OptionsEngine(FakeEngine):
    """The fake Engine plus the app-level calls."""

    def __init__(self, with_appprops=True, **kw):
        super().__init__(**kw)
        self.app_props = {"qTitle": "T", "qThumbnail": {}, "privileges": ["read"]}
        if with_appprops:
            self._new({"qInfo": {"qType": "appprops"}, "theme": "", "rtl": False,
                       "disableCellNavMenu": False, "disableCellContextMenu": False,
                       "chartAnimations": True, "sheetTitleColor": ""})

    def send_request(self, method, params=None, handle=-1, timeout=None):
        if method == "GetAppProperties":
            return {"qProp": json.loads(json.dumps(self.app_props))}
        if method == "SetAppProperties":
            self.app_props = json.loads(json.dumps(params[0]))
            return {}
        return super().send_request(method, params, handle, timeout)

    def appprops(self):
        return next(p for p in self.objects.values() if p["qInfo"]["qType"] == "appprops")


class TestOnEngine:
    def test_a_theme_is_set_and_saved(self):
        eng = OptionsEngine()
        saves = eng.saves
        out = eng.set_app_options(1, {"theme": "Dark Theme"}, None)
        assert eng.appprops()["theme"] == "Dark Theme"
        assert out["options"]["theme"] == "Dark Theme"
        assert eng.saves == saves + 1

    def test_other_options_are_left_alone(self):
        eng = OptionsEngine()
        eng.set_app_options(1, {"theme": "Dark Theme"}, None)
        assert eng.appprops()["chartAnimations"] is True
        assert eng.appprops()["disableCellNavMenu"] is False

    def test_an_app_nobody_configured_gets_the_object_on_first_write(self):
        eng = OptionsEngine(with_appprops=False)
        assert eng.get_app_options(1)["stored"] is False
        eng.set_app_options(1, {"theme": "Dark Theme"}, None)
        assert eng.get_app_options(1)["stored"] is True
        assert eng.appprops()["theme"] == "Dark Theme"

    def test_the_thumbnail_goes_into_the_app_properties(self):
        eng = OptionsEngine()
        out = eng.set_app_options(1, {}, "/appcontent/x/cover.png")
        assert eng.app_props["qThumbnail"] == {"qUrl": "/appcontent/x/cover.png"}
        assert out["thumbnail_url"] == "/appcontent/x/cover.png"
        assert eng.app_props["qTitle"] == "T"  # the rest of the properties survive

    def test_an_empty_thumbnail_clears_it(self):
        eng = OptionsEngine()
        eng.set_app_options(1, {}, "/content/Default/a.png")
        eng.set_app_options(1, {}, "")
        assert eng.app_props["qThumbnail"] == {}

    def test_a_thumbnail_alone_does_not_create_the_options_object(self):
        eng = OptionsEngine(with_appprops=False)
        eng.set_app_options(1, {}, "/content/Default/a.png")
        assert eng.get_app_options(1)["stored"] is False


class TestSheetThumbnail:
    def test_a_sheet_gets_a_thumbnail(self):
        eng = OptionsEngine()
        sheet_id = _sheet(eng)
        out = eng.update_sheet(1, sheet_id, None, None, "/appcontent/x/s.png")
        assert out["thumbnail_url"] == "/appcontent/x/s.png"
        assert eng.objects[sheet_id]["thumbnail"] == {
            "qStaticContentUrlDef": {"qUrl": "/appcontent/x/s.png"}}

    def test_an_empty_one_clears_it(self):
        eng = OptionsEngine()
        sheet_id = _sheet(eng)
        eng.update_sheet(1, sheet_id, None, None, "/appcontent/x/s.png")
        out = eng.update_sheet(1, sheet_id, None, None, "")
        assert "thumbnail_url" not in out
        assert eng.objects[sheet_id]["thumbnail"] == {"qStaticContentUrlDef": {}}

    def test_a_rename_keeps_the_thumbnail(self):
        eng = OptionsEngine()
        sheet_id = _sheet(eng)
        eng.update_sheet(1, sheet_id, None, None, "/appcontent/x/s.png")
        eng.update_sheet(1, sheet_id, "Renamed", None)
        assert eng.objects[sheet_id]["thumbnail"]["qStaticContentUrlDef"]["qUrl"]


class TestUploadTool:
    @pytest.fixture
    def repo(self, monkeypatch):
        calls = []
        stub = SimpleNamespace(
            get_app_by_id=lambda app_id: {"published": False},
            upload_app_content=lambda *a: calls.append(a) or {"id": "1"})
        monkeypatch.setattr(context, "repo_api", stub)
        return calls

    def _call(self, **kw):
        return json.loads(tools.upload_app_image(**kw))

    def test_an_image_is_uploaded_and_its_path_returned(self, repo, tmp_path):
        image = tmp_path / "cover.png"
        image.write_bytes(b"\x89PNG-data")
        out = self._call(app_id="A", image_path=str(image))
        assert out["thumbnail_url"] == "/appcontent/A/cover.png"
        assert repo == [("A", "cover.png", b"\x89PNG-data", "image/png")]

    def test_the_name_can_be_chosen(self, repo, tmp_path):
        image = tmp_path / "x.png"
        image.write_bytes(b"d")
        assert self._call(app_id="A", image_path=str(image),
                          name="home.png")["thumbnail_url"] == "/appcontent/A/home.png"

    @pytest.mark.parametrize("filename", ["notes.txt", "key.pem", "script.py", "noext"])
    def test_only_images_are_uploaded(self, repo, tmp_path, filename):
        path = tmp_path / filename
        path.write_bytes(b"secret")
        assert "error" in self._call(app_id="A", image_path=str(path))
        assert repo == []

    @pytest.mark.parametrize("name", ["../x.png", "a/b.png", "a b.png", ""])
    def test_the_name_cannot_walk_anywhere(self, repo, tmp_path, name):
        image = tmp_path / "x.png"
        image.write_bytes(b"d")
        out = self._call(app_id="A", image_path=str(image), name=name or None) \
            if name else None
        if name:
            assert "error" in out and repo == []

    def test_an_empty_or_missing_file_is_refused(self, repo, tmp_path):
        empty = tmp_path / "e.png"
        empty.write_bytes(b"")
        assert "error" in self._call(app_id="A", image_path=str(empty))
        assert "error" in self._call(app_id="A", image_path=str(tmp_path / "nope.png"))

    def test_a_big_image_is_refused(self, repo, tmp_path):
        big = tmp_path / "big.png"
        big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        assert "error" in self._call(app_id="A", image_path=str(big))
        assert repo == []

    def test_a_server_refusal_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(context, "repo_api", SimpleNamespace(
            get_app_by_id=lambda app_id: {"published": False},
            upload_app_content=lambda *a: {"error": "HTTP 403: nope"}))
        image = tmp_path / "x.png"
        image.write_bytes(b"d")
        assert "403" in self._call(app_id="A", image_path=str(image))["error"]
