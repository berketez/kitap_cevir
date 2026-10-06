r"""
Fix avi 2026-07-28 — latex_pipeline/translator.py performans bulgulari.

F16: _restore_stash O(S*N) kuadratikti (idx basina body.find + tam-govde
     yeniden insa). Artik tek soldan-saga gecis; sozlesmeler korunur:
     ilk gecis restore + kopya silme (#73), bosluk garantisi, ic ice
     placeholder ters-sira semantigi, eksik id'ler icin slow-path.

F18: _translate_stashed_formatting / _translate_href_display_texts /
     caption cevirisi oge basina ayri MT cagrisi yapiyordu. Artik MT
     gerektiren metinler toplanip _safe_translate_batch ->
     _batch_mt_call -> translate_batch_protected ile TOPLU cevriliyor;
     dogrulama/rollback, placeholder koruma ve timeout zinciri ayni.
"""

import re
import sys
import os
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latex_pipeline.translator import LatexTranslator, TranslationStats
from latex_pipeline.config import PipelineConfig


# ---------------------------------------------------------------------------
# Yardimcilar
# ---------------------------------------------------------------------------

def _bare_instance() -> LatexTranslator:
    """MT'siz iskelet instance (yalniz _restore_stash gibi saf metotlar)."""
    return LatexTranslator.__new__(LatexTranslator)


def _make_translator(batch_map=None, single_map=None) -> LatexTranslator:
    """Sahte BookTranslator'li gercek LatexTranslator.

    batch_map/single_map: girdi metni -> cikti metni sozlugu.
    Sozlukte olmayan girdiler oldugu gibi doner (echo).
    """
    config = PipelineConfig(translate=True, segment_timeout=60)
    lt = LatexTranslator(
        pipeline_config=config,
        mt_engine="hymt-7b",
        source_lang="en",
        target_lang="tr",
    )

    mock_bt = MagicMock()
    b_map = batch_map or {}
    s_map = single_map or {}

    def _batch(texts):
        return [b_map.get(t, t) for t in texts]

    def _single(text, is_heading=False):
        return s_map.get(text, text)

    mock_bt.translate_batch_protected = MagicMock(side_effect=_batch)
    mock_bt.translate_text = MagicMock(side_effect=_single)

    lt._translator = mock_bt
    lt._checkpoint_dir = None
    lt._checkpoint_counter = 0
    lt._progress_callback = None
    lt.stats = TranslationStats()
    lt._BATCH_SIZE = 64  # testte alt-batch bolunmesin
    return lt


# ---------------------------------------------------------------------------
# F16 — _restore_stash tek gecis
# ---------------------------------------------------------------------------

class TestF16RestoreContracts:
    """Eski davranis sozlesmeleri yeni tek-gecisli restore'da da gecerli."""

    def _restore(self, body, stash):
        return _bare_instance()._restore_stash(body, stash)

    def test_basic_restore_and_spacing(self):
        stash = [r"$x^2$", r"\ref{fig:1}"]
        body = "once<<LATEX_0>>sonra ve <<LATEX_1>>."
        result = self._restore(body, stash)
        assert "once $x^2$ sonra" in result
        assert result.endswith(r"\ref{fig:1}.")
        assert "<<LATEX_" not in result

    def test_duplicate_restored_once_copies_deleted(self):
        """#73: MT'nin cogalttigi placeholder icerigi BIR kez basilir."""
        stash = [r"\begin{equation}E=mc^2\end{equation}"]
        body = "once <<LATEX_0>> orta <<LATEX_0>> son <<LATEX_0>>"
        result = self._restore(body, stash)
        assert result.count("E=mc^2") == 1
        assert "<<LATEX_" not in result

    def test_many_duplicates_all_removed(self):
        """8'den fazla kopya da ciktida kalmaz (dup sil + orphan supur)."""
        stash = [r"$y$"]
        body = " ".join(["<<LATEX_0>>"] * 12)
        result = self._restore(body, stash)
        assert result.count("$y$") == 1
        assert "<<LATEX_" not in result

    def test_nested_placeholder_expanded(self):
        """Dis stash icerigi daha KUCUK id'li placeholder tasiyabilir."""
        stash = [r"$a$", r"\footnote{bkz <<LATEX_0>>}"]
        body = "metin <<LATEX_1>> devam"
        result = self._restore(body, stash)
        assert r"\footnote{bkz $a$}" in result
        assert "<<LATEX_" not in result

    def test_nested_two_levels(self):
        stash = [r"$a$", r"\emph{ic <<LATEX_0>>}", r"\footnote{dis <<LATEX_1>>}"]
        body = "x <<LATEX_2>> y"
        result = self._restore(body, stash)
        assert r"\footnote{dis \emph{ic $a$}}" in result

    def test_nested_higher_id_not_expanded(self):
        """Ic placeholder id'si distan BUYUKSE acilmaz (eski ters-sira
        semantigi) — orphan supurucu temizler."""
        stash = [r"X <<LATEX_2>> Y", "ARA", "GIZLI"]
        body = "bas <<LATEX_0>> son"
        result = self._restore(body, stash)
        assert "GIZLI" not in result
        assert "<<LATEX_" not in result
        assert "X" in result and "Y" in result

    def test_leading_zero_id_swept_not_restored(self):
        """<<LATEX_007>> gecerli token degildir; stash[7] basilmaz."""
        stash = ["s%d" % i for i in range(10)]
        body = "a <<LATEX_007>> b <<LATEX_1>> c"
        result = self._restore(body, stash)
        assert "s7" not in result
        assert "s1" in result
        assert "<<LATEX_" not in result

    def test_out_of_range_id_swept(self):
        stash = [r"$x$"]
        body = "a <<LATEX_0>> b <<LATEX_999>> c"
        result = self._restore(body, stash)
        assert "$x$" in result
        assert "LATEX" not in result

    def test_mangled_space_form_restored(self):
        stash = [r"$\alpha$"]
        body = "deger << LATEX_0 >> olur"
        result = self._restore(body, stash)
        assert r"$\alpha$" in result
        assert "LATEX" not in result

    def test_mangled_slow_path_expands_nested_content(self):
        """Parite fix'i: mangled restore'un soktugu icerikteki kucuk id'li
        placeholder'lar da acilir (eski akista sonraki pass aciyordu)."""
        stash = [r"$a$", "ARA", r"\footnote{bkz <<LATEX_0>>}"]
        body = "metin << LATEX_2 >> devam"
        result = self._restore(body, stash)
        assert r"\footnote{bkz $a$}" in result
        assert "<<LATEX_" not in result

    def test_unicode_mangled_still_restored(self):
        stash = ["FILLER"] * 19 + [r"$\alpha$"]
        body = "text \u00ab\u00abLATEX\u2081\u2089\u00bb\u00bb more"
        result = self._restore(body, stash)
        assert r"$\alpha$" in result
        assert "\u00ab" not in result

    def test_missing_placeholder_no_leak(self):
        """MT'nin tamamen yuttugu id sessizce atlanir (icerik kaybi zaten
        vardi; onemli olan placeholder sizmamasi)."""
        stash = [r"$x$", r"$y$"]
        body = "yalniz <<LATEX_1>> var"
        result = self._restore(body, stash)
        assert "$y$" in result
        assert "<<LATEX_" not in result

    def test_empty_stash_noop(self):
        result = self._restore("duz metin.", [])
        assert result == "duz metin."


class TestF16RestorePerformance:
    """Kuadratik gerileme kilidi: 8k stash lineer surede restore edilmeli.

    Olcum (bu makine): eski kod 8k'da ~1.0-5.8 s, yeni kod ~0.03 s.
    Esik CI payiyla genis tutuldu; kuadratik donus esigi yine yakalar.
    """

    def test_restore_8k_stash_linear_time(self):
        n = 8000
        stash = [rf"$x_{{{i}}}$" for i in range(n)]
        body = "".join(
            f"cumle {i} icinde <<LATEX_{i}>> var. " for i in range(n)
        )
        inst = _bare_instance()
        t0 = time.perf_counter()
        result = inst._restore_stash(body, stash)
        dt = time.perf_counter() - t0
        assert "<<LATEX_" not in result
        assert "$x_{7999}$" in result
        assert dt < 2.0, f"restore 8k stash cok yavas: {dt:.2f}s (kuadratik donus?)"

    def test_restore_missing_heavy_linear_time(self):
        """Binlerce YUTULMUS placeholder'da slow-path taramasi patlamamali."""
        n = 8000
        stash = [rf"$x_{{{i}}}$" for i in range(n)]
        parts = []
        for i in range(n):
            if i % 4 == 0:
                parts.append(f"cumle {i} kayip. ")
            else:
                parts.append(f"cumle {i} <<LATEX_{i}>> var. ")
        inst = _bare_instance()
        t0 = time.perf_counter()
        result = inst._restore_stash("".join(parts), stash)
        dt = time.perf_counter() - t0
        assert "<<LATEX_" not in result
        assert dt < 2.0, f"eksik-yogun restore cok yavas: {dt:.2f}s"


# ---------------------------------------------------------------------------
# F18 — _safe_translate_batch
# ---------------------------------------------------------------------------

class TestF18SafeTranslateBatch:

    def test_multiple_items_single_batch_call(self):
        lt = _make_translator(batch_map={
            "first sentence here": "birinci cumle burada",
            "second sentence here": "ikinci cumle burada",
            "third sentence here": "ucuncu cumle burada",
        })
        outs = lt._safe_translate_batch([
            "first sentence here",
            "second sentence here",
            "third sentence here",
        ])
        assert outs == [
            "birinci cumle burada",
            "ikinci cumle burada",
            "ucuncu cumle burada",
        ]
        assert lt._translator.translate_batch_protected.call_count == 1
        assert lt._translator.translate_text.call_count == 0
        assert lt.stats.segments_translated == 3

    def test_short_and_placeholder_only_items_skipped(self):
        lt = _make_translator(batch_map={
            "gercek metin cumlesi": "gercek metnin karsiligi",
            "ikinci gercek cumle": "ikinci metnin karsiligi",
        })
        outs = lt._safe_translate_batch([
            "ab",                       # <3 karakter -> atla
            "<<LATEX_0>>",              # yalniz placeholder -> atla
            "gercek metin cumlesi",
            "ikinci gercek cumle",
        ])
        assert outs[0] == "ab"
        assert outs[1] == "<<LATEX_0>>"
        assert outs[2] == "gercek metnin karsiligi"
        assert outs[3] == "ikinci metnin karsiligi"
        assert lt.stats.segments_skipped == 2
        # Atlanan ogeler MT'ye gitmedi
        sent = lt._translator.translate_batch_protected.call_args[0][0]
        assert "ab" not in sent
        assert "<<LATEX_0>>" not in sent

    def test_single_item_uses_single_path(self):
        """Tek batchlenebilir oge: mevcut tekli yol korunur."""
        lt = _make_translator(single_map={
            "only one sentence": "tek bir cumle",
        })
        outs = lt._safe_translate_batch(["only one sentence"])
        assert outs == ["tek bir cumle"]
        assert lt._translator.translate_batch_protected.call_count == 0
        assert lt._translator.translate_text.call_count >= 1

    def test_long_item_falls_back_to_single_path(self):
        """max_segment_chars ustu oge cumle-bolmeli tekli yola duser."""
        lt = _make_translator()
        max_chars = lt.pipeline_config.max_segment_chars
        long_text = ("Uzun bir cumle parcasi. " * (max_chars // 20 + 2))
        short_a = "short piece one here"
        short_b = "short piece two here"
        outs = lt._safe_translate_batch([short_a, long_text, short_b])
        assert len(outs) == 3
        # Uzun oge tekli yoldan gecti (translate_text cagrildi)
        assert lt._translator.translate_text.call_count >= 1
        # Kisa ogeler yine tek batch'te
        assert lt._translator.translate_batch_protected.call_count == 1

    def test_batch_error_falls_back_sequential(self):
        """translate_batch_protected patlarsa sirali fallback calisir."""
        lt = _make_translator(single_map={
            "alpha sentence text": "alfa cumle metni",
            "beta sentence text": "beta cumle metni",
        })
        lt._translator.translate_batch_protected = MagicMock(
            side_effect=RuntimeError("model down"),
        )
        # _translate_batch_hymt fallback'ine de dusmesin
        del lt._translator._translate_batch_hymt
        outs = lt._safe_translate_batch([
            "alpha sentence text", "beta sentence text",
        ])
        assert outs == ["alfa cumle metni", "beta cumle metni"]
        assert lt._translator.translate_text.call_count == 2

    def test_placeholder_loss_reverts_to_original(self):
        """MT placeholder yutarsa segment orijinaliyle geri alinir."""
        src = "metin <<LATEX_3>> devam ediyor"
        lt = _make_translator(batch_map={
            src: "placeholder kayboldu",
            "ikinci normal cumle": "ikinci ceviri cumlesi",
        })
        outs = lt._safe_translate_batch([src, "ikinci normal cumle"])
        assert outs[0] == src  # geri alindi
        assert outs[1] == "ikinci ceviri cumlesi"
        assert lt.stats.placeholders_lost >= 1

    def test_inline_commands_stashed_and_restored(self):
        r"""\newpage gibi yapisal komutlar MT'ye gitmez, sonra geri gelir."""
        lt = _make_translator(batch_map={
            "metin devami burada <<CMD_0>> ile": "ceviri <<CMD_0>> sonucu",
            "baska bir cumle daha": "bir ceviri daha",
        })
        outs = lt._safe_translate_batch([
            "metin devami burada \\newpage ile",
            "baska bir cumle daha",
        ])
        assert "\\newpage" in outs[0]
        assert "<<CMD_" not in outs[0]


# ---------------------------------------------------------------------------
# F18 — stash formatting fazi toplu MT
# ---------------------------------------------------------------------------

class TestF18StashedFormattingBatch:

    def test_fmt_and_footnote_translated_in_one_batch(self):
        lt = _make_translator(batch_map={
            "the italic phrase here": "italik ifade burada",
            "the bold statement here": "kalin ifade burada",
            "a footnote sentence here": "bir dipnot cumlesi burada",
        })
        lt._stash_list = [
            r"\textit{the italic phrase here}",
            r"\textbf{the bold statement here}",
            r"\footnote{a footnote sentence here}",
        ]
        lt._translate_stashed_formatting()
        assert lt._stash_list[0] == r"\textit{italik ifade burada}"
        assert lt._stash_list[1] == r"\textbf{kalin ifade burada}"
        assert lt._stash_list[2] == r"\footnote{bir dipnot cumlesi burada}"
        # Uc oge TEK toplu MT cagrisinda cevrildi
        assert lt._translator.translate_batch_protected.call_count == 1
        assert lt._translator.translate_text.call_count == 0

    def test_validation_rollback_on_broken_translation(self):
        """Yapiyi bozan ceviri (dengesiz brace) oge bazinda geri alinir."""
        lt = _make_translator(batch_map={
            "the good phrase here": "iyi ifade burada",
            "the broken phrase here": "bozuk } ifade { fazla }",
        })
        original_broken = r"\textit{the broken phrase here}"
        lt._stash_list = [
            r"\textit{the good phrase here}",
            original_broken,
        ]
        lt._translate_stashed_formatting()
        assert lt._stash_list[0] == r"\textit{iyi ifade burada}"
        assert lt._stash_list[1] == original_broken  # rollback

    def test_footnote_optional_marker_preserved(self):
        lt = _make_translator(batch_map={
            "numbered footnote text": "numarali dipnot metni",
            "plain footnote text goes": "duz dipnot metni gider",
        })
        lt._stash_list = [
            r"\footnotetext[3]{numbered footnote text}",
            r"\footnote{plain footnote text goes}",
        ]
        lt._translate_stashed_formatting()
        assert lt._stash_list[0] == r"\footnotetext[3]{numarali dipnot metni}"
        assert lt._stash_list[1] == r"\footnote{duz dipnot metni gider}"

    def test_crossref_protected_through_batch(self):
        """Ic cross-ref'ler REF placeholder'iyla korunur ve geri gelir."""
        lt = _make_translator(batch_map={
            "see <<REF_0>> for details now": "ayrintilar icin <<REF_0>> bak",
            "another prose sentence here": "baska bir cumle burada",
        })
        lt._stash_list = [
            r"\textit{see Figure 4.10 for details now}",
            r"\textbf{another prose sentence here}",
        ]
        lt._translate_stashed_formatting()
        assert "4.10" in lt._stash_list[0]
        assert "\u015eekil" in lt._stash_list[0]  # Sekil
        assert "<<REF_" not in lt._stash_list[0]
        assert lt._stash_list[0].startswith(r"\textit{")

    def test_leading_crossref_with_trailing_prose(self):
        """Basta cross-ref + devam metni: ref cevirisi + MT'li kuyruk."""
        lt = _make_translator(batch_map={
            "shows the loss curve": "kayip egrisini gosterir",
            "another prose sentence here": "baska bir cumle burada",
        })
        lt._stash_list = [
            r"\textbf{Figure 2.5 shows the loss curve}",
            r"\textit{another prose sentence here}",
        ]
        lt._translate_stashed_formatting()
        assert "2.5" in lt._stash_list[0]
        assert "kayip egrisini gosterir" in lt._stash_list[0]

    def test_short_ref_fast_path_no_mt(self):
        """\\textbf{Fig.5.1} gibi kisa ref formu MT'siz cevrilir."""
        lt = _make_translator()
        lt._stash_list = [r"\textbf{Fig. 5.1}"]
        lt._translate_stashed_formatting()
        assert "5.1" in lt._stash_list[0]
        assert lt._translator.translate_batch_protected.call_count == 0
        assert lt._translator.translate_text.call_count == 0

    def test_empty_stash_list_noop(self):
        lt = _make_translator()
        lt._stash_list = []
        lt._translate_stashed_formatting()
        assert lt._translator.translate_batch_protected.call_count == 0


# ---------------------------------------------------------------------------
# F18 — href display fazi toplu MT
# ---------------------------------------------------------------------------

class TestF18HrefDisplayBatch:

    def test_displays_translated_in_one_batch(self):
        lt = _make_translator(batch_map={
            "the first link text": "ilk baglanti metni",
            "the second link text": "ikinci baglanti metni",
        })
        lt._stash_list = [
            r"\href{https://a.com}{the first link text}",
            r"\href{https://b.com}{the second link text}",
        ]
        lt._translate_href_display_texts()
        assert lt._stash_list[0] == r"\href{https://a.com}{ilk baglanti metni}"
        assert lt._stash_list[1] == r"\href{https://b.com}{ikinci baglanti metni}"
        assert lt._translator.translate_batch_protected.call_count == 1

    def test_pure_crossref_display_no_mt(self):
        """Salt cross-ref display MT'ye gitmez (Rocket regresyonu)."""
        lt = _make_translator()
        lt._stash_list = [r"\href{https://x.com}{Figure 4.10.}"]
        lt._translate_href_display_texts()
        assert "4.10" in lt._stash_list[0]
        assert "\u015eekil" in lt._stash_list[0]
        assert lt._translator.translate_batch_protected.call_count == 0
        assert lt._translator.translate_text.call_count == 0

    def test_mixed_display_ref_protected(self):
        lt = _make_translator(batch_map={
            "details in <<REF_0>> here": "ayrintilar <<REF_0>> icinde",
            "plain long display text": "duz uzun gorunum metni",
        })
        lt._stash_list = [
            r"\href{https://x.com}{details in Figure 3.2 here}",
            r"\href{https://y.com}{plain long display text}",
        ]
        lt._translate_href_display_texts()
        assert "3.2" in lt._stash_list[0]
        assert "<<REF_" not in lt._stash_list[0]

    def test_url_never_sent_to_mt(self):
        lt = _make_translator(batch_map={
            "the first link text": "ilk baglanti metni",
            "the second link text": "ikinci baglanti metni",
        })
        lt._stash_list = [
            r"\href{https://a.com/path_x}{the first link text}",
            r"\href{https://b.com/path_y}{the second link text}",
        ]
        lt._translate_href_display_texts()
        sent = lt._translator.translate_batch_protected.call_args[0][0]
        for s in sent:
            assert "https://" not in s


# ---------------------------------------------------------------------------
# F18 — caption toplu, baslik tekli
# ---------------------------------------------------------------------------

class TestF18CaptionBatch:

    def test_captions_batched_with_placeholder_preserved(self):
        # 2026-09-24 (Yol B parçalama P0): altyazı yer tutucusuyla BÜTÜN tek
        # segment gider; eski sözleşme parçaları ayrı ayrı çeviriyordu.
        lt = _make_translator(batch_map={
            "The loss curve for <<LATEX_0>> over many epochs":
                "Kayip egrisi soyle <<LATEX_0>> cok sayida donemde",
            "A network architecture diagram": "Bir ag mimarisi diyagrami",
        })
        lt._stash_list = []
        body = (
            "\\caption{The loss curve for <<LATEX_0>> over many epochs}\n"
            "metin arasi\n"
            "\\caption{A network architecture diagram}\n"
        )
        result = lt._translate_structured(body)
        assert "Kayip egrisi soyle" in result
        assert "<<LATEX_0>>" in result
        assert "cok sayida donemde" in result
        assert "Bir ag mimarisi diyagrami" in result
        # Iki caption'in tum duz metinleri TEK batch'te gitti
        assert lt._translator.translate_batch_protected.call_count == 1

    def test_whole_caption_echo_falls_back_to_pieces(self):
        """Model bütün segmenti yankılarsa o altyazı PARÇALARA bölünmez
        (G2a-1, mt okuma MT-1; ad tarihî): aynı altyazı tekli yoldan bir kez
        daha denenir, tutmazsa özgün kalır ve işaretlenir."""
        lt = _make_translator(batch_map={
            "The loss curve for": "Kayip egrisi soyle",
            "over many epochs": "cok sayida donemde",
            "A network architecture diagram": "Bir ag mimarisi diyagrami",
        })
        lt._stash_list = []
        body = (
            "\\caption{The loss curve for <<LATEX_0>> over many epochs}\n"
            "metin arasi\n"
            "\\caption{A network architecture diagram}\n"
        )
        result = lt._translate_structured(body)
        assert "\\caption{The loss curve for <<LATEX_0>> over many epochs}" in result
        assert "Kayip egrisi soyle" not in result     # parça çevirisi yok
        assert "Bir ag mimarisi diyagrami" in result
        # Tek toplu geçiş; yankılanan altyazı tekli yoldan yalnız BÜTÜN gitti
        assert lt._translator.translate_batch_protected.call_count == 1
        assert {c.args[0] for c in lt._translator.translate_text.call_args_list} == {
            "The loss curve for <<LATEX_0>> over many epochs"}
        assert lt.stats.placeholder_split_fallbacks == 1
        assert lt.stats.kept_source_segments == 1

    def test_short_caption_untouched(self):
        lt = _make_translator()
        lt._stash_list = []
        body = "\\caption{ok}\n"
        result = lt._translate_structured(body)
        assert "\\caption{ok}" in result
        assert lt._translator.translate_batch_protected.call_count == 0

    def test_headings_not_routed_to_batch(self):
        """Baslik tek-otorite yolu (is_heading modu + dil kapisi) batch'e
        TASINMAZ — translate_batch_protected baslik metni gormez."""
        lt = _make_translator()
        lt._stash_list = []
        body = "\\section{Advanced Network Training}\n"
        lt._translate_structured(body)
        for call in lt._translator.translate_batch_protected.call_args_list:
            for s in call[0][0]:
                assert "Advanced Network Training" not in s


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
