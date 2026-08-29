# -*- coding: utf-8 -*-
"""Emberfall - the starter world.

A valley village three days before it burns. Fate is fixed and the World Master
enforces it. What is yours: who trusts you, who follows you, and who is standing
on the Reach Road at dawn.
"""

PHASES = ["morning", "midday", "evening", "night"]


def phase_for(turn: int) -> str:
    return PHASES[turn % 4]


def day_for(turn: int) -> int:
    return turn // 4 + 1


LOCATIONS = [
    {"id": "broken_bell", "name": "The Broken Bell", "kind": "tavern",
     "desc": "A low tavern with a cracked bell hung over the hearth as a joke that stopped being funny. Smells of wet wool and barley.",
     "connects": ["village_green", "ferry_landing"]},
    {"id": "village_green", "name": "The Village Green", "kind": "open",
     "desc": "A muddy commons ringed by shuttered houses. The Ashen Reach stands over it like a held breath.",
     "connects": ["broken_bell", "ash_chapel", "vosk_forge", "bitter_well", "millhouse", "reach_road", "marrows_herbary"]},
    {"id": "ash_chapel", "name": "The Ash Chapel", "kind": "sacred",
     "desc": "Grey stone, grey glass, a floor swept so hard the flagstones shine. Sister Adrahel keeps it that way.",
     "connects": ["village_green", "bell_tower"]},
    {"id": "bell_tower", "name": "The Warden's Tower", "kind": "civic",
     "desc": "Forty-one steps to a bell the size of a cart. From the top you can see the whole valley, which is the problem.",
     "connects": ["ash_chapel"]},
    {"id": "vosk_forge", "name": "Vosk Forge", "kind": "work",
     "desc": "Coal-dark, hot even at night. Tamsin's hammer keeps a rhythm you can set a watch by.",
     "connects": ["village_green"]},
    {"id": "bitter_well", "name": "The Bitter Well", "kind": "civic",
     "desc": "The village's deep well, ringed in river stone. Somebody has scratched a tally into the rim.",
     "connects": ["village_green", "ferrows_dig"]},
    {"id": "ferrows_dig", "name": "Ferrow's Dig", "kind": "work",
     "desc": "A raw shaft into the valley floor, shored with green timber. Warm air comes up it. It should not.",
     "connects": ["bitter_well"]},
    {"id": "marrows_herbary", "name": "Marrow's Herbary", "kind": "work",
     "desc": "A cottage hung with drying stalks, every surface labelled in a small furious hand.",
     "connects": ["millhouse", "village_green"]},
    {"id": "millhouse", "name": "The Millhouse", "kind": "work",
     "desc": "Bram Odell's mill, wheel turning over black water, sacks stacked for a harvest nobody will collect.",
     "connects": ["village_green", "marrows_herbary"]},
    {"id": "ferry_landing", "name": "The Ferry Landing", "kind": "threshold",
     "desc": "A plank jetty and a flat-bottomed ferry. It crosses at slack water and not otherwise.",
     "connects": ["broken_bell"]},
    {"id": "reach_road", "name": "The Reach Road", "kind": "threshold",
     "desc": "The one road out, climbing east into snow. Whoever is standing here at the end is who lived.",
     "connects": ["village_green"]},
]

# Hard world constraints. The World Master cites these by id when it rejects.
RULES = [
    {"id": "R1_no_magic",
     "text": "There is no magic in Emberfall. No spells, visions, or supernatural power is available to anyone, including the protagonist. Such attempts fail as ordinary actions.",
     "check": {"pattern": r"\b(cast|spell|magic|summon|teleport|enchant|conjure|levitat|telepath)\w*",
               "reason": "There is no magic in Emberfall. Whatever you reach for, your hands come back empty."}},
    {"id": "R2_reach_unclimbable",
     "text": "The Ashen Reach cannot be climbed. The eastern pass is snow-choked and impassable until the first ashfall melts it open (fated turn 15). Before then nobody leaves the valley by the mountain.",
     "check": {"pattern": r"\b(climb|scale|cross)\b.*\b(reach|mountain|pass|peak)\b",
               "when": {"turn_lt": 15},
               "reason": "The eastern pass is snow-choked to the shoulder. Nobody is walking over the Reach yet."}},
    {"id": "R3_bell_authority",
     "text": "The great bell may only be rung by the Warden, Maren Vosk, or by someone she has named aloud before a witness. Others cannot reach or work the rope.",
     "check": {"pattern": r"\bring (the )?(great )?bell\b",
               "when": {"location": "bell_tower", "flag_absent": "named_to_bell"},
               "reason": "The rope is the Warden's. Maren Vosk has named no one to it, and that includes you."}},
    {"id": "R4_coal_mute",
     "text": "The child called Coal does not speak, ever, under any circumstance, and cannot be made to. Coal communicates only by gesture, drawing, or presence.",
     "check": {"pattern": r"\b(make|force|get|have)\b[^.]{0,40}\bcoal\b[^.]{0,40}\b(speak|talk|say|answer)\b",
               "reason": "Coal does not speak. Coal has never spoken. The child looks at you and waits."}},
    {"id": "R5_ferry_slack",
     "text": "The ferry crosses only at slack water - the morning and evening phases. At midday or night it is moored and will not move.",
     "check": {"pattern": r"\b(ferry|cross the water|row across)\b",
               "when": {"phase_in": ["midday", "night"]},
               "reason": "The ferry only runs at slack water - morning or evening. It is moored, and it stays moored."}},
    {"id": "R6_one_place",
     "text": "No character is in two places at once. To act on someone the protagonist must share their location; travelling to a non-adjacent location takes an extra turn."},
    {"id": "R7_fate_immutable",
     "text": "Fated events cannot be prevented, delayed, or undone by any character. They may be witnessed, prepared for, and answered - never averted."},
    {"id": "R8_mortal_stakes",
     "text": "People are ordinary and mortal. No character survives what a person would not survive, and nobody who has died acts again."},
    {"id": "R9_no_invented_property",
     "text": "The protagonist owns only what they arrived with or what has been given, taken, or made on-screen. Items, allies, and authority cannot be invented mid-sentence."},
]

# Immutable. These occur on their turn regardless of anything the player does.
FATED_EVENTS = [
    {"turn": 4, "id": "F1_wells_bitter", "immutable": True, "location": "bitter_well",
     "title": "The wells go bitter",
     "desc": "Water drawn anywhere in Emberfall comes up black and tasting of iron."},
    {"turn": 9, "id": "F2_bell_cracks", "immutable": True, "location": "ash_chapel",
     "title": "The chapel bell cracks",
     "desc": "The chapel bell splits on the noon toll - a flat dead note that carries the length of the valley."},
    {"turn": 15, "id": "F3_reach_exhales", "immutable": True, "location": "village_green",
     "title": "The Reach exhales",
     "desc": "Ash falls for an hour, the sky turns copper, and the eastern pass melts open."},
    {"turn": 22, "id": "F4_dig_collapses", "immutable": True, "location": "ferrows_dig",
     "title": "The dig collapses",
     "desc": "Ferrow's shaft caves in. Hot water rises in it, and the ground under the green is warm to the palm."},
    {"turn": 30, "id": "F5_warden_dies", "immutable": True, "location": "bell_tower",
     "kills": "maren", "title": "The Warden falls",
     "desc": "Maren Vosk dies at the top of her tower, hand on the rope, having rung the evacuation until the floor gave. She does not come down."},
    {"turn": 38, "id": "F6_the_kindling", "immutable": True, "location": "village_green",
     "title": "The Kindling",
     "desc": "Fire comes up through the valley floor and Emberfall burns to its stones."},
    {"turn": 45, "id": "F7_dawn_on_the_road", "immutable": True, "location": "reach_road",
     "title": "Dawn on the Reach Road",
     "desc": "Whoever walked out has walked out. The valley behind is a bowl of smoke."},
]


def _npc(nid, name, role, voice, constraints, goals, taboos, start, sched, seeds, rel):
    return {
        "id": nid, "name": name, "role": role, "start_location": start,
        "anchors": {"name": name, "role": role, "voice": voice,
                    "constraints": constraints, "goals": goals, "taboos": taboos},
        "schedule": sched, "seed_memories": seeds, "initial_relationship": rel,
    }


NPCS = [
    _npc("maren", "Maren Vosk", "Warden of Emberfall",
         "Short declarative sentences. Never raises her voice. Uses people's full names. Answers a question with an instruction.",
         ["Will not leave the valley while anyone is still in it.",
          "Knows the Kindling is coming and has told no one but Ferrow.",
          "Refuses to name a successor to the bell."],
         ["Get every living soul onto the Reach Road.",
          "Keep the panic from starting before she can control it."],
         ["Never admits fear aloud.", "Never begs."],
         "bell_tower",
         {"morning": "bell_tower", "midday": "village_green", "evening": "bell_tower", "night": "bell_tower"},
         ["Forty years ago my mother rang this bell and half the valley called her mad.",
          "Ferrow showed me the warm water in the dig. I have not slept since."],
         {"affinity": 0, "trust": -10, "fear": 0, "obligation": 20}),

    _npc("tamsin", "Tamsin Vosk", "Blacksmith, the Warden's daughter",
         "Blunt, profane, funny when she is not angry. Talks while working, and stops working in order to be cruel.",
         ["Will not set foot in the Warden's Tower.",
          "Has a cart packed behind the forge and has not left."],
         ["Get her mother out of Emberfall alive.",
          "Stop needing her mother's approval."],
         ["Never says the word love to Maren.", "Never asks Adrahel for anything."],
         "vosk_forge",
         {"morning": "vosk_forge", "midday": "vosk_forge", "evening": "broken_bell", "night": "vosk_forge"},
         ["I packed the cart in spring. It is still packed.",
          "She named the bell in her will before she named me."],
         {"affinity": 5, "trust": 0, "fear": 0, "obligation": 0}),

    _npc("adrahel", "Sister Adrahel", "Keeper of the Ash Chapel",
         "Warm, unhurried, faintly amused. Speaks in the plural - we, the valley. Turns every objection into a blessing.",
         ["Believes the Kindling is a purification, not a disaster.",
          "Will not physically restrain anyone, ever."],
         ["Persuade the village to meet the fire rather than run from it.",
          "Be the last one standing in the chapel."],
         ["Never lies outright.", "Never calls the fire destruction."],
         "ash_chapel",
         {"morning": "ash_chapel", "midday": "ash_chapel", "evening": "village_green", "night": "ash_chapel"},
         ["The scripture says the valley is cleaned once in a lifetime. Mine is this one.",
          "Maren thinks I am a fool. I have prayed for her twice today."],
         {"affinity": 10, "trust": 0, "fear": 0, "obligation": 0}),

    _npc("corvin", "Corvin Pell", "Debt-keeper and buyer of deeds",
         "Smooth, over-polite, itemises everything. Calls people friend at the moment he is about to take something.",
         ["Carries a ledger and will not be parted from it.",
          "Has bought eleven deeds this month at a tenth of their worth."],
         ["Own the valley floor before the ash settles.",
          "Be on the last cart out, with the ledger."],
         ["Never gives anything away free.", "Never touches a shovel."],
         "broken_bell",
         {"morning": "village_green", "midday": "broken_bell", "evening": "broken_bell", "night": "broken_bell"},
         ["Bram Odell will sign by Thursday. He always signs.",
          "Land is cheapest in the hour before everyone understands."],
         {"affinity": 0, "trust": 0, "fear": 0, "obligation": 0}),

    _npc("nessa", "Nessa Quill", "Keeper of the Broken Bell",
         "Fast, wry, interrupts herself. Trades information like coin and says so out loud.",
         ["Knows one true thing about everyone in the village.",
          "Will not give a secret away without getting one."],
         ["Keep the tavern full and the talk flowing.",
          "Find out what Maren Vosk is hiding."],
         ["Never repeats a secret she was paid to keep - she resells it instead.",
          "Never drinks with a customer."],
         "broken_bell",
         {"morning": "broken_bell", "midday": "broken_bell", "evening": "broken_bell", "night": "broken_bell"},
         ["Corvin pays for names. I have given him four.",
          "The Vosks have not eaten in the same room since midwinter."],
         {"affinity": 5, "trust": 5, "fear": 0, "obligation": 0}),

    _npc("ferrow", "Old Ferrow", "Well-digger",
         "Deaf in the left ear, so he leans in and talks too loud. Rambles, then lands one devastating sentence.",
         ["Deaf on the left; must be addressed from his right or he mishears.",
          "Is the only person besides Maren who knows what the warm water means."],
         ["Get the dig shored before it takes someone.",
          "Be believed, once, before the end."],
         ["Never goes down the shaft after dark.", "Never says the word fire."],
         "ferrows_dig",
         {"morning": "ferrows_dig", "midday": "ferrows_dig", "evening": "bitter_well", "night": "broken_bell"},
         ["Water came up at forty feet and it was warm as blood.",
          "I told the Warden. She went white and said thank you, Ferrow."],
         {"affinity": 0, "trust": 0, "fear": 0, "obligation": 0}),

    _npc("yeva", "Dr. Yeva Marrow", "Herbalist and the valley's only physician",
         "Clipped, exhausted, precise about dosages and vague about hope. Swears in a language nobody here speaks.",
         ["Is running out of charcoal and clean water.",
          "Will treat anyone, including people she despises."],
         ["Keep the bitter-water sickness from killing the children.",
          "Get her patients onto the road before the ash."],
         ["Never refuses a patient.", "Never promises anyone will live."],
         "marrows_herbary",
         {"morning": "marrows_herbary", "midday": "village_green", "evening": "marrows_herbary", "night": "marrows_herbary"},
         ["Three children on the green cough black. That is not a cold.",
          "I have charcoal for nine days if nobody else falls ill."],
         {"affinity": 0, "trust": 5, "fear": 0, "obligation": 0}),

    _npc("bram", "Bram Odell", "Miller",
         "Slow, stubborn, repeats his own last three words when cornered. Calls the mill her.",
         ["Will not sign his deed away while sober.",
          "Will not leave the millhouse for any reason before the ash falls."],
         ["Keep the mill in Odell hands.",
          "Prove to the village he was right to stay."],
         ["Never asks for help.", "Never sets foot in the chapel."],
         "millhouse",
         {"morning": "millhouse", "midday": "millhouse", "evening": "millhouse", "night": "millhouse"},
         ["Four generations of Odells and none of them ran.",
          "Pell has been at my door twice with that ledger."],
         {"affinity": 0, "trust": 0, "fear": 0, "obligation": 0}),

    _npc("hadrik", "Hadrik Sunn", "Mercenary, passing through",
         "Easy, transactional, laughs at his own bleakness. Quotes prices for things that should not have prices.",
         ["Works only for payment agreed up front.",
          "Will not break a contract he has taken money for."],
         ["Leave the valley richer than he entered it.",
          "Avoid having to decide anything that matters."],
         ["Never works free.", "Never lies about the terms."],
         "broken_bell",
         {"morning": "village_green", "midday": "broken_bell", "evening": "broken_bell", "night": "ferry_landing"},
         ["I came for a caravan job that never showed.",
          "Pell offered me coin to stand at the mill door. I have not answered."],
         {"affinity": 0, "trust": 0, "fear": 0, "obligation": 0}),

    _npc("ilo", "Ilo", "Ferrier's boy, twelve",
         "Breathless run-on sentences, asks three questions at once, repeats adult phrases he does not understand.",
         ["Twelve years old; cannot fight, lift, or keep a secret.",
          "Goes everywhere and is noticed nowhere."],
         ["Be trusted with something real by an adult.",
          "Find out where Coal came from."],
         ["Never stays quiet when asked to.", "Never goes into the chapel alone."],
         "ferry_landing",
         {"morning": "ferry_landing", "midday": "village_green", "evening": "broken_bell", "night": "ferry_landing"},
         ["I found Coal in the ash by the dig and nobody believes me.",
          "The Warden knows my name. She said it once."],
         {"affinity": 15, "trust": 10, "fear": 0, "obligation": 0}),

    _npc("coal", "Coal", "The child from the ash",
         "Never speaks. Communicates only through gesture, drawing in ash, and unnervingly steady eye contact.",
         ["Does not speak, and cannot be made to, under any circumstance.",
          "Draws the same spiral on every surface.",
          "Is never afraid of fire or heat."],
         ["Be near the bell when it matters.",
          "Make one person understand the spiral."],
         ["Never speaks.", "Never runs from heat."],
         "village_green",
         {"morning": "village_green", "midday": "ash_chapel", "evening": "village_green", "night": "bell_tower"},
         ["The spiral is the shape of the warm place under the ground.",
          "The old woman in the tower looked at my drawing and did not laugh."],
         {"affinity": 0, "trust": 0, "fear": 0, "obligation": 0}),
]

# Pre-existing tension in the social graph: (src, dst, (affinity, trust, fear, obligation))
NPC_EDGES = [
    ("tamsin", "maren", (30, -20, 0, 60)), ("maren", "tamsin", (70, 20, 0, 40)),
    ("adrahel", "maren", (-10, -30, 0, 0)), ("corvin", "bram", (-20, -40, 0, 0)),
    ("nessa", "corvin", (-30, -50, 10, 0)), ("ilo", "coal", (40, 30, 10, 0)),
    ("ferrow", "maren", (40, 60, 20, 30)), ("yeva", "adrahel", (-40, -30, 0, 0)),
]


# --------------------------------------------------------------------------
# Who holds power here, and where they actually stand at each hour.
#
# Two factions, deliberately different in kind. The Warden's Office is an
# INSTITUTION: it has a seat, named officers, patrol schedules, and law high
# enough to issue warrants. The valley itself is a CROWD: it forms opinions and
# spreads them, but it cannot arrest anyone.
#
# The schedules matter more than they look. A guard who is everywhere makes
# stealth pointless; a guard who is nowhere makes the institution decoration.
# These put someone at the Broken Bell in every phase but midday, and leave the
# Bitter Well and Ferrow's Dig unwatched - which is the whole reason those are
# the interesting places to do something you would rather nobody saw.
#
# Note Maren dies on fated turn 30. That is not a bug in the roster: the
# Warden's Office losing its head mid-run is exactly the power vacuum the
# death layer already models, and Tamsin inheriting her mother's authority is
# a story the simulation produces on its own.
FACTIONS = [
    {
        "id": "warden_office",
        "name": "The Warden's Office",
        "seat": "bell_tower",
        "law": 5,
        "members": ["maren", "hadrik", "tamsin"],
        "officers": [
            {"id": "maren", "name": "Maren Vosk", "rank": "Warden",
             "schedule": {"morning": "village_green", "midday": "bell_tower",
                          "evening": "village_green", "night": "bell_tower"}},
            {"id": "hadrik", "name": "Hadrik Sunn", "rank": "sworn sword",
             "schedule": {"morning": "broken_bell", "midday": "ferry_landing",
                          "evening": "broken_bell", "night": "broken_bell"}},
            {"id": "tamsin", "name": "Tamsin Vosk", "rank": "deputised",
             "schedule": {"morning": "vosk_forge", "midday": "village_green",
                          "evening": "vosk_forge", "night": "vosk_forge"}},
        ],
    },
    {
        "id": "locals",
        "name": "The people of Emberfall",
        "seat": "village_green",
        "law": 1,
        "members": ["adrahel", "corvin", "nessa", "ferrow", "yeva", "bram", "ilo", "coal"],
    },
]

WORLD = {
    "id": "emberfall",
    "origin": "starter",
    "name": "Emberfall",
    "tagline": "Three days before the valley burns.",
    "premise": (
        "Emberfall is a village of two hundred in the crook of the Ashen Reach. Once in a lifetime the "
        "ground beneath it wakes; the old people call it the Kindling. It is coming, and it cannot be "
        "stopped. You arrived yesterday, an outsider with a road behind you and no claim here. Fate is "
        "already written: the wells, the bell, the ash, the Warden, the fire. What is not written is who "
        "believes you, who follows you, and who is standing on the Reach Road at dawn."
    ),
    "fate_note": "القدر — fate is fixed. Your path through it is not.",
    "start_location": "broken_bell",
    "arrival": "You arrive in Emberfall off the low road.",
    "default_protagonist": "a traveller whose name nobody here has heard",
    "opening": (
        "Rain has been falling on Emberfall for two days, and the Broken Bell smells of it - wet wool, "
        "peat smoke, barley gone sour in the cask. You came in last night off the low road with mud to the "
        "knee and no story anyone believed, and nobody asked twice.\n\n"
        "Above the hearth hangs the tavern's namesake: a bell, split down one side, hung as a joke by "
        "somebody's grandfather. Nessa Quill polishes a glass she has already polished, and watches you do "
        "the thing outsiders do, which is decide whether to stay.\n\n"
        "Through the shutters, the Ashen Reach stands over the valley like a held breath."
    ),
    "locations": LOCATIONS,
    "rules": RULES,
    "fated_events": FATED_EVENTS,
    "npcs": NPCS,
    "factions": FACTIONS,
    "npc_edges": NPC_EDGES,
}

BY_ID = {n["id"]: n for n in NPCS}
LOC_BY_ID = {loc["id"]: loc for loc in LOCATIONS}
RULE_BY_ID = {r["id"]: r for r in RULES}
