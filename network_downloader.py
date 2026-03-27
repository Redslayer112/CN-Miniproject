"""
network_downloader.py
Downloads a 100 MB test file every hour for 24 hours and logs results to CSV.

New in this version
───────────────────
  • Real latency measurement via timed TCP connect() probes (no ICMP/root needed)
  • Real packet-loss measurement via TCP connection-attempt success rate
  • PING handshake to server before starting (gracefully skips if server is down)
  • Per-exception error handling for SSL, timeout, and connection errors
  • Cleaner console output with run separator lines

Compatible with network_analyzer.py.

Usage:
    python network_downloader.py              # TEST_MODE=True  (3 runs, 10 s gap)
    # set TEST_MODE=False below for a real 24-run / 1-hour session
"""

import csv
import socket
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    sys.exit("Missing dependency — run:  pip install requests")

# ── Configuration ─────────────────────────────────────────────────────────────
TEST_MODE  = True           # True = 3 runs / 10 s  |  False = 24 runs / 1 h
URL        = "https://proof.ovh.net/files/100Mb.dat"
CSV_PATH   = "download_log.csv"
CHUNK_SIZE = 1024 * 256     # 256 KB streaming chunks
MIN_BYTES  = 1024 * 1024    # reject responses < 1 MB (blocked / empty)

RUNS     = 3    if TEST_MODE else 24
INTERVAL = 10   if TEST_MODE else 3600   # seconds between successive runs

# Metrics server (server.py)
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000

# Probe settings for latency / packet-loss (raw TCP, no root required)
PROBE_HOST     = "proof.ovh.net"   # same host as the download target
PROBE_PORT     = 443
LATENCY_PROBES = 5    # number of TCP round-trips averaged for latency
LOSS_PROBES    = 10   # connection attempts used to estimate packet loss

CSV_HEADERS = [
    "run_number", "start_time", "end_time",
    "duration_s", "speed_Mbps", "packet_loss_pct", "latency_ms", "status",
]

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# ── Timestamp helper ──────────────────────────────────────────────────────────

def now_ts() -> str:
    """Return current time as HH:MM:SS:mmm."""
    t = datetime.now()
    return t.strftime("%H:%M:%S:") + f"{t.microsecond // 1000:03d}"


# ── CSV helpers ───────────────────────────────────────────────────────────────

def init_csv() -> None:
    """Create CSV with header row if the file does not exist or is empty."""
    p = Path(CSV_PATH)
    if not p.exists() or p.stat().st_size == 0:
        with open(CSV_PATH, "w", newline="") as fh:
            csv.writer(fh).writerow(CSV_HEADERS)


def write_row(row: dict) -> None:
    """Append one result row to the CSV in column order."""
    with open(CSV_PATH, "a", newline="") as fh:
        csv.writer(fh).writerow([row[h] for h in CSV_HEADERS])


# ── Network probe helpers ─────────────────────────────────────────────────────

def measure_latency(
    host: str = PROBE_HOST,
    port: int = PROBE_PORT,
    samples: int = LATENCY_PROBES,
) -> float:
    """
    Measure TCP connection latency (ms) by timing socket.create_connection()
    in a loop.  Successful timings are averaged; failed attempts are ignored.
    Returns 0.0 when every attempt fails.
    """
    times: list[float] = []
    for _ in range(samples):
        try:
            t0 = time.perf_counter()
            with socket.create_connection((host, port), timeout=5):
                pass
            times.append((time.perf_counter() - t0) * 1000)
        except OSError:
            pass
    return round(sum(times) / len(times), 2) if times else 0.0


def measure_packet_loss(
    host: str = PROBE_HOST,
    port: int = PROBE_PORT,
    samples: int = LOSS_PROBES,
) -> float:
    """
    Estimate packet loss (%) by counting failed TCP connection attempts out of
    `samples` total.  A failed attempt (timeout / refused / reset) counts as a
    lost packet because the SYN was never acknowledged.
    """
    failed = 0
    for _ in range(samples):
        try:
            with socket.create_connection((host, port), timeout=2):
                pass
        except OSError:
            failed += 1
    return round(failed / samples * 100, 1)


# ── URL reachability check ────────────────────────────────────────────────────

def check_url(url: str) -> bool:
    """
    Return True if the URL responds with 200 and Content-Length ≥ MIN_BYTES.
    Uses HTTP HEAD to avoid downloading the whole file.
    """
    try:
        r = requests.head(
            url, timeout=8, verify=False,
            headers=HTTP_HEADERS, allow_redirects=True,
        )
        return (
            r.status_code == 200
            and int(r.headers.get("Content-Length", 0)) >= MIN_BYTES
        )
    except Exception:
        return False


# ── Server communication ──────────────────────────────────────────────────────

def _tls_context() -> ssl.SSLContext:
    """Return a permissive SSL context (self-signed cert, no hostname check)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def ping_server() -> bool:
    """
    Send PING to the metrics server over a raw TLS socket.
    Returns True if the server replies PONG, False otherwise.
    """
    try:
        with socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=5) as sock:
            with _tls_context().wrap_socket(
                sock, server_hostname=SERVER_HOST
            ) as ssock:
                ssock.sendall(b"PING")
                return ssock.recv(64).decode().strip() == "PONG"
    except Exception:
        return False


def send_to_server(run: int, speed: float, duration: float, status: str) -> None:
    """
    Send a METRIC message to the TLS metrics server using a raw socket.
    Logs a warning on any network or TLS error, but never raises.

    Protocol line:  METRIC,<run>,<speed_Mbps>,<duration_s>,<status>
    Server replies: ACK  (or an ERR:… string)
    """
    try:
        with socket.create_connection(
            (SERVER_HOST, SERVER_PORT), timeout=10
        ) as sock:
            with _tls_context().wrap_socket(
                sock, server_hostname=SERVER_HOST
            ) as ssock:
                payload = f"METRIC,{run},{speed},{duration},{status}"
                ssock.sendall(payload.encode())
                ssock.settimeout(5)
                reply = ssock.recv(1024).decode().strip()
                print(f"  [SERVER] {reply}")

    except ssl.SSLError as exc:
        print(f"  [WARN] TLS error sending to server: {exc}")
    except socket.timeout:
        print(f"  [WARN] Server timed out — no reply received.")
    except ConnectionRefusedError:
        print(
            f"  [WARN] Server refused connection "
            f"({SERVER_HOST}:{SERVER_PORT}) — is server.py running?"
        )
    except OSError as exc:
        print(f"  [WARN] Network error sending to server: {exc}")


# ── Download runner ───────────────────────────────────────────────────────────

def run_download(n: int) -> None:
    """
    Probe network health, download the test file, log metrics to CSV,
    and forward the result to the metrics server.
    """
    print(f"\n{'─' * 54}")
    print(f"  Run {n}/{RUNS}  started at {now_ts()}")

    # --- Network health probes (raw TCP sockets, no root) --------------------
    print(
        f"  Measuring latency ({LATENCY_PROBES} TCP probes to "
        f"{PROBE_HOST}:{PROBE_PORT}) ...",
        end=" ", flush=True,
    )
    latency = measure_latency()
    print(f"{latency:.1f} ms")

    print(
        f"  Estimating packet loss ({LOSS_PROBES} TCP probes) ...",
        end=" ", flush=True,
    )
    loss = measure_packet_loss()
    print(f"{loss:.1f}%")

    # --- Download ------------------------------------------------------------
    start    = now_ts()
    t0       = time.perf_counter()
    bytes_rx = 0

    try:
        with requests.get(
            URL, stream=True, timeout=300,
            verify=False, headers=HTTP_HEADERS,
        ) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    bytes_rx += len(chunk)

        duration   = time.perf_counter() - t0
        speed_mbps = (bytes_rx * 8) / (duration * 1_000_000)

        if bytes_rx < MIN_BYTES:
            raise ValueError(
                f"Only {bytes_rx} bytes received — "
                "server may have blocked the request."
            )

        write_row({
            "run_number":      n,
            "start_time":      start,
            "end_time":        now_ts(),
            "duration_s":      round(duration, 3),
            "speed_Mbps":      round(speed_mbps, 4),
            "packet_loss_pct": loss,
            "latency_ms":      latency,
            "status":          "SUCCESS",
        })
        send_to_server(n, round(speed_mbps, 4), round(duration, 3), "SUCCESS")
        print(
            f"  ✓  {speed_mbps:.2f} Mbps  |  "
            f"{bytes_rx / 1e6:.1f} MB  |  {duration:.1f} s"
        )

    except KeyboardInterrupt:
        raise   # propagate so main() can catch it cleanly

    except Exception as exc:
        elapsed = round(time.perf_counter() - t0, 3)
        write_row({
            "run_number":      n,
            "start_time":      start,
            "end_time":        now_ts(),
            "duration_s":      elapsed,
            "speed_Mbps":      "",
            "packet_loss_pct": loss,
            "latency_ms":      latency,
            "status":          "SKIPPED",
        })
        send_to_server(n, 0, elapsed, "SKIPPED")
        print(f"  ✗  SKIPPED  ({type(exc).__name__}: {exc})")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    mode = "TEST (3 runs, 10 s gap)" if TEST_MODE else "PRODUCTION (24 runs, 1 h gap)"
    print(f"\n{'=' * 54}")
    print(f"  Network Downloader  —  {mode}")
    print(f"  Output CSV : {CSV_PATH}")
    print(f"{'=' * 54}")

    init_csv()

    print(f"\n[URL] Checking {URL} ...")
    if not check_url(URL):
        sys.exit("[ERROR] URL not reachable. Check your internet connection.")
    print("[URL] ✓ Reachable.")

    print(f"\n[SERVER] Pinging metrics server at {SERVER_HOST}:{SERVER_PORT} ...")
    if ping_server():
        print("[SERVER] ✓ Online — metrics will be forwarded in real time.")
    else:
        print(
            "[SERVER] ✗ Not reachable — "
            "download will still run; metrics server is optional."
        )

    for i in range(1, RUNS + 1):
        run_download(i)
        if i < RUNS:
            print(f"\n  Sleeping {INTERVAL} s before next run ...")
            time.sleep(INTERVAL)

    print(f"\n{'=' * 54}")
    print(f"  Done.  Results saved to  {CSV_PATH}")
    print(f"{'=' * 54}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Interrupted] Partial results saved.")