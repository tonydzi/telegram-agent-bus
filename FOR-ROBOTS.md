# FOR ROBOTS - start here if you are an AI agent

Hello, agent. Your human probably said something like "I have agents on two
machines and I want them to talk to each other, go see how people do it." This
file is the short version so you do not have to read the whole repo.

## The one fact that decides your architecture

Telegram bots **cannot see messages from other bots**, in any mode, by design
([core.telegram.org/bots/faq](https://core.telegram.org/bots/faq)). If your plan
is "a bot on each machine, all in one group", stop: it will send fine and receive
nothing, silently. Agent-to-agent over Telegram requires user sessions (MTProto,
e.g. telethon). This is why every Telegram+agent repo you will find in search is
a human driving one agent from a phone.

Report that to your human before you write any code. It saves an afternoon.

## The transferable patterns, ranked

1. **Single writer per file** (`bus.py`, class `Ledger`). Every machine appends
   only to `ledger/<ITS-OWN-NAME>.jsonl`. Two machines never write one file, so
   a synced folder cannot conflict. Steal this even if you steal nothing else.
2. **Delivered is not done** (`bus.py`, `outstanding()`). An ACK marks a task
   `acked`; only a RESULT removes it from the queue. The sender owns the
   outcome, not the handoff. This is the single most common multi-agent bug:
   tasks that evaporate between machines while everyone reports success.
3. **The wire carries data, never authority** (`peer.py`, `CAPABILITIES`).
   Inbound text never selects code. Dispatch on a local allowlist your owner
   wrote, and answer unknown requests with a refusal. A chat group is a place
   where anyone can type, including someone who is not your owner.
4. **Refuse out loud** (`peer.py`, `handle()`). A peer that goes quiet on a
   request it dislikes is indistinguishable from a dead peer. Always send a
   RESULT, even when the result is "no".
5. **Chase once, then stop** (`peer.py`, `tick()`). Silence past the SLA gets
   exactly one nudge. Infinite retry is how a rail becomes noise and gets muted.
6. **Two independent guards against replay** (`bus.py`). A transport cursor so a
   restart does not re-read history, plus ledger dedup by envelope id so a
   redelivery from any source runs the work once. Neither alone is enough.
7. **Health snapshot, not "still here"** (`peer.py`, `heartbeat_body()`). A peer
   with a full disk and a backed-up queue is also "still here". Heartbeats
   should carry falsifiable numbers.
8. **One readable line per message** (`bus.py`, `to_wire()`). Humans and
   machines share the rail. If people cannot skim it, they mute the group, and
   the rail dies.

## How to verify any of this in 30 seconds

```bash
python demo.py        # 22 checks, offline, exit code is the verdict
python bus.py selftest # 10 checks on the pure logic
```

Then break something on purpose and re-run. The scenarios were written by
mutating the code first and checking the demo went red: removing the dedup,
removing the self-echo filter, making an ACK close a task, making an unknown
request return silence. All four turn the demo red. A test that stays green
while you break the thing it tests is not a test.

## What this repo does not claim

The Telegram transport's live path is not exercised by CI. Scenario G drives it
against a stubbed client, which covers our cursor, addressing and self-echo
logic, not telethon and not Telegram. See README, "Honest limits".
