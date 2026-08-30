"""World modes - the dials that re-tune every other layer.

Modes compose. STRICT + HUMOR + HARDCORE is a valid, coherent world: canon is
enforced mathematically, the narrator tells it funny, and death is permanent.
Each axis is independent, so this is a product of choices rather than a list
of presets, and nothing here is a preset the player cannot leave.

Everything in this module is DETERMINISTIC and costs $0. Modes change what the
narrator is TOLD (a tone directive appended to its system prompt), what the
guardrail CANCELS, and what the deterministic maths does - never how many
model calls fire. A HUMOR world is not more expensive than a DARK one.

Consensus rule: at a table, changing a mode is a group decision, gated the
same way a character card is. Solo, the player just sets it.
"""
from __future__ import annotations

import json

from . import db, rt

# --------------------------------------------------------------------------
# The axes. Each is (id -> spec). `default` marks the value used when a world
# never chose. Ordering inside an axis is meaningful for the UI only.
# --------------------------------------------------------------------------

CANON = {
    "strict": {
        "name": "Strict", "blurb": "Canon-locked. Deviations are cancelled, not argued with.",
        "guardrail": "cancel", "famous_lines": "encourage", "timeline": "enforced",
    },
    "loose": {
        "name": "Loose", "blurb": "Canon is a suggestion. The guardrail warns, then allows.",
        "guardrail": "warn", "famous_lines": "allow", "timeline": "free",
        "default": True,
    },
}

TONE = {
    "neutral": {"name": "Neutral", "blurb": "The world's own register.", "default": True,
                "directive": ""},
    "humor": {
        "name": "Humor", "blurb": "Narrator quips. NPCs crack jokes. Absurdity is permitted.",
        "directive": ("TONE: comedy. You are not a chatbot being funny - you are this world, "
                      "and this world is funny. Land jokes in the narration itself, let NPCs be "
                      "absurd inside their own personalities, and deliver famous lines with irony "
                      "when the beat allows. Never break character to explain a joke."),
    },
    "dark": {
        "name": "Dark", "blurb": "Grim, high-stakes, heavier consequence.",
        "directive": ("TONE: horror. Grim and close. Consequence lands heavier, ambience is "
                      "physical and specific, dread accumulates. Do not relieve tension with a "
                      "joke. Violence has weight and aftermath."),
    },
    "chill": {
        "name": "Cozy", "blurb": "Low threat, social, slice-of-life. Nobody dies.",
        "directive": ("TONE: cozy. Low threat, warm, unhurried in feeling even when the pacing "
                      "is fast. Favour the social and the domestic. Conflict resolves toward "
                      "connection rather than loss."),
        "no_permadeath": True, "downed_is_knockout": True,
    },
}

STAKES = {
    "normal": {"name": "Normal", "blurb": "Death has resolutions. No permadeath.",
               "default": True, "permadeath": False},
    "hardcore": {
        "name": "Hardcore", "blurb": "Permadeath. Meta-progression carries a fraction forward.",
        "permadeath": True, "meta_progression": True,
    },
}

PACING = {
    "fast": {"name": "Fast", "blurb": "Beats land quickly. Downtime is compressed hard.",
             "beat_bias": 0.25, "ff_default_turns": 12},
    "normal": {"name": "Normal", "blurb": "A novel's rhythm.", "default": True,
               "beat_bias": 0.0, "ff_default_turns": 8},
    "epic": {"name": "Epic", "blurb": "Room to breathe. Arcs are given their length.",
             "beat_bias": -0.15, "ff_default_turns": 5},
}

COMBAT = {
    "tactical": {"name": "Tactical", "blurb": "WEGO grid: declare simultaneously, resolve together.",
                 "default": True, "wego": True},
    "narrative": {"name": "Narrative", "blurb": "Fights resolve as prose, not positions.",
                  "wego": False},
    "off": {"name": "Off", "blurb": "Pure roleplay. No combat system at all.",
            "wego": False, "disabled": True},
}

DIFFICULTY = {
    "story": {"name": "Story", "blurb": "The world yields. Resources are generous.",
              "aggression": 0.6, "scarcity": 0.7},
    "normal": {"name": "Normal", "blurb": "Fair.", "default": True,
               "aggression": 1.0, "scarcity": 1.0},
    "brutal": {"name": "Brutal", "blurb": "NPCs press advantage. Nothing is spare.",
               "aggression": 1.45, "scarcity": 1.35},
}

AXES = {
    "canon": CANON, "tone": TONE, "stakes": STAKES,
    "pacing": PACING, "combat": COMBAT, "difficulty": DIFFICULTY,
}


def _default(axis: str) -> str:
    for key, spec in AXES[axis].items():
        if spec.get("default"):
            return key
    return next(iter(AXES[axis]))


DEFAULTS = {axis: _default(axis) for axis in AXES}

# Language is free text (a world's native voice, e.g. JP-flavoured for anime
# worlds), not an enumerated axis - it is carried alongside.
LANGUAGE_KEY = "language"


class UnknownMode(ValueError):
    pass


def validate(patch: dict) -> dict:
    """Reject an unknown axis or value loudly rather than silently ignoring it -
    a typo'd mode that silently does nothing is worse than an error."""
    clean = {}
    for axis, value in (patch or {}).items():
        if axis == LANGUAGE_KEY:
            clean[axis] = str(value or "")[:60]
            continue
        if axis not in AXES:
            raise UnknownMode(f"no such mode axis {axis!r}")
        if value not in AXES[axis]:
            raise UnknownMode(f"{axis} has no setting {value!r}")
        clean[axis] = value
    return clean


def get(pt_id: str) -> dict:
    row = db.row("SELECT modes FROM playthroughs WHERE id=?", (pt_id,))
    stored = db.jload(row["modes"], {}) if row else {}
    out = dict(DEFAULTS)
    out[LANGUAGE_KEY] = ""
    out.update({k: v for k, v in stored.items() if k == LANGUAGE_KEY or k in AXES})
    return out


def set_modes(pt_id: str, patch: dict) -> dict:
    current = get(pt_id)
    current.update(validate(patch))
    db.run("UPDATE playthroughs SET modes=? WHERE id=?", (json.dumps(current), pt_id))
    rt.cache_drop(f"sl:pt:{pt_id}:snapshot")
    return current


def spec(pt_id: str, axis: str) -> dict:
    return AXES[axis][get(pt_id)[axis]]


# --------------------------------------------------------------------------
# What the rest of the engine actually asks
# --------------------------------------------------------------------------

def tone_directive(pt_id: str) -> str:
    """Appended to the narrator's system prompt. Empty on neutral, so a
    default world's prompt is byte-identical to what it was before modes
    existed - the anti-collapse anchors do not move."""
    m = get(pt_id)
    parts = []
    directive = TONE[m["tone"]].get("directive", "")
    if directive:
        parts.append(directive)
    if m[LANGUAGE_KEY]:
        parts.append(f"LANGUAGE: narrate in the register and idiom of {m[LANGUAGE_KEY]}.")
    if m["canon"] == "strict":
        parts.append("CANON: strict. Do not invent around the world's established facts. "
                     "If a character has a canon line that fits this exact beat, deliver it.")
    return "\n".join(parts)


def permadeath_on(pt_id: str) -> bool:
    """Cozy is a hard safety floor: it overrides Hardcore rather than
    negotiating with it. A player who set Cozy asked not to lose anyone, and
    that promise outranks a stakes dial someone else moved."""
    m = get(pt_id)
    if TONE[m["tone"]].get("no_permadeath"):
        return False
    return bool(STAKES[m["stakes"]].get("permadeath"))


def downed_is_knockout(pt_id: str) -> bool:
    return bool(TONE[get(pt_id)["tone"]].get("downed_is_knockout"))


def meta_progression_on(pt_id: str) -> bool:
    return bool(STAKES[get(pt_id)["stakes"]].get("meta_progression"))


def guardrail_action(pt_id: str) -> str:
    """'cancel' under STRICT, 'warn' under LOOSE."""
    return CANON[get(pt_id)["canon"]]["guardrail"]


def combat_enabled(pt_id: str) -> bool:
    return not COMBAT[get(pt_id)["combat"]].get("disabled")


def wego_enabled(pt_id: str) -> bool:
    return bool(COMBAT[get(pt_id)["combat"]].get("wego"))


def beat_bias(pt_id: str) -> float:
    """Shifts the Director's tension threshold. Fast pacing lowers the bar for
    a beat; Epic raises it. Deterministic, and it never changes the CALL
    COUNT - only whether the one Director call it was already allowed to make
    decides to fire."""
    return float(PACING[get(pt_id)["pacing"]]["beat_bias"])


def difficulty_scalars(pt_id: str) -> tuple:
    d = DIFFICULTY[get(pt_id)["difficulty"]]
    return float(d["aggression"]), float(d["scarcity"])


def fastforward_turns(pt_id: str) -> int:
    return int(PACING[get(pt_id)["pacing"]]["ff_default_turns"])


def catalogue() -> dict:
    """Everything the mode picker needs to render itself, defaults included."""
    return {
        "axes": {
            axis: {
                "default": DEFAULTS[axis],
                "options": [{"id": k, **{kk: vv for kk, vv in v.items()
                                         if kk in ("name", "blurb")}}
                            for k, v in options.items()],
            }
            for axis, options in AXES.items()
        },
        "language": {"default": "", "free_text": True},
    }


def public(pt_id: str) -> dict:
    m = get(pt_id)
    return {
        "modes": m,
        "labels": {axis: AXES[axis][m[axis]]["name"] for axis in AXES},
        "permadeath": permadeath_on(pt_id),
        "combat_enabled": combat_enabled(pt_id),
        "wego": wego_enabled(pt_id),
    }


# ---------------------------------------------------------------------------
# Canon strictness as a SPECTRUM, not one switch
# ---------------------------------------------------------------------------
# A single strict/loose dial is too blunt for how people actually play. The
# common table position is "the lore is sacred, but I do not want the physics
# argued with" - or the reverse for a hard-SF setting. The blueprint asks for
# strictness PER DOMAIN plus a consequence-severity slider, which is what this
# adds. The single `canon` axis stays exactly as it was and acts as the
# DEFAULT for every domain, so a world that never opens this panel behaves
# byte-identically to before.

DOMAINS = {
    "lore":    {"name": "Lore and history",
                "blurb": "What happened, who is who, what is already true."},
    "powers":  {"name": "Powers and their rules",
                "blurb": "How abilities work, what they cost, what they cannot do."},
    "physics": {"name": "Physics and the ordinary world",
                "blurb": "Distance, injury, fire, water - the parts nobody wrote down."},
    "tone":    {"name": "Tone and register",
                "blurb": "How grim, how funny, how the world sounds."},
    "people":  {"name": "Who characters are",
                "blurb": "Persona, voice, values. Never relaxed below 'strict'."},
}

# 0 = anything goes, 1 = the world pushes back, 2 = cancelled outright.
DOMAIN_LEVELS = {
    0: {"id": "loose",  "label": "Loose",  "blurb": "Bend it freely."},
    1: {"id": "firm",   "label": "Firm",   "blurb": "The world argues, then allows."},
    2: {"id": "strict", "label": "Strict", "blurb": "Cancelled before it happens."},
}

# How hard a consequence lands. Multiplies deterministic severity - it never
# changes WHETHER something is witnessed, only how much it costs.
SEVERITY = {
    "gentle": {"mult": 0.5, "label": "Gentle",
               "blurb": "Mistakes sting. They do not end things."},
    "normal": {"mult": 1.0, "label": "Normal", "default": True,
               "blurb": "What you did is what it costs."},
    "harsh":  {"mult": 1.6, "label": "Harsh",
               "blurb": "The world remembers hard and forgives slowly."},
}


def domain_defaults(pt_id: str) -> dict:
    """Every domain inherits the single canon dial until it is set explicitly.
    'people' is the exception and is never below strict - a character breaking
    their own persona is a product failure, not a freedom a table opted into."""
    base = 2 if get(pt_id)["canon"] == "strict" else 1
    out = {d: base for d in DOMAINS}
    out["people"] = 2
    return out


def _blob(pt_id: str) -> dict:
    row = db.row("SELECT modes FROM playthroughs WHERE id=?", (pt_id,))
    return db.jload(row["modes"], {}) if row else {}


def domains(pt_id: str) -> dict:
    stored = (_blob(pt_id).get("_domains") or {})
    out = domain_defaults(pt_id)
    for k, v in (stored or {}).items():
        if k in DOMAINS and k != "people":
            try:
                out[k] = max(0, min(2, int(v)))
            except (TypeError, ValueError):
                continue
    return out


def severity(pt_id: str) -> str:
    val = _blob(pt_id).get("_severity") or ""
    return val if val in SEVERITY else "normal"


def severity_mult(pt_id: str) -> float:
    return SEVERITY[severity(pt_id)]["mult"]


def set_domains(pt_id: str, *, levels=None, sev=None) -> dict:
    """Stored under reserved keys in the same modes blob, so the spectrum
    travels with the world and no schema change is needed. The leading
    underscore keeps them out of validate(), which would reject them as
    unknown axes."""
    blob = _blob(pt_id)
    if levels is not None:
        blob["_domains"] = {k: max(0, min(2, int(v))) for k, v in levels.items()
                            if k in DOMAINS and k != "people"}
    if sev is not None:
        if sev not in SEVERITY:
            raise ValueError(f"severity must be one of {sorted(SEVERITY)}")
        blob["_severity"] = sev
    db.run("UPDATE playthroughs SET modes=? WHERE id=?", (json.dumps(blob), pt_id))
    rt.invalidate_playthrough(pt_id)
    return domain_public(pt_id)


def domain_public(pt_id: str) -> dict:
    lv = domains(pt_id)
    return {
        # DOMAIN_LEVELS carries its own "id", which silently overwrote the
        # domain's id when splatted in - so every domain came back identified
        # as its own level. Namespaced instead.
        "domains": [{"id": d, **DOMAINS[d], "level": lv[d],
                     "level_id": DOMAIN_LEVELS[lv[d]]["id"],
                     "level_label": DOMAIN_LEVELS[lv[d]]["label"],
                     "level_blurb": DOMAIN_LEVELS[lv[d]]["blurb"],
                     "locked": d == "people"} for d in DOMAINS],
        "levels": [{"value": k, **v} for k, v in DOMAIN_LEVELS.items()],
        "severity": severity(pt_id),
        "severities": [{"id": k, **v} for k, v in SEVERITY.items()],
    }


def domain_directive(pt_id: str) -> str:
    """What the World Master is told about where this table is flexible.
    Empty when every domain sits at its default, so an untouched world sends
    exactly the bytes it always did."""
    lv = domains(pt_id)
    base = domain_defaults(pt_id)
    if lv == base:
        return ""
    words = {0: "may be bent freely", 1: "should be argued before allowed",
             2: "is absolute"}
    lines = [f"- {DOMAINS[d]['name']}: {words[lv[d]]}." for d in DOMAINS if lv[d] != base[d]]
    return "THIS TABLE'S CANON SPECTRUM:\n" + "\n".join(lines)
