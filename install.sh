#!/usr/bin/env bash
# Pasang Nyanux untuk user saat ini: perintah `nyanux`, launcher menu aplikasi, dan ikon.
# Pemakaian: ./install.sh            → pasang / perbarui
#            ./install.sh --uninstall → hapus (pengaturan di ~/.config/nyanux tetap ada)
set -euo pipefail

APP_ID="io.github.asshiddiq.Nyanux"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin/nyanux"
DESKTOP="$HOME/.local/share/applications/$APP_ID.desktop"
ICONS="$HOME/.local/share/icons/hicolor"

refresh_caches() {
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$BIN" "$DESKTOP" "$ICONS/scalable/apps/$APP_ID.svg" "$ICONS/symbolic/apps/$APP_ID-symbolic.svg"
    refresh_caches
    echo "Nyanux dihapus. Pengaturan masih ada di ~/.config/nyanux"
    exit 0
fi

python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null || {
    echo "GTK 4 / libadwaita untuk Python (PyGObject) belum terpasang." >&2
    exit 1
}

mkdir -p "$(dirname "$BIN")" "$(dirname "$DESKTOP")" "$ICONS/scalable/apps" "$ICONS/symbolic/apps"

cat > "$BIN" <<EOF
#!/usr/bin/env bash
exec python3 "$SRC/nyanux.py" "\$@"
EOF
chmod +x "$BIN"

install -m 644 "$SRC/icons/nyanux.svg" "$ICONS/scalable/apps/$APP_ID.svg"
install -m 644 "$SRC/icons/nyanux-symbolic.svg" "$ICONS/symbolic/apps/$APP_ID-symbolic.svg"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Nyanux
GenericName=Linux Helper
Comment=Biar nggak pusing ngurus Linux tanpa GUI: container Podman & kuota AI
Exec=$BIN
Icon=$APP_ID
Terminal=false
Categories=Development;
Keywords=podman;container;docker;compose;antigravity;quota;kuota;
StartupWMClass=$APP_ID
EOF

refresh_caches
echo "Nyanux terpasang. Buka dari menu aplikasi atau jalankan: nyanux"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "Catatan: tambahkan ~/.local/bin ke PATH agar perintah 'nyanux' dikenali." ;;
esac
