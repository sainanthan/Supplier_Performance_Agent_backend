"""SSH tunnel for databases that are not internet-accessible (DI Phase 2).

- The SSH server's host key MUST match a pinned SHA256 fingerprint (no trust-on-first-use, no auto-add).
- Key or password authentication; both come from the vault, never from config. No agent / no local key files are used.
- Forwards a random 127.0.0.1 port to remote_host:remote_port through the bastion; closed when the operation ends.
For sources inside a private network with no bastion, the on-prem agent (later phase) replaces this.
"""
from __future__ import annotations

import base64
import hashlib
import io
import select
import socket
import threading

import paramiko


def fingerprint(key: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class _PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str):
        self.expected = (expected or "").strip()
        if self.expected and not self.expected.startswith("SHA256:"):
            self.expected = "SHA256:" + self.expected

    def missing_host_key(self, client, hostname, key):
        got = fingerprint(key)
        if not self.expected:
            raise paramiko.SSHException(f"SSH host key not pinned. The server presented {got} - verify it with IT and enter it as the host fingerprint.")
        if got != self.expected:
            raise paramiko.SSHException(f"SSH host key mismatch: expected {self.expected}, server presented {got}. Do NOT connect until IT confirms the key.")


def _load_key(text: str, passphrase: str | None):
    errors = []
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{cls.__name__}: {e}")
    raise ValueError("SSH private key could not be read (supported: Ed25519, ECDSA, RSA in OpenSSH/PEM format)")


class SshTunnel:
    def __init__(self, ssh_host: str, ssh_port: int, ssh_user: str, host_fingerprint: str, remote_host: str, remote_port: int,
                 private_key: str | None = None, key_passphrase: str | None = None, password: str | None = None, timeout: float = 10):
        self.cfg = (ssh_host, int(ssh_port or 22), ssh_user)
        self.fp, self.remote = host_fingerprint, (remote_host, int(remote_port))
        self.pkey = _load_key(private_key, key_passphrase) if private_key else None
        self.password, self.timeout = password, timeout
        self.client = self.server = None
        self._stop = threading.Event()

    def __enter__(self):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(_PinnedHostKey(self.fp))
        h, p, u = self.cfg
        self.client.connect(h, p, username=u, pkey=self.pkey, password=self.password, timeout=self.timeout,
                            banner_timeout=self.timeout, auth_timeout=self.timeout, look_for_keys=False, allow_agent=False)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.server.settimeout(0.5)
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    @property
    def local_port(self) -> int:
        return self.server.getsockname()[1]

    def _accept(self):
        while not self._stop.is_set():
            try:
                sock, peer = self.server.accept()
            except (socket.timeout, OSError):
                continue
            try:
                chan = self.client.get_transport().open_channel("direct-tcpip", self.remote, peer)
            except Exception:  # noqa: BLE001
                sock.close()
                continue
            threading.Thread(target=self._pump, args=(sock, chan), daemon=True).start()

    def _pump(self, sock, chan):
        try:
            while not self._stop.is_set():
                r, _, _ = select.select([sock, chan], [], [], 0.5)
                if sock in r:
                    data = sock.recv(32768)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(32768)
                    if not data:
                        break
                    sock.sendall(data)
        except Exception:  # noqa: BLE001
            pass
        finally:
            chan.close()
            sock.close()

    def __exit__(self, *exc):
        self._stop.set()
        for x in (self.server, self.client):
            try:
                x and x.close()
            except Exception:  # noqa: BLE001
                pass
