# Network Speed Analyser — Socket Programming Mini Project

> **Course:** Socket Programming – Jackfruit Mini Project  
> **Language:** Python 3.10+  
> **Protocol:** TCP + TLS 1.2/1.3 (raw `socket` + `ssl` modules, no high-level frameworks)

---

## Table of Contents
1. [Problem Statement](#1-problem-statement)
2. [Architecture](#2-architecture)
3. [Components](#3-components)
4. [Protocol Design](#4-protocol-design)
5. [Setup](#5-setup)
6. [Usage](#6-usage)
7. [Performance Metrics](#7-performance-metrics)
8. [File Structure](#8-file-structure)
9. [Known Limitations](#9-known-limitations)

---

## 1. Problem Statement

**Objective:** Continuously download a 100 MB test file over 24 hours, measure real network metrics at each interval, and analyse the collected data to detect congestion, throttling, and performance trends.

**Goals:**
- Measure download speed, TCP connection latency, and packet loss across time
- Forward live metrics to a dedicated TLS-secured metrics server
- Visualise results with multi-panel charts and generate a written analysis report

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────┐
│               CLIENT MACHINE (same or remote)            │
│                                                          │
│  ┌─────────────────────┐    raw TCP probes (latency/    │
│  │  network_downloader │──► loss measurement)           │
│  │       .py           │                                 │
│  │                     │    HTTPS stream (100 MB file)  │
│  │  • measures latency │──► proof.ovh.net:443           │
│  │  • measures loss    │                                 │
│  │  • downloads file   │    TLS socket (METRIC/PING)   │
│  │  • writes CSV       │──► server.py (127.0.0.1:5000) │
│  └─────────────────────┘                                 │
│                                                          │
│  ┌─────────────────────┐                                 │
│  │  network_analyzer   │◄── download_log.csv            │
│  │       .py           │                                 │
│  │  • charts (PNG)     │                                 │
│  │  • report.txt       │                                 │
│  └─────────────────────┘                                 │
└──────────────────────────────────────────────────────────┘
         ▲
         │ TLS 1.2+ (raw socket)
         │
┌──────────────────────┐
│     server.py        │
│  (same machine,      │
│   127.0.0.1:5000)    │
│  • multi-threaded    │
│  • one thread/client │
│  • PING/STATUS/METRIC│
└──────────────────────┘
```

**Pattern:** Multi-client client–server over TCP with TLS.  
Every client connection is handled in its own daemon thread, allowing many simultaneous clients.

---

## 3. Components

| File | Role |
|---|---|
| `server.py` | TLS server — accepts metric reports from multiple downloader instances concurrently |
| `network_downloader.py` | Downloads 100 MB file repeatedly, probes latency/loss, logs to CSV, sends metrics to server |
| `network_analyzer.py` | Reads CSV(s), produces 7 charts and a written report |

---

## 4. Protocol Design

All control and data traffic travels over **TLS 1.2+ TCP sockets**.  
The application-layer messages are single-line ASCII strings.

```
Client → Server                     Server → Client
──────────────────────────────────  ───────────────────────────────────────
PING                                PONG
STATUS                              CONNECTIONS:<n>,METRICS:<n>,UPTIME:<s>
METRIC,<run>,<speed>,<dur>,<status> ACK
<anything else>                     ERR:UNKNOWN_CMD
                                    ERR:EMPTY          (no data sent)
                                    ERR:BAD_FORMAT     (wrong field count)
                                    ERR:ENCODING       (non-UTF-8 payload)
```

**Latency measurement** — `measure_latency()` times `socket.create_connection()` (the TCP three-way handshake) N times and averages the results. No ICMP / root required.

**Packet loss measurement** — `measure_packet_loss()` counts TCP SYN failures (timeout / refused / reset) out of N attempts and reports them as a percentage.

---

## 5. Setup

### Prerequisites
```bash
python --version   # 3.10 or later
openssl version    # for auto-cert generation (already installed on Linux/macOS)
pip install requests numpy pandas matplotlib scipy
```

### TLS Certificate
The server generates a **self-signed certificate** automatically on first run.  
No manual openssl command is needed. The files `server.pem` and `server.key` are created in the working directory.

---

## 6. Usage

### Step 1 — Start the metrics server
```bash
python server.py
# Optional flags:
python server.py --host 0.0.0.0 --port 5000
```

The server prints each METRIC line as it arrives and exits cleanly on Ctrl-C.

### Step 2 — Run the downloader

Edit the top of `network_downloader.py`:

```python
TEST_MODE = True   # 3 runs, 10 s gap  (quick smoke test)
TEST_MODE = False  # 24 runs, 1 h gap  (real 24-hour session)
```

Then run:
```bash
python network_downloader.py
```

Each run prints latency, packet loss, download speed, and the server's ACK.  
Results accumulate in `download_log.csv`.

### Step 3 — Analyse results
```bash
python network_analyzer.py download_log.csv
# Combine multiple logs:
python network_analyzer.py log1.csv log2.csv --output report --results-dir out/
```

Outputs (all in `results/` by default):
- `network_analysis_charts.png` — 6-panel dashboard + status pie
- `01_speed_per_run.png` … `07_status_breakdown.png` — individual panels
- `report.txt` — full narrative analysis

---

## 7. Performance Metrics

| Metric | How it is measured |
|---|---|
| **Download speed (Mbps)** | `bytes_received × 8 / (duration_s × 1 000 000)` |
| **Latency (ms)** | Average TCP `connect()` time over 5 probes |
| **Packet loss (%)** | Failed TCP connections out of 10 attempts |
| **Trend** | Linear regression (slope + Pearson r) on speed over runs |
| **Congestion detection** | Runs with packet loss > 5 % **and** speed < P25 |
| **Throttle detection** | Runs with packet loss ≤ 5 % **and** speed < P25 |

---

## 8. File Structure

```
.
├── server.py                        TLS metrics server
├── network_downloader.py            Downloader + metric reporter
├── network_analyzer.py              Chart + report generator
├── download_log_final_sakshi.csv    24-hour run data — client 1
├── download_log_final_vaibhav.csv   24-hour run data — client 2
├── server.pem                       Generated — TLS certificate (created by server)
├── server.key                       Generated — TLS private key  (created by server)
└── results/                         Generated — all charts and report.txt
```

---

## 9. Known Limitations

- **Packet-loss proxy:** TCP SYN failures are used as a proxy for packet loss. This is not ICMP-based and may over- or under-count in some network environments.
- **Self-signed certificate:** The downloader disables hostname and certificate verification (`CERT_NONE`) when connecting to the server. This is acceptable for a localhost demo; a production system would use a properly signed certificate.
- **Single CSV per session:** Running the downloader twice appends to the same CSV. Pass both CSV files to `network_analyzer.py` if you want combined analysis.