"""Settings from a file kept outside the project.

A password in `.mcp.json` sits in a file that is easy to share, commit or
paste into a chat. `QLIK_ENV_FILE` names another file instead, normally in
the user's own config directory, and the client config keeps only that path:

    "env": {"QLIK_ENV_FILE": "~/.config/skills307/.env"}

The file holds `NAME=value` lines like any `.env`. A value already set in the
process environment wins over the file, and the file wins over a `.env` in the
working directory, so a one-off override on the command line still works.
A file other users can read is used but reported: it holds a password.
"""

import logging
import os
import stat
import sys
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ENV_FILE_VARIABLE = "QLIK_ENV_FILE"


def load_settings() -> Optional[str]:
    """Load `QLIK_ENV_FILE` if set, then a `.env` in the working directory.

    Returns the path of the external file when it was loaded, else None. A
    missing or unreadable file is logged and skipped: the server then starts
    with what the environment has, and says what it lacks when it needs it.
    """
    loaded = None
    configured = os.getenv(ENV_FILE_VARIABLE, "").strip()
    if configured:
        path = os.path.abspath(os.path.expanduser(configured))
        if not os.path.isfile(path):
            logger.warning("%s=%s does not exist; settings from it are not loaded",
                           ENV_FILE_VARIABLE, configured)
        else:
            _warn_if_shared(path)
            if load_dotenv(path, override=False):
                loaded = path
            else:
                logger.warning("%s=%s holds no settings", ENV_FILE_VARIABLE, configured)
    load_dotenv()
    return loaded


def _warn_if_shared(path: str) -> None:
    if sys.platform == "win32":
        return
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "%s is readable by other users (mode %o); it holds a password. "
            "Run: chmod 600 %s", ENV_FILE_VARIABLE, mode, path)
