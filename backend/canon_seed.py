"""A small, hand-curated cast for the handful of franchises players build
against constantly, used two ways:

  1. FALLBACK - when live research (Wikipedia + Fandom) comes back empty or
     thin (a timeout, a blocked host, an obscure subdomain), a build must not
     silently fall through to the model inventing names. `Kaname` and `Aiko`
     were never in Demon Slayer; they were what the model wrote when
     `research.dossier()` returned nothing and it had to fill the cast itself.

  2. PIN - even when live research succeeds, a wiki category listing is
     alphabetical and a "top 16 by article size" ranking can still miss the
     actual protagonist if their article happens to be shorter than a popular
     side character's (a Sengoku-era flashback figure with a small page can
     outrank the lead in raw byte count). The lead(s) here are guaranteed a
     seat regardless of what ranking research produced.

Deliberately small. This is not a wiki mirror - it exists to stop the two
specific, reproduced failures (empty cast -> invented names; a real
protagonist silently dropped), not to replace live research for everything
players might type. Unmatched settings fall through to research/the model
exactly as before.
"""
from __future__ import annotations

import re

# key -> {aliases, protagonists (always present, in order), cast (name+note)}
SEEDS = {
    "demon_slayer": {
        "aliases": ("demon slayer", "kimetsu no yaiba"),
        "protagonists": ["Tanjiro Kamado", "Kanao Tsuyuri"],
        "places": [
            ("Kamado Household", "the mountainside home Tanjiro's family was slaughtered in"),
            ("Butterfly Mansion", "the Insect Hashira's estate, half hospital, half school"),
            ("Swordsmith Village", "hidden, guarded, where the demon slayers' blades are forged"),
            ("Mugen Train", "the train the Flame Hashira boarded and did not leave"),
            ("Ubuyashiki Estate", "seat of the demon slayer corps' leadership"),
        ],
        "cast": [
            ("Tanjiro Kamado", "demon slayer, carries his sister Nezuko in a box on his back"),
            ("Nezuko Kamado", "Tanjiro's sister, turned demon, fights it every day"),
            ("Zenitsu Agatsuma", "cowardly awake, terrifyingly precise asleep"),
            ("Inosuke Hashibira", "boar-mask, raised by boars, reads people through his skin"),
            ("Kanao Tsuyuri", "raised not to feel; a coin decides for her when she can't"),
            ("Giyu Tomioka", "Water Hashira, said almost nothing since his sister died"),
            ("Shinobu Kocho", "Insect Hashira, smiles the whole way through killing you"),
            ("Kyojuro Rengoku", "Flame Hashira, booming, never hedges, never rests"),
            ("Muzan Kibutsuji", "the first demon, wears whatever face keeps him hidden"),
        ],
    },
    "jujutsu_kaisen": {
        "aliases": ("jujutsu kaisen", "jjk"),
        "protagonists": ["Yuji Itadori"],
        "places": [
            ("Tokyo Jujutsu High", "trains sorcerers who exorcise curses born of human malice"),
            ("Kyoto Jujutsu High", "the sister school, and the rival"),
            ("Shibuya", "a city district that became a battlefield overnight"),
        ],
        "cast": [
            ("Yuji Itadori", "swallowed a cursed finger to save his classmates from what it would do loose"),
            ("Megumi Fushiguro", "flat affect, shikigami summoner, decides fast and regrets slow"),
            ("Nobara Kugisaki", "straight-talking, hammer and nails, came to the city to matter"),
            ("Satoru Gojo", "the strongest, and bored by it"),
            ("Sukuna", "the King of Curses, riding inside Itadori's body"),
        ],
    },
    "attack_on_titan": {
        "aliases": ("attack on titan", "shingeki no kyojin"),
        "protagonists": ["Eren Yeager"],
        "places": [
            ("Shiganshina District", "the outer wall town where it all started falling apart"),
            ("Trost District", "held, at a cost nobody who was there forgets"),
            ("Wall Maria", "the outermost wall, and the first to fail"),
        ],
        "cast": [
            ("Eren Yeager", "watched the wall fall as a child; has not stopped since"),
            ("Mikasa Ackerman", "scarf, knife, and one person she will not lose again"),
            ("Armin Arlert", "the plan nobody else could see, said quietly"),
            ("Levi Ackerman", "cleans everything, forgives nothing, kills efficiently"),
        ],
    },
    "naruto": {
        "aliases": ("naruto",),
        "protagonists": ["Naruto Uzumaki"],
        "places": [
            ("Konohagakure", "the Hidden Leaf Village"),
            ("Academy Grounds", "where every genin trained before the real world got them"),
            ("Valley of the End", "where a friendship ended in a fight"),
        ],
        "cast": [
            ("Naruto Uzumaki", "loud, orphaned, refuses to be written off"),
            ("Sasuke Uchiha", "the last of a clan he watched die"),
            ("Sakura Haruno", "outgrew being the one who needed saving"),
            ("Kakashi Hatake", "reads in the open, teaches by disappearing"),
        ],
    },
    "one_piece": {
        "aliases": ("one piece",),
        "protagonists": ["Monkey D. Luffy"],
        "places": [
            ("Foosha Village", "Luffy's home port, where the journey started"),
            ("Baratie", "a restaurant that floats, run by a crew of cooks who also fight"),
            ("Grand Line", "the sea nobody sails and comes back the same"),
        ],
        "cast": [
            ("Monkey D. Luffy", "rubber, reckless, means every promise literally"),
            ("Roronoa Zoro", "three swords, no sense of direction, absolute loyalty"),
            ("Nami", "counts everything, trusts slowly, navigates by memory"),
            ("Sanji", "will not hit a woman, will burn down anything else"),
        ],
    },
    "hazbin_hotel": {
        "aliases": ("hazbin hotel",),
        "protagonists": ["Charlie Morningstar"],
        "places": [
            ("The Hazbin Hotel", "Charlie's rehabilitation project, in the middle of Hell"),
            ("Pentagram City", "Hell's own capital, loud and lawless"),
            ("The Overlords' territory", "carved up between the ones with actual power"),
        ],
        "cast": [
            ("Charlie Morningstar", "princess of Hell, running a hotel on pure stubborn optimism"),
            ("Vaggie", "Charlie's second, a blade first and a person second when it counts"),
            ("Alastor", "1930s radio static, always smiling, terms always hidden"),
            ("Angel Dust", "performs fine because the alternative is not an option"),
            ("Husk", "used to be someone's, drinks like it still matters"),
            ("Niffty", "small, fast, unnervingly good with a knife"),
        ],
    },
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def match(setting: str, canonical: str = "") -> dict | None:
    """The seed entry for this setting, or None. Matched on either the typed
    setting or the name research resolved it to - a build that already found
    "Kimetsu no Yaiba" as the canonical title should still hit the Demon
    Slayer seed even if the player typed "demon slayer"."""
    probes = [_norm(setting), _norm(canonical)]
    for entry in SEEDS.values():
        aliases = {_norm(a) for a in entry["aliases"]}
        if any(p and (p in aliases or any(p in a or a in p for a in aliases)) for p in probes):
            return entry
    return None


def fallback_cast(setting: str, canonical: str = "") -> list:
    """Characters to use when live research came back with nothing usable."""
    entry = match(setting, canonical)
    if not entry:
        return []
    return [{"name": n, "note": note} for n, note in entry["cast"]]


def fallback_places(setting: str, canonical: str = "") -> list:
    """Places to use when live research came back with nothing usable. The
    same failure that empties a cast (a category-fetch timeout) empties this
    bucket too, and an ungrounded build invents a location exactly the way it
    invents a person - grounding_brief's REAL PLACES section goes silent and
    the model fills in whatever it likes."""
    entry = match(setting, canonical)
    if not entry:
        return []
    return [{"name": n, "note": note} for n, note in entry.get("places", [])]


def pin_protagonists(setting: str, canonical: str, characters: list) -> list:
    """Guarantee the lead(s) of a matched franchise are in the list, first -
    live research can rank a real protagonist below a shorter-article side
    character, and a Tanjiro-less Demon Slayer build is not Demon Slayer."""
    entry = match(setting, canonical)
    if not entry:
        return characters
    have = {_norm(c["name"]) for c in characters}
    missing = [{"name": p, "note": ""} for p in entry["protagonists"] if _norm(p) not in have]
    return missing + characters
