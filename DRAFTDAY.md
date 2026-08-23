# Draft day

Everything you need to do, per league. Read the two-minute version, ignore the
rest unless something breaks.

---

## The two-minute version

```bash
cd ~/ff
python3 scripts/draft_server.py          # leave this running
open http://localhost:8777
```

| league | platform | what you do |
|---|---|---|
| `719` | Sleeper | nothing — picks arrive on their own |
| `freinds-keeper` | Sleeper | nothing — keepers are already loaded |
| `family` | ESPN | open the draft room; the userscript bridges it |
| `friends` | ESPN | open the draft room; the userscript bridges it |
| `work` | Yahoo | type each pick into the board by hand |

Then draft. The board follows along.

---

## Once, before the first draft

**1. Confirm your draft slot is set.** Without it the board can't tell you when
you're up or what waiting costs. It lives at the top of each league block in
`leagues/leagues.yaml`:

```yaml
  - name: friends
    draft_slot: 1
```

Known: `work` 5, `freinds-keeper` 9, `friends` 1, `family` 10 (from ESPN).
**`719` has none** — set it before that draft.

Don't rely on the platform to supply it. ESPN's published order came back empty
after a draft was reset, and Sleeper publishes nothing until the draft opens. A
configured slot always wins over the platform's answer.

**2. ESPN leagues only — install the bridge.** ESPN's API publishes *nothing*
during a live draft (verified: zero picks while `inProgress`, then all of them
the moment it finalises), so the board reads the draft room itself.

- Install **Tampermonkey** (Chrome Web Store)
- Tampermonkey icon → *Create a new script*, delete the template
- Paste all of `scripts/espn_draft_bridge.user.js`, Ctrl+S

It runs automatically on any ESPN draft room and survives page reloads. It is
read-only, and `@connect localhost` stops it reaching anything but your machine.

**3. Rehearse the one you'll type by hand.** Only Yahoo needs this now:

```bash
python3 scripts/draft_server.py --sim work --slot 5 --transcribe
```

That drills the part that actually bites: getting every pick the room makes into
the board before the next one lands.

---

## During the draft

**The card at the top says where picks are coming from.** On a bridged league it
reads *"live from the espn draft room · last sync 2s ago"*. If it says **bridge
not connected**, the userscript isn't running — the manual entry box reappears
underneath as the fallback.

**Read the board in this order:**

1. **The case for your next pick** — the top three with a sentence each on *why*.
2. **Cost of waiting** — best available now vs. what survives to your next pick.
   This is the number that decides between two similar players.
3. **Take now** — the ranked board. `lineup` is what he adds to your starters,
   `season` is expected points for a bench piece, `gone by next pick` is his odds
   of not lasting.

**Trust it most in rounds 1-6.** After that the lineup maths flattens (a bench
player adds no starting points by definition) and ordering leans on roster
limits and assumed injury rates. Treat late rounds as a shortlist of five, not
an instruction.

**Override it when you know something it doesn't** — news, a holdout, a beat
report. Projections are a preseason snapshot. The ◆ marker means the three
sources disagree about him, so the projection is soft.

---

## When something breaks

**Board frozen / picks not arriving (ESPN).** Check the console in the draft
room for `[ff]` lines. No lines at all means the userscript didn't run — reload
the draft room. `can't reach the board` means the server died; restart it. The
bridge re-sends the whole board every tick, so it repairs itself on reconnect;
you never need to catch up by hand.

**A pick shows as `espn:123456`.** That player isn't in our projections. Harmless
for kickers and defenses, which are excluded on purpose. For anyone else, tell
me — it means a name didn't join.

**You need to fall back to manual mid-draft.** Just type into the entry box. On a
bridged league the bridge will overwrite you on its next tick, but its picture
comes from the draft room itself, so it's authoritative — nothing is lost.

**Server restart mid-draft is safe.** Picks are on disk, and the bridge
reconnects on its own within a couple of seconds.

---

## After

```bash
python3 scripts/build_boards.py        # refresh the static boards
```

In-season work (lineups, waivers, trades) runs off the same value engine —
see `scripts/weekly_report.py`.
