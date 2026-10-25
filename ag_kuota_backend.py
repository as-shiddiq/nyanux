import json
import os
import sys
import sqlite3
import base64
import re
import ssl
import subprocess
import urllib.request
import urllib.parse
import threading
import webbrowser
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone

import ag_kuota_live as live

# ─── Config ──────────────────────────────────────────────────────────────────

APP_NAME = "Nyanux"
IDE_DB_PATH = Path.home() / ".config" / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb"
OLD_DB_PATH = Path.home() / ".config" / "Antigravity" / "User" / "globalStorage" / "state.vscdb"
POLL_INTERVAL_SEC = 30
CACHE_DIR = Path.home() / ".local" / "share" / "gemini-quota-monitor"
CACHE_FILE = CACHE_DIR / "quota_cache.json"
ACCOUNTS_JSON = Path.home() / ".config" / "gemini-quota-monitor" / "accounts.json"

def load_accounts_json():
    """
    Baca accounts.json yang dikelola oleh Antigravity Manager / Gemini Quota Monitor.
    Return list of account dicts, atau [] jika file tidak ada.
    """
    try:
        if ACCOUNTS_JSON.exists():
            with open(ACCOUNTS_JSON, 'r') as f:
                accounts = json.load(f)
            if isinstance(accounts, list):
                return accounts
    except Exception as e:
        print(f"load_accounts_json error: {e}")
    return []


def refresh_access_token(credentials):
    """
    Refresh OAuth access token menggunakan refresh_token.
    Return (access_token, new_expiry) atau (None, None) jika gagal.
    """
    try:
        cred = json.loads(credentials) if isinstance(credentials, str) else credentials
        refresh_token = cred.get("refresh_token")
        client_id = cred.get("client_id")
        client_secret = cred.get("client_secret")
        token_uri = cred.get("token_uri", "https://oauth2.googleapis.com/token")

        if not all([refresh_token, client_id, client_secret]):
            return None, None

        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }).encode('utf-8')

        req = urllib.request.Request(
            token_uri,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )

        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            result = json.loads(resp.read())
            access_token = result.get("access_token")
            expires_in = result.get("expires_in", 3600)
            return access_token, expires_in

    except Exception as e:
        print(f"refresh_access_token error: {e}")
        return None, None


def fetch_user_status_with_token(access_token):
    """
    Coba panggil GetUserStatus via language server lokal yang sedang berjalan,
    dengan tambahan Authorization header dari OAuth token.
    Jika tidak ada LS yang berjalan, return None.
    
    NOTE: GetUserStatus endpoint hanya tersedia via language_server lokal,
    bukan langsung di Google API public (404). Oleh karena itu kita tetap
    perlu LS berjalan, tapi dengan OAuth token kita bisa memverifikasi akun.
    """
    # Try melalui local LS yang running
    ls_list = discover_all_ls_processes()
    if not ls_list:
        return None

    body = {"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}}
    payload = json.dumps(body).encode('utf-8')

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    url_path = "/exa.language_server_pb.LanguageServerService/GetUserStatus"

    for ls in ls_list:
        for port in ls["ports"]:
            for proto in ["http", "https"]:
                try:
                    url = f"{proto}://127.0.0.1:{port}{url_path}"
                    req = urllib.request.Request(
                        url,
                        data=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Connect-Protocol-Version": "1",
                            "X-Codeium-Csrf-Token": ls["csrf_token"],
                            "Authorization": f"Bearer {access_token}",
                        },
                        method="POST"
                    )
                    with urllib.request.urlopen(req, context=ctx, timeout=3) as resp:
                        data = json.loads(resp.read())
                        if data and "userStatus" in data:
                            return data
                except Exception:
                    continue
    return None


def update_accounts_json_token(email, new_token, new_expiry_seconds):
    """
    Update token di accounts.json setelah refresh berhasil.
    """
    try:
        accounts = load_accounts_json()
        changed = False
        for acc in accounts:
            if acc.get("email") == email:
                cred = json.loads(acc.get("credentials", "{}"))
                cred["token"] = new_token
                from datetime import timezone, timedelta
                new_expiry = datetime.now(timezone.utc) + timedelta(seconds=new_expiry_seconds)
                cred["expiry"] = new_expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                acc["credentials"] = json.dumps(cred)
                changed = True
                break
        if changed:
            with open(ACCOUNTS_JSON, 'w') as f:
                json.dump(accounts, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"update_accounts_json_token error: {e}")


def update_accounts_json_status(email, quota_groups, plan="Google AI Pro Plan"):
    """
    Update last_status di accounts.json setelah fetch kuota berhasil.
    """
    try:
        accounts = load_accounts_json()
        changed = False
        for acc in accounts:
            if acc.get("email") == email:
                acc["last_status"] = {
                    "valid": True,
                    "plan": plan or "Google AI Pro Plan",
                    "quota_groups": quota_groups,
                    "checked_at": datetime.now().astimezone().isoformat(),
                }
                changed = True
                break
        if changed:
            with open(ACCOUNTS_JSON, 'w') as f:
                json.dump(accounts, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"update_accounts_json_status error: {e}")


# ─── Quota Response Parser ───────────────────────────────────────────────────

def _parse_quota_groups_from_response(raw_response):
    """
    Parse GetUserStatus response ke format quota_groups yang kompatibel dengan accounts.json.
    Return (plan, quota_groups_list)
    """
    parsed = parse_quota_from_response(raw_response)
    plan = parsed.get("plan", "Google AI Pro Plan")
    quotas = _build_quotas_from_live_data(parsed)

    # Convert ke format quota_groups (seperti di accounts.json)
    quota_groups = []
    for cat in quotas:
        pcts = [int(m["remaining"].replace("%", "")) for m in cat["models"]]
        avg_pct = round(sum(pcts) / len(pcts)) if pcts else 0
        first_reset = cat["models"][0]["reset"] if cat["models"] else "N/A"
        first_reset_raw = cat["models"][0].get("reset_time_raw", "") if cat["models"] else ""
        quota_groups.append({
            "name": cat["category"],
            "remaining": f"{avg_pct}%",
            "reset_in": first_reset,
            "reset_time_raw": first_reset_raw,
        })

    return plan, quotas, quota_groups


# ─── Language Server Discovery ────────────────────────────────────────────────

def discover_all_ls_processes():
    """
    Temukan semua proses language_server_linux yang sedang berjalan.
    Untuk setiap proses, kembalikan dict berisi pid, csrf_token, dan daftar port
    yang benar-benar listening (dari ss, bukan dari argumen CLI).
    """
    result = []
    try:
        cmd = ["pgrep", "-af", "language_server_linux"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        if res.returncode != 0:
            return result

        lines = res.stdout.strip().split('\n')
        for line in lines:
            if "language_server_linux" not in line or "--csrf_token" not in line:
                continue

            # Extract PID (first number in line)
            pid_m = re.match(r'^(\d+)\s', line)
            if not pid_m:
                continue
            pid = pid_m.group(1)

            # Extract CSRF token (the first --csrf_token, not extension_server_csrf_token)
            token_m = re.search(r'--csrf_token[=\s]+([a-zA-Z0-9\-]+)', line)
            if not token_m:
                continue
            csrf_token = token_m.group(1)

            # Discover real listening ports via ss
            listening_ports = []
            try:
                ss_cmd = f"ss -tlnp 2>/dev/null | grep 'pid={pid}'"
                ss_out = subprocess.run(ss_cmd, shell=True, capture_output=True, text=True, timeout=3).stdout
                for ss_line in ss_out.split('\n'):
                    m = re.search(r':(\d+)\s+', ss_line)
                    if m:
                        p = int(m.group(1))
                        if p not in listening_ports:
                            listening_ports.append(p)
            except Exception:
                pass

            if listening_ports:
                result.append({
                    "pid": pid,
                    "csrf_token": csrf_token,
                    "ports": listening_ports,
                })

    except Exception as e:
        print(f"discover_all_ls_processes error: {e}")
    return result


def restart_language_server():
    """Temukan semua proses language_server_linux yang berjalan dan bunuh (SIGKILL) agar extension memuat ulang."""
    try:
        ls_list = discover_all_ls_processes()
        for ls in ls_list:
            os.kill(int(ls["pid"]), 9)
            print(f"[restart] Killed LS process with PID {ls['pid']}")
    except Exception as e:
        print(f"restart_language_server error: {e}")


def fetch_user_status_from_ls():
    """
    Coba fetch GetUserStatus dari semua proses LS yang ditemukan.
    Kembalikan raw response pertama yang berhasil, atau None.
    """
    ls_list = discover_all_ls_processes()
    if not ls_list:
        return None

    body = {"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}}
    payload = json.dumps(body).encode('utf-8')

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    url_path = "/exa.language_server_pb.LanguageServerService/GetUserStatus"

    for ls in ls_list:
        for port in ls["ports"]:
            for proto in ["http", "https"]:
                try:
                    url = f"{proto}://127.0.0.1:{port}{url_path}"
                    req = urllib.request.Request(
                        url,
                        data=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Connect-Protocol-Version": "1",
                            "X-Codeium-Csrf-Token": ls["csrf_token"]
                        },
                        method="POST"
                    )
                    with urllib.request.urlopen(req, context=ctx, timeout=3) as resp:
                        data = json.loads(resp.read())
                        if data and "userStatus" in data:
                            return data
                except Exception:
                    continue
    return None


def format_remaining_time(reset_time_iso):
    if not reset_time_iso:
        return "N/A"
    try:
        clean = reset_time_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean).astimezone()
        now = datetime.now().astimezone()
        
        abs_str = dt.strftime("%d %b %Y, %H:%M")
        
        if dt <= now:
            return f"{abs_str} (Segera di-reset)"
            
        delta = dt - now
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        
        parts = []
        if days > 0:
            parts.append(f"{days} hari")
        if hours > 0:
            parts.append(f"{hours} jam")
        if minutes > 0 or not parts:
            parts.append(f"{minutes} menit")
            
        return f"{abs_str} (dalam {', '.join(parts)})"
    except Exception:
        return reset_time_iso[:19]


def parse_quota_from_response(raw_response):
    """
    Parse GetUserStatus response ke format yang bisa ditampilkan.
    Kembalikan dict: { email, plan, models: [{ label, remaining_pct, reset_str, pool_name }] }
    """
    user_status = raw_response.get("userStatus", {})
    email = user_status.get("email", "unknown")
    
    # Detect plan type
    plan = "Google AI Pro Plan"
    sub = user_status.get("subscriptionType", "")
    if sub:
        plan = sub

    configs = user_status.get("cascadeModelConfigData", {}).get("clientModelConfigs", [])

    # Group models by quota pool (same fraction + resetTime = same pool)
    pools = {}
    model_list = []

    # First pass: group by quota pool (same fraction + resetTime)
    pool_map = {}  # (frac, resetTime) -> list of model dicts
    for m in configs:
        qi = m.get("quotaInfo")
        label = m.get("label", "Unknown Model")

        if not qi:
            model_list.append({
                "label": label,
                "remaining_pct": 100,
                "reset_str": "N/A",
                "reset_time_raw": "",
                "pool_name": "Other Models",
            })
            continue

        frac = qi.get("remainingFraction", 0.0)
        reset_time = qi.get("resetTime", "")
        pct = round(frac * 100)

        reset_str = format_remaining_time(reset_time)

        pool_key = (frac, reset_time)
        if pool_key not in pool_map:
            pool_map[pool_key] = []
        pool_map[pool_key].append({
            "label": label,
            "remaining_pct": pct,
            "reset_str": f"Reset: {reset_str}",
            "reset_time_raw": reset_time,
        })


    # Second pass: name each pool based on its constituent models
    for (frac, reset_time), models in pool_map.items():
        labels_lower = " ".join(m["label"] for m in models).lower()

        # Determine the pool display name
        if "gemini" in labels_lower and ("claude" in labels_lower or "gpt" in labels_lower):
            pool_name = "All Models"
        elif "gemini" in labels_lower:
            has_pro = any("pro" in m["label"].lower() for m in models)
            has_flash = any("flash" in m["label"].lower() for m in models)
            if has_pro and has_flash:
                pool_name = "Gemini Models"
            elif has_pro:
                pool_name = "Gemini Pro Models"
            elif has_flash:
                pool_name = "Gemini Flash Models"
            else:
                pool_name = "Gemini Models"
        elif "claude" in labels_lower and "gpt" in labels_lower:
            pool_name = "Claude dan GPT Models"
        elif "claude" in labels_lower:
            pool_name = "Claude Models"
        elif "gpt" in labels_lower:
            pool_name = "GPT Models"
        else:
            pool_name = "Other Models"

        for m in models:
            m["pool_name"] = pool_name
            model_list.append(m)

    return {
        "email": email,
        "plan": plan,
        "models": model_list,
    }



# ─── Account Discovery ───────────────────────────────────────────────────────

def get_email_from_status_bytes(raw_bytes):
    """Extract emails from protobuf-encoded status bytes."""
    emails = []
    b64_blocks = re.findall(b'[a-zA-Z0-9+/]{8,}=*', raw_bytes)
    for block in b64_blocks:
        for offset in range(4):
            for end_trim in range(4):
                sub = block[offset:]
                if end_trim > 0:
                    sub = sub[:-end_trim]
                if len(sub) < 8:
                    continue
                try:
                    padded = sub + b'=' * (4 - len(sub) % 4)
                    dec = base64.b64decode(padded)
                    m = re.search(rb'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', dec)
                    if m:
                        email_str = m.group(0).decode()
                        name_str = "User"
                        if b":" in dec:
                            parts = dec.split(b":")
                            clean_name = re.sub(rb'[^\x20-\x7E]', b'', parts[0]).decode().strip()
                            if clean_name:
                                name_str = clean_name
                        emails.append({"name": name_str, "email": email_str})
                except:
                    pass
    return emails


def get_token_from_oauth_bytes(raw_bytes):
    """Extract Google OAuth token (starts with ya29.) from protobuf-encoded bytes."""
    b64_blocks = re.findall(b'[a-zA-Z0-9+/_-]{8,}=*', raw_bytes)
    for block in b64_blocks:
        for offset in range(4):
            for end_trim in range(4):
                sub = block[offset:]
                if end_trim > 0:
                    sub = sub[:-end_trim]
                if len(sub) < 8:
                    continue
                try:
                    padded = sub + b'=' * (4 - len(sub) % 4)
                    dec = base64.b64decode(padded.replace(b'-', b'+').replace(b'_', b'/'))
                    idx = dec.find(b'ya29.')
                    if idx != -1:
                        token = dec[idx:].decode('utf-8', errors='ignore').strip()
                        token = re.sub(r'[^a-zA-Z0-9._-]', '', token)
                        return token
                except:
                    pass
    return None


def get_refresh_token_from_oauth_bytes(raw_bytes):
    """Cari Google refresh token (diawali '1//') di blob oauthToken IDE."""
    pat = rb'1//[0-9A-Za-z_\-]{30,}'
    m = re.search(pat, raw_bytes)
    if m:
        return m.group(0).decode()
    b64_blocks = re.findall(b'[a-zA-Z0-9+/_-]{8,}=*', raw_bytes)
    for block in b64_blocks:
        for offset in range(4):
            sub = block[offset:]
            if len(sub) < 8:
                continue
            try:
                padded = sub + b'=' * (-len(sub) % 4)
                dec = base64.b64decode(padded.replace(b'-', b'+').replace(b'_', b'/'))
            except Exception:
                continue
            m = re.search(pat, dec)
            if m:
                return m.group(0).decode()
            # bisa jadi ber-lapis base64 lagi
            for inner in re.findall(b'[a-zA-Z0-9+/_-]{40,}=*', dec):
                try:
                    dec2 = base64.b64decode(inner + b'=' * (-len(inner) % 4))
                    m = re.search(pat, dec2)
                    if m:
                        return m.group(0).decode()
                except Exception:
                    pass
    return None


def get_ide_active_email():
    """Email akun yang sedang login di Antigravity IDE (dibaca dari state.vscdb)."""
    for db_path in [IDE_DB_PATH, OLD_DB_PATH]:
        try:
            if not db_path.exists():
                continue
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key = 'antigravityAuthStatus'")
            row = cur.fetchone()
            conn.close()
            if row:
                email = json.loads(row[0]).get("email")
                if email:
                    return email
        except Exception:
            pass
    return None


def auto_detect_ide_accounts():
    """
    Scan state.vscdb files to auto-detect accounts active/known in the IDE.
    If the account does not exist in accounts.json, auto-import it.
    If it exists but has a newer/different token, sync the token to accounts.json.
    """
    detected_profiles = []
    # Discover known accounts from SQLite DB
    for db_path in [IDE_DB_PATH, OLD_DB_PATH]:
        try:
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                
                # Fetch userStatus (contains email/name info)
                cursor.execute("SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.userStatus'")
                row_status = cursor.fetchone()
                
                # Fetch oauthToken (contains access token)
                cursor.execute("SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.oauthToken'")
                row_oauth = cursor.fetchone()
                
                # Fetch antigravityAuthStatus (contains JSON with name, email, apiKey)
                cursor.execute("SELECT value FROM ItemTable WHERE key = 'antigravityAuthStatus'")
                row_auth = cursor.fetchone()
                
                conn.close()
                
                email = None
                name = "User"
                token = None
                refresh_tok = None
                
                if row_status:
                    raw_status = base64.b64decode(row_status[0])
                    profiles = get_email_from_status_bytes(raw_status)
                    if profiles:
                        email = profiles[0]["email"]
                        name = profiles[0]["name"]
                
                if row_oauth:
                    raw_oauth = base64.b64decode(row_oauth[0])
                    token = get_token_from_oauth_bytes(raw_oauth)
                    refresh_tok = get_refresh_token_from_oauth_bytes(raw_oauth)
                
                if email:
                    detected_profiles.append({
                        "email": email,
                        "name": name,
                        "token": token,
                        "refresh_token": refresh_tok,
                    })
                
                if row_auth:
                    try:
                        auth_data = json.loads(row_auth[0])
                        email_auth = auth_data.get("email")
                        name_auth = auth_data.get("name", "User")
                        token_auth = auth_data.get("apiKey")
                        if email_auth:
                            detected_profiles.append({
                                "email": email_auth,
                                "name": name_auth,
                                "token": token_auth,
                                "refresh_token": refresh_tok,
                            })
                    except Exception:
                        pass
        except Exception as e:
            print(f"auto_detect_ide_accounts DB parse error ({db_path}): {e}")

    # Remove duplicates from detected_profiles (keep the one with a token if multiple)
    unique_profiles = {}
    for dp in detected_profiles:
        email = dp["email"]
        if email not in unique_profiles or (dp["token"] and not unique_profiles[email]["token"]):
            prev_rt = unique_profiles.get(email, {}).get("refresh_token")
            unique_profiles[email] = dp
            if prev_rt and not dp.get("refresh_token"):
                dp["refresh_token"] = prev_rt
    detected_profiles = list(unique_profiles.values())

    if not detected_profiles:
        return

    # Baca accounts.json saat ini dan cari daftar email yang sengaja dihapus
    # Kita bisa simpan daftar email yang didelete di level file konfigurasi terpisah atau sebagai key khusus di accounts.json.
    # Namun cara termudah dan paling bersih adalah membaca accounts.json.
    # Jika akun sudah ada di file tapi ditandai sebagai dihapus (misal status.valid == False dan no credentials),
    # atau kita bisa simpan daftar deleted_emails di cache atau local config.
    # Di sini, mari kita filter detected_profiles terhadap file blacklist sederhana.
    blacklist_file = Path.home() / ".config" / "gemini-quota-monitor" / "blacklist_emails.json"
    blacklist = []
    if blacklist_file.exists():
        try:
            with open(blacklist_file, 'r') as f:
                blacklist = json.load(f)
        except Exception:
            pass

    accounts = load_accounts_json()
    updated = False
    
    for dp in detected_profiles:
        email = dp["email"]
        name = dp["name"]
        token = dp["token"]
        ide_refresh = dp.get("refresh_token")
        
        if email in blacklist:
            continue  # Abaikan email ini karena sengaja dihapus oleh user

        # Check if already exists in accounts.json
        existing = None
        for acc in accounts:
            if acc.get("email") == email:
                existing = acc
                break
                
        if existing:
            # Check if token needs update
            cred_str = existing.get("credentials", "")
            cred = {}
            if cred_str:
                try:
                    cred = json.loads(cred_str)
                except Exception:
                    pass
            
            # If the token is different and we got a valid new token from the IDE, update it
            if token and cred.get("token") != token:
                cred["token"] = token
                # Reset expiry since we got a fresh/current token from the IDE session
                from datetime import timezone, timedelta
                expiry = datetime.now(timezone.utc) + timedelta(hours=1)
                cred["expiry"] = expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                existing["credentials"] = json.dumps(cred)
                updated = True
                print(f"[Auto-Detect] Updated token for existing account: {email}")
            # Kalau akun belum punya refresh token, ambil dari IDE → kuota bisa realtime walau IDE ditutup
            if ide_refresh and not cred.get("refresh_token"):
                cred["refresh_token"] = ide_refresh
                ag_client = live.antigravity_client() or {}
                cred["client_id"] = ag_client.get("client_id", "")
                cred["client_secret"] = ag_client.get("client_secret", "")
                cred.setdefault("token_uri", "https://oauth2.googleapis.com/token")
                existing["credentials"] = json.dumps(cred)
                updated = True
                print(f"[Auto-Detect] Imported IDE refresh token for: {email}")
        else:
            # Import new account automatically!
            from datetime import timezone, timedelta
            expiry = datetime.now(timezone.utc) + timedelta(hours=1)
            ag_client = (live.antigravity_client() or {}) if ide_refresh else {}
            cred = {
                "token": token or "",
                "refresh_token": ide_refresh or "",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": ag_client.get("client_id", ""),
                "client_secret": ag_client.get("client_secret", ""),
                "scopes": [
                    "https://www.googleapis.com/auth/generative-language.retriever",
                    "openid",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile"
                ],
                "universe_domain": "googleapis.com",
                "account": "",
                "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            }
            new_acc = {
                "email": email,
                "name": name,
                "picture": "",
                "credentials": json.dumps(cred),
                "added_at": datetime.now(timezone.utc).isoformat(),
                "last_status": {
                    "valid": False,
                    "plan": "Google AI Pro Plan",
                    "quota_groups": [],
                    "checked_at": ""
                }
            }
            accounts.append(new_acc)
            updated = True
            print(f"[Auto-Detect] Imported new active IDE account: {email}")

    if updated:
        try:
            with open(ACCOUNTS_JSON, 'w') as f:
                json.dump(accounts, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"auto_detect_ide_accounts failed to save accounts.json: {e}")


# ─── Quota Cache System ──────────────────────────────────────────────────────

def load_cache():
    """Load cached quota data from disk."""
    try:
        if CACHE_FILE.exists():
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Cache load error: {e}")
    return {}


def save_cache(cache):
    """Save quota cache to disk."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Cache save error: {e}")


def save_account_to_cache(email, quotas, plan="Google AI Pro Plan"):
    """Save a single account's quota data to cache."""
    cache = load_cache()
    cache[email] = {
        "quotas": quotas,
        "plan": plan,
        "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_cache(cache)


def _parse_reset_date_from_string(reset_str):
    """
    Fallback: extract datetime from pre-formatted reset string.
    Handles formats like "Reset: 09 Aug 2026, 12:43 (dalam ...)" or just "09 Aug 2026, 12:43 (...)".
    Returns a timezone-aware datetime or None.
    """
    if not reset_str:
        return None
    try:
        # Remove "Reset: " prefix if present
        s = reset_str
        if s.startswith("Reset: "):
            s = s[7:]
        # Extract date part before the parenthetical "(dalam ...)" or "(Segera ...)"
        paren_idx = s.find("(")
        if paren_idx > 0:
            s = s[:paren_idx].strip()
        # Parse "09 Aug 2026, 12:43"
        dt = datetime.strptime(s, "%d %b %Y, %H:%M")
        # Assume local timezone
        import time
        from datetime import timezone, timedelta
        local_offset = timedelta(seconds=-time.timezone if time.daylight == 0 else -time.altzone)
        dt = dt.replace(tzinfo=timezone(local_offset))
        return dt
    except Exception:
        return None


def refresh_cached_quotas(quotas):
    """
    Recalculate reset times and auto-reset quota to 100% if reset time has passed.
    This fixes stale cache data where the countdown string was frozen.
    Also handles old cache entries that don't have reset_time_raw by parsing
    the date from the pre-formatted reset string.
    """
    now = datetime.now().astimezone()
    refreshed = []
    for cat in quotas:
        new_models = []
        for m in cat.get("models", []):
            raw_time = m.get("reset_time_raw", "")
            if raw_time:
                try:
                    clean = raw_time.replace("Z", "+00:00")
                    reset_dt = datetime.fromisoformat(clean).astimezone()
                    if reset_dt <= now:
                        # Reset time has passed → quota is back to 100%
                        new_models.append({
                            **m,
                            "remaining": "100%",
                            "reset": f"Reset: {reset_dt.strftime('%d %b %Y, %H:%M')} (Sudah di-reset)",
                        })
                    else:
                        # Recalculate countdown dynamically
                        new_reset_str = format_remaining_time(raw_time)
                        new_models.append({
                            **m,
                            "reset": f"Reset: {new_reset_str}",
                        })
                except Exception:
                    new_models.append(m)
            else:
                # Fallback: try to parse date from the formatted reset string
                reset_str = m.get("reset", "")
                parsed_dt = _parse_reset_date_from_string(reset_str)
                if parsed_dt:
                    if parsed_dt <= now:
                        # Reset time has passed → quota is back to 100%
                        new_models.append({
                            **m,
                            "remaining": "100%",
                            "reset": f"Reset: {parsed_dt.strftime('%d %b %Y, %H:%M')} (Sudah di-reset)",
                        })
                    else:
                        # Recalculate countdown dynamically from parsed date
                        delta = parsed_dt - now
                        days = delta.days
                        hours = delta.seconds // 3600
                        minutes = (delta.seconds % 3600) // 60
                        parts = []
                        if days > 0:
                            parts.append(f"{days} hari")
                        if hours > 0:
                            parts.append(f"{hours} jam")
                        if minutes > 0 or not parts:
                            parts.append(f"{minutes} menit")
                        new_models.append({
                            **m,
                            "reset": f"Reset: {parsed_dt.strftime('%d %b %Y, %H:%M')} (dalam {', '.join(parts)})",
                        })
                else:
                    new_models.append(m)
        refreshed.append({
            **cat,
            "models": new_models,
        })
    return refreshed


def _build_quotas_from_live_data(live_data):
    """Convert parsed live data models into quota categories."""
    pool_groups = {}
    for m in live_data["models"]:
        pool = m["pool_name"]
        if pool not in pool_groups:
            pool_groups[pool] = []
        pool_groups[pool].append(m)

    quotas = []
    pool_order = sorted(pool_groups.keys(), key=lambda x: (
        0 if "Gemini" in x else (1 if "Claude" in x or "GPT" in x else 2)
    ))
    for pool_name in pool_order:
        models = pool_groups[pool_name]
        quotas.append({
            "category": pool_name,
            "models": [
                {
                    "name": m["label"],
                    "remaining": f"{m['remaining_pct']}%",
                    "reset": m["reset_str"],
                    "reset_time_raw": m.get("reset_time_raw", ""),
                }
                for m in models
            ]
        })
    return quotas


_ACCOUNTS_LOCK = threading.Lock()


def _save_account_credentials(email, cred_dict):
    """Simpan credentials terbaru (token hasil refresh, project_id, client) ke accounts.json."""
    with _ACCOUNTS_LOCK:
        try:
            accounts = load_accounts_json()
            for acc in accounts:
                if acc.get("email") == email:
                    acc["credentials"] = json.dumps(cred_dict)
                    break
            else:
                return
            ACCOUNTS_JSON.parent.mkdir(parents=True, exist_ok=True)
            tmp = ACCOUNTS_JSON.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(accounts, f, indent=2, ensure_ascii=False)
            os.replace(tmp, ACCOUNTS_JSON)
        except Exception as e:
            print(f"_save_account_credentials error: {e}")


def fetch_live_quotas_parallel(oauth_accounts, max_workers=6):
    """
    Query kuota realtime semua akun secara paralel langsung ke API Cloud Code.
    Return { email: {"ok": True, plan, quotas, quota_groups} | {"ok": False, "error": str} }
    """
    from concurrent.futures import ThreadPoolExecutor
    results = {}
    targets = [a for a in oauth_accounts if a.get("email") and a.get("credentials")]
    if not targets:
        return results

    def _one(acc):
        email = acc["email"]
        try:
            r = live.fetch_live_quota_for_account(acc)
            if r["cred_changed"]:
                _save_account_credentials(email, r["credentials"])
            plan, quotas, quota_groups = _parse_quota_groups_from_response(r["raw_status"])
            return email, {"ok": True, "plan": plan, "quotas": quotas, "quota_groups": quota_groups}
        except live.LiveQuotaError as e:
            return email, {"ok": False, "error": str(e)}
        except Exception as e:
            return email, {"ok": False, "error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as ex:
        for email, res in ex.map(_one, targets):
            results[email] = res
            if res["ok"]:
                print(f"[live] {email}: OK ({res['plan']})")
            else:
                print(f"[live] {email}: gagal → {res['error']}")
    return results


def fetch_single_account_live(email):
    """Refresh realtime 1 akun saja (dipakai tombol 'Cek Sekarang')."""
    acc = next((a for a in load_accounts_json() if a.get("email") == email), None)
    if not acc:
        return {"ok": False, "error": "Akun tidak ditemukan"}
    res = fetch_live_quotas_parallel([acc]).get(email, {"ok": False, "error": "?"})
    if res.get("ok"):
        save_account_to_cache(email, res["quotas"], res["plan"])
        update_accounts_json_status(email, res["quota_groups"], res["plan"])
    return res


def gather_all_accounts():
    """
    Build account list dengan prioritas:
    0. REALTIME API Google (cloudcode-pa fetchAvailableModels) — semua akun, tanpa IDE
    1. LS live data — HANYA untuk akun yang emailnya cocok dengan LS response
    2. last_status dari accounts.json — untuk akun lain (data terakhir yg valid)
    3. Cache lokal — fallback terakhir
    
    PENTING: LS (Language Server) mengabaikan Authorization header dan selalu
    mengembalikan data akun yang aktif di IDE. Oleh karena itu kita TIDAK boleh
    assign data LS ke semua akun — hanya ke akun yang emailnya cocok.
    """
    # Auto-detect and import/sync IDE accounts
    try:
        auto_detect_ide_accounts()
    except Exception as e:
        print(f"Error during auto_detect_ide_accounts: {e}")

    accounts = []
    oauth_accounts = load_accounts_json()

    # ─── Prioritas 1: Fetch LS sekali, extract email & data ───
    if oauth_accounts:
        # Fetch LS data SEKALI saja — hasilnya hanya valid untuk 1 akun (yang aktif di IDE)
        raw_ls = fetch_user_status_from_ls()
        ls_email = None
        ls_plan = None
        ls_quotas = None
        ls_quota_groups = None

        if raw_ls:
            us = raw_ls.get("userStatus", {})
            ls_email = us.get("email")
            if ls_email:
                ls_plan, ls_quotas, ls_quota_groups = _parse_quota_groups_from_response(raw_ls)
                # Save live data ke cache & accounts.json untuk akun yang cocok
                save_account_to_cache(ls_email, ls_quotas, ls_plan)
                update_accounts_json_status(ls_email, ls_quota_groups, ls_plan)
                print(f"[gather] LS active email: {ls_email}, fractions available")

        # ─── Prioritas 0: REALTIME langsung ke API Google (seperti Antigravity Tools) ───
        # Tidak butuh IDE/browser terbuka. Semua akun di-query paralel.
        live_api = fetch_live_quotas_parallel(oauth_accounts)
        ide_active_email = get_ide_active_email()
        for email, res in live_api.items():
            if res.get("ok"):
                save_account_to_cache(email, res["quotas"], res["plan"])
                update_accounts_json_status(email, res["quota_groups"], res["plan"])

        # Reload accounts.json setelah token refresh & status update
        oauth_accounts = load_accounts_json()

        for oa in oauth_accounts:
            email = oa.get("email", "")
            name = oa.get("name", "User")
            credentials = oa.get("credentials", "")

            if not email:
                continue

            is_active = (email == (ls_email or ide_active_email))

            live_res = live_api.get(email) or {}
            live_err = live_res.get("error")

            # ─── Data realtime dari API Google ───
            if live_res.get("ok"):
                accounts.append({
                    "name": name,
                    "email": email,
                    "plan": live_res["plan"],
                    "is_active": is_active,
                    "quotas": live_res["quotas"],
                    "checked_at": datetime.now().strftime("%H:%M:%S"),
                    "has_live_data": True,
                    "data_source": "🟢 Live (API Google)",
                    "credentials": credentials,
                })
                continue

            # ─── Akun yang cocok dengan LS → pakai live data ───
            if is_active and ls_quotas:
                accounts.append({
                    "name": name,
                    "email": email,
                    "plan": ls_plan or "Google AI Pro Plan",
                    "is_active": True,
                    "quotas": ls_quotas,
                    "checked_at": datetime.now().strftime("%H:%M:%S"),
                    "has_live_data": True,
                    "data_source": "🟢 Live (LS)",
                    "credentials": credentials,
                })
                continue

            # ─── Coba Cache lokal dulu untuk info model yang lengkap ───
            cache = load_cache()
            cached = cache.get(email)
            if cached and cached.get("quotas"):
                display_checked = cached.get("cached_at", "?")
                if isinstance(display_checked, str) and " " in display_checked:
                    try:
                        parts = display_checked.split(" ")
                        if len(parts) > 1:
                            display_checked = parts[1]
                    except Exception:
                        pass
                elif isinstance(display_checked, str) and "T" in display_checked:
                    try:
                        dt = datetime.fromisoformat(display_checked)
                        display_checked = dt.strftime("%H:%M:%S")
                    except Exception:
                        pass
                refreshed_quotas = refresh_cached_quotas(cached["quotas"])
                accounts.append({
                    "name": name,
                    "email": email,
                    "plan": cached.get("plan", "Google AI Pro Plan"),
                    "is_active": False,
                    "quotas": refreshed_quotas,
                    "checked_at": display_checked,
                    "has_live_data": False,
                    "data_source": f"📋 Cached ({display_checked})",
                    "live_error": live_err,
                    "credentials": credentials,
                })
                continue

            # ─── Fallback ke last_status dari accounts.json jika cache kosong ───
            last_status = oa.get("last_status", {})
            if last_status and last_status.get("valid") and last_status.get("quota_groups"):
                qg = last_status["quota_groups"]
                plan = last_status.get("plan", "Google AI Pro Plan")
                checked_at = last_status.get("checked_at", "?")

                # Convert quota_groups ke format tampilan
                quotas = []
                for group in qg:
                    pct_str = group.get("remaining", "0%")
                    quotas.append({
                        "category": group.get("name", "Unknown"),
                        "models": [{
                            "name": group.get("name", "Unknown"),
                            "remaining": pct_str,
                            "reset": group.get("reset_in", "N/A"),
                            "reset_time_raw": group.get("reset_time_raw", ""),
                        }]
                    })

                # Refresh: recalculate times & auto-reset to 100% if past
                quotas = refresh_cached_quotas(quotas)

                # Format checked_at for display
                display_checked = checked_at
                if isinstance(checked_at, str) and "T" in checked_at:
                    try:
                        dt = datetime.fromisoformat(checked_at)
                        display_checked = dt.strftime("%H:%M:%S")
                    except Exception:
                        pass

                accounts.append({
                    "name": name,
                    "email": email,
                    "plan": plan,
                    "is_active": False,
                    "quotas": quotas,
                    "checked_at": display_checked,
                    "has_live_data": False,
                    "data_source": f"📋 Cached ({display_checked})",
                    "live_error": live_err,
                    "credentials": credentials,
                })
                continue

            # ─── Fallback terakhir: Cache lokal kosong / lama ───
            if cached and cached.get("quotas"):
                accounts.append({
                    "name": name,
                    "email": email,
                    "plan": cached.get("plan", "Google AI Pro Plan"),
                    "is_active": False,
                    "quotas": cached["quotas"],
                    "checked_at": cached.get("cached_at", "?"),
                    "has_live_data": False,
                    "data_source": f"📦 Cache ({cached.get('cached_at', '?')})",
                    "credentials": credentials,
                })
            else:
                accounts.append({
                    "name": name,
                    "email": email,
                    "plan": "Google AI Pro Plan",
                    "is_active": False,
                    "quotas": [],
                    "checked_at": datetime.now().strftime("%H:%M:%S"),
                    "has_live_data": False,
                    "data_source": "⚪ Belum ada data",
                    "live_error": live_err,
                    "credentials": credentials,
                })

    # ─── Prioritas 3: Language Server fallback (jika tidak ada accounts.json) ───
    if not oauth_accounts:
        raw_response = fetch_user_status_from_ls()
        live_data = None
        active_email = None

        if raw_response:
            live_data = parse_quota_from_response(raw_response)
            active_email = live_data["email"]

        # Discover known accounts from SQLite DB
        known_profiles = []
        for db_path in [IDE_DB_PATH, OLD_DB_PATH]:
            try:
                if db_path.exists():
                    conn = sqlite3.connect(str(db_path))
                    cursor = conn.cursor()
                    cursor.execute("SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.userStatus'")
                    row = cursor.fetchone()
                    
                    cursor.execute("SELECT value FROM ItemTable WHERE key = 'antigravityAuthStatus'")
                    row_auth = cursor.fetchone()
                    
                    conn.close()
                    
                    if row:
                        raw = base64.b64decode(row[0])
                        for p in get_email_from_status_bytes(raw):
                            if p["email"] not in [x["email"] for x in known_profiles]:
                                known_profiles.append(p)
                                
                    if row_auth:
                        try:
                            auth_data = json.loads(row_auth[0])
                            email = auth_data.get("email")
                            name = auth_data.get("name", "User")
                            if email and email not in [x["email"] for x in known_profiles]:
                                known_profiles.append({"email": email, "name": name})
                        except Exception:
                            pass
            except Exception as e:
                print(f"DB parse error ({db_path}): {e}")

        cache = load_cache()

        for p in known_profiles:
            is_active = (p["email"] == active_email)

            if is_active and live_data:
                quotas = _build_quotas_from_live_data(live_data)
                plan = live_data.get("plan", "Google AI Pro Plan")
                save_account_to_cache(p["email"], quotas, plan)

                accounts.append({
                    "name": p["name"],
                    "email": p["email"],
                    "plan": plan,
                    "is_active": True,
                    "quotas": quotas,
                    "checked_at": datetime.now().strftime("%H:%M:%S"),
                    "has_live_data": True,
                    "data_source": "🟢 Live (LS)",
                })
            else:
                cached = cache.get(p["email"])
                if cached and cached.get("quotas"):
                    refreshed_quotas = refresh_cached_quotas(cached["quotas"])
                    accounts.append({
                        "name": p["name"],
                        "email": p["email"],
                        "plan": cached.get("plan", "Google AI Pro Plan"),
                        "is_active": False,
                        "quotas": refreshed_quotas,
                        "checked_at": cached.get("cached_at", "?"),
                        "has_live_data": False,
                        "data_source": f"📦 Cache ({cached.get('cached_at', '?')})",
                    })
                else:
                    accounts.append({
                        "name": p["name"],
                        "email": p["email"],
                        "plan": "Google AI Pro Plan",
                        "is_active": False,
                        "quotas": [],
                        "checked_at": datetime.now().strftime("%H:%M:%S"),
                        "has_live_data": False,
                        "data_source": "⚪ Belum ada data",
                    })

    # Sort: active account first, then by email
    accounts.sort(key=lambda x: (not x["is_active"], x["email"]))
    return accounts

