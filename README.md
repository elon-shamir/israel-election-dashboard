# Israeli Election Odds

A static dashboard of the 2026 Knesset election: live Polymarket odds for every market tagged
`israel-election`, a seat projection built from them, and the final seat polls. No server and no
API key: the page calls Polymarket's public APIs straight from the browser (both allow any origin).

```bash
python3 -m http.server 8787 -d web     # http://127.0.0.1:8787
python3 polls.py                       # re-read the seat polls from Wikipedia into web/polls.json
python3 polymarket.py --out data/snapshot.json --closed --history 1w   # optional JSON export
```

## web/

- `polymarket.js`: fetches events (Gamma API, paged by tag) and price history (CLOB API), drops
  never-traded placeholder outcomes, orders bracket markets by range, and prices each outcome the way
  polymarket.com does (midpoint, or last trade if the spread is over 10¢).
- Seat projection (`projectSeats` in `polymarket.js`): expected seats per party from its seat-bracket
  and vote-share markets (the latter converted after the expected wasted vote), averaged by liquidity.
  The markets' total is fitted to 120 by moving thin markets most (correction proportional to
  seats / liquidity). A party is flagged low-confidence when over half its odds rest on last-trade
  prices. Blocs follow Polymarket's own definitions.
- `index.html`, Polymarket tab: headline tiles, the projected Knesset by bloc, any market's outcomes
  with a history chart, seat and vote-share distributions, and a sortable table of all markets.
  Refreshes every 30 seconds.
- `index.html`, Polls tab (`#polls`): an average of the last N polls or of the latest poll from each
  outlet, weighted by default (recency with a 7-day half-life, and √sample capped at 2,000) or equal,
  without Channel 14 and i24 News by default, by bloc and party next to the Polymarket projection,
  plus every poll.
- `polls.json`: written by `polls.py`. Polls are final (election silence from 23 Oct), so it is static.

## polls.py

Parses the 2026 seat-poll table on Wikipedia's polling page, keeping only polls taken with today's
party lineup (from 6 Sep). A party polled below the threshold is 0 seats, one marked N/A is left out
of averages, and one with no column in its table section had no list then (0). `SUPPLEMENT` holds the
final 23 Oct polls Wikipedia had not listed yet, each with its news source; a supplement row drops out
once Wikipedia lists that outlet's poll for the same date.

`polymarket.py` mirrors `polymarket.js` for the command-line export; the site does not use it.
