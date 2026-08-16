#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bus.py - the agent-to-agent bus: envelope, ledger, ACK bookkeeping, transports.

WHAT THIS IS
    The library + CLI behind `peer.py`. Two agents on two machines exchange
    envelopes over a transport. The interesting transport is a Telegram group
    the humans are already sitting in; the file transport is the offline twin
    used by the demo and by anyone whose machines share a synced folder.

INPUT / OUTPUT
    in : ABUS_MACHINE (this machine's name), ABUS_DIR (state dir),
         plus transport-specific env (see .env.example).
    out: append-only JSONL ledger at $ABUS_DIR/ledger/<MACHINE>.jsonl
         (single writer per file - never shared with a peer), and whatever
         the transport puts on the wire.

WHO CALLS IT
    `peer.py` (the example app), `demo.py` (offline self-check), and a human
    on the command line: `python bus.py send LAPTOP "..."`, `bus.py status`.

RAIL
    stdlib only. The Telegram transport needs `telethon`, imported lazily, so
    everything else keeps working without it.

DESIGN RULES THAT ARE NOT NEGOTIABLE
    1. Single writer per file. Each machine appends to its OWN ledger shard.
       Two machines never touch one file, so whole-file sync cannot conflict.
    2. Delivered is not done. A TASK is outstanding until its RESULT lands.
       The ACK only proves someone heard you.
    3. The wire carries DATA, never authority. Nothing that arrives here may
       decide what this machine is allowed to run. See peer.py: the handler
       dispatches on a local allowlist and nothing else.

updated: 2026-08-16
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta

V = 1
KINDS = ("TASK", "ACK", "RESULT", "HEARTBEAT")
MARKER = "#abus#"                    # last line of a wire message holds the JSON
DEFAULT_SLA_MIN = 15                 # a TASK with no RESULT past this is chased


# --------------------------------------------------------------------------
# envelope
# --------------------------------------------------------------------------

def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id():
    return uuid.uuid4().hex[:8]


def envelope(frm, to, kind, body, ref=None, eid=None, ts=None):
    """Build an envelope. `ref` points at the id this ACKs or reports on."""
    if kind not in KINDS:
        raise ValueError("unknown kind: %r (want one of %s)" % (kind, ", ".join(KINDS)))
    return {
        "v": V,
        "id": eid or new_id(),
        "ts": ts or now_iso(),
        "frm": frm,
        "to": to,
        "kind": kind,
        "body": body,
        "ref": ref,
    }


def to_wire(env):
    """Render an envelope as a chat message: humans read line 1, robots line N.

    The group is shared with people. If they cannot follow along at a glance,
    they stop reading the group, and then the group stops being a rail.
    """
    head = "%s %s -> %s  #%s" % (
        {"TASK": "[TASK]", "ACK": "[ACK]", "RESULT": "[RESULT]", "HEARTBEAT": "[HB]"}[env["kind"]],
        env["frm"], env["to"], env["id"],
    )
    body = (env.get("body") or "").strip()
    tail = MARKER + json.dumps(env, ensure_ascii=False, separators=(",", ":"))
    return head + ("\n" + body if body else "") + "\n" + tail


def from_wire(text):
    """Parse a chat message back into an envelope, or None if it is not ours.

    Returns None for human chatter, for malformed JSON and for anything whose
    shape we do not recognise. A rail shared with humans is full of messages
    that are not ours; that is normal, not an error.
    """
    if not text or MARKER not in text:
        return None
    raw = text[text.rindex(MARKER) + len(MARKER):].strip()
    try:
        env = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(env, dict):
        return None
    if env.get("v") != V or env.get("kind") not in KINDS:
        return None
    for field in ("id", "frm", "to"):
        if not isinstance(env.get(field), str) or not env[field]:
            return None
    if not re.match(r"^[A-Za-z0-9_.-]{1,32}$", env["frm"]) or \
       not re.match(r"^[A-Za-z0-9_.*-]{1,32}$", env["to"]):
        return None
    return env


# --------------------------------------------------------------------------
# ledger  (append-only, one shard per machine, corrupt lines survivable)
# --------------------------------------------------------------------------

class Ledger(object):
    """Append-only JSONL record of everything this machine sent and saw.

    One file, one writer: `<dir>/ledger/<MACHINE>.jsonl`. A peer reading your
    shard over a synced folder is fine; a peer WRITING it is the bug this
    layout makes impossible.
    """

    def __init__(self, root, machine):
        self.machine = machine
        self.dir = os.path.join(root, "ledger")
        self.path = os.path.join(self.dir, machine + ".jsonl")
        if not os.path.isdir(self.dir):
            os.makedirs(self.dir)

    def append(self, direction, env):
        rec = {"ts": now_iso(), "dir": direction, "env": env}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def read(self):
        """Yield (rec, ok). A garbage line yields (raw_string, False) and does
        NOT stop the read - one bad write must not eat the history after it."""
        if not os.path.isfile(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line), True
                except ValueError:
                    yield line, False

    def records(self):
        return [r for r, ok in self.read() if ok]

    def seen_ids(self):
        return set(r["env"]["id"] for r in self.records()
                   if r.get("dir") == "in" and isinstance(r.get("env"), dict))


# --------------------------------------------------------------------------
# outstanding work  ("delivered is not done")
# --------------------------------------------------------------------------

def outstanding(records, sla_min=DEFAULT_SLA_MIN, now=None):
    """TASKs this machine sent that have no RESULT yet, oldest first.

    Returns dicts: {id, to, body, sent, acked(bool), age_min, overdue(bool)}.
    An ACK moves nothing off this list - only a RESULT does. That asymmetry is
    the whole point: the sender owns the outcome, not the handoff.
    """
    now = now or datetime.utcnow()
    sent, acked, done = {}, set(), set()
    for r in records:
        env = r.get("env") or {}
        kind, direction = env.get("kind"), r.get("dir")
        if direction == "out" and kind == "TASK":
            sent[env["id"]] = env
        elif direction == "in" and kind == "ACK" and env.get("ref"):
            acked.add(env["ref"])
        elif direction == "in" and kind == "RESULT" and env.get("ref"):
            done.add(env["ref"])

    rows = []
    for eid, env in sent.items():
        if eid in done:
            continue
        try:
            t0 = datetime.strptime(env["ts"], "%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, KeyError):
            t0 = now
        age = (now - t0).total_seconds() / 60.0
        rows.append({
            "id": eid, "to": env.get("to"), "body": env.get("body"),
            "sent": env.get("ts"), "acked": eid in acked,
            "age_min": round(age, 1), "overdue": age > sla_min,
        })
    rows.sort(key=lambda r: r["age_min"], reverse=True)
    return rows


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

class FileTransport(object):
    """The offline rail: one append-only outbox file per sender.

    Used by the demo, and genuinely usable in production if your machines
    already share a folder (Syncthing / Dropbox / a git repo). Same
    single-writer rule: you append to YOUR file, you only read the others.
    """

    name = "file"

    def __init__(self, root, machine):
        self.machine = machine
        self.dir = os.path.join(root, "wire")
        self.cursor_path = os.path.join(root, "cursor-%s.json" % machine)
        if not os.path.isdir(self.dir):
            os.makedirs(self.dir)

    def send(self, env):
        path = os.path.join(self.dir, self.machine + ".outbox")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(to_wire(env).replace("\n", "\\n") + "\n")

    def _cursors(self):
        if os.path.isfile(self.cursor_path):
            try:
                with open(self.cursor_path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except ValueError:
                pass
        return {}

    def fetch(self):
        """Return every envelope addressed to us that we have not read yet."""
        cur = self._cursors()
        out = []
        for fname in sorted(os.listdir(self.dir)):
            if not fname.endswith(".outbox"):
                continue
            sender = fname[:-len(".outbox")]
            if sender == self.machine:
                continue                      # we do not read our own echo
            lines = open(os.path.join(self.dir, fname), "r", encoding="utf-8").read().splitlines()
            start = int(cur.get(sender, 0))
            for line in lines[start:]:
                env = from_wire(line.replace("\\n", "\n"))
                if env and env["to"] in (self.machine, "*"):
                    out.append(env)
            cur[sender] = len(lines)
        with open(self.cursor_path, "w", encoding="utf-8") as fh:
            json.dump(cur, fh)
        return out


class TelegramTransport(object):
    """The real rail: a Telegram group both machines and both humans are in.

    Deliberately a USER session (telethon / MTProto), not a bot. Telegram bots
    cannot see messages from other bots, so two bots in one group are deaf to
    each other and agent-to-agent traffic is impossible by platform rule. That
    dead end is why the ecosystem's Telegram+agent repos are all human->agent.
    See docs/GOTCHAS.md.

    Config (env): ABUS_TG_API_ID, ABUS_TG_API_HASH, ABUS_TG_CHAT,
    ABUS_TG_SESSION (path to the session file, default ./abus-<machine>.session).
    """

    name = "telegram"

    def __init__(self, root, machine, client=None, chat=None):
        self.machine = machine
        self.cursor_path = os.path.join(root, "tg-cursor-%s.json" % machine)
        if client is not None:
            # Injected client: used by demo.py scenario G to exercise the
            # cursor and filter logic without a phone, a login or a network.
            self._client, self._chat = client, chat
            return
        self.chat = os.environ.get("ABUS_TG_CHAT")
        self.api_id = os.environ.get("ABUS_TG_API_ID")
        self.api_hash = os.environ.get("ABUS_TG_API_HASH")
        self.session = os.environ.get("ABUS_TG_SESSION") or os.path.join(
            root, "abus-%s.session" % machine)
        missing = [k for k, v in (("ABUS_TG_API_ID", self.api_id),
                                  ("ABUS_TG_API_HASH", self.api_hash),
                                  ("ABUS_TG_CHAT", self.chat)) if not v]
        if missing:
            raise SystemExit("telegram transport needs: %s (see .env.example)"
                             % ", ".join(missing))
        try:
            from telethon.sync import TelegramClient       # noqa: F401
        except ImportError:
            raise SystemExit("telegram transport needs telethon: pip install telethon")
        from telethon.sync import TelegramClient
        self._client = TelegramClient(self.session, int(self.api_id), self.api_hash)
        self._client.start()
        self._chat = int(self.chat) if re.match(r"^-?\d+$", self.chat) else self.chat

    def send(self, env):
        self._client.send_message(self._chat, to_wire(env))

    def _last_seen(self):
        if os.path.isfile(self.cursor_path):
            try:
                with open(self.cursor_path, "r", encoding="utf-8") as fh:
                    return int(json.load(fh).get("min_id", 0))
            except (ValueError, TypeError):
                pass
        return 0

    def fetch(self):
        """Read new group messages, keep the ones addressed to us.

        Cursor is Telegram's own message id, persisted. Restarting the peer
        must not replay yesterday's tasks - that is the ACK log's job, but the
        cursor keeps us from making it do it.
        """
        min_id = self._last_seen()
        out, high = [], min_id
        for msg in self._client.iter_messages(self._chat, min_id=min_id, limit=200):
            high = max(high, msg.id)
            env = from_wire(getattr(msg, "text", "") or "")
            if env and env["to"] in (self.machine, "*") and env["frm"] != self.machine:
                out.append(env)
        with open(self.cursor_path, "w", encoding="utf-8") as fh:
            json.dump({"min_id": high}, fh)
        out.reverse()                      # iter_messages is newest-first
        return out


TRANSPORTS = {"file": FileTransport, "telegram": TelegramTransport}


def make_transport(name, root, machine):
    if name not in TRANSPORTS:
        raise SystemExit("unknown transport %r (have: %s)" % (name, ", ".join(TRANSPORTS)))
    return TRANSPORTS[name](root, machine)


# --------------------------------------------------------------------------
# the bus object peer.py drives
# --------------------------------------------------------------------------

class Bus(object):
    def __init__(self, machine=None, root=None, transport="file"):
        self.machine = machine or os.environ.get("ABUS_MACHINE") or "UNNAMED"
        self.root = root or os.environ.get("ABUS_DIR") or os.path.join(
            os.path.expanduser("~"), ".abus")
        if not os.path.isdir(self.root):
            os.makedirs(self.root)
        self.ledger = Ledger(self.root, self.machine)
        self.transport = make_transport(transport, self.root, self.machine)

    def send(self, to, body, kind="TASK", ref=None):
        env = envelope(self.machine, to, kind, body, ref=ref)
        self.transport.send(env)
        self.ledger.append("out", env)
        return env

    def fetch(self):
        """New envelopes for us, already de-duplicated against the ledger.

        Dedup is by envelope id against what we have RECORDED, not against
        what we have in memory: a peer that restarts mid-tick must not run the
        same task twice.
        """
        seen = self.ledger.seen_ids()
        fresh = []
        for env in self.transport.fetch():
            if env["id"] in seen:
                continue
            seen.add(env["id"])
            self.ledger.append("in", env)
            fresh.append(env)
        return fresh

    def outstanding(self, sla_min=DEFAULT_SLA_MIN):
        return outstanding(self.ledger.records(), sla_min=sla_min)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_send(args):
    bus = Bus(machine=args.machine, transport=args.transport)
    env = bus.send(args.to, args.body, kind=args.kind)
    print("sent #%s %s -> %s (%s)" % (env["id"], env["frm"], env["to"], env["kind"]))
    return 0


def cmd_inbox(args):
    bus = Bus(machine=args.machine, transport=args.transport)
    fresh = bus.fetch()
    if not fresh:
        print("inbox empty")
    for env in fresh:
        print("#%s %-9s from %-10s %s" % (env["id"], env["kind"], env["frm"],
                                          (env.get("body") or "")[:60]))
    return 0


def cmd_status(args):
    bus = Bus(machine=args.machine, transport=args.transport)
    rows = bus.outstanding(sla_min=args.sla)
    bad = sum(1 for _, ok in bus.ledger.read() if not ok)
    print("machine=%s  ledger=%s  corrupt_lines=%d" % (bus.machine, bus.ledger.path, bad))
    if not rows:
        print("nothing outstanding - every TASK sent has a RESULT back")
        return 0
    print("%-10s %-10s %-6s %-8s %s" % ("ID", "TO", "ACKED", "AGE_MIN", "BODY"))
    for r in rows:
        print("%-10s %-10s %-6s %-8s %s%s" % (
            "#" + r["id"], r["to"], "yes" if r["acked"] else "NO",
            r["age_min"], (r["body"] or "")[:40],
            "   <- OVERDUE" if r["overdue"] else ""))
    return 1 if any(r["overdue"] for r in rows) else 0


def cmd_selftest(args):
    """Prove the pure parts without any transport, network or peer."""
    checks = []

    e = envelope("A", "B", "TASK", "hello")
    checks.append(("roundtrip", from_wire(to_wire(e)) == e))
    checks.append(("human line first", to_wire(e).splitlines()[0].startswith("[TASK]")))
    checks.append(("chatter ignored", from_wire("just two humans talking") is None))
    checks.append(("garbage json ignored", from_wire("x " + MARKER + "{not json") is None))
    checks.append(("bad version ignored",
                   from_wire(MARKER + json.dumps({"v": 99, "kind": "TASK", "id": "a",
                                                  "frm": "A", "to": "B"})) is None))
    checks.append(("injected name ignored",
                   from_wire(MARKER + json.dumps({"v": V, "kind": "TASK", "id": "a",
                                                  "frm": "A; rm -rf /", "to": "B"})) is None))
    try:
        envelope("A", "B", "NUKE", "x")
        checks.append(("unknown kind rejected", False))
    except ValueError:
        checks.append(("unknown kind rejected", True))

    t0 = (datetime.utcnow() - timedelta(minutes=99)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recs = [{"dir": "out", "env": envelope("A", "B", "TASK", "old", eid="t1", ts=t0)},
            {"dir": "in", "env": envelope("B", "A", "ACK", "", ref="t1")}]
    rows = outstanding(recs)
    checks.append(("ack does not close a task", len(rows) == 1 and rows[0]["acked"]))
    checks.append(("overdue detected", rows[0]["overdue"]))
    recs.append({"dir": "in", "env": envelope("B", "A", "RESULT", "done", ref="t1")})
    checks.append(("result closes a task", outstanding(recs) == []))

    width = max(len(n) for n, _ in checks)
    for name, ok in checks:
        print("%s %s" % ("PASS" if ok else "FAIL", name.ljust(width)))
    failed = [n for n, ok in checks if not ok]
    print("\n%d/%d passed" % (len(checks) - len(failed), len(checks)))
    return 1 if failed else 0


def main(argv=None):
    p = argparse.ArgumentParser(description="agent-to-agent bus over a chat group")
    p.add_argument("--machine", default=os.environ.get("ABUS_MACHINE"),
                   help="this machine's name (env ABUS_MACHINE)")
    p.add_argument("--transport", default=os.environ.get("ABUS_TRANSPORT", "file"),
                   choices=sorted(TRANSPORTS))
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("send", help="send an envelope to a peer")
    s.add_argument("to")
    s.add_argument("body")
    s.add_argument("--kind", default="TASK", choices=KINDS)
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("inbox", help="pull and record new envelopes for us")
    s.set_defaults(func=cmd_inbox)

    s = sub.add_parser("status", help="what we asked for and never got back")
    s.add_argument("--sla", type=int, default=DEFAULT_SLA_MIN)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("selftest", help="prove the pure parts, no network")
    s.set_defaults(func=cmd_selftest)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
