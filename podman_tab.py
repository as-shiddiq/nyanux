"""
Podman tab — Nyanux

  [Proyek]  │ judul · aksi proyek
  (sidebar) │ cari · filter status
            │ kartu container
            │ ─────────────── (bisa di-resize)
            │ log

Di jendela sempit sidebar proyek jadi panel geser dan tombol aksi jadi ikon saja.

Fitur folder proyek:
  • Tambah folder (tombol atau drag & drop). Jika folder tidak punya compose file,
    sub-foldernya dipindai otomatis (2 level) dan semua proyek compose ditambahkan.
  • Auto-start: proyek yang ditandai akan `compose up -d` otomatis saat Nyanux dibuka.
  • Hapus dari daftar (opsional sekalian `compose down`). File di disk tidak pernah dihapus.
"""

import os
import json
import shutil
import subprocess
import threading
import webbrowser

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gdk, Gio, GObject, Pango

import podman_projects as pp
import themes

ALL_KEY = "__all__"
NONE_KEY = "__none__"
NON_WEB_PORTS = {3306, 5432, 6379, 27017, 1433, 9042, 22, 21, 11211, 5672}
DB_KEYWORDS = ["db", "mysql", "mariadb", "postgres", "redis", "mongo", "memcache", "rabbit"]
STATE_LABELS = {"running": "Berjalan", "paused": "Dijeda", "stopped": "Berhenti"}
PAW_COLORS = {"running": "#33b47c", "paused": "#e5a50a", "stopped": "#8f8d96"}

PODMAN_CSS = """
.bz-ccard { padding: 12px 10px 6px 14px; }
.bz-ccard.selected { box-shadow: inset 0 0 0 2px @accent_color; }
.bz-dot { font-size: 0.7em; }
.bz-dot.running { color: @success_color; }
.bz-dot.paused { color: @warning_color; }
.bz-dot.stopped { color: alpha(currentColor, 0.3); }
.bz-port { font-family: monospace; font-size: 0.85em; padding: 2px 6px; min-height: 0; }
.bz-log-view, .bz-log-view text {
    font-family: monospace;
    font-size: 0.85em;
    background: #0d1117;
    color: #c9d1d9;
}
.bz-log-view { padding: 10px; }
.bz-drop-active { background: alpha(@accent_color, 0.06); }
"""

_css_loaded = False


def _load_css():
    global _css_loaded
    if _css_loaded:
        return
    prov = Gtk.CssProvider()
    prov.load_from_string(PODMAN_CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
    _css_loaded = True


def _state_class(state):
    if state == "running":
        return "running"
    if state == "paused":
        return "paused"
    return "stopped"


def container_name(c):
    names = c.get("Names") or []
    return names[0].lstrip("/") if names else (c.get("Id", "")[:12] or "unknown")


def compute_ports(container):
    """Return (list label port, list url web) — heuristik sama seperti versi lama."""
    ports = container.get("Ports") or []
    exposed = container.get("ExposedPorts") or {}
    c_name = container_name(container).lower()
    image_name = (container.get("Image") or "").lower()

    def cport(p):
        return p.get("container_port") or p.get("ContainerPort")

    if container.get("Pod") and exposed:
        matched = [p for p in ports if str(cport(p)) in exposed]
        if "phpmyadmin" in c_name or "phpmyadmin" in image_name:
            matched = [p for p in matched if cport(p) == 80]
        elif any(k in c_name or k in image_name for k in DB_KEYWORDS):
            matched = [p for p in matched if cport(p) in NON_WEB_PORTS]
        if matched:
            ports = matched

    is_db = any(k in c_name or k in image_name for k in DB_KEYWORDS)
    labels, urls = [], []
    for p in ports:
        c_port = cport(p)
        h_port = p.get("host_port") or p.get("HostPort")
        proto = p.get("protocol") or p.get("Protocol") or "tcp"
        rng = p.get("range") or 1
        if not h_port:
            continue
        # Yang dibuka pengguna adalah port di komputer (host); port container cukup di tooltip
        lbl = f":{h_port}" if rng == 1 else f":{h_port}-{h_port + rng - 1}"
        tip = (f"localhost:{h_port} → port {c_port} di dalam container" if rng == 1 else
               f"localhost:{h_port}-{h_port + rng - 1} → port {c_port}-{c_port + rng - 1} di dalam container")
        web = None
        if proto == "tcp" and not is_db and h_port not in NON_WEB_PORTS and c_port not in NON_WEB_PORTS:
            web = f"http://localhost:{h_port}"
            urls.append(web)
        labels.append((lbl, web, tip))
    return labels, urls



# ═════════════════════════════════════════════════════════════════════════════
#  Widgets
# ═════════════════════════════════════════════════════════════════════════════

def icon_label_button(icon, label, tooltip=None, icon_only=False):
    """Tombol ikon + teks; di layar sempit cukup ikon (teks pindah ke tooltip)."""
    b = Gtk.Button(valign=Gtk.Align.CENTER)
    if icon_only:
        b.set_icon_name(icon)
        b.set_tooltip_text(tooltip or label)
    else:
        b.set_child(Adw.ButtonContent(icon_name=icon, label=label))
        if tooltip:
            b.set_tooltip_text(tooltip)
    return b


class ProjectRow(Gtk.ListBoxRow):
    def __init__(self, key, title, subtitle, running, total, icon, autostart=False, busy=False):
        super().__init__()
        self.key = key
        box = Gtk.Box(spacing=10, margin_top=4, margin_bottom=4)
        box.append(Gtk.Image(icon_name=icon, valign=Gtk.Align.CENTER))

        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        v.append(Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END))
        if subtitle:
            s = Gtk.Label(label=subtitle, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
            s.add_css_class("dim-label")
            s.add_css_class("caption")
            v.append(s)
        box.append(v)

        if busy:
            box.append(Adw.Spinner())
        if autostart:
            a = Gtk.Image(icon_name="media-playlist-repeat-symbolic", tooltip_text="Auto-start saat Nyanux dibuka")
            a.add_css_class("dim-label")
            box.append(a)
        count = Gtk.Label(label=f"{running}/{total}", valign=Gtk.Align.CENTER,
                          tooltip_text=f"{running} berjalan dari {total} container")
        count.add_css_class("numeric")
        count.add_css_class("caption")
        if not running:
            count.add_css_class("dim-label")
        box.append(count)
        self.set_child(box)


class ContainerCard(Gtk.Box):
    def __init__(self, container, tab, selected=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.tab = tab
        self.c = container
        self.name = container_name(container)
        self.state = container.get("State", "unknown")
        sc = _state_class(self.state)

        self.add_css_class("card")
        self.add_css_class("bz-ccard")
        if selected:
            self.add_css_class("selected")
        self.set_size_request(240, -1)

        # nama + status
        hdr = Gtk.Box(spacing=8)
        dot = Gtk.Image.new_from_paintable(themes.paw_icon(PAW_COLORS.get(sc, PAW_COLORS["stopped"]), 14))
        dot.set_pixel_size(14)
        dot.set_valign(Gtk.Align.CENTER)
        dot.set_tooltip_text(STATE_LABELS.get(sc, ""))
        hdr.append(dot)
        name = Gtk.Label(label=self.name, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                         tooltip_text=self.name)
        name.add_css_class("heading")
        hdr.append(name)
        self.append(hdr)

        meta = Gtk.Label(label=pp.short_image(container.get("Image", "")), xalign=0,
                         ellipsize=Pango.EllipsizeMode.MIDDLE, tooltip_text=container.get("Image", ""))
        meta.add_css_class("dim-label")
        meta.add_css_class("caption")
        self.append(meta)
        status = container.get("Status") or STATE_LABELS.get(sc, "")
        st = Gtk.Label(label=status, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        st.add_css_class("dim-label")
        st.add_css_class("caption")
        self.append(st)

        # port: yang bisa dibuka di browser jadi tautan
        port_labels, self.web_urls = compute_ports(container)
        running = self.state == "running"
        if port_labels:
            pf = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                             row_spacing=2, column_spacing=2, margin_top=2)
            for lbl, web, tip in port_labels[:6]:
                if web and running:
                    b = Gtk.Button(tooltip_text=f"Buka {web}\n{tip}")
                    b.set_child(Adw.ButtonContent(icon_name="external-link-symbolic", label=lbl))
                    b.add_css_class("flat")
                    b.add_css_class("bz-port")
                    b.add_css_class("accent")
                    b.connect("clicked", lambda _b, u=web: webbrowser.open(u))
                    pf.append(b)
                else:
                    l = Gtk.Label(label=lbl, halign=Gtk.Align.START, tooltip_text=tip)
                    l.add_css_class("bz-port")
                    l.add_css_class("dim-label")
                    pf.append(l)
            self.append(pf)

        # aksi
        act = Gtk.Box(spacing=2)
        self._icon_btn(act, "media-playback-stop-symbolic" if running else "media-playback-start-symbolic",
                       "Stop" if running else "Start", self._on_toggle)
        self._icon_btn(act, "view-refresh-symbolic", "Restart", self._on_restart)
        act.append(Gtk.Box(hexpand=True))
        menu = Gio.Menu()
        menu.append("Lihat log", "card.logs")
        group = Gio.SimpleActionGroup()
        actions = [("logs", lambda *_: tab.select_container_for_logs(self.name))]
        if pp.port_mappings(container):
            menu.append("Ganti port…", "card.port")
            actions.append(("port", lambda *_: tab.open_port_dialog(self.c)))
        for name_, cb in actions:
            a = Gio.SimpleAction.new(name_, None)
            a.connect("activate", cb)
            group.add_action(a)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Lainnya")
        more.add_css_class("flat")
        more.add_css_class("circular")
        more.insert_action_group("card", group)
        act.append(more)
        self.append(act)

        g = Gtk.GestureClick()
        g.connect("released", lambda *a: tab.select_container_for_logs(self.name))
        self.add_controller(g)

    def _icon_btn(self, box, icon, tip, cb):
        b = Gtk.Button(icon_name=icon, tooltip_text=tip)
        b.add_css_class("flat")
        b.add_css_class("circular")
        b.connect("clicked", cb)
        box.append(b)
        return b

    def _on_toggle(self, _b):
        action = "stop" if self.state == "running" else "start"
        self.tab.run_podman_action(["podman", action, self.name], f"{action} {self.name}")

    def _on_restart(self, _b):
        self.tab.run_podman_action(["podman", "restart", self.name], f"restart {self.name}")


# ═════════════════════════════════════════════════════════════════════════════
#  Tab
# ═════════════════════════════════════════════════════════════════════════════

class PodmanTab(Gtk.Box):
    # Diset oleh breakpoint jendela: tombol aksi jadi ikon saja
    narrow = GObject.Property(type=bool, default=False)

    def __init__(self, main_win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        _load_css()
        self.main_win = main_win
        self.projects = pp.load_projects()
        self.containers = []
        self.podman_ok = True
        self.selected_key = ALL_KEY
        self.selected_log_container = None
        self.log_mode = "container"   # "container" | "task"
        self.busy = set()
        self._proj_sig = None
        self._cards_sig = None
        self._poll_timer_id = None
        self._suppress_select = False

        self._build_ui()
        self.connect("notify::narrow", lambda *_: self._render_header())
        self.refresh_podman_list()
        self._start_auto_refresh()
        GLib.timeout_add_seconds(2, self._run_autostart)

    # ─── UI ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.split_view = Adw.OverlaySplitView(vexpand=True, min_sidebar_width=220, max_sidebar_width=280)
        self.split_view.connect("notify::collapsed",
                                lambda s, _p: self.sidebar_btn.set_visible(s.get_collapsed()))
        self.append(self.split_view)

        # ─── Sidebar: proyek ───
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.add_css_class("bz-sidebar")
        sl = Gtk.Box(spacing=6, margin_start=18, margin_end=10, margin_top=10, margin_bottom=4)
        lab = Gtk.Label(label="Proyek", xalign=0, hexpand=True)
        lab.add_css_class("bz-sidebar-title")
        sl.append(lab)
        mini_add = Gtk.Button(icon_name="list-add-symbolic",
                              tooltip_text="Tambah folder proyek (berisi compose.yaml). "
                                           "Folder induk dipindai otomatis; bisa juga seret folder ke jendela.")
        mini_add.add_css_class("flat")
        mini_add.connect("clicked", self._on_add_folder_clicked)
        sl.append(mini_add)
        side.append(sl)

        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.proj_list = Gtk.ListBox()
        self.proj_list.add_css_class("navigation-sidebar")
        self.proj_list.connect("row-selected", self._on_project_selected)
        sc.set_child(self.proj_list)
        side.append(themes.sidebar_decor(sc))
        self.split_view.set_sidebar(side)

        # ─── Konten ───
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_start=12, margin_end=12, margin_top=12, margin_bottom=12)

        head = Gtk.Box(spacing=10)
        self.sidebar_btn = Gtk.Button(icon_name="sidebar-show-symbolic", tooltip_text="Daftar proyek",
                                      valign=Gtk.Align.CENTER, visible=False)
        self.sidebar_btn.add_css_class("flat")
        self.sidebar_btn.connect("clicked", lambda b: self.split_view.set_show_sidebar(True))
        head.append(self.sidebar_btn)
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        self.title_lbl = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title_lbl.add_css_class("title-3")
        tbox.append(self.title_lbl)
        self.subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.subtitle.add_css_class("dim-label")
        self.subtitle.add_css_class("caption")
        tbox.append(self.subtitle)
        head.append(tbox)
        self.head_actions = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        head.append(self.head_actions)
        content.append(head)

        filt = Gtk.Box(spacing=6)
        self.search = Gtk.SearchEntry(placeholder_text="Cari container atau image", hexpand=True)
        self.search.connect("search-changed", lambda *_: self._render_cards(force=True))
        filt.append(self.search)
        self.filter_dd = Gtk.DropDown.new_from_strings(["Semua", "Berjalan", "Berhenti"])
        self.filter_dd.set_tooltip_text("Filter status")
        self.filter_dd.connect("notify::selected", lambda *_: self._render_cards(force=True))
        filt.append(self.filter_dd)
        content.append(filt)

        # kartu │ log
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True, position=420,
                          shrink_end_child=False, resize_end_child=False, wide_handle=True)
        content.append(paned)

        self.cards_stack = Gtk.Stack(vexpand=True)
        cs = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.flowbox = Gtk.FlowBox(valign=Gtk.Align.START, homogeneous=True, max_children_per_line=6,
                                   min_children_per_line=1, row_spacing=10, column_spacing=10,
                                   selection_mode=Gtk.SelectionMode.NONE, margin_bottom=8)
        cs.set_child(self.flowbox)
        self.cards_stack.add_named(cs, "cards")
        self.empty_page = Adw.StatusPage()
        self.empty_page.add_css_class("compact")
        themes.on_change(lambda: self._render_cards(force=True))
        self.cards_stack.add_named(self.empty_page, "empty")
        paned.set_start_child(self.cards_stack)

        log = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8)
        lh = Gtk.Box(spacing=6)
        self.log_title = Gtk.Label(label="Log — pilih container", xalign=0, hexpand=True,
                                   ellipsize=Pango.EllipsizeMode.END)
        self.log_title.add_css_class("heading")
        lh.append(self.log_title)
        fl = Gtk.Label(label="Ikuti")
        fl.add_css_class("dim-label")
        lh.append(fl)
        self.follow_sw = Gtk.Switch(active=True, valign=Gtk.Align.CENTER,
                                    tooltip_text="Perbarui log otomatis & scroll ke bawah")
        lh.append(self.follow_sw)
        rb = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Muat ulang log")
        rb.add_css_class("flat")
        rb.connect("clicked", lambda b: self.refresh_podman_logs(force=True))
        lh.append(rb)
        cb = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Bersihkan")
        cb.add_css_class("flat")
        cb.connect("clicked", self.clear_log_view)
        lh.append(cb)
        log.append(lh)

        frame = Gtk.Frame(vexpand=True)
        ls = Gtk.ScrolledWindow(vexpand=True, min_content_height=100)
        self.log_text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.log_text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_text.add_css_class("bz-log-view")
        ls.set_child(self.log_text)
        frame.set_child(ls)
        log.append(frame)
        paned.set_end_child(log)

        self.split_view.set_content(content)

        # Drag & drop folder
        try:
            dt = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            dt.connect("drop", self._on_drop)
            dt.connect("enter", lambda *a: (self.add_css_class("bz-drop-active"), Gdk.DragAction.COPY)[1])
            dt.connect("leave", lambda *a: self.remove_css_class("bz-drop-active"))
            self.add_controller(dt)
        except Exception as e:
            print(f"[podman] drag&drop tidak tersedia: {e}")

        self._render_projects(force=True)
        self._render_header()

    # ─── Helpers ─────────────────────────────────────────────────────────────
    def _toast(self, msg):
        if hasattr(self.main_win, "_toast"):
            self.main_win._toast(msg)

    def _project(self, key):
        return next((p for p in self.projects if p["path"] == key), None)

    def _containers_for(self, key):
        if key == ALL_KEY:
            return list(self.containers)
        if key == NONE_KEY:
            return [c for c in self.containers if not pp.container_project_key(c, self.projects)]
        return [c for c in self.containers if pp.container_project_key(c, self.projects) == key]

    # ─── Polling ─────────────────────────────────────────────────────────────
    def _start_auto_refresh(self):
        if self._poll_timer_id is not None:
            GLib.source_remove(self._poll_timer_id)
        self._poll_timer_id = GLib.timeout_add_seconds(4, self._auto_tick)

    def _auto_tick(self):
        self.refresh_podman_list()
        return True

    def refresh_podman_list(self):
        def _worker():
            ok, containers = True, []
            try:
                res = subprocess.run(["podman", "ps", "-a", "--format", "json"],
                                     capture_output=True, text=True, timeout=20)
                if res.returncode == 0:
                    containers = json.loads(res.stdout or "[]") or []
                else:
                    ok = False
            except FileNotFoundError:
                ok = False
            except Exception as e:
                print(f"[podman] ps error: {e}")
            GLib.idle_add(self._apply_containers, ok, containers)
        threading.Thread(target=_worker, daemon=True).start()

    def _apply_containers(self, ok, containers):
        self.podman_ok = ok
        self.containers = [c for c in containers if not c.get("IsInfra")]
        self.containers.sort(key=lambda c: (c.get("State") != "running", container_name(c)))
        self._render_projects()
        self._render_header()
        self._render_cards()
        if self.follow_sw.get_active() and self.log_mode == "container":
            self.refresh_podman_logs()
        return False

    # ─── Project list ────────────────────────────────────────────────────────
    def _render_projects(self, force=False):
        stats = {}
        for c in self.containers:
            k = pp.container_project_key(c, self.projects) or NONE_KEY
            r, t = stats.get(k, (0, 0))
            stats[k] = (r + (c.get("State") == "running"), t + 1)
        sig = (tuple((p["path"], p.get("autostart")) for p in self.projects),
               tuple(sorted(stats.items())), tuple(sorted(self.busy)), len(self.containers))
        if sig == self._proj_sig and not force:
            return
        self._proj_sig = sig

        self._suppress_select = True
        while (ch := self.proj_list.get_first_child()):
            self.proj_list.remove(ch)

        run_all = sum(1 for c in self.containers if c.get("State") == "running")
        rows = [ProjectRow(ALL_KEY, "Semua container", "", run_all, len(self.containers),
                           "view-grid-symbolic")]
        for p in self.projects:
            r, t = stats.get(p["path"], (0, 0))
            exists = os.path.isdir(p["path"])
            rows.append(ProjectRow(
                p["path"], p.get("name") or os.path.basename(p["path"]),
                pp.pretty_path(p["path"]) if exists else "Folder tidak ditemukan",
                r, t, "folder-symbolic", p.get("autostart"), p["path"] in self.busy))
        r, t = stats.get(NONE_KEY, (0, 0))
        if t:
            rows.append(ProjectRow(NONE_KEY, "Tanpa proyek", "Di luar folder terdaftar",
                                   r, t, "view-list-symbolic"))
        for row in rows:
            self.proj_list.append(row)
            if row.key == self.selected_key:
                self.proj_list.select_row(row)
        if not self.proj_list.get_selected_row():
            self.selected_key = ALL_KEY
            self.proj_list.select_row(rows[0])
        self._suppress_select = False

    def _on_project_selected(self, _lb, row):
        if self._suppress_select or row is None:
            return
        if self.split_view.get_collapsed():
            self.split_view.set_show_sidebar(False)
        if row.key != self.selected_key:
            self.selected_key = row.key
            self._render_header()
            self._render_cards(force=True)

    # ─── Header konten ───────────────────────────────────────────────────────
    def _render_header(self):
        while (ch := self.head_actions.get_first_child()):
            self.head_actions.remove(ch)
        key = self.selected_key
        conts = self._containers_for(key)
        running = sum(1 for c in conts if c.get("State") == "running")
        counts = f"{running} berjalan · {len(conts)} total"
        compact = self.narrow

        if key in (ALL_KEY, NONE_KEY):
            self.title_lbl.set_label("Semua container" if key == ALL_KEY else "Tanpa proyek")
            self.subtitle.set_label(counts)
            if conts:
                box = Gtk.Box(css_classes=["linked"])
                b1 = icon_label_button("media-playback-start-symbolic", "Start semua", icon_only=compact)
                b1.connect("clicked", lambda b: self._bulk(conts, "start"))
                box.append(b1)
                b2 = icon_label_button("media-playback-stop-symbolic", "Stop semua", icon_only=compact)
                b2.connect("clicked", lambda b: self._bulk(conts, "stop"))
                box.append(b2)
                self.head_actions.append(box)
            add = icon_label_button("folder-new-symbolic", "Tambah folder",
                                    "Tambah folder proyek berisi compose.yaml", icon_only=True)
            add.add_css_class("flat")
            add.connect("clicked", self._on_add_folder_clicked)
            self.head_actions.append(add)
            return

        p = self._project(key)
        if not p:
            return
        self.title_lbl.set_label(p.get("name") or os.path.basename(p["path"]))
        self.subtitle.set_label(f"{pp.pretty_path(p['path'])} · {counts}")
        self.subtitle.set_tooltip_text(f"{p['path']}\n{p.get('compose_file') or 'compose ?'} · "
                                       f"{' '.join(pp.compose_base_cmd())}")

        busy = key in self.busy
        if busy:
            self.head_actions.append(Adw.Spinner())
        box = Gtk.Box(css_classes=["linked"])
        for icon, label, action, tip in (
                ("media-playback-start-symbolic", "Up", "up", "compose up -d"),
                ("media-playback-stop-symbolic", "Down", "down", "compose down"),
                ("view-refresh-symbolic", "Restart", "restart", "compose restart")):
            b = icon_label_button(icon, label, tip, icon_only=compact)
            b.set_sensitive(not busy)
            b.connect("clicked", lambda _b, a=action: self.run_compose(p, a))
            box.append(b)
        self.head_actions.append(box)

        menu = Gio.Menu()
        menu.append("Jalankan saat Nyanux dibuka", "proj.autostart")
        menu.append("Buka folder", "proj.open")
        menu.append("Hapus dari daftar…", "proj.remove")
        group = Gio.SimpleActionGroup()
        auto = Gio.SimpleAction.new_stateful("autostart", None, GLib.Variant.new_boolean(bool(p.get("autostart"))))
        auto.connect("change-state", lambda a, v, path=p["path"]: (a.set_state(v),
                                                                  self._set_autostart(path, v.get_boolean())))
        group.add_action(auto)
        for name, cb in (("open", lambda *_: self._open_folder(p["path"])),
                         ("remove", lambda *_: self._confirm_remove(p))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            group.add_action(a)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Lainnya",
                              valign=Gtk.Align.CENTER)
        more.add_css_class("flat")
        more.insert_action_group("proj", group)
        self.head_actions.append(more)

    # ─── Cards ───────────────────────────────────────────────────────────────
    def _filtered_containers(self):
        conts = self._containers_for(self.selected_key)
        f = self.filter_dd.get_selected()
        if f == 1:
            conts = [c for c in conts if c.get("State") == "running"]
        elif f == 2:
            conts = [c for c in conts if c.get("State") != "running"]
        q = self.search.get_text().strip().lower()
        if q:
            conts = [c for c in conts if q in container_name(c).lower() or q in (c.get("Image") or "").lower()]
        return conts

    def _render_cards(self, force=False):
        conts = self._filtered_containers()
        sig = (self.selected_key, self.selected_log_container, self.podman_ok,
               tuple((c.get("Id"), c.get("State"), c.get("Status"), json.dumps(c.get("Ports"), sort_keys=True))
                     for c in conts))
        if sig == self._cards_sig and not force:
            return
        self._cards_sig = sig

        while (ch := self.flowbox.get_first_child()):
            self.flowbox.remove(ch)

        if not self.podman_ok:
            self.empty_page.set_paintable(themes.illustration("curious", 160))
            self.empty_page.set_title("Podman tidak bisa dijalankan")
            self.empty_page.set_description("Pastikan podman terpasang dan bisa dijalankan dari terminal.")
            self.empty_page.set_child(None)
            self.cards_stack.set_visible_child_name("empty")
            return
        if not conts:
            p = self._project(self.selected_key)
            searching = not p and (self.search.get_text() or self.filter_dd.get_selected())
            self.empty_page.set_paintable(themes.illustration("curious" if searching else "sleep", 160))
            btn = None
            if p:
                self.empty_page.set_title("Belum ada container")
                self.empty_page.set_description(f"Jalankan compose up di {p.get('name')} untuk membuatnya.")
                btn = Gtk.Button(label="Jalankan compose up")
                btn.connect("clicked", lambda b: self.run_compose(p, "up"))
                btn.set_sensitive(p["path"] not in self.busy)
            elif self.search.get_text() or self.filter_dd.get_selected():
                self.empty_page.set_title("Tidak ada yang cocok")
                self.empty_page.set_description("Ubah kata kunci pencarian atau filter status.")
            else:
                self.empty_page.set_title("Kardusnya masih kosong")
                self.empty_page.set_description("Tambah folder proyek berisi compose.yaml, "
                                                "atau seret foldernya ke jendela ini.")
                btn = Gtk.Button(label="Tambah folder")
                btn.connect("clicked", self._on_add_folder_clicked)
            if btn:
                btn.add_css_class("pill")
                btn.add_css_class("suggested-action")
                btn.set_halign(Gtk.Align.CENTER)
            self.empty_page.set_child(btn)
            self.cards_stack.set_visible_child_name("empty")
            return

        for c in conts:
            card = ContainerCard(c, self, selected=(container_name(c) == self.selected_log_container))
            child = Gtk.FlowBoxChild(focusable=False)
            child.set_child(card)
            self.flowbox.append(child)
        self.cards_stack.set_visible_child_name("cards")

    # ─── Actions ─────────────────────────────────────────────────────────────
    def run_podman_action(self, argv, label):
        def _worker():
            try:
                r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
                ok = r.returncode == 0
                msg = (r.stderr or r.stdout or "").strip().splitlines()
                GLib.idle_add(self._toast, f"{label} selesai" if ok else f"{label} gagal: {msg[-1] if msg else 'tanpa pesan'}")
            except Exception as e:
                GLib.idle_add(self._toast, f"{label} gagal: {e}")
            GLib.idle_add(self.refresh_podman_list)
        self._toast(f"{label}…")
        threading.Thread(target=_worker, daemon=True).start()

    # Kompatibel dengan kode lama
    def run_podman_cmd_async(self, cmd_str, on_complete=None):
        def _target():
            subprocess.run(cmd_str, shell=True, capture_output=True, text=True)
            if on_complete:
                GLib.idle_add(on_complete)
        threading.Thread(target=_target, daemon=True).start()

    def _bulk(self, conts, action):
        names = [container_name(c) for c in conts
                 if (c.get("State") == "running") == (action == "stop")]
        if not names:
            self._toast("Tidak ada container yang perlu diubah")
            return
        self.run_podman_action(["podman", action] + names, f"{action} {len(names)} container")

    def run_compose(self, project, action, quiet=False):
        path = project["path"]
        if path in self.busy:
            return
        if not os.path.isdir(path):
            self._toast("Folder proyek tidak ditemukan")
            return
        if not pp.find_compose_file(path):
            self._toast("File compose tidak ditemukan di folder ini")
            return
        project["compose_file"] = pp.find_compose_file(path).name
        argv = pp.compose_cmd(project, action)
        self.busy.add(path)
        self.log_mode = "task"
        self.selected_log_container = None
        self.log_title.set_label(f"{project.get('name')} — {' '.join(argv)}")
        self.log_text.get_buffer().set_text(f"$ cd {pp.pretty_path(path)}\n$ {' '.join(argv)}\n")
        self._render_projects(force=True)
        self._render_header()
        if not quiet:
            self._toast(f"compose {action}: {project.get('name')}…")

        def _worker():
            code = -1
            try:
                proc = subprocess.Popen(argv, cwd=path, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in proc.stdout:
                    GLib.idle_add(self._log_append, line)
                code = proc.wait()
            except FileNotFoundError:
                GLib.idle_add(self._log_append,
                              "\n✗ podman-compose / podman compose tidak ditemukan.\n"
                              "  Pasang: `pip install podman-compose` atau paket podman-compose.\n")
            except Exception as e:
                GLib.idle_add(self._log_append, f"\n✗ {e}\n")
            GLib.idle_add(_done, code)

        def _done(code):
            self.busy.discard(path)
            self._log_append(f"\n{'✓ selesai' if code == 0 else f'✗ keluar dengan kode {code}'}\n")
            self._toast(f"compose {action} {project.get('name')}: {'selesai' if code == 0 else 'gagal'}")
            self._render_projects(force=True)
            self._render_header()
            self.refresh_podman_list()
            return False

        threading.Thread(target=_worker, daemon=True).start()

    def _run_autostart(self):
        targets = [p for p in self.projects if p.get("autostart") and os.path.isdir(p["path"])]
        if not targets:
            return False

        # Jalankan berurutan agar log tidak bercampur
        def _next(i=0):
            if i >= len(targets):
                return False
            if targets[i]["path"] in self.busy:
                GLib.timeout_add(800, _next, i)
                return False
            self.run_compose(targets[i], "up", quiet=True)
            GLib.timeout_add(800, _wait, i)
            return False

        def _wait(i):
            if targets[i]["path"] in self.busy:
                return True
            _next(i + 1)
            return False

        self._toast(f"Menjalankan {len(targets)} proyek auto-start…")
        _next(0)
        return False

    def _set_autostart(self, path, active):
        p = self._project(path)
        if p and bool(p.get("autostart")) != active:
            p["autostart"] = active
            pp.save_projects(self.projects)
            self._render_projects(force=True)
            self._toast("Auto-start aktif" if active else "Auto-start dimatikan")

    def _open_folder(self, path):
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
        except Exception:
            subprocess.Popen(["xdg-open", path])

    # ─── Tambah / hapus folder ───────────────────────────────────────────────
    def _on_add_folder_clicked(self, *_):
        parent = self.get_root()
        if hasattr(Gtk, "FileDialog"):
            dlg = Gtk.FileDialog(title="Pilih folder proyek (atau folder induk untuk dipindai)")

            def _cb(d, res):
                try:
                    f = d.select_folder_finish(res)
                except GLib.Error:
                    return
                if f and f.get_path():
                    self.add_folders([f.get_path()])
            dlg.select_folder(parent, None, _cb)
        else:
            dlg = Gtk.FileChooserNative(title="Pilih folder proyek", transient_for=parent,
                                        action=Gtk.FileChooserAction.SELECT_FOLDER)

            def _resp(d, r):
                if r == Gtk.ResponseType.ACCEPT and d.get_file():
                    self.add_folders([d.get_file().get_path()])
                d.destroy()
            dlg.connect("response", _resp)
            dlg.show()

    def _on_drop(self, _target, value, _x, _y):
        self.remove_css_class("bz-drop-active")
        try:
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
        except Exception:
            return False
        dirs = [p for p in paths if os.path.isdir(p)]
        if not dirs:
            self._toast("Seret folder, bukan file")
            return False
        self.add_folders(dirs)
        return True

    def add_folders(self, folders):
        def _worker():
            projects = list(self.projects)
            added = dup = 0
            for f in folders:
                projects, a, d = pp.add_folder(projects, f)
                added += a
                dup += d
            GLib.idle_add(_done, projects, added, dup)

        def _done(projects, added, dup):
            self.projects = projects
            pp.save_projects(self.projects)
            if added:
                self._toast(f"{added} proyek ditambahkan" + (f" ({dup} sudah ada)" if dup else ""))
                # pilih proyek terakhir yang ditambahkan
                self.selected_key = self.projects[-1]["path"]
            elif dup:
                self._toast("Folder sudah ada di daftar")
            else:
                self._toast("Tidak ada compose.yaml / docker-compose.yml di folder itu")
            self._render_projects(force=True)
            self._render_header()
            self._render_cards(force=True)
            return False

        threading.Thread(target=_worker, daemon=True).start()

    def _confirm_remove(self, p):
        name = p.get("name") or p["path"]
        body = (f"{pp.pretty_path(p['path'])}\n\nProyek hanya dihapus dari daftar Nyanux — "
                f"file di disk tidak disentuh.")
        if hasattr(Adw, "AlertDialog"):
            dlg = Adw.AlertDialog(heading=f"Hapus “{name}”?", body=body)
        else:
            dlg = Adw.MessageDialog(transient_for=self.get_root(), heading=f"Hapus “{name}”?", body=body)
        dlg.add_response("cancel", "Batal")
        dlg.add_response("down", "Down & Hapus")
        dlg.add_response("remove", "Hapus dari daftar")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_response_appearance("down", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def _resp(_d, response):
            if response == "cancel":
                return
            if response == "down":
                self.run_compose(dict(p), "down")
            self.projects = pp.remove_project(self.projects, p["path"])
            pp.save_projects(self.projects)
            if self.selected_key == p["path"]:
                self.selected_key = ALL_KEY
            self._render_projects(force=True)
            self._render_header()
            self._render_cards(force=True)
            self._toast(f"{name} dihapus dari daftar")

        dlg.connect("response", _resp)
        if hasattr(Adw, "AlertDialog") and isinstance(dlg, Adw.AlertDialog):
            dlg.present(self.get_root())
        else:
            dlg.present()

    # ─── Ganti port ──────────────────────────────────────────────────────────
    def open_port_dialog(self, container):
        name = container_name(container)
        maps = pp.port_mappings(container)
        cinfo = pp.compose_info(container)
        how = ("Ganti port yang Anda buka di browser (localhost). "
               "Port di dalam container tetap sama.")
        if cinfo:
            how += "\n\nService ini akan dibuat ulang; file compose proyek tidak diubah."
        else:
            how += ("\n\nContainer akan dibuat ulang. Data di volume aman, tapi perubahan di dalam "
                    "container yang tidak disimpan di volume akan hilang.")
        heading = f"Ganti port “{name}”"
        if hasattr(Adw, "AlertDialog"):
            dlg = Adw.AlertDialog(heading=heading, body=how)
        else:
            dlg = Adw.MessageDialog(transient_for=self.get_root(), heading=heading, body=how)

        lb = Gtk.ListBox()
        lb.add_css_class("boxed-list")
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        rows = []
        for m in maps:
            proto = "" if m["protocol"] == "tcp" else f"/{m['protocol']}"
            if m["range"] > 1:
                r = Adw.ActionRow(title=f"Rentang port {pp.port_spec(m)}",
                                  subtitle="Rentang port tidak bisa diubah di sini")
                lb.append(r)
                continue
            r = Adw.SpinRow.new_with_range(1, 65535, 1)
            r.set_title("Port di komputer Anda" if len(maps) == 1 else f"Port untuk container :{m['container_port']}{proto}")
            r.set_subtitle(f"localhost:{m['host_port']} → port {m['container_port']}{proto} di dalam container")
            r.set_value(m["host_port"])
            r.set_numeric(True)
            lb.append(r)
            rows.append((m, r))
        dlg.set_extra_child(lb)

        dlg.add_response("cancel", "Batal")
        dlg.add_response("apply", "Terapkan")
        dlg.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("apply")
        dlg.set_close_response("cancel")

        def _resp(_d, response):
            if response != "apply":
                return
            changes = {}
            for m, r in rows:
                new = int(r.get_value())
                if new != m["host_port"]:
                    changes[(m["container_port"], m["protocol"])] = new
            if not changes:
                self._toast("Port tidak berubah")
                return
            err = self._validate_ports(maps, changes)
            if err:
                self._toast(err)
                return
            self.change_ports(container, changes)

        dlg.connect("response", _resp)
        if hasattr(Adw, "AlertDialog") and isinstance(dlg, Adw.AlertDialog):
            dlg.present(self.get_root())
        else:
            dlg.present()

    def _validate_ports(self, maps, changes):
        current = {m["host_port"] for m in maps}
        final = [changes.get((m["container_port"], m["protocol"]), m["host_port"]) for m in maps]
        if len(final) != len(set(final)):
            return "Port host tidak boleh sama untuk dua mapping"
        low = pp.unprivileged_port_start()
        # Port milik container lain (termasuk yang sedang berhenti) → bentrok saat dinyalakan
        taken = {m["host_port"]: container_name(c) for c in self.containers
                 for m in pp.port_mappings(c) if m["host_port"] not in current}
        for p in changes.values():
            if p < low:
                return f"Port {p} butuh root (Podman rootless hanya bisa ≥ {low})"
            if p in taken:
                return f"Port {p} sudah dipakai container “{taken[p]}”"
            if p not in current and not pp.port_is_free(p):
                return f"Port {p} sedang dipakai program lain"
        return None

    def change_ports(self, container, changes):
        name = container_name(container)
        maps = pp.port_mappings(container)
        cinfo = pp.compose_info(container)
        self.log_mode = "task"
        self.selected_log_container = None
        self.log_title.set_label(f"{name} — ganti port")
        self.log_text.get_buffer().set_text("")
        self._toast(f"Mengganti port {name}…")

        def _run(argv, cwd=None):
            GLib.idle_add(self._log_append, f"$ {' '.join(argv)}\n")
            proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                GLib.idle_add(self._log_append, line)
            return proc.wait()

        def _compose():
            wd, svc, cfgs = cinfo
            specs = [pp.port_spec(m, changes.get((m["container_port"], m["protocol"])))
                     for m in maps]
            ov = pp.set_port_override(wd, svc, specs)
            GLib.idle_add(self._log_append, f"# override: {pp.pretty_path(str(ov))}\n")
            return _run(pp.compose_recreate_cmd(wd, cfgs, svc), cwd=wd) == 0

        def _standalone():
            r = subprocess.run(["podman", "inspect", name, "--format", "{{json .Config.CreateCommand}}"],
                               capture_output=True, text=True, timeout=20)
            try:
                cmd = json.loads(r.stdout or "null")
            except Exception:
                cmd = None
            if not cmd or len(cmd) < 2 or cmd[1] not in ("run", "create"):
                GLib.idle_add(self._log_append, "✗ Perintah pembuatan container tidak tersimpan — "
                                                "tidak bisa dibuat ulang otomatis.\n")
                return False
            new_cmd, done = pp.rewrite_publish_args(cmd, changes)
            if done != set(changes):
                GLib.idle_add(self._log_append, "✗ Mapping port tidak ditemukan di perintah asli "
                                                "(mungkin memakai -P / pod).\n")
                return False
            if new_cmd[1] == "run" and not any(a in ("-d", "--detach") or a.startswith("--detach=")
                                               for a in new_cmd):
                new_cmd.insert(2, "-d")
            was_running = container.get("State") == "running"
            bak = f"{name}-nyanux-old"
            if was_running and _run(["podman", "stop", name]) != 0:
                return False
            if _run(["podman", "rename", name, bak]) != 0:
                return False
            ok = _run(new_cmd) == 0
            if ok and new_cmd[1] == "create" and was_running:
                ok = _run(["podman", "start", name]) == 0
            if ok:
                _run(["podman", "rm", bak])
                return True
            GLib.idle_add(self._log_append, "\n↩ Gagal — mengembalikan container lama…\n")
            subprocess.run(["podman", "rm", "-f", name], capture_output=True)
            _run(["podman", "rename", bak, name])
            if was_running:
                _run(["podman", "start", name])
            return False

        def _worker():
            try:
                ok = _compose() if cinfo else _standalone()
            except Exception as e:
                GLib.idle_add(self._log_append, f"\n✗ {e}\n")
                ok = False
            GLib.idle_add(_done, ok)

        def _done(ok):
            new = ", ".join(str(p) for p in changes.values())
            self._log_append(f"\n{'✓ selesai' if ok else '✗ gagal'}\n")
            self._toast(f"Port {name} sekarang {new}" if ok else f"Gagal mengganti port {name}, lihat log")
            self.refresh_podman_list()
            return False

        threading.Thread(target=_worker, daemon=True).start()

    # ─── Logs ────────────────────────────────────────────────────────────────
    def select_container_for_logs(self, name):
        self.selected_log_container = name
        self.log_mode = "container"
        self.log_title.set_label(f"Log — {name}")
        self.refresh_podman_logs(force=True)
        self._render_cards(force=True)

    def clear_log_view(self, *_):
        self.selected_log_container = None
        self.log_mode = "container"
        self.log_title.set_label("Log — pilih container")
        self.log_text.get_buffer().set_text("")
        self._render_cards(force=True)

    def refresh_podman_logs(self, force=False):
        name = self.selected_log_container
        if not name or self.log_mode != "container":
            return

        def _worker():
            try:
                r = subprocess.run(["podman", "logs", "--tail=300", name],
                                   capture_output=True, text=True, timeout=15)
                out = (r.stdout or "") + (r.stderr or "")
                GLib.idle_add(self._set_log_buffer, name, out)
            except Exception:
                pass
        threading.Thread(target=_worker, daemon=True).start()

    def _set_log_buffer(self, name, text):
        if name != self.selected_log_container or self.log_mode != "container":
            return False
        buf = self.log_text.get_buffer()
        if buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False) == text:
            return False
        buf.set_text(text or "(log kosong)")
        if self.follow_sw.get_active():
            self._scroll_bottom()
        return False

    def _log_append(self, text):
        buf = self.log_text.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        self._scroll_bottom()
        return False

    def _scroll_bottom(self):
        buf = self.log_text.get_buffer()
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log_text.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        buf.delete_mark(mark)
