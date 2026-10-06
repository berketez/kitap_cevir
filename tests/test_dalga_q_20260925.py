"""Dalga Q (2026-09-25) — QC çok dilli + derleme çıktısı.

Q1  Kaynak taraf atıf sözcükleri altı kaynak dilde (patterns.XREF_LABEL_WORDS):
    "Рис. 1.1", "Таблица 1.1", "Abb.", "Tabla", "Gl. (1.4)" artık kaynakta
    atıf; Rusça bölümde her doğru şekil numarası "Uydurulmus referans"
    CRITICAL alıyordu. Türkçe taraf deseni birebir aynı kaldı.
Q2  Yol A'da \\footnote{Çevirmen notu: ...} uzunluk oranı / halüsinasyon /
    bilgi kaybı hesaplarına sayılmaz (başka dipnotlar sayılır).
Q3  Kesme işaretli Türkçe ek ("Mars'a giden") İngilizce artikel değildir;
    artikel bulgusu rapor-only (AutoFixer'da düzeltici yok), AutoFixer bu
    biçime iki yolda da dokunmaz.
Q4  2_derle: dil-nötr çıkış 3 iletisi + --json qc_score / qc_report /
    qc_top / epub_note alanları (sözleşme S4, EPUB notu S5).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from latex_pipeline.autofix import AutoFixer
from latex_pipeline.patterns import (
    TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE,
    XREF_LABEL_WORDS,
    xref_label_alternation,
)
from latex_pipeline.qc_engine import (
    QCEngine,
    QCIssue,
    QCReport,
    parse_qc_report_summary,
    strip_translator_notes,
)

derle = importlib.import_module("2_derle")


@pytest.fixture
def engine():
    return QCEngine()


def _issues(report, check, needle=""):
    return [i for i in report.issues
            if i.check_name == check and needle in i.message]


# ===========================================================================
# Q1 — altı dilde kaynak atıf sözcükleri
# ===========================================================================

# 2026-09-24 deseni (çeviri tarafı) — birebir korunmalı.
_OLD_TR_XREF_PATTERN = (
    r"\b(?:"
    r"(?:Figures?|Figs?\.?|Tables?|Tab\.?|Definitions?|Def\.?"
    r"|Theorems?|Thm\.?|Lemmas?|Propositions?|Prop\.?"
    r"|Corollar(?:y|ies)|Examples?|Ex\.?"
    r"|Exercises?|Algorithms?|Sections?|Sec\.?|Chapters?|Ch\.?"
    r"|Equations?|Eqs?\.?|Remarks?|Proofs?|Propert(?:y|ies)"
    r"|Appendix|Appendices"
    r"|(?:Şekil|Tablo|Tanım|Teorem|Lemma|Önerme|Sonuç|Örnek"
    r"|Alıştırma|Algoritma|Bölüm|Denklem|İspat|Özellik)(?:ler|lar)?"
    r"|(?-i:Şek\.|Denk\.)"
    r")[\s~]*(\d+(?:\.\d+)*)"
    r"|(?:Teorem|Lemma|Önerme|Sonuç|Örne[kğ]|Alıştırma|Algoritma"
    r"|Bölüm|Denklem|İspat|Özellik"
    r")\w+[\s~]*(\d+(?:\.\d+)+)"
    r")"
)

_SRC = {
    "en": ("As shown in Figure 1.2 and Table 1.1, Eq. (1.3) holds for the "
           "nozzle; see Chapter 2 and Section 2.4 for the derivation."),
    "de": ("Wie in Abb. 1.2 und Tabelle 1.1 gezeigt, gilt Gl. (1.3) für die "
           "Düse; siehe Kapitel 2 und Abschnitt 2.4 für die Herleitung."),
    "ru": ("Как показано на рис. 1.2 и в табл. 1.1, для сопла справедлива "
           "формула (1.3); вывод см. в гл. 2 и разделе 2.4."),
    "fr": ("Comme le montrent la figure 1.2 et le tableau 1.1, l'équation "
           "(1.3) est vérifiée pour la tuyère ; voir le chapitre 2 et la "
           "section 2.4 pour la démonstration."),
    "es": ("Como se muestra en la Figura 1.2 y la Tabla 1.1, la ecuación "
           "(1.3) se cumple para la tobera; véase el capítulo 2 y la "
           "sección 2.4 para la deducción."),
    "it": ("Come mostrato nella Figura 1.2 e nella Tabella 1.1, l'equazione "
           "(1.3) vale per l'ugello; si veda il capitolo 2 e la sezione 2.4 "
           "per la derivazione."),
}
_TR = ("Şekil 1.2 ve Tablo 1.1'de görüldüğü gibi, lüle için Denklem 1.3 "
       "geçerlidir; türetme için bkz. Bölüm 2 ve Bölüm 2.4.")


class TestQ1SourceLabels:
    def test_tr_side_pattern_unchanged(self):
        assert QCEngine._XREF_NUM_RE.pattern == _OLD_TR_XREF_PATTERN

    def test_constant_covers_six_source_languages(self):
        for cat in ("figure", "table", "equation", "chapter"):
            assert set(XREF_LABEL_WORDS[cat]) == {"en", "de", "ru", "fr",
                                                  "es", "it"}, cat

    @pytest.mark.parametrize("lang", sorted(_SRC))
    def test_correct_translation_no_fabrication(self, engine, lang):
        rep = engine.analyze(_TR, _SRC[lang], manual_translation=True)
        assert _issues(rep, "hallucination_pattern", "Uydurulmus") == []
        assert _issues(rep, "number_hallucination") == []

    @pytest.mark.parametrize("lang", sorted(_SRC))
    def test_source_numbers_found(self, lang):
        nums = QCEngine._xref_numbers(_SRC[lang], source=True)
        assert {"1.2", "1.1", "2", "2.4"} <= set(nums)

    @pytest.mark.parametrize("text,expected", [
        ("см. рис. 1.2 и табл. 1.1", ["1.2", "1.1"]),
        ("Рис 1.8. Схема РДТТ", ["1.8"]),             # OCR: nokta düşmüş
        ("РИСУНОК 3 и ТАБЛИЦА 4", ["3", "4"]),        # büyük harf, Kiril
        ("Abbildungen 2 und Tab. 3", ["2", "3"]),
        ("le tableau 3 et le chap. 5", ["3", "5"]),
        ("la Tabla 3 del Capítulo 4", ["3", "4"]),
        ("la tabella 2.1 nel capitolo 3", ["2.1", "3"]),
    ])
    def test_source_side_forms(self, text, expected):
        assert QCEngine._xref_numbers(text, source=True) == expected

    def test_foreign_labels_not_on_tr_side(self):
        # Çeviri tarafı değişmedi: yabancı etiket orada atıf sayılmaz.
        assert QCEngine._xref_numbers("рис. 1.2 ve Abb. 3") == []

    def test_real_fabrication_still_caught_ru(self, engine):
        src = "Как показано на рис. 1.2, давление падает вдоль сопла камеры."
        tr = ("Şekil 1.2'de ve Şekil 1.7'de görüldüğü gibi basınç lüle "
              "boyunca düşer.")
        rep = engine.analyze(tr, src, manual_translation=True)
        assert _issues(rep, "hallucination_pattern", "'1.7'"), rep.issues
        assert _issues(rep, "number_hallucination", "'1.7'")

    def test_real_fabrication_still_caught_en(self, engine):
        src = "As shown in Figure 1.2, pressure drops along the nozzle."
        tr = "Şekil 1.2 ve Şekil 1.9'da görüldüğü gibi basınç düşer."
        rep = engine.analyze(tr, src, manual_translation=True)
        assert _issues(rep, "hallucination_pattern", "'1.9'")

    def test_missing_number_caught_de(self, engine):
        src = "Siehe Abb. 1.2 und Abb. 1.3 für den Druckverlauf in der Düse."
        tr = "Düzedeki basınç dağılımı için bkz. Şekil 1.2."
        rep = engine.analyze(tr, src, manual_translation=True)
        assert _issues(rep, "number_hallucination", "'1.3'")

    def test_paren_equation_source_fabrication_check(self, engine):
        """'формулы (1.4)' kaynakta denklem atfı: 'Denklem 1.4' uydurma değil."""
        src = "Подставляя значения в формулу (1.4), получаем тягу двигателя."
        tr = "Değerleri Denklem 1.4'te yerine koyarak motor itkisini buluruz."
        rep = engine.analyze(tr, src, manual_translation=True)
        assert _issues(rep, "hallucination_pattern", "Uydurulmus") == []
        assert _issues(rep, "number_hallucination") == []

    def test_english_chap_sect_abbrev_recognised(self):
        # Kasıtlı genişleme: "Chap."/"Sect." İngilizcede de atıf kısaltması
        # (bir test kitabında "Sect. 6.4" -> "Bölüm 6.4" sahte HIGH alıyordu).
        assert QCEngine._xref_numbers("see Chap. 4 and Sect. 6.4",
                                      source=True) == ["4", "6.4"]

    def test_ocr_dot_loss_reconciled(self, engine):
        """Kaynak OCR'ı 'Рис. 1.1'i 'Рис 11' okumuş; çevirmen 1.1 yazmış."""
        src = ("Рис 11 Схема испытательного стенда\n\n"
               "Схема приведена на рис. 1.2 и далее.")
        tr = ("Şekil 1.1. Deney tezgâhının şeması\n\n"
              "Şema Şekil 1.2'de verilmiştir.")
        rep = engine.analyze(tr, src, manual_translation=True)
        assert _issues(rep, "number_hallucination") == [], rep.issues

    def test_dotted_source_number_not_reconciled(self, engine):
        """'Рис. 4.18' -> 'Şekil 1.18': rakam değişimi raporlanmaya devam eder."""
        src = "Рис. 4.18. ОМ-7 — опытный учебный двигатель"
        tr = "Şekil 1.18. OM-7 — deneysel eğitim motoru"
        rep = engine.analyze(tr, src, manual_translation=True)
        assert _issues(rep, "number_hallucination", "'4.18'")

    def test_reconcile_skips_leading_zero(self):
        missing = [("005", 1, 0, 1)]
        halluc = [("0.05", 1)]
        from collections import Counter
        m, h = QCEngine._reconcile_ocr_dot_loss(
            missing, halluc, Counter({"005": 1}), Counter({"0.05": 1}))
        assert m == missing and h == halluc

    def test_reconcile_partial_count(self):
        from collections import Counter
        m, h = QCEngine._reconcile_ocr_dot_loss(
            [("11", 3, 0, 3)], [],
            Counter({"11": 3, "1.1": 1}), Counter({"1.1": 3}))
        # 1.1 fazlası 2 -> 3 eksikten 2'si uzlaşır, 1 kalır
        assert m == [("11", 3, 0, 1)]


# ===========================================================================
# Q2 — çevirmen notu dipnotları uzunluk hesaplarına sayılmaz (Yol A)
# ===========================================================================

_NOTE = ("\\footnote{Çevirmen notu: Şemadaki etiketler yukarıdan aşağıya "
         "şöyledir: pompalar santrifüj ve pozitif yer değiştirmeli olarak "
         "ikiye ayrılır; pozitif olanlar pistonlu, dişli, vidalı ve "
         "hibrit motorlardır. Kısaltmalar metinde ayrıca açıklanmıştır "
         "ve sonraki geçişlerde tekrar edilmez.}")


class TestQ2TranslatorNotes:
    def test_strip_basic(self):
        txt = "Şekil 1.1. Sınıflandırma" + _NOTE + " devam."
        assert strip_translator_notes(txt) == "Şekil 1.1. Sınıflandırma devam."

    def test_strip_nested_and_variants(self):
        txt = ("a\\footnote{\\textit{Çevirmen notu:} $p_{\\text{н}}$ "
               "dış basınç.} b\\footnote{çevirmen notu: küçük harf} c"
               "\\footnote[3]{Çeviri notu: köşeli} d")
        assert strip_translator_notes(txt) == "a b c d"

    def test_other_footnotes_untouched(self):
        txt = "a\\footnote{Kaynak: Glushko, 1946.} b"
        assert strip_translator_notes(txt) == txt

    def test_unbalanced_left_as_is(self):
        txt = "a\\footnote{Çevirmen notu: kapanmayan"
        assert strip_translator_notes(txt) == txt

    def test_escaped_brace_inside_note(self):
        txt = "a\\footnote{Çevirmen notu: küme \\{x\\} gösterimi} b"
        assert strip_translator_notes(txt) == "a b"

    def test_open_pattern_needs_footnote(self):
        assert not TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE.search(
            "Çevirmen notu: düz metin, dipnot değil")

    _EN = ("\\chapter{Классификация}\n\n"
           "Рис. 1.1. Схема испытательного стенда для малых двигателей.\n\n"
           "Далее подробно рассматриваются узлы этого стенда.\n")
    _TR_BODY = ("\\chapter{Sınıflandırma}\n\n"
                "Şekil 1.1. Küçük motorlar için deney tezgâhının "
                "şeması." + _NOTE + "\n\n"
                "Aşağıda bu tezgâhın parçaları ayrıntılı incelenir.\n")

    def test_para_ratio_ignores_note_in_manual(self, engine):
        rep = engine.analyze(self._TR_BODY, self._EN, manual_translation=True)
        assert _issues(rep, "hallucination", "Possible hallucination") == []

    def test_para_ratio_counts_note_in_yol_b(self, engine):
        # Yol B'de (MT) not bir sızıntıdır: davranış değişmedi.
        rep = engine.analyze(self._TR_BODY, self._EN, manual_translation=False)
        assert _issues(rep, "hallucination", "Possible hallucination")

    def test_other_footnote_still_counts_in_manual(self, engine):
        other = _NOTE.replace("Çevirmen notu:", "Ek bilgi:")
        tr = self._TR_BODY.replace(_NOTE, other)
        rep = engine.analyze(tr, self._EN, manual_translation=True)
        assert _issues(rep, "hallucination", "Possible hallucination")

    def test_doc_length_ratio_ignores_notes_in_manual(self, engine):
        en_para = "Двигатель создает тягу за счет истечения газов из сопла."
        tr_para = ("Motor, gazların lüleden akışıyla itki üretir." + _NOTE
                   + _NOTE.replace("Şemadaki", "Tablodaki"))
        en = "\n\n".join([en_para] * 4)
        tr = "\n\n".join([tr_para] * 4)
        manual = engine.analyze(tr, en, manual_translation=True)
        yol_b = engine.analyze(tr, en, manual_translation=False)
        assert _issues(manual, "sentence_length_alignment", "EN length") == []
        assert _issues(yol_b, "sentence_length_alignment", "EN length")


# ===========================================================================
# Q3 — kesme işaretli Türkçe ek artikel değildir
# ===========================================================================

class TestQ3ApostropheSuffix:
    @pytest.mark.parametrize("line", [
        "İzmir'e, Bursa'ya ve Konya'ya giden yeni otobüs hatlarını açtı.",
        "Yolcuları Ankara'ya bırakıp İstanbul'a geri getiren seferler oldu.",
        "Motorlar NASA'ya ait test tezgâhında denendi ve onaylandı.",
        "Yolcuları Ankara’ya bırakıp geri getiren seferler başarılı oldu.",
        "Newton‘a göre kuvvet kütle ile ivmenin çarpımına eşittir.",
    ])
    def test_suffix_not_article(self, engine, line):
        rep = engine.analyze(line)
        assert _issues(rep, "turkish_naturalness", "artikel") == []

    def test_real_article_still_flagged(self, engine):
        rep = engine.analyze("Bu görevde a spacecraft ve bir füze kullanıldı.")
        found = _issues(rep, "turkish_naturalness", "'a spacecraft'")
        assert found
        assert all(not i.auto_fixable for i in found), (
            "düzeltici yok; '[auto-fixable]' etiketi yanıltıcıydı")

    @pytest.mark.parametrize("manual", [False, True])
    def test_autofixer_never_touches_suffix(self, manual):
        body = ("\\chapter{Görevler}\n\n"
                "Belediye İzmir'e, Bursa'ya ve Konya'ya giden hatlar ile "
                "NASA'ya ait a spacecraft araçlarını karşılaştırdı.\n")
        fixer = AutoFixer(qc_engine=QCEngine(), translator=None,
                          manual_translation=manual)
        fixed, _ = fixer.run(body, "")
        for token in ("İzmir'e,", "Bursa'ya", "Konya'ya giden", "NASA'ya"):
            assert token in fixed, (token, fixed)


# ===========================================================================
# Q4 — 2_derle: dil-nötr ileti + S4 JSON alanları
# ===========================================================================

_QC_TEXT = """============================================================
QC ENGINE REPORT
============================================================

  Score: 41/100 (D)
  Total issues: 6
    CRITICAL: 3
    HIGH:     1
    MEDIUM:   2
    LOW:      0
  Auto-fixable: 0

  Issues by check:
    number_hallucination: 2
    hallucination: 2
    mixed_language_line: 1
    first_person_academic: 1

  Details:
    [CRITICAL] L1: x
============================================================
"""


def _json_tail(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


class TestQ4Derle:
    @pytest.fixture
    def book(self, tmp_path):
        out = tmp_path / "output" / "kitap"
        (out / "source").mkdir(parents=True)
        (out / "source" / "body.tex").write_text("x" * 200, encoding="utf-8")
        (out / "full_book.md").write_text("# kitap\n", encoding="utf-8")
        (out / "compiled").mkdir()
        (out / "compiled" / "kitap_TR.pdf").write_bytes(b"%PDF " + b"x" * 4096)
        tex = tmp_path / "EXPORT_TR.tex"
        tex.write_text("\\chapter{Deneme}\n" + "içerik " * 50,
                       encoding="utf-8")
        return out, tex

    def _run(self, monkeypatch, out, tex, exit_code, qc_text=None,
             extra=()):
        def _fake_pipeline(*a, **k):
            if qc_text is not None:
                (out / "qc_report.txt").write_text(qc_text, encoding="utf-8")
            return exit_code
        monkeypatch.setattr(derle, "run_pipeline", _fake_pipeline)
        monkeypatch.setattr(sys, "argv", ["2_derle.py", str(tex),
                                          "--output-dir", str(out), "--json",
                                          *extra])
        return derle.main()

    def test_exit3_message_language_neutral(self, book, monkeypatch, capsys):
        out, tex = book
        code = self._run(monkeypatch, out, tex, 3, qc_text=_QC_TEXT)
        text = capsys.readouterr().out
        assert code == 3
        assert "İngilizce" not in text
        assert "kaynak dilde kalmış" in text
        assert "kaynakla eşleşmiyor" in text
        assert "QC skoru: 41/100" in text

    def test_json_qc_fields(self, book, monkeypatch, capsys):
        out, tex = book
        self._run(monkeypatch, out, tex, 3, qc_text=_QC_TEXT)
        data = _json_tail(capsys.readouterr().out)
        assert data["qc_score"] == 41.0
        assert data["qc_report"] == str(out / "qc_report.txt")
        assert data["qc_top"] == [
            {"check": "number_hallucination", "count": 2},
            {"check": "hallucination", "count": 2},
            {"check": "mixed_language_line", "count": 1},
        ]
        assert data["epub"] is None and data["epub_note"] is None

    def test_stale_qc_report_not_reported(self, book, monkeypatch, capsys):
        out, tex = book
        rep = out / "qc_report.txt"
        rep.write_text(_QC_TEXT, encoding="utf-8")
        old = time.time() - 3600
        os.utime(rep, (old, old))
        self._run(monkeypatch, out, tex, 1)            # QC hiç koşmadı
        data = _json_tail(capsys.readouterr().out)
        assert data["qc_score"] is None
        assert data["qc_report"] is None
        assert data["qc_top"] == []

    def test_early_exit_has_same_field_set(self, tmp_path, monkeypatch,
                                           capsys):
        monkeypatch.setattr(sys, "argv", ["2_derle.py",
                                          str(tmp_path / "yok.tex"), "--json"])
        assert derle.main() == 1
        data = _json_tail(capsys.readouterr().out)
        for key in ("qc_score", "qc_report", "qc_top", "epub", "epub_note"):
            assert key in data
        assert data["error"] == "input_missing"

    def _epub_setup(self, out, monkeypatch, result=None, note=None,
                    note_age=None, raise_exc=None):
        (out / "source_book.epub").write_bytes(b"PK" + b"x" * 2048)

        def _fake_epub(original_epub, output_dir, book_name):
            if note is not None:
                p = Path(output_dir) / "compiled" / derle.EPUB_SKIP_NOTE_NAME
                p.write_text(note + "\n", encoding="utf-8")
                if note_age:
                    t = time.time() - note_age
                    os.utime(p, (t, t))
            if raise_exc:
                raise raise_exc
            return result

        import latex_pipeline.epub_output as epub_mod
        monkeypatch.setattr(epub_mod, "generate_epub_output", _fake_epub)

    def test_epub_note_from_s5_file(self, book, monkeypatch, capsys):
        out, tex = book
        self._epub_setup(out, monkeypatch, result=None,
                         note="Bölümler kaynakla hizalanamadı; EPUB atlandı.")
        self._run(monkeypatch, out, tex, 0, qc_text=_QC_TEXT)
        text = capsys.readouterr().out
        data = _json_tail(text)
        assert data["epub"] is None
        assert data["epub_note"] == "Bölümler kaynakla hizalanamadı; EPUB atlandı."
        assert "EPUB üretilmedi" in text

    def test_epub_note_stale_file_ignored(self, book, monkeypatch, capsys):
        out, tex = book
        self._epub_setup(out, monkeypatch, result=None,
                         note="eski koşudan kalma", note_age=3600)
        self._run(monkeypatch, out, tex, 0, qc_text=_QC_TEXT)
        assert _json_tail(capsys.readouterr().out)["epub_note"] is None

    def test_epub_note_on_exception(self, book, monkeypatch, capsys):
        out, tex = book
        self._epub_setup(out, monkeypatch, raise_exc=RuntimeError("bozuk"))
        self._run(monkeypatch, out, tex, 0, qc_text=_QC_TEXT)
        note = _json_tail(capsys.readouterr().out)["epub_note"]
        assert note and "bozuk" in note

    def test_epub_generated_no_note(self, book, monkeypatch, capsys):
        out, tex = book
        epub = out / "compiled" / "kitap_TR.epub"
        self._epub_setup(out, monkeypatch, result=str(epub))
        self._run(monkeypatch, out, tex, 0, qc_text=_QC_TEXT)
        data = _json_tail(capsys.readouterr().out)
        assert data["epub"] == str(epub) and data["epub_note"] is None


class TestQ4SummaryParser:
    def test_round_trip_with_print_summary(self, capsys):
        rep = QCReport()
        for check, n, sev in (("hallucination", 3, "critical"),
                              ("number_hallucination", 5, "high"),
                              ("turkish_naturalness", 1, "medium"),
                              ("mixed_language_line", 2, "medium")):
            for _ in range(n):
                rep.issues.append(QCIssue(check, sev, 1, "m", False, 0.9,
                                          "", ""))
        rep.score = 12.0
        rep.print_summary()
        parsed = parse_qc_report_summary(capsys.readouterr().out)
        assert parsed["score"] == 12.0
        assert parsed["top"] == [
            {"check": "number_hallucination", "count": 5},
            {"check": "hallucination", "count": 3},
            {"check": "mixed_language_line", "count": 2},
        ]

    def test_no_score_line(self):
        assert parse_qc_report_summary("boş rapor") is None

    def test_no_issues(self, capsys):
        rep = QCReport()
        rep.score = 100.0
        rep.print_summary()
        assert parse_qc_report_summary(capsys.readouterr().out) == {
            "score": 100.0, "top": []}
