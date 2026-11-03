"""Checking objects and sheets without looking at them.

Engine accepts an object it cannot draw: it answers with data and no error,
and the client then shows nothing. Everything that went wrong in practice —
an empty table, empty charts, an unfinished filter pane — was a structural
gap, not something Engine reported. These checks look for those gaps.

Two kinds of finding:

* `error`   — Engine says it is broken, or the structure cannot work
              (a table without a column order, a chart over its dimension
              limit, an object on no cell of its sheet).
* `warning` — differs from what Qlik's own editor writes. Harmless on some
              objects (a KPI and a pivot table draw without it) and fatal
              on others (charts and tables drew nothing), so it is shown
              rather than hidden.

The functions here are pure: they read property structures that the caller
has fetched, and return findings.
"""

from typing import Any, Dict, Iterable, List, Optional, Set

from .authoring import (
    GRID_COLUMNS,
    _LIMITS,
    _VISUAL_DEFAULTS,
    layout_notes,
    layout_problems,
)

# Keys every standard visualisation carries.
_COMMON_KEYS = ("showTitles", "title", "subtitle", "visualization")
# Keys the editor writes on every column of a chart or table; a table's
# columns also carry their text alignment.
_COLUMN_KEYS = ("cId", "autoSort")
_TABLE_COLUMN_KEYS = ("textAlign",)
# Cube keys the editor writes, by type.
_CUBE_KEYS: Dict[str, Iterable[str]] = {
    "table": ("qColumnOrder", "columnOrder", "columnWidths"),
    "barchart": ("qAlwaysFullyExpanded",),
    "linechart": ("qAlwaysFullyExpanded",),
    "piechart": ("qAlwaysFullyExpanded",),
    "combochart": ("qAlwaysFullyExpanded",),
}


def finding(severity: str, path: str, message: str, **extra: Any) -> Dict[str, Any]:
    out = {"severity": severity, "path": path, "message": message}
    out.update(extra)
    return out


def _columns(cube: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(cube.get("qDimensions") or []) + list(cube.get("qMeasures") or [])


def lint_cube(object_type: str, cube: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Structure of a hypercube definition."""
    out: List[Dict[str, Any]] = []
    dims = cube.get("qDimensions") or []
    measures = cube.get("qMeasures") or []
    columns = len(dims) + len(measures)

    limits = _LIMITS.get(object_type)
    if limits:
        for name, items in (("dimensions", dims), ("measures", measures)):
            low, high = limits[name]
            if len(items) < low or (high is not None and len(items) > high):
                span = f"{low}" if low == high else f"{low}..{high if high is not None else 'many'}"
                out.append(finding("error", f"qHyperCubeDef.{name}",
                                   f"{object_type} takes {span} {name}, has {len(items)}"))
        if object_type == "table" and columns == 0:
            out.append(finding("error", "qHyperCubeDef", "table has no columns"))

    # Column identity: the client keys columns, widths and order by cId.
    ids = [(c.get("qDef") or {}).get("cId") for c in _columns(cube)]
    if object_type in _LIMITS and any(not i for i in ids):
        out.append(finding("error", "qHyperCubeDef.columns",
                           "a column has no cId; the client cannot identify it"))
    named = [i for i in ids if i]
    if len(named) != len(set(named)):
        out.append(finding("error", "qHyperCubeDef.columns", "two columns share a cId"))

    order = cube.get("qInterColumnSortOrder")
    if isinstance(order, list) and columns:
        if any((not isinstance(i, int)) or i < 0 or i >= columns for i in order):
            out.append(finding("error", "qHyperCubeDef.qInterColumnSortOrder",
                               f"refers to a column outside 0..{columns - 1}"))
        elif len(set(order)) != columns:
            out.append(finding("warning", "qHyperCubeDef.qInterColumnSortOrder",
                               f"lists {len(set(order))} of {columns} columns"))

    for window in cube.get("qInitialDataFetch") or []:
        if isinstance(window, dict) and columns and window.get("qWidth", columns) < columns:
            out.append(finding("error", "qHyperCubeDef.qInitialDataFetch",
                               f"fetches {window.get('qWidth')} columns of {columns}"))

    if object_type == "table" and columns:
        for key in ("qColumnOrder", "columnOrder"):
            value = cube.get(key)
            if not value:
                out.append(finding(
                    "error", f"qHyperCubeDef.{key}",
                    "missing or empty: a table with no column order has no columns to draw"))
            elif sorted(value) != list(range(columns)):
                out.append(finding("error", f"qHyperCubeDef.{key}",
                                   f"is {value}, but the table has {columns} columns"))
        widths = cube.get("columnWidths")
        if widths is None:
            out.append(finding("error", "qHyperCubeDef.columnWidths",
                               "missing: the client reads one width per column"))
        elif len(widths) != columns:
            out.append(finding("error", "qHyperCubeDef.columnWidths",
                               f"has {len(widths)} widths for {columns} columns"))
    return out


def lint_object(props: Dict[str, Any],
                children: Optional[List[Dict[str, Any]]] = None,
                layout: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Findings for one object.

    `children` are the property structures of its child objects (listboxes
    of a filter pane, charts of a container); `layout` is its `GetLayout`
    answer, whose `qError` members are Engine's own verdict.
    """
    out: List[Dict[str, Any]] = []
    object_type = (props.get("qInfo") or {}).get("qType", "")
    if not object_type:
        return [finding("error", "qInfo.qType", "object has no type")]

    for problem in layout_problems(layout):
        out.append(finding("error", "layout", problem))
    for note in layout_notes(layout):
        out.append(finding("info", "layout", note))

    # A linked object holds no properties of its own: they come from the
    # master object it extends, which is checked where it lives.
    if props.get("qExtendsId"):
        return out

    if object_type in _VISUAL_DEFAULTS or object_type in ("container", "filterpane"):
        if props.get("visualization") not in (None, object_type):
            out.append(finding(
                "error", "visualization",
                f"is {props.get('visualization')!r} but the object is a {object_type}"))
        missing = [k for k in _COMMON_KEYS if k not in props]
        missing += [k for k in _VISUAL_DEFAULTS.get(object_type, {}) if k not in props]
        if missing:
            out.append(finding(
                "warning", "properties",
                "missing what Qlik's editor writes: " + ", ".join(missing),
                keys=missing))

    cube = props.get("qHyperCubeDef")
    if isinstance(cube, dict) and object_type in _LIMITS:
        out.extend(lint_cube(object_type, cube))
        missing_cube = [k for k in _CUBE_KEYS.get(object_type, ()) if k not in cube]
        # the table's own are errors in lint_cube
        missing_cube = [k for k in missing_cube if object_type != "table"]
        if missing_cube:
            out.append(finding("warning", "qHyperCubeDef",
                               "missing what Qlik's editor writes: " + ", ".join(missing_cube),
                               keys=missing_cube))
        missing_col = []
        for i, column in enumerate(_columns(cube)):
            wanted = _COLUMN_KEYS + (_TABLE_COLUMN_KEYS if object_type == "table" else ())
            absent = [k for k in wanted if k not in (column.get("qDef") or {})
                      and "qLibraryId" not in column]
            if absent:
                missing_col.append(f"column {i}: {', '.join(absent)}")
        if missing_col:
            out.append(finding("warning", "qHyperCubeDef.columns",
                               "missing what Qlik's editor writes on a column: "
                               + "; ".join(missing_col)))
    elif object_type in _LIMITS:
        out.append(finding("error", "qHyperCubeDef", f"{object_type} has no hypercube"))

    if object_type == "filterpane":
        if not props.get("qChildListDef"):
            out.append(finding("error", "qChildListDef",
                               "missing: Engine does not list the listboxes as children, "
                               "and the client shows an incomplete visualisation"))
        if children is not None and not children:
            out.append(finding("error", "children", "the filter pane holds no listbox"))

    if object_type == "container":
        out.extend(_lint_container(props, children))
    return out


def _lint_container(props: Dict[str, Any],
                    children: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    entries = props.get("children") or []
    if not entries:
        out.append(finding("error", "children", "the container lists no children"))
    refs: Set[str] = {e.get("refId") for e in entries if e.get("refId")}
    if len(refs) != len(entries):
        out.append(finding("error", "children", "a child entry has no refId, or two share one"))
    if not props.get("qChildListDef"):
        out.append(finding("error", "qChildListDef", "missing: the container cannot list its charts"))
    if children is not None:
        pointed = {c.get("containerChildId") for c in children}
        for ref in sorted(refs - pointed):
            out.append(finding("error", "children", f"entry {ref} has no chart pointing back at it"))
        for ref in sorted(x for x in pointed - refs if x):
            out.append(finding("error", "children",
                               f"a chart points at {ref}, which the container does not list"))
        if any(not c.get("containerChildId") for c in children):
            out.append(finding("error", "children",
                               "a chart inside the container has no containerChildId"))
    return out


def lint_sheet_layout(sheet_props: Dict[str, Any],
                      object_ids: Iterable[str]) -> List[Dict[str, Any]]:
    """Cells of a sheet against the objects it holds."""
    out: List[Dict[str, Any]] = []
    columns = int(sheet_props.get("columns") or GRID_COLUMNS)
    rows = int(sheet_props.get("rows") or 0)
    cells = sheet_props.get("cells") or []
    ids = set(object_ids)
    placed = {c.get("name") for c in cells}

    for object_id in sorted(ids - placed):
        out.append(finding("error", f"cells.{object_id}",
                           "the object is on no cell of the sheet: it exists but is not shown",
                           object_id=object_id))
    for cell in cells:
        name = cell.get("name")
        if name not in ids:
            out.append(finding("error", f"cells.{name}",
                               "a cell refers to an object the sheet does not hold",
                               object_id=name))
        col, row = cell.get("col", 0), cell.get("row", 0)
        colspan, rowspan = cell.get("colspan", 1), cell.get("rowspan", 1)
        if col < 0 or row < 0 or colspan < 1 or rowspan < 1 or col + colspan > columns:
            out.append(finding("error", f"cells.{name}",
                               f"does not fit the {columns}-column grid "
                               f"(col {col}, colspan {colspan})", object_id=name))
        elif rows and row + rowspan > rows:
            out.append(finding("warning", f"cells.{name}",
                               f"runs past the sheet's {rows} rows", object_id=name))

    for i, a in enumerate(cells):
        for b in cells[i + 1:]:
            if (a.get("col", 0) < b.get("col", 0) + b.get("colspan", 1)
                    and b.get("col", 0) < a.get("col", 0) + a.get("colspan", 1)
                    and a.get("row", 0) < b.get("row", 0) + b.get("rowspan", 1)
                    and b.get("row", 0) < a.get("row", 0) + a.get("rowspan", 1)):
                out.append(finding("warning", f"cells.{a.get('name')}",
                                   f"overlaps {b.get('name')}", object_id=a.get("name")))
    return out


def summarise(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    return {"ok": errors == 0, "errors": errors, "warnings": warnings}
