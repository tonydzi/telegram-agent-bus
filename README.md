# telegram-agent-bus

**Two agents, two machines, one Telegram group. They send each other tasks, ACK them, and report back. You are in the same group, reading along.**

Not a phone remote for one agent. That already exists a dozen times over. This is the other direction: machine A's agent asks machine B's agent for something, and neither machine needs a public IP, an open port, a VPN, or a server. The transport is a group chat you are already in.

```
$ ABUS_MACHINE=HUB python bus.py send LAPTOP "disk /"
sent #76046038 HUB -> LAPTOP (TASK)

$ ABUS_MACHINE=HUB python bus.py status
machine=HUB  ledger=/tmp/abus-live/ledger/HUB.jsonl  corrupt_lines=0
ID         TO         ACKED  AGE_MIN  BODY
#76046038  LAPTOP     NO     0.0      disk /

# ... on the laptop, wherever it is ...
$ python peer.py --machine LAPTOP --once
  <- TASK #76046038 from HUB: disk /
  -> RESULT #76046038: /: 270.1 GB free of 1000.2 GB

$ ABUS_MACHINE=HUB python bus.py status
nothing outstanding - every TASK sent has a RESULT back
```

That transcript is unedited output of those four commands on the file transport,
which is the same code path the Telegram transport plugs into.

## Check it before you believe it

```bash
git clone https://github.com/tonydzi/telegram-agent-bus
cd telegram-agent-bus
python demo.py
```

No packages, no network, no Telegram account, no API key. Two peers run in one
process over the file transport and seven scenarios assert their own end state,
so the exit code is the verdict. On a fresh clone it prints `22/22 checks passed`.

`python bus.py selftest` is the smaller, faster version: 10 checks on the pure
envelope and queue logic.

## Why this does not exist already

We looked before building. What is out there:

| What | Examples | Direction |
|---|---|---|
| Telegram bot that drives Claude Code for you | [RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram) (2.7k★) and ~11 more | human → agent |
| Multi-machine Claude Code | [Peter-Moriarty/claude-code-multi-machine-setup](https://github.com/Peter-Moriarty/claude-code-multi-machine-setup), `relava`, `kmichels/...` | config/KB **sync**, no messaging |
| Fleet layer with cross-machine coordination | [willau95/the-cc-harness](https://github.com/willau95/the-cc-harness) | agent ↔ agent over SSH+Tailscale, plus a React dashboard |
| Agent bus as a feature of a bot platform | [duckdash2/cc-telegram-bridge](https://github.com/duckdash2/cc-telegram-bridge) | bots collaborating **on one host** |

None of them is two agents on two machines talking through a chat. The reason
that gap exists is a Telegram platform rule, and it is worth stating plainly
because it is what stops everyone who tries the obvious thing first:

> Why doesn't my bot see messages from other bots? Bots talking to each other
> could potentially get stuck in unwelcome loops. To avoid this, we decided
> that bots will not be able to see messages from other bots regardless of mode.
>
> — [core.telegram.org/bots/faq](https://core.telegram.org/bots/faq)

So a bot per machine cannot work, in any configuration. Agent-to-agent over
Telegram needs **user sessions** (MTProto / telethon), not bots. That single
fact is most of what this repo has to teach; the rest is 700 lines of Python
(`bus.py` + `peer.py`, blanks and comments included).

## What is in the box

| File | What |
|---|---|
| `bus.py` | envelope, append-only ledger, outstanding-work bookkeeping, two transports, CLI |
| `peer.py` | the example app: the listening loop, the capability table, the chase |
| `demo.py` | seven offline scenarios, self-checking, doubles as the integration test |
| `docs/GOTCHAS.md` | every wall we hit, including the ones that are not in any doc |

Stdlib only. `telethon` is imported lazily and only by the Telegram transport,
so everything else runs on a bare Python 3.9+ (CI: 3.9, 3.11, 3.13 on Linux and macOS).

## Five rules that make it survive contact

1. **Single writer per file.** Every machine appends to its own ledger shard,
   `ledger/<MACHINE>.jsonl`. Two machines never write one file, so a synced
   folder cannot produce a conflict. This one invariant deletes a whole class
   of bugs.
2. **Delivered is not done.** An ACK proves someone heard you. Only a RESULT
   closes a task. `bus.py status` lists what you asked for and never got back,
   and exits non-zero when something is past its SLA, so a cron job can watch it.
3. **Silence gets chased, not assumed.** A task past the SLA gets one automatic
   nudge. Once. A bus that retries forever is a bus everybody mutes.
4. **The wire carries data, never authority.** Inbound text never selects code.
   `peer.py` dispatches on `CAPABILITIES`, a table this machine's owner wrote;
   an unknown request comes back as a refusal, not as a shell. A group chat is
   a place where anyone can type. Treat it like one.
5. **Humans read the same rail.** Every message is a readable first line plus a
   machine-readable tail. If your people cannot follow along at a glance, they
   stop reading the group, and then the group stops being a rail.

## Real Telegram, in about ten minutes

1. Get `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org) → API development tools.
2. Make a group. Put both of your Telegram accounts in it (or one account, if
   both machines run as you). Get its chat id.
3. On each machine:

```bash
pip install telethon
cp .env.example .env      # fill in, then source it
export ABUS_MACHINE=HUB ABUS_TRANSPORT=telegram
python peer.py --machine HUB          # first run asks for the login code
```

4. From the other machine: `python bus.py --transport telegram send HUB "uptime"`.

Full setup notes, the failure modes, and what each variable means:
[`docs/GOTCHAS.md`](docs/GOTCHAS.md).

## Honest limits

- **The Telegram transport is not covered by `demo.py`'s live path.** Scenario G
  drives it against a stubbed client, which tests our cursor, addressing and
  self-echo logic but not telethon and not Telegram. The machine this was written
  on has no Telegram session, so the live rail is verified by the setup above,
  by hand, not by CI. Said plainly rather than implied.
- **No encryption beyond Telegram's own.** Group messages are readable by
  everyone in the group and by Telegram. Do not put secrets on this rail.
  Capability names and results, yes. Tokens, no.
- **The file transport has no locking.** It relies on append-only writes and one
  writer per file. That has held for us; it is not a distributed log.
- **Ordering is per-sender.** There is no global clock and no attempt at one.
- **This is an example, not a framework.** It is meant to be read in one sitting
  and modified. The production system it was distilled from is 903 lines in its
  mailbox module alone, before the send gate and the ACK tracker; `bus.py` here
  is 503 with the interesting parts intact.

## Where it came from

Distilled from a fleet of machines running Claude Code that coordinate through a
Telegram group day to day: a human owner, an AI cofounder, and several computers
that are not always awake at the same time. The protocol layer above this one,
consensus and governance, lives in
[claude-consensus](https://github.com/tonydzi/claude-consensus). The private
content stays private; the pattern is here, MIT.

Built by Anton Dziatkovskii with his AI cofounder. Commits carry an
`Assisted-by:` trailer where that is true.

Issues and PRs welcome, especially "your rule 4 is not enough and here is why".
