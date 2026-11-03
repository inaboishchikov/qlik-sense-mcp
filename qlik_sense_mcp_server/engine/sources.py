"""The structure of an app's data sources: connections, folders, files,
tables and field names.

Nothing here reads data. The calls ask Engine for names - which connections
exist, what a folder holds, which tables a file or a database has and what
their fields are called - and never for a preview, a sample or a row.
Connection strings are not returned either: they carry hosts, accounts and
tokens, so only the kind of connection, its provider and, for a folder, its
path are.
"""

import re
from typing import Any, Dict, List, Optional

# File extension -> Engine's data format for GetFileTables.
FILE_FORMATS = {
    ".qvd": "QVD", ".qvx": "QVX", ".csv": "CSV", ".txt": "CSV", ".tsv": "CSV",
    ".xls": "EXCEL_BIFF", ".xlsx": "EXCEL_OOXML", ".xlsm": "EXCEL_OOXML",
    ".parquet": "PARQUET", ".json": "JSON", ".xml": "XML",
}

_PROVIDER = re.compile(r"provider=([^;\"]+)", re.IGNORECASE)
_DRIVER = re.compile(r"driver=([^;\"]+)", re.IGNORECASE)
_DSN = re.compile(r"ODBC CONNECT TO \[([^;\]]+)", re.IGNORECASE)


def describe_connection(connection: Dict[str, Any]) -> Dict[str, Any]:
    """A connection without its connection string.

    A folder shows its path, which is what a script needs to know; a custom
    connector only its provider and driver; an ODBC connection the data
    source name. Nothing that can hold a credential.
    """
    kind = str(connection.get("qType") or "")
    text = str(connection.get("qConnectionString") or "")
    is_folder = kind.lower() == "folder"
    out: Dict[str, Any] = {
        "name": connection.get("qName"),
        "type": kind,
        "kind": "folder" if is_folder else "database",
    }
    if is_folder:
        out["path"] = text
    else:
        for label, pattern in (("provider", _PROVIDER), ("driver", _DRIVER), ("dsn", _DSN)):
            found = pattern.search(text)
            if found:
                out[label] = found.group(1)
    return out


def find_connection(connections: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    """A connection by name, with or without the `lib://` prefix."""
    wanted = str(name).strip()
    if wanted.lower().startswith("lib://"):
        wanted = wanted[6:].split("/", 1)[0]
    for connection in connections:
        if str(connection.get("qName", "")).lower() == wanted.lower():
            return connection
    raise ValueError(f"No connection {name!r}.")


def file_format(path: str, override: Optional[str] = None) -> str:
    """Engine's name for the format of the file at `path`."""
    if override:
        return str(override).upper()
    dot = path.rfind(".")
    extension = path[dot:].lower() if dot >= 0 else ""
    if extension not in FILE_FORMATS:
        raise ValueError(f"The format of {path!r} is not known from its extension.")
    return FILE_FORMATS[extension]


def is_file_path(path: Optional[str], file_type: Optional[str]) -> bool:
    """Whether `path` names a file rather than a folder.

    A folder's name may hold a dot, so only a known extension, or a
    `file_type` the caller gave, makes a path a file.
    """
    if not path:
        return False
    return bool(file_type) or any(path.lower().endswith(ext) for ext in FILE_FORMATS)


def field_entry(field: Dict[str, Any]) -> Dict[str, Any]:
    """A field as a name, and a type only where the driver reports one."""
    out: Dict[str, Any] = {"name": field.get("qName")}
    data_type = field.get("qDataType")
    if isinstance(data_type, dict) and data_type.get("qType"):
        out["type"] = data_type["qType"]
        for source, key in (("qHasScale", "scale"), ("qHasPrecision", "precision")):
            if data_type.get(source) is not None:
                out[key] = data_type[source]
    elif isinstance(data_type, str) and data_type:
        out["type"] = data_type
    return out


class EngineSourcesMixin:
    """Structure of the data sources reachable from an open app."""

    def list_connections(self, app_handle: int) -> List[Dict[str, Any]]:
        result = self.send_request("GetConnections", [], handle=app_handle)
        return [describe_connection(c) for c in result.get("qConnections") or []]

    def _source_connection(self, app_handle: int, name: str) -> Dict[str, Any]:
        result = self.send_request("GetConnections", [], handle=app_handle)
        return find_connection(result.get("qConnections") or [], name)

    def source_folder(self, app_handle: int, connection: str,
                      path: str) -> List[Dict[str, Any]]:
        conn = self._source_connection(app_handle, connection)
        result = self.send_request(
            "GetFolderItemsForConnection", [conn["qId"], path or ""], handle=app_handle)
        return [{"name": item.get("qName"), "type": str(item.get("qType", "")).lower()}
                for item in result.get("qFolderItems") or []]

    def source_file(self, app_handle: int, connection: str, path: str,
                    file_type: Optional[str], max_tables: int) -> Dict[str, Any]:
        """The tables of a file and their field names. No preview is asked for."""
        conn = self._source_connection(app_handle, connection)
        data_format = {"qType": file_format(path, file_type)}
        tables = self.send_request(
            "GetFileTables", [conn["qId"], path, data_format],
            handle=app_handle).get("qTables") or []
        out = []
        for table in tables[:max_tables]:
            name = table.get("qName", "")
            fields = self.send_request(
                "GetFileTableFields", [conn["qId"], path, data_format, name],
                handle=app_handle).get("qFields") or []
            out.append({"name": name, "fields": [field_entry(f) for f in fields]})
        reply: Dict[str, Any] = {"format": data_format["qType"], "tables": out}
        if len(tables) > max_tables:
            reply["truncated"] = True
        return reply

    def source_databases(self, app_handle: int, connection: str) -> List[Dict[str, Any]]:
        conn = self._source_connection(app_handle, connection)
        result = self.send_request("GetDatabases", [conn["qId"]], handle=app_handle)
        return [{"name": d.get("qName"), "default": bool(d.get("qIsDefault"))}
                for d in result.get("qDatabases") or []]

    def source_tables(self, app_handle: int, connection: str, database: str,
                      owner: str) -> List[Dict[str, Any]]:
        conn = self._source_connection(app_handle, connection)
        result = self.send_request(
            "GetDatabaseTables", [conn["qId"], database, owner or ""], handle=app_handle)
        return [{"name": t.get("qName"), "type": str(t.get("qType", "")).lower()}
                for t in result.get("qTables") or []]

    def source_table_fields(self, app_handle: int, connection: str, database: str,
                            owner: str, table: str) -> List[Dict[str, Any]]:
        conn = self._source_connection(app_handle, connection)
        result = self.send_request(
            "GetDatabaseTableFields", [conn["qId"], database, owner or "", table],
            handle=app_handle)
        return [field_entry(f) for f in result.get("qFields") or []]
