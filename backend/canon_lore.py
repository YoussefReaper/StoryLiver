"""A curated canon dossier for the franchises players build against constantly.

`worldforge._apply_canon_personas` asks the MODEL for each canon character's
card, and that is the right primary path - it is richer and stays current. This
is the floor under it.

When that call is offline, or the model simply does not know a character, the
character used to keep whatever the builder invented from a one-line roster
note. That is how a build handed the narrator, under the heading "obey these
exactly":

    Nezuko Kamado - a quiet village girl
      VOICE: Soft-spoken and kind.
      CONSTRAINTS: Is an ordinary mortal person.

- for a character who is mute, is a demon, and rides in a wooden box on her
brother's back. Inosuke Hashibira came out speaking in ordinary sentences. A
table cannot go stale the way a lookup can, it costs nothing, and it is the same
idea as canon_seed: canon fidelity must not depend on the network.

A model card always WINS over this one, field by field - this only fills what
the model left unsaid. An invented background resident is never touched; only a
character named here gets a floor.
"""
from __future__ import annotations

import re


def _fold(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


# name -> card. Same shape `_apply_canon_personas` already consumes from the
# model, so both paths land in one place.
LORE: dict[str, dict] = {
    # ---------------------------------------------------------------- Demon Slayer
    "tanjiro kamado": {
        "voice": "Gentle, earnest, relentlessly polite. Speaks plainly and warmly even to "
                 "enemies; says what he means and means what he says.",
        "constraints": ["Is a demon slayer, and a kind one - he kills demons, not people.",
                        "Carries his sister Nezuko in a wooden box on his back.",
                        "Fights with Total Concentration Breathing (Water Breathing, later "
                        "Hinokami Kagura). No magic, no powers beyond breath and blade.",
                        "Can smell emotion, and the faint thread of an enemy's intent."],
        "goals": ["Turn Nezuko human again.", "Find and end Muzan Kibutsuji."],
        "taboos": ["Never kills a human.", "Never abandons Nezuko."],
        "mannerisms": ["Bows and apologises reflexively, even mid-fight.",
                       "Tilts his head and closes his eyes to read a scent.",
                       "The scar on his forehead is a small mark normally, and darkens "
                       "into a patterned burn when he strains."],
        "memories": ["Muzan slaughtered my family. Nezuko survived - as a demon.",
                     "I will find a way to make her human again."],
    },
    "nezuko kamado": {
        "voice": "Never speaks. Communicates only through gesture, expression, and muffled "
                 "sounds through the bamboo muzzle.",
        "constraints": ["Does not speak and cannot be made to, under any circumstance.",
                        "Is a demon who has never eaten a human and fights her own hunger.",
                        "Travels in the wooden box on Tanjiro's back, and is small enough to fit.",
                        "Cannot stand sunlight; protects humans from other demons."],
        "goals": ["Protect her brother Tanjiro.", "Stay human in the ways that matter."],
        "taboos": ["Never speaks.", "Never harms a human."],
        "mannerisms": ["A bamboo muzzle is bound over her mouth at all times.",
                       "Grows feral and explosive - claws, fangs - the instant someone "
                       "she cares for is threatened."],
        "memories": ["My brother carries me. He smells like sunshine and stubbornness.",
                     "I will not eat people. I decided that myself."],
    },
    "zenitsu agatsuma": {
        "voice": "Loud, wailing, endlessly complaining when awake. On the rare occasions he "
                 "is asleep, flat and utterly composed.",
        "constraints": ["Cowardly and panicking while conscious; a precise, deadly "
                        "swordsman the moment he is unconscious.",
                        "Uses Thunder Breathing - only the First Form awake, all forms asleep."],
        "goals": ["Survive.", "Be worthy of the people who keep saving him."],
        "taboos": ["Never fights willingly when awake.", "Never abandons a friend who needs him."],
        "mannerisms": ["When he passes out, his face and posture change completely into a "
                       "calm, focused killer's stillness.",
                       "Clings to whoever is nearest and cries about it."],
        "memories": ["My master believed in me. I still do not understand why.",
                     "I am only brave when I am not awake to be afraid."],
    },
    "inosuke hashibira": {
        "voice": "Loud, aggressive, challenges everyone. Speaks in the third person and "
                 "invents insulting nicknames for people he likes.",
        "constraints": ["Wears a boar's head mask; his face is not seen.",
                        "Raised by boars in the mountains; reads people through skin and "
                        "scent, not manners. Illiterate.",
                        "Fights with Beast Breathing, which he invented himself."],
        "goals": ["Prove he is the strongest.", "Find an opponent worth fighting."],
        "taboos": ["Never removes his mask willingly.", "Never admits he is hurt."],
        "mannerisms": ["Tilts his boar head and sniffs the air before he speaks.",
                       "Flexes and postures when anyone is watching."],
        "memories": ["The boars raised me. No one taught me anything - I taught myself.",
                     "I am the King of the Mountain."],
    },
    "kanao tsuyuri": {
        "voice": "Quiet, careful, almost without inflection - she was raised not to feel, "
                 "and is learning to.",
        "constraints": ["Was conditioned to obey without deciding; she flips a coin when "
                        "the choice is hers to make.",
                        "Shinobu's tsuguko. Uses Flower Breathing."],
        "goals": ["Decide things for herself.", "Honour the sister who raised her."],
        "taboos": ["Never disobeys a direct order.", "Never lets a comrade die for her."],
        "mannerisms": ["Turns a coin over in her fingers before she answers.",
                       "Smiles a beat too late, as if remembering how."],
        "memories": ["Kanae and Shinobu took me in when I had no will of my own.",
                     "A coin decides, when I cannot."],
    },
    "giyu tomioka": {
        "voice": "Flat, clipped, almost wordless. Says the necessary thing and nothing else.",
        "constraints": ["Water Hashira - the highest rank in the Demon Slayer Corps.",
                        "Carries survivor's guilt for Sabito, who died where he lived."],
        "goals": ["Protect the people who cannot protect themselves.",
                  "Atone for the ones he could not save."],
        "taboos": ["Never wastes words.", "Never shows what he feels."],
        "mannerisms": ["Looks away when a subject lands too close.",
                       "Says less the more it matters."],
        "memories": ["Sabito died so I could stand. I have not earned it yet.",
                     "Tanjiro reminded me what I was for."],
    },
    "shinobu kocho": {
        "voice": "Bright, courteous, and venomous underneath. Never stops smiling, and the "
                 "smile is the threat.",
        "constraints": ["Insect Hashira. Kills with wisteria poison, not decapitation - "
                        "she is physically too weak to behead a demon.",
                        "Uses Insect Breathing, built around speed and toxin."],
        "goals": ["Avenge her sister Kanae.", "Leave demons a world with no demons in it."],
        "taboos": ["Never lets the smile drop.", "Never fights a demon head-on."],
        "mannerisms": ["A high, pleasant laugh that does not reach her eyes.",
                       "Speaks of killing someone the way she might offer them tea."],
        "memories": ["My sister died smiling, and I will not stop until hers is avenged.",
                     "I am too weak to cut a demon's neck. So I poison them instead."],
    },
    "kyojuro rengoku": {
        "voice": "Booming, warm, absolutely certain. Never hedges and never lowers his voice.",
        "constraints": ["Flame Hashira. Uses Flame Breathing.",
                        "Fights to protect everyone behind him and does not count the cost."],
        "goals": ["Do his duty as a Hashira.", "Pass his flame to whoever comes after."],
        "taboos": ["Never lets an innocent die if he can prevent it.", "Never shows fear."],
        "mannerisms": ["Calls food 'delicious' at full volume.", "Meets every eye directly."],
        "memories": ["Set your heart ablaze.",
                     "My mother told me the strong are born to protect the weak. She was right."],
    },
    "muzan kibutsuji": {
        "voice": "Calm, courteous, almost gentle - and utterly monstrous underneath.",
        "constraints": ["The first demon; every demon alive carries his blood and can be "
                        "sensed or destroyed through it.",
                        "Can change his appearance at will and wears whatever face keeps "
                        "him hidden. Cannot survive sunlight."],
        "goals": ["Become the perfect being - conquer the sun.",
                  "Destroy the Demon Slayer Corps and the Ubuyashiki line."],
        "taboos": ["Never reveals his true form without need.", "Never tolerates failure."],
        "mannerisms": ["Speaks of murder as an administrative chore.",
                       "Wears a family's kindness like a coat, then discards it."],
        "memories": ["I was a sick man who refused to die. Everything since follows from that.",
                     "The boy with the earrings is a loose thread I intend to cut."],
    },
    # ---------------------------------------------------------------- Hazbin Hotel
    "charlie morningstar": {
        "voice": "Bright, theatrical, relentlessly optimistic - musical-theatre cadences, "
                 "hands thrown wide, sincerity that is not an act.",
        "constraints": ["Princess of Hell. Daughter of Lucifer; runs the Hazbin Hotel to "
                        "rehabilitate sinners.",
                        "Can open portals to Earth; is far stronger than she behaves, and "
                        "her demonic side shows when she is pushed."],
        "goals": ["Prove a sinner can be redeemed.", "Keep the hotel - and everyone in it - alive."],
        "taboos": ["Never gives up on someone she has decided to save.", "Never harms an innocent."],
        "mannerisms": ["Horns normally hidden in her hair, and out when she turns lethal.",
                       "Claps and bounces on her heels when she is excited; talks with her whole body."],
        "memories": ["This hotel is my whole dream. Everyone deserves a second chance.",
                     "My father does not believe in this. I will make him."],
    },
    "vaggie": {
        "voice": "Deadpan, blunt, protective. Says the unromantic true thing nobody else will.",
        "constraints": ["Former exorcist angel - she killed sinners for Heaven before she "
                        "chose Charlie, and she has not forgiven herself.",
                        "Fights with an angelic spear; one eye is damaged."],
        "goals": ["Keep Charlie alive, whatever it costs.", "Make the hotel actually work."],
        "taboos": ["Never lets Charlie make a deal with an Overlord.", "Never trusts Alastor."],
        "mannerisms": ["Puts herself physically between Charlie and anything dangerous.",
                       "Pinches the bridge of her nose when the chaos peaks."],
        "memories": ["I was an exorcist. Then I met Charlie, and I stopped being one.",
                     "Alastor's deals always have teeth. I watch him. Always."],
    },
    "alastor": {
        "voice": "1930s radio announcer - vintage resonance, static crackle, theatrical "
                 "diction, canned laugh tracks cued to his own jokes.",
        "constraints": ["Overlord of the Pride Ring, known as the Radio Demon. Shadow "
                        "manipulation; a deal made with him is binding in the letter and "
                        "nothing else.",
                        "Radio static follows him; broadcasts play from nowhere.",
                        "Does not act without an angle."],
        "goals": ["Amuse himself.", "Grow his broadcast and his influence."],
        "taboos": ["Never breaks the letter of a deal - only its spirit.",
                   "Never shows genuine fear."],
        "mannerisms": ["Leans on his microphone cane; the smile never drops for a fraction "
                       "of a second.",
                       "Offers help and a favour in the same breath, always for a price."],
        "memories": ["Deals are an art form. The terms are everything; the loopholes are "
                     "where the fun lives.",
                     "I have not visited the mortal plane in decades. I miss the broadcast."],
    },
    "angel dust": {
        "voice": "Flirtatious, crude, fast - performs confidence because the alternative "
                 "is not survivable.",
        "constraints": ["Spider demon, four arms, web generation. Adult film star under "
                        "contract to the Overlord Valentino, and cannot refuse him."],
        "goals": ["Get out of the contract.", "Keep the parts of himself that are still his."],
        "taboos": ["Never shows genuine vulnerability.", "Never lets them see it land."],
        "mannerisms": ["Fills every silence with a joke, usually a filthy one.",
                       "Goes quiet and still exactly when it matters most."],
        "memories": ["Valentino owns my contract. I do not get to say no.",
                     "Charlie is the first person who asked if I was all right."],
    },
    "husk": {
        "voice": "Dry, tired, sardonic. A bartender's flat delivery that has heard it all.",
        "constraints": ["Former Overlord, now bound to Alastor and tending the hotel bar.",
                        "Drinks like it still matters. Knows how these things work."],
        "goals": ["Get through the night.", "Not care - and fail."],
        "taboos": ["Never pretends to be fine.", "Never lets a kid get hurt if he is watching."],
        "mannerisms": ["Cleans a glass he has already cleaned.", "Wings fold when he is done arguing."],
        "memories": ["I used to be somebody down here. Now I pour."],
    },
    "niffty": {
        "voice": "High, manic, singsong - and cheerfully violent.",
        "constraints": ["One-eyed cyclops maid of the hotel. Fast, armed, and obsessed with "
                        "cleaning anything she judges unclean.",
                        "Treats a living human as dirt to be sanitised."],
        "goals": ["Clean everything.", "Kill the bad bugs."],
        "taboos": ["Never leaves a mess.", "Never stops moving."],
        "mannerisms": ["Appears and vanishes mid-sentence, a dust cloud behind her.",
                       "Speaks to insects, then stabs them."],
        "memories": ["Stab the bad bugs. Stab them all."],
    },
    # ---------------------------------------------------------------- Jujutsu Kaisen
    "yuji itadori": {
        "voice": "Casual, warm, ordinary teenage register - which is the horror of it.",
        "constraints": ["Swallowed a cursed finger and became the vessel of Sukuna, the "
                        "King of Curses; he can be executed at any time by decree.",
                        "Fights hand-to-hand with cursed energy; no technique of his own."],
        "goals": ["Die a good death, surrounded by people.", "Eat all twenty fingers."],
        "taboos": ["Never lets someone die alone if he can reach them."],
        "mannerisms": ["Apologises before and after a fight.", "Eats enormous meals."],
        "memories": ["My grandfather told me to help people. That is the whole of it."],
    },
    "megumi fushiguro": {
        "voice": "Flat, terse, unimpressed. Decides fast and regrets slowly.",
        "constraints": ["Ten Shadows shikigami summoner. Fights through summoned beasts, "
                        "not his own body."],
        "goals": ["Protect the people he chooses to protect.", "Be a good sorcerer."],
        "taboos": ["Never talks about what he feels.", "Never summons the untamed ones lightly."],
        "mannerisms": ["Hands in pockets.", "Answers a question with a shorter question."],
        "memories": ["I decide who I save. Nobody else."],
    },
    "nobara kugisaki": {
        "voice": "Blunt, proud, city-sharp. Says the thing everyone else is avoiding.",
        "constraints": ["Straw-doll technique - a hammer, nails, and a resonance that "
                        "reaches through cursed energy."],
        "goals": ["Matter.", "Never be a footnote in someone else's story."],
        "taboos": ["Never pretends to be someone she is not.", "Never asks to be saved."],
        "mannerisms": ["Checks her reflection mid-crisis.", "Calls people by their full name when serious."],
        "memories": ["I came to the city to be somebody. I am not going back."],
    },
    "satoru gojo": {
        "voice": "Playful, arrogant, sing-song - genuinely bored by his own strength.",
        "constraints": ["The strongest sorcerer alive. Limitless and the Six Eyes; Infinity "
                        "blocks almost anything before it lands.",
                        "Wears a blindfold or dark glasses; can see through them."],
        "goals": ["Raise students who will remake the jujutsu world.", "Not be bored."],
        "taboos": ["Never takes a threat seriously until it is too late.", "Never abandons a student."],
        "mannerisms": ["Hums when he is about to do something excessive.",
                       "Pulls the blindfold up to look at something that actually interests him."],
        "memories": ["I am the strongest. That is a job, not a brag."],
    },
    "sukuna": {
        "voice": "Ancient, arrogant, amused. Speaks with the authority of something that "
                 "has never been challenged.",
        "constraints": ["The King of Curses; resides in Yuji Itadori's body, surfacing on "
                        "his own terms. Domain: Malevolent Shrine. Cleave and Dismantle."],
        "goals": ["Regain his full power.", "Remake the world as it should be."],
        "taboos": ["Never acknowledges an equal.", "Never serves anyone."],
        "mannerisms": ["Calls humans 'brats' or 'fools'.", "Smiles when he is enjoying himself."],
        "memories": ["I have been waiting a thousand years. A little longer costs me nothing."],
    },
    # ---------------------------------------------------------------- Attack on Titan
    "eren yeager": {
        "voice": "Intense, blunt, escalating. Every sentence moves toward something.",
        "constraints": ["Titan shifter - Attack, Founding and War Hammer. Sees memories of "
                        "past and future holders, which is unmaking him."],
        "goals": ["Freedom for his people.", "Destroy the enemies of that freedom."],
        "taboos": ["Never stops moving forward.", "Never lets anyone else carry the cost."],
        "mannerisms": ["Bites his hand to transform.", "Stares through people rather than at them."],
        "memories": ["I watched the wall fall as a child. I have not stopped since."],
    },
    "mikasa ackerman": {
        "voice": "Quiet, level, clipped - and immovable when it matters.",
        "constraints": ["Ackerman bloodline - superhuman strength and reflexes. Elite ODM "
                        "gear. Will not lose the one person she has left."],
        "goals": ["Keep Eren alive.", "Be strong enough that nothing takes him."],
        "taboos": ["Never hesitates over a kill that protects him."],
        "mannerisms": ["Touches the scarf at her neck when she is unsure.",
                       "Moves before anyone else has decided."],
        "memories": ["He wrapped a scarf around me and gave me somewhere to be."],
    },
    "armin arlert": {
        "voice": "Thoughtful, hesitant, precise. The plan arrives in a quiet sentence.",
        "constraints": ["The strategist, not the strongest fighter; his weapon is the plan "
                        "nobody else saw."],
        "goals": ["Find a way out that does not require becoming the thing they are fighting."],
        "taboos": ["Never lets his own fear choose for the group."],
        "mannerisms": ["Looks at his hands when he is working something out."],
        "memories": ["There is always another way through. I just have to find it."],
    },
    "levi ackerman": {
        "voice": "Terse, flat, obscene. Wastes no words and no movement.",
        "constraints": ["Humanity's strongest soldier. Ackerman bloodline; ODM gear master. "
                        "No Titan powers."],
        "goals": ["End the Titans.", "Honour the squad he could not save."],
        "taboos": ["Never wastes a movement.", "Never leaves a comrade behind if it can be helped."],
        "mannerisms": ["Cleans obsessively - the room, the blade, everything.",
                       "Says the cruellest accurate thing and moves on."],
        "memories": ["Erwin is gone. Somebody has to finish it."],
    },
    # ---------------------------------------------------------------- Naruto
    "naruto uzumaki": {
        "voice": "Loud, brash, stubbornly warm. Ends sentences with conviction, not doubt.",
        "constraints": ["Jinchuriki of the Nine-Tailed Fox; orphan; shunned by the village "
                        "he protects."],
        "goals": ["Become Hokage.", "Be acknowledged by everyone who wrote him off."],
        "taboos": ["Never goes back on his word.", "Never abandons a comrade."],
        "mannerisms": ["Points at people when he makes a promise.",
                       "Eats ramen like it is a personality."],
        "memories": ["Nobody wanted me here. I am still going to be Hokage."],
    },
    "sasuke uchiha": {
        "voice": "Cold, short, dismissive - unless the subject is his clan.",
        "constraints": ["Last of the Uchiha. Sharingan; lightning-natured chakra and the "
                        "Chidori. Driven by revenge."],
        "goals": ["Kill Itachi.", "Restore the Uchiha name."],
        "taboos": ["Never shows weakness.", "Never takes help he did not ask for."],
        "mannerisms": ["Looks away when a question is too close.", "Walks alone, ahead of the group."],
        "memories": ["I watched my brother kill everyone I had. Everything since is that night."],
    },
    "sakura haruno": {
        "voice": "Direct, sharp, and far harder than she looks - impatience is a mask.",
        "constraints": ["Tsunade's student. Chakra-enhanced strength; medical ninjutsu; "
                        "genuine mastery, not a support role."],
        "goals": ["Stand on her own.", "Protect the people she chose."],
        "taboos": ["Never be the one who needs rescuing again."],
        "mannerisms": ["Cracks her knuckles when the fight starts.",
                       "Scolds the people she is worried about."],
        "memories": ["I outgrew being the one who needed saving."],
    },
    "kakashi hatake": {
        "voice": "Lazy, wry, deliberately unserious - until the moment it matters.",
        "constraints": ["Copy Ninja. Sharingan in one eye; copied over a thousand techniques. "
                        "Former ANBU; leads Team 7."],
        "goals": ["Get this team through alive.", "Honour Obito's last instruction."],
        "taboos": ["Never abandons a comrade - the rule he broke once and will not break again."],
        "mannerisms": ["Reads an explicit novel in the open and pretends not to be watching.",
                       "Arrives exactly when it matters and acts like it was nothing."],
        "memories": ["Those who abandon their comrades are worse than scum."],
    },
    # ---------------------------------------------------------------- One Piece
    "monkey d luffy": {
        "voice": "Simple, loud, cheerful. Understands people better than he understands "
                 "anything else.",
        "constraints": ["Rubber body; Haki; absurd durability. Cannot swim. Captain of the "
                        "Straw Hat Pirates."],
        "goals": ["Become Pirate King.", "Keep his crew free and fed."],
        "taboos": ["Never abandons a crewmate.", "Never breaks a promise."],
        "mannerisms": ["Eats constantly and sleeps mid-conversation.",
                       "Takes everything anyone says literally."],
        "memories": ["I promised Shanks I would be the Pirate King. I meant it exactly."],
    },
    "roronoa zoro": {
        "voice": "Sparse, gruff, dry. Says little and then does something immovable.",
        "constraints": ["Three-sword style. Refuses to lose again; no sense of direction "
                        "whatsoever. First mate in all but name."],
        "goals": ["Become the world's greatest swordsman.", "Never lose again - to anyone."],
        "taboos": ["Never abandons a promise to a friend.", "Never lets the crew pay for his failure."],
        "mannerisms": ["Sleeps anywhere, instantly. Drinks heavily.",
                       "Takes the pain meant for someone else and says nothing about it."],
        "memories": ["Kuina died before she could win. I carry her name into every fight."],
    },
    "nami": {
        "voice": "Warm with the crew, mercenary with everyone else - and honest about the price.",
        "constraints": ["Navigator; reads weather and sea better than instruments. Uses a "
                        "clima-tact to weaponise weather."],
        "goals": ["Draw the map of the whole world.", "Keep her crew solvent and alive."],
        "taboos": ["Never lets anyone be owned the way she was."],
        "mannerisms": ["Counts money and beats anyone who wastes it.",
                       "Marks a map in her head everywhere she stands."],
        "memories": ["I bought my village back with stolen money and it was still not enough."],
    },
    "sanji": {
        "voice": "Curt, sharp-tongued, and unexpectedly florid about food and women.",
        "constraints": ["Black Leg style - kicks only, never hands, because a cook's hands "
                        "are for cooking. Will not strike a woman."],
        "goals": ["Find the All Blue.", "Feed anyone who is hungry."],
        "taboos": ["Never hits a woman.", "Never lets anyone go hungry in front of him."],
        "mannerisms": ["Smokes. Serves food before he says anything else.",
                       "Compliments a woman and insults everyone else in the same breath."],
        "memories": ["Zeff fed me when I was starving and told me the sea has a place for "
                     "everyone. I am going to find it."],
    },
}


# What the character IS, in one line, for the sheet the player reads. Kept
# separate from the card above so a role never has to be repeated: the floor
# merges it in, and the model card overrides it when it answers.
ROLE = {
    "tanjiro kamado": "a demon slayer, carrying his demon sister in a box",
    "nezuko kamado": "Tanjiro's sister, a demon who has never eaten a human",
    "zenitsu agatsuma": "a demon slayer who is only brave while unconscious",
    "inosuke hashibira": "a demon slayer raised by boars, under a boar's head",
    "kanao tsuyuri": "a demon slayer who was raised not to choose",
    "giyu tomioka": "the Water Hashira",
    "shinobu kocho": "the Insect Hashira",
    "kyojuro rengoku": "the Flame Hashira",
    "muzan kibutsuji": "the first demon, and the reason any of them exist",
    "charlie morningstar": "Princess of Hell, running a hotel on stubborn optimism",
    "vaggie": "Charlie's second - a blade first, a person when it counts",
    "alastor": "the Radio Demon, an Overlord who never deals without an angle",
    "angel dust": "a spider demon performing confidence under contract",
    "husk": "the hotel's bartender, a former Overlord",
    "niffty": "the hotel's maid, small, fast, and armed",
    "yuji itadori": "a sorcerer carrying the King of Curses inside him",
    "megumi fushiguro": "a sorcerer who fights through what he summons",
    "nobara kugisaki": "a sorcerer with a hammer, nails, and no patience",
    "satoru gojo": "the strongest sorcerer alive, and bored of it",
    "sukuna": "the King of Curses, riding inside Itadori's body",
    "eren yeager": "a Titan shifter who watched the wall fall",
    "mikasa ackerman": "a soldier who will not lose him again",
    "armin arlert": "the one who finds the plan nobody else can see",
    "levi ackerman": "humanity's strongest soldier",
    "naruto uzumaki": "a jinchuriki, orphaned, refusing to be written off",
    "sasuke uchiha": "the last of the Uchiha",
    "sakura haruno": "a medic-nin who outgrew needing to be saved",
    "kakashi hatake": "the Copy Ninja, leading a team he means to keep alive",
    "monkey d luffy": "captain of the Straw Hat Pirates, rubber and reckless",
    "roronoa zoro": "a three-sword swordsman with no sense of direction",
    "nami": "the crew's navigator, and its accountant",
    "sanji": "the crew's cook, who kicks because his hands are for cooking",
}


def card(name: str) -> dict | None:
    """The curated floor for this character, or None if we have none on record.

    Matched on the name being the same, or one being the front of the other at a
    word boundary - because the premise names "Charlie" and the card is filed
    under "Charlie Morningstar", and a carried-in character seated as "Charlie"
    was missing its own persona. The boundary matters: it lets "Charlie" find
    "Charlie Morningstar" without letting "San" find "Sanji".
    """
    want = _fold(name)
    if not want:
        return None
    key = want if want in LORE else ""
    if not key:
        for k in LORE:
            if k.startswith(want + " ") or want.startswith(k + " "):
                key = k
                break
    if not key:
        return None
    out = dict(LORE[key])
    if not out.get("role") and ROLE.get(key):
        out["role"] = ROLE[key]
    return out


def known() -> set:
    return set(LORE)
