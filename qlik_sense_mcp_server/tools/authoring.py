"""Tools that change an app: load script, sheets, and the objects on them.

Registered only with QLIK_WRITE_TOOLS=true (see `context._write_tool`).
Every tool runs inside the Engine client's transaction like the read
tools, and every change is checked against Engine before it is saved —
see `engine/authoring.py`.
"""

import mimetypes
import os
import re
from typing import Any, Dict, List, Optional

from . import context
from .context import _write_tool
from .helpers import _check, _engine_serialised, _err, _ok, _timed
from ..engine.app_options import (
    build_app_option_changes,
    check_content_url,
)
from ..engine.authoring import (
    OBJECT_TYPES,
    check_extra_properties,
    normalise_position,
    DEFAULT_SIZE,
)

logger = context.logger

def _open(app_id: str) -> int:
    return context.engine_api.ensure_app(app_id, no_data=False)


def _failed(message: str, ex: Exception, **extra: Any) -> str:
    text = str(ex) or repr(ex)
    if isinstance(ex, ValueError) or text.startswith("No "):
        return _err(f"{message}: {text}", error_category="invalid_argument", **extra)
    return _err(f"{message}: {text}", error_category="engine_api_error", **extra)


@_write_tool()
@_timed
@_engine_serialised
def create_app(name: str, script: Optional[str] = None, reload: bool = False) -> str:
    """
    Create a new app, optionally with a load script, optionally loaded. WRITE OPERATION.

    WHEN TO USE
        To start a dashboard from nothing: this, then `create_sheet` and
        `add_sheet_object`. With an inline script (`LOAD ... INLINE [...]`,
        `AUTOGENERATE`) the app is self-contained and needs no data source.

    WHEN NOT TO USE
        To copy an existing app: not supported.

    The app is created unpublished in this identity's personal space, so it
    is visible in the hub under Work until published.

    Args:
        name: app name.
        script: load script to set (syntax-checked; a script with errors is
            reported and the app is still created, empty).
        reload: run the script after setting it. Needs `script`.

    Returns:
        `app_id`, `script_saved`, `reloaded`; `syntax_errors` when the
        script was refused. Use `get_app_details` to see the loaded tables.
    """
    e = _check()
    if e:
        return e
    if not name or not name.strip():
        return _err("`name` must not be empty", error_category="invalid_argument")
    if reload and not script:
        return _err("`reload` needs a `script`", error_category="invalid_argument")
    try:
        app_id = context.engine_api.create_app(name.strip())
    except Exception as ex:
        return _failed("Could not create the app", ex)
    reply: Dict[str, Any] = {"app_id": app_id, "name": name.strip(),
                             "script_saved": False, "reloaded": False}
    try:
        handle = _open(app_id)
        if script:
            outcome = context.engine_api.replace_script_checked(handle, script)
            reply["script_saved"] = outcome["saved"]
            if not outcome["saved"]:
                reply["syntax_errors"] = outcome["syntax_errors"]
            elif reload:
                outcome = context.engine_api.reload_logged(handle)
                reply["reloaded"] = outcome["reloaded"]
                reply["reload_errors"] = outcome["errors"]
                reply["reload_log"] = outcome["log"]
        else:
            context.engine_api.save_app(handle)
    except Exception as ex:
        reply["warning"] = f"App created, but the follow-up step failed: {ex}"
    return _ok(reply)


@_write_tool()
@_timed
@_engine_serialised
def reload_app(app_id: str) -> str:
    """
    Run an app's load script now and save the loaded data. WRITE OPERATION.

    WHEN TO USE
        To load a script you just set, in an app you are building. The
        reload runs inside this session and blocks until it finishes.

    WHEN NOT TO USE
        For a production app or a long reload: use `start_task`, which runs
        on the scheduler and leaves a script log.

    Args:
        app_id: application GUID, unpublished.

    Returns:
        `reloaded` (on false the data is unchanged), `errors` (`message` and
        the `statement` that failed) and `log` (lines the load reported),
        `seconds`.
    """
    e = _check()
    if e:
        return e
    try:
        outcome = context.engine_api.reload_logged(_open(app_id))
    except Exception as ex:
        return _failed("Could not reload the app", ex, app_id=app_id)
    reply: Dict[str, Any] = {"app_id": app_id, **outcome}
    return _ok(reply)


@_write_tool()
@_timed
@_engine_serialised
def set_app_script(app_id: str, script: str, check_syntax: bool = True) -> str:
    """
    Replace an app's whole load script. WRITE OPERATION.

    WHEN TO USE
        To write or rewrite the script from scratch. For a small edit to an
        existing script use `patch_app_script` — it cannot drop the part of
        the script you did not mean to touch.

    WHEN NOT TO USE
        To run the script. This saves it; the data changes only when the app
        is reloaded (`start_task`, or a reload in the hub).
        On a published app: Qlik refuses.

    Read the current script first with `get_app_script`: this replaces it
    entirely.

    Args:
        app_id: application GUID, unpublished.
        script: the complete new load script, tabs separated by
            `///$tab Name` lines as Qlik writes them.
        check_syntax: run Engine's syntax check before saving (default
            true). On a syntax error the previous script is put back,
            nothing is saved, and the errors come back in `syntax_errors`.
            Qlik's check is shallow: it misses an unclosed bracket, an
            unterminated string and a missing connection. Run
            `check_app_script` first for the fuller scan, and `reload_app`
            for the real test.

    Returns:
        `saved`, `previous_script_length`, `new_script_length`; on a syntax
        error `saved: false`, `syntax_errors` (tab, line_in_tab, column,
        text, secondary) and `previous_script`. Fix the entries without
        `secondary` first — the others are consequences.

    Does not return:
        The data. A saved script has not been executed.
    """
    e = _check()
    if e:
        return e
    try:
        handle = _open(app_id)
        outcome = context.engine_api.replace_script_checked(handle, script, check=check_syntax)
    except Exception as ex:
        return _failed("Could not set the script", ex, app_id=app_id)
    previous = outcome.pop("previous_script") or ""
    reply = {"app_id": app_id, "previous_script_length": len(previous),
             "new_script_length": len(script), **outcome}
    if not outcome["saved"]:
        reply["previous_script"] = previous
    return _ok(reply)


@_write_tool()
@_timed
@_engine_serialised
def patch_app_script(app_id: str, find: str, replace: str,
                     replace_all: bool = False, check_syntax: bool = True) -> str:
    """
    Replace one piece of an app's load script with another. WRITE OPERATION.

    WHEN TO USE
        To change a statement, a path, a variable, a filter — anything
        smaller than the script. `find` must occur exactly once, so the
        edit lands where you meant; if it is ambiguous you are told, not
        guessed for.

    WHEN NOT TO USE
        To rewrite most of the script — `set_app_script`.
        To run it: this saves only; reload separately.

    Args:
        app_id: application GUID, unpublished.
        find: exact text to look for, whitespace and case included. Include
            enough surrounding lines to be unique. Not a regex.
        replace: what takes its place; an empty string deletes `find`.
        replace_all: replace every occurrence instead of requiring one.
        check_syntax: as in `set_app_script`; a failing edit is undone.

    Returns:
        `saved`, `replacements`, lengths before and after. If `find` is
        absent or ambiguous: an error with `occurrences`. On a syntax
        error `saved: false` and `syntax_errors`, the script unchanged.

    Does not return:
        The edited script — read it with `get_app_script`.
    """
    e = _check()
    if e:
        return e
    if not find:
        return _err("`find` must not be empty", error_category="invalid_argument")
    try:
        handle = _open(app_id)
        current = context.engine_api.get_script(handle) or ""
        count = current.count(find)
        if count == 0:
            return _err("`find` does not occur in the script", occurrences=0,
                        app_id=app_id)
        if count > 1 and not replace_all:
            return _err(
                f"`find` occurs {count} times; add surrounding text to make it unique, "
                f"or pass replace_all=true", occurrences=count, app_id=app_id)
        updated = current.replace(find, replace)
        outcome = context.engine_api.replace_script_checked(handle, updated, check=check_syntax)
    except Exception as ex:
        return _failed("Could not patch the script", ex, app_id=app_id)
    outcome.pop("previous_script", None)
    return _ok({"app_id": app_id, "replacements": count,
                "previous_script_length": len(current),
                "new_script_length": len(updated), **outcome})


@_write_tool()
@_timed
@_engine_serialised
def create_sheet(app_id: str, title: str, description: str = "") -> str:
    """
    Add an empty sheet at the end of an app. WRITE OPERATION.

    WHEN TO USE
        Before `add_sheet_object`, which needs a sheet to put charts on.

    WHEN NOT TO USE
        On a published app: Qlik refuses.

    Args:
        app_id: application GUID, unpublished.
        title: sheet name shown in the tab.
        description: optional longer text.

    Returns:
        `sheet_id` — pass it to `add_sheet_object` and the other sheet tools.
    """
    e = _check()
    if e:
        return e
    if not title or not title.strip():
        return _err("`title` must not be empty", error_category="invalid_argument")
    try:
        sheet_id = context.engine_api.create_sheet(_open(app_id), title.strip(), description)
    except Exception as ex:
        return _failed("Could not create the sheet", ex, app_id=app_id)
    return _ok({"app_id": app_id, "sheet_id": sheet_id, "title": title.strip()})


@_write_tool()
@_timed
@_engine_serialised
def update_sheet(app_id: str, sheet_id: str, title: Optional[str] = None,
                 description: Optional[str] = None,
                 thumbnail_url: Optional[str] = None) -> str:
    """
    Rename a sheet, change its description or its thumbnail. WRITE OPERATION.

    Args:
        app_id: application GUID, unpublished.
        sheet_id: from `get_app_sheets` or `create_sheet`.
        title: new name; omit to leave it.
        description: new description; omit to leave it, empty string to clear.
        thumbnail_url: the sheet's thumbnail, a server path such as
            `/appcontent/<app id>/cover.png` (see `upload_app_image`) or
            `/content/Default/<file>`; empty string clears it.

    Returns:
        The sheet's `title` and `description` after the change, and
        `thumbnail_url` when it has one.
    """
    e = _check()
    if e:
        return e
    if title is None and description is None and thumbnail_url is None:
        return _err("Nothing to change: give title, description or thumbnail_url",
                    error_category="invalid_argument")
    try:
        if thumbnail_url is not None:
            check_content_url(thumbnail_url, "thumbnail_url")
        result = context.engine_api.update_sheet(
            _open(app_id), sheet_id, title, description, thumbnail_url)
    except Exception as ex:
        return _failed("Could not update the sheet", ex, app_id=app_id, sheet_id=sheet_id)
    return _ok({"app_id": app_id, "sheet_id": sheet_id, **result})


@_write_tool()
@_timed
@_engine_serialised
def delete_sheet(app_id: str, sheet_id: str) -> str:
    """
    Delete a sheet and every object on it. DESTRUCTIVE WRITE OPERATION.

    Cannot be undone from here. List the sheet's objects with
    `get_app_sheet_objects` first and confirm with the user.

    Args:
        app_id: application GUID, unpublished.
        sheet_id: from `get_app_sheets`.

    Returns:
        `objects_removed`: how many objects the sheet held.
    """
    e = _check()
    if e:
        return e
    try:
        result = context.engine_api.delete_sheet(_open(app_id), sheet_id)
    except Exception as ex:
        return _failed("Could not delete the sheet", ex, app_id=app_id, sheet_id=sheet_id)
    return _ok({"app_id": app_id, "sheet_id": sheet_id, **result})


@_write_tool()
@_timed
@_engine_serialised
def add_sheet_object(
    app_id: str,
    sheet_id: str,
    object_type: str,
    title: str = "",
    subtitle: str = "",
    dimensions: Optional[List[Dict[str, Any]]] = None,
    measures: Optional[List[Dict[str, Any]]] = None,
    fields: Optional[List[str]] = None,
    position: Optional[Dict[str, int]] = None,
    extra_properties: Optional[Dict[str, Any]] = None,
    children: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Put a chart, table, KPI, filter pane or container on a sheet. WRITE OPERATION.

    WHEN TO USE
        To build a dashboard: `create_sheet`, then one call per object. The
        object is checked against Engine before it is kept — a field that
        does not exist or an expression that does not parse is reported in
        `problems` and the object is NOT created, rather than left on the
        sheet as an error tile.

    WHEN NOT TO USE
        To change an object that already exists — `update_sheet_object`.
        For visualisations outside `object_type` (combo chart, map, scatter):
        not supported here.

    Args:
        app_id: application GUID, unpublished.
        sheet_id: from `get_app_sheets` or `create_sheet`.
        object_type: one of
            `kpi` (0 dimensions, 1-2 measures),
            `table` (any columns),
            `pivot-table` (>=1 dimension, >=1 measure),
            `barchart` / `linechart` (1-2 dimensions, >=1 measure),
            `piechart` (1 dimension, 1 measure),
            `combochart` (1-2 dimensions, >=1 measure; bars and lines, one
            or two value axes),
            `filterpane` (takes `fields`, not dimensions or measures),
            `container` (takes `children`, not dimensions or measures).
        title: shown above the object; empty hides the title.
        subtitle: optional second line.
        dimensions: list of `{field | expression | library_id, label?,
            include_nulls?}`. Give exactly one of the first three.
            `field` is a field name with no brackets; `expression` is a
            calculated dimension such as `Year(OrderDate)`; `library_id` is
            a master dimension's id.
        measures: list of `{expression | library_id, label?, format?,
            sort?}`. `expression` is Qlik syntax, set analysis included:
            `Sum({<Year={2024}>} Sales)`. `format` is a number format such
            as `#,##0.00` or `0.0%`. `sort` is `asc` or `desc` and orders
            the chart by that measure. For `combochart` only: `series` is
            `bar`, `line` or `marker` (default: the first measure a bar,
            the rest lines) and `axis` is `0` for the left value axis or
            `1` for the right.
        fields: for `filterpane` only: field names, one listbox each.
        children: for `container` only: the charts inside it, each
            `{object_type, label, condition?, title?, subtitle?, dimensions?,
            measures?, extra_properties?}`. `object_type` is any chart type
            above; `label` is its tab or dropdown text; `condition` is a
            Qlik expression, and the chart shows only while it is true.
            A container is how one chart slot shows different charts
            depending on a selection.
        position: `{col, row, colspan, rowspan}` on the 24-column sheet
            grid, all optional. Omitted, the object goes in the first free
            place; omitted sizes default by type (kpi 6x3, charts 12x6,
            table 24x8). Give both `col` and `row` or neither. The sheet
            grows downward when needed.
        extra_properties: raw Qlik properties merged over the object, for
            styling the tool has no argument for (colours, legend,
            orientation). Cannot set qInfo, qHyperCubeDef, visualization or
            cells.

    Returns:
        `created`, `object_id`, `cell` (where it landed) and `problems`.
        When `created` is false, `problems` lists what Engine rejected and
        nothing was added.

    Does not return:
        The data the object shows — query that with `engine_query`.
    """
    e = _check()
    if e:
        return e
    if object_type not in OBJECT_TYPES:
        return _err(f"Unknown object_type {object_type!r}", error_category="invalid_argument")
    try:
        extra = check_extra_properties(extra_properties)
        place = normalise_position(position, DEFAULT_SIZE[object_type])
        result = context.engine_api.add_object(
            _open(app_id), sheet_id, object_type, title, subtitle,
            dimensions or [], measures or [], fields or [], place, extra, children or [])
    except Exception as ex:
        return _failed("Could not add the object", ex, app_id=app_id, sheet_id=sheet_id)
    if not result.get("created"):
        return _err("Engine rejected the object; nothing was added",
                    problems=result["problems"], app_id=app_id, sheet_id=sheet_id)
    return _ok({"app_id": app_id, "sheet_id": sheet_id, **result})


@_write_tool()
@_timed
@_engine_serialised
def update_sheet_object(
    app_id: str,
    object_id: str,
    sheet_id: Optional[str] = None,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    dimensions: Optional[List[Dict[str, Any]]] = None,
    measures: Optional[List[Dict[str, Any]]] = None,
    position: Optional[Dict[str, int]] = None,
    extra_properties: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Change an existing chart, table or KPI. WRITE OPERATION.

    WHEN TO USE
        To retitle, re-point at other fields, change an expression, or move
        and resize an object. The change is kept only if Engine accepts the
        result; otherwise the object is restored and `problems` says why.

    WHEN NOT TO USE
        To add an object — `add_sheet_object`.

    Everything you omit stays as it is. `dimensions` and `measures` each
    replace their whole list: read the current ones with `get_app_object`,
    edit, and send the full list back.

    Args:
        app_id: application GUID, unpublished.
        object_id: from `get_app_sheet_objects` or `add_sheet_object`.
        sheet_id: required only with `position` — the sheet the object is on.
        title, subtitle: new text; empty string clears.
        dimensions, measures: full replacement lists, same shape as in
            `add_sheet_object`.
        position: `{col, row, colspan, rowspan}`; any subset, the rest stay.
        extra_properties: raw Qlik properties merged over the object. Cannot
            set qInfo, qHyperCubeDef, visualization or cells.

    Returns:
        `updated`, `object_id`, `object_type`, `problems`, and `cell` when
        the object was moved.
    """
    e = _check()
    if e:
        return e
    if all(v is None for v in (title, subtitle, dimensions, measures, position,
                               extra_properties)):
        return _err("Nothing to change", error_category="invalid_argument")
    try:
        extra = check_extra_properties(extra_properties)
        place = normalise_position(position, (1, 1)) if position is not None else None
        if place is not None:
            place = {k: v for k, v in place.items() if v is not None}
        result = context.engine_api.update_object(
            _open(app_id), object_id, sheet_id, title, subtitle,
            dimensions, measures, place or None, extra)
    except Exception as ex:
        return _failed("Could not update the object", ex, app_id=app_id, object_id=object_id)
    if not result.get("updated"):
        return _err("Engine rejected the change; the object is as it was",
                    problems=result["problems"], app_id=app_id, object_id=object_id)
    return _ok({"app_id": app_id, **result})


@_write_tool()
@_timed
@_engine_serialised
def delete_sheet_object(app_id: str, object_id: str,
                        sheet_id: Optional[str] = None) -> str:
    """
    Remove one object from a sheet. DESTRUCTIVE WRITE OPERATION.

    Args:
        app_id: application GUID, unpublished.
        object_id: from `get_app_sheet_objects`.
        sheet_id: the sheet it sits on. Give it: without it the object is
            destroyed but its empty slot stays on the sheet.

    Returns:
        `cell_removed`: whether the object's slot on the sheet was cleared.
    """
    e = _check()
    if e:
        return e
    try:
        result = context.engine_api.delete_object(_open(app_id), object_id, sheet_id)
    except Exception as ex:
        return _failed("Could not delete the object", ex, app_id=app_id, object_id=object_id)
    return _ok({"app_id": app_id, **result})


@_write_tool()
@_timed
@_engine_serialised
def get_app_options(app_id: str) -> str:
    """
    Read an app's settings: theme, menus, animation, sheet title style, thumbnail.

    The settings of the app's options dialog (Параметры → Вид, Стиль заголовка
    листа). `get_app_details` and the app layout do not carry them.

    Args:
        app_id: application GUID.

    Returns:
        `options` (theme, rtl, hover_menu, context_menu, chart_animations,
        sheet_title_background, sheet_title_gradient, sheet_title_color,
        sheet_logo_url, sheet_logo_position), `thumbnail_url` and `stored`
        (false when nobody has opened the options dialog yet, so nothing
        is written and the app runs on defaults). To learn the exact
        value a theme is stored under, read an app that already uses it.

    Does not return:
        The Insight Advisor switch, which Qlik stores elsewhere.
    """
    e = _check()
    if e:
        return e
    try:
        result = context.engine_api.get_app_options(_open(app_id))
    except Exception as ex:
        return _failed("Could not read the app options", ex, app_id=app_id)
    return _ok({"app_id": app_id, **result})


@_write_tool()
@_timed
@_engine_serialised
def set_app_options(
    app_id: str,
    theme: Optional[str] = None,
    rtl: Optional[bool] = None,
    hover_menu: Optional[bool] = None,
    context_menu: Optional[bool] = None,
    chart_animations: Optional[bool] = None,
    sheet_title_background: Optional[str] = None,
    sheet_title_gradient: Optional[str] = None,
    sheet_title_color: Optional[str] = None,
    sheet_logo_url: Optional[str] = None,
    sheet_logo_position: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
) -> str:
    """
    Change an app's settings. WRITE OPERATION.

    WHEN TO USE
        To set a theme, switch menus and animation, style the sheet title,
        or give the app a thumbnail, instead of doing it by hand in the
        options dialog. Only what you pass is changed.

    WHEN NOT TO USE
        On a published app: Qlik refuses.
        To turn Insight Advisor off: not covered.

    Args:
        app_id: application GUID, unpublished.
        theme: the theme as it is stored, e.g. `Dark Theme` (read it from an
            app that already uses the theme with `get_app_options`). Engine
            accepts any text, so a misspelt name does not fail: the app
            quietly falls back to its default theme.
        rtl: right-to-left layout.
        hover_menu: the menu shown over a visualisation on hover.
        context_menu: the right-click menu of a visualisation.
        chart_animations: animate charts.
        sheet_title_background, sheet_title_gradient, sheet_title_color:
            colours of the sheet title bar, as CSS colour strings; empty
            string restores the default.
        sheet_logo_url, sheet_logo_position: logo in the sheet title,
            a server path (see `upload_app_image`) and its position.
        thumbnail_url: the app's thumbnail, a server path such as
            `/appcontent/<app id>/cover.png` or `/content/Default/<file>`;
            empty string clears it.

    Returns:
        `options` and `thumbnail_url` as stored after the change. The hub
        caches thumbnails, so refresh the page to see a new one.
    """
    e = _check()
    if e:
        return e
    try:
        changes, thumbnail = build_app_option_changes(
            theme=theme, rtl=rtl, hover_menu=hover_menu, context_menu=context_menu,
            chart_animations=chart_animations, sheet_title_background=sheet_title_background,
            sheet_title_gradient=sheet_title_gradient, sheet_title_color=sheet_title_color,
            sheet_logo_url=sheet_logo_url, sheet_logo_position=sheet_logo_position,
            thumbnail_url=thumbnail_url)
        if not changes and thumbnail is None:
            return _err("Nothing to change", error_category="invalid_argument")
        result = context.engine_api.set_app_options(_open(app_id), changes, thumbnail)
    except Exception as ex:
        return _failed("Could not change the app options", ex, app_id=app_id)
    return _ok({"app_id": app_id, **{k: v for k, v in result.items() if k != "raw"}})


_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".svg": "image/svg+xml"}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024


@_write_tool()
@_timed
def upload_app_image(app_id: str, image_path: str, name: Optional[str] = None) -> str:
    """
    Upload an image into an app's content library. WRITE OPERATION.

    WHEN TO USE
        To get a server path for a cover: the result goes into
        `set_app_options(thumbnail_url=...)` for the app or
        `update_sheet(thumbnail_url=...)` for a sheet.

    The file is read from the machine the MCP server runs on. Only images
    (png, jpg, gif, svg) up to 5 MB are accepted.

    Args:
        app_id: application GUID, unpublished.
        image_path: local path to the image.
        name: file name in the library; defaults to the local file's name.
            Letters, digits, dot, dash and underscore only. An existing
            file of that name is replaced.

    Returns:
        `thumbnail_url`: the server path to use.
    """
    e = _check()
    if e:
        return e
    ext = os.path.splitext(image_path)[1].lower()
    if ext not in _IMAGE_TYPES:
        return _err(f"Only images can be uploaded, not {ext or 'a file without an extension'}",
                    error_category="invalid_argument")
    filename = name or os.path.basename(image_path)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", filename):
        return _err("name may only hold letters, digits, dot, dash and underscore",
                    error_category="invalid_argument")
    try:
        with open(image_path, "rb") as handle:
            data = handle.read(_MAX_IMAGE_BYTES + 1)
    except OSError as ex:
        return _err(f"Could not read the image: {ex}", error_category="invalid_argument")
    if not data:
        return _err("The image file is empty", error_category="invalid_argument")
    if len(data) > _MAX_IMAGE_BYTES:
        return _err(f"The image is over {_MAX_IMAGE_BYTES // (1024 * 1024)} MB",
                    error_category="invalid_argument")
    content_type = _IMAGE_TYPES.get(os.path.splitext(filename)[1].lower()) \
        or mimetypes.guess_type(filename)[0] or _IMAGE_TYPES[ext]
    try:
        result = context.repo_api.upload_app_content(app_id, filename, data, content_type)
    except Exception as ex:
        return _failed("Could not upload the image", ex, app_id=app_id)
    if isinstance(result, dict) and result.get("error"):
        return _err(f"Could not upload the image: {result['error']}", app_id=app_id)
    return _ok({"app_id": app_id, "file": filename, "bytes": len(data),
                "thumbnail_url": f"/appcontent/{app_id}/{filename}",
                "qrs_response": result})
