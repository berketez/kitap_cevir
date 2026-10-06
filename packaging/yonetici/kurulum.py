"""Kurulum işi: plan, sıralı bileşen kurulumu, iptal, sürdürme, onarım, kaldırma.

Yalnız stdlib (TASARIM A.2). Dayanak: TASARIM C, D, E, B.5.

Kulvarlar arası sözleşme
========================

K4 (``sunucu.py``) bu sınıfı şöyle kullanır::

    k = Kurulum(kok, uygulama, manifest, indirici=<K2 uyarlayıcısı>,
                gunluk_yolu=<kurulum günlüğü>)
    k.bilesen_ozetleri()      # GET /api/durum  "bilesenler"
    k.tex_algilanan()         # GET /api/durum  "tex_algilanan"
    k.uygulama_hazir()        # GET /api/durum  "uygulama_hazir"
    k.lisans_metni(kimlik)    # onay ekranı metni (lisans.onay_metni)
    k.lisans_kabul(kimlik)    # POST /api/lisans {bilesen, kabul: true}
    k.lisans_geri_al(kimlik)  # kutunun işareti kaldırılınca
    k.plan(tex=..., istege_bagli=[...], onar=[...])   # POST /api/plan (PlanHatasi → 400)
    k.baslat(plan_kimligi, olay)                     # POST /api/kur (KurulumHatasi → 409)
    k.iptal()                                        # POST /api/iptal
    k.calisiyor                                      # kapanış onayı
    k.kaldir(kimlik)                                 # POST /api/kaldir

``olay(tur, veri)`` iş parçacığından çağrılır (SSE kuyruğu iş parçacığı
güvenli olmalı). Olaylar (B.5): ``adim_basladi {bilesen, adim, ad, sira,
baslangic}``, ``adim_bitti {bilesen, adim, sure_sn, atlandi}``, ``sayac
{bilesen, i, n, birim}``, ``bekleme {bilesen, neden, kalan_sn}``, ``hata
{bilesen, kod, ileti, ayrinti, ayrinti_gunluk}``, ``bitti {sonuc,
kurulanlar, kalanlar}``; ``ilerleme {bilesen, oge, bayt, toplam_bayt,
hiz_bayt_sn}`` ve indirmedeki ``bekleme`` olaylarını indirici yayar.

K2 (indirici) sözleşmesi::

    indirici(ogeler: Sequence[Mapping], istek: IndirmeIstegi) -> None

- Her öğeyi ``istek.kok / oge["hedef"]``'e indirir ve SHA-256'sını
  doğrular; hedefte doğru dosya zaten varsa indirmez. ``.part`` + ``Range``,
  ``yeniden_kullan`` (APFS klonu), 403/410/429 kuralları indiricinindir.
- İlerlemeyi ``istek.olay("ilerleme", {...})`` ile gerçek baytla bildirir.
- ``istek.iptal`` kurulunca en geç bir parça sonra durur ve bir istisna
  yükseltir (tür serbest; kurulum ``iptal`` bayrağına bakıp işi "iptal"
  sayar). ``.part`` dosyaları kalır.
- Başarısızlıkta istisna yükseltir; istisnada ``kod`` ve ``ileti``
  öznitelikleri varsa (Türkçe) kullanıcıya olduğu gibi gösterilir.

Bu sözleşmenin K2'ye tek uyarlayıcısı ``IndiriciUyarlayici``'dir (aşağıda);
yönetici de aynı sınıfı kullanır.

macOS koşulu (``kosul.macos``: ``"14"``, ``"26+"``), macOS'a göre boyut
seçimi ve disk ön denetimi (geçici tepe, ek alan) burada yeniden yazılmaz:
tek kaynak ``manifest.py``'dir (``oge_uygun_mu``, ``bayt_sec``,
``ek_alan_tepesi``).

Manifest adımlarının alanları (C.2 ``adimlar[]``; K7 sürüm betiği böyle yazar)
------------------------------------------------------------------------------

==================  ===========================================================
tur                 alanlar
==================  ===========================================================
uv_kur              ``oge`` (uv arşivi), ``uye`` (varsayılan
                    ``uv-aarch64-apple-darwin/uv``) → ``<kök>/bin/uv``
tekerlek_kur        ``grup``, ``kilit`` (uygulamaya göre; varsayılan
                    ``packaging/requirements/<grup>.txt``), ``kaynaktan``
                    (derlenebilir paketler), ``derleme_ortami`` ({değişken: değer})
arsiv_ac            ``oge``, ``hedef`` (köke göre klasör), ``uyeler``
                    ({arşivdeki yol: hedefe göre ad}; yoksa bütün arşiv)
dosya_yerlestir     ``kaynak`` (``uygulama:<yol>`` | ``kok:<yol>``), ``hedef``, ``kip``
tectonic_isit       ``belge`` (varsayılan ``packaging/tex/isitma.tex``)
mactex_pkg_ac       ``oge`` (MacTeX.pkg), ``yol`` (beklenen xelatex; varsayılan
                    ``/Library/TeX/texbin/xelatex``)
tex_algila_sina     ``yol`` (isteğe bağlı: yalnız bu xelatex)
lisans_yaz          ``oge`` (lisans metni), ``dizin`` (model klasörü),
                    ``lisans_klasoru`` (``LICENSES/`` altı), ``dosya_adi``
surya_manifest_yaz  ``kok`` (varsayılan ``modeller/datalab``)
==================  ===========================================================

``dogrulama`` tek nesne ya da liste: ``python_ice_aktar {moduller}``,
``dosyalar_sha``, ``tex_derle``, ``komut {argv, ortam}`` (argv'de
``{kok}``, ``{venv_python}``, ``{uygulama}`` yer tutucuları).

Öğelerin ömrü: ``tur == "tekerlek"`` öğeleri ``tekerlek_kur``'un, ``oge``
alanıyla anılanlar o adımın tüketimidir; adım başarıyla bitince hedefleri
``indirilen/`` altındaysa silinir. Geri kalanlar (modeller, yazıtipi) kalıcıdır
ve bileşenin başında iner.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set

try:
    from . import ayarlar, disk, donanim, durum, indirici, lisans, manifest
    from .adimlar import (ADIMLAR, OGE_TUKETEN_ADIMLAR, TEKERLEK_ADIMI, TEKERLEK_TURU,
                          paketler, tex)
    from .adimlar.ortak import (KOD_DOGRULAMA, KOD_INDIRME, KOD_MANIFEST, AdimHatasi,
                                AdimSonucu, Baglam, IptalEdildi, gecen)
except ImportError:  # düz betik olarak yüklendi (sys.path[0] = packaging/yonetici)
    import ayarlar  # type: ignore[no-redef]
    import disk  # type: ignore[no-redef]
    import donanim  # type: ignore[no-redef]
    import durum  # type: ignore[no-redef]
    import indirici  # type: ignore[no-redef]
    import lisans  # type: ignore[no-redef]
    import manifest  # type: ignore[no-redef]
    from adimlar import (ADIMLAR, OGE_TUKETEN_ADIMLAR, TEKERLEK_ADIMI,  # type: ignore
                         TEKERLEK_TURU, paketler, tex)
    from adimlar.ortak import (KOD_DOGRULAMA, KOD_INDIRME, KOD_MANIFEST,  # type: ignore
                               AdimHatasi, AdimSonucu, Baglam, IptalEdildi, gecen)

logger = logging.getLogger("kitapcevir.yonetici.kurulum")

# ---------------------------------------------------------------------------
# Sabitler (yalnız bu modülde)
# ---------------------------------------------------------------------------
ZORUNLU = "zorunlu"
ISTEGE_BAGLI = "istege_bagli"
SECIM = "secim"
TEX_GRUBU = "tex"
TEX_HAFIF = "tex-hafif"              # TeX türü değişirken kaldırılabilen tek seçim (B.5)
ON_KOSUL_TEX_KURULU = "tex_kurulu"   # on_kosul {"algila": "tex_kurulu"}
PLAN_KIMLIGI_UZUNLUGU = 16
ADIM_ANAHTARI_UZUNLUGU = 12
SAKLANAN_PLAN_SAYISI = 8
GUNCELLEME_ETIKETI = "guncelleme"    # fazla paket temizliğinin olay etiketi (bileşen değil)
# "Kaldır"ın hiçbir bileşen kaydıyla bütün olarak silemeyeceği klasörler (köke göre):
# kullanıcı verisi ve başka bileşenlerin ortak kökleri.
KORUNAN_KLASORLER = ("veri", "veri/models", "veri/output", "durum", "venv", "app", "bin",
                     "calisma_zamani", "LICENSES", "indirilen", "modeller")

SONUC_TAMAM = "tamam"
SONUC_IPTAL = "iptal"
SONUC_HATA = "hata"
SONUC_SURUYOR = "suruyor"

KOD_IS_SURUYOR = "is_suruyor"
KOD_PLAN_YOK = "plan_yok"
KOD_PLAN_ESKIDI = "plan_eskidi"
KOD_ONAY_EKSIK = "onay_eksik"
KOD_DISK = "disk_yetersiz"
KOD_DISK_DOLU = "disk_dolu"
KOD_KALDIRILAMAZ = "kaldirilamaz"
KOD_BEKLENMEYEN = "beklenmeyen"
KOD_IPTAL = "iptal"

# Arayüzün kurulum listesi için adım adları (E.5)
ADIM_ADLARI: Dict[str, str] = {
    "indir": "Dosyalar indiriliyor",
    "uv_kur": "uv kuruluyor",
    "tekerlek_kur": "Paketler kuruluyor",
    "arsiv_ac": "Arşiv açılıyor",
    "dosya_yerlestir": "Dosya yerleştiriliyor",
    "tectonic_isit": "TeX paketleri indiriliyor (ısıtma derlemesi)",
    "mactex_pkg_ac": "MacTeX kurulum programı",
    "tex_algila_sina": "TeX şablon sınaması",
    "lisans_yaz": "Lisans dosyaları yazılıyor",
    "surya_manifest_yaz": "Model klasörleri denetleniyor",
    "dogrulama": "Doğrulanıyor",
}

MSG_ZORUNLU = "Zorunlu bileşen"
MSG_TEX_KURULU_YOK = "Bu Mac'te kurulu TeX bulunamadı."
MSG_IS_SURUYOR = "Şu anda bir kurulum işi sürüyor; bitmesini bekle ya da iptal et."
MSG_PLAN_YOK = "Bu plan bulunamadı; seçimi yeniden gönder."
MSG_PLAN_ESKIDI = "Bileşenlerin durumu plan yapıldıktan sonra değişti; plan yeniden hazırlanmalı."
MSG_ONAY_EKSIK = "Lisans onayı eksik: {adlar}. Onay verilmeden kurulum başlamaz."
MSG_DISK = "Diskte yeterli boş alan yok: {eksik} bayt daha gerekiyor."
MSG_DISK_DOLU = "Diskte yer kalmadı; indirilen kısım saklandı, yer açınca kaldığı yerden sürer."
MSG_KALDIRILAMAZ = "Bu bileşen kaldırılamaz: {neden}"
MSG_TEX_ETKIN = "etkin TeX bu; önce başka bir TeX türüne geç"
MSG_IPTAL = "Kurulum iptal edildi; indirilen kısım saklandı, kaldığı yerden sürer."
MSG_SINAMA_BELGESI_YOK = "Şablon sınama belgesi bulunamadı: {yol}"


class KurulumHatasi(Exception):
    """Kurulum işi başlatılamadı / kaldırılamadı / başarısız (Türkçe ileti; K4 409 döner)."""

    def __init__(self, kod: str, ileti: str, ayrinti: str = "", bilesen: str = ""):
        super().__init__(ileti)
        self.kod = kod
        self.ileti = ileti
        self.ayrinti = ayrinti
        self.bilesen = bilesen


class PlanHatasi(KurulumHatasi):
    """Plan isteği geçersiz (bilinmeyen bileşen, seçilemeyen seçenek; K4 400 döner)."""


@dataclass
class IndirmeIstegi:
    """İndiriciye verilen bağlam (K2 sözleşmesi, yukarıda)."""
    kok: Path
    bilesen: str
    olay: Callable[[str, Dict[str, Any]], None]
    iptal: threading.Event
    macos_ana: Optional[int]


Indirici = Callable[[Sequence[Mapping[str, Any]], IndirmeIstegi], None]


def macos_ana_surumu() -> Optional[int]:
    """Bu Mac'in macOS ana sürümü (``26.7`` → 26); okunamazsa None."""
    surum = platform.mac_ver()[0]
    try:
        return int(surum.split(".")[0]) if surum else None
    except ValueError:
        return None


class IndiriciUyarlayici:
    """``indirici(ogeler, istek)`` (yukarıdaki sözleşme) çağrısını K2'nin
    ``indirici.Indirici``'siyle yapar. TEK uyarlayıcıdır: yönetici
    (``yonetici.IndiriciUyarlayici`` bu sınıfın kendisidir) ve ``indirici``
    verilmeyen ``Kurulum`` bunu kullanır.

    - İş başına tek ``Indirici``: kurulum aynı işte her adım için ayrı çağırır;
      ``istek.iptal`` olayı aynı kaldıkça aynı nesne kullanılır. Böylece hız
      bütün işin ağdan gelen baytından ölçülür ve "Sürdür" yeni nesneyle başlar
      (``baslat`` her işe yeni iptal olayı verir).
    - İptal: ``istek.iptal`` kurulunca ``Indirici.iptal()`` çağrılır; açık
      bağlantılar kesilir, ``.part`` dosyaları kalır, ``IptalEdildi`` yükselir.
    - Hata: ``IndirmeHatasi`` (``kod``/``ileti`` Türkçe) yükselir; kurulum onu
      olduğu gibi kullanıcıya iletir.
    """

    def __init__(self, surum: str, *,
                 indirici_sinifi: Optional[Callable[..., Any]] = None,
                 ca_dosyasi: Optional[os.PathLike | str] = None):
        self.surum = surum
        self._sinif = indirici_sinifi if indirici_sinifi is not None else indirici.Indirici
        self._ca = ca_dosyasi
        self._kilit = threading.Lock()
        self._ind: Any = None
        self._iptal_olayi: Optional[threading.Event] = None

    def _is_indiricisi(self, istek: IndirmeIstegi) -> Any:
        with self._kilit:
            if (self._ind is None or self._iptal_olayi is not istek.iptal
                    or getattr(self._ind, "durduruldu", False)):
                ek = {"ca_dosyasi": self._ca} if self._ca is not None else {}
                self._ind = self._sinif(istek.kok, surum=self.surum, olay=istek.olay, **ek)
                self._iptal_olayi = istek.iptal
            return self._ind

    def __call__(self, ogeler: Sequence[Mapping[str, Any]], istek: IndirmeIstegi) -> None:
        if istek.iptal.is_set():
            raise IptalEdildi()
        ind = self._is_indiricisi(istek)
        liste = [indirici.Oge.manifestten(o, istek.bilesen) for o in ogeler]
        bitti = threading.Event()

        def _iptal_izle() -> None:
            while not bitti.wait(ayarlar.IPTAL_YOKLAMA_SN):
                if istek.iptal.is_set():
                    ind.iptal()
                    return

        izleyici = threading.Thread(target=_iptal_izle, name="indirme-iptal", daemon=True)
        izleyici.start()
        try:
            sonuc = ind.indir(liste)
        finally:
            bitti.set()
            izleyici.join(timeout=1)
        if sonuc.basarili:
            return
        if istek.iptal.is_set() or sonuc.durum == indirici.IS_IPTAL:
            raise IptalEdildi()
        hata = sonuc.hata
        if hata is None:
            hatalar = sonuc.hatalar()
            hata = hatalar[0].hata if hatalar and hatalar[0].hata else indirici.IndirmeHatasi(
                indirici.HATA_BEKLENMEYEN, "İndirme tamamlanamadı.")
        raise hata


def varsayilan_indirici(manifest_verisi: Mapping[str, Any]) -> Indirici:
    """``indirici`` verilmezse kullanılan: ``IndiriciUyarlayici`` (manifestin sürümüyle)."""
    uyg = manifest_verisi.get("uygulama") if isinstance(manifest_verisi, Mapping) else None
    surum = str((uyg or {}).get("surum") or "0.0.0") if isinstance(uyg, Mapping) else "0.0.0"
    return IndiriciUyarlayici(surum)


def tex_sina(*, duzen: durum.KokDuzeni, uygulama: os.PathLike | str,
             xelatex: os.PathLike | str, iptal: Optional[threading.Event] = None
             ) -> Dict[str, Any]:
    """Kurulu bir xelatex'in şablon sınaması (K4 ``VarsayilanArka.tex_sina`` sözleşmesi).

    Dönüş: ``{"gecti", "ilk_hata", "surum", "hata", "sure_sn"}`` (``ilk_hata`` =
    ``hata`` = günlükteki ilk hata satırı). Sonuç ``durum/tex_algilama.json``'da
    önbelleklidir: xelatex ya da şablon değişmedikçe yeniden derlenmez.
    """
    belge = Path(uygulama) / tex.ISITMA_BELGESI
    if not belge.is_file():
        ileti = MSG_SINAMA_BELGESI_YOK.format(yol=belge)
        return {"gecti": False, "ilk_hata": ileti, "hata": ileti, "surum": "", "sure_sn": 0.0}
    bilgi = tex.xelatex_bilgisi(duzen, Path(uygulama), os.fspath(xelatex), sina=True,
                                iptal=iptal)
    sinama = bilgi.get("sinama") or {}
    hata = str(sinama.get("hata") or "")
    return {"gecti": bool(sinama.get("gecti")), "ilk_hata": hata, "hata": hata,
            "surum": str(bilgi.get("surum") or ""), "sure_sn": sinama.get("sure_sn")}


def _sessiz(tur: str, veri: Dict[str, Any]) -> None:
    return None


def adim_anahtari(sira: int, adim: Mapping[str, Any]) -> str:
    """Adım kaydının anahtarı: sıra + tür + alanların özeti (alan değişirse yeniden koşar)."""
    return f"{sira}-{adim.get('tur')}-{durum.kanonik_sha256(dict(adim))[:ADIM_ANAHTARI_UZUNLUGU]}"


def _oge_kaydi_anahtari(oge: Mapping[str, Any]) -> str:
    return f"oge:{oge.get('kimlik')}"


# ---------------------------------------------------------------------------
# Kurulum
# ---------------------------------------------------------------------------
class Kurulum:
    """Bir kurulum köküne ve manifeste bağlı kurulum yöneticisi (aynı anda tek iş)."""

    def __init__(self, kok: Optional[os.PathLike | str] = None,
                 uygulama: Optional[os.PathLike | str] = None,
                 manifest: Optional[Mapping[str, Any]] = None, *,
                 indirici: Optional[Indirici] = None,
                 gunluk_yolu: Optional[os.PathLike | str] = None,
                 macos_ana: Optional[int] = None, disk_olc: bool = False,
                 duzen: Optional[durum.KokDuzeni] = None,
                 olay: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 iptal: Optional[threading.Event] = None):
        """İki çağrı biçimi de geçerlidir:

        - ``Kurulum(kok, uygulama, manifest, indirici=...)`` (bu modülün API'si);
        - ``Kurulum(duzen=KokDuzeni, manifest=dict, uygulama=Path, olay=..., iptal=Event)``
          (başlıksız çağrı biçimi). ``olay``/``iptal`` burada verilirse
          ``calistir`` onları kullanır.

        ``indirici`` verilmezse tek uyarlayıcı ``IndiriciUyarlayici`` (K2'nin
        ``indirici.Indirici``'si üzerinde) kurulur.
        """
        if duzen is not None:
            kok = duzen.kok
        if kok is None or uygulama is None or manifest is None:
            raise TypeError("Kurulum: kok (ya da duzen), uygulama ve manifest gerekli")
        self.duzen = durum.KokDuzeni(kok)
        self.defter = durum.KurulumDurumu(self.duzen)
        self.uygulama = Path(uygulama)
        self.manifest = manifest
        self.indirici = indirici if indirici is not None else varsayilan_indirici(manifest)
        self._varsayilan_olay = olay
        self._dis_iptal = iptal
        self.gunluk_yolu = str(gunluk_yolu) if gunluk_yolu else ""
        self.macos_ana = macos_ana if macos_ana is not None else macos_ana_surumu()
        self.disk_olc = disk_olc
        self._kilit = threading.RLock()
        self._iptal = threading.Event()
        self._is: Optional[threading.Thread] = None
        self._suren_plan: Optional[Dict[str, Any]] = None
        self._planlar: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Manifest erişimi
    # ------------------------------------------------------------------
    @property
    def bilesenler(self) -> List[Mapping[str, Any]]:
        return [b for b in (self.manifest.get("bilesenler") or []) if isinstance(b, Mapping)]

    @property
    def gruplar(self) -> List[Mapping[str, Any]]:
        return [g for g in (self.manifest.get("gruplar") or []) if isinstance(g, Mapping)]

    def bilesen(self, kimlik: str) -> Mapping[str, Any]:
        for b in self.bilesenler:
            if b.get("kimlik") == kimlik:
                return b
        raise PlanHatasi(KOD_MANIFEST, f"Bilinmeyen bileşen: {kimlik}")

    @property
    def uygulama_surumu(self) -> str:
        uyg = self.manifest.get("uygulama")
        return str((uyg or {}).get("surum") or "") if isinstance(uyg, Mapping) else ""

    def _macos_metni(self) -> str:
        """``manifest.py``'nin beklediği ana sürüm ("26"); bilinmiyorsa "" (tahmin yok)."""
        return str(self.macos_ana) if self.macos_ana is not None else ""

    def _macos_uygun(self, oge: Mapping[str, Any]) -> bool:
        """``manifest.oge_uygun_mu`` (tek kaynak): ``"14"`` yalnız 14, ``"26+"`` 26 ve sonrası.

        macOS sürümü okunamadıysa koşullu öğe seçilmez (hangi tekerleğin uyduğu
        bilinmez); koşulsuz öğe her zaman iner.
        """
        try:
            return manifest.oge_uygun_mu(oge, self._macos_metni())
        except manifest.ManifestHatasi:
            return False

    def uygun_ogeler(self, bilesen: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        """Bu macOS sürümüne uyan öğeler (``kosul.macos``; C.1-4)."""
        return [o for o in (bilesen.get("ogeler") or [])
                if isinstance(o, Mapping) and self._macos_uygun(o)]

    def boyut(self, bilesen: Mapping[str, Any], alan: str) -> Optional[int]:
        """Manifestteki ölçülmüş boyut: ``manifest.bayt_sec`` (tek kaynak).

        ``hepsi`` varsa o; yoksa bu macOS'un anahtarı; o da yoksa ondan küçük en
        yakın ölçüm (macOS 27'de ``26``). Ölçüm yoksa (ya da macOS bilinmiyorsa
        ve ``hepsi`` yoksa) None: plan bunu "boyutu bilinmeyen" diye gösterir.
        """
        boyut = bilesen.get("boyut")
        sozluk = boyut.get(alan) if isinstance(boyut, Mapping) else None
        if not isinstance(sozluk, Mapping) or not sozluk:
            return None
        try:
            return manifest.bayt_sec(sozluk, self._macos_metni())
        except (manifest.ManifestHatasi, TypeError, ValueError):
            return None

    def _tuketilen_kimlikler(self, bilesen: Mapping[str, Any]) -> Set[str]:
        adimlar = [a for a in (bilesen.get("adimlar") or []) if isinstance(a, Mapping)]
        tuketilen = {str(a["oge"]) for a in adimlar
                     if a.get("tur") in OGE_TUKETEN_ADIMLAR and a.get("oge")}
        if any(a.get("tur") == TEKERLEK_ADIMI for a in adimlar):
            tuketilen |= {str(o.get("kimlik")) for o in bilesen.get("ogeler") or []
                          if isinstance(o, Mapping) and o.get("tur") == TEKERLEK_TURU}
        return tuketilen

    def kalici_ogeler(self, bilesen: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        tuketilen = self._tuketilen_kimlikler(bilesen)
        return [o for o in self.uygun_ogeler(bilesen) if str(o.get("kimlik")) not in tuketilen]

    def gecici_bayt(self, bilesen: Mapping[str, Any]) -> int:
        """Bileşenin ``indirilen/``'de geçici duran öğelerinin toplamı (disk tepesi için).

        Tanım ``manifest.gecici_mi``'dir: tüketilen öğe yalnız ``indirilen/``
        altındaysa silinir (``_tuketilenleri_sil``); başka yerdeki kalıcıdır.
        """
        return manifest.gecici_toplami(self.uygun_ogeler(bilesen))

    def disk_kalemi(self, bilesen: Mapping[str, Any]) -> "manifest.DiskKalemi":
        """Bileşenin disk ön denetimi kalemi (diskte duran baytlar sayılır)."""
        return manifest.disk_kalemi(str(bilesen.get("kimlik")), self.uygun_ogeler(bilesen),
                                    self.boyut(bilesen, "disk_bayt") or 0, kok=self.duzen.kok)

    # ------------------------------------------------------------------
    # Durum (GET /api/durum)
    # ------------------------------------------------------------------
    @property
    def calisiyor(self) -> bool:
        with self._kilit:
            return self._is is not None and self._is.is_alive()

    def tex_algilanan(self) -> List[Dict[str, Any]]:
        return tex.tex_algila(self.duzen, self.uygulama, sina=True)

    def _tex_kurulu_hukmu(self, algilanan: Optional[List[Dict[str, Any]]] = None
                          ) -> Dict[str, Any]:
        algilanan = self.tex_algilanan() if algilanan is None else algilanan
        if not algilanan:
            return {"gorunur": False, "secilebilir": False, "neden": MSG_TEX_KURULU_YOK}
        for aday in algilanan:
            if (aday.get("sinama") or {}).get("gecti"):
                return {"gorunur": True, "secilebilir": True, "neden": "", "xelatex": aday["yol"],
                        "surum": aday.get("surum", "")}
        hata = (algilanan[0].get("sinama") or {}).get("hata") or tex.MSG_TEX_HATA_BILINMIYOR
        return {"gorunur": True, "secilebilir": False,
                "neden": tex.MSG_TEX_SINAMA.format(hata=hata)}

    def _durum(self, bilesen: Mapping[str, Any]) -> tuple:
        return durum.bilesen_durumu(self.duzen, self.defter, bilesen)

    def _etkin_tex(self) -> Optional[str]:
        kayit = durum.tex_oku(self.duzen)
        return str(kayit.get("tur")) if kayit else None

    def bilesen_ozetleri(self) -> List[Dict[str, Any]]:
        """B.5 ``bilesenler[]``: durum, seçilebilirlik, boyut, lisans (sayılar gerçek)."""
        suren: Set[str] = set()
        with self._kilit:
            if self.calisiyor and self._suren_plan:
                suren = set(self._suren_plan.get("kalanlar") or [])
        etkin_tex = self._etkin_tex()
        algilanan: Optional[List[Dict[str, Any]]] = None
        ozetler: List[Dict[str, Any]] = []
        for b in self.bilesenler:
            kimlik = str(b.get("kimlik"))
            zorunluluk = b.get("zorunluluk")
            hal, hal_nedeni = self._durum(b)
            if kimlik in suren:
                hal = durum.DURUM_KURULUYOR
            ozet: Dict[str, Any] = {
                "kimlik": kimlik, "ad": b.get("ad", ""), "aciklama": b.get("aciklama", ""),
                "zorunluluk": zorunluluk, "grup": b.get("grup"), "surum": b.get("surum", ""),
                "durum": hal, "durum_nedeni": hal_nedeni,
                "kilitli": zorunluluk == ZORUNLU, "gorunur": True,
                "secilebilir": zorunluluk != ZORUNLU, "neden": "", "uyari": "", "ipucu": "",
                "bilgi": "", "etkin": None,
                "boyut": {"indirme_bayt": self.boyut(b, "indirme_bayt"),
                          "disk_bayt": self.boyut(b, "disk_bayt"),
                          "olcum_notu": (b.get("boyut") or {}).get("olcum_notu", "")},
                "lisans": {k: v for k, v in lisans.onay_durumu(self.duzen, b).items()},
            }
            if zorunluluk == ZORUNLU:
                ozet["neden"] = MSG_ZORUNLU
            kapi = donanim.bilesen_kapisi(self.uygulama, b.get("donanim_kosulu"), bilesen=b)
            for alan in ("uyari", "ipucu", "bilgi"):
                ozet[alan] = kapi[alan]
            if not kapi["secilebilir"]:
                ozet.update(secilebilir=False, neden=kapi["neden"])
            on_kosul = b.get("on_kosul")
            if isinstance(on_kosul, Mapping) and on_kosul.get("algila") == ON_KOSUL_TEX_KURULU:
                algilanan = self.tex_algilanan() if algilanan is None else algilanan
                hukum = self._tex_kurulu_hukmu(algilanan)
                ozet["gorunur"] = hukum["gorunur"]
                if not hukum["secilebilir"]:
                    ozet.update(secilebilir=False, neden=hukum["neden"])
                elif hukum.get("xelatex"):
                    ozet["bilgi"] = ozet["bilgi"] or f"{hukum['xelatex']} ({hukum['surum']})"
            if b.get("grup") == TEX_GRUBU:
                ozet["etkin"] = etkin_tex == kimlik
            ozet["kaldirilabilir"] = self._kaldirma_engeli(b, etkin_tex) is None \
                and hal in (durum.DURUM_KURULU, durum.DURUM_BOZUK, durum.DURUM_GUNCELLENECEK)
            ozetler.append(ozet)
        return ozetler

    def uygulama_hazir(self) -> bool:
        """Zorunlular kurulu ve zorunlu her seçim grubunun etkin üyesi kurulu mu?"""
        for b in self.bilesenler:
            if b.get("zorunluluk") == ZORUNLU and self._durum(b)[0] != durum.DURUM_KURULU:
                return False
        etkin_tex = durum.tex_oku(self.duzen)
        for g in self.gruplar:
            if not g.get("zorunlu"):
                continue
            uyeler = {str(b.get("kimlik")): b for b in self.bilesenler
                      if b.get("grup") == g.get("kimlik")}
            if g.get("kimlik") == TEX_GRUBU:
                if not etkin_tex or etkin_tex.get("tur") not in uyeler:
                    return False
                if not os.access(str(etkin_tex.get("xelatex")), os.X_OK):
                    return False
                if self._durum(uyeler[str(etkin_tex["tur"])])[0] != durum.DURUM_KURULU:
                    return False
            elif not any(self._durum(b)[0] == durum.DURUM_KURULU for b in uyeler.values()):
                return False
        return True

    # ------------------------------------------------------------------
    # Lisans (POST /api/lisans)
    # ------------------------------------------------------------------
    def lisans_metni(self, kimlik: str) -> Dict[str, Any]:
        return lisans.onay_metni(self.bilesen(kimlik))

    def lisans_kabul(self, kimlik: str) -> Dict[str, Any]:
        return lisans.kabul_et(self.duzen, self.bilesen(kimlik),
                               uygulama_surumu=self.uygulama_surumu)

    def lisans_geri_al(self, kimlik: str) -> bool:
        self.bilesen(kimlik)
        return lisans.geri_al(self.duzen, kimlik)

    # ------------------------------------------------------------------
    # Plan (POST /api/plan)
    # ------------------------------------------------------------------
    def _secim_grubu_uyeleri(self, grup: str) -> List[Mapping[str, Any]]:
        return [b for b in self.bilesenler if b.get("grup") == grup]

    def _sirala(self, kimlikler: Set[str]) -> List[str]:
        """Bağımlılık sırası; eşitlikte manifestteki sıra (C.1-3: çekirdek → mlx → pdf → …)."""
        sira = {str(b.get("kimlik")): i for i, b in enumerate(self.bilesenler)}
        kalan = set(kimlikler)
        sonuc: List[str] = []
        while kalan:
            hazir = sorted((k for k in kalan
                            if not (set(self.bilesen(k).get("bagimliliklar") or []) & kalan)),
                           key=lambda k: sira[k])
            if not hazir:
                raise PlanHatasi(KOD_MANIFEST, "Manifestte döngüsel bağımlılık var: "
                                               + ", ".join(sorted(kalan)))
            sonuc.append(hazir[0])
            kalan.discard(hazir[0])
        return sonuc

    def plan(self, tex: Optional[str] = None, istege_bagli: Iterable[str] = (),
             onar: Iterable[str] = (), secimler: Optional[Mapping[str, str]] = None
             ) -> Dict[str, Any]:
        """Kurulum planı: sıralı bileşenler, gerçek boyut toplamları, disk ve onay denetimi.

        ``tex`` = TeX türü (``tex-hafif`` / ``tex-tam`` / ``tex-kurulu``);
        ``secimler`` genel biçimdir ({grup: kimlik}). Seçim verilmeyen zorunlu
        grupta etkin üye korunur, yoksa grubun ``varsayilan``'ı alınır.
        ``onar`` kurulu da olsa yeniden kurulacak bileşenlerdir (E.7 "Onar").
        """
        secimler = dict(secimler or {})
        if tex:
            secimler[TEX_GRUBU] = tex
        istenen: Set[str] = {str(b.get("kimlik")) for b in self.bilesenler
                             if b.get("zorunluluk") == ZORUNLU}
        etkin_tex = self._etkin_tex()
        for g in self.gruplar:
            gk = str(g.get("kimlik"))
            uyeler = {str(b.get("kimlik")) for b in self._secim_grubu_uyeleri(gk)}
            secilen = secimler.get(gk)
            if secilen is None:
                if not g.get("zorunlu"):
                    continue
                secilen = etkin_tex if (gk == TEX_GRUBU and etkin_tex in uyeler) \
                    else str(g.get("varsayilan"))
            if secilen not in uyeler:
                raise PlanHatasi(KOD_MANIFEST, f"'{gk}' grubunda '{secilen}' seçeneği yok.")
            b = self.bilesen(secilen)
            on_kosul = b.get("on_kosul")
            if isinstance(on_kosul, Mapping) and on_kosul.get("algila") == ON_KOSUL_TEX_KURULU:
                hukum = self._tex_kurulu_hukmu()
                if not hukum["secilebilir"]:
                    raise PlanHatasi(KOD_MANIFEST, hukum["neden"])
            secimler[gk] = secilen
            istenen.add(secilen)
        for kimlik in istege_bagli:
            b = self.bilesen(str(kimlik))
            if b.get("zorunluluk") != ISTEGE_BAGLI:
                raise PlanHatasi(KOD_MANIFEST, f"'{kimlik}' isteğe bağlı bir bileşen değil.")
            kapi = donanim.bilesen_kapisi(self.uygulama, b.get("donanim_kosulu"), bilesen=b)
            if not kapi["secilebilir"]:
                raise PlanHatasi(KOD_MANIFEST, kapi["neden"])
            istenen.add(str(kimlik))
        onar_kume = {str(k) for k in onar}
        for kimlik in onar_kume:
            self.bilesen(kimlik)
        istenen |= onar_kume
        # Bağımlılıkların kapanışı
        kuyruk = list(istenen)
        while kuyruk:
            for bag in self.bilesen(kuyruk.pop()).get("bagimliliklar") or []:
                if str(bag) not in istenen:
                    istenen.add(str(bag))
                    kuyruk.append(str(bag))
        sirali = self._sirala(istenen)

        kurulacak: List[str] = []
        hazir: List[str] = []
        for kimlik in sirali:
            hal = self._durum(self.bilesen(kimlik))[0]
            (hazir if hal == durum.DURUM_KURULU and kimlik not in onar_kume
             else kurulacak).append(kimlik)
        kb = [self.bilesen(k) for k in kurulacak]
        eksik_onaylar = lisans.eksik_onaylar(self.duzen, kb)
        indirme = [self.boyut(b, "indirme_bayt") for b in kb]
        disk_bayt = [self.boyut(b, "disk_bayt") for b in kb]
        boyutu_bilinmeyen = [k for k, i, d in zip(kurulacak, indirme, disk_bayt)
                             if i is None or d is None]
        # Disk ön denetimi: tek tanım manifest.py'de (geçici tepe + ek alan; kurulum
        # sırasıyla, diskte yarım ya da yerinde duran baytlar düşülerek).
        kalemler = [self.disk_kalemi(b) for b in kb]
        gecici_tepe = manifest.gecici_tepe(kalemler)
        ihtiyac = manifest.ek_alan_tepesi(kalemler)
        denetim = disk.denetle(self.duzen.kok, ihtiyac)
        adimlar = []
        for b in kb:
            for tur in ["indir"] + [str(a.get("tur")) for a in b.get("adimlar") or []
                                    if isinstance(a, Mapping)] + ["dogrulama"]:
                adimlar.append({"bilesen": str(b.get("kimlik")), "bilesen_adi": b.get("ad", ""),
                                "adim": tur, "ad": ADIM_ADLARI.get(tur, tur)})
        govde = {"kurulacak": kurulacak, "onar": sorted(onar_kume), "secimler": secimler,
                 "uygulama": self.uygulama_surumu, "macos": self.macos_ana,
                 "imzalar": {k: durum.bilesen_imzasi(self.bilesen(k)) for k in kurulacak},
                 "durumlar": {k: self._durum(self.bilesen(k))[0] for k in sirali}}
        kimlik = durum.kanonik_sha256(govde)[:PLAN_KIMLIGI_UZUNLUGU]
        plan = {
            "plan_kimligi": kimlik,
            "bilesenler": kurulacak,
            "kurulu": hazir,
            "onar": sorted(onar_kume),
            "secimler": secimler,
            "istege_bagli": sorted(str(k) for k in istege_bagli),
            "adimlar": adimlar,
            "indirme_bayt": sum(i or 0 for i in indirme),
            "disk_bayt": sum(d or 0 for d in disk_bayt),
            "gecici_bayt": gecici_tepe,
            "boyutu_bilinmeyen": boyutu_bilinmeyen,
            "bos_disk_bayt": denetim.bos_bayt,
            "gerekli_bayt": denetim.gerekli_bayt,
            "eksik_bayt": denetim.eksik_bayt,
            "yeter": denetim.yeter,
            "eksik_onaylar": eksik_onaylar,
        }
        with self._kilit:
            self._planlar[kimlik] = plan
            while len(self._planlar) > SAKLANAN_PLAN_SAYISI:
                self._planlar.pop(next(iter(self._planlar)))
        return plan

    # ------------------------------------------------------------------
    # İş (POST /api/kur, /api/iptal)
    # ------------------------------------------------------------------
    def _baslatilabilir(self, plan_kimligi: str) -> Dict[str, Any]:
        eski = self._planlar.get(plan_kimligi)
        if eski is None:
            raise KurulumHatasi(KOD_PLAN_YOK, MSG_PLAN_YOK)
        taze = self.plan(secimler=eski["secimler"], istege_bagli=eski["istege_bagli"],
                         onar=eski["onar"])
        if taze["plan_kimligi"] != plan_kimligi:
            raise KurulumHatasi(KOD_PLAN_ESKIDI, MSG_PLAN_ESKIDI)
        if taze["eksik_onaylar"]:
            adlar = ", ".join(str(self.bilesen(k).get("ad") or k) for k in taze["eksik_onaylar"])
            raise KurulumHatasi(KOD_ONAY_EKSIK, MSG_ONAY_EKSIK.format(adlar=adlar))
        if not taze["yeter"]:
            raise KurulumHatasi(KOD_DISK, MSG_DISK.format(eksik=taze["eksik_bayt"]))
        return taze

    def baslat(self, plan_kimligi: str,
               olay: Callable[[str, Dict[str, Any]], None]) -> Dict[str, Any]:
        """Planı ayrı iş parçacığında başlatır (aynı anda tek iş); planı döndürür."""
        with self._kilit:
            if self.calisiyor:
                raise KurulumHatasi(KOD_IS_SURUYOR, MSG_IS_SURUYOR)
            plan = self._baslatilabilir(plan_kimligi)
            self._iptal = threading.Event()
            self._suren_plan = {"plan_kimligi": plan_kimligi,
                                "kalanlar": list(plan["bilesenler"])}
            self._is = threading.Thread(target=self.calistir, args=(plan, olay, self._iptal),
                                        name="kurulum", daemon=True)
            self._is.start()
            return plan

    def iptal(self) -> bool:
        """Süren işi durdurur (indirilen kısım saklanır). İş yoksa False."""
        with self._kilit:
            if not self.calisiyor:
                return False
            self._iptal.set()
            return True

    def bekle(self, zaman_asimi: Optional[float] = None) -> bool:
        """Süren iş bitene kadar bekler (başlıksız kip ve testler); bittiyse True."""
        is_ = self._is
        if is_ is None:
            return True
        is_.join(zaman_asimi)
        return not is_.is_alive()

    def calistir(self, plan: Optional[Mapping[str, Any]] = None,
                 olay: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 iptal: Optional[threading.Event] = None, *,
                 adimlar: Optional[Sequence[Mapping[str, Any]]] = None,
                 tex: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Planı bu iş parçacığında koşar (başlıksız kip ``baslat`` yerine bunu çağırır).

        İki biçim:

        - ``calistir(plan, olay, iptal)``: ``plan`` bu sınıfın ``plan()`` çıktısı.
          ``hata`` ve ``bitti`` olaylarını da bu yöntem yayar. Dönüş:
          ``{"sonuc", "kurulanlar", "kalanlar", "hata"?}``.
        - ``calistir(adimlar=[{"bilesen", "onar"?, "xelatex"?}, ...], tex=<TeX türü>)``
          (K4 sözleşmesi): plan K4'ündür. ``hata``/``bitti`` olaylarını K4 yayar;
          hata ya da iptalde ``KurulumHatasi`` (``kod``, ``ileti``, ``ayrinti``)
          yükselir, başarıda None döner.

        Onay ve disk denetimi ``baslat``'tadır (K4 biçiminde K4'ün planındadır);
        burada yalnız plan koşulur. Yine de lisans onayı eksik bileşen varsa iş
        başlamaz: zorunlu bileşenler onaysız inmez (Berke'nin kararı).
        """
        olay = self._ilk_dolu(olay, self._varsayilan_olay, _sessiz)
        iptal = self._ilk_dolu(iptal, self._dis_iptal, None) or threading.Event()
        if adimlar is not None:
            k4_plani = self._adimlardan_plan(adimlar, tex)
            ozet = self._calistir(k4_plani, olay, iptal, son_olaylar=False)
            if ozet["sonuc"] == SONUC_IPTAL:
                raise KurulumHatasi(KOD_IPTAL, MSG_IPTAL)
            if ozet["sonuc"] == SONUC_HATA:
                h = ozet.get("hata") or {}
                raise KurulumHatasi(str(h.get("kod") or KOD_BEKLENMEYEN),
                                    str(h.get("ileti") or ""), str(h.get("ayrinti") or ""),
                                    str(h.get("bilesen") or ""))
            return None
        if plan is None:
            raise TypeError("calistir: plan ya da adimlar gerekli")
        return self._calistir(plan, olay, iptal, son_olaylar=True)

    @staticmethod
    def _ilk_dolu(*adaylar: Any) -> Any:
        """None olmayan ilk değer (boş liste gibi "yanlış" ama geçerli dinleyiciler korunur)."""
        return next((a for a in adaylar if a is not None), None)

    def _adimlardan_plan(self, adimlar: Sequence[Mapping[str, Any]],
                         tex_turu: Optional[str]) -> Dict[str, Any]:
        """K4'ün plan satırlarından (``{"bilesen", "onar", "xelatex"?}``) iç plan."""
        bilesenler: List[str] = []
        onar: List[str] = []
        tercihler: Dict[str, Dict[str, Any]] = {}
        for satir in adimlar:
            kimlik = str(satir.get("bilesen"))
            self.bilesen(kimlik)
            if kimlik not in bilesenler:
                bilesenler.append(kimlik)
            if satir.get("onar") and kimlik not in onar:
                onar.append(kimlik)
            if satir.get("xelatex"):
                tercihler[kimlik] = {"xelatex": str(satir["xelatex"])}
        if tex_turu is not None:
            self.bilesen(str(tex_turu))
        govde = {"bilesenler": bilesenler, "onar": onar, "tex": tex_turu}
        return {"plan_kimligi": durum.kanonik_sha256(govde)[:PLAN_KIMLIGI_UZUNLUGU],
                "bilesenler": bilesenler, "onar": onar, "tercihler": tercihler,
                "secimler": {TEX_GRUBU: tex_turu} if tex_turu else {}}

    def _calistir(self, plan: Mapping[str, Any], olay: Callable[[str, Dict[str, Any]], None],
                  iptal: threading.Event, *, son_olaylar: bool) -> Dict[str, Any]:
        kalanlar = list(plan.get("bilesenler") or [])
        onar = set(plan.get("onar") or [])
        tercihler = plan.get("tercihler") if isinstance(plan.get("tercihler"), Mapping) else {}
        kurulanlar: List[str] = []
        hata: Optional[Dict[str, Any]] = None
        sonuc = SONUC_TAMAM
        guncellenen_grup = False
        self.defter.son_is_kaydet({"plan_kimligi": plan.get("plan_kimligi"),
                                   "bilesenler": list(kalanlar), "sonuc": SONUC_SURUYOR})
        try:
            eksik = lisans.eksik_onaylar(self.duzen, [self.bilesen(k) for k in kalanlar])
            if eksik:
                adlar = ", ".join(str(self.bilesen(k).get("ad") or k) for k in eksik)
                raise AdimHatasi(KOD_ONAY_EKSIK, MSG_ONAY_EKSIK.format(adlar=adlar))
            while kalanlar:
                kimlik = kalanlar[0]
                b = self.bilesen(kimlik)
                t0 = time.monotonic()
                once = disk.disk_kullanimi_bayt(self.duzen.kok) if self.disk_olc else None
                guncellenen_grup |= self._bileseni_kur(b, olay, iptal, yeniden=kimlik in onar,
                                                       tercihler=dict(tercihler.get(kimlik) or {}))
                self._bildir(olay, "bilesen_bitti", {"bilesen": kimlik})
                olcum = {"bilesen": kimlik, "sure_sn": gecen(t0), "zaman": durum.simdi()}
                if once is not None:
                    sonra = disk.disk_kullanimi_bayt(self.duzen.kok)
                    olcum.update(disk_once_bayt=once, disk_sonra_bayt=sonra,
                                 disk_fark_bayt=sonra - once)
                durum.satir_ekle(self.duzen.olcum_dosyasi, olcum)
                kurulanlar.append(kalanlar.pop(0))
                with self._kilit:
                    if self._suren_plan is not None:
                        self._suren_plan["kalanlar"] = list(kalanlar)
            if guncellenen_grup:
                self._fazla_paketleri_kaldir(olay, iptal)
            secilen_tex = (plan.get("secimler") or {}).get(TEX_GRUBU)
            if secilen_tex:
                self._tex_etkinlestir(str(secilen_tex))
            self._gecicileri_temizle(plan)
        except IptalEdildi:
            sonuc = SONUC_IPTAL
        except AdimHatasi as exc:
            if iptal.is_set():
                sonuc = SONUC_IPTAL
            else:
                sonuc = SONUC_HATA
                hata = {"bilesen": kalanlar[0] if kalanlar else "", "kod": exc.kod,
                        "ileti": exc.ileti, "ayrinti": exc.ayrinti,
                        "ayrinti_gunluk": self.gunluk_yolu}
        except Exception as exc:  # noqa: BLE001 — beklenmeyen hata da kullanıcıya iletilir
            logger.exception("kurulumda beklenmeyen hata")
            if iptal.is_set():
                sonuc = SONUC_IPTAL
            else:
                sonuc = SONUC_HATA
                hata = {"bilesen": kalanlar[0] if kalanlar else "", "kod": KOD_BEKLENMEYEN,
                        "ileti": f"Beklenmeyen hata: {type(exc).__name__}: {exc}",
                        "ayrinti": "", "ayrinti_gunluk": self.gunluk_yolu}
        finally:
            kayit: Dict[str, Any] = {"plan_kimligi": plan.get("plan_kimligi"),
                                     "bilesenler": list(plan.get("bilesenler") or []),
                                     "kurulanlar": kurulanlar, "sonuc": sonuc}
            if hata:
                kayit["hata"] = hata
            try:
                self.defter.son_is_kaydet(kayit)
            except OSError:
                logger.exception("son iş kaydı yazılamadı")
            with self._kilit:
                self._suren_plan = None
        ozet = {"sonuc": sonuc, "kurulanlar": kurulanlar, "kalanlar": kalanlar}
        if son_olaylar:
            if hata:
                self._bildir(olay, "hata", hata)
            self._bildir(olay, "bitti", dict(ozet))
        if hata:
            ozet["hata"] = hata
        return ozet

    def _tex_etkinlestir(self, kimlik: str) -> None:
        """Seçilen TeX türü kuruluysa ``durum/tex.json``'u ona çevirir (kurulum gerekmeden).

        "Türünü değiştir…" (E.7): kurulu Hafif TeX'e geri dönmek yeniden kurulum
        istemez. Kaydında TeX bilgisi olmayan (henüz kurulmamış) tür değişmez.
        """
        kayit = self.defter.bilesen(kimlik) or {}
        bilgi = kayit.get("tex")
        if not isinstance(bilgi, dict) or not bilgi.get("xelatex"):
            return
        if self._durum(self.bilesen(kimlik))[0] != durum.DURUM_KURULU:
            return
        mevcut = durum.tex_oku(self.duzen)
        if mevcut and mevcut.get("tur") == kimlik and mevcut.get("xelatex") == bilgi["xelatex"]:
            return
        durum.tex_yaz(self.duzen, kimlik, Path(str(bilgi["xelatex"])),
                      surum=str(bilgi.get("surum") or ""), sinama=bilgi.get("sinama") or {})

    # ------------------------------------------------------------------
    # Bileşen kurulumu
    # ------------------------------------------------------------------
    @staticmethod
    def _bildir(olay: Callable[[str, Dict[str, Any]], None], tur: str,
                veri: Dict[str, Any]) -> None:
        try:
            olay(tur, veri)
        except Exception:  # noqa: BLE001 — dinleyici hatası kurulumu durdurmaz
            logger.exception("olay dinleyicisi hata verdi (%s)", tur)

    def _oge_hazir(self, kimlik: str, oge: Mapping[str, Any]) -> bool:
        kayit = self.defter.adim(kimlik, _oge_kaydi_anahtari(oge))
        if not kayit or kayit.get("sha256") != oge.get("sha256"):
            return False
        try:
            st = self.duzen.kok_yolu(str(oge.get("hedef"))).stat()
        except (OSError, ValueError):
            return False
        return (st.st_size == kayit.get("bayt") and st.st_mtime_ns == kayit.get("mtime_ns")
                and st.st_ino == kayit.get("ino"))

    def _indir(self, kimlik: str, ogeler: Sequence[Mapping[str, Any]],
               olay: Callable[[str, Dict[str, Any]], None], iptal: threading.Event) -> None:
        kalan = [o for o in ogeler if not self._oge_hazir(kimlik, o)]
        if not kalan:
            return
        if iptal.is_set():
            raise IptalEdildi()
        istek = IndirmeIstegi(self.duzen.kok, kimlik, olay, iptal, self.macos_ana)
        try:
            self.indirici(kalan, istek)
        except (IptalEdildi, AdimHatasi):
            if iptal.is_set():
                raise IptalEdildi() from None
            raise
        except Exception as exc:  # noqa: BLE001 — indiricinin hatası Türkçe iletiye çevrilir
            if iptal.is_set():
                raise IptalEdildi() from exc
            if disk.disk_dolu_hatasi_mi(exc):
                raise AdimHatasi(KOD_DISK_DOLU, MSG_DISK_DOLU, str(exc)) from exc
            kod = str(getattr(exc, "kod", "") or KOD_INDIRME)
            ileti = str(getattr(exc, "ileti", "") or f"İndirme başarısız: {exc}")
            raise AdimHatasi(kod, ileti, str(exc)) from exc
        for oge in kalan:
            yol = self.duzen.kok_yolu(str(oge.get("hedef")))
            try:
                st = yol.stat()
            except OSError as exc:
                raise AdimHatasi(KOD_INDIRME, f"İndirilen dosya yerinde değil: "
                                              f"{oge.get('hedef')}") from exc
            if isinstance(oge.get("bayt"), int) and st.st_size != oge["bayt"]:
                raise AdimHatasi(KOD_INDIRME, f"İndirilen dosyanın boyutu uyuşmuyor: "
                                              f"{oge.get('hedef')}")
            self.defter.adim_kaydet(kimlik, _oge_kaydi_anahtari(oge),
                                    {"sha256": oge.get("sha256"), "bayt": st.st_size,
                                     "mtime_ns": st.st_mtime_ns, "ino": st.st_ino})

    def _tuketilenleri_sil(self, b: Baglam, kimlikler: Iterable[str]) -> None:
        for kimlik in kimlikler:
            try:
                oge = b.oge(kimlik)
            except AdimHatasi:
                continue
            if not b.indirilen_altinda_mi(oge):
                continue
            try:
                b.oge_yolu(oge).unlink(missing_ok=True)
            except OSError:
                logger.warning("geçici dosya silinemedi: %s", oge.get("hedef"))
            self.defter.adim_sil(b.kimlik, _oge_kaydi_anahtari(oge))

    def _uygulama_ortami(self) -> Dict[str, str]:
        """Doğrulama alt süreçlerinin ortamı (yollar KokDuzeni'nden; Streamlit ortamıyla aynı)."""
        return {"MODEL_CACHE_DIR": str(self.duzen.modeller_datalab),
                "FONT_PATH": str(self.duzen.font_yolu),
                "TECTONIC_CACHE_DIR": str(self.duzen.tex_cache),
                "HF_HUB_OFFLINE": "1"}

    def _bileseni_kur(self, bilesen: Mapping[str, Any],
                      olay: Callable[[str, Dict[str, Any]], None],
                      iptal: threading.Event, *, yeniden: bool,
                      tercihler: Optional[Dict[str, Any]] = None) -> bool:
        """Bir bileşeni kurar; bir paket grubu kilit değişikliğiyle yeniden kurulduysa True."""
        kimlik = str(bilesen.get("kimlik"))
        adimlar = [a for a in (bilesen.get("adimlar") or []) if isinstance(a, Mapping)]
        for adim in adimlar:
            if adim.get("tur") not in ADIMLAR:
                raise AdimHatasi(KOD_MANIFEST, f"Bilinmeyen adım türü: {adim.get('tur')}")
        if yeniden:
            veri = self.defter.oku()
            for anahtar in [a for a in veri["adimlar"] if a.startswith(f"{kimlik}/")]:
                self.defter.adim_sil(kimlik, anahtar.split("/", 1)[1])

        def _indir(ogeler: Sequence[Mapping[str, Any]]) -> None:
            self._indir(kimlik, ogeler, olay, iptal)

        b = Baglam(duzen=self.duzen, kayit_defteri=self.defter, uygulama=self.uygulama,
                   manifest=self.manifest, bilesen=bilesen, olay=olay, iptal=iptal,
                   indir=_indir, ogeler=self.uygun_ogeler(bilesen), yeniden=yeniden,
                   macos_ana=self.macos_ana, tercihler=dict(tercihler or {}))
        kalici = self.kalici_ogeler(bilesen)
        turler = [str(a.get("tur")) for a in adimlar]

        t0 = time.monotonic()
        kalici_bayt = sum(int(o.get("bayt") or 0) for o in kalici)
        b.adim_kimligi = "indir"
        b.bildir("adim_basladi", ad=ADIM_ADLARI["indir"], tur="indir", sira=0,
                 baslangic=time.time(), oge_sayisi=len(kalici), toplam_bayt=kalici_bayt)
        _indir(kalici)
        b.bildir("adim_bitti", sure_sn=gecen(t0), atlandi=False)

        sonuclar: List[AdimSonucu] = []
        guncellenen_grup = False
        for sira, adim in enumerate(adimlar, start=1):
            tur = str(adim.get("tur"))
            anahtar = adim_anahtari(sira, adim)
            onceki = self.defter.adim(kimlik, anahtar)
            t0 = time.monotonic()
            # Aynı türden iki adım olan bileşende kimlikler karışmasın
            b.adim_kimligi = tur if turler.count(tur) == 1 else f"{tur}-{sira}"
            ek: Dict[str, Any] = {}
            if tur == "tectonic_isit" and isinstance(bilesen.get("sayac_beklenen"), Mapping):
                ek["sayac_beklenen"] = dict(bilesen["sayac_beklenen"])
            b.bildir("adim_basladi", ad=ADIM_ADLARI.get(tur, tur), tur=tur, sira=sira,
                     baslangic=time.time(), **ek)
            b.iptal_denetle()
            sonuc = ADIMLAR[tur](b, adim, onceki)
            if not sonuc.atlandi:
                if tur == TEKERLEK_ADIMI and self._grup_degisti(kimlik, adim, sonuc):
                    guncellenen_grup = True
                self.defter.adim_kaydet(kimlik, anahtar, sonuc.kayda())
            self._tuketilenleri_sil(b, sonuc.tuketilen)
            sure = gecen(t0)
            durum.satir_ekle(self.duzen.olcum_dosyasi,
                             {"bilesen": kimlik, "adim": tur, "sira": sira, "sure_sn": sure,
                              "atlandi": sonuc.atlandi, "zaman": durum.simdi()})
            b.bildir("adim_bitti", sure_sn=sure, atlandi=sonuc.atlandi)
            sonuclar.append(sonuc)

        t0 = time.monotonic()
        b.adim_kimligi = "dogrulama"
        b.bildir("adim_basladi", ad=ADIM_ADLARI["dogrulama"], tur="dogrulama",
                 sira=len(adimlar) + 1, baslangic=time.time())
        self._dogrula(b, kalici, sonuclar)
        b.bildir("adim_bitti", sure_sn=gecen(t0), atlandi=False)
        b.adim_kimligi = None

        self._bilesen_kaydi_yaz(bilesen, kalici, sonuclar)
        return guncellenen_grup

    def _grup_degisti(self, kimlik: str, adim: Mapping[str, Any], sonuc: AdimSonucu) -> bool:
        """Bu grup önceden başka bir kilitle kurulu muydu (güncelleme, G.4-3)?"""
        kayit = self.defter.bilesen(kimlik) or {}
        gruplar = kayit.get("gruplar") if isinstance(kayit.get("gruplar"), dict) else {}
        grup = str(adim.get("grup"))
        eski = gruplar.get(grup) if isinstance(gruplar.get(grup), dict) else None
        yeni = sonuc.gruplar.get(grup) or {}
        return bool(eski) and eski.get("kilit_sha256") != yeni.get("kilit_sha256")

    def _dogrula(self, b: Baglam, kalici: Sequence[Mapping[str, Any]],
                 sonuclar: Sequence[AdimSonucu]) -> None:
        dogrulama = b.bilesen.get("dogrulama")
        liste = dogrulama if isinstance(dogrulama, list) else [dogrulama] if dogrulama else []
        for d in liste:
            if not isinstance(d, Mapping):
                raise AdimHatasi(KOD_MANIFEST, "Geçersiz doğrulama kaydı.")
            tur = d.get("tur")
            b.iptal_denetle()
            if tur == "python_ice_aktar":
                paketler.python_ice_aktar(b, list(d.get("moduller") or []),
                                          ortam=self._uygulama_ortami())
            elif tur == "dosyalar_sha":
                for oge in kalici:
                    b.iptal_denetle()
                    yol = b.oge_yolu(oge)
                    try:
                        gercek = durum.sha256_dosya(yol)
                    except OSError as exc:
                        raise AdimHatasi(KOD_DOGRULAMA,
                                         f"Dosya okunamadı: {oge.get('hedef')}") from exc
                    if gercek != oge.get("sha256"):
                        self.defter.adim_sil(b.kimlik, _oge_kaydi_anahtari(oge))
                        raise AdimHatasi(KOD_DOGRULAMA, f"Dosya bozuk (SHA-256 uyuşmuyor): "
                                                        f"{oge.get('hedef')}")
            elif tur == "tex_derle":
                tex_sonucu = next((s.tex for s in reversed(sonuclar) if s.tex), None)
                sinama = (tex_sonucu or {}).get("sinama") or {}
                if tex_sonucu and sinama.get("gecti") and not all(s.atlandi for s in sonuclar):
                    continue
                xelatex = (tex_sonucu or {}).get("xelatex")
                if not xelatex:
                    raise AdimHatasi(KOD_DOGRULAMA, "TeX derleyicisi belirlenemedi.")
                belge = b.uygulama_yolu(tex.ISITMA_BELGESI)
                yeni = tex.tex_sina(self.duzen, str(xelatex), belge, b.iptal)
                if not yeni["gecti"]:
                    raise AdimHatasi(KOD_DOGRULAMA, tex.MSG_TEX_SINAMA.format(hata=yeni["hata"]))
            elif tur == "komut":
                yer = {"kok": str(self.duzen.kok), "venv_python": str(self.duzen.venv_python),
                       "uygulama": str(self.uygulama)}
                argv = [str(a).format(**yer) for a in d.get("argv") or []]
                if not argv:
                    raise AdimHatasi(KOD_MANIFEST, "komut doğrulamasında argv yok.")
                ortam = dict(self._uygulama_ortami())
                ortam.update({str(k): str(v).format(**yer) for k, v in (d.get("ortam") or {}).items()})
                sonuc = b.komut(argv, ortam=ortam, cwd=self.duzen.kok)
                if sonuc.rc != 0:
                    raise AdimHatasi(KOD_DOGRULAMA, "Doğrulama komutu başarısız.", sonuc.ayrinti)
            else:
                raise AdimHatasi(KOD_MANIFEST, f"Bilinmeyen doğrulama türü: {tur}")

    def _bilesen_kaydi_yaz(self, bilesen: Mapping[str, Any], kalici: Sequence[Mapping[str, Any]],
                           sonuclar: Sequence[AdimSonucu]) -> None:
        kimlik = str(bilesen.get("kimlik"))
        dosyalar: Dict[str, int] = {}
        for goreli in [str(o.get("hedef")) for o in kalici] + \
                [d for s in sonuclar for d in s.dosyalar]:
            try:
                dosyalar[goreli] = self.duzen.kok_yolu(goreli).stat().st_size
            except (OSError, ValueError) as exc:
                raise AdimHatasi(KOD_DOGRULAMA, f"Kurulan dosya yerinde değil: {goreli}") from exc
        gruplar: Dict[str, Any] = {}
        harici: List[str] = []
        yollar: List[str] = []
        tex_sonucu: Optional[Dict[str, Any]] = None
        for s in sonuclar:
            gruplar.update(s.gruplar)
            harici += [h for h in s.harici if h not in harici]
            yollar += [y for y in s.yollar if y not in yollar]
            tex_sonucu = s.tex or tex_sonucu
        self.defter.bilesen_kaydet(kimlik, {
            "imza": durum.bilesen_imzasi(bilesen), "surum": bilesen.get("surum", ""),
            "zaman": durum.simdi(), "dosyalar": dosyalar, "gruplar": gruplar,
            "harici": harici, "yollar": yollar, "tex": tex_sonucu,
            "uygulama_surumu": self.uygulama_surumu})
        if bilesen.get("grup") == TEX_GRUBU and tex_sonucu:
            durum.tex_yaz(self.duzen, kimlik, Path(str(tex_sonucu["xelatex"])),
                          surum=str(tex_sonucu.get("surum") or ""),
                          sinama=tex_sonucu.get("sinama") or {})

    def _kurulu_paket_adlari(self) -> Set[str]:
        adlar: Set[str] = set()
        for b in self.bilesenler:
            kayit = self.defter.bilesen(str(b.get("kimlik"))) or {}
            for bilgi in (kayit.get("gruplar") or {}).values():
                for cift in (bilgi or {}).get("paketler") or []:
                    adlar.add(durum.normal_ad(str(cift[0])))
        return adlar

    def _fazla_paketleri_kaldir(self, olay: Callable[[str, Dict[str, Any]], None],
                                iptal: threading.Event) -> None:
        """Güncellemede kilitten çıkan paketleri kaldırır (G.4-3, ``uv pip sync`` karşılığı)."""
        fazla = durum.fazla_paketler(self.duzen.site_packages, self._kurulu_paket_adlari())
        if not fazla:
            return
        logger.info("kilitte olmayan paketler kaldırılıyor: %s", ", ".join(fazla))
        b = Baglam(duzen=self.duzen, kayit_defteri=self.defter, uygulama=self.uygulama,
                   manifest=self.manifest, bilesen={"kimlik": GUNCELLEME_ETIKETI}, olay=olay,
                   iptal=iptal, indir=lambda ogeler: None)
        paketler.paketleri_kaldir(b, fazla)

    def _gecicileri_temizle(self, plan: Mapping[str, Any]) -> None:
        """Başarılı işten sonra bu planın geçici dosyalarını siler.

        Başka bileşenlerin yarım ``.part`` dosyaları (ör. önceki bir işte
        iptal edilen HY-MT) sürdürülebilsin diye korunur.
        """
        for grup in {str(a.get("grup")) for k in plan.get("bilesenler") or []
                     for a in self.bilesen(k).get("adimlar") or []
                     if isinstance(a, Mapping) and a.get("tur") == TEKERLEK_ADIMI}:
            for ad in (paketler.ETKIN_KILIT_SABLONU.format(grup=grup),
                       paketler.ETKIN_KILIT_SABLONU.format(grup=f"{grup}-derleme")):
                (self.duzen.indirilen / ad).unlink(missing_ok=True)
        for klasor in (self.duzen.tekerlek_deposu, self.duzen.indirilen):
            try:
                klasor.rmdir()      # yalnız boşsa
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Kaldırma (POST /api/kaldir)
    # ------------------------------------------------------------------
    def _kaldirma_engeli(self, bilesen: Mapping[str, Any],
                         etkin_tex: Optional[str] = None) -> Optional[str]:
        if bilesen.get("kaldirilabilir") is False:
            return "manifestte kaldırılamaz işaretli"
        zorunluluk = bilesen.get("zorunluluk")
        if zorunluluk == ZORUNLU:
            return "zorunlu bileşen"
        if zorunluluk == SECIM:
            if bilesen.get("kimlik") != TEX_HAFIF:
                return "sistem geneline kurulu ya da bizim kurmadığımız TeX"
            if (etkin_tex if etkin_tex is not None else self._etkin_tex()) == TEX_HAFIF:
                return MSG_TEX_ETKIN
        return None

    def kaldir(self, kimlik: str, olay: Optional[Callable[[str, Dict[str, Any]], None]] = None
               ) -> Dict[str, Any]:
        """İsteğe bağlı bileşeni ya da etkin olmayan Hafif TeX'i kaldırır (lisans K8).

        Dönüş: ``{"bilesen", "silinen_bayt"}`` (``silinen_bayt`` gerçek ``du`` ölçümü).
        """
        olay = olay if olay is not None else _sessiz
        with self._kilit:
            if self.calisiyor:
                raise KurulumHatasi(KOD_IS_SURUYOR, MSG_IS_SURUYOR)
            bilesen = self.bilesen(kimlik)
            engel = self._kaldirma_engeli(bilesen)
            if engel:
                raise KurulumHatasi(KOD_KALDIRILAMAZ, MSG_KALDIRILAMAZ.format(neden=engel))
            kayit = self.defter.bilesen(kimlik) or {}
            silinecek: List[Path] = []
            for adim in bilesen.get("adimlar") or []:
                if not isinstance(adim, Mapping):
                    continue
                if adim.get("tur") == "lisans_yaz" and adim.get("dizin"):
                    silinecek.append(self.duzen.kok_yolu(str(adim["dizin"])))
                if adim.get("tur") == "tectonic_isit":
                    silinecek.append(self.duzen.tex)
            for goreli in kayit.get("yollar") or []:
                yol = self.duzen.kok_yolu(str(goreli))
                if yol not in silinecek:
                    silinecek.append(yol)
            korunan = {self.duzen.kok_yolu(k) for k in KORUNAN_KLASORLER}
            for yol in [y for y in silinecek if y in korunan or y == self.duzen.kok]:
                logger.warning("korunan klasör kaldırılmadı: %s", yol)
                silinecek.remove(yol)
            dosyalar = [self.duzen.kok_yolu(g) for g in (kayit.get("dosyalar") or {})]
            once = disk.disk_kullanimi_bayt(self.duzen.kok)

            # Paketler: yalnız bu bileşene özgü olanlar (öteki kurulu grupların
            # kilitlerinde geçen paketler kalır).
            bu_paketler = {durum.normal_ad(str(c[0])): str(c[0])
                           for bilgi in (kayit.get("gruplar") or {}).values()
                           for c in (bilgi or {}).get("paketler") or []}
            if bu_paketler and self.duzen.venv_python.exists():
                oteki: Set[str] = set()
                for b in self.bilesenler:
                    if b.get("kimlik") == kimlik:
                        continue
                    ok = self.defter.bilesen(str(b.get("kimlik"))) or {}
                    for bilgi in (ok.get("gruplar") or {}).values():
                        oteki |= {durum.normal_ad(str(c[0]))
                                  for c in (bilgi or {}).get("paketler") or []}
                kurulu = durum.kurulu_dagitimlar(self.duzen.site_packages)
                adlar = sorted(ad for n, ad in bu_paketler.items() if n not in oteki and n in kurulu)
                b = Baglam(duzen=self.duzen, kayit_defteri=self.defter, uygulama=self.uygulama,
                           manifest=self.manifest, bilesen=bilesen, olay=olay,
                           iptal=threading.Event(), indir=lambda ogeler: None)
                paketler.paketleri_kaldir(b, adlar)
            for klasor in silinecek:
                if klasor.is_dir():
                    shutil.rmtree(klasor)
            for yol in dosyalar:
                yol.unlink(missing_ok=True)
                self._bos_atalari_sil(yol.parent)
            if bilesen.get("grup") == TEX_GRUBU and self._etkin_tex() == kimlik:
                durum.tex_sil(self.duzen)
            self.defter.bilesen_sil(kimlik)
            sonra = disk.disk_kullanimi_bayt(self.duzen.kok)
            return {"bilesen": kimlik, "silinen_bayt": max(0, once - sonra)}

    def _bos_atalari_sil(self, klasor: Path) -> None:
        """Kökün altındaki boş klasörleri yukarı doğru siler (kökün kendisi kalır)."""
        kok = self.duzen.kok
        while klasor != kok and kok in klasor.parents:
            try:
                klasor.rmdir()
            except OSError:
                return
            klasor = klasor.parent
