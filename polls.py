"""Israeli election seat polls, from Wikipedia's polling page.

Polls are final (no poll may be published from the Friday before the vote), so this runs once:
  python3 polls.py [--out web/polls.json]

Only polls taken with today's party lineup are kept: the table starts a new section with new columns
whenever lists merge or split, and polls from an older lineup cannot be averaged with current ones.
"""

import argparse
import json
import os
import re
import sys
from datetime import date
from datetime import datetime
from datetime import timezone

import polymarket

PAGE = "Opinion_polling_for_the_2026_Israeli_legislative_election"
API = "https://en.wikipedia.org/w/api.php"

# Channel 14 (Shlomo Filber, a Likud affiliate) and i24 News (Direct Polls); the dashboard can leave them out
EXCLUDABLE = ["Channel 14", "i24 News"]

# the table's column labels -> the names the Polymarket projection uses
NAMES = {
    "Otzma": "Otzma Yehudit",
    "UTJ": "United Torah Judaism",
    "Dems": "The Democrats",
    "Blue & White": "Blue and White",
    "Reserv.-NEP": "Reservists–New Economic Party",
}
NOT_PARTIES = {"Fieldwork date", "Polling firm", "Publisher", "Sample size", "Others", "Gov."}
# final polls published 22-23 Oct that Wikipedia had not listed when this ran; a party not named
# in the report is left out (None), one reported below the threshold is 0
_FINAL_ZERO = {"Reservists–New Economic Party": 0, "Amcha Yisrael": 0, "Haredi Public": 0}
SUPPLEMENT = [
    {
        # the article's own bloc line (opposition 52, coalition 50) does not follow from these party figures
        "date": "2026-10-22", "fieldwork": "22 Oct", "firm": "Maagar Mochot", "publisher": "Channel 16", "sample": None,
        "seats": {"Yashar": 20, "Likud": 17, "Together": 14, "The Democrats": 9, "Otzma Yehudit": 9, "Joint List": 9,
                  "United Torah Judaism": 8, "Yisrael Beiteinu": 7, "Shas": 7, "RZP-Zehut": 7, "Ra'am": 5,
                  "Reservists–New Economic Party": 4, "Amcha Yisrael": 4, "Haredi Public": None},
        "source": "https://www.ice.co.il/research/news/article/1134767",
    },
    {
        "date": "2026-10-23", "fieldwork": "22–23 Oct", "firm": "Midgam R&C", "publisher": "HaHadashot 12", "sample": 1018,
        "seats": {"Yashar": 23, "Likud": 21, "Together": 15, "The Democrats": 10, "Joint List": 9, "Shas": 8,
                  "Yisrael Beiteinu": 8, "Otzma Yehudit": 8, "United Torah Judaism": 7, "RZP-Zehut": 6, "Ra'am": 5,
                  **_FINAL_ZERO},
        "source": "https://www.mako.co.il/news-israel-elections/2026/Article-8ea5e45ec2961a1027.htm",
    },
    {
        "date": "2026-10-23", "fieldwork": "23 Oct", "firm": "The Madad", "publisher": "Channel 13", "sample": 1875,
        "seats": {"Yashar": 22, "Likud": 19, "Together": 14, "The Democrats": 10, "Joint List": 9, "Yisrael Beiteinu": 8,
                  "Otzma Yehudit": 8, "United Torah Judaism": 8, "RZP-Zehut": 8, "Shas": 7, "Ra'am": 7, **_FINAL_ZERO},
        "source": "https://www.timesofisrael.com/final-polls-before-election-show-both-blocs-short-of-majority-small-parties-out-of-knesset/",
    },
    {
        "date": "2026-10-23", "fieldwork": "23 Oct", "firm": "Kantar", "publisher": "Kan 11", "sample": 1106,
        "seats": {"Yashar": 22, "Likud": 20, "Together": 14, "Joint List": 9, "The Democrats": 8, "Otzma Yehudit": 8,
                  "Yisrael Beiteinu": 8, "United Torah Judaism": 8, "Shas": 7, "RZP-Zehut": 7, "Ra'am": 5,
                  "Reservists–New Economic Party": 4, "Amcha Yisrael": 0, "Haredi Public": 0},
        "source": "https://www.timesofisrael.com/final-polls-before-election-show-both-blocs-short-of-majority-small-parties-out-of-knesset/",
    },
]

# samples Wikipedia leaves blank, filled only while it still does: (publisher, date) -> respondents
SAMPLE_FIXES = {("Israel Hayom", "2026-10-22"): 1110}

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def fetch_wikitext():
    data = polymarket._get(API, {"action": "parse", "page": PAGE, "prop": "wikitext|revid", "format": "json", "formatversion": 2})
    return data["parse"]["wikitext"], data["parse"]["revid"]


def _mask(s):
    """Hide the pipes inside {{templates}} and [[links]] so only cell separators remain."""
    out, depth, i = [], 0, 0
    while i < len(s):
        pair = s[i:i + 2]
        if pair in ("{{", "[["):
            depth += 1
            out.append(pair)
            i += 2
        elif pair in ("}}", "]]") and depth:
            depth -= 1
            out.append(pair)
            i += 2
        else:
            out.append("\x00" if s[i] == "|" and depth else s[i])
            i += 1
    return "".join(out)


def _cells(line):
    """A table line ('| a || b' or '! x') -> [(attrs, content)], with template pipes restored."""
    cells = []
    for cell in _mask(line[1:]).split("||" if line[0] == "|" else "!!"):
        attrs, content = cell.split("|", 1) if "|" in cell else ("", cell)
        cells.append((attrs.replace("\x00", "|"), content.replace("\x00", "|")))
    return cells


def _colspan(attrs):
    m = re.search(r'colspan\s*=\s*"?(\d+)', attrs)
    return int(m.group(1)) if m else 1


def _text(s):
    s = re.sub(r"\{\{efn[^{}]*\}\}", "", s)
    s = re.sub(r"\{\{(?:small|nowrap)\|([^{}]*)\}\}", r"\1", s, flags=re.I)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"<br\s*/?>", " ", s)
    s = s.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", s).strip()


def _seats(text):
    """'30' -> 30; '(1.9%)', a party polled below the threshold -> 0; '' or N/A -> None."""
    if re.fullmatch(r"\d+", text):
        return int(text)
    if text.startswith("(") or text.startswith("<"):
        return 0
    return None


def _date(opdrts):
    """{{Opdrts|21|22|Oct|2026}} -> (date(2026, 10, 22), '21–22 Oct')."""
    parts = [p.strip() for p in opdrts.strip("{}").split("|")[1:]]
    start, end, month, year = (parts + ["", "", "", ""])[:4]
    d = date(int(year), MONTHS[month[:3].lower()], int(end))
    label = f"{start}–{end} {month[:3]}" if start else f"{end} {month[:3]}"
    return d, label


def _sections(wikitext):
    """Yield (columns, rows) for each lineup section of the 2026 seat-projection table, newest first."""
    start = wikitext.index("=== 2026 ===", wikitext.index("== Seat projections =="))
    end = wikitext.index("\n==", start + 12)
    body = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", wikitext[start:end], flags=re.S)
    lines = body.split("\n")

    columns, rows, row, in_header = None, [], None, False
    for line in lines:
        if line.startswith("!") and "Fieldwork" in line:
            if columns is not None:
                yield columns, rows
            columns, rows, row, in_header = [], [], None, True
        if columns is None:
            continue
        if line.startswith("|-") or line.startswith("|}"):
            in_header = False
            if row:
                rows.append(row)
            row = [] if line.startswith("|-") else None
        elif in_header and line.startswith("!"):
            for attrs, content in _cells(line):
                columns.append((_text(content), _colspan(attrs), content))
        elif row is not None and line.startswith("|"):
            row.extend(_cells(line))
    if columns is not None:
        if row:
            rows.append(row)
        yield columns, rows


def _party_name(raw, text):
    links = re.findall(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", raw)
    name = "-".join(_text(link) for link in links) if len(links) > 1 else text
    name = re.sub(r"\s*-\s*", "-", name).strip()
    return NAMES.get(name, name)


def parse(wikitext):
    polls, lineup = [], None
    for columns, rows in _sections(wikitext):
        # a header ends at its first sub-row of blank colour cells; keep the named columns only
        named = []
        for text, span, raw in columns:
            if not text and not raw.strip():
                break
            named.append((text, span, raw))
        slots = []
        for text, span, raw in named:
            name = None if text in NOT_PARTIES or not text else _party_name(raw, text)
            slots.extend([name] * span)
        parties = [s for i, s in enumerate(slots) if s and (i == 0 or slots[i - 1] != s)]
        if lineup is None:
            lineup = parties
        elif not set(lineup) <= set(parties):
            break  # an older lineup: stop here

        for row in rows:
            if not row or "Opdrts" not in row[0][1]:
                continue  # the election-silence banner and other notes
            values = []
            for attrs, content in row:
                values.extend([(_text(content), _colspan(attrs))] * _colspan(attrs))
            when, fieldwork = _date(row[0][1])
            seats, i = {}, 0
            while i < min(len(slots), len(values)):
                name, (text, span) = slots[i], values[i]
                if name:
                    width = slots.count(name)
                    value = _seats(text)
                    if span < width:  # a joint list polled as two separate parties
                        parts = [_seats(values[i + k][0]) for k in range(width)]
                        known = [p for p in parts if p is not None]
                        value = sum(known) if known else None
                    seats[name] = value
                    i += width
                else:
                    i += 1
            if all(v is None for v in seats.values()):
                continue  # a news event spanning the row, not a poll
            sample = re.sub(r"[^\d]", "", values[3][0]) if len(values) > 3 else ""
            polls.append({
                "date": when.isoformat(),
                "fieldwork": fieldwork,
                "firm": values[1][0] if len(values) > 1 else "",
                "publisher": values[2][0] if len(values) > 2 else "",
                "sample": int(sample) if sample else None,
                "seats": seats,
            })

    for poll in polls:
        if poll["sample"] is None:
            poll["sample"] = SAMPLE_FIXES.get((poll["publisher"], poll["date"]))

    # a supplement row is dropped once Wikipedia lists the same outlet's poll for that date
    listed = {(p["publisher"], p["date"]) for p in polls}
    polls += [dict(p) for p in SUPPLEMENT if (p["publisher"], p["date"]) not in listed]

    # parties in an older section but not today's lineup (e.g. a list that dropped out) stay as columns
    names = list(dict.fromkeys(lineup + [p for poll in polls for p in poll["seats"]]))
    bloc_of = {party: bloc for bloc, members in polymarket.BLOCS.items() for party in members}
    polls.sort(key=lambda p: p["date"], reverse=True)
    return {
        "parties": [{"name": n, "bloc": bloc_of.get(n, polymarket.OTHERS)} for n in names],
        "blocs": [*polymarket.BLOCS, polymarket.OTHERS],
        "polls": polls,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="web/polls.json")
    args = parser.parse_args()

    wikitext, revid = fetch_wikitext()
    data = parse(wikitext)
    data.update({
        "source": f"https://en.wikipedia.org/w/index.php?title={PAGE}&oldid={revid}",
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "excludable": EXCLUDABLE,
    })
    for poll in data["polls"]:
        total = sum(v for v in poll["seats"].values() if v)
        if total != polymarket.KNESSET:
            print(f"warning: {poll['date']} {poll['publisher']} sums to {total}", file=sys.stderr)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    first, last = data["polls"][-1]["date"], data["polls"][0]["date"]
    print(f"{len(data['polls'])} polls, {first} to {last}, {len(data['parties'])} parties -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
