"""Image caches as JPEG sprite sheets instead of base64 inside JSON.

A shared Artifact only takes images as real image files (JPEG, PNG, GIF, WebP); a JSON file full of base64
pictures can't be reviewed for public sharing. So the build's image caches (diary_posters.json, photos.json,
past_<year>.json) keep their JSON shape, but every "data:image/..." string in them is saved into a few JPEG sheets
next to the file (<stem>.sheet0.jpg, <stem>.sheet1.jpg, ...) and replaced by a reference "imgsheet:<file>#x,y,w,h".
load_json() turns the references back into data URIs, so the rest of build.py sees the same data as before.

The page itself embeds no images either: build.py externalizes every image in DATA into img.sheet<N>.jpg next to
index.html, and the page cuts them out with a canvas when the sheets load (2021-2023 diary posters likewise, via
pack() into diary_posters_extra.sheet<N>.jpg).

Usage: load_json(path, default) / save_json(obj, path, **json_kwargs)
       python3 imgsheets.py FILE.json ...   converts old base64 caches in place"""
import base64, glob, io, json, os, re, sys

SHEET_W, SHEET_H, GRID = 2048, 4096, 16
QUALITY = 90          # the sheets (stored cache)
URI_QUALITY = 80      # data URIs handed back to the build (and embedded in the page)
_src = {}             # data URI -> the PIL image it was cut from, so re-saving doesn't re-decode a lossy copy


def _img(uri):
    from PIL import Image
    if uri in _src:
        return _src[uri]
    return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("RGB")


def _uri(im, q=URI_QUALITY):
    out = io.BytesIO()
    im.save(out, "JPEG", quality=q, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def pack(uris, folder, stem, quality=QUALITY, subsampling=0):
    """Saves each data URI of `uris` ({key: uri}) into JPEG sheets folder/<stem>.sheet<N>.jpg.
    Returns (sheet file names, {key: [sheet index, x, y, w, h]}). Removes leftover sheets of the same stem."""
    from PIL import Image
    up = lambda n: -(-n // GRID) * GRID
    sheets, boxes, cur, x, y, row = [], {}, [], 0, 0, 0
    done = {}
    for key, uri in uris.items():
        if uri in done:
            boxes[key] = done[uri]
            continue
        try:
            im = _img(uri)
        except Exception:
            continue
        w, h = im.size
        if x + w > SHEET_W:
            x, y, row = 0, y + row, 0
        if y + h > SHEET_H:
            sheets.append(cur)
            cur, x, y, row = [], 0, 0, 0
        cur.append((im, x, y))
        boxes[key] = done[uri] = [len(sheets), x, y, w, h]
        x, row = x + up(w), max(row, up(h))
    if cur:
        sheets.append(cur)
    names = []
    for i, items in enumerate(sheets):
        H = max(y + im.size[1] for im, _, y in items)
        sh = Image.new("RGB", (SHEET_W, up(H)), (24, 24, 24))
        for im, x, y in items:
            sh.paste(im, (x, y))
        name = f"{stem}.sheet{i}.jpg"
        sh.save(os.path.join(folder, name), "JPEG", quality=quality, subsampling=subsampling, optimize=True)
        names.append(name)
    for old in glob.glob(os.path.join(glob.escape(folder), glob.escape(stem) + ".sheet*.jpg")):
        if os.path.basename(old) not in names:
            os.remove(old)
    return names, boxes


def save_json(obj, path, **kw):
    """json.dump(obj) to path, with every data:image string moved into JPEG sheets next to it."""
    folder, stem = os.path.dirname(os.path.abspath(path)), os.path.splitext(os.path.basename(path))[0]
    json.dump(externalize(obj, folder, stem), open(path, "w"), **kw)


def externalize(obj, folder, stem, quality=QUALITY, subsampling=0):
    """A copy of obj with every data:image string saved into folder/<stem>.sheet<N>.jpg and replaced by an
    "imgsheet:<file>#x,y,w,h" reference."""
    found = {}

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and o.startswith("data:image/"):
            found.setdefault(o, len(found))
    walk(obj)
    names, boxes = pack({i: u for u, i in found.items()}, folder, stem, quality, subsampling)
    ref = {u: f"imgsheet:{names[b[0]]}#{','.join(map(str, b[1:]))}" for u, i in found.items() if (b := boxes.get(i))}

    def sub(o):
        if isinstance(o, dict):
            return {k: sub(v) for k, v in o.items()}
        if isinstance(o, list):
            return [sub(v) for v in o]
        if isinstance(o, str) and o.startswith("data:image/"):
            return ref.get(o)
        return o
    return sub(obj)


def load_json(path, default=None):
    """json.load(path), with imgsheet references turned back into data URIs. `default` if the file is missing."""
    if not os.path.exists(path):
        return default
    return internalize(json.load(open(path)), os.path.dirname(os.path.abspath(path)))


def internalize(obj, folder):
    """The reverse of externalize: imgsheet references (sheets in `folder`) back to data URIs."""
    from PIL import Image
    cache = {}

    def get(ref):
        m = re.fullmatch(r"imgsheet:([^#/\\]+)#(\d+),(\d+),(\d+),(\d+)", ref)
        if not m:
            return None
        if m.group(1) not in cache:
            p = os.path.join(folder, m.group(1))
            cache[m.group(1)] = Image.open(p).convert("RGB") if os.path.exists(p) else None
        sh = cache[m.group(1)]
        if sh is None:
            return None
        x, y, w, h = map(int, m.groups()[1:])
        im = sh.crop((x, y, x + w, y + h))
        uri = _uri(im)
        _src[uri] = im
        return uri

    def sub(o):
        if isinstance(o, dict):
            return {k: sub(v) for k, v in o.items()}
        if isinstance(o, list):
            return [sub(v) for v in o]
        if isinstance(o, str) and o.startswith("imgsheet:"):
            return get(o)
        return o
    return sub(obj)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        save_json(load_json(p), p, separators=(",", ":"))
        print(p, "->", sorted(os.path.basename(f) for f in glob.glob(os.path.splitext(p)[0] + ".sheet*.jpg")), file=sys.stderr)
