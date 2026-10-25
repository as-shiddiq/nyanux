"""
podman_projects.py — Logika (tanpa GTK) untuk folder proyek Podman di Nyanux.

- Menyimpan daftar folder proyek di ~/.config/nyanux/podman_projects.json
- Mendeteksi file compose di folder (atau memindai sub-folder)
- Mencocokkan container dengan proyeknya lewat label compose
- Menyusun perintah compose (podman-compose atau `podman compose`)
"""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "nyanux"
PROJECTS_FILE = CONFIG_DIR / "podman_projects.json"

COMPOSE_FILENAMES = [
    "compose.yaml", "compose.yml",
    "docker-compose.yaml", "docker-compose.yml",
    "podman-compose.yaml", "podman-compose.yml",
    "container-compose.yaml", "container-compose.yml",
]
SKIP_DIRS = {".git", "node_modules", "vendor", ".venv", "venv", "__pycache__",
             ".cache", "dist", "build", "target", ".idea", ".vscode"}


# ─── Penyimpanan ──────────────────────────────────────────────────────────────

def load_projects():
    try:
        if PROJECTS_FILE.exists():
            data = json.loads(PROJECTS_FILE.read_text())
            if isinstance(data, list):
                return [p for p in data if isinstance(p, dict) and p.get("path")]
    except Exception as e:
        print(f"[podman_projects] load error: {e}")
    return []


def save_projects(projects):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROJECTS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(projects, indent=2, ensure_ascii=False))
        os.replace(tmp, PROJECTS_FILE)
    except Exception as e:
        print(f"[podman_projects] save error: {e}")


# ─── Deteksi ──────────────────────────────────────────────────────────────────

def find_compose_file(folder):
    folder = Path(folder)
    for name in COMPOSE_FILENAMES:
        f = folder / name
        if f.is_file():
            return f
    return None


def scan_for_projects(folder, max_depth=2):
    """
    Kembalikan list folder (Path) yang berisi file compose.
    Jika folder itu sendiri punya compose → hanya folder itu.
    Jika tidak → pindai sub-folder sampai max_depth.
    """
    root = Path(folder).expanduser().resolve()
    if find_compose_file(root):
        return [root]
    found = []

    def _walk(d, depth):
        if depth > max_depth:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except Exception:
            return
        for sub in entries:
            if not sub.is_dir() or sub.name in SKIP_DIRS or sub.name.startswith("."):
                continue
            if find_compose_file(sub):
                found.append(sub)
            else:
                _walk(sub, depth + 1)

    _walk(root, 1)
    return found


def compose_project_name(folder, compose_file=None):
    """Nama proyek compose: `name:` di file compose, atau nama folder yang dinormalisasi."""
    cf = Path(compose_file) if compose_file else find_compose_file(folder)
    if cf and cf.is_file():
        try:
            for line in cf.read_text(errors="ignore").splitlines():
                m = re.match(r'^name:\s*["\']?([A-Za-z0-9_.-]+)', line)
                if m:
                    return m.group(1).lower()
        except Exception:
            pass
    return Path(folder).name


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def make_project_entry(folder):
    folder = Path(folder).expanduser().resolve()
    cf = find_compose_file(folder)
    return {
        "path": str(folder),
        "name": folder.name,
        "compose_file": cf.name if cf else "",
        "compose_name": compose_project_name(folder, cf),
        "autostart": False,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }


def add_folder(projects, folder):
    """
    Tambah folder (atau hasil scan sub-folder) ke daftar.
    Return (projects_baru, jumlah_ditambah, jumlah_sudah_ada).
    """
    found = scan_for_projects(folder)
    existing = {os.path.realpath(p["path"]) for p in projects}
    added = dup = 0
    for f in found:
        if os.path.realpath(str(f)) in existing:
            dup += 1
            continue
        projects.append(make_project_entry(f))
        existing.add(os.path.realpath(str(f)))
        added += 1
    return projects, added, dup


def remove_project(projects, path):
    rp = os.path.realpath(path)
    return [p for p in projects if os.path.realpath(p["path"]) != rp]


# ─── Pencocokan container ↔ proyek ────────────────────────────────────────────

def container_project_key(container, projects):
    """Return path proyek pemilik container, atau None."""
    labels = container.get("Labels") or {}
    wd = (labels.get("com.docker.compose.project.working_dir")
          or labels.get("io.podman.compose.working_dir") or "")
    pname = (labels.get("com.docker.compose.project")
             or labels.get("io.podman.compose.project") or "")
    if wd:
        rwd = os.path.realpath(wd)
        for p in projects:
            if os.path.realpath(p["path"]) == rwd:
                return p["path"]
    if pname:
        np = _norm(pname)
        for p in projects:
            if np and np in (_norm(p.get("compose_name")), _norm(p.get("name"))):
                return p["path"]
    return None


def compose_label_name(container):
    labels = container.get("Labels") or {}
    return labels.get("com.docker.compose.project") or labels.get("io.podman.compose.project")


# ─── Perintah compose ─────────────────────────────────────────────────────────

def compose_base_cmd():
    if shutil.which("podman-compose"):
        return ["podman-compose"]
    return ["podman", "compose"]


def _compose_file_args(path, config_files=None):
    """Argumen -f untuk proyek, termasuk override port Nyanux (kalau ada)."""
    files = [f for f in (config_files or []) if f]
    ov = override_file(path)
    if ov and not files:
        cf = find_compose_file(path)
        files = [cf.name] if cf else []
    args = []
    for f in files:
        args += ["-f", f]
    if ov and files:
        args += ["-f", str(ov)]
    return args


def compose_cmd(project, action):
    """action: up | down | restart | pull | stop | start"""
    cfgs = [project["compose_file"]] if project.get("compose_file") else None
    base = compose_base_cmd() + _compose_file_args(project["path"], cfgs)
    if action == "up":
        return base + ["up", "-d"]
    if action == "down":
        return base + ["down"]
    return base + [action]


# ─── Ganti port ───────────────────────────────────────────────────────────────
#
# Container compose: port ditimpa lewat file override milik Nyanux (`ports: !override`)
# di ~/.config/nyanux/compose_overrides — file compose proyek tidak diubah.
# Container `podman run`: dibuat ulang dari CreateCommand aslinya dengan -p yang baru.

PORT_OVERRIDES_FILE = CONFIG_DIR / "port_overrides.json"
OVERRIDES_DIR = CONFIG_DIR / "compose_overrides"


def port_mappings(container):
    """Mapping port host aktif: list dict(host_ip, host_port, container_port, protocol, range)."""
    out = []
    for p in container.get("Ports") or []:
        h = p.get("host_port") or p.get("HostPort")
        c = p.get("container_port") or p.get("ContainerPort")
        if not h or not c:
            continue
        out.append({"host_ip": p.get("host_ip") or p.get("HostIP") or "",
                    "host_port": int(h), "container_port": int(c),
                    "protocol": p.get("protocol") or p.get("Protocol") or "tcp",
                    "range": int(p.get("range") or 1)})
    return out


def port_spec(m, host_port=None):
    """Mapping → string gaya `-p` / compose, mis. "127.0.0.1:8090:80/udp"."""
    h, c, r = host_port or m["host_port"], m["container_port"], m.get("range", 1)
    s = f"{h}:{c}" if r == 1 else f"{h}-{h + r - 1}:{c}-{c + r - 1}"
    if m.get("host_ip") and m["host_ip"] not in ("0.0.0.0", "::"):
        s = f"{m['host_ip']}:{s}"
    if m.get("protocol", "tcp") != "tcp":
        s += f"/{m['protocol']}"
    return s


def port_is_free(port):
    import socket
    # Port rootless Podman (pasta) tidak selalu menghalangi bind → cek juga apakah ada yang menjawab
    for host in ("127.0.0.1", "::1"):
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex((host, port)) == 0:
                    return False
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def unprivileged_port_start():
    try:
        return int(Path("/proc/sys/net/ipv4/ip_unprivileged_port_start").read_text().strip())
    except Exception:
        return 1024


def compose_info(container):
    """(working_dir, service, [config_files]) untuk container compose, atau None."""
    labels = container.get("Labels") or {}
    wd = labels.get("com.docker.compose.project.working_dir") or labels.get("io.podman.compose.working_dir")
    svc = labels.get("com.docker.compose.service") or labels.get("io.podman.compose.service")
    if not wd or not svc:
        return None
    cfgs = labels.get("com.docker.compose.project.config_files") or ""
    # Jangan ikutkan override yang tercatat dari recreate sebelumnya (termasuk dari nama lama, Bazu)
    files = [f for f in cfgs.split(",") if f and "/compose_overrides/" not in f]
    return wd, svc, files


def _load_port_overrides():
    try:
        if PORT_OVERRIDES_FILE.exists():
            data = json.loads(PORT_OVERRIDES_FILE.read_text())
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[podman_projects] port overrides load error: {e}")
    return {}


def _override_path(path):
    import hashlib
    rp = os.path.realpath(path)
    return OVERRIDES_DIR / f"{Path(rp).name}-{hashlib.sha1(rp.encode()).hexdigest()[:8]}.yaml"


def override_file(path):
    f = _override_path(path)
    return f if f.is_file() and os.path.realpath(path) in _load_port_overrides() else None


def set_port_override(path, service, specs):
    """Simpan port baru 1 service & tulis ulang file override YAML proyek."""
    data = _load_port_overrides()
    rp = os.path.realpath(path)
    data.setdefault(rp, {})[service] = list(specs)
    lines = ["# Dibuat oleh Nyanux (Ganti Port). Hapus file ini untuk kembali ke port asli.",
             "services:"]
    for svc, ports in sorted(data[rp].items()):
        lines.append(f"  {json.dumps(svc)}:")
        lines.append("    ports: !override")
        lines += [f"      - {json.dumps(p)}" for p in ports]
    OVERRIDES_DIR.mkdir(parents=True, exist_ok=True)
    _override_path(rp).write_text("\n".join(lines) + "\n")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PORT_OVERRIDES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return _override_path(rp)


def compose_recreate_cmd(path, config_files, service):
    """Buat ulang 1 service saja (pakai override) tanpa menyentuh service lain."""
    return (compose_base_cmd() + _compose_file_args(path, config_files)
            + ["up", "-d", "--force-recreate", "--no-deps", service])


def rewrite_publish_args(argv, changes):
    """
    Ganti port host di argumen -p/--publish sebuah perintah `podman run/create`.
    changes: {(container_port, protocol): host_port_baru}
    Return (argv_baru, set kunci yang berhasil diganti).
    """
    out, done = [], set()

    def _sub(val):
        spec, _, proto = val.partition("/")
        proto = proto or "tcp"
        parts = spec.split(":")
        if len(parts) < 2 or not parts[-1].isdigit():
            return val
        key = (int(parts[-1]), proto)
        if key not in changes:
            return val
        parts[-2] = str(changes[key])
        done.add(key)
        return ":".join(parts) + (f"/{proto}" if val.count("/") else "")

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-p", "--publish") and i + 1 < len(argv):
            out += [a, _sub(argv[i + 1])]
            i += 2
            continue
        if a.startswith("--publish="):
            out.append("--publish=" + _sub(a[len("--publish="):]))
        elif a.startswith("-p") and len(a) > 2 and not a.startswith("--"):
            out.append("-p" + _sub(a[2:]))
        else:
            out.append(a)
        i += 1
    return out, done


# ─── Helpers tampilan ─────────────────────────────────────────────────────────

def short_image(image):
    s = image or ""
    s = s.split("@")[0]
    parts = s.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    if parts and parts[0] == "library":
        parts = parts[1:]
    return "/".join(parts) or image or "-"


def pretty_path(path):
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path
