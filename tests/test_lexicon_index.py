"""L'index de mots de lexicon._candidate_rules ne doit jamais changer un résultat (09/10/2026).

Il saute les libellés dont aucun terme ne peut apparaître ; ce test compare, sur des textes
choisis pour ses cas limites (traits d'union, apostrophes, pluriels, sigles en regex, accents),
avec l'évaluation exhaustive libellé par libellé qu'il remplace.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lexicon as L

TEXTS = (
    "Femtosecond laser cutting of nitinol stents and catheters for medical devices.",
    "Écriture directe de guides d'onde 3D dans le verre pour la photonique intégrée.",
    "Through-silicon vias (TSV) and glass interposers for advanced packaging of MEMS.",
    "USP-Laser Bohren von Einspritzdüsen für die Automobilindustrie.",
    "Micro-optics, micro-lenses and DOE fabrication by selective laser-induced etching (SLE).",
    "Texturation de moules d'injection et d'outils coupants PCD par laser ultrabref.",
    "Battery electrodes for Li-ion batteries structured in GHz burst mode.",
    "No market at all in this sentence about pulse duration and fluence.",
    "",
)


def _exhaustive(text: str, lexicon: L.Lexicon) -> list[tuple[str, list[str]]]:
    matches = []
    for label, rule in lexicon.items():
        hits = L._rule_match_terms(text, rule)
        if hits:
            matches.append((L._specificity_score(rule, hits), label, hits))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [(label, hits) for _, label, hits in matches]


class LexiconIndexTests(unittest.TestCase):
    def test_indexed_matching_equals_exhaustive_matching(self):
        lexicons = {
            "markets": L.MARKETS, "components": L.COMPONENTS, "operations": L.OPERATIONS,
            "materials": L.MATERIALS, "axes": L.TECHNOLOGY_AXES, "architectures": L.APPLICATION_ARCHITECTURES,
            "document_markets": L.DOCUMENT_MARKETS, "document_components": L.DOCUMENT_COMPONENTS,
        }
        for name, lexicon in lexicons.items():
            for text in TEXTS:
                with self.subTest(lexicon=name, text=text[:40]):
                    self.assertEqual(_exhaustive(text, lexicon), L._match_all_labels(text, lexicon))


if __name__ == "__main__":
    unittest.main()
