"""Kulvar Q (2026-09-25) — QC yanlış alarmları (dil teyidi raporu R5).

Her sınıf bir düzeltmenin regresyon testidir; girdiler SENTETİKTİR (kısa,
bu test için yazılmış cümleler; kitap düzyazısı yok).

Q1  first_person_academic: "-yordu" (3. tekil) birinci şahıs değildir;
    "-yordum"/"-yorduk" yakalanır. Kaynak birinci tekil şahısla yazılmışsa
    (zamir yoğunluğu) çevirideki birinci şahıs sadakattir.
Q2  Anormal ünsüz kümesi: büyük harfli özel ad, kaynakça satırı ve kaynakta
    AYNEN geçen kelime muaf; kaynakta olmayan küçük harfli küme yakalanır.
Q3  number_hallucination: küçük "şek." kısaltması; OCR'ın noktayı boşluğa
    çevirdiği kaynak atıf ("Рис 1 13"); tek bölümlü belgede baskı ön ek
    hatası ("Рис. 4.18" -> "Şekil 1.18") ve öksüz noktalı numara.
Q4  Yanlış alarmlar: "a priori", "laminar" (İspanyolca kaynak, satır
    kayması), "MacClellan" (kaynakta aynen), paragraf sınırı kayması
    (sayfa sonu bölünmesi), İspanyolca kaynakta ASCII Türkçe başlık.
Q5  Özgün dilinde bırakılmış üçüncü dil alıntısı çevrilmemiş sayılmaz ve
    aynı satırlar foreign_language_leakage'da ikinci kez sayılmaz.
Q6  KNOWN_LONGER_COMMANDS: \\textheight/\\textfraction/\\textfloatsep;
    autofix '\\text' onarımı bunları bozmaz.
Q7  Inline math sayımı: boş tabanlı simge atomu önceki atomla bir ifade;
    kapanmamış '$$' paragraf sonunda biter ve ayrıca raporlanır.
Q8  EPUB dipnot kalıntısı "[[N]](dosya#çapa)" markdown_link_remnant
    (rapor-only).
"""

from __future__ import annotations

from collections import Counter

import pytest

from latex_pipeline.autofix import AutoFixer, _find_math_spans
from latex_pipeline.qc_engine import (
    KNOWN_LONGER_COMMANDS,
    QCEngine,
    QCReport,
)


# ---------------------------------------------------------------------------
# Sentetik kaynak metin üreticileri (dil tespiti >= 150 kelime ister)
# ---------------------------------------------------------------------------

_ES = ("El agua fluye por el tubo y la presión cambia con la altura del "
       "depósito. Los ingenieros miden la velocidad en cada punto para "
       "comparar los datos con la teoría. ")
_IT = ("Il sistema oscilla con una frequenza che dipende dalla massa e "
       "dalla rigidezza della molla. Gli studenti misurano il periodo per "
       "confrontare i risultati con la teoria. ")
_EN = ("The fluid moves through the pipe and the pressure changes with the "
       "height of the tank. The engineers measure the velocity at each "
       "point to compare the data with the theory. ")
_RU = ("Жидкость течёт по трубе, и давление меняется с высотой бака. "
       "Инженеры измеряют скорость в каждой точке, чтобы сравнить данные "
       "с теорией. ")
_TR = ("Akışkan boru boyunca ilerler ve basınç haznenin yüksekliğiyle "
       "değişir. Mühendisler hızı her noktada ölçerek verileri kuramla "
       "karşılaştırır. ")


def _body(sentence: str, n: int = 14) -> str:
    return "\n\n".join(sentence for _ in range(n))


def _analyze(tr: str, src: str, manual: bool = True, **kw) -> QCReport:
    return QCEngine(book_type="science", **kw).analyze(
        tr, src, manual_translation=manual)


def _by_check(rep: QCReport, check: str):
    return [i for i in rep.issues if i.check_name == check]


# ---------------------------------------------------------------------------
# Q1 — birinci şahıs
# ---------------------------------------------------------------------------

class TestBirinciSahis:
    def _fp(self, tr: str, src: str = ""):
        return _by_check(_analyze(tr, src or _body(_EN)),
                         "first_person_academic")

    def test_ucuncu_tekil_yordu_birinci_sahis_degil(self):
        tr = _body(_TR) + "\n\nMotor o yıllarda sıvı oksijenle çalışıyordu " \
             "ve deneyler sürüyordu.\n"
        assert self._fp(tr) == []

    def test_yordum_ve_yorduk_yakalanir(self):
        tr = _body(_TR) + "\n\nO sırada deneyi hazırlıyordum ve " \
             "sonuçları bekliyorduk.\n"
        found = self._fp(tr)
        assert len(found) == 1
        assert "hazırlıyordum" in found[0].message
        assert "bekliyorduk" in found[0].message

    def test_kaynak_birinci_sahisla_yazilmissa_uyari_yok(self):
        # İspanyolca kaynak: yazar kendi sesiyle konuşuyor (me/mi/yo).
        src = _body(_ES + "Yo creo que mi método me parece claro. ")
        tr = _body(_TR) + "\n\nBence yöntemim açık; bunu varsayıyorum.\n"
        assert self._fp(tr, src) == []

    def test_kaynak_kisiliksizse_uyari_surer(self):
        tr = _body(_TR) + "\n\nBence yöntemim açık; bunu varsayıyorum.\n"
        assert len(self._fp(tr, _body(_ES))) == 1


# ---------------------------------------------------------------------------
# Q2 — anormal ünsüz kümesi
# ---------------------------------------------------------------------------

class TestUnsuzKumesi:
    def _clusters(self, tr: str, src: str):
        return [i for i in _by_check(_analyze(tr, src),
                                     "hallucination_pattern")
                if "unsuz kumesi" in i.message]

    def test_buyuk_harfli_ozel_ad_muaf(self):
        tr = _body(_TR) + "\n\nBu harita Kurtschmidt tarafından çizilmiştir " \
             "ve bölge sınırlarını gösterir.\n"
        assert self._clusters(tr, _body(_EN)) == []

    def test_kaynakca_satiri_muaf(self):
        tr = (_body(_TR) + "\n\n\\section{Kaynakça}\n\n"
              "\\begin{itemize}\n  \\item [1] A. Yazar: Makale. "
              "\\textit{zeitschrift für testkunde} 3 (2020), S. 1–9.\n"
              "\\end{itemize}\n")
        assert self._clusters(tr, _body(_EN)) == []

    def test_kaynakta_aynen_gecen_kelime_muaf(self):
        src = _body(_EN) + "\n\nSee note[[1]](dosya-h-18.htm.xhtml#N1).\n"
        tr = _body(_TR) + "\n\nNota bakınız: dosya-h-18.htm.xhtml adresi " \
             "bu bölümde kullanılır.\n"
        assert self._clusters(tr, src) == []

    def test_kaynakta_olmayan_kume_yakalanir(self):
        tr = _body(_TR) + "\n\nBu değer brtschkal biçiminde yazılmış bir " \
             "sonuç verir ve tabloya girer.\n"
        found = self._clusters(tr, _body(_EN))
        assert len(found) == 1 and "rtschk" in found[0].message


# ---------------------------------------------------------------------------
# Q3 — atıf numaraları
# ---------------------------------------------------------------------------

class TestAtifNumarasi:
    def test_kucuk_sek_kisaltmasi_atif_sayilir(self):
        assert QCEngine._xref_numbers("basınç artar (şek. 1.1).") == ["1.1"]
        # küçük "denk." Türkçe sıfat kalır
        assert QCEngine._xref_numbers("birbirine denk. 3 kez") == []

    def test_kucuk_sek_ana_desenle_metin_sirasinda_birlesir(self):
        # 2026-09-28: küçük "şek." ayrı desenle eklenir (ana desen dizgesi
        # test_dalga_q'da kilitli); sonuç metindeki sırayı korur ve iki
        # desen aynı atfı iki kez saymaz.
        assert QCEngine._xref_numbers(
            "Şekil 1.2 ve (şek. 1.1) ile Şek. 2.3, Tablo 2.4") == [
                "1.2", "1.1", "2.3", "2.4"]
        # kaynak tarafı küçük Türkçe kısaltmayı tanımaz (yalnız çeviri)
        assert QCEngine._xref_numbers("(şek. 1.1)", source=True) == []

    def test_kesme_ekli_atif_sayilir(self):
        assert QCEngine._xref_numbers("Tablo 1.1'de ve Şekil 1.2'ye") == [
            "1.1", "1.2"]

    def test_sek_kisaltmasiyla_eksik_referans_yok(self):
        src = _body(_ES) + "\n\nLa presión crece (fig. 1.1).\n\n" \
              "Figura 1.1: Distribución.\n"
        tr = _body(_TR) + "\n\nBasınç artar (şek. 1.1).\n\n" \
             "Şekil 1.1: Dağılım.\n"
        assert _by_check(_analyze(tr, src), "number_hallucination") == []

    # Tek bölümlü belge: kaynak atıfların büyük çoğunluğu "1.x".
    _REFS_RU = "См. рис. 1.1, рис. 1.2, рис. 1.3, рис. 1.4 и рис. 1.5."
    _REFS_TR = "Bkz. Şekil 1.1, Şekil 1.2, Şekil 1.3, Şekil 1.4 ve Şekil 1.5."

    def _ru_doc(self, caption: str) -> str:
        return (_body(_RU) + "\n\n" + self._REFS_RU + "\n\n"
                "На рис. 1.18 показан двигатель.\n\n" + caption + "\n")

    def test_ocr_bosluklu_numara_uzlasir(self):
        src = self._ru_doc("Рис 1 13 Двигатель А-1\n\nРис. 1.13 и рис. 1.13.")
        tr = (_body(_TR) + "\n\n" + self._REFS_TR + "\n\n"
              "Şekil 1.18'de motor görülmektedir.\n\n"
              "Şekil 1.13 A-1 motoru\n\nŞekil 1.13 ve Şekil 1.13.\n")
        assert _by_check(_analyze(tr, src), "number_hallucination") == []

    def test_tek_bolumde_baski_on_ek_hatasi_uzlasir(self):
        src = self._ru_doc("Рис. 4.18. Первый двигатель")
        tr = (_body(_TR) + "\n\n" + self._REFS_TR + "\n\n"
              "Şekil 1.18'de motor görülmektedir.\n\n"
              "Şekil 1.18. İlk motor\n")
        rep = _analyze(tr, src)
        assert _by_check(rep, "number_hallucination") == []
        assert not any("4.18" in i.message
                       for i in _by_check(rep, "hallucination"))

    def test_gercek_bolum_onekli_degisim_uzlasmaz(self):
        # 4.x başka atıflarda da geçiyor: gerçek bölüm, uzlaştırma yok.
        # (Ek 1.x atıfları baskınlık eşiğini aşar; engelleyen yalnız
        # ön ekin başka numarada da geçmesidir.)
        extra_ru = " ".join(f"рис. 1.{k}." for k in range(6, 16))
        extra_tr = " ".join(f"Şekil 1.{k}." for k in range(6, 16))
        src = self._ru_doc("Рис. 4.18. Первый двигатель\n\nСм. рис. 4.2. "
                           + extra_ru)
        tr = (_body(_TR) + "\n\n" + self._REFS_TR + "\n\n"
              "Şekil 1.18'de motor görülmektedir.\n\n"
              "Şekil 1.18. İlk motor\n\nBkz. Şekil 4.2. " + extra_tr + "\n")
        msgs = [i.message for i in _by_check(_analyze(tr, src),
                                             "number_hallucination")]
        assert any("'4.18'" in m for m in msgs)

    def test_rakam_degisimi_uzlasmaz(self):
        src = _body(_EN) + "\n\nSee Figure 2.14 and Figure 2.3.\n"
        tr = _body(_TR) + "\n\nBkz. Şekil 2.12 ve Şekil 2.3.\n"
        msgs = [i.message for i in _by_check(_analyze(tr, src, manual=False),
                                             "number_hallucination")]
        assert any("'2.14'" in m for m in msgs)


# ---------------------------------------------------------------------------
# Q4 — rapordaki diğer yanlış alarmlar
# ---------------------------------------------------------------------------

class TestDigerYanlisAlarmlar:
    def test_a_priori_artikel_degil(self):
        tr = _body(_TR) + "\n\nBu öğretilere karşı zihnimizin \\textit{a " \
             "priori} bir direnci vardır ve a posteriori kanıt ister.\n"
        found = [i for i in _by_check(_analyze(tr, _body(_ES)),
                                      "turkish_naturalness")
                 if "artikel" in i.message]
        assert found == []

    def test_ingilizce_artikel_hala_yakalanir(self):
        tr = _body(_TR) + "\n\nBu bölümde yöntem the model ile açıklanır " \
             "ve sonuç verir.\n"
        found = [i for i in _by_check(_analyze(tr, _body(_EN)),
                                      "turkish_naturalness")
                 if "artikel" in i.message]
        assert len(found) == 1

    def test_ispanyolca_kaynakta_turbulento_takasi_aciklar(self):
        src = (_body(_ES) + "\n\nEn régimen turbulento el factor depende "
               "de la rugosidad.\n\nSe usa la ec. (2.5) si el flujo es "
               "laminar.\n")
        # Satırlar bilerek kaydırıldı: "türbülanslı" satırı kaynağın
        # "laminar" satırının ±2 penceresine düşer.
        tr = (_body(_TR) + "\n\n\n\nLaminer akışta ifade kesindir.\n\n"
              "türbülanslı rejimde faktör pürüzlülüğe bağlıdır.\n")
        assert _by_check(_analyze(tr, src), "antonym_guard") == []

    def test_ingilizce_gercek_takas_yakalanir(self):
        src = _body(_EN) + "\n\nThe flow in the pipe is laminar here.\n"
        tr = _body(_TR) + "\n\nBorudaki akış burada türbülanslıdır.\n"
        assert len(_by_check(_analyze(tr, src, manual=False),
                             "antonym_guard")) == 1

    def test_sayi_esitken_dogru_karsilik_eksikse_takas_yakalanir(self):
        # 2026-09-28: kaynak 1 'laminar' + 2 'turbulent'; çeviri 'laminar'ı
        # 'türbülanslı', bir 'turbulent'i 'türbülans' yazdı. 'türbülanslı'
        # sayısı (2) kaynak 'turbulent' sayısını aşmıyor ama kaynaktaki
        # 'laminar'ın doğru karşılığı ('laminer') çeviride yok: takas
        # satır düzeyinde aranmalı ve bulunmalı.
        src_lines = [f"Line {k} plain text." for k in range(30)]
        tr_lines = [f"Satır {k} düz metin." for k in range(30)]
        src_lines[5] = "Near the inlet the flow is laminar."
        tr_lines[5] = "Girişe yakın akış türbülanslıdır."
        src_lines[12] = "Far downstream the flow becomes turbulent."
        tr_lines[12] = "Çok aşağıda akış türbülanslı hâle gelir."
        src_lines[20] = "Turbulent mixing dominates the wake."
        tr_lines[20] = "İz bölgesinde türbülans karışımı baskındır."
        src = "\n".join(src_lines) + "\n"
        tr = "\n".join(tr_lines) + "\n"
        rep = QCReport()
        QCEngine()._check_antonym_guard(tr, src, tr.split("\n"),
                                        src.split("\n"), rep)
        found = _by_check(rep, "antonym_guard")
        assert len(found) == 1 and found[0].line_num == 6

    def test_kaynakta_aynen_gecen_ozel_ad_bitisik_bosluk_degil(self):
        src = _body(_EN) + "\n\nMajor MacClellan spoke first.\n"
        tr = _body(_TR) + "\n\nİlk olarak Binbaşı MacClellan konuştu.\n"
        assert _by_check(_analyze(tr, src), "word_concatenation") == []

    def test_gercek_bitisik_kelime_yakalanir(self):
        tr = _body(_TR) + "\n\nBu yöntemBu sonucu verir ve kullanılır.\n"
        assert len(_by_check(_analyze(tr, _body(_EN)),
                             "word_concatenation")) == 1

    # --- paragraf sınırı kayması (sayfa sonu bölünmesi) ---

    _EN_A = ("Military rockets must satisfy strict requirements that follow "
             "from their use: long storage in a state of readiness, short "
             "launch time, the possibility of trans")
    _EN_B = "portation in a state close to readiness, and so on and so forth."
    _TR_A = ("Askerî füzeler kullanımlarından doğan katı gereksinimleri "
             "karşılamalıdır: hazır durumda uzun süre depolanabilme, kısa "
             "fırlatma süresi, hazırlığa yakın bir durumda")
    _TR_B = "taşınabilme vb."

    def _loss(self, tr_a: str, tr_b: str):
        src = "\n\n".join([_EN * 2, self._EN_A, self._EN_B, _EN * 2])
        tr = "\n\n".join([_TR * 2, tr_a, tr_b, _TR * 2])
        return [i for i in _by_check(_analyze(tr, src), "hallucination")
                if "Information loss" in i.message]

    def test_sayfa_sonu_bolunmesi_sinir_kaymasi_kayip_degil(self):
        assert self._loss(self._TR_A, self._TR_B) == []

    def test_gercek_kayip_hala_yakalanir(self):
        # Komşu paragraf telafi etmiyor (kısaltılmış): gerçek kayıp.
        assert len(self._loss(self._TR_A[:40], self._TR_B)) >= 1

    # --- başlık ---

    def test_ispanyolca_kaynakta_ascii_turkce_baslik_cevrilmemis_degil(self):
        src = _body(_ES) + "\n\n\\subsubsection{Recuadro 1.1. La paradoja " \
              "hidrostática}\n"
        tr = _body(_TR) + "\n\n\\subsubsection{Kutu 1.1. Hidrostatik " \
             "paradoks}\n"
        assert _by_check(_analyze(tr, src), "untranslated_heading") == []

    def test_kaynakta_gecen_ingilizce_baslik_ispanyolca_kaynakta_yakalanir(
            self):
        src = _body(_ES) + "\n\n\\section{Machine Learning Methods}\n"
        tr = _body(_TR) + "\n\n\\section{Machine Learning Methods}\n"
        assert len(_by_check(_analyze(tr, src),
                             "untranslated_heading")) == 1

    def test_ingilizce_kaynakta_ingilizce_baslik_yakalanir(self):
        src = _body(_EN) + "\n\n\\section{Hydrostatic Paradox}\n"
        tr = _body(_TR) + "\n\n\\section{Hydrostatic Paradox}\n"
        assert len(_by_check(_analyze(tr, src),
                             "untranslated_heading")) == 1


# ---------------------------------------------------------------------------
# Q5 — özgün dilinde bırakılmış alıntı
# ---------------------------------------------------------------------------

_FR_QUOTE = [
    "\"Nous voulons observer les planètes avec les meilleurs instruments de",
    "notre temps, comparer les mesures des astronomes et montrer que la",
    "science peut répondre à des questions que les poètes posaient dans",
    "les livres anciens; c'est pour nous un travail qui demande de la",
    "patience et beaucoup de nuits claires\".",
]


class TestOzgunDilAlinti:
    def _src(self) -> str:
        return (_body(_IT) + "\n\nL'autore scrive: " + "\n".join(_FR_QUOTE)
                + " Questo è il programma.\n")

    def test_ucuncu_dil_alintisi_cevrilmemis_sayilmaz(self):
        tr = (_body(_TR) + "\n\nYazar şöyle der: " + "\n".join(_FR_QUOTE)
              + "\\footnote{Çevirmen notu: Bilimin bütün ışıklarını ortak "
              "noktaya yöneltmek; çağımız için önemli bir sorundur.} "
              "Bu, programın özüdür.\n")
        rep = _analyze(tr, self._src())
        assert _by_check(rep, "untranslated") == []
        assert _by_check(rep, "foreign_language_leakage") == []

    def test_kaynagin_kendi_dilindeki_kopya_cevrilmemistir(self):
        it_lines = ["Il sistema oscilla con una frequenza che dipende dalla",
                    "massa e dalla rigidezza della molla; gli studenti",
                    "misurano il periodo per confrontare i risultati con la",
                    "teoria e con le misure della lezione precedente."]
        src = _body(_IT) + "\n\n" + "\n".join(it_lines) + "\n"
        tr = _body(_TR) + "\n\n" + "\n".join(it_lines) + "\n"
        rep = _analyze(tr, src)
        assert len(_by_check(rep, "untranslated")) >= 1

    _IT_LINES = ["Il sistema oscilla con una frequenza che dipende dalla",
                 "massa e dalla rigidezza della molla; gli studenti",
                 "misurano il periodo per confrontare i risultati con la",
                 "teoria e con le misure della lezione precedente."]

    def test_ayni_satirlar_ikinci_kez_sayilmaz(self):
        # 2026-09-28: İtalyanca kaynak, İtalyanca kalmış satırlar: bulgu
        # "untranslated"tır; foreign_language_leakage aynı satırları ikinci
        # kez saymaz (R5: aynı pasaj 2 CRITICAL + 1 MEDIUM sayılıyordu).
        src = _body(_IT) + "\n\n" + "\n".join(self._IT_LINES) + "\n"
        tr = _body(_TR) + "\n\n" + "\n".join(self._IT_LINES) + "\n"
        rep = _analyze(tr, src)
        assert len(_by_check(rep, "untranslated")) >= 1
        assert _by_check(rep, "foreign_language_leakage") == []

    def test_kaynaktan_farkli_dil_sizintisi_iki_denetimde_kalir(self):
        # 2026-09-28 düzeltmesi: 25 Eylül'ün yarım kodu Check 2'nin saydığı
        # HER satırı Check 19'dan düşürüyordu; kaynaksız ya da kaynaktan
        # FARKLI dildeki (İngilizce kaynak, Fransızca çıktı) sızıntı Check
        # 19'da görünmez oluyordu (test_dil_destegi / test_yolb_qc
        # korumaları). Bu durumda iki denetim de raporlar (eski davranış).
        tr = _body(_TR) + "\n\n" + "\n".join(_FR_QUOTE) + "\n"
        rep = _analyze(tr, _body(_EN))
        assert len(_by_check(rep, "untranslated")) >= 1
        assert _by_check(rep, "foreign_language_leakage")

    def test_ingilizce_eser_adi_iceren_kaynak_satiri_bloktan_dusmez(self):
        # 2026-09-28: İtalyanca kaynağın çevrilmemiş satırı içindeki kısa
        # İngilizce eser adı satırı "İngilizce" gösteriyordu; satır kaynakta
        # birebir geçtiği için alıntı muafiyeti alıyor ve blok ikiye
        # bölünüyordu (_deneme_UIa_cikar). İngilizce dalında muafiyet yok.
        lines = ["Il sistema oscilla con una frequenza che dipende dalla",
                 "Terre e Davide Brewster (More Worlds than One) i quali "
                 "collocarono",
                 "misurano il periodo per confrontare i risultati con la",
                 "teoria e con le misure della lezione precedente."]
        src = _body(_IT) + "\n\n" + "\n".join(lines) + "\n"
        tr = _body(_TR) + "\n\n" + "\n".join(lines) + "\n"
        unt = _by_check(_analyze(tr, src), "untranslated")
        assert len(unt) == 1, unt
        assert "4 consecutive lines" in unt[0].message

    def test_kaynaksiz_analizde_sizinti_raporlanir(self):
        tr = _body(_TR) + "\n\n" + "\n".join(self._IT_LINES) + "\n"
        rep = _analyze(tr, "")
        assert _by_check(rep, "foreign_language_leakage")


# ---------------------------------------------------------------------------
# Q6 — KNOWN_LONGER_COMMANDS / autofix '\text' onarımı
# ---------------------------------------------------------------------------

class TestUzunKomutlar:
    @pytest.mark.parametrize("name", ["textheight", "textfraction",
                                      "textfloatsep", "textstyle"])
    def test_kumede(self, name):
        assert name in KNOWN_LONGER_COMMANDS

    def test_autofix_text_onarimi_parametreleri_bozmaz(self):
        body = ("\\includegraphics[width=0.8\\textwidth,height=0.7"
                "\\textheight]{a.png}\n"
                "\\setlength{\\textfloatsep}{10pt} "
                "\\renewcommand{\\textfraction}{0.1}\n"
                "\\textbfSpektrum burada.\n")
        fixer = AutoFixer.__new__(AutoFixer)
        fixed, _ = AutoFixer._fix_broken_command_braces(
            fixer, body, [], _find_math_spans(body))
        assert "\\text{height}" not in fixed
        assert "\\text{floatsep}" not in fixed
        assert "\\text{fraction}" not in fixed
        assert "0.7\\textheight" in fixed
        assert "\\textbf{Spektrum}" in fixed


# ---------------------------------------------------------------------------
# Q7 — inline math sayımı
# ---------------------------------------------------------------------------

class TestInlineMatematik:
    def test_bos_tabanli_simge_birlesmesi_kayip_degil(self):
        e = QCEngine()
        src = r"Oran $\zeta$ ${}^{2}$ ve $\omega$${}^{n}$ ile $x$ verilir."
        tr = r"Oran $\zeta^{2}$ ve $\omega^{n}$ ile $x$ verilir."
        assert e._count_inline_math(src) == e._count_inline_math(tr) == 3

    def test_ayri_ifadeler_hala_ayri_sayilir(self):
        e = QCEngine()
        assert e._count_inline_math(r"$a$ ve $b$, $c$$\theta$") == 4

    def test_kapanmamis_display_paragraf_sonunda_biter(self):
        e = QCEngine()
        tr = ("$$^{1} Türkçe dipnot metni $\\lambda$ yaygındır.\n\n"
              "Sonraki paragraf $a$ ve $b$ içerir.\n")
        count, open_lines = QCEngine._scan_dollar_math(tr, keep_lines=True)
        assert count == 2          # sonraki paragraf yutulmaz
        assert open_lines == [1]
        rep = QCReport()
        e._check_formula_integrity(tr, "", tr.split("\n"), [], rep)
        found = _by_check(rep, "broken_latex_syntax")
        assert len(found) == 1 and found[0].line_num == 1
        assert found[0].auto_fixable is False

    def test_blok_ici_bos_satir_sahte_paragraf_sonu_olmaz(self):
        # Silinen \begin{split} blok yeri boş satır sayılmamalı.
        tr = "$$\n\\begin{split}\na &= b\n\\end{split}\n$$ ve $x$.\n"
        count, open_lines = QCEngine._scan_dollar_math(tr)
        assert open_lines == [] and count == 1


# ---------------------------------------------------------------------------
# Q8 — dosya çapalı markdown bağlantısı
# ---------------------------------------------------------------------------

class TestDosyaCapaliBaglanti:
    def test_rapor_only_bulgu(self):
        tr = ("görünebilir[[1]](55176\\_66373-h-18.htm.xhtml\\#Footnote\\_1);"
              " ama\n")
        rep = QCReport()
        QCEngine()._check_markdown_link_remnants(tr, tr.split("\n"), rep)
        found = _by_check(rep, "markdown_link_remnant")
        assert len(found) == 1
        assert found[0].auto_fixable is False

    def test_sayfa_capasi_eski_desenle_kalir(self):
        tr = "bkz.[[3](#page-12-0)] burada\n"
        rep = QCReport()
        QCEngine()._check_markdown_link_remnants(tr, tr.split("\n"), rep)
        found = _by_check(rep, "markdown_link_remnant")
        # Eski desenler (A/D) aynen bulur ve autofix'e açık kalır; yeni
        # dosya çapalı desen bu biçime karışmaz.
        assert found and all(i.auto_fixable for i in found)
        assert not any("File-anchored" in i.message for i in found)


# ---------------------------------------------------------------------------
# Kaynak bağlamı önbelleği
# ---------------------------------------------------------------------------

def test_kaynak_baglami_ayni_kaynakta_yeniden_kurulmaz():
    e = QCEngine(book_type="science")
    src = _body(_ES)
    e.analyze(_body(_TR), src, manual_translation=True)
    first = e._src_ctx
    e.analyze(_body(_TR) + "\n\nEk.\n", src, manual_translation=True)
    assert e._src_ctx is first and first.lang == "es"
    e.analyze(_body(_TR), "", manual_translation=True)
    assert e._src_ctx is None


# ---------------------------------------------------------------------------
# 2026-09-28 — KITAPCEVIR-META başlığı TR→kaynak satır eşlemesini kaydırmaz
# ---------------------------------------------------------------------------

# Yol A çevirisinin başındaki 12 satırlık üst veri bloğunun biçimi
# (değerler sentetik).
_META = "\n".join(["% ==== KITAPCEVIR-META v1 ===="]
                  + [f"% alan{k}: deger" for k in range(9)]
                  + ["% ============================", ""])


class TestMetaBasligiSatirKaymasi:
    def test_leading_comment_lines(self):
        assert QCEngine._leading_comment_lines(_META.split("\n")
                                               + ["metin"]) == 12
        assert QCEngine._leading_comment_lines(["metin", "% not"]) == 0

    def test_kaynakta_ayni_yerdeki_dipnot_isareti_yanlis_alarm_degil(self):
        # Kaynakta 30. satırda "${}^{1}$" dipnot işareti; çeviride aynı
        # satır META başlığı yüzünden 12 satır aşağıda. Eskiden ±5 pencere
        # kaynak satırını ıskalıyor, MEDIUM "Standalone superscript" çıkıyordu.
        src_lines = [f"Satir {k} duz metin." for k in range(200)]
        tr_lines = [f"Satır {k} düz metin." for k in range(200)]
        src_lines[30] = "Il sistema oscilla.${}^{1}$ Per esempio"
        tr_lines[30] = "Sistem salınır.$^{1}$ Örneğin"
        tr = _META + "\n" + "\n".join(tr_lines) + "\n"
        rep = QCReport()
        QCEngine()._check_broken_inline_math(
            tr, tr.split("\n"), rep, en_lines=src_lines)
        assert _by_check(rep, "broken_inline_math") == []

    def test_kaynakta_olmayan_tek_basina_ust_simge_yakalanir(self):
        src_lines = [f"Satir {k} duz metin." for k in range(60)]
        tr_lines = [f"Satır {k} düz metin." for k in range(60)]
        tr_lines[30] = "Sistem salınır.$^{1}$ Örneğin"
        tr = _META + "\n" + "\n".join(tr_lines) + "\n"
        rep = QCReport()
        QCEngine()._check_broken_inline_math(
            tr, tr.split("\n"), rep, en_lines=src_lines)
        assert len(_by_check(rep, "broken_inline_math")) == 1

    def test_antonym_guard_meta_basligiyla_ayni_satiri_bulur(self):
        # Gerçek takas: kaynak 'laminar', çeviri 'türbülanslı' — META
        # başlığı olmadan da olsa da aynı satırda yakalanmalı.
        src_lines = [f"Line {k} plain text." for k in range(40)]
        tr_lines = [f"Satır {k} düz metin." for k in range(40)]
        src_lines[20] = "The flow near the wall is laminar."
        tr_lines[20] = "Çepere yakın akış türbülanslıdır."
        src = "\n".join(src_lines) + "\n"
        for tr in ("\n".join(tr_lines) + "\n",
                   _META + "\n" + "\n".join(tr_lines) + "\n"):
            rep = QCReport()
            QCEngine()._check_antonym_guard(
                tr, src, tr.split("\n"), src.split("\n"), rep)
            assert len(_by_check(rep, "antonym_guard")) == 1, tr[:40]


# ---------------------------------------------------------------------------
# Vq (2026-09-28) — "Fragmented inline math" '$' okuması TeX gibi
# ---------------------------------------------------------------------------

class TestParcaliMatematikDisplayOkumasi:
    """Ardışık iki '$...$' parçası ancak TeX'in okumasında (metin kipinde
    '$$' display açar) gerçekten satır içi matematikse sayılır. Eskiden
    display dipnot işaretinden sonra gelen düzyazı ('$$...$$ metin $x$')
    ikinci inline parça sanılıp sahte HIGH veriliyordu (dil_sent_de)."""

    def _frag(self, tr_line: str):
        rep = QCReport()
        QCEngine()._check_broken_inline_math(
            tr_line + "\n", [tr_line], rep, en_lines=None)
        return [i for i in _by_check(rep, "broken_inline_math")
                if i.message.startswith("Fragmented")]

    def test_display_isaretten_sonra_duzyazi_parca_sayilmaz(self):
        line = ("$${}^{^1}$$ Kaynak yazında bu nicelik çoğunlukla k ile "
                "gösterilir; burada $\\lambda$ kullanılır.")
        assert self._frag(line) == []

    def test_gercek_bitisik_parca_hala_yakalanir(self):
        assert len(self._frag("Değer $x^{2}$ $_{i}$ olur.")) == 1

    def test_display_sonrasindaki_gercek_parca_yakalanir(self):
        line = "$$a=b$$ ve ardından $y$ $^{2}$ gelir."
        assert len(self._frag(line)) == 1

    def test_bitisik_sarmalama_eskisi_gibi(self):
        # '$x$$\theta$' iki inline atomdur (ortadaki '$$' display değil).
        assert len(self._frag("Oran $x$$^{3}$ verilir.")) == 1


class TestParcaliMatematikSanitizerParitesi:
    """Vq (2026-09-28): çeviri sanitize edilmiş gövdedir; kaynakta aynen
    duran metin kipi Yunan harfi/simgesi ('\\textit{δ ≈}') çeviride
    '$\\delta$ $\\approx$' olur. Kaynak satırın sarmalamalı biçiminde aynı
    parçalar varsa MT kaynaklı parçalanma değildir (dil_sent_it HIGH x3)."""

    def _frag(self, tr_line: str, src_line: str):
        rep = QCReport()
        QCEngine()._check_broken_inline_math(
            tr_line + "\n", [tr_line], rep, en_lines=[src_line])
        return [i for i in _by_check(rep, "broken_inline_math")
                if i.message.startswith("Fragmented")]

    def test_kaynakta_ayni_metin_kipi_simgesi_alarm_degil(self):
        # Dönüştürücü '≈'yi zaten '$\approx$' yazar; metin kipindeki 'δ'yı
        # yalnız sanitizer sarar (kaynak gövdede çıplak kalır).
        src = "Oran 1,5 ise \\textit{δ $\\approx$} 0,405 bulunur."
        tr = "Oran 1,5 ise \\textit{$\\delta$ $\\approx$} 0,405 bulunur."
        assert self._frag(tr, src) == []

    def test_kaynakta_olmayan_parcalanma_yakalanir(self):
        # Kaynakta tek ifade, çeviride MT'nin böldüğü üst simge.
        src = "Değer $x^{2}_{i}$ olarak verilir."
        tr = "Değer $x^{2}$ $_{i}$ olarak verilir."
        assert len(self._frag(tr, src)) == 1

    def test_kaynakta_baska_simge_varken_yakalanir(self):
        # Kaynakta 'ζ' var ama 'δ ≈' yok: sarmalama paritesi eşleşmez.
        src = "Sönüm oranı \\textit{ζ} küçüktür."
        tr = "Sönüm oranı \\textit{$\\delta$ $\\approx$} küçüktür."
        assert len(self._frag(tr, src)) == 1


class TestDilTemizligiBitisikAtom:
    """Vq (2026-09-28): dil ölçümü temizleyicisinin '$$.*?$$' deseni bitişik
    iki satır içi atomun ortasındaki '$$'yi ('$\\alpha$${}^{0}$') display
    açılışı sanıp bir sonraki '$$'ye kadar bütün satırları boşaltıyordu;
    aradaki çevrilmemiş Almanca paragraf yakalanmıyordu (sessiz kaçak)."""

    _DE = ("Die Wärme fließt durch die Wand, und der Temperaturunterschied "
           "zwischen den beiden Oberflächen bestimmt die Größe des "
           "Wärmestroms, der für die Auslegung maßgeblich ist. Für eine "
           "dünne Schicht mit großer Fläche gilt diese Näherung besonders "
           "gut, weil die Ränder kaum eine Rolle spielen.")

    def _tr(self, adjacent: bool) -> str:
        ek1 = " Oran $\\alpha$${}^{0}$ olur." if adjacent else ""
        ek2 = " Ayrıca $x$${}^{2}$ verilir." if adjacent else ""
        paras = [_TR] * 6 + [_TR + ek1, self._DE, _TR + ek2] + [_TR] * 6
        return "\n\n".join(paras) + "\n"

    def _src(self) -> str:
        return "\n\n".join([self._DE] * 15) + "\n"

    def _untranslated(self, tr: str):
        rep = _analyze(tr, self._src())
        return [i for i in rep.issues if i.check_name == "untranslated"]

    def test_bitisik_atom_cevrilmemis_paragrafi_gizlemez(self):
        assert len(self._untranslated(self._tr(adjacent=False))) == 1
        found = self._untranslated(self._tr(adjacent=True))
        assert len(found) == 1 and "German" in found[0].message

    def test_temizleyici_satir_hizasini_korur(self):
        from latex_pipeline.qc_engine import _clean_for_language
        text = "a $\\alpha$${}^{0}$ b\n\nWärme fließt hier\n\nc $$x$$ d $y$\n"
        out = _clean_for_language(text)
        assert out.count("\n") == text.count("\n")
        assert "Wärme fließt hier" in out.split("\n")[2]
        assert "$" not in out
