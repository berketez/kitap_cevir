# -*- coding: utf-8 -*-
"""Marker'ın düşürdüğü kalın/italik vurgunun kaynak PDF'ten geri getirilmesi.

5 Ekim 2026 (çeviri okuma bulguları). Marker vurguyu
tutarsız aktarır: aynı biçimdeki beş soru satırından biri düz, biri italik
çıkar; madde etiketlerinin kalını, şekil altyazısının kalın italik etiketi,
Latince alıntının italiği düşer. Bu modül kaynak PDF'in metin katmanındaki
yazı tipi adlarından (Bold, Italic, CMBX, CMTI, "-B", ".I" ...) kalın/italik
"koşuları" çıkarır ve Markdown'da yalnız EKSİK olan vurguyu ekler:

- düz metin -> ``*x*`` / ``**x**`` / ``***x***``;
- italik içindeki kalın italik koşu -> italiğin içine ``**x**``;
- kalın içindeki kalın italik koşu -> kalının içine ``*x*``.

Metin eklenmez, silinmez; yalnız vurgu işareti eklenir. Var olan vurgu
kaldırılmaz. Belirsiz her durumda (koşu Markdown'da benzersiz bağlamla
bulunamıyor, başlık/kod/tablo sınırı, bağlantı ya da etiket ortasında, karma
vurgu) dokunulmaz. Eş aralıklı (kod) ve matematik yazı tipleri kapsam dışıdır.

Sözleşme: ``onar_raporlu(markdown, pdf_path) -> (markdown, VurguRaporu)``.
PDF yoksa, açılamıyorsa ya da metin katmanı yoksa Markdown AYNEN döner.
Dönüştürücüde ``pdf_metin_onarim.onar``'dan sonra, ``_wrap_pdf_monospace``'ten
önce çağrılır (ham Marker Markdown'ı).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from latex_pipeline import pdf_metin_onarim as pmo

logger = logging.getLogger(__name__)

# Koşu en az bu kadar harf taşır ("Q:" gibi iki harfli etiket dahil).
KOSU_EN_AZ_HARF = 2
# Eşleme bağlamı: koşu + (sol, sağ) pencere (normalleştirilmiş, boşluksuz
# karakter). Tek yanlı pencere: şekil altyazısının solunda PDF'te şeklin
# içindeki yazılar durur, Markdown'da görsel; başlığa yapışık satırın sağı.
BAGLAM_PAYLARI = ((0, 0), (8, 8), (0, 16), (16, 0), (16, 16), (0, 32), (32, 0),
                  (32, 32))
BAGLAM_EN_KISA = 12
# Beklenen konum çevresinde arama penceresi (Markdown sırası PDF'e göre
# kayabilir: dipnot, şekil, taşınan paragraf).
ARAMA_PENCERESI = 20000
# Belgenin gövdesi baştan sona italik/kalın dizilmişse (oran eşiği aşılırsa)
# o boyut onarılmaz: vurgu değil, gövde yazı tipidir.
GOVDE_ORANI = 0.5

_ITALIK, _KALIN = 1, 2
_ISARET = {_ITALIK: "*", _KALIN: "**", _ITALIK | _KALIN: "***"}

_MATEMATIK_FONT_RE = re.compile(
    r"^(?:cmmi|cmmib|cmsy|cmbsy|cmex|msam|msbm|eufm|eusm|mtsy|mtex|rmtmi|symbol"
    r"|advpssym|wasy|stmary)|math", re.IGNORECASE)
_ES_ARALIKLI_FONT_RE = re.compile(
    r"mono|courier|consol|menlo|typewriter|inconsolata|sourcecode|firacode"
    r"|^(?:cmtt|sftt|ectt|lmmono|cmsltt)", re.IGNORECASE)
_KALIN_FONT_RE = re.compile(
    r"bold|black|heavy|semibold|demibold|extrabold|ultrabold"
    r"|^(?:cmbx|cmb|sfbx|ecbx|cmbxti|cmbxsl)$|^cmbx|[-.,_](?:b|bd|bi|sb|bdit)$",
    re.IGNORECASE)
_ITALIK_FONT_RE = re.compile(
    r"italic|oblique|slant|^(?:cmti|cmsl|cmbxti|cmbxsl|sfti|sfsl|ecti)"
    r"|[-.,_](?:i|it|bi|bdit|ital)$", re.IGNORECASE)
# Ağırlık sözcüğüne ayraçsız bitişik italik eki: Adobe "MinionPro-BoldIt",
# "-SemiboldIt", "-LightIt"; URW "NimbusRomNo9L-ReguItal", "-BoldItal".
# Büyük/küçük harf duyarlı (küçük harften sonra büyük "It"): "Split",
# "Transit" gibi adlar italik sayılmaz.
_ITALIK_BITISIK_EK_RE = re.compile(r"(?<=[a-z])It(?:al)?$")


def font_stili(font: str) -> int:
    """Yazı tipi adından vurgu: 0 düz, 1 italik, 2 kalın, 3 ikisi; -1 koşuyu
    keser (matematik/simge, eş aralıklı ya da adsız yazı tipi)."""
    f = (font or "").rsplit("+", 1)[-1]
    f = re.sub(r"-Identity-[HV]$", "", f)
    f = re.sub(r"\d+$", "", f)
    if not f or _MATEMATIK_FONT_RE.search(f) or _ES_ARALIKLI_FONT_RE.search(f):
        return -1
    f = re.sub(r"(?:MT|PS|Std|LT)$", "", f)
    stil = 0
    if _ITALIK_FONT_RE.search(f) or _ITALIK_BITISIK_EK_RE.search(f):
        stil |= _ITALIK
    if _KALIN_FONT_RE.search(f):
        stil |= _KALIN
    return stil


@dataclass
class VurguRaporu:
    sayac: Dict[str, int] = field(default_factory=dict)
    ornekler: List[str] = field(default_factory=list)
    # Dokunulmayan koşular (neden: metin) — tanı için, ilk 300.
    atlanan: List[str] = field(default_factory=list)

    def say(self, ad: str, ornek: str = "") -> None:
        self.sayac[ad] = self.sayac.get(ad, 0) + 1
        if ornek and len(self.ornekler) < 40:
            self.ornekler.append(f"{ad}: {ornek}")

    def atla(self, neden: str, ornek: str = "") -> None:
        self.sayac[neden] = self.sayac.get(neden, 0) + 1
        if len(self.atlanan) < 300:
            self.atlanan.append(f"{neden}: {ornek[:70]}")


# ---------------------------------------------------------------------------
# PDF koşuları
# ---------------------------------------------------------------------------

def _kosular(pdf: "pmo._PdfAkis") -> Tuple[List[Tuple[int, int, int]], int]:
    """[(a, b, stil)]: normalleştirilmiş PDF dizgisinde sabit vurgulu en uzun
    aralıklar; ikinci değer gövde yüzünden kapatılan boyutlar."""
    stil_cache: Dict[str, int] = {}
    norm_stil: List[int] = []
    harf = {_ITALIK: 0, _KALIN: 0}
    toplam = 0
    for i, k in enumerate(pdf.kaynak):
        font = pdf.font[k]
        st = stil_cache.get(font)
        if st is None:
            st = stil_cache[font] = font_stili(font)
        norm_stil.append(st)
        if st >= 0 and pdf.norm[i].isalpha():
            toplam += 1
            for bit in (_ITALIK, _KALIN):
                if st & bit:
                    harf[bit] += 1
    kapali = 0
    for bit in (_ITALIK, _KALIN):
        if toplam and harf[bit] / toplam > GOVDE_ORANI:
            kapali |= bit
    kosular: List[Tuple[int, int, int]] = []
    n = len(norm_stil)
    i = 0
    while i < n:
        st = norm_stil[i]
        st = st & ~kapali if st > 0 else st
        if st <= 0:
            i += 1
            continue
        sayfa = pdf.sayfa[pdf.kaynak[i]]
        j = i + 1
        while j < n:
            sj = norm_stil[j]
            sj = sj & ~kapali if sj > 0 else sj
            if sj != st or pdf.sayfa[pdf.kaynak[j]] != sayfa:
                break
            j += 1
        if sum(1 for c in pdf.norm[i:j] if c.isalpha()) >= KOSU_EN_AZ_HARF:
            kosular.append((i, j, st))
        i = j
    return kosular, kapali


def _bul(pdf_norm: str, md_norm: str, a: int, b: int, beklenen: int) -> Optional[int]:
    """PDF norm aralığı [a, b) için Markdown norm başlangıcı; bağlam iki
    metinde de (önce beklenen konum penceresinde, sonra bütün metinde) tam
    bir kez geçmelidir."""
    np_, nm = len(pdf_norm), len(md_norm)
    for genis in (False, True):
        for sol, sag in BAGLAM_PAYLARI:
            wa, wb = max(0, a - sol), min(np_, b + sag)
            w = pdf_norm[wa:wb]
            if len(w) < BAGLAM_EN_KISA or pmo._ENGEL in w:
                continue
            if genis:
                plo, phi, mlo, mhi = 0, np_, 0, nm
            else:
                plo, phi = max(0, wa - ARAMA_PENCERESI), min(np_, wb + ARAMA_PENCERESI)
                mlo = max(0, beklenen - (a - wa) - ARAMA_PENCERESI)
                mhi = min(nm, beklenen + (wb - a) + ARAMA_PENCERESI)
            if pdf_norm.find(w, plo, phi) != wa or pdf_norm.find(w, wa + 1, phi) >= 0:
                continue
            j = md_norm.find(w, mlo, mhi)
            if j < 0 or md_norm.find(w, j + 1, mhi) >= 0:
                continue
            return j + (a - wa)
    return None


# ---------------------------------------------------------------------------
# Markdown'daki mevcut vurgu
# ---------------------------------------------------------------------------

_GIZLI_RE = re.compile(r"`[^`\n]*`|\$\$.*?\$\$|\$[^$\n]*\$|\]\([^)\n]*\)|<[^<>\n]*>|\\.")


def _satir_vurgusu(satir: str) -> Tuple[List[int], List[bool]]:
    """Satırdaki her karakterin Markdown vurgusu (0-3) ve vurgu işareti olup
    olmadığı (CommonMark'ın sınırlayıcı işleme kuralının sadeleştirilmişi:
    "*" ve sözcük sınırındaki "_"; içten dışa eşleşme, ikili önce)."""
    n = len(satir)
    gizli = [False] * n
    for m in _GIZLI_RE.finditer(satir):
        for k in range(m.start(), m.end()):
            gizli[k] = True
    kosular = []  # [konum, sayı, karakter, açabilir, kapatabilir]
    i = 0
    while i < n:
        c = satir[i]
        if c in "*_" and not gizli[i]:
            j = i
            while j < n and satir[j] == c and not gizli[j]:
                j += 1
            once = satir[i - 1] if i > 0 else " "
            sonra = satir[j] if j < n else " "
            acar = not sonra.isspace()
            kapar = not once.isspace()
            if c == "_":
                acar = acar and not once.isalnum()
                kapar = kapar and not sonra.isalnum()
            kosular.append([i, j - i, c, acar, kapar])
            i = j
        else:
            i += 1
    vurgu = [0] * n
    isaret = [False] * n
    yigin: List[list] = []  # [konum, kalan sayı, karakter]
    for kon, say, c, acar, kapar in kosular:
        kalan, bas = say, kon
        while kapar and kalan > 0:
            k = len(yigin) - 1
            while k >= 0 and yigin[k][2] != c:
                k -= 1
            if k < 0:
                break
            ac = yigin[k]
            kul = 2 if (kalan >= 2 and ac[1] >= 2) else 1
            ic_bas, ic_son = ac[0] + ac[1], bas
            bit = _KALIN if kul == 2 else _ITALIK
            for t in range(ic_bas, ic_son):
                vurgu[t] |= bit
            for t in list(range(ic_bas - kul, ic_bas)) + list(range(bas, bas + kul)):
                isaret[t] = True
            ac[1] -= kul
            kalan -= kul
            bas += kul
            del yigin[k + 1:]
            if ac[1] == 0:
                yigin.pop(k)
        if acar and kalan > 0:
            yigin.append([bas, kalan, c])
    return vurgu, isaret


# ---------------------------------------------------------------------------
# Onarım
# ---------------------------------------------------------------------------

_YASAK_KARAKTER = set("[]`$<>\n")
# Aralığın içinde kalabilecekler: boş çapa ve satır sonu etiketi (tablo
# dışında). Bağlantı kalamaz: dönüştürücü vurgunun içindeki "[metin](adres)"i
# bağlantıya çevirmez, ham Markdown basılır (korpus denetimi, kaynakça
# maddesi: "*[Kitap Adı: Alt Başlık](https://…) Devamı*"). Kesilen kod, matematik ve
# simge etiketi de yasak karakter bırakır.
_SERBEST_ETIKET_RE = re.compile(r"<span\b[^<>\n]*>\s*</span>|<br\s*/?>", re.IGNORECASE)
# Satır içi kod parçası: içine eklenen vurgu işareti kodda düz metin olur
# (dönüştürücü "`**x**`"i \texttt{**x**} basar). Kodu kesen koşu zaten yasak
# karakter (ters tırnak) bırakır; bu, kodun içeriğini tam kaplayan koşu için.
_KOD_PARCASI_RE = re.compile(r"`[^`\n]*`")
_ATLANAN_SATIR_RE = re.compile(r"^\s*(?:#|```|!\[|\$\$|\\\[)")
def onar_raporlu(markdown: str, pdf_path: Optional[str]) -> Tuple[str, VurguRaporu]:
    rapor = VurguRaporu()
    if not markdown or not pdf_path or not str(pdf_path).lower().endswith(".pdf"):
        return markdown, rapor
    pdf = pmo._pdf_akisi_kur(str(pdf_path))
    if pdf is None:
        rapor.say("pdf_yok")
        return markdown, rapor
    ak = pmo._md_akisi_kur(markdown)
    kosular, kapali = _kosular(pdf)
    if kapali:
        rapor.say("govde_vurgusu_kapali")
    satir_bas = [0]
    for m in re.finditer("\n", markdown):
        satir_bas.append(m.end())
    vurgu_cache: Dict[int, Tuple[List[int], List[bool]]] = {}
    eklemeler: List[Tuple[int, str]] = []
    son_p, son_m = 0, 0
    for a, b, stil in kosular:
        L = b - a
        j = _bul(pdf.norm, ak.norm, a, b, son_m + (a - son_p))
        if j is None or ak.norm[j:j + L] != pdf.norm[a:b]:
            rapor.atla("eslesmedi", pdf.norm[a:b])
            continue
        son_p, son_m = a, j
        # Koşu Markdown satırlarına (kalın paragraf + kalın başlık) ve üst/alt
        # simgede (dipnot işareti) parçalara bölünür; simge vurgulanmaz.
        parca_bas = j
        for t in range(j, j + L + 1):
            if (t == j + L or ak.satir[t] != ak.satir[parca_bas]
                    or (ak.sinif[t] == "N") != (ak.sinif[parca_bas] == "N")):
                if ak.sinif[parca_bas] == "N":
                    _parca_isle(markdown, ak, parca_bas, t, stil, satir_bas,
                                vurgu_cache, eklemeler, rapor)
                parca_bas = t
    if not eklemeler:
        return markdown, rapor
    out = markdown
    for konum, metin in sorted(eklemeler, key=lambda e: e[0], reverse=True):
        out = out[:konum] + metin + out[konum:]
    return out, rapor


# Başlık satırında yalnız şu iki durumda işlem yapılır (öteki başlık metni
# zaten kalın dizilir): başlığın sonuna yapışık vurgulu soru (dönüştürücü
# onu ayrı satıra indirir; converter._HEADING_TRAILING_QUESTION_RE ile aynı
# kalıp) ve tamamı vurgulu soru olan başlık (dönüştürücü paragrafa indirir;
# converter._QUESTION_HEADING_RE).
_BASLIK_SORUSU_RE = re.compile(
    r"^#{1,6}\s+(?:.*?[^\s*]\s+)?(?P<q>(?P<m>\*\*\*|\*(?!\*))(?=[^\s*])[^*\n]*?\?(?P=m))\s*$")


def _parca_isle(md: str, ak: "pmo._MdAkis", j0: int, j1: int, stil: int,
                satir_bas: List[int], vurgu_cache, eklemeler, rapor) -> None:
    if j1 - j0 < 1 or sum(1 for c in ak.norm[j0:j1] if c.isalpha()) < KOSU_EN_AZ_HARF:
        return
    # Baştaki/sondaki noktalama Markdown'da sözcüğe bitişikse kırpılır ("Figure 2"
    # kalın italik + ": metin" italik; iki nokta işareti etiketin içinde).
    while j1 - j0 > 1 and not ak.norm[j0].isalnum() and ak.bas[j0] > 0 \
            and md[ak.bas[j0] - 1].isalnum():
        j0 += 1
    while j1 - j0 > 1 and not ak.norm[j1 - 1].isalnum() and ak.son[j1 - 1] < len(md) \
            and md[ak.son[j1 - 1]].isalnum():
        j1 -= 1
    s, e = ak.bas[j0], ak.son[j1 - 1]
    parca = md[s:e]
    no = ak.satir[j0]
    sb = satir_bas[no]
    se = md.find("\n", sb)
    se = len(md) if se < 0 else se
    satir = md[sb:se]
    if satir.lstrip().startswith("#"):
        m = _BASLIK_SORUSU_RE.match(satir.strip())
        ofset = len(satir) - len(satir.lstrip())
        if not m or not (sb + ofset + m.start("q") < s and e < sb + ofset + m.end("q")):
            rapor.atla("baslik", parca)
            return
    elif _ATLANAN_SATIR_RE.match(satir):
        rapor.atla("kod_gorsel", parca)
        return
    tablo = satir.lstrip().startswith("|")
    if (_YASAK_KARAKTER & set(_SERBEST_ETIKET_RE.sub("", parca))
            or (tablo and ("|" in parca or "<br" in parca))):
        rapor.atla("sinir_kesiyor", parca)
        return
    if any(sb + k.start() < e and s < sb + k.end() for k in _KOD_PARCASI_RE.finditer(satir)):
        rapor.atla("kod_ici", parca)
        return
    once = md[s - 1] if s > sb else " "
    sonra = md[e] if e < se else " "
    if once.isalnum() or sonra.isalnum():
        rapor.atla("sozcuk_ortasi", parca)
        return
    if no not in vurgu_cache:
        vurgu_cache[no] = _satir_vurgusu(satir)
    vurgu, _ = vurgu_cache[no]
    if any(not sb <= ak.bas[t] < se for t in range(j0, j1)):
        rapor.atla("satir_disi", parca)
        return
    mevcut = {vurgu[ak.bas[t] - sb] for t in range(j0, j1)}
    if len(mevcut) != 1:
        rapor.atla("karma_vurgu", parca)
        return
    m0 = mevcut.pop()
    if m0 == stil:
        return
    if m0 & ~stil:
        rapor.atla("celisen_vurgu", parca)
        return
    if m0 == 0:
        isaret = _ISARET[stil]
        if once in "*_" or sonra in "*_":
            rapor.atla("bitisik_isaret", parca)
            return
        tur = {_ITALIK: "italik", _KALIN: "kalin", _ITALIK | _KALIN: "kalin_italik"}[stil]
    else:
        isaret = _ISARET[stil & ~m0]
        tur = "kalin_italige" if m0 == _ITALIK else "italik_kalina"
    eklemeler.append((s, isaret))
    eklemeler.append((e, isaret))
    rapor.say(tur, parca[:60])


def onar(markdown: str, pdf_path: Optional[str]) -> str:
    return onar_raporlu(markdown, pdf_path)[0]
