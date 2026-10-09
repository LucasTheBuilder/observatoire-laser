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
    # Français, ajouté avec national_projects.py (13/09/2026). Les projets ANR/FUI/FEDER et les
    # pages « projets collaboratifs » des acteurs français décrivent leur objet en français,
    # où AUCUN des termes ci-dessus n'apparaît -- la moitié anglophone du vocabulaire suffisait
    # tant que la seule source de projets était CORDIS, qui publie ses objectifs en anglais.
    # Les regex (pas any_of) parce que le français décline : "ultracourtes", "ultra-brèves",
    # "ultrarapides" -- _term_pattern pose des frontières de mot et ne les attraperait pas.
    # "ultracourt"/"ultrarapide" seuls sont ambigus en français (électronique ultrarapide,
    # liaison ultrarapide) : ils exigent donc un contexte laser/impulsion/photonique, comme
    # "USP laser" plus haut exige déjà laser/pulse.
    "impulsions ultracourtes (FR)": {
        "regex": (r"\bultra[- ]?(?:court|bref|br[èe]v)\w*",),
        "requires_any": ("laser", "impulsion", "impulsions", "photonique"),
    },
    "laser ultrarapide (FR)": {
        "regex": (r"\bultra[- ]?rapide\w*",),
        "requires_any": ("laser", "impulsion", "impulsions", "photonique"),
    },
    # "femtoseconde" est déjà dans la règle "femtosecond" ci-dessus, mais au singulier exact :
    # "impulsions femtosecondes", la forme courante en français, lui échappait.
    "femtoseconde (FR)": {"regex": (r"\bfemtoseconde\w*",)},
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
                            "cutting quality", "laser singulation", "singulation", "slicing", "laser slicing",
                            # La découpe dans la langue des brevets (29/09/2026) : TRUMPF écrit
                            # « separating », ALPHANOV « cutting materials ». Des tournures
                            # entières, jamais « cutting » seul (« cutting tools », « cutting-edge »).
                            # Mesuré : 30 brevets Lens sur 109 et 4 publications sur 304, toutes de
                            # découpe de verre.
                            "glass cutting", "cutting materials", "cutting a material",
                            "cutting of brittle materials", "separating a workpiece", "separating workpieces",
                            "separating a material", "separating a transparent", "separating ultrathin glass",
                            "separating ultra-thin glass")},
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
        # Le soudage dans la langue des brevets TRUMPF (29/09/2026) : 10 brevets Lens sur 109,
        # aucune publication.
        "joining partners", "parts to be joined",
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
    # "marquage" nu est un faux ami depuis que des textes FRANÇAIS entrent dans le pipeline
    # (national_projects.py, 14/09/2026) : en biologie, un marquage est un traçage fluorescent.
    # Le projet ANR SOFICARS -- microscopie CARS "sans avoir recours à aucun marquage
    # fluorescent" -- matchait donc l'opération "Marquage" au sens laser, ce qui neutralisait
    # is_laser_the_source (dont la seconde moitié épargne tout texte nommant un procédé) et
    # faisait entrer un projet de développement de SOURCE dans l'observatoire.
    "Marquage": {
        "any_of": ("laser marking", "marquage laser", "marquage", "laser marker", "annealing marking", "color marking"),
        "exclude": ("marquage fluorescent", "marquage moléculaire", "fluorescent labelling", "fluorescent labeling"),
    },
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

# === Élargissement du 07/10/2026 : marchés et produits ===========================================
# Demandé par Lucas (« ajoute un maximum de mots : noms de produits, marchés »). Chaque terme a
# été mesuré sur le corpus réel avant d'entrer -- 2 472 textes : blocs laser des 22 887 pages
# d'acteurs crawlées, citations d'offres, titres et résumés des 413 documents -- et ses
# occurrences relues. Écartés à la relecture, parce qu'ils mentaient dans NOS textes :
# « diagnostics » seul (aussi le diagnostic de procédé), « traceability » (la traçabilité qualité,
# pas l'anti-contrefaçon), « nuclear » seul (« nuclear transition in thorium »), « aiguilles »
# (les aiguilles d'une montre, et il matche DANS « micro-aiguilles »), « dials » seul,
# « outillage » seul (« sans outillage »), « antenna » seul (« photosynthetic antenna »),
# « hologram » seul (un élément du montage optique d'un brevet), « scalpel » (« cleaned with a
# scalpel »), « fan-out » (structures photoniques fan-in/fan-out), « düse » (la buse de la
# machine), « chip manufacturing » et « wafer-level » (de la microfluidique verre),
# « halbleiter » seul (un matériau dans une liste), « embossing » seul (un procédé
# d'outillage), et les sigles courts FPC, MLA, GDL, ITER.
#
# Garder les deux nombres : _term_pattern pose une frontière de mot, « filter » ne matche pas
# « filters » (piège déjà rencontré trois fois, voir LASER_AS_SOURCE_CUES).
#
# MARKETS et COMPONENTS servent à l'extraction de faits (qui exige marché + pièce + opération
# dans une même phrase), aux tags de la page Technologie et à la compilation de la page Marché
# -- jamais au portail d'entrée is_on_topic, qui ne lit que TECHNOLOGY_AXES. Ces ajouts
# étiquettent mieux ce qui est déjà admis ; ils ne font rien entrer de nouveau dans le corpus.
_MARKET_TERMS_ADDED: dict[str, tuple[str, ...]] = {
    "Médical": (
        "medical device", "medical devices", "dispositif médical", "dispositifs médicaux", "medizintechnik",
        "medical technology", "medical engineering", "ophthalmic", "ophthalmology", "ophtalmologie", "ophtalmique",
        "dental", "dentaire", "orthopedic", "orthopaedic", "orthopédique", "cardiovascular", "cardiovasculaire",
        "implantable", "medical industry", "industrie médicale", "secteur médical", "médical", "médicale",
    ),
    "Batteries": (
        "lithium-ion", "li-ion battery", "li-ion batteries", "solid-state battery", "solid-state batteries",
        "batterie", "battery manufacturing", "battery production", "gigafactory", "gigafactories",
    ),
    "Semi-conducteurs": (
        "semi-conducteurs", "semiconductor industry", "semiconductor manufacturing",
        "halbleiterindustrie", "halbleitertechnik", "microelectronic", "micro-électronique", "mikroelektronik",
    ),
    "Aéronautique": (
        "aéronautique", "aérospatial", "aérospatiale", "aerospace industry", "luftfahrt", "aircraft engine",
        "aircraft engines", "aero-engine", "aero-engines", "jet engine", "jet engines", "avionics", "avionique",
    ),
    "Spatial": (
        "space industry", "industrie spatiale", "space applications", "satellites", "raumfahrt", "launcher",
        "launchers", "lanceur", "lanceurs", "space-qualified", "newspace",
    ),
    "Défense": ("defence industry", "defense industry", "armement", "militaire", "verteidigung", "wehrtechnik"),
    "Automobile": (
        "automotive industry", "industrie automobile", "automotive sector", "car manufacturer", "car manufacturers",
        "automobilindustrie", "fahrzeugbau", "electric vehicles", "véhicule électrique", "véhicules électriques",
        "powertrain", "powertrains",
    ),
    "Luxe": (
        "luxury goods", "luxury industry", "joaillerie", "jewelry", "jewellery", "bijouterie", "schmuck",
        "uhrenindustrie", "watch industry", "watchmakers", "watchmaker", "horloger", "horlogère", "horlogers",
        "haute horlogerie", "maroquinerie",
    ),
    "Quantum": (
        "quantum technology", "quantum technologies", "quantum computer", "quantum computers", "quantum sensor",
        "quantum sensors", "quantum communication", "technologies quantiques", "quantentechnologie", "qubit", "qubits",
    ),
    "Photonique": ("photonics industry", "integrated photonic", "silicon photonics", "photonique intégrée", "photonik"),
    "Sciences de la vie": (
        "life science", "biotechnology", "biotech", "biotechnologie", "pharmaceutical", "pharmaceuticals",
        "pharmaceutique", "pharma", "in vitro diagnostics", "in-vitro diagnostics", "medical diagnostics",
        "point-of-care", "point of care", "genomics", "cell culture", "organ-on-chip", "organ on chip",
        "sciences du vivant",
    ),
    "Photovoltaïque": (
        "solar module", "solar modules", "solar panel", "solar panels", "panneau solaire", "panneaux solaires",
        "module photovoltaïque", "modules photovoltaïques", "perovskite solar", "thin-film solar", "photovoltaik",
        "solarzelle", "solarzellen",
    ),
    "Hydrogène": (
        "hydrogen economy", "fuel cells", "piles à combustible", "electrolysis", "électrolyse", "wasserstoff",
        "brennstoffzelle", "brennstoffzellen", "pem fuel cell", "electrolysers", "electrolyzers",
    ),
}

# Cinq marchés nouveaux. Chacun a au moins une occurrence relue dans le corpus, sauf
# « Sécurité & anti-contrefaçon » qui en a neuf (marquage anti-contrefaçon, lutte contre la
# contrefaçon) et « Impression & emballage » (formes flexographiques, réservoirs d'encre).
_MARKETS_ADDED: Lexicon = {
    "Électronique": {"any_of": (
        "consumer electronics", "électronique grand public", "electronics industry", "electronics manufacturing",
        "industrie électronique", "printed electronics", "électronique imprimée", "flexible electronics",
        "électronique flexible", "smartphone", "smartphones", "wearables", "elektronikindustrie",
    )},
    "Télécommunications": {
        "any_of": (
            "telecommunications", "telecommunication", "télécommunications", "telecom", "datacom",
            "optical communication", "optical communications", "communications optiques", "5g", "data center",
            "data centers", "datacenter", "datacenters",
        ),
        # « waveguides at the telecom wavelengths 1310 nm and 1550 nm » nomme une BANDE
        # spectrale, pas le marché des télécoms (13 occurrences sur 13 dans le corpus).
        "exclude": ("telecom wavelength", "telecom wavelengths", "telecommunications wavelength",
                    "telecommunications wavelengths", "telecommunication wavelengths"),
    },
    "Impression & emballage": {"any_of": (
        "printing industry", "industrie de l'impression", "imprimerie", "packaging industry",
        "industrie de l'emballage", "flexographic", "flexographie", "druckindustrie", "verpackungsindustrie",
    )},
    "Sécurité & anti-contrefaçon": {"any_of": (
        "anti-counterfeiting", "anti-counterfeit", "anticounterfeiting", "anti-contrefaçon", "counterfeiting",
        "contrefaçon", "banknote", "banknotes", "billets de banque", "fälschungsschutz",
    )},
    "Outillage & coupe": {"any_of": (
        "cutting tool industry", "tool industry", "tooling industry", "werkzeugbau", "werkzeugindustrie",
        "industrie de l'outillage", "toolmaking", "tool making",
    )},
    "Énergie nucléaire & fusion": {"any_of": (
        "nuclear industry", "nuclear power", "nuclear energy", "industrie nucléaire", "fusion energy",
        "inertial confinement", "tokamak", "kernenergie",
    )},
}

_COMPONENT_TERMS_ADDED: dict[str, tuple[str, ...]] = {
    "Stents": ("stents", "stent struts", "bioresorbable scaffold", "bioresorbable scaffolds"),
    "Cathéters": ("catheters", "catheter tip", "catheter tubing"),
    "Aiguilles médicales": ("hypodermic needle", "hypodermic needles", "kanüle", "kanülen", "cannula", "cannulas",
                            "cannulae", "cannule", "cannules"),
    "Implants": ("implants", "dental implant", "dental implants", "implant dentaire", "implants dentaires",
                 "bone implant", "bone implants", "orthopedic implant", "orthopaedic implant", "prosthesis", "prostheses",
                 "prothèse", "prothèses", "implantat", "implantate"),
    "Wafers": ("wafer dicing", "plaquette de silicium", "plaquettes de silicium", "sic wafer", "sic wafers",
               "sapphire wafer", "sapphire wafers"),
    "Substrats": ("substrats",),
    "Packaging avancé": ("chip packaging", "wafer-level packaging", "fan-out packaging", "fan-out wafer",
                         "3d packaging", "system in package", "packaging avancé"),
    "MicroLED": ("micro-leds", "microleds", "micro led", "micro leds"),
    "PCB": ("printed circuit boards", "circuit imprimé", "circuits imprimés", "leiterplatte", "leiterplatten",
            "flexible printed circuit", "flexible printed circuits"),
    "Microcanaux": ("microchannels", "micro-channels", "microcanaux", "micro-canaux", "mikrokanal", "mikrokanäle"),
    "Dispositifs microfluidiques": ("microfluidic devices", "microfluidic chips", "lab-on-a-chip", "lab on a chip",
                                    "puce microfluidique", "puces microfluidiques", "dispositif microfluidique",
                                    "dispositifs microfluidiques", "microfluidic cartridge", "microfluidic cartridges",
                                    "mikrofluidik-chip", "mikrofluidik-chips"),
    "Fibres optiques": ("optical fibers", "optical fibres", "fibre optique", "fibres optiques", "fiber bragg grating",
                        "fiber bragg gratings", "fbg", "fbgs", "glasfaser", "glasfasern"),
    "Guides d'onde": ("waveguides", "guide d'onde", "guides d'onde", "wellenleiter"),
    "Buses": ("nozzles", "buses", "spray nozzle", "spray nozzles"),
    "Injecteurs": ("injectors", "fuel injector", "fuel injectors", "injection nozzle", "injection nozzles", "injecteurs",
                   "einspritzdüse", "einspritzdüsen"),
    "Aubes / composants turbine": ("turbine blades", "aube de turbine", "aubes de turbine", "turbinenschaufel",
                                   "turbinenschaufeln", "cooling holes", "trous de refroidissement", "combustor",
                                   "combustors", "combustion chamber", "combustion chambers"),
    "Pièges à ions": ("ion-trap", "ion-traps"),
    "Connectique": ("connectors", "connecteur", "connecteurs", "steckverbinder"),
    "Optique intégrée": ("photonic integrated circuits", "photonic circuit", "photonic circuits", "integrated optical"),
    "Composants d'affichage": ("display panels", "oled panel", "oled panels", "écran oled", "écrans oled", "écrans",
                               "displays", "cover glass", "touch panel", "touch panels", "écran tactile", "écrans tactiles"),
    "Moules et outillage de précision": ("mould", "moulds", "mold insert", "mold inserts", "mould insert", "mould inserts",
                                         "injection molds", "injection moulds", "embossing tool", "embossing tools",
                                         "stamper", "stampers", "tire mold", "tire molds", "tyre mould", "tyre moulds",
                                         "spritzgießwerkzeug", "spritzgusswerkzeug", "formeinsatz", "formeinsätze"),
    "Micro-aiguilles": ("micro-needle", "micro-needles", "microneedle array", "microneedle arrays"),
    "Lentilles de contact": ("contact-lens", "contact-lenses"),
    "Vias traversants (TSV)": ("through-silicon vias", "through silicon vias"),
    "Cellules photovoltaïques": ("pv cells", "perc cell", "perc cells", "heterojunction cell", "heterojunction cells",
                                 "topcon", "perovskite cell", "perovskite cells", "cellules photovoltaïques",
                                 "solar wafer", "solar wafers"),
    "Plaques bipolaires": ("bipolarplatte", "bipolarplatten", "plaques bipolaires"),
    "Réseaux de diffraction": ("gratings", "volume bragg grating", "volume bragg gratings", "beugungsgitter"),
    "Micro-lentilles": ("micro-lens", "micro-lenses", "micro lens array", "micro lens arrays"),
    "Composants horlogers": ("watch parts", "watch components", "composants horlogers", "pièces horlogères",
                             "uhrenteile", "balance spring", "balance springs", "hairspring", "hairsprings",
                             "spiral horloger", "watch movements", "escapement", "escapements", "échappement"),
    "Boîtiers de montres": ("watch cases", "boîtiers de montre", "uhrengehäuse"),
    "Cadrans de montres": ("cadrans", "zifferblatt", "zifferblätter"),
    "Puces photoniques": ("photonic integrated chip", "photonic integrated chips"),
}

# Seize familles de produits nouvelles, toutes des pièces que le micro-usinage ultra-rapide
# fabrique ou traite couramment ; celles qui ont une occurrence dans le corpus l'ont relue.
_COMPONENTS_ADDED: Lexicon = {
    "Outils coupants": {"any_of": (
        "cutting tool", "cutting tools", "cutting insert", "cutting inserts", "indexable insert", "indexable inserts",
        "pcd tool", "pcd tools", "pcd insert", "pcd inserts", "cbn tool", "cbn tools", "carbide tool", "carbide tools",
        "outil coupant", "outils coupants", "plaquette de coupe", "plaquettes de coupe", "zerspanungswerkzeug",
        "zerspanungswerkzeuge", "schneidwerkzeug", "schneidwerkzeuge", "drill bit", "drill bits", "end mill", "end mills",
    ),
        # L'outil MÉCANIQUE que le laser remplace n'est pas le produit fabriqué : « mostly used
        # are mechanical saws or conventional cutting tools » (cas Pulsar, test_v3_4_market_engine).
        "exclude": (
            "conventional cutting tool", "conventional cutting tools", "mechanical cutting tool",
            "mechanical cutting tools", "traditional cutting tool", "traditional cutting tools",
        ),
    },
    "Filtres et membranes": {"any_of": (
        "filtration membrane", "filtration membranes", "microfilter", "microfilters", "micro-filter", "micro-filters",
        "micro filter", "micro filters", "membrane filter", "membrane filters", "sieve", "sieves", "micro sieve",
        "micro sieves", "tamis", "membrane de filtration", "membranes de filtration", "microfiltre", "microfiltres",
        "perforated foil", "perforated foils",
    )},
    "Filières": {"any_of": (
        # « filière » seul est exclu : en français il désigne aussi un secteur (« la filière
        # hydrogène »). Ne restent que les formes qui nomment la pièce.
        "spinneret", "spinnerets", "filière d'extrusion", "filières d'extrusion", "filière de filage", "extrusion die", "extrusion dies", "drawing die",
        "drawing dies", "wire drawing die", "wire drawing dies", "spinndüse", "spinndüsen",
    )},
    "Cylindres d'impression et d'embossage": {"any_of": (
        "anilox", "anilox roll", "anilox rolls", "embossing roll", "embossing rolls", "embossing cylinder",
        "embossing cylinders", "printing roll", "printing rolls", "printing cylinder", "printing cylinders",
        "gravure cylinder", "gravure cylinders", "rotary die", "rotary dies", "prägewalze", "prägewalzen",
        "cylindre d'impression", "cylindres d'impression", "printing form", "printing forms",
    )},
    "Pierres précieuses et diamants": {"any_of": (
        "gemstone", "gemstones", "lab-grown diamond", "lab-grown diamonds", "diamant de synthèse", "diamants de synthèse",
        "pierre précieuse", "pierres précieuses", "edelstein", "edelsteine", "diamond jewelry",
    )},
    "Verres de montre et vitres saphir": {"any_of": (
        "watch crystal", "watch crystals", "glace de montre", "glaces de montre", "sapphire window", "sapphire windows",
        "sapphire crystal", "uhrglas",
    )},
    "Valves cardiaques": {"any_of": (
        "heart valve", "heart valves", "valve cardiaque", "valves cardiaques", "herzklappe", "herzklappen",
    )},
    "Hypotubes et tubes médicaux": {"any_of": (
        "hypotube", "hypotubes", "medical tubing", "medical tube", "medical tubes", "tube médical", "tubes médicaux",
    )},
    "Lames et instruments chirurgicaux": {"any_of": (
        "surgical blade", "surgical blades", "scalpel blade", "scalpel blades", "surgical instrument",
        "surgical instruments", "instrument chirurgical", "instruments chirurgicaux", "skalpelle", "razor blade",
        "razor blades",
    )},
    "Endoscopes": {"any_of": ("endoscope", "endoscopes", "endoskop", "endoskope")},
    "Antennes RFID et radiofréquence": {"any_of": (
        "rfid antenna", "rfid antennas", "antenne rfid", "antennes rfid", "5g antenna", "5g antennas",
        "antenna array", "antenna arrays",
    )},
    "Cartes à puce et documents d'identité": {"any_of": (
        "smart card", "smart cards", "carte à puce", "cartes à puce", "id card", "id cards", "chipkarte",
        "chipkarten", "contactless payment card", "contactless payment cards",
    )},
    "Hologrammes et marquages de sécurité": {"any_of": (
        "holograms", "hologrammes", "security marking", "security markings", "anti-counterfeiting marking",
        "marquage de sécurité", "marquages de sécurité",
    )},
    "Boîtiers hermétiques": {"any_of": (
        "hermetic package", "hermetic packages", "hermetic packaging", "hermetic sealing", "hermetic seal",
        "hermetic seals", "boîtier hermétique", "boîtiers hermétiques", "encapsulation hermétique",
        "glass-to-metal seal", "glass-to-metal seals",
    )},
    "Modules et cellules de batteries": {"any_of": (
        "battery cell", "battery cells", "battery module", "battery modules", "battery pack", "battery packs",
        "pouch cell", "pouch cells", "cylindrical cell", "cylindrical cells", "module de batterie",
        "modules de batterie", "pack batterie",
    )},
    "Panneaux acoustiques et surfaces micro-perforées": {"any_of": (
        "acoustic panel", "acoustic panels", "acoustic liner", "acoustic liners", "micro-perforated",
        "microperforated", "panneau acoustique", "panneaux acoustiques",
    )},
    "Ressorts et pièces de micromécanique": {"any_of": (
        "micro spring", "micro springs", "microspring", "microsprings", "flexure", "flexures", "compliant mechanism",
        "compliant mechanisms", "micro gear", "micro gears", "microgear", "microgears", "micro-engrenage",
        "micro-engrenages",
    )},
}


def _extend(lexicon: Lexicon, terms: dict[str, tuple[str, ...]], added: Lexicon) -> None:
    for label, extra in terms.items():
        rule = lexicon[label]
        rule["any_of"] = tuple(dict.fromkeys((*rule.get("any_of", ()), *extra)))
    for label, rule in added.items():
        if label in lexicon:
            raise ValueError(f"{label!r} existe déjà dans le lexique")
        lexicon[label] = rule


_extend(MARKETS, _MARKET_TERMS_ADDED, _MARKETS_ADDED)
_extend(COMPONENTS, _COMPONENT_TERMS_ADDED, _COMPONENTS_ADDED)


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

# Termes qui attestent une PRODUCTION réelle, par opposition à une intention ou un essai. C'est
# la seule distinction de maturité que le chemin marché conserve : un fait est "existing" si
# l'un de ces termes apparaît, "radar" sinon (voir is_production ci-dessous).
PRODUCTION_TERMS = ("mass production", "volume production", "series production", "serial production", "production industrielle", "production en série", "production line", "manufacturing line", "high-volume manufacturing", "commercial production", "customer production", "contract manufacturing", "job shop", "manufacturing services", "small series", "small batch", "lohnfertigung", "auftragsfertigung", "lavorazione conto terzi", "conto terzi", "fabricación por contrato", "fabricacion por contrato", "subcontratación", "subcontratacion")

# Échelle de maturité à cinq niveaux, du plus mature (Production) au moins mature (R&D) --
# _detect_maturity() parcourt cette liste DANS L'ORDRE et retourne le premier stage dont un
# terme apparaît dans le texte, donc l'ordre encode une priorité : si un texte mentionne à la
# fois "prototype" et "production en série", "Production" gagne.
#
# RÉSERVÉE AUX SIGNAUX TECHNOLOGIQUES (technology_signals.maturity_stage, où le stage EST l'axe
# lu par la page Technologie). Retirée du chemin MARCHÉ le 2026-10-06 : 180 des 258 faits
# portaient "Maturité industrielle non déterminée" ou NULL, l'étiquette s'affichait pourtant sur
# chaque carte de revue, et les deux tiers des faits classés "radar" y tombaient par simple
# ABSENCE de détection plutôt que par un signal de R&D réel -- une opposition existant/radar en
# partie fictive. Ne pas la recâbler sur evidence/offers : le marché ne connaît plus que
# is_production().
MATURITY_RULES = (
    ("Production", "existing", PRODUCTION_TERMS),
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

# Index de déclenchement par lexique (09/10/2026). Un libellé ne peut matcher que si l'un de ses
# termes any_of/all_of (ou l'une de ses regex) est présent : _rule_match_terms exige au moins un
# « hit ». Mesuré : 2,7 millions de recherches regex et 11 s pour classer les 413 documents de la
# page Marché (près d'une minute dans le conteneur), alors que la plupart des phrases ne nomment
# aucun marché ni aucune pièce.
#
# Le filtre est un index de MOTS, pas une regex : une alternance de 2 000 termes gardés par
# (?<!\w) ne s'optimise pas et ne gagnait qu'un facteur 1,7. Un terme ne peut matcher que si son
# premier mot apparaît tel quel parmi les mots du texte normalisé (_term_pattern pose une
# frontière de mot avant et après chaque morceau) : c'est une condition NÉCESSAIRE, donc le
# résultat est strictement identique -- seuls les libellés qui ne pouvaient pas matcher sont
# sautés. Un libellé à `regex`, ou dont un terme ne commence pas par une lettre ou un chiffre,
# est toujours évalué.
_WORD = re.compile(r"\w+")
_ALWAYS: frozenset[str] = frozenset()
_INDEXES: dict[int, tuple[int, dict[str, frozenset[str]]]] = {}


def _first_word(term: str) -> str | None:
    norm = _normalize_text(term)
    match = _WORD.match(norm)
    return match.group(0) if match else None


def _label_keys(rule: LexiconRule) -> frozenset[str]:
    if rule.get("regex"):
        return _ALWAYS
    keys = set()
    for term in (*rule.get("any_of", ()), *rule.get("all_of", ())):
        for variant in _term_variants(term):
            word = _first_word(variant)
            if word is None:
                return _ALWAYS
            keys.add(word)
    return frozenset(keys)


def _lexicon_index(lexicon: Lexicon) -> dict[str, frozenset[str]]:
    # Clé = identité du dictionnaire + taille : les lexiques sont des constantes de module,
    # complétés une seule fois à l'import (voir _extend) avant tout appel.
    cached = _INDEXES.get(id(lexicon))
    if cached and cached[0] == len(lexicon):
        return cached[1]
    index = {label: _label_keys(rule) for label, rule in lexicon.items()}
    _INDEXES[id(lexicon)] = (len(lexicon), index)
    return index


def _candidate_rules(text: str, lexicon: Lexicon) -> list[tuple[str, LexiconRule]]:
    """Les seuls libellés dont un terme peut apparaître dans `text` -- les autres ne matchent pas."""
    words = set(_WORD.findall(_normalize_text(text)))
    return [
        (label, lexicon[label]) for label, keys in _lexicon_index(lexicon).items()
        if keys is _ALWAYS or not keys.isdisjoint(words)
    ]


def _match_label_details(text: str, lexicon: Lexicon) -> tuple[str | None, list[str]]:
    """Cherche le MEILLEUR libellé (le plus spécifique, voir _specificity_score) qui matche
    dans `text` pour un lexique donné, et renvoie (libellé, termes trouvés) ou (None, [])."""
    matches: list[tuple[tuple[int, int, int], str, list[str]]] = []
    for label, rule in _candidate_rules(text, lexicon):
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
    for label, rule in _candidate_rules(text, lexicon):
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

# Trois libellés de COMPONENTS ne disent pas la PIÈCE quand on les lit sur un titre de
# publication, et les garder ferait de la dimension « pièce » un doublon bruyant :
#
#   - "Composants en verre" matche `fused silica` / `borosilicate glass`, c'est-à-dire la
#     matière et non l'objet. Mesuré : les 11 publications concernées portent DÉJÀ
#     Matériau = Verre, donc le libellé n'apporte rien, il répète la même chose ailleurs ;
#   - "Substrats" matche `substrate`, qui dans un titre est presque toujours un paramètre de
#     procédé (« Effect of substrate temperature on surface roughness... ») ;
#   - "Capteurs" matche `sensor`, y compris le capteur qui SURVEILLE le procédé (« Real-time
#     analysis of inline sensor data during USP-laser machining ») -- l'inverse d'une pièce
#     fabriquée.
#
# Même mécanique que _NON_DOCUMENT_OPERATIONS : ces libellés restent intacts pour Marché et
# Offres, où ils se lisent sur une page d'acteur et non sur un titre de douze mots.
# Deux termes de MARKETS ne survivent pas a la lecture d'un resume, et c'est une affaire de
# LANGUE, pas de peri. Le lexique a ete ecrit pour des titres de douze mots ; un resume OpenAlex
# en fait mille cent, d'anglais scientifique courant.
#
#   - « spatial » designe l'industrie spatiale en francais et une simple geometrie en anglais.
#     Releve sur le corpus : 33 occurrences, et les huit premieres lues a la main disent toutes
#     « spatial beam shape », « Spatial Light Modulator », « spatial separation », « spatial and
#     temporal accuracy » -- zero industrie spatiale ;
#   - « optical » est l'adjectif le plus banal de cette litterature : sur 73 occurrences, la
#     plupart nomment un INSTRUMENT de mesure (« optical microscopy », « optical profilometry »,
#     « optical properties »), pas le marche de l'optique. Les vrais cas -- guides d'onde,
#     elements diffractifs -- sont deja captes par la dimension piece.
#
# Les autres termes du marche resistent tres bien : « automotive », « semiconductor »,
# « biomedical », « defence », « solar cells » ne veulent dire qu'une chose.
#
# MARKETS lui-meme n'est pas touche : il sert aussi a l'extraction de faits marche, sur des
# pages d'acteurs ou le contexte est tout autre, et le modifier reecrirait des faits deja
# valides. Meme separation que DOCUMENT_OPERATIONS et DOCUMENT_COMPONENTS, au terme pres.
#
# Resserré le 06/10/2026, après relecture à la main des 96 tags marché et 76 tags produit de la
# page Technologie (les mêmes lexiques servent la compilation de la page Marché depuis le
# 28/09) : « optics » et « lens » y désignaient presque toujours l'optique DE LA MACHINE
# (« f-theta lens », « helical drilling optics », « processing optics ») -- les 24 publications
# classées Optique l'étaient toutes par ce biais ; « photonic » seul ramenait « digital photonic
# process chain » (le nom que Fraunhofer donne à sa chaîne de procédé laser) et « photonic
# crystal fiber » (la fibre qui livre le faisceau) ; « quantum » ramenait « quantum efficiency »
# et « quantum-dot-doped glass ». Le marché Optique ne s'ouvre plus qu'à des noms de PRODUIT
# optique, Photonique qu'à des dispositifs photoniques.
_DOCUMENT_MARKET_DROPPED: dict[str, frozenset[str]] = {
    "Spatial": frozenset({"spatial"}),
    "Optique": frozenset({"optical", "optics", "lens", "lenses"}),
    "Photonique": frozenset({"photonic"}),
}
_DOCUMENT_MARKET_ADDED: dict[str, tuple[str, ...]] = {
    "Optique": ("micro-optics", "micro-optique", "micro-optiques", "optics manufacturing", "precision optics", "optical components"),
    "Photonique": (
        "photonic device", "photonic devices", "photonic component", "photonic components",
        "photonic integrated circuit", "photonic integrated circuits", "photonic chip", "photonic chips",
        "photonic applications",
    ),
}
_DOCUMENT_MARKET_EXCLUDES: dict[str, tuple[str, ...]] = {
    "Quantum": ("quantum efficiency", "quantum dot", "quantum dots", "quantum-dot", "quantum electronics", "quantum yield", "quantum well"),
    # Un matériau n'est pas un marché : « ablation on metallic and semiconductor materials ».
    "Semi-conducteurs": ("semiconductor material", "semiconductor materials", "organic semiconductor", "organic semiconductors"),
    "Photonique": ("photonic crystal fiber", "photonic crystal fibre", "photonic crystal fibers", "photonic process chain"),
}
DOCUMENT_MARKETS: Lexicon = {
    label: {
        **rule,
        "any_of": tuple(
            term for term in rule.get("any_of", ()) if term not in _DOCUMENT_MARKET_DROPPED.get(label, frozenset())
        ) + _DOCUMENT_MARKET_ADDED.get(label, ()),
        "exclude": rule.get("exclude", ()) + _DOCUMENT_MARKET_EXCLUDES.get(label, ()),
    }
    for label, rule in MARKETS.items()
}


_NON_DOCUMENT_COMPONENTS = frozenset({"Composants en verre", "Substrats", "Capteurs"})
# Deux pièces que le texte scientifique nomme pour autre chose qu'un produit fabriqué (même
# relecture du 06/10/2026) : « electrode » y est aussi l'électrode d'un photodétecteur, d'une
# OLED ou d'une cellule d'électrolyse, jamais une électrode de BATTERIE sans le dire ; un
# élément optique diffractif est, six fois sur sept, l'outil qui divise le faisceau.
_DOCUMENT_COMPONENT_GUARDS: dict[str, dict[str, tuple[str, ...]]] = {
    "Électrodes de batteries": {
        "requires_any": ("battery", "batteries", "batterie", "li-ion", "lithium", "energy storage"),
    },
    "Éléments optiques diffractifs (DOE)": {
        # La règle d'origine attend « diffract », que la frontière de mot refuse dans
        # « diffractive » : lue sur une phrase sans le mot laser, elle ne passait plus.
        "requires_any": ("diffractive",),
        "exclude": ("beamlet", "beamlets", "beam division", "beam splitting", "multi-beam", "multibeam", "parallel beams", "galvo", "imaged"),
    },
}
DOCUMENT_COMPONENTS: Lexicon = {
    label: {
        **rule,
        "requires_any": rule.get("requires_any", ()) + _DOCUMENT_COMPONENT_GUARDS.get(label, {}).get("requires_any", ()),
        "exclude": rule.get("exclude", ()) + _DOCUMENT_COMPONENT_GUARDS.get(label, {}).get("exclude", ()),
    }
    for label, rule in COMPONENTS.items()
    if label not in _NON_DOCUMENT_COMPONENTS
}

# Les sept vocabulaires sur lesquels un document est classé. L'ordre n'a aucun effet ici -- les
# libellés sont disjoints, voir db._reconcile_technology_signal_dimensions -- mais il est écrit
# dans l'ordre de lecture décidé par Lucas le 13/09/2026 (« matériau / acteur / opération laser /
# si possible le marché ou le nom de la pièce ; procédé et capacité machine sont des P2 »), pour
# que ce fichier et la page racontent la même hiérarchie.
DOCUMENT_LEXICONS: dict[str, Lexicon] = {
    "operation": DOCUMENT_OPERATIONS,
    "material": MATERIALS,
    "market": DOCUMENT_MARKETS,
    # Ajoutée le 13/09/2026. Sans effet sur ce qui ENTRE dans le corpus : is_on_topic ne lit que
    # TECHNOLOGY_AXES, jamais ce dictionnaire -- cette dimension ne fait qu'étiqueter ce qui est
    # déjà admis. Mesurée sur les 144 publications : 12 nomment une pièce (micro-canaux, guides
    # d'onde, buses, électrodes de batteries, micro-aiguilles, fibres, interposeurs).
    "component": DOCUMENT_COMPONENTS,
    "architecture": APPLICATION_ARCHITECTURES,
    "process_technology": PROCESS_TECHNOLOGIES,
    "machine_capability": MACHINE_CAPABILITIES,
}

_GENERIC_PROCESS_AXES = frozenset({
    "Monitoring IA procédé", "Beam shaping", "Multi-beam / parallélisation", "Fabrication roll-to-roll (batteries)",
    # DLIP rejoint la liste le 10/09/2026 : contrairement à SLE et LIPSS, qui sont par
    # définition des procédés ultra-rapides, le DLIP se pratique aussi bien en nanoseconde
    # qu'en femtoseconde. Trois publications du corpus l'employaient sans nommer de régime,
    # dont une annonçant explicitement du "continuous-wave polishing". Le régime retenu étant
    # femto + ultra-rapide, l'axe ne peut plus admettre un contenu à lui seul.
    "Burst GHz/MHz", "Bulk", "DLIP", "Interaction laser-matière",
    # « Haute puissance / hauts taux » rejoint la liste le 10/09/2026, et c'est la collecte qui
    # l'a démontré : son vocabulaire ("high average power", "high repetition rate") décrit
    # d'abord une SOURCE, et comme l'axe n'était pas générique il ouvrait is_on_topic à lui
    # seul. Trois publications d'Amplitude sont entrées par cette porte -- un amplificateur
    # Yb, un module Nd:verre pour la fusion, le pétawatt d'ELI ALPS -- aucune ne portant de
    # terme femto ni d'autre axe. Mesuré : 13 projets Horizon portent cet axe, 5 n'entraient
    # que par lui ; côté corpus, il ne fait perdre aucune publication, celles qui parlent de
    # cadence pour un procédé nomment aussi le procédé.
    "Haute puissance / hauts taux",
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
    # Ajouts du 10/09/2026, mesurés sur une reconnaissance OpenAlex des 65 acteurs suivis :
    # l'imagerie ultra-rapide est l'autre grande famille d'instruments, et elle passait
    # entièrement à travers. « Advanced evaluation of single-shot ultrafast imaging
    # interferometry for increased resolution » (Fraunhofer IWS) avait été retirée à la main
    # le matin même et serait revenue à la collecte suivante. Sans danger pour les travaux
    # de diagnostic DE procédé -- « Pump-probe shadography of glass drilling » nomme le
    # perçage, la seconde moitié de la règle le garde.
    "imaging interferometry", "interferometry", "ultrafast imaging", "single-shot imaging",
    # Le miroir français, ajouté le 15/09/2026. La garde était entièrement anglophone alors que
    # sa jumelle is_laser_the_source parlait français depuis la veille : une garde traduite,
    # l'autre non. L'asymétrie se voyait à l'œil nu --
    #
    #     « Femtosecond transient absorption spectroscopy of nanoparticles »   -> écarté
    #     « Spectroscopie femtoseconde résolue en temps des métaux carbonyles » -> ADMIS
    #
    # la même phrase, traduite, changeait de verdict. Sans conséquence tant que le corpus était
    # anglophone ; l'arrivée des projets ANR (national_projects.py) l'a rendue coûteuse, et le
    # tableau de bord ANR de Lucas a montré l'ampleur : plus de la moitié des projets
    # femtoseconde financés en France emploient le laser comme INSTRUMENT de mesure.
    #
    # Mesuré sur les 314 projets ANR passant _laser_match, et c'est l'exacte mesure de ce qui
    # manquait : la liste anglaise en écartait 0, celle-ci en écarte 46. Aucun des 46 ne nomme
    # d'opération laser (contrôle inverse), et aucune des 304 publications du corpus ne bouge.
    #
    # Terme pour terme, pas un filet plus large : « spectroscopie », « microscopie » et
    # « imagerie » NUS en écarteraient 137, sans qu'on puisse montrer que les 91 de plus sont
    # des instruments. Ce qui n'est pas écarté en anglais ne doit pas l'être en français.
    "spectroscopie d'absorption transitoire", "absorption transitoire",
    "spectroscopie pompe-sonde", "spectroscopie pompe sonde", "pompe-sonde", "pompe sonde",
    "photoluminescence résolue en temps", "spectroscopie résolue en temps",
    "spectroscopie ultrarapide", "spectroscopie ultra-rapide", "spectroscopie femtoseconde",
    "spectroscopie d'absorption à deux photons", "spectroscopie à deux photons",
    "interférométrie", "imagerie ultrarapide", "imagerie ultra-rapide",
    "microscopie à deux photons", "microscopie multiphotonique", "microscopie multimodale",
    "microscopie à 3 photons", "microscopie à trois photons",
)


# Vocabulaire du DÉVELOPPEMENT DE SOURCE : construire le laser, pas s'en servir pour
# transformer la matière. Décision de périmètre de Lucas (10/09/2026) : « les informations
# uniquement liées aux sources sont HORS SUJET ».
LASER_AS_SOURCE_CUES = (
    "laser platform", "laser source", "light source", "driver platform", "laser oscillator",
    "fiber laser", "fibre laser", "thin-disk oscillator", "laser amplifier", "frequency comb",
    # Ajouts du 10/09/2026. La première liste ne connaissait que le mot « laser » accolé à un
    # type de source ; elle laissait donc passer tout ce qui décrit une source par sa
    # PERFORMANCE ou son ARCHITECTURE INTERNE -- « energy scalable CPA front-end platform »,
    # « Power-scaled femtosecond lasers for industrial productivity », « kW femtosecond laser
    # with beam steering functionality ». Trois annonces de source d'ALPHANOV et Amplitude,
    # trouvées par la reconnaissance OpenAlex du 10/09/2026.
    "cpa", "front-end", "front end", "oscillator", "beam steering",
    "power-scaled", "power scaling", "power scalable", "energy scalable", "energy-scalable",
    # L'optique de la source, même raisonnement : un empilement diélectrique pour laser
    # ultra-rapide est un composant, pas un procédé (« Physics-Informed Inverse Design of
    # Ultrafast Coatings », LZH). Volontairement précis : « coating » seul ferait sortir les
    # cinq publications Sirris de texturation de revêtements nanocellulose et bois.
    "optical coating", "optical coatings", "dielectric coating", "dielectric coatings",
    "ultrafast coating", "ultrafast coatings",
    # Deuxième passe, le soir même : la collecte a ramené huit annonces de source de plus, et
    # cinq sont passées à travers la liste ci-dessus pour une raison bête -- _term_pattern pose
    # une frontière de mot, donc « oscillator » ne matche PAS « oscillators ». Le piège est
    # connu et il a encore mordu ; tout terme de source s'écrit donc aux deux nombres.
    "oscillators", "amplifier", "amplifiers", "parametric oscillator", "parametric oscillators",
    "mopa", "multi-pass cell", "multipass cell", "petawatt", "peak power",
    "thin film coating", "thin film coatings", "mirror", "mirrors",
    # Troisième passe (13/09/2026), avec l'arrivée des projets nationaux : la liste était
    # entièrement anglophone, or l'ANR finance beaucoup de développement de SOURCE et le décrit
    # en français. Mesuré sur les 598 projets ANR contenant « laser » : sans ces termes, des
    # projets comme « Nouvelles Architectures pour les lasers intenses dans le moyen
    # infrarouge » ou « Lasers accordables pompés par LED » entraient dans l'observatoire comme
    # s'ils développaient un procédé. Les deux nombres, toujours (voir la note ci-dessus).
    "source laser", "sources laser", "sources lasers", "cavité laser", "cavités laser",
    "oscillateur", "oscillateurs", "amplificateur", "amplificateurs", "pompage",
    "peigne de fréquences", "milieu amplificateur", "laser accordable", "lasers accordables",
    "laser à fibre", "lasers à fibre", "puissance crête",
    # Quatrième passe (14/09/2026), déclenchée par les projets UKRI : la liste décrivait une
    # source par son ARCHITECTURE (oscillateur, amplificateur, CPA) mais jamais par son MILIEU
    # À GAIN ni par son mécanisme de blocage de modes. « GraTi:S - Graphene for Titanium
    # Sapphire Lasers » (Coherent, Innovate UK) entrait donc comme projet de l'observatoire
    # alors qu'il développe un cristal laser. Mesuré sur les projets GtR on-topic des quatre
    # acteurs britanniques retenus : ces termes en écartent exactement un, GraTi:S, et aucun
    # autre -- « mode-locked » ne fait pas sortir un travail de procédé qui décrit sa source,
    # la seconde moitié de la règle (un procédé nommé épargne le texte) s'en charge.
    "titanium sapphire", "ti:sapphire", "ti sapphire", "saturable absorber",
    "mode-locked", "mode locked", "mode-locking", "laser crystal", "laser crystals",
    "gain medium", "gain media", "laser gain", "doped crystal", "doped crystals",
    # Cinquième passe (14/09/2026), sur les projets ANR : le français nomme volontiers une
    # source « source (à fibres) optique(s) » sans jamais écrire le mot laser à côté. Cas
    # trouvé en production : « FLEX-UV — Source à fibres optiques émettant dans l'ultraviolet
    # extrême » (ALPhANOV) entrait comme projet de l'observatoire. Mesuré sur les dix projets
    # ANR on-topic des acteurs suivis : ces termes en écartent exactement un, FLEX-UV.
    "source à fibre", "source à fibres", "sources à fibre", "sources à fibres",
    "source fibrée", "sources fibrées", "source optique", "sources optiques",
)


def is_laser_the_source(text: str) -> bool:
    """Vrai quand le texte porte sur la construction d'une SOURCE, sans procédé nommé.

    Le second membre est ce qui rend la règle utilisable, exactement comme pour
    is_laser_the_instrument : « Development of a modular femtosecond laser system for optical
    fiber and surface micromachining » construit bien une machine, mais nomme son procédé --
    c'est une machine de fabrication, elle reste. « Additive-manufactured monolithic femtosecond
    laser platform » ne nomme rien d'autre que la source : elle sort.

    « beam steering » mérite un mot : c'est ce qui fait sortir « Ultrafast solid-state laser
    beam steering system for productivity increase in PBF-LB/M of 316L » (LZH), où « ultrafast »
    qualifie le SCANNER et non les impulsions, et où le procédé est de la fusion sur lit de
    poudre. Un vrai travail de procédé qui emploie du beam steering nomme son opération et
    reste -- c'est encore la seconde moitié de la règle qui tranche, pas une exception listée.

    Mesuré le 10/09/2026 sur les 79 publications en base et les 32 candidates d'une
    reconnaissance OpenAlex : rejette les 7 titres de source visés, aucun autre.
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

    Bilingue depuis le 15/09/2026 (voir LASER_AS_INSTRUMENT_CUES) : la règle vaut désormais
    sur un résumé d'appel à projets français comme sur un titre de revue anglophone.

    Appliqué dans is_on_topic() depuis le 09/09/2026, donc à TOUTES les sources, projets
    CORDIS compris -- où il retire 26 des 257 projets qui y étaient admis (dynamique
    électronique attoseconde, physique ultra-rapide des pérovskites, simulations de matière
    quantique, transfert de proton dans les protéines). La règle de périmètre qui le justifie
    est explicite : la veille suit les DÉVELOPPEMENTS de la technologie laser ultra-rapide,
    pas les travaux qui s'en servent comme d'un instrument pour observer autre chose. Une étude
    de photocatalyse sondée au femtoseconde n'y est donc pas.

    Ce docstring disait aussi, jusqu'au 10/09/2026, qu'« un projet de source ultra-rapide à
    haute cadence reste dans le sujet ». Ce n'est plus vrai : le périmètre tranché ce jour-là
    met les travaux de source hors sujet, et c'est is_laser_the_source() qui s'en charge.
    """
    if not any(_contains_term(text, cue) for cue in LASER_AS_INSTRUMENT_CUES):
        return False
    if {label for label, _ in _match_all_labels(text, TECHNOLOGY_AXES)}:
        return False
    if {label for label, _ in _match_all_labels(text, OPERATIONS)}:
        return False
    return not any(_rule_matches(text, rule) for rule in APPLICATION_ARCHITECTURES.values())


def _detect_maturity(text: str) -> tuple[str, str]:
    """Return the most mature explicit stage, using boundary-safe matching.

    Signaux technologiques uniquement -- voir MATURITY_RULES pour pourquoi le chemin marché ne
    l'appelle plus.
    """
    for stage, bucket, terms in MATURITY_RULES:
        if any(_contains_term(text, term) for term in terms):
            return bucket, stage
    return "unknown", "Maturité industrielle non déterminée"


def is_production(text: str) -> bool:
    """Le texte atteste-t-il une production réelle (par opposition à une intention) ?

    Remplace _detect_maturity sur le chemin marché. Renvoie un booléen et non un niveau : le
    besoin était de séparer "déjà en production" de "pas encore", pas de graduer cinq étapes
    dont quatre n'étaient jamais détectées (voir MATURITY_RULES). Une absence de terme n'est
    donc plus une maturité "inconnue" à afficher, juste un fait qui n'a pas prouvé sa mise en
    production.
    """
    return any(_contains_term(text, term) for term in PRODUCTION_TERMS)

_QUOTE_MAX = 700


def _term_position(text: str, term: str) -> int | None:
    """Position de `term` dans `text`, aux mêmes règles de frontière que _contains_term."""
    match = _term_pattern(term).search(_normalize_text(text))
    return match.start() if match else None


def _quote(text: str, terms: tuple[str, ...] | list[str]) -> str:
    """Choisit, parmi les phrases de `text`, celle qui contient le plus de `terms` (la citation
    la plus "preuve") pour l'afficher dans l'UI comme justification du fait extrait.

    La coupe se recentre sur le terme. Jusqu'au 14/09/2026 la phrase retenue était tronquée
    depuis son DÉBUT, ce qui pouvait couper juste avant le mot qui l'avait fait gagner : mesuré
    sur le corpus documentaire, 8 citations sur 790 ne contenaient plus aucun terme
    déclencheur. Une preuve qui ne montre pas ce qu'elle prouve n'est pas une preuve. Le défaut
    ne pouvait pas se voir tant que les documents n'avaient que leur titre pour texte -- il est
    apparu avec les résumés OpenAlex, longs de mille caractères."""
    text = (text or "").strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    def score(sentence: str) -> tuple[int, int]:
        hits = sum(_contains_term(sentence, term) for term in terms)
        return hits, min(len(sentence), _QUOTE_MAX)
    ranked = sorted((s.strip() for s in sentences if s.strip()), key=score, reverse=True)
    best = ranked[0] if ranked else text
    if len(best) <= _QUOTE_MAX:
        return best
    positions = [pos for term in terms if (pos := _term_position(best, term)) is not None]
    if not positions:
        return best[:_QUOTE_MAX]
    # Fenêtre centrée sur la première occurrence, recadrée quand elle déborde d'un côté.
    start = max(0, min(min(positions) - _QUOTE_MAX // 3, len(best) - _QUOTE_MAX))
    return f"…{best[start:start + _QUOTE_MAX]}" if start else best[:_QUOTE_MAX]


# --- API publique, pour les modules hors crawl -------------------------------------------
# Mêmes fonctions, sous un nom qui ne signale plus "usage interne à scrapers".
normalize_text = _normalize_text
contains_term = _contains_term
match_all_labels = _match_all_labels
match_label_details = _match_label_details
detect_maturity = _detect_maturity
best_quote = _quote
laser_match = _laser_match


# === Marché et produit nommés dans une PHRASE ==================================================
# Un marché ou une pièce ne se lit pas sur un texte entier : une page d'acteur est souvent une
# liste de publications ou un menu, un résumé scientifique enchaîne dix phrases sur dix sujets.
# Ces dimensions-là se rattachent donc phrase par phrase, et la phrase est la preuve. Partagé par
# la page Technologie (scrapers.upsert_document_technology_signal) et la compilation de la page
# Marché (market_compilation.py).
def _sentences(text: str) -> list[str]:
    """Small deterministic sentence/window splitter used for relation validation."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return []
    # Semicolons and bullets are meaningful separators on product/project/publication cards.
    parts = re.split(r"(?<=[.!?])\s+|\s*[•·▪◦]\s*|\s*;\s*", clean)
    return [part.strip() for part in parts if len(part.strip()) >= 12]


# Sur une page d'acteur, une phrase plus longue que ça n'est pas une phrase : c'est un menu, une
# liste de publications ou un tableau aplati par le crawler. Un titre ou un résumé n'a pas ce
# défaut, et un brevet écrit volontiers une phrase de 600 caractères : aucun plafond côté
# documents (relu le 06/10/2026 -- le plafond y faisait perdre des micro-LED et des wafers).
OFFER_SENTENCE_MAX = 320
DOCUMENT_SENTENCE_MAX: int | None = None

# Une référence bibliographique cite un travail, elle ne décrit pas une offre : un DOI, un
# congrès, un « [ PDF 2.2 MB ] ». Les initiales d'auteurs (« Gillner, A.: ») se lisent
# sensibles à la casse : en IGNORECASE, « etc., » en serait une.
_BIBLIOGRAPHIC_RE = re.compile(
    r"doi\.org|\bdoi\b|\bet al\b|conference|congress|proceedings|\[\s*pdf|autor\*innen",
    re.IGNORECASE,
)
_AUTHOR_INITIALS_RE = re.compile(r"\b[A-Z]\.[,:]\s")
_NANOSECOND_TERMS = ("nanosecond", "nanoseconde", "nanosecondes", "ns laser", "ns-laser")


def _sentence_is_usable(
    sentence: str, readable: str, *, max_len: int | None, reject_bibliography: bool, reject_negation: bool,
) -> bool:
    if max_len is not None and len(sentence) > max_len:
        return False
    if reject_bibliography and (_BIBLIOGRAPHIC_RE.search(sentence) or _AUTHOR_INITIALS_RE.search(sentence)):
        return False
    norm = _normalize_text(readable)
    # Négation seulement, pas le contraste (« conventional », « classic ») : qualifier la
    # méthode concurrente ne change pas le marché nommé dans la même phrase. Et seulement sur
    # une offre (« we do not machine stents ») : dans un résumé, la négation porte sur un
    # résultat (« surfaces sometimes do not meet the requirements » de l'outillage de moule),
    # jamais sur le marché qu'elle nomme.
    if reject_negation and any(_contains_term_normalized(norm, cue) for cue in NEGATION_CUES):
        return False
    # La phrase parle du laser qu'on construit, pas de ce qu'on en fait -- plus strict que
    # is_laser_the_source : ici un procédé nommé ne la sauve pas, on y lit un marché.
    if any(_contains_term_normalized(norm, cue) for cue in LASER_AS_SOURCE_CUES):
        return False
    if any(_contains_term_normalized(norm, term) for term in _NANOSECOND_TERMS) and not _laser_match(readable):
        return False
    return True


def labels_named_in_sentences(
    texts: list[str],
    lexicon: Lexicon,
    *,
    names: list[str] | None = None,
    operation_rule: LexiconRule | None = None,
    max_len: int | None = DOCUMENT_SENTENCE_MAX,
    reject_bibliography: bool = False,
    reject_negation: bool = False,
) -> dict[str, str]:
    """Chaque libellé de `lexicon` qu'une phrase de `texts` nomme, avec la PREMIÈRE phrase qui
    le porte, telle qu'écrite par la source.

    `names` (acteurs) sont retirés avant lecture : « Pulsar Photonics » n'est pas le marché de
    la photonique. `operation_rule`, quand il est donné, exige que la phrase nomme aussi
    l'opération : une page qui décrit six prestations ne rattache à la découpe que la phrase
    qui parle de découpe.
    """
    found: dict[str, str] = {}
    for text in texts:
        for sentence in _sentences(text or ""):
            readable = sentence
            for name in names or []:
                if name:
                    readable = re.sub(re.escape(name), " ", readable, flags=re.IGNORECASE)
            # Les libellés d'abord, les gardes ensuite : la plupart des phrases ne nomment rien,
            # et les gardes (200 indices de source laser, négation...) coûtaient plus que le
            # classement lui-même. Même résultat -- une phrase sans libellé n'apporte rien.
            labels = [label for label, _hits in _match_all_labels(readable, lexicon) if label not in found]
            if not labels:
                continue
            if operation_rule and not _rule_matches(readable, operation_rule):
                continue
            if not _sentence_is_usable(
                sentence, readable, max_len=max_len, reject_bibliography=reject_bibliography, reject_negation=reject_negation,
            ):
                continue
            for label in labels:
                found.setdefault(label, sentence)
    return found
