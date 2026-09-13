"""The story, as something a person can read.

A player finishes forty turns and the only way to get any of it out was
"Export everything" - a JSON dump of every layer, every memory and every
scalar. That is the right thing to offer and nobody has ever read one. What
they want is the thing they just made: the prose, in order, with the speech
attributed and their own turns marked, on a page they can keep, print or hand
to somebody.

So this renders the narrative feed as ONE self-contained HTML file:

  * no network. Fonts fall back to whatever the reader has, images are the
    player's own uploads inlined as data URIs, and nothing phones home - the
    same promise sharecard.py makes, for the same reason.
  * the same feed grammar as the app, reduced to what survives on paper: a
    speaker plate becomes a name and a line, hearsay keeps its dashed rule,
    a refusal stays in the world's voice, and the player's own turns stay
    indented against a rule so you can see your own hand in it.
  * day and chapter headings, because forty turns without one is a wall.

It is deliberately NOT the export. The export is the state; this is the story.
"""
from __future__ import annotations

import base64
import html
import re
from pathlib import Path

from . import chapters, config, db, engine

# What the reader is allowed to see. `you`/`opening`/`narration`/`speech` are
# the story; the rest are the beats that happened IN the story. Anything the
# feed uses for chrome (thinking, queued, errors) never reaches the page.
READABLE = {"opening", "you", "narration", "speech", "refusal", "fate",
            "contest", "reveal", "whisper", "combat", "safety"}

_MEDIA = re.compile(r"^/media/[0-9a-f]{32}\.(png|jpg|gif|webp)$")
_MIME = {"png": "image/png", "jpg": "image/jpeg",
         "gif": "image/gif", "webp": "image/webp"}


def _portrait_data_uri(path: str) -> str:
    """Inline the player's own art. A transcript that linked back to this
    server would stop working the moment it was moved, mailed or archived -
    which is most of what a keepsake is for."""
    if not _MEDIA.match(path or ""):
        return ""
    name = path.rsplit("/", 1)[-1]
    file = Path(config.DATA_DIR) / "media" / name
    try:
        raw = file.read_bytes()
    except OSError:
        return ""
    if len(raw) > 2 * 1024 * 1024:      # keep the page openable
        return ""
    ext = name.rsplit(".", 1)[-1].lower()
    return f"data:{_MIME.get(ext, 'image/png')};base64,{base64.b64encode(raw).decode()}"


def _e(text) -> str:
    return html.escape(str(text or ""))


def _paras(text: str) -> str:
    bits = [b.strip() for b in re.split(r"\n\s*\n", str(text or "").strip()) if b.strip()]
    return "".join(f"<p>{_e(b)}</p>" for b in bits) or ""


def entries(pt_id: str) -> list[dict]:
    rows = db.rows(
        "SELECT turn,kind,actor,text,meta FROM narrative WHERE playthrough_id=?"
        " ORDER BY id", (pt_id,))
    out = []
    for r in rows:
        if r["kind"] not in READABLE:
            continue
        meta = db.jload(r["meta"], {}) or {}
        out.append({"turn": r["turn"], "kind": r["kind"], "actor": r["actor"],
                    "text": r["text"] or "", "meta": meta})
    return out


def _block(e: dict, faces: dict) -> str:
    kind, text, meta = e["kind"], e["text"], e["meta"]

    if kind == "you":
        return f'<div class="mine">{_e(text)}</div>'

    if kind == "speech":
        sp = meta.get("speaker") or {}
        name = sp.get("name") or "Someone"
        art = faces.get(sp.get("npc") or "")
        face = (f'<span class="face"><img src="{art}" alt=""></span>' if art
                else f'<span class="face">{_e(_initials(name))}</span>')
        return (f'<div class="said">{face}<div><b>{_e(name)}</b>'
                f'<q>{_e(text)}</q></div></div>')

    if kind == "refusal":
        return f'<div class="declines"><em>the world declines</em>{_paras(text)}</div>'

    if kind == "fate":
        if meta.get("as_news"):
            body = re.sub(r"^Word reaches you:\s*", "", text, flags=re.I)
            return f'<div class="hearsay"><em>word reaches you</em>{_paras(body)}</div>'
        title = f'<h4>{_e(meta.get("title"))}</h4>' if meta.get("title") else ""
        return f'<div class="fated">{title}{_paras(text)}</div>'

    if kind == "reveal":
        return (f'<div class="broke"><em>the lie comes apart</em>'
                f'<p>{_e(meta.get("actual") or text)}</p></div>')

    if kind == "whisper":
        w = meta.get("payload") or {}
        return (f'<div class="whispered"><em>whispered, only to you</em>'
                f'<p>{_e(w.get("text") or text)}</p></div>')

    if kind == "contest":
        return f'<div class="contested"><em>contested</em>{_paras(text)}</div>'

    if kind == "safety":
        return f'<div class="declines"><em>set aside</em>{_paras(text)}</div>'

    return f'<div class="prose">{_paras(text)}</div>'


def _initials(name: str) -> str:
    parts = [w for w in str(name or "?").split() if w]
    return ("".join(w[0] for w in parts)[:2] or "?").upper()


def render(pt_id: str, user_id: str = "") -> str:
    pt = engine._pt(pt_id)
    if not pt:
        raise KeyError(pt_id)
    world = engine.world_for(pt)

    faces = {n["id"]: _portrait_data_uri(n.get("portrait", ""))
             for n in world.npcs if n.get("portrait")}
    faces = {k: v for k, v in faces.items() if v}

    card = db.row("SELECT name, concept, avatar_url FROM cards WHERE playthrough_id=?"
                  " ORDER BY updated_at DESC LIMIT 1", (pt_id,))
    you = (card["name"] if card else "") or pt["protagonist"] or "a traveller"
    concept = (card["concept"] if card else "") or ""
    my_face = _portrait_data_uri(card["avatar_url"] if card else "")

    book = chapters.of(pt_id) or {}
    chapter_titles = {c.get("n"): c.get("title")
                      for c in (book.get("book") or []) if c.get("n")}

    rows = entries(pt_id)
    body, day, last_turn = [], None, -1
    for e in rows:
        turn = e["turn"]
        if turn != last_turn:
            d = world.day_for(turn)
            if d != day:
                day = d
                body.append(f'<div class="day">Day {d}</div>')
            last_turn = turn
        body.append(_block(e, faces))

    title = world.name
    head_face = (f'<span class="me"><img src="{my_face}" alt=""></span>' if my_face
                 else f'<span class="me">{_e(_initials(you))}</span>')
    chapter_line = ""
    if chapter_titles:
        now = book.get("n")
        if now and chapter_titles.get(now):
            chapter_line = f'<div class="chapter">Chapter {now} &middot; {_e(chapter_titles[now])}</div>'

    tagline = world.get("tagline") or ""
    return _PAGE.format(
        title=_e(title),
        tagline_block=f'<p class="tagline">{_e(tagline)}</p>' if tagline else "",
        head_face=head_face,
        you=_e(you),
        concept=_e(concept),
        turns=pt["current_turn"],
        chapter=chapter_line,
        body="\n".join(body),
        stamp=_e(pt["updated_at"][:10] if pt["updated_at"] else ""),
    )


# One file, no network. Deliberately plain: this is meant to survive being
# emailed, printed, and opened in five years by something that is not a
# browser we have heard of.
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: #16131A; color: #EFE9E1;
    font: 400 16px/1.6 "Manrope", system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .page {{ max-width: 34rem; margin: 0 auto; padding: 56px 22px 120px; }}
  header {{ border-bottom: 1px solid rgba(239,233,225,.14); padding-bottom: 26px; margin-bottom: 34px; }}
  h1 {{ font: 500 34px/1.15 "Spectral", Georgia, serif; margin: 0 0 6px; letter-spacing: -.01em; }}
  .tagline {{ font: italic 400 17px/1.5 "Spectral", Georgia, serif; color: #F5B96A; margin: 0 0 20px; }}
  .whois {{ display: flex; align-items: center; gap: 14px; }}
  .me, .face {{
    flex: none; border-radius: 50%; overflow: hidden; display: grid; place-items: center;
    background: radial-gradient(120% 90% at 32% 24%, #3A3346, #221F2C 74%);
    box-shadow: inset 0 0 0 1px rgba(240,169,76,.22);
    font-family: "Spectral", Georgia, serif; color: rgba(240,169,76,.72);
  }}
  .me {{ width: 62px; height: 62px; font-size: 23px; }}
  .face {{ width: 46px; height: 46px; font-size: 17px; }}
  .me img, .face img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .whois b {{ font: 500 18px "Spectral", Georgia, serif; display: block; }}
  .whois span {{ font-size: 13px; color: rgba(239,233,225,.62); }}
  .meta {{ margin-top: 16px; font: 400 11.5px/1.6 ui-monospace, "SFMono-Regular", monospace;
           letter-spacing: .1em; text-transform: uppercase; color: rgba(239,233,225,.5); }}
  .chapter {{ margin-top: 4px; }}
  .day {{
    margin: 40px 0 22px; font: 500 10.5px ui-monospace, monospace; letter-spacing: .2em;
    text-transform: uppercase; color: rgba(239,233,225,.4);
    border-top: 1px solid rgba(239,233,225,.12); padding-top: 12px;
  }}
  .prose p {{ font: 400 18px/1.75 "Spectral", Georgia, serif; margin: 0 0 1.05em; }}
  .mine {{
    margin: 22px 0; border-left: 2px solid rgba(240,169,76,.6); padding-left: 15px;
    font-size: 15px; line-height: 1.6; color: rgba(239,233,225,.74);
  }}
  .said {{ display: flex; gap: 16px; align-items: flex-start; margin: 24px 0; }}
  .said b {{ font: 600 17px "Spectral", Georgia, serif; }}
  .said q {{ display: block; margin-top: 7px; font: 400 20px/1.55 "Spectral", Georgia, serif; }}
  .declines, .hearsay, .whispered, .contested, .fated, .broke {{ margin: 24px 0; }}
  .declines, .hearsay {{ padding-left: 15px; color: rgba(239,233,225,.66); }}
  .declines {{ border-left: 1px solid rgba(239,233,225,.2); }}
  .hearsay {{ border-left: 1px dashed rgba(239,233,225,.24); font-style: italic; }}
  .whispered {{ background: rgba(86,196,192,.1); border-radius: 10px; padding: 14px 16px; }}
  .contested {{ border-top: 1px solid rgba(240,169,76,.3); padding-top: 11px; }}
  .fated {{ border-top: 1px solid rgba(240,169,76,.32); padding-top: 14px; }}
  .broke {{ border-top: 1px solid rgba(255,107,122,.6); border-bottom: 1px solid rgba(255,107,122,.24);
            padding: 16px 0 18px; }}
  .broke p {{ margin: 0; font: 400 21px/1.5 "Spectral", Georgia, serif; }}
  em {{
    display: block; font: 500 9.5px ui-monospace, monospace; letter-spacing: .14em;
    text-transform: uppercase; font-style: normal; margin-bottom: 8px;
    color: rgba(239,233,225,.62);
  }}
  .hearsay em {{ font-style: normal; }}
  .whispered em {{ color: #7FDEDB; }}
  .broke em {{ color: #FF6B7A; }}
  .declines p, .hearsay p, .whispered p, .contested p, .fated p {{
    font: 400 17px/1.65 "Spectral", Georgia, serif; margin: 0 0 .8em;
  }}
  h4 {{ font: 500 19px "Spectral", Georgia, serif; margin: 0 0 8px; }}
  footer {{ margin-top: 56px; padding-top: 20px; border-top: 1px solid rgba(239,233,225,.12);
            font-size: 12px; color: rgba(239,233,225,.45); }}
  @media print {{
    body {{ background: #fff; color: #111; }}
    .me, .face {{ background: #eee; box-shadow: inset 0 0 0 1px #ccc; color: #7a4a10; }}
    .mine {{ color: #444; }}
    em {{ color: #666; }}
    .whispered {{ background: #f2f2f2; }}
  }}
</style></head><body><div class="page">
<header>
  <h1>{title}</h1>
  {tagline_block}
  <div class="whois">{head_face}<div><b>{you}</b><span>{concept}</span></div></div>
  <div class="meta">{turns} turns lived{chapter}</div>
</header>
{body}
<footer>Written in StoryLiver. Nothing here was drawn by a machine &mdash; any
picture in this file was uploaded by the player. Saved {stamp}.</footer>
</div></body></html>"""
