#!/usr/bin/env python3
"""linkcheck.py - optional pre-session iperf3 capacity check of the PC <-> headset link.

Why this exists
---------------
Every sampler in cell.py measures the *stream*: what VD/Air Link actually delivered, how many packets
the radio had to retry. None of them can say what the link could have carried, so a session that
reads badly leaves the interesting question open -- is the radio saturated, or is the encoder/config
the limit? That is what the original hand-characterisation answered for this rig (6 GHz TCP downlink
1050 Mbps with zero retransmits, no UDP loss knee because the headset's UDP *send* path is CPU-capped
near 495 Mbps, and a defective 5 GHz radio at 17-29 Mbps). This automates the same measurements so a
run can carry its own link numbers instead of relying on a one-off measurement from months ago.

It is deliberately run BEFORE the session, with no video:
  - iperf3 competes with the stream for the same radio, so running it during play corrupts both
    measurements; and
  - its numbers are only meaningful against an otherwise-idle link.

Orientation: the headset runs the *server* (its aarch64 build, pushed to /data/local/tmp, which is
also the side we care about) and the PC runs the client. Downlink is therefore a plain client->server
test, and uplink is `-R`. Nothing needs an inbound allowance on the PC except the `-R` data
connection, which is why a failing reverse test is reported rather than treated as fatal.

Usage:
  python linkcheck.py <run_id> [--ip <headset_ip>] [--serial <adb_serial>]
                      [--seconds 8] [--udp-mbps 200,500,1000]
"""
import json
import os
import socket
import subprocess
import sys
import time

import qsite

REMOTE_BIN = "/data/local/tmp/iperf3"
PORT = 5201
DEFAULT_SECONDS = 8
# A rate counts as lossless below this. Matches the write-up's own bar: "no cell exceeded 0.09 %
# loss anywhere in the matrix", and "no rate crossed 0.1 %" for the UDP ramp.
LOSS_KNEE_MAX_PCT = 0.1


class LinkCheckError(RuntimeError):
    """The check could not run at all -- no headset, push failed, server never came up. Distinct from
    a check that ran and produced unflattering numbers; the wizard continues either way."""


# ---------------------------------------------------------------- adb plumbing
def _adb(serial, *args, timeout=30, check=False):
    cmd = [qsite.path("adb")]
    if serial:
        cmd += ["-s", serial]
    cmd += [str(a) for a in args]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    if check and p.returncode != 0:
        raise LinkCheckError(f"adb {args[0]} failed: {(p.stderr or p.stdout).strip()}")
    return p


def ensure_remote_binary(serial, local_path, remote=REMOTE_BIN, progress=print):
    """Push the Android iperf3 to the headset, skipping the transfer when a same-size copy is already
    there. Size, not hash: this goes over the wireless link we are about to measure, at ~1 MB/s, and a
    stale-but-identical-size binary is not a failure mode worth re-copying 3 MB for every session."""
    local_size = os.path.getsize(local_path)
    existing = _adb(serial, "shell", f"stat -c %s {remote} 2>/dev/null || echo none").stdout.strip()
    if existing.splitlines()[:1] == [str(local_size)]:
        progress(f"  headset already has {remote} ({local_size} bytes), not re-pushing")
        return remote
    progress(f"  pushing {os.path.basename(local_path)} -> {remote} ...")
    p = _adb(serial, "push", local_path, remote, timeout=600)
    if p.returncode != 0:
        raise LinkCheckError(f"adb push failed: {(p.stderr or p.stdout).strip()}")
    _adb(serial, "shell", f"chmod 755 {remote}", check=True)
    pushed = _adb(serial, "shell", f"stat -c %s {remote}").stdout.strip()
    if pushed.splitlines()[:1] != [str(local_size)]:
        raise LinkCheckError(f"push landed a different size ({pushed!r}, wanted {local_size})")
    return remote


def start_server(serial, remote=REMOTE_BIN, host="", port=PORT, log=None, ready_timeout=20,
                 progress=print):
    """Start the headset-side server as a held-open adb shell process and wait until the port answers.

    Foreground rather than `-D`: the daemon flag is not in every Android build, and a process we hold
    open is easier to guarantee dead afterwards. Readiness is a TCP connect from this side, not a
    parsed log line -- the log format is not ours to depend on, and an unreachable host and a dead
    server would otherwise look the same."""
    proc = subprocess.Popen([qsite.path("adb"), "-s", serial, "shell", f"{remote} -s -p {port}"],
                            stdout=log or subprocess.DEVNULL, stderr=subprocess.STDOUT)
    deadline = time.time() + ready_timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise LinkCheckError(f"iperf3 server exited immediately (rc={proc.returncode}); "
                                 f"is {remote} really an Android/aarch64 binary?")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex((host, port)) == 0:
                return proc
        time.sleep(0.5)
    proc.terminate()
    raise LinkCheckError(f"headset iperf3 server never listened on {host}:{port} within "
                         f"{ready_timeout}s")


def stop_server(proc, serial, remote=REMOTE_BIN):
    """Best-effort teardown. Terminating the adb client does not reliably kill the remote shell, so
    the pkill is the part that actually matters -- a server left listening on the headset would make
    the *next* run's check connect to a stale build."""
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if serial:
        _adb(serial, "shell", f"pkill -f {remote} 2>/dev/null || true")


# ---------------------------------------------------------------- measurement
def _client_json(pc_exe, host, port, args, timeout):
    """Run one iperf3 client test and return its parsed JSON (-J). Raises on a non-zero exit, which
    is how `-R` surfaces a blocked reverse connection."""
    cmd = [pc_exe, "-c", host, "-p", str(port), "-J"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LinkCheckError(f"iperf3 client timed out after {timeout}s: {' '.join(args)}")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout).strip().splitlines()
        raise LinkCheckError(tail[-1] if tail else f"iperf3 exited {p.returncode}")
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise LinkCheckError(f"could not parse iperf3 JSON output: {p.stdout[:200]!r}")


def reduce_tcp(j):
    """Delivered Mbps and retransmits from one TCP test, whichever direction it ran.

    `sum_received` is the receiving end's view -- the data direction, so it is the right number for
    both orientations -- and `sum_sent.retransmits` is the sender's TCP retransmit count. Verified
    against this rig's own 3.21 captures: iperf_tcp_6g.json reports sum_received 717.07 Mbps and
    sum_sent.retransmits 2, matching the write-up's "717 Mbps uplink, 2 retransmits".

    Absent `retransmits` means zero, not unknown: 3.21 omits the key when nothing was retransmitted
    (confirmed locally -- a clean loopback run's sum_sent has no such key, while a capture with 2 does),
    and the write-up's headline "1050 Mbps, 0 retransmits" is exactly that case."""
    end = j.get("end", {})
    recv = end.get("sum_received") or {}
    sent = end.get("sum_sent") or {}
    mbps = round(recv.get("bits_per_second", 0) / 1e6, 1) if recv.get("bits_per_second") else None
    return mbps, (sent.get("retransmits", 0) if sent else None)


def reduce_udp(j):
    """Achieved Mbps, loss and jitter from one UDP test. Uses `end.sum`, which merges the receiver's
    loss/jitter into the sender's counters -- the same block this rig's captures tally (200 Mbps ->
    0.0039 % lost, and 1200 Mbps requested -> 496.08 Mbps / 0.0215 % lost), and the only one whose
    loss_percent reflects what actually arrived."""
    s = (j.get("end") or {}).get("sum") or {}
    if not s.get("bits_per_second"):
        return None
    return {"mbps": round(s["bits_per_second"] / 1e6, 1),
            "lost_pct": round(s.get("lost_percent", 0.0), 4),
            "jitter_ms": round(s.get("jitter_ms", 0.0), 4),
            "lost_packets": s.get("lost_packets"),
            "packets": s.get("packets")}


def run_linkcheck(run_id, host, serial=None, pc_exe=None, headset_bin=None,
                  seconds=DEFAULT_SECONDS, udp_mbps=(200, 500, 1000), progress=print):
    """Run the full check into runs/<run_id>/ and return the result dict (also written as
    linkcheck.json there, with the raw per-test JSONs under linkcheck/)."""
    pc_exe = pc_exe or qsite.iperf3_exe()
    headset_bin = headset_bin or qsite.iperf3_android()
    if not pc_exe:
        raise LinkCheckError("no PC iperf3 client -- set iperf3_exe, drop vendor/iperf3.exe, or "
                             "install iperf3")
    if not headset_bin:
        raise LinkCheckError("no headset iperf3 build -- set iperf3_android or drop an aarch64 "
                             "binary named 'iperf3' in vendor/")
    if not host:
        raise LinkCheckError("no headset IP to test against")
    if not serial:
        raise LinkCheckError("no adb serial -- connect the headset first")

    run_dir = os.path.join(qsite.base_dir(), "runs", run_id)
    raw_dir = os.path.join(run_dir, "linkcheck")
    os.makedirs(raw_dir, exist_ok=True)
    progress(f"  headset {host}, client {pc_exe}")

    out = {"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "quest_ip": host, "port": PORT,
           "seconds": seconds, "pc_iperf3": pc_exe, "headset_iperf3": headset_bin,
           "pc_version": None, "tcp_down_mbps": None, "tcp_down_retransmits": None,
           "tcp_up_mbps": None, "tcp_up_retransmits": None, "udp": [],
           "udp_last_zero_loss_mbps": None}
    server_proc, server_log, remote = None, None, REMOTE_BIN
    try:
        remote = ensure_remote_binary(serial, headset_bin, progress=progress)
        _adb(serial, "shell", f"pkill -f {remote} 2>/dev/null || true")
        server_log = open(os.path.join(raw_dir, "server.log"), "w", encoding="utf-8")
        server_proc = start_server(serial, remote, host, PORT, log=server_log, progress=progress)
        progress("  server up")

        for label, args in (("tcp_down", ["-t", str(seconds)]),
                            ("tcp_up", ["-t", str(seconds), "-R"])):
            try:
                j = _client_json(pc_exe, host, PORT, args, timeout=seconds + 45)
            except LinkCheckError as e:
                # Not fatal: `-R` needs the headset to open a connection back to this PC, which a
                # firewall can refuse. A failed reverse test still leaves the downlink -- the
                # direction the video actually travels -- intact.
                out[label + "_error"] = str(e)
                progress(f"  {label}: failed ({e})")
                continue
            with open(os.path.join(raw_dir, label + ".json"), "w", encoding="utf-8") as fh:
                json.dump(j, fh, indent=1)
            out["pc_version"] = out["pc_version"] or (j.get("start") or {}).get("version")
            mbps, retrans = reduce_tcp(j)
            out[label + "_mbps"], out[label + "_retransmits"] = mbps, retrans
            progress(f"  {label}: {mbps} Mbps" +
                     (f", {retrans} retransmits" if retrans is not None else ""))

        for m in udp_mbps:
            try:
                j = _client_json(pc_exe, host, PORT, ["-u", "-b", f"{m}M", "-t", str(seconds)],
                                 timeout=seconds + 45)
            except LinkCheckError as e:
                progress(f"  udp {m} Mbps: failed ({e})")
                continue
            with open(os.path.join(raw_dir, f"udp_{m}.json"), "w", encoding="utf-8") as fh:
                json.dump(j, fh, indent=1)
            row = reduce_udp(j)
            if row:
                row["target_mbps"] = m
                out["udp"].append(row)
                progress(f"  udp {m} Mbps requested -> {row['mbps']} Mbps, "
                         f"{row['lost_pct']}% lost, jitter {row['jitter_ms']} ms")
    finally:
        stop_server(server_proc, serial, remote)
        if server_log:
            server_log.close()

    lossless = [r["target_mbps"] for r in out["udp"]
                if r.get("lost_pct") is not None and r["lost_pct"] < LOSS_KNEE_MAX_PCT]
    out["udp_last_zero_loss_mbps"] = max(lossless) if lossless else None
    out["udp_knee_max_pct"] = LOSS_KNEE_MAX_PCT
    with open(os.path.join(run_dir, "linkcheck.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    return out


def summary_lines(res):
    """The two or three lines worth showing a human, as a list (so callers can indent them)."""
    if not res:
        return []
    down = res.get("tcp_down_mbps")
    up = res.get("tcp_up_mbps")
    parts = []
    if down is not None:
        parts.append(f"down {down} Mbps" +
                     (f" ({res.get('tcp_down_retransmits')} retransmits)"
                      if res.get("tcp_down_retransmits") is not None else ""))
    if up is not None:
        parts.append(f"up {up} Mbps")
    elif res.get("tcp_up_error"):
        parts.append("up failed (reverse connection blocked?)")
    lines = ["TCP " + ", ".join(parts)] if parts else []
    udp = res.get("udp") or []
    if udp:
        lines.append("UDP " + "; ".join(f"{r['target_mbps']}->{r['mbps']} Mbps "
                                        f"({r['lost_pct']}% lost)" for r in udp))
        knee = res.get("udp_last_zero_loss_mbps")
        lines.append("lossless up to " + (f"{knee} Mbps" if knee else
                                          f"no rate tested (loss above {res.get('udp_knee_max_pct')}%)"))
    return lines


def print_summary(res, prefix="  "):
    lines = summary_lines(res)
    if lines:
        print(prefix + "Link check: " + lines[0])
        for line in lines[1:]:
            print(prefix + "            " + line)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="pre-session iperf3 link capacity check")
    ap.add_argument("run_id")
    ap.add_argument("--ip", default=None, help="headset IP (default: from the adb serial or site.json)")
    ap.add_argument("--serial", default=None, help="adb serial (host:port)")
    ap.add_argument("--seconds", type=int, default=DEFAULT_SECONDS)
    ap.add_argument("--udp-mbps", default="200,500,1000")
    a = ap.parse_args()
    host = a.ip or (a.serial.split(":")[0] if a.serial and ":" in a.serial
                    else qsite.get("quest_ip"))
    try:
        res = run_linkcheck(a.run_id, host, serial=a.serial, seconds=a.seconds,
                            udp_mbps=[int(x) for x in a.udp_mbps.split(",") if x.strip()])
    except LinkCheckError as e:
        print(f"link check failed: {e}")
        return 1
    print_summary(res, prefix="")
    print(f"\nwrote runs/{a.run_id}/linkcheck.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
