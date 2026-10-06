"""Kulvar G4 (2026-09-30): okuma ajanlarının bulduğu, QC'nin görmediği hatalar.

Kanıtlar (okuma bulguları, 2026-09-30):
  * model çıktılarının okuması: MT-1 parçalı yol, MT-7
    "Bi < ≤ 0 , ≤ 1", MT-9 Almanca uydurma cümle, MT-16 "Rehfeld & Söhne",
    MT-17 ``\\caption*{\\textbf{Şekil} 1.1:}`` + dışarıda metin, MT-18 "Cam**";
    gpt-oss çıktısında ``\\caption*`` düşmüş.
  * elle onarılan bir raporun okuması: gövdede dipnot
    tanımı paragrafı, dipnota yapışmış gövde, düz rakam dipnot işareti, metne
    sızmış sayfa numarası, görünen ``<u>…</u>``, altyazıya yapışmış gövde.

Her denetim iki yönden sınanır: okuma bulgusundaki GERÇEK satırı yakalıyor VE
temiz çeviride (aynı satırın düzgün hâli ya da meşru benzer yapı) alarm
vermiyor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from latex_pipeline.qc_engine import (
    QCEngine,
    anchor_follow,
    fragment_anchors,
    paren_balance,
    rel_normalize,
)

ROOT = Path(__file__).resolve().parent.parent


def _analyze(tr: str, src: str = "", manual: bool = False):
    return QCEngine().analyze(tr, src, manual_translation=manual)


def _by_check(report, name: str):
    return [i for i in report.issues if i.check_name == name]


def _doc(*paras: str) -> str:
    """Paragrafları boş satırla birleştir (gerçek gövde düzeni)."""
    return "\n\n".join(paras) + "\n"


# ---------------------------------------------------------------------------
# 1) Parçalı yol (MT-1)
# ---------------------------------------------------------------------------

# Yapı: bir model çıktısındaki satır — aynı çapalar
# (aynı sıra, aynı ardıl türü), aynı belirtiler (satır başı boşluk, çapadan
# sonra büyük harfli parça, "…’nin", satır sonunda "is"); metin sentetik
# (KARAR 26).
_FRAG_TR = (
    " Farklı kütlelere sahip iki cisimden kurulu bir düzeni ele alalım. "
    "$m_1$ ve $m_2$ , $m_1 > m_2$ Cisimler tam küre biçimindedir ve "
    "üzerlerinde kütleçekiminden başka hiçbir kuvvet yoktur. Bir eylemsiz "
    "çerçeve seçilsin. $\\mathcal{F}_i$ ; konum okları $\\vec{R}_1$ ile "
    "$\\vec{R}_2$ Kütlelerin yerlerini verir. $m_1$ ve $m_2$ "
    "Sırasıyla. …’nin yeri. $m_2$ …ye göre $m_1$ is")
_FRAG_EN = (
    "Take a pair of bodies with different masses  $m_1$  and  $m_2$ ,  "
    "$m_1 > m_2$ ; both bodies are perfect spheres and gravity is the only "
    "force acting on them. Pick an inertial reference  $\\mathcal{F}_i$ "
    "; position arrows  $\\vec{R}_1$  and  $\\vec{R}_2$  give the locations of "
    "masses  $m_1$  and  $m_2$ , respectively. The location of  $m_2$  "
    "with respect to  $m_1$  is")
# Aynı satırın düzgün çevirisi.
_FRAG_TR_CLEAN = (
    "Kütleleri $m_1$ ve $m_2$ olan ($m_1 > m_2$) iki cisimli bir düzeni ele "
    "alalım; iki cisim de tam küredir ve üzerlerinde kütleçekiminden başka "
    "kuvvet yoktur. Bir $\\mathcal{F}_i$ eylemsiz çerçevesi seçelim; "
    "$\\vec{R}_1$ ve $\\vec{R}_2$ konum okları sırasıyla $m_1$ ve $m_2$ "
    "kütlelerinin yerini gösterir. $m_2$'nin $m_1$'e göre yeri")

# Bir model çıktısından (sentetik Almanca metnin çevirisi; birebir).
_FRAG_DE_TR = (
    " Tahmini hesaplamalar için genellikle o malzemenin oda sıcaklığındaki "
    "değerinin kullanılması yeterlidir. Çelik malzeme söz konusu olduğunda "
    "ise bu değer daha da düşer. $\\lambda$ 20 ile arasında \\textit{◦} C ve "
    "600 \\textit{◦}Ancak C değeri oldukça belirgindir; bu nedenle doğru "
    "hesaplamalar yapılırken sıcaklığa bağlı bu değişikliklerin dikkate "
    "alınması gerekir.")
_FRAG_DE_EN = (
    "Für Überschlagsrechnungen genügt es meist, den Wert bei Raumtemperatur zu "
    "verwenden. Bei Stahl sinkt \\textit{λ} zwischen 20 \\textit{◦}C und 600 "
    "\\textit{◦}C allerdings merklich, sodass genaue Rechnungen die "
    "Temperaturabhängigkeit berücksichtigen müssen.")


class TestParcaliYol:
    def test_mt1_gercek_satir_yakalanir(self):
        rep = _analyze(_doc("Giriş paragrafı.", _FRAG_TR),
                       _doc("Intro paragraph.", _FRAG_EN))
        hits = _by_check(rep, "fragmented_line")
        assert len(hits) == 1
        h = hits[0]
        assert h.severity == "high" and h.line_num == 3
        # Belirtiler iletide: büyük harfle başlayan parçalar, "…’nin",
        # satır sonunda çevrilmemiş "is".
        assert "Sırasıyla" in h.message and "Kütlelerin" in h.message
        assert "…’nin" in h.message
        assert "'is'" in h.message

    def test_mt1_almanca_kaynak(self):
        rep = _analyze(_doc("Bir.", _FRAG_DE_TR), _doc("Eins.", _FRAG_DE_EN))
        hits = _by_check(rep, "fragmented_line")
        assert len(hits) == 1 and "Ancak" in hits[0].message

    def test_temiz_ceviri_alarm_yok(self):
        rep = _analyze(_doc("Giriş paragrafı.", _FRAG_TR_CLEAN),
                       _doc("Intro paragraph.", _FRAG_EN))
        assert _by_check(rep, "fragmented_line") == []

    def test_formulden_sonra_cumle_basi_kaynakta_da_varsa_belirti_degil(self):
        # Kaynakta da formülden sonra yeni cümle büyük harfle başlıyor.
        tr = ("Kütle $M$ sabittir. Hız $v$ Sabit değildir; ivme $a$ Değişir.")
        en = ("The mass $M$ is constant. The speed $v$ Varies; the "
              "acceleration $a$ Changes.")
        rep = _analyze(_doc(tr), _doc(en))
        assert _by_check(rep, "fragmented_line") == []

    def test_ozel_ad_ve_etiket_kelimesi_belirti_degil(self):
        # "Güneş" belgede hiç küçük harfle geçmiyor (özel ad), "Şekil" etiket.
        tr = ("Bu değer $\\mu$ Güneş için en büyüktür; eğri $r(t)$ Şekil 2.1'de "
              "görülür ve Güneş sisteminde benzeri yoktur.")
        en = ("This value $\\mu$ for the Sun is the largest; the curve $r(t)$ "
              "in Fig. 2.1 has no counterpart in the solar system.")
        rep = _analyze(_doc(tr), _doc(en))
        assert _by_check(rep, "fragmented_line") == []

    def test_kaynak_yoksa_calismaz(self):
        assert _by_check(_analyze(_doc(_FRAG_TR)), "fragmented_line") == []

    def test_capa_ve_ardi(self):
        line = "$m_1$ ve $m_2$ Sırasıyla. (1.2) sonra"
        keys = [k for _s, _e, k in fragment_anchors(line)]
        assert keys == ["m:m_1", "m:m_2", "r:(1.2)"]
        end = fragment_anchors(line)[1][1]
        assert anchor_follow(line, end) == ("cap", "Sırasıyla", False)
        # Yalnız işleç olan matematik çapa değildir.
        assert fragment_anchors("WTP $\\leftarrow$ Affect") == []

    def test_ayrac_dengesi_matematigi_saymaz(self):
        assert paren_balance("$f(x$ ve (a) [b]") == (0, 0)
        assert paren_balance("değer ( $R_E$ Dünya") == (1, 0)


# ---------------------------------------------------------------------------
# 2) Sayı bozulması (MT-7)
# ---------------------------------------------------------------------------

# Bir model çıktısından (sentetik Almanca metnin çevirisi; birebir).
_BI_TR = ("Eğer Bi \\textit{<} $\\leq$ 0 \\textit{,} $\\leq$ 1 ise, vücuttaki "
          "sıcaklığın mekansal olarak sabit olduğu söylenebilir. Bu durumda "
          "enerji dengesi şu şekilde olur:")
_BI_EN = ("Ist Bi \\textit{<} 0\\textit{,}1, so darf man die Temperatur im "
          "Körper als räumlich konstant ansehen. Die Energiebilanz lautet dann")
_BI_TR_CLEAN = ("Bi \\textit{<} 0\\textit{,}1 ise cisimdeki sıcaklık "
                "uzayda sabit sayılabilir. Enerji dengesi bu durumda şöyledir:")


class TestSayiBozulmasi:
    def test_mt7_gercek_satir_yakalanir(self):
        rep = _analyze(_doc("Giriş.", _BI_TR), _doc("Einleitung.", _BI_EN))
        hits = _by_check(rep, "number_corruption")
        assert len(hits) == 1
        h = hits[0]
        assert h.severity == "high" and h.line_num == 3
        assert "'< ≤'" in h.message
        assert "bölünmüş ondalık" in h.message
        assert "'< 0,1'" in h.message      # kaynaktaki karşılaştırma kayıp

    def test_temiz_ceviri_alarm_yok(self):
        rep = _analyze(_doc("Giriş.", _BI_TR_CLEAN), _doc("Einleitung.", _BI_EN))
        assert _by_check(rep, "number_corruption") == []

    def test_mesru_siralama_ve_esitsizlik(self):
        tr = ("Bölüm 1, 2 ve 3 bu konuyu işler. Burada $0 \\leq x \\leq 1$ ve "
              "$0 < t < 1$ alınır; adım 0,1 s seçildi.")
        en = ("Chapters 1, 2 and 3 cover this. Here $0 \\leq x \\leq 1$ and "
              "$0 < t < 1$; the step is 0.1 s.")
        rep = _analyze(_doc(tr), _doc(en))
        assert _by_check(rep, "number_corruption") == []

    def test_kaynakta_da_ayni_bicim_varsa_bulgu_yok(self):
        # Kaynak (Marker) zaten "0 , 1" diye bölmüşse çeviri hatası değildir.
        tr = "Oran 0 , 1 civarındadır."
        en = "The ratio is about 0 , 1 here, i.e. 0.1."
        rep = _analyze(_doc(tr), _doc(en))
        assert _by_check(rep, "number_corruption") == []

    def test_normalize(self):
        assert rel_normalize("Bi \\textit{<} $\\leq$ 0") == "Bi < ≤ 0"


# ---------------------------------------------------------------------------
# 3) Altyazı ve dipnot iskeleti (MT-17, gpt-oss)
# ---------------------------------------------------------------------------

def _figure(caption_line: str, graphic: str = "_page_3_Figure_2.png") -> str:
    return ("\\begin{figure}[H]\n  \\centering\n"
            f"  \\includegraphics[width=0.8\\textwidth]{{{graphic}}}\n"
            f"  {caption_line}\n\\end{{figure}}")


_CAP_SRC = ("\\caption*{\\textbf{Abbildung 1.1:} Temperaturverlauf in einer "
            "ebenen Wand bei stationärer Wärmeleitung}")
# Bir model çıktısından (sentetik Almanca sınama metninin çevirisi; birebir).
_CAP_MT17 = ("\\caption*{\\textbf{Şekil} 1.1:} Durağan ısı iletimi durumunda "
             "düz bir duvardaki sıcaklık değişimi")
_CAP_CLEAN = ("\\caption*{\\textbf{Şekil 1.1:} Düz bir duvarda kararlı ısı "
              "iletiminde sıcaklık dağılımı}")
# Başka bir model çıktısında \caption* düşmüş, metin float içinde.
_CAP_GPTOSS = "Bir düz duvarın sabit ısı iletiminde sıcaklık dağılımı"

# Yapı: elle onarım öncesi bir çeviri satırı — etiketli \textit grubu (51
# sözcük, nokta ile biter) + yapışık gövde paragrafı (51 sözcük; 533, iki
# \textsuperscript, kıvrık tırnak, \href); metin sentetik (KARAR 26).
_CAP_YAPISIK = (
    "\\caption*{\\textit{Şekil 8: Her çubuk bir fidenin serada kaldığı günleri "
    "gösterir; renkler, fidenin ilk ne zaman sulandığını, ilk yaprağını ne "
    "zaman açtığını ve gübre deneyine tam olarak hangi hafta alındığını belirtir. Deney "
    "ilerleyip ışık süresi uzadıkça gelişme belirginleşir ve bu dönemde seraya "
    "giren genç fideler pencereden gelen ışığa çok daha hızlı biçimde yönelir.} Bu "
    "dönemde serada bulunan 533 fidenin \\textsuperscript{30} yüzde 90'ından "
    "fazlası ışığa oldukça hızla yöneldi. \\textsuperscript{31} Bakıcılar bu "
    "yönelimin beklenen, olağan ve zararsız bir bitki tepkisi olduğunu baştan biliyordu, ancak "
    "ölçümleri düzenli tutmanın deneyin sonucunu güçlendireceğini düşündükleri "
    "ve “komşu” sıralardaki bitkileri de izlemeye istekli oldukları için "
    "her sabah kayıt tuttular (bakım \\href{#page-56-0}{günlüğü} üzerine bölümümüze bakınız).}")


class TestAltyaziIskeleti:
    def test_mt17_ayrac_disinda_metin(self):
        rep = _analyze(_doc("Metin.", _figure(_CAP_MT17)),
                       _doc("Text.", _figure(_CAP_SRC)))
        hits = _by_check(rep, "caption_structure")
        assert len(hits) == 1
        assert hits[0].severity == "medium"
        assert "ayracının dışında" in hits[0].message
        assert hits[0].line_num == 6

    def test_gptoss_altyazi_kaybi(self):
        rep = _analyze(_doc("Metin.", _figure(_CAP_GPTOSS)),
                       _doc("Text.", _figure(_CAP_SRC)))
        hits = _by_check(rep, "caption_structure")
        assert len(hits) == 1          # başıboş metin ikinci kez sayılmaz
        assert hits[0].severity == "high"
        assert "Altyazı kaybı" in hits[0].message

    def test_temiz_altyazi_alarm_yok(self):
        rep = _analyze(_doc("Metin.", _figure(_CAP_CLEAN)),
                       _doc("Text.", _figure(_CAP_SRC)))
        assert _by_check(rep, "caption_structure") == []

    def test_altyaziya_yapismis_govde(self):
        rep = _analyze(_doc("Metin.", _figure(_CAP_YAPISIK)), "", manual=True)
        hits = _by_check(rep, "caption_structure")
        assert len(hits) == 1
        assert hits[0].severity == "medium"
        assert "Altyazıya gövde paragrafı yapışmış" in hits[0].message

    def test_kalin_baslik_ve_aciklama_mesru(self):
        # "Başlık + açıklama" biçimli meşru uzun altyazı.
        cap = ("\\caption{\\textbf{Şekil 2: Sıcaklık dağılımı.} Düz duvarda "
               "sıcaklık, iç yüzeyden dış yüzeye doğrusal olarak azalır; eğim "
               "ısı iletkenliğiyle ters orantılıdır ve duvar kalınlığı arttıkça "
               "ısı akısı azalır. Kesikli çizgi iki katmanlı duvarı gösterir.}")
        rep = _analyze(_doc("Metin.", _figure(cap)), "")
        assert _by_check(rep, "caption_structure") == []

    def test_dipnot_kaybi_ve_cevirmen_notu(self):
        en = _doc("One.\\footnote{A.} Two.\\footnote{B.}")
        rep = _analyze(_doc("Bir.\\footnote{A.} İki."), en)
        hits = _by_check(rep, "footnote_count")
        assert len(hits) == 1 and hits[0].severity == "high"
        # Eşit sayı → bulgu yok.
        rep = _analyze(_doc("Bir.\\footnote{A.} İki.\\footnote{B.}"), en)
        assert _by_check(rep, "footnote_count") == []


# ---------------------------------------------------------------------------
# 4) Kaynak dilde cümle (MT-9)
# ---------------------------------------------------------------------------

# Bir model çıktısından (sentetik Almanca metnin çevirisi; birebir).
_DE_TR = ("In \\textit{a} wird die Wärmeleitfähigkeit behandelt. Bu problemin "
          "çözümü genellikle zaman alıcıdır; ancak neyse ki, bu hesaplamaların "
          "gerekmediği önemli bir istisna durumu vardır.")
_DE_EN = ("in der \\textit{a} die Temperaturleitfähigkeit ist. Ihre Lösung ist "
          "oft mühsam; glücklicherweise gibt es einen wichtigen Sonderfall, der "
          "ohne sie auskommt.")
# Kaynak belgenin geri kalanı Almanca olsun (baskın dil = de; dil tespiti
# en az 1000 harf / 150 kelime ister): sentetik Almanca sınama metninin dört
# paragrafı ve bir model çevirisi (birebir).
_DE_BG = (
    "Wärme ist Energie, die allein aufgrund eines Temperaturunterschieds von "
    "einem Ort zu einem anderen übertragen wird. In der Technik unterscheidet "
    "man drei Mechanismen: Leitung, Konvektion und Strahlung. Dieses Kapitel "
    "behandelt ausschließlich die Wärmeleitung, also den Energietransport in "
    "ruhender Materie, bei dem sich die Teilchen selbst nicht makroskopisch "
    "bewegen. Wer die Leitung verstanden hat, besitzt das Werkzeug, um "
    "Dämmschichten zu bemessen, Kühlkörper auszulegen oder die Erwärmung "
    "eines Bauteils abzuschätzen.\n\n"
    "Der grundlegende Zusammenhang zwischen dem Wärmestrom und dem "
    "Temperaturfeld ist nach Joseph Fourier benannt. Für einen "
    "eindimensionalen Fall, in dem sich die Temperatur nur längs der "
    "Koordinate x ändert, lautet er\n\n"
    "Gl. (1.3) legt eine Analogie zum Ohm'schen Gesetz nahe. Setzt man die "
    "Temperaturdifferenz an die Stelle der Spannung und den Wärmestrom an die "
    "Stelle des elektrischen Stroms, so erhält man den thermischen "
    "Widerstand\n\n"
    "Die Wärmeleitfähigkeit ist eine Stoffeigenschaft, die man Tabellen "
    "entnimmt. Tab. 1.1 zeigt, dass die Werte über mehr als vier "
    "Zehnerpotenzen streuen. Metalle leiten Wärme gut, weil freie Elektronen "
    "am Transport beteiligt sind; poröse Dämmstoffe verdanken ihre Wirkung "
    "dagegen der eingeschlossenen Luft, die nicht strömen kann.")
_TR_BG = (
    "Isı, yalnızca sıcaklık farkı nedeniyle bir yerden diğerine aktarılan bir "
    "enerjidir. Teknik alanda üç farklı mekanizma ayırt edilir: iletim, "
    "konveksiyon ve radyasyon. Bu bölüm yalnızca ısı iletimini ele "
    "almaktadır; yani, parçacıkların makroskopik ölçekte hareket etmediği "
    "durumlarda gerçekleşen enerji transferini incelemektedir. Isı iletimini "
    "anlayan bir kişi, yalıtım katmanlarının ölçülmesi, soğutucu elemanların "
    "yerleştirilmesi veya bir parçanın ısınma derecesinin tahmin edilmesi "
    "gibi konularda gerekli bilgiye sahip olur.\n\n"
    "Joseph Fourier’a göre, ısı akısı ile sıcaklık alanı arasındaki temel "
    "ilişki böyle adlandırılmıştır. Sıcaklığın yalnızca x koordinatı boyunca "
    "değiştiği tek boyutlu bir durum için bu ilişki şu şekildedir\n\n"
    "Gl. (1.3) bir analoji önermektedir; Ohm yasasına benzer bir ilişki "
    "kurulabilir. Eğer sıcaklık farkını gerilimin yerine ve ısı akımını da "
    "elektrik akımının yerine koyarsak, termal direnç elde ederiz.\n\n"
    "Isı iletkenliği, tablolardan elde edilen bir madde özelliğidir. Tablo "
    "1.1, bu değerlerin dört onluk kuvvetten fazla bir aralıkta değiştiğini "
    "göstermektedir. Metaller, serbest elektronların ısı iletimine katılımı "
    "nedeniyle iyi ısı iletkenidir; öte yandan gözenekli yalıtım "
    "malzemelerinin etkisi, hareket edemeyen hava moleküllerinden "
    "kaynaklanır.")


class TestKaynakDilindeCumle:
    def test_mt9_uydurma_almanca_cumle(self):
        rep = _analyze(_doc(_TR_BG, _DE_TR), _doc(_DE_BG, _DE_EN))
        hits = _by_check(rep, "source_language_sentence")
        assert len(hits) == 1
        h = hits[0]
        assert h.severity == "high" and h.line_num == 9
        assert "Almanca" in h.message and "uydurmuş" in h.message

    def test_eski_dedektorler_neden_kacirdi(self):
        # Check 19 bu satırda yalnız "wird"i görür ("die" beyaz listede) ve
        # belge başına 2 isabet ister; Check 2 Türkçe harfli satırı
        # sınıflandırmaz. Yeni denetim olmadan satır için bulgu yoktu.
        rep = _analyze(_doc(_TR_BG, _DE_TR), _doc(_DE_BG, _DE_EN))
        others = [i for i in rep.issues if i.line_num == 9
                  and i.check_name in ("untranslated",
                                       "foreign_language_leakage")]
        assert others == []   # aynı bulgu iki kez sayılmaz

    def test_kaynakta_birebir_cumle_cevrilmemis_sayilir(self):
        de = ("Die Temperatur im Körper ist dann räumlich konstant und hängt "
              "nur von der Zeit ab.")
        tr = de + " Bu varsayım hesabı çok kolaylaştırır."
        rep = _analyze(_doc(_TR_BG, tr), _doc(_DE_BG, de))
        hits = _by_check(rep, "source_language_sentence")
        assert len(hits) == 1 and "kaynakta birebir var" in hits[0].message

    def test_temiz_turkce_ve_terim_alarm_yok(self):
        tr = ("Bu bölümde ısıl yayınım (Almanca: \\textit{Temperaturleitfähigkeit}) "
              "ele alınır; $a$ ile gösterilir ve birimi m²/s'dir.")
        rep = _analyze(_doc(_TR_BG, tr), _doc(_DE_BG, _DE_EN))
        assert _by_check(rep, "source_language_sentence") == []

    def test_ingilizce_kaynakta_calismaz(self):
        # İngilizce kaynakta cümle düzeyi denetim Check 2'nin işidir.
        rep = _analyze(_doc(_TR_BG, _DE_TR),
                       _doc("Heat conduction is the transport of energy in a "
                            "body caused by temperature differences.",
                            "where a is the thermal diffusivity."))
        assert _by_check(rep, "source_language_sentence") == []


# ---------------------------------------------------------------------------
# 5) Boru hattı artıkları (MT-16, MT-18)
# ---------------------------------------------------------------------------

# Yapı: elle onarım öncesi bir çeviride dipnot 3 tanımı ve ona boş
# satırsız yapışan gövde paragrafı; aşağıdaki kalıpların metni sentetik
# (KARAR 26), biçim öğeleri (\\textsuperscript, \\href, ~400, <u>…</u>,
# sondaki "60") aynı.
_FN3_YAPISIK = ("\\textsuperscript{3} Öte yandan, \\href{#page-20-0}{ileride} "
           "değineceğimiz üzere, ölçümleri yapmak için belediyeden ücretsiz alan "
           "izni aldık (bu da yerel kuralların bir gereğidir). Çalışma "
           "boyunca yaklaşık \\textasciitilde{}400 saatlik saha emeği "
           "harcadığımızı düşünüyoruz.")
_GOVDE_YAPISIK = ("Bu rapor dört kısımdan oluşuyor. Önce, saha ölçümlerini "
              "bitirdikten sonra kıyı aşınmasına ilişkin ulaştığımız ana "
              "\\href{#page-4-0}{sonuçları} özetliyoruz:")
# Düz rakam dipnot işareti "60".
_DUZ_RAKAM_MADDE = ("  \\item \\textbf{Model-7 Beta:} Bu sürüm yalnız deneme amaçlı bir "
             "sürüm değildi (genel kullanıma açık biçimde yayımlanmış durumda); "
             "ancak güvenlik sınamaları için içerik süzgeçleri bilerek "
             "devre dışıydı. 60")
_FN60 = ("\\textsuperscript{60} Deneme sürümleri, kısıtlama "
            "uygulanmadan sınanmak üzere hazırlanmış sürümlerdir.")
# Görünen <u>…</u>.
_GORUNEN_U = ("\\textsuperscript{76} Genel bir not olarak, bu kayıtların "
         "bir kısmının son sayfaları kayıptı (<u>önceki</u> kısımda "
         "belirttiğimiz gibi).")


class TestBoruHattiArtiklari:
    def test_dipnot_tanimi_govdede_ve_yapisan_govde(self):
        tr = ("Giriş paragrafı.\n\n" + _FN3_YAPISIK + "\n" + _GOVDE_YAPISIK + "\n")
        rep = _analyze(tr, "", manual=True)
        defs = _by_check(rep, "footnote_def_in_body")
        assert len(defs) == 1 and defs[0].line_num == 3
        merge = _by_check(rep, "footnote_body_merge")
        assert len(merge) == 1 and merge[0].severity == "medium"
        assert merge[0].line_num == 3

    def test_bos_satirla_ayrilmis_dipnot_yapisma_degil(self):
        tr = _doc("Giriş paragrafı.", _FN3_YAPISIK, _GOVDE_YAPISIK)
        rep = _analyze(tr, "", manual=True)
        assert _by_check(rep, "footnote_body_merge") == []

    def test_duz_rakam_dipnot_isareti(self):
        tr = ("\\begin{itemize}\n" + _DUZ_RAKAM_MADDE + "\n\\end{itemize}\n\n"
              + _FN60 + "\n")
        rep = _analyze(tr, "", manual=True)
        hits = _by_check(rep, "plain_footnote_marker")
        assert len(hits) == 1 and hits[0].line_num == 2

    def test_dipnot_tanimi_yoksa_sondaki_sayi_isaret_sayilmaz(self):
        tr = _doc("Toplam katılımcı sayısı: 60")
        assert _by_check(_analyze(tr), "plain_footnote_marker") == []

    def test_sizmis_sayfa_numarasi(self):
        # Dipnot 21, boş satır, "10", \subsection.
        tr = ("\\textsuperscript{21} Gözlemciler zaman zaman yeni gelen "
              "gönüllüleri uyardı.\n\n10\n\\subsection{Ölçüm düzenini "
              "aksatmaya yönelik ortak girişimler}\n")
        rep = _analyze(tr, "", manual=True)
        hits = _by_check(rep, "page_number_leak")
        assert len(hits) == 1 and hits[0].line_num == 3
        assert "'10'" in hits[0].message

    def test_tablo_ve_paragraf_ici_sayi_sayfa_numarasi_degil(self):
        tr = ("\\begin{tabular}{l}\n2024\n\\end{tabular}\n\n"
              "Yıl şuydu:\n2024\nve sonrası.\n")
        assert _by_check(_analyze(tr), "page_number_leak") == []

    def test_gorunen_html_etiketi(self):
        rep = _analyze(_doc(_GORUNEN_U), "", manual=True)
        hits = _by_check(rep, "visible_html_tag")
        assert len(hits) == 1 and "<u>" in hits[0].message

    def test_kod_icinde_html_mesru(self):
        tr = _doc("Etiket \\texttt{<u>metin</u>} biçimindedir; "
                  "\\verb|<b>| da kullanılır.")
        assert _by_check(_analyze(tr), "visible_html_tag") == []

    def test_markdown_artigi_mt18(self):
        # Bir model çıktısından (sentetik Almanca metnin çevirisi; birebir), tablo içinde.
        tr = ("\\begin{tabular}{l l l}\nCam** & 2500 & 0,76 \\\\\n"
              "\\end{tabular}\n")
        hits = _by_check(_analyze(tr), "markdown_remnant")
        assert len(hits) == 1 and hits[0].line_num == 2

    def test_kaciissiz_ampersand_mt16(self):
        # Bir model çıktısından (sentetik Almanca metnin çevirisi; birebir).
        tr = ("\\begin{enumerate}\n  \\item [2] M. Olbrich, P. Seidl: "
              "\\textit{Katı cisimlerde ısı iletimi}. Berlin: Rehfeld & Söhne, "
              "2015.\n\\end{enumerate}\n")
        en = ("\\begin{enumerate}\n  \\item [2] M. Olbrich, P. Seidl: "
              "\\textit{Wärmeleitung in Festkörpern}. Berlin: Rehfeld \\& "
              "Söhne, 2015.\n\\end{enumerate}\n")
        hits = _by_check(_analyze(tr, en), "unescaped_ampersand")
        assert len(hits) == 1
        assert hits[0].severity == "high" and hits[0].line_num == 2
        assert "kaynakta '\\&' kaçışlı" in hits[0].message

    def test_hizalama_ortaminda_ve_kacisli_ampersand_mesru(self):
        tr = ("Rehfeld \\& Söhne yayınevi.\n\n\\begin{tabular}{ll}\na & b \\\\\n"
              "\\end{tabular}\n\n\\begin{align*}\nx &= 1\n\\end{align*}\n\n"
              "Bağlantı \\href{https://x.org/?a=1&b=2}{burada}.\n")
        assert _by_check(_analyze(tr), "unescaped_ampersand") == []


# ---------------------------------------------------------------------------
# 7) Derlem ölçümünde görülen yanlış alarmlar ve ciddiyet ayarları
#    (iç derlem ölçümü)
# ---------------------------------------------------------------------------

# Sentetik İspanyolca sınama metninden (birebir; bizim metnimiz).
_ES_BG = (
    "Un fluido es una sustancia que se deforma de manera continua cuando se "
    "le aplica un esfuerzo cortante, por pequeño que sea. Cuando el fluido "
    "está en reposo no existen tales esfuerzos y la única acción que "
    "transmite es la presión. Este capítulo estudia cómo varía la presión "
    "dentro de un líquido en equilibrio y qué fuerzas ejerce sobre las "
    "paredes y sobre los cuerpos sumergidos. ¿Por qué una presa es mucho más "
    "gruesa en la base que en la coronación? La respuesta se encuentra en las "
    "pocas ecuaciones que siguen.\n\n"
    "y se mide en pascales, con 1 Pa = $1 \\text{ N/m}^2$ . En un fluido en "
    "reposo la presión en un punto es la misma en todas las direcciones; es, "
    "por tanto, una magnitud escalar. En la práctica técnica se usan también "
    "el bar, con $1 \\text{ bar} = 10^5 \\text{ Pa}$ , y la atmósfera normal, "
    "que equivale a $101\\,325 \\text{ Pa}$ .\n\n"
    "La ec. (1.3) es la base de los manómetros de columna. En un tubo en U "
    "que contiene un líquido manométrico de densidad $\\rho_m$ , una "
    "diferencia de niveles $\\Delta h$ indica una diferencia de presiones "
    "$\\Delta p = \\rho_m g \\, \\Delta h$ . Con mercurio, una columna de 76 "
    "cm corresponde aproximadamente a la presión atmosférica normal. Conviene "
    "distinguir tres formas de expresar la presión:")


class TestDerlemYanlisAlarmlari:
    def test_ozgun_dilde_eser_adi_cumle_sayilmaz(self):
        tr = ("Bu konu, Cajal'ın \\textit{Observaciones sobre la enseñanza de "
              "la Medicina} adlı yapıtında ele alınır; yazar burada öğretim "
              "yöntemlerini tartışır.")
        rep = _analyze(_doc(_TR_BG, tr), _doc(_ES_BG, "Texto."))
        assert _by_check(rep, "source_language_sentence") == []

    def test_tamami_kaynak_dilde_satir_check2_isi(self):
        # Satırda Türkçe yok: çevrilmemiş satır Check 2'nin işi, burada
        # ikinci kez sayılmaz ("Türkçe satırda" iletisi yanlış olurdu).
        es = ("Cuando el fluido está en reposo no existen tales esfuerzos y la "
              "única acción que transmite es la presión.")
        rep = _analyze(_doc(_TR_BG, es), _doc(_ES_BG, es))
        assert _by_check(rep, "source_language_sentence") == []

    def test_llm_istem_bicimi_html_degil(self):
        # Llama istem biçimi (bir EPUB kitabın çevirisinde görüldü).
        tr = _doc("> <s>[INST] <<SYS>>", "> Sen yardımsever bir asistansın.")
        assert _by_check(_analyze(tr), "visible_html_tag") == []

    def test_gorsel_dosya_adinda_ampersand_mesru(self):
        # bugav/F1-cikarim s10_gorsel_adlari (birebir); hatasız derlendi.
        tr = _figure("\\caption{Görsel.}", graphic="fig&5.png").replace(
            "[width=0.8\\textwidth]", "[width=1.25in]")
        assert _by_check(_analyze(tr), "unescaped_ampersand") == []

    def test_ampersand_kaynakta_da_kacissiz(self):
        # Bir test kitabında bağlantı METNİNDE
        # kaçışsız '&' (kaynakta da aynı; Marker artığı) — gerçek hata.
        line = ("  \\item Hollis RT, Marsh KA (1971) Designing field and "
                "semi-controlled studies for practice. Hartley Brothers, "
                "Boston. \\href{https://www.example.edu/~ra/Hollis&Marsh-"
                "1968-Field&SemiControlledStudiesForPractice.pdf}{https://www.example"
                ".edu/~ra/Hollis&Marsh-1968-Field&}")
        tr = "\\begin{itemize}\n" + line + "\n\\end{itemize}\n"
        hits = _by_check(_analyze(tr, tr), "unescaped_ampersand")
        assert len(hits) == 1 and "kaynakta da aynen kaçışsız" in hits[0].message

    def test_kod_ciktisi_ve_yil_sayfa_numarasi_degil(self):
        # İngilizce bir test kitabında "Cevap:" listelemesinden sonra "38";
        # numaralı kod örneği; İspanyolca bir kitabın başlık sayfası "1923".
        tr = ("\\begin{lstlisting}\nCevap:\n\\end{lstlisting}\n\n38\n\n"
              "\\begin{lstlisting}\nörnek\n\\end{lstlisting}\n\n"
              "Örnekler şöyledir:\n\n1\n\n\\begin{lstlisting}\n{a: 1}\n"
              "\\end{lstlisting}\n\nMadrid\n\n1923\n\n\\chapter*{Önsöz}\n")
        assert _by_check(_analyze(tr), "page_number_leak") == []

    def test_acili_ayraci_dusmus_yer_tutucu(self):
        # Yapı: bir test kitabının çevirisi (künye sentetik): öksüz
        # “LATEX_1941”, bölünmüş "Cha p.", sayfa aralığı, ek almış italik ad.
        line = ("  \\item 6. Lindqvist, A. B. ve Moreau, P. J., “LATEX_1941”, AIAA, "
                "Boston, 1979, Cha p. 7, pp. 211–260. \\textit{Hybrid "
                "Motor Design’ın Temelleri}")
        tr = "\\begin{itemize}\n" + line + "\n\\end{itemize}\n"
        hits = [i for i in _by_check(_analyze(tr), "placeholder_orphans")
                if "LATEX_1941" in i.message]
        assert len(hits) == 1 and hits[0].severity == "critical"

    def test_dipnot_metne_gomulmus_medium(self):
        # Kamu malı Fransızca bir kitabın çevirisinde \footnote
        # satır içi "[* …]" notuna çevrilmiş (içerik yerinde).
        en = _doc("des cartes sélénographiques\\footnote{De σελἡνη , mot grec "
                  "qui signifie Lune.} avec une perfection")
        tr = _doc("onu aşan bir mükemmellikle selenografik haritalar[*] "
                  "çizilmiştir;", "[* Ay anlamına gelen Yunanca\nσελἡνη\n"
                  "sözcüğünden.]")
        hits = _by_check(_analyze(tr, en), "footnote_count")
        assert len(hits) == 1 and hits[0].severity == "medium"
        assert "gömülmüş" in hits[0].message

    def test_altyazi_float_disinda_etiketiyle_medium(self):
        # Sentetik Almanca sınama metninin çevirisinden (birebir): \caption
        # yok ama "\textbf{Şekil 1.1:} …" float'tan hemen sonra duruyor.
        tr = _doc(_figure("% altyazı yok"),
                  "\\textbf{Şekil 1.1:} Kararlı ısı iletiminde düzlem bir "
                  "duvardaki sıcaklık dağılımı")
        hits = _by_check(_analyze(tr, _doc(_figure(_CAP_SRC))),
                         "caption_structure")
        assert len(hits) == 1 and hits[0].severity == "medium"
        assert "Altyazı yapısı kayboldu" in hits[0].message

    def test_zayif_parcali_belirti_medium(self):
        # Yapı: bir test kitabının çevirisinden (metin
        # sentetik): yalnız iki büyük harfli parça, anlam yerinde → MEDIUM.
        tr = ("\\textit{q} Kilogram başına eklenen ısıdır. \\textit{s} Kilogram "
              "başına kazanılan entropidir. ")
        en = ("\\textit{q} is the heat added per kilogram \\textit{s} is the "
              "entropy gained per kilogram")
        hits = _by_check(_analyze(_doc(tr), _doc(en)), "fragmented_line")
        assert len(hits) == 1 and hits[0].severity == "medium"

    def test_kalin_uzun_baslik_ve_aciklama_mesru(self):
        # 10 kelimelik kalın başlık + uzun açıklama: meşru altyazı biçimi.
        cap = ("\\caption{\\textbf{Şekil 3: Mesaj panosunda etkin ajan sayısının "
               "saatlere göre değişimi ve dağılımı.} Her çubuk bir saatlik "
               "aralıktaki etkin ajan sayısını gösterir; renkler ajanın panoyla "
               "ilk karşılaştığı günü belirtir. Kesikli çizgi yedi günlük "
               "hareketli ortalamadır ve gri alan tesisteki gözlem dönemini "
               "gösterir. Veriler yalnız transkripti eksiksiz olan ajanları "
               "kapsar; eksik olanlar ayrı tabloda verilmiştir.}")
        assert _by_check(_analyze(_doc("Metin.", _figure(cap))),
                         "caption_structure") == []
