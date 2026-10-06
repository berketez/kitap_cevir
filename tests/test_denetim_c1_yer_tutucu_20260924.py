"""Denetim C1 (24 Eylül 2026) — yer tutucu bekçisi (Yol B, yerel model çevirisi).

24 Eylül denetiminin (iç denetim notu) kalemleri için regresyon kilitleri:

* C1a — Mercek 2 #1: kaynakta olmayan (halüsine) id artık kabul edilmez;
        segment orijinaline döner, bütün gönderilen satır parçalı yola düşer.
        Talimat örneği gerçek id uzayında değil ("<<LATEX_N>>"). Rapordaki
        "Einstein: enerjidir." sondası uçtan uca (çeviri + restore).
* C1b — Mercek 3 #8: kopya id (çoklu küme eşitliği) → düşüş. Yer değiştirme
        BİLİNEN SINIR olarak belgelenir (sıra kontrolü yok).
* C1c — Mercek 2 #3: koruma cümlesinin yankısı (rapordaki 6 varyant + ekli
        biçimler) temizlenir; Türkçe-harf guard'ı bu silmeyi geri koymaz;
        meşru Türkçe cümle silinmez; kalıp ALL_PROMPT_LEAK_PATTERNS'ta (MT-only).
* C1d — Mercek 2 #4: düşüş sözleşmesi — uzun bütün satır (cümle yolu),
        kalıntı geri alması (remnant revert) ve sıralı yolda kalan süre bütçesi.
        G2a-1 (2026-10-05, mt okuma MT-1): "düşüş" artık parçalı yol DEĞİL,
        kurtarma sırasıdır (aynı satır bir kez daha → sade biçim → özgün dilde
        bırak + işaret); sözleşmeler buna göre güncellendi.
* C1e — Mercek 3 #6: çalışma dizininden bağımsız sentetik gövde fikstürü
        (tests/fixtures/yolb_govde_sentetik.tex; kitap metni DEĞİL, aynı
        yapıda elle yazılmış satırlar: satır içi $..$, \\cite, \\ref,
        \\textbf etiket, yer tutuculu uzun satır, \\item, altyazı, Latince).

Model yüklenmez; MT sahte fonksiyonlarla taklit edilir.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import core.translator as core_translator
from core.translator import BookTranslator, _PLACEHOLDER_CLAUSE
from latex_pipeline.config import PipelineConfig
from latex_pipeline.patterns import (
    ALL_PROMPT_LEAK_PATTERNS,
    PLACEHOLDER_CLAUSE_LEAK_RE,
    PROMPT_LEAK_PATTERNS,
    PROMPT_LEAK_PATTERNS_MT_ONLY,
)
from latex_pipeline.sanitizer import LatexSanitizer, SanitizeReport
from latex_pipeline.translator import LatexTranslator, TranslationStats

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "yolb_govde_sentetik.tex"

_PH_RE = re.compile(r"<<(?:LATEX|CMD)_\d+>>")
_LATEX_ID_RE = re.compile(r"<<LATEX_(\d+)>>")


# ---------------------------------------------------------------------------
# Sahte çevirmen (test_parcalama_20260924 ile aynı ilke)
# ---------------------------------------------------------------------------

def _tr_words(s: str) -> str:
    """ROT13 + ünlü→Türkçe harf: kaynağa benzemez, İngilizce kalıntı sayılmaz."""
    res = []
    for ch in s:
        o = ord(ch)
        if 97 <= o <= 122:
            ch = chr((o - 97 + 13) % 26 + 97)
        elif 65 <= o <= 90:
            ch = chr((o - 65 + 13) % 26 + 65)
        res.append({"a": "ı", "e": "ü", "o": "ö"}.get(ch, ch))
    return "".join(res)


def _fake_tr(text: str) -> str:
    """Yer tutucuları koruyan sahte çeviri."""
    out = []
    pos = 0
    for m in _PH_RE.finditer(text):
        out.append(_tr_words(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_tr_words(text[pos:]))
    return "".join(out)


def _w(word: str) -> str:
    return _tr_words(word)


def _make_lt(batch_fn=None, single_fn=None, **cfg) -> LatexTranslator:
    cfg.setdefault("segment_timeout", 60)
    lt = LatexTranslator(
        pipeline_config=PipelineConfig(translate=True, **cfg),
        mt_engine="hymt-7b", source_lang="en", target_lang="tr",
    )
    bt = MagicMock()
    bt.translate_batch_protected = batch_fn or (lambda texts: [_fake_tr(t) for t in texts])
    bt.translate_text = single_fn or (lambda t, *a, **k: _fake_tr(t))
    lt._translator = bt
    return lt


def _bare_lt() -> LatexTranslator:
    lt = LatexTranslator.__new__(LatexTranslator)
    lt.target_lang = "tr"
    lt.source_lang = "en"
    lt.stats = TranslationStats()
    return lt


# Sentetik satır (kitap metni değil): iki yer tutuculu, "where" ile başlayan
# gerçek gövde satırlarının yapısında.
LINE = ("where <<LATEX_0>> is the thrust acting on the vehicle and <<LATEX_1>> "
        "is its mass flow rate. For a constant exhaust speed, it is")


# ---------------------------------------------------------------------------
# C1a — halüsine / fazladan id
# ---------------------------------------------------------------------------

class TestC1aExtraPlaceholder:
    ORIG = "The force is <<LATEX_0>>."
    TR = "Kuvvet <<LATEX_0>> ve <<LATEX_12>> ile verilir."

    def test_guard_rejects_extra_id(self):
        lt = _bare_lt()
        assert lt._missing_placeholders(self.TR, self.ORIG) == {"<<LATEX_12>>"}
        assert lt._revert_on_placeholder_loss(self.TR, self.ORIG) == self.ORIG
        assert lt.stats.placeholders_extra == 1
        assert lt.stats.placeholders_lost == 0

    def test_guard_accepts_equal_multiset(self):
        lt = _bare_lt()
        good = "Kuvvet <<LATEX_0>> ile verilir."
        assert lt._missing_placeholders(good, self.ORIG) == set()
        assert lt._revert_on_placeholder_loss(good, self.ORIG) == good
        assert lt.stats.placeholders_extra == 0 and lt.stats.placeholders_lost == 0

    def test_extra_id_in_plain_segment_rejected(self):
        """Kaynakta hiç yer tutucu yokken çıktıda id belirmesi de fazlalıktır."""
        lt = _bare_lt()
        orig = "Plain sentence without any formula."
        assert lt._revert_on_placeholder_loss("Formülsüz <<LATEX_5>> cümle.", orig) == orig
        assert lt.stats.placeholders_extra == 1

    @pytest.mark.parametrize("odd", ["<<LATEX_N>>", "<< LATEX_0 >>", "<<LATEX_n>>", "<<CMD_N>>"])
    def test_malformed_token_counts_as_extra(self, odd):
        lt = _bare_lt()
        tr = f"Kuvvet <<LATEX_0>> {odd} ile verilir."
        assert lt._revert_on_placeholder_loss(tr, self.ORIG) == self.ORIG
        assert lt.stats.placeholders_extra == 1

    def test_loss_and_extra_both_counted(self):
        lt = _bare_lt()
        orig = "Take <<LATEX_0>> and <<LATEX_1>>."
        assert lt._revert_on_placeholder_loss("Al <<LATEX_0>> ve <<LATEX_7>>.", orig) == orig
        assert lt.stats.placeholders_lost == 1
        assert lt.stats.placeholders_extra == 1

    def test_instruction_example_outside_id_space(self):
        """Talimat örneği gerçek id uzayında değil; yankısı bekçiye takılır."""
        assert "<<LATEX_N>>" in _PLACEHOLDER_CLAUSE
        assert not re.search(r"<<[A-Z]+_\d+>>", _PLACEHOLDER_CLAUSE)
        lt = _bare_lt()
        assert lt._missing_placeholders("Kuvvet <<LATEX_0>> <<LATEX_N>>.", self.ORIG) \
            == {"<<LATEX_N>>"}

    @staticmethod
    def _einstein_stash():
        stash = [f"$f_{{{i}}}$" for i in range(13)]
        stash[0] = r"$\vec F = m\vec a$"
        stash[12] = "$E = mc^2$"
        return stash

    def test_einstein_probe_batched_end_to_end(self):
        """Rapordaki sonda: halüsine <<LATEX_12>> artık formülü taşımıyor.

        Eskiden 1. satır kabul ediliyordu; restore'un "ilk geçiş restore,
        kopyalar silinir" kuralı (#73) E = mc^2'yi kuvvet satırına koyup
        'Einstein: enerjidir.' satırından siliyordu.
        """
        body = "The force is <<LATEX_0>>.\n\nEinstein: <<LATEX_12>> is the energy."

        def batch(texts):
            outs = []
            for t in texts:
                if t == "The force is <<LATEX_0>>.":
                    outs.append("Kuvvet <<LATEX_0>> ve <<LATEX_12>> ile verilir.")
                elif t == "Einstein: <<LATEX_12>> is the energy.":
                    outs.append("Einstein: <<LATEX_12>> enerjidir.")
                else:
                    outs.append(_fake_tr(t))
            return outs

        lt = _make_lt(batch_fn=batch)
        out = lt._translate_paragraphs(body)
        restored = lt._restore_stash(out, self._einstein_stash())
        lines = restored.split("\n")
        assert lines[-1] == "Einstein: $E = mc^2$ enerjidir."
        assert r"$\vec F = m\vec a$" in lines[0]
        assert "E = mc^2" not in lines[0]
        assert restored.count("E = mc^2") == 1
        assert lt.stats.placeholders_extra == 1
        assert lt.stats.placeholder_split_fallbacks == 1

    def test_einstein_probe_sequential(self):
        def single(t, *a, **k):
            if t == "The force is <<LATEX_0>>.":
                return "Kuvvet <<LATEX_0>> ve <<LATEX_12>> ile verilir."
            return _fake_tr(t)

        lt = _make_lt(single_fn=single)
        out = lt._translate_line_preserving_placeholders("The force is <<LATEX_0>>.")
        assert "<<LATEX_12>>" not in out and "<<LATEX_0>>" in out
        assert lt.stats.placeholder_split_fallbacks == 1

    def test_caption_batch_extra_id_falls_back(self):
        def batch(texts):
            outs = []
            for t in texts:
                tr = _fake_tr(t)
                if t.startswith("<<LATEX_9>>"):
                    tr += " <<LATEX_3>>"            # başka altyazının id'si
                outs.append(tr)
            return outs

        lt = _make_lt(batch_fn=batch)
        caps = ["<<LATEX_3>>: Orbit of the upper stage.",
                "<<LATEX_9>>: Ground track of the probe and <<LATEX_10>> here."]
        outs = lt._translate_lines_preserving_placeholders_batch(caps)
        assert outs[1].count("<<LATEX_3>>") == 0
        assert "<<LATEX_9>>" in outs[1] and "<<LATEX_10>>" in outs[1]
        assert _w("Ground") in outs[1]              # parçalı yol çevirdi
        assert lt.stats.placeholder_split_fallbacks == 1


# ---------------------------------------------------------------------------
# C1b — kopya id (çoklu küme) ve yer değiştirme (bilinen sınır)
# ---------------------------------------------------------------------------

class TestC1bDuplicateAndSwap:
    def test_duplicate_in_whole_line_falls_back(self):
        calls = []

        def batch(texts):
            calls.append(list(texts))
            outs = []
            for t in texts:
                tr = _fake_tr(t)
                if "<<LATEX_0>>" in t and "<<LATEX_1>>" in t:
                    tr += " <<LATEX_0>>"            # kopya
                outs.append(tr)
            return outs

        lt = _make_lt(batch_fn=batch)
        out = lt._translate_paragraphs(LINE)
        assert calls == [[LINE]]                     # G2a-1: parça geçişi yok
        assert Counter(_PH_RE.findall(out)) == Counter(_PH_RE.findall(LINE))
        assert lt.stats.placeholders_extra == 1
        assert lt.stats.placeholder_split_fallbacks == 1
        assert lt.stats.recovered_by_retry == 1       # tekli tekrar tuttu
        assert _w("thrust") in out

    def test_duplicate_sequential_falls_back(self):
        def single(t, *a, **k):
            tr = _fake_tr(t)
            if "<<LATEX_0>>" in t and "<<LATEX_1>>" in t:
                tr = tr.replace("<<LATEX_1>>", "<<LATEX_1>> <<LATEX_1>>")
            return tr

        lt = _make_lt(single_fn=single)
        out = lt._translate_line_preserving_placeholders(LINE)
        assert Counter(_PH_RE.findall(out)) == Counter(_PH_RE.findall(LINE))
        assert lt.stats.placeholder_split_fallbacks == 1

    def test_swap_is_known_limit(self):
        """BİLİNEN SINIR — yer değiştirme bekçiden GEÇER (bilinçli karar).

        Türkçe söz dizimi (özne–nesne–yüklem, ters devrik yapı) yer
        tutucuların sırasını meşru olarak değiştirir: "<<LATEX_1>> …
        <<LATEX_0>>" doğru bir çeviri olabilir. Sıra kontrolü bu yüzden
        konmadı (her doğru çeviride yanlış alarm → gereksiz parçalı yol).
        Bedeli: anlamı değiştiren yer değiştirme (itki ile kütle debisinin
        karışması gibi) fark edilmez. Bekçi yalnız çoklu küme eşitliğine bakar.
        """
        def batch(texts):
            outs = []
            for t in texts:
                tr = _fake_tr(t)
                if "<<LATEX_0>>" in t and "<<LATEX_1>>" in t:
                    tr = (tr.replace("<<LATEX_0>>", "\0")
                            .replace("<<LATEX_1>>", "<<LATEX_0>>")
                            .replace("\0", "<<LATEX_1>>"))
                outs.append(tr)
            return outs

        lt = _make_lt(batch_fn=batch)
        out = lt._translate_paragraphs(LINE)
        assert out.index("<<LATEX_1>>") < out.index("<<LATEX_0>>")   # sıra değişti
        assert lt.stats.placeholder_split_fallbacks == 0             # kabul edildi
        assert lt.stats.placeholders_extra == 0


# ---------------------------------------------------------------------------
# C1c — koruma cümlesinin yankısı
# ---------------------------------------------------------------------------

_ORIG_SHORT = "The force is given by <<LATEX_3>>."
_ORIG_LONG = ("The force is given by <<LATEX_3>> and this also holds for the "
              "momentum <<LATEX_4>> of the body.")

# Rapordaki 6 varyant (denetim-yolb/leak_probe.py) + ekli/uzun biçimler.
LEAK_CASES = [
    ("Keep placeholder tokens like <<LATEX_12>> exactly as they are. "
     "Kuvvet <<LATEX_3>> ile verilir.", _ORIG_SHORT, "Kuvvet <<LATEX_3>> ile verilir."),
    ("Kuvvet <<LATEX_3>> ile verilir. Keep placeholder tokens like <<LATEX_12>> "
     "exactly as they are.", _ORIG_SHORT, "Kuvvet <<LATEX_3>> ile verilir."),
    ("<<LATEX_12>> gibi yer tutucuları olduğu gibi koru. Kuvvet <<LATEX_3>> ile verilir.",
     _ORIG_SHORT, "Kuvvet <<LATEX_3>> ile verilir."),
    ("<<LATEX_12>> gibi yer tutucu belirteçleri aynen koruyun. Kuvvet <<LATEX_3>> "
     "ile verilir.", _ORIG_SHORT, "Kuvvet <<LATEX_3>> ile verilir."),
    ("Yer tutucuları aynen koruyun. Kuvvet <<LATEX_3>> ile verilir.",
     _ORIG_SHORT, "Kuvvet <<LATEX_3>> ile verilir."),
    ("Kuvvet <<LATEX_3>> ile verilir ve bu, cismin momentumu <<LATEX_4>> için de "
     "geçerlidir; ayrıca <<LATEX_12>> gibi yer tutucuları olduğu gibi koru.",
     _ORIG_LONG,
     "Kuvvet <<LATEX_3>> ile verilir ve bu, cismin momentumu <<LATEX_4>> için de "
     "geçerlidir."),
    # Eklenen biçimler: 'belirteçlerini tam olarak oldukları gibi', yeni örnek token
    ("<<LATEX_12>> gibi yer tutucu belirteçlerini tam olarak oldukları gibi koruyun. "
     "Kuvvet <<LATEX_3>> ile verilir.", _ORIG_SHORT, "Kuvvet <<LATEX_3>> ile verilir."),
    ("Keep placeholder tokens like <<LATEX_N>> exactly as they are. "
     "Kuvvet <<LATEX_3>> ile verilir.", _ORIG_SHORT, "Kuvvet <<LATEX_3>> ile verilir."),
    ("Kuvvet <<LATEX_3>> ile verilir ve <<LATEX_N>> gibi yer tutucuları aynen bırakın.",
     _ORIG_SHORT, "Kuvvet <<LATEX_3>> ile verilir."),
]

LEGIT_SENTENCES = [
    "Bu yer tutucu sistemi olduğu gibi çalışır.",
    "Derleyici yer tutucuyu olduğu gibi bırakır.",
    "Şablon motoru yer tutucuları aynen korur.",
    "Bu yöntem yer tutucu değerleri olduğu gibi tutar.",
    "Bir yer tutucu var. Sonra formülü olduğu gibi koru.",
]


class TestC1cClauseLeak:
    @pytest.mark.parametrize("res,orig,expected", LEAK_CASES)
    def test_strip_prompt_leaks_removes_clause(self, res, orig, expected):
        out = BookTranslator._strip_prompt_leaks(MagicMock(), res, original=orig)
        assert out == expected
        assert "yer tutucu" not in out.lower() and "placeholder" not in out.lower()

    def test_short_segment_guard_does_not_restore_leak(self):
        """CORE-03 Türkçe-harf guard'ı yankı silmesini geri koymaz (kısa segment)."""
        res = "Yer tutucu belirteçleri aynen koruyun. Evet <<LATEX_3>>."
        out = BookTranslator._strip_prompt_leaks(MagicMock(), res, original="Yes <<LATEX_3>>.")
        assert out == "Evet <<LATEX_3>>."

    def test_clause_only_output_falls_back_to_original(self):
        out = BookTranslator._strip_prompt_leaks(
            MagicMock(), "<<LATEX_N>> gibi yer tutucuları olduğu gibi koru.",
            original=_ORIG_SHORT)
        assert out == _ORIG_SHORT          # echo → latex_pipeline düşüş yolu

    def test_guard_still_protects_other_patterns(self):
        """Muafiyet yalnız yankı kalıbı için: öteki Türkçe kalıplarda guard sürer."""
        res = "Sadece çevrilmiş metni ver ve formül ile hız korunur."
        out = BookTranslator._strip_prompt_leaks(MagicMock(), res, original="x")
        assert out == res

    @pytest.mark.parametrize("text", LEGIT_SENTENCES)
    def test_legit_sentence_kept(self, text):
        assert PLACEHOLDER_CLAUSE_LEAK_RE.sub("", text) == text
        assert BookTranslator._strip_prompt_leaks(MagicMock(), text, original="x") == text

    def test_pattern_registered_as_mt_only(self):
        """ALL_PROMPT_LEAK_PATTERNS'ta var; manuel (Yol A) listesinde yok."""
        assert PLACEHOLDER_CLAUSE_LEAK_RE in PROMPT_LEAK_PATTERNS_MT_ONLY
        assert PLACEHOLDER_CLAUSE_LEAK_RE in ALL_PROMPT_LEAK_PATTERNS
        assert PLACEHOLDER_CLAUSE_LEAK_RE not in PROMPT_LEAK_PATTERNS
        assert core_translator._PLACEHOLDER_CLAUSE_LEAK_RE is PLACEHOLDER_CLAUSE_LEAK_RE

    def test_latex_post_process_strips_clause(self):
        lt = _bare_lt()
        r = lt._post_process_translation(
            "Kuvvet <<LATEX_0>> ile verilir. <<LATEX_N>> gibi yer tutucuları olduğu gibi koru.",
            "The force is given by <<LATEX_0>>.")
        assert r == "Kuvvet <<LATEX_0>> ile verilir."

    def test_sanitizer_strips_only_in_mt_mode(self):
        body = "Kuvvet aşağıda verilir.\nYer tutucuları aynen koruyun.\nSon satır.\n"
        yol_b = LatexSanitizer(manual_translation=False)._fix_prompt_leaks(
            body, SanitizeReport())
        yol_a = LatexSanitizer(manual_translation=True)._fix_prompt_leaks(
            body, SanitizeReport())
        assert "tutucu" not in yol_b
        assert yol_a == body               # Yol A: manuel çeviriye dokunulmaz


# ---------------------------------------------------------------------------
# C1d — düşüş sözleşmesi
# ---------------------------------------------------------------------------

LONG_LINE = ("The first burn raises the apoapsis <<LATEX_0>> by a small amount. "
             "The second burn circularises the orbit at <<LATEX_1>> as required.")


def _drop_ph1_single(t, *a, **k):
    tr = _fake_tr(t)
    if "second burn" in t and "<<LATEX_1>>" in t:
        tr = tr.replace("<<LATEX_1>>", "")      # cümle düzeyinde kayıp
    return tr


class TestC1dFallbackContract:
    SECOND = "The second burn circularises the orbit at <<LATEX_1>> as required."

    def test_long_whole_line_sentence_loss_falls_back(self):
        """(a) Uzun satır (cümle yolu): cümle kaybında satır kurtarma sırasına
        girer; hep yutan modelde tutmayan cümle kaynak dilde kalır (parça
        çevirisi yok), öteki cümle çevrili kalır, işaretlenir (G2a-1)."""
        lt = _make_lt(single_fn=_drop_ph1_single, max_segment_chars=60)
        out = lt._translate_paragraphs(LONG_LINE)
        # cümle + İngilizce kalıntı yeniden denemesi: ikisi de geri alındı
        assert lt.stats.placeholders_lost >= 1
        assert lt.stats.placeholder_split_fallbacks == 1
        assert self.SECOND in out                    # özgün cümle, birebir
        assert "The first burn" not in out and _w("first") in out
        assert lt.stats.kept_source_segments == 1
        assert lt.stats.kept_source_list[0]["kismi"] is True
        assert Counter(_PH_RE.findall(out)) == Counter(_PH_RE.findall(LONG_LINE))

    def test_long_whole_line_same_as_sequential(self):
        """(a) Batched ve sıralı yol aynı durumda aynı kararı verir."""
        lt = _make_lt(single_fn=_drop_ph1_single, max_segment_chars=60)
        out = lt._translate_line_preserving_placeholders(LONG_LINE)
        assert lt.stats.placeholder_split_fallbacks == 1
        assert self.SECOND in out and _w("first") in out
        assert lt.stats.kept_source_segments == 1

    def test_long_whole_line_clean_not_split(self):
        lt = _make_lt(max_segment_chars=60)
        out = lt._translate_paragraphs(LONG_LINE)
        assert lt.stats.placeholder_split_fallbacks == 0
        assert _w("second") in out

    def test_remnant_revert_whole_line_falls_back(self):
        """(b) Onarılamayan [[BIRIM]] kalıntısı → satır kurtarma sırasına
        girer (G2a-1: aynı satır tekli yoldan bir kez daha); kalıntı yeniden
        denemesi (_fix_english_remnants) için boşa MT çağrısı yapılmaz."""
        single_sent = []
        def batch(texts):
            outs = []
            for t in texts:
                tr = _fake_tr(t)
                if "<<LATEX_0>>" in t and "<<LATEX_1>>" in t:
                    tr += " [[BIRIM_0]]"
                outs.append(tr)
            return outs

        def single(t, *a, **k):
            single_sent.append(t)
            return _fake_tr(t)

        lt = _make_lt(batch_fn=batch, single_fn=single)
        out = lt._translate_paragraphs(LINE)
        assert single_sent == [LINE]                 # yalnız bütün satır tekrarı
        assert lt.stats.remnant_reverts == 1
        assert lt.stats.placeholder_split_fallbacks == 1
        assert lt.stats.recovered_by_retry == 1
        assert _w("thrust") in out and "[[" not in out
        assert Counter(_PH_RE.findall(out)) == Counter(_PH_RE.findall(LINE))

    def test_remnant_revert_non_whole_segment_unchanged(self):
        """Yer tutucusuz segmentte kalıntı geri alması: parça bölme yok,
        satır sayacı artmaz. G2a-1: segment bir kez daha (tekli) denenir;
        tutmazsa özgün kalır ve işaretlenir."""
        def batch(texts):
            return [_fake_tr(t) + " [[BIRIM_0]]" for t in texts]

        lt = _make_lt(batch_fn=batch,
                      single_fn=lambda t, *a, **k: _fake_tr(t) + " [[BIRIM_0]]")
        out = lt._translate_paragraphs("km3 per second")
        assert out == "km3 per second"
        assert lt.stats.placeholder_split_fallbacks == 0
        assert lt.stats.kept_source_segments == 1

    def test_remnant_revert_non_whole_segment_retried(self):
        def batch(texts):
            return [_fake_tr(t) + " [[BIRIM_0]]" for t in texts]

        lt = _make_lt(batch_fn=batch)                # tekli yol temiz
        out = lt._translate_paragraphs("km3 per second")
        assert out == _fake_tr("km3 per second") and "[[" not in out
        assert lt.stats.placeholder_split_fallbacks == 0
        assert lt.stats.recovered_by_retry == 1

    def test_sequential_fallback_uses_remaining_budget(self):
        """(c) Kurtarma denemeleri segment_timeout'un KALANINI paylaşır
        (G2a-1: parçalar yerine tekrar / sade biçim denemeleri)."""
        budget = 2.0
        spent = 0.4

        def single(t, *a, **k):
            if "<<LATEX_0>>" in t and "<<LATEX_1>>" in t:
                time.sleep(spent)
                return _fake_tr(t).replace("<<LATEX_1>>", "")
            return _fake_tr(t)

        lt = _make_lt(single_fn=single, segment_timeout=budget)
        seen = []
        orig = lt._safe_translate_once

        def spy(text, timeout=None):
            seen.append(timeout)
            return orig(text, timeout)

        lt._safe_translate_once = spy
        out = lt._translate_line_preserving_placeholders(LINE)
        assert seen[0] == budget                     # bütün satır: tam bütçe
        retry_timeouts = seen[1:]
        assert len(retry_timeouts) == 1              # tekrar (stash/komut yok)
        assert all(0 < t <= budget - spent + 0.05 for t in retry_timeouts)
        assert lt.stats.placeholder_split_fallbacks == 1
        assert out == LINE                           # hep yutuyor: özgün kalır
        assert lt.stats.kept_source_segments == 1

    def test_sequential_timeout_does_not_double_budget(self):
        """(c) Bütün satır zaman aşımına düştüyse parçalar yeni bütçe almaz."""
        calls = []

        def single(t, *a, **k):
            calls.append(t)
            if "<<LATEX_0>>" in t:
                time.sleep(0.6)                      # zaman aşımı (0.2 sn)
            return _fake_tr(t)

        lt = _make_lt(single_fn=single, segment_timeout=0.2)
        out = lt._translate_line_preserving_placeholders(LINE)
        assert out == LINE                           # kaynak hâliyle kaldı
        assert calls == [LINE]                       # parçalar MT'ye gitmedi

    def test_pieces_without_deadline_unchanged(self):
        lt = _make_lt()
        out = lt._translate_line_pieces(LINE)
        assert _w("thrust") in out and "<<LATEX_0>>" in out


# ---------------------------------------------------------------------------
# C1e — çalışma dizininden bağımsız sentetik gövde fikstürü
# ---------------------------------------------------------------------------

def _fixture_body():
    lt = LatexTranslator.__new__(LatexTranslator)
    return lt._stash_protected(FIXTURE.read_text(encoding="utf-8"))


def _ph_lines(lt: LatexTranslator, body: str):
    return [
        l for l in body.split("\n")
        if "<<LATEX_" in l and not lt._is_placeholder_only(l)
        and len(re.sub(r"<<LATEX_\d+>>", "", l).strip()) >= 3
        and not re.match(r"\s*\\(?:chapter|section|subsection|subsubsection|caption)", l)
    ]


def _wrong_line_ids(body: str, out: str) -> int:
    """Kaynaktaki satırından başka satıra taşınmış id sayısı."""
    home = {}
    for i, l in enumerate(body.split("\n")):
        for d in _LATEX_ID_RE.findall(l):
            home[d] = i
    return sum(1 for i, l in enumerate(out.split("\n"))
               for d in _LATEX_ID_RE.findall(l) if home.get(d) != i)


class TestC1eSyntheticBody:
    def test_fixture_is_small_synthetic_and_cwd_independent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)                  # proje kökü dışından
        assert FIXTURE.is_file()
        n = len(FIXTURE.read_text(encoding="utf-8").splitlines())
        assert 20 <= n <= 60
        body, stash = _fixture_body()
        assert len(stash) >= 20

    def test_segment_counts_and_preservation(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sent = []

        def batch(texts):
            sent.extend(texts)
            return [_fake_tr(t) for t in texts]

        lt = _make_lt(batch_fn=batch)
        body, _ = _fixture_body()
        out = lt._translate_paragraphs(body)
        ph_segments = [t for t in sent if "<<LATEX_" in t]
        assert len(ph_segments) == len(_ph_lines(lt, body))   # satır = segment
        assert len(ph_segments) >= 10
        assert lt.stats.placeholder_split_fallbacks == 0
        assert lt.stats.segments_foreign_skipped == 1         # Latince satır
        assert Counter(_LATEX_ID_RE.findall(body)) == Counter(_LATEX_ID_RE.findall(out))
        assert _wrong_line_ids(body, out) == 0
        assert not any(b.startswith(" ") and not a.startswith(" ")
                       for a, b in zip(body.split("\n"), out.split("\n")))

    @pytest.mark.parametrize("leak", [
        " <<LATEX_N>> gibi yer tutucuları olduğu gibi koru.",
        " Keep placeholder tokens like <<LATEX_12>> exactly as they are.",
        " Yer tutucu belirteçlerini tam olarak oldukları gibi koruyun.",
    ])
    def test_no_leak_when_model_echoes_clause(self, leak):
        def batch(texts):
            return [_fake_tr(t) + (leak if "<<LATEX_" in t else "") for t in texts]

        lt = _make_lt(batch_fn=batch)
        body, _ = _fixture_body()
        out = lt._translate_paragraphs(body)
        low = out.lower()
        assert "tutucu" not in low and "placeholder" not in low
        assert "<<LATEX_N>>" not in out
        assert Counter(_LATEX_ID_RE.findall(body)) == Counter(_LATEX_ID_RE.findall(out))
        assert _wrong_line_ids(body, out) == 0

    @pytest.mark.parametrize("mode", ["fazla", "kopya", "literal", "kayip"])
    def test_corruptions_fall_back_to_pieces(self, mode):
        """Her 2. yer tutuculu segment bozulur → bütün satırlar kurtarma
        sırasına girer (G2a-1; ad tarihî), id'ler kendi satırında kalır,
        çoklu küme korunur."""
        body, _ = _fixture_body()
        whole_lines = {l.strip() for l in body.split("\n")}
        corrupted = []
        state = {"n": 0}

        def batch(texts):
            outs = []
            for t in texts:
                tr = _fake_tr(t)
                ids = _LATEX_ID_RE.findall(t)
                if ids:
                    state["n"] += 1
                    if state["n"] % 2 == 0:
                        corrupted.append(t)
                        if mode == "fazla":
                            other = "0" if ids[0] != "0" else "1"
                            tr += f" <<LATEX_{other}>>"
                        elif mode == "kopya":
                            tr += f" <<LATEX_{ids[0]}>>"
                        elif mode == "literal":
                            tr += " <<LATEX_N>>"
                        else:
                            tr = tr.replace(f"<<LATEX_{ids[-1]}>>", "", 1)
                outs.append(tr)
            return outs

        lt = _make_lt(batch_fn=batch)
        out = lt._translate_paragraphs(body)
        corrupted_whole = [t for t in corrupted if t in whole_lines]
        assert corrupted_whole
        assert lt.stats.placeholder_split_fallbacks == len(corrupted_whole)
        assert Counter(_LATEX_ID_RE.findall(body)) == Counter(_LATEX_ID_RE.findall(out))
        assert _wrong_line_ids(body, out) == 0
        assert "<<LATEX_N>>" not in out

    def test_long_path_on_fixture_falls_back(self):
        """Uzun satır yolu (cümle bölme) gerçekçi yapıda: her yer tutuculu
        cümlede kayıp → bütün uzun satırlar parçalı yola düşer, küme korunur."""
        body, _ = _fixture_body()

        def single(t, *a, **k):
            tr = _fake_tr(t)
            ids = _LATEX_ID_RE.findall(t)
            if ids:
                tr = tr.replace(f"<<LATEX_{ids[-1]}>>", "", 1)
            return tr

        lt = _make_lt(single_fn=single, max_segment_chars=100)
        out = lt._translate_paragraphs(body)
        long_ph_lines = [
            l for l in _ph_lines(lt, body)
            if not l.lstrip().startswith("\\item")
            and len(re.sub(r"<<LATEX_\d+>>", "", l).strip()) > 100
        ]
        assert long_ph_lines
        assert lt.stats.placeholder_split_fallbacks == len(long_ph_lines)
        assert Counter(_LATEX_ID_RE.findall(body)) == Counter(_LATEX_ID_RE.findall(out))
        assert _wrong_line_ids(body, out) == 0


# ---------------------------------------------------------------------------
# Denetçi B (24 Eylül 2026) — yankı silmesi metni yapıştırmaz
# ---------------------------------------------------------------------------
# Eski kalıp sondaki boşluğu ve satır sonunu da yutuyordu: bağlaçlı cümle
# ortası yankıda core "verilir.Sonra", latex/sanitizer ``sub("")`` yolu
# "verilirSonra" ve "verilir\section{B}" üretiyordu; kapanışsız kalıp
# "bırakın dedi" gibi devam eden cümleyi de siliyordu.

_MID_LEAKS = [
    "Kuvvet <<LATEX_0>> ile verilir; ayrıca yer tutucuları aynen koruyun. "
    "Sonra ivme bulunur.",
    "Kuvvet <<LATEX_0>> ile verilir, yer tutucuları olduğu gibi koruyun. "
    "Sonra ivme bulunur.",
    "Kuvvet <<LATEX_0>> ile verilir ve <<LATEX_N>> gibi yer tutucuları aynen "
    "koruyun. Sonra ivme bulunur.",
    "Kuvvet <<LATEX_0>> ile verilir; keep placeholder tokens like <<LATEX_N>> "
    "exactly as they are. Sonra ivme bulunur.",
]
_MID_EXPECTED = "Kuvvet <<LATEX_0>> ile verilir. Sonra ivme bulunur."
_MID_ORIG = "The force is <<LATEX_0>>. Then the acceleration follows."


class TestDenetciBClauseNoGlue:
    @pytest.mark.parametrize("res", _MID_LEAKS)
    def test_core_mid_sentence_keeps_space(self, res):
        out = BookTranslator._strip_prompt_leaks(MagicMock(), res, original=_MID_ORIG)
        assert out == _MID_EXPECTED

    @pytest.mark.parametrize("res", _MID_LEAKS)
    def test_plain_sub_does_not_glue(self, res):
        """latex/sanitizer/retranslator yolları kalıbı ``sub("")`` ile uygular."""
        assert PLACEHOLDER_CLAUSE_LEAK_RE.sub("", res) == _MID_EXPECTED

    @pytest.mark.parametrize("res", _MID_LEAKS[:2])
    def test_latex_post_process_mid_sentence(self, res):
        lt = _bare_lt()
        assert lt._post_process_translation(res, _MID_ORIG) == _MID_EXPECTED

    def test_continuing_sentence_not_deleted(self):
        """Yankı cümle sonunda biter; devam eden (aktarılan) cümle silinmez."""
        text = "Kullanıcı, yer tutucuları olduğu gibi bırakın dedi."
        assert PLACEHOLDER_CLAUSE_LEAK_RE.sub("", text) == text
        assert BookTranslator._strip_prompt_leaks(MagicMock(), text, original="x") == text

    def test_sanitizer_body_structure_kept(self):
        """Yol B sanitize: yankı silinir, başlık önceki paragrafa yapışmaz;
        satırın tamamı olan yankı satır sonuyla gider (boş satır kalmaz)."""
        body = ("Kuvvet verilir, yer tutucuları aynen koruyun.\n\n\\section{B}\n"
                "İlk satır.\nYer tutucuları aynen koruyun.\nSon satır.\n")
        out = LatexSanitizer(manual_translation=False)._fix_prompt_leaks(
            body, SanitizeReport())
        assert out == ("Kuvvet verilir.\n\n\\section{B}\n"
                       "İlk satır.\nSon satır.\n")
        assert LatexSanitizer(manual_translation=True)._fix_prompt_leaks(
            body, SanitizeReport()) == body
