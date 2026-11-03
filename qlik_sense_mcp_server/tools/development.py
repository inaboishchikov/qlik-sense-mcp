"""Development tools: check what was built, and try a script.

Registered under QLIK_WRITE_TOOLS together with the authoring tools they
serve. They return verdicts, never rows.
"""

from typing import Any, Dict, Optional

from . import context
from .authoring import _open
from .context import _write_tool
from .helpers import _check, _engine_serialised, _err, _ok, _timed


def _fail(message: str, ex: Exception, **extra: Any) -> str:
    return _err(f"{message}: {ex}", **extra,
                **({"error_category": "invalid_argument"} if isinstance(ex, ValueError) else {}))


@_write_tool()
@_timed
@_engine_serialised
def validate_object(app_id: str, object_id: str) -> str:
    """
    Check one object for what stops it drawing, without looking at it.

    Engine accepts an object it cannot draw: it answers with data and no
    error, and the client shows an empty place. This finds the structural
    gaps behind that, and Engine's own validation errors.

    WHEN TO USE
        After `add_sheet_object` or `update_sheet_object`, and on any object
        that shows nothing in the hub.

    Args:
        app_id: application GUID.
        object_id: from `get_app_sheet_objects`.

    Returns:
        `ok` (no errors), `errors`, `warnings` and `findings`: each
        `{severity, path, message}`. An `error` means broken or Engine-
        reported; a `warning` means it differs from what Qlik's editor
        writes: harmless on a KPI or pivot table, but a table or chart
        without those drew nothing. Judge warnings by the object type.

    Does not return:
        A picture, or any data of the object.
    """
    e = _check()
    if e:
        return e
    try:
        result = context.engine_api.validate_object(_open(app_id), object_id)
    except Exception as ex:
        return _fail("Could not check the object", ex, app_id=app_id, object_id=object_id)
    return _ok({"app_id": app_id, **result})


@_write_tool()
@_timed
@_engine_serialised
def validate_sheet(app_id: str, sheet_id: str) -> str:
    """
    Check a sheet and every object on it. See `validate_object`.

    Adds the checks that need the whole sheet: an object on no cell (it
    exists but is not shown), a cell for an object that is not there,
    overlapping or off-grid cells.

    Args:
        app_id: application GUID.
        sheet_id: from `get_app_sheets`.

    Returns:
        `ok`, `errors`, `warnings` for the whole sheet, `sheet_findings`,
        and `objects`: one entry per object with its own `findings`.
    """
    e = _check()
    if e:
        return e
    try:
        result = context.engine_api.validate_sheet(_open(app_id), sheet_id)
    except Exception as ex:
        return _fail("Could not check the sheet", ex, app_id=app_id, sheet_id=sheet_id)
    return _ok({"app_id": app_id, **result})


@_write_tool()
@_timed
@_engine_serialised
def check_app_script(app_id: str, script: Optional[str] = None) -> str:
    """
    Check a load script without saving anything or running it.

    Two checks, because Qlik's own is shallow: `CheckScriptSyntax` catches
    gross errors (`FOO BAR BAZ;`, a malformed function call) and lets an
    unclosed bracket, an unterminated string, a missing semicolon and
    `LOAD LOAD FROM;` pass. So a scan reads the script as well, and checks
    every `lib://<connection>/` against the connections this identity has.

    WHEN TO USE
        To try a script before `set_app_script` or `patch_app_script`, or to
        check the one the app has. The app's script is left exactly as it
        was, whatever the outcome.

    Args:
        app_id: application GUID.
        script: the script to try; omit to check the current one.

    Returns:
        `ok`, `syntax_errors` (Qlik's: tab, line_in_tab, column, text,
        secondary; fix those without `secondary` first), `scan` (ours:
        `{severity, line, message}`) and `connections_checked`.

    Does not return:
        Whether the script will *run*. A missing file, a wrong field name or
        a failing statement shows only when it is reloaded (`reload_app`),
        which also names the statement that failed.
    """
    e = _check()
    if e:
        return e
    try:
        result = context.engine_api.check_script(_open(app_id), script)
    except Exception as ex:
        return _fail("Could not check the script", ex, app_id=app_id)
    return _ok({"app_id": app_id, **result})
