"""Settings from a file outside the project (`QLIK_ENV_FILE`)."""

import os
import stat
import sys

import pytest

from qlik_sense_mcp_server import env_file


@pytest.fixture
def isolated(monkeypatch):
    """Only the named file is read: the project's own `.env` is not."""
    real = env_file.load_dotenv

    def load(path=None, **kw):
        return real(path, **kw) if path else False

    monkeypatch.setattr(env_file, "load_dotenv", load)
    # The loader writes to os.environ directly; setting then deleting registers
    # each name with monkeypatch so it is gone again after the test.
    for name in ("QLIK_ENV_FILE", "QLIK_PASSWORD", "QLIK_USER_ID"):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    return monkeypatch


def write(path, text, mode=0o600):
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


class TestLoading:
    def test_the_named_file_is_read(self, isolated, tmp_path):
        path = write(tmp_path / "qlik.env", "QLIK_PASSWORD=from-file\nQLIK_USER_ID=ann\n")
        isolated.setenv("QLIK_ENV_FILE", path)
        assert env_file.load_settings() == path
        assert os.environ["QLIK_PASSWORD"] == "from-file"
        assert os.environ["QLIK_USER_ID"] == "ann"

    def test_a_value_already_set_wins_over_the_file(self, isolated, tmp_path):
        path = write(tmp_path / "qlik.env", "QLIK_PASSWORD=from-file\n")
        isolated.setenv("QLIK_ENV_FILE", path)
        isolated.setenv("QLIK_PASSWORD", "from-process")
        env_file.load_settings()
        assert os.environ["QLIK_PASSWORD"] == "from-process"

    def test_a_tilde_is_the_users_home(self, isolated, tmp_path):
        isolated.setenv("HOME", str(tmp_path))
        isolated.setenv("USERPROFILE", str(tmp_path))
        (tmp_path / "cfg").mkdir()
        write(tmp_path / "cfg" / ".env", "QLIK_USER_ID=bob\n")
        isolated.setenv("QLIK_ENV_FILE", "~/cfg/.env")
        assert env_file.load_settings() == str(tmp_path / "cfg" / ".env")
        assert os.environ["QLIK_USER_ID"] == "bob"

    def test_without_the_variable_nothing_is_loaded(self, isolated):
        assert env_file.load_settings() is None


class TestMissingOrOdd:
    def test_a_missing_file_is_reported_and_skipped(self, isolated, tmp_path, caplog):
        isolated.setenv("QLIK_ENV_FILE", str(tmp_path / "nope.env"))
        with caplog.at_level("WARNING"):
            assert env_file.load_settings() is None
        assert "does not exist" in caplog.text

    def test_an_empty_file_is_reported(self, isolated, tmp_path, caplog):
        isolated.setenv("QLIK_ENV_FILE", write(tmp_path / "e.env", ""))
        with caplog.at_level("WARNING"):
            assert env_file.load_settings() is None
        assert "holds no settings" in caplog.text

    def test_the_password_never_reaches_the_log(self, isolated, tmp_path, caplog):
        path = write(tmp_path / "q.env", "QLIK_PASSWORD=hunter2\n", mode=0o644)
        isolated.setenv("QLIK_ENV_FILE", path)
        with caplog.at_level("DEBUG"):
            env_file.load_settings()
        assert "hunter2" not in caplog.text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
class TestPermissions:
    def test_a_file_others_can_read_is_used_but_reported(self, isolated, tmp_path, caplog):
        path = write(tmp_path / "q.env", "QLIK_PASSWORD=x\n", mode=0o644)
        isolated.setenv("QLIK_ENV_FILE", path)
        with caplog.at_level("WARNING"):
            assert env_file.load_settings() == path
        assert "readable by other users" in caplog.text
        assert os.environ["QLIK_PASSWORD"] == "x"

    def test_an_owner_only_file_is_not_reported(self, isolated, tmp_path, caplog):
        path = write(tmp_path / "q.env", "QLIK_PASSWORD=x\n", mode=0o600)
        isolated.setenv("QLIK_ENV_FILE", path)
        with caplog.at_level("WARNING"):
            env_file.load_settings()
        assert "readable by other users" not in caplog.text
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
