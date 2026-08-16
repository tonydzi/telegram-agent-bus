# Gotchas

Every wall we hit, in the order you are likely to hit them. Several of these
are not in any documentation, which is the only reason this file exists.

## 1. A bot per machine cannot work. Ever.

The obvious design is one Telegram bot per machine, all in one group. It cannot
work, and the failure is silent: your bots send happily and receive nothing.

> Bots talking to each other could potentially get stuck in unwelcome loops. To
> avoid this, we decided that bots will not be able to see messages from other
> bots regardless of mode.
> — [core.telegram.org/bots/faq](https://core.telegram.org/bots/faq)

"Regardless of mode" means privacy mode, admin rights and group type do not help.
This is why every Telegram+agent repo you can find is a human driving one agent
from a phone: the bot API physically cannot carry agent-to-agent traffic.

**What to do:** use a user session (MTProto, `telethon` here). The account is a
normal Telegram account. It sees other accounts, and it sees bots.

## 2. One bot token, two pollers, silent theft

Related, and worth knowing even though this repo does not use bots: `getUpdates`
hands each update to exactly one caller and drops the losers with a 409. Two
machines polling one token means each sees a random half of the traffic. If you
build a bot-based side rail, give every process its own token.

## 3. Sessions are single-use files

A telethon `.session` is a SQLite file holding an authorized connection. Copying
one to a second machine and running both gets the session invalidated or
throttled. Log in separately on each machine, keep the session next to that
machine's state, and never sync the file. `.gitignore` here covers `*.session`
for the same reason.

## 4. Your own messages come back at you

You read the group; the group contains what you just posted. Without a filter a
peer answers itself, then answers the answer. `TelegramTransport.fetch()` drops
anything whose `frm` is us.

The first version of the test for this used a message addressed to the other
machine, which the address filter caught anyway, so the check stayed green while
the self-echo guard was dead code. Scenario G now uses a broadcast from
ourselves, which only the self-echo guard can drop. If you change that filter,
change the mutant with it.

## 5. Restarting a peer must not replay yesterday

Two independent guards, and you want both:

- the transport cursor (Telegram's own message id, persisted) so you do not
  re-read the backlog;
- ledger dedup by envelope id, so a redelivery from anywhere runs the work once.

The cursor alone breaks when the file is lost. Dedup alone means a restart
re-reads a year of group history and does a lot of pointless parsing.

## 6. `min_id` is exclusive and `iter_messages` is newest-first

`iter_messages(chat, min_id=N)` returns messages with id **greater than** N,
newest first. Store the highest id you saw, and reverse the batch before
handling it, or your peer processes a burst backwards and the RESULT for a task
lands before the task.

## 7. Chat ids for groups are negative and change shape

A basic group is `-123456789`. Once it is upgraded to a supergroup the id turns
into `-100...`, and the old one stops resolving. If your peer suddenly goes deaf
after someone changed a group setting, re-read the id.

## 8. Rate limits are per account, not per chat

Telegram will hand you a `FloodWaitError` with a number of seconds. Heartbeats
every 20 seconds across five machines is enough to meet it. Defaults here are a
20 second poll and a heartbeat every 30 ticks, which is about ten minutes.
Polling is cheap; sending is what gets you limited.

## 9. The group is a shared, untrusted, human-readable space

Three consequences, all load-bearing:

- **Untrusted.** Anyone in the group can type an envelope. `peer.py` dispatches
  only on its own capability table, and `from_wire` rejects machine names that
  are not `[A-Za-z0-9_.-]`. Never grow this into "run whatever the body says".
- **Shared.** Do not put secrets, tokens or customer data on the rail. Telegram
  can read it and so can everyone in the group.
- **Human-readable.** Keep the first line something a person can skim. The
  moment your teammates start muting the group, you have lost the property that
  made a chat a good bus in the first place.

## 10. The file transport is not a distributed log

It works because of two rules, not because of locking: append-only writes, and
one writer per file. Break either (two machines sharing a name, a peer editing
another peer's outbox) and you get exactly the sync conflicts the design was
supposed to remove. Machine names are identities. Do not reuse them.
