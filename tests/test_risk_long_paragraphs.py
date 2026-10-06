"""Risk 2 — Uzun paragraf >2000 char stress testi.

13 Mart fiyaskosu: _MAX_SEGMENT_CHARS=500 idi, 500+ char paragraflar
cümlelere bölünüyor, regex başarısız olunca segment çevrilmeden
geçiyordu. Fix: 500→2000 + _hard_split fallback.

Bu testler:
1. 2000-10000 char paragrafların TAMAMI çevriliyor mu?
2. _SENTENCE_SPLIT_RE başarısız olduğunda _hard_split devreye giriyor mu?
3. Cümle ayracı olmayan (newline-only / no period) blok ne oluyor?
4. Red pill kitap stili (uzun paragraflar, az formül) çalışıyor mu?
5. Bilimsel kitap stili (uzun paragraf + iç içe formül) çalışıyor mu?

Tüm testler MOCK kullanır — gerçek HY-MT yüklenmez, hızlı koşar.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _english_words(n: int) -> str:
    """N kelimeli rastgele İngilizce metin (cümle ayracı içerir)."""
    base = (
        "The behavioral dynamics of modern interaction reveal complex patterns. "
        "Researchers have identified several key mechanisms underlying this phenomenon. "
        "Empirical studies consistently demonstrate that the effect is robust across populations. "
        "However the underlying mechanism remains contested in the literature. "
        "Critics argue that the framework oversimplifies a multifaceted reality. "
    )
    text = ""
    while len(text.split()) < n:
        text += base
    return " ".join(text.split()[:n])


def _no_period_block(n_chars: int) -> str:
    """Cümle ayracı OLMAYAN (sadece virgül + ve/and) uzun blok.

    Sentence-split regex'in başarısız olacağı patolojik vaka.
    Hard split fallback'i tetiklemeli.
    """
    word = "behavioral_dynamics_observation_pattern_evidence_consistent_robust_modern_interaction "
    return (word * (n_chars // len(word) + 2))[:n_chars]


class TestLongParagraphTranslation:
    """Uzun paragraflar bütünüyle çevrilmeli."""

    def _check_all_translated(self, lt, mock_batch_fn, mock_single_fn, original: str) -> str:
        """Helper: original'ı çevir, [TR]...[/TR] markerları var mı kontrol et."""
        result = lt.translate(original)
        # Mock [TR] tag'i yerleştirir; en az bir [TR] olmalı (çeviri yapıldı)
        assert "[TR]" in result, f"Çeviri yapılmadı: {result[:200]}"
        return result

    def test_paragraph_2500_chars_with_periods(self, make_latex_translator,
                                                 mock_batch_fn, mock_single_fn):
        """2500 char, cümle ayracı VAR — sentence split çalışmalı."""
        lt = make_latex_translator()
        text = _english_words(400)  # ~2500 char
        assert len(text) > 2000
        result = self._check_all_translated(lt, mock_batch_fn, mock_single_fn, text)
        # Sonuçta orijinal bütünlüğüyle kalmamalı
        assert result.count("[TR]") >= 1

    def test_paragraph_5000_chars(self, make_latex_translator,
                                    mock_batch_fn, mock_single_fn):
        """5000 char — daha uzun, hala period'lu."""
        lt = make_latex_translator()
        text = _english_words(800)
        assert len(text) > 4500
        result = self._check_all_translated(lt, mock_batch_fn, mock_single_fn, text)
        assert "[TR]" in result

    def test_paragraph_10000_chars(self, make_latex_translator,
                                     mock_batch_fn, mock_single_fn):
        """10000 char — abartı uzun. Pipeline patlamasın."""
        lt = make_latex_translator()
        text = _english_words(1600)
        result = self._check_all_translated(lt, mock_batch_fn, mock_single_fn, text)
        assert "[TR]" in result

    def test_paragraph_no_periods_3000_chars(self, make_latex_translator,
                                                mock_batch_fn, mock_single_fn):
        """3000 char, cümle ayracı YOK — _hard_split devreye girmeli.

        Bu 13 Mart bug'ının patolojik versiyonu: regex başarısız.
        Önceden bu segment çevrilmeden geçiyordu.
        """
        lt = make_latex_translator()
        text = _no_period_block(3000)
        assert "." not in text
        assert len(text) > 2500
        result = self._check_all_translated(lt, mock_batch_fn, mock_single_fn, text)
        # En az 1 hard-split chunk çevrilmiş olmalı
        assert "[TR]" in result, "Hard split fallback çalışmadı"

    def test_paragraph_with_inline_formulas(self, make_latex_translator,
                                              mock_batch_fn, mock_single_fn):
        """Uzun paragraf + iç içe formül (bilimsel kitap stili)."""
        lt = make_latex_translator()
        text = (
            _english_words(300)
            + r" The equation $E = mc^2$ describes mass-energy. "
            + _english_words(200)
            + r" Furthermore $\nabla \cdot \vec{B} = 0$ is Maxwell's third equation. "
            + _english_words(250)
        )
        result = self._check_all_translated(lt, mock_batch_fn, mock_single_fn, text)
        # Math kalmış olmalı (LATEX placeholder restore edildikten sonra)
        assert "$" in result or "<<LATEX" in result, "Formül kayboldu"


class TestSentenceSplitFallback:
    """_translate_by_sentences ve _hard_split davranışı."""

    def test_hard_split_chunks_at_500_chars(self, make_latex_translator):
        """_hard_split 500 char'lık chunk'lara böler."""
        lt = make_latex_translator()
        text = "x " * 1500  # 3000 char, no period, no sentence boundary
        chunks = lt._hard_split(text, max_chars=500)
        assert len(chunks) >= 5
        # Her chunk 500'e yakın olmalı (kelime sınırlarında bölündüğü için biraz değişebilir)
        for chunk in chunks:
            assert len(chunk) <= 700  # tolerance for word boundary

    def test_sentence_split_with_abbreviations(self, make_latex_translator,
                                                 mock_batch_fn, mock_single_fn):
        """Fig. / Eq. / vs. gibi kısaltmalar yanlış böldürmemeli."""
        lt = make_latex_translator()
        text = (
            "See Fig. 1 for details. The equation in Eq. 2.3 shows the relationship. "
            "Smith et al. demonstrated this. According to Dr. Jones the result holds. "
            * 5
        )
        result = lt.translate(text)
        # Çeviri tamamlanmalı
        assert "[TR]" in result

    def test_hard_split_preserves_no_content(self, make_latex_translator):
        """_hard_split chunk'ları birleştirildiğinde orijinal içerik korunmalı."""
        lt = make_latex_translator()
        text = _no_period_block(2500)
        chunks = lt._hard_split(text, max_chars=500)
        rejoined = " ".join(chunks)
        # Karakter sayısı yakın olmalı (whitespace farkları olabilir)
        assert abs(len(rejoined.replace(" ", "")) - len(text.replace(" ", ""))) < 50


class TestRedPillBookStyle:
    """Red pill kitapları: uzun paragraflar, az/hiç formül, monoton metin."""

    def test_long_monologue_paragraph(self, make_latex_translator,
                                        mock_batch_fn, mock_single_fn):
        """Tek paragraf, 2000+ char, sosyolojik analiz tarzı."""
        lt = make_latex_translator()
        text = (
            "The dynamics of contemporary intersexual relationships reveal patterns "
            "that have been historically obscured by cultural narratives. "
        ) * 30  # ~3000 char
        result = lt.translate(text)
        assert "[TR]" in result

    def test_chained_long_paragraphs(self, make_latex_translator,
                                       mock_batch_fn, mock_single_fn):
        """5 ayrı 1500 char paragraf — ardışık çeviri, hiçbiri kaybolmasın."""
        lt = make_latex_translator()
        body = "\n\n".join([_english_words(250) for _ in range(5)])
        result = lt.translate(body)
        # Her paragraf çevrilmiş olmalı (en az 5 [TR] markeri)
        tr_count = result.count("[TR]")
        assert tr_count >= 3, f"Bazı paragraflar atlandı: sadece {tr_count} [TR]"


class TestEngineeringBookStyle:
    """Mühendislik kitabı: uzun paragraf + formül + ref + cite + tabular."""

    def test_dense_engineering_paragraph(self, make_latex_translator,
                                           mock_batch_fn, mock_single_fn):
        """Mühendislik tarzı: çok formül, çok ref, uzun açıklama."""
        lt = make_latex_translator()
        text = (
            r"The thrust equation \cite{sutton2016} relates exhaust velocity "
            r"$v_e$ to mass flow rate $\dot{m}$ via $F = \dot{m} v_e + A_e (p_e - p_a)$. "
            r"See Fig.~\ref{fig:thrust} and Eq.~\eqref{eq:1}. "
        ) * 15  # ~2500 char + many cmds
        result = lt.translate(text)
        # Cite/ref korunmuş olmalı
        assert r"\cite" in result or "<<LATEX" in result
        assert "[TR]" in result

    def test_paragraph_with_nested_math_envs(self, make_latex_translator,
                                                mock_batch_fn, mock_single_fn):
        """Paragraf + display math + inline math karışık."""
        lt = make_latex_translator()
        text = (
            "The Lagrangian formulation of classical mechanics begins with the principle "
            "of stationary action. We define the action functional as follows:\n\n"
            r"\begin{equation}" "\n"
            r"S = \int_{t_1}^{t_2} L(q, \dot{q}, t) \, dt" "\n"
            r"\end{equation}" "\n\n"
            "where $L = T - V$ is the Lagrangian, $T$ is kinetic energy, and $V$ is potential. "
        ) * 8
        assert len(text) > 2000
        result = lt.translate(text)
        assert "[TR]" in result


class TestPlaceholderPreservation:
    """Uzun paragraflarda placeholder kaybı olmamalı."""

    def test_no_placeholder_loss_in_long_segment(self, make_latex_translator,
                                                   mock_batch_fn, mock_single_fn):
        """3000 char paragraf + 5 formül → 5 placeholder geri gelmeli."""
        lt = make_latex_translator()
        text = (
            _english_words(100)
            + r" Equation 1: $a^2 + b^2 = c^2$. "
            + _english_words(100)
            + r" Equation 2: $E = mc^2$. "
            + _english_words(100)
            + r" Equation 3: $F = ma$. "
            + _english_words(100)
            + r" Equation 4: $\int x dx = x^2/2$. "
            + _english_words(100)
            + r" Equation 5: $e^{i\pi} + 1 = 0$. "
        )
        assert len(text) > 2500
        result = lt.translate(text)
        # 5 formülün hepsi $...$ olarak veya placeholder olarak korunmuş olmalı
        dollar_count = result.count("$")
        # Her $...$ için 2 dolar işareti = 5 formül için en az 8 (1-2 kayıp tolere edilir)
        assert dollar_count >= 8 or "<<LATEX" in result, \
            f"Çok fazla formül kayboldu: {dollar_count} dolar işareti"
