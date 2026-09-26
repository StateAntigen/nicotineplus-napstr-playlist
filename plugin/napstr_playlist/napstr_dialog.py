# SPDX-FileCopyrightText: 2026 StateAntigen
# SPDX-License-Identifier: 0BSD
"""Optional GTK review window for a playlist under construction.

Nicotine+ has no plugin API for adding a page, so this module builds a plain
top-level ``Gtk.Window`` of its own and drives the plugin through the same
``/napstr`` command handlers the log pane uses. That keeps one implementation
of every action.

The module is imported lazily by the plugin, so a headless Nicotine+ (or a
GTK 3 build, which is not supported on Windows any more) never touches it.
"""

from gi.repository import GLib
from gi.repository import Gtk
from gi.repository import Pango

import napstr_match
import napstr_state

REFRESH_INTERVAL_SECONDS = 2
MAX_ROWS = 1000

# One window is enough; reopening the command just presents the existing one
_window = None


def open_review_window(plugin):
    """Open (or present) the review window for ``plugin``."""

    global _window  # pylint: disable=global-statement

    if Gtk.get_major_version() < 4:
        raise RuntimeError("the review window needs GTK 4")

    if _window is not None and _window.plugin is plugin:
        _window.present()
        _window.refresh()
        return _window

    if _window is not None:
        _window.destroy()

    _window = ReviewWindow(plugin)
    _window.present()

    return _window


def _parent_window():
    """Best effort lookup of a Nicotine+ window to parent this one to."""

    try:
        window = getattr(Gtk.Application.get_default(), "window", None)

        if window is not None:
            return window

    except Exception:  # pylint: disable=broad-except
        pass

    try:
        toplevels = Gtk.Window.get_toplevels()

        for index in range(toplevels.get_n_items()):
            candidate = toplevels.get_item(index)

            if isinstance(candidate, Gtk.Window) and candidate.get_visible():
                return candidate

    except Exception:  # pylint: disable=broad-except
        pass

    return None


class ReviewWindow(Gtk.Window):
    """A list of entries with the actions that make sense for each one."""

    def __init__(self, plugin):

        super().__init__(
            title="NAPSTR playlist",
            default_width=1080,
            default_height=680,
            destroy_with_parent=True,
            child=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        )

        self.plugin = plugin
        self._rows = {}
        self._timeout_id = None

        parent = _parent_window()

        if parent is not None:
            self.set_transient_for(parent)

        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        for label, tooltip, argument in (
                ("Auto-pick all", "Search every entry and download confident matches", "auto all"),
                ("Search all", "Search every entry and let you choose", "search all"),
                ("Scan folders", "Match entries against the folders listed in the settings", "scan"),
                ("Hash all", "SHA-256 every local file", "hash all")):
            button = Gtk.Button(label=label, tooltip_text=tooltip)
            button.connect("clicked", self._on_action, argument)
            header.pack_start(button)

        publish_button = Gtk.Button(label="Publish…", tooltip_text="Publish the kind 30425 event")
        publish_button.connect("clicked", self._on_action, "publish")
        header.pack_end(publish_button)

        self.summary_label = Gtk.Label(xalign=0, margin_start=12, margin_end=12, margin_top=6)

        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)

        scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scrolled.set_child(self.list_box)

        content = self.get_child()
        content.append(scrolled)
        content.append(self.summary_label)

        self.connect("close-request", self._on_close)
        self.connect("show", self._start_refresh)

        self.refresh()

    # ------------------------------------------------------------------
    # refresh loop
    # ------------------------------------------------------------------

    def _start_refresh(self, *_args):

        if self._timeout_id is None:
            self._timeout_id = GLib.timeout_add_seconds(REFRESH_INTERVAL_SECONDS, self.refresh)

    def _on_close(self, *_args):

        global _window  # pylint: disable=global-statement

        self._stop_refresh()

        if _window is self:
            _window = None

        return False

    def _stop_refresh(self):

        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None

    def destroy(self):

        self._stop_refresh()
        super().destroy()

    # ------------------------------------------------------------------
    # rows
    # ------------------------------------------------------------------

    def refresh(self):
        """Rebuild or update the rows from the plugin state."""

        playlist = self.plugin.playlist

        if playlist is None:
            self._clear_rows()
            self.summary_label.set_text("No playlist loaded. Use /napstr load in the log pane.")
            return True

        entries = playlist.entries[:MAX_ROWS]
        self._drop_stale_rows({entry["position"] for entry in entries})

        for entry in entries:
            row = self._rows.get(entry["position"])

            if row is None:
                row = self._create_row(entry)
                self._rows[entry["position"]] = row

            self._update_row(row, entry)

        summary = playlist.summary()
        published = "published" if playlist.published else "not published"

        self.summary_label.set_text(
            f"{playlist.title} ({playlist.id}) - {summary['with_file_id']}/{summary['total']} hashed, "
            f"{published}, best-match threshold {self.plugin.settings.get('auto_pick_threshold', 0.8)}"
            + (f", showing the first {MAX_ROWS} entries" if len(playlist.entries) > MAX_ROWS else ""))

        return True

    def _clear_rows(self):

        for row in self._rows.values():
            self.list_box.remove(row["container"])

        self._rows.clear()

    def _drop_stale_rows(self, positions):

        for position in list(self._rows):
            if position in positions:
                continue

            self.list_box.remove(self._rows[position]["container"])
            del self._rows[position]

    def _create_row(self, entry):

        container = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)

        label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        score_label = Gtk.Label(xalign=1, width_chars=6)
        status_label = Gtk.Label(xalign=1, width_chars=22)

        options_button = Gtk.Button(label="Options…")
        options_button.connect("clicked", self._on_options, entry["position"])

        skip_button = Gtk.Button(label="Skip")
        skip_button.connect("clicked", self._on_skip, entry["position"])

        exclude_button = Gtk.Button(label="Exclude")
        exclude_button.connect("clicked", self._on_exclude, entry["position"])

        box.append(label)
        box.append(score_label)
        box.append(status_label)
        box.append(options_button)
        box.append(skip_button)
        box.append(exclude_button)
        container.set_child(box)
        self.list_box.append(container)

        return {
            "container": container,
            "label": label,
            "score": score_label,
            "status": status_label,
            "skip": skip_button,
            "exclude": exclude_button
        }

    def _update_row(self, row, entry):

        # The raw status decides the buttons; the shown status prefers the file
        # ID, and an excluded entry can still have one.
        raw_status = entry.get("status", napstr_state.STATUS_NEW)
        score = entry.get("score")
        status = raw_status

        if entry.get("file_id") and raw_status != napstr_state.STATUS_EXCLUDED:
            status = f"hashed {entry['file_id'][:10]}"

        row["label"].set_text(f"{entry['position']}. {entry['artist']} - {entry['title']}")
        row["label"].set_tooltip_text(
            entry.get("local_path") or entry.get("notes") or entry.get("query") or entry["title"])
        row["score"].set_text("" if score is None else f"{float(score):.2f}")
        row["status"].set_text(status)
        row["skip"].set_label("Unskip" if raw_status == napstr_state.STATUS_SKIPPED else "Skip")
        row["exclude"].set_label(
            "Include" if raw_status == napstr_state.STATUS_EXCLUDED else "Exclude")

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _run_command(self, argument):
        """Run a plugin command exactly as the log pane would."""

        try:
            self.plugin.napstr_command(argument)

        finally:
            self.refresh()

    def _on_action(self, _button, argument):
        self._run_command(argument)

    def _on_skip(self, button, position):

        action = "unskip" if button.get_label() == "Unskip" else "skip"
        self._run_command(f"{action} {position}")

    def _on_exclude(self, button, position):

        action = "include" if button.get_label() == "Include" else "exclude"
        self._run_command(f"{action} {position}")

    def _on_options(self, _button, position):

        playlist = self.plugin.playlist

        if playlist is None:
            return

        entry = playlist.entry(position)

        if entry is None:
            return

        window = CandidateWindow(self, self.plugin, entry)
        window.present()


class CandidateWindow(Gtk.Window):
    """Candidate list for one entry; clicking a row downloads it."""

    def __init__(self, parent, plugin, entry):

        super().__init__(
            title=f"Entry {entry['position']} - {entry['artist']} - {entry['title']}",
            default_width=900,
            default_height=520,
            transient_for=parent,
            child=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        )

        self.plugin = plugin
        self.entry = entry
        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)

        chrome = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                         margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)

        search_button = Gtk.Button(label="Search again")
        search_button.connect("clicked", self._on_search)

        auto_button = Gtk.Button(label="Auto-pick best")
        auto_button.connect("clicked", self._on_auto)

        chrome.append(search_button)
        chrome.append(auto_button)

        self.hint = Gtk.Label(
            xalign=0, margin_start=12, margin_end=12, margin_bottom=6, wrap=True,
            label="Click a candidate to download it. Scores come from artist, title, album "
                  "and duration, minus penalties for live/remix/cover versions.")

        scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scrolled.set_child(self.list_box)

        content = self.get_child()
        content.append(chrome)
        content.append(scrolled)
        content.append(self.hint)

        self.connect("close-request", self._on_close)
        self._populate()

    def _on_close(self, *_args):
        self.destroy()
        return False

    def _on_search(self, _button):

        self.plugin.napstr_command(f"search {self.entry['position']}")
        self.hint.set_text("Searching… reopen this window in a moment.")

    def _on_auto(self, _button):

        self.plugin.napstr_command(f"auto {self.entry['position']}")
        self.hint.set_text("Deciding automatically… reopen this window in a moment.")

    def _populate(self):

        child = self.list_box.get_first_child()

        while child is not None:
            next_child = child.get_next_sibling()
            self.list_box.remove(child)
            child = next_child

        candidates = self.entry.get("candidates") or []

        if not candidates:

            row = Gtk.ListBoxRow()
            row.set_child(Gtk.Label(
                xalign=0, margin_start=12, margin_top=6, margin_bottom=6,
                label="No candidates yet. Use 'Search again'."))
            self.list_box.append(row)
            return

        chosen = self.entry.get("chosen") or {}

        for rank, candidate in enumerate(candidates[:50], start=1):
            selected = (chosen.get("username") == candidate.get("username")
                        and chosen.get("path") == candidate.get("path"))
            prefix = "* " if selected else ""
            tooltip = "; ".join(candidate.get("reasons") or [])

            button = Gtk.Button(
                label=f"{prefix}{rank}. {float(candidate.get('score') or 0.0):.2f}  "
                      f"{napstr_match.describe_candidate(candidate)}",
                tooltip_text=tooltip,
                halign=Gtk.Align.FILL
            )
            button.connect("clicked", self._on_pick, rank)

            row = Gtk.ListBoxRow()
            row.set_child(button)
            self.list_box.append(row)

    def _on_pick(self, _button, rank):

        self.plugin.napstr_command(f"pick {self.entry['position']} {rank}")
        self.hint.set_text("Queued for download; hashing starts when the file arrives.")
