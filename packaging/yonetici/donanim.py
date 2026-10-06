"""Donanım kapısı sihirbaz için: MLX kurulmadan ölçüm, hüküm ``core.hardware``'dan.

Yalnız stdlib (``ctypes``). Dayanak: TASARIM A.3, E.3; KARARLAR 17/17a.

Sözleşme (formülün tek kaynağı)
-------------------------------
Bu modül bellek **hesabı yapmaz** ve ``core.hardware``'daki hiçbir kuralı
kopyalamaz. Gerekli bellek (``assess``), genel kapı (``general_gate``),
"sığmaz" iletisi (``too_big_message``), KV şekli (``kv_shape_from_hf_config``)
ve sayı biçimi (``format_gb``, ``format_fit_gb``) uygulamanın kendi
``core/hardware.py`` modülünden içe aktarılarak çağrılır. Böylece
``core/hardware.py`` değiştiğinde sihirbazın hükmü de kendiliğinden değişir.
Bu modülün yaptığı yalnız üç şeydir:

1. **Ölçüm.** MLX sihirbaz anında kurulu değildir; ``core.hardware`` o
   durumda ``sysctl hw.memsize × 2/3`` yedeğine düşer (bu Mac'te 24,0 GiB,
   gerçekte 28,08 GiB). Bu modül Metal'in ``recommendedMaxWorkingSetSize``
   değerini stdlib ``ctypes`` ile okur (MLX'in ``max_recommended_working_set_size``
   değeriyle aynı sayı; ölçüm: 30.150.672.384 = 30.150.672.384) ve
   ``core.hardware.HardwareInfo(source=SOURCE_METAL)`` olarak kurar.
   ``core.hardware`` kendisi Metal ölçümü verirse (MLX kuruluysa ya da F3
   geldiyse) onun ölçümü kullanılır; bu modülün ölçümü yalnız yedektir ve
   F3'ten sonra silinebilir.
2. **İçe aktarma.** ``core/__init__.py`` stdlib Python'da ``rapidfuzz``
   yüzünden patlar. ``core`` için boş bir paket kabuğu kurulur
   (``sys.modules['core']``, ``__path__ = [<uygulama>/core]``); böylece
   ``core.hardware`` ve bağımlılıkları (``config.defaults``,
   ``core.mt_engines.backends``) stdlib ile yüklenir. Mevcut dosyaya
   dokunulmaz. ``core`` zaten gerçek paket olarak yüklüyse o kullanılır.
3. **Girdilerin uygulamayla aynı tanımı.** Uygulama (``discovery``) bir MLX
   modelinin ağırlığını model klasöründeki ``MLX_WEIGHTS_GLOB``
   (``*.safetensors``) dosyalarının toplamı olarak ölçer ve KV şeklini
   ``config.json``'dan ``kv_shape_from_hf_config`` ile okur; kip
   ``MODE_BATCHED``'dır. Sihirbaz model inmeden önce aynı hükmü verebilsin
   diye:

   - ağırlık = manifestteki bileşen öğelerinden, adı ``MLX_WEIGHTS_GLOB``'a
     uyan dosyaların ``bayt`` toplamı (desen ``discovery``'den içe aktarılır);
     öğelerden hesaplanamazsa ``donanim_kosulu.agirlik_bayt``;
   - KV = ``donanim_kosulu.config`` (modelin ``config.json``'undaki alanlar,
     birebir) varsa ``kv_shape_from_hf_config(config)``; yoksa şemadaki
     ``kv {katman, kv_basligi, head_dim, oge_bayt}`` alanlarından.

``core.hardware`` içe aktarılamazsa (uygulama kodu yok ya da bozuk) hüküm
"ölçülemedi" olur: ``core.hardware`` ile aynı ilke, ölçemediğimiz makineyi
kapatmak uydurma hüküm olurdu; kapı açık kalır ve nedeni ``uyari``'da yazar.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import fnmatch
import importlib
import logging
import sys
import threading
import types
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger("kitapcevir.yonetici.donanim")

# ---------------------------------------------------------------------------
# Sabitler (yalnız bu modülde)
# ---------------------------------------------------------------------------
_METAL_CERCEVESI = "/System/Library/Frameworks/Metal.framework/Metal"
_OBJC_KITAPLIGI = "objc"
_SECICI_CALISMA_KUMESI = b"recommendedMaxWorkingSetSize"
_SECICI_AD = b"name"
_SECICI_UTF8 = b"UTF8String"

KOSUL_GENEL_KAPI = "yerel_model_genel_kapi"
KOSUL_MODEL_SIGAR = "model_sigar"

# E.3'teki olumlu satır. Sayılar core.hardware biçimleyicilerinden gelir;
# "sığar" hükmünün kendisi de core.hardware.assess'tendir.
MSG_SIGAR = "Bu Mac'e sığar: gerekli ~{need} GB, kullanılabilir {have} GB."
MSG_CORE_YOK = ("Donanım denetimi yapılamadı ({neden}); bellek denetimi yapılmadan "
                "devam ediliyor.")
MSG_KOSUL_BILINMIYOR = "Bilinmeyen donanım koşulu: {tur}"
MSG_AGIRLIK_YOK = "Model ağırlığı manifestte yok; bellek denetimi yapılamadı."


# ---------------------------------------------------------------------------
# 1. Metal ölçümü (stdlib ctypes)
# ---------------------------------------------------------------------------
def metal_olc() -> Dict[str, Any]:
    """Metal varsayılan aygıtının ``recommendedMaxWorkingSetSize`` değeri ve adı.

    Dönüş: ``{"kullanilabilir_bayt": int | None, "cihaz": str, "neden": str}``.
    Hata olursa ``kullanilabilir_bayt`` None'dır ve ``neden`` Türkçe açıklar.
    """
    try:
        objc_yolu = ctypes.util.find_library(_OBJC_KITAPLIGI)
        if not objc_yolu:
            return {"kullanilabilir_bayt": None, "cihaz": "", "neden": "objc kitaplığı bulunamadı"}
        objc = ctypes.cdll.LoadLibrary(objc_yolu)
        metal = ctypes.cdll.LoadLibrary(_METAL_CERCEVESI)
    except OSError as exc:
        return {"kullanilabilir_bayt": None, "cihaz": "",
                "neden": f"Metal çerçevesi yüklenemedi: {type(exc).__name__}"}

    metal.MTLCreateSystemDefaultDevice.restype = ctypes.c_void_p
    metal.MTLCreateSystemDefaultDevice.argtypes = []
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    aygit = metal.MTLCreateSystemDefaultDevice()
    if not aygit:
        return {"kullanilabilir_bayt": None, "cihaz": "", "neden": "Metal aygıtı yok"}

    gonder = ("objc_msgSend", objc)
    gonder_u64 = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p)(gonder)
    gonder_id = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(gonder)
    gonder_metin = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)(gonder)

    bayt = int(gonder_u64(aygit, objc.sel_registerName(_SECICI_CALISMA_KUMESI)))
    cihaz = ""
    ad = gonder_id(aygit, objc.sel_registerName(_SECICI_AD))
    if ad:
        ham = gonder_metin(ad, objc.sel_registerName(_SECICI_UTF8))
        if ham:
            cihaz = ham.decode("utf-8", "replace").strip()
    if bayt <= 0:
        return {"kullanilabilir_bayt": None, "cihaz": cihaz,
                "neden": "Metal kullanılabilir bellek değeri vermedi"}
    return {"kullanilabilir_bayt": bayt, "cihaz": cihaz, "neden": ""}


_METAL_ONBELLEK: Optional[Dict[str, Any]] = None
_METAL_KILIDI = threading.Lock()


def metal_olcumu(yenile: bool = False) -> Dict[str, Any]:
    """``metal_olc`` sonucu, süreç başına bir kez (``/api/durum`` her açılışta sorar)."""
    global _METAL_ONBELLEK
    with _METAL_KILIDI:
        if _METAL_ONBELLEK is None or yenile:
            _METAL_ONBELLEK = metal_olc()
        return dict(_METAL_ONBELLEK)


# ---------------------------------------------------------------------------
# 2. core.hardware'ı stdlib Python'da içe aktarma (paket kabuğu)
# ---------------------------------------------------------------------------
_ICE_AKTARMA_KILIDI = threading.RLock()


class CoreYuklenemedi(RuntimeError):
    """Uygulamanın ``core.hardware`` modülü içe aktarılamadı."""


def core_hardware(uygulama: Path):
    """``<uygulama>/core/hardware.py``'yi ``core/__init__.py``'yi atlayarak yükler.

    ``uygulama`` = uygulama kodunun kökü (paketli kipte ``<kök>/app/guncel``).
    ``core`` zaten gerçek paket olarak yüklüyse (geliştirici venv'i) o kullanılır.
    """
    uygulama = Path(uygulama)
    with _ICE_AKTARMA_KILIDI:
        mevcut = sys.modules.get("core")
        if mevcut is None:
            core_dizini = uygulama / "core"
            if not (core_dizini / "hardware.py").is_file():
                raise CoreYuklenemedi(f"{core_dizini / 'hardware.py'} yok")
            if str(uygulama) not in sys.path:
                sys.path.insert(0, str(uygulama))
            kabuk = types.ModuleType("core")
            kabuk.__path__ = [str(core_dizini)]   # type: ignore[attr-defined]
            kabuk.__package__ = "core"
            sys.modules["core"] = kabuk
        try:
            return importlib.import_module("core.hardware")
        except Exception as exc:  # ImportError, SyntaxError ...
            raise CoreYuklenemedi(f"core.hardware içe aktarılamadı: {type(exc).__name__}: {exc}") \
                from exc


def _branding_ipucu(uygulama: Path) -> str:
    """``ui.branding.MODEL_HW_TEX_HINT`` (yoksa boş; metin burada yazılmaz)."""
    try:
        core_hardware(uygulama)   # ui.branding core.lang_detect'i ister: kabuk kurulmuş olsun
        branding = importlib.import_module("ui.branding")
    except Exception:  # noqa: BLE001 — ipucu yalnız ek satır; yoksa gösterilmez
        return ""
    return str(getattr(branding, "MODEL_HW_TEX_HINT", ""))


def agirlik_deseni(uygulama: Path) -> Optional[str]:
    """Uygulamanın MLX ağırlık deseni (``discovery.MLX_WEIGHTS_GLOB``); yoksa None."""
    try:
        with _ICE_AKTARMA_KILIDI:
            core_hardware(uygulama)
            kesif = importlib.import_module("core.mt_engines.discovery")
    except Exception as exc:  # noqa: BLE001 — desen yoksa manifestin agirlik_bayt'ı kullanılır
        logger.warning("discovery içe aktarılamadı, ağırlık manifestten: %s", exc)
        return None
    desen = getattr(kesif, "MLX_WEIGHTS_GLOB", None)
    return desen if isinstance(desen, str) and desen else None


# ---------------------------------------------------------------------------
# 3. HardwareInfo ve hükümler
# ---------------------------------------------------------------------------
def donanim_bilgisi(uygulama: Path, *, olcum: Optional[Mapping[str, Any]] = None):
    """``core.hardware.HardwareInfo``: önce core'un kendi ölçümü, Metal değilse ctypes.

    ``olcum`` testte ``metal_olcumu()`` yerine verilir.
    """
    hw_mod = core_hardware(uygulama)
    core_olcumu = hw_mod.hardware_info()
    if core_olcumu.measured and core_olcumu.source == hw_mod.SOURCE_METAL:
        return core_olcumu
    metal = dict(olcum) if olcum is not None else metal_olcumu()
    bayt = metal.get("kullanilabilir_bayt")
    if isinstance(bayt, int) and not isinstance(bayt, bool) and bayt > 0:
        return hw_mod.HardwareInfo(available_bytes=bayt,
                                   memory_bytes=core_olcumu.memory_bytes,
                                   chip=core_olcumu.chip or str(metal.get("cihaz") or ""),
                                   source=hw_mod.SOURCE_METAL)
    logger.warning("ctypes Metal ölçümü alınamadı: %s", metal.get("neden"))
    return core_olcumu


def _hw_sozluk(hw_mod, hw) -> Dict[str, Any]:
    return {
        "kaynak": hw.source,
        "cihaz": hw.chip,
        "kullanilabilir_bayt": hw.available_bytes if hw.measured else None,
        "toplam_bayt": hw.memory_bytes,
        "sorun": hw.problem,
        "baslik": hw_mod.hardware_caption(hw),
    }


def genel_kapi(uygulama: Path, hw=None) -> Dict[str, Any]:
    """``core.hardware.general_gate`` hükmü sözlük olarak (B.5 ``donanim.genel_kapi``).

    ``{"izin", "neden", "uyari", "ipucu"}``; ``neden`` core'un ``MSG_HW_TOO_SMALL``
    metnidir, ``uyari`` core'un ölçüm uyarısıdır (yedek ölçüm / ölçülemedi),
    ``ipucu`` ``ui.branding.MODEL_HW_TEX_HINT``'tir (yalnız kapalıyken).
    """
    try:
        hw_mod = core_hardware(uygulama)
        hw = hw if hw is not None else donanim_bilgisi(uygulama)
    except CoreYuklenemedi as exc:
        return {"izin": True, "neden": "", "uyari": MSG_CORE_YOK.format(neden=exc), "ipucu": ""}
    hukum = hw_mod.general_gate(hw)
    return {"izin": bool(hukum.allowed), "neden": hukum.reason, "uyari": hukum.warning,
            "ipucu": "" if hukum.allowed else _branding_ipucu(uygulama)}


def durum_ozeti(uygulama: Path, hw=None) -> Dict[str, Any]:
    """``GET /api/durum`` ``donanim`` alanı: ``{kaynak, cihaz, kullanilabilir_bayt,
    toplam_bayt, sorun, baslik, genel_kapi}``."""
    try:
        hw_mod = core_hardware(uygulama)
        hw = hw if hw is not None else donanim_bilgisi(uygulama)
    except CoreYuklenemedi as exc:
        return {"kaynak": "", "cihaz": "", "kullanilabilir_bayt": None, "toplam_bayt": None,
                "sorun": str(exc), "baslik": "",
                "genel_kapi": {"izin": True, "neden": "",
                               "uyari": MSG_CORE_YOK.format(neden=exc), "ipucu": ""}}
    ozet = _hw_sozluk(hw_mod, hw)
    ozet["genel_kapi"] = genel_kapi(uygulama, hw)
    return ozet


def _kosullar(donanim_kosulu: Any) -> List[Mapping[str, Any]]:
    if donanim_kosulu is None:
        return []
    if isinstance(donanim_kosulu, Mapping):
        return [donanim_kosulu]
    if isinstance(donanim_kosulu, Sequence) and not isinstance(donanim_kosulu, (str, bytes)):
        return [k for k in donanim_kosulu if isinstance(k, Mapping)]
    return []


def model_agirligi(uygulama: Path, kosul: Mapping[str, Any],
                   bilesen: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """Uygulamanın ölçeceği ağırlık: öğelerden ``MLX_WEIGHTS_GLOB`` toplamı.

    ``discovery`` model klasöründe (alt klasöre inmeden) desene uyan dosyaları
    toplar; burada da yalnız ilk uyan öğeyle aynı klasördekiler sayılır.
    Öğelerden hesaplanamazsa ``kosul["agirlik_bayt"]``; o da yoksa None.
    """
    beyan = kosul.get("agirlik_bayt")
    beyan = beyan if isinstance(beyan, int) and not isinstance(beyan, bool) and beyan > 0 \
        else None
    desen = agirlik_deseni(uygulama) if bilesen is not None else None
    if desen and bilesen is not None:
        klasor = None
        toplam = 0
        for oge in bilesen.get("ogeler") or []:
            hedef = PurePosixPath(str(oge.get("hedef", "")))
            if not fnmatch.fnmatchcase(hedef.name, desen):
                continue
            klasor = hedef.parent if klasor is None else klasor
            if hedef.parent == klasor and isinstance(oge.get("bayt"), int):
                toplam += int(oge["bayt"])
        if toplam > 0:
            if beyan is not None and beyan != toplam:
                logger.warning("donanim_kosulu.agirlik_bayt (%d) öğelerden ölçülenle (%d) "
                               "uyuşmuyor; uygulamanın tanımı (öğeler) kullanılıyor",
                               beyan, toplam)
            return toplam
    return beyan


def kv_sekli(hw_mod, kosul: Mapping[str, Any]):
    """KV şekli: ``config`` varsa ``kv_shape_from_hf_config``; yoksa ``kv`` alanları.

    İkisi de yoksa None: ``assess`` o zaman core'un oran yedeğini kullanır.
    """
    config = kosul.get("config")
    if isinstance(config, Mapping):
        sekil = hw_mod.kv_shape_from_hf_config(config)
        if sekil is not None:
            return sekil
    kv = kosul.get("kv")
    if isinstance(kv, Mapping):
        try:
            return hw_mod.KvShape(int(kv["katman"]), int(kv["kv_basligi"]),
                                  int(kv["head_dim"]), int(kv["head_dim"]),
                                  int(kv["oge_bayt"]), hw_mod.KV_SOURCE_CONFIG)
        except (KeyError, TypeError, ValueError):
            logger.warning("donanim_kosulu.kv okunamadı: %r", kv)
    return None


def model_uyumu(uygulama: Path, kosul: Mapping[str, Any], *,
                bilesen: Optional[Mapping[str, Any]] = None, hw=None):
    """``core.hardware.assess`` sonucu (``MemoryFit``); ağırlık bilinmiyorsa None."""
    hw_mod = core_hardware(uygulama)
    agirlik = model_agirligi(uygulama, kosul, bilesen)
    if agirlik is None:
        return None
    hw = hw if hw is not None else donanim_bilgisi(uygulama)
    return hw_mod.assess(agirlik, kv_sekli(hw_mod, kosul), mode=hw_mod.MODE_BATCHED, hw=hw)


def bilesen_kapisi(uygulama: Path, donanim_kosulu: Any, hw=None, *,
                   bilesen: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Bileşenin donanım hükmü: ``{"secilebilir", "neden", "uyari", "ipucu", "bilgi"}``.

    ``donanim_kosulu`` manifestteki alan: None, tek koşul ya da koşul listesi
    (HY-MT: ``model_sigar`` + ``yerel_model_genel_kapi``). Genel kapı önce
    değerlendirilir; ilk kapalı koşul hükmü verir. İletiler core.hardware'dan:

    - genel kapı kapalı: ``MSG_HW_TOO_SMALL`` (+ ``MODEL_HW_TEX_HINT`` ipucu);
    - model sığmıyor: ``too_big_message``;
    - model sığıyor: ``bilgi`` = "Bu Mac'e sığar: gerekli ~X GB, kullanılabilir Y GB."

    ``bilesen`` verilirse model ağırlığı öğelerden hesaplanır (``model_agirligi``).
    """
    kosullar = _kosullar(donanim_kosulu)
    sonuc: Dict[str, Any] = {"secilebilir": True, "neden": "", "uyari": "", "ipucu": "",
                             "bilgi": ""}
    if not kosullar:
        return sonuc
    try:
        hw_mod = core_hardware(uygulama)
        hw = hw if hw is not None else donanim_bilgisi(uygulama)
    except CoreYuklenemedi as exc:
        sonuc["uyari"] = MSG_CORE_YOK.format(neden=exc)
        return sonuc

    kapi = genel_kapi(uygulama, hw)
    sonuc["uyari"] = kapi["uyari"]
    # Genel kapı önce: 8 GB Mac'te "yetersiz" iletisi model iletisinden anlamlıdır.
    sirali = sorted(kosullar, key=lambda k: 0 if k.get("tur") == KOSUL_GENEL_KAPI else 1)
    for kosul in sirali:
        tur = kosul.get("tur")
        if tur == KOSUL_GENEL_KAPI:
            if not kapi["izin"]:
                sonuc.update(secilebilir=False, neden=kapi["neden"], ipucu=kapi["ipucu"])
                return sonuc
        elif tur == KOSUL_MODEL_SIGAR:
            uyum = model_uyumu(uygulama, kosul, bilesen=bilesen, hw=hw)
            if uyum is None:
                sonuc["uyari"] = sonuc["uyari"] or MSG_AGIRLIK_YOK
                continue
            if uyum.fits is False:
                sonuc.update(secilebilir=False, neden=hw_mod.too_big_message(uyum))
                return sonuc
            if uyum.fits is True:
                sonuc["bilgi"] = MSG_SIGAR.format(
                    need=hw_mod.format_fit_gb(uyum.required_bytes, uyum.available_bytes),
                    have=hw_mod.format_gb(uyum.available_bytes, round_up=False))
        else:
            sonuc.update(secilebilir=False, neden=MSG_KOSUL_BILINMIYOR.format(tur=tur))
            return sonuc
    return sonuc
