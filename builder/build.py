#!/usr/bin/env python3
"""Oscars Death Race 2027 builder (v2: Gold Derby + where-to-watch).

Odds: Gold Derby's combined nomination odds for "oscars-nominations-2027"
(every category). Gold Derby now shows a % chance instead of fractional odds,
so PTS is that % (the same number as 100 * den / (num + den)) and the
fractional odds are derived back from it.

  PTS    = Gold Derby combined % chance of a nomination in a category
  Points = sum of a film's PTS across every category entry
  Sort   = by highest category (Picture, Director, Actress, ...), then rank

Viability: contenders at 100/1 or longer (PTS <= 100/101) are dropped by
default. data.json keeps everything down to FLOOR_PTS so the page can switch
the cutoff (100/1, 50/1, 25/1) without a rebuild.

Where to watch (per film, US): JustWatch's public GraphQL API (theater
showtimes = CINEMA offers, subscription streaming, rent/buy, upcoming digital
dates, poster) and Wikidata (US release date, matched by TMDB id). If the
TMDB_API_KEY environment variable is set, TMDB's API adds limited vs wide vs
digital US dates. No keys are required.

Usage:
  python3 build.py --out DIR            # fetch everything, write DIR/index.html, DIR/data.json
  python3 build.py --out DIR --raw F    # rebuild odds from a saved raw.json (film info still fetched)

Config (next to this script):
  people.json  film_aliases (alternate title -> canonical), display_titles
               (canonical -> title shown, e.g. a foreign-language title),
               justwatch_ids / tmdb_ids (canonical title -> id, to fix a
               wrong or missing match), documentaries (extra titles to treat as docs)
  films.json   cache of matched JustWatch/TMDB ids (written by the build;
               delete an entry to force a re-match)
Unmatched films are listed in DIR/unmatched.txt.
"""
import argparse, base64, html as htmllib, json, os, re, sys, time, unicodedata, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta, date
from fractions import Fraction
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from imgsheets import load_json, save_json, pack, externalize, internalize  # image caches live in JPEG sheets, not base64 JSON

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
GD_PAGE = "https://www.goldderby.com/odds/combined-odds/oscars-nominations-2027/"
GD_LAZY = "https://www.goldderby.com/wp-json/gameplay/v1/lazy-nominees/{league}/{cat}/combined"
JW_API = "https://apis.justwatch.com/graphql"
FLOOR_PTS = 0.4          # keep in data.json (about 250/1); page default cutoff is 100/1
DEFAULT_CUTOFF = 100
ELIGIBLE_FROM = "2026-01-01"  # 99th Oscars eligibility year starts     # drop 100/1 or longer
YEARS = (2024, 2025, 2026, 2027)

# Ravi's category order (2022 list), with Casting added after Editing.
ORDER = ["Best Picture", "Best Director", "Best Actress", "Best Actor", "Best Supporting Actress",
         "Best Supporting Actor", "Best Original Screenplay", "Best Adapted Screenplay",
         "Best International Film", "Best Animated Feature", "Best Documentary Feature",
         "Best Cinematography", "Best Film Editing", "Best Casting", "Best Visual Effects",
         "Best Production Design", "Best Costume Design", "Best Makeup and Hairstyling",
         "Best Sound", "Best Score", "Best Song"]


def fetch(url, data=None, headers=None, tries=4, as_json=False):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=40) as r:
                b = r.read()
                return json.loads(b) if as_json else b
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(2 ** i)


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^(the|a|an) ", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def odds(p):
    """Fractional odds (against:for) from a % chance, like Gold Derby used to print."""
    if p <= 0:
        return "-"
    if p >= 100:
        return "1/100"
    x = (100 - p) / p
    if x >= 1.75:
        return f"{round(x)}/1"
    if x < 0.5:
        return f"1/{round(1 / x)}"
    f = Fraction(x).limit_denominator(5)
    return f"{f.numerator}/{f.denominator}"


# ---------------------------------------------------------------- Gold Derby
def _txt(s):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def parse_contestants(chunk):
    out = []
    for p in chunk.split('data-component="predictions-contestant-item"')[1:]:
        t = re.search(r"predictions-contestant-item__title'[^>]*>(.*?)</div>", p, re.S)
        st = re.search(r"predictions-contestant-item__sub-title'[^>]*>(.*?)</div>", p, re.S)
        pc = re.search(r'progress-text">\s*([\d.]+)%', p)
        if not t or not pc:
            continue
        recs = re.findall(r"records-title'[^>]*>([^<]*)<.*?records-value'[^>]*>([^<]*)<", p, re.S)
        rec = {}
        for i, (k, v) in enumerate(recs[:6]):  # first triple = share predicting a nomination, second = a win
            m = re.match(r"([\d.]+)%", v.strip())
            rec[k.strip().rstrip(":").lower() + ("" if i < 3 else "_win")] = float(m.group(1)) if m else None
        out.append({"title": _txt(t.group(1)), "sub": _txt(st.group(1)) if st else None,
                    "pct": float(pc.group(1)), "groups": rec})
    return out


def fetch_goldderby():
    page = fetch(GD_PAGE).decode("utf-8", "replace")
    raw = {"fetched_at": datetime.now(timezone.utc).isoformat(), "source": GD_PAGE, "categories": []}
    for seg in page.split('data-component="predictions-award-item"')[1:]:
        cat = re.search(r'data-category="(\d+)"', seg)
        league = re.search(r'data-league-post-id="(\d+)"', seg)
        label = re.search(r"<h3[^>]*>(.*?)</h3>", seg, re.S)
        if not (cat and league and label):
            continue
        label = _txt(label.group(1))
        rows = parse_contestants(seg)
        try:
            lazy = fetch(GD_LAZY.format(league=league.group(1), cat=cat.group(1)), as_json=True)
            if lazy and lazy.get("html"):
                rows += parse_contestants(lazy["html"])
        except Exception as e:
            print(f"lazy list failed for {label}: {e}", file=sys.stderr)
        raw["categories"].append({"label": label, "gd_cat": int(cat.group(1)), "rows": rows})
        print(f"{label}: {len(rows)} contenders", file=sys.stderr)
    return raw


# ---------------------------------------------------------------- film info
JW_FIELDS = """id content(country:US,language:"en"){title originalTitle originalReleaseYear originalReleaseDate posterUrl genres{shortName} externalIds{tmdbId imdbId} upcomingReleases{releaseDate releaseType package{clearName}} shortDescription runtime ageCertification credits{role name} scoring{tomatoMeter certifiedFresh imdbScore}} offers(country:US,platform:WEB){monetizationType presentationType package{clearName}}"""
JW_SEARCH = "query S($q:String!){popularTitles(country:US,first:8,filter:{searchQuery:$q,objectTypes:[MOVIE]}){edges{node{... on Movie{" + JW_FIELDS + "}}}}}"
JW_NODE = "query N($id:ID!){node(id:$id){... on Movie{" + JW_FIELDS + "}}}"
WIKIDATA = "https://query.wikidata.org/sparql"
TMDB_API = "https://api.themoviedb.org/3"


def jw_post(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    return fetch(JW_API, data=body, headers={"content-type": "application/json"}, as_json=True) or {}


def jw_search(q):
    d = jw_post(JW_SEARCH, {"q": q})
    return [e["node"] for e in (((d.get("data") or {}).get("popularTitles") or {}).get("edges") or []) if e.get("node")]


def jw_node(jid):
    return ((jw_post(JW_NODE, {"id": jid}).get("data") or {}).get("node")) or None


def match_film(title, cache, overrides, jw_overrides=None):
    """JustWatch title for a film: {jw, tmdb}. justwatch_ids / tmdb_ids overrides win, then the cache."""
    k = norm(title)
    if jw_overrides and k in jw_overrides:
        return {"jw": jw_overrides[k], "tmdb": None}
    want_tmdb = str(overrides[k]) if k in overrides else None
    c = cache.get(k) or {}
    if c.get("jw") and (want_tmdb is None or c.get("tmdb") == want_tmdb):
        return {"jw": c["jw"], "tmdb": c.get("tmdb")}
    try:
        for n in jw_search(title):
            ct = n.get("content") or {}
            tid = str((ct.get("externalIds") or {}).get("tmdbId") or "") or None
            if want_tmdb:
                if tid == want_tmdb:
                    return {"jw": n["id"], "tmdb": tid}
                continue
            if (ct.get("originalReleaseYear") or 0) in YEARS and k in (norm(ct.get("title") or ""), norm(ct.get("originalTitle") or "")):
                return {"jw": n["id"], "tmdb": tid}
    except Exception as e:
        print("justwatch search failed:", title, e, file=sys.stderr)
    return {"tmdb": want_tmdb} if want_tmdb else {}


def wikidata_us_dates(tmdb_ids):
    """{tmdb id: earliest US publication date} from Wikidata (no key needed)."""
    out = {}
    ids = sorted({str(t) for t in tmdb_ids if t})
    for i in range(0, len(ids), 150):
        vals = " ".join(f'"{t}"' for t in ids[i:i + 150])
        q = ("SELECT ?tmdb ?date ?place WHERE { VALUES ?tmdb { %s } ?film wdt:P4947 ?tmdb . "
             "?film p:P577 ?st . ?st ps:P577 ?date . OPTIONAL { ?st pq:P291 ?place } }" % vals)
        try:
            d = fetch(WIKIDATA + "?" + urllib.parse.urlencode({"query": q}),
                      headers={"Accept": "application/sparql-results+json", "User-Agent": "oscars-death-race-tracker/2.0 (personal list)"}, as_json=True)
        except Exception as e:
            print("wikidata failed:", e, file=sys.stderr)
            continue
        for b in (d or {}).get("results", {}).get("bindings", []):
            place = b.get("place", {}).get("value", "")
            if not place.endswith("/Q30"):  # United States
                continue
            t, dt = b["tmdb"]["value"], b["date"]["value"][:10]
            if t not in out or dt < out[t]:
                out[t] = dt
    return out


def wikidata_enwiki(tmdb_ids):
    """{tmdb id: English Wikipedia article title} from Wikidata sitelinks (for the film page's Wikipedia plot link)."""
    out = {}
    ids = sorted({str(t) for t in tmdb_ids if t})
    for i in range(0, len(ids), 150):
        vals = " ".join(f'"{t}"' for t in ids[i:i + 150])
        q = ("SELECT ?tmdb ?article WHERE { VALUES ?tmdb { %s } ?film wdt:P4947 ?tmdb . "
             "?article schema:about ?film ; schema:isPartOf <https://en.wikipedia.org/> . }" % vals)
        try:
            d = fetch(WIKIDATA + "?" + urllib.parse.urlencode({"query": q}),
                      headers={"Accept": "application/sparql-results+json", "User-Agent": "oscars-death-race-tracker/2.0 (personal list)"}, as_json=True)
        except Exception as e:
            print("wikidata enwiki failed:", e, file=sys.stderr)
            continue
        for b in (d or {}).get("results", {}).get("bindings", []):
            url = b["article"]["value"]
            out[b["tmdb"]["value"]] = urllib.parse.unquote(url.rsplit("/wiki/", 1)[-1]).replace("_", " ")
    return out


def wikidata_tech(tmdb_ids):
    """{tmdb id: {"ar": [aspect ratios], "lang": [original languages]}} from Wikidata (P2061, P364)."""
    out = {}
    ids = sorted({str(t) for t in tmdb_ids if t})
    for i in range(0, len(ids), 150):
        vals = " ".join(f'"{t}"' for t in ids[i:i + 150])
        q = ("SELECT ?tmdb ?arLabel ?langLabel WHERE { VALUES ?tmdb { %s } ?film wdt:P4947 ?tmdb . "
             "OPTIONAL { ?film wdt:P2061 ?ar } OPTIONAL { ?film wdt:P364 ?lang } "
             "SERVICE wikibase:label { bd:serviceParam wikibase:language \"en\". } }" % vals)
        try:
            d = fetch(WIKIDATA + "?" + urllib.parse.urlencode({"query": q}),
                      headers={"Accept": "application/sparql-results+json", "User-Agent": "oscars-death-race-tracker/2.0 (personal list)"}, as_json=True)
        except Exception as e:
            print("wikidata tech failed:", e, file=sys.stderr)
            continue
        for b in (d or {}).get("results", {}).get("bindings", []):
            o = out.setdefault(b["tmdb"]["value"], {"ar": [], "lang": []})
            for k, f in (("ar", "arLabel"), ("lang", "langLabel")):
                v = (b.get(f) or {}).get("value")
                if v and not re.match(r"^Q\d+$", v) and v not in o[k]:
                    o[k].append(v)
    return out


PLOT_DAYS, PLOT_MAX_WIKI, PLOT_MAX_SPOILER = 7, 200, 30   # refresh saved plots weekly; fetches per run
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_UA = {"User-Agent": "oscars-death-race-tracker/2.0 (personal list)"}


def _wikitext_plain(w):
    w = re.sub(r"<!--.*?-->", "", w, flags=re.S)
    w = re.sub(r"<ref[^>/]*/>", "", w)
    w = re.sub(r"<ref[^>]*>.*?</ref>", "", w, flags=re.S)
    w = re.sub(r"\{\{\s*'(s?)\s*\}\}", r"'\1", w)   # {{'s}} / {{'}} apostrophe templates
    w = re.sub(r"\{\{\s*(?:nowrap|lang\|[^|{}]*)\|([^{}|]*)\}\}", r"\1", w, flags=re.I)
    for _ in range(5):  # templates, innermost first
        w2 = re.sub(r"\{\{[^{}]*\}\}", "", w)
        if w2 == w:
            break
        w = w2
    w = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", w, flags=re.I)
    w = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", w)
    w = re.sub(r"\[\[([^\]]*)\]\]", r"\1", w)
    w = re.sub(r"\[https?://\S+ ([^\]]*)\]", r"\1", w)
    w = re.sub(r"'''''|''(?='s)", "", w)   # keep the apostrophe in ''Title'''s
    w = re.sub(r"'''|''", "", w)
    w = re.sub(r"<[^>]+>", "", w)
    w = htmllib.unescape(w)
    w = re.sub(r"^=== ?(.*?) ?===\s*$", r"\1:", w, flags=re.M)
    w = re.sub(r"[ \t]+", " ", w)
    w = re.sub(r" ([,.;:])", r"\1", w)
    return re.sub(r"\n{3,}", "\n\n", w).strip()


def wikipedia_plots(titles):
    """{article title: {text, section} or {none: True}}: the Plot (or Premise/Synopsis) section of each English
    Wikipedia article, as plain text. Fetches wikitext 20 articles per request, since Wikimedia rate-limits hard."""
    out, titles = {}, list(dict.fromkeys(titles))
    for i in range(0, len(titles), 20):
        chunk = titles[i:i + 20]
        url = WIKI_API + "?" + urllib.parse.urlencode({"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main",
                                                      "format": "json", "formatversion": 2, "redirects": 1, "titles": "|".join(chunk)})
        d = None
        for k in range(5):
            try:
                d = fetch(url, headers=WIKI_UA, tries=1, as_json=True)
                break
            except Exception as e:
                print("wikipedia plots: retrying after", e, file=sys.stderr)
                time.sleep(10 * (k + 1))
        if not d:
            continue
        q = d.get("query") or {}
        back = {}
        for m in (q.get("normalized") or []) + (q.get("redirects") or []):
            back[m["to"]] = back.get(m["from"], m["from"])
        for pg in q.get("pages") or []:
            src = back.get(pg.get("title"), pg.get("title"))
            src = back.get(src, src)
            revs = pg.get("revisions") or []
            w = revs[0]["slots"]["main"].get("content", "") if revs else ""
            m = re.search(r"^== ?(Plot|Premise|Synopsis|Plot summary|Story) ?==\s*$", w, re.M | re.I)
            if not m:
                out[src] = {"none": True}
                continue
            rest = w[m.end():]
            nxt = re.search(r"^==[^=].*?==\s*$", rest, re.M)
            body = _wikitext_plain(rest[:nxt.start()] if nxt else rest)
            out[src] = {"text": body, "section": m.group(1).title()} if body else {"none": True}
        time.sleep(3)
    return out


def moviespoiler_text(url):
    """The Movie Spoiler's write-up as plain text paragraphs ({coming: true} when it only has a teaser so far)."""
    h = fetch(url)
    if not h:
        return {"none": True}
    h = h.decode("utf-8", "replace")
    coming = "style-spoiler-coming" in h
    i = h.find('class="entry-content"')
    i = h.find(">", i) + 1 if i >= 0 else -1
    j = h.find('<div class="crp_related', i)
    seg = h[i:j if j > 0 else None] if i > 0 else ""
    seg = re.sub(r"<iframe.*?</iframe>", "", seg, flags=re.S)
    seg = re.sub(r"<h1.*?</h1>", "", seg, flags=re.S)
    seg = re.sub(r"<a [^>]*>\s*\*?CUT TO THE CHASE\*?\s*</a>", "", seg, flags=re.S | re.I)
    paras = []
    for blk in re.split(r"</p>|<br\s*/?>\s*<br\s*/?>", seg):
        t = htmllib.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>", "\n", blk)))
        t = re.sub(r"[ \t]+", " ", t)
        t = "\n".join(x.strip() for x in t.split("\n") if x.strip())
        t = re.sub(r"^(NOTE: This spoiler was submitted by \S+|Spoiler coming soon:?)\s*", "", t, flags=re.I).strip()
        if t:
            paras.append(t)
    text = "\n\n".join(paras).strip()
    if not text:
        return {"none": True}
    return {"text": text[:30000], "coming": coming}


def save_plots(films, cache, today):
    """Wikipedia plot sections and The Movie Spoiler write-ups, saved into films.json so the page can show them inline."""
    def stale(x):
        return not x or (today - date.fromisoformat(x.get("at", "2000-01-01"))).days >= PLOT_DAYS
    nw = ns = 0
    due = [f["wiki"] for k, f in films.items() if k in cache and f.get("wiki")
           and (stale(cache[k].get("wiki_plot")) or cache[k]["wiki_plot"].get("title") != f["wiki"])][:PLOT_MAX_WIKI]
    got = wikipedia_plots(due) if due else {}
    nw = len(got)
    for key, f in films.items():
        c = cache.get(key)
        if c is None:
            continue
        if f.get("wiki") in got:
            c["wiki_plot"] = {**got[f["wiki"]], "title": f["wiki"], "at": today.isoformat()}
        sp = c.get("spoiler_text") or {}
        age = (today - date.fromisoformat(sp.get("at", "2000-01-01"))).days
        due = (not sp or sp.get("url") != f.get("spoiler") or ((sp.get("coming") or not sp.get("text")) and age >= PLOT_DAYS) or age >= 30)
        if f.get("spoiler") and ns < PLOT_MAX_SPOILER and due:
            ns += 1
            try:
                c["spoiler_text"] = {**moviespoiler_text(f["spoiler"]), "url": f["spoiler"], "at": today.isoformat()}
            except Exception as e:
                print("movie spoiler text failed:", f["film"], e, file=sys.stderr)
            time.sleep(2)
        wp, sp = c.get("wiki_plot") or {}, c.get("spoiler_text") or {}
        if wp.get("text"):
            f["plot_wiki"] = {"text": wp["text"], "section": wp.get("section", "Plot"), "title": wp.get("title"), "at": wp.get("at")}
        if sp.get("text"):
            f["plot_spoiler"] = {"text": sp["text"], "coming": bool(sp.get("coming")), "at": sp.get("at")}
    print(f"plots: {nw} Wikipedia and {ns} Movie Spoiler fetches; {sum(1 for f in films.values() if f.get('plot_wiki'))} Wikipedia plots, "
          f"{sum(1 for f in films.values() if f.get('plot_spoiler'))} Movie Spoiler write-ups", file=sys.stderr)


CATALOG_DAYS = 3
JW_CATALOG = ("query C($after:String,$min:Int,$max:Int){popularTitles(country:US,first:100,after:$after,sortBy:POPULAR,"
              "filter:{objectTypes:[MOVIE],releaseYear:{min:$min,max:$max}}){pageInfo{endCursor hasNextPage} edges{node{id ... on Movie{"
              "content(country:US,language:\"en\"){title originalTitle originalReleaseYear shortDescription runtime "
              "externalIds{imdbId tmdbId} credits(role:DIRECTOR){name}}}}}}}")


def movie_catalog(path, today, spoiler_slugs):
    """A lookup list for the page's search: popular US movies on JustWatch (recent releases plus all-time
    favorites), with Wikipedia article titles from Wikidata. Cached in catalog.json, refreshed every few days."""
    old = json.load(open(path)) if os.path.exists(path) else {}
    if old.get("at") and (today - date.fromisoformat(old["at"])).days < CATALOG_DAYS and old.get("films"):
        return old["films"]
    out, seen = [], set()
    for lo, hi, pages in ((today.year - 1, today.year + 1, 30), (1900, today.year, 20)):
        after = None
        for _ in range(pages):
            try:
                d = jw_post(JW_CATALOG, {"after": after, "min": lo, "max": hi})
            except Exception as e:
                print("catalog page failed:", e, file=sys.stderr)
                break
            p = ((d.get("data") or {}).get("popularTitles") or {})
            for e in p.get("edges") or []:
                n = e.get("node") or {}
                c = n.get("content") or {}
                if not c.get("title") or n.get("id") in seen:
                    continue
                seen.add(n["id"])
                x = c.get("externalIds") or {}
                desc = (c.get("shortDescription") or "").strip()
                item = {"id": n["id"], "t": c["title"], "y": c.get("originalReleaseYear"), "r": c.get("runtime"),
                        "d": desc[:260] + ("…" if len(desc) > 260 else ""), "im": x.get("imdbId"), "tm": x.get("tmdbId"),
                        "dir": [k["name"] for k in (c.get("credits") or [])[:2]]}
                if c.get("originalTitle") and c["originalTitle"] != c["title"]:
                    item["ot"] = c["originalTitle"]
                out.append(item)
            pi = p.get("pageInfo") or {}
            if not pi.get("hasNextPage"):
                break
            after = pi.get("endCursor")
            time.sleep(0.3)
    if len(out) < 500 and old.get("films"):
        print(f"catalog fetch came back short ({len(out)}); keeping the saved one", file=sys.stderr)
        return old["films"]
    wiki = wikidata_enwiki(i["tm"] for i in out)
    for i in out:
        if wiki.get(str(i.get("tm"))):
            i["w"] = wiki[str(i["tm"])]
        for sl in (slugify(i["t"]), f"{slugify(i['t'])}-{i.get('y')}"):
            if sl in spoiler_slugs:
                i["ms"] = sl
    out = [{k: v for k, v in i.items() if v not in (None, "", [])} for i in out]
    json.dump({"at": today.isoformat(), "films": out}, open(path, "w"), separators=(",", ":"))
    print(f"catalog: {len(out)} movies for lookup, {sum(1 for i in out if i.get('w'))} with a Wikipedia article", file=sys.stderr)
    return out


JW_POPULAR_CINEMA = "query P($after:String){popularTitles(country:US,first:100,after:$after,sortBy:POPULAR,filter:{objectTypes:[MOVIE],monetizationTypes:[CINEMA]}){pageInfo{endCursor hasNextPage} edges{node{... on Movie{" + JW_FIELDS + "}}}}}"
JW_NEW_STREAMING = "query N($d:Date!){newTitles(country:US,date:$d,first:100,filter:{objectTypes:[MOVIE],monetizationTypes:[FLATRATE]}){edges{node{... on Movie{" + JW_FIELDS + "}}}}}"
NEW_MAX = {"theaters": 60, "soon": 20, "upcoming": 40, "streaming": 40, "classics": 24}
NEW_UPCOMING_DAYS = 56   # wide releases this far ahead, so they can go on the Planned list
NEW_CHART_MIN = 150000   # weekend gross needed to list an older release from the box office chart
NEW_STREAMING_DAYS = 21


SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of", "on", "or", "the", "to", "vs", "with"}


def smart_title(t):
    """JustWatch sometimes stores a title in all caps (THE FURTHER MIS-ADVENTURES OF CLIFF BOOTH); give it title case."""
    if not t or not t.isupper() or len(re.sub(r"[^A-Z]", "", t)) < 5:
        return t
    words = t.lower().split(" ")
    def cap(w, i):
        if i and w in SMALL_WORDS:
            return w
        return "-".join(p[:1].upper() + p[1:] for p in w.split("-"))
    return " ".join(cap(w, i) for i, w in enumerate(words))


def numbers_weekend(today):
    """The latest weekend box office chart from The Numbers (Box Office Mojo's weekend pages come back empty to scripts):
    {"wk": friday iso, "rows": {norm title: {rank, title, gross, th, total, days}}}."""
    fri = today - timedelta(days=(today.weekday() - 4) % 7)
    if (today - fri).days < 3:   # this weekend's numbers aren't in yet
        fri -= timedelta(days=7)
    for back in (0, 7):
        f = fri - timedelta(days=back)
        try:
            h = fetch(f"https://www.the-numbers.com/box-office-chart/weekend/{f.year}/{f.month:02d}/{f.day:02d}")
        except Exception as e:
            print("weekend chart failed:", f, e, file=sys.stderr)
            continue
        if not h:
            continue
        rows = {}
        num = lambda x: int(re.sub(r"[^0-9]", "", x) or 0)
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", h.decode("utf-8", "replace"), re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            a = re.search(r'<a href="/movie/[^"]+">(.*?)</a>', tr, re.S)
            if len(tds) < 9 or not a:
                continue
            title = _txt(a.group(1))
            r = {"rank": num(_txt(tds[0])), "title": title, "gross": num(_txt(tds[3])), "th": num(_txt(tds[5])),
                 "total": num(_txt(tds[7])), "days": num(_txt(tds[8]))}
            rows.setdefault(norm(title), r)
        if rows:
            print(f"weekend chart {f}: {len(rows)} films", file=sys.stderr)
            return {"wk": f.isoformat(), "rows": rows}
    return {"wk": None, "rows": {}}


def release_dates(entries):
    """First wide and first limited US theatrical dates (not re-releases) from The Numbers' calendar entries."""
    out = {}
    for d, tag in sorted(entries or []):
        t = tag.lower()
        if "re-release" in t:
            continue
        if "wide" in t and "wide" not in out:
            out["wide"] = d
        elif "wide" not in t and "limited" not in out:
            out["limited"] = d
    return out


def _new_item(n, section, today):
    c = n.get("content") or {}
    cr, sc, x = c.get("credits") or [], c.get("scoring") or {}, c.get("externalIds") or {}
    poster = ("https://images.justwatch.com" + c["posterUrl"].replace("{profile}", "s332").replace("{format}", "jpg")) if c.get("posterUrl") else None
    return {"id": n["id"], "sec": section, "t": smart_title(c.get("title")), "y": c.get("originalReleaseYear"), "rel": c.get("originalReleaseDate"),
            "r": c.get("runtime"), "mpa": c.get("ageCertification"), "d": c.get("shortDescription"),
            "dir": [k["name"] for k in cr if k.get("role") == "DIRECTOR"][:2], "cast": [k["name"] for k in cr if k.get("role") == "ACTOR"][:4],
            "rt": sc.get("tomatoMeter"), "rtf": bool(sc.get("certifiedFresh")), "imdb_score": sc.get("imdbScore"),
            "im": x.get("imdbId"), "tm": str(x.get("tmdbId") or "") or None, "genres": [g.get("shortName") for g in c.get("genres") or []],
            "offers": offers_summary(n.get("offers")), "poster_url": poster}


NEW_THEATER_DAYS, NEW_SOON_DAYS = 14, 7


def new_movies(today, contenders, spoiler_slugs, path, sched=None, chart=None):
    """The New movies section: films that opened in US theaters in the last two weeks and are still playing (dates from
    The Numbers' release calendar, showtimes from JustWatch), films opening in the next week, older films back in
    theaters, and movies that arrived on subscription streaming in the last three weeks. Each has poster, Rotten
    Tomatoes, cast, logline, where to watch, aspect ratio/language and the Wikipedia plot. Oscar contenders are
    included but marked, so the page opens their own film page. Wikipedia plots are cached in new_cache.json."""
    cache = json.load(open(path)) if os.path.exists(path) else {}
    con_by_jw = {str(f.get("jw")): k for k, f in contenders.items() if f.get("jw")}
    items, seen = [], set()
    # JustWatch's most popular movies with US showtimes (3 pages)
    pop, after = [], None
    for _ in range(3):
        try:
            d = jw_post(JW_POPULAR_CINEMA, {"after": after})
        except Exception as e:
            print("new: theaters page failed:", e, file=sys.stderr)
            break
        p = ((d.get("data") or {}).get("popularTitles") or {})
        pop += [e["node"] for e in p.get("edges") or [] if (e.get("node") or {}).get("id")]
        after = (p.get("pageInfo") or {}).get("endCursor")
        if not after or not (p.get("pageInfo") or {}).get("hasNextPage"):
            break
    pop_by_title = {}
    for n in pop:
        c = n.get("content") or {}
        for t in (c.get("title"), c.get("originalTitle")):
            if t:
                pop_by_title.setdefault(norm(t), n)
    # Opening dates from The Numbers: first US theatrical date (not a re-release) in the window
    lo, hi = (today - timedelta(days=NEW_THEATER_DAYS)).isoformat(), (today + timedelta(days=NEW_SOON_DAYS)).isoformat()
    hi_up = (today + timedelta(days=NEW_UPCOMING_DAYS)).isoformat()
    opened = {}
    for t, entries in (sched or {}).items():
        firsts = sorted((d, tag) for d, tag in entries if "re-release" not in tag.lower())
        if firsts and lo <= firsts[0][0] <= hi:
            opened[t] = firsts[0]
        elif firsts and hi < firsts[0][0] <= hi_up and release_dates(entries).get("wide") == firsts[0][0]:
            opened[t] = firsts[0]   # opens wide in the next eight weeks
    # Everything on last weekend's box office chart that's still earning (older releases included)
    chart_rows = (chart or {}).get("rows") or {}
    for t, r in chart_rows.items():
        if t not in opened and r["gross"] >= NEW_CHART_MIN:
            ds = release_dates((sched or {}).get(t))
            opened[t] = (ds.get("wide") or ds.get("limited") or "", "chart")
    searches = 0
    for t, (d, tag) in sorted(opened.items(), key=lambda x: x[1][0], reverse=True):
        n = pop_by_title.get(t)
        if not n and searches < 110:
            searches += 1
            try:
                n = next((x for x in jw_search(t) if norm((x.get("content") or {}).get("title") or "") == t
                          and ((x.get("content") or {}).get("originalReleaseYear") or 0) >= today.year - 1), None)
            except Exception as e:
                print("new: search failed:", t, e, file=sys.stderr)
            time.sleep(0.2)
        if not n or n["id"] in seen:
            continue
        it = _new_item(n, "theaters", today)
        if tag != "chart":
            it["opened"], it["release"] = d, tag
        elif d:
            it["opened"] = d
        if tag == "chart" and (it["y"] or today.year) <= today.year - 2:
            it["sec"] = "classics"   # an older film back on the chart (a re-release)
        elif d > hi:
            it["sec"] = "upcoming"
        elif d > today.isoformat():
            it["sec"] = "soon"
        elif not it["offers"]["cinema"] and t not in chart_rows:
            continue   # opened recently but no showtimes now
        seen.add(n["id"])
        items.append(it)
    # Older films with showtimes now
    weekend = weekend_theaters(today)
    for n in pop:
        c = n.get("content") or {}
        if n["id"] in seen or (c.get("originalReleaseYear") or today.year) > today.year - 3:
            continue
        it = _new_item(n, "classics", today)
        rr = rerelease_of(sched, c, today)
        if rr:
            it["back"], it["back_w"] = rr
        th = weekend.get(norm(c.get("title") or ""))
        if th:
            it["back_th"] = th
        if it["offers"]["cinema"]:
            seen.add(n["id"])
            items.append(it)
    # Box office (last weekend) and wide/limited dates for everything
    for it in items:
        keys = {norm(it["t"] or "")}
        r = next((chart_rows[k] for k in keys if k in chart_rows), None)
        if r:
            it["bo"] = {k: r[k] for k in ("rank", "gross", "th", "total", "days")}
            if it["sec"] == "classics" and not it.get("back_th"):
                it["back_th"] = [r["th"], chart.get("wk")]
        ds = next((release_dates(sched[k]) for k in keys if (sched or {}).get(k)), {})
        if ds.get("wide"):
            it["wide"] = ds["wide"]
        if ds.get("limited"):
            it["lim"] = ds["limited"]
    for back in range(1, NEW_STREAMING_DAYS + 1):
        day = (today - timedelta(days=back)).isoformat()
        try:
            edges = ((jw_post(JW_NEW_STREAMING, {"d": day}).get("data") or {}).get("newTitles") or {}).get("edges") or []
        except Exception as e:
            print("new: streaming failed:", day, e, file=sys.stderr)
            continue
        for e in edges:
            n = e.get("node") or {}
            if not n.get("id") or n["id"] in seen:
                continue
            it = _new_item(n, "streaming", today)
            # new movies only (this year or last), and only ones critics reviewed, to skip the long tail
            if (it["y"] or 0) < today.year - 1 or it["rt"] is None or not it["offers"]["sub"]:
                continue
            seen.add(n["id"])
            it["added"] = day
            ds = release_dates((sched or {}).get(norm(it["t"] or "")))
            if ds:   # had a US theatrical run (this year or last), so it's a theatrical film now streaming
                it["thr"] = ds.get("wide") or ds.get("limited")
            items.append(it)
        time.sleep(0.3)
    out = []
    for sec, cap in NEW_MAX.items():
        group = [i for i in items if i["sec"] == sec]
        for i in group:
            if i["id"] in con_by_jw:
                i["con"] = con_by_jw[i["id"]]
        if sec == "streaming":
            group.sort(key=lambda i: (i["added"], i["rt"] or 0), reverse=True)
        elif sec == "theaters":   # box office first, so the cap drops the smallest
            group.sort(key=lambda i: ((i.get("bo") or {}).get("gross") or 0, i.get("opened") or ""), reverse=True)
        elif sec == "upcoming":
            group.sort(key=lambda i: i.get("opened") or "")
        out += group[:cap]
    tids = [i["tm"] for i in out if i.get("tm")]
    wiki = wikidata_enwiki(tids)
    tech = wikidata_tech(tids)
    for i in out:
        i["w"] = wiki.get(str(i.get("tm")))
        t = tech.get(str(i.get("tm")))
        if t and (t["ar"] or t["lang"]):
            i["tech"] = t
        for sl in (slugify(i["t"] or ""), f"{slugify(i['t'] or '')}-{i.get('y')}"):
            if sl in spoiler_slugs:
                i["ms"] = sl
    due = [i["w"] for i in out if i.get("w") and (not cache.get(i["w"]) or (today - date.fromisoformat(cache[i["w"]]["at"])).days >= PLOT_DAYS)]
    for title, wp in (wikipedia_plots(due) if due else {}).items():
        cache[title] = {**wp, "at": today.isoformat()}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        posters = list(ex.map(poster_data_uri, [i.get("poster_url") for i in out]))
    for i, pd in zip(out, posters):
        i["poster"] = pd
        i.pop("poster_url", None)
        wp = cache.get(i.get("w") or "") or {}
        if wp.get("text"):
            i["plot_wiki"] = {"text": wp["text"], "section": wp.get("section", "Plot"), "title": i["w"], "at": wp.get("at")}
    keep = {i["w"] for i in out if i.get("w")}
    json.dump({k: v for k, v in cache.items() if k in keep}, open(path, "w"), indent=0, sort_keys=True)
    out = [{k: v for k, v in i.items() if v not in (None, "", [], {})} for i in out]
    print("new: " + ", ".join(f"{sum(1 for i in out if i['sec'] == s)} {s}" for s in NEW_MAX) + f"; {sum(1 for i in out if i.get('bo'))} with box office" +
          f"; {sum(1 for i in out if i.get('plot_wiki'))} with a Wikipedia plot", file=sys.stderr)
    return out


def tmdb_api_dates(tid, key):
    """US release dates by type via the TMDB API (only when TMDB_API_KEY is set)."""
    auth = {"Authorization": f"Bearer {key}"} if len(key) > 40 else {}
    url = f"{TMDB_API}/movie/{tid}/release_dates" + ("" if auth else f"?api_key={key}")
    d = fetch(url, headers={"Accept": "application/json", **auth}, as_json=True) or {}
    types = {1: "Premiere", 2: "Theatrical (limited)", 3: "Theatrical", 4: "Digital", 5: "Physical", 6: "TV"}
    us = {}
    for r in d.get("results", []):
        if r.get("iso_3166_1") != "US":
            continue
        for x in r.get("release_dates", []):
            t, dt = types.get(x.get("type")), (x.get("release_date") or "")[:10]
            if t and dt and (t not in us or dt < us[t]):
                us[t] = dt
    return us


def rerelease_of(sched, c, today):
    """Latest The Numbers re-release entry for an older film, within the last 6 months or the next month: (date, tag)."""
    lo, hi = (today - timedelta(days=183)).isoformat(), (today + timedelta(days=31)).isoformat()
    hits = []
    for t in {norm(c.get("title") or ""), norm(c.get("originalTitle") or "")} - {""}:
        hits += [(d, tag) for d, tag in (sched or {}).get(t, []) if "re-release" in tag.lower() and lo <= d <= hi]
    return max(hits) if hits else None


def weekend_theaters(today):
    """Theater counts from The Numbers' latest weekend chart: {norm title: [theaters, weekend date]} (how wide a film is playing)."""
    out = {}
    fri = today - timedelta(days=(today.weekday() - 4) % 7)
    for back in (0, 7):
        d = fri - timedelta(days=back)
        try:
            h = fetch(f"https://www.the-numbers.com/box-office-chart/weekend/{d.year}/{d.month:02d}/{d.day:02d}")
        except Exception as e:
            print("the-numbers weekend failed:", d, e, file=sys.stderr)
            continue
        if not h:
            continue
        h = h.decode("utf-8", "replace")
        for tr in re.findall(r"<tr[^>]*>.*?</tr>", h, re.S):
            a = re.search(r'<a href="/movie/[^"]+">(.*?)</a>', tr, re.S)
            cells = [_txt(x).replace(",", "").strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            gi = next((i for i, x in enumerate(cells) if x.startswith("$")), None)
            ths = [x for x in cells[gi + 1:] if x.isdigit()] if gi is not None else []
            if a and ths:   # theaters is the first bare number after the weekend gross
                out.setdefault(norm(_txt(a.group(1))), [int(ths[0]), d.isoformat()])
        if out:
            break
    return out


def numbers_schedule(years=(2025, 2026, 2027)):
    """US release calendar from The Numbers: {norm title: [(iso date, tag)]}, tag like Wide, Expands Wide, Limited."""
    out = {}
    for y in years:
        try:
            h = fetch(f"https://www.the-numbers.com/movies/release-schedule/{y}")
        except Exception as e:
            print("the-numbers failed:", y, e, file=sys.stderr)
            continue
        if not h:
            continue
        h = h.decode("utf-8", "replace")
        cur = None
        for tr in re.findall(r"<tr[^>]*>.*?</tr>", h, re.S):
            m = re.search(r'id="(\d{4}-\d\d-\d\d)"', tr)
            if m:
                cur = m.group(1)
            a = re.search(r'<a href="/movie/([^"]+)">(.*?)</a></b>\s*\(([^)]*)\)', tr, re.S)
            if not (a and cur):
                continue
            slug_title = re.sub(r"-\((\d{4})[^)]*\)$", "", urllib.parse.unquote(a.group(1))).replace("-", " ")
            tag = a.group(3).strip()
            for t in {norm(_txt(a.group(2))), norm(re.sub(r"\[[^]]*\]\s*", "", _txt(a.group(2)))), norm(slug_title)}:
                out.setdefault(t, []).append((cur, tag))
    return out


def us_dates_from_numbers(entries):
    us = {}
    for d, tag in sorted(entries):
        t = tag.lower()
        if "re-release" in t or d < ELIGIBLE_FROM:  # older dates are other films with the same title
            continue
        if "wide" in t and "Theatrical" not in us:
            us["Theatrical"] = d
        elif ("limited" in t or "special" in t or "exclusive" in t) and "Theatrical (limited)" not in us:
            us["Theatrical (limited)"] = d
    return us


def letterboxd_sync(user, wanted_slugs, cache):
    """Films `user` has logged on Letterboxd, for the slugs we care about: {slug: {"where": "theater"|"home"}}.
    Reads the public films page (most recent ~72), diary page and RSS. A film whose review/diary entry
    carries the tag "streaming" counts as watched at home; anything else as watched in a theater.
    `cache` (tool/letterboxd.json) keeps every hit, since only recent pages are readable."""
    base = f"https://letterboxd.com/{user}"
    seen = set()
    diary = {}  # slug -> (latest watched date, rating) from the RSS feed (about the last 50 diary entries)
    for path in ("/films/", "/diary/", "/rss/"):
        try:
            h = fetch(base + path)
        except Exception as e:
            print("letterboxd failed:", path, e, file=sys.stderr)
            continue
        if not h:
            continue
        h = h.decode("utf-8", "replace")
        if path == "/rss/":
            for it in re.findall(r"<item>(.*?)</item>", h, re.S):
                sl = re.search(rf"/{re.escape(user)}/film/([a-z0-9-]+)/", it)
                d = re.search(r"<letterboxd:watchedDate>([0-9-]+)<", it)
                rt = re.search(r"<letterboxd:memberRating>([0-9.]+)<", it)
                if sl and d and d.group(1) >= diary.get(sl.group(1), ("", None))[0]:
                    diary[sl.group(1)] = (d.group(1), float(rt.group(1)) if rt else None)
        seen.update(re.findall(r'data-item-slug="([a-z0-9-]+)"', h))
        seen.update(re.findall(rf"/{re.escape(user)}/film/([a-z0-9-]+)/", h))
    for slug in sorted(seen & set(wanted_slugs)):
        if slug in cache and cache[slug].get("where"):
            continue
        where = "theater"
        try:
            h = fetch(f"{base}/film/{slug}/")
            if h and re.search(rb'/tag/(streaming|plane)/', h):   # streamed or seen on a plane = not in a theater
                where = "home"
        except Exception as e:
            print("letterboxd entry failed:", slug, e, file=sys.stderr)
            continue
        cache[slug] = {"where": where, "synced": date.today().isoformat()}
        time.sleep(0.5)
    for slug, (d, rt) in diary.items():  # watch date and rating, for the Watched tab
        if slug in cache and d >= cache[slug].get("date", ""):
            cache[slug]["date"] = d
            if rt is not None:
                cache[slug]["rating"] = rt
    return cache


def letterboxd_diary(user, store, today):
    """Every diary entry in `user`'s RSS feed (the last ~50 entries) with its tags, for the Watched tab's year stats.
    Letterboxd blocks older diary pages, so entries are kept in `store` (letterboxd.json under "_diary", keyed by
    viewing id) and build up over time. Tags come from each entry's page; recent entries are re-read for a week,
    since tags often get added a day or two after logging."""
    try:
        h = fetch(f"https://letterboxd.com/{user}/rss/")
    except Exception as e:
        print("letterboxd rss failed:", e, file=sys.stderr)
        return store
    h = (h or b"").decode("utf-8", "replace")
    n = 0
    for it in re.findall(r"<item>(.*?)</item>", h, re.S):
        g = re.search(r"<guid[^>]*>letterboxd-(?:review|watch)-(\d+)</guid>", it)
        d = re.search(r"<letterboxd:watchedDate>([0-9-]+)<", it)
        link = re.search(r"<link>(https://letterboxd.com/[^<]+)</link>", it)
        if not (g and d and link):
            continue
        vid = g.group(1)
        def tag(name):
            m = re.search(rf"<letterboxd:{name}>(.*?)</letterboxd:{name}>", it, re.S)
            return htmllib.unescape(m.group(1).strip()) if m else None
        e = store.get(vid) or {}
        sl = re.search(r"/film/([a-z0-9-]+)/", link.group(1))
        tm = re.search(r"<tmdb:movieId>(\d+)<", it)
        e.update({"date": d.group(1), "title": tag("filmTitle"), "year": int(tag("filmYear") or 0) or None,
                  "rating": float(tag("memberRating")) if tag("memberRating") else None,
                  "rewatch": tag("rewatch") == "Yes", "slug": sl.group(1) if sl else None,
                  "tmdb": tm.group(1) if tm else None, "url": link.group(1)})
        img = re.search(r'<img src="(https://a\.ltrbxd\.com/[^"]+)"', it)
        if img:
            e["poster_url"] = img.group(1)
        recent = (today - date.fromisoformat(d.group(1))).days <= 7
        if "tags" not in e or (recent and e.get("tags_at") != today.isoformat()):
            try:
                p = fetch(link.group(1))
                p = (p or b"").decode("utf-8", "replace")
                blk = re.search(r'<ul class="tags">(.*?)</ul>', p, re.S)
                e["tags"] = [htmllib.unescape(t.strip()) for t in re.findall(r'<a href="/[^"]+/tag/[^"]+">([^<]+)</a>', blk.group(1))] if blk else []
                e["tags_at"] = today.isoformat()
                n += 1
            except Exception as ex:
                print("letterboxd entry tags failed:", link.group(1), ex, file=sys.stderr)
            time.sleep(1)
        store[vid] = e
    # Runtime from each film's Letterboxd page (read once per film), so the year stats can leave out shorts.
    rt = {e["slug"]: e["runtime"] for e in store.values() if e.get("slug") and e.get("runtime")}
    for e in store.values():
        sl = e.get("slug")
        if not sl or e.get("runtime"):
            continue
        if sl not in rt:
            try:
                p = (fetch(f"https://letterboxd.com/film/{sl}/") or b"").decode("utf-8", "replace")
                m = re.search(r'text-footer">\s*([\d,]+)&nbsp;min', p)
                rt[sl] = int(m.group(1).replace(",", "")) if m else None
            except Exception as ex:
                rt[sl] = None
                print("letterboxd runtime failed:", sl, ex, file=sys.stderr)
            time.sleep(1)
        if rt.get(sl):
            e["runtime"] = rt[sl]
    print(f"letterboxd diary: {len(store)} entries saved, {n} entry pages read this run", file=sys.stderr)
    return store


def diary_posters(diary, path):
    """Poster art for every diary entry (from the RSS feed's Letterboxd poster), shrunk to a data URI and cached by
    film slug in diary_posters.json (images in its JPEG sheets). Sets entry["poster"] and drops poster_url."""
    cache = load_json(path, {})
    todo = {e["slug"]: e["poster_url"] for e in diary if e.get("slug") and e.get("poster_url") and not cache.get(e["slug"])}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        for slug, uri in zip(todo, ex.map(poster_data_uri, todo.values())):
            if uri:
                cache[slug] = uri
    for e in diary:
        e.pop("poster_url", None)
        if cache.get(e.get("slug")):
            e["poster"] = cache[e["slug"]]
    if todo:
        save_json(cache, path)
    print(f"diary posters: {sum(1 for e in diary if e.get('poster'))} of {len(diary)} ({len(todo)} fetched)", file=sys.stderr)


def combine_diary(rss):
    """The RSS diary plus Ravi's spreadsheet (sheet_diary.json) and Letterboxd export (lb_export.json), merged by
    diary_merge.merge_diary. Writes diary_report.md (where the sources disagree). Returns (diary, theater tag map)."""
    from diary_merge import merge_diary, report_md
    rd = lambda n: json.load(open(os.path.join(HERE, n))) if os.path.exists(os.path.join(HERE, n)) else []
    films = json.load(open(os.path.join(HERE, "diary_films.json"))) if os.path.exists(os.path.join(HERE, "diary_films.json")) else {}
    diary, rep, tmap = merge_diary(rss, rd("sheet_diary.json"), rd("lb_export.json"), films)
    open(os.path.join(HERE, "diary_report.md"), "w").write(report_md(rep, tmap))
    print(f"diary: {len(diary)} entries ({rep['paired']} spreadsheet rows matched to Letterboxd)", file=sys.stderr)
    return diary, tmap


def small_poster(uri, width=150):
    """Re-encode a poster data URI at `width` px (about 7 KB), so hundreds of diary posters fit in the page."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=72, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
    except Exception:
        return uri


def attach_diary_films(diary, films_path, posters_path, poster_from=2024, extra_from=2021, extra_dir=None):
    """Film details for diary entries (diary_films.json, filled by diary_films.py from Letterboxd film pages).
    Sets entry["fid"] (the Letterboxd slug) and returns {slug: details} for the page. Posters are embedded only
    for films watched in `poster_from` or later, to keep the page small; films watched from `extra_from` until then get
    theirs in JPEG sheets (diary_posters_extra.sheet<N>.jpg, written to `extra_dir`, default next to posters_path)
    published next to the page; the page cuts them out after it opens. Their positions go in the returned
    dict under "_xposters" ({"sheets": [...], "at": {slug: [sheet, x, y, w, h]}}), which main() moves to DATA."""
    films = json.load(open(films_path)) if os.path.exists(films_path) else {}
    pcache = load_json(posters_path, {})
    out, want, extra = {}, {}, {}
    by_name = {}
    for f in films.values():
        if f and f.get("title"):
            by_name.setdefault((norm(f["title"]), f.get("year")), f)
    for e in diary:
        if e.get("slug"):
            f = films.get("slug:" + e["slug"])
        elif e.get("src") == "lbx":
            f = films.get(f"film:{norm(e['title'])}|{e.get('year')}") or by_name.get((norm(e["title"]), e.get("year")))
        else:
            # A spreadsheet row paired with a Letterboxd entry: that entry names the film and its year exactly
            lbf = (films.get(f"film:{norm(e['lb_title'])}|{e.get('lb_year')}") or by_name.get((norm(e["lb_title"]), e.get("lb_year")))) if e.get("lb_title") else None
            f = films.get(f"{norm(e['title'])}|{e['date'][:4]}")
            if lbf or (f and e.get("lb_year") and f.get("year") and abs(f["year"] - e["lb_year"]) > 1):
                f = lbf
        if not f:
            continue
        if e.get("src") == "lbx" and f.get("runtime") and not e.get("runtime"):
            e["runtime"] = f["runtime"]   # exact name + year match, so safe to use for the shorts rule
        sl = f["slug"]
        e["fid"] = sl
        if sl not in out:
            out[sl] = {k: f.get(k) for k in ("slug", "title", "year", "runtime", "directors", "cast", "genres", "language", "lb_avg", "tmdb") if f.get(k)}
            if f.get("about"):
                out[sl]["about"] = f["about"][:600]
        if int(e["date"][:4]) >= poster_from and not e.get("poster") and f.get("poster_url"):
            want[sl] = f["poster_url"]
        elif int(e["date"][:4]) >= extra_from and not e.get("poster") and f.get("poster_url"):
            extra[sl] = f["poster_url"]
    extra = {sl: u for sl, u in extra.items() if sl not in want}
    todo = {sl: u for sl, u in {**want, **extra}.items() if not pcache.get(sl)}
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            for sl, uri in zip(todo, ex.map(poster_data_uri, todo.values())):
                if uri:
                    pcache[sl] = small_poster(uri)
        save_json(pcache, posters_path)
    for sl in want:
        if pcache.get(sl):
            out[sl]["poster"] = pcache[sl]
    sheets, at = pack({sl: pcache[sl] for sl in sorted(extra) if pcache.get(sl)},
                      extra_dir or os.path.dirname(os.path.abspath(posters_path)), "diary_posters_extra", quality=82)
    from diary_merge import mark_shorts
    mark_shorts(diary, out)
    print(f"diary films: {sum(1 for e in diary if e.get('fid'))} of {len(diary)} entries matched, "
          f"{sum(1 for f in out.values() if f.get('poster'))} posters ({len(todo)} fetched), {len(at)} in sheets", file=sys.stderr)
    out["_xposters"] = {"sheets": sheets, "at": at}
    return out


# ---------------------------------------------------------------- past Oscars
PAST_YEARS = {2026: {"ordinal": "98th", "gd": "oscars-2026", "wiki": "98th Academy Awards", "films_year": 2025}}


WIKI_CAT = {"Best Director": "Best Directing", "Best Actor": "Best Actor in a Leading Role", "Best Actress": "Best Actress in a Leading Role",
            "Best Supporting Actor": "Best Actor in a Supporting Role", "Best Supporting Actress": "Best Actress in a Supporting Role",
            "Best Original Screenplay": "Best Writing (Original Screenplay)", "Best Adapted Screenplay": "Best Writing (Adapted Screenplay)",
            "Best Animated Feature": "Best Animated Feature Film", "Best International Film": "Best International Feature Film",
            "Best Documentary Feature": "Best Documentary Feature Film", "Best Documentary Short": "Best Documentary Short Film",
            "Best Live Action Short": "Best Live Action Short Film", "Best Animated Short": "Best Animated Short Film",
            "Best Score": "Best Music (Original Score)", "Best Song": "Best Music (Original Song)"}


def _gd_categories(url):
    """Gold Derby final predictions page -> [{label, rows:[{title, sub, pct}]}] (nominees only once nominations are out)."""
    page = fetch(url).decode("utf-8", "replace")
    cats = []
    for seg in page.split('data-component="predictions-award-item"')[1:]:
        label = re.search(r"<h3[^>]*>(.*?)</h3>", seg, re.S)
        if label:
            cats.append({"label": _txt(label.group(1)), "rows": parse_contestants(seg)})
    return cats


def _wiki_winners(title):
    """{category label: winning title or name} from the ceremony's Wikipedia article (first bolded entry per category)."""
    url = WIKI_API + "?" + urllib.parse.urlencode({"action": "parse", "page": title, "prop": "wikitext", "format": "json", "formatversion": 2, "redirects": 1})
    d = None
    for k in range(6):
        try:
            d = fetch(url, headers=WIKI_UA, tries=1, as_json=True)
            break
        except Exception as e:
            print("wikipedia winners: retrying after", e, file=sys.stderr)
            time.sleep(15 * (k + 1))
    w = ((d or {}).get("parse") or {}).get("wikitext") or ""
    out = {}
    for m in re.finditer(r"\{\{\s*Award category\s*\|[^|]*\|\s*\[\[([^\]|]+)(?:\|([^\]]+))?\]\]\s*\}\}(.*?)(?=\{\{\s*Award category|\n\|-|\n\|\}|$)", w, re.S | re.I):
        label = (m.group(2) or m.group(1)).strip()
        first = re.search(r"^\*\s*(.+)$", m.group(3), re.M)
        if first:
            line = first.group(1)
            bold = re.findall(r"'{3,}(.*?)'{3,}", line)
            out[label] = _wikitext_plain(bold[0] if bold else line)
    return out


def past_oscars(year, path, cache):
    """A past ceremony for the Oscars year switcher: every category's nominees with Gold Derby's final win odds and
    the winner, plus poster/details for each nominated film. Saved to past_<year>.json once complete (it never changes)."""
    if os.path.exists(path):
        old = load_json(path)
        if old.get("complete"):
            return old
    cfg = PAST_YEARS[year]
    cats = _gd_categories(f"https://www.goldderby.com/odds/combined-odds/{cfg['gd']}/")
    winners = _wiki_winners(cfg["wiki"])
    films = {}
    for c in cats:
        lab = c["label"]
        wk = WIKI_CAT.get(lab, lab)
        wl = next((v for k, v in winners.items() if norm(k) == norm(wk)), None)
        head = " " + norm(wl.split(" – ")[0]) + " " if wl else ""
        for r in c["rows"]:
            film = r["sub"] if r["sub"] and lab not in ("Best Picture", "Best International Film", "Best Animated Feature", "Best Documentary Feature",
                                                          "Best Animated Short", "Best Documentary Short", "Best Live Action Short") else r["title"]
            if lab == "Best Song":
                film = r["title"]
            r["film"] = film
            r["key"] = norm(film)
            r["winner"] = bool(head) and (" " + norm(r["title"]) + " ") in head
            films.setdefault(r["key"], {"film": film})
    def one(item):
        key, f = item
        ids = match_film(f["film"], cache, {}, None)
        if ids.get("jw") is None or not ids:
            try:
                fy = cfg["films_year"]
                near = [x for x in jw_search(f["film"]) if fy - 1 <= ((x.get("content") or {}).get("originalReleaseYear") or 0) <= fy + 1]
                n = next((x for x in near if norm((x.get("content") or {}).get("title") or "") == key), near[0] if near else None)
                if n:
                    ids = {"jw": n["id"]}
            except Exception as e:
                print("past film search failed:", f["film"], e, file=sys.stderr)
        info = film_info(f["film"], ids) if ids.get("jw") else {}
        d = info.get("details") or {}
        return key, {"film": f["film"], "tmdb": info.get("tmdb"), "poster": poster_data_uri(info.get("poster")),
                     "details": {k: d.get(k) for k in ("directors", "cast", "runtime", "rating", "logline", "year", "rt", "rt_fresh", "imdb_id")},
                     "offers": info.get("offers")}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        films = dict(ex.map(one, films.items()))
    tech = wikidata_tech(f.get("tmdb") for f in films.values())
    wiki = wikidata_enwiki(f.get("tmdb") for f in films.values())
    for f in films.values():
        t = tech.get(str(f.get("tmdb")))
        if t and (t["ar"] or t["lang"]):
            f["tech"] = t
        if wiki.get(str(f.get("tmdb"))):
            f["wiki"] = wiki[str(f["tmdb"])]
    n_win = sum(1 for c in cats if any(r["winner"] for r in c["rows"]))
    out = {"year": year, "ordinal": cfg["ordinal"], "films_year": cfg["films_year"], "categories": cats, "films": films,
           "complete": n_win >= len(cats) - 2 and len(cats) >= 20}
    save_json(out, path)
    print(f"past oscars {year}: {len(cats)} categories, {len(films)} films, winners found in {n_win}", file=sys.stderr)
    return out


def letterboxd_film(slug=None, tmdb=None):
    """Letterboxd's public film page: its slug, average rating and the top popular reviews (for the after-the-movie view)."""
    h = fetch(f"https://letterboxd.com/film/{slug}/" if slug else f"https://letterboxd.com/tmdb/{tmdb}/")
    if not h:
        return None
    h = h.decode("utf-8", "replace")
    m = re.search(r'<meta property="og:url" content="https://letterboxd.com/film/([a-z0-9-]+)/', h)
    out = {"slug": m.group(1) if m else slug, "at": date.today().isoformat(), "reviews": []}
    m = re.search(r'"ratingValue":\s*([0-9.]+)', h)
    if m:
        out["avg"] = round(float(m.group(1)), 1)
    i = h.find("js-popular-reviews")
    for art in re.findall(r'<article class="production-viewing.*?</article>', h[i:] if i >= 0 else "", re.S)[:6]:
        who = re.search(r'class="displayname">(.*?)</strong>', art, re.S)
        link = re.search(r'href="(/[^"/]+/film/[^"]+/)" class="context"', art)
        stars = re.search(r'class="glyph -rating"[^>]*aria-label="([^"]+)"', art)
        body = re.search(r'js-review-body[^>]*>(.*?)</div>', art, re.S)
        likes = re.search(r'data-count="(\d+)"', art)
        if not body:
            continue
        txt = htmllib.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"</p>\s*<p>", "\n", body.group(1))))
        txt = re.sub(r"[ \t]+", " ", txt).strip()
        if not txt:
            continue
        out["reviews"].append({"who": htmllib.unescape(who.group(1).strip()) if who else "", "stars": stars.group(1) if stars else "",
                               "text": txt[:420] + ("…" if len(txt) > 420 else ""),
                               "url": "https://letterboxd.com" + link.group(1) if link else None,
                               "likes": int(likes.group(1)) if likes else None,
                               "spoilers": "contains spoilers" in art})
    return out


def mediastinger(title, year):
    """MediaStinger's during/after-the-credits note for a film, or {"none": True} if it has no page yet."""
    today = date.today().isoformat()
    s = slugify(title)
    for path in ([f"{s}-{year}-after-the-credits", f"{s}-after-the-credits"] if year else [f"{s}-after-the-credits"]):
        url = f"https://mediastinger.com/{path}/"
        try:
            h = fetch(url, tries=2)
        except Exception as e:
            print("mediastinger failed:", title, e, file=sys.stderr)
            return None
        time.sleep(10)  # robots.txt asks for a 10 second crawl delay
        if not h:
            continue
        h = h.decode("utf-8", "replace")
        t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.S)
        t = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", t)))
        m = re.search(r"Credits Running Time:(.*?)(?:Special thanks|Stinger information added|User Stinger Score)", t)
        if not m:
            continue
        block = m.group(1)
        clean = lambda x: re.sub(r"\s+([.,!?])", r"\1", x).strip(" -") if x and x.strip(" -") else None
        rt = re.match(r"\s*([^A-Z]*?minutes?|N/A)", block)
        out = {"url": url, "at": today, "credits_len": clean(rt.group(1)) if rt else None}
        # Sections in page order: "During the Credits", "After the Credits", each maybe followed by a hidden "Spoiler"
        sec = None
        for head, body in re.findall(r"(During the Credits|After the Credits|Spoiler)(.*?)(?=During the Credits|After the Credits|Spoiler|$)", block):
            if head == "Spoiler":
                if sec and clean(body):
                    out[sec + "_spoiler"] = clean(body)
            else:
                sec = "during" if head.startswith("During") else "after"
                out[sec] = clean(body)
        return out
    return {"none": True, "at": today}


def offers_summary(offers):
    cinema, sub, rent = set(), set(), set()
    for o in offers or []:
        name = (o.get("package") or {}).get("clearName") or ""
        mt, pt = o.get("monetizationType"), o.get("presentationType")
        if mt == "CINEMA":
            cinema.add(name)
        elif pt in ("DVD", "BLURAY") or "DVD" in name:
            continue
        elif mt in ("FLATRATE", "FREE", "ADS"):
            sub.add(re.sub(r"\s+(Amazon Channel|Apple TV Channel|Roku Premium Channel)$", "", name))
        elif mt in ("RENT", "BUY"):
            rent.add(name)
    return {"cinema": sorted(cinema), "sub": sorted(sub), "rent": sorted(rent)}


def film_info(title, ids):
    info = {"tmdb": ids.get("tmdb"), "jw": ids.get("jw"), "us": {}, "poster": None, "genres": [],
            "premiere": None, "offers": {"cinema": [], "sub": [], "rent": []}, "upcoming": []}
    if ids.get("jw"):
        try:
            n = jw_node(ids["jw"])
        except Exception as e:
            n = None
            print("justwatch node failed:", title, e, file=sys.stderr)
        if n:
            c = n.get("content") or {}
            info["offers"] = offers_summary(n.get("offers"))
            info["premiere"] = c.get("originalReleaseDate")
            info["genres"] = [g.get("shortName") for g in c.get("genres") or []]
            info["tmdb"] = info["tmdb"] or str((c.get("externalIds") or {}).get("tmdbId") or "") or None
            if c.get("posterUrl"):
                info["poster"] = "https://images.justwatch.com" + c["posterUrl"].replace("{profile}", "s332").replace("{format}", "jpg")
            cr = c.get("credits") or []
            sc = c.get("scoring") or {}
            info["details"] = {
                "directors": [x["name"] for x in cr if x.get("role") == "DIRECTOR"][:3],
                "cast": [x["name"] for x in cr if x.get("role") == "ACTOR"][:6],
                "runtime": c.get("runtime"), "rating": c.get("ageCertification"),
                "logline": c.get("shortDescription"), "year": c.get("originalReleaseYear"),
                "rt": sc.get("tomatoMeter"), "rt_fresh": bool(sc.get("certifiedFresh")),
                "imdb_id": (c.get("externalIds") or {}).get("imdbId"),
                "jw_url": None,
            }
            info["upcoming"] = [{"date": u.get("releaseDate"), "type": u.get("releaseType"),
                                 "where": (u.get("package") or {}).get("clearName")} for u in c.get("upcomingReleases") or []]
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if key and info["tmdb"]:
        try:
            info["us"] = tmdb_api_dates(info["tmdb"], key)
        except Exception as e:
            print("tmdb api failed:", title, e, file=sys.stderr)
    return info


def availability(info, today, is_doc):
    us = info.get("us", {})
    limited, wide, digital = us.get("Theatrical (limited)"), us.get("Theatrical"), us.get("Digital")
    if not (limited or wide) and info.get("us_wikidata"):
        wide = info["us_wikidata"]
    t = today.isoformat()
    for u in info.get("upcoming") or []:
        if u.get("type") == "DIGITAL" and u.get("date") and not digital:
            digital = u["date"]
        if u.get("type") == "THEATRICAL" and u.get("date") and not wide:
            wide = u["date"]
    dates = [d for d in (limited, wide) if d]
    first = min(dates) if dates else None
    off = info.get("offers", {})
    weeks = (today - date.fromisoformat(first)).days // 7 + 1 if first and first <= t else None
    upcoming = [d for d in dates if d > t]
    at_home = bool(off.get("sub") or off.get("rent"))
    stale = at_home and weeks is not None and weeks > 10  # old showtime listings linger on JustWatch
    if wide and wide <= t:
        weeks = (today - date.fromisoformat(wide)).days // 7 + 1  # count weeks from the wide release
    if off.get("cinema") and (first is None or first <= t) and not stale:
        bucket = "theaters"
    elif first and first <= t and weeks <= 6 and not off.get("sub") and not off.get("rent") and not (digital and digital <= t):
        bucket = "theaters"  # recently opened, JustWatch has no showtimes listed
    elif upcoming or (off.get("cinema") and first and first > t):
        bucket = "soon"
    elif off.get("sub"):
        bucket = "streaming"
    elif off.get("rent") or (digital and digital <= t):
        bucket = "rent"
    elif first:
        bucket = "between"
    else:
        bucket = "undated"
    home = digital if digital and digital > t else None
    return {"bucket": bucket, "limited": limited, "wide": wide, "digital": digital, "first": first,
            "weeks": weeks, "next_theatrical": min(upcoming) if upcoming else None,
            "premiere": info.get("premiere"), "home_soon": home,
            "home_where": next((u["where"] for u in info.get("upcoming") or [] if u.get("type") == "DIGITAL" and u.get("date") == home), None),
            "doc": is_doc}


def _yt_results(q):
    h = fetch("https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": q}))
    if not h:
        return []
    h = h.decode("utf-8", "replace")
    out, seen = [], set()
    for vid, t in re.findall(r'"videoId":"([\w-]{11})".{0,600}?"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', h):
        if vid not in seen:
            seen.add(vid)
            out.append((vid, json.loads('"' + t + '"')))
    return out


def youtube_teaser(title, year=None):
    """First official teaser on YouTube; falls back to the first official trailer."""
    k = norm(title)
    junk = (r"\b(2|3|4|ii|iii|final|new|second|third|latest|reaction|reacts|review|breakdown|explained|fan|concept|ai|"
            r"extended|clip|featurette|recap|spot|behind|interview|music|soundtrack|ost|theme|song|saw|scene|leaked|parody)\b")
    cands = []
    for q in (f"{title} {year or ''} official teaser", f"{title} {year or ''} official trailer"):
        res = []
        for attempt in range(2):
            try:
                res = _yt_results(q.replace("  ", " "))
            except Exception:
                res = []
            if res:
                break
            time.sleep(1)
        cands += [(v, t) for v, t in res if k in norm(t) and not re.search(junk, norm(t)) and (v, t) not in cands]
    def tier(t):
        n = norm(t)
        off, teaser, trailer = "official" in n, "teaser" in n, "trailer" in n
        return 0 if off and teaser else 1 if off and trailer else 2 if teaser else 3 if trailer else 9
    cands = sorted(cands, key=lambda c: tier(c[1]))  # stable: keeps YouTube's order within a tier
    return {"id": cands[0][0], "title": cands[0][1]} if cands and tier(cands[0][1]) < 9 else None


def moviespoiler_slugs():
    """All movie slugs on themoviespoiler.com, from its sitemap."""
    slugs = set()
    for i in range(1, 30):
        try:
            x = fetch(f"https://themoviespoiler.com/wp-sitemap-posts-movies-{i}.xml")
        except Exception:
            break
        if not x:
            break
        found = re.findall(rb"<loc>https://themoviespoiler.com/movies/([^/<]+)/?</loc>", x)
        if not found:
            break
        slugs.update(f.decode() for f in found)
    return slugs


def slugify(t):
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def poster_data_uri(url):
    if not url:
        return None
    try:
        b = fetch(url, headers={"Accept": "image/*"})
        try:  # shrink to ~240px wide to keep the page light (optional: needs Pillow)
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(b)).convert("RGB")
            if im.width > 240:
                im = im.resize((240, round(im.height * 240 / im.width)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=78, optimize=True)
            b = buf.getvalue()
        except Exception:
            pass
        if b and len(b) < 200000:
            return "data:image/jpeg;base64," + base64.b64encode(b).decode()
    except Exception as e:
        print("poster failed:", url, e, file=sys.stderr)
    return None


# ---------------------------------------------------------------- build
GROUPS = ("experts", "editors", "users")


def combined_weights(raw):
    """Gold Derby only publishes a combined % for nominations. It is a weighted blend of the
    experts / editors / users shares, so fit those weights (least squares) and reuse them to
    blend the per-group win shares into a combined win %."""
    X, y = [], []
    for c in raw["categories"]:
        for r in c["rows"]:
            g = r["groups"]
            X.append([g.get(k) or 0.0 for k in GROUPS])
            y.append(r["pct"])
    try:
        A = [[sum(a[i] * a[j] for a in X) for j in range(3)] + [sum(a[i] * t for a, t in zip(X, y))] for i in range(3)]
        for i in range(3):
            piv = A[i][i]
            A[i] = [v / piv for v in A[i]]
            for j in range(3):
                if j != i:
                    f = A[j][i]
                    A[j] = [vj - f * vi for vj, vi in zip(A[j], A[i])]
        w = [max(0.0, A[i][3]) for i in range(3)]
        tot = sum(w)
        if tot <= 0:
            raise ValueError
        return [x / tot for x in w]
    except Exception:
        return [1 / 3] * 3


ACTING = ("Best Actress", "Best Actor", "Best Supporting Actress", "Best Supporting Actor")


def image_data_uri(url, width=240, aspect=None):
    """Download an image and shrink it (and optionally crop it to width:height = aspect, keeping the top,
    where faces are) into a JPEG data URI. Needs Pillow for the resize/crop; without it the image is used as is."""
    b = fetch(url, headers={"Accept": "image/*", "User-Agent": "oscars-tracker/1.0 (personal project)"})
    if not b:
        return None
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(b)).convert("RGB")
        if aspect:
            want_h = round(im.width / aspect)
            if im.height > want_h:
                top = round((im.height - want_h) * 0.15)
                im = im.crop((0, top, im.width, top + want_h))
            else:
                want_w = round(im.height * aspect)
                left = (im.width - want_w) // 2
                im = im.crop((left, 0, left + want_w, im.height))
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80, optimize=True)
        b = buf.getvalue()
    except Exception:
        pass
    return "data:image/jpeg;base64," + base64.b64encode(b).decode()


def person_photos(names, path):
    """Headshots for the acting categories, from each performer's English Wikipedia page image (Wikimedia
    Commons). Cached in photos.json by name ("" = looked up, nothing usable) so the daily refresh only looks
    up new names. Wikimedia rate-limits hard, so requests are paced and retried."""
    cache = load_json(path, {})
    todo = [n for n in names if n not in cache]
    for n in todo:
        src, errored = None, False
        for title in (n, n + " (actor)", n + " (actress)"):
            url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"))
            r = None
            for i in range(5):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "oscars-tracker/1.0 (personal project)"})
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        r = json.load(resp)
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        break
                    time.sleep(5 * (i + 1))
                except Exception:
                    time.sleep(3)
            else:
                errored = True  # rate-limited every time: don't remember this as "no photo"
            time.sleep(1.5)
            if not r or r.get("type") == "disambiguation":
                continue
            desc = (r.get("description") or "").lower()
            if r.get("thumbnail") and any(w in desc for w in ("actor", "actress", "singer", "comedian", "performer", "musician", "model", "rapper", "director", "television")):
                src = r["thumbnail"]["source"]
                break
        if not src and errored:
            continue
        try:
            cache[n] = image_data_uri(src, width=200, aspect=2 / 3) if src else ""
        except Exception as e:
            print("photo failed:", n, e, file=sys.stderr)
            continue  # try again next run
        time.sleep(1.5)
    if todo:
        save_json(cache, path, indent=0, sort_keys=True)
    found = sum(1 for n in names if cache.get(n))
    print(f"photos: {found} of {len(names)} performers have a headshot", file=sys.stderr)
    return {n: cache[n] for n in names if cache.get(n)}


AFTER_LB_DAYS, AFTER_STINGER_RETRY_DAYS = 5, 5   # refresh reviews / retry a missing MediaStinger page after this many days
AFTER_MAX_LB, AFTER_MAX_STINGER = 40, 14         # fetches per run (MediaStinger asks for 10 s between requests)


def after_the_movie(films, cache, today, display_titles):
    t = today.isoformat()
    def age(d):
        return (today - date.fromisoformat(d)).days if d else 9999
    released = []
    for key, f in films.items():
        av = f.get("avail") or {}
        out = av.get("bucket") in ("theaters", "streaming", "rent", "between") or (av.get("first") and av["first"] <= t)
        if out or f.get("letterboxd"):
            released.append(key)
    logged = sorted((k for k in released if films[k].get("letterboxd")), key=lambda k: films[k]["letterboxd"].get("date", ""), reverse=True)
    released = logged + [k for k in released if k not in logged]   # films Ravi logged first, newest first
    n_lb = n_st = 0
    for key in released:
        f = films[key]
        if key not in cache:
            continue
        a = cache[key].setdefault("after", {})
        lbk = (f.get("letterboxd") or {}).get("slug")
        if (n_lb < AFTER_MAX_LB) and age((a.get("lb") or {}).get("at")) >= AFTER_LB_DAYS:
            n_lb += 1
            try:
                got = letterboxd_film(slug=lbk or (a.get("lb") or {}).get("slug"), tmdb=f.get("tmdb"))
                if got:
                    a["lb"] = got
            except Exception as e:
                print("letterboxd film failed:", f["film"], e, file=sys.stderr)
            time.sleep(1)
        st = a.get("stinger")
        if n_st < AFTER_MAX_STINGER and (not st or (st.get("none") and age(st.get("at")) >= AFTER_STINGER_RETRY_DAYS)):
            n_st += 1
            yr = (f.get("details") or {}).get("year")
            got = None
            for title in dict.fromkeys([f["film"], f.get("gd_title") or f["film"]]):
                got = mediastinger(title, yr)
                if got and not got.get("none"):
                    break
            if got:
                a["stinger"] = got
        if a:
            f["after"] = a
    have = sum(1 for k in released if (cache.get(k) or {}).get("after", {}).get("stinger", {}).get("url"))
    print(f"after the movie: {len(released)} films out, {n_lb} Letterboxd and {n_st} MediaStinger fetches this run, "
          f"{have} with post-credits notes", file=sys.stderr)


def update_history(data, path):
    """Keep one snapshot of every category's nomination/win chances per day (Pacific date of the Gold Derby
    fetch; a later fetch the same day replaces it) in history.json, and return the slice the page needs:
    {"dates": [...], "s": {category: {film key: [[nom, win] or None per date]}}}."""
    hist = json.load(open(path)) if os.path.exists(path) else {"v": 1, "snapshots": {}}
    fetched = datetime.fromisoformat(data["updated"]).astimezone(timezone(timedelta(hours=-8)))
    day = fetched.date().isoformat()
    hist["snapshots"][day] = {c["label"]: {r["key"]: [round(r["pts"], 1), round(r.get("win") or 0, 1)] for r in c["rows"]}
                              for c in data["categories"]}
    json.dump(hist, open(path, "w"), separators=(",", ":"), sort_keys=True)
    dates = sorted(hist["snapshots"])
    out = {}
    for c in data["categories"]:
        out[c["label"]] = {r["key"]: [hist["snapshots"][d].get(c["label"], {}).get(r["key"]) for d in dates] for r in c["rows"]}
    print(f"history: {len(dates)} daily snapshots ({dates[0]} to {dates[-1]})", file=sys.stderr)
    return {"dates": dates, "s": out}


def build(raw, outdir, skip_films=False, reuse=None, public=False):
    cfg = json.load(open(os.path.join(HERE, "people.json")))
    aliases = {norm(k): v for k, v in cfg.get("film_aliases", {}).items() if not k.startswith("_")}
    display_titles = {norm(k): v for k, v in cfg.get("display_titles", {}).items() if not k.startswith("_")}
    overrides = {norm(k): v for k, v in cfg.get("tmdb_ids", {}).items() if not k.startswith("_")}
    jw_overrides = {norm(k): v for k, v in cfg.get("justwatch_ids", {}).items() if not k.startswith("_")}
    extra_docs = {norm(x) for x in cfg.get("documentaries", [])}
    keep_docs = [norm(canon_title) for canon_title in cfg.get("show_documentaries", [])]
    cache_path = os.path.join(HERE, "films.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    def canon(name):
        return aliases.get(norm(name), name.strip())

    bylabel = {c["label"]: c for c in raw["categories"]}
    weights = combined_weights(raw)
    print("combined weights (experts, editors, users):", [round(w, 3) for w in weights], file=sys.stderr)
    labels = [l for l in ORDER if l in bylabel] + [l for l in bylabel if l not in ORDER]
    out_cats, titles = [], {}
    for label in labels:
        rows = []
        for r in bylabel[label]["rows"]:
            if r["pct"] < FLOOR_PTS:
                continue
            if r.get("sub") and label != "Best Song":
                film, person, song = r["sub"], r["title"], None
            else:
                film, person, song = r["title"], None, r.get("sub")
            film = canon(film)
            key = norm(film)
            titles.setdefault(key, film)
            rows.append({"key": key, "person": person, "song": song, "pts": round(r["pct"], 2),
                         "odds": odds(r["pct"]), "experts": r["groups"].get("experts"), "experts_win": r["groups"].get("experts_win"),
                         "win": round(sum(w * (r["groups"].get(g + "_win") or 0) for w, g in zip(weights, GROUPS)), 2)})
        rows.sort(key=lambda x: -x["pts"])
        for i, x in enumerate(rows, 1):
            x["num"] = i
        out_cats.append({"label": label, "rows": rows})

    docs = {r["key"] for c in out_cats if c["label"] == "Best Documentary Feature" for r in c["rows"]} | extra_docs
    today = datetime.now(timezone(timedelta(hours=-8))).date()  # Pacific
    prev = json.load(open(reuse)) if reuse else {}

    def one(item):
        key, film = item
        if key in prev or skip_films:
            return None, None
        ids = match_film(film, cache, overrides, jw_overrides)
        info = film_info(film, ids)
        info["poster_data"] = poster_data_uri(info.get("poster"))
        c = cache.get(key) or {}
        if c.get("yt") and c.get("jw") == ids.get("jw"):
            info["teaser"] = c["yt"]
        else:
            try:
                info["teaser"] = youtube_teaser(display_titles.get(key, film), (info.get("details") or {}).get("year"))
            except Exception as e:
                print("youtube failed:", film, e, file=sys.stderr)
        return ids, info

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        fetched = dict(zip(titles, ex.map(one, titles.items())))
    wd = {} if skip_films else wikidata_us_dates(i.get("tmdb") for _, i in fetched.values() if i)
    try:
        spoiler = set() if skip_films else moviespoiler_slugs()
    except Exception as e:
        spoiler = set()
        print("movie spoiler sitemap failed:", e, file=sys.stderr)
    print(f"movie spoiler: {len(spoiler)} summaries", file=sys.stderr)
    sched = {} if skip_films else numbers_schedule()
    print(f"the numbers: {len(sched)} titles on the US release calendar", file=sys.stderr)
    films, unmatched = {}, []
    for key, film in titles.items():
        f = {"key": key, "film": display_titles.get(key, film), "gd_title": film}
        ids, info = fetched[key]
        if key in prev:
            f = {**prev[key], **f}
        elif info is not None:
            ids["tmdb"] = ids.get("tmdb") or info.get("tmdb")
            if ids.get("jw"):
                keep = {k: v for k, v in (cache.get(key) or {}).items() if k in ("after", "wiki", "wiki_plot", "spoiler_text", "tech")}
                cache[key] = {"title": film, **{k: v for k, v in ids.items() if v}, **keep}
                if info.get("teaser"):
                    cache[key]["yt"] = info["teaser"]
            else:
                unmatched.append(film)
            info["us_wikidata"] = wd.get(str(info.get("tmdb")))
            if not info.get("us"):  # TMDB API (if keyed) wins; else The Numbers' calendar
                for t in (display_titles.get(key, film), film):
                    if norm(t) in sched:
                        info["us"] = us_dates_from_numbers(sched[norm(t)])
                        break
            is_doc = key in docs or "doc" in (info.get("genres") or [])
            f.update({"tmdb": info.get("tmdb"), "jw": info.get("jw"), "genres": info.get("genres"),
                      "offers": info.get("offers"), "avail": availability(info, today, is_doc),
                      "poster": info.get("poster_data"), "details": info.get("details") or {},
                      "teaser": info.get("teaser")})
            yr = str((info.get("details") or {}).get("year") or "")
            for t in (display_titles.get(key, film), film):
                hit = next((sl for sl in (slugify(t), f"{slugify(t)}-{yr}") if sl in spoiler), None)
                if hit:
                    f["spoiler"] = f"https://themoviespoiler.com/movies/{hit}/"
                    break
        films[key] = f
        print(f"{film}: {f.get('avail', {}).get('bucket')} {f.get('avail', {}).get('first')}", file=sys.stderr)

    # Aspect ratio and original language (Wikidata), cached in films.json under "tech"
    if not skip_films:
        tech = wikidata_tech(f.get("tmdb") for f in films.values())
        for key, f in films.items():
            t = tech.get(str(f.get("tmdb")))
            if t and (t["ar"] or t["lang"]) and key in cache:
                cache[key]["tech"] = t
            t = (t if t and (t["ar"] or t["lang"]) else None) or (cache.get(key) or {}).get("tech")
            if t:
                f["tech"] = t
        print(f"wikidata: {sum(1 for f in films.values() if (f.get('tech') or {}).get('ar'))} films with an aspect ratio, "
              f"{sum(1 for f in films.values() if (f.get('tech') or {}).get('lang'))} with a language", file=sys.stderr)

    # Wikipedia article for each film (the film page links its Plot section). Cached in films.json under "wiki".
    if not skip_films:
        wiki = wikidata_enwiki(f.get("tmdb") for f in films.values())
        for key, f in films.items():
            t = wiki.get(str(f.get("tmdb")))
            if t and key in cache:
                cache[key]["wiki"] = t
            t = t or (cache.get(key) or {}).get("wiki")
            if t:
                f["wiki"] = t
        print(f"wikipedia: {sum(1 for f in films.values() if f.get('wiki'))} films with an article", file=sys.stderr)

    diary = []
    # Letterboxd: mark films already logged (manual statuses on the page still win)
    lb_user = None if public else cfg.get("letterboxd_user")   # --public: nothing of Ravi's is fetched
    if lb_user and not skip_films:
        lb_path = os.path.join(HERE, "letterboxd.json")
        lb = json.load(open(lb_path)) if os.path.exists(lb_path) else {}
        cand = {}
        for key, f in films.items():
            yr = str((f.get("details") or {}).get("year") or "")
            for t in {f["film"], f.get("gd_title") or f["film"]}:
                for sl in {slugify(t), f"{slugify(t)}-{yr}" if yr else slugify(t), f"{slugify(t)}-2026", f"{slugify(t)}-2025"}:
                    cand.setdefault(sl, key)
        for k, sl in cfg.get("letterboxd_slugs", {}).items():
            if not k.startswith("_"):
                cand[sl] = norm(k)
        lb = letterboxd_sync(lb_user, cand, lb)
        lb["_diary"] = letterboxd_diary(lb_user, lb.get("_diary") or {}, today)
        diary = sorted(({k: v for k, v in e.items() if k in ("date", "title", "year", "rating", "rewatch", "slug", "tmdb", "tags", "poster_url", "runtime")}
                        for e in lb["_diary"].values()), key=lambda e: e["date"], reverse=True)
        diary_posters(diary, os.path.join(HERE, "diary_posters.json"))
        json.dump(lb, open(lb_path, "w"), indent=1, sort_keys=True)
        for sl, v in lb.items():
            if sl.startswith("_"):
                continue
            if sl in cand and cand[sl] in films:
                films[cand[sl]]["letterboxd"] = {"slug": sl, "where": v.get("where", "theater"),
                                                 **{k: v[k] for k in ("date", "rating") if v.get(k) is not None}}
        print(f"letterboxd: {sum(1 for f in films.values() if f.get('letterboxd'))} contenders logged by {lb_user}", file=sys.stderr)

    # Ravi's own spreadsheet log (movies.xlsx -> sheet_diary.json via import_sheet.py): theater trips from before
    # Letterboxd. Merged into the Diary; an entry already in the Letterboxd diary (same day, same title) is skipped.
    diary, lb_theaters = ([], {}) if public else combine_diary(diary)
    os.makedirs(outdir, exist_ok=True)
    dfilms = {"_xposters": {"sheets": [], "at": {}}} if public else attach_diary_films(diary, os.path.join(HERE, "diary_films.json"), os.path.join(HERE, "diary_posters.json"), extra_dir=outdir)
    xposters = dfilms.pop("_xposters")

    # After the movie (Watched tab): Letterboxd popular reviews and MediaStinger post-credits notes, for films
    # already out. Cached in films.json under "after"; a few per run, films Ravi logged first.
    if not skip_films:
        after_the_movie(films, cache, today, display_titles)
        save_plots(films, cache, today)
    past = {}
    for y in PAST_YEARS:
        try:
            p = past_oscars(y, os.path.join(HERE, f"past_{y}.json"), {}) if not skip_films else None
            if p:
                past[str(y)] = p
        except Exception as e:
            print("past oscars failed:", y, e, file=sys.stderr)
    chart = {}
    try:
        chart = {} if skip_films or public else numbers_weekend(today)
        new = [] if skip_films or public else new_movies(today, films, spoiler, os.path.join(HERE, "new_cache.json"), sched, chart)
    except Exception as e:
        new = []
        print("new movies failed:", e, file=sys.stderr)

    for title, url in cfg.get("reddit_threads", {}).items():   # r/movies official discussion threads, title -> URL
        k = norm(aliases.get(norm(title), title))
        if not title.startswith("_") and k in films:
            films[k]["reddit"] = url
    for title, url in cfg.get("poster_urls", {}).items():
        if title.startswith("_"):
            continue
        k = norm(aliases.get(norm(title), title))
        if k in films:
            try:
                films[k]["poster"] = image_data_uri(url) or films[k].get("poster")
            except Exception as e:
                print("poster override failed:", title, e, file=sys.stderr)
    performers = sorted({p.strip() for c in out_cats if c["label"] in ACTING for r in c["rows"] if r.get("person")
                         for p in [r["person"]]})
    photos = {} if skip_films else person_photos(performers, os.path.join(HERE, "photos.json"))
    data = {"updated": raw["fetched_at"], "built": datetime.now(timezone.utc).isoformat(), "today": today.isoformat(),
            "source": raw.get("source"), "default_cutoff": DEFAULT_CUTOFF, "keep_docs": keep_docs, "photos": photos, "new": new, "boxoffice_wk": (chart or {}).get("wk"), "past": past, "diary": diary, "dfilms": dfilms, "xposters": xposters, "theater_names": {**lb_theaters, **{k: v for k, v in cfg.get("letterboxd_theaters", {}).items() if not k.startswith("_")}}, "win_weights": weights, "categories": out_cats, "films": films}
    data["hist"] = update_history(data, os.path.join(HERE, "history.json"))
    os.makedirs(outdir, exist_ok=True)
    json.dump({**data, "films": {k: {**v, "poster": bool(v.get("poster"))} for k, v in films.items()}},
              open(os.path.join(outdir, "data.json"), "w"), indent=1)
    json.dump(films, open(os.path.join(outdir, "films_full.json"), "w"))
    if not skip_films:
        json.dump(cache, open(cache_path, "w"), indent=1, sort_keys=True)
    with open(os.path.join(outdir, "unmatched.txt"), "w") as fh:
        fh.write("\n".join(unmatched))
    tpl = open(os.path.join(HERE, "template.html")).read()
    # No images inside the page (public sharing only takes real image files): they go to img.sheet<N>.jpg next to it
    data = externalize(data, outdir, "img", quality=80, subsampling=2)
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    open(os.path.join(outdir, "index.html"), "w").write(tpl.replace("/*__DATA__*/null", blob))
    viable = {r["key"] for c in out_cats for r in c["rows"] if r["pts"] > 100 / (DEFAULT_CUTOFF + 1)}
    print(f"{len(films)} films in data, {len(viable)} at the default cutoff, {len(out_cats)} categories, "
          f"{len(unmatched)} unmatched", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--raw")
    ap.add_argument("--skip-films", action="store_true", help="odds only (no TMDB/JustWatch)")
    ap.add_argument("--public", action="store_true", help="Oscars-only public build (GitHub Actions): no Letterboxd, diary or New movies")
    ap.add_argument("--reuse-films", help="films_full.json from an earlier build: reuse its film info instead of refetching")
    a = ap.parse_args()
    if a.raw:
        raw = json.load(open(a.raw))
    else:
        raw = fetch_goldderby()
        if not raw["categories"]:
            sys.exit("Gold Derby page had no categories; layout may have changed")
        os.makedirs(a.out, exist_ok=True)
        json.dump(raw, open(os.path.join(a.out, "raw.json"), "w"))
    build(raw, a.out, a.skip_films, a.reuse_films, a.public)
