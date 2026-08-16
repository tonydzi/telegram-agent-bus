#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""demo.py - the whole protocol on one machine, offline, in about two seconds.

WHAT THIS IS
    Two peers (HUB and LAPTOP) driven in one process over the file transport
    in a temp directory. No network, no Telegram account, no packages, no API
    key. Every scenario asserts its own end state, so the exit code is the
    verdict: 0 means the protocol did what the README claims.

    Run it before you believe anything in the README:

        python demo.py            # traces + verdict
        python demo.py --keep     # keep the temp state dir to poke at it

SCENARIOS
    A  happy path        TASK -> ACK -> work -> RESULT, sender's queue empties
    B  refusal           an unsupported request comes back as a RESULT, not silence
    C  silent peer       a TASK past SLA is chased; an ACK alone does NOT close it
    D  double delivery   the same envelope twice runs the work once
    E  corrupt ledger    a garbage line does not eat the history after it
    F  humans in the room ordinary chat in the group is ignored, not parsed
    G  telegram logic   cursor, addressing and self-echo, against a stubbed client

WHO CALLS IT
    You, a reviewer, and CI. It doubles as the integration test for bus.py +
    peer.py, so it is the thing to run after changing either.

updated: 2026-08-16
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

import bus as busmod
import peer as peermod

HUB, LAPTOP = "HUB", "LAPTOP"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("   %s %s%s" % ("PASS" if ok else "FAIL", name,
                          ("  (%s)" % detail) if detail and not ok else ""))
    return ok


def quiet(*_a, **_kw):
    pass


def make(root, machine):
    return busmod.Bus(machine=machine, root=root, transport="file")


def isolate(root, name):
    """Each scenario gets its own state dir.

    Learned the hard way while writing this file: sharing one dir let an
    unread chase-task from scenario C leak into D and F and break both. A
    scenario that can be polluted by its neighbour proves nothing.
    """
    sub = os.path.join(root, name)
    os.makedirs(sub)
    return sub


def scenario_a(root):
    root = isolate(root, 'a')
    print("\nA. happy path - HUB asks, LAPTOP answers")
    hub, laptop = make(root, HUB), make(root, LAPTOP)

    env = hub.send(LAPTOP, "disk /")
    print("   HUB  -> TASK #%s 'disk /'" % env["id"])

    n = peermod.tick(laptop, log=quiet)
    check("laptop ACKed and produced a RESULT", n["acked"] == 1 and n["result"] == 1)

    open_before = hub.outstanding()
    check("task is outstanding until the RESULT is read back", len(open_before) == 1)

    peermod.tick(hub, log=quiet)
    rows = hub.outstanding()
    check("RESULT closes the task", rows == [], "still open: %s" % rows)

    inbound = [r for r in hub.ledger.records() if r["dir"] == "in"]
    kinds = [r["env"]["kind"] for r in inbound]
    check("HUB saw exactly one ACK and one RESULT",
          kinds.count("ACK") == 1 and kinds.count("RESULT") == 1, kinds)
    body = [r["env"]["body"] for r in inbound if r["env"]["kind"] == "RESULT"][0]
    print("   RESULT body: %s" % body)
    check("the work actually ran", "GB" in body, body)


def scenario_b(root):
    root = isolate(root, 'b')
    print("\nB. refusal - an unknown request answers, it does not go quiet")
    hub, laptop = make(root, HUB), make(root, LAPTOP)
    hub.send(LAPTOP, "rm -rf /")
    peermod.tick(laptop, log=quiet)
    peermod.tick(hub, log=quiet)
    results = [r["env"] for r in hub.ledger.records()
               if r["dir"] == "in" and r["env"]["kind"] == "RESULT"]
    last = results[-1]["body"]
    print("   RESULT body: %s" % last)
    check("refusal came back as a RESULT", last.startswith("REFUSED"), last)
    check("nothing was executed", "unsupported request" in last, last)
    check("sender's queue is empty again", hub.outstanding() == [])


def scenario_c(root):
    root = isolate(root, 'c')
    print("\nC. silent peer - ACK is not done, and silence gets chased")
    hub = make(root, HUB)
    old = (datetime.utcnow() - timedelta(minutes=99)).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = busmod.envelope(HUB, LAPTOP, "TASK", "uptime", ts=old)
    hub.transport.send(env)
    hub.ledger.append("out", env)

    # the peer hears it and ACKs, then never comes back with a result
    laptop = make(root, LAPTOP)
    for got in laptop.fetch():
        laptop.send(got["frm"], "", kind="ACK", ref=got["id"])

    peermod.tick(hub, log=quiet)                     # HUB reads the ACK
    rows = [r for r in hub.outstanding() if r["id"] == env["id"]]
    check("an ACKed-but-unfinished task stays outstanding", len(rows) == 1)
    check("it is marked acked", rows and rows[0]["acked"])
    check("it is flagged overdue", rows and rows[0]["overdue"])

    n = peermod.tick(hub, log=quiet)
    check("the chase fired exactly once", n["chased"] == 1, n)


def scenario_d(root):
    root = isolate(root, 'd')
    print("\nD. double delivery - the same envelope twice runs the work once")
    hub, laptop = make(root, HUB), make(root, LAPTOP)
    env = hub.send(LAPTOP, "ping")
    # a rail that redelivers is normal: a resend, a restart, a synced folder
    # replaying a file. The dedup lives in the ledger, not in memory.
    hub.transport.send(env)

    n = peermod.tick(laptop, log=quiet)
    check("delivered twice, handled once", n["in"] == 1 and n["result"] == 1, n)


def scenario_e(root):
    root = isolate(root, 'e')
    print("\nE. corrupt ledger line - history after it survives")
    hub = make(root, HUB)
    with open(hub.ledger.path, "a", encoding="utf-8") as fh:
        fh.write("{ this line is not json\n")
    hub.send(LAPTOP, "ping")

    pairs = list(hub.ledger.read())
    bad = [p for p in pairs if not p[1]]
    check("the bad line is reported, not swallowed", len(bad) == 1)
    good = hub.ledger.records()
    check("records after the bad line are still readable",
          good and good[-1]["env"]["body"] == "ping")


def scenario_f(root):
    root = isolate(root, 'f')
    print("\nF. humans in the room - ordinary chat is ignored")
    laptop = make(root, LAPTOP)
    wire = os.path.join(root, "wire", "HUMAN.outbox")
    with open(wire, "a", encoding="utf-8") as fh:
        fh.write("anyone know why the build is red?\n")
        fh.write("also %s{\"v\":1,\"kind\":\"TASK\"}\n" % busmod.MARKER)   # malformed
        fh.write("%s%s\n" % (busmod.MARKER, json.dumps(
            {"v": 1, "id": "x1", "frm": "HUB; drop table", "to": LAPTOP,
             "kind": "TASK", "body": "ping"})))                            # bad name
    n = peermod.tick(laptop, log=quiet)
    check("none of the three lines became work", n["in"] == 0, n)


class StubTelegram(object):
    """A telethon stand-in: a list of messages, newest first, like the real one.

    This tests OUR code (cursor persistence, addressing filter, self-echo
    filter, parsing) without a phone number. It does NOT test telethon or
    Telegram - see the README's honest-limits section.
    """

    def __init__(self, messages):
        self.messages = messages          # [(id, text)] oldest first
        self.sent = []

    def send_message(self, chat, text):
        self.sent.append((chat, text))

    def iter_messages(self, chat, min_id=0, limit=200):
        class M(object):
            def __init__(self, i, t):
                self.id, self.text = i, t
        rows = [M(i, t) for i, t in self.messages if i > min_id]
        return list(reversed(rows))[:limit]


def scenario_g(root):
    root = isolate(root, 'g')
    print("\nG. telegram transport logic - cursor, addressing, self-echo (stubbed client)")
    wire = lambda frm, to, kind, body: busmod.to_wire(   # noqa: E731
        busmod.envelope(frm, to, kind, body))
    msgs = [
        (10, "morning everyone"),                                  # a human
        (11, wire(HUB, LAPTOP, "TASK", "ping")),                   # for us
        # our own broadcast: it passes the address filter, so only the
        # self-echo check can drop it. (First version of this scenario used a
        # LAPTOP->HUB message here, which the address filter caught anyway -
        # the check was green while the self-echo guard was dead code.)
        (12, wire(LAPTOP, "*", "HEARTBEAT", "host=laptop")),
        (13, wire(HUB, "OTHER", "TASK", "ping")),                  # not for us
        (14, wire(HUB, "*", "HEARTBEAT", "host=hub")),             # broadcast
    ]
    stub = StubTelegram(msgs)
    t = busmod.TelegramTransport(root, LAPTOP, client=stub, chat=-100)

    got = t.fetch()
    check("only the addressed envelopes came through", len(got) == 2,
          [g["kind"] for g in got])
    check("oldest first", [g["kind"] for g in got] == ["TASK", "HEARTBEAT"],
          [g["kind"] for g in got])
    check("our own message was not read back", all(g["frm"] != LAPTOP for g in got))

    check("cursor persisted, second fetch is empty", t.fetch() == [])
    stub.messages.append((15, wire(HUB, LAPTOP, "TASK", "uptime")))
    check("a new message after the cursor is picked up", len(t.fetch()) == 1)

    t.send(busmod.envelope(LAPTOP, HUB, "RESULT", "ok", ref="z1"))
    check("send reaches the client with a human-readable head",
          stub.sent and stub.sent[0][1].startswith("[RESULT] LAPTOP -> HUB"),
          stub.sent[:1])


def main(argv=None):
    ap = argparse.ArgumentParser(description="offline, self-checking demo")
    ap.add_argument("--keep", action="store_true", help="keep the temp state dir")
    args = ap.parse_args(argv)

    root = tempfile.mkdtemp(prefix="abus-demo-")
    print("state dir: %s" % root)
    try:
        for fn in (scenario_a, scenario_b, scenario_c, scenario_d, scenario_e, scenario_f,
                   scenario_g):
            fn(root)
    finally:
        if args.keep:
            print("\nkept: %s" % root)
        else:
            shutil.rmtree(root, ignore_errors=True)

    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d checks passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("FAILED: %s" % ", ".join(failed))
        return 1
    print("verdict: the protocol does what the README says")
    return 0


if __name__ == "__main__":
    sys.exit(main())
