"""
themes.py — Tema kucing untuk Nyanux.

Setiap tema = satu jenis kucing:
  • warna aksen + nuansa latar (terang & gelap) lewat variabel CSS libadwaita
  • warna bulu kucing di ilustrasi (halaman kosong, kucing mengintip di sidebar)

Pemakaian:
    themes.apply("oren")                         # ganti tema
    themes.illustration("sleep", 160)            # Gdk.Paintable sesuai tema aktif
    themes.on_change(callback)                   # dipanggil setelah tema berganti
"""

import math
import random
from string import Template

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk

# ─── Bulu kucing ─────────────────────────────────────────────────────────────
# p1/p2 = bercak (belang tiga), stripe = loreng (oren). Kalau tidak dipakai, isi sama
# dengan bulu / opasitas 0 supaya template tetap satu.
_FUR = {
    "abu":    dict(fur="#8b8a92", fur_dark="#5e5c64", muzzle="#c0bfbc", ear="#f8a5b0",
                   eye="#241f31", pupil="#241f31", line="#3d3846", p1="#8b8a92", p2="#8b8a92",
                   stripe="#5e5c64", stripe_op="0"),
    "oren":   dict(fur="#f2a654", fur_dark="#d9822b", muzzle="#fde7c8", ear="#f8a5b0",
                   eye="#3d2b1f", pupil="#3d2b1f", line="#5c3a1e", p1="#f2a654", p2="#f2a654",
                   stripe="#c8661c", stripe_op="0.75"),
    "hitam":  dict(fur="#34323c", fur_dark="#1f1e25", muzzle="#4a4754", ear="#8a5f72",
                   eye="#f5c211", pupil="#1f1e25", line="#a8a3b5", p1="#34323c", p2="#34323c",
                   stripe="#1f1e25", stripe_op="0"),
    "belang": dict(fur="#f7f2ea", fur_dark="#d9cfc2", muzzle="#ffffff", ear="#f8a5b0",
                   eye="#3d2b1f", pupil="#3d2b1f", line="#5e5c64", p1="#e8913c", p2="#3a3540",
                   stripe="#d9cfc2", stripe_op="0"),
}

# ─── Tema ────────────────────────────────────────────────────────────────────
# (kunci, nama, bulu, aksen, teks di atas aksen, {terang}, {gelap})
THEMES = [
    ("system", "Bawaan sistem", "abu", None, None, None, None),
    ("oren", "Oren", "oren", "#e4782f", "#ffffff",
     dict(window="#fbf4ec", view="#fffaf5", sidebar="#f5e8da", header="#f8eee3"),
     dict(window="#27221e", view="#201c19", sidebar="#2e2722", header="#2e2722")),
    ("abu", "Abu", "abu", "#6b80b5", "#ffffff",
     dict(window="#f3f4f7", view="#fbfbfd", sidebar="#e8eaf1", header="#eef0f5"),
     dict(window="#222329", view="#1c1d22", sidebar="#2a2c33", header="#2a2c33")),
    ("hitam", "Hitam", "hitam", "#d4a21c", "#1d1c22",
     dict(window="#f5f4f1", view="#fcfbf8", sidebar="#ebe8e1", header="#f0eee9"),
     dict(window="#1b1a1f", view="#151418", sidebar="#232128", header="#232128")),
    ("belang", "Belang tiga", "belang", "#cf5f78", "#ffffff",
     dict(window="#fbf3f1", view="#fffafa", sidebar="#f4e4e0", header="#f8ece9"),
     dict(window="#282124", view="#211b1e", sidebar="#30272b", header="#30272b")),
]
THEME_KEYS = [t[0] for t in THEMES]

_provider = None
_current = "system"
_listeners = []
_cache = {}
_decor = []          # gambar hiasan (kucing mengintip) yang bisa disembunyikan
_decor_visible = True


class _ScaledPaintable(GObject.Object, Gdk.Paintable):
    """Tekstur 2x (tajam di HiDPI) dengan ukuran tampil 1x."""

    def __init__(self, texture, width, height):
        super().__init__()
        self._tex, self._w, self._h = texture, width, height

    def do_snapshot(self, snapshot, width, height):
        self._tex.snapshot(snapshot, width, height)

    def do_get_intrinsic_width(self):
        return self._w

    def do_get_intrinsic_height(self):
        return self._h

    def do_get_intrinsic_aspect_ratio(self):
        return self._w / self._h


def _palette_css(p):
    return (f"  --window-bg-color: {p['window']};\n"
            f"  --view-bg-color: {p['view']};\n"
            f"  --sidebar-bg-color: {p['sidebar']};\n"
            f"  --headerbar-bg-color: {p['header']};\n")


def _theme_css(key, is_dark):
    # Bukan @media prefers-color-scheme: itu mengikuti sistem, sedangkan mode Nyanux
    # bisa dipaksa terang/gelap. Jadi palet dipilih di sini dan dipasang ulang saat mode berubah.
    t = next((t for t in THEMES if t[0] == key), THEMES[0])
    _key, _name, _fur, accent, accent_fg, light, dark = t
    if not accent:
        return ""
    return (":root {\n"
            f"  --accent-bg-color: {accent};\n"
            f"  --accent-fg-color: {accent_fg};\n"
            + _palette_css(dark if is_dark else light) +
            "}\n")


def _reload_css(*_):
    if _provider is not None:
        _provider.load_from_string(_theme_css(_current, Adw.StyleManager.get_default().get_dark()))


def current():
    return _current


def apply(key):
    """Pasang tema (CSS + bulu ilustrasi) lalu beri tahu semua pendengar."""
    global _provider, _current
    _current = key if key in THEME_KEYS else "system"
    display = Gdk.Display.get_default()
    if _provider is None:
        _provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, _provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 5)
        Adw.StyleManager.get_default().connect("notify::dark", _reload_css)
    _reload_css()
    for cb in list(_listeners):
        try:
            cb()
        except Exception as e:
            print(f"[themes] listener gagal: {e}")


def on_change(cb):
    _listeners.append(cb)


def fur_key():
    return next((t[2] for t in THEMES if t[0] == _current), "abu")


# ─── Ilustrasi ───────────────────────────────────────────────────────────────

_BOX_DEFS = """
  <linearGradient id="box" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#e3aa62"/><stop offset="1" stop-color="#c88540"/>
  </linearGradient>
  <linearGradient id="flap" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#f0c27f"/><stop offset="1" stop-color="#d6994f"/>
  </linearGradient>
"""

# Kucing tidur melingkar di kardus kosong — "belum ada apa-apa"
_SLEEP = Template("""<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160">
<defs>""" + _BOX_DEFS + """</defs>
<ellipse cx="80" cy="146" rx="54" ry="6" fill="#000" opacity="0.12"/>
<path d="M34 82 44 68h72l10 14z" fill="#a8692e"/>
<rect x="28" y="78" width="104" height="14" rx="3" fill="#6f4520"/>
<path d="M118 84q20-2 18-20q-2-10-11-8" fill="none" stroke="$fur" stroke-width="9" stroke-linecap="round"/>
<path d="M131 60q-4-5-7-4" fill="none" stroke="$fur_dark" stroke-width="9" stroke-linecap="round"/>
<ellipse cx="88" cy="82" rx="40" ry="18" fill="$fur"/>
<path d="M84 68v10M96 67v11M108 70v9" stroke="$stripe" stroke-width="4" stroke-linecap="round" opacity="$stripe_op"/>
<ellipse cx="100" cy="76" rx="14" ry="8" fill="$p1"/>
<path d="M40 66 41 46 55 57z" fill="$fur"/><path d="M57 54 70 44 72 61z" fill="$fur"/>
<path d="M43 61 44 51 51 57z" fill="$ear"/><path d="M60 55 67 50 68 59z" fill="$ear"/>
<circle cx="56" cy="70" r="18" fill="$fur"/>
<ellipse cx="62" cy="62" rx="7" ry="5" fill="$p2"/>
<path d="M50 56v6M56 54v7M62 56v6" stroke="$stripe" stroke-width="3" stroke-linecap="round" opacity="$stripe_op"/>
<path d="M45 71q4 3 8 0M59 71q4 3 8 0" fill="none" stroke="$line" stroke-width="2.2" stroke-linecap="round"/>
<ellipse cx="56" cy="79" rx="7" ry="4.5" fill="$muzzle"/>
<path d="M54 76.5h4l-2 2z" fill="#e66b7c"/>
<rect x="24" y="86" width="112" height="50" rx="8" fill="#8f5a2a"/>
<rect x="24" y="84" width="112" height="48" rx="8" fill="url(#box)"/>
<rect x="74" y="84" width="12" height="48" fill="#f3d39d" opacity="0.55"/>
<path d="M24 88 36 86 22 66 10 74z" fill="url(#flap)"/><path d="M136 88 124 86 138 66 150 74z" fill="url(#flap)"/>
<g fill="none" stroke="#8f9bb3" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" opacity="0.8">
  <path d="M100 44h8l-8 9h8"/><path d="M114 28h10l-10 11h10"/><path d="M130 10h12l-12 13h12"/>
</g>
</svg>""")

# Kucing duduk, kepala miring, tanda tanya — "tidak ketemu" / "ada masalah"
_CURIOUS = Template("""<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160">
<ellipse cx="80" cy="148" rx="44" ry="6" fill="#000" opacity="0.12"/>
<path d="M106 136q32 4 30-24q-1-13-11-13" fill="none" stroke="$fur" stroke-width="10" stroke-linecap="round"/>
<ellipse cx="80" cy="112" rx="34" ry="32" fill="$fur"/>
<ellipse cx="94" cy="104" rx="13" ry="10" fill="$p1"/>
<path d="M58 100q-4 10 0 22M102 100q4 10 0 22" fill="none" stroke="$stripe" stroke-width="4" stroke-linecap="round" opacity="$stripe_op"/>
<ellipse cx="80" cy="120" rx="16" ry="20" fill="$muzzle"/>
<ellipse cx="68" cy="142" rx="10" ry="6" fill="$muzzle"/><ellipse cx="92" cy="142" rx="10" ry="6" fill="$muzzle"/>
<g transform="rotate(-12 80 66)">
  <path d="M54 58 58 26 76 44z" fill="$fur"/><path d="M106 58 102 26 84 44z" fill="$fur"/>
  <path d="M59 34 61 50 70 44z" fill="$ear"/><path d="M101 34 99 50 90 44z" fill="$ear"/>
  <ellipse cx="80" cy="66" rx="31" ry="26" fill="$fur"/>
  <ellipse cx="66" cy="52" rx="9" ry="6" fill="$p2"/>
  <path d="M74 44v8M80 42v9M86 44v8" stroke="$stripe" stroke-width="3" stroke-linecap="round" opacity="$stripe_op"/>
  <ellipse cx="68" cy="64" rx="5.5" ry="6.5" fill="$eye"/><ellipse cx="92" cy="64" rx="5.5" ry="6.5" fill="$eye"/>
  <ellipse cx="68" cy="64" rx="1.8" ry="5" fill="$pupil"/><ellipse cx="92" cy="64" rx="1.8" ry="5" fill="$pupil"/>
  <circle cx="69.8" cy="61.5" r="1.7" fill="#fff"/><circle cx="93.8" cy="61.5" r="1.7" fill="#fff"/>
  <ellipse cx="80" cy="76" rx="10" ry="6.5" fill="$muzzle"/>
  <path d="M77 72.5h6l-3 3z" fill="#e66b7c"/>
  <path d="M80 75.5v2M80 77.5q-2.5 2.5-5 1M80 77.5q2.5 2.5 5 1" fill="none" stroke="$line" stroke-width="1.5" stroke-linecap="round"/>
</g>
<g fill="none" stroke="#8f9bb3" stroke-width="5" stroke-linecap="round" opacity="0.85">
  <path d="M118 22q0-11 11-11t10 10q0 7-9 10v6"/>
</g>
<circle cx="130" cy="46" r="3.2" fill="#8f9bb3" opacity="0.85"/>
</svg>""")

# Kepala + kaki mengintip dari tepi bawah — hiasan sidebar
_PEEK = Template("""<svg xmlns="http://www.w3.org/2000/svg" width="88" height="40" viewBox="16 0 88 40">
<path d="M36 28 40 4 55 19z" fill="$fur"/><path d="M84 28 80 4 65 19z" fill="$fur"/>
<path d="M41 11 43 22 50 18z" fill="$ear"/><path d="M79 11 77 22 70 18z" fill="$ear"/>
<ellipse cx="60" cy="42" rx="29" ry="25" fill="$fur"/>
<ellipse cx="48" cy="26" rx="8" ry="5.5" fill="$p2"/><ellipse cx="74" cy="34" rx="7" ry="5" fill="$p1"/>
<path d="M54 20v6M60 18v7M66 20v6" stroke="$stripe" stroke-width="2.6" stroke-linecap="round" opacity="$stripe_op"/>
<ellipse cx="49" cy="32" rx="4.2" ry="5" fill="$eye"/><ellipse cx="71" cy="32" rx="4.2" ry="5" fill="$eye"/>
<ellipse cx="49" cy="32" rx="1.4" ry="4" fill="$pupil"/><ellipse cx="71" cy="32" rx="1.4" ry="4" fill="$pupil"/>
<circle cx="50.4" cy="30" r="1.3" fill="#fff"/><circle cx="72.4" cy="30" r="1.3" fill="#fff"/>
<ellipse cx="32" cy="40" rx="10" ry="6" fill="$muzzle"/><ellipse cx="88" cy="40" rx="10" ry="6" fill="$muzzle"/>
<path d="M29 36v4M35 36v4M85 36v4M91 36v4" stroke="$fur_dark" stroke-width="1.2" stroke-linecap="round" opacity="0.6"/>
</svg>""")

# Satu jejak kaki (viewBox 24×24): bantalan + 4 jari
_PAW_SHAPE = (
    '<path d="M12 12.6c-3.1 0-6.2 3.3-6.2 5.8 0 1.7 1.3 2.5 2.9 2.5 1.2 0 2.1-.6 3.3-.6s2.1.6 3.3.6'
    'c1.6 0 2.9-.8 2.9-2.5 0-2.5-3.1-5.8-6.2-5.8z"/>'
    '<ellipse cx="5.6" cy="10.4" rx="2" ry="2.5" transform="rotate(-22 5.6 10.4)"/>'
    '<ellipse cx="9.5" cy="6.6" rx="2.1" ry="2.7" transform="rotate(-8 9.5 6.6)"/>'
    '<ellipse cx="14.5" cy="6.6" rx="2.1" ry="2.7" transform="rotate(8 14.5 6.6)"/>'
    '<ellipse cx="18.4" cy="10.4" rx="2" ry="2.5" transform="rotate(22 18.4 10.4)"/>'
)

_PAW = Template('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
                '<g fill="$color">' + _PAW_SHAPE + '</g></svg>')

# Ukuran kanvas jejak kaki (koordinat SVG); digambar memenuhi area kosong sidebar
TRAIL_W, TRAIL_H = 280, 520


def _paw_trail_svg(color, seed=11):
    """Jejak kucing berjalan dari dekat kucing (bawah) ke atas dengan arah yang berubah-ubah
    sedikit tiap langkah (random walk), berbelok balik kalau mendekati tepi. Kiri-kanan
    bergantian, sudut mengikuti arah jalan, ukuran & jarak langkah bervariasi, dan makin
    ke atas makin pudar supaya tidak menimpa teks daftar. Seed tetap → posisi tidak berubah."""
    rnd = random.Random(seed)
    x, y = TRAIL_W * 0.55, TRAIL_H - 18
    heading = rnd.uniform(-20, 20)            # derajat; 0 = lurus ke atas
    prints, side = [], 1
    while y > 16:
        heading += rnd.uniform(-26, 26)
        if x < 60:
            heading += 22                     # terlalu ke kiri → belok kanan
        elif x > TRAIL_W - 60:
            heading -= 22                     # terlalu ke kanan → belok kiri
        heading = max(-65, min(65, heading))
        step = 44 + rnd.uniform(-8, 12)
        rad = math.radians(heading)
        x += math.sin(rad) * step
        y -= math.cos(rad) * step
        gap = 11 + rnd.uniform(-3, 3)         # jarak kaki kiri-kanan
        px = x + math.cos(rad) * gap * side + rnd.uniform(-2, 2)
        py = y + math.sin(rad) * gap * side + rnd.uniform(-2, 2)
        angle = heading + rnd.uniform(-15, 15) + side * 7
        scale = rnd.uniform(0.85, 1.15)
        t = 1 - y / TRAIL_H                   # 0 dekat kucing → 1 di atas
        opacity = 0.32 * (1 - t) ** 1.4 + rnd.uniform(-0.02, 0.02)
        if opacity > 0.03:
            prints.append((px, py, angle, scale, opacity))
        side = -side

    uses = "".join(
        f'<use href="#p" opacity="{o:.2f}" transform="translate({x - 12:.1f} {y - 12:.1f}) '
        f'rotate({a:.1f} 12 12) translate({12 - 12 * s:.1f} {12 - 12 * s:.1f}) scale({s:.2f})"/>'
        for x, y, a, s, o in prints)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{TRAIL_W}" height="{TRAIL_H}" '
            f'viewBox="0 0 {TRAIL_W} {TRAIL_H}"><defs><g id="p">{_PAW_SHAPE}</g></defs>'
            f'<g fill="{color}">{uses}</g></svg>')


_TEMPLATES = {"sleep": _SLEEP, "curious": _CURIOUS, "peek": _PEEK}

# Warna jejak kaki per tema (pakai aksen tema; "system" netral)
_PAW_COLOR = {"system": "#9a9996"}


def _paw_color():
    t = next((t for t in THEMES if t[0] == _current), THEMES[0])
    return _PAW_COLOR.get(_current) or t[3] or "#9a9996"


def illustration_svg(name, fur=None):
    if name == "paws":
        return _paw_trail_svg(_paw_color())
    return _TEMPLATES[name].substitute(_FUR[fur or fur_key()])


def _render(svg, width):
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(svg.encode()))
    pb = GdkPixbuf.Pixbuf.new_from_stream_at_scale(stream, width * 2, -1, True, None)
    return _ScaledPaintable(Gdk.Texture.new_for_pixbuf(pb), width,
                            round(width * pb.get_height() / pb.get_width()))


def paw_icon(color, size=12):
    """Ikon jejak kaki satu warna (untuk status, dsb.)."""
    key = ("paw", color, size)
    if key not in _cache:
        _cache[key] = _render(_PAW.substitute(color=color), size)
    return _cache[key]


def illustration(name, width):
    """Gdk.Paintable ilustrasi `name` (sleep | curious | peek) selebar `width` px, warna tema aktif.
    Dirender 2x agar tetap tajam di layar HiDPI, tapi ukuran tampilnya tetap `width`."""
    fur = fur_key()
    key = (name, fur, _paw_color(), width)
    if key not in _cache:
        _cache[key] = _render(illustration_svg(name, fur), width)
    return _cache[key]


def set_decor_visible(visible):
    global _decor_visible
    _decor_visible = bool(visible)
    for pic in _decor:
        pic.set_visible(_decor_visible)


def sidebar_decor(list_widget):
    """Bungkus daftar sidebar: jejak kaki samar di sudut + kucing besar mengintip di bawah.
    Kembalikan widget pengganti untuk ditaruh di sidebar."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
    overlay = Gtk.Overlay(vexpand=True)
    overlay.set_child(list_widget)
    paws = themed_picture("paws", TRAIL_W, TRAIL_H, decor=True)
    paws.set_size_request(-1, -1)
    paws.set_halign(Gtk.Align.FILL)
    paws.set_valign(Gtk.Align.END)
    paws.set_can_target(False)          # klik tembus ke daftar
    overlay.add_overlay(paws)
    box.append(overlay)
    # Kucing selebar sidebar; menyusut proporsional kalau sidebar sempit
    peek = themed_picture("peek", 300, 136, decor=True)
    peek.set_size_request(180, 82)
    peek.set_halign(Gtk.Align.FILL)
    peek.set_can_target(False)
    box.append(peek)
    return box


def themed_picture(name, width, height, decor=False):
    """Gtk.Picture yang otomatis diwarnai ulang saat tema berganti.
    decor=True → ikut disembunyikan saat "Hiasan kucing" dimatikan."""
    pic = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN, valign=Gtk.Align.END)
    pic.set_size_request(width, height)
    if decor:
        _decor.append(pic)
        pic.set_visible(_decor_visible)

    def _refresh():
        pic.set_paintable(illustration(name, width))
    _refresh()
    on_change(_refresh)
    return pic
