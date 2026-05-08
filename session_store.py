#!/usr/bin/env python3
"""
Session Store — browser cookie persistence (ported from Tianyan V2).

Compatible with Tianyan's AES-256-CBC encrypted session format.
Sessions are stored at ~/.flowruntime/sessions/ by default.
Also reads ~/.tianyan/sessions/ for backward compatibility.

Usage:
    store = SessionStore()
    store.save("xianyu", cookies, localStorage={...})
    data = store.load("xianyu")  # => {cookies: [...], localStorage: {...}}
"""

import json
import os
import base64
import secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional


# ── Config ───────────────────────────────────────────
DEFAULT_STORAGE_DIR = Path.home() / ".flowruntime" / "sessions"
LEGACY_STORAGE_DIR = Path.home() / ".tianyan" / "sessions"

ALGORITHM = "aes-256-cbc"
KEY_LENGTH = 32  # 256 bits
IV_LENGTH = 16   # 128 bits


# ── Crypto (lazy import — falls back gracefully) ─────

try:
    from Crypto.Cipher import AES as _AES
    from Crypto.Util.Padding import pad, unpad
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


# ── Session Store ────────────────────────────────────

class SessionStore:
    """Encrypted browser session persistence.

    Compatible with Tianyan V2's Node.js AES-256-CBC format.
    Auto-generates a master key on first use.
    """

    def __init__(self, storage_dir: Optional[Path] = None):
        self.storage_dir = storage_dir or DEFAULT_STORAGE_DIR
        self.master_key_path = self.storage_dir.parent / ".masterkey"
        self._key_cache: dict[str, bytes] = {}  # path → key

    # ── Public API ────────────────────────────────

    def save(self, profile: str, cookies: list[dict],
             local_storage: Optional[dict] = None) -> dict:
        """Save cookies for a profile. Returns stored metadata."""
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # Preserve createdAt from existing session
        created_at = None
        existing = self._read_raw(profile)
        if existing and "createdAt" in existing:
            created_at = existing["createdAt"]

        now = datetime.now(timezone.utc).isoformat()
        created_at = created_at or now

        payload = json.dumps({"cookies": cookies, "localStorage": local_storage or {}},
                             ensure_ascii=False)

        if HAS_CRYPTO:
            key = self._load_key(self.master_key_path)
            iv = secrets.token_bytes(IV_LENGTH)
            cipher = _AES.new(key, _AES.MODE_CBC, iv)
            encrypted = cipher.encrypt(pad(payload.encode("utf-8"), _AES.block_size))
            stored = {
                "profile": profile,
                "encrypted": True,
                "data": base64.b64encode(encrypted).decode("ascii"),
                "iv": base64.b64encode(iv).decode("ascii"),
                "createdAt": created_at,
                "updatedAt": now,
            }
        else:
            # Unencrypted fallback (pip install pycryptodome for encryption)
            stored = {
                "profile": profile,
                "encrypted": False,
                "data": base64.b64encode(payload.encode("utf-8")).decode("ascii"),
                "createdAt": created_at,
                "updatedAt": now,
            }

        path = self.storage_dir / f"{profile}.json"
        path.write_text(json.dumps(stored, ensure_ascii=False, indent=2))
        return stored

    def load(self, profile: str) -> Optional[dict]:
        """Load saved session. Returns {cookies, localStorage, createdAt, updatedAt} or None."""
        stored = self._read_raw(profile)
        if not stored:
            return None

        payload_str = self._decrypt(stored)
        if not payload_str:
            return None

        try:
            payload = json.loads(payload_str)
            return {
                "profile": stored.get("profile", profile),
                "cookies": payload.get("cookies", []),
                "localStorage": payload.get("localStorage", {}),
                "createdAt": stored.get("createdAt"),
                "updatedAt": stored.get("updatedAt"),
            }
        except json.JSONDecodeError:
            return None

    def list_profiles(self) -> list[str]:
        """List all saved session profiles."""
        profiles = []
        for d in [self.storage_dir, LEGACY_STORAGE_DIR]:
            if d.exists():
                for f in d.glob("*.json"):
                    profiles.append(f.stem)
        return sorted(set(profiles))

    def delete(self, profile: str):
        """Delete a saved session. No-op if not found."""
        path = self.storage_dir / f"{profile}.json"
        if path.exists():
            path.unlink()

    # ── Private ───────────────────────────────────

    def _read_raw(self, profile: str) -> Optional[dict]:
        """Read raw stored session dict from disk. Checks both FlowRuntime and Tianyan dirs."""
        for base in [self.storage_dir, LEGACY_STORAGE_DIR]:
            path = base / f"{profile}.json"
            if path.exists():
                try:
                    return json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
        return None

    def _decrypt(self, stored: dict) -> Optional[str]:
        """Decrypt stored payload. Uses correct master key for the storage source."""
        data_b64 = stored.get("data", "")
        try:
            raw = base64.b64decode(data_b64)
        except Exception:
            return None

        if not stored.get("encrypted"):
            return raw.decode("utf-8")

        if not HAS_CRYPTO:
            return None

        # Determine which master key to use: check where this session came from
        profile = stored.get("profile", "")
        key = self._get_master_key_for(profile)

        try:
            iv = base64.b64decode(stored["iv"])
            cipher = _AES.new(key, _AES.MODE_CBC, iv)
            decrypted = unpad(cipher.decrypt(raw), _AES.block_size)
            return decrypted.decode("utf-8")
        except Exception:
            return None

    def _get_master_key_for(self, profile: str) -> bytes:
        """Get the correct master key for a given profile.

        Checks which directory the profile lives in and loads the matching key.
        """
        flow_path = self.storage_dir / f"{profile}.json"
        legacy_path = LEGACY_STORAGE_DIR / f"{profile}.json"

        if legacy_path.exists() and not flow_path.exists():
            return self._load_key(LEGACY_STORAGE_DIR.parent / ".masterkey")

        return self._load_key(self.master_key_path)

    def _load_key(self, key_path: Path) -> bytes:
        """Load a master key from disk, caching in memory."""
        path_str = str(key_path)
        if path_str in self._key_cache:
            return self._key_cache[path_str]

        key_path.parent.mkdir(parents=True, exist_ok=True)

        if key_path.exists():
            key_b64 = key_path.read_text().strip()
            key = base64.b64decode(key_b64)
        else:
            key = secrets.token_bytes(KEY_LENGTH)
            key_path.write_text(base64.b64encode(key).decode("ascii"))
            key_path.chmod(0o600)

        self._key_cache[path_str] = key
        return key

    def _get_master_key(self) -> bytes:
        """Load or generate the master encryption key."""
        if self._master_key:
            return self._master_key

        self.master_key_path.parent.mkdir(parents=True, exist_ok=True)

        if self.master_key_path.exists():
            key_b64 = self.master_key_path.read_text().strip()
            self._master_key = base64.b64decode(key_b64)
        else:
            key = secrets.token_bytes(KEY_LENGTH)
            self.master_key_path.write_text(base64.b64encode(key).decode("ascii"))
            self.master_key_path.chmod(0o600)
            self._master_key = key

        return self._master_key


# ── Quick Test ───────────────────────────────────────
if __name__ == "__main__":
    store = SessionStore()
    print(f"  Storage: {store.storage_dir}")
    print(f"  Crypto available: {HAS_CRYPTO}")

    # Round-trip test
    test_cookies = [
        {"name": "session", "value": "abc123", "domain": ".example.com", "path": "/"},
    ]
    store.save("test_profile", test_cookies)
    loaded = store.load("test_profile")

    assert loaded is not None, "load failed"
    assert loaded["cookies"][0]["name"] == "session", "cookie mismatch"
    print(f"  Round-trip: OK ({len(loaded['cookies'])} cookies)")

    # List profiles
    profiles = store.list_profiles()
    print(f"  Profiles: {profiles}")

    # Cleanup
    store.delete("test_profile")
    print(f"  Cleanup: OK")
    print("  SessionStore: ready")
