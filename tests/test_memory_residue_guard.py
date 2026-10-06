"""Çeviri belleği kalıntı koruması (2026-07-06 sutton smoke).

Bozulmuş NER placeholder kalıntısı (MİYOR0000@@ vb.) içeren çeviriler
cache'e yazılmamalı — sutton smoke'ta zehirli kayıt sonraki segmentlere
"Bellekten" yoluyla geri döndü. _PLACEHOLDER_RE genişletildi.
"""

import os
import tempfile
import unittest

from core.memory import TranslationMemory, _PLACEHOLDER_RE


class TestResiduePattern(unittest.TestCase):
    """Genişletilmiş _PLACEHOLDER_RE deseninin kapsamı."""

    def test_match_mangled_full_wrap(self):
        self.assertTrue(_PLACEHOLDER_RE.search("Yazar @@BİLİNMİYOR0000@@"))

    def test_match_leading_marks_dropped(self):
        self.assertTrue(_PLACEHOLDER_RE.search("Yazar MİYOR0000@@"))

    def test_match_trailing_marks_dropped(self):
        self.assertTrue(_PLACEHOLDER_RE.search("Yazar @@MİYOR0000"))

    def test_match_bare_stem(self):
        self.assertTrue(
            _PLACEHOLDER_RE.search("Yerçekimi-MİYOR0000-MİYOR0001 / 99")
        )

    def test_match_canonical_ner(self):
        self.assertTrue(_PLACEHOLDER_RE.search("@@PERSON0002@@"))

    def test_no_match_clean_turkish(self):
        self.assertIsNone(
            _PLACEHOLDER_RE.search("Bu temiz bir Türkçe cümle, yıl 2024.")
        )

    def test_no_match_latex(self):
        self.assertIsNone(_PLACEHOLDER_RE.search("\\textbf{kalın} ve $x^2$"))

    def test_no_match_isbn(self):
        self.assertIsNone(_PLACEHOLDER_RE.search("ISBN 9781118753651 (Ciltli)"))

    def test_no_match_phone_fax(self):
        self.assertIsNone(_PLACEHOLDER_RE.search("faks (978) 646-8600"))

    def test_no_match_lowercase_turkish_verb(self):
        # Gerçek Türkçe fiil + yıl kombinasyonu yakalanmamalı
        self.assertIsNone(
            _PLACEHOLDER_RE.search("çalışmıyor 2024 yılından beri")
        )


class TestCacheWriteGuardResidue(unittest.TestCase):
    """add() bozulmuş kalıntılı çeviriyi reddetmeli."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.tm = TranslationMemory(db_path=self.tmp.name, project_name="test")

    def tearDown(self):
        self.tm.close()
        os.unlink(self.tmp.name)

    def test_reject_target_with_mangled_residue(self):
        result = self.tm.add("Oscar Whitlow biography", "Merhaba MİYOR0000@@ dünya")
        self.assertFalse(result)
        self.assertIsNone(self.tm.get("Oscar Whitlow biography"))

    def test_reject_target_with_bare_residue(self):
        result = self.tm.add(
            "Gravity-Free Drag-Free Space Flight / 99",
            "Yerçekimi-MİYOR0000-MİYOR0001 / 99",
        )
        self.assertFalse(result)

    def test_accept_clean_translation(self):
        result = self.tm.add("Hello world", "Merhaba dünya")
        self.assertTrue(result)
        entry = self.tm.get("Hello world")
        self.assertIsNotNone(entry)


if __name__ == "__main__":
    unittest.main()
