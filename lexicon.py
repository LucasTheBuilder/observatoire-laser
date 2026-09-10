"""Lexique métier de l'observatoire : les labels du domaine et la mécanique qui les reconnaît.

Extrait de ``scrapers.py``. Ces définitions n'ont rien de propre au crawl -- elles décrivent le
vocabulaire du laser ultra-rapide (marchés, composants, opérations, procédés, matériaux,
maturité) et la façon sûre de le retrouver dans un texte. Elles étaient pourtant enfermées dans
le crawler, obligeant ``capabilities.py`` et ``cordis.py`` à importer des fonctions privées
(``_match_all_labels``, ``_detect_maturity``, ``_match_label_details``, ``_quote``) d'un module
de 3 200 lignes qui ne les concernait pas.

Les noms publics en fin de fichier sont l'API destinée aux autres modules ; les noms préfixés
d'un underscore restent ceux qu'utilise ``scrapers.py`` en interne, inchangés pour ne pas
toucher à son code ni à celui des tests.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# === Bloc 2/6 : lexiques métier (dictionnaires de règles texte -> libellé canonique) ===
# Chaque lexique associe un libellé "propre" (ex: "Médical") à une règle (LexiconRule) qui
# décrit comment le reconnaître dans un texte brut :
#   - any_of: le libellé matche si AU MOINS UN des termes est présent
#   - all_of: le libellé matche seulement si TOUS les termes sont présents
#   - regex: motif regex alternatif (utile pour un sigle comme "SLE", "TGV"...)
#   - requires_any: garde-fou -- même si any_of/regex matche, le fait ne compte que si un des
#     mots de ce groupe est AUSSI présent (évite les faux positifs sur un sigle trop court)
#   - exclude: annule le match si un de ces termes est présent
# Voir _rule_match_terms() pour l'implémentation exacte de ces clés, et _match_label_details()
# pour comment on choisit le meilleur libellé quand plusieurs règles matchent à la fois.
# A lexicon rule maps a few well-known keys (any_of/all_of/regex/requires_any/exclude) to
# tuples of terms/patterns. Annotating the lexicons below lets mypy check every call site
# that takes a lexicon (_rule_match_terms, _match_label, _match_all_labels, ...) instead of
# widening them all to plain dicts.
LexiconRule = dict[str, tuple[str, ...]]
Lexicon = dict[str, LexiconRule]

# Vocabulaire laser : sert de "garde d'entrée" (_laser_match) -- une page/bloc doit contenir
# au moins un de ces termes pour être considéré comme pertinent au domaine (ultra-rapide/
# femtoseconde), avant même de chercher un marché/composant/opération.
LASER_RULES: Lexicon = {
    "femtosecond": {"any_of": ("femtosecond", "femtoseconde")},
    "fs laser": {"regex": (r"\bfs[ -]?laser\b",)},
    "ultrafast": {"any_of": ("ultrafast",)},
    "ultrashort pulse": {"any_of": ("ultra-short pulse", "ultrashort pulse", "ultrashort-pulse", "ultra short pulse", "ultrashort")},
    "USP laser": {"regex": (r"\busp(?:[ -]?laser)?\b",), "requires_any": ("laser", "pulse", "machining", "processing")},
    "UKP laser": {"regex": (r"\bukp(?:[ -]?laser)?\b",), "requires_any": ("laser", "pulse", "bearbeitung")},
    "Ultrakurzpulslaser": {"any_of": ("ultrakurzpulslaser", "ultrakurzpuls laser")},
}

# Marchés/secteurs applicatifs finaux (une des 3 dimensions "core" d'un fait marché, avec
# COMPONENTS et OPERATIONS -- voir _candidate()).
MARKETS: Lexicon = {
    "Médical": {"any_of": ("medical", "medtech", "surgical", "healthcare", "biomedical")},
    "Batteries": {"any_of": ("battery", "batteries", "energy storage", "battery cell")},
    "Optique": {"any_of": ("optical", "optique", "lens", "lenses", "optics")},
    "Semi-conducteurs": {"any_of": ("semiconductor", "semi-conducteur", "microelectronics", "microélectronique")},
    "Aéronautique": {"any_of": ("aeronautic", "aeronautical", "aviation", "aircraft", "aerospace")},
    "Spatial": {"any_of": ("spacecraft", "satellite", "space propulsion", "space industry", "spatial")},
    "Défense": {"any_of": ("defence", "defense", "military", "défense")},
    "Automobile": {"any_of": ("automotive", "automobile", "e-mobility", "electric vehicle")},
    "Luxe": {"any_of": ("luxury", "luxe", "horlogerie", "watchmaking")},
    "Quantum": {"any_of": ("quantum", "ion trap", "ion traps", "quantum computing", "quantum sensing", "quantum cryptography")},
    "Photonique": {"any_of": ("photonic", "photonics", "photonique")},
    "Sciences de la vie": {"any_of": ("life sciences", "drug discovery", "cell therapy", "cell therapies", "antibody isolation", "single-cell analysis", "single cell analysis", "biophotonics")},
    "Photovoltaïque": {"any_of": ("photovoltaic", "photovoltaics", "solar cell", "solar cells", "pv cell", "photovoltaïque")},
    # Kept separate from Batteries/Photovoltaïque (same granularity as those two) rather than
    # merged into a broader "Énergie" label, to avoid touching the fact_key of existing rows.
    "Hydrogène": {"any_of": ("hydrogen", "hydrogène", "electrolyzer", "electrolyser", "électrolyseur", "fuel cell", "pile à combustible", "power-to-gas")},
}

# Composants/objets physiques fabriqués ou traités (2e dimension "core").
COMPONENTS: Lexicon = {
    "Composants en Nitinol pour cathéters": {"all_of": ("nitinol", "catheter")},
    "Lentilles intraoculaires (IOL)": {"any_of": ("intraocular lens", "intraocular lenses"), "regex": (r"\biol\b",)},
    "Stents": {"any_of": ("stent",)},
    "Cathéters": {"any_of": ("catheter", "cathéter")},
    "Guidewires": {"any_of": ("guidewire", "guide wire")},
    "Aiguilles médicales": {"any_of": ("medical needle", "surgical needle", "needle")},
    "Implants": {"any_of": ("implant",)},
    "Électrodes de batteries": {"any_of": ("battery electrode", "electrode", "électrode")},
    "Collecteurs de courant": {"any_of": ("current collector", "battery foil", "electrode foil", "busbar", "battery tab")},
    "Wafers": {"any_of": ("semiconductor wafer", "silicon wafer", "glass wafer", "wafer")},
    "Interposeurs en verre": {"any_of": ("glass interposer", "glass interposer substrate")},
    "Substrats": {"any_of": ("glass substrate", "ceramic substrate", "silicon substrate", "substrate")},
    "Packaging avancé": {"any_of": ("advanced packaging", "semiconductor package", "chip package")},
    "MEMS": {"regex": (r"\bmems\b",)},
    "MicroLED": {"any_of": ("microled", "micro-led")},
    "PCB": {"any_of": ("printed circuit board",), "regex": (r"\bpcb\b",)},
    "Microcanaux": {"any_of": ("microchannel", "micro-channel", "microcanal")},
    "Dispositifs microfluidiques": {"any_of": ("microfluidic device", "microfluidic chip", "lab-on-chip", "lab on chip")},
    "Composants en verre": {"any_of": ("glass component", "composant en verre", "microstructured glass", "fused silica", "borosilicate glass")},
    "Fibres optiques": {"any_of": ("optical fiber", "optical fibre")},
    "Guides d'onde": {"any_of": ("waveguide", "wave guide")},
    "Buses": {"any_of": ("nozzle", "buse")},
    "Injecteurs": {"any_of": ("injector", "injecteur")},
    "Aubes / composants turbine": {"any_of": ("turbine blade", "turbine component", "aube")},
    "Capteurs": {"any_of": ("sensor", "capteur")},
    "Pièges à ions": {"any_of": ("ion trap", "ion traps", "piège à ions", "pièges à ions")},
    "Connectique": {"any_of": ("connector", "electrical connector", "connectique", "interconnect")},
    # Bare "resistor"/"capacitor"/"inductor" would over-match unrelated electronics prose;
    # kept to compound phrases that are specific to this discrete-component category.
    "Composants passifs": {"any_of": ("passive component", "composant passif", "surface mount component", "smd component")},
    "Optique intégrée": {"any_of": ("integrated optics", "integrated photonics", "optique intégrée", "photonic integrated circuit")},
    # "display"/"écran" alone are too generic (matches "displays excellent properties" etc.) --
    # compound phrases only.
    "Composants d'affichage": {"any_of": ("display panel", "microdisplay", "micro-display", "display glass", "cover glass display")},
    "Moules et outillage de précision": {"any_of": ("mold", "molds", "moule", "moules", "injection mold", "tooling insert", "outillage de précision")},
    # Chantier 2 item 5 : le lexique composants était le premier facteur de perte de l'audit
    # (missing_component = 332/386 blocs laser rejetés sur un run). Entrées ajoutées ci-dessous,
    # choisies pour couvrir des familles de composants déjà bien établies dans l'industrie du
    # micro-usinage laser ultra-rapide mais absentes du lexique initial (médical implantable,
    # semi-conducteurs, énergie, optique de précision, horlogerie) plutôt que de fabriquer une
    # terminologie -- ce sont des catégories génériques, pas des affirmations sur un acteur.
    "Boîtiers de dispositifs implantables": {"any_of": ("pacemaker housing", "pacemaker can", "icd housing", "implantable device housing", "boîtier de pacemaker")},
    "Marqueurs radio-opaques": {"any_of": ("radiopaque marker", "radiopaque markers", "marqueur radio-opaque")},
    "Micro-aiguilles": {"any_of": ("microneedle", "microneedles", "micro-aiguille", "micro-aiguilles")},
    "Lentilles de contact": {"any_of": ("contact lens", "contact lenses", "lentille de contact")},
    "Composants d'audioprothèses": {"any_of": ("hearing aid component", "hearing aid shell", "audioprothèse")},
    "Vias traversants (TSV)": {"any_of": ("through-silicon via", "through silicon via", "via traversant"), "regex": (r"\btsv\b",)},
    "Photomasques": {"any_of": ("photomask", "photomasks", "masque photolithographique")},
    "Puces RFID": {"regex": (r"\brfid\b",)},
    "Capteurs d'image": {"any_of": ("image sensor", "cmos sensor", "ccd sensor", "capteur d'image")},
    "Séparateurs de batteries": {"any_of": ("battery separator", "separator film", "séparateur de batterie")},
    "Cellules photovoltaïques": {"any_of": ("solar cell", "solar cells", "photovoltaic cell", "cellule photovoltaïque")},
    "Plaques bipolaires": {"any_of": ("bipolar plate", "bipolar plates", "plaque bipolaire")},
    "Membranes électrolytiques": {"any_of": ("electrolyte membrane", "membrane electrode assembly", "membrane électrolytique")},
    "Réseaux de diffraction": {"any_of": ("diffraction grating", "diffraction gratings", "réseau de diffraction")},
    "Micro-lentilles": {"any_of": ("microlens", "microlenses", "micro-lentille", "micro-lentilles", "lens array", "microlens array")},
    "Miroirs de précision": {"any_of": ("precision mirror", "precision mirrors", "miroir de précision")},
    "Éléments optiques diffractifs (DOE)": {"any_of": ("diffractive optical element", "diffractive optical elements"), "regex": (r"\bdoe\b",), "requires_any": ("laser", "optic", "optique", "diffract")},
    "Composants horlogers": {"any_of": ("watch movement", "watch component", "composant horloger", "mouvement horloger")},
    "Boîtiers de montres": {"any_of": ("watch case", "watch casing", "boîtier de montre")},
    "Cadrans de montres": {"any_of": ("watch dial", "watch dials", "cadran de montre")},
    "Résonateurs": {"any_of": ("resonator", "resonators", "résonateur", "résonateurs"), "requires_any": ("laser", "photonic", "optical", "optique", "quantum", "microwave")},
    "Boucliers thermiques": {"any_of": ("heat shield", "heat shields", "bouclier thermique")},
    "Puces photoniques": {"any_of": ("photonic chip", "photonic chips", "puce photonique")},
}

# Opérations/procédés laser appliqués au composant (3e dimension "core" -- un fait marché
# valide requiert un market + un component + une operation trouvés dans la même "fenêtre" de
# texte, voir _relation_evidence).
OPERATIONS: Lexicon = {
    "Micro-usinage": {"any_of": ("micromachining", "micro-machining", "micro machining")},
    "Microdécoupe": {"any_of": ("microcutting", "micro-cutting", "laser cutting", "microdécoupe", "découpe laser", "tube cutting",
                            "cutting quality", "laser singulation", "singulation", "slicing", "laser slicing")},
    # Le vocabulaire TGV est ici plutôt que dans une opération à lui : un via traversant est un
    # TROU, et le percer est une opération de perçage. L'architecture "TGV" reste par ailleurs
    # dans APPLICATION_ARCHITECTURES -- un même document parle des deux, la structure obtenue et
    # le geste qui l'obtient, et les deux dimensions ont le droit de partager des termes (seuls
    # les LIBELLÉS doivent rester disjoints).
    "Microperçage": {"any_of": (
        "microdrilling", "micro-drilling", "laser drilling", "microperçage", "perçage laser",
        "drilling", "micro-hole", "microhole",
        "through glass via", "through-glass via", "through glass vias", "through-glass vias",
        "through via in glass", "through vias in glass", "through-via in glass", "through-vias in glass",
        "via in glass", "vias in glass", "glass via", "glass vias", "through hole", "through holes",
    ), "regex": (r"\bTGVs?\b",), "requires_any": (
        "laser", "glass", "silica", "via", "hole", "interposer", "drilling", "perçage",
    )},
    # A absorbé "Texturation de surface" le 10/09/2026, comme "Soudage" la veille : cet axe,
    # écrit dans PROCESS_TECHNOLOGIES, faisait double emploi avec celui-ci. Seules les deux
    # formes "microstructuring" manquaient ici -- "texturing" et "surface structuring" y étaient
    # déjà, ce qui rendait la redondance quasi totale.
    "Texturation": {"any_of": (
        "texturing", "surface texturing", "texturation", "surface structuring", "structuration",
        "microstructuring", "micro-structuring",
    )},
    # Les termes de PROPRIÉTÉ (superhydrophobe, mouillabilité, biointerface...) ont d'abord été
    # écrits comme un axe séparé dans PROCESS_TECHNOLOGIES le 09/09/2026, avant de constater que
    # ce libellé existait déjà ici. Fusionnés plutôt que laissés en double : un même libellé dans
    # deux vocabulaires se serait affiché deux fois sur la page, et aurait faussé tout comptage
    # par famille. "superhydrophobic" est listé à part de "hydrophobic surface" -- _term_pattern
    # pose une frontière (?<!\w) que le préfixe "super" casse.
    "Fonctionnalisation de surface": {"any_of": (
        "surface functionalization", "surface functionalisation", "functional surface",
        "functionalized surface", "functionalised surface",
        "superhydrophobic", "superhydrophilic", "hydrophobic surface", "hydrophilic surface",
        "wettability", "wetting behavior", "biofunctional", "biointerface", "biointerfaces",
        "anti-icing", "self-cleaning surface", "antibacterial surface", "contact angle",
    )},
    "Ablation": {"any_of": ("ablation", "selective ablation")},
    # A absorbé "Soudage / assemblage de transparents" le 10/09/2026 : cet axe, écrit la veille
    # dans PROCESS_TECHNOLOGIES, faisait double emploi avec celui-ci et s'affichait à côté de
    # lui dans deux groupes de facettes voisins. "welding" couvrait déjà le gros du corpus ; ce
    # qui manquait ici, ce sont les formes que le mot seul ne matche pas (weld glass) et
    # l'assemblage sans fusion (dissimilar bonding).
    "Soudage": {"any_of": (
        "welding", "soudage", "micro-welding", "microwelding",
        "weld glass", "transparent welding", "dissimilar bonding",
    )},
    # "etching" manquait : le lexique ne connaissait que "engraving"/"gravure", donc aucun
    # titre en "laser-induced chemical etching" ou "selective etching" n'était classé -- alors
    # que c'est la formulation courante pour le verre et le silicium. C'est l'opération réelle
    # derrière l'ancien libellé "Écriture de guide d'onde" (10/09/2026), qui nommait un produit.
    # Le GESTE d'écrire dans la matière au faisceau, à distinguer de la pièce obtenue :
    # "Écriture de guide d'onde" a été retiré le 10/09/2026 parce qu'il nommait un produit, mais
    # l'opération elle-même manquait alors au lexique. Six publications du corpus l'emploient,
    # dont quatre n'avaient aucune opération.
    "Écriture directe": {"any_of": (
        "direct laser writing", "laser direct writing", "direct writing", "laser writing",
        "femtosecond laser writing", "écriture directe",
    )},
    "Nanostructuration": {"any_of": (
        "nanostructure", "nanostructures", "nanostructuring", "nanopatterning",
        "sub-wavelength patterning", "nanograting", "nanogratings",
    )},
    "Gravure": {"any_of": ("engraving", "gravure", "etching", "laser etching", "chemical etching", "selective etching")},
    "Dicing": {"any_of": ("laser dicing", "stealth dicing", "dicing")},
    "Scribing": {"any_of": ("laser scribing", "scribing")},
    "Écriture de guide d'onde": {"any_of": ("waveguide writing", "direct laser writing of waveguide")},
    # Ajouté le 10/09/2026 : "scribing" a été retiré comme libellé (c'est le mot anglais du
    # marquage, pas une opération française), mais le marquage lui-même manquait au lexique.
    "Marquage": {"any_of": ("laser marking", "marquage laser", "marquage", "laser marker", "annealing marking", "color marking")},
    "Nettoyage": {"any_of": ("laser cleaning", "nettoyage laser")},
    "Polissage": {"any_of": ("laser polishing", "polishing")},
    # "volumetric scribing" ajouté ici et non dans "Marquage" : le seul document du corpus qui
    # employait "scribing" fait du scribing VOLUMIQUE du verre au faisceau de Bessel, c'est-à-dire
    # une modification interne servant à séparer -- pas un marquage de surface. Le mot anglais
    # "scribing" couvre les deux gestes, le français les distingue.
    "Modification interne": {"any_of": ("internal modification", "in-volume modification", "volume modification", "bulk modification", "volumetric scribing")},
    "Debonding": {"any_of": ("laser debonding", "debonding")},
    "Rainurage": {"any_of": ("grooving", "laser grooving")},
    "Milling": {"any_of": ("laser milling", "micromilling", "micro-milling")},
    "Fabrication additive": {"any_of": ("additive manufacturing", "laser additive manufacturing", "directed energy deposition", "fabrication additive", "metal 3d printing")},
    "Tournage laser": {"any_of": ("laser turning", "tournage laser")},
    "Micro-assemblage": {"any_of": ("micro-assembly", "micro assembly", "micro-assemblage", "die attach", "wire bonding", "flip-chip")},
}

# Dimensions "complémentaires" (facultatives, jamais requises pour valider un fait) : elles
# enrichissent le fait mais ne peuvent jamais se substituer à market/component/operation
# (voir _candidate(): "Complementary dimensions ... can never substitute a core one").
# --- Dimension 1 : le PROCÉDÉ TECHNOLOGIQUE ---------------------------------------------
#
# Ce que le laser fait subir à la matière, par un mécanisme physique NOMMÉ. Séparé des
# capacités machine le 10/09/2026 : les deux vivaient dans la même table sous l'étiquette
# "axe technologique", qui répondait donc à deux questions différentes.
#
# La séparation n'est pas cosmétique, elle rend lisible une structure du corpus : mesuré sur
# les 98 lignes, les procédés ne sortent QUE de publications (SLE, LIPSS, DLIP, Bulk : 10
# publications, 0 projet) et les capacités machine quasi exclusivement de projets européens
# (Haute puissance, Multi-beam, Roll-to-roll : 0 publication, 7 projets). Un article décrit un
# mécanisme, un projet finance une machine -- voir le bloc de lecture en bas de la page
# Technologie laser, qui recalcule ce contraste à chaque rendu.
PROCESS_TECHNOLOGIES: Lexicon = {
    "SLE": {"any_of": ("selective laser etching", "selective laser-induced etching", "selective laser induced etching", "laser assisted etching", "laser-assisted etching", "isle process"), "regex": (r"\bSLE\b",), "requires_any": ("laser", "etching", "glass", "silica")},
    # LSFL et HSFL (Low/High Spatial Frequency LIPSS) étaient deux libellés frères, et n'ont
    # jamais classé un seul document : ce sont des sous-types de LIPSS, et leurs acronymes
    # n'apparaissent pas dans les titres. Repliés ici le 10/09/2026 plutôt que laissés vides.
    "LIPSS": {"any_of": ("laser-induced periodic surface structures", "laser induced periodic surface structures"), "regex": (r"\bLIPSS\b", r"\bLSFL\b", r"\bHSFL\b")},
    "DLIP": {"any_of": ("direct laser interference patterning",), "regex": (r"\bDLIP\b",)},
    # "in-volume" est délibérément absent : _term_pattern autorise l'espace comme séparateur,
    # donc le terme matcherait "in volume production" -- une mention de maturité industrielle,
    # pas d'usinage en volume.
    # Catégorie unique pour la recherche fondamentale sur l'interaction laser-matière
    # (décision de Lucas, 10/09/2026). Ces travaux étudient comment la matière RÉPOND, pas
    # comment on la transforme : les mélanger aux publications de procédé fausse la lecture --
    # la moitié d'entre eux ressortaient sous "Ablation", à côté d'articles d'usinage.
    #
    # Générique par nécessité : "molecular dynamics", "heat transport" et "numerical simulation"
    # sont de la physique générale. Mesuré sur les 23 451 projets Horizon, ce vocabulaire seul
    # en ferait entrer 141 sans aucun terme ultra-rapide.
    "Interaction laser-matière": {"any_of": (
        "two-temperature model", "two temperature model", "molecular dynamics", "ablation threshold",
        "laser-material interaction", "laser material interaction", "laser-matter interaction",
        "laser interaction with", "electron-phonon", "heat transport", "incubation factor",
        "stress wave", "defect interaction", "beam degradation", "removal mechanisms",
        "numerical modelling", "numerical simulation",
    )},
    "Bulk": {"any_of": ("bulk modification", "bulk modifications", "bulk material modification", "volume modification", "refractive index modification", "bulk silicon", "bulk glass")},
    # "Fonctionnalisation de surface", "Soudage / assemblage de transparents" et "Texturation de
    # surface" ont vécu ici moins de deux jours : les trois libellés existaient déjà dans
    # OPERATIONS, qui a absorbé leurs termes. Un libellé présent dans deux vocabulaires
    # s'afficherait deux fois sur la page et fausserait le comptage par famille (voir le test de
    # disjonction). Leçon générale : chercher dans les six vocabulaires AVANT d'en écrire un.
}

# --- Dimension 2 : la CAPACITÉ MACHINE ---------------------------------------------------
#
# Ce dont la source, le faisceau, la ligne ou le pilotage sont capables -- indépendamment du
# procédé qu'on en tire. C'est le vocabulaire des projets européens, qui financent le
# développement d'une machine plutôt que la description d'un mécanisme.
#
# Ces libellés sont presque tous dans _GENERIC_PROCESS_AXES, et ce n'est pas un hasard : une
# capacité se décrit avec des mots ("process monitoring", "parallel processing", "burst mode")
# que d'autres industries emploient aussi. La frontière conceptuelle procédé/capacité et la
# frontière technique spécifique/générique décrivent presque la même chose.
MACHINE_CAPABILITIES: Lexicon = {
    "Burst GHz/MHz": {"any_of": ("ghz burst", "mhz burst", "burst mode", "burst regime", "burst regimes", "laser burst", "laser bursts", "single burst", "intra-burst")},
    "Beam shaping": {"any_of": ("beam shaping", "dynamic beam shaping", "programmable laser beam", "spatial light modulator", "adaptive optics beam")},
    "Haute puissance / hauts taux": {"any_of": ("high average power", "high repetition rate", "mhz processing", "high-throughput ablation")},
    "Multi-beam / parallélisation": {"any_of": ("multi-beam", "multibeam", "beam splitting", "diffractive optical element", "parallel processing")},
    "Monitoring IA procédé": {"any_of": ("process monitoring", "in-line monitoring", "digital twin", "data-driven process optimization", "process optimization ai", "closed-loop process control")},
    # Architecture de ligne plutôt que capacité de faisceau, et le seul libellé de cette table
    # à porter un marché dans son nom. Classé ici sur décision de Lucas (10/09/2026) : c'est
    # bien une capacité de la MACHINE, même si c'est la ligne entière et non la source.
    "Fabrication roll-to-roll (batteries)": {"any_of": ("roll-to-roll", "roll to roll", "r2r processing", "battery electrode manufacturing")},
}

# Vue fusionnée, pour les appelants qui raisonnent sur "l'axe technologique" au sens large et
# n'ont aucune raison de distinguer les deux : l'extraction de faits marché (scrapers._candidate)
# et la classification des projets CORDIS. Les scinder là-bas aurait retiré des libellés à ces
# deux chemins sans rien apporter -- seule la PAGE a besoin de la distinction.
TECHNOLOGY_AXES: Lexicon = {**PROCESS_TECHNOLOGIES, **MACHINE_CAPABILITIES}

APPLICATION_ARCHITECTURES: Lexicon = {
    # "through vias in glass" et ses variantes : la formulation employée quand le matériau est
    # rejeté après le nom du perçage plutôt qu'inséré dedans ("Customised through vias in glass
    # interposer production", Oxford Lasers). Aucune des quatre formes d'origine ne la matchait
    # -- _term_pattern joint les mots avec [\s-]+, donc "through glass via" exige que "glass"
    # soit entre les deux. Trou trouvé pendant l'audit du 09/09/2026, sur une publication réelle
    # du corpus. `requires_any` inchangé : il couvre déjà "glass"/"via"/"interposer".
    "TGV": {
        "any_of": ("through glass via", "through-glass via", "through glass vias", "through-glass vias",
                   "through via in glass", "through vias in glass", "through-via in glass", "through-vias in glass",
                   "via in glass", "vias in glass", "glass via", "glass vias"),
        "regex": (r"\bTGVs?\b",),
        "requires_any": ("glass", "via", "interposer", "semiconductor", "packaging"),
    },
}

MATERIALS: Lexicon = {
    "Verre": {"any_of": ("glass", "fused silica", "borosilicate", "quartz glass", "verre")},
    "Saphir": {"any_of": ("sapphire", "saphir")},
    "Silicium": {"any_of": ("silicon", "silicium")},
    "Nitinol": {"any_of": ("nitinol", "ni-ti", "niti")},
    "Céramique": {"any_of": ("ceramic", "alumina", "zirconia", "céramique", "céramiques")},
    "Polymère": {"any_of": ("polymer", "polymeric", "peek", "polyimide", "polymère", "polymères")},
    "Métal": {"any_of": ("stainless steel", "titanium", "aluminium", "aluminum", "copper", "nickel", "titane", "acier inoxydable", "cuivre", "métaux")},
    "Composite": {"any_of": ("composite", "cfrp", "carbon fiber reinforced polymer", "cmc", "ceramic matrix composite")},
    "Magnésium": {"any_of": ("magnesium", "magnésium")},
}

# Bénéfices/besoins client mis en avant (productivité, propreté du procédé...).
PERFORMANCE_TERMS: Lexicon = {
    "Productivité": {"any_of": ("high throughput", "throughput", "high-speed processing", "high speed processing", "large-area processing", "large area processing", "débit", "cadence de production", "cadence élevée")},
    "Parallélisation": {"any_of": ("parallel processing", "multibeam", "multi-beam", "beam splitting", "diffractive optical element", "polygon scanner")},
    "Haute puissance": {"any_of": ("high average power", "high-power ultrafast", "high power ultrafast", "high repetition rate", "mhz processing")},
    # The four below capture the customer's underlying industrial need/pain point (why the
    # process is wanted), not the laser's own spec -- a gap flagged by an external audit and
    # confirmed absent from this lexicon entirely (not just unmatched in current data).
    "Maîtrise thermique": {"any_of": ("heat affected zone", "heat-affected zone", "haz", "minimal thermal damage", "athermal processing", "cold ablation", "zone thermiquement affectée", "zone affectée thermiquement", "sans dommage thermique")},
    "Propreté du procédé": {"any_of": ("debris-free", "burr-free", "redeposition-free", "clean cut", "sans bavure", "sans débris", "propreté du perçage", "propreté de la découpe")},
    "Rugosité maîtrisée": {"any_of": ("low surface roughness", "surface roughness reduction", "faible rugosité", "état de surface", "smooth surface finish", "surface finish quality")},
    "Frottement maîtrisé": {"any_of": ("coefficient of friction", "friction reduction", "tribological", "tribologie", "frottement", "coefficient de frottement", "lubrication", "lubrification", "oil retention", "lubricant retention")},
    "Mouillabilité": {"any_of": ("wettability", "hydrophobic surface", "hydrophilic surface", "hydrophobe", "hydrophile", "contact angle", "wetting behavior")},
    # Bare "yield"/"intégration" would over-match (financial yield, software CI, vertical
    # integration...) -- kept to compound phrases specific to a production-line context.
    "Rendement de production": {"any_of": ("production yield", "process yield", "yield improvement", "rendement de production", "taux de rendement")},
    "Intégration procédé": {"any_of": ("process integration", "line integration", "system integration", "intégration en ligne", "intégration procédé", "intégration process")},
}

# Explicit negation/contrast markers. A sentence or window carrying one of these cannot
# establish a positive relation even when it lexically contains market+component+operation
# terms (e.g. "unlike laser cutting, we use..." or "n'offre pas de découpe laser pour...").
# Deliberately excludes ambiguous cues such as "without"/"sans": those are routinely used
# descriptively in this domain ("contactless cutting" / "découpe sans contact") rather than
# to negate the claim, and a false rejection there would just widen the recall gap further.
NEGATION_CUES = (
    "unlike", "contrairement à", "contrairement a",
    "rather than", "instead of", "plutôt que", "plutot que", "au lieu de",
    "no longer", "not yet", "not currently",
    "does not", "do not", "did not", "cannot", "can not", "will not",
    "doesn't", "don't", "didn't", "isn't", "aren't", "wasn't", "weren't",
    "won't", "can't", "couldn't", "wouldn't", "shouldn't",
    "ne propose pas", "n'offre pas", "ne fait pas", "ne fabrique pas", "ne fournit pas",
    "n'est pas encore", "ne sont pas encore",
)

# Contrastive markers introducing what the ACTOR'S COMPETITORS/predecessors do, not the actor
# itself (§10.6 audit veille, 30/08/2026, cas Pulsar Photonics: "With the classic laser dicing
# [...] are mostly used wafer saws or laser-based fixed optics systems" attribuait à Pulsar une
# opération décrite comme celle du repoussoir dont il se démarque). "unlike"/"instead of" sont
# déjà dans NEGATION_CUES ; le reste complète la liste donnée par l'audit. Vérifié avec le même
# garde que la négation (_is_negated), pas séparément.
CONTRAST_CUES = (
    "classic", "classique", "conventional", "conventionnel", "conventionnelle",
    "traditional", "traditionnel", "traditionnelle", "whereas", "herkömmlich", "herkommlich",
)

# Conservative market inference used only for display when the market is not explicit.
# Inferred values never count as an independent acceptance signal.
MARKET_INFERENCE = {
    "Médical": {"components": {"Stents", "Cathéters", "Guidewires", "Aiguilles médicales", "Lentilles intraoculaires (IOL)", "Composants en Nitinol pour cathéters"}},
    "Batteries": {"components": {"Électrodes de batteries", "Collecteurs de courant"}},
    "Semi-conducteurs": {"components": {"Wafers", "Interposeurs en verre", "Packaging avancé", "MEMS", "MicroLED", "PCB"}, "architectures": {"TGV"}},
}

# Market labels that overlap so heavily in ordinary industry prose (e.g. "optical fiber" and
# "photonics" describing the very same application) that co-occurrence is not a signal of two
# independent applications. Without this, _relation_window_is_ambiguous rejects a large share of
# genuinely direct photonics-market sentences purely because they also contain an "optical" word.
# Kept deliberately small and manually curated -- unlike MARKET_INFERENCE this has no component
# anchor to verify against, so a cluster is only safe when its members are near-synonyms.
MARKET_SYNONYM_CLUSTERS = (
    frozenset({"Optique", "Photonique"}),
)

# Niveaux de maturité industrielle, du plus mature (Production) au moins mature (R&D) --
# _detect_maturity() parcourt cette liste DANS L'ORDRE et retourne le premier stage dont un
# terme apparaît dans le texte, donc l'ordre encode une priorité : si un texte mentionne à la
# fois "prototype" et "production en série", "Production" gagne. Chaque règle associe un
# stage précis à un bucket large ("existing" = déjà en production, "radar" = pas encore).
MATURITY_RULES = (
    ("Production", "existing", ("mass production", "volume production", "series production", "serial production", "production industrielle", "production en série", "production line", "manufacturing line", "high-volume manufacturing", "commercial production", "customer production", "contract manufacturing", "job shop", "manufacturing services", "small series", "small batch", "lohnfertigung", "auftragsfertigung", "lavorazione conto terzi", "conto terzi", "fabricación por contrato", "fabricacion por contrato", "subcontratación", "subcontratacion")),
    ("Industrialisation", "radar", ("industrialization", "industrialisation", "industrial implementation", "industrialiser", "to industrialize", "scale-up", "scaling-up", "production-ready", "manufacturing integration")),
    ("Pré-industrialisation", "radar", ("pilot line", "ligne pilote", "pilot production", "pre-series", "présérie", "pre-production", "qualification", "process qualification", "production trial")),
    ("Prototype", "radar", ("prototype", "prototyping", "demonstrator", "technology demonstrator")),
    ("R&D", "radar", ("proof of concept", "feasibility study", "process development", "research project", "development program", "project aims", "projet vise", "collaborative project")),
)

# === Bloc 3/6 : moteur de correspondance générique sur les lexiques ci-dessus ===
def _normalize_text(text: str) -> str:
    """Normalize Unicode, punctuation variants and whitespace without losing semantics."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u00ad", "").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text

MORPHOLOGY_VARIANTS = {
    "stent": ("stents",),
    "catheter": ("catheters", "cathéter", "cathéters"),
    "guidewire": ("guidewires",),
    "guide wire": ("guide wires",),
    "needle": ("needles",),
    "medical needle": ("medical needles",),
    "surgical needle": ("surgical needles",),
    "implant": ("implants",),
    "electrode": ("electrodes", "électrode", "électrodes"),
    "battery electrode": ("battery electrodes",),
    "current collector": ("current collectors",),
    "battery foil": ("battery foils",),
    "electrode foil": ("electrode foils",),
    "busbar": ("busbars",),
    "battery tab": ("battery tabs",),
    "wafer": ("wafers",),
    "semiconductor wafer": ("semiconductor wafers",),
    "silicon wafer": ("silicon wafers",),
    "glass wafer": ("glass wafers",),
    "substrate": ("substrates",),
    "glass substrate": ("glass substrates",),
    "ceramic substrate": ("ceramic substrates",),
    "silicon substrate": ("silicon substrates",),
    "microchannel": ("microchannels",),
    "micro-channel": ("micro-channels",),
    "glass component": ("glass components",),
    "optical fiber": ("optical fibers",),
    "optical fibre": ("optical fibres",),
    "waveguide": ("waveguides",),
    "wave guide": ("wave guides",),
    "nozzle": ("nozzles",),
    "injector": ("injectors",),
    "turbine blade": ("turbine blades",),
    "turbine component": ("turbine components",),
    "sensor": ("sensors",),
    "capteur": ("capteurs",),
}

@lru_cache(maxsize=1024)
def _term_variants(term: str) -> tuple[str, ...]:
    """Renvoie le terme original + ses variantes de pluriel/langue connues (MORPHOLOGY_VARIANTS),
    pour qu'un lexique n'ait pas besoin de lister "stent" ET "stents" séparément."""
    norm = _normalize_text(term)
    variants = MORPHOLOGY_VARIANTS.get(norm, ())
    return tuple(dict.fromkeys((term, *variants)))

@lru_cache(maxsize=2048)
def _term_pattern(term: str) -> re.Pattern[str]:
    """Compile a safe lexical pattern.

    Single alphanumeric terms use word boundaries; multi-word/hyphenated expressions
    allow flexible spaces/hyphens. This avoids substring matches such as 'sle' inside
    unrelated words while preserving industrial spelling variants.
    """
    norm = _normalize_text(term)
    pieces = [re.escape(p) for p in re.split(r"[\s-]+", norm) if p]
    if not pieces:
        return re.compile(r"a^")
    body = r"[\s-]+".join(pieces)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)

def _contains_term_normalized(norm_text: str, term: str) -> bool:
    """Match one lexical term against text that has already been normalized."""
    return any(bool(_term_pattern(variant).search(norm_text)) for variant in _term_variants(term))

def _contains_term(text: str, term: str) -> bool:
    return _contains_term_normalized(_normalize_text(text), term)

def _rule_match_terms(text: str, rule: LexiconRule) -> list[str]:
    """Return only the actual lexical evidence supporting a complete rule."""
    norm = _normalize_text(text)
    excludes = rule.get("exclude", ())
    if excludes and any(_contains_term_normalized(norm, term) for term in excludes):
        return []

    all_of = rule.get("all_of", ())
    if all_of and not all(_contains_term_normalized(norm, term) for term in all_of):
        return []

    hits: list[str] = []
    if all_of:
        hits.extend(term for term in all_of if _contains_term_normalized(norm, term))
    for term in rule.get("any_of", ()):
        if _contains_term_normalized(norm, term):
            hits.append(term)
    for pattern in rule.get("regex", ()):
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            hits.append(match.group(0))

    if not hits:
        return []
    requires_any = rule.get("requires_any", ())
    if requires_any and not any(_contains_term_normalized(norm, term) for term in requires_any):
        return []
    return list(dict.fromkeys(hits))

def _rule_matches(text: str, rule: LexiconRule) -> bool:
    """Version booléenne de _rule_match_terms, pour les appels qui n'ont pas besoin des termes trouvés."""
    return bool(_rule_match_terms(text, rule))

def _specificity_score(rule: LexiconRule, hits: list[str]) -> tuple[int, int, int]:
    """Favor explicit multi-term rules and longer evidence over generic labels."""
    all_bonus = 3 if rule.get("all_of") else 0
    regex_bonus = 1 if rule.get("regex") else 0
    lexical_weight = sum(len(_normalize_text(hit)) for hit in hits)
    return (all_bonus + regex_bonus + len(hits), lexical_weight, len(rule.get("all_of", ())))

def _match_label_details(text: str, lexicon: Lexicon) -> tuple[str | None, list[str]]:
    """Cherche le MEILLEUR libellé (le plus spécifique, voir _specificity_score) qui matche
    dans `text` pour un lexique donné, et renvoie (libellé, termes trouvés) ou (None, [])."""
    matches: list[tuple[tuple[int, int, int], str, list[str]]] = []
    for label, rule in lexicon.items():
        hits = _rule_match_terms(text, rule)
        if hits:
            matches.append((_specificity_score(rule, hits), label, hits))
    if not matches:
        return None, []
    matches.sort(key=lambda item: item[0], reverse=True)
    _, label, hits = matches[0]
    return label, hits

def _match_all_labels(text: str, lexicon: Lexicon) -> list[tuple[str, list[str]]]:
    """Return all matching canonical labels, ordered by specificity, not just the first one."""
    matches: list[tuple[tuple[int, int, int], str, list[str]]] = []
    for label, rule in lexicon.items():
        hits = _rule_match_terms(text, rule)
        if hits:
            matches.append((_specificity_score(rule, hits), label, hits))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [(label, hits) for _, label, hits in matches]

def _laser_match(text: str) -> bool:
    """Le "portail d'entrée" du domaine : vrai si `text` contient au moins un terme laser
    ultra-rapide connu (voir LASER_RULES). Sans ce match, aucun candidat n'est jamais créé."""
    return any(_rule_matches(text, rule) for rule in LASER_RULES.values())

# "Monitoring IA procédé" et "Beam shaping" sont volontairement génériques dans
# PROCESS_TECHNOLOGIES (digital twin, process monitoring, spatial light modulator...) parce que
# technology_signals ne les tague jamais que sur un texte ayant déjà passé un filtre laser en
# amont (voir _candidate()/_offer_candidates(), et l'ancien cordis.py qui les vérifiait sur le
# titre+objectif SANS jamais exiger _laser_match). Utilisés seuls comme filtre de pertinence
# thématique (voir is_on_topic ci-dessous, appelée par cordis.py/openalex.py), ces termes
# génériques créent de faux positifs sur du contenu industriel sans rapport avec le laser --
# vérifié : un document Tekniker "AI-Enriched Safety Criteria Catalogue and Digital Twin
# Framework for Predictive Safety and Maintenance", sans aucune mention de laser, matchait
# "Monitoring IA procédé" via "digital twin" seul. Exclus ici pour cette raison ; SLE/LIPSS/
# DLIP/LSFL/HSFL restent des procédés assez spécifiquement laser pour être fiables seuls.
# "Multi-beam / parallélisation" ("parallel processing") et "Fabrication roll-to-roll
# (batteries)" ("roll-to-roll", technique de fabrication générique -- impression, revêtement,
# pas seulement laser) rejoignent la même exclusion pour la même raison (Lot 2 §2.7) :
# "Haute puissance / hauts taux" reste hors de cette liste, ses termes (repetition rate, mhz
# processing) étant assez spécifiquement photonique pour rester fiables seuls.
# Les quatre axes ajoutés le 09/09/2026 rejoignent cette liste, mesure à l'appui : passés sur
# les 23 451 projets Horizon Europe du cache, en ne comptant que ceux SANS terme laser
# ultra-rapide, ils feraient entrer respectivement +0 (Burst), +2 (Soudage : un système de
# puissance spatial et un projet de stockage d'hydrogène), +4 (Texturation, dont une cellule
# solaire) et +0 (Bulk) projets hors sujet. Deux d'entre eux sont donc déjà des portes
# ouvertes, et les deux à +0 le doivent au corpus du moment, pas à leur vocabulaire : "burst
# mode" et "bulk silicon" sont de l'anglais courant en télécom comme en microélectronique.
#
# Les y mettre tous ne coûte RIEN, et c'est ce qui a tranché : les 21 publications visées
# passent toutes _laser_match (21/21 vérifiées). Le garde-fou ne perd donc aucun document,
# et ferme complètement le portail d'entrée -- exactement la fuite qui avait laissé RE4DY,
# iDriving et EEETHOS s'afficher comme projets de l'observatoire.
# Les six vocabulaires fermés qui classent un document, et le nom de dimension écrit dans
# technology_signals.dimension. Jusqu'au 09/09/2026 le chemin documentaire ne lisait que le
# premier : 61 des 92 documents ressortaient sans aucune famille, dont 41 que ces cinq autres
# classaient déjà -- ils ne servaient qu'à l'extraction de faits marché.
#
# Les libellés doivent rester DISJOINTS d'un vocabulaire à l'autre (vérifié par un test) : un
# même libellé dans deux dimensions apparaîtrait deux fois sur la page et fausserait le
# comptage par famille. C'est ce qui a fait fusionner "Fonctionnalisation de surface" dans
# OPERATIONS au lieu de la laisser aussi dans PROCESS_TECHNOLOGIES.
# Trois libellés d'opération que le chemin DOCUMENTAIRE n'utilise pas (10/09/2026) :
#
#   - "Micro-usinage" est générique. Dans un titre de publication il ne dit rien de plus que
#     "laser" -- l'opération réelle est une découpe, une gravure ou un perçage, et c'est celle-là
#     qu'il faut nommer.
#   - "Écriture de guide d'onde" nomme un PRODUIT (la pièce obtenue), pas un geste.
#   - "Scribing" est le mot anglais du marquage, pas une opération en français.
#
# Ils restent dans OPERATIONS, et c'est délibéré : l'extraction de faits marché et la page
# Offres & capacités les emploient sur des pages d'ACTEURS, où "Micro-usinage" est le nom d'une
# prestation vendue -- 45 capacités, 47 opérations et 37 faits marché en dépendent, et c'est la
# valeur la plus fréquente des trois tables. Ce qui est du remplissage dans un titre d'article
# est une offre commerciale sur un site d'entreprise.
_NON_DOCUMENT_OPERATIONS = frozenset({"Micro-usinage", "Écriture de guide d'onde", "Scribing"})
DOCUMENT_OPERATIONS: Lexicon = {
    label: rule for label, rule in OPERATIONS.items() if label not in _NON_DOCUMENT_OPERATIONS
}

DOCUMENT_LEXICONS: dict[str, Lexicon] = {
    "process_technology": PROCESS_TECHNOLOGIES,
    "machine_capability": MACHINE_CAPABILITIES,
    "operation": DOCUMENT_OPERATIONS,
    "material": MATERIALS,
    "market": MARKETS,
    "architecture": APPLICATION_ARCHITECTURES,
}

_GENERIC_PROCESS_AXES = frozenset({
    "Monitoring IA procédé", "Beam shaping", "Multi-beam / parallélisation", "Fabrication roll-to-roll (batteries)",
    # DLIP rejoint la liste le 10/09/2026 : contrairement à SLE et LIPSS, qui sont par
    # définition des procédés ultra-rapides, le DLIP se pratique aussi bien en nanoseconde
    # qu'en femtoseconde. Trois publications du corpus l'employaient sans nommer de régime,
    # dont une annonçant explicitement du "continuous-wave polishing". Le régime retenu étant
    # femto + ultra-rapide, l'axe ne peut plus admettre un contenu à lui seul.
    "Burst GHz/MHz", "Bulk", "DLIP", "Interaction laser-matière",
})

def is_on_topic(text: str) -> bool:
    """Filtre de pertinence thématique réutilisé HORS du pipeline de crawl (cordis.py,
    openalex.py) pour décider si un contenu obtenu ailleurs (projet CORDIS, publication
    OpenAlex) relève seulement du laser ultra-rapide -- mêmes lexiques que le reste de ce
    module (LASER_RULES via _laser_match, TECHNOLOGY_AXES pour un procédé ou une capacité
    nommés), moins les axes trop génériques ci-dessus. Voir audit v8 §2.1 : sans ce filtre, un centre
    technologique généraliste matché par alias (Tekniker, CEIT) fait remonter la totalité de
    ses projets/publications, quel que soit leur sujet réel.

    Depuis le 09/09/2026, le vocabulaire ultra-rapide ne suffit plus à lui seul : un travail
    où l'impulsion femtoseconde est l'INSTRUMENT DE MESURE et non le procédé est écarté ici
    aussi (voir is_laser_the_instrument). L'objectif de la veille est de suivre les
    DÉVELOPPEMENTS de la technologie laser ultra-rapide et de ses procédés ; une étude de
    photocatalyse ou de dynamique électronique attoseconde ne développe pas la technologie,
    elle s'en sert pour observer autre chose. Mesuré sur les 23 451 projets Horizon Europe du
    cache : 26 des 257 admis relèvent de cette catégorie.
    """
    if is_laser_the_instrument(text) or is_laser_the_source(text):
        return False
    if _laser_match(text):
        return True
    labels = {label for label, _ in _match_all_labels(text, TECHNOLOGY_AXES)}
    return bool(labels - _GENERIC_PROCESS_AXES)


# Techniques de CARACTÉRISATION ultra-rapides : dans ces travaux, l'impulsion femtoseconde est
# l'instrument de mesure, pas le procédé de fabrication. Le vocabulaire laser est bien là --
# _laser_match dit vrai -- mais le sujet est de la physico-chimie, pas de la mise en œuvre
# matière. Cas trouvé en production (audit du 09/09/2026) : "Revealing the enhanced
# photocatalytic hydrogen production mechanism [...] by femtosecond transient absorption
# spectroscopy", collecté par Crossref et affiché comme document de l'observatoire.
LASER_AS_INSTRUMENT_CUES = (
    "transient absorption spectroscopy", "transient absorption", "pump-probe spectroscopy",
    "pump probe spectroscopy", "time-resolved photoluminescence", "ultrafast spectroscopy",
    "femtosecond spectroscopy", "two-photon absorption spectroscopy",
)


# Vocabulaire du DÉVELOPPEMENT DE SOURCE : construire le laser, pas s'en servir pour
# transformer la matière. Décision de périmètre de Lucas (10/09/2026) : « les informations
# uniquement liées aux sources sont HORS SUJET ».
LASER_AS_SOURCE_CUES = (
    "laser platform", "laser source", "light source", "driver platform", "laser oscillator",
    "fiber laser", "fibre laser", "thin-disk oscillator", "laser amplifier", "frequency comb",
)


def is_laser_the_source(text: str) -> bool:
    """Vrai quand le texte porte sur la construction d'une SOURCE, sans procédé nommé.

    Le second membre est ce qui rend la règle utilisable, exactement comme pour
    is_laser_the_instrument : « Development of a modular femtosecond laser system for optical
    fiber and surface micromachining » construit bien une machine, mais nomme son procédé --
    c'est une machine de fabrication, elle reste. « Additive-manufactured monolithic femtosecond
    laser platform » ne nomme rien d'autre que la source : elle sort.

    Vérifié sur les 90 publications du corpus : rejette exactement les deux qui doivent l'être.
    """
    if not any(_contains_term(text, cue) for cue in LASER_AS_SOURCE_CUES):
        return False
    return not any(_match_all_labels(text, lexicon) for lexicon in (OPERATIONS, APPLICATION_ARCHITECTURES))


def is_laser_the_instrument(text: str) -> bool:
    """Vrai quand le texte relève d'une caractérisation ultra-rapide SANS procédé nommé.

    La seconde moitié de la condition est ce qui rend la règle utilisable : la spectroscopie
    sert aussi, légitimement, à SURVEILLER un procédé laser (deux publications IREPA du corpus,
    "Monitoring of ultrashort pulse laser surface texturing using spectroscopy..."). Exiger
    l'absence de tout procédé nommé -- TECHNOLOGY_AXES, OPERATIONS ou une architecture
    applicative -- distingue les deux sans avoir à lister les exceptions une par une. Vérifié
    sur les 25 publications en base : rejette la seule qui doit l'être, garde les deux de
    monitoring.

    Appliqué dans is_on_topic() depuis le 09/09/2026, donc à TOUTES les sources, projets
    CORDIS compris -- où il retire 26 des 257 projets qui y étaient admis (dynamique
    électronique attoseconde, physique ultra-rapide des pérovskites, simulations de matière
    quantique, transfert de proton dans les protéines). La règle de périmètre qui le justifie
    est explicite : la veille suit les DÉVELOPPEMENTS de la technologie laser ultra-rapide,
    pas les travaux qui s'en servent comme d'un instrument pour observer autre chose. Un
    projet de source ultra-rapide à haute cadence reste donc dans le sujet, une étude de
    photocatalyse sondée au femtoseconde n'y est pas.
    """
    if not any(_contains_term(text, cue) for cue in LASER_AS_INSTRUMENT_CUES):
        return False
    if {label for label, _ in _match_all_labels(text, TECHNOLOGY_AXES)}:
        return False
    if {label for label, _ in _match_all_labels(text, OPERATIONS)}:
        return False
    return not any(_rule_matches(text, rule) for rule in APPLICATION_ARCHITECTURES.values())


def _detect_maturity(text: str) -> tuple[str, str]:
    """Return the most mature explicit stage, using boundary-safe matching."""
    for stage, bucket, terms in MATURITY_RULES:
        if any(_contains_term(text, term) for term in terms):
            return bucket, stage
    return "unknown", "Maturité industrielle non déterminée"

def _quote(text: str, terms: tuple[str, ...] | list[str]) -> str:
    """Choisit, parmi les phrases de `text`, celle qui contient le plus de `terms` (la citation
    la plus "preuve") pour l'afficher dans l'UI comme justification du fait extrait."""
    text = (text or "").strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    def score(sentence: str) -> tuple[int, int]:
        hits = sum(_contains_term(sentence, term) for term in terms)
        return hits, min(len(sentence), 700)
    ranked = sorted((s.strip() for s in sentences if s.strip()), key=score, reverse=True)
    return (ranked[0] if ranked else text)[:700]


# --- API publique, pour les modules hors crawl -------------------------------------------
# Mêmes fonctions, sous un nom qui ne signale plus "usage interne à scrapers".
normalize_text = _normalize_text
contains_term = _contains_term
match_all_labels = _match_all_labels
match_label_details = _match_label_details
detect_maturity = _detect_maturity
best_quote = _quote
laser_match = _laser_match
