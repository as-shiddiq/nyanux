"""
Ringkasan — container yang berjalan dan sisa kuota tiap akun, dalam satu halaman.
Tidak memanggil jaringan: kuota diambil dari data tab Kuota AI (atau cache-nya).
"""

import subprocess
import json
import threading
import webbrowser
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

import ag_kuota_backend as backend
import podman_projects as pp
from podman_tab import compute_ports, container_name


class DashboardTab(Adw.Bin):
    def __init__(self, main_win):
        super().__init__()
        self.main_win = main_win
        self._busy = False

        self.page = Adw.PreferencesPage()
        self.set_child(self.page)

        self.podman_group = self._group("Container", "Buka Podman", "podman")
        self.kuota_group = self._group("Kuota AI", "Buka Kuota AI", "ag_kuota")
        self._rows = {self.podman_group: [], self.kuota_group: []}

        self.refresh_dashboard()

    def _group(self, title, link_label, target):
        g = Adw.PreferencesGroup(title=title)
        btn = Gtk.Button(label=link_label, valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.connect("clicked", lambda b: self.main_win._switch_tab(target))
        g.set_header_suffix(btn)
        self.page.add(g)
        return g

    def _set_rows(self, group, rows):
        for r in self._rows[group]:
            group.remove(r)
        for r in rows:
            group.add(r)
        self._rows[group] = rows

    # ─── Data ────────────────────────────────────────────────────────────────
    def refresh_dashboard(self):
        if self._busy:
            return
        self._busy = True

        def _worker():
            containers, ok = [], True
            try:
                res = subprocess.run(["podman", "ps", "-a", "--format", "json"],
                                     capture_output=True, text=True, timeout=20)
                ok = res.returncode == 0
                containers = json.loads(res.stdout or "[]") or [] if ok else []
            except Exception:
                ok = False
            accounts = list(getattr(self.main_win, "ag_kuota_tab", None) and
                            self.main_win.ag_kuota_tab.accounts or [])
            if not accounts:
                accounts = self._accounts_from_cache()
            GLib.idle_add(self._apply, ok, [c for c in containers if not c.get("IsInfra")], accounts)

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _accounts_from_cache():
        try:
            cache = backend.load_cache()
            active = backend.get_ide_active_email() or ""
            out = []
            for a in backend.load_accounts_json():
                c = cache.get(a.get("email"), {})
                out.append({"name": a.get("name", ""), "email": a.get("email", ""),
                            "is_active": a.get("email") == active,
                            "quotas": backend.refresh_cached_quotas(c.get("quotas") or []),
                            "has_live_data": False})
            return out
        except Exception as e:
            print(f"[dashboard] cache error: {e}")
            return []

    def _apply(self, ok, containers, accounts):
        self._busy = False
        self._apply_podman(ok, containers)
        self._apply_kuota(accounts)
        return False

    # ─── Container ───────────────────────────────────────────────────────────
    def _apply_podman(self, ok, containers):
        if not ok:
            self.podman_group.set_description("Podman tidak bisa dijalankan")
            self._set_rows(self.podman_group, [])
            return
        running = [c for c in containers if c.get("State") == "running"]
        stopped = len(containers) - len(running)
        self.podman_group.set_description(f"{len(running)} berjalan · {stopped} berhenti")

        rows = []
        for c in sorted(running, key=container_name):
            row = Adw.ActionRow(title=GLib.markup_escape_text(container_name(c)),
                                subtitle=GLib.markup_escape_text(pp.short_image(c.get("Image", ""))))
            _labels, urls = compute_ports(c)
            if urls:
                port = urls[0].rsplit(":", 1)[-1]
                b = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text=f"Buka {urls[0]}")
                b.set_child(Adw.ButtonContent(icon_name="external-link-symbolic", label=f":{port}"))
                b.add_css_class("flat")
                b.connect("clicked", lambda _b, u=urls[0]: webbrowser.open(u))
                row.add_suffix(b)
            rows.append(row)
        if not rows:
            rows.append(Adw.ActionRow(title="Tidak ada container yang berjalan"))
        self._set_rows(self.podman_group, rows)

    # ─── Kuota ───────────────────────────────────────────────────────────────
    def _apply_kuota(self, accounts):
        from ag_kuota_tab import _pct, _needs_relogin, quota_suffix
        active = next((a for a in accounts if a.get("is_active")), None)
        self.kuota_group.set_description(
            f"Dipakai di IDE: {active['email']}" if active else
            ("Belum ada akun" if not accounts else ""))

        rows = []
        for a in accounts:
            parts, lowest = [], None
            for cat in a.get("quotas") or []:
                pcts = [_pct(m) for m in cat.get("models") or []]
                if not pcts:
                    continue
                low = min(pcts)
                lowest = low if lowest is None else min(lowest, low)
                parts.append(f"{cat.get('category', '').replace(' Models', '')} {low}%")
            row = Adw.ActionRow(title=GLib.markup_escape_text(a.get("name") or a.get("email", "")),
                                subtitle=GLib.markup_escape_text(" · ".join(parts) or a.get("email", "")))
            row.set_subtitle_lines(1)
            if _needs_relogin(a):
                tag = Gtk.Label(label="Perlu login", valign=Gtk.Align.CENTER)
                tag.add_css_class("bz-tag")
                tag.add_css_class("warning")
                row.add_suffix(tag)
            elif lowest is not None:
                row.add_suffix(quota_suffix(lowest))
            rows.append(row)
        self._set_rows(self.kuota_group, rows)
