#!/usr/bin/env python3
"""
Nyanux — biar nggak pusing ngurus Linux tanpa GUI.
Ringkasan, Podman Runner & Kuota AI dalam satu jendela GTK4 / Libadwaita.

Tata letak adaptif:
  • lebar  → view switcher di header bar, sidebar proyek/akun selalu tampil
  • sedang → sidebar proyek/akun jadi panel geser (tombol di kiri atas konten)
  • sempit → view switcher pindah ke bar bawah
"""

import sys
import json
import webbrowser
from pathlib import Path

# Ensure directory is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, Gio, GLib

from dashboard_tab import DashboardTab
from podman_tab import PodmanTab
from ag_kuota_tab import AGKuotaTab, quota_summary, _needs_relogin
from podman_tab import compute_ports, container_name
from tray import TrayIcon, MenuItem

APP_NAME = "Nyanux"
APP_ID = "io.github.asshiddiq.Nyanux"
APP_VERSION = "2.0"
CONFIG_DIR = Path.home() / ".config" / "nyanux"
OLD_CONFIG_DIR = Path.home() / ".config" / "bazu"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

THEMES = [("system", "Ikuti sistem", Adw.ColorScheme.DEFAULT),
          ("light", "Terang", Adw.ColorScheme.FORCE_LIGHT),
          ("dark", "Gelap", Adw.ColorScheme.FORCE_DARK)]

# Lebar (sp) di bawah ini sidebar proyek/akun dilipat, dan view switcher pindah ke bawah
TRAY_ICON = APP_ID + "-tray"

SPLIT_BREAKPOINT = 900
NARROW_BREAKPOINT = 600


def migrate_old_config():
    """Pindahkan pengaturan dari nama lama (Bazu) sekali saja."""
    try:
        if OLD_CONFIG_DIR.is_dir() and not CONFIG_DIR.exists():
            OLD_CONFIG_DIR.rename(CONFIG_DIR)
            print(f"Pengaturan dipindah: {OLD_CONFIG_DIR} → {CONFIG_DIR}")
    except Exception as e:
        print(f"Gagal memindah pengaturan lama: {e}")


def load_app_settings():
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading settings: {e}")
    return {"theme": "system"}


def save_app_settings(settings):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print(f"Error saving settings: {e}")


CUSTOM_CSS = """
/* Sidebar di dalam tab (proyek / akun) */
.bz-sidebar { background: @sidebar_bg_color; }
.bz-sidebar-title { font-weight: 700; }

/* Tag kecil: IDE, cache, auto-start */
.bz-tag {
    font-size: 0.75em;
    font-weight: 700;
    padding: 1px 6px;
    border-radius: 6px;
    background: alpha(currentColor, 0.1);
}
.bz-tag.accent { color: @accent_color; background: alpha(@accent_color, 0.15); }
.bz-tag.warning { color: @warning_color; background: alpha(@warning_color, 0.15); }

/* Kuota per model */
.bz-quota-bar { min-width: 96px; }
.bz-quota-bar block.filled.success { background-color: @success_color; }
.bz-quota-bar block.filled.warning { background-color: @warning_color; }
.bz-quota-bar block.filled.error { background-color: @error_color; }
.bz-pct { font-feature-settings: "tnum"; min-width: 3.2em; }
"""


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1120, 760)
        self.set_size_request(360, 480)
        self.set_icon_name(APP_ID)
        self.style_manager = Adw.StyleManager.get_default()
        self.settings = load_app_settings()

        self._build_ui()
        self._install_actions()
        self.apply_theme(self.settings.get("theme", "system"), save=False)
        self._start_tray()
        self.connect("close-request", self._on_close_request)

    # ─── UI ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.content_stack = Adw.ViewStack()
        self.content_stack.set_vexpand(True)

        self.dashboard_tab = DashboardTab(self)
        self.podman_tab = PodmanTab(self)
        self.ag_kuota_tab = AGKuotaTab(self)
        for widget, name, title, icon in (
                (self.dashboard_tab, "dashboard", "Ringkasan", "go-home-symbolic"),
                (self.podman_tab, "podman", "Podman", "utilities-terminal-symbolic"),
                (self.ag_kuota_tab, "ag_kuota", "Kuota AI", "speedometer-symbolic")):
            self.content_stack.add_titled_with_icon(widget, name, title, icon)
        self.content_stack.connect("notify::visible-child-name", self._on_page_changed)

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.content_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        menu = Gio.Menu()
        menu.append("Muat ulang", "win.refresh")
        menu.append("Preferensi", "win.preferences")
        menu.append("Tentang Nyanux", "win.about")
        menu.append("Keluar", "app.quit")
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        menu_btn.set_tooltip_text("Menu utama")
        menu_btn.set_primary(True)
        header.pack_end(menu_btn)

        switcher_bar = Adw.ViewSwitcherBar(stack=self.content_stack)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.content_stack)
        toolbar.add_bottom_bar(switcher_bar)

        self._toast_overlay = Adw.ToastOverlay(child=toolbar)
        self.set_content(self._toast_overlay)

        # Breakpoint: hanya satu yang aktif (yang terakhir cocok), jadi breakpoint
        # sempit juga harus melipat sidebar.
        split = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {SPLIT_BREAKPOINT}sp"))
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {NARROW_BREAKPOINT}sp"))
        for bp in (split, narrow):
            for tab in (self.podman_tab, self.ag_kuota_tab):
                bp.add_setter(tab.split_view, "collapsed", True)
            self.add_breakpoint(bp)
        narrow.add_setter(header, "title-widget", Adw.WindowTitle(title=APP_NAME))
        narrow.add_setter(switcher_bar, "reveal", True)
        narrow.add_setter(self.podman_tab, "narrow", True)

        self.content_stack.set_visible_child_name("dashboard")

    def _install_actions(self):
        for name, cb, accels in (("refresh", self._on_refresh, ["<Control>r", "F5"]),
                                 ("preferences", self._on_preferences, ["<Control>comma"]),
                                 ("about", self._on_about, [])):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            if accels:
                self.get_application().set_accels_for_action(f"win.{name}", accels)

    # ─── System tray ─────────────────────────────────────────────────────────
    def _start_tray(self):
        self.tray = TrayIcon("nyanux", APP_NAME, TRAY_ICON, self._build_tray_menu,
                             lambda: self.show_window())
        self.tray_ok = self.tray.start()
        if self.tray_ok:
            GLib.timeout_add_seconds(5, self._update_tray_tooltip)

    def show_window(self, page=None):
        if page:
            self._switch_tab(page)
        self.set_visible(True)
        self.present()

    def _on_close_request(self, _win):
        # Tetap jalan di tray: jendela cuma disembunyikan
        if self.tray_ok and self.settings.get("close_to_tray", True):
            self.set_visible(False)
            return True
        return False

    def quit_app(self):
        if getattr(self, "tray", None):
            self.tray.stop()
        self.get_application().quit()

    def _update_tray_tooltip(self):
        conts = self.podman_tab.containers
        running = sum(1 for c in conts if c.get("State") == "running")
        lines = [f"{running} container berjalan" if self.podman_tab.podman_ok else "Podman tidak bisa dijalankan"]
        lows = [(quota_summary(a)[1], a) for a in self.ag_kuota_tab.accounts]
        lows = [(p, a) for p, a in lows if p is not None]
        if lows:
            p, a = min(lows, key=lambda x: x[0])
            lines.append(f"Kuota terendah: {p}% ({a.get('name') or a.get('email')})")
        self.tray.set_tooltip(APP_NAME, "\n".join(lines))
        return True

    def _build_tray_menu(self):
        M = MenuItem
        items = [M("Buka Nyanux", lambda: self.show_window()), M.separator()]

        # Container yang berjalan langsung di menu utama; detailnya di submenu
        pt = self.podman_tab
        if not pt.podman_ok:
            items.append(M.info("Podman tidak aktif"))
        else:
            running = [c for c in pt.containers if c.get("State") == "running"]
            stopped = [c for c in pt.containers if c.get("State") != "running"]
            if not running:
                items.append(M.info("Tidak ada container berjalan"))
            for c in running:
                name = container_name(c)
                _labels, urls = compute_ports(c)
                kids = [M(f"Buka {urls[0].replace('http://', '')}", lambda u=urls[0]: webbrowser.open(u))] if urls else []
                kids += [
                    M("Lihat log", lambda n=name: self._open_logs(n)),
                    M("Restart", lambda n=name: pt.run_podman_action(["podman", "restart", n], f"restart {n}")),
                    M("Stop", lambda n=name: pt.run_podman_action(["podman", "stop", n], f"stop {n}")),
                ]
                items.append(M(name, children=kids))
            if stopped:
                items.append(M("Jalankan container", children=[
                    M(container_name(c), lambda n=container_name(c): pt.run_podman_action(["podman", "start", n], f"start {n}"))
                    for c in stopped]))
        items.append(M.separator())

        # Kuota: satu baris per akun di submenu (nama pendek + kuota terendah)
        accounts = self.ag_kuota_tab.accounts
        rows = []
        for a in accounts:
            _parts, low = quota_summary(a)
            short = (a.get("email") or a.get("name") or "?").split("@")[0]
            value = "login ulang" if _needs_relogin(a) else (f"{low}%" if low is not None else "—")
            rows.append(M(f"{short}{' (IDE)' if a.get('is_active') else ''}  {value}",
                          lambda: self.show_window("ag_kuota")))
        items.append(M("Kuota AI", children=rows) if rows else M.info("Kuota AI: belum ada akun"))
        items += [M.separator(), M("Keluar", self.quit_app)]
        return items

    def _open_logs(self, name):
        self.show_window("podman")
        self.podman_tab.select_container_for_logs(name)

    # ─── Navigasi ────────────────────────────────────────────────────────────
    def _switch_tab(self, page_name):
        self.content_stack.set_visible_child_name(page_name)

    def _on_page_changed(self, stack, _pspec):
        if stack.get_visible_child_name() == "dashboard":
            self.dashboard_tab.refresh_dashboard()

    def _on_refresh(self, *_):
        active = self.content_stack.get_visible_child_name()
        if active == "dashboard":
            self.dashboard_tab.refresh_dashboard()
        elif active == "podman":
            self.podman_tab.refresh_podman_list()
        else:
            self.ag_kuota_tab.refresh_data()

    # ─── Tema & preferensi ───────────────────────────────────────────────────
    def apply_theme(self, key, save=True):
        scheme = next((s for k, _l, s in THEMES if k == key), Adw.ColorScheme.DEFAULT)
        self.style_manager.set_color_scheme(scheme)
        if save:
            self.settings["theme"] = key
            save_app_settings(self.settings)

    def _on_preferences(self, *_):
        dlg = Adw.PreferencesDialog()
        page = Adw.PreferencesPage()
        dlg.add(page)

        look = Adw.PreferencesGroup(title="Tampilan")
        theme_row = Adw.ComboRow(title="Tema", model=Gtk.StringList.new([l for _k, l, _s in THEMES]))
        keys = [k for k, _l, _s in THEMES]
        cur = self.settings.get("theme", "system")
        theme_row.set_selected(keys.index(cur) if cur in keys else 0)
        theme_row.connect("notify::selected", lambda r, _p: self.apply_theme(keys[r.get_selected()]))
        look.add(theme_row)
        page.add(look)

        quota = Adw.PreferencesGroup(title="Kuota AI",
                                     description="Sisa kuota diambil langsung dari API Google untuk semua akun.")
        poll = Adw.SpinRow.new_with_range(10, 600, 5)
        poll.set_title("Interval pembaruan")
        poll.set_subtitle("Detik")
        poll.set_value(self.ag_kuota_tab.poll_interval)
        poll.connect("notify::value", lambda r, _p: self.ag_kuota_tab.set_poll_interval(int(r.get_value())))
        quota.add(poll)
        page.add(quota)

        tray_group = Adw.PreferencesGroup(title="System tray")
        stay = Adw.SwitchRow(title="Tetap jalan di tray saat jendela ditutup",
                             subtitle="Klik ikon Nyanux di tray untuk melihat info dan membuka lagi",
                             active=self.settings.get("close_to_tray", True), sensitive=self.tray_ok)
        stay.connect("notify::active", lambda r, _p: self._set_setting("close_to_tray", r.get_active()))
        tray_group.add(stay)
        page.add(tray_group)

        dlg.present(self)

    def _set_setting(self, key, value):
        self.settings[key] = value
        save_app_settings(self.settings)

    def _on_about(self, *_):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=APP_VERSION,
            developer_name="as-shiddiq",
            comments="Biar nggak pusing ngurus Linux tanpa GUI. Sekarang: container Podman dan kuota AI Antigravity.",
            website="https://github.com/as-shiddiq/nyanux",
            issue_url="https://github.com/as-shiddiq/nyanux/issues",
            license_type=Gtk.License.MIT_X11,
            copyright="© 2026 as-shiddiq",
        )
        about.present(self)

    def _toast(self, msg):
        self._toast_overlay.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg), timeout=3))


class NyanuxApp(Adw.Application):
    def __init__(self):
        # Satu instance saja: membuka Nyanux lagi cukup menampilkan jendela yang ada
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None
        self.connect("activate", self._on_activate)
        quit_act = Gio.SimpleAction.new("quit", None)
        quit_act.connect("activate", lambda *_: self.win.quit_app() if self.win else self.quit())
        self.add_action(quit_act)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def _on_activate(self, app):
        if self.win:
            self.win.show_window()
            return
        css = Gtk.CssProvider()
        css.load_from_string(CUSTOM_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.win = MainWindow(app)
        self.win.present()


if __name__ == "__main__":
    migrate_old_config()
    NyanuxApp().run(sys.argv)
