// ==UserScript==
// @name         FF Agent — ESPN draft bridge
// @namespace    local.ffagent
// @version      1.1
// @author       Carter
// @description  Mirror an ESPN draft room into the local draft board. Read-only; data never leaves this machine.
// @match        https://fantasy.espn.com/football/draft*
// @grant        GM_xmlhttpRequest
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

// WHY A USERSCRIPT AND NOT A CONSOLE SNIPPET
// ESPN's read API publishes nothing during a live draft (verified: mDraftDetail
// and mRoster both return inProgress=true with zero picks, then every pick the
// moment it finalises). So the draft room's DOM is the only live source. But a
// snippet pasted into that page cannot reach http://localhost -- Chrome blocks
// the https->http hop from page context, silently, with the request simply
// never resolving. GM_xmlhttpRequest runs in the extension's context, which is
// not subject to that, so this is the transport that actually works.
//
// It also means no pasting: open the draft room and it runs.

(() => {
  "use strict";

  // ESPN leagueId -> the name in leagues.yaml. Add a line per league.
  const LEAGUES = {
    "779624":  "family",
    "371699":  "friends",
  };
  const BOARD = "http://localhost:8777";
  const EVERY = 2000;

  const leagueId = new URLSearchParams(location.search).get("leagueId");
  const league = LEAGUES[leagueId];
  if (!league) {
    console.warn(`[ff] leagueId ${leagueId} isn't mapped in the bridge — add it.`);
    return;
  }

  const T = el => (el?.textContent || "").replace(/\s+/g, " ").trim();

  function scrape() {
    return [...document.querySelectorAll(".draft-board-grid-pick-cell.completedPick")]
      .map(c => {
        const [round, slot] = T(c.querySelector(".roundPick")).split(".").map(Number);
        return {
          round, slot,
          name: (T(c.querySelector(".playerFirstName")) + " "
                 + T(c.querySelector(".playerLastName"))).trim(),
          team: T(c.querySelector(".playerProTeam")),
          position: T(c.querySelector(".positionPill")),
          // The room tags your own cells, so ownership needs no id matching.
          mine: c.classList.contains("myTeam"),
        };
      })
      .filter(p => p.name && p.round)
      .map((p, i) => ({ ...p, pick_no: i + 1 }));
  }

  let last = -1, fails = 0;

  function post(picks) {
    GM_xmlhttpRequest({
      method: "POST",
      url: `${BOARD}/ingest?league=${encodeURIComponent(league)}`,
      headers: { "Content-Type": "text/plain" },
      data: JSON.stringify({ picks }),
      timeout: 5000,
      onload: r => {
        fails = 0;
        let j = {};
        try { j = JSON.parse(r.responseText); } catch (_) {}
        if (j.error) return console.error("[ff] board rejected:", j.error);
        if (picks.length !== last) {
          last = picks.length;
          console.log(`[ff] ${j.picks} picks -> ${league}, on the clock: ${j.on_clock}`
            + (j.unmatched?.length ? ` (not on our board: ${j.unmatched.join(", ")})` : ""));
        }
      },
      // One failed request must never stop the bridge; the draft doesn't pause.
      onerror:   () => warn(),
      ontimeout: () => warn(),
    });
  }

  function warn() {
    if (++fails === 1 || fails % 15 === 0)
      console.warn(`[ff] can't reach ${BOARD} (${fails}x). Is draft_server.py running?`);
  }

  setInterval(() => post(scrape()), EVERY);
  post(scrape());
  console.log(`[ff] bridging this draft room -> ${league}. Read-only.`);
})();
