"""Tests pour la taxonomie des axes technologiques (Lot 2 §2.7, audit veille §10.10,
30/08/2026) : "deux libellés d'axe coexistent pour le même concept [...] à 500 lignes, tout
comptage par axe sera faux et personne ne s'en apercevra" -- et "deux signaux portent
maturity_stage='Industrialisation' ET bucket='radar', deux champs qui se contredisent". Vérifié
contre les données réelles : cette paire N'EST PAS incohérente une fois TECHNOLOGY_STAGE_TO_
BUCKET appliqué (seul 'Production' vaut 'existing') -- le garde-fou corrige les vraies
incohérences sans jamais toucher une paire déjà correcte.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as dbmod
from scrapers import PROCESS_TECHNOLOGIES, _match_all_labels, is_on_topic


def _setup(tmp: str) -> Path:
    tech_db = Path(tmp) / "technology.db"
    with (
        patch.object(dbmod, "ACTORS_DB", Path(tmp) / "actors.db"),
        patch.object(dbmod, "MARKET_DB", Path(tmp) / "market.db"),
        patch.object(dbmod, "TECH_DB", tech_db),
    ):
        dbmod.init_databases()
    return tech_db


def _insert_signal(db, *, axis: str, project_name: str, actor_names: list[str], maturity_stage: str = "Prototype", bucket: str = "radar") -> int:
    fact_key = dbmod.technology_signal_key(axis, project_name)
    return db.execute(
        """INSERT INTO technology_signals(
               axis,maturity_stage,bucket,project_name,actor_names,source_url,quote,fact_key,fingerprint,
               review_status,field_confidence,created_at,updated_at,last_seen_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            axis, maturity_stage, bucket, project_name, json.dumps(actor_names), "https://example.test/project",
            "quote", fact_key, f"fp-{fact_key}", "accepted", 0.9, dbmod.utc_now(), dbmod.utc_now(), dbmod.utc_now(),
        ),
    ).lastrowid


class NormalizeTechnologyAxesTests(unittest.TestCase):
    def test_legacy_digital_twin_label_is_renamed_to_canonical_axis(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                _insert_signal(db, axis="Monitoring + IA / digital twin", project_name="ProjectX", actor_names=["Actor A"])
                dbmod._normalize_technology_axes(db)
            row = dbmod.rows(tech_db, "SELECT axis FROM technology_signals")[0]
            self.assertEqual("Monitoring IA procédé", row["axis"])

    def test_legacy_beam_shaping_label_is_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                _insert_signal(db, axis="Beam shaping / surfaces 3D", project_name="ProjectY", actor_names=["Actor B"])
                dbmod._normalize_technology_axes(db)
            row = dbmod.rows(tech_db, "SELECT axis FROM technology_signals")[0]
            self.assertEqual("Beam shaping", row["axis"])

    def test_merges_into_existing_canonical_row_instead_of_creating_a_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                _insert_signal(db, axis="Monitoring IA procédé", project_name="ProjectX", actor_names=["Actor A"])
                _insert_signal(db, axis="Monitoring + IA / digital twin", project_name="ProjectX", actor_names=["Actor B"])
                dbmod._normalize_technology_axes(db)
            rows = dbmod.rows(tech_db, "SELECT axis,actor_names FROM technology_signals")
            self.assertEqual(1, len(rows))
            self.assertEqual("Monitoring IA procédé", rows[0]["axis"])
            self.assertEqual(["Actor A", "Actor B"], sorted(json.loads(rows[0]["actor_names"])))

    def test_is_idempotent_on_repeated_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                _insert_signal(db, axis="Monitoring + IA / digital twin", project_name="ProjectX", actor_names=["Actor A"])
                dbmod._normalize_technology_axes(db)
                dbmod._normalize_technology_axes(db)
            count = dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signals")
            self.assertEqual(1, count)

    def test_a_document_signal_is_renamed_on_its_own_url_not_on_a_null_project(self):
        """Le discriminant du fact_key n'est pas toujours le projet : un signal documentaire le
        dérive de l'URL du document. La fonction ne connaissait que le cas projet, et aurait
        calculé la même clé pour DEUX documents distincts (project_name NULL des deux côtés) --
        donc fusionné des lignes sans rapport. Corrigé le 10/09/2026."""
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                for url in ("https://doi.org/10.1/a", "https://doi.org/10.1/b"):
                    dbmod.upsert_technology_signal(
                        db, fact_key=dbmod.technology_signal_key("Soudage / assemblage de transparents", url),
                        axis="Soudage / assemblage de transparents", maturity_stage="Prototype",
                        bucket="radar", actor_names=[], source_url=url, quote="q",
                        field_confidence=0.7, dimension="process_technology",
                    )
                dbmod._normalize_technology_axes(db)

            rows = dbmod.rows(tech_db, "SELECT axis,source_url FROM technology_signals ORDER BY source_url")
            self.assertEqual(2, len(rows), "deux documents distincts ne doivent jamais fusionner")
            self.assertEqual(["Soudage", "Soudage"], [row["axis"] for row in rows])
            self.assertEqual(["https://doi.org/10.1/a", "https://doi.org/10.1/b"], [row["source_url"] for row in rows])

    def test_merging_moves_the_citations_instead_of_deleting_them(self):
        """Une fusion ne doit jamais faire disparaître de la preuve : les sources de la ligne
        absorbée sont transférées avant le DELETE, que ON DELETE CASCADE emporterait sinon."""
        url = "https://doi.org/10.1/merge"
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                for axis, quote in (("Soudage", "cite du survivant"), ("Soudage / assemblage de transparents", "cite de l'absorbe")):
                    _, signal_id = dbmod.upsert_technology_signal(
                        db, fact_key=dbmod.technology_signal_key(axis, url), axis=axis,
                        maturity_stage="Prototype", bucket="radar", actor_names=[], source_url=url,
                        quote=quote, field_confidence=0.7, dimension="operation",
                    )
                    dbmod.upsert_fact_source(
                        db, "technology_signal_sources", signal_id,
                        source_url=url, quote=quote, fingerprint=f"fp-{axis}",
                    )
                dbmod._normalize_technology_axes(db)

            self.assertEqual(1, dbmod.scalar(tech_db, "SELECT COUNT(*) FROM technology_signals"))
            quotes = {row["quote"] for row in dbmod.rows(tech_db, "SELECT quote FROM technology_signal_sources")}
            self.assertEqual({"cite du survivant", "cite de l'absorbe"}, quotes)

    def test_axis_not_in_the_alias_map_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                _insert_signal(db, axis="SLE", project_name="ProjectZ", actor_names=["Actor C"])
                dbmod._normalize_technology_axes(db)
            row = dbmod.rows(tech_db, "SELECT axis FROM technology_signals")[0]
            self.assertEqual("SLE", row["axis"])


class ReconcileTechnologySignalMaturityTests(unittest.TestCase):
    def test_production_stage_with_wrong_bucket_is_corrected_to_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                signal_id = _insert_signal(db, axis="SLE", project_name="P1", actor_names=["A"], maturity_stage="Production", bucket="radar")
                dbmod._reconcile_technology_signal_maturity(db)
            row = dbmod.rows(tech_db, "SELECT bucket FROM technology_signals WHERE id=?", (signal_id,))[0]
            self.assertEqual("existing", row["bucket"])

    def test_industrialisation_stage_with_radar_bucket_is_already_correct_and_untouched(self):
        # §10.10's own cited example -- confirmed NOT a real contradiction: only 'Production'
        # maps to 'existing' in TECHNOLOGY_STAGE_TO_BUCKET, so this pairing is expected.
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                signal_id = _insert_signal(db, axis="SLE", project_name="P2", actor_names=["A"], maturity_stage="Industrialisation", bucket="radar")
                dbmod._reconcile_technology_signal_maturity(db)
            row = dbmod.rows(tech_db, "SELECT bucket FROM technology_signals WHERE id=?", (signal_id,))[0]
            self.assertEqual("radar", row["bucket"])

    def test_unrecognized_stage_defaults_to_radar_never_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tech_db = _setup(tmp)
            with dbmod.connect(tech_db) as db:
                signal_id = _insert_signal(db, axis="SLE", project_name="P3", actor_names=["A"], maturity_stage="Something Unrecognized", bucket="existing")
                dbmod._reconcile_technology_signal_maturity(db)
            row = dbmod.rows(tech_db, "SELECT bucket FROM technology_signals WHERE id=?", (signal_id,))[0]
            self.assertEqual("radar", row["bucket"])

    def test_all_five_canonical_stages_map_as_expected(self):
        expected = {
            "Production": "existing",
            "Industrialisation": "radar",
            "Pré-industrialisation": "radar",
            "Prototype": "radar",
            "R&D": "radar",
        }
        self.assertEqual(expected, {k: v for k, v in dbmod.TECHNOLOGY_STAGE_TO_BUCKET.items() if k in expected})


class NewProcessTechnologyAxesTests(unittest.TestCase):
    def test_three_new_axes_are_registered(self):
        for axis in ("Haute puissance / hauts taux", "Multi-beam / parallélisation", "Fabrication roll-to-roll (batteries)"):
            self.assertIn(axis, PROCESS_TECHNOLOGIES)

    def test_roll_to_roll_alone_is_not_on_topic_without_laser_context(self):
        # §10.10: these new axes carry generic manufacturing terms (roll-to-roll, parallel
        # processing) that must not, by themselves, make unrelated content look laser-relevant.
        text = "This system supports roll-to-roll battery electrode manufacturing."
        labels = {label for label, _ in _match_all_labels(text, PROCESS_TECHNOLOGIES)}
        self.assertIn("Fabrication roll-to-roll (batteries)", labels)
        self.assertFalse(is_on_topic(text))

    def test_roll_to_roll_with_laser_context_is_on_topic(self):
        text = "Femtosecond laser structuring enables roll-to-roll battery electrode manufacturing."
        self.assertTrue(is_on_topic(text))


class CorpusDrivenAxesTests(unittest.TestCase):
    """Quatre axes ajoutés le 09/09/2026 après audit du corpus : 21 des 25 publications
    sortaient sans axe parce que le lexique ne nommait ni le régime burst, ni le soudage de
    transparents, ni l'usinage en volume, ni la texturation de surface.

    Les titres testés sont ceux des publications réellement en base, pas des exemples
    fabriqués : c'est ce qui rend ces tests capables de détecter une régression de lexique.
    """

    AXES = ("Burst GHz/MHz", "Bulk")

    # (titre réel, axe attendu) -- couvre les variantes d'écriture rencontrées : "MHz Burst",
    # "GHz-burst regimes", "laser bursts", "micro-welding" contre "microwelding".
    REELS = (
        ("GHz and MHz Burst enhanced femtosecond laser structuring of electrodes for improved Li-Ion battery performances", "Burst GHz/MHz"),
        ("Comparative study of bulk modifications in borosilicate glass induced by femtosecond laser in single pulse, MHz-, and GHz-burst regimes", "Burst GHz/MHz"),
        ("High-precision polishing of laser-engraved complex profiles using femtosecond laser bursts", "Burst GHz/MHz"),
        ("Enhancement of ultrashort laser pulses absorption in glass using single MHz burst", "Burst GHz/MHz"),
        ("Comparative study of bulk modifications in borosilicate glass induced by femtosecond laser in single pulse, MHz-, and GHz-burst regimes", "Bulk"),
        ("Study of bottom-up column formation in bulk silicon initiated at silicon-air and silicon-glass interfaces by ultrafast laser processing", "Bulk"),
    )

    def test_registered_axes(self):
        for axis in self.AXES:
            with self.subTest(axis=axis):
                self.assertIn(axis, PROCESS_TECHNOLOGIES)

    def test_welding_lives_only_in_operations(self):
        """"Soudage / assemblage de transparents" a vécu un jour dans PROCESS_TECHNOLOGIES.
        OPERATIONS["Soudage"] a absorbé ses termes : deux libellés voisins s'affichaient côte à
        côte dans deux groupes de facettes, pour la même idée."""
        from lexicon import OPERATIONS
        self.assertNotIn("Soudage / assemblage de transparents", PROCESS_TECHNOLOGIES)
        for title in (
            "Large-area glass welding and dissimilar bonding using femtosecond lasers",
            "Large-scale, high-strength transparent welding of thick fused silica using femtosecond laser pulses",
            "Ultrafast laser microwelding for quantum technology",
            "40MHz femtosecond laser single burst to weld glass",
            "Strength-plasticity synergic sapphire/Cu joints via ultrafast laser micro-welding",
        ):
            with self.subTest(title=title[:50]):
                labels = {label for label, _ in _match_all_labels(title, OPERATIONS)}
                self.assertIn("Soudage", labels)

        # Et le vocabulaire absorbé n'ouvre toujours aucune porte : OPERATIONS n'entre pas
        # dans is_on_topic, donc une ligne de soudage industrielle sans terme ultra-rapide
        # reste dehors -- ce que garantissait auparavant _GENERIC_PROCESS_AXES.
        self.assertFalse(is_on_topic("Glass welding line for architectural panels, with dissimilar bonding of frames."))

    def test_real_titles_get_their_axis(self):
        for title, axis in self.REELS:
            with self.subTest(axis=axis, title=title[:60]):
                labels = {label for label, _ in _match_all_labels(title, PROCESS_TECHNOLOGIES)}
                self.assertIn(axis, labels)

    def test_every_new_axis_is_generic(self):
        # C'est le garde-fou qui empêche ces axes de rouvrir le portail d'entrée : "burst mode"
        # est de l'anglais courant en télécom, "bulk silicon" en microélectronique. Les
        # publications visées passant toutes _laser_match, le classement en générique ne coûte
        # aucun document.
        from lexicon import _GENERIC_PROCESS_AXES
        for axis in self.AXES:
            with self.subTest(axis=axis):
                self.assertIn(axis, _GENERIC_PROCESS_AXES)

    def test_texturing_lives_only_in_operations(self):
        """Même sort que "Soudage" : OPERATIONS["Texturation"] portait déjà "texturing" et
        "surface structuring", donc l'axe de procédé écrit la veille était redondant à 90 %."""
        from lexicon import OPERATIONS
        self.assertNotIn("Texturation de surface", PROCESS_TECHNOLOGIES)
        for title in (
            "Monitoring of ultrashort pulse laser surface texturing using spectroscopy and deep learning model",
            "Enabling in-situ monitoring of ultrashort pulse laser surface texturing",
            "Surface smoothing and periodic microstructuring of additively manufactured scalmalloy",
        ):
            with self.subTest(title=title[:50]):
                labels = {label for label, _ in _match_all_labels(title, OPERATIONS)}
                self.assertIn("Texturation", labels)
        # Et le vocabulaire absorbé n'ouvre plus aucune porte : OPERATIONS est hors is_on_topic,
        # alors que cet axe seul faisait entrer 4 projets Horizon sans terme ultra-rapide.
        self.assertFalse(is_on_topic("Laser-based surface texturing of parts for climate neutral manufacturing."))

    def test_new_axes_alone_never_make_content_on_topic(self):
        # Un texte industriel qui porte le vocabulaire mais aucun terme ultra-rapide : c'est
        # exactement ce qui avait laissé entrer RE4DY/iDriving/EEETHOS via "digital twin".
        hors_sujet = (
            "Characterisation of bulk silicon wafers for photovoltaic cell production.",
            "The receiver switches to burst mode during peak data transfer.",
        )
        for text in hors_sujet:
            with self.subTest(text=text[:50]):
                labels = {label for label, _ in _match_all_labels(text, PROCESS_TECHNOLOGIES)}
                self.assertTrue(labels & set(self.AXES), "le texte doit bien porter le vocabulaire")
                self.assertFalse(is_on_topic(text))

    def test_same_content_with_an_ultrafast_term_is_on_topic(self):
        for text in (
            "Femtosecond laser surface texturing of parts.",
            "Ultrafast laser glass welding with dissimilar bonding.",
            "Ultrashort pulse bulk modification of silicon.",
            "GHz burst ablation with an ultrafast laser source.",
        ):
            with self.subTest(text=text[:50]):
                self.assertTrue(is_on_topic(text))

    def test_surface_functionalisation_lives_in_operations_not_here(self):
        """Écrit d'abord ici le 09/09/2026, avec un `requires_any` femto, avant de constater que
        le libellé existait déjà dans OPERATIONS. Les termes y ont été fusionnés et l'entrée
        retirée d'ici : un même libellé dans deux vocabulaires s'afficherait deux fois sur la
        page et fausserait le comptage par famille.

        Le garde `requires_any` est devenu inutile du même coup. OPERATIONS n'entre pas dans
        is_on_topic(), donc ce vocabulaire ne peut plus élargir le portail d'entrée -- alors
        que dans PROCESS_TECHNOLOGIES il l'aurait fait de 38 projets Horizon Europe.
        """
        from lexicon import OPERATIONS
        axis = "Fonctionnalisation de surface"
        self.assertNotIn(axis, PROCESS_TECHNOLOGIES)
        self.assertIn(axis, OPERATIONS)

        # Le vocabulaire classe toujours les mêmes publications, depuis son nouveau foyer.
        for title in (
            "Femtosecond‐Laser Mould Texturing Enables Tunable Biointerfaces in COC Microfluidics",
            "High-throughput fabrication of biofunctional polymer surfaces via ultrafast laser structuring and injection molding",
            "Research on the superhydrophobic surface of silicone rubber induced by femtosecond laser",
        ):
            with self.subTest(title=title[:50]):
                labels = {label for label, _ in _match_all_labels(title, OPERATIONS)}
                self.assertIn(axis, labels)

        # Et il ne fait plus entrer quoi que ce soit : OPERATIONS est hors du filtre d'entrée.
        for text in (
            "Engineered porous electrodes with tunable wettability for redox flow batteries.",
            "Bio-inspired superhydrophobic coating for wind turbine ice protection.",
            "Laser Based Surface Functionalization of parts for climate neutral manufacturing.",
        ):
            with self.subTest(text=text[:50]):
                self.assertFalse(is_on_topic(text))

    def test_superhydrophobic_is_listed_on_its_own(self):
        # _term_pattern pose une frontière (?<!\w) : le préfixe "super" empêche
        # "hydrophobic surface" de matcher "superhydrophobic surface". Sans le terme dédié,
        # l'axe raterait la publication qui l'emploie (doc 206).
        from lexicon import _contains_term
        self.assertFalse(_contains_term("superhydrophobic surface of silicone rubber", "hydrophobic surface"))
        self.assertTrue(_contains_term("superhydrophobic surface of silicone rubber", "superhydrophobic"))

    def test_through_vias_in_glass_matches_tgv(self):
        """L'axe TGV attendait "through glass via" : _term_pattern joint les mots avec [\\s-]+,
        donc la formulation où le matériau est rejeté après le perçage ("through vias in glass
        interposer production", Oxford Lasers) ne matchait pas. Trou trouvé sur une publication
        réelle du corpus.
        """
        from lexicon import APPLICATION_ARCHITECTURES, _rule_matches
        tgv = APPLICATION_ARCHITECTURES["TGV"]
        for text in (
            "Customised through vias in glass interposer production using ultrafast lasers and chemical etching",
            "Through-vias in glass for advanced packaging",
            "Through glass vias for semiconductor interposers",
            "TGV drilling in glass wafers",
        ):
            with self.subTest(text=text[:50]):
                self.assertTrue(_rule_matches(text, tgv))

    def test_tgv_still_needs_its_context_guard(self):
        # requires_any inchangé : "via" seul dans une phrase sans contexte verre/packaging ne
        # doit pas suffire.
        from lexicon import APPLICATION_ARCHITECTURES, _rule_matches
        self.assertFalse(_rule_matches("Delivered via courier to the customer", APPLICATION_ARCHITECTURES["TGV"]))

    def test_bulk_does_not_match_a_maturity_statement(self):
        # "in-volume" est volontairement absent du lexique : _term_pattern accepte l'espace
        # comme séparateur, donc le terme matcherait "in volume production" -- une mention de
        # production industrielle, pas d'usinage en volume.
        text = "Femtosecond laser cutting already in volume production for automotive parts."
        labels = {label for label, _ in _match_all_labels(text, PROCESS_TECHNOLOGIES)}
        self.assertNotIn("Bulk", labels)


if __name__ == "__main__":
    unittest.main()
