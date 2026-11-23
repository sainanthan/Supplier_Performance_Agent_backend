"""Secrets vault for connection credentials (DI Phase 1).

Rule: passwords, tokens and keys are never stored in plain text - not in code, not in the database, not in logs, never
returned by the API (masked only). Connections hold a `secret_ref`; the vault resolves it at run time.

Providers (env INTEGRATION_VAULT):
  local  (default) encrypted store: Fernet (AES-128-CBC + HMAC-SHA256) ciphertext in integration.secrets; the master key
         lives OUTSIDE the database - env INTEGRATION_MASTER_KEYS (comma-separated, newest first, enables rotation) or
         the gitignored file backend/.integration_key (created on first use).
  azure  Azure Key Vault (env AZURE_KEY_VAULT_URL + DefaultAzureCredential). Same interface; swap without code changes.
"""
from __future__ import annotations

import abc
import hashlib
import json
import os
import uuid

from sqlalchemy import text

from ..config import BACKEND_DIR

_KEY_FILE = BACKEND_DIR / ".integration_key"


class Vault(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def put(self, values: dict, actor: str) -> str: ...          # returns secret_ref

    @abc.abstractmethod
    def get(self, ref: str) -> dict: ...

    @abc.abstractmethod
    def replace(self, ref: str, values: dict, actor: str) -> None: ...

    @abc.abstractmethod
    def delete(self, ref: str) -> None: ...

    def describe(self) -> dict:
        return {"provider": self.name}


class LocalEncryptedVault(Vault):
    name = "local-encrypted"

    def __init__(self):
        from cryptography.fernet import Fernet, MultiFernet
        keys = [k.strip() for k in os.environ.get("INTEGRATION_MASTER_KEYS", "").split(",") if k.strip()]
        if not keys:
            if not _KEY_FILE.exists():
                _KEY_FILE.write_text(Fernet.generate_key().decode(), encoding="utf-8")
            keys = [k.strip() for k in _KEY_FILE.read_text(encoding="utf-8").replace("\n", ",").split(",") if k.strip()]   # newest first
        self._keys = keys
        self._f = MultiFernet([Fernet(k.encode()) for k in keys])

    @staticmethod
    def _fp(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def put(self, values: dict, actor: str) -> str:
        from .dbx import engine
        ref = f"vault:local:{uuid.uuid4()}"
        token = self._f.encrypt(json.dumps(values).encode()).decode()
        with engine.begin() as c:
            c.execute(text("""INSERT INTO integration.secrets (ref, ciphertext, key_fingerprint, created_by)
                              VALUES (:r, :c, :k, :a)"""), {"r": ref, "c": token, "k": self._fp(self._keys[0]), "a": actor})
        return ref

    def get(self, ref: str) -> dict:
        from .dbx import engine
        with engine.connect() as c:
            row = c.execute(text("SELECT ciphertext FROM integration.secrets WHERE ref = :r"), {"r": ref}).fetchone()
        if not row:
            raise KeyError("secret not found")
        return json.loads(self._f.decrypt(row[0].encode()))

    def replace(self, ref: str, values: dict, actor: str) -> None:
        from .dbx import engine
        token = self._f.encrypt(json.dumps(values).encode()).decode()
        with engine.begin() as c:
            c.execute(text("""UPDATE integration.secrets SET ciphertext = :c, key_fingerprint = :k, rotated_at = now(), created_by = :a
                              WHERE ref = :r"""), {"r": ref, "c": token, "k": self._fp(self._keys[0]), "a": actor})

    def delete(self, ref: str) -> None:
        from .dbx import engine
        with engine.begin() as c:
            c.execute(text("DELETE FROM integration.secrets WHERE ref = :r"), {"r": ref})

    def rotate(self, refs: list[str] | None = None) -> int:
        """Re-encrypt secrets with the newest key (after adding a new key in FRONT of INTEGRATION_MASTER_KEYS).
        All-or-nothing: every secret is decrypted first; if any cannot be decrypted with the configured keys, nothing is
        changed and the unreadable refs are reported (an unreadable secret means a key is missing - never drop it)."""
        from cryptography.fernet import InvalidToken
        from .dbx import engine
        with engine.begin() as c:
            q = "SELECT ref, ciphertext FROM integration.secrets" + (" WHERE ref = ANY(:r)" if refs else "")
            rows = c.execute(text(q), {"r": refs} if refs else {}).fetchall()
            new, bad = [], []
            for ref, ct in rows:
                try:
                    new.append((ref, self._f.rotate(ct.encode()).decode()))
                except InvalidToken:
                    bad.append(ref)
            if bad:
                raise RuntimeError(f"rotation aborted: {len(bad)} secret(s) cannot be decrypted with the configured keys: {bad[:5]}")
            for ref, ct in new:
                c.execute(text("UPDATE integration.secrets SET ciphertext = :c, key_fingerprint = :k, rotated_at = now() WHERE ref = :r"),
                          {"c": ct, "k": self._fp(self._keys[0]), "r": ref})
        return len(new)

    def describe(self) -> dict:
        return {"provider": self.name, "key_fingerprint": self._fp(self._keys[0]), "keys_configured": len(self._keys),
                "key_source": "env INTEGRATION_MASTER_KEYS" if os.environ.get("INTEGRATION_MASTER_KEYS") else "file backend/.integration_key (gitignored)"}


class AzureKeyVault(Vault):
    """Azure Key Vault adapter (enable with INTEGRATION_VAULT=azure). Needs: pip install azure-identity azure-keyvault-secrets,
    env AZURE_KEY_VAULT_URL, and a managed identity / service principal with get/set/delete on secrets."""
    name = "azure-key-vault"

    def __init__(self):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError("Azure Key Vault adapter needs: pip install azure-identity azure-keyvault-secrets") from e
        self._c = SecretClient(vault_url=os.environ["AZURE_KEY_VAULT_URL"], credential=DefaultAzureCredential())

    def put(self, values: dict, actor: str) -> str:
        name = f"spa-conn-{uuid.uuid4()}"
        self._c.set_secret(name, json.dumps(values), tags={"created_by": actor})
        return f"vault:azure:{name}"

    def get(self, ref: str) -> dict:
        return json.loads(self._c.get_secret(ref.split(":", 2)[2]).value)

    def replace(self, ref: str, values: dict, actor: str) -> None:
        self._c.set_secret(ref.split(":", 2)[2], json.dumps(values), tags={"created_by": actor})

    def delete(self, ref: str) -> None:
        self._c.begin_delete_secret(ref.split(":", 2)[2])


_VAULT: Vault | None = None


def vault() -> Vault:
    global _VAULT
    if _VAULT is None:
        _VAULT = AzureKeyVault() if os.environ.get("INTEGRATION_VAULT", "local") == "azure" else LocalEncryptedVault()
    return _VAULT


def mask(value) -> str:
    """What the UI may show for a secret: never the value, only that it is set and its length class."""
    if value in (None, ""):
        return ""
    s = str(value)
    return "••••••••" + (s[-2:] if len(s) >= 12 else "")
