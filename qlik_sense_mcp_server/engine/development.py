"""What an author needs while building an app, short of changing it.

Checking an object or a sheet, trying a script without saving it, and
running a reload and reading its log. The structure of a data source is in
`sources.py`.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from .authoring import format_script_errors
from .lint import lint_object, lint_sheet_layout, summarise, finding
from .authoring import layout_problems

logger = logging.getLogger(__name__)

_LIB = re.compile(r"lib://([^/\\\]\)\s'\"]+)", re.IGNORECASE)
_WORD_CHAR = re.compile(r"[A-Za-z0-9_]")


def _without_comments(script: str) -> str:
    """The script with comments blanked and every line break kept."""
    blank = lambda m: re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"/\*.*?\*/", blank, script, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", blank, text)


def scan_script(script: str, connection_names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """What Engine's own syntax check lets through.

    `CheckScriptSyntax` only catches gross errors. An unclosed bracket, an
    unterminated string, a missing semicolon and a `lib://` connection that
    does not exist all pass it and fail only at reload, one run at a time.
    This reads the script once, skipping comments and quoted text, and
    reports those, with line numbers. It is a scan, not a parser: it finds
    the errors that are certain and says nothing about the rest.
    """
    findings: List[Dict[str, Any]] = []
    stack: List[tuple] = []          # (char, line) of open ( and [
    line, i, n = 1, 0, len(script)
    last_code = ""                    # last significant character, for the trailing ;
    last_line = 1

    def add(severity: str, at: int, message: str) -> None:
        findings.append({"severity": severity, "line": at, "message": message})

    fresh = True                      # no code yet in the current statement

    while i < n:
        c = script[i]
        two = script[i:i + 2]
        if c == "\n":
            line += 1
            i += 1
            continue
        in_name = bool(stack) and stack[-1][0] == "["
        if not in_name and two == "//" and (i == 0 or script[i - 1] != ":"):
            while i < n and script[i] != "\n":
                i += 1
            continue
        if not in_name and two == "/*":
            end = script.find("*/", i + 2)
            if end < 0:
                add("error", line, "a /* comment is never closed")
                break
            line += script.count("\n", i, end)
            i = end + 2
            continue
        if (not in_name and script[i:i + 3].upper() == "REM" and fresh
                and (i + 3 >= n or not _WORD_CHAR.match(script[i + 3]))):
            end = script.find(";", i)
            end = n if end < 0 else end
            line += script.count("\n", i, end)
            i = end
            continue
        if in_name:
            if c == "]":
                stack.pop()
            i += 1
            continue
        if c in "'\"":
            start_line, j = line, i + 1
            closed = False
            while j < n:
                if script[j] == "\n":
                    line += 1
                if script[j] == c:
                    if j + 1 < n and script[j + 1] == c:     # '' is an escaped quote
                        j += 2
                        continue
                    closed = True
                    break
                j += 1
            if not closed:
                add("error", start_line, f"a {c} string is never closed")
                break
            i = j + 1
            last_code = c
            last_line = line
            fresh = False
            continue
        if c == "[":
            fresh = False
            stack.append(("[", line))
        elif c == "(":
            stack.append(("(", line))
        elif c == ")":
            if stack and stack[-1][0] == "(":
                stack.pop()
            else:
                add("error", line, "a ) has no matching (")
        elif c == "]":
            add("error", line, "a ] has no matching [")
        if not c.isspace():
            last_code = c
            last_line = line
            fresh = c == ";"
        i += 1

    for char, at in stack:
        add("error", at, f"a {char} is never closed")
    if last_code and last_code != ";" and not any(f["severity"] == "error" for f in findings):
        add("warning", last_line, "the script does not end with a semicolon")

    if connection_names is not None:
        known = {name.lower() for name in connection_names}
        reported = set()
        for number, code in enumerate(_without_comments(script).split("\n"), start=1):
            for match in _LIB.finditer(code):
                name = match.group(1)
                if name.lower() not in known and (name.lower(), number) not in reported:
                    reported.add((name.lower(), number))
                    add("error", number,
                        f"connection {name!r} is not available to this identity: "
                        f"`lib://{name}/...` fails with 'Connection not found'")
    findings.sort(key=lambda f: (f["line"], f["severity"]))
    return findings


def _split_error(text: str) -> Dict[str, Any]:
    """`Connection not found: x\r\nLOAD ...` -> message and the statement."""
    head, _, rest = str(text).partition("\r\n")
    if not rest:
        head, _, rest = str(text).partition("\n")
    return {"message": head.strip(), "statement": rest.strip(), "severity": "error"}


def parse_progress(progress: Dict[str, Any], failed: bool = False) -> Dict[str, Any]:
    """What a reload reported: errors with the statement that failed, and the log.

    Engine reports a failure in one of two shapes: `qErrorData` entries, or
    a script-error message among the progress messages (code 10) with the
    statement after the text. When the reload failed and only the second is
    there, it becomes the error, so the caller reads one place.
    """
    errors: List[Dict[str, Any]] = []
    seen = set()
    for err in progress.get("qErrorData") or []:
        key = (err.get("qErrorString"), err.get("qLine"))
        if key in seen:
            continue
        seen.add(key)
        errors.append({"message": err.get("qErrorString"), "statement": err.get("qLine"),
                       "severity": str(err.get("qErrorDataCode", "")).replace("EDC_", "").lower()
                       or "error"})
    messages: List[str] = []
    coded: List[tuple] = []
    for message in progress.get("qPersistentProgressMessages") or []:
        text = " ".join(str(p) for p in (message.get("qMessageParameters") or []) if p)
        if text:
            messages.append(text)
            coded.append((message.get("qMessageCode"), text))
    if failed and not errors and coded:
        script_errors = [text for code, text in coded if code == 10] or [coded[-1][1]]
        errors = [_split_error(text) for text in script_errors]
        messages = [m for m in messages if m not in script_errors]
    out: Dict[str, Any] = {"errors": errors, "log": messages}
    if progress.get("qMillisecs") is not None:
        out["seconds"] = round(progress["qMillisecs"] / 1000.0, 2)
    return out


class EngineDevelopmentMixin:
    # ── checking ──────────────────────────────────────────────────────────

    def _children_of(self, app_handle: int, layout: Dict[str, Any]):
        """Property structures and layouts of an object's children."""
        props_list: List[Dict[str, Any]] = []
        problems: List[Dict[str, Any]] = []
        for item in ((layout.get("qChildList") or {}).get("qItems")) or []:
            child_id = (item.get("qInfo") or {}).get("qId")
            if not child_id:
                continue
            try:
                handle = self._handle_of(app_handle, child_id, "object")
                props_list.append(self._properties(handle))
                for text in layout_problems(self._layout(handle)):
                    problems.append(finding("error", f"child {child_id}", text))
            except Exception as exc:
                problems.append(finding("error", f"child {child_id}",
                                        f"could not be read: {exc}"))
        return props_list, problems

    def validate_object(self, app_handle: int, object_id: str) -> Dict[str, Any]:
        handle = self._handle_of(app_handle, object_id, "object")
        props = self._properties(handle)
        layout = self._layout(handle)
        children, child_problems = self._children_of(app_handle, layout)
        findings = lint_object(props, children, layout) + child_problems
        return {"object_id": object_id,
                "object_type": (props.get("qInfo") or {}).get("qType"),
                "title": props.get("title") if isinstance(props.get("title"), str) else "",
                "findings": findings, **summarise(findings)}

    def validate_sheet(self, app_handle: int, sheet_id: str) -> Dict[str, Any]:
        handle = self._handle_of(app_handle, sheet_id, "sheet")
        props = self._properties(handle)
        layout = self._layout(handle)
        items = ((layout.get("qChildList") or {}).get("qItems")) or []
        ids = [(i.get("qInfo") or {}).get("qId") for i in items if (i.get("qInfo") or {}).get("qId")]
        sheet_findings = lint_sheet_layout(props, ids)
        objects = [self.validate_object(app_handle, object_id) for object_id in ids]
        total_errors = sum(1 for f in sheet_findings if f["severity"] == "error") \
            + sum(o["errors"] for o in objects)
        total_warnings = sum(1 for f in sheet_findings if f["severity"] == "warning") \
            + sum(o["warnings"] for o in objects)
        return {"sheet_id": sheet_id,
                "title": (props.get("qMetaDef") or {}).get("title", ""),
                "sheet_findings": sheet_findings, "objects": objects,
                "ok": total_errors == 0, "errors": total_errors, "warnings": total_warnings}

    # ── script and reload ────────────────────────────────────────────────

    def check_script(self, app_handle: int, script: Optional[str] = None) -> Dict[str, Any]:
        """Syntax-check a script, or the current one, and change nothing.

        A candidate is set for the check and the original put back, whatever
        the outcome. Nothing is saved.
        """
        current = self.get_script(app_handle) or ""
        candidate = current if script is None else script
        changed = script is not None and script != current
        try:
            if changed:
                self.set_script(app_handle, candidate)
            errors = format_script_errors(candidate, self.check_script_syntax(app_handle))
        finally:
            if changed:
                try:
                    self.set_script(app_handle, current)
                except Exception as exc:
                    logger.error("Could not restore the script after a check: %s", exc)
        try:
            names = [c["name"] for c in self.list_connections(app_handle)]
        except Exception as exc:
            logger.debug("Could not list connections for the script scan: %s", exc)
            names = None
        scan = scan_script(candidate, names)
        ok = not errors and not any(f["severity"] == "error" for f in scan)
        return {"ok": ok, "syntax_errors": errors, "scan": scan,
                "connections_checked": names is not None, "script_length": len(candidate)}

    def reload_logged(self, app_handle: int, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Reload in this session, save if it worked, and say what happened.

        The reload's own request id selects its progress report, which Engine
        keeps after the reload ends: the errors with the statement that
        failed, and the log lines.
        """
        request_id = self.request_id + 1
        result = self.send_request("DoReload", [0, False, False], handle=app_handle,
                                   timeout=timeout)
        reloaded = bool(result.get("qReturn"))
        info: Dict[str, Any] = {"errors": [], "log": []}
        try:
            progress = self.send_request("GetProgress", [request_id], handle=-1)
            info = parse_progress(progress.get("qProgressData") or {}, failed=not reloaded)
        except Exception as exc:
            logger.warning("Could not read the reload progress: %s", exc)
            info["note"] = "The reload's progress report could not be read."
        if reloaded:
            self.save_app(app_handle)
        return {"reloaded": reloaded, **info}
