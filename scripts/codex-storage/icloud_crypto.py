#!/usr/bin/env python3
"""Encrypt, authenticate and decrypt the nightly iCloud packages (icloud_crypto.py).

Package format v1 (`mac-agents-<host>-<YYYYMMDD>-<HHMMSS>.tar.gz.enc`):

    OpenSSL `enc` output  ||  HMAC-SHA256 tag (32 bytes)  ||  b"ORCA-PKG-MAC-v1\\n" (16 bytes)

* The OpenSSL part is `openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -salt` of the
  tar.gz stream: b"Salted__", an 8-byte salt, then the AES-256-CBC ciphertext.
* The tag authenticates every byte before it (encrypt-then-MAC). Its key is derived from the same
  Keychain password with PBKDF2-HMAC-SHA256 over a distinct label plus the OpenSSL salt, so it is
  unrelated to the encryption key and costs as much to guess as the encryption key itself.

Legacy format (packages written before v1): bare `openssl enc -aes-256-cbc -pbkdf2` output with the
OpenSSL default of 10000 iterations and no MAC. `decrypt` still reads it and warns that its
integrity cannot be checked.

Commands (the password is the first line read from --password-fd N; without that option `verify`
and `decrypt` read it from stdin, or prompt when stdin is a terminal):

    tar -czf - ... | icloud_crypto.py encrypt --out PKG --password-fd 3 3< <(password)
    icloud_crypto.py verify  PKG --password-fd 3      # exit 0 = v1 tag valid, 3 = legacy, 1 = bad
    icloud_crypto.py decrypt PKG [--require-mac] > archive.tar.gz

Restoring by hand on any Mac:

    security find-generic-password -s icloud-package-key -w \\
      | python3 icloud_crypto.py decrypt PKG | tar -xzf - -C DEST

Without this helper a v1 package still opens with stock OpenSSL once the 48-byte trailer is cut off
(`head -c $(( $(stat -f %z PKG) - 48 )) PKG | openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000
-md sha256`), and a legacy one with `openssl enc -d -aes-256-cbc -pbkdf2 -md sha256 -in PKG`; both
skip the integrity check.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
from pathlib import Path
import subprocess
import sys
from typing import Optional

OPENSSL = "/usr/bin/openssl"
ITERATIONS = 600_000
LEGACY_ITERATIONS = 10_000
MAGIC = b"ORCA-PKG-MAC-v1\n"
TAG_LEN = 32
TRAILER_LEN = TAG_LEN + len(MAGIC)
MAC_LABEL = b"orca-blueprint/icloud-package/hmac-sha256/v1\x00"
OPENSSL_HEADER = b"Salted__"
HEADER_LEN = len(OPENSSL_HEADER) + 8
CHUNK = 1 << 20

EXIT_OK, EXIT_BAD, EXIT_LEGACY = 0, 1, 3


class PackageError(Exception):
    pass


def read_password(fd: Optional[int], allow_stdin: bool) -> bytes:
    if fd is not None:
        with os.fdopen(fd, "rb", closefd=True) as stream:
            raw = stream.readline()
    elif allow_stdin and sys.stdin.isatty():
        raw = getpass.getpass("Package password: ").encode("utf-8")
    elif allow_stdin:
        raw = sys.stdin.buffer.readline()
    else:
        raise PackageError("a password is required (--password-fd)")
    password = raw.rstrip(b"\r\n")
    if not password:
        raise PackageError("empty password: the Keychain secret is missing")
    return password


def _openssl(args: list, password: bytes, **kwargs) -> subprocess.Popen:
    """Start OpenSSL with the password on a private pipe, never on the command line."""
    r, w = os.pipe()
    try:
        os.write(w, password + b"\n")
    finally:
        os.close(w)
    try:
        return subprocess.Popen([OPENSSL, *args, "-pass", f"fd:{r}"], pass_fds=(r,), **kwargs)
    finally:
        os.close(r)


def _mac_key(password: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password, MAC_LABEL + salt, ITERATIONS, dklen=32)


def _read_salt(path: Path) -> bytes:
    with path.open("rb") as f:
        head = f.read(HEADER_LEN)
    if len(head) != HEADER_LEN or not head.startswith(OPENSSL_HEADER):
        raise PackageError(f"{path.name} is not an OpenSSL salted file")
    return head[len(OPENSSL_HEADER):]


def _tag(path: Path, length: int, key: bytes) -> bytes:
    mac = hmac.new(key, digestmod=hashlib.sha256)
    remaining = length
    with path.open("rb") as f:
        while remaining > 0:
            chunk = f.read(min(CHUNK, remaining))
            if not chunk:
                raise PackageError(f"{path.name} is shorter than expected")
            mac.update(chunk)
            remaining -= len(chunk)
    return mac.digest()


def package_format(path: Path) -> str:
    """'v1' when the file carries the MAC trailer, else 'legacy'."""
    size = path.stat().st_size
    if size < HEADER_LEN + TRAILER_LEN:
        return "legacy"
    with path.open("rb") as f:
        f.seek(size - len(MAGIC))
        return "v1" if f.read(len(MAGIC)) == MAGIC else "legacy"


def encrypt(out: Path, password: bytes) -> None:
    """Encrypt stdin into `out` and append the authentication trailer."""
    proc = _openssl(
        ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(ITERATIONS), "-md", "sha256", "-salt", "-out", str(out)],
        password,
    )
    if proc.wait() != 0:
        raise PackageError(f"openssl enc exited {proc.returncode}")
    os.chmod(out, 0o600)
    length = out.stat().st_size
    tag = _tag(out, length, _mac_key(password, _read_salt(out)))
    with out.open("ab") as f:
        f.write(tag + MAGIC)
        f.flush()
        os.fsync(f.fileno())


def verify(path: Path, password: bytes) -> str:
    """Return 'v1' when the tag is valid or 'legacy' when there is none; raise when it is wrong."""
    if package_format(path) == "legacy":
        return "legacy"
    length = path.stat().st_size - TRAILER_LEN
    with path.open("rb") as f:
        f.seek(length)
        stored = f.read(TAG_LEN)
    expected = _tag(path, length, _mac_key(password, _read_salt(path)))
    if not hmac.compare_digest(stored, expected):
        raise PackageError(f"{path.name}: MAC mismatch (wrong password, or the file was altered or truncated)")
    return "v1"


def decrypt(path: Path, password: bytes, require_mac: bool = False) -> int:
    """Verify, then stream the decrypted tar.gz to stdout. Returns OpenSSL's exit code."""
    fmt = verify(path, password)
    if fmt == "legacy":
        if require_mac:
            raise PackageError(f"{path.name} has no MAC trailer")
        print(f"warning: {path.name} uses the legacy format without a MAC; its integrity is not checked",
              file=sys.stderr)
        length, iterations = path.stat().st_size, LEGACY_ITERATIONS
    else:
        length, iterations = path.stat().st_size - TRAILER_LEN, ITERATIONS
    sys.stdout.flush()
    proc = _openssl(
        ["enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", str(iterations), "-md", "sha256"],
        password,
        stdin=subprocess.PIPE,
    )
    remaining = length
    try:
        with path.open("rb") as f:
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                proc.stdin.write(chunk)
                remaining -= len(chunk)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    return proc.wait()


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Encrypt / verify / decrypt nightly iCloud agent packages.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    enc = sub.add_parser("encrypt", help="encrypt stdin into --out and append the MAC trailer")
    enc.add_argument("--out", required=True, type=Path)
    enc.add_argument("--password-fd", type=int, required=True)
    for name in ("verify", "decrypt"):
        p = sub.add_parser(name)
        p.add_argument("package", type=Path)
        p.add_argument("--password-fd", type=int)
        if name == "decrypt":
            p.add_argument("--require-mac", action="store_true", help="refuse packages without a MAC")
    args = ap.parse_args(argv)
    try:
        password = read_password(args.password_fd, allow_stdin=args.cmd != "encrypt")
        if args.cmd == "encrypt":
            encrypt(args.out, password)
            return EXIT_OK
        if args.cmd == "verify":
            return EXIT_OK if verify(args.package, password) == "v1" else EXIT_LEGACY
        return EXIT_OK if decrypt(args.package, password, args.require_mac) == 0 else EXIT_BAD
    except (PackageError, OSError) as exc:
        print(f"icloud_crypto: {exc}", file=sys.stderr)
        return EXIT_BAD


if __name__ == "__main__":
    sys.exit(main())
