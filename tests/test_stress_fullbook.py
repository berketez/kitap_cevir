"""
Stress testleri: Gercek kitap kosullarini simule eden kapsamli testler.

MOTIVASYON:
    13 Mart 2026'da 483 sayfalik bir akademik kitap cevirisinde %70 Ingilizce
    kaldi. Mevcut 49 test kisa snippet'ler uzerinde basarili oluyordu cunku
    kolay kisimlari test ediyordu. Bu dosya GERCEK kitap kosullarini simule
    eder: uzun paragraflar, 50+ formul, ic ice environment'lar, chapter
    gecisleri, batch sinir durumlari, ve hata cascading senaryolari.

KATEGORI:
    A. Gercekci body.tex simulasyonu (5 test)
    B. Stash butunlugu stress test (4 test)
    C. Error cascading / dayaniklilik (4 test)
    D. TOC / chapter transition (3 test)
    E. Batch processing edge cases (3 test)

Toplam: 19+ test
"""

import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Proje kokunu path'e ekle
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Gercekci sentetik LaTeX icerikleri
# ---------------------------------------------------------------------------

REALISTIC_BODY = r"""
\chapter{INTRODUCTION TO THERMODYNAMICS}

\tableofcontents

\mainmatter

\section{Basic Concepts}

The study of thermodynamics is fundamental to understanding energy transfer in physical systems.
The first law of thermodynamics tells us that energy cannot be created or destroyed, only transformed
from one form to another. This principle, also known as the law of conservation of energy, has
profound implications for engineering design and analysis.

Consider a closed system with internal energy $U$, heat transfer $Q$, and work $W$:

\begin{equation}
\label{eq:first_law}
\Delta U = Q - W
\end{equation}

where $\Delta U$ represents the change in internal energy. According to \cite{cengel2019},
this relationship holds for all thermodynamic processes. See Figure~\ref{fig:energy_balance}
for a schematic representation.

\begin{tabular}{lcc}
\hline
Property & Symbol & Unit \\
\hline
Temperature & $T$ & K \\
Pressure & $P$ & Pa \\
Volume & $V$ & m$^3$ \\
Entropy & $S$ & J/K \\
\hline
\end{tabular}

\subsection{THE ZEROTH LAW}

The zeroth law establishes the concept of thermal equilibrium. If system A is in thermal
equilibrium with system C, and system B is also in thermal equilibrium with system C, then
systems A and B are in thermal equilibrium with each other.

\begin{lstlisting}[language=Python]
def calculate_entropy(T, Q):
    return Q / T
\end{lstlisting}

This has implications for temperature measurement as described by \cref{sec:measurement}
and \autoref{tab:instruments}.

""" + (
    "The thermodynamic analysis of complex systems requires careful consideration "
    "of multiple interacting variables. " * 25
) + r"""

\section{ADVANCED MATHEMATICAL FORMULATION}

The Navier-Stokes equations in their incompressible form are given by:

\begin{align}
\label{eq:ns_momentum}
\rho \frac{\partial \mathbf{u}}{\partial t} + \rho (\mathbf{u} \cdot \nabla) \mathbf{u} &= -\nabla p + \mu \nabla^2 \mathbf{u} + \rho \mathbf{g} \\
\label{eq:ns_continuity}
\nabla \cdot \mathbf{u} &= 0
\end{align}

These equations, combined with appropriate boundary conditions (see \eqref{eq:first_law}),
form the foundation of computational fluid dynamics (CFD). The Reynolds number
$Re = \frac{\rho u L}{\mu}$ determines the flow regime.
"""


# ---------------------------------------------------------------------------
# Yardimci fonksiyonlar
# ---------------------------------------------------------------------------

def _count_placeholders(text: str) -> int:
    """Body'deki <<LATEX_N>> placeholder sayisini dondurur."""
    return len(re.findall(r"<<LATEX_\d+>>", text))


def _placeholder_ids(text: str) -> set:
    """Body'deki placeholder ID'lerini set olarak dondurur."""
    return set(int(m.group(1)) for m in re.finditer(r"<<LATEX_(\d+)>>", text))


def _has_no_orphan_placeholders(text: str) -> bool:
    """Restore sonrasi hicbir <<LATEX_N>> kalmamis mi kontrol eder."""
    return not re.search(r"<<LATEX_\d+>>", text)


# ===========================================================================
# A. GERCEKCI BODY.TEX SIMULASYONU
# ===========================================================================

class TestRealisticBodyStashRestore:
    """Gercek bir akademik kitap sayfasindaki TUM korumali LaTeX iceriginin
    stash-restore dongusu sonrasi eksiksiz geri geldigi dogrulanir.

    BUG BAGLAMII (13 Mart): Tabular, equation, lstlisting gibi environment'lar
    stash'e atilip geri donmuyordu -> eksik icerik, kirik formul.
    """

    def test_realistic_body_stash_restore_complete(self, make_latex_translator):
        """Gercekci body'deki tum korumali icerik stash+restore sonrasi
        eksiksiz geri gelmeli; hicbir <<LATEX_N>> placeholder kalmamali.
        """
        lt = make_latex_translator()
        body, stash = lt._stash_protected(REALISTIC_BODY)

        # Stash bos olmamali - en az equation, align, tabular, lstlisting,
        # inline math ($U$, $Q$, $W$, $T$, $P$, $V$, $S$, $Re...$),
        # cite, ref, label, cref, autoref, eqref
        assert len(stash) >= 10, (
            f"Stash'te sadece {len(stash)} eleman var, gercekci body'de "
            f"en az 10 korumali parca bekleniyor (math, tabular, code, ref...)"
        )

        # Body'deki her placeholder icin stash'te karsilik olmali
        ph_ids = _placeholder_ids(body)
        for pid in ph_ids:
            assert pid < len(stash), (
                f"Placeholder <<LATEX_{pid}>> var ama stash boyutu {len(stash)}"
            )

        # Restore
        restored = lt._restore_stash(body, stash)

        # Hicbir placeholder kalmamali
        orphans = re.findall(r"<<LATEX_\d+>>", restored)
        assert len(orphans) == 0, (
            f"Restore sonrasi {len(orphans)} yetim placeholder kaldi: "
            f"{orphans[:5]}"
        )

        # Kritik icerikler geri gelmeli
        assert r"\begin{equation}" in restored, (
            "equation environment restore sonrasi kayip"
        )
        assert r"\begin{align}" in restored, (
            "align environment restore sonrasi kayip"
        )
        assert r"\begin{tabular}" in restored, (
            "tabular environment restore sonrasi kayip"
        )
        assert r"\begin{lstlisting}" in restored, (
            "lstlisting environment restore sonrasi kayip"
        )
        assert r"\Delta U = Q - W" in restored, (
            "Formul icerigi (Delta U = Q - W) restore sonrasi kayip"
        )

    def test_realistic_body_refs_preserved(self, make_latex_translator):
        r"""Tum cross-reference komutlari (\cite, \ref, \label, \cref,
        \autoref, \eqref) stash sonrasi korunmali.
        """
        lt = make_latex_translator()
        body, stash = lt._stash_protected(REALISTIC_BODY)

        # Stash icerigi: tum ref tiplerine bakalim
        all_stashed = "\n".join(stash)

        expected_refs = [
            (r"\cite{cengel2019}", "cite"),
            (r"\ref{fig:energy_balance}", "ref"),
            (r"\label{eq:first_law}", "label"),
            (r"\cref{sec:measurement}", "cref"),
            (r"\autoref{tab:instruments}", "autoref"),
            (r"\eqref{eq:first_law}", "eqref"),
        ]

        for ref_cmd, ref_type in expected_refs:
            # Ya stash'te ya da body'de (stash'e atilmamis olabilir)
            in_stash = ref_cmd in all_stashed
            in_body = ref_cmd in body
            assert in_stash or in_body, (
                f"{ref_type} komutu '{ref_cmd}' ne stash'te ne body'de — kayip!"
            )

        # Restore sonrasi hepsi orijinal body'de olmali
        restored = lt._restore_stash(body, stash)
        for ref_cmd, ref_type in expected_refs:
            assert ref_cmd in restored, (
                f"{ref_type} komutu '{ref_cmd}' restore sonrasi kayip"
            )

    def test_realistic_body_allcaps_headings_detected(self):
        """ALL-CAPS basliklar _smart_title_case ile title case'e donusebilmeli.

        REALISTIC_BODY icindeki 3 ALL-CAPS baslik:
        - INTRODUCTION TO THERMODYNAMICS
        - THE ZEROTH LAW
        - ADVANCED MATHEMATICAL FORMULATION
        """
        from latex_pipeline.translator import _smart_title_case

        all_caps_headings = [
            "INTRODUCTION TO THERMODYNAMICS",
            "THE ZEROTH LAW",
            "ADVANCED MATHEMATICAL FORMULATION",
        ]

        for heading in all_caps_headings:
            result = _smart_title_case(heading)
            # Sonuc ALL-CAPS olmamali
            assert result != heading, (
                f"ALL-CAPS baslik degismedi: '{heading}'"
            )
            # Her kelimenin ilk harfi buyuk olmali (title case)
            words = result.split()
            for word in words:
                assert word[0].isupper(), (
                    f"'{heading}' -> '{result}' donusumunde "
                    f"'{word}' kelimesinin ilk harfi kucuk"
                )
            # Tamami buyuk harf olmamali (kisaltma degilse)
            from latex_pipeline.translator import _KNOWN_ACRONYMS
            for word in words:
                if word.upper() not in _KNOWN_ACRONYMS:
                    assert not word.isupper(), (
                        f"'{word}' hala ALL-CAPS ama kisaltma degil"
                    )

    def test_realistic_body_long_paragraph_split(self, make_latex_translator):
        """25x tekrarlanan cumle (2500+ char) iceren paragraf _translate_paragraphs
        ile cevrilmeli, kaybolmamali.
        """
        lt = make_latex_translator()

        # Uzun paragraf: REALISTIC_BODY'deki tekrarlanan kisim
        long_sentence = (
            "The thermodynamic analysis of complex systems requires careful "
            "consideration of multiple interacting variables. "
        )
        long_para = long_sentence * 25
        assert len(long_para) > 2500, (
            f"Test paragrafi {len(long_para)} karakter, en az 2500 bekleniyor"
        )

        result = lt._translate_paragraphs(long_para)

        # Sonuc bos olmamali
        assert result.strip(), "Uzun paragraf bos dondu - tamamen atlanmis"

        # Sonuc orijinalden farkli olmali (mock translator [TR]...[/TR] ekler)
        assert result != long_para, (
            "Uzun paragraf hic cevrilmeden geri dondu"
        )

        # [TR] marker olmali (mock translator bunu ekler)
        assert "[TR]" in result, (
            "Mock translator ciktisindan [TR] marker bulunamadi - "
            "paragraf MT'ye gitmemis"
        )

    def test_realistic_body_code_block_untouched(self, make_latex_translator):
        r"""lstlisting icindeki Python kodu stash sonrasi TAMAMEN korunmali.
        'def calculate_entropy' satirinda hicbir degisiklik olmamali.
        """
        lt = make_latex_translator()
        body, stash = lt._stash_protected(REALISTIC_BODY)

        # lstlisting icerigi stash'te olmali
        code_found = False
        for s in stash:
            if "lstlisting" in s and "calculate_entropy" in s:
                code_found = True
                # Kod bozulmamis mi?
                assert "def calculate_entropy(T, Q):" in s, (
                    "Fonksiyon imzasi bozulmus: " + s[:200]
                )
                assert "return Q / T" in s, (
                    "return ifadesi bozulmus: " + s[:200]
                )
                break

        assert code_found, (
            "lstlisting icindeki 'calculate_entropy' kodu stash'te bulunamadi"
        )

        # Restore sonrasi da bozulmamis olmali
        restored = lt._restore_stash(body, stash)
        assert "def calculate_entropy(T, Q):" in restored, (
            "Restore sonrasi kod blogu bozulmus"
        )
        assert "return Q / T" in restored, (
            "Restore sonrasi return ifadesi bozulmus"
        )


# ===========================================================================
# B. STASH BUTUNLUGU STRESS TESTLERI
# ===========================================================================

class TestStashIntegrityStress:
    """Cok sayida formul ve ic ice yapilar icin stash mekanizmasinin
    butunlugunu dogrular.

    BUG BAGLAMI: 13 Mart'ta stash index kaymasi ve kayip placeholder'lar
    nedeniyle formuller kayboluyordu. Bu testler ayni senaryoyu 50 formul
    olceginde stres-test eder.
    """

    def test_50_formulas_all_recovered(self, make_latex_translator):
        """50 farkli equation environment olustur, stash -> restore
        dongusu sonrasi 50'si de eksiksiz geri gelmeli.
        """
        lt = make_latex_translator()

        # 50 farkli formul iceren body olustur
        parts = []
        formulas = []
        for i in range(50):
            formula = f"x_{{{i}}} = \\alpha_{{{i}}} + \\beta_{{{i}}} \\cdot z_{{{i}}}"
            formulas.append(formula)
            parts.append(
                f"This is paragraph {i} introducing the formula.\n\n"
                f"\\begin{{equation}}\n"
                f"\\label{{eq:formula_{i}}}\n"
                f"{formula}\n"
                f"\\end{{equation}}\n"
            )

        body_input = "\n".join(parts)

        # Stash
        body, stash = lt._stash_protected(body_input)

        # En az 50 equation + 50 label stash'lenmis olmali
        equation_count = sum(
            1 for s in stash if r"\begin{equation}" in s
        )
        assert equation_count == 50, (
            f"50 equation environment bekleniyordu, stash'te {equation_count} var"
        )

        # Restore
        restored = lt._restore_stash(body, stash)

        # Hicbir placeholder kalmamali
        assert _has_no_orphan_placeholders(restored), (
            "50-formul restore sonrasi yetim placeholder var"
        )

        # Her formul icerigi geri gelmeli
        for i, formula in enumerate(formulas):
            assert formula in restored, (
                f"Formul {i} restore sonrasi kayip: {formula[:60]}"
            )

    def test_nested_environments(self, make_latex_translator):
        r"""Ic ice environment'lar: equation icinde cases.
        Dis environment stash'lenirken ic yapi bozulmamali.
        """
        lt = make_latex_translator()

        nested_latex = (
            r"\begin{equation}" "\n"
            r"\label{eq:piecewise}" "\n"
            r"f(x) = \begin{cases}" "\n"
            r"  x^2 & \text{if } x \geq 0 \\" "\n"
            r"  -x^2 & \text{if } x < 0" "\n"
            r"\end{cases}" "\n"
            r"\end{equation}"
        )

        body_with_text = f"Consider the piecewise function:\n\n{nested_latex}\n\nThis is defined for all real numbers."

        body, stash = lt._stash_protected(body_with_text)

        # Nested structure stash'te korunmali
        nested_found = False
        for s in stash:
            if r"\begin{equation}" in s and r"\begin{cases}" in s:
                nested_found = True
                # Ic yapi tamamen korunmus mu?
                assert r"\end{cases}" in s, "cases environment kapanisi kayip"
                assert r"\end{equation}" in s, "equation kapanisi kayip"
                assert r"x^2" in s, "Formul icerigi (x^2) kayip"
                assert r"\text{if }" in s, "text komutu kayip"
                break

        assert nested_found, (
            "Ic ice equation>cases yapisi stash'te bulunamadi"
        )

        # Restore sonrasi tamamen geri gelmeli
        restored = lt._restore_stash(body, stash)
        assert r"\begin{cases}" in restored
        assert r"\end{cases}" in restored
        assert r"x^2 & \text{if } x \geq 0" in restored, (
            "Ic yapi restore sonrasi bozulmus"
        )

    def test_adjacent_formulas_no_merge(self, make_latex_translator):
        """Art arda gelen formuller ayri stash entry'leri olmali,
        birbiriyle karistirilmamali.
        """
        lt = make_latex_translator()

        adjacent = (
            r"\begin{equation}" "\n"
            r"E = mc^2" "\n"
            r"\end{equation}" "\n"
            r"\begin{equation}" "\n"
            r"F = ma" "\n"
            r"\end{equation}"
        )

        body, stash = lt._stash_protected(adjacent)

        # Iki ayri equation stash entry olmali
        eq_entries = [s for s in stash if r"\begin{equation}" in s]
        assert len(eq_entries) == 2, (
            f"2 ayri equation entry bekleniyor, {len(eq_entries)} bulundu - "
            f"formuller birlesmis olabilir"
        )

        # Her entry kendi formulunu icermeli
        e_mc2_found = any("E = mc^2" in s for s in eq_entries)
        f_ma_found = any("F = ma" in s for s in eq_entries)
        assert e_mc2_found, "E=mc^2 formulu stash'te bulunamadi"
        assert f_ma_found, "F=ma formulu stash'te bulunamadi"

        # Birinin icinde digeri OLMAMALI
        for entry in eq_entries:
            has_both = ("E = mc^2" in entry) and ("F = ma" in entry)
            assert not has_both, (
                "Iki formul ayni stash entry'de - merge olmus!"
            )

    def test_inline_math_mixed_with_text(self, make_latex_translator):
        """Inline math ($...$) iceren satirda: math korunmali,
        arasindaki metin cevrilmeli.
        """
        lt = make_latex_translator()

        text = "The variable $x$ increases as $y = f(x)$ where $f$ is continuous."

        body, stash = lt._stash_protected(text)

        # En az 3 inline math stash'lenmeli ($x$, $y=f(x)$, $f$)
        inline_count = sum(1 for s in stash if s.startswith("$") and s.endswith("$"))
        assert inline_count >= 3, (
            f"En az 3 inline math bekleniyor, {inline_count} bulundu. "
            f"Stash icerigi: {stash}"
        )

        # Body'de placeholder'lar arasinda metin olmali
        # Ornegin "The variable <<LATEX_0>> increases as <<LATEX_1>> where <<LATEX_2>> is continuous."
        text_between = re.sub(r"<<LATEX_\d+>>", "", body).strip()
        assert len(text_between) > 20, (
            "Placeholder'lar arasinda yeterli metin kalmamis: " + repr(body)
        )

        # Restore sonrasi orijinal metin geri gelmeli
        restored = lt._restore_stash(body, stash)
        assert "$x$" in restored, "Inline math $x$ kayip"
        assert "$y = f(x)$" in restored, "Inline math $y=f(x)$ kayip"
        assert "$f$" in restored, "Inline math $f$ kayip"
        assert "variable" in restored, "Metin ('variable') kayip"
        assert "continuous" in restored, "Metin ('continuous') kayip"


# ===========================================================================
# C. ERROR CASCADING / DAYANIKLILIK TESTLERI
# ===========================================================================

class TestErrorCascadingResilience:
    """Hata durumlarinda pipeline'in crash olmadan devam ettigini dogrular.

    BUG BAGLAMI (13 Mart): MT hatasi olunca Ingilizce metin oldugun gibi
    ciktiya akiyordu, pipeline crash olabiliyordu. 4 Nisan: graceful
    degradation fix'leri eklendi.
    """

    def test_10_percent_mt_failure_graceful(self, make_latex_translator):
        """Her 10. segment'te MT exception firlatildiginda:
        - Pipeline crash olmamali
        - Basarili segmentler cevrilmis olmali
        - Basarisiz segmentler icin pipeline devam etmeli
        """
        call_counter = [0]

        def _failing_batch(texts, max_length=512):
            results = []
            for t in texts:
                call_counter[0] += 1
                if call_counter[0] % 10 == 0:
                    raise RuntimeError(f"Simulated GPU OOM at call {call_counter[0]}")
                # Basarili ceviri
                parts = re.split(r'(<<(?:LATEX|CMD|TERM)_\d+>>)', t)
                translated = []
                for p in parts:
                    if re.fullmatch(r'<<(?:LATEX|CMD|TERM)_\d+>>', p):
                        translated.append(p)
                    else:
                        translated.append(f"[TR]{p}[/TR]")
                results.append("".join(translated))
            return results

        def _failing_single(text):
            call_counter[0] += 1
            if call_counter[0] % 10 == 0:
                raise RuntimeError(f"Simulated GPU OOM at call {call_counter[0]}")
            parts = re.split(r'(<<(?:LATEX|CMD|TERM)_\d+>>)', text)
            translated = []
            for p in parts:
                if re.fullmatch(r'<<(?:LATEX|CMD|TERM)_\d+>>', p):
                    translated.append(p)
                else:
                    translated.append(f"[TR]{p}[/TR]")
            return "".join(translated)

        lt = make_latex_translator(
            batch_fn=_failing_batch,
            single_fn=_failing_single,
        )

        # 20 paragraflik body
        paragraphs = []
        for i in range(20):
            paragraphs.append(
                f"This is paragraph number {i} about a complex topic "
                f"that needs careful translation."
            )
        body = "\n\n".join(paragraphs)

        # Pipeline CRASH OLMAMALI
        try:
            result = lt._translate_paragraphs(body)
        except Exception as exc:
            pytest.fail(
                f"Pipeline %10 MT hatasi ile crash oldu: {exc}"
            )

        # Sonuc bos olmamali
        assert result.strip(), "20 paragraflik body tamamen bos dondu"

        # En az bazi segmentler cevrilmis olmali
        tr_count = result.count("[TR]")
        assert tr_count > 0, (
            "Hicbir segment cevrilmemis - tum basarili cagrilar da kaybolmus"
        )

    def test_empty_segments_handled(self, make_latex_translator):
        """Bos paragraflar ve sadece whitespace iceren paragraflar
        pipeline'i crash ettirmemeli, bosluklar korunmali.
        """
        lt = make_latex_translator()

        body_with_empties = (
            "First real paragraph about physics.\n"
            "\n"
            "\n"
            "   \n"
            "\n"
            "Second real paragraph about chemistry.\n"
            "\n"
            "   \t  \n"
            "\n"
            "Third paragraph with actual content for translation."
        )

        try:
            result = lt._translate_paragraphs(body_with_empties)
        except Exception as exc:
            pytest.fail(
                f"Bos segmentli body pipeline'i crash ettirdi: {exc}"
            )

        # Sonuc bos olmamali
        assert result.strip(), "Bos segmentli body tamamen bos dondu"

        # En az 3 paragraf cevrilmis olmali
        tr_count = result.count("[TR]")
        assert tr_count >= 3, (
            f"En az 3 paragrafin cevrilmesi bekleniyor, {tr_count} bulundu"
        )

    def test_unicode_turkish_chars_preserved(self, make_latex_translator):
        """Turkce karakterler ceviri sonrasi bozulmamali.
        ASCII dusurme (o -> o, s -> s) OLMAMALI.
        """
        # Mock translator Turkce karakter iceren metin dondursun
        turkish_text = (
            "Bu paragrafta ozel Turkce karakterler var: "
            "\u00e7 \u011f \u0131 \u0130 \u00f6 \u015f \u00fc "
            "\u00c7 \u011e \u00d6 \u015e \u00dc"
        )

        def _turkish_batch(texts, max_length=512):
            return [turkish_text for _ in texts]

        def _turkish_single(text):
            return turkish_text

        lt = make_latex_translator(
            batch_fn=_turkish_batch,
            single_fn=_turkish_single,
        )

        body = "This paragraph should be translated to Turkish with special characters."
        result = lt._translate_paragraphs(body)

        # Turkce karakterler korunmali
        turkish_chars = [
            ("\u00e7", "c-cedilla"),
            ("\u011f", "g-breve"),
            ("\u0131", "dotless-i"),
            ("\u00f6", "o-umlaut"),
            ("\u015f", "s-cedilla"),
            ("\u00fc", "u-umlaut"),
        ]
        for char, name in turkish_chars:
            assert char in result, (
                f"Turkce karakter '{char}' ({name}) ceviri sonrasi kayip - "
                f"ASCII dusurme olmus olabilir"
            )

    def test_very_long_document_500_paragraphs(self, make_latex_translator):
        """500 paragraflik dokuman: giren paragraf sayisi = cikan paragraf sayisi.
        Ilk ve son paragraf dogru sirada.
        """
        lt = make_latex_translator()

        # 500 paragraf, her biri 100-300 karakter arasi
        paragraphs = []
        for i in range(500):
            base = f"Paragraph {i}: "
            # Degisken uzunluk: 100-300 karakter arasi
            repeat_count = 2 + (i % 5)  # 2-6 arasi
            filler = (
                "This text discusses important concepts in the field "
                "of engineering and scientific analysis. "
            )
            para = base + filler * repeat_count
            # 100-300 arasi oldugunu dogrula
            if len(para) < 100:
                para += filler
            if len(para) > 300:
                para = para[:300]
            paragraphs.append(para)

        body = "\n\n".join(paragraphs)

        result = lt._translate_paragraphs(body)

        # Sonuc bos olmamali
        assert result.strip(), "500 paragraflik dokuman bos dondu"

        # Ilk paragrafin izi olmali (mock translator [TR] ile sarar)
        assert "Paragraph 0:" in result or "[TR]Paragraph 0:" in result, (
            "Ilk paragraf (Paragraph 0) ciktida bulunamadi"
        )

        # Son paragrafin izi olmali
        assert "Paragraph 499:" in result or "[TR]Paragraph 499:" in result, (
            "Son paragraf (Paragraph 499) ciktida bulunamadi"
        )

        # Sira kontrolu: Paragraph 0 Paragraph 499'dan once gelmeli
        pos_first = result.find("Paragraph 0:")
        pos_last = result.find("Paragraph 499:")
        assert pos_first < pos_last, (
            f"Paragraf sirasi bozulmus: Paragraph 0 pos={pos_first}, "
            f"Paragraph 499 pos={pos_last}"
        )

        # Paragraf kaybetmemis olmali: rastgele 10 paragraf kontrol et
        import random
        random.seed(42)
        sample_indices = random.sample(range(500), 10)
        for idx in sample_indices:
            assert f"Paragraph {idx}:" in result, (
                f"Paragraph {idx} ciktida bulunamadi - paragraf kaybolmus"
            )


# ===========================================================================
# D. TOC / CHAPTER TRANSITION TESTLERI
# ===========================================================================

class TestTocChapterTransition:
    r"""Table of contents ve chapter gecislerinin ceviri sonrasi yapisal
    butunlugunu dogrular.

    BUG BAGLAMI: \tableofcontents + \mainmatter pattern'i bozulunca
    tum kitap yapisini etkiler. Chapter gecislerinde stash index kaymasi
    yasanabilir.
    """

    def test_toc_tabular_content_translated(self, make_latex_translator):
        """TOC genellikle tabular formatinda gelir. Tabular tamamen
        stash'lenir ama yapi korunmali.
        """
        lt = make_latex_translator()

        toc_tabular = (
            r"\begin{tabular}{lll}" "\n"
            r"\hline" "\n"
            r"Chapter 1 & Introduction to Heat Transfer & 5 \\" "\n"
            r"Chapter 2 & Conduction & 25 \\" "\n"
            r"Chapter 3 & Convection & 55 \\" "\n"
            r"Chapter 4 & Radiation & 85 \\" "\n"
            r"\hline" "\n"
            r"\end{tabular}"
        )

        body, stash = lt._stash_protected(toc_tabular)

        # Tabular stash'e atilmis olmali
        assert len(stash) >= 1, "TOC tabular stash'e atilmamis"

        # Stash'teki tabular yapisal olarak saglikli mi?
        tab_entry = next((s for s in stash if r"\begin{tabular}" in s), None)
        assert tab_entry is not None, "tabular entry stash'te yok"

        assert tab_entry.count("&") >= 8, (
            f"Tabular'da en az 8 '&' ayirici bekleniyor, "
            f"{tab_entry.count('&')} bulundu"
        )
        assert tab_entry.count(r"\\") >= 4, (
            "Tabular satir sonlari (\\\\) kayip"
        )

        # Restore sonrasi yapisal butunluk
        restored = lt._restore_stash(body, stash)
        assert r"\begin{tabular}" in restored
        assert r"\end{tabular}" in restored
        assert "Chapter 1" in restored
        assert "Chapter 4" in restored

    def test_chapter_transition_structure(self, make_latex_translator):
        r"""'\chapter{TITLE}\n\tableofcontents\n\mainmatter' pattern'i
        korunmali, sira degismemeli.
        """
        lt = make_latex_translator()

        transition = (
            r"\chapter{HEAT TRANSFER FUNDAMENTALS}" "\n\n"
            r"\tableofcontents" "\n\n"
            r"\mainmatter" "\n\n"
            r"\section{Overview}" "\n\n"
            "Heat transfer is the exchange of thermal energy between physical systems."
        )

        # translate() uzerinden full pipeline cagir
        result = lt.translate(transition)

        # Yapisal komutlar korunmus olmali
        assert r"\tableofcontents" in result, (
            "\\tableofcontents chapter gecisinde kaybolmus"
        )
        assert r"\mainmatter" in result, (
            "\\mainmatter chapter gecisinde kaybolmus"
        )

        # Sira: chapter -> tableofcontents -> mainmatter -> section
        pos_chapter = result.find(r"\chapter")
        pos_toc = result.find(r"\tableofcontents")
        pos_main = result.find(r"\mainmatter")
        pos_section = result.find(r"\section")

        assert pos_chapter < pos_toc < pos_main < pos_section, (
            f"Chapter gecis sirasi bozulmus: "
            f"chapter={pos_chapter}, toc={pos_toc}, "
            f"mainmatter={pos_main}, section={pos_section}"
        )

    def test_multiple_chapters_all_translated(self, make_latex_translator):
        """5 chapter, her birinde section + subsection. Tum basliklar
        cevrilmeli (mock translator [TR] marker'i ekler).
        """
        lt = make_latex_translator()

        chapters = []
        for i in range(1, 6):
            ch = (
                f"\\chapter{{Chapter {i} Title About Engineering}}\n\n"
                f"\\section{{Section {i}.1 Introduction}}\n\n"
                f"This is the introductory paragraph for chapter {i}. "
                f"It discusses fundamental concepts.\n\n"
                f"\\subsection{{Subsection {i}.1.1 Details}}\n\n"
                f"Detailed discussion of the topic for chapter {i} continues here."
            )
            chapters.append(ch)

        body = "\n\n".join(chapters)

        result = lt.translate(body)

        # Her chapter cevrilmis olmali
        for i in range(1, 6):
            # Chapter basliginin bir sekilde cevrilmis olmasi lazim
            # (mock translator [TR] ekler veya _smart_title_case donusturur)
            assert f"chapter" in result.lower() or f"Chapter {i}" in result or "[TR]" in result, (
                f"Chapter {i} basligi ciktida bulunamadi"
            )

        # Paragraf metinleri cevrilmis olmali
        tr_count = result.count("[TR]")
        assert tr_count >= 5, (
            f"En az 5 cevrilmis segment bekleniyor (5 chapter), "
            f"{tr_count} bulundu"
        )

        # Section/subsection yapisi korunmali
        section_count = len(re.findall(r"\\section\{", result))
        subsection_count = len(re.findall(r"\\subsection\{", result))
        assert section_count == 5, (
            f"5 \\section bekleniyor, {section_count} bulundu"
        )
        assert subsection_count == 5, (
            f"5 \\subsection bekleniyor, {subsection_count} bulundu"
        )


# ===========================================================================
# E. BATCH PROCESSING EDGE CASES
# ===========================================================================

class TestBatchProcessingEdgeCases:
    """Batch ceviri mekanizmasinin sinir durumlarini test eder.

    Pipeline _BATCH_SIZE=8 kullanir. Bu testler batch sinirlari,
    artik segmentler, tek segmentlik batch gibi durumlari hedefler.
    """

    def test_batch_boundary_no_segment_loss(self, make_latex_translator):
        """batch_size=8 ile 23 segment (2 tam batch + 7 artik):
        23'u de cevrilmeli, hicbiri kaybolmamali.
        """
        lt = make_latex_translator()

        # 23 paragraf: her biri benzersiz identifier iceriyor
        paragraphs = []
        for i in range(23):
            paragraphs.append(
                f"Unique paragraph MARKER_{i:03d} with sufficient "
                f"content length for translation to proceed correctly."
            )
        body = "\n\n".join(paragraphs)

        result = lt._translate_paragraphs(body)

        # 23 segment'in hepsi ciktida olmali
        for i in range(23):
            marker = f"MARKER_{i:03d}"
            assert marker in result, (
                f"Segment {i} ({marker}) ciktida bulunamadi - "
                f"batch sinirinda kaybolmus olabilir"
            )

    def test_single_segment_batch(self, make_latex_translator):
        """Tek segmentlik batch crash olmamali, normal cevrilmeli."""
        lt = make_latex_translator()

        body = "This is the only paragraph in the entire document, and it must be translated properly."

        try:
            result = lt._translate_paragraphs(body)
        except Exception as exc:
            pytest.fail(
                f"Tek segmentlik batch crash oldu: {exc}"
            )

        assert result.strip(), "Tek segment bos dondu"
        assert "[TR]" in result, (
            "Tek segment cevrilmemis - mock translator [TR] marker'i yok"
        )

    def test_batch_with_mixed_lengths(self, make_latex_translator):
        """1 karakter ile 1500 karakter arasi karisik uzunlukta segmentler.
        Hepsi cevrilmeli (cok kisa olanlar skip edilebilir ama crash olmamali).
        """
        lt = make_latex_translator()

        # Karisik uzunluklar
        segments = []

        # 1 karakter
        segments.append("A")

        # 5 karakter
        segments.append("Short")

        # 50 karakter
        segments.append("A medium length sentence about thermodynamic systems.")

        # 200 karakter
        segments.append(
            "A longer paragraph discussing the principles of heat conduction "
            "through various materials including metals, ceramics, and polymers. "
            "Temperature gradients drive the flow of thermal energy."
        )

        # 500 karakter
        segments.append(
            "The study of fluid mechanics involves understanding the behavior of "
            "liquids and gases at rest and in motion. The governing equations, known "
            "as the Navier-Stokes equations, describe the relationship between "
            "velocity, pressure, density, and viscous forces in a fluid. These "
            "equations form the foundation of computational fluid dynamics, which "
            "enables engineers to simulate complex flow patterns around aircraft "
            "wings, turbine blades, and other aerodynamic surfaces." * 1
        )

        # 1000 karakter
        segments.append(
            "Energy transfer in thermal systems occurs through three fundamental "
            "mechanisms: conduction, convection, and radiation. " * 8
        )

        # 1500 karakter
        segments.append(
            "The principles of thermodynamics govern all energy transformations "
            "in the universe, from microscopic molecular interactions to "
            "macroscopic engineering systems. " * 12
        )

        body = "\n\n".join(segments)

        try:
            result = lt._translate_paragraphs(body)
        except Exception as exc:
            pytest.fail(
                f"Karisik uzunluklu segmentler crash oldu: {exc}"
            )

        # Sonuc bos olmamali
        assert result.strip(), "Karisik uzunluklu body bos dondu"

        # En az orta ve uzun segmentler cevrilmis olmali
        tr_count = result.count("[TR]")
        assert tr_count >= 3, (
            f"En az 3 segment cevrilmesi bekleniyor (orta+uzun), "
            f"{tr_count} bulundu. Cok kisa segmentler skip edilebilir "
            f"ama digerleri cevrilmeli."
        )


# ===========================================================================
# BONUS: Full Pipeline Entegrasyon Testi
# ===========================================================================

class TestFullPipelineIntegration:
    """Tam translate() cagrisini REALISTIC_BODY ile yapar.
    Stash -> structured -> paragraphs -> restore -> post-process
    tum asamalarin birlesmis sonucunu kontrol eder.
    """

    def test_full_pipeline_realistic_body(self, make_latex_translator):
        """REALISTIC_BODY'yi tam pipeline'dan gecir.
        Kritik yapisal elementler korunmus, metin cevrilmis olmali.
        """
        lt = make_latex_translator()

        result = lt.translate(REALISTIC_BODY)

        # 1. Sonuc bos olmamali
        assert result.strip(), (
            "Tam pipeline sonucu bos - ciddi hata"
        )

        # 2. Yapisal LaTeX komutlari korunmali
        structural_cmds = [
            r"\chapter{",
            r"\section{",
            r"\subsection{",
            r"\tableofcontents",
            r"\mainmatter",
        ]
        for cmd in structural_cmds:
            assert cmd in result, (
                f"Yapisal komut kayip: {cmd}"
            )

        # 3. Math environment'lar korunmali
        assert r"\begin{equation}" in result, "equation environment kayip"
        assert r"\end{equation}" in result, "equation kapanisi kayip"
        assert r"\begin{align}" in result, "align environment kayip"
        assert r"\end{align}" in result, "align kapanisi kayip"

        # 4. Formul icerikleri bozulmamis olmali
        assert r"\Delta U = Q - W" in result, "Birinci yasa formulu kayip"
        assert r"\nabla \cdot \mathbf{u}" in result, (
            "Sureklilik denklemi kayip"
        )

        # 5. Tabular yapisi korunmali
        assert r"\begin{tabular}" in result, "tabular environment kayip"
        assert r"\end{tabular}" in result, "tabular kapanisi kayip"

        # 6. Code block korunmali
        assert "def calculate_entropy" in result, (
            "lstlisting icindeki Python kodu kayip"
        )

        # 7. Referanslar korunmali
        assert r"\cite{cengel2019}" in result, "cite referansi kayip"

        # 8. Hicbir yetim placeholder kalmamali
        orphan_placeholders = re.findall(r"<<(?:LATEX|CMD|TERM)_\d+>>", result)
        assert len(orphan_placeholders) == 0, (
            f"Pipeline ciktisinda {len(orphan_placeholders)} yetim "
            f"placeholder kaldi: {orphan_placeholders[:5]}"
        )

        # 9. Ceviri yapilmis olmali (metin kisimlarinda [TR] marker)
        assert "[TR]" in result, (
            "Hicbir metin cevrilmemis - mock translator izi ([TR]) yok"
        )


# ===========================================================================
# Dogrudan calistirma
# ===========================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
