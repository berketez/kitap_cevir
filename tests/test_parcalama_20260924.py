"""Dalga 2 (24 Eylül 2026) — parçalama ve segment düzeyi çeviri hataları.

20 sayfalık bir Yol B koşusunun envanteri (iç denetim notu)
kategori A/B/C(parçalama)/I3/I5/J1/J3/H1/F için regresyon kilitleri:

* P0  — yer tutuculu satır modele BÜTÜN gider; kayıpta o satır eski parçalı
        yola düşer (batched, sıralı, altyazı batch'i). G2a-1 (2026-10-05, mt
        okuma MT-1): parçalı yol KALDIRILDI — kayıpta aynı satır bir kez daha
        → sade biçim → özgün dilde bırak + işaret (tests/test_G2a1_g.py).
* P0  — ``_AUTHOR_YEAR_RE`` "his Principia (1687)"ı atıf sanmaz.
* P2  — kaynakta olmayan sondaki "…" silinir (J1 kozmetik).
* P2  — [[UNIT/BIRIM]] kalıntısı: core onarır; onarılamazsa segment orijinaline
        döner, SİLİNMEZ; birim/sembol hücresi MT'ye gitmez (H1).
* P2  — İngilizce olmayan (Latince) kaynak segment çevrilmeden geçer (F).

Model yüklenmez; MT sahte fonksiyonlarla taklit edilir.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core import lang_detect as ld
from core.translator import BookTranslator
from latex_pipeline.config import PipelineConfig
from latex_pipeline.translator import (
    LatexTranslator,
    TranslationStats,
    _AUTHOR_YEAR_RE,
)

_PH_RE = re.compile(r"<<(?:LATEX|CMD)_\d+>>")
# G2a-1: sahte çevirmen LaTeX komut adlarını da korur (gerçek model gibi);
# eskiden "\textasciitilde" → "\grkgnfpvvgvyqr" yapıyordu ve iskelet bekçisi
# bunu haklı olarak bozuk çıktı sayıyor.
_KEEP_RE = re.compile(r"<<(?:LATEX|CMD)_\d+>>|\\[A-Za-z]+\*?")


def _fake_tr(text: str) -> str:
    """Yer tutucuları koruyan, Türkçe harfli sahte çeviri (echo değil)."""
    out = []
    pos = 0
    for m in _KEEP_RE.finditer(text):
        out.append(_tr_words(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_tr_words(text[pos:]))
    return "".join(out)


def _tr_words(s: str) -> str:
    """ROT13 + ünlü→Türkçe harf: kaynağa benzemez (``_check_mixed_language``
    %85 benzerlik eşiği tetiklenmez) ve İngilizce kalıntı sayılmaz."""
    res = []
    for ch in s:
        o = ord(ch)
        if 97 <= o <= 122:
            ch = chr((o - 97 + 13) % 26 + 97)
        elif 65 <= o <= 90:
            ch = chr((o - 65 + 13) % 26 + 65)
        res.append({"a": "ı", "e": "ü", "o": "ö"}.get(ch, ch))
    return "".join(res)


def _w(word: str) -> str:
    """Bir İngilizce sözcüğün sahte çevirideki karşılığı (assert için)."""
    return _tr_words(word)


def _make_lt(batch_fn=None, single_fn=None) -> LatexTranslator:
    lt = LatexTranslator(
        pipeline_config=PipelineConfig(translate=True, segment_timeout=60),
        mt_engine="hymt-7b", source_lang="en", target_lang="tr",
    )
    bt = MagicMock()
    bt.translate_batch_protected = batch_fn or (lambda texts: [_fake_tr(t) for t in texts])
    bt.translate_text = single_fn or (lambda t, *a, **k: _fake_tr(t))
    lt._translator = bt
    return lt


# Yapı: bir kaynak gövde satırı (yer tutucu + düzyazı + yer tutucu + cümle sonu
# + yarım kuyruk); metin sentetik (KARAR 26).
LINE = ("where <<LATEX_0>> is the net push exerted on the moving cart "
        "and <<LATEX_1>> is its momentum. For a fixed load m, it is")


# ---------------------------------------------------------------------------
# P0 — batched yol: satır bütün gider
# ---------------------------------------------------------------------------

class TestBatchedWholeLine:
    def test_line_with_placeholders_sent_as_one_segment(self):
        sent = []

        def batch(texts):
            sent.extend(texts)
            return [_fake_tr(t) for t in texts]

        lt = _make_lt(batch_fn=batch)
        out = lt._translate_paragraphs(LINE)
        assert sent == [LINE], sent          # 1 segment, 5 parça değil
        assert "<<LATEX_0>>" in out and "<<LATEX_1>>" in out
        assert not out.startswith(" ")       # J3: satır başı boşluk yok
        assert "  " not in out               # parça birleştirme boşluğu yok
        assert lt.stats.placeholder_split_fallbacks == 0

    def test_no_single_word_fragments(self):
        """Eski yol 'where' / 'is' / 'and' parçaları üretiyordu."""
        sent = []

        def batch(texts):
            sent.extend(texts)
            return [_fake_tr(t) for t in texts]

        lt = _make_lt(batch_fn=batch)
        body = ("The position of <<LATEX_3>> relative to <<LATEX_4>> is\n"
                "perpendicular to <<LATEX_5>>, i.e. the trajectory lies in <<LATEX_6>>")
        lt._translate_paragraphs(body)
        assert len(sent) == 2
        assert all(len(_PH_RE.sub(" ", t).split()) > 1 for t in sent)

    def test_lost_placeholder_line_retried_whole(self):
        """G2a-1 (mt okuma MT-1): model yer tutucu yutarsa satır parçalara
        BÖLÜNMEZ; aynı satır bir kez daha (tekli yol) gönderilir."""
        calls = []
        single_sent = []

        def batch(texts):
            calls.append(list(texts))
            outs = []
            for t in texts:
                tr = _fake_tr(t)
                if "<<LATEX_1>>" in t and "<<LATEX_0>>" in t:
                    tr = tr.replace("<<LATEX_1>>", "")   # bütün satırda kayıp
                outs.append(tr)
            return outs

        def single(t, *a, **k):
            single_sent.append(t)
            return _fake_tr(t)

        lt = _make_lt(batch_fn=batch, single_fn=single)
        out = lt._translate_paragraphs(LINE)
        assert calls == [[LINE]]                     # parça geçişi yok
        assert single_sent and all(t == LINE for t in single_sent)
        assert "<<LATEX_0>>" in out and "<<LATEX_1>>" in out
        assert lt.stats.placeholder_split_fallbacks == 1
        assert lt.stats.placeholders_lost >= 1
        assert lt.stats.recovered_by_retry == 1
        assert _w("where") in out and _w("momentum") in out

    def test_echo_line_retried_whole(self):
        """Bütün satır echo dönerse de kurtarma sırası (parçalı yol yok)."""
        calls = []

        def batch(texts):
            calls.append(list(texts))
            return list(texts)                # echo

        lt = _make_lt(batch_fn=batch)
        out = lt._translate_paragraphs(LINE)
        assert len(calls) == 1                # ikinci (parça) toplu geçiş yok
        assert _w("momentum") in out          # tekli tekrar tuttu
        assert lt.stats.placeholder_split_fallbacks == 1
        assert lt.stats.recovered_by_retry == 1

    def test_plain_lines_unchanged_behaviour(self):
        sent = []

        def batch(texts):
            sent.extend(texts)
            return [_fake_tr(t) for t in texts]

        lt = _make_lt(batch_fn=batch)
        out = lt._translate_paragraphs("Plain sentence one.\n\n\\item Second item text")
        assert sent == ["Plain sentence one.", "Second item text"]
        assert "\\item " + _w("Second") in out

    def test_caption_like_line_keeps_colon(self):
        """I3: 'Figure 1.1: ...' altyazısı düz satır; ':' modele gider ve kalır."""
        sent = []

        def batch(texts):
            sent.extend(texts)
            return [_fake_tr(t) for t in texts]

        lt = _make_lt(batch_fn=batch)
        out = lt._translate_paragraphs("<<LATEX_7>>: Kepler's second law.")
        assert sent == ["<<LATEX_7>>: Kepler's second law."]
        assert out.startswith("<<LATEX_7>>:")


# ---------------------------------------------------------------------------
# P0 — sıralı yol ve altyazı batch'i
# ---------------------------------------------------------------------------

class TestSequentialAndCaptionPaths:
    def test_sequential_line_sent_whole(self):
        sent = []

        def single(t, *a, **k):
            sent.append(t)
            return _fake_tr(t)

        lt = _make_lt(single_fn=single)
        out = lt._translate_line_preserving_placeholders(LINE)
        assert sent == [LINE]
        assert "<<LATEX_0>>" in out and "<<LATEX_1>>" in out
        assert lt.stats.placeholder_split_fallbacks == 0

    def test_sequential_loss_keeps_source_no_pieces(self):
        """G2a-1: hep yutan modelde satır parça parça çevrilmez; özgün kalır,
        işaretlenir (QC kaynak dilde kalan satırı raporlar)."""
        sent = []

        def single(t, *a, **k):
            sent.append(t)
            tr = _fake_tr(t)
            if "<<LATEX_0>>" in t and "<<LATEX_1>>" in t:
                tr = tr.replace("<<LATEX_0>>", "")
            return tr

        lt = _make_lt(single_fn=single)
        out = lt._translate_line_preserving_placeholders(LINE)
        assert sent == [LINE, LINE]           # ilk deneme + tekrar; parça yok
        assert out == LINE
        assert lt.stats.placeholder_split_fallbacks == 1
        assert lt.stats.kept_source_segments == 1

    def test_caption_batch_whole_and_fallback(self):
        calls = []

        def batch(texts):
            calls.append(list(texts))
            outs = []
            for t in texts:
                tr = _fake_tr(t)
                if t.startswith("<<LATEX_9>>"):
                    tr = tr.replace("<<LATEX_9>>", "")    # bu altyazıda kayıp
                outs.append(tr)
            return outs

        lt = _make_lt(batch_fn=batch)
        caps = ["<<LATEX_7>>: Kepler's second law.",
                "<<LATEX_9>>: Sketch of the laws and <<LATEX_10>> here."]
        outs = lt._translate_lines_preserving_placeholders_batch(caps)
        assert calls[0] == caps                     # ikisi de bütün gitti
        assert outs[0].startswith("<<LATEX_7>>:")   # ':' korundu
        assert "<<LATEX_9>>" in outs[1] and "<<LATEX_10>>" in outs[1]
        assert _w("Sketch") in outs[1]              # tekli tekrar çevirdi
        assert len(calls) == 1                      # parça geçişi yok
        assert lt.stats.placeholder_split_fallbacks == 1
        assert lt.stats.recovered_by_retry == 1


# ---------------------------------------------------------------------------
# P0 — "Principia (1687)" atıf değil
# ---------------------------------------------------------------------------

class TestAuthorYear:
    @pytest.mark.parametrize("text,expected", [
        ("LeMarc (2015)", "LeMarc (2015)"),
        ("Kovarsky (2012)", "Kovarsky (2012)"),
        ("as shown by Smith (2015) and others", "Smith (2015)"),
        ("In Part II of his Survey (1902) Hallberg describes", None),
        ("see the Origin (1859) for details", None),
        ("in their Report (2020) they claim", None),
    ])
    def test_regex(self, text, expected):
        m = _AUTHOR_YEAR_RE.search(text)
        assert (m.group(0) if m else None) == expected

    def test_stash_keeps_principia_in_prose(self):
        lt = LatexTranslator.__new__(LatexTranslator)
        body, stash = lt._stash_protected(
            "In Part II of his Survey (1902) Hallberg describes the three cases:")
        assert "Survey (1902)" in body
        assert not stash


# ---------------------------------------------------------------------------
# P2 — kaynakta olmayan sondaki "…"
# ---------------------------------------------------------------------------

def _bare_lt() -> LatexTranslator:
    lt = LatexTranslator.__new__(LatexTranslator)
    lt.target_lang = "tr"
    lt.source_lang = "en"
    lt.stats = TranslationStats()
    return lt


class TestTrailingEllipsis:
    def test_removed_when_source_has_none(self):
        lt = _bare_lt()
        r = lt._post_process_translation(
            "Bu nedenle, açısal momentum…", "Therefore, the angular momentum")
        assert r == "Bu nedenle, açısal momentum"
        assert lt.stats.trailing_ellipsis_removed == 1

    def test_three_dots_variant(self):
        lt = _bare_lt()
        r = lt._post_process_translation("Bu nedenle, açısal momentum ...",
                                         "Therefore, the angular momentum")
        assert r == "Bu nedenle, açısal momentum"

    def test_kept_when_source_ends_with_ellipsis(self):
        lt = _bare_lt()
        r = lt._post_process_translation("ve böyle devam eder…", "and so on…")
        assert r.endswith("…")
        assert lt.stats.trailing_ellipsis_removed == 0


# ---------------------------------------------------------------------------
# P2 — [[UNIT/BIRIM]] kalıntısı ve birim hücresi (H1)
# ---------------------------------------------------------------------------

def _bare_bt(source_lang: str = "en") -> BookTranslator:
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = SimpleNamespace(source_lang=source_lang)
    bt.stats = {}
    return bt


class TestUnitRemnants:
    @pytest.mark.parametrize("mangled", [
        "[[BIRIM_0]]", "[[BİRİM_0]]", "[[ UNIT_0 ]]", "[[UNIT 0]]", "[[Birim_0]]",
    ])
    def test_core_restore_units_repairs_mangled(self, mangled):
        bt = _bare_bt()
        assert bt._restore_units(f"km3 −{mangled}", ["2 s"]) == "km3 −2 s"
        assert bt.stats["placeholders_repaired"] == 1

    def test_core_restore_formulas_repairs_mangled(self):
        bt = _bare_bt()
        assert bt._restore_formulas("değer [[FORMÜL_1]] olur", ["$a$", "$b$"]) == \
            "değer $b$ olur"

    def test_core_out_of_range_index_left_alone(self):
        bt = _bare_bt()
        assert bt._restore_units("x [[BIRIM_5]] y", ["2 s"]) == "x [[BIRIM_5]] y"
        assert bt.stats.get("placeholders_repaired", 0) == 0

    def test_post_process_reverts_instead_of_deleting(self):
        """Onarılamayan kalıntı: segment orijinaline döner, veri silinmez."""
        lt = _bare_lt()
        r = lt._post_process_translation("km³ −[[BIRIM_0]]", "km3 −2 s")
        assert r == "km3 −2 s"
        assert lt.stats.remnant_reverts == 1
        r2 = lt._post_process_translation("değer [[FORMULA_0]] olur", "value $x$ is")
        assert r2 == "value $x$ is"

    @pytest.mark.parametrize("cell,unit_like", [
        ("km3 −2 s", True), ("kg m/s²", True), ("N·m", True), ("Hz", True),
        ("10^{11}", True), ("<<LATEX_3>> s", True),
        ("Sun", False), ("Mass (kg)", False), ("rad", False), ("Earth–Moon", False),
    ])
    def test_unit_like_cell(self, cell, unit_like):
        assert LatexTranslator._is_unit_like_cell(cell) is unit_like

    def test_tabular_body_skips_unit_cells(self):
        lt = _bare_lt()
        seen = []

        def cell_spy(cell):
            seen.append(cell.strip())
            return cell

        lt._translate_cell = cell_spy
        lt._translate_tabular_body("Body & Mass & km3 −2 s \\\\ Sun & 1.0 & kg m/s² \\\\")
        assert "km3 −2 s" not in seen and "kg m/s²" not in seen
        assert "Body" in seen and "Sun" in seen

    def test_translate_cell_never_sends_unit_cell_to_mt(self):
        lt = _make_lt(single_fn=lambda t, *a, **k: pytest.fail("MT çağrıldı: " + t))
        assert lt._translate_cell(" km3 −2 s ") == " km3 −2 s "


# ---------------------------------------------------------------------------
# P2 — Latince / İngilizce olmayan segment kapısı (F)
# ---------------------------------------------------------------------------

# Kamu malı: Kepler, Astronomia Nova (1609) ve Harmonices Mundi (1619);
# Newton, Principia (1687), hareket yasaları.
LATIN_LINES = [
    "... sequenti capite, ubi simul etiam demonstrabitur, nullam Planetae "
    "relinqui figuram Orbitæ, praeterquam perfecte ellipticam; conspirantibus "
    "rationibus, a principiis Physicis, derivatis, cum experientia observationum "
    "et hypotheseos vicariæ hoc capite allegata.",
    "Quare ex superioribus, sicut se habet CDE area ad dimidium temporis "
    "restitutorii, quod dicatur nobis 180 gradus: sic CAG, CAH areae ad morarum "
    "in CG et CH diuturnitatem.",
    "Sed res est certissima exactissimaque, quod proportio quae est inter "
    "binorum quorumcunque Planetarum tempora periodica, sit praecise "
    "sesquialtera proportionis mediarum distantiarum, id est Orbium ipsorum",
    "Lex I - Corpus omne perseverare in statu suo quiscendi vel movendi "
    "uniformiter in directum, nisi quatenus a viribus impressis cogitur statum "
    "illum mutare.",
    "Lex II - Mutationem motus proportionalem esse vi motrici impressæ, et "
    "fieri secundum lineam rectam qua vis illa imprimitur.",
    "Lex III - Actioni contrariam semper et æ qualem esse reactionem: sive "
    "corporum duorum actiones in se mutuo semper esse æquales et in partes "
    "contrarias dirigi.",
]
ENGLISH_LINES = [
    LINE,
    "In Part II of his Survey (1902) Hallberg restates the old Axiomata sive Leges Motus:",
    "For many centuries the paths of the planets were pictured as small circles "
    "riding on larger circles. In the Astronomia Nova we find",
    "The torque about the forum is unique per se and ad hoc, i.e. the status quo.",
    "For n > 2 the n-body problem is generally believed to lack a closed-form answer.",
]


class TestForeignSegmentGate:
    @pytest.mark.parametrize("text", LATIN_LINES)
    def test_latin_detected(self, text):
        assert ld.classify_foreign_segment(text) == "la"

    @pytest.mark.parametrize("text", ENGLISH_LINES)
    def test_english_not_flagged(self, text):
        assert ld.classify_foreign_segment(text) is None

    def test_short_segment_not_classified(self):
        assert ld.classify_foreign_segment("Lex I") is None
        assert ld.classify_foreign_segment("et al.") is None

    def test_other_languages_via_existing_rules(self):
        de = ("Die Bewegung der Planeten wird durch die Anziehungskraft der Sonne "
              "bestimmt und ist nicht gleichförmig.")
        assert ld.classify_foreign_segment(de) == "de"
        ru = "Для ускоренного перемещения аппарата необходимо создать силу тяги."
        assert ld.classify_foreign_segment(ru) == "ru"

    def test_existing_profiles_untouched(self):
        """Yalnız ekleme: kitap düzeyi profiller/eşikler Latince'den etkilenmez."""
        assert "la" not in ld.PROFILES
        assert ld.LATIN_PROFILE_LANGS == ("en", "de", "fr", "es", "it", "pt", "tr")
        assert "the" in ld.PROFILES["en"] and "ve" in ld.PROFILES["tr"]
        assert not (ld.LATIN_WORDS & ld.PROFILES["en"])
        assert not (ld.LATIN_WORDS & ld.PROFILES["tr"])

    def test_batched_skips_latin_segment(self):
        sent = []

        def batch(texts):
            sent.extend(texts)
            return [_fake_tr(t) for t in texts]

        lt = _make_lt(batch_fn=batch)
        body = LATIN_LINES[3] + "\n\nThe first law requires an inertial system."
        out = lt._translate_paragraphs(body)
        assert sent == ["The first law requires an inertial system."]
        assert LATIN_LINES[3] in out
        assert lt.stats.segments_foreign_skipped == 1

    def test_safe_translate_skips_latin(self):
        lt = _make_lt(single_fn=lambda t, *a, **k: pytest.fail("MT çağrıldı"))
        assert lt._safe_translate(LATIN_LINES[4]) == LATIN_LINES[4]
        assert lt.stats.segments_foreign_skipped == 1

    def test_safe_translate_batch_skips_latin(self):
        lt = _make_lt(batch_fn=lambda texts: pytest.fail("MT çağrıldı"))
        outs = lt._safe_translate_batch([LATIN_LINES[5], LATIN_LINES[1]])
        assert outs == [LATIN_LINES[5], LATIN_LINES[1]]

    def test_core_translate_text_gate(self):
        """retranslator/autofix core'a doğrudan gelse de Latince çevrilmez."""
        bt = _bare_bt("en")
        assert bt.translate_text(LATIN_LINES[0]) == LATIN_LINES[0]
        assert bt.stats["segments_foreign_skipped"] == 1

    def test_core_gate_follows_source_language(self):
        # K5 (2026-09-28, KARARLAR 14): yerel model yolu 6 kaynak dili çevirir;
        # kapı eskiden İngilizce dışında TAMAMEN kapalıydı, artık kaynak dile
        # göre çalışır. Almanca kaynakta Latince alıntı yine ayrılır; Roman
        # dillerinde (fr/es/it) Latince kararı verilmez — Latince profil
        # kelimeleri ("est", "se", "qui") kaynağın işlev kelimeleriyle çakışır,
        # yanlış pozitif kaynağın kendi paragrafını çevirmeden bırakırdı.
        assert _bare_bt("de")._foreign_source_segment(LATIN_LINES[0]) == "la"
        assert _bare_bt("fr")._foreign_source_segment(LATIN_LINES[0]) is None


# ---------------------------------------------------------------------------
# P0 — yer tutuculu segmentin MT talimatı (core, koşullu cümle)
# ---------------------------------------------------------------------------

class TestPlaceholderInstruction:
    """Ölçüm (24 gerçek satır, HY-MT 7B, 24 Eyl): cümle yokken 6/24 yer tutucu
    yutuldu, cümleyle 2/24. Cümle yalnız yer tutuculu metne eklenir."""

    def _bt(self, book_type="science"):
        bt = BookTranslator.__new__(BookTranslator)
        bt.config = SimpleNamespace(book_type=book_type)
        return bt

    def test_default_instruction_unchanged(self):
        bt = self._bt()
        assert bt._build_translation_instruction("English", "Turkish") == \
            "Translate from English to Turkish. Preserve math notation. Output only the translation."

    def test_clause_added_only_when_requested(self):
        bt = self._bt("general")
        with_ph = bt._build_translation_instruction("English", "Turkish", placeholders=True)
        # 2026-09-24 (denetim C1a): örnek token gerçek id uzayında değil.
        assert "Keep placeholder tokens like <<LATEX_N>> exactly as they are." in with_ph
        assert not re.search(r"<<LATEX_\d+>>", with_ph)
        assert with_ph.endswith("Output only the translation.")
        # Başlık modunda cümle eklenmez
        head = bt._build_translation_instruction("English", "Turkish", is_heading=True,
                                                 placeholders=True)
        assert "placeholder" not in head

    def test_has_placeholder_tokens(self):
        assert BookTranslator._has_placeholder_tokens("a <<LATEX_3>> b")
        assert BookTranslator._has_placeholder_tokens("<<CMD_0>> x")
        assert not BookTranslator._has_placeholder_tokens("plain text <notag>")

    def test_clause_echo_is_stripped(self):
        bt = self._bt()
        out = bt._strip_prompt_leaks(
            "Keep placeholder tokens like <<LATEX_12>> exactly as they are. "
            "Burada <<LATEX_0>> kuvvettir.", original="where <<LATEX_0>> is the force.")
        assert out == "Burada <<LATEX_0>> kuvvettir."
