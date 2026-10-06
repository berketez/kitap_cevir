"""Lisans onayları: Datalab (PDF desteği modelleri) ve Tencent (HY-MT).

Yalnız stdlib. Dayanak: lisans raporu §8.1 (K9) ve §8.2 (Ekran A ve B),
Berke'nin 29 Eylül kararları ve KARAR 34 (5 Ekim 2026):

- KENDİ YAZDIĞIMIZ LİSANS ÖZETİ YOKTUR (KARAR 34: "bizim sorumluluğumuzda
  olmayacak"). Onay ekranı lisansın RESMÎ metnini gösterir (uygulama
  arşivindeki kopya, SHA-256'sı manifestle eşleşirse) ve resmî adresine
  bağlar; kullanıcı o metni kabul eder. Ekrandaki Türkçe cümleler yalnız neyin
  nereden indiğini ve hangi lisansla sunulduğunu söyler, koşulları anlatmaz.
- Datalab model lisansı kabul edilmezse KURULUM DURUR. PDF desteği zorunlu
  bileşendir; "yalnız EPUB" seçeneği yoktur. Kabul edilmeden zorunlu
  bileşenler inmez ("Kur" kapalı).
- HY-MT için yalnız "okudum, kabul ediyorum" kutusu vardır. Bölge beyanı
  kutusu ve macOS bölge ayarına bakan uyarı YOKTUR. Tencent lisansının m.3(e)
  maddesi gereği sağlayıcının adı ve Tencent'le bağlantısızlık beyanı
  ("beyan") gösterilir; bu bir özet değil, lisansın istediği bildirimdir.

Kayıt (K9): ``<kök>/durum/lisans_onaylari.json``::

    {"sema_surumu": 2,
     "onaylar": {"pdf": {"metin_sha256": ..., "metin_url": ..., "zaman": ...,
                          "uygulama_surumu": ...}}}

Onay, kabul edilen resmî metnin kimliğine bağlıdır: manifestteki SHA-256
(``lisans.metin_sha256``) ve resmî adres (``lisans.metin_url``, sabit etiket ya
da commit'e bağlı). Metnin SHA-256'sı değişirse onay yeniden istenir. Şema 1
kayıtları (özet SHA'lı) geçersizdir: ekran değiştiği için yeniden onay gerekir.

Resmî adres ve yerel kopya adı manifestten gelir (``lisans.metin_url`` /
``lisans.metin_dosyasi``); burada yazılmaz. HY-MT'nin kaynak deposu ve
revizyonu da manifestteki öğe adreslerinden türetilir. Resmî metnin sürüm
betiğindeki gözden geçirme kapısı (metin değişince sürüm durur) sürüm
makinesinin ayarlarındadır (``packaging/surum/surum_ayarlari.py``).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import unquote, urlparse

try:
    from .durum import KokDuzeni, json_oku, json_yaz_atomik, simdi
except ImportError:  # düz betik olarak yüklendi (sys.path[0] = packaging/yonetici)
    from durum import KokDuzeni, json_oku, json_yaz_atomik, simdi  # type: ignore[no-redef]

# 2 (KARAR 34): kayıtta özet SHA'sı yerine resmî metnin kimliği (SHA-256 + adres).
SEMA_SURUMU = 2

# ---------------------------------------------------------------------------
# Onay ekranı metinleri (arayüzün "sen" diliyle). KARAR 34: lisans koşullarını
# kendi cümlelerimizle anlatan madde YOKTUR; ekran resmî metni gösterir.
#
# Anahtar = manifestteki bileşen kimliği.
# ---------------------------------------------------------------------------
PDF = "pdf"
HYMT = "hymt"

# Her onay ekranında aynı: bağlayıcı olanın resmî metin olduğu (özet yok).
RESMI_NOT = ("Bağlayıcı olan, yukarıdaki resmî lisans metnidir (İngilizce). Kitapçevir bu "
             "lisansı özetlemez ya da yorumlamaz; sorun varsa lisans sahibine danış.")

ONAY_METINLERI: Dict[str, Dict[str, Any]] = {
    PDF: {
        "baslik": "Kitapçevir kurulumu: lisanslar",
        "giris": (
            "Kitapçevir özgür yazılımdır ve GNU GPL 3.0 lisansıyla dağıtılır. Kurulum "
            "sırasında aşağıdaki bileşenler kendi yayıncılarının sunucularından doğrudan "
            "bu Mac'e iner. Kitapçevir bu dosyaları barındırmaz."
        ),
        "bilesenler": [
            "Python, arayüz ve kitaplıklar: açık kaynak lisanslar (GPL, AGPL, MIT, BSD, "
            "Apache). Lisans metinleri “Lisanslar” klasöründe.",
            "TeX: tectonic (MIT) ya da seçtiğin TeX dağıtımı.",
            "PDF desteği için Datalab'ın (Surya/Marker) yapay zekâ modelleri. Bu modeller "
            "Datalab'ın model lisansıyla sunulur; resmî metni aşağıda.",
        ],
        "resmi_not": RESMI_NOT,
        "beyan": "",
        "kutu_etiketi": "Datalab model lisansının resmî metnini okudum ve kabul ediyorum.",
        "red_iletisi": (
            "Datalab model lisansı kabul edilmeden kurulum sürdürülemez: PDF desteği zorunlu "
            "bileşendir ve bu modeller olmadan çalışmaz."
        ),
        "tam_metin_etiketi": "Resmî adresi tarayıcıda aç",
    },
    HYMT: {
        "baslik": "HY-MT 1.5 7B çeviri modeli (Tencent)",
        "giris": (
            "Kaynak: huggingface.co/{depo} (revizyon {revizyon}). Tencent'in "
            "{lisans_depo} modelinin MLX biçimine dönüştürülmüş hâlidir. Model, Hugging "
            "Face'ten doğrudan bu Mac'e iner; Kitapçevir bu dosyaları barındırmaz."
        ),
        "bilesenler": [
            "Bu model Tencent HY Community License ve onun Kabul Edilebilir Kullanım "
            "Politikası ile sunulur; resmî metni aşağıda.",
        ],
        "resmi_not": RESMI_NOT,
        "beyan": (
            "Kitapçevir, Berke Tezgöçen tarafından geliştirilen bağımsız bir projedir. Tencent "
            "ile bağlantılı değildir; Tencent tarafından desteklenmez ya da onaylanmaz."
        ),
        "kutu_etiketi": (
            "Tencent HY Community License'ın resmî metnini ve Kabul Edilebilir Kullanım "
            "Politikası'nı okudum, kabul ediyorum."
        ),
        "red_iletisi": "Lisans kabul edilmeden HY-MT indirilmez.",
        "tam_metin_etiketi": "Resmî adresi tarayıcıda aç",
    },
}

# Onay ekranında gösterilen alanlar (arayüz bunları olduğu gibi yazar).
_GOSTERILEN_ALANLAR = ("baslik", "giris", "bilesenler", "resmi_not", "beyan", "kutu_etiketi")

# Hugging Face adresi: https://huggingface.co/<kuruluş>/<depo>/resolve/<revizyon>/<dosya>
_HF_YOLU = re.compile(r"^/(?P<depo>[^/]+/[^/]+)/resolve/(?P<revizyon>[^/]+)/(?P<dosya>.+)$")
_KISA_REVIZYON = 7


class LisansHatasi(ValueError):
    """Onay verilemeyen/istenemeyen durum (ileti Türkçe, kullanıcıya gösterilebilir)."""


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def hf_kaynagi(url: str) -> Optional[Dict[str, str]]:
    """Hugging Face ``resolve`` adresinden ``{depo, revizyon, dosya}``; değilse None."""
    try:
        parca = urlparse(url)
    except ValueError:
        return None
    if parca.hostname != "huggingface.co":
        return None
    bulunan = _HF_YOLU.match(unquote(parca.path))
    return bulunan.groupdict() if bulunan else None


def _lisans_alani(bilesen: Mapping[str, Any]) -> Mapping[str, Any]:
    alan = bilesen.get("lisans")
    return alan if isinstance(alan, Mapping) else {}


def onay_gerekli(bilesen: Mapping[str, Any]) -> bool:
    return bool(_lisans_alani(bilesen).get("onay_gerekli"))


def hymt_kaynaklari(bilesen: Mapping[str, Any]) -> Dict[str, str]:
    """HY-MT öğelerinin adreslerinden model deposu/revizyonu ve lisans deposu.

    Lisans metni öğesi, manifestteki ``lisans.metin_sha256`` ile aynı SHA'yı
    taşıyan öğedir; geri kalan Hugging Face öğeleri model dosyalarıdır.
    """
    metin_sha = _lisans_alani(bilesen).get("metin_sha256")
    model: Optional[Dict[str, str]] = None
    lisans: Optional[Dict[str, str]] = None
    for oge in bilesen.get("ogeler") or []:
        kaynak = hf_kaynagi(str(oge.get("url", "")))
        if not kaynak:
            continue
        if metin_sha and oge.get("sha256") == metin_sha:
            lisans = lisans or kaynak
        else:
            model = model or kaynak
    if not model or not lisans:
        raise LisansHatasi("HY-MT öğelerinden model ve lisans kaynağı çıkarılamadı")
    return {"depo": model["depo"], "revizyon": model["revizyon"][:_KISA_REVIZYON],
            "revizyon_tam": model["revizyon"], "lisans_depo": lisans["depo"],
            "lisans_revizyon": lisans["revizyon"]}


def _metni_doldur(sablon: Mapping[str, Any], degerler: Mapping[str, str]) -> Dict[str, Any]:
    dolu: Dict[str, Any] = {}
    for alan, deger in sablon.items():
        if isinstance(deger, str):
            dolu[alan] = deger.format(**degerler) if degerler else deger
        elif isinstance(deger, list):
            dolu[alan] = [d.format(**degerler) if degerler else d for d in deger]
        else:
            dolu[alan] = deger
    return dolu


# ---------------------------------------------------------------------------
# Dışa açık API (K4 sunucu: GET /api/durum, POST /api/lisans)
# ---------------------------------------------------------------------------
def onay_metni(bilesen: Mapping[str, Any]) -> Dict[str, Any]:
    """Bileşenin onay ekranı metni (arayüz bu sözlüğü olduğu gibi gösterir).

    Dönen alanlar: ``bilesen, baslik, giris, bilesenler, resmi_not, beyan,
    kutu_etiketi, red_iletisi, tam_metin_etiketi, tam_metin_url,
    tam_metin_dosyasi, metin_sha256``. Resmî metnin kendisi ``GET
    /api/lisans_metni`` ile gelir (yerel kopya, SHA-256'sı manifestle eşleşirse).

    Onay gerektirmeyen ya da metni olmayan bileşende ``LisansHatasi``.
    """
    kimlik = str(bilesen.get("kimlik"))
    if not onay_gerekli(bilesen):
        raise LisansHatasi(f"{kimlik} bileşeni lisans onayı gerektirmiyor")
    sablon = ONAY_METINLERI.get(kimlik)
    if sablon is None:
        raise LisansHatasi(f"{kimlik} bileşeninin onay metni yok; kurulum onaysız sürmez")
    degerler = hymt_kaynaklari(bilesen) if kimlik == HYMT else {}
    metin = _metni_doldur(sablon, degerler)
    alan = _lisans_alani(bilesen)
    return {
        "bilesen": kimlik,
        **{k: metin[k] for k in _GOSTERILEN_ALANLAR},
        "red_iletisi": metin["red_iletisi"],
        "tam_metin_etiketi": metin["tam_metin_etiketi"],
        "tam_metin_url": alan.get("metin_url") or "",
        "tam_metin_dosyasi": alan.get("metin_dosyasi") or "",
        "metin_sha256": str(alan.get("metin_sha256") or ""),
    }


def _onaylar(duzen: KokDuzeni) -> Dict[str, Any]:
    veri = json_oku(duzen.lisans_onay_dosyasi, None)
    if not isinstance(veri, dict) or veri.get("sema_surumu") != SEMA_SURUMU \
            or not isinstance(veri.get("onaylar"), dict):
        return {"sema_surumu": SEMA_SURUMU, "onaylar": {}}
    return veri


def onay_durumu(duzen: KokDuzeni, bilesen: Mapping[str, Any]) -> Dict[str, Any]:
    """``{"onay_gerekli", "onaylandi", "neden"}`` (B.5 ``bilesenler[].lisans``)."""
    if not onay_gerekli(bilesen):
        return {"onay_gerekli": False, "onaylandi": True, "neden": ""}
    kimlik = str(bilesen.get("kimlik"))
    try:
        metin = onay_metni(bilesen)
    except LisansHatasi as exc:
        return {"onay_gerekli": True, "onaylandi": False, "neden": str(exc)}
    kayit = _onaylar(duzen)["onaylar"].get(kimlik)
    if not isinstance(kayit, dict):
        return {"onay_gerekli": True, "onaylandi": False, "neden": metin["red_iletisi"]}
    if not metin["metin_sha256"] or kayit.get("metin_sha256") != metin["metin_sha256"]:
        return {"onay_gerekli": True, "onaylandi": False,
                "neden": "Lisans metni değişti; yeniden onay gerekiyor."}
    return {"onay_gerekli": True, "onaylandi": True, "neden": ""}


def kabul_et(duzen: KokDuzeni, bilesen: Mapping[str, Any], *,
             uygulama_surumu: str = "") -> Dict[str, Any]:
    """Kullanıcının kabulünü kaydeder (yalnız "kabul ediyorum" kutusu işaretlenince çağrılır).

    Kayıt, kabul edilen resmî metnin kimliğidir: SHA-256 + resmî adres (KARAR 34).
    """
    metin = onay_metni(bilesen)
    if not metin["metin_sha256"]:
        raise LisansHatasi("manifestte lisans metninin SHA-256'sı yok; onay kaydedilemez")
    veri = _onaylar(duzen)
    kayit = {"metin_sha256": metin["metin_sha256"], "metin_url": metin["tam_metin_url"],
             "zaman": simdi(), "uygulama_surumu": uygulama_surumu}
    veri["onaylar"][metin["bilesen"]] = kayit
    json_yaz_atomik(duzen.lisans_onay_dosyasi, veri)
    return dict(kayit)


def geri_al(duzen: KokDuzeni, kimlik: str) -> bool:
    """Onay kaydını siler (ör. kullanıcı kutunun işaretini kaldırdı). Silindiyse True."""
    veri = _onaylar(duzen)
    if veri["onaylar"].pop(kimlik, None) is None:
        return False
    json_yaz_atomik(duzen.lisans_onay_dosyasi, veri)
    return True


def eksik_onaylar(duzen: KokDuzeni, bilesenler: Iterable[Mapping[str, Any]]) -> List[str]:
    """Onayı gerekli ama verilmemiş (ya da geçersizleşmiş) bileşen kimlikleri."""
    return [str(b.get("kimlik")) for b in bilesenler
            if onay_gerekli(b) and not onay_durumu(duzen, b)["onaylandi"]]


# ---------------------------------------------------------------------------
# Tencent NOTICE (lisans 3(d); lisans raporu K6 ve §8.3)
# ---------------------------------------------------------------------------
_NOTICE_ONCESI = "contains the following notice:"
_ACILIS_TIRNAKLARI = "“\""
_KAPANIS_TIRNAKLARI = "”\""


def tencent_notice(lisans_metni: str) -> str:
    """Tencent lisansının 3(d) maddesindeki tırnak içindeki bildirim cümlesi (birebir).

    Cümlenin içinde ikinci bir tırnaklı ad (“Tencent HY”) bulunduğundan ilk
    kapanış tırnağında kesilmez: işaret ifadesinden sonra satır sonuna kadar
    alınır, yalnız dıştaki açılış ve kapanış tırnakları atılır. Bulunamazsa
    ``LisansHatasi`` (NOTICE uydurulmaz).
    """
    for satir in lisans_metni.splitlines():
        yer = satir.find(_NOTICE_ONCESI)
        if yer < 0:
            continue
        cumle = satir[yer + len(_NOTICE_ONCESI):].strip()
        if cumle[:1] in _ACILIS_TIRNAKLARI and cumle[-1:] in _KAPANIS_TIRNAKLARI:
            cumle = cumle[1:-1].strip()
            if cumle:
                return cumle
    raise LisansHatasi("Tencent lisans metninde 3(d) bildirim cümlesi bulunamadı")
