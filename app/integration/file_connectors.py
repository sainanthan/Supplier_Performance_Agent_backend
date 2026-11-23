"""File connectors: CSV / Excel upload and SFTP (DI Phase 4). Read-only towards the source.

Parsing (shared): CSV (UTF-8 / UTF-8-BOM, falls back to detected encoding; delimiter sniffed from , ; TAB |) and Excel .xlsx
(openpyxl, read-only, values not formulas). First non-empty row = header (blank / duplicate names made unique). Caps: 50 MB
per file, 1,000,000 rows. Every row gets _file and _file_time so loads can be incremental (watermark _file_time) and traceable.

Upload store: uploaded files are ENCRYPTED AT REST (Fernet with a data key that lives in the vault - envelope encryption), kept
in integration.files with size, SHA-256 and uploader; nothing is parsed into KPI tables until a mapping runs. Re-uploading the
same content is detected (same SHA-256). Allowed: .csv .txt .tsv .xlsx.

SFTP: the server's host key must match a pinned SHA256 fingerprint (same rule as the SSH tunnel). The connector only lists and
opens files for READING inside the configured folder; it never writes, renames or deletes. Object = a file name pattern
(e.g. "po_*.csv"); extraction reads matching files whose modification time is >= the watermark.
"""
from __future__ import annotations

import csv
import datetime as dt
import fnmatch
import hashlib
import io
import posixpath
import time

from sqlalchemy import text

from .base import BaseConnector, ConnectorMeta, Field, SourceField, SourceObject, TestResult
from .dbx import engine

MAX_FILE = 50 * 1024 * 1024
MAX_ROWS = 1_000_000
EXTS = (".csv", ".txt", ".tsv", ".xlsx")


# ------------------------------------------------------------------ parsing
def _header(raw: list) -> list[str]:
    out, seen = [], {}
    for i, h in enumerate(raw):
        n = (str(h).strip() if h is not None else "") or f"column_{i + 1}"
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 1
        out.append(n)
    return out


def _cell(v):
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, str):
        return v.strip()
    return v


def parse_file(name: str, data: bytes, sheet: str | None = None, delimiter: str | None = None) -> tuple[list[str], list[dict]]:
    if len(data) > MAX_FILE:
        raise ValueError(f"{name}: file larger than {MAX_FILE // 1024 // 1024} MB")
    low = name.lower()
    if low.endswith(".xlsx"):
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:  # noqa: BLE001
            raise ValueError(f"{name}: not a readable .xlsx workbook") from None
        try:
            ws = wb[sheet] if sheet else wb.worksheets[0]
        except KeyError:
            raise ValueError(f"{name}: sheet '{sheet}' not found (sheets: {', '.join(wb.sheetnames)})") from None
        it = ws.iter_rows(values_only=True)
        hdr = None
        for row in it:
            if any(c not in (None, "") for c in row):
                hdr = _header(list(row))
                break
        if hdr is None:
            return [], []
        rows = []
        for row in it:
            if not any(c not in (None, "") for c in row):
                continue
            rows.append({h: _cell(v) for h, v in zip(hdr, row)})
            if len(rows) > MAX_ROWS:
                raise ValueError(f"{name}: more than {MAX_ROWS:,} rows")
        wb.close()
        return hdr, rows
    if not low.endswith((".csv", ".txt", ".tsv")):
        raise ValueError(f"{name}: unsupported file type (allowed: {', '.join(EXTS)})")
    try:
        txt = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        if not best:
            raise ValueError(f"{name}: could not detect the text encoding - save it as UTF-8") from None
        txt = str(best)
    if not delimiter:
        try:
            delimiter = csv.Sniffer().sniff(txt[:20000], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = "\t" if low.endswith(".tsv") else ","
    rd = csv.reader(io.StringIO(txt), delimiter=delimiter)
    hdr = None
    rows = []
    for row in rd:
        if hdr is None:
            if any(c.strip() for c in row):
                hdr = _header(row)
            continue
        if not any(c.strip() for c in row):
            continue
        rows.append({h: (v.strip() if v.strip() != "" else None) for h, v in zip(hdr, row)})
        if len(rows) > MAX_ROWS:
            raise ValueError(f"{name}: more than {MAX_ROWS:,} rows")
    return hdr or [], rows


def _fields(con: BaseConnector, cols: list[str], rows: list[dict]) -> list[SourceField]:
    out = []
    for c in cols + ["_file", "_file_time"]:
        sample = next((r.get(c) for r in rows if r.get(c) not in (None, "")), None)
        t = "number" if isinstance(sample, (int, float)) else "text"
        out.append(SourceField(c, t, True, False, con.is_sensitive(c)))
    return out


# ------------------------------------------------------------------ encrypted upload store
_DEK: list = []


def _fernet():
    """Data-encryption key for uploaded files, created once and kept in the vault (envelope encryption)."""
    from cryptography.fernet import Fernet
    from .secrets import vault
    if not _DEK:
        with engine.begin() as c:
            ref = c.execute(text("SELECT value FROM integration.settings WHERE key = 'files_dek_ref'")).scalar()
            if not ref:
                ref = vault().put({"key": Fernet.generate_key().decode()}, "system")
                c.execute(text("INSERT INTO integration.settings (key, value) VALUES ('files_dek_ref', :r)"), {"r": ref})
        _DEK.append(Fernet(vault().get(ref)["key"].encode()))
    return _DEK[0]


def store_upload(cid: str, name: str, data: bytes, actor: str) -> dict:
    name = posixpath.basename(name.replace("\\", "/")).strip()
    if not name or not name.lower().endswith(EXTS):
        raise ValueError(f"only {', '.join(EXTS)} files can be uploaded")
    if len(data) > MAX_FILE:
        raise ValueError(f"file larger than {MAX_FILE // 1024 // 1024} MB")
    cols, rows = parse_file(name, data)                                  # reject unreadable files at the door
    if not cols:
        raise ValueError(f"{name}: no header row / no data found")
    sha = hashlib.sha256(data).hexdigest()
    with engine.begin() as c:
        dup = c.execute(text("SELECT id FROM integration.files WHERE connection_id = :c AND sha256 = :s AND active"), {"c": cid, "s": sha}).scalar()
        if dup:
            raise ValueError(f"this exact file was already uploaded (file #{dup})")
        fid = c.execute(text("""INSERT INTO integration.files (connection_id, name, size_bytes, sha256, row_count, columns, content_enc, uploaded_by)
                                VALUES (:c, :n, :sz, :s, :rc, CAST(:cols AS jsonb), :ct, :a) RETURNING id"""),
                        {"c": cid, "n": name, "sz": len(data), "s": sha, "rc": len(rows), "cols": __import__("json").dumps(cols),
                         "ct": _fernet().encrypt(data), "a": actor}).scalar()
    return {"id": fid, "name": name, "size_bytes": len(data), "rows": len(rows), "columns": cols, "sha256": sha}


def list_files(cid: str) -> list[dict]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text("""SELECT id, name, size_bytes, sha256, row_count, columns, uploaded_by, uploaded_at
                                                            FROM integration.files WHERE connection_id = :c AND active ORDER BY uploaded_at DESC"""), {"c": cid})]


def delete_file(cid: str, fid: int) -> None:
    with engine.begin() as c:
        n = c.execute(text("UPDATE integration.files SET active = FALSE, content_enc = NULL WHERE id = :i AND connection_id = :c AND active"),
                      {"i": fid, "c": cid}).rowcount
    if not n:
        raise LookupError("file not found")


class CsvExcelConnector(BaseConnector):
    meta = ConnectorMeta("csv_excel", "CSV / Excel upload", "Files & storage",
                         "Upload CSV or Excel (.xlsx) files; they are stored encrypted and loaded through a mapping. Object = file name or pattern.",
                         "file-spreadsheet", "available", "4", True,
                         [Field("sheet", "Excel sheet (optional)", help="Default: the first sheet"),
                          Field("delimiter", "CSV delimiter (optional)", help="Detected automatically")], "files")
    connection_id: str | None = None

    def _files(self, pattern: str | None = None, since=None) -> list[dict]:
        with engine.connect() as c:
            rows = [dict(r._mapping) for r in c.execute(text("""SELECT id, name, uploaded_at FROM integration.files WHERE connection_id = :c AND active
                                                                ORDER BY uploaded_at"""), {"c": self.connection_id})]
        rows = [r for r in rows if not pattern or fnmatch.fnmatch(r["name"].lower(), pattern.lower())]
        return [r for r in rows if since is None or r["uploaded_at"].isoformat() >= str(since)]

    def _read(self, fid: int) -> tuple[str, bytes, dt.datetime]:
        with engine.connect() as c:
            r = c.execute(text("SELECT name, content_enc, uploaded_at FROM integration.files WHERE id = :i AND connection_id = :c AND active"),
                          {"i": fid, "c": self.connection_id}).fetchone()
        if not r:
            raise LookupError("file not found")
        return r[0], _fernet().decrypt(bytes(r[1])), r[2]

    def _rows(self, fid: int) -> list[dict]:
        name, data, ts = self._read(fid)
        _, rows = parse_file(name, data, self.config.get("sheet") or None, self.config.get("delimiter") or None)
        for r in rows:
            r["_file"], r["_file_time"] = name, ts.isoformat()
        return rows

    def test_connection(self) -> TestResult:
        n = len(self._files())
        return TestResult(True, f"Ready - {n} file(s) uploaded." if n else "Ready - upload a file to start.",
                          None, {"files": n, "storage": "encrypted at rest (vault data key)", "session": "read-only", "warnings": []})

    def discover_schema(self) -> list[SourceObject]:
        latest = {}
        for f in self._files():
            latest[f["name"]] = f
        out = []
        for name, f in sorted(latest.items()):
            rows = self._rows(f["id"])
            cols = [c for c in (rows[0].keys() if rows else []) if not c.startswith("_file")]
            out.append(SourceObject(name, "file", _fields(self, cols, rows[:200]), len(rows)))
        return out

    def _preview(self, obj: str, limit: int) -> list[dict]:
        fs = self._files(obj)
        if not fs:
            raise ValueError(f"no uploaded file matches '{obj}'")
        return self._rows(fs[-1]["id"])[:limit]

    def extract(self, obj, watermark_column=None, since=None, batch_size: int = 5000):
        for f in self._files(obj, since if watermark_column == "_file_time" else None):
            rows = self._rows(f["id"])
            for i in range(0, len(rows), batch_size):
                yield rows[i:i + batch_size]


# ------------------------------------------------------------------ SFTP
class SftpConnector(BaseConnector):
    meta = ConnectorMeta("sftp", "SFTP", "Files & storage",
                         "Pick up CSV / Excel files from an SFTP folder (pinned host key; list + read only).", "folder-sync", "available", "4", True,
                         [Field("host", "Host", required=True), Field("port", "Port", "number", True, 22), Field("username", "Username", required=True,
                                                                                                            help="Read-only account"),
                          Field("host_fingerprint", "Server host-key fingerprint", required=True, help="SHA256:... from the server owner - any other key is refused"),
                          Field("password", "Password", "secret"), Field("private_key", "Private key", "secret_text", help="OpenSSH / PEM; stored in the vault"),
                          Field("key_passphrase", "Key passphrase", "secret"),
                          Field("folder", "Folder", required=True, default="/outbound"), Field("pattern", "File pattern", default="*.csv"),
                          Field("sheet", "Excel sheet (optional)", group="advanced"), Field("delimiter", "CSV delimiter (optional)", group="advanced"),
                          Field("internal_network", "Server is on the internal network", "boolean", default=False, group="advanced")], "files")
    environment = "DEV"

    @classmethod
    def policy_errors(cls, config, secrets, environment):
        errs = []
        if not secrets.get("password") and not secrets.get("private_key"):
            errs.append("a password or a private key is required")
        if not str(config.get("host_fingerprint") or "").strip():
            errs.append("the server host-key fingerprint is required (no trust-on-first-use)")
        if ".." in str(config.get("folder") or "").split("/"):
            errs.append("the folder may not contain '..'")
        return errs

    def _connect(self):
        import paramiko
        from .http_base import check_host
        from .tunnel import _PinnedHostKey, _load_key
        check_host(self.config.get("host"), bool(self.config.get("internal_network")), self.environment)
        cl = paramiko.SSHClient()
        cl.set_missing_host_key_policy(_PinnedHostKey(self.config.get("host_fingerprint")))
        pkey = _load_key(self.secrets["private_key"], self.secrets.get("key_passphrase")) if self.secrets.get("private_key") else None
        cl.connect(self.config["host"], int(self.config.get("port") or 22), username=self.config.get("username"), password=self.secrets.get("password"),
                   pkey=pkey, timeout=15, banner_timeout=15, auth_timeout=15, look_for_keys=False, allow_agent=False)
        return cl, cl.open_sftp()

    def _folder(self) -> str:
        return posixpath.normpath(self.config.get("folder") or "/")

    def _list(self, sftp, pattern: str | None, since=None) -> list:
        import stat as st
        pat = (pattern or self.config.get("pattern") or "*").lower()
        items = [a for a in sftp.listdir_attr(self._folder()) if st.S_ISREG(a.st_mode or 0) and fnmatch.fnmatch(a.filename.lower(), pat)
                 and a.filename.lower().endswith(EXTS)]
        items.sort(key=lambda a: a.st_mtime or 0)
        if since is not None:
            items = [a for a in items if dt.datetime.fromtimestamp(a.st_mtime, dt.timezone.utc).isoformat() >= str(since)]
        return items[:5000]

    def _rows(self, sftp, a) -> list[dict]:
        if (a.st_size or 0) > MAX_FILE:
            raise ValueError(f"{a.filename}: larger than {MAX_FILE // 1024 // 1024} MB")
        with sftp.open(posixpath.join(self._folder(), a.filename), "rb") as fh:     # read-only open
            data = fh.read(MAX_FILE + 1)
        _, rows = parse_file(a.filename, data, self.config.get("sheet") or None, self.config.get("delimiter") or None)
        ts = dt.datetime.fromtimestamp(a.st_mtime, dt.timezone.utc).isoformat()
        for r in rows:
            r["_file"], r["_file_time"] = a.filename, ts
        return rows

    def test_connection(self) -> TestResult:
        t0 = time.perf_counter()
        try:
            cl, sftp = self._connect()
            try:
                files = self._list(sftp, None)
            finally:
                sftp.close()
                cl.close()
        except Exception as e:  # noqa: BLE001
            from .db_connectors import explain
            code, raw, hint = explain(e)
            return TestResult(False, f"Could not connect ({code.lower()}).", hint, {"category": code, "error": raw})
        return TestResult(True, f"Connected - {len(files)} matching file(s) in {self._folder()}.", None,
                          {"latency_ms": round((time.perf_counter() - t0) * 1000), "files": len(files), "host_key": "pinned and verified",
                           "session": "list + read only", "tls": True, "warnings": []})

    def discover_schema(self) -> list[SourceObject]:
        cl, sftp = self._connect()
        try:
            files = self._list(sftp, None)
            if not files:
                return []
            rows = self._rows(sftp, files[-1])
        finally:
            sftp.close()
            cl.close()
        cols = [c for c in (rows[0].keys() if rows else []) if not c.startswith("_file")]
        return [SourceObject(self.config.get("pattern") or "*", "file pattern", _fields(self, cols, rows[:200]), None)]

    def _preview(self, obj: str, limit: int) -> list[dict]:
        cl, sftp = self._connect()
        try:
            files = self._list(sftp, obj)
            if not files:
                raise ValueError(f"no file matches '{obj}' in {self._folder()}")
            return self._rows(sftp, files[-1])[:limit]
        finally:
            sftp.close()
            cl.close()

    def extract(self, obj, watermark_column=None, since=None, batch_size: int = 5000):
        cl, sftp = self._connect()
        try:
            for a in self._list(sftp, obj, since if watermark_column == "_file_time" else None):
                rows = self._rows(sftp, a)
                for i in range(0, len(rows), batch_size):
                    yield rows[i:i + batch_size]
        finally:
            sftp.close()
            cl.close()


FILE_CONNECTORS = {c.meta.key: c for c in (CsvExcelConnector, SftpConnector)}
