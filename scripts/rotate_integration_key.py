"""Rotate the Data Integration master key (local encrypted vault).

    python -m scripts.rotate_integration_key            # rotate: new key in front, re-encrypt ALL secrets, verify, retire old key
    python -m scripts.rotate_integration_key --keep-old # same, but keep the old key as a fallback (retire later by re-running)
    python -m scripts.rotate_integration_key --status   # show key fingerprints and how many secrets each key protects

Safety
- The key file is backed up first (backend/.integration_key.bak-<timestamp>, gitignored) - keep it until you have verified.
- Re-encryption is all-or-nothing (vault.rotate): if any secret cannot be decrypted, nothing changes.
- Every secret is then decrypted with the NEW key alone before the old key is retired.
- Only key FINGERPRINTS are printed - never key material or secret values.
- Restart the backend afterwards (each process caches the vault).
When the keys come from env INTEGRATION_MASTER_KEYS (or Azure Key Vault), rotate there instead: put the new key in FRONT of the
list, restart, run this script with --reencrypt-only, then remove the old key and restart again.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
from pathlib import Path


def _fp(k: str) -> str:
    from app.integration.secrets import LocalEncryptedVault
    return LocalEncryptedVault._fp(k)


def status() -> dict:
    from sqlalchemy import text
    from app.integration.dbx import engine
    from app.integration.secrets import LocalEncryptedVault
    v = LocalEncryptedVault()
    with engine.connect() as c:
        per = {r[0]: r[1] for r in c.execute(text("SELECT key_fingerprint, count(*) FROM integration.secrets GROUP BY 1"))}
    return {"keys": [_fp(k) for k in v._keys], "secrets_per_key_fingerprint": per}


def rotate_key_file(key_file: Path, keep_old: bool = False, refs: list[str] | None = None) -> dict:
    from cryptography.fernet import Fernet
    from app.integration import secrets as S
    if os.environ.get("INTEGRATION_MASTER_KEYS"):
        raise SystemExit("Keys come from env INTEGRATION_MASTER_KEYS - rotate there (see the module docstring).")
    old = [k.strip() for k in key_file.read_text(encoding="utf-8").replace("\n", ",").split(",") if k.strip()]
    backup = key_file.with_name(f"{key_file.name}.bak-{dt.datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(key_file, backup)
    new = Fernet.generate_key().decode()
    key_file.write_text(",".join([new, *old]), encoding="utf-8")
    try:
        v = S.LocalEncryptedVault()
        n = v.rotate(refs)                                           # all-or-nothing
        only_new = S.LocalEncryptedVault.__new__(S.LocalEncryptedVault)
        from cryptography.fernet import MultiFernet
        only_new._keys, only_new._f = [new], MultiFernet([Fernet(new.encode())])
        from sqlalchemy import text
        from app.integration.dbx import engine
        with engine.connect() as c:
            all_refs = refs or [r[0] for r in c.execute(text("SELECT ref FROM integration.secrets"))]
        for ref in all_refs:
            only_new.get(ref)                                        # raises if anything still needs the old key
    except Exception:
        shutil.copy2(backup, key_file)                               # restore: nothing retired
        raise
    if not keep_old:
        key_file.write_text(new, encoding="utf-8")
    return {"reencrypted": n, "verified": len(all_refs), "new_key": _fp(new), "retired": [] if keep_old else [_fp(k) for k in old],
            "backup": str(backup), "next": "Restart the backend so every process loads the new key."}


def main():
    from app.integration.secrets import _KEY_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--keep-old", action="store_true")
    ap.add_argument("--reencrypt-only", action="store_true", help="env-managed keys: re-encrypt with the newest configured key")
    a = ap.parse_args()
    if a.status:
        print(status())
        return
    if a.reencrypt_only:
        from app.integration.secrets import LocalEncryptedVault
        print({"reencrypted": LocalEncryptedVault().rotate(None)})
        return
    out = rotate_key_file(Path(_KEY_FILE), a.keep_old)
    from app.integration import service as S
    S.audit("operator", "vault:key_rotated", None, {k: out[k] for k in ("reencrypted", "verified", "new_key", "retired")})
    print(out)


if __name__ == "__main__":
    main()
