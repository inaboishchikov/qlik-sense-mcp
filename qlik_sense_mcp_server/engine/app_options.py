"""App-level settings: theme, menus, animation, sheet title style, thumbnail.

Qlik keeps these in a hidden app object of type `appprops` (theme, rtl,
the two menu switches, chart animation and the sheet title style) and in
the app's own properties (`qThumbnail`). Neither shows up in the app
layout, which is why `GetAppLayout` looks as if the app had no options.

The object exists only once somebody has opened the options dialog, so a
fresh app may have none; it is created on the first write.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from ..exceptions import QlikEngineError

logger = logging.getLogger(__name__)

APPPROPS_TYPE = "appprops"

# Option name -> (appprops key, inverted). A menu is "on" in the dialog
# when its `disable...` key is false, so the name the caller uses is the
# positive one and the stored key is its opposite.
_FLAGS: Dict[str, Tuple[str, bool]] = {
    "rtl": ("rtl", False),
    "hover_menu": ("disableCellNavMenu", True),
    "context_menu": ("disableCellContextMenu", True),
    "chart_animations": ("chartAnimations", False),
}
_TEXTS: Dict[str, str] = {
    "theme": "theme",
    "sheet_title_background": "sheetTitleBgColor",
    "sheet_title_gradient": "sheetTitleGradientColor",
    "sheet_title_color": "sheetTitleColor",
    "sheet_logo_position": "sheetLogoPosition",
}

OPTION_NAMES = tuple(_FLAGS) + tuple(_TEXTS) + ("sheet_logo_url", "thumbnail_url")


def check_content_url(url: str, what: str = "url") -> str:
    """A path into the server's content library, e.g. `/appcontent/<app>/x.png`."""
    if not isinstance(url, str):
        raise ValueError(f"{what} must be a string")
    if url and (not url.startswith("/") or url.startswith("//") or ".." in url
                or "://" in url):
        raise ValueError(
            f"{what} must be a server path such as /appcontent/<app id>/cover.png "
            f"or /content/Default/<file>, got {url!r}")
    return url


def build_app_option_changes(**options: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """Turn friendly option names into `appprops` keys.

    Returns the changes for the `appprops` object and the new app thumbnail
    URL if one was asked for (it lives in the app properties, not there).
    Options left as None are not touched; an empty string clears a text
    option.
    """
    unknown = sorted(set(options) - set(OPTION_NAMES))
    if unknown:
        raise ValueError(f"unknown option(s) {unknown}; known: {list(OPTION_NAMES)}")

    changes: Dict[str, Any] = {}
    thumbnail: Optional[str] = None
    for name, value in options.items():
        if value is None:
            continue
        if name in _FLAGS:
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false, got {value!r}")
            key, inverted = _FLAGS[name]
            changes[key] = (not value) if inverted else value
        elif name in _TEXTS:
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string, got {value!r}")
            if name == "theme" and not value.strip():
                raise ValueError("theme must name a theme; an empty one would not clear it")
            changes[_TEXTS[name]] = value
        elif name == "sheet_logo_url":
            changes["sheetLogoThumbnail"] = {"qStaticContentUrlDef": (
                {"qUrl": check_content_url(value, name)} if value else {})}
        elif name == "thumbnail_url":
            thumbnail = check_content_url(value, name)
    return changes, thumbnail


def read_app_options(props: Dict[str, Any]) -> Dict[str, Any]:
    """The `appprops` object as the friendly options a caller sets."""
    options: Dict[str, Any] = {}
    for name, (key, inverted) in _FLAGS.items():
        if key in props:
            options[name] = (not props[key]) if inverted else props[key]
    for name, key in _TEXTS.items():
        if key in props:
            options[name] = props[key]
    logo = ((props.get("sheetLogoThumbnail") or {}).get("qStaticContentUrlDef") or {}).get("qUrl")
    if logo:
        options["sheet_logo_url"] = logo
    return options


class EngineAppOptionsMixin:
    def _appprops(self, app_handle: int) -> Tuple[Optional[int], Dict[str, Any]]:
        """Handle and properties of the app's `appprops` object, if it exists."""
        infos = self.send_request("GetAllInfos", [], handle=app_handle).get("qInfos") or []
        for info in infos:
            if info.get("qType") == APPPROPS_TYPE:
                result = self.send_request("GetObject", {"qId": info["qId"]}, handle=app_handle)
                handle = (result.get("qReturn") or {}).get("qHandle")
                if handle:
                    return handle, self._properties(handle)
        return None, {}

    def get_app_options(self, app_handle: int) -> Dict[str, Any]:
        _, props = self._appprops(app_handle)
        app_props = self.send_request("GetAppProperties", [], handle=app_handle).get("qProp") or {}
        return {
            "options": read_app_options(props),
            "thumbnail_url": (app_props.get("qThumbnail") or {}).get("qUrl", ""),
            "raw": {k: v for k, v in props.items() if k not in ("qInfo", "qMetaDef")},
            "stored": bool(props),
        }

    def set_app_options(self, app_handle: int, changes: Dict[str, Any],
                        thumbnail_url: Optional[str]) -> Dict[str, Any]:
        """Write app options, then save. Returns what is stored afterwards."""
        if changes:
            handle, props = self._appprops(app_handle)
            if handle is None:
                created = self.send_request(
                    "CreateObject", [{"qInfo": {"qType": APPPROPS_TYPE}, **changes}],
                    handle=app_handle)
                if not (created.get("qReturn") or {}).get("qGenericId"):
                    raise QlikEngineError(f"Engine did not create the app options: {created}")
            else:
                props.update(changes)
                self._set_properties(handle, props)
        if thumbnail_url is not None:
            app_props = self.send_request(
                "GetAppProperties", [], handle=app_handle).get("qProp") or {}
            app_props["qThumbnail"] = {"qUrl": thumbnail_url} if thumbnail_url else {}
            self.send_request("SetAppProperties", [app_props], handle=app_handle)
        self.save_app(app_handle)
        return self.get_app_options(app_handle)
