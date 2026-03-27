"""
server.py
TLS socket server that receives metrics from network_downloader.py clients.

Features:
  - Auto-generates a self-signed TLS certificate on first run
  - Handles multiple concurrent clients via threads
  - Supports PING, STATUS, and METRIC commands
  - Gracefully handles SSL handshake failures and abrupt disconnections
  - Tracks per-session stats in a thread-safe counter
  - Clean Ctrl-C shutdown

Usage:
    python server.py
    python server.py --host 0.0.0.0 --port 5000
"""

import argparse
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000
CERT_FILE    = "server.pem"
KEY_FILE     = "server.key"
RECV_TIMEOUT = 30   # seconds a client has to send its first byte
BACKLOG      = 10   # max queued connections

# ── Shared state (protected by _lock) ─────────────────────────────────────────
_lock            = threading.Lock()
_total_conn      = 0
_total_metrics   = 0
_start_time      = time.time()
_server_running  = True   # cleared by signal handler to stop accept() loop


# ── Certificate helpers ───────────────────────────────────────────────────────

def generate_cert(cert_file: str = CERT_FILE, key_file: str = KEY_FILE) -> None:
    """
    Generate a self-signed RSA-2048 certificate with openssl if the files
    do not already exist.  Exits with a clear error message when openssl is
    not available.
    """
    if os.path.exists(cert_file) and os.path.exists(key_file):
        print(f"[CERT] Using existing certificate ({cert_file}, {key_file}).")
        return

    print("[CERT] Generating self-signed TLS certificate ...")
    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", key_file, "-out", cert_file,
                "-days", "365", "-nodes",
                "-subj", "/CN=localhost/O=NetworkAnalyzer/C=IN",
            ],
            check=True,
            capture_output=True,
        )
        print(f"[CERT] Generated {cert_file}  and  {key_file}.")
    except FileNotFoundError:
        sys.exit(
            "[ERROR] openssl is not installed.\n"
            "        Install it, or place server.pem and server.key manually."
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(f"[ERROR] Certificate generation failed:\n{exc.stderr.decode()}")


# ── Client handler ────────────────────────────────────────────────────────────

def handle_client(conn: ssl.SSLSocket, addr: tuple) -> None:
    """
    Serve one TLS client connection in its own thread.

    Protocol
    --------
    Client → Server          Server → Client
    ──────────────────────   ─────────────────────────────
    PING                     PONG
    STATUS                   CONNECTIONS:<n>,METRICS:<n>,UPTIME:<s>
    METRIC,<run>,<speed>,    ACK
      <duration>,<status>
    (anything else)          ERR:UNKNOWN_CMD
    """
    global _total_conn, _total_metrics

    with _lock:
        _total_conn += 1

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[+] Connected : {addr}  [{ts}]")

    try:
        conn.settimeout(RECV_TIMEOUT)
        raw = conn.recv(4096)

        if not raw:
            # Client connected but sent nothing (abrupt close)
            try:
                conn.send(b"ERR:EMPTY")
            except OSError:
                pass
            return

        message = raw.decode("utf-8").strip()

        # ── PING ──────────────────────────────────────────────────────────────
        if message == "PING":
            conn.send(b"PONG")

        # ── STATUS ────────────────────────────────────────────────────────────
        elif message == "STATUS":
            with _lock:
                payload = (
                    f"CONNECTIONS:{_total_conn},"
                    f"METRICS:{_total_metrics},"
                    f"UPTIME:{int(time.time() - _start_time)}"
                )
            conn.send(payload.encode())

        # ── METRIC ────────────────────────────────────────────────────────────
        elif message.startswith("METRIC,"):
            parts = message.split(",")
            if len(parts) != 5:
                conn.send(b"ERR:BAD_FORMAT")
                print(f"[!] Bad METRIC from {addr}: {message!r}")
                return

            _, run, speed, duration, status = parts

            with _lock:
                _total_metrics += 1

            ts2 = datetime.now().strftime("%H:%M:%S")
            print(
                f"[DATA] {addr}  run={run}  speed={speed} Mbps  "
                f"duration={duration}s  status={status}  [{ts2}]"
            )
            conn.send(b"ACK")

        # ── Unknown command ───────────────────────────────────────────────────
        else:
            conn.send(b"ERR:UNKNOWN_CMD")
            print(f"[!] Unknown command from {addr}: {message[:80]!r}")

    # ── Exception handling ────────────────────────────────────────────────────
    except ssl.SSLError as exc:
        # Covers handshake failures and mid-stream TLS errors
        print(f"[!] TLS error with {addr}: {exc}")

    except socket.timeout:
        print(f"[!] Timeout waiting for data from {addr} (>{RECV_TIMEOUT}s).")

    except ConnectionResetError:
        print(f"[!] Connection reset by {addr} (client disconnected abruptly).")

    except UnicodeDecodeError:
        print(f"[!] Encoding error from {addr} — non-UTF-8 payload.")
        try:
            conn.send(b"ERR:ENCODING")
        except OSError:
            pass

    except OSError as exc:
        print(f"[!] Socket error with {addr}: {exc}")

    finally:
        try:
            conn.close()
        except OSError:
            pass
        print(f"[-] Disconnected: {addr}")


# ── Signal handler ────────────────────────────────────────────────────────────

def _on_signal(sig, frame):          # noqa: ARG001
    global _server_running
    print("\n[SERVER] Caught signal — shutting down gracefully ...")
    _server_running = False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global _start_time

    parser = argparse.ArgumentParser(description="TLS metric-collection server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--cert", default=CERT_FILE)
    parser.add_argument("--key",  default=KEY_FILE)
    args = parser.parse_args()

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    generate_cert(args.cert, args.key)

    # Build TLS context (server side)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=args.cert, keyfile=args.key)
    context.minimum_version = ssl.TLSVersion.TLSv1_2   # reject old TLS

    # Create raw TCP socket, bind, and listen
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        raw_sock.bind((args.host, args.port))
    except OSError as exc:
        sys.exit(f"[ERROR] Cannot bind to {args.host}:{args.port} — {exc}")
    raw_sock.listen(BACKLOG)

    _start_time = time.time()
    print(f"[TLS SERVER] Listening on {args.host}:{args.port}  (TLS 1.2+)")
    print(f"[TLS SERVER] Cert: {args.cert}  |  Key: {args.key}")
    print(f"[TLS SERVER] Press Ctrl-C to stop.\n")

    # Wrap the listening socket with TLS
    with context.wrap_socket(raw_sock, server_side=True) as tls_server:
        # Set a short accept() timeout so the _server_running flag is polled
        tls_server.settimeout(1.0)

        while _server_running:
            try:
                conn, addr = tls_server.accept()
            except socket.timeout:
                # Normal periodic timeout — check _server_running and loop
                continue
            except ssl.SSLError as exc:
                # TLS handshake failure during accept (e.g. self-signed cert
                # rejected by a non-permissive client, or a port scanner)
                print(f"[!] TLS handshake failed on incoming connection: {exc}")
                continue
            except OSError:
                # Socket closed by signal handler
                break

            t = threading.Thread(
                target=handle_client, args=(conn, addr), daemon=True
            )
            t.start()

    with _lock:
        print(
            f"\n[SERVER] Stopped.  "
            f"Total connections: {_total_conn}  |  "
            f"Total metrics received: {_total_metrics}"
        )


if __name__ == "__main__":
    main()