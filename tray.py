"""
tray.py — Ikon system tray (StatusNotifierItem) tanpa GTK3 / AppIndicator.

GTK4 tidak punya API tray, jadi ikon & menunya diekspor langsung lewat D-Bus:
  • org.kde.StatusNotifierItem  → ikon, tooltip, klik
  • com.canonical.dbusmenu      → isi menu (dibangun ulang setiap kali menu dibuka)

Didukung KDE Plasma, Cinnamon, XFCE, Budgie, dll. GNOME butuh ekstensi
"AppIndicator and KStatusNotifierItem Support"; tanpa itu ikon tidak muncul tapi
aplikasi tetap jalan normal.

Pemakaian:
    tray = TrayIcon("nyanux", "Nyanux", icon_name, build_menu, on_activate)
    tray.set_tooltip("Nyanux", "3 container berjalan")

build_menu() mengembalikan list MenuItem; dipanggil saat menu akan dibuka.
"""

import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from gi.repository import Gio, GLib

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/StatusNotifierItem/Menu"
WATCHER = "org.kde.StatusNotifierWatcher"

_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

_MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""


@dataclass
class MenuItem:
    """Satu baris menu. label=None → pemisah. Tanpa on_click → teks info (tidak bisa diklik)."""
    label: Optional[str] = None
    on_click: Optional[Callable[[], None]] = None
    icon: str = ""
    children: List["MenuItem"] = field(default_factory=list)
    enabled: bool = True

    @staticmethod
    def separator():
        return MenuItem(None)

    @staticmethod
    def info(text):
        return MenuItem(text, enabled=False)


class TrayIcon:
    def __init__(self, app_id, title, icon_name, build_menu, on_activate, icon_theme_path=""):
        self.app_id = app_id
        self.title = title
        self.icon_name = icon_name
        self.icon_theme_path = icon_theme_path
        self.build_menu = build_menu
        self.on_activate = on_activate
        self.tooltip = (title, "")

        self._revision = 0
        self._items = {}          # id → (props dict, [child ids], MenuItem)
        # ID harus tetap untuk baris yang sama antar-rebuild: panel menyimpan layout lama,
        # jadi ID yang bergeser bisa membuat item tampil sebagai pemisah / judul.
        self._ids = {}            # kunci stabil → id
        self._layout_sig = None
        self._bus = None
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._reg_ids = []
        self._name_id = 0
        self._watch_id = 0

    # ─── Siklus hidup ────────────────────────────────────────────────────────
    def start(self):
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as e:
            print(f"[tray] D-Bus tidak tersedia: {e.message}")
            return False
        sni = Gio.DBusNodeInfo.new_for_xml(_SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(_MENU_XML).interfaces[0]
        self._reg_ids = [
            self._bus.register_object(SNI_PATH, sni, self._sni_call, self._sni_prop, None),
            self._bus.register_object(MENU_PATH, menu, self._menu_call, self._menu_prop, None),
        ]
        self._rebuild()
        self._name_id = Gio.bus_own_name_on_connection(
            self._bus, self._bus_name, Gio.BusNameOwnerFlags.NONE, None, None)
        # Daftar ulang setiap kali panel (watcher) muncul / restart
        self._watch_id = Gio.bus_watch_name_on_connection(
            self._bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self._on_watcher_appeared, None)
        return True

    def stop(self):
        if not self._bus:
            return
        for rid in self._reg_ids:
            self._bus.unregister_object(rid)
        if self._name_id:
            Gio.bus_unown_name(self._name_id)
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
        self._reg_ids, self._name_id, self._watch_id = [], 0, 0

    def _on_watcher_appeared(self, conn, name, owner):
        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self._bus_name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                  self._on_registered)

    def _on_registered(self, conn, res):
        try:
            conn.call_finish(res)
        except GLib.Error as e:
            print(f"[tray] gagal mendaftar ke system tray: {e.message}")

    # ─── API ─────────────────────────────────────────────────────────────────
    def set_tooltip(self, title, body=""):
        if (title, body) == self.tooltip:
            return
        self.tooltip = (title, body)
        self._emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)

    def refresh_menu(self):
        """Bangun ulang menu (mis. setelah data berubah saat menu sedang terbuka)."""
        if self._rebuild():
            self._notify_layout()

    def _notify_layout(self):
        self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                   GLib.Variant("(ui)", (self._revision, 0)))

    def _emit(self, path, iface, signal, params):
        if self._bus:
            try:
                self._bus.emit_signal(None, path, iface, signal, params)
            except GLib.Error:
                pass

    # ─── StatusNotifierItem ──────────────────────────────────────────────────
    def _sni_prop(self, conn, sender, path, iface, prop):
        title, body = self.tooltip
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.app_id),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconThemePath": GLib.Variant("s", self.icon_theme_path),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon_name, [], title, body)),
            # Klik kiri langsung membuka menu info (bukan jendela)
            "ItemIsMenu": GLib.Variant("b", True),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _sni_call(self, conn, sender, path, iface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self._call_safe, self.on_activate)
        invocation.return_value(None)

    # ─── dbusmenu ────────────────────────────────────────────────────────────
    def _rebuild(self):
        self._items = {0: ({"children-display": GLib.Variant("s", "submenu")}, [], None)}
        try:
            entries = self.build_menu() or []
        except Exception as e:
            print(f"[tray] gagal membangun menu: {e}")
            entries = [MenuItem.info("Gagal memuat menu")]
        self._items[0][1].extend(self._add_items(entries, ""))
        sig = repr(self._layout(0, -1))
        changed = sig != self._layout_sig
        if changed:
            self._layout_sig = sig
            self._revision += 1
        return changed

    def _stable_id(self, key):
        if key not in self._ids:
            self._ids[key] = len(self._ids) + 1
        return self._ids[key]

    def _add_items(self, entries, prefix):
        ids, seen = [], {}
        for it in entries:
            base = f"{prefix}/{it.label if it.label is not None else '---'}"
            seen[base] = seen.get(base, 0) + 1
            iid = self._stable_id(f"{base}#{seen[base]}")
            if it.label is None:
                props = {"type": GLib.Variant("s", "separator")}
            else:
                props = {"label": GLib.Variant("s", it.label.replace("_", "__")),
                         "enabled": GLib.Variant("b", bool(it.enabled and (it.on_click or it.children)))}
                if it.icon:
                    props["icon-name"] = GLib.Variant("s", it.icon)
                if it.children:
                    props["children-display"] = GLib.Variant("s", "submenu")
            self._items[iid] = (props, [], it)
            self._items[iid][1].extend(self._add_items(it.children, f"{base}#{seen[base]}"))
            ids.append(iid)
        return ids

    def _layout(self, iid, depth):
        props, children, _it = self._items.get(iid, ({}, [], None))
        kids = []
        if depth != 0:
            kids = [GLib.Variant("(ia{sv}av)", self._layout(c, depth - 1)) for c in children]
        return (iid, props, kids)

    def _menu_prop(self, conn, sender, path, iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [self.icon_theme_path] if self.icon_theme_path else []),
        }.get(prop)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            parent, depth, _names = args
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self._revision, self._layout(parent, depth))))
        elif method == "GetGroupProperties":
            ids, _names = args
            ids = ids or [i for i in self._items if i]
            out = [(i, self._items[i][0]) for i in ids if i in self._items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (out,)))
        elif method == "GetProperty":
            iid, name = args
            v = self._items.get(iid, ({}, [], None))[0].get(name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (v,)))
        elif method == "Event":
            iid, event, _data, _ts = args
            self._handle_event(iid, event)
            invocation.return_value(None)
        elif method == "EventGroup":
            errors = []
            for iid, event, _data, _ts in args[0]:
                if iid in self._items:
                    self._handle_event(iid, event)
                else:
                    errors.append(iid)
            invocation.return_value(GLib.Variant("(ai)", (errors,)))
        elif method == "AboutToShow":
            # Menu akan dibuka → ambil data terbaru
            changed = args[0] == 0 and self._rebuild()
            invocation.return_value(GLib.Variant("(b)", (bool(changed),)))
            if changed:
                self._notify_layout()
        elif method == "AboutToShowGroup":
            ids = args[0]
            changed = 0 in ids and self._rebuild()
            invocation.return_value(GLib.Variant("(aiai)", ([0] if changed else [], [])))
            if changed:
                self._notify_layout()
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _handle_event(self, iid, event):
        if event != "clicked":
            return
        it = self._items.get(iid, (None, None, None))[2]
        if it and it.on_click:
            GLib.idle_add(self._call_safe, it.on_click)

    @staticmethod
    def _call_safe(fn):
        try:
            fn()
        except Exception as e:
            print(f"[tray] aksi gagal: {e}")
        return False
