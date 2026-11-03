"""Which tools the server advertises.

The default surface is the analysis tools and nothing else. Reload-task
administration needs QRS repository-admin rights — a QMC role, not a
property of the authentication method — so it defaults to on in
certificate mode (normally a trusted admin identity) and off in JWT/form
mode (normally an ordinary analyst identity who would just get 403s).
QLIK_TASK_TOOLS overrides the default in either direction, including
turning task tools on outside certificate mode for an identity verified to
hold those rights.

A tool the caller cannot use is not free: its name and description sit in
the model's context, and a model that reads about task administration
tries it.
"""

import importlib

import pytest

from qlik_sense_mcp_server import server as srv
from qlik_sense_mcp_server.tools import context
from qlik_sense_mcp_server.tools import engine as engine_tools
from qlik_sense_mcp_server.tools import repository as repository_tools
from qlik_sense_mcp_server.tools import tasks as task_tools
from qlik_sense_mcp_server.tools import authoring as authoring_tools
from qlik_sense_mcp_server.tools import development as development_tools


TASK_TOOLS = {
    "get_tasks", "get_task_details", "start_task", "create_task",
    "update_task", "delete_task", "get_task_schedule",
    "create_task_schedule", "get_task_executions", "get_task_script_log",
    "get_failed_tasks_with_logs", "get_task_dependencies",
    "update_task_schedule", "delete_task_schedule",
}

AUTHORING_TOOLS = {
    "create_app", "reload_app", "get_app_options", "set_app_options",
    "upload_app_image", "validate_object", "validate_sheet", "check_app_script",
    "set_app_script", "patch_app_script", "create_sheet", "update_sheet",
    "delete_sheet", "add_sheet_object", "update_sheet_object",
    "delete_sheet_object",
}

ANALYSIS_TOOLS = {
    "get_about", "get_apps", "get_app_details", "get_app_script",
    "get_app_field_statistics", "engine_get_field_range", "get_app_field",
    "get_app_variables", "get_app_sheets", "get_app_sheet_objects",
    "get_app_object", "engine_query", "engine_create_hypercube",
    "search_app", "get_data_connections", "get_source_structure",
}


def _reimport():
    """Rebuild the whole tool surface from scratch.

    Registration happens at import time and depends on the environment,
    so the reload has to start at `context` — it builds the clients and
    the MCP host — and then re-run every tool module against that fresh
    host. Reloading only `server` would re-export tools registered on the
    previous host and show the previous auth mode.
    """
    module = importlib.reload(context)
    for tool_module in (repository_tools, engine_tools, task_tools,
                        authoring_tools, development_tools):
        importlib.reload(tool_module)
    return importlib.reload(srv)


@pytest.fixture
def reload_server(monkeypatch):
    """Re-import the server under a given environment, then restore it."""
    def _reload(**env):
        for key in ("QLIK_SERVER_URL", "QLIK_JWT_TOKEN", "QLIK_USER_DIRECTORY",
                    "QLIK_USER_ID", "QLIK_PASSWORD", "QLIK_CLIENT_CERT_PATH",
                    "QLIK_CLIENT_KEY_PATH", "QLIK_TASK_TOOLS",
                    "QLIK_WRITE_TOOLS"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return _reimport()

    yield _reload
    # Leave the modules in their original state for the rest of the suite.
    monkeypatch.undo()
    _reimport()


class TestDefaultSurface:
    def test_jwt_mode_registers_the_analysis_tools_only(self, reload_server):
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com/jwt",
            QLIK_JWT_TOKEN="header.payload.signature",
        )
        assert module.config.auth_mode == "jwt"
        assert set(module.mcp._tool_manager._tools) == ANALYSIS_TOOLS

    def test_certificate_mode_adds_task_administration(self, reload_server):
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com",
            QLIK_USER_DIRECTORY="COMPANY",
            QLIK_USER_ID="svc_mcp",
        )
        assert module.config.auth_mode == "certificate"
        names = set(module.mcp._tool_manager._tools)
        assert TASK_TOOLS <= names
        assert ANALYSIS_TOOLS <= names

    def test_form_mode_registers_the_analysis_tools_only(self, reload_server):
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com/forms",
            QLIK_USER_DIRECTORY="COMPANY",
            QLIK_USER_ID="ivanov",
            QLIK_PASSWORD="s3cret",
        )
        assert module.config.auth_mode == "form"
        assert set(module.mcp._tool_manager._tools) == ANALYSIS_TOOLS

    def test_an_unconfigured_server_shows_everything(self, reload_server):
        """`--help` and a misconfigured host must not silently lose tools."""
        names = set(reload_server().mcp._tool_manager._tools)
        assert TASK_TOOLS <= names
        assert ANALYSIS_TOOLS <= names


class TestTaskToolsSwitch:
    def test_jwt_mode_leaves_them_out_by_default(self, reload_server):
        """Most JWT identities are ordinary analysts without QRS admin
        rights, so the default stays off without an explicit opt-in."""
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com/jwt",
            QLIK_JWT_TOKEN="header.payload.signature",
        )
        assert set(module.mcp._tool_manager._tools) == ANALYSIS_TOOLS

    def test_jwt_mode_turns_them_on_when_explicitly_asked_for(self, reload_server):
        """QRS checks the QMC role behind the session, not how it was
        authenticated — an operator who has verified the JWT identity holds
        admin rights can opt in explicitly."""
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com/jwt",
            QLIK_JWT_TOKEN="header.payload.signature",
            QLIK_TASK_TOOLS="true",
        )
        names = set(module.mcp._tool_manager._tools)
        assert TASK_TOOLS <= names
        assert ANALYSIS_TOOLS <= names

    def test_form_mode_leaves_them_out_by_default(self, reload_server):
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com/forms",
            QLIK_USER_DIRECTORY="COMPANY",
            QLIK_USER_ID="ivanov",
            QLIK_PASSWORD="s3cret",
        )
        assert set(module.mcp._tool_manager._tools) == ANALYSIS_TOOLS

    def test_form_mode_turns_them_on_when_explicitly_asked_for(self, reload_server):
        """Same reasoning as JWT above — verified live against a form-mode
        session backed by an identity with QRS admin rights."""
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com/forms",
            QLIK_USER_DIRECTORY="COMPANY",
            QLIK_USER_ID="ivanov",
            QLIK_PASSWORD="s3cret",
            QLIK_TASK_TOOLS="true",
        )
        names = set(module.mcp._tool_manager._tools)
        assert TASK_TOOLS <= names
        assert ANALYSIS_TOOLS <= names

    @pytest.mark.parametrize("value", ["false", "0", "no", "FALSE"])
    def test_turning_it_off_drops_them_from_jwt_mode_too(self, reload_server, value):
        """QLIK_TASK_TOOLS=false is an unambiguous opt-out in every mode,
        including the (hypothetical) case where they were already on."""
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com/jwt",
            QLIK_JWT_TOKEN="header.payload.signature",
            QLIK_TASK_TOOLS=value,
        )
        assert set(module.mcp._tool_manager._tools) == ANALYSIS_TOOLS

    @pytest.mark.parametrize("value", ["false", "0", "no", "FALSE"])
    def test_turning_it_off_drops_them_from_certificate_mode(
            self, reload_server, value):
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com",
            QLIK_USER_DIRECTORY="COMPANY",
            QLIK_USER_ID="svc_mcp",
            QLIK_TASK_TOOLS=value,
        )
        assert set(module.mcp._tool_manager._tools) == ANALYSIS_TOOLS

    @pytest.mark.parametrize("value", ["true", "1", "yes", ""])
    def test_anything_else_keeps_them(self, reload_server, value):
        module = reload_server(
            QLIK_SERVER_URL="https://qlik.example.com",
            QLIK_USER_DIRECTORY="COMPANY",
            QLIK_USER_ID="svc_mcp",
            QLIK_TASK_TOOLS=value,
        )
        assert TASK_TOOLS <= set(module.mcp._tool_manager._tools)


class TestWriteToolsSwitch:
    """Authoring changes an app, so it is off in every mode until asked for."""

    JWT = dict(QLIK_SERVER_URL="https://qlik.example.com/jwt",
               QLIK_JWT_TOKEN="header.payload.signature")
    CERT = dict(QLIK_SERVER_URL="https://qlik.example.com",
                QLIK_USER_DIRECTORY="COMPANY", QLIK_USER_ID="svc_mcp")

    @pytest.mark.parametrize("env", ["JWT", "CERT"])
    def test_off_by_default(self, reload_server, env):
        names = set(reload_server(**getattr(self, env)).mcp._tool_manager._tools)
        assert not AUTHORING_TOOLS & names

    @pytest.mark.parametrize("value", ["false", "0", "no", ""])
    def test_anything_but_yes_leaves_them_out(self, reload_server, value):
        names = set(reload_server(QLIK_WRITE_TOOLS=value, **self.JWT)
                    .mcp._tool_manager._tools)
        assert not AUTHORING_TOOLS & names

    @pytest.mark.parametrize("value", ["true", "1", "YES"])
    def test_on_when_asked_for(self, reload_server, value):
        names = set(reload_server(QLIK_WRITE_TOOLS=value, **self.JWT)
                    .mcp._tool_manager._tools)
        assert AUTHORING_TOOLS <= names
        assert ANALYSIS_TOOLS <= names

    def test_does_not_drag_in_task_administration(self, reload_server):
        names = set(reload_server(QLIK_WRITE_TOOLS="true", QLIK_TASK_TOOLS="false",
                                  **self.JWT)
                    .mcp._tool_manager._tools)
        assert not TASK_TOOLS & names
