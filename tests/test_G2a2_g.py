"""G2a-2 kulvarı (5 Ekim 2026): yerel modelin LaTeX çeviri zinciri, 2. bölüm.

Kanıt: yerel model testlerinin çalışma kopyası çıktıları (iç denetim) ve
okuma bulguları ("mt okuma MT-k").

Kalemler:

1. Kaynakça modele gitmez (mt okuma MT-19): girdiler aynen kalır, yalnız
   bölüm başlığı çevrilir (KARAR 20).
2. İstem işareti sızıntısı: ``<<<TRANSLATE_START>>>`` ve bozuk biçimleri
   çıktıdan her zaman temizlenir, sızıntı günlüğe yazılır.
3. Bağlaç parçaları (mt okuma MT-13): display matematikten önceki kısa parça
   ("where", "One obtains") komşu bağlamla birlikte çevrilir.
4. Ondalık ayırıcı (mt okuma MT-14, MT-15): Türkçe düzyazıda virgül;
   muhafazakâr kural.
5. Dedektör tutarlılığı (mt okuma §2, RAPOR MT-1): retranslator, çevirmenin
   segment kapısının "başka dil" dediği satıra (Latince) "English" demez.
6. ``\\appendix`` modele gitmez (G1a-1 önerisi).

KARAR 26 gereği testlerde kitap metni YOK: bütün cümleler sentetiktir (aynı
LaTeX yapısı, kendi cümlelerimiz). Kamu malı Latince (Newton) serbesttir.
Model yüklenmez; MagicMock çevirmen ya da sahte Ollama (yalnız test altyapısı).
"""
from __future__ import annotations

import re
import zlib
from unittest.mock import MagicMock

from latex_pipeline.config import PipelineConfig
from latex_pipeline.translator import LatexTranslator

# ---------------------------------------------------------------------------
# Sahte model (yalnız test altyapısı)
# ---------------------------------------------------------------------------
_TR = ("çalışma", "değer", "ışık", "yörünge", "şekil", "güneş", "ağırlık",
       "kütle", "hız", "yüzey", "dönüş", "öğrenci", "kuvvet", "enerji",
       "sıcaklık", "basınç", "akış", "düzlem", "eğri", "açı")
_TOKEN = re.compile(r"(<<[A-Z]+_\d+>>|\[\[[A-Za-z]+_\d+\]\]|@@[A-Za-z]+\d+@@)")
_WORDISH = re.compile(r"[^\W\d_]+(?:_[A-Za-z0-9]+)?")


def _tr_words(text: str) -> str:
    """Sözcük başına kararlı Türkçe sözcük; yer tutucu, sayı, noktalama ve
    LaTeX komut adları aynen kalır."""
    def word(m: re.Match) -> str:
        w = m.group(0)
        if m.start() > 0 and m.string[m.start() - 1] == "\\":
            return w
        if len(w) == 1 or "_" in w:
            return w
        tr = _TR[zlib.crc32(w.lower().encode("utf-8")) % len(_TR)]
        return tr[:1].upper() + tr[1:] if w[:1].isupper() else tr
    return "".join(p if _TOKEN.fullmatch(p) else _WORDISH.sub(word, p)
                   for p in _TOKEN.split(text))


def _mock_lt(batch_fn=None, single_fn=None, source_lang="en",
             calls=None) -> LatexTranslator:
    """MagicMock çevirmenli LatexTranslator; *calls* listesine modele giden
    her metin eklenir."""
    lt = LatexTranslator(
        pipeline_config=PipelineConfig(translate=True, segment_timeout=60),
        mt_engine="hymt-7b", source_lang=source_lang, target_lang="tr",
    )
    sent = calls if calls is not None else []
    b_fn = batch_fn or (lambda texts: [_tr_words(t) for t in texts])
    s_fn = single_fn or (lambda t, *a, **k: _tr_words(t))

    def batch(texts):
        sent.extend(texts)
        return b_fn(texts)

    def single(text, *a, **k):
        sent.append(text)
        return s_fn(text, *a, **k)

    bt = MagicMock()
    bt.translate_batch_protected = batch
    bt.translate_text = single
    lt._translator = bt
    return lt


# ===========================================================================
# (6) \appendix modele gitmez
# ===========================================================================
APPENDIX_BODY = (
    "\\chapter{Main Results}\n\n"
    "The engine was tested on a cold morning and the results were recorded.\n\n"
    "\\appendix\n\n"
    "\\chapter{Measurement Tables}\n\n"
    "The tables below list every value that was measured during the test.\n"
)


class TestEkKomutu:
    def test_appendix_yer_tutucuya_alinir(self):
        lt = _mock_lt()
        body, stash = lt._stash_protected(APPENDIX_BODY)
        assert "\\appendix" not in body
        assert "\\appendix" in stash

    def test_appendix_modele_gitmez_ve_korunur(self):
        calls = []
        lt = _mock_lt(calls=calls)
        out = lt.translate(APPENDIX_BODY)
        assert calls, "modele hiç istek gitmedi"
        assert not any("appendix" in c for c in calls), calls
        assert out.count("\\appendix") == 1
        assert "\n\\appendix\n" in out

    def test_satir_ici_appendix_de_korunur(self):
        stashed, cmds = LatexTranslator._stash_inline_commands(
            "\\appendix The tables follow here.")
        assert cmds == ["\\appendix"]
        assert "appendix" not in stashed

    def test_retranslator_yapisal_satir_sayar(self):
        from latex_pipeline.retranslator import _is_structural_line
        assert _is_structural_line("\\appendix")


# ===========================================================================
# (5) Dedektör tutarlılığı: retranslator, çevirmenin segment kapısıyla aynı
# kararı verir (mt okuma §2 / RAPOR MT-1: bir model testinde çevirmen Newton
# Latincesine "(la), çevrilmeden geçildi" derken retranslator aynı satırlara
# "English detected" diyordu)
# ===========================================================================
# Kamu malı: Newton, Principia (1687), hareket yasaları.
NEWTON_LEX = (
    "Lex I - Corpus omne perseverare in statu suo quiescendi vel movendi "
    "uniformiter in directum, nisi quatenus a viribus impressis cogitur statum "
    "illum mutare.",
    "Lex II - Mutationem motus proportionalem esse vi motrici impressae, et "
    "fieri secundum lineam rectam qua vis illa imprimitur.",
    "Lex III - Actioni contrariam semper et aequalem esse reactionem: sive "
    "corporum duorum actiones in se mutuo semper esse aequales et in partes "
    "contrarias dirigi.",
)
EN_SENTENCE = ("The satellite moves along the elliptical path, and it is "
               "visible from the ground at night.")


class TestDedektorTutarliligi:
    def test_latince_satir_cevrilmemis_sayilmaz(self):
        from core.lang_detect import classify_foreign_segment
        from latex_pipeline.retranslator import is_untranslated_line
        for line in NEWTON_LEX:
            for src in ("en", "de", "ru"):
                gate = classify_foreign_segment(line, src)
                assert gate == "la", (src, line)
                assert is_untranslated_line(line, src) is False, (src, line)

    def test_kapi_yabanci_diyorsa_cevrilmemis_degil(self):
        """İngilizce kaynakta kapının 'başka dil' dediği her satır (Latince,
        Fransızca alıntı) retranslator'da da çevrilmemiş değildir."""
        from core.lang_detect import classify_foreign_segment
        from latex_pipeline.retranslator import is_untranslated_line
        samples = NEWTON_LEX + (
            "Il est clair que ce qui se passe ici est tres important pour nous.",
            "Das ist ein langer Satz und wir sind mit dem Ergebnis sehr zufrieden.",
        )
        checked = 0
        for line in samples:
            if classify_foreign_segment(line, "en") is not None:
                checked += 1
                assert is_untranslated_line(line, "en") is False, line
        assert checked >= len(NEWTON_LEX)

    def test_qc_dedektoru_kaynaga_bakar_degismedi(self):
        """QC'nin ortak sezgiseli (is_english_line) değişmedi: QC Latinceyi
        yalnız KAYNAKTA birebir geçiyorsa muaf tutar (F2 kuralı); kaynakta
        olmayan Latince satırı (model uydurması) yakalamaya devam eder."""
        from latex_pipeline.retranslator import is_english_line
        assert is_english_line(NEWTON_LEX[1]) is True

    def test_ingilizce_satir_hala_yakalanir(self):
        from latex_pipeline.retranslator import is_english_line, is_untranslated_line
        assert is_english_line(EN_SENTENCE) is True
        assert is_untranslated_line(EN_SENTENCE, "en") is True
        # Rusça kaynakta modelin İngilizce ürettiği satır yine çevrilmemiştir
        # (kapı "en" der; bu bilinçli istisna korunur).
        assert is_untranslated_line(EN_SENTENCE, "ru") is True

    def test_retranslator_latince_satira_dokunmaz(self):
        from latex_pipeline.retranslator import scan_and_retranslate
        calls = []

        def tr(text):
            calls.append(text)
            return "Her cisim, üzerine etki eden kuvvet durumunu değiştirene dek durur."

        body = ("\\section{Hareket Yasaları}\n\n"
                "Newton yasalarını Latince özgün biçimleriyle aşağıda veriyoruz.\n\n"
                + "\n\n".join(NEWTON_LEX) + "\n")
        for src in ("en", "de"):
            calls.clear()
            res = scan_and_retranslate(body, translator_fn=tr, source_lang=src)
            assert res.english_lines_found == 0, (src, res.changes)
            assert res.output_tex == body
            assert calls == []

    def test_karma_satirdaki_latince_cumle_isaretlenmez(self):
        """Türkçe cümle + Latince cümle (model testindeki bir satırın biçimi): cümle düzeyi
        dedektör de Latinceye 'kaynak dilde kalmış' demez."""
        from latex_pipeline.retranslator import scan_and_retranslate
        calls = []
        line = ("Newton birinci yasayı şöyle yazar ve bunu hiç değiştirmez. "
                + NEWTON_LEX[1].split(" - ", 1)[1])
        res = scan_and_retranslate(line + "\n", translator_fn=lambda t: calls.append(t) or t,
                                   source_lang="en")
        assert res.english_lines_found == 0, res.changes
        assert calls == []

    def test_cevirmen_kalinti_dedektoru_de_ayni(self):
        """Çevirmenin kendi 'İngilizce kalıntı' denetimi (_is_english_sentence)
        de aynı kararı verir: Latince cümle yeniden çeviriye gitmez."""
        lt = _mock_lt(source_lang="en")
        for line in NEWTON_LEX:
            assert lt._is_english_sentence(line) is False, line


# ===========================================================================
# (1) Kaynakça modele gitmez; yalnız bölüm başlığı çevrilir (KARAR 20)
# mt okuma MT-19: bir model çıktısında eser adları çevrildi, "3. Aufl." "Auflage"
# oldu; başlık "Literatur" → "Kütüphanecilik" (MT-32).
# ===========================================================================
TOC = "\\addcontentsline{toc}{section}{\\csname @currentlabelname\\endcsname}"
# Sentetik künyeler (uydurma yazar/eser; yapı gerçek kanıttaki gibi).
DE_ENTRIES = (
    "\\begin{itemize}\n"
    "  \\item [1] P. Lindqvist: \\textit{Grundlagen der Kältetechnik}. 2. Aufl. "
    "Bremen: Verlag Nordlicht, 2017.\n"
    "  \\item [2] R. Ambach, T. Kreuzer: \\textit{Strömung in engen Kanälen}. "
    "Graz: Seewald \\& Partner, 2012.\n"
    "  \\item [3] U. Feldmann: Messfehler bei Thermoelementen. \\textit{Blätter "
    "für Messtechnik} 7 (2020), S. 11–29.\n"
    "\\end{itemize}"
)
DE_BIB_BODY = (
    "\\section{Die Wärmebilanz}\n\n"
    "Die Wärme fließt immer von der warmen Seite zur kalten Seite der Wand.\n\n"
    "\\section*{Literatur}" + TOC + "\n\n"
    + DE_ENTRIES + "\n\n"
    "\\section{Aufgaben}\n\n"
    "Berechnen Sie den Wärmestrom durch die Wand bei doppelter Dicke.\n"
)
_ENTRY_MARKERS = ("Lindqvist", "Aufl", "Ambach", "Feldmann", "Messtechnik",
                  "Nordlicht", "Kältetechnik")


def _sent_any(calls, words):
    return [c for c in calls if any(w in c for w in words)]


class TestKaynakca:
    def test_almanca_kaynakca_girdileri_modele_gitmez(self):
        calls = []
        lt = _mock_lt(source_lang="de", calls=calls)
        out = lt.translate(DE_BIB_BODY)
        assert not _sent_any(calls, _ENTRY_MARKERS), _sent_any(calls, _ENTRY_MARKERS)
        assert DE_ENTRIES in out                       # girdiler birebir
        assert "\\section*{Kaynakça}" + TOC in out     # başlık çevrildi, TOC aynen
        assert "Literatur" not in out
        # Kaynakçadan sonraki bölüm çevrildi (#57: bölge onu yutmaz).
        assert _sent_any(calls, ("Berechnen",))
        assert "Berechnen" not in out
        assert lt.stats.bibliography_sections == 1

    def test_baslik_modele_gitmez(self):
        calls = []
        lt = _mock_lt(source_lang="de", calls=calls)
        lt.translate(DE_BIB_BODY)
        assert not _sent_any(calls, ("Literatur",)), calls

    def test_alti_dilde_baslik(self):
        cases = (
            ("en", "\\chapter*{References}", "\\chapter*{Kaynaklar}"),
            ("en", "\\chapter*{Bibliography}", "\\chapter*{Kaynakça}"),
            ("en", "\\section*{REFERENCES}", "\\section*{KAYNAKLAR}"),
            ("de", "\\section*{Literaturverzeichnis}", "\\section*{Kaynakça}"),
            ("fr", "\\section*{Bibliographie}", "\\section*{Kaynakça}"),
            ("es", "\\section*{Bibliografía}", "\\section*{Kaynakça}"),
            ("it", "\\section*{Riferimenti bibliografici}", "\\section*{Kaynakça}"),
            ("ru", "\\section*{Список литературы}", "\\section*{Kaynakça}"),
        )
        entry = "Q. Zorbas: \\textit{Il vento e la vela}. Napoli: Edizioni Faro, 2004."
        for src, head, want in cases:
            calls = []
            lt = _mock_lt(source_lang=src, calls=calls)
            out = lt.translate(head + "\n\n" + entry + "\n")
            assert want in out, (src, out)
            assert entry in out, (src, out)
            assert not _sent_any(calls, ("Zorbas", "vento")), (src, calls)

    def test_numarali_kaynakca_basligi_numarasini_korur(self):
        calls = []
        lt = _mock_lt(source_lang="de", calls=calls)
        out = lt.translate("\\section{5 Literatur}\n\n" + DE_ENTRIES + "\n")
        assert "\\section{5 Kaynakça}" in out
        assert DE_ENTRIES in out
        assert not _sent_any(calls, _ENTRY_MARKERS)

    def test_alt_alt_bolum_duzeyinde_kaynakca(self):
        """Marker'ın \\subsubsection'a indirdiği bölüm sonu kaynakçası."""
        calls = []
        lt = _mock_lt(source_lang="en", calls=calls)
        body = ("\\subsubsection*{References}"
                "\\addcontentsline{toc}{subsection}{\\csname @currentlabelname\\endcsname}\n\n"
                "1. Okonkwo, B. and Varga, L., \\textit{Pumps for Small Farms}, "
                "Lakeside Press, Dublin, 1999.\n\n"
                "\\section{Valves}\n\n"
                "A valve controls the flow of water through the pipe.\n")
        out = lt.translate(body)
        assert not _sent_any(calls, ("Okonkwo", "Lakeside")), calls
        assert "Okonkwo, B. and Varga, L., \\textit{Pumps for Small Farms}" in out
        assert "\\subsubsection*{Kaynaklar}" in out
        assert _sent_any(calls, ("valve controls",))

    def test_thebibliography_ortami_aynen(self):
        calls = []
        lt = _mock_lt(source_lang="en", calls=calls)
        env = ("\\begin{thebibliography}{9}\n"
               "\\bibitem{ok} B. Okonkwo, \\emph{Pumps for Small Farms}, 1999.\n"
               "\\end{thebibliography}")
        out = lt.translate("The pump is described below.\n\n" + env + "\n")
        assert env in out
        assert not _sent_any(calls, ("Okonkwo",))

    def test_retranslator_kaynakca_girdilerine_dokunmaz(self):
        from latex_pipeline.retranslator import scan_and_retranslate
        calls = []
        tr_body = ("\\section{Isı Dengesi}\n\n"
                   "Isı her zaman duvarın sıcak yüzünden soğuk yüzüne akar.\n\n"
                   "\\section*{Kaynakça}" + TOC + "\n\n" + DE_ENTRIES + "\n")
        res = scan_and_retranslate(tr_body, translator_fn=lambda t: calls.append(t) or t,
                                   source_lang="de")
        assert calls == [] and res.output_tex == tr_body
        en_body = ("\\subsubsection*{Kaynaklar}"
                   "\\addcontentsline{toc}{subsection}{\\csname @currentlabelname\\endcsname}\n\n"
                   "1. Okonkwo, B. and Varga, L., \\textit{Pumps for Small Farms and "
                   "the Gardens of the North}, Lakeside Press, Dublin, 1999.\n\n"
                   "\\section{Vanalar}\n\n"
                   "The valve controls the flow of the water in the pipe of the house.\n")
        res = scan_and_retranslate(en_body, translator_fn=lambda t: calls.append(t) or "Vana borudaki suyun akışını denetler.",
                                   source_lang="en")
        assert not _sent_any(calls, ("Okonkwo",)), calls
        assert _sent_any(calls, ("valve controls",)), calls


# ===========================================================================
# (2) İstem işareti sızıntısı: çıktıdan HER ZAMAN temizlenir, günlüğe yazılır.
# Kanıt: bir test kitabının model çevirisinde iki yer —
# "<<><TRANSLATE_END>>", "<<><TRANSLATE_START>>" (patterns.py deseni bu bozuk
# biçimi yakalamıyordu); başka bir model testinin günlüğü: işaretli metin cümle düzeyinde
# yeniden çeviriye temizliksiz gitti.
# ===========================================================================
TR_SENT = "Bu yöntem, akış hızını ölçmek için kullanılan en basit yoldur."


class TestIstemIsareti:
    def test_bozuk_bicimler_temizlenir(self):
        from latex_pipeline.translator import strip_prompt_markers
        cases = (
            "<<<TRANSLATE_START>>>" + TR_SENT,
            "<<><TRANSLATE_START>>" + TR_SENT,
            TR_SENT + " <<><TRANSLATE_END>>",
            "<<< TRANSLATE START >>> " + TR_SENT,
            "<<TRANSLATE_END>>" + TR_SENT,
            "<<<ÇEVİRİ_BAŞLANGICI>>>" + TR_SENT,
            "<<<translate_start>>>" + TR_SENT,
        )
        for text in cases:
            cleaned, found = strip_prompt_markers(text)
            assert cleaned == TR_SENT, (text, cleaned)
            assert len(found) == 1, (text, found)

    def test_satir_ortasinda_bosluk_tekillesir(self):
        from latex_pipeline.translator import strip_prompt_markers
        cleaned, _ = strip_prompt_markers(
            "Gaz boğazda ses hızına ulaşır. <<><TRANSLATE_END>> Basınç düşer.")
        assert cleaned == "Gaz boğazda ses hızına ulaşır. Basınç düşer."

    def test_yer_tutucu_ve_matematik_dokunulmaz(self):
        from latex_pipeline.translator import strip_prompt_markers
        text = "Değer <<LATEX_3>> ile <<CMD_0>> arasında; $a<b>c$ ve x << y."
        assert strip_prompt_markers(text) == (text, [])

    def test_model_isareti_yankilarsa_temizlenir_ve_gunluge_yazilir(self, caplog):
        lt = _mock_lt(single_fn=lambda t, *a, **k: "<<><TRANSLATE_START>>" + _tr_words(t))
        with caplog.at_level("WARNING", logger="latex_pipeline.translator"):
            out = lt._safe_translate("The pump moves the water from the well to the tank.")
        assert "TRANSLATE" not in out and "<<>" not in out
        assert lt.stats.prompt_marker_leaks >= 1
        assert any("İstem işareti" in r.getMessage() for r in caplog.records)
        # İşaretin '<' '>' karakterleri ilişki simgesi bekçisini tetikleyip
        # iyi çeviriyi geri aldırmamalı (temizlik bekçilerden önce).
        assert lt.stats.kept_source_segments == 0
        assert not out.startswith("The pump")

    def test_baglam_isaretleri_modele_gitmez(self):
        """autofix'in bağlamlı yeniden çeviri girdisi: işaretler modele
        gönderilmez (model görmez, belleğe girmez).

        FX3 (2026-10-05) gerekçeli güncelleme: eskiden çıktıda da işaret
        yoktu — autofix hedefi hiç çıkaramıyor, bağlamlı yeniden çeviri
        boşa gidiyordu (bir model testinde 5 blokta "Could not extract
        target"). Artık yalnız hedef modele gider ve çevirisi protokol
        işaretlerinin ARASINA konur; bağlam satırları modele gitmez, aynen
        döner (autofix yalnız hedefi çıkarır)."""
        calls = []
        lt = _mock_lt(calls=calls)
        full = ("The tank is filled every morning.\n"
                "<<<TRANSLATE_START>>>The pump moves the water from the well."
                "<<<TRANSLATE_END>>>\n"
                "The valve is closed at night.")
        out = lt._safe_translate(full)
        assert calls and not any("TRANSLATE" in c for c in calls), calls
        assert not any("tank" in c or "valve" in c for c in calls), calls
        assert out.count("<<<TRANSLATE_START>>>") == 1
        assert out.count("<<<TRANSLATE_END>>>") == 1
        target = out.split("<<<TRANSLATE_START>>>")[1].split("<<<TRANSLATE_END>>>")[0]
        assert target.strip() and "pump" not in target

    def test_cumle_duzeyi_yeniden_ceviri_sizintisi_temizlenir(self):
        """Yankı → _check_mixed_language cümle düzeyi yeniden çeviri (çıktısı
        eskiden istem temizliğinden geçmiyordu)."""
        src = ("The pump moves the water from the well to the tank. "
               "The valve is closed at night by the farmer.")
        state = {"n": 0}

        def single(t, *a, **k):
            state["n"] += 1
            if state["n"] == 1:
                return t                                   # yankı
            return "<<><TRANSLATE_END>> " + _tr_words(t)

        lt = _mock_lt(single_fn=single)
        out = lt._safe_translate(src)
        assert "TRANSLATE" not in out and "<<>" not in out, out
        # Eskiden işaretin '<' '>' karakterleri sayı/simge bekçisine takılıp
        # segmenti kaynak dilde bıraktırıyordu.
        assert out != src and lt.stats.kept_source_segments == 0, out

    def test_translate_sonunda_govdede_isaret_kalmaz(self):
        def batch(texts):
            return ["<<<TRANSLATE_START>>>" + _tr_words(t) for t in texts]
        lt = _mock_lt(batch_fn=batch)
        body = ("\\section{Pumps}\n\n"
                "The pump moves the water from the well to the tank.\n\n"
                "The valve is closed at night by the farmer.\n")
        out = lt.translate(body)
        assert "TRANSLATE" not in out, out
        assert lt.stats.prompt_marker_leaks >= 1

    def test_retranslator_temizler_ve_gunluge_yazar(self, caplog):
        from latex_pipeline.retranslator import _clean_prompt_leaks
        with caplog.at_level("WARNING", logger="latex_pipeline.retranslator"):
            out = _clean_prompt_leaks("<<><TRANSLATE_START>>" + TR_SENT)
        assert out == TR_SENT
        assert any("İstem işareti" in r.getMessage() for r in caplog.records)


# ===========================================================================
# (4) Ondalık ayırıcı: Türkçe düzyazıda virgül — MUHAFAZAKÂR kural
# mt okuma MT-15 (bir model testinde "7.91", "1.327" ile "0,0123" karışık), MT-14
# (Almanca kaynağın "0,24"ü noktaya dönmüş).
# ===========================================================================
class TestOndalikAyirici:
    def _loc(self, tr, src, lang="en"):
        from latex_pipeline.translator import localize_decimal_separators
        return localize_decimal_separators(tr, src, lang)

    def test_ingilizce_kaynakta_birebir_gecen_ondalik_virgul_olur(self):
        out, n = self._loc("Dairesel hız 7.91 km/s, kaçış hızı 11.18 km/s olur.",
                           "The circular velocity is 7.91 km/s and the escape "
                           "velocity is 11.18 km/s.")
        assert out == "Dairesel hız 7,91 km/s, kaçış hızı 11,18 km/s olur."
        assert n == 2

    def test_kaynakta_olmayan_sayiya_dokunulmaz(self):
        out, n = self._loc("Değer 3.5 olarak alınır.", "The value is taken as three.")
        assert (out, n) == ("Değer 3.5 olarak alınır.", 0)

    def test_atif_numaralari_kapsam_disi(self):
        out, n = self._loc("Bölüm 2.3'te 4.5 m ölçüldü; bkz. Şekil 1.2 ve (1.5).",
                           "In Section 2.3 we measured 4.5 m; see Figure 1.2 and (1.5).")
        assert out == "Bölüm 2.3'te 4,5 m ölçüldü; bkz. Şekil 1.2 ve (1.5)."
        assert n == 1
        out, n = self._loc("2.3 numaralı denklemden 0.5 bulunur.",
                           "From equation 2.3 one finds 0.5.")
        assert out == "2.3 numaralı denklemden 0,5 bulunur." and n == 1

    def test_surum_tarih_saat_url_kapsam_disi(self):
        cases = (
            ("Python 3.8 ile süre 2.5 saniyedir.", "With Python 3.8 the run takes 2.5 seconds.",
             "Python 3.8 ile süre 2,5 saniyedir."),
            ("1.2.3 sürümü 12.03.2021 tarihinde çıktı.", "Release 1.2.3 came out on 12.03.2021.",
             "1.2.3 sürümü 12.03.2021 tarihinde çıktı."),
            ("Deney saat 10.30'da başladı.", "The test started at 10.30 am.",
             "Deney saat 10.30'da başladı."),
            ("Belge www.site.org/v2.5/doc adresinde.", "The file is at www.site.org/v2.5/doc today.",
             "Belge www.site.org/v2.5/doc adresinde."),
            ("USB 3.0 bağlantısı kullanılır.", "A USB 3.0 link is used here.",
             "USB 3.0 bağlantısı kullanılır."),
        )
        for tr, src, want in cases:
            assert self._loc(tr, src)[0] == want, (tr, src)

    def test_binlik_ayirici_ve_belirsizlik(self):
        # İngilizce "1,500" binliktir: çıktıdaki "1.500"e dokunulmaz.
        out, n = self._loc("Yaklaşık 1.500 vakada oran yüzde 2.5 idi.",
                           "In about 1,500 cases the rate was 2.5 percent.")
        assert out == "Yaklaşık 1.500 vakada oran yüzde 2,5 idi." and n == 1
        # Kaynakta aynı rakamlar iki ayırıcıyla: belirsiz, dokunulmaz.
        out, n = self._loc("Değerler 1.327 ve 1.327 olur.", "The values are 1.327 and 1,327.")
        assert n == 0

    def test_almanca_kaynakta_virgul_virgul_kalir(self):
        out, n = self._loc("Duvar 0.24 m kalınlığında ve 1.000 tuğladan oluşur.",
                           "Die Wand ist 0,24 m dick und hat 1.000 Steine.", "de")
        assert out == "Duvar 0,24 m kalınlığında ve 1.000 tuğladan oluşur." and n == 1
        out, n = self._loc("Bölüm 1.2'ye göre kalınlık 0,3 m'dir.",
                           "Nach Abschnitt 1.2 ist die Dicke 0,3 m.", "de")
        assert out == "Bölüm 1.2'ye göre kalınlık 0,3 m'dir." and n == 0

    def test_yer_tutucu_ve_matematik_dokunulmaz(self):
        out, n = self._loc("Hız <<LATEX_3>> değerine, yani 7.91 sınırına ulaşır.",
                           "The speed reaches <<LATEX_3>>, that is the 7.91 limit.")
        assert out == "Hız <<LATEX_3>> değerine, yani 7,91 sınırına ulaşır." and n == 1

    def test_satir_basi_numara_ve_yanki_kapsam_disi(self):
        """Gerçek ölçümde (bir test kitabında) yakalanan yanlış dönüşümler: alıştırma
        numarası satır başında ("10.3 Bir deneyde", "\\item 10.4 ..."),
        başlık numarası ve çevrilmemiş (yankı) satırdaki sayı."""
        cases = (
            ("10.3 Bir püskürtme deneyinde basınç 1.5 MPa olur.",
             "10.3 In a spray test the pressure is 1.5 MPa.",
             "10.3 Bir püskürtme deneyinde basınç 1,5 MPa olur."),
            ("\\item 10.4 Bir motor 0.85 MPa basınçta çalışır.",
             "\\item 10.4 An engine works at 0.85 MPa.",
             "\\item 10.4 Bir motor 0,85 MPa basınçta çalışır."),
            ("\\section{10.5 Enjektörün Başarımı}", "\\section{10.5 INJECTOR PERFORMANCE}",
             "\\section{10.5 Enjektörün Başarımı}"),
        )
        for tr, src, want in cases:
            assert self._loc(tr, src)[0] == want, (tr, src)
        # Etiketten sonraki numara aralığı/listesi (bir test kitabında: "Denklem
        # 7.15 7.17'ye kadar" ← "Eqs. 7.15–7.17").
        assert self._loc("Denklem 7.15 7.17'ye kadar birleştirilirse 2.5 bulunur.",
                         "Combining Eqs. 7.15–7.17 one finds 2.5.")[0] == \
            "Denklem 7.15 7.17'ye kadar birleştirilirse 2,5 bulunur."
        assert self._loc("Şekil 2.1 ve 2.2'de 0.5 m görülür.",
                         "Figures 2.1 and 2.2 show 0.5 m.")[0] == \
            "Şekil 2.1 ve 2.2'de 0,5 m görülür."
        echo = "The chamber pressure is around 2.73 MPa in this engine."
        assert self._loc(echo, echo) == (echo, 0)

    def test_kaynak_dilde_kalan_cumleye_dokunulmaz(self):
        """Kısmen çevrilmiş satırdaki İngilizce kalıntı cümle (bir test kitabının
        ölçümünde ~26 dönüşüm böyle yerdeydi): sayısı kaynak biçiminde kalır."""
        from latex_pipeline.translator import localize_decimal_separators
        lt = _mock_lt(source_lang="en")
        tr = "Basınç 1.5 MPa olur. The pump efficiency is 0.92 in this case."
        src = "The pressure is 1.5 MPa. The pump efficiency is 0.92 in this case."
        out, n = localize_decimal_separators(tr, src, "en",
                                             keep_fn=lt._is_english_sentence)
        assert out == "Basınç 1,5 MPa olur. The pump efficiency is 0.92 in this case."
        assert n == 1

    def test_zincirde_uygulanir_ve_sayilir(self):
        def batch(texts):
            return [t.replace("The orbital speed is", "Yörünge hızı")
                     .replace("near the surface.", "yüzeye yakın.") for t in texts]
        lt = _mock_lt(batch_fn=batch)
        body = ("\\section{Speed}\n\n"
                "The orbital speed is 7.91 km/s near the surface.\n")
        out = lt.translate(body)
        assert "Yörünge hızı 7,91 km/s yüzeye yakın." in out, out
        assert lt.stats.decimal_separators_localized == 1


# ===========================================================================
# (3) Bağlaç parçaları komşu bağlamla birlikte çevrilir (mt okuma MT-13:
# "where" → "nerede", "The dot product" → "Çarpım noktası"; display matematik
# cümleyi bölünce kısa parça bağlamsız gidiyordu).
# ===========================================================================
EQ1 = "\\[\na = F/m\n\\]"
EQ2 = "\\[\nF = -\\sum_j G m_j / r_j^2\n\\]"
TAIL = "is the total pull exerted on the body by all the other bodies of the system."
LONG_LEAD = "The motion of every body in the system can be written in this form"
GROUP_BODY = ("\\section{Motion}\n\n" + LONG_LEAD + "\n\n" + EQ1 + "\n\nwhere\n\n"
              + EQ2 + "\n\n" + TAIL + "\n")
CHAIN_BODY = ("\\section{Motion}\n\nNewton's law reads\n\n" + EQ1 + "\n\nwhere\n\n"
              + EQ2 + "\n\n" + TAIL + "\n")


def _paras(text):
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


class TestBaglacParcalari:
    def test_where_tek_basina_gitmez_baglamla_gider(self):
        calls = []
        lt = _mock_lt(calls=calls)
        out = lt.translate(GROUP_BODY)
        assert "where" not in [c.strip() for c in calls], calls
        joined = [c for c in calls if c.startswith("where <<LATEX_") and "total pull" in c]
        assert joined, calls
        # Kayıpsız: iki display birebir ve sırayla; paragraf yapısı korunur.
        assert out.index(EQ1) < out.index(EQ2)
        ps = _paras(out)
        i2 = ps.index(EQ2)
        assert ps[i2 - 2] == EQ1                    # EQ1, <where çevirisi>, EQ2
        assert ps[i2 - 1].strip() and "where" not in ps[i2 - 1]
        assert ps[i2 + 1].strip() and "total pull" not in ps[i2 + 1]
        assert lt.stats.context_groups == 1

    def test_zincir_iki_display(self):
        calls = []
        lt = _mock_lt(calls=calls)
        out = lt.translate(CHAIN_BODY)
        chain = [c for c in calls if c.startswith("Newton's law reads <<LATEX_")]
        assert chain and "where <<LATEX_" in chain[0] and "total pull" in chain[0], calls
        assert out.index(EQ1) < out.index(EQ2)
        assert lt.stats.context_groups == 1

    def test_turkce_soz_dizimi_yer_tutucuyu_basa_alirsa(self):
        """Model display yer tutucusunu başa aldı: parça paragrafı boş kalır,
        display önce, çevirinin tamamı ardından (boş paragraf yok)."""
        def batch(texts):
            out = []
            for t in texts:
                m = re.match(r"where (<<LATEX_\d+>>) (.*)", t)
                out.append(f"{m.group(1)} ifadesi, sistemdeki öteki cisimlerin "
                           f"uyguladığı toplam çekimdir." if m else _tr_words(t))
            return out
        lt = _mock_lt(batch_fn=batch, single_fn=lambda t, *a, **k: batch([t])[0])
        out = lt.translate(GROUP_BODY)
        ps = _paras(out)
        i2 = ps.index(EQ2)
        assert ps[i2 - 1] == EQ1                     # "where" paragrafı eridi
        assert ps[i2 + 1].startswith("ifadesi, sistemdeki")
        assert lt.stats.context_groups == 1

    def test_tutmazsa_parcalar_ayri_cevrilir_kayipsiz(self):
        """Bağlamlı çeviri yer tutucuyu yutarsa eski davranış: parçalar ayrı."""
        calls = []

        def lossy(t):
            if t.startswith("where <<LATEX_"):
                return re.sub(r"<<LATEX_\d+>>", "", _tr_words(t))
            return _tr_words(t)
        lt = _mock_lt(batch_fn=lambda ts: [lossy(t) for t in ts],
                      single_fn=lambda t, *a, **k: lossy(t), calls=calls)
        out = lt.translate(GROUP_BODY)
        assert "where" in [c.strip() for c in calls]        # ayrı çeviri
        assert out.count(EQ1) == 1 and out.count(EQ2) == 1
        assert lt.stats.context_group_fallbacks == 1
        assert lt.stats.context_groups == 0

    def test_display_sirasi_bozulursa_ayri_cevrilir(self):
        def swap(t):
            phs = re.findall(r"<<LATEX_\d+>>", t)
            out = _tr_words(t)
            if t.startswith("Newton's law reads") and len(phs) == 2:
                out = (out.replace(phs[0], "\x01").replace(phs[1], phs[0])
                       .replace("\x01", phs[1]))
            return out
        lt = _mock_lt(batch_fn=lambda ts: [swap(t) for t in ts],
                      single_fn=lambda t, *a, **k: swap(t))
        out = lt.translate(CHAIN_BODY)
        assert out.index(EQ1) < out.index(EQ2)
        assert lt.stats.context_group_fallbacks == 1

    def test_sirali_yol_da_baglamla_cevirir(self):
        """Toplu paragraf yolu çökerse _translate_paragraphs_sequential."""
        calls = []
        lt = _mock_lt(calls=calls)

        def boom(paragraphs):
            raise RuntimeError("toplu yol yok")
        lt._translate_paragraphs_batched = boom
        out = lt.translate(GROUP_BODY)
        assert "where" not in [c.strip() for c in calls], calls
        assert any(c.startswith("where <<LATEX_") for c in calls), calls
        assert out.index(EQ1) < out.index(EQ2)
        assert lt.stats.context_groups == 1

    def test_tam_cumle_ve_uzun_paragraf_birlesmez(self):
        calls = []
        lt = _mock_lt(calls=calls)
        body = ("Thus the result follows.\n\n" + EQ1 + "\n\n" + LONG_LEAD + "\n\n"
                + EQ2 + "\n\n" + TAIL + "\n")
        lt.translate(body)
        assert not any("<<LATEX_" in c and "result follows" in c for c in calls)
        assert not any(c.startswith(LONG_LEAD + " <<LATEX_") for c in calls)
        assert lt.stats.context_groups == 0


# ===========================================================================
# Uçtan uca (sahte Ollama, translate_book.run_pipeline; QC + autofix +
# retranslator AÇIK): beş kalem birlikte, kayıpsız.
# ===========================================================================
from test_kaynak_dil_20260928 import (  # noqa: E402,F401  (fikstür + yardımcı)
    _point_sources, fake_turkish, make_book, pipeline_env, restore_root_logger,
    run_book,
)

E2E_DE_BODY = (
    "\\chapter{Wärmeleitung}\n\n"
    "\\section{Die Wand}\n\n"
    "Eine Wand mit der Dicke von 0,24 m trennt den warmen Raum von der kalten "
    "Luft draußen, und die Wärme fließt langsam hindurch.\n\n"
    "Der Wärmestrom folgt aus dem Gesetz\n\n"
    "\\[\n\\dot{Q} = \\lambda A \\Delta T / s\n\\]\n\n"
    "mit der Wärmeleitfähigkeit\n\n"
    "\\[\n\\lambda = 0{,}8\n\\]\n\n"
    "und der Fläche A der Wand, die hier groß genug gewählt wurde.\n\n"
    "\\section*{Literatur}" + TOC + "\n\n" + DE_ENTRIES + "\n\n"
    "\\appendix\n\n"
    "\\chapter{Tabellen}\n\n"
    "Die folgenden Tabellen enthalten alle Werte, die im Text benutzt wurden.\n"
)


def _marker_echo(text: str) -> str:
    """Sahte model: çevirir ve ara sıra istem işaretini bozuk biçimde yankılar."""
    out = fake_turkish(text)
    return ("<<><TRANSLATE_START>>" + out) if "Wand" in text else out


class TestUctanUca:
    def test_almanca_kitap_zinciri(self, fake_llm_server, monkeypatch, pipeline_env):
        srv = fake_llm_server("ollama", models=["sahte"], reply=_marker_echo)
        _point_sources(monkeypatch, ollama_url=srv.url)
        out_dir = make_book(pipeline_env, E2E_DE_BODY, "kitap_g2a2")
        code = run_book(out_dir, "ollama:sahte", skip_qc=False)
        # 3 = son QC puanı eşiğin altında (sahte sözcük çevirisi); çökme yok.
        assert code in (0, 3), code
        sent = [b["messages"][-1]["content"] for b in srv.requests_to("/api/chat")]
        body = (out_dir / "source" / "translated_body.tex").read_text(encoding="utf-8")
        # (1) kaynakça modele gitmedi, girdiler birebir, başlık çevrildi
        assert not _sent_any(sent, _ENTRY_MARKERS), _sent_any(sent, _ENTRY_MARKERS)
        assert DE_ENTRIES in body and "\\section*{Kaynakça}" in body
        # (2) istem işareti çıktıya geçmedi
        assert "TRANSLATE" not in body
        # (3) "mit der Wärmeleitfähigkeit" tek başına gitmedi
        assert "mit der Wärmeleitfähigkeit" not in [s.strip() for s in sent]
        # (6) \appendix modele gitmedi, korundu
        assert not any("appendix" in s for s in sent) and "\\appendix" in body
        # kayıpsız: iki display birebir
        assert "\\dot{Q} = \\lambda A \\Delta T / s" in body
        assert "\\lambda = 0{,}8" in body
