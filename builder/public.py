"""Public Oscars-only site (GitHub Pages: rkudeshi/oscars-watch -> oscars.raviudeshi.com).

Takes a finished private build (out/index.html + out/img.sheet*.jpg from build.py) and writes a public copy:
only the Oscars section (Where to watch, Odds, Ranked, Moves, past Oscars years). Left out: New movies, Watchlist,
Diary, Letterboxd marks and Ravi's statuses/plans. Visitors can mark films watched; that's saved in their own browser (localStorage). Images are re-packed so the public sheets hold
only images the public page uses (no diary posters).

Usage: python3 public.py --src out --dst site     (site/ then gets index.html, img.sheet*.jpg, CNAME, .nojekyll)
"""
import argparse, glob, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from imgsheets import internalize, externalize

DOMAIN = "oscars.raviudeshi.com"
KEEP = ["updated", "built", "today", "source", "default_cutoff", "keep_docs", "photos", "past", "win_weights",
        "categories", "films", "hist"]
FILM_DROP = {"letterboxd"}

CSS = """<style id="public-css">
nav.sections,#hideplanned,button.plan,.lb,.wl-badge{display:none!important}
.pub-foot{margin:28px 0 8px;font-size:13px;color:var(--muted,#888)}
</style>
"""

def replace_once(h, old, new, label):
    n = h.count(old)
    if n != 1:
        raise SystemExit(f"public.py: expected one '{label}' in the page, found {n}; the template changed")
    return h.replace(old, new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="private build output folder (index.html + img.sheet*.jpg)")
    ap.add_argument("--dst", required=True)
    a = ap.parse_args()
    h = open(os.path.join(a.src, "index.html")).read()
    head = "const DATA = "
    i = h.index(head) + len(head)
    data, j = json.JSONDecoder().raw_decode(h, i)
    data = internalize(data, a.src)
    pub = {k: data[k] for k in KEEP if k in data}
    pub["films"] = {k: {kk: vv for kk, vv in f.items() if kk not in FILM_DROP} for k, f in data["films"].items()}
    pub["public"] = True
    os.makedirs(a.dst, exist_ok=True)
    for old in glob.glob(os.path.join(a.dst, "img.sheet*.jpg")):
        os.remove(old)
    pub = externalize(pub, a.dst, "img", quality=80, subsampling=2)
    blob = json.dumps(pub, separators=(",", ":")).replace("</", "<\\/")
    h = h[:i] + blob + h[j:]

    # Page edits: Oscars only, nothing personal
    h = replace_once(h, "<title>Ravi's Movie Tracker</title>",
                     '<!doctype html>\n<html lang="en">\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>Oscars Watch</title>\n<meta name="description" content="Oscars Death Race 2027: every film with a real shot at a nomination, '
                     'its odds and where to watch it in the US.">\n'
                     '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Ctext y=%22.9em%22 font-size=%2290%22%3E%F0%9F%8F%86%3C/text%3E%3C/svg%3E">\n'
                     + CSS, "title")
    h = replace_once(h, '<section class="progress" id="progress" aria-label="Ravi\'s progress">', '<section class="progress" id="progress" aria-label="Your progress">', "progress")
    h = replace_once(h, '<span class="who-l">Ravi\'s progress</span>', '<span class="who-l">Your progress</span>', "progress label")
    h = replace_once(h, "const isPlanned = key => isPlannedNew({con:key});", "const isPlanned = key => false;", "isPlanned")
    # Visitors' own watch marks, saved in their browser (localStorage) instead of Ravi's db
    h = replace_once(h, "const statusOf = key => statuses[key] || lbStatus(key) || \"Not watched\";",
                     "const statusOf = key => statuses[key] || \"Not watched\";\n"
                     "const LS_KEY = \"oscarswatch-status-2027\";\n"
                     "try { const o = JSON.parse(localStorage.getItem(LS_KEY) || \"{}\"); statuses = o.statuses || {}; watchedAt = o.watchedAt || {}; } catch(e){}", "statusOf")
    h = replace_once(h, 'aria-label="Status for ${esc(f.film)}"${db?"":" disabled"}>', 'aria-label="Status for ${esc(f.film)}">', "status select")
    h = replace_once(h, "const s = e.target.closest && e.target.closest(\"select.status\"); if (!s || !db) return;",
                     "const s = e.target.closest && e.target.closest(\"select.status\"); if (!s) return;", "status change")
    h = replace_once(h, 'try { await db.doc("status/" + key.replace(/[^a-z0-9]+/g,"-")).set(doc); }\n  catch(err){ $("#wnote").textContent = "Couldn\'t save that change. Only the list\'s owner can edit statuses."; }',
                     'Object.keys(statuses).forEach(k => { if (statuses[k]==="Not watched") delete statuses[k]; });\n'
                     '  try { localStorage.setItem(LS_KEY, JSON.stringify({statuses, watchedAt})); }\n'
                     '  catch(err){ $("#wnote").textContent = "Couldn\'t save that change in this browser."; }', "persist")
    h = replace_once(h, '$("#wnote").textContent = !db ? "Watch status can\'t be saved in this view." : `films watched${ui.hideDocs?" (not counting docs)":""}, ${inT} in a theater. ${pool.filter(f => isPlanned(f.key) && !statusOf(f.key).startsWith("Watched")).length} more on your watchlist.`;',
                     '$("#wnote").textContent = `films watched${ui.hideDocs?" (not counting docs)":""}, ${inT} in a theater. Set a film\'s status to track it; it\'s saved in this browser only.`;', "wnote")
    h = replace_once(h, 'if (!["theater","home"].includes(ui.optimize)) ui.optimize = "theater";\ncompute(); render();',
                     'if (!["theater","home"].includes(ui.optimize)) ui.optimize = "theater";\n'
                     'ui.hidePlanned = false; if (["seen","find","wl"].includes(ui.view)) ui.view = "watch";\n'
                     'compute(); render();', "init")
    # How-this-works: drop the paragraphs about the private sections and personal tracking
    for start in ("<div>The site has three sections:", "<div><b>Diary</b> lists"):
        k = h.index(start)
        h = h[:k] + h[h.index("</div>", k) + len("</div>"):]
    for s in (" and counts as planned unless you un-plan it", "; seen films are faded and planned films are dimmed with a gray outline",
              " Seen films in Best Picture's top 10 sit under <b>Seen, back at the showcase</b>. The second progress count leaves out films that aren't out yet."):
        h = h.replace(s, "")
    h = replace_once(h, "\n  </section>\n</div>\n\n<script>",
                     '\n  </section>\n  <p class="pub-foot">Updated daily from Gold Derby, JustWatch and The Numbers.</p>\n</div>\n<script>', "footer")
    open(os.path.join(a.dst, "index.html"), "w").write(h)
    open(os.path.join(a.dst, "CNAME"), "w").write(DOMAIN + "\n")
    open(os.path.join(a.dst, ".nojekyll"), "w").write("")
    leftovers = [w for w in ("diary", "dfilms", "xposters", "theater_names", "letterboxd") if f'"{w}"' in blob]
    if leftovers:
        raise SystemExit(f"public.py: private keys still in DATA: {leftovers}")
    print(f"public site: {len(pub['films'])} films, {len(glob.glob(os.path.join(a.dst, 'img.sheet*.jpg')))} image sheets, "
          f"index.html {os.path.getsize(os.path.join(a.dst, 'index.html'))//1024} KB", file=sys.stderr)


if __name__ == "__main__":
    main()
