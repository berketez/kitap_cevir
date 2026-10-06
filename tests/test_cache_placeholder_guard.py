"""
Cache Placeholder Guard Tests
- Fix 2: add() placeholder iceren veriyi reddetmeli
- Fix 3: find_similar() placeholder iceren sonuclari filtrelemeli
- Fix 4: guillemet format temizligi
"""

import os
import re
import sqlite3
import tempfile
import unittest

# Proje kokunu path'e ekle
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.memory import TranslationMemory, _PLACEHOLDER_RE


class TestPlaceholderRegex(unittest.TestCase):
    """_PLACEHOLDER_RE pattern testi"""

    def test_matches_latex_placeholder(self):
        self.assertTrue(_PLACEHOLDER_RE.search("Bu <<LATEX_42>> metindir"))

    def test_matches_ref_placeholder(self):
        self.assertTrue(_PLACEHOLDER_RE.search("Bakiniz <<REF_3>>"))

    def test_matches_term_placeholder(self):
        self.assertTrue(_PLACEHOLDER_RE.search("<<TERM_17>> terimi"))

    def test_matches_cite_placeholder(self):
        self.assertTrue(_PLACEHOLDER_RE.search("<<CITE_5>> referansi"))

    def test_matches_url_placeholder(self):
        self.assertTrue(_PLACEHOLDER_RE.search("Link: <<URL_1>>"))

    def test_matches_auth_placeholder(self):
        self.assertTrue(_PLACEHOLDER_RE.search("Yazar <<AUTH_9>>"))

    def test_no_match_clean_text(self):
        self.assertIsNone(_PLACEHOLDER_RE.search("Bu temiz bir Turkce cumle"))

    def test_no_match_latex_command(self):
        self.assertIsNone(_PLACEHOLDER_RE.search("\\textbf{bold} ve $x^2$"))

    def test_no_match_angle_brackets_no_type(self):
        self.assertIsNone(_PLACEHOLDER_RE.search("<<123>>"))

    def test_no_match_lowercase_type(self):
        self.assertIsNone(_PLACEHOLDER_RE.search("<<latex_42>>"))


class TestCacheWriteGuard(unittest.TestCase):
    """Fix 2: add() placeholder guard testi"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.tm = TranslationMemory(db_path=self.tmp.name, project_name="test")

    def tearDown(self):
        self.tm.close()
        os.unlink(self.tmp.name)

    def test_reject_target_with_latex_placeholder(self):
        result = self.tm.add("Hello world", "Merhaba <<LATEX_5>> dunya")
        self.assertFalse(result)
        # DB'de olmamali
        entry = self.tm.get("Hello world")
        self.assertIsNone(entry)

    def test_reject_target_with_ref_placeholder(self):
        result = self.tm.add("See Figure 3", "Bakiniz Sekil <<REF_3>>")
        self.assertFalse(result)
        entry = self.tm.get("See Figure 3")
        self.assertIsNone(entry)

    def test_reject_source_with_placeholder(self):
        result = self.tm.add("Hello <<LATEX_1>> world", "Merhaba dunya")
        self.assertFalse(result)

    def test_accept_clean_text(self):
        result = self.tm.add("Hello world", "Merhaba dunya")
        self.assertTrue(result)
        entry = self.tm.get("Hello world")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["target_text"], "Merhaba dunya")

    def test_reject_multiple_placeholders(self):
        result = self.tm.add(
            "From <<LATEX_1>> to <<LATEX_2>>",
            "<<LATEX_1>>'den <<LATEX_2>>'ye"
        )
        self.assertFalse(result)

    def test_reject_only_placeholder(self):
        result = self.tm.add("<<LATEX_5>>", "<<LATEX_5>>")
        self.assertFalse(result)


class TestCacheReadGuard(unittest.TestCase):
    """Fix 3: find_similar() ve get() placeholder filtreleme testi"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.tm = TranslationMemory(db_path=self.tmp.name, project_name="test")

        # Direkt SQL ile kirli entry ekle (guard'i bypass ederek)
        cursor = self.tm.conn.cursor()
        cursor.execute("""
            INSERT INTO translations
            (hash, source_text, target_text, project, confirmed)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "dirty_hash_1",
            "The force acting on the mass",
            "Kutle uzerine etkiyen <<LATEX_42>> kuvvet",
            "test",
            0,
        ))
        cursor.execute("""
            INSERT INTO translations
            (hash, source_text, target_text, project, confirmed)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "dirty_hash_2",
            "<<LATEX_99>> placeholder source",
            "Placeholder kaynak",
            "test",
            0,
        ))
        # Temiz entry de ekle
        cursor.execute("""
            INSERT INTO translations
            (hash, source_text, target_text, project, confirmed)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "clean_hash_1",
            "The force acting on the body",
            "Cisim uzerine etkiyen kuvvet",
            "test",
            0,
        ))
        self.tm.conn.commit()

    def tearDown(self):
        self.tm.close()
        os.unlink(self.tmp.name)

    def test_find_similar_filters_dirty_target(self):
        """Fuzzy match kirli target entry'yi dondurmemeli"""
        results = self.tm.find_similar("The force acting on the mass", threshold=70)
        for r in results:
            self.assertNotIn("<<LATEX_", r["target"])

    def test_find_similar_filters_dirty_source(self):
        """Fuzzy match kirli source entry'yi dondurmemeli"""
        results = self.tm.find_similar("<<LATEX_99>> placeholder source", threshold=70)
        for r in results:
            self.assertIsNone(_PLACEHOLDER_RE.search(r["source"]))
            self.assertIsNone(_PLACEHOLDER_RE.search(r["target"]))

    def test_find_similar_returns_clean(self):
        """Temiz entry'ler dondurulebilmeli"""
        results = self.tm.find_similar("The force acting on the body", threshold=90)
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0]["target"], "Cisim uzerine etkiyen kuvvet")

    def test_get_filters_dirty_exact_match(self):
        """get() kirli exact match dondurmemeli"""
        # Direkt SQL ile hash'i dogru sekilde ekle
        import hashlib
        text = "dirty exact test"
        h = hashlib.md5(text.strip().lower().encode('utf-8')).hexdigest()
        cursor = self.tm.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO translations
            (hash, source_text, target_text, project, confirmed)
            VALUES (?, ?, ?, ?, ?)
        """, (h, text, "Kirli <<REF_7>> exact sonuc", "test", 0))
        self.tm.conn.commit()

        result = self.tm.get("dirty exact test")
        self.assertIsNone(result)


class TestCleanPlaceholderEntries(unittest.TestCase):
    """clean_placeholder_entries() metod testi"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.tm = TranslationMemory(db_path=self.tmp.name, project_name="test")

        # Kirli entry'ler ekle (SQL ile)
        cursor = self.tm.conn.cursor()
        for i in range(5):
            cursor.execute("""
                INSERT INTO translations
                (hash, source_text, target_text, project)
                VALUES (?, ?, ?, ?)
            """, (f"dirty_{i}", f"Source {i}", f"Target <<LATEX_{i}>>", "test"))
        # Temiz entry'ler ekle
        for i in range(3):
            cursor.execute("""
                INSERT INTO translations
                (hash, source_text, target_text, project)
                VALUES (?, ?, ?, ?)
            """, (f"clean_{i}", f"Clean source {i}", f"Temiz hedef {i}", "test"))
        self.tm.conn.commit()

    def tearDown(self):
        self.tm.close()
        os.unlink(self.tmp.name)

    def test_clean_removes_dirty(self):
        deleted = self.tm.clean_placeholder_entries()
        self.assertEqual(deleted, 5)

    def test_clean_keeps_clean(self):
        self.tm.clean_placeholder_entries()
        stats = self.tm.get_stats()
        self.assertEqual(stats["total_entries"], 3)


class TestGuilleMetPattern(unittest.TestCase):
    """Fix 4: guillemet format placeholder regex testi"""

    def setUp(self):
        # autofix.py'deki pattern'i tekrar olustur
        self._GUILLEMET_RE = re.compile(
            r"\u00ab([A-Z]+)"
            r"[\u2080-\u2089]+"
            r"\s*>{1,2}"
        )

    def test_matches_basic_guillemet(self):
        # \u00ab = «, \u2081 = ₁, \u2082 = ₂, \u2087 = ₇
        text = "\u00abLATEX\u2081\u2082\u2087 >>"
        self.assertTrue(self._GUILLEMET_RE.search(text))

    def test_matches_no_space(self):
        text = "\u00abLATEX\u2081\u2082\u2087>>"
        self.assertTrue(self._GUILLEMET_RE.search(text))

    def test_matches_single_gt(self):
        text = "\u00abLATEX\u2081\u2082\u2087>"
        self.assertTrue(self._GUILLEMET_RE.search(text))

    def test_matches_ref_type(self):
        text = "\u00abREF\u2083 >>"
        self.assertTrue(self._GUILLEMET_RE.search(text))

    def test_no_match_normal_guillemet(self):
        # Normal guillemet kullanimi (yazi icinde)
        text = "\u00abmerhaba\u00bb"
        self.assertIsNone(self._GUILLEMET_RE.search(text))

    def test_no_match_standard_placeholder(self):
        # Standard <<LATEX_127>> guillemet degildir
        text = "<<LATEX_127>>"
        self.assertIsNone(self._GUILLEMET_RE.search(text))

    def test_cleanup_in_context(self):
        text = "Bu \u00abLATEX\u2081\u2082\u2087 >> bir denklemdir."
        cleaned = self._GUILLEMET_RE.sub("", text)
        self.assertEqual(cleaned, "Bu  bir denklemdir.")

    def test_multiple_guillemets(self):
        text = "\u00abLATEX\u2081 >> ve \u00abREF\u2083\u2084 >>"
        matches = self._GUILLEMET_RE.findall(text)
        self.assertEqual(len(matches), 2)


class TestBulkAddGuard(unittest.TestCase):
    """bulk_add() da guard'dan gecmeli"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.tm = TranslationMemory(db_path=self.tmp.name, project_name="test")

    def tearDown(self):
        self.tm.close()
        os.unlink(self.tmp.name)

    def test_bulk_add_filters_dirty(self):
        translations = [
            ("Hello", "Merhaba"),
            ("See <<LATEX_1>>", "Bakiniz <<LATEX_1>>"),
            ("World", "Dunya"),
        ]
        self.tm.bulk_add(translations)
        stats = self.tm.get_stats()
        self.assertEqual(stats["total_entries"], 2)  # kirli olan eklenmemeli


if __name__ == "__main__":
    unittest.main()
