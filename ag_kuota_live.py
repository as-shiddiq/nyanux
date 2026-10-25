"""
ag_kuota_live.py — Cek kuota Antigravity / Gemini secara REALTIME langsung ke API Google
(cara yang sama dengan Antigravity Tools / Antigravity Manager), tanpa perlu IDE / browser terbuka.

Alur per akun:
  1. Pastikan access token masih valid → kalau kedaluwarsa, refresh pakai refresh_token.
  2. POST  {host}/v1internal:loadCodeAssist       → project id + tier (plan)
  3. POST  {host}/v1internal:fetchAvailableModels → models[*].quotaInfo.{remainingFraction, resetTime}
  4. Jika 401 → refresh token sekali lalu ulangi.

Semua fungsi di sini murni (tidak menyentuh GTK) sehingga aman dipanggil dari thread.
"""

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Host Cloud Code yang dipakai Antigravity. Prod dulu, lalu "daily" sebagai cadangan.
CLOUDCODE_HOSTS = [
    "https://cloudcode-pa.googleapis.com",
    "https://daily-cloudcode-pa.sandbox.googleapis.com",
]
USER_AGENT = os.environ.get("NYANUX_AG_USER_AGENT",
                            os.environ.get("BAZU_AG_USER_AGENT", "antigravity/1.11.9 linux/amd64"))
TOKEN_URI = "https://oauth2.googleapis.com/token"
HTTP_TIMEOUT = 12

# Client OAuth TIDAK disimpan di kode. Sumbernya (urutan prioritas):
#   1. env ANTIGRAVITY_OAUTH_CLIENT_ID + ANTIGRAVITY_OAUTH_CLIENT_SECRET
#   2. ~/.config/nyanux/oauth_clients.json → {"clients": [{"name", "client_id", "client_secret"}, ...]}
# Client pertama dipakai untuk login baru. Refresh token hanya bisa di-refresh oleh client yang
# menerbitkannya, jadi kalau credentials tidak menyimpan client_id kita coba satu per satu.
OAUTH_CLIENTS_FILE = Path.home() / ".config" / "nyanux" / "oauth_clients.json"


def load_oauth_clients():
    clients = []

    def _add(cid, secret, name=""):
        if cid and secret and cid not in [c["client_id"] for c in clients]:
            clients.append({"name": name, "client_id": cid, "client_secret": secret})

    _add(os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_ID"), os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET"), "env")
    try:
        if OAUTH_CLIENTS_FILE.exists():
            data = json.loads(OAUTH_CLIENTS_FILE.read_text())
            for c in (data.get("clients") or []) if isinstance(data, dict) else []:
                if isinstance(c, dict):
                    _add(c.get("client_id"), c.get("client_secret"), c.get("name", ""))
    except Exception as e:
        print(f"[live] gagal membaca {OAUTH_CLIENTS_FILE}: {e}")
    return clients


def antigravity_client():
    """Client untuk login akun baru, atau None kalau belum diatur."""
    clients = load_oauth_clients()
    return clients[0] if clients else None


NO_CLIENT_MSG = ("Client OAuth belum diatur. Isi ~/.config/nyanux/oauth_clients.json "
                 "atau env ANTIGRAVITY_OAUTH_CLIENT_ID / _SECRET (lihat README).")

# Model internal yang tidak relevan untuk ditampilkan
_HIDDEN_MODEL_PATTERNS = ("tab_", "chat_", "embedding", "autocomplete", "internal")


class LiveQuotaError(Exception):
    """Error dengan pesan yang ramah untuk ditampilkan di UI."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# ─── Helpers credentials ──────────────────────────────────────────────────────

def parse_credentials(cred):
    if not cred:
        return {}
    if isinstance(cred, dict):
        return dict(cred)
    try:
        return json.loads(cred)
    except Exception:
        return {}


def get_access_token(cred):
    return cred.get("token") or cred.get("access_token") or ""


def get_expiry_ts(cred):
    """Kembalikan expiry (epoch detik) dari format apa pun yang tersimpan, atau 0."""
    exp_ms = cred.get("expiry_date")
    if exp_ms:
        try:
            return float(exp_ms) / 1000.0
        except Exception:
            pass
    exp = cred.get("expiry")
    if exp:
        try:
            return datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return 0


def token_is_fresh(cred, margin_sec=120):
    tok = get_access_token(cred)
    if not tok:
        return False
    exp = get_expiry_ts(cred)
    if not exp:
        return False  # tidak tahu kapan habis → anggap perlu refresh (kalau bisa)
    return exp - margin_sec > time.time()


def set_access_token(cred, token, expires_in):
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in or 3600))
    cred["token"] = token
    cred["access_token"] = token
    cred["expiry"] = expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cred["expiry_date"] = int(expiry.timestamp() * 1000)
    return cred


# ─── HTTP ─────────────────────────────────────────────────────────────────────

def _http_post_json(url, body, access_token=None, timeout=HTTP_TIMEOUT):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except Exception:
            payload = {}
        return e.code, payload


def refresh_token_any_client(cred):
    """
    Refresh access token. Pakai client_id dari credentials kalau ada, kalau tidak coba
    semua client dari load_oauth_clients(). Return (access_token, expires_in, client_dict) atau raise.
    """
    refresh_token = cred.get("refresh_token")
    if not refresh_token:
        raise LiveQuotaError("Tidak ada refresh token — login ulang akun ini (Tambah Akun).", "no_refresh")

    candidates = []
    if cred.get("client_id") and cred.get("client_secret"):
        candidates.append({"client_id": cred["client_id"], "client_secret": cred["client_secret"]})
    for c in load_oauth_clients():
        if c["client_id"] not in [x["client_id"] for x in candidates]:
            candidates.append(c)

    if not candidates:
        raise LiveQuotaError(NO_CLIENT_MSG, "no_client")

    last_err = "unknown"
    for client in candidates:
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
        }).encode("utf-8")
        req = urllib.request.Request(cred.get("token_uri") or TOKEN_URI, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(),
                                        timeout=HTTP_TIMEOUT) as resp:
                result = json.loads(resp.read())
                if result.get("access_token"):
                    return result["access_token"], result.get("expires_in", 3600), client
        except urllib.error.HTTPError as e:
            try:
                last_err = json.loads(e.read()).get("error", f"HTTP {e.code}")
            except Exception:
                last_err = f"HTTP {e.code}"
        except Exception as e:
            last_err = str(e)

    if last_err == "invalid_grant":
        raise LiveQuotaError("Refresh token kedaluwarsa / dicabut — login ulang akun ini.", "invalid_grant")
    raise LiveQuotaError(f"Gagal refresh token ({last_err}).", "refresh_failed")


# ─── Cloud Code API ───────────────────────────────────────────────────────────

def load_code_assist(access_token):
    """Return (project_id, plan_name, host) atau raise LiveQuotaError."""
    body = {"metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED",
                         "pluginType": "GEMINI"}}
    last = None
    for host in CLOUDCODE_HOSTS:
        try:
            status, data = _http_post_json(f"{host}/v1internal:loadCodeAssist", body, access_token)
        except Exception as e:
            last = LiveQuotaError(f"Jaringan: {e}", "network")
            continue
        if status == 401:
            raise LiveQuotaError("Token ditolak (401).", 401)
        if status == 200:
            proj = data.get("cloudaicompanionProject")
            if isinstance(proj, dict):
                proj = proj.get("id") or proj.get("name")
            tier = data.get("paidTier") or data.get("currentTier") or {}
            plan = tier.get("name") or tier.get("id") or ""
            return proj, plan, host
        last = LiveQuotaError(_api_err_msg(status, data), status)
    raise last or LiveQuotaError("loadCodeAssist gagal", "unknown")


def fetch_available_models(access_token, project_id=None, preferred_host=None):
    body = {"project": project_id} if project_id else {}
    hosts = list(CLOUDCODE_HOSTS)
    if preferred_host in hosts:
        hosts.remove(preferred_host)
        hosts.insert(0, preferred_host)
    last = None
    for host in hosts:
        try:
            status, data = _http_post_json(f"{host}/v1internal:fetchAvailableModels", body, access_token)
        except Exception as e:
            last = LiveQuotaError(f"Jaringan: {e}", "network")
            continue
        if status == 401:
            raise LiveQuotaError("Token ditolak (401).", 401)
        if status == 200:
            return data
        last = LiveQuotaError(_api_err_msg(status, data), status)
    raise last or LiveQuotaError("fetchAvailableModels gagal", "unknown")


def _api_err_msg(status, data):
    msg = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message", "")
        elif err:
            msg = str(err)
    if status == 403:
        return f"Akses ditolak (403){': ' + msg[:80] if msg else ''}"
    return f"HTTP {status}{': ' + msg[:80] if msg else ''}"


# ─── Parsing ──────────────────────────────────────────────────────────────────

def _iter_models(data):
    """Normalisasi respons fetchAvailableModels (dict ATAU list) → (id, info)."""
    models = data.get("models") if isinstance(data, dict) else None
    if isinstance(models, dict):
        for mid, info in models.items():
            if isinstance(info, dict):
                yield mid, info
    elif isinstance(models, list):
        for info in models:
            if isinstance(info, dict):
                yield info.get("name") or info.get("model") or info.get("id") or "?", info


def _reset_to_iso(v):
    if not v:
        return ""
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            return ""
    return str(v)


def _pretty_model_name(mid, info):
    name = info.get("displayName") or info.get("label")
    if name:
        return name
    s = mid.split("/")[-1]
    s = re.sub(r"[-_]+", " ", s).strip()
    return " ".join(w.upper() if w in ("gpt", "oss") else w.capitalize() for w in s.split())


def models_to_user_status(data, email="", plan=""):
    """
    Konversi respons fetchAvailableModels ke bentuk GetUserStatus agar bisa diproses
    oleh parser yang sudah ada (parse_quota_from_response di ag_kuota_backend).
    """
    configs_all, configs_main = [], []
    for mid, info in _iter_models(data):
        qi = info.get("quotaInfo") or info.get("quota") or {}
        if not qi:
            continue
        frac = qi.get("remainingFraction")
        if frac is None and "percentage" in qi:
            frac = float(qi["percentage"]) / 100.0
        if frac is None:
            frac = 0.0  # quotaInfo tanpa remainingFraction = kuota habis
        cfg = {
            "label": _pretty_model_name(mid, info),
            "quotaInfo": {"remainingFraction": float(frac),
                          "resetTime": _reset_to_iso(qi.get("resetTime"))},
        }
        configs_all.append(cfg)
        low = mid.lower()
        if any(k in low for k in ("gemini", "claude", "gpt")) and \
                not any(low.startswith(p) or f"/{p}" in low for p in _HIDDEN_MODEL_PATTERNS):
            configs_main.append(cfg)

    configs = configs_main or configs_all
    configs.sort(key=lambda c: c["label"])
    return {"userStatus": {
        "email": email,
        "subscriptionType": plan or "",
        "cascadeModelConfigData": {"clientModelConfigs": configs},
    }}


# ─── Entry point ──────────────────────────────────────────────────────────────

def fetch_live_quota_for_account(account):
    """
    account: dict dari accounts.json.
    Return dict:
      { "raw_status": <GetUserStatus-like dict>, "plan": str,
        "credentials": <dict credentials terbaru>, "cred_changed": bool,
        "project_id": str|None }
    Raise LiveQuotaError jika gagal.
    """
    email = account.get("email", "")
    cred = parse_credentials(account.get("credentials"))
    if not cred:
        raise LiveQuotaError("Akun belum punya kredensial — login dulu.", "no_cred")

    changed = False

    def _do_refresh():
        nonlocal changed
        tok, exp, client = refresh_token_any_client(cred)
        set_access_token(cred, tok, exp)
        cred["client_id"] = client["client_id"]
        cred["client_secret"] = client["client_secret"]
        changed = True
        return tok

    if token_is_fresh(cred):
        token = get_access_token(cred)
    elif cred.get("refresh_token"):
        token = _do_refresh()
    else:
        token = get_access_token(cred)  # session token dari IDE, coba saja
        if not token:
            raise LiveQuotaError("Token kosong — login ulang akun ini.", "no_token")

    def _query(tok):
        project_id = cred.get("project_id")
        plan = cred.get("plan_name", "")
        host = None
        if not project_id:
            project_id, plan, host = load_code_assist(tok)
            if project_id:
                cred["project_id"] = project_id
            if plan:
                cred["plan_name"] = plan
            nonlocal changed
            changed = True
        data = fetch_available_models(tok, project_id, host)
        return data, plan, project_id

    try:
        data, plan, project_id = _query(token)
    except LiveQuotaError as e:
        if e.code == 401 and cred.get("refresh_token"):
            token = _do_refresh()
            data, plan, project_id = _query(token)
        elif e.code == 401:
            raise LiveQuotaError("Token sesi IDE kedaluwarsa & tidak ada refresh token — "
                                 "login ulang via Tambah Akun.", 401)
        else:
            raise

    raw = models_to_user_status(data, email=email, plan=plan or "Google AI Pro")
    if not raw["userStatus"]["cascadeModelConfigData"]["clientModelConfigs"]:
        raise LiveQuotaError("API tidak mengembalikan data kuota model.", "empty")

    return {"raw_status": raw, "plan": plan or "Google AI Pro", "credentials": cred,
            "cred_changed": changed, "project_id": project_id}
