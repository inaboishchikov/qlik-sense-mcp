"""Checking objects, trying scripts, reading reload logs."""

import contextlib
import copy
import json
from types import SimpleNamespace

import pytest

from qlik_sense_mcp_server.engine import authoring as au
from qlik_sense_mcp_server.engine import development as dev
from qlik_sense_mcp_server.engine import lint
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.tools import development as tools
from tests.test_authoring import FakeEngine, _sheet
from tests.test_app_options import OptionsEngine  # noqa: F401  (shares the fake)


def paths(findings, severity=None):
    return [f["path"] for f in findings if severity in (None, f["severity"])]


def messages(findings):
    return " | ".join(f["message"] for f in findings)


# ─── Objects built by this server are clean ─────────────────────────────────


class TestBuiltObjectsAreClean:
    DIM = [{"field": "A"}]
    MEAS = [{"expression": "Sum(B)"}]

    @pytest.mark.parametrize("object_type,dims,measures", [
        ("table", DIM, MEAS), ("barchart", DIM, MEAS), ("linechart", DIM, MEAS),
        ("piechart", DIM, MEAS), ("combochart", DIM, MEAS + [{"expression": "Sum(C)"}]),
        ("pivot-table", DIM, MEAS), ("kpi", [], MEAS),
    ])
    def test_no_findings_at_all(self, object_type, dims, measures):
        props = au.build_object_properties(object_type, "T", "", dims, measures)
        assert lint.lint_object(props) == []

    def test_a_filter_pane_and_a_container_are_clean(self):
        pane = au.build_filterpane_properties("F")
        assert lint.lint_object(pane, [au.build_listbox_properties("A")]) == []
        container, children = au.build_container("C", "", [
            {"object_type": "barchart", "label": "x", "dimensions": self.DIM,
             "measures": self.MEAS}])
        assert lint.lint_object(container, children) == []


# ─── What the incidents were, as findings ───────────────────────────────────


class TestTheEmptyTable:
    """A table with these gaps had data and drew nothing."""

    def table(self):
        return au.build_object_properties("table", "T", "", [{"field": "A"}],
                                          [{"expression": "Sum(B)"}])

    def test_a_missing_column_order_and_widths_are_errors(self):
        props = self.table()
        for key in ("qColumnOrder", "columnOrder", "columnWidths"):
            props["qHyperCubeDef"].pop(key)
        found = lint.lint_object(props)
        assert {"qHyperCubeDef.qColumnOrder", "qHyperCubeDef.columnOrder",
                "qHyperCubeDef.columnWidths"} <= set(paths(found, "error"))

    def test_an_empty_column_order_is_the_engine_default_and_an_error(self):
        props = self.table()
        props["qHyperCubeDef"]["qColumnOrder"] = []
        assert "qHyperCubeDef.qColumnOrder" in paths(lint.lint_object(props), "error")

    def test_an_order_for_the_wrong_number_of_columns(self):
        props = self.table()
        props["qHyperCubeDef"]["columnOrder"] = [0, 1, 2]
        assert "qHyperCubeDef.columnOrder" in paths(lint.lint_object(props), "error")

    def test_widths_for_the_wrong_number_of_columns(self):
        props = self.table()
        props["qHyperCubeDef"]["columnWidths"] = [-1]
        assert "qHyperCubeDef.columnWidths" in paths(lint.lint_object(props), "error")

    def test_columns_without_ids(self):
        props = self.table()
        for column in props["qHyperCubeDef"]["qDimensions"]:
            column["qDef"].pop("cId")
        assert "qHyperCubeDef.columns" in paths(lint.lint_object(props), "error")

    def test_two_columns_sharing_an_id(self):
        props = self.table()
        cube = props["qHyperCubeDef"]
        cube["qMeasures"][0]["qDef"]["cId"] = cube["qDimensions"][0]["qDef"]["cId"]
        assert "two columns share a cId" in messages(lint.lint_object(props))

    def test_missing_editor_defaults_are_named_as_a_warning(self):
        props = self.table()
        props.pop("totals")
        found = [f for f in lint.lint_object(props) if f["path"] == "properties"]
        assert found and found[0]["severity"] == "warning" and "totals" in found[0]["keys"]


class TestTheEmptyCharts:
    def bar(self):
        return au.build_object_properties("barchart", "T", "", [{"field": "A"}],
                                          [{"expression": "Sum(B)"}])

    def test_missing_color_legend_and_the_rest_are_listed(self):
        props = self.bar()
        for key in ("color", "legend", "dataPoint", "components", "tooltip"):
            props.pop(key)
        found = next(f for f in lint.lint_object(props) if f["path"] == "properties")
        assert {"color", "legend", "dataPoint", "components", "tooltip"} <= set(found["keys"])

    def test_a_chart_over_its_dimension_limit(self):
        props = self.bar()
        props["qHyperCubeDef"]["qDimensions"] *= 3
        assert "qHyperCubeDef.dimensions" in paths(lint.lint_object(props), "error")

    def test_a_pie_with_two_measures(self):
        props = au.build_object_properties("piechart", "", "", [{"field": "A"}],
                                           [{"expression": "1"}])
        props["qHyperCubeDef"]["qMeasures"] *= 2
        assert "qHyperCubeDef.measures" in paths(lint.lint_object(props), "error")

    def test_a_fetch_window_narrower_than_the_columns(self):
        props = self.bar()
        props["qHyperCubeDef"]["qInitialDataFetch"][0]["qWidth"] = 1
        assert "qHyperCubeDef.qInitialDataFetch" in paths(lint.lint_object(props), "error")

    def test_a_sort_order_naming_a_column_that_is_not_there(self):
        props = self.bar()
        props["qHyperCubeDef"]["qInterColumnSortOrder"] = [0, 5]
        assert "qHyperCubeDef.qInterColumnSortOrder" in paths(lint.lint_object(props), "error")

    def test_the_visualisation_must_match_the_type(self):
        props = self.bar()
        props["visualization"] = "linechart"
        assert "visualization" in paths(lint.lint_object(props), "error")

    def test_engines_own_verdict_is_an_error(self):
        layout = {"qHyperCube": {"qDimensionInfo": [
            {"qFallbackTitle": "Nope", "qError": {"qErrorCode": 7}}]}}
        found = lint.lint_object(self.bar(), layout=layout)
        assert any(f["path"] == "layout" and "dimension Nope" in f["message"] for f in found)

    def test_a_chart_with_no_hypercube(self):
        props = self.bar()
        props.pop("qHyperCubeDef")
        assert "qHyperCubeDef" in paths(lint.lint_object(props), "error")

    def test_an_object_without_a_type(self):
        assert lint.lint_object({"qInfo": {}})[0]["path"] == "qInfo.qType"


class TestTheUnfinishedFilterPane:
    def test_no_child_list_definition(self):
        props = au.build_filterpane_properties("F")
        props.pop("qChildListDef")
        found = lint.lint_object(props, [au.build_listbox_properties("A")])
        assert "qChildListDef" in paths(found, "error")

    def test_no_listboxes(self):
        found = lint.lint_object(au.build_filterpane_properties("F"), [])
        assert "children" in paths(found, "error")


class TestContainers:
    def make(self):
        return au.build_container("C", "", [
            {"object_type": "barchart", "label": "a", "dimensions": [{"field": "A"}],
             "measures": [{"expression": "1"}]},
            {"object_type": "linechart", "label": "b", "dimensions": [{"field": "A"}],
             "measures": [{"expression": "1"}]}])

    def test_an_entry_nothing_points_at(self):
        props, children = self.make()
        found = lint.lint_object(props, children[:1])
        assert "has no chart pointing back" in messages(found)

    def test_a_chart_pointing_at_nothing_the_container_lists(self):
        props, children = self.make()
        children[0]["containerChildId"] = "stale"
        assert "does not list" in messages(lint.lint_object(props, children))

    def test_a_chart_with_no_back_pointer(self):
        props, children = self.make()
        children[0].pop("containerChildId")
        assert "no containerChildId" in messages(lint.lint_object(props, children))

    def test_no_child_list_definition(self):
        props, children = self.make()
        props.pop("qChildListDef")
        assert "qChildListDef" in paths(lint.lint_object(props, children), "error")

    def test_no_children_at_all(self):
        props, _ = self.make()
        props["children"] = []
        assert "children" in paths(lint.lint_object(props, []), "error")


class TestSheetLayout:
    def sheet(self, *cells, rows=12):
        return {"columns": 24, "rows": rows, "cells": list(cells)}

    @staticmethod
    def cell(name, col, row, colspan=6, rowspan=3):
        return {"name": name, "col": col, "row": row, "colspan": colspan, "rowspan": rowspan}

    def test_a_clean_sheet(self):
        sheet = self.sheet(self.cell("a", 0, 0), self.cell("b", 6, 0))
        assert lint.lint_sheet_layout(sheet, ["a", "b"]) == []

    def test_an_object_on_no_cell_exists_but_is_not_shown(self):
        found = lint.lint_sheet_layout(self.sheet(self.cell("a", 0, 0)), ["a", "ghost"])
        assert found[0]["severity"] == "error" and found[0]["object_id"] == "ghost"
        assert "not shown" in found[0]["message"]

    def test_a_cell_for_an_object_that_is_gone(self):
        found = lint.lint_sheet_layout(self.sheet(self.cell("gone", 0, 0)), [])
        assert "refers to an object the sheet does not hold" in messages(found)

    def test_a_cell_off_the_grid(self):
        found = lint.lint_sheet_layout(self.sheet(self.cell("a", 20, 0, colspan=6)), ["a"])
        assert "does not fit" in messages(found)

    def test_overlapping_cells_are_a_warning(self):
        sheet = self.sheet(self.cell("a", 0, 0), self.cell("b", 3, 1))
        found = lint.lint_sheet_layout(sheet, ["a", "b"])
        assert [f["severity"] for f in found] == ["warning"] and "overlaps" in found[0]["message"]

    def test_a_cell_past_the_rows_is_a_warning(self):
        found = lint.lint_sheet_layout(self.sheet(self.cell("a", 0, 10, rowspan=5)), ["a"])
        assert found[0]["severity"] == "warning"

    def test_summary(self):
        assert lint.summarise([]) == {"ok": True, "errors": 0, "warnings": 0}
        assert lint.summarise([{"severity": "error"}, {"severity": "warning"}]) == {
            "ok": False, "errors": 1, "warnings": 1}


class DevEngine(OptionsEngine):
    """The fake Engine plus what the development methods ask for."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.request_id = 0           # the real client counts its requests
        self.progress = {}
        self.reload_ok = True
        self.global_calls = []

    def send_request(self, method, params=None, handle=-1, timeout=None):
        if method == "DoReload":
            self.calls.append(method)
            self.request_id += 1
            self.last_reload_id = self.request_id
            return {"qReturn": self.reload_ok}
        if method == "GetConnections":
            return {"qConnections": getattr(self, "connections", [])}
        if method == "GetProgress":
            self.global_calls.append((params, handle))
            return {"qProgressData": self.progress}
        return super().send_request(method, params, handle, timeout)


class TestOnEngine:
    def test_validating_a_clean_sheet(self):
        eng = DevEngine()
        sid = _sheet(eng)
        eng.add_object(1, sid, "kpi", "K", "", [], [{"expression": "Sum(A)"}], [], {}, {})
        eng.add_object(1, sid, "barchart", "B", "", [{"field": "A"}],
                       [{"expression": "Sum(B)"}], [], {}, {})
        out = eng.validate_sheet(1, sid)
        assert out["ok"] is True and len(out["objects"]) == 2 and out["errors"] == 0

    def test_validating_finds_the_object_not_shown(self):
        eng = DevEngine()
        sid = _sheet(eng)
        oid = eng.add_object(1, sid, "kpi", "K", "", [], [{"expression": "1"}], [],
                             {}, {})["object_id"]
        eng.objects[sid]["cells"] = []        # the object stays, its cell goes
        out = eng.validate_sheet(1, sid)
        assert out["ok"] is False and out["sheet_findings"][0]["object_id"] == oid

    def test_validating_reads_engines_verdict_on_a_child(self):
        eng = DevEngine(bad_fields={"Nope"})
        sid = _sheet(eng)
        pane = au.build_filterpane_properties("F")
        created = eng.send_request("CreateChild", [pane], handle=eng._handle(sid))
        eng.send_request("CreateChild", [au.build_listbox_properties("Nope")],
                         handle=created["qReturn"]["qHandle"])
        out = eng.validate_object(1, created["qReturn"]["qGenericId"])
        assert out["ok"] is False and "listbox" in messages(out["findings"])

    def test_a_missing_object_is_an_error(self):
        with pytest.raises(Exception, match="No object"):
            DevEngine().validate_object(1, "ghost")


class TestScriptDryRun:
    def test_a_good_script_checks_out_and_nothing_changes(self):
        eng = DevEngine(script="LOAD 1 AS a AUTOGENERATE 1;")
        saves = eng.saves
        out = eng.check_script(1, "LOAD 2 AS b AUTOGENERATE 1;")
        assert out["ok"] is True
        assert eng.script == "LOAD 1 AS a AUTOGENERATE 1;"   # the original is back
        assert eng.saves == saves                             # nothing saved

    def test_errors_are_reported_and_the_original_is_still_restored(self):
        eng = DevEngine(script="ORIGINAL;", syntax_errors=[
            {"qTabIx": 0, "qLineInTab": 0, "qTextPos": 0, "qErrLen": 3}])
        out = eng.check_script(1, "BAD SCRIPT")
        assert out["ok"] is False and out["syntax_errors"][0]["text"] == "BAD"
        assert eng.script == "ORIGINAL;" and eng.saves == 0

    def test_without_a_candidate_the_current_script_is_checked(self):
        eng = DevEngine(script="CURRENT;")
        out = eng.check_script(1)
        assert out["ok"] is True and eng.script == "CURRENT;"
        assert eng.calls.count("SetScript") == 0

    def test_the_original_comes_back_even_when_the_check_itself_fails(self):
        eng = DevEngine(script="ORIGINAL;")
        original = eng.send_request

        def failing(method, params=None, handle=-1, timeout=None):
            if method == "CheckScriptSyntax":
                raise RuntimeError("engine hiccup")
            return original(method, params, handle, timeout)
        eng.send_request = failing
        with pytest.raises(RuntimeError):
            eng.check_script(1, "OTHER;")
        assert eng.script == "ORIGINAL;"


class TestReloadLog:
    FAILED = {"qErrorData": [
        {"qErrorString": "Connection not found: x", "qLine": "LOAD * FROM [lib://x/y.qvd]",
         "qErrorDataCode": "EDC_ERROR"},
        {"qErrorString": "Connection not found: x", "qLine": "LOAD * FROM [lib://x/y.qvd]",
         "qErrorDataCode": "EDC_ERROR"}]}

    def test_a_failed_reload_names_the_failing_statement_once(self):
        eng = DevEngine()
        eng.reload_ok, eng.progress = False, self.FAILED
        saves = eng.saves
        out = eng.reload_logged(1)
        assert out["reloaded"] is False
        assert out["errors"] == [{"message": "Connection not found: x",
                                  "statement": "LOAD * FROM [lib://x/y.qvd]",
                                  "severity": "error"}]
        assert eng.saves == saves                  # a failed reload is not saved

    def test_progress_is_asked_for_by_the_reloads_own_request_id(self):
        eng = DevEngine()
        eng.reload_logged(1)
        params, handle = eng.global_calls[0]
        assert params == [eng.last_reload_id] and handle == -1   # progress is global

    def test_a_good_reload_is_saved_and_its_log_returned(self):
        eng = DevEngine()
        eng.progress = {"qMillisecs": 1500, "qPersistentProgressMessages": [
            {"qMessageParameters": ["Orders", "<<", "1 000", "lines fetched"]},
            {"qMessageParameters": ["", "", ""]}]}
        saves = eng.saves
        out = eng.reload_logged(1)
        assert out["reloaded"] is True and eng.saves == saves + 1
        assert out["log"] == ["Orders << 1 000 lines fetched"] and out["seconds"] == 1.5

    def test_an_unreadable_progress_report_does_not_hide_the_outcome(self):
        eng = DevEngine()
        original = eng.send_request

        def failing(method, params=None, handle=-1, timeout=None):
            if method == "GetProgress":
                raise RuntimeError("no such method")
            return original(method, params, handle, timeout)
        eng.send_request = failing
        out = eng.reload_logged(1)
        assert out["reloaded"] is True and "could not be read" in out["note"]


class TestScriptTool:
    def test_tool_reports_ok(self, monkeypatch):
        eng = DevEngine(script="X;")
        eng.ensure_app = lambda *_a, **_k: 1
        monkeypatch.setattr(context, "engine_api", eng)
        monkeypatch.setattr(context, "repo_api", SimpleNamespace())
        out = json.loads(tools.check_app_script(app_id="A", script="Y;"))
        assert out["ok"] is True and eng.script == "X;"


class TestWhatIsNotAFinding:
    def test_an_unmet_calculation_condition_is_not_an_error(self):
        """Editor-made objects with a calc condition answer 7005 until a
        selection satisfies it."""
        layout = {"qHyperCube": {"qError": {"qErrorCode": 7005},
                                 "qDimensionInfo": [{"qFallbackTitle": "d",
                                                     "qError": {"qErrorCode": 7014}}],
                                 "qMeasureInfo": []}}
        props = au.build_object_properties("barchart", "", "", [{"field": "A"}],
                                           [{"expression": "1"}])
        found = lint.lint_object(props, layout=layout)
        assert [f["severity"] for f in found] == ["info"]
        assert "calculation condition" in found[0]["message"]
        assert lint.summarise(found)["ok"] is True

    def test_creation_is_not_rolled_back_for_an_unmet_condition(self):
        layout = {"qHyperCube": {"qError": {"qErrorCode": 7005}}}
        assert au.layout_problems(layout) == []
        assert au.layout_notes(layout)

    def test_a_real_error_next_to_a_condition_is_still_an_error(self):
        layout = {"qHyperCube": {"qError": {"qErrorCode": 7005},
                                 "qDimensionInfo": [{"qFallbackTitle": "Nope",
                                                     "qError": {"qErrorCode": 7}}]}}
        assert len(au.layout_problems(layout)) == 1

    def test_a_linked_object_is_checked_only_by_its_layout(self):
        """It inherits its properties from a master object."""
        linked = {"qInfo": {"qType": "filterpane"}, "qExtendsId": "abc", "qMetaDef": {}}
        assert lint.lint_object(linked, []) == []

    def test_text_alignment_is_expected_on_table_columns_only(self):
        bar = au.build_object_properties("barchart", "", "", [{"field": "A"}],
                                         [{"expression": "1"}])
        for column in bar["qHyperCubeDef"]["qDimensions"] + bar["qHyperCubeDef"]["qMeasures"]:
            column["qDef"].pop("textAlign")
        assert lint.lint_object(bar) == []
        table = au.build_object_properties("table", "", "", [{"field": "A"}],
                                           [{"expression": "1"}])
        table["qHyperCubeDef"]["qDimensions"][0]["qDef"].pop("textAlign")
        assert any("textAlign" in f["message"] for f in lint.lint_object(table))


def lines(found, severity="error"):
    return [(f["line"], f["message"]) for f in found if f["severity"] == severity]


class TestScriptScan:
    """What Qlik's own syntax check lets through."""

    def test_a_clean_script(self):
        script = "Orders:\nLOAD a, b FROM [lib://data/x.qvd] (qvd);\nSTORE Orders INTO [lib://data/y.qvd];\n"
        assert dev.scan_script(script, ["data"]) == []

    @pytest.mark.parametrize("script,fragment", [
        ("LOAD a FROM [x.qvd;\nLOAD b;", "[ is never closed"),
        ("LOAD Sum(a, b FROM x;", "( is never closed"),
        ("LOAD a) FROM x;", ") has no matching ("),
        ("LOAD a] FROM x;", "] has no matching ["),
        ("LOAD 'abc FROM x;", "string is never closed"),
        ('LOAD "abc FROM x;', "string is never closed"),
        ("/* never ends\nLOAD a;", "comment is never closed"),
    ])
    def test_unbalanced_text_is_an_error_with_its_line(self, script, fragment):
        found = dev.scan_script(script)
        assert any(fragment in m for _l, m in lines(found)), found

    def test_the_line_is_where_the_problem_starts(self):
        found = dev.scan_script("LOAD a;\nLOAD b;\nLOAD (c FROM x;\n")
        assert lines(found)[0][0] == 3

    def test_a_missing_final_semicolon_is_a_warning(self):
        found = dev.scan_script("LOAD 1 AS a AUTOGENERATE 1")
        assert lines(found, "warning") and not lines(found)

    def test_text_in_comments_and_quotes_is_not_scanned(self):
        script = ("// don't ( [ worry\n/* it's ( here */\nREM isn't a problem ( ;\n"
                  "LOAD 'a (b' AS x, \"c [d\" AS y, 'it''s' AS z AUTOGENERATE 1;\n")
        assert dev.scan_script(script) == []

    def test_brackets_hold_names_with_odd_characters(self):
        script = "LOAD [Revenue (USD)] AS r, [a 'quoted' b] AS q FROM [lib://c/f.xlsx] (ooxml);"
        assert dev.scan_script(script) == []

    def test_a_lib_path_is_not_a_comment(self):
        script = "LOAD * FROM lib://data/file.csv (txt);\nLOAD 1 AS a AUTOGENERATE 1;"
        assert dev.scan_script(script, ["data"]) == []

    def test_a_connection_that_does_not_exist(self):
        found = dev.scan_script("LOAD * FROM [lib://nope/x.qvd] (qvd);", ["data"])
        assert "'nope'" in lines(found)[0][1] and lines(found)[0][0] == 1

    def test_connection_names_are_matched_without_regard_to_case(self):
        assert dev.scan_script("LOAD * FROM [lib://DATA/x.qvd] (qvd);", ["data"]) == []

    def test_connections_are_not_checked_when_they_could_not_be_listed(self):
        assert dev.scan_script("LOAD * FROM [lib://nope/x.qvd] (qvd);", None) == []

    def test_an_empty_script_has_nothing_to_report(self):
        assert dev.scan_script("") == [] and dev.scan_script("// only a comment\n") == []


class TestScriptToolCombinesBothChecks:
    def test_the_scan_catches_what_qliks_check_lets_through(self):
        eng = DevEngine(script="OLD;")
        eng.connections = [{"qName": "data", "qId": "1", "qType": "folder",
                            "qConnectionString": "D:\\"}]
        out = eng.check_script(1, "LOAD * FROM [lib://missing/x.qvd] (qvd);")
        assert out["syntax_errors"] == []            # Qlik says fine
        assert out["ok"] is False                    # the scan does not
        assert "missing" in out["scan"][0]["message"] and out["connections_checked"] is True
        assert eng.script == "OLD;"

    def test_a_clean_script_passes_both(self):
        eng = DevEngine()
        eng.connections = [{"qName": "data", "qId": "1", "qType": "folder",
                            "qConnectionString": "D:\\"}]
        out = eng.check_script(1, "LOAD * FROM [lib://data/x.qvd] (qvd);")
        assert out["ok"] is True and out["scan"] == []


class TestFailureShapes:
    """A failed reload is reported in two shapes; the caller reads one place."""

    AS_MESSAGE = {"qErrorData": [], "qPersistentProgressMessages": [
        {"qMessageCode": 10,
         "qMessageParameters": ["Connection not found: x\r\nOrders:\nLOAD * FROM [lib://x/y.qvd]"]}]}

    def test_a_script_error_message_becomes_the_error(self):
        out = dev.parse_progress(self.AS_MESSAGE, failed=True)
        assert out["errors"] == [{"message": "Connection not found: x",
                                  "statement": "Orders:\nLOAD * FROM [lib://x/y.qvd]",
                                  "severity": "error"}]
        assert out["log"] == []                       # it is not repeated as a log line

    def test_a_success_keeps_its_messages_as_the_log(self):
        progress = {"qPersistentProgressMessages": [
            {"qMessageCode": 5, "qMessageParameters": ["Orders << AUTOGENERATE(500)"]}]}
        out = dev.parse_progress(progress, failed=False)
        assert out["errors"] == [] and out["log"] == ["Orders << AUTOGENERATE(500)"]

    def test_error_data_wins_when_there_is(self):
        out = dev.parse_progress(
            {"qErrorData": [{"qErrorString": "boom", "qLine": "LOAD x"}],
             "qPersistentProgressMessages": [
                 {"qMessageCode": 10, "qMessageParameters": ["other"]}]}, failed=True)
        assert [e["message"] for e in out["errors"]] == ["boom"]

    def test_a_failure_with_no_messages_at_all_has_no_errors_to_show(self):
        assert dev.parse_progress({}, failed=True)["errors"] == []

    def test_the_reload_tool_path_uses_it(self):
        eng = DevEngine()
        eng.reload_ok, eng.progress = False, self.AS_MESSAGE
        out = eng.reload_logged(1)
        assert out["reloaded"] is False and out["errors"][0]["message"] == "Connection not found: x"
