"""Writing to an app: what is built, what is refused, what is rolled back.

The builders are pure and tested directly. The Engine calls run against a
small stateful fake that behaves like the parts of Engine this module
touches: objects keep their properties, `GetLayout` reports validation
errors the way a real layout does, and `DoSave` is recorded so the tests
can assert that nothing broken was ever saved.
"""

import pytest

from qlik_sense_mcp_server.engine import authoring as au
from qlik_sense_mcp_server.engine import QlikEngineAPI
from qlik_sense_mcp_server.exceptions import QlikEngineError


# ─── Builders ──────────────────────────────────────────────────────────────


class TestDimensionsAndMeasures:
    def test_a_field_dimension(self):
        dim = au.build_dimension({"field": "Country", "label": "Land"})
        assert dim["qDef"]["qFieldDefs"] == ["Country"]
        assert dim["qDef"]["qFieldLabels"] == ["Land"]
        assert dim["qNullSuppression"] is True

    def test_a_calculated_dimension_gets_its_equals_sign(self):
        dim = au.build_dimension({"expression": "Year(OrderDate)"})
        assert dim["qDef"]["qFieldDefs"] == ["=Year(OrderDate)"]

    def test_a_master_dimension_is_a_library_reference(self):
        dim = au.build_dimension({"library_id": "abc"})
        assert dim["qLibraryId"] == "abc"
        assert dim["qDef"]["cId"]

    @pytest.mark.parametrize("spec", [
        {}, {"field": "A", "expression": "B"}, {"field": "A", "library_id": "x"}, "Country"])
    def test_a_dimension_needs_exactly_one_source(self, spec):
        with pytest.raises(ValueError):
            au.build_dimension(spec)

    def test_a_measure_expression_loses_its_leading_equals(self):
        m = au.build_measure({"expression": "= Sum(Sales)", "label": "Sales",
                              "format": "#,##0"})
        assert m["qDef"].pop("cId")
        for editor_default in ("autoSort", "textAlign", "numFormatFromTemplate"):
            m["qDef"].pop(editor_default)
        # separators are stated so the app's locale cannot reinterpret the mask
        assert m["qDef"] == {"qDef": "Sum(Sales)", "qLabel": "Sales",
                             "qNumFormat": {"qType": "F", "qFmt": "#,##0",
                                            "qDec": ".", "qThou": ","}}

    def test_a_measure_sort_is_numeric_and_directional(self):
        assert au.build_measure({"expression": "Sum(A)", "sort": "desc"})[
            "qSortBy"] == {"qSortByNumeric": -1}
        assert au.build_measure({"expression": "Sum(A)", "sort": "asc"})[
            "qSortBy"] == {"qSortByNumeric": 1}
        with pytest.raises(ValueError):
            au.build_measure({"expression": "Sum(A)", "sort": "up"})

    def test_every_column_has_its_own_id(self):
        """The client keys a table's columns by cId; without one the data is
        there and the table draws nothing."""
        cube = au.build_hypercube(
            "table", [{"field": "A"}, {"library_id": "x"}],
            [{"expression": "Sum(B)"}, {"library_id": "y"}])
        ids = [c["qDef"]["cId"] for c in cube["qDimensions"] + cube["qMeasures"]]
        assert all(ids) and len(set(ids)) == 4

    def test_a_table_lists_its_columns_in_order(self):
        """Engine fills qColumnOrder with [] when absent, and an empty order
        is a table with no columns."""
        cube = au.build_hypercube(
            "table", [{"field": "A"}, {"field": "B"}], [{"expression": "Sum(C)"}])
        assert cube["qColumnOrder"] == [0, 1, 2]
        assert cube["columnOrder"] == [0, 1, 2]
        assert cube["columnWidths"] == [-1, -1, -1]

    def test_a_table_carries_the_defaults_the_client_reads_unchecked(self):
        props = au.build_object_properties(
            "table", "T", "", [{"field": "A"}], [{"expression": "Sum(B)"}])
        assert props["totals"]["position"] == "noTotals"
        assert props["scrolling"]["horizontal"] is True
        for col in props["qHyperCubeDef"]["qDimensions"] + props["qHyperCubeDef"]["qMeasures"]:
            assert col["qDef"]["autoSort"] is True
            assert col["qDef"]["textAlign"] == {"auto": True, "align": "left"}

    def test_a_chart_has_no_column_order(self):
        cube = au.build_hypercube("barchart", [{"field": "A"}], [{"expression": "Sum(C)"}])
        assert "qColumnOrder" not in cube

    def test_a_sorted_measure_leads_the_sort_order(self):
        cube = au.build_hypercube(
            "barchart", [{"field": "Country"}],
            [{"expression": "Sum(A)"}, {"expression": "Sum(B)", "sort": "desc"}])
        # columns: 0 = Country, 1 = Sum(A), 2 = Sum(B)
        assert cube["qInterColumnSortOrder"] == [2, 0, 1]


class TestCounts:
    @pytest.mark.parametrize("object_type,dims,measures", [
        ("kpi", 1, 1), ("kpi", 0, 0), ("kpi", 0, 3),
        ("piechart", 1, 2), ("piechart", 0, 1),
        ("barchart", 0, 1), ("barchart", 3, 1), ("barchart", 1, 0),
        ("pivot-table", 0, 1), ("table", 0, 0),
    ])
    def test_a_chart_is_refused_a_shape_it_cannot_draw(self, object_type, dims, measures):
        with pytest.raises(ValueError):
            au.check_counts(object_type, [{}] * dims, [{}] * measures)

    @pytest.mark.parametrize("object_type,dims,measures", [
        ("kpi", 0, 1), ("kpi", 0, 2), ("table", 2, 3), ("table", 0, 1),
        ("piechart", 1, 1), ("barchart", 2, 2), ("pivot-table", 2, 1),
    ])
    def test_and_accepted_one_it_can(self, object_type, dims, measures):
        au.check_counts(object_type, [{}] * dims, [{}] * measures)

    def test_the_fetch_window_covers_every_column_within_the_cell_cap(self):
        cube = au.build_hypercube(
            "table", [{"field": f"F{i}"} for i in range(5)], [{"expression": "Sum(A)"}])
        window = cube["qInitialDataFetch"][0]
        assert window["qWidth"] == 6
        assert window["qWidth"] * window["qHeight"] <= 9900


class TestObjectProperties:
    def test_a_pivot_table_is_a_pivot_cube(self):
        props = au.build_object_properties(
            "pivot-table", "T", "", [{"field": "A"}], [{"expression": "Sum(B)"}])
        assert props["qHyperCubeDef"]["qMode"] == "P"
        assert props["qInfo"] == {"qType": "pivot-table"}
        assert props["visualization"] == "pivot-table"

    def test_titles_show_only_when_there_is_one(self):
        shown = au.build_object_properties("kpi", "Revenue", "", [], [{"expression": "1"}])
        hidden = au.build_object_properties("kpi", "", "", [], [{"expression": "1"}])
        assert shown["showTitles"] is True and hidden["showTitles"] is False

    def test_extra_properties_cannot_restructure_the_object(self):
        for key in ("qInfo", "qHyperCubeDef", "visualization", "cells"):
            with pytest.raises(ValueError):
                au.check_extra_properties({key: {}})
        assert au.check_extra_properties({"showTitles": False}) == {"showTitles": False}

    def test_deep_merge_recurses_into_dicts_and_replaces_the_rest(self):
        target = {"a": {"b": 1, "c": 2}, "d": [1]}
        au.deep_merge(target, {"a": {"b": 9}, "d": [2, 3]})
        assert target == {"a": {"b": 9, "c": 2}, "d": [2, 3]}


# ─── The sheet grid ────────────────────────────────────────────────────────


class TestPlacement:
    def test_the_first_object_goes_top_left(self):
        sheet = {"columns": 24, "rows": 12, "cells": []}
        cell = au.place_cell(sheet, "o1", "kpi", {}, (6, 3))
        assert (cell["col"], cell["row"], cell["colspan"], cell["rowspan"]) == (0, 0, 6, 3)

    def test_the_next_goes_beside_it_then_below(self):
        sheet = {"columns": 24, "rows": 12, "cells": []}
        for i in range(4):
            au.place_cell(sheet, f"k{i}", "kpi", {}, (6, 3))
        wide = au.place_cell(sheet, "t", "table", {}, (24, 8))
        assert (wide["col"], wide["row"]) == (0, 3)

    def test_a_block_never_overlaps_one_already_there(self):
        sheet = {"columns": 24, "rows": 12, "cells": [
            {"name": "a", "col": 0, "row": 0, "colspan": 24, "rowspan": 2}]}
        cell = au.place_cell(sheet, "b", "barchart", {}, (12, 6))
        assert cell["row"] == 2

    def test_the_sheet_grows_when_a_block_runs_past_the_bottom(self):
        sheet = {"columns": 24, "rows": 12, "cells": [
            {"name": "a", "col": 0, "row": 0, "colspan": 24, "rowspan": 10}]}
        au.place_cell(sheet, "b", "table", {}, (24, 8))
        assert sheet["rows"] == 18
        # bounds are a share of the grid, so they were all recomputed
        assert sheet["cells"][0]["bounds"]["height"] == pytest.approx(10 * 100 / 18)

    def test_an_explicit_position_is_taken_as_given(self):
        sheet = {"columns": 24, "rows": 12, "cells": []}
        cell = au.place_cell(sheet, "o", "kpi", {"col": 3, "row": 1, "colspan": 4}, (6, 3))
        assert (cell["col"], cell["row"], cell["colspan"], cell["rowspan"]) == (3, 1, 4, 3)

    def test_col_without_row_is_refused(self):
        with pytest.raises(ValueError):
            au.place_cell({"cells": []}, "o", "kpi", {"col": 2, "row": None}, (6, 3))

    @pytest.mark.parametrize("position", [
        {"col": 20, "colspan": 6}, {"colspan": 0}, {"col": -1}, {"col": "2"},
        {"width": 3}, "left",
    ])
    def test_a_position_off_the_grid_or_malformed_is_refused(self, position):
        with pytest.raises(ValueError):
            au.normalise_position(position, (6, 3))

    def test_moving_an_object_does_not_collide_with_its_old_self(self):
        sheet = {"columns": 24, "rows": 12, "cells": []}
        au.place_cell(sheet, "o", "kpi", {}, (6, 3))
        moved = au.place_cell(sheet, "o", "kpi", {}, (6, 3))
        assert (moved["col"], moved["row"]) == (0, 0)
        assert len(sheet["cells"]) == 1

    def test_dropping_a_cell(self):
        sheet = {"cells": [{"name": "a"}, {"name": "b"}]}
        assert au.drop_cell(sheet, "a") is True
        assert sheet["cells"] == [{"name": "b"}]
        assert au.drop_cell(sheet, "zzz") is False


# ─── Engine's verdict ──────────────────────────────────────────────────────


class TestLayoutProblems:
    def test_a_clean_layout_has_none(self):
        assert au.layout_problems({"qHyperCube": {"qDimensionInfo": [{}], "qMeasureInfo": [{}]}}) == []

    def test_a_bad_dimension_and_a_bad_measure_are_both_named(self):
        layout = {"qHyperCube": {
            "qDimensionInfo": [{"qFallbackTitle": "Nope", "qError": {"qErrorCode": 7}}],
            "qMeasureInfo": [{"qFallbackTitle": "Sum(", "qError": {"qErrorCode": 5}}]}}
        problems = au.layout_problems(layout)
        assert any("dimension Nope" in p for p in problems)
        assert any("measure Sum(" in p for p in problems)

    def test_a_cube_level_error_is_reported(self):
        assert au.layout_problems({"qHyperCube": {"qError": {"qErrorCode": 9}}})

    def test_a_listbox_error_is_reported(self):
        assert au.layout_problems({"qListObject": {"qError": {"qErrorCode": 7}}})

    def test_garbage_is_no_problem(self):
        assert au.layout_problems(None) == []


class TestScriptErrors:
    def test_the_failing_text_is_quoted(self):
        script = "LOAD a FROM x;\nLOAD bad bad;\n"
        errors = au.format_script_errors(script, [
            {"qTabIx": 0, "qLineInTab": 1, "qColInLine": 5, "qTextPos": 20,
             "qErrLen": 3, "qSecondaryFailure": False}])
        assert errors[0]["text"] == script[20:23]
        assert errors[0]["line_in_tab"] == 1
        assert errors[0]["secondary"] is False

    def test_a_position_outside_the_script_gives_no_text(self):
        errors = au.format_script_errors("x", [{"qTextPos": 500, "qErrLen": 3}])
        assert errors[0]["text"] == ""


# ─── Against a fake Engine ─────────────────────────────────────────────────


class FakeEngine(QlikEngineAPI):
    """The slice of Engine that authoring touches, with state."""

    APP = 1

    def __init__(self, script="LOAD 1 AS a AUTOGENERATE 1;", syntax_errors=None,
                 bad_fields=()):
        self.ws = object()
        self.ws_timeout_seconds = 30.0
        self.script = script
        self.syntax_errors = syntax_errors or []
        self.bad_fields = set(bad_fields)
        self.saves = 0
        self.objects = {}      # id -> properties
        self.children = {}     # parent id -> [child ids]
        self.handles = {}      # handle -> id
        self._next = 10
        self.calls = []
        self.sheets_listing = []

    # get_sheets goes through session objects; the listing is not what is
    # under test here.
    def get_sheets(self, app_handle):
        return self.sheets_listing

    def _handle(self, object_id):
        for h, i in self.handles.items():
            if i == object_id:
                return h
        self._next += 1
        self.handles[self._next] = object_id
        return self._next

    def _new(self, props, parent=None):
        self._next += 1
        object_id = f"obj{self._next}"
        self.objects[object_id] = props
        props.setdefault("qInfo", {})["qId"] = object_id
        if parent:
            self.children.setdefault(parent, []).append(object_id)
        return object_id

    def _layout_of(self, object_id):
        props = self.objects[object_id]
        layout = {"qInfo": props.get("qInfo")}
        cube = props.get("qHyperCubeDef")
        if cube:
            def bad(defs):
                return any(f in self.bad_fields for f in defs)
            dim_info = []
            for d in cube.get("qDimensions", []):
                names = (d.get("qDef") or {}).get("qFieldDefs", [])
                info = {"qFallbackTitle": (names or ["?"])[0]}
                if bad(names):
                    info["qError"] = {"qErrorCode": 7}
                dim_info.append(info)
            layout["qHyperCube"] = {"qDimensionInfo": dim_info, "qMeasureInfo": [
                {"qFallbackTitle": (m.get("qDef") or {}).get("qDef", "")}
                for m in cube.get("qMeasures", [])]}
        lst = props.get("qListObjectDef")
        if lst and any(f in self.bad_fields for f in lst["qDef"]["qFieldDefs"]):
            layout["qListObject"] = {"qError": {"qErrorCode": 7}}
        kids = self.children.get(object_id, [])
        if kids:
            layout["qChildList"] = {"qItems": [{"qInfo": {"qId": k}} for k in kids]}
        return layout

    def _remove(self, object_id):
        for kid in self.children.pop(object_id, []):
            self._remove(kid)
        self.objects.pop(object_id, None)

    def send_request(self, method, params=None, handle=-1, timeout=None):
        params = params if params is not None else []
        self.calls.append(method)
        if method == "GetScript":
            return {"qScript": self.script}
        if method == "SetScript":
            self.script = params[0]
            return {}
        if method == "CheckScriptSyntax":
            return {"qErrors": self.syntax_errors}
        if method == "DoSave":
            self.saves += 1
            return {}
        if method == "CreateObject":
            object_id = self._new(params[0])
            return {"qReturn": {"qGenericId": object_id, "qHandle": self._handle(object_id)}}
        if method == "GetObject":
            object_id = params["qId"]
            if object_id not in self.objects:
                return {"qReturn": {"qType": "", "qHandle": 0, "qGenericId": ""}}
            return {"qReturn": {"qHandle": self._handle(object_id), "qGenericId": object_id}}
        if method == "CreateChild":
            parent = self.handles[handle]
            object_id = self._new(params[0], parent)
            self.objects[parent].setdefault("_children", []).append(object_id)
            return {"qReturn": {"qGenericId": object_id, "qHandle": self._handle(object_id)}}
        if method == "GetProperties":
            return {"qProp": __import__("copy").deepcopy(self.objects[self.handles[handle]])}
        if method == "SetProperties":
            object_id = self.handles[handle]
            self.objects[object_id] = __import__("copy").deepcopy(params[0])
            return {}
        if method == "GetLayout":
            return {"qLayout": self._layout_of(self.handles[handle])}
        if method == "GetChildInfos":
            parent = self.handles[handle]
            return {"qInfos": [{"qId": k, "qType": self.objects[k]["qInfo"]["qType"]}
                               for k in self.children.get(parent, []) if k in self.objects]}
        if method == "GetAllInfos":
            return {"qInfos": [{"qId": i, "qType": p["qInfo"]["qType"]}
                               for i, p in self.objects.items()]}
        if method == "DestroyChild":
            parent = self.handles[handle]
            self._remove(params[0])
            if params[0] in self.children.get(parent, []):
                self.children[parent].remove(params[0])
            return {}
        if method == "DestroyObject":
            # As on a real server: destroying a child of a sheet from the app
            # leaves it where it is, a child without a cell.
            object_id = params[0]
            if any(object_id in kids for kids in self.children.values()):
                return {}
            self._remove(object_id)
            return {}
        raise AssertionError(f"unexpected Engine call {method}")


def _sheet(eng, title="S"):
    return eng.create_sheet(FakeEngine.APP, title)


class TestScript:
    def test_a_good_script_is_saved(self):
        eng = FakeEngine()
        out = eng.replace_script_checked(1, "LOAD 2 AS b AUTOGENERATE 1;")
        assert out["saved"] is True
        assert eng.script == "LOAD 2 AS b AUTOGENERATE 1;"
        assert eng.saves == 1

    def test_a_script_with_a_syntax_error_is_put_back_and_not_saved(self):
        eng = FakeEngine(syntax_errors=[{"qTabIx": 0, "qLineInTab": 0, "qTextPos": 0,
                                         "qErrLen": 4}])
        original = eng.script
        out = eng.replace_script_checked(1, "LOAD LOAD;")
        assert out["saved"] is False
        assert out["syntax_errors"][0]["text"] == "LOAD"
        assert eng.script == original
        assert eng.saves == 0

    def test_the_check_can_be_skipped(self):
        eng = FakeEngine(syntax_errors=[{"qTextPos": 0}])
        assert eng.replace_script_checked(1, "x", check=False)["saved"] is True
        assert "CheckScriptSyntax" not in eng.calls


class TestSheets:
    def test_a_sheet_is_created_and_saved(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng, "Overview")
        props = eng.objects[sheet_id]
        assert props["qMetaDef"]["title"] == "Overview"
        assert props["cells"] == [] and props["columns"] == 24
        assert eng.saves == 1

    def test_a_new_sheet_ranks_after_the_last_one(self):
        eng = FakeEngine()
        eng.sheets_listing = [{"qData": {"rank": 0}}, {"qData": {"rank": 4}}]
        assert eng.objects[_sheet(eng)]["rank"] == 5

    def test_a_sheet_is_renamed_without_touching_its_description(self):
        eng = FakeEngine()
        sheet_id = eng.create_sheet(1, "Old", "keep me")
        out = eng.update_sheet(1, sheet_id, "New", None)
        assert out == {"title": "New", "description": "keep me"}

    def test_a_missing_sheet_is_an_error_not_a_silent_success(self):
        with pytest.raises(QlikEngineError, match="No sheet"):
            FakeEngine().update_sheet(1, "ghost", "x", None)

    def test_deleting_a_sheet_removes_it_and_reports_its_objects(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        eng.add_object(1, sheet_id, "kpi", "K", "", [], [{"expression": "Sum(A)"}], [],
                       {}, {})
        out = eng.delete_sheet(1, sheet_id)
        assert out["objects_removed"] == 1
        assert sheet_id not in eng.objects


class TestAddObject:
    def _add(self, eng, sheet_id, object_type="barchart", **kw):
        args = dict(title="T", subtitle="", dimensions=[{"field": "Country"}],
                    measures=[{"expression": "Sum(Sales)"}], fields=[], position={},
                    extra_properties={})
        args.update(kw)
        return eng.add_object(1, sheet_id, object_type, **args)

    def test_a_chart_lands_on_the_sheet_and_is_saved(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        saves = eng.saves
        out = self._add(eng, sheet_id)
        assert out["created"] is True
        assert out["cell"] == {"col": 0, "row": 0, "colspan": 12, "rowspan": 6}
        cells = eng.objects[sheet_id]["cells"]
        assert [c["name"] for c in cells] == [out["object_id"]]
        assert eng.saves == saves + 1

    def test_a_chart_on_a_missing_field_is_not_left_behind(self):
        eng = FakeEngine(bad_fields={"Nope"})
        sheet_id = _sheet(eng)
        saves = eng.saves
        out = self._add(eng, sheet_id, dimensions=[{"field": "Nope"}])
        assert out["created"] is False
        assert any("dimension Nope" in p for p in out["problems"])
        assert eng.objects[sheet_id]["cells"] == []
        assert [i for i in eng.objects if i != sheet_id] == []
        assert eng.saves == saves

    def test_a_position_off_the_grid_creates_nothing(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        with pytest.raises(ValueError):
            self._add(eng, sheet_id, position={"col": 20, "colspan": 12})
        assert [i for i in eng.objects if i != sheet_id] == []

    def test_a_wrong_shape_creates_nothing(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        with pytest.raises(ValueError):
            self._add(eng, sheet_id, "piechart", measures=[])
        assert [i for i in eng.objects if i != sheet_id] == []

    def test_objects_fill_the_sheet_without_overlapping(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        kpi = dict(dimensions=[], measures=[{"expression": "Sum(A)"}])
        for _ in range(4):
            self._add(eng, sheet_id, "kpi", **kpi)
        self._add(eng, sheet_id, "table", dimensions=[{"field": "A"}], measures=[])
        cells = eng.objects[sheet_id]["cells"]
        assert [(c["col"], c["row"]) for c in cells] == [
            (0, 0), (6, 0), (12, 0), (18, 0), (0, 3)]

    def test_a_filter_pane_gets_one_listbox_per_field(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        out = eng.add_object(1, sheet_id, "filterpane", "Filters", "", [], [],
                             ["Country", "Year"], {}, {})
        assert out["created"] is True
        listboxes = eng.children[out["object_id"]]
        assert [eng.objects[i]["qListObjectDef"]["qDef"]["qFieldDefs"][0]
                for i in listboxes] == ["Country", "Year"]

    def test_a_filter_pane_on_a_missing_field_is_removed_with_its_listboxes(self):
        eng = FakeEngine(bad_fields={"Nope"})
        sheet_id = _sheet(eng)
        out = eng.add_object(1, sheet_id, "filterpane", "", "", [], [], ["Country", "Nope"],
                             {}, {})
        assert out["created"] is False
        assert [i for i in eng.objects if i != sheet_id] == []

    def test_a_filter_pane_needs_fields(self):
        eng = FakeEngine()
        with pytest.raises(ValueError):
            eng.add_object(1, _sheet(eng), "filterpane", "", "", [], [], [], {}, {})

    def test_a_chart_does_not_take_fields(self):
        eng = FakeEngine()
        with pytest.raises(ValueError):
            self._add(eng, _sheet(eng), fields=["A"])

    def test_extra_properties_are_merged_over_the_object(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        out = self._add(eng, sheet_id, extra_properties={"orientation": "horizontal"})
        assert eng.objects[out["object_id"]]["orientation"] == "horizontal"


class TestUpdateObject:
    def _chart(self, eng):
        sheet_id = _sheet(eng)
        out = eng.add_object(1, sheet_id, "barchart", "T", "", [{"field": "Country"}],
                             [{"expression": "Sum(Sales)"}], [], {}, {})
        return sheet_id, out["object_id"]

    def test_a_title_changes_and_the_rest_stays(self):
        eng = FakeEngine()
        _, oid = self._chart(eng)
        out = eng.update_object(1, oid, None, "New", None, None, None, None, {})
        assert out["updated"] is True
        assert eng.objects[oid]["title"] == "New"
        assert eng.objects[oid]["qHyperCubeDef"]["qMeasures"][0]["qDef"]["qDef"] == "Sum(Sales)"

    def test_measures_are_replaced_and_dimensions_kept(self):
        eng = FakeEngine()
        _, oid = self._chart(eng)
        eng.update_object(1, oid, None, None, None, None,
                          [{"expression": "Avg(Sales)"}], None, {})
        cube = eng.objects[oid]["qHyperCubeDef"]
        assert cube["qMeasures"][0]["qDef"]["qDef"] == "Avg(Sales)"
        assert cube["qDimensions"][0]["qDef"]["qFieldDefs"] == ["Country"]

    def test_stale_column_order_is_dropped_when_columns_change(self):
        eng = FakeEngine()
        _, oid = self._chart(eng)
        eng.objects[oid]["qHyperCubeDef"]["qColumnOrder"] = [1, 0]
        eng.objects[oid]["columnWidths"] = [100, 100]
        eng.update_object(1, oid, None, None, None, None,
                          [{"expression": "Sum(A)"}, {"expression": "Sum(B)"}], None, {})
        assert "qColumnOrder" not in eng.objects[oid]["qHyperCubeDef"]
        assert "columnWidths" not in eng.objects[oid]

    def test_a_change_engine_rejects_is_rolled_back_and_not_saved(self):
        eng = FakeEngine(bad_fields={"Nope"})
        _, oid = self._chart(eng)
        before = __import__("copy").deepcopy(eng.objects[oid])
        saves = eng.saves
        out = eng.update_object(1, oid, None, None, None, [{"field": "Nope"}], None,
                                None, {})
        assert out["updated"] is False and out["problems"]
        assert eng.objects[oid] == before
        assert eng.saves == saves

    def test_a_wrong_shape_is_refused_before_anything_is_written(self):
        eng = FakeEngine()
        _, oid = self._chart(eng)
        before = __import__("copy").deepcopy(eng.objects[oid])
        with pytest.raises(ValueError):
            eng.update_object(1, oid, None, None, None, [], None, None, {})
        assert eng.objects[oid] == before

    def test_an_object_can_be_moved_and_resized(self):
        eng = FakeEngine()
        sheet_id, oid = self._chart(eng)
        out = eng.update_object(1, oid, sheet_id, None, None, None, None,
                                {"col": 12, "row": 0, "colspan": 12}, {})
        assert out["cell"] == {"col": 12, "row": 0, "colspan": 12, "rowspan": 6}

    def test_moving_needs_the_sheet(self):
        eng = FakeEngine()
        _, oid = self._chart(eng)
        with pytest.raises(ValueError, match="sheet_id"):
            eng.update_object(1, oid, None, None, None, None, None, {"col": 3, "row": 0}, {})

    def test_an_object_on_a_different_sheet_is_not_moved(self):
        eng = FakeEngine()
        _, oid = self._chart(eng)
        other = _sheet(eng, "Other")
        with pytest.raises(ValueError, match="not on sheet"):
            eng.update_object(1, oid, other, None, None, None, None, {"col": 1, "row": 0}, {})

    def test_a_missing_object_is_an_error(self):
        with pytest.raises(QlikEngineError, match="No object"):
            FakeEngine().update_object(1, "ghost", None, "x", None, None, None, None, {})

    def test_an_object_without_a_cube_has_no_columns_to_change(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        out = eng.add_object(1, sheet_id, "filterpane", "", "", [], [], ["A"], {}, {})
        with pytest.raises(ValueError, match="no dimensions or measures"):
            eng.update_object(1, out["object_id"], None, None, None,
                              [{"field": "A"}], None, None, {})


class TestUpdateTableColumns:
    def test_a_table_keeps_a_full_column_order_after_its_columns_change(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        oid = eng.add_object(1, sheet_id, "table", "", "", [{"field": "A"}],
                             [{"expression": "Sum(B)"}], [], {}, {})["object_id"]
        eng.update_object(1, oid, None, None, None,
                          [{"field": "A"}, {"field": "C"}], None, None, {})
        cube = eng.objects[oid]["qHyperCubeDef"]
        assert cube["qColumnOrder"] == cube["columnOrder"] == [0, 1, 2]
        assert cube["columnWidths"] == [-1, -1, -1]


class TestDeleteObject:
    def test_the_object_and_its_cell_both_go(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        oid = eng.add_object(1, sheet_id, "kpi", "", "", [], [{"expression": "1"}], [],
                             {}, {})["object_id"]
        out = eng.delete_object(1, oid, sheet_id)
        assert out["cell_removed"] is True
        assert oid not in eng.objects
        assert eng.objects[sheet_id]["cells"] == []

    def test_without_the_sheet_the_slot_is_reported_as_left(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        oid = eng.add_object(1, sheet_id, "kpi", "", "", [], [{"expression": "1"}], [],
                             {}, {})["object_id"]
        assert eng.delete_object(1, oid, None)["cell_removed"] is False

    def test_a_missing_object_is_an_error(self):
        with pytest.raises(QlikEngineError):
            FakeEngine().delete_object(1, "ghost", None)


class TestClientDefaults:
    """Engine accepts an object without these and then the client draws nothing."""

    @pytest.mark.parametrize("object_type,must_have", [
        ("barchart", ("color", "legend", "dataPoint", "components", "tooltip",
                      "gridLine", "dimensionAxis", "measureAxis", "scrollbar", "version")),
        ("linechart", ("color", "legend", "dataPoint", "components", "tooltip",
                       "lineType", "nullMode", "version")),
        ("piechart", ("color", "legend", "dataPoint", "donut", "components")),
        ("kpi", ("textAlign", "fontSize", "showMeasureTitle")),
    ])
    def test_a_chart_carries_what_the_editor_writes(self, object_type, must_have):
        dims = [] if object_type == "kpi" else [{"field": "A"}]
        props = au.build_object_properties(object_type, "T", "", dims,
                                           [{"expression": "Sum(B)"}])
        for key in must_have:
            assert key in props, key

    def test_defaults_do_not_overwrite_what_the_builder_chose(self):
        props = au.build_object_properties("barchart", "T", "", [{"field": "A"}],
                                           [{"expression": "Sum(B)"}])
        assert props["orientation"] == "vertical"
        assert props["qInfo"] == {"qType": "barchart"}

    def test_each_chart_gets_its_own_copy(self):
        a = au.build_object_properties("barchart", "", "", [{"field": "A"}],
                                       [{"expression": "1"}])
        b = au.build_object_properties("barchart", "", "", [{"field": "A"}],
                                       [{"expression": "1"}])
        a["color"]["auto"] = False
        assert b["color"]["auto"] is True

    def test_chart_cubes_are_always_expanded(self):
        for t in ("barchart", "linechart", "piechart"):
            cube = au.build_hypercube(t, [{"field": "A"}], [{"expression": "1"}])
            au.style_columns(t, cube)
            assert cube["qAlwaysFullyExpanded"] is True

    def test_a_bar_measure_is_a_bar_series(self):
        props = au.build_object_properties("barchart", "", "", [{"field": "A"}],
                                           [{"expression": "1"}])
        assert props["qHyperCubeDef"]["qMeasures"][0]["qDef"]["series"]["type"] == "bar"

    def test_a_format_is_not_overridden_by_the_template(self):
        with_format = au.build_measure({"expression": "1", "format": "0.0"})
        without = au.build_measure({"expression": "1"})
        assert with_format["qDef"]["numFormatFromTemplate"] is False
        assert without["qDef"]["numFormatFromTemplate"] is True

    def test_a_filter_pane_lists_its_children(self):
        assert au.build_filterpane_properties("F")["qChildListDef"]

    def test_updating_columns_keeps_the_chart_extras(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        oid = eng.add_object(1, sheet_id, "barchart", "", "", [{"field": "A"}],
                             [{"expression": "Sum(B)"}], [], {}, {})["object_id"]
        eng.update_object(1, oid, None, None, None, None,
                          [{"expression": "Avg(B)"}], None, {})
        cube = eng.objects[oid]["qHyperCubeDef"]
        assert cube["qAlwaysFullyExpanded"] is True
        assert cube["qMeasures"][0]["qDef"]["series"]["type"] == "bar"


class TestComboChart:
    def test_the_first_measure_is_a_bar_and_the_rest_lines(self):
        props = au.build_object_properties(
            "combochart", "", "", [{"field": "A"}],
            [{"expression": "Sum(B)"}, {"expression": "Sum(C)"}, {"expression": "Sum(D)"}])
        series = [m["qDef"]["series"] for m in props["qHyperCubeDef"]["qMeasures"]]
        assert series == [{"type": "bar", "axis": 0}, {"type": "line", "axis": 0},
                          {"type": "line", "axis": 0}]

    def test_series_and_axis_can_be_chosen(self):
        props = au.build_object_properties(
            "combochart", "", "", [{"field": "A"}],
            [{"expression": "Sum(B)", "series": "line", "axis": 1},
             {"expression": "Sum(C)", "series": "marker"}])
        series = [m["qDef"]["series"] for m in props["qHyperCubeDef"]["qMeasures"]]
        assert series == [{"type": "line", "axis": 1}, {"type": "marker", "axis": 0}]

    @pytest.mark.parametrize("measure", [
        {"expression": "1", "series": "area"}, {"expression": "1", "axis": 2},
        {"expression": "1", "axis": True}])
    def test_a_bad_series_or_axis_is_refused(self, measure):
        with pytest.raises(ValueError):
            au.build_hypercube("combochart", [{"field": "A"}], [measure])

    def test_series_on_another_chart_is_refused_rather_than_ignored(self):
        with pytest.raises(ValueError, match="only apply to combochart"):
            au.build_hypercube("barchart", [{"field": "A"}],
                               [{"expression": "1", "series": "line"}])

    def test_a_combo_needs_a_dimension_and_a_measure(self):
        with pytest.raises(ValueError):
            au.check_counts("combochart", [], [{}])
        with pytest.raises(ValueError):
            au.check_counts("combochart", [{}], [])

    def test_it_has_two_value_axes_and_an_expanded_cube(self):
        props = au.build_object_properties("combochart", "", "", [{"field": "A"}],
                                           [{"expression": "1"}])
        assert len(props["measureAxes"]) == 2
        assert props["qHyperCubeDef"]["qAlwaysFullyExpanded"] is True
        assert props["visualization"] == "combochart"


class TestContainer:
    CHILDREN = [
        {"object_type": "linechart", "label": "Primary", "condition": "=Only(axis) = 1",
         "dimensions": [{"field": "A"}], "measures": [{"expression": "Sum(B)"}]},
        {"object_type": "combochart", "label": "It's secondary",
         "dimensions": [{"field": "A"}], "measures": [{"expression": "Sum(B)"}]},
    ]

    def test_each_child_points_back_at_its_entry(self):
        props, children = au.build_container("T", "", self.CHILDREN)
        refs = [e["refId"] for e in props["children"]]
        assert [c["containerChildId"] for c in children] == refs
        assert len(set(refs)) == 2

    def test_a_label_is_a_quoted_string_expression(self):
        props, _ = au.build_container("", "", self.CHILDREN)
        assert props["children"][0]["label"] == {"qStringExpression": {"qExpr": "'Primary'"}}
        # an apostrophe in the text must not end the literal early
        assert props["children"][1]["label"]["qStringExpression"]["qExpr"] == "'It''s secondary'"

    def test_a_condition_is_carried_only_when_given(self):
        props, _ = au.build_container("", "", self.CHILDREN)
        assert props["children"][0]["condition"] == {
            "qStringExpression": {"qExpr": "=Only(axis) = 1"}}
        assert "condition" not in props["children"][1]

    def test_the_container_lists_its_children(self):
        props, _ = au.build_container("", "", self.CHILDREN)
        assert props["qChildListDef"]["qData"]["containerChildId"] == "/containerChildId"
        assert props["visualization"] == "container"

    @pytest.mark.parametrize("children", [[], None, "x", [{"label": "no type"}],
                                          [{"object_type": "filterpane"}],
                                          [{"object_type": "container"}], ["bar"]])
    def test_bad_children_are_refused(self, children):
        with pytest.raises(ValueError):
            au.build_container("", "", children)

    def test_children_are_validated_like_any_chart(self):
        with pytest.raises(ValueError, match="piechart takes"):
            au.build_container("", "", [{"object_type": "piechart",
                                         "dimensions": [], "measures": []}])

    def test_only_a_container_takes_children(self):
        with pytest.raises(ValueError, match="only container"):
            au.prepare_object("barchart", "", "", [{"field": "A"}], [{"expression": "1"}],
                              [], [{"object_type": "kpi"}], {})

    def test_a_container_takes_nothing_but_children(self):
        with pytest.raises(ValueError):
            au.prepare_object("container", "", "", [{"field": "A"}], [], [], self.CHILDREN, {})

    def test_extra_properties_cannot_replace_the_children(self):
        with pytest.raises(ValueError):
            au.check_extra_properties({"children": []})

    def test_the_engine_creates_the_container_and_its_charts(self):
        eng = FakeEngine()
        sheet_id = _sheet(eng)
        out = eng.add_object(1, sheet_id, "container", "T", "", [], [], [], {}, {},
                             self.CHILDREN)
        assert out["created"] is True
        kids = eng.children[out["object_id"]]
        assert [eng.objects[k]["qInfo"]["qType"] for k in kids] == ["linechart", "combochart"]
        assert out["cell"]["colspan"] == 24

    def test_a_bad_child_removes_the_whole_container(self):
        eng = FakeEngine(bad_fields={"Nope"})
        sheet_id = _sheet(eng)
        saves = eng.saves
        bad = [dict(self.CHILDREN[0], dimensions=[{"field": "Nope"}])]
        out = eng.add_object(1, sheet_id, "container", "", "", [], [], [], {}, {}, bad)
        assert out["created"] is False
        assert [i for i in eng.objects if i != sheet_id] == []
        assert eng.objects[sheet_id]["cells"] == []
        assert eng.saves == saves


class TestDeletingLeavesNoGhosts:
    """`DestroyObject` on the app leaves a sheet's child where it is: no cell,
    still an object. Objects are removed through their parent."""

    def _chart(self, eng, sid):
        return eng.add_object(1, sid, "kpi", "", "", [], [{"expression": "1"}], [],
                              {}, {})["object_id"]

    def test_the_object_is_removed_from_its_sheet_not_just_unplaced(self):
        eng = FakeEngine()
        sid = _sheet(eng)
        oid = self._chart(eng, sid)
        eng.delete_object(1, oid, sid)
        assert oid not in eng.objects
        assert oid not in eng.children.get(sid, [])
        assert "DestroyChild" in eng.calls

    def test_the_sheet_is_found_when_it_is_not_named(self):
        eng = FakeEngine()
        sid = _sheet(eng)
        oid = self._chart(eng, sid)
        eng.delete_object(1, oid, None)
        assert oid not in eng.objects

    def test_a_chart_inside_a_container_is_removed_from_the_container(self):
        eng = FakeEngine()
        sid = _sheet(eng)
        cid = eng.add_object(1, sid, "container", "", "", [], [], [], {}, {},
                             TestContainer.CHILDREN)["object_id"]
        inner = eng.children[cid][0]
        eng.delete_object(1, inner, None)
        assert inner not in eng.objects and inner not in eng.children[cid]

    def test_an_object_that_is_on_no_sheet_is_not_silently_ignored(self):
        eng = FakeEngine()
        sid = _sheet(eng)
        other = _sheet(eng, "Other")
        oid = self._chart(eng, sid)
        with pytest.raises(QlikEngineError, match="not on sheet"):
            eng.delete_object(1, oid, other)
        assert oid in eng.objects

    def test_deleting_a_sheet_takes_its_objects_with_it(self):
        eng = FakeEngine()
        sid = _sheet(eng)
        ids = [self._chart(eng, sid), self._chart(eng, sid)]
        out = eng.delete_sheet(1, sid)
        assert out["objects_removed"] == 2
        assert sid not in eng.objects and not any(i in eng.objects for i in ids)

    def test_a_rolled_back_chart_leaves_nothing_on_the_sheet(self):
        eng = FakeEngine(bad_fields={"Nope"})
        sid = _sheet(eng)
        eng.add_object(1, sid, "barchart", "", "", [{"field": "Nope"}],
                       [{"expression": "1"}], [], {}, {})
        assert eng.children.get(sid, []) == []
        assert [i for i in eng.objects if i != sid] == []
