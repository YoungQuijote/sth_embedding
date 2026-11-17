"""Single-admin credential rotation with a persistent, salted password verifier."""

import hashlib
import json
import os
import secrets
import threading
import time


class AdminAuth:
    def __init__(self, store, initial_token=None):
        self.store = store
        self.lock = threading.RLock()
        self.record = store.get("auth", "admin")
        self.cached_digest = None
        if self.record is None:
            token = initial_token or os.environ.get("PANEL_ADMIN_TOKEN")
            if not token:
                path = store.directory / "admin.token"
                try:
                    with path.open("x") as stream:
                        os.chmod(path, 0o600)
                        stream.write(secrets.token_urlsafe(32))
                except FileExistsError:
                    pass
                token = path.read_text().strip()
            if len(token) < 16:
                raise ValueError("PANEL_ADMIN_TOKEN 至少 16 字符")
            self.cached_digest = self.digest(token)

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode("utf-8")).digest()

    @staticmethod
    def derive(value, salt):
        return hashlib.scrypt(
            value.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=16384,
            r=8,
            p=1,
            dklen=32,
            maxmem=64 * 1024**2,
        ).hex()

    def verify(self, value):
        if not value or len(value) > 4096:
            return False
        with self.lock:
            digest = self.digest(value)
            # Cache only the successfully verified current credential, in memory.
            # Only salted scrypt verifiers are written to disk; this digest is never persisted.
            if self.cached_digest is not None:
                return secrets.compare_digest(digest, self.cached_digest)
            if self.record and secrets.compare_digest(
                self.derive(value, self.record["salt"]), self.record["verifier"]
            ):
                self.cached_digest = digest
                return True
            return False

    def change(self, current, new):
        with self.lock:
            if not self.verify(current):
                return False
            salt = secrets.token_hex(16)
            record = {
                "id": "admin",
                "algorithm": "scrypt-n16384-r8-p1",
                "salt": salt,
                "verifier": self.derive(new, salt),
                "changed_at": time.time(),
            }
            # Commit before replacing the live verifier: failed persistence keeps old login valid.
            with self.store.connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO objects VALUES('auth','admin',?)", (json.dumps(record),)
                )
                db.execute("DELETE FROM objects WHERE kind='ticket'")
            self.record = record
            self.cached_digest = self.digest(new)
            try:
                (self.store.directory / "admin.token").unlink(missing_ok=True)
            except OSError:
                pass  # A leftover bootstrap file is ignored once a verifier exists.
            return True
