"""Writing to an app: the load script, sheets, and the objects on them.

Everything else in the Engine client reads. This is the one mixin that
changes an app, so it is built around two rules:

* A change is checked before it is kept. A chart is created, its layout is
  read back, and Engine's own validation errors (a field that does not
  exist, an expression that does not parse) decide whether it stays. A
  load script is syntax-checked the same way. A broken result is rolled
  back rather than left on a sheet for somebody to find.
* Nothing is saved until the check passed. `DoSave` is the last step.

The functions at module level are pure — they build Engine property
structures and read layouts — so they can be tested without a socket.
"""

import copy
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..exceptions import QlikEngineError

logger = logging.getLogger(__name__)

# The sheet grid Qlik Sense lays objects on.
GRID_COLUMNS = 24
GRID_ROWS = 12

# Visualisations this module knows how to build. Each is a name Engine
# stores in `qInfo.qType` and `visualization`.
HYPERCUBE_TYPES = ("kpi", "table", "pivot-table", "barchart", "linechart", "piechart",
                    "combochart")
OBJECT_TYPES = HYPERCUBE_TYPES + ("filterpane", "container")
# A container holds charts; it does not hold filter panes or other containers.
CONTAINER_CHILD_TYPES = HYPERCUBE_TYPES

# (colspan, rowspan) on the 24x12 grid when the caller does not say.
DEFAULT_SIZE: Dict[str, Tuple[int, int]] = {
    "kpi": (6, 3),
    "table": (24, 8),
    "pivot-table": (24, 8),
    "barchart": (12, 6),
    "linechart": (12, 6),
    "piechart": (8, 6),
    "combochart": (12, 6),
    "filterpane": (24, 2),
    "container": (24, 8),
}

# (min, max) dimensions and measures each type accepts; None is unbounded.
# `table` counts columns, so it takes any mix of the two.
_LIMITS: Dict[str, Dict[str, Tuple[int, Optional[int]]]] = {
    "kpi": {"dimensions": (0, 0), "measures": (1, 2)},
    "table": {"dimensions": (0, None), "measures": (0, None)},
    "pivot-table": {"dimensions": (1, None), "measures": (1, None)},
    "barchart": {"dimensions": (1, 2), "measures": (1, None)},
    "linechart": {"dimensions": (1, 2), "measures": (1, None)},
    "piechart": {"dimensions": (1, 1), "measures": (1, 1)},
    "combochart": {"dimensions": (1, 2), "measures": (1, None)},
}

_PROTECTED_KEYS = ("qInfo", "qHyperCubeDef", "visualization", "cells", "children")

SERIES_TYPES = ("bar", "line", "marker")


# ─── Pure builders ─────────────────────────────────────────────────────────


def _column_id() -> str:
    """The `cId` Qlik Sense's own editor gives every column.

    The table visualisation keys its columns, widths and order by it. A
    column without one is accepted by Engine and has data, but the table
    in the client has nothing to draw it under.
    """
    return uuid.uuid4().hex[:8]


def _column_defaults() -> Dict[str, Any]:
    """What the client's editor writes on every column.

    The table in the client reads `autoSort` and `textAlign` off each
    column without checking they are there.
    """
    return {
        "cId": _column_id(),
        "autoSort": True,
        "textAlign": {"auto": True, "align": "left"},
    }


def build_dimension(spec: Any, index: int = 0) -> Dict[str, Any]:
    """One `qDimensions` entry from `{field | expression | library_id, label}`."""
    if not isinstance(spec, dict):
        raise ValueError(f"dimensions[{index}] must be an object, got {type(spec).__name__}")
    given = [k for k in ("field", "expression", "library_id") if spec.get(k)]
    if len(given) != 1:
        raise ValueError(
            f"dimensions[{index}] needs exactly one of field, expression, library_id "
            f"(got {given or 'none'})")

    if spec.get("library_id"):
        return {"qLibraryId": spec["library_id"], "qDef": _column_defaults()}

    if spec.get("field"):
        definition = str(spec["field"])
    else:
        expr = str(spec["expression"]).strip()
        definition = expr if expr.startswith("=") else "=" + expr
    label = spec.get("label") or (spec.get("field") or "")
    return {
        "qDef": {
            **_column_defaults(),
            "qGrouping": "N",
            "othersLabel": "Others",
            "qFieldDefs": [definition],
            "qFieldLabels": [label],
            "qSortCriterias": [
                {"qSortByAscii": 1, "qSortByLoadOrder": 1, "qExpression": {}}],
        },
        "qNullSuppression": not spec.get("include_nulls", False),
    }


def build_measure(spec: Any, index: int = 0) -> Dict[str, Any]:
    """One `qMeasures` entry from `{expression | library_id, label, format, sort}`."""
    if not isinstance(spec, dict):
        raise ValueError(f"measures[{index}] must be an object, got {type(spec).__name__}")
    given = [k for k in ("expression", "library_id") if spec.get(k)]
    if len(given) != 1:
        raise ValueError(
            f"measures[{index}] needs exactly one of expression, library_id "
            f"(got {given or 'none'})")

    if spec.get("library_id"):
        measure: Dict[str, Any] = {"qLibraryId": spec["library_id"],
                                   "qDef": _column_defaults()}
    else:
        # Engine stores a measure expression without the leading "=".
        expression = str(spec["expression"]).strip()
        if expression.startswith("="):
            expression = expression[1:].lstrip()
        definition: Dict[str, Any] = {**_column_defaults(), "qDef": expression}
        if spec.get("label"):
            definition["qLabel"] = spec["label"]
        if spec.get("format"):
            # Separators are stated, not inherited: left to the app's locale,
            # "#,##0.00" came out as "6490,500.00" on a comma-decimal app.
            definition["qNumFormat"] = {"qType": "F", "qFmt": str(spec["format"]),
                                        "qDec": ".", "qThou": ","}
        measure = {"qDef": definition}

    if spec.get("series") is not None or spec.get("axis") is not None:
        series = spec.get("series", "bar")
        axis = spec.get("axis", 0)
        if series not in SERIES_TYPES:
            raise ValueError(f"measures[{index}].series must be one of {list(SERIES_TYPES)}, "
                             f"got {series!r}")
        if axis not in (0, 1) or isinstance(axis, bool):
            raise ValueError(f"measures[{index}].axis must be 0 (left) or 1 (right), got {axis!r}")
        measure["qDef"]["series"] = {"type": series, "axis": axis}
    if "qLibraryId" not in measure:
        # From a template unless the caller gave a format, which the
        # template would otherwise override.
        measure["qDef"]["numFormatFromTemplate"] = not spec.get("format")
    measure.setdefault("qAttributeExpressions", [])
    measure.setdefault("qAttributeDimensions", [])
    measure.setdefault("qTrendLines", [])

    sort = spec.get("sort")
    if sort:
        if sort not in ("asc", "desc"):
            raise ValueError(f"measures[{index}].sort must be 'asc' or 'desc', got {sort!r}")
        measure["qSortBy"] = {"qSortByNumeric": 1 if sort == "asc" else -1}
    return measure


def inter_column_sort_order(cube: Dict[str, Any]) -> List[int]:
    """Sort priority of the columns: sorted measures first, then dimensions.

    Engine sorts by the columns in this order. Without it a chart ignores a
    measure's `qSortBy` and the bars come out alphabetical.
    """
    dims = cube.get("qDimensions") or []
    measures = cube.get("qMeasures") or []
    sorted_measures = [len(dims) + i for i, m in enumerate(measures)
                       if (m.get("qSortBy") or {}).get("qSortByNumeric")]
    rest_measures = [len(dims) + i for i in range(len(measures))
                     if len(dims) + i not in sorted_measures]
    return sorted_measures + list(range(len(dims))) + rest_measures


def apply_table_columns(cube: Dict[str, Any]) -> None:
    """Column order and widths, written where the client's table reads them.

    The editor stores the order twice (`qColumnOrder` for Engine,
    `columnOrder` for the visualisation) and one width per column, -1
    meaning automatic. A table missing any of them is created and holds
    data but the client cannot draw it, and gives it no properties panel.
    """
    order = natural_column_order(cube)
    cube["qColumnOrder"] = order
    cube["columnOrder"] = list(order)
    cube["columnWidths"] = [-1] * len(order)


def natural_column_order(cube: Dict[str, Any]) -> List[int]:
    """Columns as listed: dimensions, then measures.

    Engine fills `qColumnOrder` with an empty list when none is given, and
    a table with an empty order has no columns to show.
    """
    return list(range(len(cube.get("qDimensions") or []) + len(cube.get("qMeasures") or [])))


def check_counts(object_type: str, dimensions: List[Any], measures: List[Any]) -> None:
    """Refuse a dimension/measure count the visualisation cannot draw."""
    limits = _LIMITS[object_type]
    for name, items in (("dimensions", dimensions), ("measures", measures)):
        low, high = limits[name]
        if len(items) < low or (high is not None and len(items) > high):
            span = f"{low}" if low == high else f"{low}..{high if high is not None else 'many'}"
            raise ValueError(
                f"{object_type} takes {span} {name}, got {len(items)}")
    if object_type == "table" and not dimensions and not measures:
        raise ValueError("table needs at least one dimension or measure")


def build_hypercube(object_type: str, dimensions: List[Any], measures: List[Any]) -> Dict[str, Any]:
    """`qHyperCubeDef` for a chart, with the counts checked."""
    check_counts(object_type, dimensions, measures)
    if object_type != "combochart":
        for i, spec in enumerate(measures):
            if isinstance(spec, dict) and (spec.get("series") is not None
                                           or spec.get("axis") is not None):
                raise ValueError(
                    f"measures[{i}]: series and axis only apply to combochart, not {object_type}")
    cube: Dict[str, Any] = {
        "qDimensions": [build_dimension(d, i) for i, d in enumerate(dimensions)],
        "qMeasures": [build_measure(m, i) for i, m in enumerate(measures)],
        "qSuppressZero": False,
        "qSuppressMissing": True,
        "qMode": "P" if object_type == "pivot-table" else "S",
    }
    set_fetch_window(cube)
    cube["qInterColumnSortOrder"] = inter_column_sort_order(cube)
    if object_type == "table":
        apply_table_columns(cube)
    return cube


def set_fetch_window(cube: Dict[str, Any]) -> None:
    """Initial data fetch wide enough for every column, within Engine's cell cap."""
    columns = max(1, len(cube.get("qDimensions") or []) + len(cube.get("qMeasures") or []))
    cube["qInitialDataFetch"] = [
        {"qTop": 0, "qLeft": 0, "qWidth": columns, "qHeight": max(1, min(500, 9000 // columns))}]


_COLOR = {
    "auto": True, "mode": "primary", "useBaseColors": "off",
    "paletteColor": {"index": 6}, "useDimColVal": True, "useMeasureGradient": True,
    "persistent": False, "expressionIsColor": True, "expressionLabel": "",
    "measureScheme": "sg", "reverseScheme": False, "dimensionScheme": "12",
    "autoMinMax": True, "measureMin": 0, "measureMax": 10,
}
_DIMENSION_AXIS = {"continuousAuto": True, "show": "labels", "label": "auto",
                   "dock": "near", "axisDisplayMode": "auto", "maxVisibleItems": 10}
_MEASURE_AXIS = {"show": "labels", "dock": "near", "spacing": 1, "autoMinMax": True,
                 "minMax": "min", "min": 0, "max": 10, "logarithmic": False}

# What the editor writes on a new object. The client reads most of these
# without checking they exist, so an object created without them is
# accepted by Engine, answers with data, and draws nothing. Taken from
# objects the editor made on a live server; colours are automatic and no
# value refers to anything in a particular app.
_VISUAL_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "kpi": {
        "footnote": "", "showDetails": False, "showMeasureTitle": True,
        "textAlign": "center", "fontSize": "M", "useLink": False,
    },
    "barchart": {
        "footnote": "", "showDetails": False,
        "components": [{"key": "bar", "style": {"width": 0.7, "strokeColor": {
            "index": -1, "color": "#205891"}, "strokeWidth": "none"}}],
        "scrollbar": "bar", "scrollStartPos": 0,
        "gridLine": {"auto": True, "spacing": 2},
        "dataPoint": {"showLabels": True, "showSegmentLabels": False,
                      "showTotalLabels": True, "show": False,
                      "showLineLabels": False, "showBarLabels": True},
        "color": _COLOR,
        "legend": {"show": False, "dock": "auto", "showTitle": True},
        "dimensionAxis": _DIMENSION_AXIS, "preferContinuousAxis": False,
        "measureAxis": _MEASURE_AXIS,
        "tooltip": {"auto": True, "hideBasic": False},
        "version": "1.1.0", "showMiniChartForContinuousAxis": True,
    },
    "linechart": {
        "footnote": "", "showDetails": False, "scrollStartPos": 1,
        "dataPoint": {"show": True, "showLabels": False, "showSegmentLabels": False,
                      "showTotalLabels": False},
        "gridLine": {"auto": True, "spacing": 2},
        "color": _COLOR,
        "legend": {"show": False, "dock": "auto", "showTitle": True},
        "dimensionAxis": _DIMENSION_AXIS, "preferContinuousAxis": True,
        "showMiniChartForContinuousAxis": True, "measureAxis": _MEASURE_AXIS,
        "disableNavMenu": False,
        "components": [{"key": "line", "style": {"lineThickness": 2, "lineCurve": "linear"}},
                       {"key": "axis"}, {"key": "label"}, {"key": "legend"}],
        "scrollbar": "bar",
        "tooltip": {"auto": True, "hideBasic": False, "title": "", "description": ""},
        "version": "1.4.0", "lineType": "line", "stackedArea": False,
        "separateStacking": False, "nullMode": "gap",
    },
    "piechart": {
        "footnote": "", "showDetails": False,
        "donut": {"showAsDonut": True}, "dimensionTitle": False,
        "dataPoint": {"auto": True, "labelMode": "share", "labelValueMode": "arc",
                      "showLabels": True},
        "color": _COLOR,
        "legend": {"show": False, "dock": "auto", "showTitle": True},
        "components": [],
    },
    "combochart": {
        "footnote": "", "showDetails": False,
        "components": [{"key": "line", "style": {"lineThickness": 2, "lineType": "solid",
                                                 "lineCurve": "linear"}},
                       {"key": "bar"}, {"key": "axis"}, {"key": "legend"}],
        "barGrouping": {"grouping": "grouped"}, "scrollStartPos": 1, "nullMode": "gap",
        "dataPoint": {"show": True, "showLineLabels": False, "showBarLabels": False},
        "color": _COLOR,
        "legend": {"show": False, "dock": "auto", "showTitle": True},
        "dimensionAxis": _DIMENSION_AXIS, "preferContinuousAxis": False,
        "showMiniChartForContinuousAxis": True,
        "measureAxes": [
            {"show": "all", "dock": "near", "spacing": 1, "autoMinMax": True,
             "minMax": "min", "min": 0, "max": 10},
            {"show": "all", "dock": "far", "spacing": 1, "autoMinMax": True,
             "minMax": "min", "min": 0, "max": 10}],
        "version": "0.23.1",
    },
    "pivot-table": {"footnote": "", "showDetails": False},
    "table": {
        "footnote": "", "showDetails": True,
        "totals": {"show": True, "position": "noTotals", "label": "Totals"},
        "scrolling": {"keepFirstColumnInView": False, "horizontal": True},
        "multiline": {"wrapTextInHeaders": True, "wrapTextInCells": True},
    },
}

# Charts whose cube the editor marks as always expanded.
_EXPANDED_CUBE = ("barchart", "linechart", "piechart", "combochart")


def style_columns(object_type: str, cube: Dict[str, Any]) -> None:
    """Per-type extras on the cube and its columns; idempotent."""
    if object_type in _EXPANDED_CUBE:
        cube["qAlwaysFullyExpanded"] = True
    if object_type == "combochart":
        # First measure a bar, the rest lines, on the left axis, unless said.
        for i, measure in enumerate(cube.get("qMeasures") or []):
            if "qLibraryId" not in measure:
                measure["qDef"].setdefault(
                    "series", {"type": "bar" if i == 0 else "line", "axis": 0})
    if object_type == "barchart":
        for measure in cube.get("qMeasures") or []:
            if "qLibraryId" not in measure:
                measure["qDef"].setdefault(
                    "series", {"type": "bar", "axis": 0, "marker": "circle",
                               "markerFill": True})


def build_object_properties(object_type: str, title: str = "", subtitle: str = "",
                            dimensions: Optional[List[Any]] = None,
                            measures: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Full property structure for a new chart."""
    props: Dict[str, Any] = {
        "qInfo": {"qType": object_type},
        "visualization": object_type,
        "showTitles": bool(title or subtitle),
        "title": title or "",
        "subtitle": subtitle or "",
        "qHyperCubeDef": build_hypercube(object_type, dimensions or [], measures or []),
    }
    if object_type in ("barchart", "linechart", "combochart"):
        props["orientation"] = "vertical"
    if object_type == "barchart":
        props["barGrouping"] = {"grouping": "grouped"}
    if object_type == "pivot-table":
        props["qHyperCubeDef"]["qNoOfLeftDims"] = -1
    for key, value in _VISUAL_DEFAULTS.get(object_type, {}).items():
        props.setdefault(key, copy.deepcopy(value))
    style_columns(object_type, props["qHyperCubeDef"])
    return props


def build_filterpane_properties(title: str = "") -> Dict[str, Any]:
    return {
        "qInfo": {"qType": "filterpane"},
        "visualization": "filterpane",
        "showTitles": bool(title),
        "title": title or "",
        "subtitle": "",
        # Without this Engine does not list the listboxes as children, and
        # the client shows an incomplete visualisation.
        "qChildListDef": {"qData": {"title": "/title"}},
    }


def _string_expression(expression: str) -> Dict[str, Any]:
    return {"qStringExpression": {"qExpr": expression}}


def _quoted(text: str) -> str:
    """A Qlik string literal for `text`."""
    return "'" + str(text).replace("'", "''") + "'"


def build_container(title: str, subtitle: str, child_specs: List[Any],
                    extra_children_defaults: Optional[Dict[str, Any]] = None
                    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """A container and the property structures of the charts inside it.

    Each entry of `child_specs` is `{object_type, label, condition?, title?,
    subtitle?, dimensions?, measures?, extra_properties?}`. `label` is the
    tab or dropdown text; `condition` is a Qlik expression that decides
    when the child is shown. A child points back at its entry in the
    container through `containerChildId`.
    """
    if not isinstance(child_specs, list) or not child_specs:
        raise ValueError("container needs `children`: a non-empty list of charts")
    entries: List[Dict[str, Any]] = []
    children: List[Dict[str, Any]] = []
    for i, spec in enumerate(child_specs):
        if not isinstance(spec, dict):
            raise ValueError(f"children[{i}] must be an object")
        child_type = spec.get("object_type")
        if child_type not in CONTAINER_CHILD_TYPES:
            raise ValueError(
                f"children[{i}].object_type must be one of {list(CONTAINER_CHILD_TYPES)}, "
                f"got {child_type!r}")
        ref = uuid.uuid4().hex[:8]
        entry: Dict[str, Any] = {
            "refId": ref,
            "label": _string_expression(_quoted(spec.get("label") or f"Tab {i + 1}")),
            "isMaster": False,
            "cId": _column_id(),
        }
        if spec.get("condition"):
            entry["condition"] = _string_expression(str(spec["condition"]))
        entries.append(entry)
        child = build_object_properties(
            child_type, spec.get("title", ""), spec.get("subtitle", ""),
            spec.get("dimensions") or [], spec.get("measures") or [])
        deep_merge(child, check_extra_properties(spec.get("extra_properties")))
        child["containerChildId"] = ref
        children.append(child)

    props = {
        "qInfo": {"qType": "container"},
        "visualization": "container",
        "children": entries,
        "showTitles": bool(title or subtitle),
        "title": title or "",
        "subtitle": subtitle or "",
        "footnote": "",
        "disableNavMenu": False,
        "showDetails": False,
        "borders": "noBorder",
        "showTabs": True,
        "useDropdown": False,
        "useScrollButton": True,
        "showIcons": False,
        "supportRefresh": False,
        "hasExternalChildren": False,
        "qChildListDef": {"qData": {
            "visualization": "/visualization", "containerChildId": "/containerChildId",
            "qExtendsId": "/qExtendsId", "title": "/title", "showCondition": "/showCondition"}},
    }
    return props, children


def prepare_object(object_type: str, title: str, subtitle: str,
                   dimensions: List[Any], measures: List[Any], fields: List[str],
                   children: List[Any], extra_properties: Dict[str, Any]
                   ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Validate the request for one object and build everything to create.

    Returns the object's properties and the property structures of the
    children to create inside it (listboxes of a filter pane, charts of a
    container). Raises ValueError for a request that cannot be drawn,
    before anything has been sent to Engine.
    """
    child_props: List[Dict[str, Any]] = []
    if children and object_type != "container":
        raise ValueError(f"{object_type} takes no `children`; only container does")
    if object_type == "filterpane":
        if not fields:
            raise ValueError("filterpane needs `fields`: the fields to filter on")
        if dimensions or measures:
            raise ValueError("filterpane takes `fields`, not dimensions or measures")
        props = build_filterpane_properties(title)
        child_props = [build_listbox_properties(f) for f in fields]
    elif object_type == "container":
        if fields or dimensions or measures:
            raise ValueError("container takes `children`, not fields, dimensions or measures")
        props, child_props = build_container(title, subtitle, children)
    else:
        if fields:
            raise ValueError(f"{object_type} takes dimensions and measures, not `fields`")
        props = build_object_properties(object_type, title, subtitle, dimensions, measures)
    deep_merge(props, extra_properties)
    return props, child_props


def build_listbox_properties(field: str) -> Dict[str, Any]:
    """One listbox inside a filter pane."""
    return {
        "qInfo": {"qType": "listbox"},
        "visualization": "listbox",
        "title": field,
        "showTitles": True,
        "frequencyMode": "N",
        "qListObjectDef": {
            "qShowAlternatives": True,
            "qStateName": "",
            "qLibraryId": "",
            "qDef": {
                "qFieldDefs": [field],
                "qFieldLabels": [""],
                "qSortCriterias": [{"qSortByState": 1, "qSortByAscii": 1,
                                    "qSortByNumeric": 1, "qSortByLoadOrder": 1}],
            },
            "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qHeight": 10, "qWidth": 1}],
        },
    }


def build_sheet_properties(title: str, description: str, rank: int) -> Dict[str, Any]:
    return {
        "qInfo": {"qType": "sheet"},
        "qMetaDef": {"title": title, "description": description or ""},
        "rank": rank,
        "thumbnail": {"qStaticContentUrlDef": {}},
        "columns": GRID_COLUMNS,
        "rows": GRID_ROWS,
        "cells": [],
        "qChildListDef": {"qData": {"title": "/title"}},
    }


def deep_merge(target: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `patch` into `target` in place: dicts recurse, everything else replaces."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


def check_extra_properties(extra: Any) -> Dict[str, Any]:
    """`extra_properties` may style an object but not restructure it."""
    if extra is None:
        return {}
    if not isinstance(extra, dict):
        raise ValueError("extra_properties must be an object")
    clash = [k for k in _PROTECTED_KEYS if k in extra]
    if clash:
        raise ValueError(
            f"extra_properties may not set {clash}: use dimensions, measures and "
            f"position for those")
    return extra


# ─── Layout on the sheet grid ──────────────────────────────────────────────


def normalise_position(position: Any, default_size: Tuple[int, int]) -> Dict[str, Optional[int]]:
    """Validate a `{col, row, colspan, rowspan}` request; absent keys stay None."""
    if position is None:
        position = {}
    if not isinstance(position, dict):
        raise ValueError("position must be an object with col, row, colspan, rowspan")
    unknown = sorted(set(position) - {"col", "row", "colspan", "rowspan"})
    if unknown:
        raise ValueError(f"unknown position keys {unknown}; use col, row, colspan, rowspan")
    out: Dict[str, Optional[int]] = {}
    for key in ("col", "row", "colspan", "rowspan"):
        value = position.get(key)
        if value is None:
            out[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"position.{key} must be an integer, got {value!r}")
        floor = 1 if key in ("colspan", "rowspan") else 0
        if value < floor:
            raise ValueError(f"position.{key} must be >= {floor}, got {value}")
        out[key] = value
    colspan = out["colspan"] or default_size[0]
    if (out["col"] or 0) + colspan > GRID_COLUMNS:
        raise ValueError(
            f"position runs off the {GRID_COLUMNS}-column grid: "
            f"col {out['col'] or 0} + colspan {colspan}")
    return out


def find_free_slot(cells: List[Dict[str, Any]], colspan: int, rowspan: int,
                   columns: int = GRID_COLUMNS) -> Tuple[int, int]:
    """First (col, row), reading left to right and top to bottom, where a
    colspan x rowspan block overlaps nothing already on the sheet."""
    taken = set()
    bottom = 0
    for cell in cells:
        c, r = int(cell.get("col", 0)), int(cell.get("row", 0))
        cs, rs = int(cell.get("colspan", 1)), int(cell.get("rowspan", 1))
        bottom = max(bottom, r + rs)
        for dr in range(rs):
            for dc in range(cs):
                taken.add((c + dc, r + dr))
    for row in range(bottom + 1):
        for col in range(columns - colspan + 1):
            if all((col + dc, row + dr) not in taken
                   for dr in range(rowspan) for dc in range(colspan)):
                return col, row
    return 0, bottom


def cell_bounds(cell: Dict[str, Any], columns: int, rows: int) -> Dict[str, float]:
    """The percent rectangle Sense stores next to a cell."""
    return {
        "x": cell["col"] * 100.0 / columns,
        "y": cell["row"] * 100.0 / rows,
        "width": cell["colspan"] * 100.0 / columns,
        "height": cell["rowspan"] * 100.0 / rows,
    }


def place_cell(sheet_props: Dict[str, Any], object_id: str, object_type: str,
               position: Dict[str, Optional[int]],
               default_size: Tuple[int, int]) -> Dict[str, Any]:
    """Put (or move) an object's cell on the sheet; mutates `sheet_props`.

    The sheet grows downward when the block does not fit. `bounds` is
    recomputed for every cell then, because it is a percentage of a grid
    that just changed height.
    """
    columns = int(sheet_props.get("columns") or GRID_COLUMNS)
    rows = int(sheet_props.get("rows") or GRID_ROWS)
    cells = [c for c in (sheet_props.get("cells") or []) if c.get("name") != object_id]
    colspan = position.get("colspan") or default_size[0]
    rowspan = position.get("rowspan") or default_size[1]
    if position.get("col") is not None and position.get("row") is not None:
        col, row = position["col"], position["row"]
    elif position.get("col") is not None or position.get("row") is not None:
        raise ValueError("give both position.col and position.row, or neither")
    else:
        col, row = find_free_slot(cells, colspan, rowspan, columns)
    if col + colspan > columns:
        raise ValueError(f"position runs off the {columns}-column grid")

    cell = {"name": object_id, "type": object_type,
            "col": col, "row": row, "colspan": colspan, "rowspan": rowspan}
    cells.append(cell)
    new_rows = max(rows, row + rowspan)
    sheet_props["rows"] = new_rows
    for each in cells:
        each["bounds"] = cell_bounds(each, columns, new_rows)
    sheet_props["cells"] = cells
    return cell


def drop_cell(sheet_props: Dict[str, Any], object_id: str) -> bool:
    cells = sheet_props.get("cells") or []
    kept = [c for c in cells if c.get("name") != object_id]
    sheet_props["cells"] = kept
    return len(kept) != len(cells)


# ─── Reading Engine's verdict ──────────────────────────────────────────────


def _describe_error(error: Any) -> str:
    if not isinstance(error, dict):
        return str(error)
    text = f"error code {error.get('qErrorCode')}"
    extra = error.get("qExtendedMessage") or error.get("qContext")
    return f"{text}: {extra}" if extra else text


# Engine answers these for an object whose calculation condition (`qCalcCond`)
# is not met with the current selections. The object is fine; it draws a
# message instead of data until a selection satisfies the condition.
CALC_CONDITION_CODES = (7005, 7014)


def _layout_errors(layout: Optional[Dict[str, Any]]):
    """(where, error) for every `qError` in a layout."""
    if not isinstance(layout, dict):
        return
    if layout.get("qError"):
        yield "object", layout["qError"]
    cube = layout.get("qHyperCube") or {}
    if cube.get("qError"):
        yield "hypercube", cube["qError"]
    for i, info in enumerate(cube.get("qDimensionInfo") or []):
        if isinstance(info, dict) and info.get("qError"):
            yield f"dimension {info.get('qFallbackTitle') or '#' + str(i)}", info["qError"]
    for i, info in enumerate(cube.get("qMeasureInfo") or []):
        if isinstance(info, dict) and info.get("qError"):
            yield f"measure {info.get('qFallbackTitle') or '#' + str(i)}", info["qError"]
    list_object = layout.get("qListObject") or {}
    if list_object.get("qError"):
        yield "listbox", list_object["qError"]


def _is_calc_condition(error: Any) -> bool:
    return isinstance(error, dict) and error.get("qErrorCode") in CALC_CONDITION_CODES


def layout_problems(layout: Optional[Dict[str, Any]]) -> List[str]:
    """What Engine says is wrong with an object, read from its layout.

    A chart on a missing field or with an unparsable expression is created
    without complaint; the failure only shows up here, in the `qError`
    members of the layout. An unmet calculation condition is not a problem
    and is left out; see `layout_notes`.
    """
    return [f"{where}: {_describe_error(error)}"
            for where, error in _layout_errors(layout) if not _is_calc_condition(error)]


def layout_notes(layout: Optional[Dict[str, Any]]) -> List[str]:
    """Calculation conditions that are not met right now, one note at most."""
    if any(_is_calc_condition(error) for _, error in _layout_errors(layout)):
        return ["a calculation condition is not met with the current selections, so the "
                "object draws a message instead of data"]
    return []


def format_script_errors(script: str, errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`CheckScriptSyntax` errors as something a caller can act on."""
    out = []
    for err in errors or []:
        pos = err.get("qTextPos")
        length = err.get("qErrLen") or 0
        snippet = ""
        if isinstance(pos, int) and 0 <= pos <= len(script):
            snippet = script[pos:pos + max(length, 1)][:80]
        out.append({
            "tab": err.get("qTabIx"),
            "line_in_tab": err.get("qLineInTab"),
            "column": err.get("qColInLine"),
            "text": snippet,
            # A follow-on error caused by an earlier one: fix the others first.
            "secondary": bool(err.get("qSecondaryFailure")),
        })
    return out


# ─── Engine calls ──────────────────────────────────────────────────────────


class EngineAuthoringMixin:
    def set_script(self, app_handle: int, script: str) -> None:
        self.send_request("SetScript", [script], handle=app_handle)

    def check_script_syntax(self, app_handle: int) -> List[Dict[str, Any]]:
        result = self.send_request("CheckScriptSyntax", [], handle=app_handle)
        return result.get("qErrors") or []

    def save_app(self, app_handle: int) -> None:
        self.send_request("DoSave", [], handle=app_handle)

    def create_app(self, name: str) -> str:
        """Create an empty app in the identity's personal space; returns its id.

        `CreateApp` is a global call, so the document socket is dropped and
        a global one opened first. The caller then opens the new app with
        `ensure_app`.
        """
        self.disconnect()
        self.connect()
        result = self.send_request("CreateApp", {"qAppName": name}, handle=-1)
        app_id = result.get("qAppId")
        if not result.get("qSuccess", True) or not app_id:
            raise QlikEngineError(f"Engine did not create the app: {result}")
        return app_id

    def reload_app(self, app_handle: int, timeout: Optional[float] = None) -> bool:
        """Run the load script inside this session and save the loaded data.

        Returns Engine's own verdict; a failed reload is not saved.
        """
        result = self.send_request("DoReload", [0, False, False], handle=app_handle,
                                   timeout=timeout)
        if not result.get("qReturn"):
            return False
        self.save_app(app_handle)
        return True

    def replace_script_checked(self, app_handle: int, new_script: str,
                               check: bool = True) -> Dict[str, Any]:
        """Replace the load script, keep it only if it parses, then save.

        On a syntax error the old script is put back and nothing is saved,
        so the app is exactly as it was.
        """
        previous = self.get_script(app_handle)
        self.set_script(app_handle, new_script)
        errors: List[Dict[str, Any]] = []
        if check:
            errors = format_script_errors(new_script, self.check_script_syntax(app_handle))
        if errors:
            self.set_script(app_handle, previous)
            return {"saved": False, "syntax_errors": errors,
                    "previous_script": previous}
        self.save_app(app_handle)
        return {"saved": True, "syntax_errors": [], "previous_script": previous}

    def _handle_of(self, app_handle: int, object_id: str, what: str) -> int:
        result = self.send_request("GetObject", {"qId": object_id}, handle=app_handle)
        handle = (result.get("qReturn") or {}).get("qHandle")
        if not handle or handle < 0:
            raise QlikEngineError(f"No {what} {object_id!r} in this app")
        return handle

    def _properties(self, handle: int) -> Dict[str, Any]:
        return self.send_request("GetProperties", [], handle=handle).get("qProp") or {}

    def _set_properties(self, handle: int, props: Dict[str, Any]) -> None:
        self.send_request("SetProperties", [props], handle=handle)

    def _layout(self, handle: int) -> Dict[str, Any]:
        return self.send_request("GetLayout", [], handle=handle).get("qLayout") or {}

    def create_sheet(self, app_handle: int, title: str, description: str = "") -> str:
        ranks = [(s.get("qData") or {}).get("rank") for s in self.get_sheets(app_handle)]
        rank = max([r for r in ranks if isinstance(r, (int, float))], default=-1) + 1
        result = self.send_request(
            "CreateObject", [build_sheet_properties(title, description, int(rank))],
            handle=app_handle)
        sheet_id = (result.get("qReturn") or {}).get("qGenericId")
        if not sheet_id:
            raise QlikEngineError(f"Engine did not return an id for the new sheet: {result}")
        self.save_app(app_handle)
        return sheet_id

    def update_sheet(self, app_handle: int, sheet_id: str,
                     title: Optional[str], description: Optional[str],
                     thumbnail_url: Optional[str] = None) -> Dict[str, Any]:
        handle = self._handle_of(app_handle, sheet_id, "sheet")
        props = self._properties(handle)
        meta = props.setdefault("qMetaDef", {})
        if title is not None:
            meta["title"] = title
        if description is not None:
            meta["description"] = description
        if thumbnail_url is not None:
            props["thumbnail"] = {"qStaticContentUrlDef": (
                {"qUrl": thumbnail_url} if thumbnail_url else {})}
        self._set_properties(handle, props)
        self.save_app(app_handle)
        reply = {"title": meta.get("title", ""), "description": meta.get("description", "")}
        url = ((props.get("thumbnail") or {}).get("qStaticContentUrlDef") or {}).get("qUrl")
        if url:
            reply["thumbnail_url"] = url
        return reply

    def delete_sheet(self, app_handle: int, sheet_id: str) -> Dict[str, Any]:
        handle = self._handle_of(app_handle, sheet_id, "sheet")
        children = ((self._layout(handle).get("qChildList") or {}).get("qItems")) or []
        # Through the sheet, one by one: an object destroyed any other way
        # stays a child of its sheet, without a cell, invisible but there.
        for item in children:
            child_id = (item.get("qInfo") or {}).get("qId")
            if child_id:
                self.send_request("DestroyChild", [child_id], handle=handle)
        self.send_request("DestroyObject", [sheet_id], handle=app_handle)
        self.save_app(app_handle)
        return {"objects_removed": len(children)}

    def add_object(self, app_handle: int, sheet_id: str, object_type: str,
                   title: str, subtitle: str,
                   dimensions: List[Any], measures: List[Any], fields: List[str],
                   position: Dict[str, Optional[int]],
                   extra_properties: Dict[str, Any],
                   children: Optional[List[Any]] = None) -> Dict[str, Any]:
        """Create a chart, filter pane or container on a sheet."""
        props, child_props = prepare_object(
            object_type, title, subtitle, dimensions, measures, fields,
            children or [], extra_properties)
        return self.add_prepared(app_handle, sheet_id, props, child_props, object_type,
                                 position, DEFAULT_SIZE[object_type])

    def add_prepared(self, app_handle: int, sheet_id: str, props: Dict[str, Any],
                     child_props: List[Dict[str, Any]], object_type: str,
                     position: Dict[str, Optional[int]],
                     default_size: Tuple[int, int]) -> Dict[str, Any]:
        """Create an object from finished properties, check it, place it, save.

        The one path every object takes onto a sheet, so the checks cannot
        differ between kinds: Engine's layout verdict decides whether the
        object stays, and nothing is saved until it has passed.
        """
        sheet_handle = self._handle_of(app_handle, sheet_id, "sheet")
        sheet_props = self._properties(sheet_handle)
        # Place first, against the sheet as it is: a position that does not
        # fit is a caller error and should not leave an object behind.
        place_cell(copy.deepcopy(sheet_props), "__pending__", object_type, position,
                   default_size)

        created = self.send_request("CreateChild", [props], handle=sheet_handle)
        object_id = (created.get("qReturn") or {}).get("qGenericId")
        object_handle = (created.get("qReturn") or {}).get("qHandle")
        if not object_id or not object_handle:
            raise QlikEngineError(f"Engine did not return the new object: {created}")

        try:
            for child in child_props:
                self.send_request("CreateChild", [child], handle=object_handle)
            problems = self._problems_of_tree(app_handle, object_handle)
        except Exception:
            self._discard(sheet_handle, object_id)
            raise
        if problems:
            self._discard(sheet_handle, object_id)
            return {"created": False, "problems": problems}

        cell = place_cell(sheet_props, object_id, object_type, position, default_size)
        self._set_properties(sheet_handle, sheet_props)
        self.save_app(app_handle)
        return {"created": True, "object_id": object_id, "object_type": object_type,
                "cell": {k: cell[k] for k in ("col", "row", "colspan", "rowspan")},
                "problems": []}

    def _problems_of_tree(self, app_handle: int, handle: int) -> List[str]:
        """Problems of an object and of the children it was built with."""
        layout = self._layout(handle)
        problems = layout_problems(layout)
        for item in ((layout.get("qChildList") or {}).get("qItems")) or []:
            child_id = (item.get("qInfo") or {}).get("qId")
            if not child_id:
                continue
            try:
                child = self.send_request("GetObject", {"qId": child_id}, handle=app_handle)
                child_handle = (child.get("qReturn") or {}).get("qHandle")
                if child_handle:
                    problems += layout_problems(self._layout(child_handle))
            except Exception as exc:
                logger.debug("Could not check child %s: %s", child_id, exc)
        return problems

    def _discard(self, parent_handle: int, object_id: str) -> None:
        """Remove an object just created under `parent_handle`.

        Through the parent: `DestroyObject` on the app leaves a child of a
        sheet where it was, so a rolled-back chart would stay on the sheet
        as an invisible object.
        """
        try:
            self.send_request("DestroyChild", [object_id], handle=parent_handle)
        except Exception as exc:
            logger.warning("Could not remove %s after a failed create: %s", object_id, exc)

    def update_object(self, app_handle: int, object_id: str, sheet_id: Optional[str],
                      title: Optional[str], subtitle: Optional[str],
                      dimensions: Optional[List[Any]], measures: Optional[List[Any]],
                      position: Optional[Dict[str, Optional[int]]],
                      extra_properties: Dict[str, Any]) -> Dict[str, Any]:
        """Change an object, keep the change only if Engine accepts it."""
        handle = self._handle_of(app_handle, object_id, "object")
        original = self._properties(handle)
        object_type = (original.get("qInfo") or {}).get("qType", "")
        props = copy.deepcopy(original)

        if title is not None:
            props["title"] = title
            props["showTitles"] = bool(title or props.get("subtitle"))
        if subtitle is not None:
            props["subtitle"] = subtitle
            props["showTitles"] = bool(props.get("title") or subtitle)

        if dimensions is not None or measures is not None:
            cube = props.get("qHyperCubeDef")
            if not isinstance(cube, dict):
                raise ValueError(
                    f"{object_type or 'this object'} has no dimensions or measures to change")
            new_dims = ([build_dimension(d, i) for i, d in enumerate(dimensions)]
                        if dimensions is not None else cube.get("qDimensions") or [])
            new_measures = ([build_measure(m, i) for i, m in enumerate(measures)]
                            if measures is not None else cube.get("qMeasures") or [])
            if object_type in _LIMITS:
                check_counts(object_type, new_dims, new_measures)
            cube["qDimensions"], cube["qMeasures"] = new_dims, new_measures
            # These index the old columns; Engine falls back to the natural
            # order when they are absent, and rejects them when stale.
            for stale in ("qColumnOrder", "columnOrder", "columnWidths"):
                cube.pop(stale, None)
            props.pop("columnWidths", None)
            if object_type == "table":
                apply_table_columns(cube)
            style_columns(object_type, cube)
            set_fetch_window(cube)
            cube["qInterColumnSortOrder"] = inter_column_sort_order(cube)

        deep_merge(props, extra_properties)
        self._set_properties(handle, props)
        problems = layout_problems(self._layout(handle))
        if problems:
            self._set_properties(handle, original)
            return {"updated": False, "problems": problems}

        moved = None
        if position:
            if not sheet_id:
                self._set_properties(handle, original)
                raise ValueError("position needs sheet_id: the sheet the object sits on")
            sheet_handle = self._handle_of(app_handle, sheet_id, "sheet")
            sheet_props = self._properties(sheet_handle)
            size = DEFAULT_SIZE.get(object_type, (12, 6))
            existing = next((c for c in sheet_props.get("cells") or []
                             if c.get("name") == object_id), None)
            if existing is None:
                self._set_properties(handle, original)
                raise ValueError(f"object {object_id!r} is not on sheet {sheet_id!r}")
            merged = {k: position.get(k) if position.get(k) is not None else existing.get(k)
                      for k in ("col", "row", "colspan", "rowspan")}
            try:
                moved = place_cell(sheet_props, object_id, existing.get("type") or object_type,
                                   merged, size)
            except ValueError:
                self._set_properties(handle, original)
                raise
            self._set_properties(sheet_handle, sheet_props)

        self.save_app(app_handle)
        reply: Dict[str, Any] = {"updated": True, "object_id": object_id,
                                 "object_type": object_type, "problems": []}
        if moved:
            reply["cell"] = {k: moved[k] for k in ("col", "row", "colspan", "rowspan")}
        return reply

    def _find_parent(self, app_handle: int, object_id: str,
                     sheet_id: Optional[str] = None) -> Optional[int]:
        """Handle of the object that holds `object_id` as a child.

        A sheet, or a container or filter pane on one. None when nothing
        does, which is the case for an app-level object.
        """
        if sheet_id:
            sheet_ids = [sheet_id]
        else:
            infos = self.send_request("GetAllInfos", [], handle=app_handle).get("qInfos") or []
            sheet_ids = [i["qId"] for i in infos if i.get("qType") == "sheet"]
        for sid in sheet_ids:
            sheet_handle = self._handle_of(app_handle, sid, "sheet")
            kids = self.send_request("GetChildInfos", [], handle=sheet_handle).get("qInfos") or []
            if any(k.get("qId") == object_id for k in kids):
                return sheet_handle
            for kid in kids:
                if kid.get("qType") in ("container", "filterpane"):
                    kid_handle = self._handle_of(app_handle, kid["qId"], "object")
                    inner = self.send_request(
                        "GetChildInfos", [], handle=kid_handle).get("qInfos") or []
                    if any(k.get("qId") == object_id for k in inner):
                        return kid_handle
        return None

    def delete_object(self, app_handle: int, object_id: str,
                      sheet_id: Optional[str]) -> Dict[str, Any]:
        self._handle_of(app_handle, object_id, "object")
        cell_removed = False
        if sheet_id:
            sheet_handle = self._handle_of(app_handle, sheet_id, "sheet")
            sheet_props = self._properties(sheet_handle)
            if drop_cell(sheet_props, object_id):
                self._set_properties(sheet_handle, sheet_props)
                cell_removed = True
        parent = self._find_parent(app_handle, object_id, sheet_id)
        if parent is None:
            raise QlikEngineError(
                f"Object {object_id!r} is not on "
                f"{'sheet ' + repr(sheet_id) if sheet_id else 'any sheet'}; nothing was removed")
        self.send_request("DestroyChild", [object_id], handle=parent)
        self.save_app(app_handle)
        return {"object_id": object_id, "cell_removed": cell_removed}
