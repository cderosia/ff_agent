// ESPN live-draft bridge — paste into the draft room's DevTools console.
//
// WHY THIS EXISTS
// ESPN's read API publishes nothing while a draft is live. Verified on a real
// draft: mDraftDetail and mRoster both returned `inProgress: true` with ZERO
// picks and ZERO rostered players, polled repeatedly for 40+ seconds. The
// moment the draft finalised, the same endpoint returned all 60 picks. So
// there is no server-side way to follow an ESPN draft as it happens.
//
// The draft room itself has every pick in the DOM. This reads it and posts it
// to the local board, which turns ESPN from a manual-entry league into a live
// one.
//
// HOW TO USE
//   1. Start the board:  python3 scripts/draft_server.py
//   2. Set `ingest: true` on the league in leagues.yaml, and restart.
//   3. Open your ESPN draft room, F12 -> Console, paste this, Enter.
//   4. Watch the board. Stop it any time with:  __ffStop()
//
// It re-sends the WHOLE board every tick rather than diffing, so a dropped
// request, a page reload or a reconnect self-heals on the next tick.

(() => {
  const LEAGUE = "testdraft";                  // <-- the name in leagues.yaml
  const BOARD  = "http://localhost:8777";
  const EVERY  = 2000;                          // ms

  const T = el => (el?.textContent || "").replace(/\s+/g, " ").trim();

  function scrape() {
    return [...document.querySelectorAll(".draft-board-grid-pick-cell.completedPick")]
      .map(c => {
        const [rd, slot] = T(c.querySelector(".roundPick")).split(".").map(Number);
        const first = T(c.querySelector(".playerFirstName"));
        const last  = T(c.querySelector(".playerLastName"));
        return {
          round: rd, slot,
          name: `${first} ${last}`.trim(),
          team: T(c.querySelector(".playerProTeam")),
          position: T(c.querySelector(".positionPill")),
          // the draft room tags your own cells; that's how the board knows
          // which picks are yours without matching team ids.
          mine: c.classList.contains("myTeam"),
        };
      })
      .filter(p => p.name && p.round)
      .map((p, i) => ({ ...p, pick_no: i + 1 }));
  }

  let last = -1, fails = 0;
  async function tick() {
    const picks = scrape();
    try {
      const r = await fetch(`${BOARD}/ingest?league=${encodeURIComponent(LEAGUE)}`, {
        method: "POST",
        headers: { "Content-Type": "text/plain" },   // simple request: no preflight
        body: JSON.stringify({ picks }),
      });
      const j = await r.json();
      fails = 0;
      if (j.error) console.error("[ff] board rejected:", j.error);
      else if (picks.length !== last) {
        last = picks.length;
        console.log(`[ff] ${j.picks} picks -> board, on the clock: ${j.on_clock}`
          + (j.unmatched?.length ? ` (not on our board: ${j.unmatched.join(", ")})` : ""));
      }
    } catch (e) {
      // Don't die on one bad request -- the draft doesn't stop for it.
      if (++fails === 1 || fails % 10 === 0)
        console.warn(`[ff] can't reach the board (${fails}x). Is draft_server running?`);
    }
  }

  clearInterval(window.__ffTimer);
  window.__ffTimer = setInterval(tick, EVERY);
  window.__ffStop = () => { clearInterval(window.__ffTimer); console.log("[ff] stopped."); };
  tick();
  console.log(`[ff] bridging this draft room -> ${BOARD} as "${LEAGUE}". Stop with __ffStop()`);
})();
