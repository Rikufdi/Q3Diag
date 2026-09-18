#!/usr/bin/env python3
"""analyze.py — PCVR Wi-Fi diagnostics reduction.

Subcommands:
  cell   <runs_dir> <run_id> [--quest-ip IP] [--pc-ip IP] [--window START,END]
         Reduce one run's cap.pcapng to wire UDP TX/RX Mbps + packets/s over the
         measurement window. Prints a JSON object to stdout.

  report <runs_dir> [--out report.md]
         Read runs/*/settings.json + results.json, quest_wifi_samples.tsv and
         ovr_metrics.csv, and write the final report.
"""
import csv
import json
import os
import subprocess
import sys

import qsite

TSHARK = qsite.path("tshark")
PC_IP_DEFAULT = qsite.get("pc_ip")


def find_tshark():
    return TSHARK


def tshark_fields(cap, fields, display_filter=None):
    cmd = [find_tshark(), "-r", cap, "-T", "fields"]
    for f in fields:
        cmd += ["-e", f]
    if display_filter:
        cmd += ["-Y", display_filter]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout


def reduce_cell(runs_dir, run_id, quest_ip=None, pc_ip=PC_IP_DEFAULT, window=None):
    run_dir = os.path.join(runs_dir, run_id)
    cap = os.path.join(run_dir, "cap.pcapng")
    if not os.path.exists(cap):
        # fall back to .etl (no pcap conversion done yet)
        cap = os.path.join(run_dir, "cap.etl")
    if not os.path.exists(cap):
        err = {"error": "no capture found in " + run_dir}
        print(json.dumps(err))
        return err

    txt = tshark_fields(cap, ["frame.time_relative", "ip.src", "ip.dst", "tcp.len", "udp.length"], "tcp || udp")
    lo, hi = (0.0, float("inf"))
    if window:
        parts = [float(x) for x in window.split(",")]
        if len(parts) == 2:
            lo, hi = parts[0], parts[1]

    tx_bytes = 0.0
    rx_bytes = 0.0
    tx_pkts = 0
    rx_pkts = 0
    t_min = t_max = None
    for line in txt.splitlines():
        fields = line.split("\t")
        if len(fields) < 5:
            continue
        try:
            t = float(fields[0])
            src = fields[1]
            dst = fields[2]
            tcp_len = float(fields[3]) if fields[3] else 0.0
            udp_len = float(fields[4]) if fields[4] else 0.0
            length = tcp_len + udp_len
        except ValueError:
            continue
        if not (lo <= t <= hi):
            continue
        if t_min is None or t < t_min:
            t_min = t
        if t_max is None or t > t_max:
            t_max = t
        if quest_ip and dst == quest_ip:
            tx_bytes += length
            tx_pkts += 1
        elif quest_ip and src == quest_ip:
            rx_bytes += length
            rx_pkts += 1
        elif pc_ip and src == pc_ip:
            tx_bytes += length
            tx_pkts += 1
        elif pc_ip and dst == pc_ip:
            rx_bytes += length
            rx_pkts += 1

    dur = (t_max - t_min) if (t_min is not None and t_max is not None and t_max > t_min) else 1.0
    result = {
        "wire_tx_mbps": round(tx_bytes * 8 / dur / 1e6, 2),
        "wire_rx_mbps": round(rx_bytes * 8 / dur / 1e6, 2),
        "wire_pkts_per_s": round((tx_pkts + rx_pkts) / dur, 1),
        "window_start_s": round(t_min, 3) if t_min is not None else None,
        "window_end_s": round(t_max, 3) if t_max is not None else None,
    }
    print(json.dumps(result))
    return result


def _load_json(p):
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(part, whole):
    p = _num(part)
    w = _num(whole)
    if p is None or w is None or w == 0:
        return None
    return round(p / w * 100.0, 3)


def build_report(runs_dir, out_path):
    runs = []
    for name in sorted(os.listdir(runs_dir)):
        rd = os.path.join(runs_dir, name)
        s = _load_json(os.path.join(rd, "settings.json"))
        r = _load_json(os.path.join(rd, "results.json"))
        if s:
            runs.append((name, s, r))

    lines = []
    w = lines.append
    w("# PCVR Wi-Fi streaming diagnostics — report")
    w("")
    w(f"Runs analysed: {len(runs)}")

    # baseline knee table
    knee = _load_json(os.path.join(runs_dir, "..", "baseline", "knee.json"))
    if knee:
        w("")
        w("## Baseline (no video) — iperf3 UDP")
        w("")
        w("| band | knee rate (Mbps) | last zero-loss (Mbps) |")
        w("|---|---|---|")
        for b in ("6g", "5g"):
            r = knee.get(b, {})
            w(f"| {b} | {r.get('knee_rate_mbps', '-')} | {r.get('last_zero_loss_mbps', '-')} |")

    # per-stack tables
    for stack in ("vd", "airlink"):
        rows = [x for x in runs if x[1].get("stack") == stack]
        if not rows:
            continue
        label = "Virtual Desktop" if stack == "vd" else "Meta Air Link"
        w("")
        w(f"## {label}")
        w("")
        w("| cell | codec | bitrate (Mbps) | wire TX (Mbps) | retry % | lost % | ping p50 | ping p95 | total lat (ms) | net lat (ms) | FPS |")
        w("|---|---|---|---|---|---|---|---|---|---|---|")
        for name, s, r in rows:
            r = r or {}
            w("| {n} | {c} | {b} | {wt} | {rt} | {ls} | {p50} | {p95} | {lt} | {ln} | {fps} |".format(
                n=name, c=s.get("codec", "-"), b=s.get("bitrate_mbps", "-"),
                wt=r.get("wire_tx_mbps", "-"), rt=r.get("retry_rate_pct", "-"),
                ls=r.get("lost_rate_pct", "-"), p50=r.get("ping_rtt_p50_ms", "-"),
                p95=r.get("ping_rtt_p95_ms", "-"), lt=r.get("overlay_latency_total_ms", "-"),
                ln=r.get("overlay_latency_network_ms", "-"), fps=r.get("overlay_fps", "-")))

    w("")
    w("## Findings")
    w("")
    w("_populated after matrix runs complete_")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote " + out_path)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    cmd = sys.argv[1]
    runs_dir = sys.argv[2]
    if cmd == "cell":
        run_id = sys.argv[3] if len(sys.argv) > 3 else None
        quest_ip = None
        window = None
        args = sys.argv[4:]
        i = 0
        while i < len(args):
            if args[i] == "--quest-ip" and i + 1 < len(args):
                quest_ip = args[i + 1]
                i += 2
            elif args[i] == "--window" and i + 1 < len(args):
                window = args[i + 1]
                i += 2
            else:
                i += 1
        reduce_cell(runs_dir, run_id, quest_ip=quest_ip, window=window)
    elif cmd == "report":
        out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(runs_dir, "report.md")
        build_report(runs_dir, out)
    else:
        print("unknown command: " + cmd)


if __name__ == "__main__":
    main()
