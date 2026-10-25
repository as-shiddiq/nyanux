import sys
import json
import base64
import threading
import urllib.parse
from datetime import datetime, timezone, timedelta
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Pango

# Import backend original AG Kuota
import ag_kuota_backend as backend
import ag_kuota_live as live
import themes

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def _avatar_text(name):
    """Nama untuk inisial avatar, tanpa bagian dalam kurung: "Budi (kerja)" → "Budi"."""
    import re
    return re.sub(r"\s*\(.*?\)", "", name or "").strip() or name or "?"


def _needs_relogin(account):
    return (not account.get("has_live_data")
            and "login ulang" in (account.get("live_error") or "").lower())


def _pct(model):
    try:
        return int(str(model.get("remaining", "0")).replace("%", ""))
    except ValueError:
        return 0


def _reset_dt(model):
    raw = model.get("reset_time_raw")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def _fmt_reset(dt):
    """'Reset 13:10 · 4 j 5 m lagi' — ringkas, dalam waktu lokal."""
    now = datetime.now().astimezone()
    if dt <= now:
        return "Sudah reset"
    when = dt.strftime("%H:%M")
    if dt.date() != now.date():
        when = f"{dt.day} {_MONTHS[dt.month - 1]} {when}"
    mins = int((dt - now).total_seconds() // 60)
    d, h, m = mins // 1440, (mins % 1440) // 60, mins % 60
    left = f"{d} h {h} j" if d else (f"{h} j {m} m" if h else f"{m} m")
    return f"Reset {when} · {left} lagi"


def _reset_text(model):
    dt = _reset_dt(model)
    if dt:
        return _fmt_reset(dt)
    return (model.get("reset") or "").replace("Reset: ", "Reset ")


def quota_summary(account):
    """(["Gemini 90%", "Claude dan GPT 40%"], persen terendah | None) — kuota terendah per kategori."""
    parts, lowest = [], None
    for cat in account.get("quotas") or []:
        pcts = [_pct(m) for m in cat.get("models") or []]
        if not pcts:
            continue
        low = min(pcts)
        lowest = low if lowest is None else min(lowest, low)
        parts.append(f"{cat.get('category', '').replace(' Models', '')} {low}%")
    return parts, lowest


def _level_class(pct):
    return "success" if pct > 50 else ("warning" if pct > 20 else "error")


def quota_suffix(pct):
    """Bar + angka persen untuk ditaruh sebagai suffix sebuah row."""
    box = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
    bar = Gtk.LevelBar(min_value=0, max_value=100, value=pct, valign=Gtk.Align.CENTER)
    bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
    for name in ("low", "high", "full"):
        bar.remove_offset_value(name)
    bar.add_css_class("bz-quota-bar")
    bar.add_css_class(_level_class(pct))
    box.append(bar)
    lbl = Gtk.Label(label=f"{pct}%", xalign=1)
    lbl.add_css_class("bz-pct")
    lbl.add_css_class("numeric")
    if pct <= 20:
        lbl.add_css_class("error")
    box.append(lbl)
    return box


class AccountRow(Gtk.ListBoxRow):
    def __init__(self, account):
        super().__init__()
        self.account = account
        box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6)
        box.append(Adw.Avatar(size=32, text=_avatar_text(account.get("name")), show_initials=True))

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        name = Gtk.Label(label=account.get("name", ""), xalign=0, ellipsize=Pango.EllipsizeMode.END)
        vbox.append(name)
        email = Gtk.Label(label=account.get("email", ""), xalign=0, ellipsize=Pango.EllipsizeMode.END)
        email.add_css_class("dim-label")
        email.add_css_class("caption")
        vbox.append(email)
        box.append(vbox)

        if account.get("is_active"):
            box.append(self._tag("IDE", "accent", "Akun yang sedang dipakai Antigravity IDE"))
        if _needs_relogin(account):
            box.append(self._tag("Login", "warning", account.get("live_error")))
        elif not account.get("has_live_data"):
            box.append(self._tag("Cache", None, account.get("live_error") or "Data dari cache"))
        self.set_child(box)

    @staticmethod
    def _tag(text, cls, tip):
        t = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
        t.add_css_class("bz-tag")
        if cls:
            t.add_css_class(cls)
        if tip:
            t.set_tooltip_text(tip)
        return t


class AGKuotaTab(Gtk.Box):
    def __init__(self, main_win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.main_win = main_win
        self.selected_account = None
        self.accounts = []
        self._poll_timer_id = None
        self._suppress_select = False
        self.poll_interval = int(main_win.settings.get("quota_poll_sec", backend.POLL_INTERVAL_SEC))

        self._build_ui()
        self.refresh_data(silent=True)
        self._start_auto_refresh()

    # ─── UI ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.split_view = Adw.OverlaySplitView(vexpand=True, min_sidebar_width=240, max_sidebar_width=300)
        self.split_view.connect("notify::collapsed", self._on_collapsed_changed)
        self.append(self.split_view)

        # ─── Sidebar: daftar akun ───
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.add_css_class("bz-sidebar")
        head = Gtk.Box(spacing=6, margin_start=18, margin_end=10, margin_top=10, margin_bottom=4)
        t = Gtk.Label(label="Akun", xalign=0, hexpand=True)
        t.add_css_class("bz-sidebar-title")
        head.append(t)
        add_btn = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Tambah akun Google")
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._on_add_account_clicked)
        head.append(add_btn)
        side.append(head)

        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.list_box = Gtk.ListBox()
        self.list_box.add_css_class("navigation-sidebar")
        self.list_box.connect("row-selected", self._on_account_selected)
        scroll.set_child(self.list_box)
        side.append(themes.sidebar_decor(scroll))
        self.split_view.set_sidebar(side)

        # ─── Konten: detail akun ───
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", self._on_banner_clicked)
        content.append(self.banner)

        self.detail_stack = Gtk.Stack(vexpand=True)
        content.append(self.detail_stack)

        scroll_detail = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=560)
        self.detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                                  margin_start=12, margin_end=12, margin_top=18, margin_bottom=24)
        clamp.set_child(self.detail_box)
        scroll_detail.set_child(clamp)
        self.detail_stack.add_named(scroll_detail, "detail")

        self.empty_page = Adw.StatusPage(paintable=themes.illustration("sleep", 160),
                                         title="Belum ada akun",
                                         description="Tambahkan akun Google untuk memantau sisa kuota Antigravity.")
        empty_btn = Gtk.Button(label="Tambah akun", halign=Gtk.Align.CENTER)
        empty_btn.add_css_class("pill")
        empty_btn.add_css_class("suggested-action")
        empty_btn.connect("clicked", self._on_add_account_clicked)
        self.empty_page.set_child(empty_btn)
        self.detail_stack.add_named(self.empty_page, "empty")
        themes.on_change(lambda: self.empty_page.set_paintable(themes.illustration("sleep", 160)))

        self.loading_page = Adw.StatusPage(title="Memuat akun…")
        self.loading_page.set_child(Adw.Spinner(halign=Gtk.Align.CENTER))
        self.detail_stack.add_named(self.loading_page, "loading")
        self.detail_stack.set_visible_child_name("loading")

        self.split_view.set_content(content)

    def _on_collapsed_changed(self, split, _pspec):
        if self.selected_account:
            self._render_detail()

    def _toast(self, msg):
        if hasattr(self.main_win, "_toast"):
            self.main_win._toast(msg)

    # ─── Polling ─────────────────────────────────────────────────────────────
    def _start_auto_refresh(self):
        if self._poll_timer_id:
            GLib.source_remove(self._poll_timer_id)
        self._poll_timer_id = GLib.timeout_add_seconds(self.poll_interval, self._auto_refresh_tick)

    def _auto_refresh_tick(self):
        self.refresh_data(silent=True)
        return True

    def set_poll_interval(self, seconds):
        self.poll_interval = max(10, int(seconds))
        self.main_win.settings["quota_poll_sec"] = self.poll_interval
        from nyanux import save_app_settings
        save_app_settings(self.main_win.settings)
        self._start_auto_refresh()

    def refresh_data(self, silent=False):
        def _fetch():
            accs = backend.gather_all_accounts()
            GLib.idle_add(self._update_accounts_ui, accs, silent)
        threading.Thread(target=_fetch, daemon=True).start()

    def _update_accounts_ui(self, accounts, silent=False):
        prev_email = self.selected_account["email"] if self.selected_account else None
        self.accounts = accounts
        self.updated_at = datetime.now()

        self._suppress_select = True
        while (c := self.list_box.get_first_child()):
            self.list_box.remove(c)
        for acc in self.accounts:
            self.list_box.append(AccountRow(acc))
        self._suppress_select = False

        if not self.accounts:
            self.selected_account = None
            self.banner.set_revealed(False)
            self.detail_stack.set_visible_child_name("empty")
            return False

        idx = next((i for i, a in enumerate(self.accounts) if a["email"] == prev_email), None)
        if idx is None:
            idx = next((i for i, a in enumerate(self.accounts) if a.get("is_active")), 0)
        self._suppress_select = True
        self.list_box.select_row(self.list_box.get_row_at_index(idx))
        self._suppress_select = False
        self.selected_account = self.accounts[idx]
        self._render_detail()
        if not silent:
            self._toast("Kuota diperbarui")
        if hasattr(self.main_win, "dashboard_tab"):
            self.main_win.dashboard_tab.refresh_dashboard()
        return False

    def _on_account_selected(self, box, row):
        if self._suppress_select or not row:
            return
        idx = row.get_index()
        if 0 <= idx < len(self.accounts):
            self.selected_account = self.accounts[idx]
            self._render_detail()
            if self.split_view.get_collapsed():
                self.split_view.set_show_sidebar(False)

    # ─── Detail akun ─────────────────────────────────────────────────────────
    def _render_detail(self):
        acc = self.selected_account
        if not acc:
            return
        self.detail_stack.set_visible_child_name("detail")
        while (c := self.detail_box.get_first_child()):
            self.detail_box.remove(c)

        # Banner: data cache / perlu login ulang
        if _needs_relogin(acc):
            self.banner.set_title("Sesi akun ini kedaluwarsa. Login ulang untuk kuota realtime.")
            self.banner.set_button_label("Login ulang")
            self.banner.set_revealed(True)
        elif not acc.get("has_live_data"):
            err = acc.get("live_error")
            self.banner.set_title(f"Menampilkan data cache{': ' + err if err else ''}")
            self.banner.set_button_label("Coba lagi")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

        # ─── Header akun ───
        head = Gtk.Box(spacing=14)
        if self.split_view.get_collapsed():
            head.append(self._sidebar_toggle())
        head.append(Adw.Avatar(size=48, text=_avatar_text(acc.get("name")), show_initials=True,
                               valign=Gtk.Align.CENTER))
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True, spacing=2)
        name = Gtk.Label(label=acc.get("name", ""), xalign=0, ellipsize=Pango.EllipsizeMode.END)
        name.add_css_class("title-2")
        info.append(name)
        sub = Gtk.Label(label=f"{acc['email']} · {acc.get('plan') or '—'}", xalign=0,
                        ellipsize=Pango.EllipsizeMode.MIDDLE, selectable=True)
        sub.add_css_class("dim-label")
        info.append(sub)
        head.append(info)
        self.detail_box.append(head)

        # ─── Aksi ───
        actions = Gtk.Box(spacing=8)
        if acc.get("is_active"):
            act = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER, margin_end=4)
            act.append(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            act.append(Gtk.Label(label="Dipakai di IDE"))
            act.add_css_class("dim-label")
            actions.append(act)
        else:
            use_btn = Gtk.Button(label="Gunakan di IDE",
                                 tooltip_text="Ganti akun yang dipakai Antigravity IDE ke akun ini")
            use_btn.connect("clicked", self._on_switch_clicked)
            actions.append(use_btn)

        self.check_now_btn = Gtk.Button(label="Cek sekarang",
                                        tooltip_text="Ambil sisa kuota realtime dari API Google")
        self.check_now_btn.connect("clicked", self._on_check_now_clicked)
        actions.append(self.check_now_btn)

        spacer = Gtk.Box(hexpand=True)
        actions.append(spacer)

        menu = Gio.Menu()
        menu.append("Login ulang", "acct.relogin")
        menu.append("Hapus akun", "acct.delete")
        group = Gio.SimpleActionGroup()
        for name_, cb in (("relogin", lambda *_: self._on_add_account_clicked(None, login_hint=acc["email"])),
                          ("delete", lambda *_: self._on_delete_account_clicked(None))):
            a = Gio.SimpleAction.new(name_, None)
            a.connect("activate", cb)
            group.add_action(a)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Lainnya")
        more.add_css_class("flat")
        more.insert_action_group("acct", group)
        actions.append(more)
        self.detail_box.append(actions)

        # ─── Kuota ───
        quotas = acc.get("quotas") or []
        if not quotas:
            st = Adw.StatusPage(icon_name="speedometer-symbolic", title="Belum ada data kuota",
                                description=acc.get("live_error") or "Klik “Cek sekarang” untuk mengambil data.")
            st.add_css_class("compact")
            self.detail_box.append(st)
        for cat in quotas:
            self.detail_box.append(self._quota_group(cat))

        stamp = acc.get("checked_at") or ""
        src = "Realtime dari API Google" if acc.get("has_live_data") else "Data cache"
        foot = Gtk.Label(label=f"{src} · diperbarui {stamp}" if stamp else src, xalign=0)
        foot.add_css_class("dim-label")
        foot.add_css_class("caption")
        self.detail_box.append(foot)

    def _quota_group(self, cat):
        models = cat.get("models") or []
        resets = {_reset_text(m) for m in models}
        common_reset = resets.pop() if len(resets) == 1 else None

        group = Adw.PreferencesGroup(title=GLib.markup_escape_text(cat.get("category", "")))
        if common_reset:
            group.set_description(common_reset)
        for m in models:
            row = Adw.ActionRow(title=GLib.markup_escape_text(m.get("name", "")))
            if not common_reset:
                row.set_subtitle(_reset_text(m))
            row.add_suffix(quota_suffix(_pct(m)))
            group.add(row)
        return group

    def _sidebar_toggle(self):
        btn = Gtk.Button(icon_name="sidebar-show-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text="Daftar akun")
        btn.add_css_class("flat")
        btn.connect("clicked", lambda b: self.split_view.set_show_sidebar(True))
        return btn

    def _on_banner_clicked(self, banner):
        if not self.selected_account:
            return
        if _needs_relogin(self.selected_account):
            self._on_add_account_clicked(None, login_hint=self.selected_account["email"])
        else:
            self._on_check_now_clicked(self.check_now_btn)

    def _show_no_client_dialog(self):
        dlg = Adw.AlertDialog(
            heading="Client OAuth belum diatur",
            body=("Untuk login akun Google, Nyanux butuh client ID & secret OAuth Antigravity. "
                  "Nilainya tidak disimpan di kode.\n\nIsi file ~/.config/nyanux/oauth_clients.json "
                  "atau set env ANTIGRAVITY_OAUTH_CLIENT_ID dan ANTIGRAVITY_OAUTH_CLIENT_SECRET. "
                  "Contoh format ada di README."))
        dlg.add_response("ok", "Mengerti")
        dlg.present(self.get_root())

    def _on_check_now_clicked(self, btn):
        if not self.selected_account:
            return
        email = self.selected_account["email"]
        btn.set_sensitive(False)
        self._toast(f"Mengecek kuota {email}…")

        def _worker():
            res = backend.fetch_single_account_live(email)
            GLib.idle_add(_done, res)

        def _done(res):
            btn.set_sensitive(True)
            if res.get("ok"):
                self._toast(f"Kuota {email} diperbarui")
            else:
                self._toast(f"Gagal: {res.get('error')}")
            self.refresh_data(silent=True)
            return False

        threading.Thread(target=_worker, daemon=True).start()

    def _on_delete_account_clicked(self, btn):
        if not self.selected_account:
            return

        email = self.selected_account["email"]

        dialog = Adw.MessageDialog(
            transient_for=self.main_win,
            heading="Hapus akun?",
            body=f"{email} akan dihapus dari daftar. Akun Google-nya sendiri tidak terpengaruh.",
        )
        dialog.add_response("cancel", "Batal")
        dialog.add_response("delete", "Hapus")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(d, response_id):
            if response_id == "delete":
                try:
                    accounts_list = backend.load_accounts_json()
                    new_list = [acc for acc in accounts_list if acc.get("email") != email]
                    with open(backend.ACCOUNTS_JSON, 'w') as f:
                        json.dump(new_list, f, indent=2, ensure_ascii=False)
                except Exception as e:
                    print(f"Error removing from accounts.json: {e}")

                try:
                    cache = backend.load_cache()
                    if email in cache:
                        del cache[email]
                        backend.save_cache(cache)
                except Exception as e:
                    print(f"Error removing from cache: {e}")

                self._toast(f"Akun {email} dihapus")
                self.selected_account = None
                self.refresh_data()
            d.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def _on_switch_clicked(self, btn):
        if not self.selected_account:
            return

        email = self.selected_account["email"]
        token = self.selected_account.get("token")
        name = self.selected_account.get("name", "")

        try:
            accs = backend.load_accounts_json()
            for acc in accs:
                acc["is_active"] = (acc.get("email") == email)
            with open(backend.ACCOUNTS_JSON, 'w') as f:
                json.dump(accs, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving active flag: {e}")

        if token and backend.IDE_DB_PATH.exists():
            try:
                import sqlite3, base64, re
                def encode_varint(value):
                    ret = bytearray()
                    while value >= 0x80:
                        ret.append((value & 0x7f) | 0x80)
                        value >>= 7
                    ret.append(value & 0x7f)
                    return bytes(ret)

                conn = sqlite3.connect(str(backend.IDE_DB_PATH))
                cursor = conn.cursor()

                cursor.execute("SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.oauthToken'")
                row_oauth = cursor.fetchone()
                if row_oauth:
                    val_bytes = base64.b64decode(row_oauth[0])
                    b64_blocks = re.findall(b'[a-zA-Z0-9+/_-]{12,}=*', val_bytes)
                    for block in b64_blocks:
                        try:
                            padded = block + b'=' * (4 - len(block) % 4)
                            dec = base64.b64decode(padded.replace(b'-', b'+').replace(b'_', b'/'))
                            if dec.startswith(b'\n') and b'ya29.' in dec:
                                new_dec = b'\n' + encode_varint(len(token)) + token.encode('utf-8')
                                new_block_b64 = base64.b64encode(new_dec)
                                val_bytes = val_bytes.replace(block, new_block_b64)
                                new_oauth_b64 = base64.b64encode(val_bytes).decode('utf-8')
                                cursor.execute(
                                    "UPDATE ItemTable SET value = ? WHERE key = 'antigravityUnifiedStateSync.oauthToken'",
                                    (new_oauth_b64,)
                                )
                                break
                        except Exception:
                            continue

                cursor.execute("SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.userStatus'")
                row_status = cursor.fetchone()
                if row_status:
                    val = base64.b64decode(row_status[0])
                    b64_blocks = re.findall(b'[a-zA-Z0-9+/]{12,}=*', val)
                    for block in b64_blocks:
                        try:
                            padded = block + b'=' * (4 - len(block) % 4)
                            decoded = base64.b64decode(padded)
                            if b"@" in decoded:
                                new_b64_email = base64.b64encode(email.encode('utf-8'))
                                val = val.replace(block, new_b64_email)
                                break
                        except Exception:
                            continue
                    new_b64_proto = base64.b64encode(val).decode('utf-8')
                    cursor.execute(
                        "UPDATE ItemTable SET value = ? WHERE key = 'antigravityUnifiedStateSync.userStatus'",
                        (new_b64_proto,)
                    )

                cursor.execute("SELECT value FROM ItemTable WHERE key = 'antigravityAuthStatus'")
                row_auth = cursor.fetchone()
                auth_data = {}
                if row_auth:
                    try:
                        auth_data = json.loads(row_auth[0])
                    except Exception:
                        pass
                auth_data["email"] = email
                auth_data["name"] = name
                auth_data["apiKey"] = token
                cursor.execute(
                    "INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('antigravityAuthStatus', ?)",
                    (json.dumps(auth_data),)
                )

                conn.commit()
                conn.close()

                backend.restart_language_server()
                self._toast(f"Antigravity IDE sekarang memakai {email}")
            except Exception as e:
                self._toast(f"Gagal mengganti akun IDE: {e}")
        else:
            self._toast(f"Akun {email} dipilih")

        self.refresh_data()

    def _on_add_account_clicked(self, btn, login_hint=None):
        # Pakai client OAuth Antigravity agar token bisa dipakai ke API Cloud Code (kuota realtime)
        ag_client = live.antigravity_client()
        if not ag_client:
            self._show_no_client_dialog()
            return
        client_id = ag_client["client_id"]
        client_secret = ag_client["client_secret"]

        import socket
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from urllib.parse import quote, urlparse, parse_qs
        import urllib.request

        def find_free_port():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', 0))
                return s.getsockname()[1]

        port = find_free_port()
        redirect_uri = f"http://127.0.0.1:{port}"
        auth_code_holder = {}

        class OAuthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                if 'code' in params:
                    auth_code_holder['code'] = params['code'][0]
                    self.send_response(200)
                    self.send_header('Content-type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(b"<h1>Login Berhasil!</h1><p>Anda dapat menutup jendela browser ini.</p>")
                else:
                    self.send_response(400)
                    self.end_headers()

            def log_message(self, format, *args):
                pass

        def run_oauth_server():
            try:
                server = HTTPServer(('127.0.0.1', port), OAuthHandler)
                server.timeout = 120
                server.handle_request()
                server.server_close()

                if 'code' not in auth_code_holder:
                    return

                code = auth_code_holder['code']
                token_url = "https://oauth2.googleapis.com/token"
                data = {
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code"
                }
                req = urllib.request.Request(
                    token_url,
                    data=urllib.parse.urlencode(data).encode('utf-8'),
                    headers={"Content-Type": "application/x-www-form-urlencoded"}
                )
                with urllib.request.urlopen(req) as resp:
                    token_resp = json.loads(resp.read().decode('utf-8'))

                refresh_token = token_resp.get("refresh_token")
                access_token = token_resp.get("access_token")

                info_req = urllib.request.Request(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                with urllib.request.urlopen(info_req) as resp:
                    user_info = json.loads(resp.read().decode('utf-8'))

                email = user_info.get("email")
                name = user_info.get("name", email.split('@')[0])

                accounts = backend.load_accounts_json()
                existing = next((a for a in accounts if a.get("email") == email), None)
                cred = {
                    "refresh_token": refresh_token,
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "token_type": "Bearer",
                }
                live.set_access_token(cred, access_token, token_resp.get("expires_in", 3600))
                if existing:
                    existing["name"] = name
                    if refresh_token:
                        existing["credentials"] = json.dumps(cred)
                else:
                    accounts.append({
                        "email": email,
                        "name": name,
                        "is_active": True,
                        "credentials": json.dumps(cred) if refresh_token else "",
                        "added_at": datetime.now(timezone.utc).isoformat(),
                        "last_status": {"valid": True, "plan": "Standard Plan", "quota_groups": []}
                    })

                backend.ACCOUNTS_JSON.parent.mkdir(parents=True, exist_ok=True)
                with open(backend.ACCOUNTS_JSON, "w") as f:
                    json.dump(accounts, f, indent=2, ensure_ascii=False)

                try:
                    backend.fetch_single_account_live(email)
                except Exception:
                    pass
                GLib.idle_add(self._toast, f"Akun {email} ditambahkan")
                GLib.idle_add(self.refresh_data)
            except Exception as e:
                GLib.idle_add(self._toast, f"Gagal login: {e}")

        threading.Thread(target=run_oauth_server, daemon=True).start()

        scopes = [
            "https://www.googleapis.com/auth/cloud-platform",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "openid"
        ]
        scope_str = "+".join(quote(s) for s in scopes)
        auth_url = (
            f"https://accounts.google.com/o/oauth2/v2/auth"
            f"?access_type=offline"
            f"&client_id={client_id}"
            f"&prompt=consent"
            f"&redirect_uri={quote(redirect_uri)}"
            f"&response_type=code"
            f"&scope={scope_str}"
        )
        if login_hint:
            auth_url += f"&login_hint={quote(login_hint)}"
        import webbrowser
        webbrowser.open(auth_url)
        self._toast("Lanjutkan login di browser")
