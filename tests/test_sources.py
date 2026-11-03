"""The structure of an app's data sources: connections, folders, files,
tables and field names.

The point of these tools is what they leave out. They name things; they never
read a value, ask for a preview or return a connection string, and the tests
below pin each of those.
"""

import contextlib
import json
from types import SimpleNamespace

import pytest

from qlik_sense_mcp_server.engine import sources
from qlik_sense_mcp_server.engine.sources import EngineSourcesMixin
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.tools import engine as tools

CONNECTIONS = [
    {"qId": "1", "qName": "data_files", "qType": "folder",
     "qConnectionString": "D:\\Data\\"},
    {"qId": "2", "qName": "warehouse", "qType": "ODBC",
     "qConnectionString": "ODBC CONNECT TO [warehouse_dsn;READTIMEOUT=3600;PWD=hunter2;]"},
    {"qId": "3", "qName": "analytics", "qType": "QvOdbcConnectorPackage.exe",
     "qConnectionString": 'CUSTOM CONNECT TO "provider=QvOdbcConnectorPackage.exe;driver=gbq;'
                          'OAuthMechanism=0;Email=someone@corp.example;RefreshToken=SECRET"'},
    {"qId": "4", "qName": "pg", "qType": "QvOdbcConnectorPackage.exe",
     "qConnectionString": 'CUSTOM CONNECT TO "provider=QvOdbcConnectorPackage.exe;'
                          'driver=postgres;host=10.1.2.3;password=topsecret"'},
]


# ─── Describing a connection ─────────────────────────────────────────────────


class TestConnectionDescription:
    @pytest.mark.parametrize("secret", ["hunter2", "someone@corp.example", "SECRET",
                                        "10.1.2.3", "topsecret", "READTIMEOUT"])
    def test_nothing_that_could_be_a_credential_comes_out(self, secret):
        text = json.dumps([sources.describe_connection(c) for c in CONNECTIONS])
        assert secret not in text

    def test_a_folder_shows_its_path(self):
        out = sources.describe_connection(CONNECTIONS[0])
        assert out["kind"] == "folder" and out["path"] == "D:\\Data\\"

    def test_a_database_shows_provider_driver_or_dsn(self):
        assert sources.describe_connection(CONNECTIONS[1])["dsn"] == "warehouse_dsn"
        custom = sources.describe_connection(CONNECTIONS[2])
        assert custom["driver"] == "gbq" and custom["provider"] == "QvOdbcConnectorPackage.exe"
        assert all(sources.describe_connection(c)["kind"] == "database" for c in CONNECTIONS[1:])

    @pytest.mark.parametrize("name", ["data_files", "DATA_FILES", "lib://data_files",
                                      "lib://data_files/a/b.qvd", " data_files "])
    def test_found_with_or_without_lib(self, name):
        assert sources.find_connection(CONNECTIONS, name)["qId"] == "1"

    def test_an_unknown_one_is_named_and_nothing_more(self):
        with pytest.raises(ValueError, match="No connection 'nope'") as raised:
            sources.find_connection(CONNECTIONS, "nope")
        assert "warehouse" not in str(raised.value)     # the caller can list them itself


class TestFormatsAndFields:
    @pytest.mark.parametrize("path,expected", [
        ("a/b.qvd", "QVD"), ("x.CSV", "CSV"), ("x.xlsx", "EXCEL_OOXML"),
        ("x.parquet", "PARQUET"), ("x.json", "JSON")])
    def test_the_format_comes_from_the_extension(self, path, expected):
        assert sources.file_format(path) == expected

    def test_an_unknown_extension_needs_a_type(self):
        with pytest.raises(ValueError, match="not known from its extension"):
            sources.file_format("a/b.dat")
        assert sources.file_format("a/b.dat", "csv") == "CSV"

    @pytest.mark.parametrize("path,file_type,expected", [
        ("a/b.qvd", None, True), ("a/b.dat", None, False), ("a/b.dat", "csv", True),
        ("a/folder.v2", None, False), ("", None, False), (None, None, False)])
    def test_a_path_is_a_file_only_when_something_says_so(self, path, file_type, expected):
        """A folder's name may hold a dot."""
        assert sources.is_file_path(path, file_type) is expected

    def test_a_field_is_a_name_and_a_type_only_when_reported(self):
        assert sources.field_entry({"qName": "a"}) == {"name": "a"}
        assert sources.field_entry({"qName": "a", "qDataType": {"qType": "INTEGER"}}) == {
            "name": "a", "type": "INTEGER"}
        assert sources.field_entry(
            {"qName": "a", "qDataType": {"qType": "DECIMAL", "qHasScale": 2}})["scale"] == 2
        assert sources.field_entry({"qName": "a", "qDataType": "TEXT"})["type"] == "TEXT"


# ─── The Engine calls ────────────────────────────────────────────────────────

# Everything the mixin may ask Engine. Anything that could return rows is
# absent, and the tests fail if one appears.
ALLOWED_METHODS = {
    "GetConnections", "GetFolderItemsForConnection", "GetFileTables",
    "GetFileTableFields", "GetDatabases", "GetDatabaseTables", "GetDatabaseTableFields",
}


class Client(EngineSourcesMixin):
    """Answers the source calls from canned replies and records what was asked."""

    def __init__(self):
        self.asked = []

    def send_request(self, method, params=None, handle=-1, timeout=None):
        self.asked.append((method, params))
        return {
            "GetConnections": {"qConnections": CONNECTIONS},
            "GetFolderItemsForConnection": {"qFolderItems": [
                {"qName": "day_2024_a.qvd", "qType": "FILE"}, {"qName": "sub", "qType": "FOLDER"}]},
            "GetFileTables": {"qTables": [{"qName": "t1"}, {"qName": "t2"}, {"qName": "t3"}]},
            "GetFileTableFields": {"qFields": [{"qName": "id"}, {"qName": "dt"}]},
            "GetDatabases": {"qDatabases": [{"qName": "db1", "qIsDefault": True}, {"qName": "db2"}]},
            "GetDatabaseTables": {"qTables": [{"qName": "t", "qType": "TABLE"},
                                              {"qName": "v", "qType": "VIEW"}]},
            "GetDatabaseTableFields": {"qFields": [{"qName": "id"}, {"qName": "name"}]},
        }[method]

    def methods(self):
        return {m for m, _ in self.asked}


class TestEngineCalls:
    def test_connections_come_back_described(self):
        out = Client().list_connections(1)
        assert [c["name"] for c in out] == ["data_files", "warehouse", "analytics", "pg"]
        assert "qConnectionString" not in json.dumps(out)

    def test_a_folder_lists_names_and_kinds(self):
        client = Client()
        out = client.source_folder(1, "lib://data_files", "x/y")
        assert out == [{"name": "day_2024_a.qvd", "type": "file"},
                       {"name": "sub", "type": "folder"}]
        assert ("GetFolderItemsForConnection", ["1", "x/y"]) in client.asked

    def test_a_file_gives_tables_with_field_names(self):
        client = Client()
        out = client.source_file(1, "data_files", "a/b.qvd", None, 50)
        assert out["format"] == "QVD" and [t["name"] for t in out["tables"]] == ["t1", "t2", "t3"]
        assert out["tables"][0]["fields"] == [{"name": "id"}, {"name": "dt"}]
        assert ("GetFileTables", ["1", "a/b.qvd", {"qType": "QVD"}]) in client.asked

    def test_a_file_with_many_tables_is_cut_and_says_so(self):
        out = Client().source_file(1, "data_files", "a/b.xlsx", None, 2)
        assert len(out["tables"]) == 2 and out["truncated"] is True

    def test_a_database_descends_databases_tables_fields(self):
        client = Client()
        assert client.source_databases(1, "warehouse") == [
            {"name": "db1", "default": True}, {"name": "db2", "default": False}]
        assert client.source_tables(1, "warehouse", "db1", "") == [
            {"name": "t", "type": "table"}, {"name": "v", "type": "view"}]
        assert client.source_table_fields(1, "warehouse", "db1", "", "t") == [
            {"name": "id"}, {"name": "name"}]
        assert ("GetDatabaseTableFields", ["2", "db1", "", "t"]) in client.asked

    def test_an_unknown_format_is_refused_before_asking_engine(self):
        client = Client()
        with pytest.raises(ValueError, match="not known from its extension"):
            client.source_file(1, "data_files", "a/b.dat", None, 50)
        assert client.methods() == {"GetConnections"}

    def test_no_method_that_could_return_data_is_ever_called(self):
        """Structure only: a preview or a row would come from a method that is
        not on this list."""
        client = Client()
        client.list_connections(1)
        client.source_folder(1, "data_files", "")
        client.source_file(1, "data_files", "a.qvd", None, 50)
        client.source_databases(1, "warehouse")
        client.source_tables(1, "warehouse", "db1", "")
        client.source_table_fields(1, "warehouse", "db1", "", "t")
        assert client.methods() <= ALLOWED_METHODS


# ─── The tools ───────────────────────────────────────────────────────────────


class Engine(Client):
    """The client as the tools see it: opens apps, serialises calls."""

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def ensure_app(self, app_id, no_data=False):
        return 1


@pytest.fixture
def engine(monkeypatch):
    eng = Engine()
    monkeypatch.setattr(context, "engine_api", eng)
    monkeypatch.setattr(context, "repo_api", SimpleNamespace())
    return eng


def run(tool, **kwargs):
    return json.loads(tool(**kwargs))


class TestConnectionsTool:
    def test_lists_them_without_their_strings(self, engine):
        out = run(tools.get_data_connections, app_id="A")
        assert out["count"] == 4
        text = json.dumps(out)
        for secret in ("hunter2", "SECRET", "topsecret", "someone@corp.example", "10.1.2.3"):
            assert secret not in text


class TestStructureTool:
    def test_a_folder_lists_its_contents_with_lib_paths(self, engine):
        out = run(tools.get_source_structure, app_id="A", connection="data_files", path="Data")
        assert out["level"] == "folder" and out["total"] == 2
        assert out["items"][0]["lib"] == "lib://data_files/Data/day_2024_a.qvd"

    def test_the_root_of_a_folder(self, engine):
        out = run(tools.get_source_structure, app_id="A", connection="lib://data_files")
        assert out["level"] == "folder" and out["items"][0]["lib"] == "lib://data_files/day_2024_a.qvd"

    def test_a_pattern_narrows_and_the_total_says_what_matched(self, engine):
        out = run(tools.get_source_structure, app_id="A", connection="data_files",
                  name_pattern="day_2024_*")
        assert [i["name"] for i in out["items"]] == ["day_2024_a.qvd"] and out["total"] == 1

    def test_a_cut_list_says_so(self, engine):
        out = run(tools.get_source_structure, app_id="A", connection="data_files", limit=1)
        assert len(out["items"]) == 1 and out["truncated"] is True and "narrow" in out["note"]

    def test_a_file_path_describes_its_tables_and_fields(self, engine):
        out = run(tools.get_source_structure, app_id="A", connection="data_files",
                  path="Data/x.qvd")
        assert out["level"] == "file" and out["tables"][0]["fields"] == [
            {"name": "id"}, {"name": "dt"}]

    def test_a_path_with_an_unknown_extension_is_a_folder_unless_told_otherwise(self, engine):
        as_folder = run(tools.get_source_structure, app_id="A", connection="data_files",
                        path="a/b.dat")
        as_file = run(tools.get_source_structure, app_id="A", connection="data_files",
                      path="a/b.dat", file_type="csv")
        assert as_folder["level"] == "folder" and as_file["level"] == "file"

    def test_a_database_descends_in_three_steps(self, engine):
        a = run(tools.get_source_structure, app_id="A", connection="warehouse")
        b = run(tools.get_source_structure, app_id="A", connection="warehouse", database="db1")
        c = run(tools.get_source_structure, app_id="A", connection="warehouse", database="db1",
                table="t")
        assert (a["level"], b["level"], c["level"]) == ("databases", "tables", "fields")
        assert [f["name"] for f in c["items"]] == ["id", "name"]

    def test_a_source_that_reports_no_types_says_so(self, engine):
        c = run(tools.get_source_structure, app_id="A", connection="warehouse", database="db1",
                table="t")
        assert "names only" in c["note"]

    def test_an_unknown_connection_is_named_without_advice(self, engine):
        out = run(tools.get_source_structure, app_id="A", connection="nope")
        assert out["error_category"] == "invalid_argument" and "'nope'" in out["error"]
        assert "allowed_values" not in out and "hint" not in out

    @pytest.mark.parametrize("limit", [0, -3, True, "5"])
    def test_a_bad_limit_is_refused(self, engine, limit):
        out = run(tools.get_source_structure, app_id="A", connection="data_files", limit=limit)
        assert out["error_category"] == "invalid_argument"

    def test_an_engine_failure_is_an_engine_error(self, engine, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("socket gone")

        monkeypatch.setattr(engine, "send_request", boom)
        out = run(tools.get_source_structure, app_id="A", connection="data_files")
        assert out["error_category"] == "engine_api_error" and "socket gone" in out["error"]
