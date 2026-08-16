#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""peer.py - the example app: one agent machine, listening on the bus.

WHAT THIS IS
    Run this on machine A and on machine B and they talk to each other. A
    sends a TASK, B ACKs it within a tick, does the work from its OWN
    allowlist, and sends a RESULT. A stops asking only when the RESULT lands.

INPUT / OUTPUT
    in : envelopes from bus.py's transport (file or telegram).
    out: ACK + RESULT envelopes, a periodic HEARTBEAT, and a chase line for
         every TASK of ours that is past its SLA.

WHO CALLS IT
    A human (`python peer.py --machine LAPTOP --transport telegram`), a
    launchd/systemd unit, or demo.py which drives two of these in one process.

RAIL
    stdlib only; the telegram transport adds telethon (see bus.py).

THE SECURITY LINE, SAID ONCE AND MEANT
    The body of an inbound envelope is DATA. It never selects code. The peer
    dispatches on CAPABILITIES below - a table this machine's owner wrote -
    and an unknown request comes back as a polite RESULT, not as a shell.
    A chat group is a place where anyone can type; treat it like one.

updated: 2026-08-16
"""

import argparse
import os
import platform
import shutil
import sys
import time

from bus import Bus, DEFAULT_SLA_MIN


# --------------------------------------------------------------------------
# capabilities: what this machine will do when asked. Yours will differ.
# --------------------------------------------------------------------------

def cap_ping(arg):
    return "pong from %s" % platform.node()


def cap_uptime(arg):
    try:
        with open("/proc/uptime") as fh:                      # linux
            return "up %.1f h" % (float(fh.read().split()[0]) / 3600.0)
    except (IOError, OSError, ValueError, IndexError):
        pass
    try:                                                       # macos / bsd
        import subprocess
        out = subprocess.check_output(["uptime"], stderr=subprocess.STDOUT)
        return out.decode("utf-8", "replace").strip()
    except Exception as exc:                                   # noqa: BLE001
        return "uptime unavailable: %s" % exc


def cap_disk(arg):
    total, used, free = shutil.disk_usage(arg.strip() or "/")
    return "%s: %.1f GB free of %.1f GB" % (arg.strip() or "/",
                                            free / 1e9, total / 1e9)


def cap_echo(arg):
    return arg


CAPABILITIES = {
    "ping": cap_ping,
    "uptime": cap_uptime,
    "disk": cap_disk,
    "echo": cap_echo,
}


def handle(body):
    """Turn a request into a result string. Never raises, never executes text.

    Returns (ok, text). `ok=False` still produces a RESULT: a peer that goes
    quiet on a request it dislikes is worse than one that says no, because
    silence is indistinguishable from a dead machine.
    """
    verb, _, arg = (body or "").strip().partition(" ")
    fn = CAPABILITIES.get(verb.lower())
    if not fn:
        return False, "unsupported request %r; I do: %s" % (
            verb, ", ".join(sorted(CAPABILITIES)))
    try:
        return True, fn(arg)
    except Exception as exc:                                   # noqa: BLE001
        return False, "%s failed: %s: %s" % (verb, type(exc).__name__, exc)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

def tick(bus, sla_min=DEFAULT_SLA_MIN, chased=None, log=print):
    """One pass: drain the inbox, answer, then chase our own silent tasks.

    Returns a small dict of counters so a caller (demo, a watchdog, a test)
    can assert on what happened instead of scraping stdout.
    """
    chased = chased if chased is not None else set()
    n = {"in": 0, "acked": 0, "result": 0, "chased": 0}

    for env in bus.fetch():
        n["in"] += 1
        kind, eid, frm = env["kind"], env["id"], env["frm"]

        if kind == "TASK":
            # ACK first, work second. The ACK is cheap and it is what turns
            # "did it even arrive?" from a question into a fact.
            bus.send(frm, "", kind="ACK", ref=eid)
            n["acked"] += 1
            log("  <- TASK #%s from %s: %s" % (eid, frm, env.get("body")))
            ok, text = handle(env.get("body"))
            bus.send(frm, ("" if ok else "REFUSED: ") + text, kind="RESULT", ref=eid)
            n["result"] += 1
            log("  -> RESULT #%s: %s" % (eid, text))

        elif kind == "ACK":
            log("  <- ACK  for #%s from %s" % (env.get("ref"), frm))

        elif kind == "RESULT":
            log("  <- RESULT for #%s from %s: %s" % (env.get("ref"), frm, env.get("body")))

        elif kind == "HEARTBEAT":
            log("  <- HB   from %s: %s" % (frm, env.get("body")))

    for row in bus.outstanding(sla_min=sla_min):
        if not row["overdue"] or row["id"] in chased:
            continue
        chased.add(row["id"])
        n["chased"] += 1
        log("  !! #%s to %s is %.0f min old with no RESULT%s - chasing"
            % (row["id"], row["to"], row["age_min"],
               "" if row["acked"] else " and never ACKed"))
        bus.send(row["to"], "still waiting on #%s: %s" % (row["id"], row["body"]),
                 kind="TASK", ref=row["id"])

    return n


def heartbeat_body(bus):
    """A heartbeat is a health snapshot, not a 'still here'.

    'Still here' is worthless: a peer whose disk is full and whose queue is
    backed up is also 'still here'. Say something falsifiable.
    """
    rows = bus.outstanding()
    total, _used, free = shutil.disk_usage("/")
    return "host=%s outstanding=%d overdue=%d free=%.0fGB" % (
        platform.node(), len(rows), sum(1 for r in rows if r["overdue"]), free / 1e9)


def main(argv=None):
    p = argparse.ArgumentParser(description="an agent peer on the bus")
    p.add_argument("--machine", default=os.environ.get("ABUS_MACHINE"),
                   help="this machine's name (env ABUS_MACHINE)")
    p.add_argument("--transport", default=os.environ.get("ABUS_TRANSPORT", "file"),
                   choices=["file", "telegram"])
    p.add_argument("--interval", type=int, default=20, help="seconds between ticks")
    p.add_argument("--sla", type=int, default=DEFAULT_SLA_MIN,
                   help="minutes before a TASK with no RESULT is chased")
    p.add_argument("--heartbeat-every", type=int, default=30,
                   help="ticks between heartbeats (0 = never)")
    p.add_argument("--once", action="store_true", help="one tick, then exit")
    args = p.parse_args(argv)

    if not args.machine:
        raise SystemExit("--machine (or ABUS_MACHINE) is required: peers need names")

    bus = Bus(machine=args.machine, transport=args.transport)
    print("peer %s up  transport=%s  state=%s" % (bus.machine, args.transport, bus.root))

    chased, ticks = set(), 0
    while True:
        ticks += 1
        try:
            tick(bus, sla_min=args.sla, chased=chased)
            if args.heartbeat_every and ticks % args.heartbeat_every == 0:
                bus.send("*", heartbeat_body(bus), kind="HEARTBEAT")
        except KeyboardInterrupt:
            print("bye")
            return 0
        except Exception as exc:                               # noqa: BLE001
            # A peer that dies on one bad tick is a peer nobody can rely on.
            print("tick failed (%s: %s) - continuing" % (type(exc).__name__, exc))
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
