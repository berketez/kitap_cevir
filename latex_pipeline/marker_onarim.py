"""Marker çıktısında sayfa kırılımı onarımı (G1b, 2026-09-30; G1b-1
değerlendirme + tamamlama 2026-10-05, testler: tests/test_G1b_g.py; G1b-2
liste/tablo/denklem 2026-10-05, testler: tests/test_G1b2_g.py).

EPUB yolunda çağrılmaz (EPUB'da sayfa kırılımı yok).

Marker PDF'i sayfa sayfa okur; sayfa kırılımı Markdown'a paragraf sonu olarak
geçer. Bunun sonuçları (iç okuma bulguları, 2026-09-30):

1. Sayfa sonunda bölünmüş paragraf iki paragraf olur; araya sayfa altındaki
   dipnot tanımları, sayfa numarası ya da şekil girer (MT-12). Liste
   maddesi de böyle bölünür; devamı yeni bir madde olur.
2. Kaynak sayfa numarası tek başına satır olarak metne sızar ("10", "37",
   "73").
3. Sayfa kırılımıyla bölünmüş tablo iki tablo olur; ikinci tablonun "başlık
   satırı" aslında ilk tablonun devamıdır, satır ya da hücre ikiye bölünür.
4. Numaralı liste sayfa kırılımında (araya dipnot tanımları girince) iki
   listeye bölünür; ikinci parça elle numara taşıyan imli liste olarak
   kalır.
   Numaralı listenin imi de sayfa kırılımında değişebilir ("1." + "- 2.").
5. İç içe liste düzeyi Marker'da girintiyle verilir; bu modül girintiyi
   korur, sayfa kırılımında kaybolmuş harfli alt madde düzeyini ("  - c." +
   "- d.") ve etiketi ile içeriği ayrı maddeye düşmüş alt maddeyi
   ("- **ad:**" + "  - ^desen") onarır (G1b-2). Düzleştirme
   dönüştürücüdedir.
6. Madde iminin ardında fazladan kalın tire: "- **- Metin**".
7. Display denklemin hemen altına düşmüş matematik satırları (mt okuma
   MT-11: ";  $…$" çiftin ikinci yarısı; MT-6: hizalı bloğun satır içi
   matematik satırları) display'e geri alınır (G1b-2).
8. Sayfa altındaki dipnot tanımı bir sonraki sayfanın ilk bloğuna (görsel,
   başlık, madde, kod, gövde paragrafı) satır sonuyla yapışır; boş satırla
   ayrılır (FX1; V1 yeni hata 4).
9. Sayfa üst/alt bilgisi ("6 1 Introduction", "References 33") gövdeye
   sızar; kesin tanınan silinir ve sayılır (KARAR 32; FX1).

Sözleşme: ``onar(markdown) -> str`` ve ``onar_rapor(markdown) -> (str,
dict)`` (sözlük: değişiklik türü -> sayı, anahtarlar ``SAYAC_TURLERI``).
Doğrulama/günlük için ``onar_ayrintili(markdown) -> (str, dict, list)``
değişikliklerin listesini de verir. Çağrı yeri: ``converter.convert()``
içinde ``_preprocess_markdown``'dan SONRA, ``_extract_footnotes`` /
``_pair_page_footnotes``'tan ÖNCE. O noktada ``<sup>N</sup>`` işaretleri
üst simge nöbetçisine (``\\x02S<k>\\x02``) dönmüştür; modül iki biçimi de
tanır. Dipnot tanım paragrafları ayrı paragraf olarak kalır ve yalnız
birleşen bloğun ARDINA taşınır (sıraları korunur); böylece
``_pair_page_footnotes`` onları geriye doğru aramayla yine bulur.

Kayıpsızlık: hiçbir metin düşmez, eklenmez. İstisnalar sayfalama
artıklarıdır (kaynak metnin parçası değil, sayfa süsü), silinen her öğe
ayrıntıda "deger"/"metin" ile sayılır: sayfa numarası (yalnız dizi
tutarlıysa), KARAR 30 devam etiketi / yinelenen tablo başlığı, KARAR 32
sayfa üst/alt bilgisi (ust_bilgi_silindi). Emin olunmayan her durumda
dokunulmaz (muhafazakâr).
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from latex_pipeline.patterns import CHAPTER_LABEL_WORDS, xref_label_alternation

# ---------------------------------------------------------------------------
# Adlandırılmış sabitler
# ---------------------------------------------------------------------------

# Bölünmüş iki yarı (paragraf, madde, tablo, liste) arasında en çok bu kadar
# "ara blok" (dipnot tanımı, sayfa numarası, görsel, altyazı, sayfa üst
# bilgisi) bulunabilir. Bir sayfanın altı ile sonrakinin üstü arasında ölçülen
# en çok ara blok: hf olay raporunda 5 (dipnot tanımları).
ARA_BLOK_AZAMI = 8

# Sayfa numarası: tek başına satırda 1-4 rakam.
SAYFA_NO_BASAMAK_AZAMI = 4

# Cümle sonundan sonra düz rakamla basılmış dipnot işareti ("… the valve
# closed. 34", "… our main result: 79"; Marker üst simgeyi düz metne indirmiş): bu
# kadar basamağa kadar sayı işaret sayılır, satır kapalı uçludur.
DUZ_DIPNOT_BASAMAK_AZAMI = 3

# Devam parçasının ilk sözcüğü belgede en az bu kadar kez geçmeli (kopuk
# hece değil: "tion across …", Marker'ın düşürdüğü satırın artığı) ya da
# sistem sözlüğünde bulunmalı.
DEVAM_SOZCUK_TEKRAR_ASGARI = 2
# macOS'ta her zaman bulunan İngilizce sözcük listesi; yoksa yalnız belge
# içi tekrar ölçütü kullanılır.
SISTEM_SOZLUGU = "/usr/share/dict/words"

# Birleşecek paragrafın ilk yarısı en az bu kadar sözcük içermeli (başlık
# ya da etiket gibi kısa, noktasız satırlar birleştirilmez).
ILK_YARI_SOZCUK_ASGARI = 4

# Liste maddesi devamı: maddenin ilk yarısı en az bu kadar sözcük (kısa
# ifade listeleri — "- elma" / "- armut" — noktasız biter, birleştirilmez).
MADDE_ILK_YARI_SOZCUK_ASGARI = 6

# Liste birleştirme: dipnot tanımları ikinci liste parçasının ardına
# taşınırken aradaki dolu satır sayısı bunu aşmamalı (dönüştürücünün
# _pair_page_footnotes penceresi 40 dolu satırdır; tanım işaretinden fazla
# uzaklaşmasın).
LISTE_TASIMA_SATIR_AZAMI = 20

# Tablo hücresi devamı: önceki hücre en az bu kadar sözcük içermeli.
HUCRE_ILK_YARI_SOZCUK_ASGARI = 3

# Yer değiştirmiş devam parçası (Marker okuma sırası hatası): açık
# uçlu paragraftan sonra en çok bu kadar blok ileride aranır.
KAYIK_DEVAM_BLOK_AZAMI = 6

# Sayfalar arası bölünmüş dipnot tanımı: açık uçlu tanımın
# devamı bir sonraki sayfanın dipnot alanının başında, gövdeden sonra gelir.
# Tanım öbeğiyle bir sonraki dipnot alanı arasında en çok bu kadar blok
# bulunabilir (bir sayfanın gövdesi; bir test kitabında 7 blok ölçüldü).
DIPNOT_DEVAM_BLOK_AZAMI = 30

# Sayfa üst/alt bilgisi: kısa, noktasız, belgede en az bu kadar kez
# tek başına yinelenen satır.
UST_BILGI_TEKRAR_ASGARI = 3
UST_BILGI_UZUNLUK_AZAMI = 80

# Paragraf sonuna yapışmış üst bilgi ("… we revisit Preface ix", "… the method
# References 423"; bir test kitabında): üst bilgi en çok bu kadar sözcük.
YAPISIK_UST_SOZCUK_AZAMI = 5

# Görselin hemen yanında, etiketi ortada büyük harfle duran bozuk altyazı
# (sentetik örnek: "Cutaway view of … under a given FIGURE 2.2 load and
# speed") en çok bu kadar karakter.
IC_ALTYAZI_UZUNLUK_AZAMI = 200

# Şekil lejantı: en az bu kadar "simge – ad" girdisi.
LEJANT_GIRDI_ASGARI = 2

# Kesin cümle sonu: noktadan önceki belirteç en az bu kadar harf/rakam
# ("tested." kesin; "al.", "Fig.", "pp." kısaltma olabilir). Dipnot
# tanımına yapışık gövde devamı kuralında kullanılır (bkz. _kesin_kapali).
KESIN_SON_SOZCUK_ASGARI = 4

# Kısa rakamlı ad/kod satır sonunda ("АБ-12", "PM2.5", "H2O"): en çok bu
# kadar karakterse sözcük gibi sayılır (açık uçlu olabilir). Daha uzun
# rakamlı kimlikler ("abcKOD9180", base64 parçaları) kapalı sayılır.
# Sentetik örnek: "…новый двигатель АБ-12" + görsel + "работает на земле…".
KISA_KIMLIK_UZUNLUK_AZAMI = 8

# Seyrek devam sözcüğü (belgede bir kez, sistem sözlüğünde yok): en az bu kadar
# harfliyse ve kökü (son KOK_KESME harfi atılmış, en az KOK_ASGARI harf)
# belgede başka bir sözcüğün başı olarak geçiyorsa gerçek sözcük sayılır
# ("underexpanded" ~ "underexpansion", "расположено" ~ "расположенных").
# Kısa kopuk heceler ("tion", "ment") bu yoldan geçemez.
SEYREK_SOZCUK_ASGARI = 6
KOK_ASGARI = 5
KOK_KESME = 3

# Sayfa kırılımında düzeyi kaybolmuş harfli alt madde dizisi (G1b-2): ikinci
# parçanın başında dizinin devamından ÖNCE en çok bu kadar etiketsiz madde
# bulunabilir; bunlar önceki alt maddenin ikinci paragrafıdır (örnek:
# "(a) …" sayfa sonunda biter, sonraki sayfa "Reducing the overall
# cost …" paragrafıyla sürer, ardından "(b)", "(c)").
IC_DEVAM_PARAGRAF_AZAMI = 2

# Ara bloksuz bölünmüş madde (G1b-2): bu kadar sözcükten uzun madde düzyazıdır
# (cümle biçimli liste kanıtı); noktasız ifade listesinin maddeleri kısadır
# ("- Regular cleaning of the intake filter screens": 7 sözcük). Bir test
# kitabında tek maddelik listenin 30+ sözcüklük maddesi sayfa sonunda
# "… in the" diye kesilmiş, devamı "upper valley during the dry season …".
CUMLE_MADDE_SOZCUK_ASGARI = 15

# Ara bloksuz bölünmüş madde (G1b-2): iki yarının da ilk bu kadar
# karakterinde ayraçlı yıl ("(2012)") varsa ikisi de kaynakça girdisidir;
# küçük harfli ikinci yarı ("de Vries J (2015) …") yeni girdidir, devam değil.
KAYNAKCA_YIL_PENCERE = 60

# ---------------------------------------------------------------------------
# Düzenli ifadeler
# ---------------------------------------------------------------------------

_NOBET = "\x02"
# Dipnot tanımı: satır başında üst simge (nöbetçi ya da ham <sup>) + metin.
# Dönüştürücünün _PAGE_FOOTNOTE_DEF_RE'siyle aynı biçim (+ ham <sup>).
_DIPNOT_TANIM_RE = re.compile(
    r"^[ \t]*(?:\x02S\d+\x02|<sup>[ \t]*(?:\d{1,3}|[*†‡§¶]{1,3})[ \t]*</sup>)"
    r"[ \t]*(\S.*)$")
_HARF2_RE = re.compile(r"[^\W\d_]{2}")
_BASLIK_RE = re.compile(r"^\s*#{1,6}(?:\s|$)")
_CIT_RE = re.compile(r"^\s*```")
_TABLO_RE = re.compile(r"^\s*\|")
_GORSEL_RE = re.compile(r"^!\[[^\]]*\]\([^)]+\)\s*(?:\\\\)?\s*$")
_YATAY_RE = re.compile(r"^\s*[-*_]{3,}\s*$")
_ALINTI_RE = re.compile(r"^\s*>")
_YORUM_RE = re.compile(r"^\s*<!--")
# Dönüştürücünün liste düzenli ifadeleriyle aynı (_UL_ITEM_RE, _OL_ITEM_RE).
_IMLI_RE = re.compile(r"^(\s*)([-*])(\s+)(.*)$")
_SIRALI_RE = re.compile(r"^(\s*)(\d+)\.(\s+)(.*)$")
# İmli maddenin metnindeki elle numara ("- 10. Metin").
_MADDE_NO_RE = re.compile(r"^(\d+)\.\s+")
_SAYI_RE = re.compile(r"^\s*(\d{1,%d})\s*$" % SAYFA_NO_BASAMAK_AZAMI)
# Cümle sonu noktalaması (kapayıcılarla) + boşluk + düz rakamlı dipnot işareti.
_DUZ_DIPNOT_SON_RE = re.compile(
    r"[.!?…:;][\"'”’»)\]]*\s+\d{1,%d}$" % DUZ_DIPNOT_BASAMAK_AZAMI)
# Sözcük biçimli belirteç: harfler (iç tire/kesme işaretiyle), ardından en
# çok birkaç noktalama. Rakam, alt çizgi, ters bölü içeren belirteç (kod,
# kimlik, "abcKOD9180\\_TO…", base64) sözcük değildir.
# Eğik çizgiyle birleşik sözcük de sözcüktür ("electrical/mechanical",
# "and/or").
_SOZCUK_BELIRTEC_RE = re.compile(
    r"^[\"'“‘«(\[]?[^\W\d_]+(?:[-'’/][^\W\d_]+)*[.,;:!?…\"'”’»)\]]{0,3}$")
_SAYI_BELIRTEC_RE = re.compile(r"^[(\[]?\d+(?:[.,]\d+)*%?[.,;:)\]]{0,2}$")
# Rakamlı kimlik ya da noktalı kısaltma + virgül: "XK2049B,", "GPT-5.6,",
# "(i.e.," (örnek: "Another sensor, XK2049B," + dipnot 87-89 + "reported
# a fault…"; "values (i.e.," + "measured …"). Virgül
# cümlenin sürdüğünü gösterir. Virgülsüz kimlik ve ters bölü/alt çizgili kod
# kimliği ("abcKOD\\_V8…") yine kapalı sayılır.
_KIMLIK_VIRGUL_RE = re.compile(r"^[(\[\"'“‘«]?[^\W_]+(?:[-.'’][^\W_]+)*\.?,$")
# Harf ya da Roma rakamlı sıralayıcı: "a) ", "(b) ", "iv. ".
_SIRALAYICI_RE = re.compile(r"^\(?(?:[a-z]|[ivxl]{1,5})[.)]\s")
# Satır başında vurgulu ya da matematik kipinde kısa simge: "*m* is",
# "$\\dot m$ =" (değişken tanımı satırı; önceki satırın devamı değildir).
_SIMGE_BAS_RE = re.compile(r"^(?:\*{1,2}[^*\s]{1,3}\*{1,2}|_[^_\s]{1,3}_|\$[^$]{1,20}\$)(?:\s|$)")
# Tek harfli değişken tanımı: "g is the acceleration", "T = period".
_TANIM_SATIRI_RE = re.compile(r"^\s*[^\W\d_]\s+(?:is|are|=|denotes)\s")
# Satır sonunda heceleme tiresi: "pos-" (harf + tire).
_TIRE_SON_RE = re.compile(r"([^\W\d_]+)-$")
# Tireli bileşik sözcük: "exposure-response", "двигатель-ный".
_TIRELI_BILESIK_RE = re.compile(r"(?<![\w\-])([^\W\d_]+)-([^\W\d_]+)")
# Tek başına sözcük (tire, kesme ya da harf/rakamla bitişik değil).
_TEKIL_SOZCUK_RE = re.compile(r"(?<![\w\-'’])([^\W\d_]+)(?![\w\-'’])")
# Bağ başı: "[metin](hedef)".
_BAG_BAS_RE = re.compile(r"^\[[^\]]*\]\(")
# Şekil lejantı girdisi: satır başında ya da virgül/noktalı virgülden sonra
# 1-4 karakterlik simge + uzun tire + ad ("7 – escape wheel", "o — горючее").
_LEJANT_GIRDI_RE = re.compile(r"(?:^|[,;]\s*)[^\s,;]{1,4}\s*[—–]\s*\S")
# Satır sonunda denklem numarası: "… (2.48)".
_DENKLEM_NO_SON_RE = re.compile(r"\(\s*\d+(?:[.\-–]\d+)*[a-z]?\s*\)\s*$")
# Altyazı: şekil/tablo etiketi + numara (patterns.XREF_LABEL_WORDS).
_ALTYAZI_RE = re.compile(
    r"^\s*(?:\*{1,2}|_{1,2})?\s*(?:" + xref_label_alternation(["figure", "table"])
    + r")\s*\d", re.IGNORECASE)
# Etiket + numara (+ bitişik alt şekil harfi, kapanan vurgu, iki nokta/nokta):
# ardından gelen ilk belirteç altyazıyı gövde atfından ayırır (bkz.
# _altyazi_satiri_mi).
_ALTYAZI_NO_RE = re.compile(
    r"^\s*(?:\*{1,2}|_{1,2})?\s*(?:" + xref_label_alternation(["figure", "table"])
    + r")\s*\d+(?:[.\-–]\d+)*[a-z]?(?:\*{1,2}|_{1,2})?[.:]?", re.IGNORECASE)
# Alt şekil harfi: "a", "a)", "(a)", "a." ("Fig. 2.3 a The valve …").
_ALT_SEKIL_HARFI_RE = re.compile(r"^\(?[a-h][).]?$")
# Küçük harfle başladığı hâlde altyazı olan ekler: "Fig. 2.31 continued".
_ALTYAZI_DEVAM_SOZCUKLERI = frozenset(
    {"continued", "cont", "contd", "fortsetzung", "продолжение", "suite"})
# Altyazı etiketi metnin ortasında, BÜYÜK harfle (sentetik: "… a given
# FIGURE 2.2 load and speed"): Marker altyazının satırlarını karıştırmış.
# Gövde metni şekle "Figure 2.2" diye atıf yapar; büyük harfli etiket
# altyazı izidir.
_IC_ALTYAZI_RE = re.compile(r"(?<![\w.])(?:FIGURES?|FIGS?\.|TABLES?)\s*\d")
# Satır sonundaki sayfa numarası (Arap ya da küçük Roma rakamı, i–xxxix).
_SON_SAYFA_NO_RE = re.compile(
    r"\s(\d{1,%d}|(?=[ivx])x{0,3}(?:ix|iv|v?i{0,3}))$" % SAYFA_NO_BASAMAK_AZAMI)
# Madde metninin başındaki fazladan tire: "**- Metin**" ya da "**-** Metin".
_TIRE_KALIN_RE = re.compile(r"^(\*\*|__)[ \t]*[-–—][ \t]+(?=\S)")
_TIRE_YALNIZ_RE = re.compile(r"^(\*\*|__)[ \t]*[-–—][ \t]*\1[ \t]+(?=\S)")
# Kalınsız tire imi: madde metni "--- ", "-- ", "- ", "— ", "– " ile başlıyor
# (kaynakta tireyle imlenmiş liste; Marker "- " ekleyip tireyi metinde
# bırakmış, dil_sent_ru: "- --- расчётный режим" -> "• —"). Yalnız bloktaki
# BÜTÜN maddelerde varsa silinir (tek maddedeki tire alıntı/konuşma olabilir).
_TIRE_DUZ_RE = re.compile(r"^(?:-{1,3}|[–—])[ \t]+(?=\S)")

# G1b-2: bölünmüş liste / iç içe liste / denklem düzenli ifadeleri.
# Cümle bitiren noktalama + (kapayıcı) + boşluk + büyük harf: madde kendi
# içinde bir cümle bitiriyor (cümle biçimli liste kanıtı).
_IC_CUMLE_SONU_RE = re.compile(r"[.!?…][\"'”’»)\]*_]*\s+[\"'“‘«(\[*_]*(?=[^\W\d_])")
# Kaynakça girdisindeki ayraçlı yıl: "(2012)", "(2016a)".
_YIL_RE = re.compile(r"\((?:1[5-9]|20)\d\d[a-z]?\)")
# Kişi adı önekleri ("van Dam", "de Gaulle", "von Neumann"): küçük harfli
# önek + büyük harfli ad, cümle devamı değil.
_AD_ONEKLERI = frozenset({
    "van", "von", "de", "der", "den", "da", "di", "du", "del", "della", "des",
    "le", "la", "dos", "das", "ten", "ter", "zu", "al", "el", "bin", "ibn"})
# Alt madde sıra etiketi: "(a) ", "a. ", "a) ", "(iv) ", "iv. ", "(3) ", "3) ".
# Düz "3. " numaralı listedir (_SIRALI_RE / _MADDE_NO_RE), burada sayılmaz.
_SIRA_ETIKET_RE = re.compile(r"^(\(?)([a-zA-Z]{1,6}|\d{1,2})([.)])\s")
_ROMA_RE = re.compile(r"^(x{0,3})(ix|iv|v?i{0,3})$")
# Yalnız kalın etiketten oluşan madde ("**action/go:**", "**Result**:") ve
# etiket + içerik ("**ad:** ^abAD…").
_ETIKET_YALNIZ_RE = re.compile(r"^(\*\*|__)[^*_\n]{1,80}?(?::\1|\1:)\s*$")
_ETIKETLI_ICERIK_RE = re.compile(r"^(\*\*|__)[^*_\n]{1,80}?(?::\1|\1:)\s+(\S.*)$")
# MT-11: display denklemin hemen altında ";" ile başlayan tek satır içi
# matematik (+ isteğe bağlı sondaki noktalama).
_DENKLEM_DEVAM_RE = re.compile(r"^;\s*\$([^$]+)\$\s*([.,;]?)\s*$")
# MT-6: yalnız tek satır içi matematikten oluşan satır.
_SATIR_ICI_DENKLEM_RE = re.compile(r"^\s*\$([^$]+)\$\s*$")
# Matematikte bağıntı (denklem satırı kanıtı): =, <, >, ≤, \le, \approx, \to …
_BAGINTI_RE = re.compile(
    r"(?<!\\)[=<>]|[≤≥≈≠≡∝→⇒⟹]|\\(?:le|ge|leq|geq|ne|neq|approx|equiv|sim|"
    r"simeq|cong|propto|to|rightarrow|Rightarrow|longrightarrow|implies|iff|"
    r"in|ll|gg|lesssim|gtrsim)(?![A-Za-z])")

# Başlığın başındaki bölüm etiketi ("Chapter", "Bölüm", "Глава" …).
_BOLUM_ETIKETI_RE = re.compile(r"^\s*" + CHAPTER_LABEL_WORDS + r"\s+")

# Cümle sonu noktalaması (kapanan tırnak/ayraçtan önce de olabilir).
_CUMLE_SONU = set(".!?…:;")
_KAPAYICI = set(")]}\"'”’»›")
# Açık uç: harf, rakam, virgül, tire (heceleme).
_ACIK_UC_NOKTALAMA = set(",-‐–")
# Satır sonundaki bağ hedefi, üst simge nöbetçisi, vurgu işaretleri.
_SON_BAG_RE = re.compile(r"\]\((?:[^()\s]|\([^()\s]*\))*\)$")
_SON_NOBET_RE = re.compile(r"(?:\x02[SF]\d+\x02|<sup>[^<]*</sup>)$")


# ---------------------------------------------------------------------------
# Blok modeli
# ---------------------------------------------------------------------------

@dataclass
class _Blok:
    tur: str                 # baslik, kod, denklem, tablo, gorsel, liste,
                             # dipnot, sayi, yatay, alinti, paragraf, diger
    satirlar: List[str]
    bos_once: List[str] = field(default_factory=list)   # öndeki boş satırlar

    @property
    def metin(self) -> str:
        return " ".join(s.strip() for s in self.satirlar)


def _satir_turu(s: str) -> Optional[str]:
    """Tek satırlık blok başlangıcı türü (paragraf/liste dışı)."""
    if _BASLIK_RE.match(s):
        return "baslik"
    if _GORSEL_RE.match(s.strip().replace(r"\_", "_")):
        return "gorsel"
    m = _DIPNOT_TANIM_RE.match(s)
    if m and _HARF2_RE.search(m.group(1)):
        return "dipnot"
    if _YATAY_RE.match(s) and not _IMLI_RE.match(s + " x"):
        return "yatay"
    if _ALINTI_RE.match(s):
        return "alinti"
    if _YORUM_RE.match(s):
        return "diger"
    return None


def _madde_mi(s: str) -> bool:
    return bool(_IMLI_RE.match(s) or _SIRALI_RE.match(s))


def _ayristir(metin: str) -> Tuple[List[_Blok], List[str]]:
    """Metni bloklara ayır. Birleştirme ``_birlestir`` ile birebir geri döner."""
    satirlar = metin.split("\n")
    n = len(satirlar)
    bloklar: List[_Blok] = []
    bos: List[str] = []
    i = 0
    while i < n:
        s = satirlar[i]
        if not s.strip():
            bos.append(s)
            i += 1
            continue
        j = i + 1
        if _CIT_RE.match(s):
            tur = "kod"
            while j < n and not _CIT_RE.match(satirlar[j]):
                j += 1
            j = min(j + 1, n)
        elif s.strip().startswith("$$"):
            tur = "denklem"
            ic = s.strip()[2:]
            if "$$" not in ic:
                k = j
                while k < n and k - i <= 30 and "$$" not in satirlar[k] \
                        and not _BASLIK_RE.match(satirlar[k]):
                    k += 1
                if k < n and "$$" in satirlar[k]:
                    j = k + 1
        elif _TABLO_RE.match(s):
            tur = "tablo"
            while j < n and _TABLO_RE.match(satirlar[j]):
                j += 1
        elif _madde_mi(s):
            tur = "liste"
            while j < n and satirlar[j].strip():
                t = satirlar[j]
                if _madde_mi(t):
                    j += 1
                    continue
                if (_satir_turu(t) or _CIT_RE.match(t) or _TABLO_RE.match(t)
                        or t.strip().startswith("$$")):
                    break
                girinti = len(t) - len(t.lstrip(" \t"))
                ilk = _ilk_gorunur(t)
                if girinti >= 2 or (ilk.isalpha() and ilk.islower()):
                    j += 1
                    continue
                break
        else:
            tur = _satir_turu(s)
            if tur is None:
                tur = "paragraf"
                while j < n and satirlar[j].strip():
                    t = satirlar[j]
                    if (_satir_turu(t) or _madde_mi(t) or _CIT_RE.match(t)
                            or _TABLO_RE.match(t) or t.strip().startswith("$$")):
                        break
                    j += 1
                if j == i + 1 and _SAYI_RE.match(s):
                    tur = "sayi"
        bloklar.append(_Blok(tur, satirlar[i:j], bos))
        bos = []
        i = j
    return bloklar, bos


def _birlestir(bloklar: List[_Blok], son_bos: List[str]) -> str:
    out: List[str] = []
    for b in bloklar:
        out.extend(b.bos_once)
        out.extend(b.satirlar)
    out.extend(son_bos)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Metin uçları
# ---------------------------------------------------------------------------

def _son_temiz(metin: str) -> str:
    """Sondaki dipnot nöbetçisi / üst simge, bağ hedefi ("](#page-…)"),
    vurgu işaretleri atılmış metin."""
    t = metin.rstrip()
    while True:
        eski = t
        t = _SON_NOBET_RE.sub("", t).rstrip()
        m = _SON_BAG_RE.search(t)
        if m:
            t = t[:m.start()]
        t = t.rstrip("*_ \t")
        if t.endswith("\\") and not t.endswith("\\\\"):
            t = t[:-1]
        if t == eski:
            break
    return t


def _son_gorunur(metin: str) -> Tuple[str, str]:
    """(son anlamlı karakter, kapayıcılardan önceki karakter)."""
    t = _son_temiz(metin)
    if not t:
        return "", ""
    son = t[-1]
    k = len(t) - 1
    while k >= 0 and t[k] in _KAPAYICI:
        k -= 1
    once = t[k] if k >= 0 else ""
    return son, once


def _belirtec_temiz(b: str) -> str:
    """Belirteçten vurgu işaretleri, üst simge nöbetçisi ve bağ sözdizimi
    atılmış hâli."""
    b = re.sub(r"\x02[SF]\d+\x02|<sup>[^<]*</sup>", "", b)
    b = b.strip("*_")
    m = re.match(r"^\[([^\]]*)\]\([^)]*\)(.*)$", b)
    if m:
        b = m.group(1) + m.group(2)
    b = b.strip("*_[]")
    # Sondaki noktalamadan önceki kapanan vurgu: "*Height*," -> "Height,"
    # (eskiden sözcük sayılmıyor, satır kapalı sanılıyordu).
    return re.sub(r"[*_]+(?=[.,;:!?…\"'”’»)\]]+$)", "", b)


def _sozcuk_belirteci_mi(b: str) -> bool:
    """Belirteç düz sözcük mü (kod/kimlik/base64 parçası değil)?"""
    return bool(_SOZCUK_BELIRTEC_RE.match(_belirtec_temiz(b)))


def _kisa_kimlik_mi(b: str) -> bool:
    """Kısa rakamlı ad/kod mu ("РД-107", "PM2.5", "H2O")? Harf ve rakam
    içerir, yalnız harf/rakam ve iç tire/nokta, en çok
    KISA_KIMLIK_UZUNLUK_AZAMI karakter."""
    return (len(b) <= KISA_KIMLIK_UZUNLUK_AZAMI
            and re.fullmatch(r"[^\W_]+(?:[-.][^\W_]+)*", b) is not None
            and re.search(r"[^\W\d_]", b) is not None
            and re.search(r"\d", b) is not None)


def _acik_uclu(metin: str) -> bool:
    """Metin cümle sonu noktalaması olmadan mı bitiyor (devamı var mı)?

    Son belirteç düz sözcük ya da sayı olmalı: kod/kimlik parçasıyla
    ("…Qx4730kb…Lmn", "…REPLY\\_abcKOD9180") biten satır açık uçlu
    sayılmaz. Cümle sonundan sonra düz rakamla basılmış dipnot işareti
    ("… the valve closed. 34") satırı kapatır.
    """
    if _DENKLEM_NO_SON_RE.search(metin):
        return False
    t = _son_temiz(metin)
    if _DUZ_DIPNOT_SON_RE.search(t):
        return False
    son_belirtec = t.split()[-1] if t.split() else ""
    if not (_sozcuk_belirteci_mi(son_belirtec)
            or _SAYI_BELIRTEC_RE.match(_belirtec_temiz(son_belirtec))
            or _KIMLIK_VIRGUL_RE.match(_belirtec_temiz(son_belirtec))
            or _kisa_kimlik_mi(_belirtec_temiz(son_belirtec))
            or _TIRE_SON_RE.search(son_belirtec)):
        return False
    son, once = _son_gorunur(metin)
    if not son:
        return False
    if son in _CUMLE_SONU or once in _CUMLE_SONU:
        return False
    if son in _KAPAYICI:
        return once.isalnum()
    return son.isalnum() or son in _ACIK_UC_NOKTALAMA


def _kesin_kapali(metin: str) -> bool:
    """Metin KESİN cümle sonuyla mı bitiyor ("." "!" "?" "…"; kapanan
    tırnak/ayraçtan önce de olabilir)?

    Kısaltma gibi biten satır ("the U.S.", "et al.", "Fig.") kesin sayılmaz:
    noktadan önceki belirteç iç noktasız ve en az KESIN_SON_SOZCUK_ASGARI
    harf/rakam olmalı. İki nokta ve noktalı virgül cümleyi bitirmez.
    """
    t = _son_temiz(metin)
    k = len(t)
    while k > 0 and t[k - 1] in _KAPAYICI:
        k -= 1
    t = t[:k]
    if not t or t[-1] not in ".!?…":
        return False
    if t[-1] == ".":
        son = t.split()[-1].rstrip(".").lstrip("\"'“‘«([*_")
        if "." in son or len(re.sub(r"[\W_]", "", son)) < KESIN_SON_SOZCUK_ASGARI:
            return False
    return True


def _ilk_gorunur(metin: str) -> str:
    """İlk anlamlı karakter (baştaki vurgu işaretleri ve bağ ayracı atılır)."""
    t = metin.lstrip().lstrip("*_")
    if _BAG_BAS_RE.match(t):
        t = t[1:].lstrip("*_")
    return t[:1]


def _devam_bicimli(metin: str) -> bool:
    """Metin bir önceki cümlenin devamı gibi mi başlıyor (küçük harf)?

    Harf/Roma rakamlı sıralayıcı ("a) ", "(iv) ") ve vurgulu ya da matematik
    kipindeki kısa simgeyle başlayan tanım satırı ("*m* is the mass",
    "$x$ = …") devam sayılmaz.
    """
    t = metin.lstrip()
    if _SIRALAYICI_RE.match(t.lstrip("*_")):
        return False
    if _SIMGE_BAS_RE.match(t):
        return False
    ilk = _ilk_gorunur(t)
    if not (ilk.isalpha() and ilk.islower()):
        return False
    # İlk belirteç düz sözcük olmalı ("abcKOD9180\\_TO…" gibi kimlik değil).
    ilk_belirtec = t.split()[0] if t.split() else ""
    return _sozcuk_belirteci_mi(ilk_belirtec)


def _ilk_sozcuk_metni(metin: str) -> str:
    """Baştaki vurgu ve bağ ayracı atılmış metin."""
    t = metin.lstrip().lstrip("*_")
    if _BAG_BAS_RE.match(t):
        t = t[1:].lstrip("*_")
    return t


def _sozcuk_sayisi(metin: str) -> int:
    return len(re.findall(r"[^\W\d_]{2,}", metin))


# FX1 (5 Ekim 2026, V1 yeni hata 4): dipnot tanımına boş satırsız
# yapışık satır. Sayıyla biten cümle ("… around 50%.", "… in Table 2.")
# kısaltma değildir; _kesin_kapali'nin en az KESIN_SON_SOZCUK_ASGARI harf
# kuralı yalnız sözcüklere uygulanır.
_SAYI_SONU_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*%?\.$")


def _tanim_kapali(metin: str) -> bool:
    """Dipnot tanımı kesin cümle sonuyla mı bitiyor (bkz. _kesin_kapali;
    ek olarak sayı + nokta)?"""
    if _kesin_kapali(metin):
        return True
    t = _son_temiz(metin).rstrip("".join(_KAPAYICI))
    return bool(_SAYI_SONU_RE.search(t))


def tanim_ardi_sinir_mi(tanim: str, sonraki: str) -> bool:
    """Dipnot tanımı satırının hemen ardındaki (boş satırsız) satır tanımın
    devamı DEĞİL mi (tanım orada biter mi)?

    Marker sayfa altındaki tanımı bir sonraki sayfanın ilk bloğuna satır
    sonuyla yapıştırabilir (görsel, başlık, kod çiti, madde ya da gövde
    paragrafı). Kurallar (muhafazakâr):
    - Başlık, görsel, kod çiti, tablo satırı, display denklem, yatay çizgi,
      HTML yorumu dipnot metninin devamı olamaz: sınır.
    - Madde imi ya da alıntı: tanım cümle noktalamasıyla (". ! ? …")
      bitiyorsa sınır; ":" ya da açık uçla bitiyorsa liste dipnota ait
      olabilir, dokunulmaz.
    - Düz satır: tanım KESİN kapalıysa (_tanim_kapali; "et al." gibi
      kısaltma değil) ve satır önceki cümlenin devamı gibi başlamıyorsa
      (küçük harfli düz sözcük değil: büyük harf, kod kimliği, sayı …) sınır.
      Küçük harfli devam gövde cümlesinin devamıdır, paragraf birleşmesinin
      işidir (bkz. _ara_bloklar istisna 2).
    """
    s = sonraki.strip()
    if not s:
        return True
    if (_BASLIK_RE.match(s) or _GORSEL_RE.match(s.replace(r"\_", "_"))
            or _CIT_RE.match(s) or _TABLO_RE.match(s) or s.startswith("$$")
            or (_YATAY_RE.match(s) and not _IMLI_RE.match(s + " x"))
            or _YORUM_RE.match(s)):
        return True
    if _madde_mi(sonraki) or _ALINTI_RE.match(s):
        son, once = _son_gorunur(tanim)
        return son in ".!?…" or (son in _KAPAYICI and once in ".!?…")
    return _tanim_kapali(tanim) and not _devam_bicimli(s)


def _vurgu_isareti_son(t: str) -> str:
    m = re.search(r"(\*{1,3}|_{1,2})$", t.rstrip())
    return m.group(1) if m else ""


def _vurgu_isareti_bas(t: str) -> str:
    m = re.match(r"(\*{1,3}|_{1,2})", t.lstrip())
    return m.group(1) if m else ""


def _vurgulu_kod_devami(sol: str, sag: str) -> bool:
    """Aynı vurguyla sarılı alıntının devamı `kod` parçasıyla mı başlıyor?

    "*{… values like*" + "*`abcKOD`. We could …*": ikinci yarı küçük harfle
    başlamaz ama cümle ortasıdır. Yalnız iki yarı da aynı vurgu işaretiyle
    sarılıysa (bölünmüş tek alıntı) kabul edilir.
    """
    v1, v2 = _vurgu_isareti_son(sol), _vurgu_isareti_bas(sag)
    if not v1 or v1 != v2:
        return False
    return sag.lstrip()[len(v2):].startswith("`")


def _metin_ekle(sol: str, sag: str) -> str:
    """Bölünmüş iki yarıyı tek boşlukla birleştir.

    Her iki yarı aynı vurguyla sarılıysa ("*… tried to*" + "*predict …*")
    ara kapanış/açılış işaretleri atılır: tek vurgu aralığı olur. Heceleme
    tiresi olduğu gibi kalır (bkz. RAPOR.md; derlemde örnek yok).
    """
    sol = sol.rstrip()
    sag = sag.lstrip()
    v1, v2 = _vurgu_isareti_son(sol), _vurgu_isareti_bas(sag)
    if v1 and v1 == v2 and len(sol) > len(v1) and len(sag) > len(v2) \
            and not sol[:-len(v1)].endswith(("*", "_")) \
            and not sag[len(v2):].startswith(("*", "_", " ")):
        return sol[:-len(v1)] + " " + sag[len(v2):]
    return sol + " " + sag


# ---------------------------------------------------------------------------
# Ara bloklar (sayfa kırılımında iki yarı arasına girebilenler)
# ---------------------------------------------------------------------------

_SISTEM_SOZLUGU_ONBELLEK: Optional[frozenset] = None


def _sistem_sozlugu() -> frozenset:
    """SISTEM_SOZLUGU'ndaki sözcükler (küçük harf); dosya yoksa boş küme."""
    global _SISTEM_SOZLUGU_ONBELLEK
    if _SISTEM_SOZLUGU_ONBELLEK is None:
        try:
            with open(SISTEM_SOZLUGU, encoding="utf-8", errors="ignore") as f:
                _SISTEM_SOZLUGU_ONBELLEK = frozenset(
                    s.strip().casefold() for s in f if s.strip())
        except OSError:
            _SISTEM_SOZLUGU_ONBELLEK = frozenset()
    return _SISTEM_SOZLUGU_ONBELLEK


# Sistem sözlüğü (Webster) çekimli biçimleri çoğunlukla içermez
# ("remained", "tried"): kök bu eklerden biri atılarak da aranır.
_INGILIZCE_EKLER = ("ing", "ies", "ied", "es", "ed", "s", "d", "ly")


def _sozlukte_mi(w: str) -> bool:
    """Sözcük (ya da basit İngilizce ekiyle kökü) sistem sözlüğünde mi?"""
    sozluk = _sistem_sozlugu()
    if w in sozluk:
        return True
    for ek in _INGILIZCE_EKLER:
        if w.endswith(ek) and len(w) - len(ek) >= 3:
            kok = w[:-len(ek)]
            if kok in sozluk or (ek in ("ies", "ied") and kok + "y" in sozluk) \
                    or (ek in ("ed", "ing") and kok + "e" in sozluk):
                return True
    return False


def _norm_ust_bilgi(s: str) -> str:
    t = re.sub(r"[*_#]", "", s)
    t = re.sub(r"\d+", "", t)
    return re.sub(r"\s+", " ", t).strip().casefold()


def _ust_bilgiler(bloklar: List[_Blok]) -> set:
    """Belgede tek başına yinelenen kısa, noktasız satırlar (üst/alt bilgi)."""
    sayac: Dict[str, int] = {}
    for b in bloklar:
        if b.tur != "paragraf" or len(b.satirlar) != 1:
            continue
        s = b.satirlar[0].strip()
        if len(s) > UST_BILGI_UZUNLUK_AZAMI or s[-1:] in _CUMLE_SONU:
            continue
        k = _norm_ust_bilgi(s)
        if len(k) >= 4:
            sayac[k] = sayac.get(k, 0) + 1
    return {k for k, v in sayac.items() if v >= UST_BILGI_TEKRAR_ASGARI}


def _altyazi_satiri_mi(s: str) -> bool:
    """Satır şekil/tablo altyazısı mı?

    Etiket + numarayla başlaması yetmez: ardından küçük harfli bir sözcük
    geliyorsa satır gövde paragrafıdır, şekle atıf yapar ("Figure 2.31 shows
    the operation…", "Table 4.2 summarizes…", "Tables 7.2 and 7.3 present…";
    bir test kitabında 13 satır). Eskiden bunlar altyazı sayılıyor,
    (a) devamları hiç birleşmiyor, (b) görsel yanındaysa ara blok sanılıp
    birleşik paragrafın ardına taşınıyordu. Altyazıda etiketten sonra
    noktalama, büyük harf, ayraç ya da alt şekil harfi + büyük harf gelir;
    "Fig. 2.31 continued" altyazı devamıdır.
    """
    if not _ALTYAZI_RE.match(s):
        return False
    k = _ALTYAZI_NO_RE.match(s)
    if not k:
        return True
    kalan = s[k.end():].split()
    if len(kalan) > 1 and _ALT_SEKIL_HARFI_RE.match(kalan[0]):
        kalan = kalan[1:]
    if not kalan:
        return True
    ilk = _ilk_gorunur(kalan[0])
    if not (ilk.isalpha() and ilk.islower()):
        return True
    m = re.match(r"[^\W\d_]+", _ilk_sozcuk_metni(kalan[0]))
    return bool(m) and m.group(0).casefold() in _ALTYAZI_DEVAM_SOZCUKLERI


def _altyazi_mi(b: _Blok) -> bool:
    return b.tur == "paragraf" and _altyazi_satiri_mi(b.metin)


def _lejant_metni_mi(metin: str) -> bool:
    """Şekil lejantı: "1 – valve, 2 – piston, …", "н — насос;
    к — клапан; …" (en az iki "simge – ad" girdisi)."""
    return len(_LEJANT_GIRDI_RE.findall(metin)) >= LEJANT_GIRDI_ASGARI


def _lejant_mi(b: _Blok) -> bool:
    return b.tur == "paragraf" and _lejant_metni_mi(b.metin)


# ---------------------------------------------------------------------------
# Liste maddesi ve denklem yardımcıları (G1b-2)
# ---------------------------------------------------------------------------

def _girinti(s: str) -> int:
    return len(s) - len(s.lstrip(" \t"))


def _madde_ogeleri(b: _Blok) -> List[Tuple[int, int, int, str]]:
    """Liste bloğunun maddeleri: (ilk satır, son satır + 1, girinti, metin).

    Metin imden sonraki gövdedir (imli maddede elle numara "2. " dahil),
    devam satırları boşlukla katılır."""
    ogeler: List[List] = []
    for k, s in enumerate(b.satirlar):
        m = _IMLI_RE.match(s) or _SIRALI_RE.match(s)
        if m:
            ogeler.append([k, k + 1, len(m.group(1)), m.group(4).strip()])
        elif ogeler:
            ogeler[-1][1] = k + 1
            ogeler[-1][3] += " " + s.strip()
    return [tuple(o) for o in ogeler]


def _kucuk_baslar(metin: str) -> bool:
    """Madde metni (elle numara, sıra etiketi — "a. ", "(b) " —, vurgu ve
    bağ ayracı atılmış) küçük harfle mi başlıyor?"""
    t = _MADDE_NO_RE.sub("", metin.strip(), count=1)
    if _sira_etiketi(t):
        t = t[_SIRA_ETIKET_RE.match(t).end():]
    ilk = _ilk_gorunur(t)
    return ilk.isalpha() and ilk.islower()


def _cumle_sonu_ile_biter(metin: str, iki_nokta: bool = False) -> bool:
    """Metin cümle bitiren noktalamayla ("." "!" "?" "…"; isteğe bağlı ":")
    mı bitiyor (kapanan tırnak/ayraçtan önce de olabilir)?"""
    son, once = _son_gorunur(metin)
    bitirici = ".!?…:" if iki_nokta else ".!?…"
    return bool(son) and (son in bitirici or (son in _KAPAYICI and once in bitirici))


def _ic_cumle_sonu(metin: str) -> bool:
    """Metin kendi içinde bir cümle bitirip büyük harfle yenisine mi
    başlıyor ("… the old pumps. What the team …")?"""
    for m in _IC_CUMLE_SONU_RE.finditer(metin):
        if metin[m.end():m.end() + 1].isupper():
            return True
    return False


def _ad_onekiyle_basliyor(metin: str) -> bool:
    """Metin küçük harfli ad önekiyle başlayan kişi adı mı ("van Dam K",
    "de la Cruz A", "von Neumann")?"""
    sozler = _ilk_sozcuk_metni(metin).split()
    k = 0
    while k < len(sozler) and sozler[k].casefold() in _AD_ONEKLERI:
        k += 1
    return 0 < k < len(sozler) and sozler[k][:1].isupper()


def _roma_degeri(s: str) -> Optional[int]:
    """Küçük Roma rakamı (i–xxxix) değeri; değilse None."""
    m = _ROMA_RE.match(s)
    if not s or not m:
        return None
    onlar = len(m.group(1)) * 10
    birler = {"": 0, "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6,
              "vii": 7, "viii": 8, "ix": 9}[m.group(2)]
    return onlar + birler or None


def _sira_etiketi(metin: str):
    """Alt madde sıra etiketi: ((açılış, kapanış, harf türü), {(dizi, değer)})
    ya da None. "i" hem harf (9) hem Roma rakamı (1) olabilir: iki yorum da
    döner; dizinin devamı ikisinden biriyle sürmelidir."""
    m = _SIRA_ETIKET_RE.match(metin)
    if not m:
        return None
    acilis, etiket, kapanis = m.groups()
    if acilis and kapanis != ")":
        return None
    yorumlar = set()
    if etiket.isdigit():
        if kapanis != ")":
            return None                  # "3. " numaralı listedir
        yorumlar.add(("sayi", int(etiket)))
        tur = "d"
    else:
        if not (etiket.islower() or etiket.isupper()):
            return None
        tur = "k" if etiket.islower() else "b"
        kucuk = etiket.lower()
        if len(kucuk) == 1:
            yorumlar.add(("harf", ord(kucuk) - ord("a") + 1))
        roma = _roma_degeri(kucuk)
        if roma:
            yorumlar.add(("roma", roma))
        if not yorumlar:
            return None
    return (acilis, kapanis, tur), frozenset(yorumlar)


def _sonraki_etiket_mi(e1, e2) -> bool:
    """e2 etiketi, e1'in aynı biçimdeki bir sonrakisi mi ("(a)" -> "(b)",
    "(i)" -> "(ii)", "c." -> "d.")?"""
    if e1 is None or e2 is None or e1[0] != e2[0]:
        return False
    return any((dizi, deger + 1) in e2[1] for dizi, deger in e1[1])


def _display_kapali(b: _Blok) -> bool:
    """Denklem bloğu kapanmış bir display mi ("$$…$$" ya da çok satırlı
    "$$ … $$")?"""
    ilk = b.satirlar[0].strip()
    son = b.satirlar[-1].rstrip()
    if len(b.satirlar) == 1:
        return ilk.startswith("$$") and son.endswith("$$") and len(ilk) > 4
    return ilk.startswith("$$") and son.endswith("$$")


# onar_rapor sözlüğünün anahtarları (değişiklik türü -> sayı). Boş girdide de
# hepsi 0 olarak döner; çağıran taraf anahtarın varlığına güvenebilir.
# KARAR 30 (Berke, 5 Ekim 2026): sayfa sonunda bölünüp sonraki sayfada
# başlık satırı yinelenerek ve "devamı" etiketiyle süren tablo tek tabloda
# birleşir; yinelenen başlık satırı ve devam etiketi sayfalama artığıdır
# (sayfa numarası gibi — kayıpsızlık kuralının ikinci istisnası) ve silinir.
# Örnek (bir test kitabında Tablo 8.3): tablo + "(continued)" + "Table 8.3
# (continued)" + aynı başlık satırıyla süren tablo. Tablo 4.2'de ikinci etiket OCR'la
# bozulmuş bir mini tablo ("| <b>Table 4.2</b> | (continued) |" ve altında
# "| 1 able 4.2 | Commuea |"). Altı kaynak dilin devam sözcükleri:
_DEVAM_SOZ = (r"(?:continued|cont(?:'d|\.)|fortsetzung|fortgesetzt|suite"
              r"|continuaci[oó]n|continua|continuazione|segue"
              r"|продолжение|окончание)")
_TABLO_SOZ = r"(?:table|tab\.|tabelle|tableau|tabla|tabella|таблица)"
# Tek başına devam etiketi: "(continued)", "continued", "(Fortsetzung)".
_DEVAM_ETIKET_RE = re.compile(r"^\(?\s*" + _DEVAM_SOZ + r"\s*\)?\.?$", re.I)
# Devam altyazısı: "Table 8.3 (continued)", "Tabelle 2.1 (Fortsetzung)".
_DEVAM_ALTYAZI_RE = re.compile(
    r"^" + _TABLO_SOZ + r"\s*\d+(?:[.\-–]\d+)*\s*[:.]?\s*\(?\s*" + _DEVAM_SOZ
    + r"\s*\)?\.?$", re.I)
# Devam artığı mini tablo en çok bu kadar içerik satırı taşır (başlık +
# OCR'lı kopyası; ölçülen olguda 2).
DEVAM_MINI_TABLO_SATIR_AZAMI = 3


def _hucre_metni(satir: str) -> List[str]:
    """Tablo satırının hücreleri: etiketsiz, boşlukları sıkıştırılmış, küçük harf."""
    govde = satir.strip().strip("|")
    hucreler = []
    for h in govde.split("|"):
        h = re.sub(r"<[^>]+>", " ", h)
        # Dönüştürücü <b>/<i>'yi bu modülden ÖNCE **/__/* yapıyor (V1
        # bulgusu): vurgu işaretleri karşılaştırmada yok sayılır.
        h = re.sub(r"\*\*|__|(?<!\w)[*_]|[*_](?!\w)", "", h)
        hucreler.append(re.sub(r"\s+", " ", h).strip().lower())
    return hucreler


def _tablo_ayirici_mi(satir: str) -> bool:
    return bool(re.match(r"^\s*\|?\s*:?-{3,}", satir))


def _devam_artigi_mi(b: "_Blok") -> bool:
    """Blok bir devam etiketi ya da devam altyazısı (mini tablo dahil) mı?"""
    if b.tur == "tablo":
        icerik = [s for s in b.satirlar if not _tablo_ayirici_mi(s)]
        if not icerik or len(icerik) > DEVAM_MINI_TABLO_SATIR_AZAMI:
            return False
        ilk = " ".join(h for h in _hucre_metni(icerik[0]) if h)
        return bool(_DEVAM_ALTYAZI_RE.match(ilk))
    if b.tur in ("paragraf", "diger") and len(b.satirlar) == 1:
        t = re.sub(r"<[^>]+>", " ", b.satirlar[0])
        t = re.sub(r"\*\*|__", "", t)
        t = re.sub(r"\s+", " ", t).strip().strip("*_").strip()
        return bool(_DEVAM_ETIKET_RE.match(t) or _DEVAM_ALTYAZI_RE.match(t))
    return False


SAYAC_TURLERI: Tuple[str, ...] = (
    "sayfa_no_silindi",
    "paragraf_birlesti",
    "madde_birlesti",
    "kayik_devam_birlesti",
    "dipnot_devam_birlesti",
    "liste_birlesti",
    "tablo_birlesti",
    "tablo_satiri_birlesti",
    "tablo_bos_sutun_atildi",
    "tire_silindi",
    "ara_blok_tasindi",
    "yapisik_altyazi_ayrildi",
    "yapisik_ust_bilgi_ayrildi",
    # G1b-2
    "liste_imi_birlesti",
    "ic_madde_birlesti",
    "ic_liste_duzeyi_onarildi",
    "denklem_devami_birlesti",
    "denklem_satiri_ayrildi",
    # KARAR 30 (5 Ekim 2026): devam eden tablo
    "devam_tablosu_birlesti",
    "devam_etiketi_silindi",
    "yinelenen_baslik_silindi",
    # FX1 (5 Ekim 2026): dipnot tanımına yapışık blok boş satırla ayrıldı
    "dipnot_tanimi_ayrildi",
    # KARAR 32 (5 Ekim 2026): sayfa üst/alt bilgisi silindi (ayrıntıda
    # "deger": silinen satır; kayıpsızlık denetimi bunu düşer)
    "ust_bilgi_silindi",
)


class _Onarici:
    def __init__(self, metin: str):
        self.bloklar, self.son_bos = _ayristir(metin)
        self.ust_bilgi = _ust_bilgiler(self.bloklar)
        self.sayac: Dict[str, int] = {k: 0 for k in SAYAC_TURLERI}
        self.ayrinti: List[Dict[str, str]] = []

    # -- yardımcılar ------------------------------------------------------
    def _ara_mi(self, p: int, ara: List[int]) -> bool:
        """p. blok sayfa kırılımı ara bloğu mu?"""
        b = self.bloklar[p]
        if b.tur in ("dipnot", "sayi", "gorsel"):
            return True
        if b.tur == "paragraf" and len(b.satirlar) == 1 \
                and (_norm_ust_bilgi(b.satirlar[0]) in self.ust_bilgi
                     or self._sayfa_no_ust_bilgi_mi(b.satirlar[0])):
            return True
        if _altyazi_mi(b) or _lejant_mi(b):
            # Altyazı / lejant şekil öbeğinin içinde ara bloktur: önceki ara
            # blok görsel/altyazı/lejant ya da sonraki blok görsel (istisna:
            # aşağıda görselsiz tek satırlık altyazı).
            onceki = self.bloklar[ara[-1]] if ara else None
            if onceki is not None and (onceki.tur == "gorsel" or _altyazi_mi(onceki)
                                       or _lejant_mi(onceki)):
                return True
            if p + 1 < len(self.bloklar) and self.bloklar[p + 1].tur == "gorsel":
                return True
            if _altyazi_mi(b) and len(b.satirlar) == 1:
                # Görseli Marker başka yere koymuş tek satırlık altyazı
                # (sentetik: "…we use this model to" +
                # "FIGURE 2.7 Sketch of the test rig …" + "estimate the
                # losses …"). Gövde atfı ("Figure 2.7
                # shows …") altyazı sayılmaz (_altyazi_satiri_mi); ardından
                # tablo/görsel gibi gövde dışı blok gelirse birleşme olmaz.
                # Birleşecek ilk yarı görselin ya da kod bloğunun (Marker
                # çizimi metin olarak verebiliyor) hemen ardındaysa şeklin
                # içindeki etiket/açıklamadır: görsel + etiket listesi +
                # "Fig. 3.6 …" + gövde devamı ya da çizim kod bloğu +
                # "Symbols : a = input value, …" + "Fig. 9.1 …" + gövde
                # devamı. Ara sayılmaz.
                ilk = p - len(ara) - 1
                if not (ilk >= 1
                        and self.bloklar[ilk - 1].tur in ("gorsel", "kod")):
                    return True
        if self._ic_altyazi_mi(p):
            return True
        return False

    def _sayfa_no_ust_bilgi_mi(self, satir: str) -> bool:
        """Tek başına satır sayfa numarası + belgede bilinen üst bilgi/başlık
        metni mi ("4 1 Introduction", "Preface ix"; G1b-2)? Yinelenme sayısı
        aranmaz: sayfa numarası ile başlık metninin birlikteliği üst bilgi
        kanıtıdır (bir test kitabında "N 1 Introduction" iki kez tek
        başına, bir kez gövdeye yapışık geçiyor; yapışık olan ayrılınca
        sayım değişiyor, onar eşgüçlü olmuyordu)."""
        return self._ust_bilgi_coz(satir) is not None

    def _ust_bilgi_coz(self, satir: str) -> Optional[Tuple[str, int, bool]]:
        """"Sayfa no + bilinen üst bilgi/başlık metni" satırının (ad anahtarı,
        sayfa numarası, Roma rakamı mı) üçlüsü; değilse None (bkz.
        _sayfa_no_ust_bilgi_mi)."""
        bilinen = getattr(self, "bilinen_ust", None)
        t = satir.strip()
        if not bilinen or len(t) > UST_BILGI_UZUNLUK_AZAMI or t[-1:] in _CUMLE_SONU:
            return None
        sozler = t.split()
        if len(sozler) < 2 or len(sozler) > YAPISIK_UST_SOZCUK_AZAMI + 1:
            return None
        no = r"\d{1,%d}" % SAYFA_NO_BASAMAK_AZAMI
        if re.fullmatch(no, sozler[0]):
            ad, deger, roma = " ".join(sozler[1:]), int(sozler[0]), False
        elif _SON_SAYFA_NO_RE.search(t):
            ad, son = " ".join(sozler[:-1]), sozler[-1]
            roma = not son.isdigit()
            deger = _roma_degeri(son) if roma else int(son)
            if deger is None:
                return None
        else:
            return None
        ilk = _ilk_gorunur(ad)
        k = _norm_ust_bilgi(ad)
        if ilk.isalnum() and not ilk.islower() and k in bilinen:
            return k, deger, roma
        return None

    def _kesin_ust_bilgi(self, satir: str) -> Optional[Tuple[str, int, bool]]:
        """KARAR 32: üst bilgi satırı adı belgenin bir BAŞLIĞIYLA eşleşiyorsa
        (kesin tanınan) _ust_bilgi_coz üçlüsü; değilse None."""
        c = self._ust_bilgi_coz(satir)
        return c if c and c[0] in getattr(self, "baslik_ust", ()) else None

    def _ust_bilgi_silindi(self, metin: str) -> None:
        self._kaydet("ust_bilgi_silindi", deger=metin.strip())

    def _ic_altyazi_mi(self, p: int) -> bool:
        """p. blok, görselin hemen bitişiğinde, etiketi ortada büyük harfle
        duran bozuk altyazı mı (sentetik: "Cutaway view of … under a given
        FIGURE 2.2 load and speed")? Kısa, tek
        satır, bitişik blok görsel."""
        b = self.bloklar[p]
        if b.tur != "paragraf" or len(b.satirlar) != 1:
            return False
        if len(b.metin) > IC_ALTYAZI_UZUNLUK_AZAMI or not _IC_ALTYAZI_RE.search(b.metin):
            return False
        komsu = [self.bloklar[k] for k in (p - 1, p + 1) if 0 <= k < len(self.bloklar)]
        return any(k.tur == "gorsel" for k in komsu)

    def _ara_bloklar(self, p: int) -> Tuple[List[int], Optional[int]]:
        """p'den sonraki ara bloklar ve ardındaki ilk ara olmayan blok."""
        ara: List[int] = []
        q = p + 1
        while q < len(self.bloklar) and len(ara) <= ARA_BLOK_AZAMI:
            if self._ara_mi(q, ara):
                ara.append(q)
                q += 1
                continue
            if ara and not self.bloklar[q].bos_once:
                # Ara bloğa boş satırsız yapışık blok: dipnot tanımının ya
                # da altyazının devam satırı olabilir; gövde yarısı sayılmaz.
                # İstisna 1: dipnot tanımına yapışık, küçük harfle süren liste
                # maddesi ("<sup>44</sup>Later…\n- returned its value…"):
                # tanımın devamı madde imiyle başlamaz.
                # İstisna 2: KESİN cümle sonuyla kapanan dipnot tanımına
                # yapışık, küçük harfle başlayan paragraf (örnek:
                # "<sup>12</sup> … through the archive.\nshowed great
                # interest …"): kapanmış tanımın devamı küçük harfle
                # başlamaz; gövde cümlesinin devamıdır. Noktasız biten
                # tanımda yapışık satır tanımın kendi devamı olabilir.
                c = self.bloklar[q]
                onceki = self.bloklar[ara[-1]]
                liste_devami = (c.tur == "liste" and onceki.tur == "dipnot"
                                and _devam_bicimli(self._ilk_metin(c)))
                paragraf_devami = (c.tur == "paragraf" and onceki.tur == "dipnot"
                                   and _kesin_kapali(onceki.satirlar[-1])
                                   and _devam_bicimli(c.satirlar[0]))
                if not (liste_devami or paragraf_devami):
                    return ara, None
            return ara, q
        return ara, None

    def _kaydet(self, tur: str, **bilgi: str) -> None:
        self.sayac[tur] = self.sayac.get(tur, 0) + 1
        d = {"tur": tur}
        d.update(bilgi)
        self.ayrinti.append(d)

    def _tasi_ardina(self, hedef: int, ara: List[int], silinecek: List[int]) -> None:
        """Ara blokları *hedef* bloğun ardına taşı, *silinecek*leri at.

        KARAR 32: ara bloklardaki kesin tanınan sayfa üst/alt bilgisi
        ("4 1 Introduction"; birleşen iki yarının arasında olması sayfa
        sınırı kanıtıdır) taşınmaz, silinir ve sayılır."""
        ust = [k for k in ara if self.bloklar[k].tur == "paragraf"
               and len(self.bloklar[k].satirlar) == 1
               and self._kesin_ust_bilgi(self.bloklar[k].satirlar[0])]
        for k in ust:
            self._ust_bilgi_silindi(self.bloklar[k].satirlar[0])
        ara = [k for k in ara if k not in ust]
        silinecek = list(silinecek) + ust
        tasinan = [self.bloklar[k] for k in ara]
        for b in tasinan:
            if not b.bos_once:
                b.bos_once = [""]
        yeni: List[_Blok] = []
        at = set(ara) | set(silinecek)
        for k, b in enumerate(self.bloklar):
            if k in at:
                continue
            yeni.append(b)
            if k == hedef:
                yeni.extend(tasinan)
        # Silinen bloğun ardındaki blok boş satırsız kalmasın.
        self.bloklar = yeni
        if ara:
            self.sayac["ara_blok_tasindi"] += len(ara)

    # -- kalem 6: "- **- Metin**" ------------------------------------------
    def tire_duzelt(self) -> None:
        for b in self.bloklar:
            if b.tur != "liste":
                continue
            self._duz_tire_imi(b)
            for k, s in enumerate(b.satirlar):
                m = _IMLI_RE.match(s) or _SIRALI_RE.match(s)
                if not m:
                    continue
                govde = m.group(4)
                y = _TIRE_YALNIZ_RE.sub("", govde, count=1)
                if y == govde:
                    y = _TIRE_KALIN_RE.sub(lambda mm: mm.group(1), govde, count=1)
                if y != govde:
                    b.satirlar[k] = s[:m.start(4)] + y
                    self._kaydet("tire_silindi", once=s[:80], sonra=b.satirlar[k][:80])

    def _duz_tire_imi(self, b: _Blok) -> None:
        """Bloktaki bütün maddeler kalınsız tire imiyle başlıyorsa imi sil
        (bkz. _TIRE_DUZ_RE)."""
        maddeler = []
        for k, s in enumerate(b.satirlar):
            m = _IMLI_RE.match(s) or _SIRALI_RE.match(s)
            if m:
                maddeler.append((k, m))
        if not maddeler or not all(_TIRE_DUZ_RE.match(m.group(4)) for _, m in maddeler):
            return
        for k, m in maddeler:
            s = b.satirlar[k]
            b.satirlar[k] = s[:m.start(4)] + _TIRE_DUZ_RE.sub("", m.group(4), count=1)
            self._kaydet("tire_silindi", once=s[:80], sonra=b.satirlar[k][:80])

    # -- kalem 1: bölünmüş paragraf / madde --------------------------------
    def _sozluk_kur(self) -> None:
        """Belgenin sözcük sıklığı (devam parçasının gerçek sözcük olduğunu
        denetlemek için: "tion across …" gibi kopuk hece birleştirilmez)."""
        self.sozluk: Dict[str, int] = {}
        self.tireli: Dict[str, int] = {}
        # Belgede küçük harfle geçen sözcükler: büyük harfle başlayan satırın
        # ilk sözcüğü bunlardan biriyse büyük harf cümle başı içindir; değilse
        # özel addır ("Fourier analysis …") ve cümle ortası olabilir.
        self.kucuk_sozcukler: set = set()
        for b in self.bloklar:
            if b.tur in ("kod", "denklem", "gorsel"):
                continue
            # Yalnız tek başına sözcük ("semi-markov" içindeki "markov"
            # sayılmaz: özel ad bileşikte küçük yazılabilir).
            self.kucuk_sozcukler.update(
                w for w in _TEKIL_SOZCUK_RE.findall(b.metin) if w.islower())
            for w in re.findall(r"[^\W\d_]+", b.metin):
                w = w.casefold()
                self.sozluk[w] = self.sozluk.get(w, 0) + 1
            # Tireli bileşikler ("exposure-response"): satır sonu tiresi
            # yalnız bileşik biçim belgede hiç geçmiyorsa atılır.
            for m in _TIRELI_BILESIK_RE.finditer(b.metin):
                w = (m.group(1) + "-" + m.group(2)).casefold()
                self.tireli[w] = self.tireli.get(w, 0) + 1
        self.sirali_sozcukler: List[str] = sorted(self.sozluk)
        self.duz_metin = "\n".join(b.metin for b in self.bloklar
                                    if b.tur not in ("kod", "denklem", "gorsel"))
        # Üst bilgi olabilecek metinler: tek başına yinelenen satırlar ve
        # başlıklar (üst bilgi bölüm/kısım adıdır: "### **Preface**").
        self.bilinen_ust = set(self.ust_bilgi)
        # KARAR 32: silme için yalnız BAŞLIKTAN bilinen ad "kesin tanınır"
        # (başlıklar silinmez: ikinci çağrı aynı kümeyi görür, eşgüçlü).
        self.baslik_ust: set = set()
        for b in self.bloklar:
            if b.tur == "baslik":
                t = re.sub(r"[*_#]", "", re.sub(r"<[^>]+>", "", b.satirlar[0])).strip()
                # Üst bilgi bölüm etiketini taşımayabilir: başlık "Chapter 1
                # Introduction", üst bilgi "1 Introduction" (G1b-2).
                # Etiketsiz biçim de bilinir.
                for aday in (t, _BOLUM_ETIKETI_RE.sub("", t, count=1)):
                    k = _norm_ust_bilgi(aday)
                    if len(k) >= 4:
                        self.bilinen_ust.add(k)
                        self.baslik_ust.add(k)

    def _baslik_gibi(self, metin: str) -> bool:
        """Kısa ve sözcüklerinin çoğu büyük harfle başlayan satır (başlık,
        tablo/şekil adı): noktasız bitmesi olağandır, devamı yoktur."""
        sozler = re.findall(r"[^\W\d_]{4,}", metin)
        if not sozler or len(re.findall(r"\S+", metin)) > 15:
            return False
        buyuk = sum(1 for w in sozler if w[0].isupper())
        return buyuk / len(sozler) >= 0.6

    @staticmethod
    def _ifade_gibi(metin: str) -> bool:
        """İçinde hiç cümle noktalaması olmayan kısa satır (liste benzeri)."""
        t = metin.strip()
        return len(t) < 100 and not any(ch in _CUMLE_SONU for ch in t)

    def _ilk_yari_uygun(self, b: _Blok, p: Optional[int] = None) -> bool:
        if b.tur == "paragraf":
            if p is not None and self._ic_altyazi_mi(p):
                return False       # görselin bozuk altyazısı: gövde yarısı değil
            son = b.satirlar[-1]
            if not _acik_uclu(son) or _SAYI_RE.match(son):
                return False
            if _sozcuk_sayisi(b.metin) < ILK_YARI_SOZCUK_ASGARI:
                return False
            if _SIMGE_BAS_RE.match(b.satirlar[0].lstrip()) \
                    or _TANIM_SATIRI_RE.match(b.satirlar[0]):
                return False       # değişken tanımı satırı
            if self._baslik_gibi(b.metin):
                return False
            return True
        if b.tur == "liste":
            son = b.satirlar[-1]
            return (_acik_uclu(son)
                    and _sozcuk_sayisi(self._son_madde_metni(b))
                    >= MADDE_ILK_YARI_SOZCUK_ASGARI)
        return False

    @staticmethod
    def _son_madde_metni(b: _Blok) -> str:
        k = len(b.satirlar) - 1
        while k > 0 and not _madde_mi(b.satirlar[k]):
            k -= 1
        return " ".join(s.strip() for s in b.satirlar[k:])

    def _sol_hazirla(self, sol: str) -> Tuple[Optional[str], Optional[str]]:
        """Sol yarının sonuna yapışmış sayfa üst bilgisini ayıkla.

        Marker bazen sayfanın üst bilgisini ("Preface ix", "References 423")
        önceki sayfanın son gövde satırının sonuna ekler (örnek: "…
        and then revisit Preface ix" + "methods, possible …"). Dönüş:
        (sol, None) yapışık üst bilgi yok; (gövde, üst bilgi) üst bilgi
        belgede bilinen bir üst bilgi/başlık metni; (None, None) büyük harfli
        sözcük + sayfa numarası ile bitiyor ama üst bilgi olduğu
        doğrulanamıyor ("… the Discussion 421"): birleştirilmez.
        """
        t = sol.rstrip()
        m = _SON_SAYFA_NO_RE.search(t)
        if not m:
            return self._sol_hazirla_no_once(sol)
        once = t[:m.start()].rstrip()
        sozler = once.split()
        if not sozler:
            return sol, None
        ilk = _ilk_gorunur(sozler[-1])
        if not (ilk.isalpha() and ilk.isupper()):
            return sol, None          # "near 2", "в 1936": sıradan sayı
        for k in range(min(YAPISIK_UST_SOZCUK_AZAMI, len(sozler) - 1), 0, -1):
            aday = " ".join(sozler[-k:])
            if _norm_ust_bilgi(aday) in self.bilinen_ust:
                govde = once[:len(once) - len(aday)].rstrip()
                if govde and _acik_uclu(govde):
                    return govde, aday + " " + m.group(1)
                return None, None
        return None, None

    def _sol_hazirla_no_once(self, sol: str) -> Tuple[Optional[str], Optional[str]]:
        """Sayfa numarası ÖNDE, üst bilgi sonda yapışmış ("… In the 6 1
        Introduction" + "next part we …"; G1b-2): son sözcükler sayı +
        belgede bilinen üst bilgi/başlık
        metniyse ("1 Introduction" -> "introduction") ve kalan gövde açık
        uçluysa (gövde, üst bilgi) döner. Bilinen üst bilgi yoksa (sol, None):
        "… in 1936 Vienna" sıradan metindir, eski davranış korunur.
        """
        t = sol.rstrip()
        sozler = t.split()
        for k in range(min(YAPISIK_UST_SOZCUK_AZAMI + 1, len(sozler) - 1), 1, -1):
            aday = sozler[-k:]
            if not re.fullmatch(r"\d{1,%d}" % SAYFA_NO_BASAMAK_AZAMI, aday[0]):
                continue
            ad = " ".join(aday[1:])
            ilk = _ilk_gorunur(ad)
            if not (ilk.isalnum() and not ilk.islower()):
                continue
            if _norm_ust_bilgi(ad) not in self.bilinen_ust:
                continue
            ust = " ".join(aday)
            govde = t[:len(t) - len(ust)].rstrip()
            if govde and _acik_uclu(govde):
                return govde, ust
            return None, None
        return sol, None

    @staticmethod
    def _blok_ic_metni(b: _Blok) -> str:
        """Bloğun (liste için son maddenin, dipnot için tanımın) işaretsiz
        metni."""
        if b.tur == "liste":
            k = len(b.satirlar) - 1
            while k > 0 and not _madde_mi(b.satirlar[k]):
                k -= 1
            m = _IMLI_RE.match(b.satirlar[k]) or _SIRALI_RE.match(b.satirlar[k])
            ilk = m.group(4) if m else b.satirlar[k]
            return " ".join([ilk.strip()] + [x.strip() for x in b.satirlar[k + 1:]])
        if b.tur == "dipnot":
            m = _DIPNOT_TANIM_RE.match(b.satirlar[0])
            ilk = m.group(1) if m else b.satirlar[0]
            return " ".join([ilk.strip()] + [x.strip() for x in b.satirlar[1:]])
        return b.metin

    def _devam_uygun(self, sol_blok: _Blok, sol: str, sag: str) -> bool:
        """*sag* metni *sol*un (açık uçlu) devamı olabilir mi?"""
        v_son = _vurgu_isareti_son(sol)
        if v_son and _vurgu_isareti_bas(sag) != v_son \
                and self._blok_ic_metni(sol_blok).lstrip().startswith(v_son):
            # Baştan sona vurgulu blok (alıntı, şekildeki metin) sayfa
            # sonunda kesilmiş; devamı da aynı vurguyla başlamalı. Vurgusuz
            # küçük harfli parça başka bir paragrafın artığıdır (örnek:
            # "*"Unlike … the old methods of Pa*" + "seiner eigenen
            # Zeit."): birleştirilmez.
            return False
        if _vurgulu_kod_devami(sol, sag):
            # Vurgulu alıntı kod parçasıyla sürüyor ("*… values like*" +
            # "*`abcKOD`. We could …*"): iki yarı aynı
            # vurguyla sarılı, devam `kod` ile başlıyor.
            return not (_lejant_metni_mi(sag) or _lejant_metni_mi(sol))
        if not _devam_bicimli(sag) or _TANIM_SATIRI_RE.match(sag):
            return False
        if _ALTYAZI_RE.match(sag) or _lejant_metni_mi(sag) or _lejant_metni_mi(sol):
            return False
        if _altyazi_satiri_mi(sol):
            # Altyazının devamı da altyazı biçiminde olmalı ("*…*" + "*…*").
            v = _vurgu_isareti_bas(sol)
            if not v or _vurgu_isareti_bas(sag) != v:
                return False
        if self._ifade_gibi(sol_blok.metin) and self._ifade_gibi(sag):
            return False
        # Devam parçasının ilk sözcüğü belgede başka yerde de geçmeli ya da
        # sistem sözlüğünde bulunmalı (kopuk hece değil: "tion across …").
        # Heceleme tiresinde ("pos-" + "sibilities") parça zaten hecedir.
        if not _TIRE_SON_RE.search(sol.rstrip()):
            m = re.match(r"[^\W\d_]+", _ilk_sozcuk_metni(sag))
            if not m:
                return False
            w = m.group(0).casefold()
            if (self.sozluk.get(w, 0) < DEVAM_SOZCUK_TEKRAR_ASGARI
                    and not _sozlukte_mi(w) and not self._koku_geciyor(w)):
                return False
        return True

    def _koku_geciyor(self, w: str) -> bool:
        """Seyrek sözcüğün kökü belgede başka bir sözcüğün başı mı? (bkz.
        SEYREK_SOZCUK_ASGARI). Sistem sözlüğü yalnız İngilizce; öteki
        dillerde çekimli biçimler çoğunlukla belgede bir kez geçer."""
        if len(w) < SEYREK_SOZCUK_ASGARI:
            return False
        kok = w[:max(KOK_ASGARI, len(w) - KOK_KESME)]
        i = bisect.bisect_left(self.sirali_sozcukler, kok)
        while i < len(self.sirali_sozcukler) and self.sirali_sozcukler[i].startswith(kok):
            if self.sirali_sozcukler[i] != w:
                return True
            i += 1
        return False

    def _birlestir_metin(self, sol: str, sag: str) -> Tuple[str, str]:
        """(birleşik metin, yöntem). Heceleme tiresi: sözcüğün tiresiz biçimi
        belgede geçiyor ve tireli bileşik biçimi ("exposure-response";
        bir test kitabında 124 kez) hiç geçmiyorsa tire atılır; yoksa tire
        korunur (boşluksuz). Emin olunmayan durumda tire kalır: gerçek
        bileşiğin tiresini düşürmek metin kaybıdır."""
        m1 = _TIRE_SON_RE.search(sol.rstrip())
        m2 = re.match(r"[^\W\d_]+", _ilk_sozcuk_metni(sag))
        if m1 and m2:
            govde = sol.rstrip()[:m1.start() + len(m1.group(1))]
            bitisik = (m1.group(1) + m2.group(0)).casefold()
            tireli = (m1.group(1) + "-" + m2.group(0)).casefold()
            sag_t = sag.lstrip()
            if self.sozluk.get(bitisik, 0) >= 1 and self.tireli.get(tireli, 0) == 0:
                return govde + sag_t, "hece_tiresiz"
            return govde + "-" + sag_t, "hece_tireli"
        return _metin_ekle(sol, sag), "bosluk"

    def _yapisik_altyazi_ayir(self, p: int) -> bool:
        """Paragrafın son satırı ona boş satırsız yapışmış şekil altyazısıysa
        ve ondan önceki satır açık uçluysa, altyazı satırını ayrı paragraf
        yap (bölünmüş gövde cümlesi altyazının ötesinde sürer). Yalnız
        ardından bir birleştirme yapılacaksa çağrılır.
        """
        b = self.bloklar[p]
        if b.tur != "paragraf" or len(b.satirlar) < 2:
            return False
        if not _altyazi_satiri_mi(b.satirlar[-1]) or not _acik_uclu(b.satirlar[-2]):
            return False
        yeni = _Blok("paragraf", [b.satirlar[-1]], [""])
        b.satirlar = b.satirlar[:-1]
        self.bloklar.insert(p + 1, yeni)
        return True

    @staticmethod
    def _ara_bloksuz_madde(b: _Blok, c: Optional[_Blok], sag: str) -> bool:
        """Ara bloksuz (yalnız boş satırla ayrılmış) madde devamı kanıtı.

        Marker kimi kitapta sayfa üst bilgisini ve numarasını düşürür; sayfa
        kırılımının tek izi boş satırdır (bir test kitabında: 36 bölünmüş maddenin 35'i,
        PDF'te arada yalnız üst bilgi + sayfa numarası). Boş satır tek başına
        kanıt değildir (liste ile ardındaki paragraf arasında hep vardır);
        kabul için hepsi gerekir:

        1. bölünen madde ve aynı düzeydeki öteki maddeler (b'de ve c liste
           ise c'nin ilk maddesinden sonrakiler) küçük harfle başlamıyor:
           küçük harfli devam parçası bu listede aykırıdır;
        2. liste cümle biçimli: aynı düzeydeki öteki maddelerden biri cümle
           sonuyla bitiyor, bölünen madde kendi içinde bir cümle bitiriyor
           ya da en az CUMLE_MADDE_SOZCUK_ASGARI sözcük. Noktasız ifade
           listesinde ("- Self-supervised pretraining on raw text") devam
           parçası listeye giren cümlenin devamı da olabilir ("which we
           compare …");
        3. devam parçası bir cümle sonu içeriyor ("such that" + denklem:
           maddenin mi cümlenin mi devamı belli değil);
        4. devam parçası ad önekli kaynakça girdisi değil ("van Dam K
           (2012) …") ve iki yarı birlikte yıl taşıyan kaynakça girdisi değil;
        5. c liste ise ilk maddesi bölünen maddeden daha girintili değil
           (daha girintili madde alt maddedir; Marker sayfa kırılımında
           girintiyi kaybedebilir, kazanmaz).
        """
        ogeler = _madde_ogeleri(b)
        if not ogeler:
            return False
        bolunen = ogeler[-1]
        duzey = bolunen[2]
        kardes = [o[3] for o in ogeler[:-1] if o[2] == duzey]
        if c is not None:
            oc = _madde_ogeleri(c)
            if not oc or oc[0][2] > duzey or not c.bos_once:
                return False
            kardes += [o[3] for o in oc[1:] if o[2] == oc[0][2]]
        if _kucuk_baslar(bolunen[3]) or any(_kucuk_baslar(t) for t in kardes):
            return False
        if not (_ic_cumle_sonu(bolunen[3])
                or _sozcuk_sayisi(bolunen[3]) >= CUMLE_MADDE_SOZCUK_ASGARI
                or any(_cumle_sonu_ile_biter(t) for t in kardes)):
            return False
        if not (_ic_cumle_sonu(sag) or _cumle_sonu_ile_biter(sag, iki_nokta=True)):
            return False
        if _ad_onekiyle_basliyor(sag):
            return False
        if _YIL_RE.search(bolunen[3][:KAYNAKCA_YIL_PENCERE]) \
                and _YIL_RE.search(sag[:KAYNAKCA_YIL_PENCERE]):
            return False
        return True

    def paragraf_birlestir(self) -> None:
        self._sozluk_kur()
        p = 0
        while p < len(self.bloklar):
            b = self.bloklar[p]
            if (b.tur == "paragraf" and len(b.satirlar) >= 2
                    and _altyazi_satiri_mi(b.satirlar[-1])
                    and _acik_uclu(b.satirlar[-2])):
                # Yapışık altyazı: önce birleştirilebilir mi diye bak.
                yedek = (list(b.satirlar), list(self.bloklar))
                self._yapisik_altyazi_ayir(p)
                if self._ilk_yari_uygun(b):
                    ara, q = self._ara_bloklar(p)
                    c = self.bloklar[q] if q is not None else None
                    if (c is not None and c.tur == "paragraf"
                            and self._devam_uygun(b, b.satirlar[-1], c.satirlar[0])):
                        self._kaydet("yapisik_altyazi_ayrildi",
                                     altyazi=self.bloklar[p + 1].satirlar[0][:80])
                    else:
                        b.satirlar, self.bloklar = yedek
                else:
                    b.satirlar, self.bloklar = yedek
            if not self._ilk_yari_uygun(b, p):
                p += 1
                continue
            ara, q = self._ara_bloklar(p)
            if q is None or len(ara) > ARA_BLOK_AZAMI:
                p += 1
                continue
            c = self.bloklar[q]
            once, ust = self._sol_hazirla(b.satirlar[-1])
            if once is None:
                # Büyük harfli sözcük + sayı ile bitiyor, yapışık üst bilgi
                # olabilir ama doğrulanamıyor: dokunulmaz.
                p += 1
                continue
            if c.tur == "paragraf" and b.tur == "paragraf" \
                    and self._devam_uygun(b, once, c.satirlar[0]):
                b.satirlar[-1], yontem = self._birlestir_metin(once, c.satirlar[0])
                b.satirlar.extend(c.satirlar[1:])
                self._kaydet("paragraf_birlesti", sol=once[-120:],
                             sag=c.satirlar[0][:120], yontem=yontem, ara=",".join(
                                 self.bloklar[k].tur for k in ara))
                self._tasi_ardina(p, ara, [q])
                self._ust_bilgi_ekle(p, ust)
                continue          # birleşen paragraf yine açık uçlu olabilir
            if c.tur == "liste" and b.tur == "liste":
                # Kırılım izi: ara blok (dipnot, sayfa no, görsel …) ya da
                # ara bloksuz durumda cümle biçimli listede aykırı küçük
                # harfli madde (bkz. _ara_bloksuz_madde; G1b-2).
                m = _IMLI_RE.match(c.satirlar[0]) or _SIRALI_RE.match(c.satirlar[0])
                govde = m.group(4) if m else ""
                if (m and _IMLI_RE.match(c.satirlar[0])
                        and not _MADDE_NO_RE.match(govde)
                        and (ara or self._ara_bloksuz_madde(b, c, govde))
                        and self._devam_uygun(b, once, govde)):
                    b.satirlar[-1], yontem = self._birlestir_metin(once, govde)
                    b.satirlar.extend(c.satirlar[1:])
                    self._kaydet("madde_birlesti", sol=once[-120:], sag=govde[:120],
                                 yontem=yontem,
                                 ara=",".join(self.bloklar[k].tur for k in ara))
                    self._tasi_ardina(p, ara, [q])
                    self._ust_bilgi_ekle(p, ust)
                    continue
            if (c.tur == "paragraf" and b.tur == "liste"
                    and len(c.satirlar) == 1
                    and (ara or self._ara_bloksuz_madde(b, None, c.satirlar[0]))
                    and self._devam_uygun(b, once, c.satirlar[0])):
                # Maddenin devamı sayfa kırılımından sonra düz paragraf
                # olmuş: paragraf maddenin satırına katılır.
                # Kırılım izi (ara blok) ya da ara bloksuz kanıt şarttır
                # (örnek: "… to reduce the water tank" + "volume. Copper …";
                # liste ile paragraf arasındaki boş
                # satır tek başına iz değildir).
                b.satirlar[-1], yontem = self._birlestir_metin(once, c.satirlar[0])
                self._kaydet("madde_birlesti", sol=once[-120:],
                             sag=c.satirlar[0][:120], yontem=yontem,
                             ara=",".join(self.bloklar[k].tur for k in ara))
                self._tasi_ardina(p, ara, [q])
                self._ust_bilgi_ekle(p, ust)
                continue
            if b.tur == "paragraf" and c.tur == "paragraf" and ust is None \
                    and not _devam_bicimli(c.satirlar[0]):
                if self._kayik_devam(p, ara, q):
                    continue
            p += 1

    def _ust_bilgi_ekle(self, p: int, ust: Optional[str]) -> None:
        """Birleşen yarıdan ayıklanan üst bilgiyi (varsa) birleşik bloğun
        hemen ardına ayrı paragraf olarak koy (metin düşmez, yalnız yer
        değiştirir). KARAR 32: adı belgenin bir başlığıyla eşleşen üst bilgi
        (kesin tanınan) konmaz, silinir ve sayılır."""
        if not ust:
            return
        self._kaydet("yapisik_ust_bilgi_ayrildi", ust=ust)
        if self._kesin_ust_bilgi(ust):
            # KARAR 32: başlıkla eşleşen üst bilgi sayfa sınırında (yarım
            # cümlenin birleştiği yerde) duruyordu: silinir ve sayılır.
            self._ust_bilgi_silindi(ust)
            return
        self.bloklar.insert(p + 1, _Blok("paragraf", [ust], [""]))

    def _cumle_basi_gibi(self, metin: str) -> bool:
        """Satır, büyük harfi yalnız cümle başı olduğu için taşıyan sıradan
        bir sözcükle mi başlıyor ("Eventually", "When", "It")? Sözcüğün
        küçük harfli biçimi belgede geçmeli."""
        m = re.match(r"[^\W\d_]+", _ilk_sozcuk_metni(metin).lstrip("\"'“‘«(["))
        if not m:
            return False
        w = m.group(0)
        return w[:1].isupper() and w.lower() in self.kucuk_sozcukler

    def _kayik_devam(self, p: int, ara: List[int], q: int) -> bool:
        """Okuma sırası hatası: açık uçlu paragrafın devamı birkaç
        blok aşağıda. Koşullar: aradaki bütün metin paragrafları kapalı uçlu
        (başka yarım cümle yok) ve büyük harfle başlıyor, ilk sözcükleri
        belgede küçük harfle de geçiyor (özel ad değil; özel adla başlayan
        paragraf asıl devam olabilir, bkz. _cumle_basi_gibi); aday devam parçası
        küçük harfle başlıyor, sözcüğü belgede geçiyor ve sayfa kırılımı
        izinde (hemen önünde ya da ardında dipnot tanımı / sayfa numarası /
        görsel). Aranan alan başlık, liste, tablo, kod, denklem ve alıntıda
        biter.
        """
        b = self.bloklar[p]
        once = b.satirlar[-1]
        r = q
        sayac = 0
        while r < len(self.bloklar) and sayac < KAYIK_DEVAM_BLOK_AZAMI:
            c = self.bloklar[r]
            if c.tur in ("dipnot", "sayi", "gorsel") or _altyazi_mi(c):
                r += 1
                continue
            if c.tur != "paragraf":
                return False
            if _devam_bicimli(c.satirlar[0]):
                break
            if _acik_uclu(c.satirlar[-1]):
                return False
            if not self._cumle_basi_gibi(c.satirlar[0]):
                # Atlanacak paragraf özel adla başlıyor: açık uçlu cümlenin
                # asıl devamı olabilir ("… a simple scheme for" +
                # "Fourier analysis (FA); …").
                return False
            sayac += 1
            r += 1
        else:
            return False
        if r >= len(self.bloklar) or r == q:
            return False
        c = self.bloklar[r]
        if not self._devam_uygun(b, once, c.satirlar[0]):
            return False
        komsu = [self.bloklar[k] for k in (r - 1, r + 1) if 0 <= k < len(self.bloklar)]
        if not any(k.tur in ("dipnot", "sayi", "gorsel") for k in komsu) \
                and not self._hece_birlesimi_gecerli(once, c.satirlar[0]):
            # Sayfa kırılımı izi yok: yalnız heceyle bölünmüş sözcükte kabul
            # (iki sütunlu bir Almanca kitapta: "…des großen Flug-" + başka
            # sütunun paragrafı + "hafens, seiner …").
            return False
        b.satirlar[-1], yontem = self._birlestir_metin(once, c.satirlar[0])
        b.satirlar.extend(c.satirlar[1:])
        self._kaydet("kayik_devam_birlesti", sol=once[-120:],
                     sag=c.satirlar[0][:120], yontem=yontem,
                     atlanan=str(sayac))
        del self.bloklar[r]
        return True

    def _hece_birlesimi_gecerli(self, sol: str, sag: str) -> bool:
        """Sol yarı heceleme tiresiyle bitiyor ("Kon-") ve birleşik sözcük
        ("kontinents") belgede, sistem sözlüğünde ya da kökü belgede geçiyor
        mu?"""
        m1 = _TIRE_SON_RE.search(sol.rstrip())
        m2 = re.match(r"[^\W\d_]+", _ilk_sozcuk_metni(sag))
        if not (m1 and m2):
            return False
        w = (m1.group(1) + m2.group(0)).casefold()
        return (self.sozluk.get(w, 0) >= 1 or _sozlukte_mi(w)
                or self._koku_geciyor(w))

    # -- kalem 1 (dipnot): sayfalar arası bölünmüş dipnot tanımı -----------
    def dipnot_devam_birlestir(self) -> None:
        """Sayfa sonunda yarıda kalan dipnot tanımının devamını tanıma kat.

        PDF'te uzun dipnot sonraki sayfanın dipnot alanında sürer; Marker bu
        devamı sonraki sayfanın gövdesinden SONRA, o sayfanın dipnot
        tanımlarından hemen önce ayrı paragraf olarak yazar (sentetik:
        "…was flagged by a senior" … "reviewer as urgent and …").
        Gövdede kalan parça okura ana metin gibi görünür.

        Koşullar (hepsi): tanım öbeğinde (art arda dipnot/sayfa numarası
        blokları) açık uçlu TEK tanım var; öbekten sonraki ilk dipnot
        alanına kadar en çok DIPNOT_DEVAM_BLOK_AZAMI blok ve arada "# "
        bölüm başlığı yok; o alanın hemen önündeki blok (aday) tek satırlık,
        devam biçimli paragraf ve belgede/sözlükte geçen sözcükle başlıyor;
        öbekle aday arasındaki bütün paragraf ve maddeler kapalı uçlu (aday
        başka bir gövde cümlesinin devamı olamaz).
        """
        p = 0
        while p < len(self.bloklar):
            if self.bloklar[p].tur != "dipnot":
                p += 1
                continue
            r0 = p
            r1 = p
            while r1 + 1 < len(self.bloklar) \
                    and self.bloklar[r1 + 1].tur in ("dipnot", "sayi"):
                r1 += 1
            p = r1 + 1
            acik = [k for k in range(r0, r1 + 1)
                    if self.bloklar[k].tur == "dipnot"
                    and _acik_uclu(self.bloklar[k].satirlar[-1])]
            if len(acik) != 1:
                continue
            d = self.bloklar[acik[0]]
            m = _DIPNOT_TANIM_RE.match(d.satirlar[0])
            if not m or _sozcuk_sayisi(m.group(1)) < ILK_YARI_SOZCUK_ASGARI:
                continue
            # Sonraki dipnot alanı.
            z = r1 + 1
            while z < len(self.bloklar) and z - r1 <= DIPNOT_DEVAM_BLOK_AZAMI \
                    and self.bloklar[z].tur != "dipnot":
                if self.bloklar[z].tur == "baslik" \
                        and re.match(r"^\s*#\s", self.bloklar[z].satirlar[0]):
                    break
                z += 1
            if z >= len(self.bloklar) or self.bloklar[z].tur != "dipnot":
                continue
            a = z - 1
            while a > r1 and self.bloklar[a].tur == "sayi":
                a -= 1
            if a <= r1:
                continue
            c = self.bloklar[a]
            if c.tur != "paragraf" or len(c.satirlar) != 1 or not c.bos_once:
                continue
            ara_govde = self.bloklar[r1 + 1:a]
            if any(x.tur in ("paragraf", "liste") and _acik_uclu(x.satirlar[-1])
                   for x in ara_govde):
                continue
            sol, ust = self._sol_hazirla(d.satirlar[-1])
            if sol is None or ust is not None:
                continue
            if not self._devam_uygun(d, sol, c.satirlar[0]):
                continue
            d.satirlar[-1], yontem = self._birlestir_metin(sol, c.satirlar[0])
            self._kaydet("dipnot_devam_birlesti", sol=sol[-120:],
                         sag=c.satirlar[0][:120], yontem=yontem,
                         atlanan=str(len(ara_govde)))
            del self.bloklar[a]
            p = a

    # -- kalem 4: bölünmüş liste -------------------------------------------
    @staticmethod
    def _madde_bilgisi(s: str) -> Optional[Tuple[str, str, Optional[int]]]:
        """(girinti, tür, numara): tür 'imli-no' / 'imli' / 'sirali'."""
        m = _IMLI_RE.match(s)
        if m:
            nm = _MADDE_NO_RE.match(m.group(4))
            if nm:
                return m.group(1), "imli-no", int(nm.group(1))
            return m.group(1), "imli", None
        m = _SIRALI_RE.match(s)
        if m:
            return m.group(1), "sirali", int(m.group(2))
        return None

    def _son_madde(self, b: _Blok):
        for s in reversed(b.satirlar):
            bil = self._madde_bilgisi(s)
            if bil and bil[0] == self._madde_bilgisi(b.satirlar[0])[0]:
                return bil
        return None

    def liste_birlestir(self) -> None:
        p = 0
        while p < len(self.bloklar):
            b = self.bloklar[p]
            if b.tur != "liste":
                p += 1
                continue
            ara, q = self._ara_bloklar(p)
            if (q is None or not ara
                    or any(self.bloklar[k].tur not in ("dipnot", "sayi")
                           for k in ara)
                    or self.bloklar[q].tur != "liste"):
                p += 1
                continue
            c = self.bloklar[q]
            son = self._son_madde(b)
            ilk = self._madde_bilgisi(c.satirlar[0])
            # Numaralı listenin imi sayfa kırılımında değişebilir ("- 2." ->
            # "3."; G1b-2): iki numaralı tür uyumludur, im sonra
            # liste_imi_birlestir'de eşitlenir.
            if (son is None or ilk is None or son[0] != ilk[0]
                    or (son[1] != ilk[1]
                        and {son[1], ilk[1]} != {"imli-no", "sirali"})):
                p += 1
                continue
            if son[1] in ("imli-no", "sirali"):
                if ilk[2] != son[2] + 1:
                    p += 1
                    continue
            elif _acik_uclu(b.satirlar[-1]) and _devam_bicimli(self._ilk_metin(c)):
                # Noktasız biten imli madde + küçük harfli madde: devamı mı
                # yeni madde mi belli değil (devam birleştirmesi
                # paragraf_birlestir'de; orada reddedildiyse dokunulmaz).
                # Büyük harfle başlayan madde yeni maddedir: iki parça tek
                # liste olur, madde metinleri değişmez.
                p += 1
                continue
            if len(c.satirlar) > LISTE_TASIMA_SATIR_AZAMI:
                p += 1
                continue
            c.bos_once = []
            self._kaydet("liste_birlesti", sol=b.satirlar[-1][:60],
                         sag=c.satirlar[0][:60],
                         ara=",".join(self.bloklar[k].tur for k in ara))
            b.satirlar.extend(c.satirlar)
            self._tasi_ardina(p, ara, [q])
            # p aynı kalır: birleşen liste yine bölünmüş olabilir.

    # -- G1b-2: numaralı liste imi sayfa kırılımında değişmiş ----------------
    def _madde_zincirleri(self) -> List[List[Tuple[int, int, str, int]]]:
        """Ardışık numaralı madde zincirleri: [(blok, satır, tür, numara)].

        Zincir aynı girintide n, n+1, … numaralı maddelerden oluşur. Aynı
        blokta aralarında yalnız daha girintili maddeler ve devam satırları;
        bloklar arasında yalnız boş satır ya da dipnot/sayfa numarası ara
        blokları bulunabilir. Aynı ya da daha az girintili başka madde,
        paragraf, başlık … zinciri keser.
        """
        zincirler: List[List[Tuple[int, int, str, int]]] = []
        acik: Dict[int, List[Tuple[int, int, str, int]]] = {}
        for p, b in enumerate(self.bloklar):
            if b.tur in ("dipnot", "sayi"):
                continue
            if b.tur != "liste":
                zincirler.extend(acik.values())
                acik = {}
                continue
            for k, s in enumerate(b.satirlar):
                bil = self._madde_bilgisi(s)
                if bil is None:
                    continue
                g = len(bil[0])
                # Daha derin düzeylerin zincirleri bu maddeyle kapanır.
                for d in [d for d in acik if d > g]:
                    zincirler.append(acik.pop(d))
                z = acik.get(g)
                if bil[2] is not None and z and z[-1][3] + 1 == bil[2]:
                    z.append((p, k, bil[1], bil[2]))
                    continue
                if z:
                    zincirler.append(acik.pop(g))
                if bil[2] is not None:
                    acik[g] = [(p, k, bil[1], bil[2])]
        zincirler.extend(acik.values())
        return [z for z in zincirler if len(z) >= 2]

    def liste_imi_birlestir(self) -> None:
        """Numaralı listenin imi sayfa kırılımında değişmiş ("1." + "- 2.",
        "- 16." + "17." + "- 18."; üç test kitabında 4, 1 ve 12 olgu).

        Marker sayfanın ilk maddesini kimi zaman "N." ötekilerini "- N."
        yazar; dönüştürücü iki ayrı liste çıkarır: "1." tek maddelik numaralı
        liste (yeniden 1'den sayar), "- 2." … madde imli ve elle numaralı
        ("• 2."). Ardışık numaralı zincirde iki im birlikte varsa hepsi tek
        ime çekilir: zincir 1'den başlıyorsa çoğunluğun imi (eşitlikte ilk
        maddeninki), başlamıyorsa "- N." (numaralı listeye çevrilirse
        dönüştürücü 1'den sayar, numara yanlış basılır). Metin değişmez,
        yalnız "- " imi eklenir ya da atılır.
        """
        for z in self._madde_zincirleri():
            turler = [t for _, _, t, _ in z]
            if len(set(turler)) < 2:
                continue
            if z[0][3] == 1:
                n_imli = turler.count("imli-no")
                n_sirali = turler.count("sirali")
                if n_imli != n_sirali:
                    hedef = "imli-no" if n_imli > n_sirali else "sirali"
                else:
                    hedef = turler[0]
            else:
                hedef = "imli-no"
            degisen = 0
            for p, k, tur, _ in z:
                if tur == hedef:
                    continue
                b = self.bloklar[p]
                s = b.satirlar[k]
                if hedef == "imli-no":
                    m = _SIRALI_RE.match(s)
                    b.satirlar[k] = (m.group(1) + "- " + m.group(2) + "."
                                     + m.group(3) + m.group(4))
                else:
                    m = _IMLI_RE.match(s)
                    b.satirlar[k] = m.group(1) + m.group(4)
                degisen += 1
            ilk = self.bloklar[z[0][0]].satirlar[z[0][1]]
            self._kaydet("liste_imi_birlesti", hedef=hedef, satir=str(degisen),
                         ilk=ilk[:80])

    # -- G1b-2: iç içe liste ---------------------------------------------------
    def ic_madde_birlestir(self) -> None:
        """Etiketi ile içeriği ayrı maddeye düşmüş alt madde.

        "- **ad/soyad/yaş:**" + "  - ^ab(?:…)": kaynakta tek satırdır
        ("○ ad/soyad/yaş: ^ab(?:…"), içerik uzun olduğu için alt satıra
        kaymış, Marker bunu alt madde sanmış. Koşullar: madde yalnız kalın
        etiketten oluşuyor; hemen altında TEK alt madde var (devam satırsız,
        kalın etiketsiz, harfle başlamayan içerik: desen/kod/sayı); aynı
        düzeyde en az iki kardeş madde "**etiket:** içerik" biçiminde ve
        içerikleri de harfle başlamıyor. Gerçek alt liste (iki ve daha çok
        alt madde ya da düz cümle) dokunulmaz.
        """
        for b in self.bloklar:
            if b.tur != "liste":
                continue
            ogeler = _madde_ogeleri(b)
            birlesecek: List[Tuple[int, int]] = []
            for i, (k0, k1, g, metin) in enumerate(ogeler[:-1]):
                if k1 - k0 != 1 or not _ETIKET_YALNIZ_RE.match(metin):
                    continue
                cocuk = ogeler[i + 1]
                if cocuk[2] <= g or cocuk[1] - cocuk[0] != 1 or cocuk[0] != k1:
                    continue
                if i + 2 < len(ogeler) and ogeler[i + 2][2] > g:
                    continue                      # birden çok alt madde
                ic = cocuk[3]
                if not ic or ic[:1].isalpha() or ic.startswith(("*", "_")):
                    continue
                kardes = 0
                for j, o in enumerate(ogeler):
                    if j == i or o[2] != g:
                        continue
                    mm = _ETIKETLI_ICERIK_RE.match(o[3])
                    if mm and not mm.group(2)[:1].isalpha():
                        kardes += 1
                if kardes < 2:
                    continue
                birlesecek.append((k0, cocuk[0]))
            for k0, kc in reversed(birlesecek):
                ic = (_IMLI_RE.match(b.satirlar[kc]) or _SIRALI_RE.match(b.satirlar[kc])).group(4)
                b.satirlar[k0] = b.satirlar[k0].rstrip() + " " + ic.strip()
                del b.satirlar[kc]
                self._kaydet("ic_madde_birlesti", etiket=b.satirlar[k0][:80])

    def ic_liste_duzeyi_onar(self) -> None:
        """Sayfa kırılımında düzeyi kaybolmuş harfli alt madde dizisi.

        Örnek 1: "  - a." "  - b." "  - c." (ikinci düzey) +
        sayfa kırılımı + "- d." … "- k." (birinci düzeye düşmüş); örnek 2:
        "  - (a) …" + "- Reducing …" (a'nın ikinci paragrafı)
        + "- (b)" + "- (c)". Koşullar: liste bloğunun son maddesi girintili ve
        sıra etiketli; sonraki liste bloğu yalnız boş satır ya da dipnot/
        sayfa numarası ara bloklarıyla ayrılmış; onun başında en çok
        IC_DEVAM_PARAGRAF_AZAMI etiketsiz madde, ardından AYNI biçimde bir
        sonraki etiket ("(a)" -> "(b)") daha az girintiyle geliyor. Dizinin
        maddeleri (alt maddeleriyle) önceki düzeye taşınır, öndeki etiketsiz
        maddeler önceki alt maddenin girintili devam satırı olur; iki blok tek
        liste olur, ara bloklar ardına taşınır. Dizi sürmüyorsa dokunulmaz.
        """
        p = 0
        while p < len(self.bloklar):
            b = self.bloklar[p]
            if b.tur != "liste":
                p += 1
                continue
            ogeler = _madde_ogeleri(b)
            son = ogeler[-1] if ogeler else None
            etiket = _sira_etiketi(son[3]) if son and son[2] > 0 else None
            ara, q = self._ara_bloklar(p) if etiket else ([], None)
            if (q is None or self.bloklar[q].tur != "liste"
                    or any(self.bloklar[k].tur not in ("dipnot", "sayi") for k in ara)):
                p += 1
                continue
            c = self.bloklar[q]
            oc = _madde_ogeleri(c)
            ust = oc[0][2] if oc else 0
            j = 0
            while (j < len(oc) and j < IC_DEVAM_PARAGRAF_AZAMI and oc[j][2] == ust
                   and _sira_etiketi(oc[j][3]) is None
                   and self._madde_bilgisi(c.satirlar[oc[j][0]])[1] == "imli"):
                j += 1
            if (j >= len(oc) or oc[j][2] != ust or ust >= son[2]
                    or not _sonraki_etiket_mi(etiket, _sira_etiketi(oc[j][3]))):
                p += 1
                continue
            # Dizinin sonu: aynı düzeyde bir sonraki etiketi taşımayan ilk
            # madde; dizinin alt maddeleri (daha girintili) dizinin içindedir.
            onceki = _sira_etiketi(oc[j][3])
            son_satir = len(c.satirlar)
            for t in range(j + 1, len(oc)):
                if oc[t][2] > ust:
                    continue
                e = _sira_etiketi(oc[t][3])
                if oc[t][2] != ust or not _sonraki_etiket_mi(onceki, e):
                    son_satir = oc[t][0]
                    break
                onceki = e
            kayma = " " * (son[2] - ust)
            devam_girinti = " " * (son[2] + 2)
            yeni = [devam_girinti + oc[t][3] for t in range(j)]
            yeni += [kayma + s if s.strip() else s
                     for s in c.satirlar[oc[j][0]:son_satir]]
            yeni += c.satirlar[son_satir:]
            self._kaydet("ic_liste_duzeyi_onarildi", sol=son[3][:80],
                         sag=oc[j][3][:80], devam_paragrafi=str(j),
                         ara=",".join(self.bloklar[k].tur for k in ara))
            b.satirlar.extend(yeni)
            self._tasi_ardina(p, ara, [q])
            # p aynı kalır: birleşen liste yine bölünmüş olabilir.

    # -- kalem 2: sayfa numarası -------------------------------------------
    def sayfa_no_sil(self) -> None:
        """Kalem 2: metne sızan kaynak sayfa numarasını sil.

        Aday: tek başına, önünde boş satır olan 1-4 rakamlı satır. Silinmesi
        için (a) sayfa kırılımının yapısal izinde olmalı (bkz.
        _sayfa_no_yapisal) ve (b) belgedeki bütün kabul edilen adaylarla
        birlikte kesin artan bir dizi oluşturmalı (sayfa numaraları artar;
        diziye uymayan aday sayfa numarası değildir, dokunulmaz).
        """
        adaylar = [k for k, b in enumerate(self.bloklar) if b.tur == "sayi"]
        if not adaylar:
            return
        kabul = [k for k in adaylar if self._sayfa_no_yapisal(k)]
        degerler = [int(self.bloklar[k].satirlar[0].strip()) for k in kabul]
        secilen = _her_artan_en_uzunda(degerler)
        sil = [kabul[i] for i in secilen]
        if not sil:
            return
        for k in sil:
            self._kaydet("sayfa_no_silindi", deger=self.bloklar[k].satirlar[0].strip())
        at = set(sil)
        yeni: List[_Blok] = []
        devreden: Optional[List[str]] = None
        for k, b in enumerate(self.bloklar):
            if k in at:
                if devreden is None:
                    devreden = b.bos_once
                continue
            if devreden is not None:
                # Silinen satırın önündeki boş satır ardındaki bloğa geçer
                # ("…\n\n73\n### Başlık" -> "…\n\n### Başlık").
                if not b.bos_once:
                    b.bos_once = list(devreden)
                devreden = None
            yeni.append(b)
        self.bloklar = yeni

    def _sayfa_no_yapisal(self, k: int) -> bool:
        """Aday, sayfa kırılımının yapısal izinde mi?

        Kanıtlardan biri gerekir: bitişiğinde dipnot tanımı (sayfa altı),
        açık uçlu paragraf/madde ile devam biçimli paragraf/madde arasında
        (bölünmüş cümle), ardından doğrudan başlık (boş satırsız; Marker
        sayfa numarasını yeni sayfanın başlığına yapıştırır).
        """
        b = self.bloklar[k]
        if not b.bos_once and k > 0:
            return False       # önceki blokla aynı paragrafta
        onceki = self.bloklar[k - 1] if k > 0 else None
        sonraki = self.bloklar[k + 1] if k + 1 < len(self.bloklar) else None
        if (onceki and onceki.tur == "dipnot") or (sonraki and sonraki.tur == "dipnot"):
            return True
        if sonraki and sonraki.tur == "baslik" and not sonraki.bos_once:
            return True
        if onceki and sonraki and onceki.tur in ("paragraf", "liste") \
                and sonraki.tur in ("paragraf", "liste") \
                and _acik_uclu(onceki.satirlar[-1]) \
                and _devam_bicimli(self._ilk_metin(sonraki)):
            return True
        return False

    @staticmethod
    def _ilk_metin(b: _Blok) -> str:
        m = _IMLI_RE.match(b.satirlar[0]) or _SIRALI_RE.match(b.satirlar[0])
        return m.group(4) if m else b.satirlar[0]

    # -- KARAR 30: başlığı yinelenerek "devamı" etiketiyle süren tablo ------
    def devam_tablosu_birlestir(self) -> None:
        """Tablo + devam artıkları (en az bir etiket) + AYNI başlık satırıyla
        süren tablo → tek tablo. Yinelenen başlık + ayırıcı satırı ve devam
        artıkları silinir (sayılır); başlık farklıysa ya da arada etiket
        yoksa dokunulmaz (iki ayrı tablo olabilir)."""
        p = 0
        while p < len(self.bloklar):
            b = self.bloklar[p]
            if b.tur != "tablo" or len(b.satirlar) < 2:
                p += 1
                continue
            k = p + 1
            artik: List[int] = []
            while k < len(self.bloklar) and (
                    _devam_artigi_mi(self.bloklar[k])
                    or self.bloklar[k].tur == "sayi"):
                if self.bloklar[k].tur != "sayi":
                    artik.append(k)
                k += 1
            if (not artik or k >= len(self.bloklar)
                    or self.bloklar[k].tur != "tablo"):
                p += 1
                continue
            c = self.bloklar[k]
            if (len(c.satirlar) < 3 or not _tablo_ayirici_mi(c.satirlar[1])
                    or _hucre_metni(c.satirlar[0]) != _hucre_metni(b.satirlar[0])):
                p += 1
                continue
            for j in artik:
                self._kaydet("devam_etiketi_silindi",
                             metin=" / ".join(self.bloklar[j].satirlar)[:80])
            self._kaydet("yinelenen_baslik_silindi", baslik=c.satirlar[0][:80])
            # Yinelenen başlık çıkınca ikinci parça başlıksız bir devamdır;
            # bölünmüş son satırın (ilk hücresi boş devam satırı) birleşmesi
            # için mevcut bölünmüş tablo kuralına verilir; kabul etmezse
            # satırlar olduğu gibi eklenir (başlık ve etiket zaten doğrulandı).
            devam = c.satirlar[2:]
            birlesik = (_tablo_birlesimi(b.satirlar, [devam[0], c.satirlar[1]] + devam[1:])
                        if devam else None)
            if birlesik is not None:
                yeni, bilgi = birlesik
                if bilgi.get("satir_birlesti") == "1":
                    self.sayac["tablo_satiri_birlesti"] += 1
            else:
                yeni = b.satirlar + devam
            self._kaydet("devam_tablosu_birlesti", sol=b.satirlar[0][:60],
                         eklenen_satir=str(len(devam)))
            b.satirlar = yeni
            sil = set(artik) | {k}
            self.bloklar = [x for i, x in enumerate(self.bloklar) if i not in sil]
            # aynı tablo bir sonraki sayfada da sürebilir: p yerinde kalır

    # -- kalem 3: bölünmüş tablo --------------------------------------------
    def tablo_birlestir(self) -> None:
        p = 0
        while p < len(self.bloklar):
            b = self.bloklar[p]
            if b.tur != "tablo":
                p += 1
                continue
            ara, q = self._ara_bloklar(p)
            if (q is None or self.bloklar[q].tur != "tablo"
                    or any(self.bloklar[k].tur not in ("dipnot", "sayi")
                           for k in ara)):
                p += 1
                continue
            c = self.bloklar[q]
            birlesik = _tablo_birlesimi(b.satirlar, c.satirlar)
            if birlesik is None:
                p += 1
                continue
            satirlar, bilgi = birlesik
            self._kaydet("tablo_birlesti", sol=b.satirlar[0][:60],
                         sag=c.satirlar[0][:60],
                         ara=",".join(self.bloklar[k].tur for k in ara), **bilgi)
            if bilgi.get("satir_birlesti") == "1":
                self.sayac["tablo_satiri_birlesti"] += 1
            if bilgi.get("bos_sutun") not in (None, "0"):
                self.sayac["tablo_bos_sutun_atildi"] += 1
            b.satirlar = satirlar
            self._tasi_ardina(p, ara, [q])

    # -- G1b-2: denklem (mt okuma MT-11, MT-6) ------------------------------
    def denklem_onar(self) -> None:
        """Display denklemin hemen ardına düşmüş matematik satırları.

        MT-11: denklem çiftinin ikinci yarısı ";" ile başlayan satır içi
        matematik olarak display bloğunun BOŞ SATIRSIZ altına düşmüş
        ("$$\\cos E = …$$" + ";  $\\cos E = …$"; iki test kitabında 12 yer).
        Kaynakta iki ifade tek satırda, ";" ile ayrılmıştır: satır display'in
        içine Marker'ın aynı belgede kullandığı biçimle (" \\; ; \\; ")
        katılır.

        MT-6: hizalı denklem bloğunun ilk satırı display, kalanları
        satır içi matematik satırları olarak gelmiş ("$$r_P = …$$" + "  " +
        " $r_A = …$   " …; üç test kitabında 4, 5 ve 1 olgu); dönüştürücü
        bunları tek paragrafta birleştiriyor, denklemler yan yana
        yapışıyordu. Display'in ardında (boş satırsız ya da yalnız
        Marker'ın satır kırma boşluklarından oluşan satırla) gelen ve HER
        satırı tek satır içi matematik + bağıntı (=, <, \\le, \\to …) olan
        paragrafın her satırı ayrı display olur. Bağıntısız satır (sarılmış
        uzun denklemin devamı: " $\\times 5.45N_2$ ") varsa dokunulmaz.
        """
        p = 0
        while p + 1 < len(self.bloklar):
            d = self.bloklar[p]
            c = self.bloklar[p + 1]
            if d.tur != "denklem" or c.tur != "paragraf" or not _display_kapali(d):
                p += 1
                continue
            m = _DENKLEM_DEVAM_RE.match(c.satirlar[0])
            if m and not c.bos_once and len(c.satirlar) == 1:
                son = d.satirlar[-1].rstrip()
                ic = m.group(1).strip() + (" " + m.group(2) if m.group(2) else "")
                d.satirlar[-1] = son[:-2].rstrip() + " \\; ; \\; " + ic + "$$"
                self._kaydet("denklem_devami_birlesti", denklem=son[-60:],
                             devam=c.satirlar[0][:80])
                del self.bloklar[p + 1]
                continue
            bosluk = (not c.bos_once
                      or (len(c.bos_once) == 1 and c.bos_once[0] != ""
                          and not c.bos_once[0].strip()))
            esl = [_SATIR_ICI_DENKLEM_RE.match(s) for s in c.satirlar]
            if bosluk and all(esl) and all(_BAGINTI_RE.search(x.group(1)) for x in esl):
                c.satirlar = ["$$" + x.group(1).strip() + "$$" for x in esl]
                c.tur = "denklem"
                self._kaydet("denklem_satiri_ayrildi", satir=str(len(esl)),
                             ilk=c.satirlar[0][:80])
            p += 1

    # -- KARAR 32: tek başına sayfa üst/alt bilgisi --------------------------
    def ust_bilgi_sil(self) -> None:
        """Gövdeye tek başına satır olarak sızmış, sayfa sınırında tekrar eden
        ve kesin tanınan sayfa üst/alt bilgisini sil ve say (KARAR 32;
        kayıpsızlığın üçüncü istisnası).

        Aday: tek satırlık paragraf = sayfa numarası + adı belgenin bir
        BAŞLIĞIYLA eşleşen metin ("2 1 Introduction", "References 33",
        "432 Index"; _kesin_ust_bilgi). Silinmesi için (hepsi):
        - aynı ad belgede en az iki kez üst bilgi olarak geçer (tek başına ya
          da paragraf sonuna yapışık) ve sayfa numaraları belge sırasıyla
          kesin artar (Arap ve Roma ayrı dizi): üst bilgi sayfadan sayfaya
          yinelenir, numarası artar;
        - komşu bloklar da aday değil (art arda "ad + sayfa" satırları
          içindekiler listesidir, dokunulmaz).
        Tek geçen aday burada silinmez; paragraf birleşmesinin arasında
        kalırsa (sayfa sınırı kanıtı) _tasi_ardina siler. Başlıkla eşleşmeyen
        yinelenen satırlar ("where", "(continued)", yazar adı) dokunulmaz.
        """
        self._sozluk_kur()
        adaylar: Dict[int, Tuple[str, int, bool]] = {}
        for k, b in enumerate(self.bloklar):
            if b.tur == "paragraf" and len(b.satirlar) == 1:
                c = self._kesin_ust_bilgi(b.satirlar[0])
                if c:
                    adaylar[k] = c
        if not adaylar:
            return
        gecis: Dict[str, List[Tuple[float, int, bool]]] = {}
        for k, (ad, deger, roma) in adaylar.items():
            gecis.setdefault(ad, []).append((float(k), deger, roma))
        for k, b in enumerate(self.bloklar):
            if b.tur in ("paragraf", "liste") and k not in adaylar:
                _, ust = self._sol_hazirla(b.satirlar[-1])
                c = self._kesin_ust_bilgi(ust) if ust else None
                if c:
                    gecis.setdefault(c[0], []).append((k + 0.5, c[1], c[2]))
        tekrar = set()
        for ad, g in gecis.items():
            g.sort()
            artan = all(
                all(x < y for x, y in zip(dizi, dizi[1:]))
                for dizi in ([d for _, d, r in g if not r], [d for _, d, r in g if r]))
            if len(g) >= 2 and artan:
                tekrar.add(ad)
        sil = [k for k, (ad, _, _) in adaylar.items()
               if ad in tekrar and k - 1 not in adaylar and k + 1 not in adaylar]
        if not sil:
            return
        for k in sil:
            self._ust_bilgi_silindi(self.bloklar[k].satirlar[0])
        at = set(sil)
        yeni: List[_Blok] = []
        devreden: Optional[List[str]] = None
        for k, b in enumerate(self.bloklar):
            if k in at:
                if devreden is None:
                    devreden = b.bos_once
                continue
            if devreden is not None:
                if not b.bos_once:
                    b.bos_once = list(devreden)
                devreden = None
            yeni.append(b)
        self.bloklar = yeni

    # -- FX1: dipnot tanımına yapışık blok ----------------------------------
    def yapisik_tanim_ayir(self) -> None:
        """Dipnot tanımına boş satırsız yapışmış sonraki blok (görsel,
        başlık, madde, kod, gövde paragrafı …; bkz. tanim_ardi_sinir_mi)
        boş satırla ayrılır: tanım tek başına paragraf olur, dönüştürücü
        onu işaretine eşleyebilir (V1 yeni hata 4). Metin
        değişmez, yalnız bir boş satır eklenir. Birleştirme adımlarından
        ÖNCE çalışır: onlar (ve ikinci çağrı) aynı yapıyı görür, eşgüçlü."""
        for k in range(len(self.bloklar) - 1):
            d, c = self.bloklar[k], self.bloklar[k + 1]
            # Art arda tanımlar zaten ayrı paragraf sayılır (dönüştürücü).
            if d.tur != "dipnot" or c.bos_once or c.tur == "dipnot":
                continue
            if tanim_ardi_sinir_mi(d.satirlar[-1], c.satirlar[0]):
                c.bos_once = [""]
                self._kaydet("dipnot_tanimi_ayrildi", tanim=d.satirlar[-1][-80:],
                             sonraki=c.satirlar[0][:80])

    def calistir(self) -> str:
        self.yapisik_tanim_ayir()
        self.sayfa_no_sil()
        self.tire_duzelt()
        self.denklem_onar()
        self.ust_bilgi_sil()
        self.paragraf_birlestir()
        self.dipnot_devam_birlestir()
        self.ic_liste_duzeyi_onar()
        self.liste_birlestir()
        self.liste_imi_birlestir()
        self.ic_madde_birlestir()
        self.devam_tablosu_birlestir()
        self.tablo_birlestir()
        return _birlestir(self.bloklar, self.son_bos)


def _her_artan_en_uzunda(degerler: List[int]) -> List[int]:
    """Kesin artan en uzun alt dizilerin HEPSİNDE bulunan dizinler (artan).

    Sayfa numaraları belgede artar; birden çok eşit uzunlukta artan dizi
    varsa ([37, 12]: [37] ya da [12]; [10, 37, 12]: [10, 37] ya da [10, 12])
    yalnız hepsinde ortak olan aday sayfa numarası sayılır, öteki adaylar
    belirsizdir ve dokunulmaz. Yöntem: i'de biten (sol) ve i'den başlayan
    (sag) en uzun artan dizi uzunluğu; sol+sag-1 = en uzun ise i bir en uzun
    dizidedir; o "düzeyde" (sol değeri) tek böyle dizin varsa hepsindedir.
    O(n²); aday sayısı küçük.
    """
    n = len(degerler)
    if n == 0:
        return []
    sol = [1] * n
    for i in range(n):
        for j in range(i):
            if degerler[j] < degerler[i]:
                sol[i] = max(sol[i], sol[j] + 1)
    sag = [1] * n
    for i in range(n - 1, -1, -1):
        for j in range(i + 1, n):
            if degerler[j] > degerler[i]:
                sag[i] = max(sag[i], sag[j] + 1)
    en_uzun = max(sol)
    duzey: Dict[int, List[int]] = {}
    for i in range(n):
        if sol[i] + sag[i] - 1 == en_uzun:
            duzey.setdefault(sol[i], []).append(i)
    return sorted(v[0] for v in duzey.values() if len(v) == 1)


# ---------------------------------------------------------------------------
# Tablo yardımcıları
# ---------------------------------------------------------------------------

def _hucreler(satir: str) -> List[str]:
    """Markdown tablo satırını hücrelere böl (``\\|`` kaçışı ve `kod` korunur)."""
    s = satir.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    hucre: List[str] = []
    cur = []
    kod = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            cur.append(s[i:i + 2])
            i += 2
            continue
        if ch == "`":
            kod = not kod
        if ch == "|" and not kod:
            hucre.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    hucre.append("".join(cur))
    return [h.strip() for h in hucre]


def _ayrac_mi(satir: str) -> bool:
    """Tablo ayraç satırı ("|---|:--:|"). Marker dar boş sütunda "--" yazar;
    bu yüzden hücre başına en az bir tire yeter."""
    return bool(re.match(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$", satir))


def _satir_yaz(hucreler: List[str]) -> str:
    return "| " + " | ".join(hucreler) + " |"


def _sayisal(h: str) -> bool:
    t = re.sub(r"[*_`$\\]", "", h).strip()
    return bool(t) and bool(re.fullmatch(r"[-+~≈<>]?\s*[\d.,\s%]+%?", t))


def _hucre_tipi(h: str) -> str:
    if not h.strip():
        return "bos"
    return "sayi" if _sayisal(h) else "metin"


def _tablo_birlesimi(t1: List[str], t2: List[str]):
    """Sayfa kırılımıyla bölünmüş iki tabloyu birleştir; emin değilse None.

    Koşullar: iki tablonun da ayraç satırı (``|---|``) ikinci satırda;
    sütun sayıları eşit (ikinci tablonun hep boş kalan sondaki sütunları
    atılarak); ikinci tablonun "başlık satırı" başlık değil veri gibi:
    (a) ilk dolu hücresi küçük harfle başlıyor ya da boş hücre içeriyor ve
    ilk tablonun son satırıyla tamamlayıcı, veya (b) hücre tipleri (sayı /
    metin) ilk tablonun gövde satırlarıyla birebir aynı ve en az bir sayısal
    hücre var. İkinci tablonun başlığı ilk tablonunkiyle aynıysa (yinelenen
    başlık) birleştirilmez: yinelenen başlığı silmek metin düşürür.
    """
    if len(t1) < 3 or len(t2) < 2 or not _ayrac_mi(t1[1]) or not _ayrac_mi(t2[1]):
        return None
    if any(_ayrac_mi(s) for s in t1[2:]) or any(_ayrac_mi(s) for s in t2[2:]):
        return None
    r1 = [_hucreler(s) for s in [t1[0]] + t1[2:]]
    r2 = [_hucreler(s) for s in [t2[0]] + t2[2:]]
    n1 = {len(r) for r in r1}
    n2 = {len(r) for r in r2}
    if len(n1) != 1 or len(n2) != 1:
        return None
    k1, k2 = n1.pop(), n2.pop()
    bos_sutun = 0
    if k2 > k1 and all(not any(r[c] for r in r2) for c in range(k1, k2)):
        bos_sutun = k2 - k1
        r2 = [r[:k1] for r in r2]
        k2 = k1
    if k1 != k2:
        return None
    if [h.casefold() for h in r1[0]] == [h.casefold() for h in r2[0]]:
        return None
    bas2 = r2[0]
    son1 = r1[-1]
    govde1 = r1[1:]
    # (a) devam: başlık satırı son satırın devamı (bölünmüş satır)
    devam = False
    ortak = [c for c in range(k1) if son1[c] and bas2[c]]
    tamamlayici = any(not son1[c] and bas2[c] for c in range(k1)) and \
        any(son1[c] and not bas2[c] for c in range(k1))
    dolu2 = [h for h in bas2 if h]
    if dolu2 and _devam_bicimli(dolu2[0]):
        if all(_acik_uclu(son1[c]) and _devam_bicimli(bas2[c]) for c in ortak) \
                and (ortak or tamamlayici):
            devam = True
    # (b) veri satırı: tipler gövdeyle aynı
    veri = False
    if govde1 and not devam:
        tip2 = [_hucre_tipi(h) for h in bas2]
        tipler = {tuple(_hucre_tipi(h) for h in r) for r in govde1}
        if len(tipler) == 1 and tuple(tip2) in tipler and "sayi" in tip2 \
                and "bos" not in tip2:
            veri = True
    if not devam and not veri:
        return None
    bilgi = {"bos_sutun": str(bos_sutun), "satir_birlesti": "0", "hucre_devami": "0"}
    govde2 = r2[1:]
    if devam:
        yeni_son = [(_metin_ekle(son1[c], bas2[c]) if son1[c] and bas2[c]
                     else son1[c] or bas2[c]) for c in range(k1)]
        satirlar = list(t1[:-1]) if len(t1) > 2 else list(t1)
        satirlar.append(_satir_yaz(yeni_son))
        bilgi["satir_birlesti"] = "1"
    else:
        satirlar = list(t1)
        satirlar.append(_satir_yaz(bas2) if bos_sutun else t2[0])
    # Sütun atıldıysa ikinci tablonun satırları yeniden yazılır; yoksa
    # özgün satırlar aynen kalır.
    for s, r in zip(t2[2:], govde2):
        satirlar.append(_satir_yaz(r) if bos_sutun else s)
    # Birleşik tabloda hücre devamı satırları: önceki satırın
    # açık uçlu hücresine katılır.
    satirlar, n = _hucre_devami_birlestir(satirlar)
    bilgi["hucre_devami"] = str(n)
    return satirlar, bilgi


def _hucre_devami_birlestir(satirlar: List[str]) -> Tuple[List[str], int]:
    """Tablo içinde hücre metninin devamı olan satırları öncekine kat.

    Devam satırı: dolu hücrelerinin HEPSİ küçük harfle başlar, önceki
    satırın aynı sütundaki hücresi açık uçlu (noktasız) ve en az
    HUCRE_ILK_YARI_SOZCUK_ASGARI sözcük, ve devam satırında önceki satırın
    dolu olduğu en az bir hücre boştur (satır seyrek). Büyük harfle başlayan
    (grup/rowspan) satırlar dokunulmaz.
    """
    if len(satirlar) < 4:
        return satirlar, 0
    bas, ayrac = satirlar[0], satirlar[1]
    govde = [_hucreler(s) for s in satirlar[2:]]
    ozgun = list(satirlar[2:])
    out: List[List[str]] = []
    out_s: List[Optional[str]] = []
    n = 0
    for r, s in zip(govde, ozgun):
        if out:
            onceki = out[-1]
            dolu = [c for c in range(len(r)) if r[c]]
            if (len(r) == len(onceki) and dolu
                    and all(_devam_bicimli(r[c]) and onceki[c]
                            and _acik_uclu(onceki[c])
                            and _sozcuk_sayisi(onceki[c]) >= HUCRE_ILK_YARI_SOZCUK_ASGARI
                            for c in dolu)
                    and any(onceki[c] and not r[c] for c in range(len(r)))):
                out[-1] = [(_metin_ekle(onceki[c], r[c]) if r[c] else onceki[c])
                           for c in range(len(r))]
                out_s[-1] = None
                n += 1
                continue
        out.append(list(r))
        out_s.append(s)
    yeni = [bas, ayrac] + [s if s is not None else _satir_yaz(r)
                           for r, s in zip(out, out_s)]
    return yeni, n


# ---------------------------------------------------------------------------
# Genel arayüz
# ---------------------------------------------------------------------------

def onar_ayrintili(markdown: str) -> Tuple[str, Dict[str, int], List[Dict[str, str]]]:
    """(onarılmış metin, sayım sözlüğü, değişiklik listesi).

    Değişiklik listesinin her öğesi {"tur": <SAYAC_TURLERI'nden biri>, ...}
    biçimindedir (birleşen yarıların uçları, silinen sayfa numarası vb.);
    doğrulama ve günlük için. ``ara_blok_tasindi`` dışındaki her sayım,
    listedeki o türden öğe sayısına eşittir.
    """
    if not markdown or not markdown.strip():
        return markdown, {k: 0 for k in SAYAC_TURLERI}, []
    o = _Onarici(markdown)
    metin = o.calistir()
    return metin, dict(o.sayac), list(o.ayrinti)


def onar_rapor(markdown: str) -> Tuple[str, Dict[str, int]]:
    """Onarılmış metin ve sayım sözlüğü (değişiklik türü -> sayı; anahtarlar
    SAYAC_TURLERI, boş girdide hepsi 0)."""
    metin, sayac, _ = onar_ayrintili(markdown)
    return metin, sayac


def onar(markdown: str) -> str:
    """Marker Markdown'ında sayfa kırılımı bozukluklarını onar (bkz. modül)."""
    return onar_ayrintili(markdown)[0]
