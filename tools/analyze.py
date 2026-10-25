#!/usr/bin/env python3
"""analyze.py — PCVR Wi-Fi diagnostics reporting.

Subcommand:
  report <runs_dir> [--out report.md]
         Read runs/*/settings.json + results.json, quest_wifi_samples.tsv and
         ovr_metrics.csv, and write the final report.

The pcap reduction that used to live here (the `cell` subcommand, which parsed a captured
cap.pcapng/cap.etl with tshark into wire UDP TX/RX Mbps) was removed on 2026-10-25 along with the
elevated pktmon plumbing that produced those captures: no shipped path recorded packets, the metric
was permanently null, and keeping it meant requiring Wireshark to start the app (issue #4). The
delivered rate is reconstructed from wlan0 rx deltas instead, which needs neither admin nor tshark.
"""
import csv
import json
import os
import sys


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
    if cmd == "report":
        out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(runs_dir, "report.md")
        build_report(runs_dir, out)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
