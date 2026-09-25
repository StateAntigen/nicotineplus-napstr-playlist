# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Static sanity checks for the plugin, using only the standard library.

Nicotine+ does not ship a linter, and the plugin runs inside the frozen
interpreter, so this script does the checks that matter before deploying:

1. every module compiles;
2. every ``self.<name>`` reference in the plugin class is defined in the
   class, assigned in ``__init__``, or part of the ``BasePlugin`` API;
3. event callbacks registered through ``events.connect`` are bound methods of
   the plugin class, which is what Nicotine+ relies on to remove them when the
   plugin is disabled (it compares ``function.__module__`` to the plugin name);
4. ``PLUGININFO`` parses the way ``PluginHandler.get_plugin_info`` parses it.

Run from the repository root:

    python tools/selfcheck.py
"""

import ast
import os
import py_compile
import sys
import tempfile

PLUGIN_NAME = "napstr_playlist"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugin", PLUGIN_NAME)
MODULES = (
    "__init__.py",
    "napstr_crypto.py",
    "napstr_csv.py",
    "napstr_event.py",
    "napstr_match.py",
    "napstr_relay.py",
    "napstr_state.py",
    "napstr_dialog.py"
)

# Attributes provided by pynicotine.pluginsystem.BasePlugin
BASE_PLUGIN_API = {
    "log", "output", "send_public", "send_private", "echo_public", "echo_private",
    "echo_message", "send_message",
    "init", "disable", "loaded_notification", "unloaded_notification", "shutdown_notification",
    "public_room_message_notification", "search_request_notification", "distrib_search_notification",
    "incoming_private_chat_event", "incoming_private_chat_notification",
    "incoming_public_chat_event", "incoming_public_chat_notification",
    "outgoing_private_chat_event", "outgoing_private_chat_notification",
    "outgoing_public_chat_event", "outgoing_public_chat_notification",
    "outgoing_global_search_event", "outgoing_room_search_event", "outgoing_buddy_search_event",
    "outgoing_user_search_event", "outgoing_wishlist_search_event",
    "user_resolve_notification", "server_connect_notification", "server_disconnect_notification",
    "join_chatroom_notification", "leave_chatroom_notification",
    "user_join_chatroom_notification", "user_leave_chatroom_notification",
    "user_stats_notification", "user_status_notification",
    "upload_queued_notification", "upload_started_notification", "upload_finished_notification",
    "download_started_notification", "download_finished_notification",
    "parent", "config", "core", "path", "human_name", "internal_name",
    "settings", "metasettings", "commands",
    "__publiccommands__", "__privatecommands__"
}


def check_compiles():
    """Compile every module; return a list of problems."""

    problems = []

    for name in MODULES:
        path = os.path.join(PLUGIN_DIR, name)

        if not os.path.isfile(path):
            problems.append(f"missing module: {name}")
            continue

        try:
            with tempfile.TemporaryDirectory() as temporary_folder:
                py_compile.compile(
                    path, doraise=True,
                    cfile=os.path.join(temporary_folder, f"{name}.pyc"))

        except py_compile.PyCompileError as error:
            problems.append(f"{name}: {error.msg.strip()}")

    return problems


def _plugin_class(tree):

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Plugin":
            return node

    return None


def check_self_attributes():
    """Report ``self.<name>`` uses that are not defined anywhere obvious."""

    path = os.path.join(PLUGIN_DIR, "__init__.py")

    with open(path, encoding="utf-8") as file_handle:
        tree = ast.parse(file_handle.read(), filename=path)

    plugin = _plugin_class(tree)

    if plugin is None:
        return ["__init__.py: no Plugin class found"]

    defined = set(BASE_PLUGIN_API)

    for node in ast.walk(plugin):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)

                elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                        and target.value.id == "self":
                    defined.add(target.attr)

    used = {}

    for node in ast.walk(plugin):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self":
            used.setdefault(node.attr, node.lineno)

    problems = []

    for name, lineno in sorted(used.items(), key=lambda item: item[1]):
        if name in defined or name.startswith("__"):
            continue

        problems.append(f"__init__.py:{lineno}: self.{name} is never defined")

    return problems


def check_event_callbacks_are_methods():
    """Event callbacks must be plugin methods so disable() can unregister them."""

    path = os.path.join(PLUGIN_DIR, "__init__.py")

    with open(path, encoding="utf-8") as file_handle:
        tree = ast.parse(file_handle.read(), filename=path)

    problems = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func

        if not (isinstance(func, ast.Attribute) and func.attr == "connect"):
            continue

        if len(node.args) < 2:
            continue

        callback = node.args[1]

        if isinstance(callback, ast.Attribute) and isinstance(callback.value, ast.Name) \
                and callback.value.id == "self":
            continue

        problems.append(
            f"__init__.py:{node.lineno}: events.connect callback is not a bound method "
            "(it will not be removed when the plugin is disabled)")

    return problems


def check_module_imports():
    """Helper modules must not import pynicotine, so they stay testable."""

    problems = []

    for name in MODULES:
        if name in {"__init__.py", "napstr_dialog.py"}:
            continue

        path = os.path.join(PLUGIN_DIR, name)

        if not os.path.isfile(path):
            continue

        with open(path, encoding="utf-8") as file_handle:
            tree = ast.parse(file_handle.read(), filename=path)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "pynicotine":
                        problems.append(f"{name}:{node.lineno}: imports {alias.name}")

            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pynicotine":
                problems.append(f"{name}:{node.lineno}: imports from {node.module}")

    return problems


def check_plugininfo():
    """Parse PLUGININFO the way ``PluginHandler.get_plugin_info`` does.

    Values are Python literals, optionally wrapped in ``_(...)`` for
    translation. Lines that fail to parse are silently skipped by Nicotine+,
    which is exactly why a typo here is worth catching in advance.
    """

    path = os.path.join(PLUGIN_DIR, "PLUGININFO")

    if not os.path.isfile(path):
        return ["missing PLUGININFO"]

    problems = []
    info = {}

    with open(path, encoding="utf-8") as file_handle:
        for lineno, line in enumerate(file_handle, start=1):
            key, _separator, value = line.partition("=")
            key = key.strip()
            value = value.strip()

            if not key:
                continue

            if value.startswith("_(") and value.endswith(")"):
                value = value[2:-1]

            try:
                info[key] = ast.literal_eval(value)

            except (ValueError, SyntaxError):
                problems.append(f"PLUGININFO:{lineno}: cannot parse value for {key!r}: {value!r}")

    if not info.get("Name"):
        problems.append("PLUGININFO: a non-empty Name is required")

    return problems


def main():

    checks = (
        ("compilation", check_compiles()),
        ("self attributes", check_self_attributes()),
        ("event callbacks", check_event_callbacks_are_methods()),
        ("helper module purity", check_module_imports()),
        ("PLUGININFO", check_plugininfo())
    )
    failed = False

    for name, problems in checks:
        if problems:
            failed = True
            print(f"FAIL  {name}")

            for problem in problems:
                print(f"      {problem}")

        else:
            print(f"ok    {name}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
