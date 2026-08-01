"""
Eve v7 — Google Drive brain sync.

Maqsad: VPS badalne pe bot apni yaadein na bhoole.

Kaise:
  * Local SQLite hi primary rehta hai (fast).
  * Har SYNC_INTERVAL pe DB ka gzip snapshot Drive folder me upload.
  * Boot pe: Drive ka latest snapshot local se naya ho to download + restore.
  * Drive pe last KEEP_SNAPSHOTS versions rehte hain, purane auto-delete.

Auth: Google service account JSON.
  GOOGLE_SERVICE_ACCOUNT_JSON = /path/to/sa.json   (ya raw JSON string)
  GDRIVE_FOLDER_ID            = Drive folder ka ID (us folder ko SA email
                                ke saath share karna zaroori hai)

Install:
  pip install google-api-python-client google-auth
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger("eve.drive")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
SNAPSHOT_PREFIX = "eve_brain_"
KEEP_SNAPSHOTS = 20
SYNC_INTERVAL = 300          # 5 min

_service = None
_lock = threading.RLock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_last_sync: Dict[str, Any] = {"at": None, "ok": None, "error": None, "size": 0}


# ------------------------------------------------------------------ auth


def _creds():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON set nahi hai")

    from google.oauth2 import service_account

    if raw.lstrip().startswith("{"):
        info = json.loads(raw)
    else:
        p = Path(raw)
        if not p.exists():
            raise RuntimeError(f"service account file nahi mili: {raw}")
        info = json.loads(p.read_text())
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def _svc():
    global _service
    if _service is None:
        from googleapiclient.discovery import build
        _service = build("drive", "v3", credentials=_creds(), cache_discovery=False)
    return _service


def _folder_id() -> str:
    fid = os.getenv("GDRIVE_FOLDER_ID", "").strip()
    if not fid:
        raise RuntimeError("GDRIVE_FOLDER_ID set nahi hai")
    return fid


def is_configured() -> bool:
    return bool(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        and os.getenv("GDRIVE_FOLDER_ID", "").strip()
    )


# ------------------------------------------------------------- snapshot


def _db_path() -> Path:
    return Path(config.DB_PATH)


def _make_snapshot() -> bytes:
    """
    SQLite ka consistent snapshot — sqlite3 backup API use karte hain taaki
    WAL ke beech me copy karne pe corrupt na ho. Phir gzip.
    """
    src = sqlite3.connect(str(_db_path()))
    try:
        buf = io.BytesIO()
        tmp = _db_path().with_suffix(".snapshot.tmp")
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
        data = tmp.read_bytes()
        tmp.unlink(missing_ok=True)
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
            gz.write(data)
        return buf.getvalue()
    finally:
        src.close()


# --------------------------------------------------------------- upload


def upload_snapshot() -> Dict[str, Any]:
    """DB ka naya snapshot Drive pe daalo. Result dict return karta hai."""
    with _lock:
        try:
            from googleapiclient.http import MediaIoBaseUpload

            blob = _make_snapshot()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            name = f"{SNAPSHOT_PREFIX}{stamp}.db.gz"

            media = MediaIoBaseUpload(
                io.BytesIO(blob), mimetype="application/gzip", resumable=False
            )
            _svc().files().create(
                body={"name": name, "parents": [_folder_id()]},
                media_body=media,
                fields="id,name,size",
            ).execute()

            _prune()
            _last_sync.update({
                "at": datetime.now(timezone.utc).isoformat(),
                "ok": True, "error": None, "size": len(blob),
            })
            logger.info("[DRIVE] snapshot uploaded: %s (%.1f KB)", name, len(blob) / 1024)
            return {"ok": True, "name": name, "size": len(blob)}

        except Exception as e:
            _last_sync.update({
                "at": datetime.now(timezone.utc).isoformat(),
                "ok": False, "error": str(e)[:300],
            })
            logger.error("[DRIVE] upload fail: %s", e)
            return {"ok": False, "error": str(e)}


def _prune() -> None:
    try:
        items = list_snapshots()
        for f in items[KEEP_SNAPSHOTS:]:
            _svc().files().delete(fileId=f["id"]).execute()
            logger.info("[DRIVE] purana snapshot delete: %s", f["name"])
    except Exception as e:
        logger.warning("[DRIVE] prune fail: %s", e)


def list_snapshots() -> List[Dict[str, Any]]:
    """Naye se purane order me."""
    res = _svc().files().list(
        q=f"'{_folder_id()}' in parents and name contains '{SNAPSHOT_PREFIX}' and trashed = false",
        orderBy="name desc",
        pageSize=100,
        fields="files(id,name,size,modifiedTime)",
    ).execute()
    return res.get("files", [])


# ------------------------------------------------------------- download


def restore_latest(force: bool = False) -> Dict[str, Any]:
    """
    Drive ka latest snapshot local DB pe restore karo.
    force=False -> sirf tab jab local DB missing ho ya Drive wala naya ho.
    """
    with _lock:
        try:
            from googleapiclient.http import MediaIoBaseDownload

            files = list_snapshots()
            if not files:
                return {"ok": False, "error": "Drive pe koi snapshot nahi mila"}

            latest = files[0]
            local = _db_path()

            if not force and local.exists():
                remote_ts = latest.get("modifiedTime", "")
                local_ts = datetime.fromtimestamp(
                    local.stat().st_mtime, tz=timezone.utc
                ).isoformat()
                if local_ts >= remote_ts:
                    return {"ok": True, "skipped": True,
                            "reason": "local DB already Drive jitni nayi hai"}

            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(
                buf, _svc().files().get_media(fileId=latest["id"])
            )
            done = False
            while not done:
                _, done = downloader.next_chunk()

            raw = gzip.decompress(buf.getvalue())

            local.parent.mkdir(parents=True, exist_ok=True)
            if local.exists():
                shutil.copy2(local, local.with_suffix(".db.prev"))
            tmp = local.with_suffix(".db.incoming")
            tmp.write_bytes(raw)

            # sanity check — corrupt file se local DB mat maaro
            check = sqlite3.connect(str(tmp))
            try:
                ok = check.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                check.close()
            if ok != "ok":
                tmp.unlink(missing_ok=True)
                return {"ok": False, "error": f"snapshot corrupt: {ok}"}

            for side in ("-wal", "-shm"):
                Path(str(local) + side).unlink(missing_ok=True)
            tmp.replace(local)

            logger.info("[DRIVE] restored from %s", latest["name"])
            return {"ok": True, "name": latest["name"], "size": len(raw)}

        except Exception as e:
            logger.error("[DRIVE] restore fail: %s", e)
            return {"ok": False, "error": str(e)}


# ---------------------------------------------------------- background


def _loop() -> None:
    while not _stop.wait(SYNC_INTERVAL):
        try:
            upload_snapshot()
        except Exception as e:
            logger.error("[DRIVE] loop error: %s", e)


def start_autosync() -> bool:
    """Background sync thread chalu karo. Configured na ho to False."""
    global _thread
    if not is_configured():
        logger.info("[DRIVE] configured nahi — autosync skip")
        return False
    if _thread and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="drive-sync", daemon=True)
    _thread.start()
    logger.info("[DRIVE] autosync ON (har %ds)", SYNC_INTERVAL)
    return True


def stop_autosync() -> None:
    _stop.set()


def boot_restore() -> Dict[str, Any]:
    """
    Bot start hone se PEHLE call karo (init_db se bhi pehle).
    Naya VPS -> local DB nahi -> Drive se poora brain wapas.
    """
    if not is_configured():
        return {"ok": False, "error": "Drive configured nahi"}
    return restore_latest(force=False)


def status() -> Dict[str, Any]:
    st = dict(_last_sync)
    st["configured"] = is_configured()
    st["running"] = bool(_thread and _thread.is_alive())
    st["interval_sec"] = SYNC_INTERVAL
    if st["configured"]:
        try:
            snaps = list_snapshots()
            st["snapshots"] = len(snaps)
            st["latest"] = snaps[0]["name"] if snaps else None
        except Exception as e:
            st["snapshots"] = "?"
            st["error"] = str(e)[:200]
    return st


def status_text() -> str:
    s = status()
    if not s["configured"]:
        return (
            "☁️ Drive: SET NAHI HAI\n\n"
            "VPS pe .env me daal:\n"
            "GOOGLE_SERVICE_ACCOUNT_JSON=/root/eve/sa.json\n"
            "GDRIVE_FOLDER_ID=<folder id>\n\n"
            "Folder ko service account ki email ke saath share karna mat bhulna."
        )
    lines = [
        f"☁️ Drive: {'🟢 chal raha' if s['running'] else '🟡 autosync off'}",
        f"Snapshots: {s.get('snapshots', '?')} (rakhte hain last {KEEP_SNAPSHOTS})",
        f"Latest: {s.get('latest') or '-'}",
        f"Interval: har {SYNC_INTERVAL // 60} min",
    ]
    if s.get("at"):
        lines.append(f"Last sync: {s['at']} — {'✅' if s.get('ok') else '❌'}")
    if s.get("error"):
        lines.append(f"Error: {s['error']}")
    return "\n".join(lines)
