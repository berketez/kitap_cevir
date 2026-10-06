"""2026-07-11 Yol A + Yol B zayiflik denetimi fix'lerinin regresyon testleri.

Denetim: 11 boyutlu çok-ajanlı tarama (88 ham bulgu) + elle doğrulama.
Her test sınıfı bir bulgu numarasına (audit_findings.json) karşılık gelir.
"""

import re

import pytest


# ---------------------------------------------------------------------------
# #10/#11 — core/memory.py: placeholder + echo zehiri
# ---------------------------------------------------------------------------

class TestMemoryPoisonGuards:
    def test_placeholder_re_catches_turkish_letters(self):
        """[[BİRİM_0]] (MT'nin çevirdiği placeholder gövdesi) yakalanmalı."""
        from core.memory import _PLACEHOLDER_RE
        assert _PLACEHOLDER_RE.search("deger [[BİRİM_0]] olarak")
        assert _PLACEHOLDER_RE.search("[[BIRIM_3]]")
        assert _PLACEHOLDER_RE.search("[[UNIT_12]]")
        assert _PLACEHOLDER_RE.search("[[PGREF_0]]")
        assert not _PLACEHOLDER_RE.search("normal [koseli] metin")

    def test_poison_echo_detection(self):
        from core.memory import _is_poison_echo
        # Ingilizce echo (stopword'lu) -> zehir
        assert _is_poison_echo("The Anchor Escapement", "The Anchor Escapement")
        # Ingilizce echo (stopword'suz TOC basligi, >=2 kelime) -> zehir
        assert _is_poison_echo("Kernel Regression", "Kernel Regression")
        # Turkce karakterli birebir segment -> zehir DEGIL
        assert not _is_poison_echo("Çeviri örneği", "Çeviri örneği")
        # Tek token URL -> zehir DEGIL
        assert not _is_poison_echo(
            "http://en.wikipedia.org/wiki/X", "http://en.wikipedia.org/wiki/X"
        )
        # Kaynak != hedef -> echo degil
        assert not _is_poison_echo("The force", "Kuvvet")

    def test_add_rejects_echo_and_protects_confirmed(self, tmp_path):
        from core.memory import TranslationMemory
        tm = TranslationMemory(db_path=str(tmp_path / "t.db"), project_name="t")
        # Echo yazilmaz
        assert tm.add("The laws of motion", "The laws of motion") is False
        assert tm.get("The laws of motion") is None
        # Onayli ceviri sonraki MT ciktisiyla EZILMEZ
        assert tm.add("source text here", "insan cevirisi", confirmed=True)
        assert tm.add("source text here", "kotu mt ciktisi") is True
        row = tm.get("source text here")
        assert row["target_text"] == "insan cevirisi"
        tm.close()

    def test_clean_poisoned_entries_removes_live_poison(self, tmp_path):
        import sqlite3
        from core.memory import TranslationMemory
        db = str(tmp_path / "t.db")
        # Zehirli kayitlari guard'lari baypas ederek elle enjekte et
        tm = TranslationMemory(db_path=db, project_name="t", auto_clean=False)
        tm.conn.execute(
            "INSERT INTO translations (hash, source_text, target_text, project)"
            " VALUES ('h1', '2004a', '[[BİRİM_0]]', 't')"
        )
        tm.conn.execute(
            "INSERT INTO translations (hash, source_text, target_text, project)"
            " VALUES ('h2', 'The Anchor Escapement', 'The Anchor Escapement', 't')"
        )
        tm.conn.commit()
        deleted = tm.clean_poisoned_entries()
        assert deleted == 2
        tm.close()

    def test_number_guard_is_multiset(self, tmp_path):
        from core.memory import TranslationMemory
        tm = TranslationMemory(db_path=str(tmp_path / "t.db"), project_name="t")
        tm.add("value rises from 5 to 5 units exactly here",
               "deger tam burada 5'ten 5 birime cikar")
        # Ayni rakam KUMESI ({5}) ama farkli COKLUK (5 vs 5,5) -> similar donmemeli
        res = tm.find_similar("value rises from 5 to 9 units exactly here",
                              threshold=80)
        assert res == []
        tm.close()


# ---------------------------------------------------------------------------
# #13/#14 — core/ner_protector.py
# ---------------------------------------------------------------------------

class TestNerProtector:
    def _protector(self):
        from core.ner_protector import NERProtector
        p = NERProtector.__new__(NERProtector)
        p.protected_entities = {}
        p.persons = set()
        p.organizations = set()
        p.works = set()
        p.terms = set()
        return p

    def test_known_term_word_boundary(self):
        """'MIT' limit/permit/submitted icinde eslesmemeli (#13)."""
        p = self._protector()
        p.protected_entities = {"MIT": {}}
        p.organizations = {"mit"}
        ents = p.detect_entities("The limit was submitted to MIT for review")
        known = [(e, s, t) for e, cat, s, t in ents if cat == "ORG"]
        assert len(known) == 1
        text, s, e = known[0][0], known[0][1], known[0][2]
        assert text == "MIT"

    def test_overlapping_entities_do_not_corrupt_text(self):
        """Cakisan entity'ler metni cogaltmamali/yok etmemeli (#14)."""
        p = self._protector()
        p.protected_entities = {"New York": {}, "New York Times": {}}
        p.organizations = {"new york", "new york times"}
        original = "I read the New York Times daily."
        protected, mapping = p.protect(original)
        restored = p.restore(protected, mapping)
        assert "New York" in restored
        assert "daily" in restored
        # Cift yazim yok
        assert restored.count("New York") == 1


# ---------------------------------------------------------------------------
# #3 — translate_book.check_brace_balance nested href
# ---------------------------------------------------------------------------

class TestBraceBalanceHref:
    def test_nested_href_counts_balanced(self):
        from translate_book import check_brace_balance
        _, _, diff = check_brace_balance(
            r"Metin \href{https://x.com/a}{\textbf{baglanti}} devam."
        )
        assert diff == 0

    def test_url_percent_not_comment(self):
        from translate_book import check_brace_balance
        _, _, diff = check_brace_balance(
            r"\url{http://a.b/c%20d} kalan {x} dengeli}"
        )
        # {x} dengeli + tek fazla } -> -1... metin: '{x} dengeli}' -> 1 acik 2 kapali
        assert diff == -1


# ---------------------------------------------------------------------------
# #43 — latex_pipeline.translator._HEADING_RE: \part + opsiyonel arg
# ---------------------------------------------------------------------------

class TestHeadingRegex:
    def test_part_and_optional_arg_matched(self):
        from latex_pipeline.translator import _HEADING_RE
        m = _HEADING_RE.search(r"\part{Book One}")
        assert m and m.group(3) == "Book One"
        m = _HEADING_RE.search(r"\chapter[Short]{The Long Title}")
        assert m and m.group(2) == "[Short]" and m.group(3) == "The Long Title"
        m = _HEADING_RE.search(r"\section*{Overview}")
        assert m and m.group(3) == "Overview"


# ---------------------------------------------------------------------------
# #8 — qc_engine placeholder korlugu
# ---------------------------------------------------------------------------

class TestQcPlaceholderCoverage:
    @pytest.mark.parametrize("token", [
        "[[REF_3]]", "[[PGREF_0]]", "[[BİRİM_2]]", "[[UNIT_1]]", "[[FORMULA_7]]",
    ])
    def test_pipeline_placeholders_flagged(self, token):
        from latex_pipeline.qc_engine import QCEngine
        qc = QCEngine()
        rep = qc.analyze(f"Bu cümlede {token} artığı kaldı ve görünmemeli.\n", "")
        assert any(
            i.check_name == "placeholder_orphans" for i in rep.issues
        ), f"{token} yakalanmadı"


# ---------------------------------------------------------------------------
# #35/#79 — qc_engine untranslated_heading sozde-ek muafiyeti
# ---------------------------------------------------------------------------

class TestQcHeadingSuffixExemption:
    def test_english_heading_with_pseudo_suffix_flagged(self):
        from latex_pipeline.qc_engine import QCEngine
        qc = QCEngine()
        tr = "\\chapter{Scalar Products}\n\\section{Hidden Layers}\n"
        en = "\\chapter{Scalar Products}\n\\section{Hidden Layers}\n"
        rep = qc.analyze(tr, en)
        heads = [i for i in rep.issues if i.check_name == "untranslated_heading"]
        assert any("Scalar" in i.message for i in heads)
        assert any("Hidden" in i.message for i in heads)

    def test_ascii_turkish_heading_stays_exempt(self):
        from latex_pipeline.qc_engine import QCEngine
        qc = QCEngine()
        tr = "\\section{Temel Kavramlar}\n\\section{Dogrusal Denklemler}\n"
        en = "\\section{Basic Concepts}\n\\section{Linear Equations}\n"
        rep = qc.analyze(tr, en)
        heads = [i for i in rep.issues if i.check_name == "untranslated_heading"]
        assert not heads


# ---------------------------------------------------------------------------
# #32 — qc_engine tek-satir Ingilizce paragraf eskalasyonu
# ---------------------------------------------------------------------------

class TestQcSingleLineEscalation:
    def test_long_english_paragraph_is_critical(self):
        from latex_pipeline.qc_engine import QCEngine
        qc = QCEngine()
        para = (
            "This is a fully untranslated English paragraph that keeps "
            "going for a long time because machine translation failed "
            "silently and nobody noticed the problem in the output at all, "
            "which is exactly the failure mode we want to escalate now."
        )
        tr = f"Türkçe satır burada.\n\n{para}\n\nBaşka Türkçe satır.\n"
        rep = qc.analyze(tr, "")
        unt = [i for i in rep.issues if i.check_name == "untranslated"]
        assert any(i.severity == "critical" for i in unt), (
            [f"{i.severity}:{i.message}" for i in unt]
        )


# ---------------------------------------------------------------------------
# #12 — retranslator.is_english_line teknik-terim acligi
# ---------------------------------------------------------------------------

class TestEnglishDetectorTechnicalStarvation:
    def test_technical_sentence_detected(self):
        from latex_pipeline.retranslator import is_english_line
        assert is_english_line(
            "Orthogonal matrices preserve eigenvalues and determinants "
            "of linear transformations."
        )

    def test_turkish_ascii_sentence_not_flagged(self):
        from latex_pipeline.retranslator import is_english_line
        assert not is_english_line(
            "Enerji korunumu yasasi burada gecerlidir cunku sistem kapalidir."
        )


# ---------------------------------------------------------------------------
# #6 — sanitizer ic ice math env stash
# ---------------------------------------------------------------------------

class TestSanitizerNestedMathStash:
    def test_nested_env_stashed_whole(self):
        from latex_pipeline.sanitizer import _MATH_ENV_RE
        t = (r"\begin{align} x &= 1 \\ \begin{cases} a & b \end{cases}"
             r" \\ y &= 2 \end{align} sonra metin")
        m = _MATH_ENV_RE.search(t)
        assert m is not None
        assert m.group(0).endswith(r"\end{align}")


# ---------------------------------------------------------------------------
# #62/#2 — sanitizer and/or satir kapisi + manuel mod
# ---------------------------------------------------------------------------

class TestSanitizerEnglishLeakGating:
    def test_and_or_only_on_turkish_lines(self):
        from latex_pipeline.sanitizer import LatexSanitizer
        s = LatexSanitizer()
        body = (
            "Bu yöntem hız and ivme üzerinde çalışır.\n"
            "The Rise of Coastal Trade and the Maritime Economy\n"
        )
        out, _ = s.sanitize(body)
        lines = out.split("\n")
        assert " ve " in lines[0]           # Türkçe satırda düzeltildi
        assert " and " in lines[1]          # İngilizce satıra dokunulmadı

    def test_manual_mode_skips_english_leaks(self):
        from latex_pipeline.sanitizer import LatexSanitizer
        s = LatexSanitizer(manual_translation=True)
        body = "Bu yöntem hız and ivme üzerinde çalışır.\n"
        out, _ = s.sanitize(body)
        assert " and " in out               # Yol A: ajan çıktısı otorite


# ---------------------------------------------------------------------------
# #5 — converter bold/italik/link escape
# ---------------------------------------------------------------------------

class TestConverterInlineEscape:
    def _converter(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.converter import MarkdownToLatex
        return MarkdownToLatex(PipelineConfig(output_dir="/tmp/x"))

    def test_bold_content_escaped(self):
        c = self._converter()
        out = c._convert_inline("**50% discount & more**")
        assert r"\%" in out and r"\&" in out
        assert "\\textbf{" in out

    def test_bold_with_math_token_survives(self):
        c = self._converter()
        out = c._convert_inline("**deger $x_1$ oldu**")
        assert "$x_1$" in out               # math icerigi bozulmadi


# ---------------------------------------------------------------------------
# #4 — converter ardisik dedup (kuresel degil)
# ---------------------------------------------------------------------------

class TestConverterHeadingDedup:
    def test_recurring_section_across_chapters_kept(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.converter import MarkdownToLatex
        c = MarkdownToLatex(PipelineConfig(output_dir="/tmp/x"))
        md = (
            "# Chapter One\n\ntext a\n\n## Exercises\n\nsoru 1\n\n"
            "# Chapter Two\n\ntext b\n\n## Exercises\n\nsoru 2\n"
        )
        tex = c.convert(md)
        assert tex.count("Exercises") >= 2, tex

    def test_adjacent_duplicate_still_removed(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.converter import MarkdownToLatex
        c = MarkdownToLatex(PipelineConfig(output_dir="/tmp/x"))
        md = "# Chapter One\n\ntext\n\n## Summary\n## Summary\n\nson\n"
        tex = c.convert(md)
        assert tex.count("Summary") == 1, tex


# ---------------------------------------------------------------------------
# #73 — restore_stash cift placeholder tek restore
# ---------------------------------------------------------------------------

class TestRestoreStashDuplicate:
    def test_duplicated_placeholder_restored_once(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator
        lt = LatexTranslator(PipelineConfig(output_dir="/tmp/x"))
        stash = [r"\begin{equation}E=mc^2\end{equation}"]
        body = "Once <<LATEX_0>> sonra yine <<LATEX_0>> bitti."
        out = lt._restore_stash(body, stash)
        assert out.count("E=mc^2") == 1
        assert "<<LATEX_0>>" not in out
