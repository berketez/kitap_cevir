#!/usr/bin/env python3
"""Kitapçevir sürüm betiği (packaging/TASARIM.md G.1, C, H.1): tek komutla DMG.

Tek komut (proje kökünden; sürüm makinesi, Xcode Komut Satırı Araçları gerekli)::

    ./venv/bin/python packaging/surum/surum_cikar.py --surum 1.0.0 \\
        --kaynak <commit|etiket|HEAD|calisma-agaci> --lisans-kabul pdf

Adımlar:

1. Temiz kaynak: ``git archive <commit>`` (ya da çalışma ağacı: izlenen + izlenmeyen
   ama yok sayılmayan dosyalar). Açık yayına girmeyenler ``surum_ayarlari.DISLANANLAR``
   ile dışarıda kalır; arşiv bir bekçiyle yeniden denetlenir.
2. Ölçüm (her sayı sürümde ölçülür): kilitlerdeki her paket için PyPI'daki tekerlek
   (kilit SHA-256'sıyla çapraz denetim), python-build-standalone, uv, tectonic,
   Surya/Marker modelleri (indirilip SHA-256 hesaplanır), HY-MT (HF ağacı; ağırlık
   İNDİRİLMEZ), MacTeX (boyut + SHA-256 + yansıdaki .sha512 doğrulaması), GGUF tekerleği
   (sürüm makinesinde derlenir ve denetlenir).
3. Ölçüm koşusu: sahte köke (sahte HOME) temiz kurulum, bileşen başına ``du`` farkı
   (C.1-3 sırası) ve tectonic ısıtmasının indirdiği dosya sayısı. Kurulum yöneticinin
   kendi ``Kurulum`` sınıfıyla, DMG'deki taşınabilir Python'la koşar.
4. ``manifest.json`` şemaya (stdlib doğrulayıcı + varsa jsonschema) karşı doğrulanır.
5. Uygulama arşivi (``LICENSES/``: GPL, KAYNAK_KODU.txt, resmî Datalab/Tencent metinleri,
   gömülü Python'un ve uv/tectonic/GGUF'un kendi lisans dosyaları; 00_OKU.txt), ``derle.sh``
   ile ad-hoc mühürlü ``Kitapçevir.app``, ``hdiutil`` ile ULFO DMG (+ ilk açılış yönergesi),
   ``SHA256SUMS`` ve ``surum_raporu.json``.

Sürüm makinesinde gerekenler: Xcode Komut Satırı Araçları (swiftc, codesign), ``uv`` ve
``zstd`` (PBS "full" arşivindeki lisans dosyaları için; ``brew install zstd``).

Yayın (``gh release create``) bu betiğin işi değildir; komut sonda yazdırılır.
Depo/kaynak adresi tek yerde: ``packaging/yonetici/ayarlar.py`` (``DEPO``).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fnmatch
import gzip
import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

BURASI = Path(__file__).resolve().parent
DEPO_KOKU = BURASI.parent.parent
sys.path.insert(0, str(BURASI))

import onbellek as ob  # noqa: E402
import surum_ayarlari as SA  # noqa: E402
import tekerlek  # noqa: E402

DERLE_SH = DEPO_KOKU / "packaging" / "kabuk" / "derle.sh"
OLCUM_SURUCUSU = BURASI / "olcum_kosusu.py"
SEMA_GORELI = "packaging/manifest.schema.json"
MANIFEST_GORELI = "packaging/manifest.json"      # yönetici buradan okur (yonetici.MANIFEST_GORELI)

_T0 = time.monotonic()


def gunluk(ileti: str) -> None:
    gecen = time.monotonic() - _T0
    print(f"[{int(gecen // 60):02d}:{gecen % 60:04.1f}] {ileti}", flush=True)


class SurumHatasi(RuntimeError):
    """Sürümü durduran hata (Türkçe ileti)."""


class ArkaPlan:
    """Daemon iş parçacığında tek iş; ``bekle()`` sonucu ya da istisnayı verir."""

    def __init__(self, is_, *argumanlar):
        self._sonuc: Any = None
        self._hata: Optional[BaseException] = None
        self._is = threading.Thread(target=self._calis, args=(is_, *argumanlar), daemon=True)
        self._is.start()

    def _calis(self, is_, *argumanlar) -> None:
        try:
            self._sonuc = is_(*argumanlar)
        except BaseException as exc:  # noqa: BLE001 — bekle()'de yeniden yükseltilir
            self._hata = exc

    def bekle(self) -> Any:
        self._is.join()
        if self._hata is not None:
            raise self._hata
        return self._sonuc


def calistir(argv: Sequence[Any], **kw) -> subprocess.CompletedProcess:
    sonuc = subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kw)
    if sonuc.returncode != 0:
        raise SurumHatasi(f"komut başarısız ({sonuc.returncode}): {' '.join(map(str, argv))}\n"
                          f"{(sonuc.stdout or '')[-2000:]}\n{(sonuc.stderr or '')[-2000:]}")
    return sonuc


def git(*argv: str) -> str:
    return calistir(["git", "-C", DEPO_KOKU, *argv]).stdout.strip()


def sha256_dosya(yol: Path) -> str:
    return ob.dosya_ozetleri(yol)["sha256"]


# ===========================================================================
# 1. Temiz kaynak
# ===========================================================================
def dislanmis_mi(goreli: str) -> bool:
    """``surum_ayarlari.DISLANANLAR`` desenleri (``**/`` kök düzeyini de kapsar)."""
    for desen in SA.DISLANANLAR:
        if desen.startswith("**/"):
            kalan = desen[3:]
            if fnmatch.fnmatchcase(goreli, kalan) or fnmatch.fnmatchcase(goreli, "*/" + kalan):
                return True
        elif fnmatch.fnmatchcase(goreli, desen):
            return True
    return False


def bekci_ihlalleri(adlar: Iterable[str]) -> List[str]:
    """Arşivde bulunmaması gereken yollar (Berke'nin açık yayın listesi)."""
    ihlal = []
    for ad in adlar:
        if (any(ad.startswith(o) for o in SA.BEKCI_ONEKLERI) or ad in SA.BEKCI_DOSYALARI
                or any(fnmatch.fnmatchcase(ad, d) for d in SA.BEKCI_DESENLERI)):
            ihlal.append(ad)
    return ihlal


def kaynak_hazirla(kaynak: str, hedef: Path) -> Dict[str, Any]:
    """Temiz kaynağı ``hedef``'e koyar; commit bilgisini döner."""
    if hedef.exists():
        shutil.rmtree(hedef)
    hedef.mkdir(parents=True)
    alinan: List[str] = []
    atlanan: List[str] = []
    if kaynak == "calisma-agaci":
        commit = git("rev-parse", "HEAD")
        kirli = bool(git("status", "--porcelain"))
        cikti = calistir(["git", "-C", DEPO_KOKU, "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"]).stdout
        for goreli in sorted(set(p for p in cikti.split("\0") if p)):
            kaynak_yol = DEPO_KOKU / goreli
            if dislanmis_mi(goreli):
                atlanan.append(goreli)
                continue
            if not kaynak_yol.is_file() or kaynak_yol.is_symlink():
                continue   # çalışma ağacında silinmiş ya da bağ (depoda bağ yok)
            hedef_yol = hedef / goreli
            hedef_yol.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(kaynak_yol, hedef_yol)
            os.chmod(hedef_yol, 0o755 if os.access(kaynak_yol, os.X_OK) else 0o644)
            alinan.append(goreli)
    else:
        commit = git("rev-parse", "--verify", f"{kaynak}^{{commit}}")
        kirli = False
        surec = subprocess.Popen(["git", "-C", str(DEPO_KOKU), "archive", "--format=tar", commit],
                                 stdout=subprocess.PIPE)
        assert surec.stdout is not None
        with tarfile.open(fileobj=surec.stdout, mode="r|") as tar:
            for uye in tar:
                if not uye.isfile():
                    continue
                if dislanmis_mi(uye.name):
                    atlanan.append(uye.name)
                    continue
                hedef_yol = hedef / uye.name
                hedef_yol.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(uye)
                assert f is not None
                with open(hedef_yol, "wb") as yaz:
                    shutil.copyfileobj(f, yaz)
                os.chmod(hedef_yol, 0o755 if uye.mode & 0o111 else 0o644)
                alinan.append(uye.name)
        if surec.wait() != 0:
            raise SurumHatasi(f"git archive başarısız: {commit}")
    kisa = git("rev-parse", "--short=7", commit)
    return {"dizin": hedef, "commit": commit, "kisa": kisa, "kirli": kirli,
            "yapi_sayisi": int(git("rev-list", "--count", commit)),
            "zaman": int(git("log", "-1", "--format=%ct", commit)),
            "alinan": len(alinan), "atlanan": sorted(atlanan), "kaynak": kaynak}


@contextlib.contextmanager
def _bytecode_kapali():
    """İçe aktarma süresince ``__pycache__`` yazılmaz (K9 E1).

    Temiz kaynaktan içe aktarılan modüllerin ``.pyc``'leri kaynağın içine yazılıyor ve
    uygulama arşivine giriyordu; ``co_filename`` alanları sürüm makinesinin yolunu
    (kullanıcı adı, karalama klasörü) taşıyordu ve arşiv yeniden üretilemez oluyordu.
    """
    onceki = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = onceki


def yonetici_modulleri(kaynak_dizini: Path) -> Dict[str, Any]:
    """Temiz kaynaktaki yönetici modülleri (kilit okuma, manifest doğrulama, lisans
    NOTICE'ı kurulumla birebir aynı kodla yapılsın diye). Kaynağa bytecode yazılmaz."""
    yol = str(kaynak_dizini / "packaging")
    if yol not in sys.path:
        sys.path.insert(0, yol)
    with _bytecode_kapali():
        return {ad: importlib.import_module(f"yonetici.{ad}")
                for ad in ("ayarlar", "durum", "manifest", "lisans", "disk")} | {
            "arsiv": importlib.import_module("yonetici.adimlar.arsiv"),
            # Sistem PATH'inin tek kaynağı (Finder ortamı; adimlar/ortak.py SISTEM_PATH)
            "ortak": importlib.import_module("yonetici.adimlar.ortak"),
            # Boyut biçimi (yalnız stdlib; F12): sürüm notu uygulamayla aynı biçimde yazılır
            "bicim": _dosyadan_modul("kitapcevir_ui_bicim", kaynak_dizini / "ui" / "bicim.py")}


def _dosyadan_modul(ad: str, yol: Path) -> Any:
    import importlib.util
    spec = importlib.util.spec_from_file_location(ad, yol)
    modul = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with _bytecode_kapali():
        spec.loader.exec_module(modul)
    return modul


def branding_oku(kaynak_dizini: Path) -> Dict[str, str]:
    """Ekrandaki adlar ``ui/branding.py``'den (tek kaynak), ayrı süreçte.

    ``core/__init__.py`` atlanır (yöneticinin ``donanim.core_hardware`` kabuğu).
    """
    betik = (
        "import json, sys\n"
        "from pathlib import Path\n"
        f"k = Path({str(kaynak_dizini)!r})\n"
        "sys.path.insert(0, str(k / 'packaging'))\n"
        "from yonetici import donanim\n"
        "donanim.core_hardware(k)\n"
        "import ui.branding as b\n"
        "print(json.dumps({'MODEL_TITLE': b.MODEL_TITLE, 'MODEL_BETA_LABEL': b.MODEL_BETA_LABEL,"
        " 'model_tirnakli': b.quoted(b.MODEL_TITLE), 'APP_NAME': b.APP_NAME},"
        " ensure_ascii=False))\n")
    # -B: alt süreç temiz kaynağa __pycache__ yazmasın (K9 E1).
    sonuc = calistir([sys.executable, "-I", "-B", "-c", betik])
    return json.loads(sonuc.stdout.strip().splitlines()[-1])


def derle_sabitleri() -> Dict[str, str]:
    """``derle.sh --sabitler``: kabuk tarafının tek kaynağı (MIN_MACOS …)."""
    cikti = calistir(["/bin/bash", DERLE_SH, "--sabitler"]).stdout
    return dict(satir.split("=", 1) for satir in cikti.splitlines() if "=" in satir)


# ===========================================================================
# 2. Ölçüm: kaynaklardan boyut ve SHA-256
# ===========================================================================
def python_surumu() -> Tuple[int, int]:
    ana, alt = SA.PBS_PYTHON.split(".")[:2]
    return int(ana), int(alt)


def _ad(paket: Any) -> str:
    """PEP 503 adı (``llama-cpp-python``); yöneticinin ``durum.normal_ad``'ı ``_`` kullanır,
    sürüm ayarları ve PyPI ``-`` biçimindedir."""
    return tekerlek.normal_ad(paket.ad)


def kilitleri_oku(m: Dict[str, Any], kaynak_dizini: Path) -> Dict[str, List[Any]]:
    kilitler = {}
    for grup in SA.KILIT_GRUPLARI:
        yol = kaynak_dizini / "packaging" / "requirements" / f"{grup}.txt"
        kilitler[grup] = m["durum"].kilit_oku(yol)
    return kilitler


def grup_paketleri(kilitler: Mapping[str, Sequence[Any]]
                   ) -> Tuple[Dict[str, List[Any]], Dict[str, List[str]]]:
    """Her gruba yalnız önceki gruplarda olmayan paketler (C.1-3). Sürüm çelişkisi = dur.

    İkinci dönüş: grubun tekerleğini paylaştığı önceki gruplar. Kurulum çevrimdışı
    olduğundan bir grup, ortak paketinin tekerleğini taşıyan grup kurulmadan
    kurulamaz: bu gruplar bileşenin ``bagimliliklar``'ına yazılır.
    """
    gorulen: Dict[str, Tuple[str, str]] = {}
    yeni: Dict[str, List[Any]] = {}
    bagimli: Dict[str, List[str]] = {}
    for grup in SA.KILIT_GRUPLARI:
        yeni[grup] = []
        bagimli[grup] = []
        for p in kilitler[grup]:
            if _ad(p) in gorulen:
                if gorulen[_ad(p)][0] not in bagimli[grup]:
                    bagimli[grup].append(gorulen[_ad(p)][0])
                if gorulen[_ad(p)][1] != p.surum:
                    raise SurumHatasi(
                        f"Kilitler arasında sürüm çelişkisi: {p.ad} {gorulen[_ad(p)][0]}.txt'de "
                        f"{gorulen[_ad(p)][1]}, {grup}.txt'de {p.surum}. Sıralı kurulumda önceki "
                        "grubun paketi bozulur; kilitler tek çözümle yeniden üretilmeli "
                        "(packaging/requirements/derle_kilit.sh).")
                continue
            gorulen[_ad(p)] = (grup, p.surum)
            yeni[grup].append(p)
    for grup in bagimli:
        bagimli[grup] = [g for g in SA.KILIT_GRUPLARI if g in bagimli[grup]]
    return yeni, bagimli


def pypi_tekerlekleri(on: ob.Onbellek, paket: Any) -> List[Dict[str, Any]]:
    """PyPI'daki dosyalardan kilit özetiyle eşleşen tekerlekler."""
    veri = on.json_oku(SA.PYPI_JSON_URL.format(ad=_ad(paket), surum=paket.surum))
    ozetler = {o.lower() for o in paket.ozetler}
    dosyalar = []
    for d in veri.get("urls") or []:
        if d.get("packagetype") != "bdist_wheel" or d.get("yanked"):
            continue
        sha = str((d.get("digests") or {}).get("sha256", "")).lower()
        if sha in ozetler:
            dosyalar.append({"filename": d["filename"], "url": d["url"], "size": int(d["size"]),
                             "sha256": sha})
    sdist = [d for d in veri.get("urls") or [] if d.get("packagetype") == "sdist"
             and str((d.get("digests") or {}).get("sha256", "")).lower() in ozetler]
    return dosyalar, sdist


def tekerlek_ogeleri(on: ob.Onbellek, yeni: Mapping[str, Sequence[Any]],
                     gguf_tekerlegi: Optional[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Grup → manifest tekerlek öğeleri (macOS koşullarıyla)."""
    py = python_surumu()
    sonuc: Dict[str, List[Dict[str, Any]]] = {g: [] for g in yeni}

    def _paket(grup: str, p: Any) -> List[Dict[str, Any]]:
        if _ad(p) in SA.KAYNAKTAN_DERLENENLER:
            if gguf_tekerlegi is None:
                raise SurumHatasi(f"{p.ad}: sürümde derlenen tekerlek yok")
            return [gguf_tekerlegi["oge"]]
        dosyalar, _ = pypi_tekerlekleri(on, p)
        if not dosyalar:
            raise SurumHatasi(f"{p.ad}=={p.surum}: PyPI'da kilit özetiyle eşleşen tekerlek yok")
        try:
            secim = tekerlek.sec(dosyalar, py, SA.MACOS_ANA_SURUMLERI)
        except ValueError as exc:
            raise SurumHatasi(f"{p.ad}=={p.surum}: {exc}") from exc
        ogeler = []
        for kayit, kosul in tekerlek.kosullar(secim):
            oge = {"kimlik": kayit["filename"], "url": kayit["url"], "bayt": kayit["size"],
                   "sha256": kayit["sha256"], "hedef": f"indirilen/tekerlek/{kayit['filename']}",
                   "tur": "tekerlek"}
            if kosul:
                oge["kosul"] = {"macos": kosul}
            ogeler.append(oge)
        return ogeler

    with ThreadPoolExecutor(max_workers=SA.INDIRME_IS_PARCACIGI) as havuz:
        isler = {(g, _ad(p)): havuz.submit(_paket, g, p) for g in yeni for p in yeni[g]}
        for (g, _), is_ in isler.items():
            sonuc[g].extend(is_.result())
    return sonuc


def ozet_dosyasindan(metin: str, ad: str) -> str:
    """``<hex>  <ad>`` satırlarından ``ad``'ın SHA-256'sı."""
    for satir in metin.splitlines():
        parcalar = satir.strip().split()
        if len(parcalar) >= 2 and parcalar[-1].lstrip("*") == ad and re.fullmatch(
                r"[0-9a-f]{64}", parcalar[0]):
            return parcalar[0]
    raise SurumHatasi(f"özet dosyasında {ad} yok")


def ayar_dosyasi_oku(tekerlek_yolu: Path, uye: str) -> str:
    with zipfile.ZipFile(tekerlek_yolu) as z:
        return z.read(uye).decode("utf-8")


def datalab_kaynaklari(surya_tekerlegi: Path, marker_tekerlegi: Path) -> Dict[str, Any]:
    """Kilitteki surya/marker sürümünün model klasörleri, model adresi ve yazıtipi adresi."""
    surya = ayar_dosyasi_oku(surya_tekerlegi, SA.SURYA_AYAR_DOSYASI)
    marker = ayar_dosyasi_oku(marker_tekerlegi, SA.MARKER_AYAR_DOSYASI)
    taban = re.search(r'S3_BASE_URL\s*:\s*str\s*=\s*"(https://[^"]+)"', surya)
    klasorler = sorted(set(re.findall(r'_MODEL_CHECKPOINT\s*:\s*str\s*=\s*"s3://([^"]+)"', surya)))
    artifakt = re.search(r'ARTIFACT_URL\s*:\s*str\s*=\s*"(https://[^"]+)"', marker)
    yazitipi = re.search(r'FONT_NAME\s*:\s*str\s*=\s*"([^"]+)"', marker)
    if not (taban and klasorler and artifakt and yazitipi):
        raise SurumHatasi("surya/marker settings.py'den model adresleri okunamadı")
    return {"taban": taban.group(1).rstrip("/"), "klasorler": klasorler,
            "yazitipi_url": f"{artifakt.group(1).rstrip('/')}/{yazitipi.group(1)}",
            "yazitipi_adi": yazitipi.group(1)}


def guvenli_kimlik(*parcalar: str) -> str:
    metin = "-".join(parcalar)
    metin = re.sub(r"[^A-Za-z0-9._+-]+", "-", metin).strip("-.")
    return metin[:200]


# ===========================================================================
# GGUF tekerleği (TASARIM G.1-5): sürüm makinesinde derlenir ve denetlenir
# ===========================================================================
def gguf_tekerlegi_hazirla(on: ob.Onbellek, kilitler: Mapping[str, Sequence[Any]],
                           pbs_python: Path, min_macos: str, depo: str, surum: str,
                           is_dizini: Path, sistem_path: str) -> Dict[str, Any]:
    ad = next(iter(SA.KAYNAKTAN_DERLENENLER))
    paket = next((p for p in kilitler["gguf"] if _ad(p) == ad), None)
    if paket is None:
        raise SurumHatasi(f"gguf.txt'de {ad} yok")
    _, sdistler = pypi_tekerlekleri(on, paket)
    if not sdistler:
        raise SurumHatasi(f"{ad}=={paket.surum}: kilit özetiyle eşleşen kaynak paketi yok")
    sd = sdistler[0]
    sdist = on.dosya(sd["url"], sha256=sd["digests"]["sha256"], bayt=int(sd["size"]))
    ortam_ek = dict(SA.KAYNAKTAN_DERLENENLER[ad])
    ortam_ek["MACOSX_DEPLOYMENT_TARGET"] = min_macos
    anahtar = hashlib.sha256(json.dumps({"sdist": sdist.sha256, "ortam": ortam_ek,
                                         "python": SA.PBS_PYTHON}, sort_keys=True).encode()
                             ).hexdigest()[:16]
    klasor = on.kok / "gguf" / anahtar
    mevcut = sorted(klasor.glob("*.whl")) if klasor.is_dir() else []
    if mevcut:
        tekerlek_yolu = mevcut[0]
        gunluk(f"GGUF tekerleği önbellekten: {tekerlek_yolu.name}")
    else:
        gunluk(f"GGUF tekerleği derleniyor ({ad} {paket.surum}, Metal; birkaç dakika)")
        uv = shutil.which("uv")
        if not uv:
            raise SurumHatasi("uv bulunamadı (GGUF tekerleği derlemesi için gerekli)")
        gecici = klasor.with_name(anahtar + ".yeni")
        shutil.rmtree(gecici, ignore_errors=True)
        gecici.mkdir(parents=True)
        ortam = {"PATH": sistem_path, "HOME": os.environ.get("HOME", ""), **ortam_ek}
        t0 = time.monotonic()
        calistir([uv, "build", "--wheel", "--python", pbs_python, "--out-dir", gecici, sdist.yol],
                 env=ortam)
        gunluk(f"  derleme {time.monotonic() - t0:.0f} sn")
        uretilen = sorted(gecici.glob("*.whl"))
        if len(uretilen) != 1:
            raise SurumHatasi(f"GGUF derlemesi tek tekerlek üretmedi: {uretilen}")
        shutil.rmtree(klasor, ignore_errors=True)
        os.replace(gecici, klasor)
        tekerlek_yolu = klasor / uretilen[0].name
    denetim = gguf_tekerlegi_denetle(tekerlek_yolu, pbs_python, min_macos, kilitler, on, is_dizini)
    sha = sha256_dosya(tekerlek_yolu)
    oge = {"kimlik": tekerlek_yolu.name,
           "url": f"https://github.com/{depo}/releases/download/v{surum}/{tekerlek_yolu.name}",
           "bayt": tekerlek_yolu.stat().st_size, "sha256": sha,
           "hedef": f"indirilen/tekerlek/{tekerlek_yolu.name}", "tur": "tekerlek"}
    return {"yol": tekerlek_yolu, "oge": oge, "denetim": denetim, "sdist_sha256": sdist.sha256,
            "sdist_yol": sdist.yol, "surum": paket.surum}


def gguf_tekerlegi_denetle(yol: Path, pbs_python: Path, min_macos: str,
                           kilitler: Mapping[str, Sequence[Any]], on: ob.Onbellek,
                           is_dizini: Path) -> Dict[str, Any]:
    """ZIP bütünlüğü, dylib'lerde ``minos``, ayrı venv'de ``llama_supports_gpu_offload()``."""
    with zipfile.ZipFile(yol) as z:
        bozuk = z.testzip()
        if bozuk is not None:
            raise SurumHatasi(f"GGUF tekerleği bozuk ZIP: {bozuk}")
        dylibler = [n for n in z.namelist() if n.endswith(".dylib") or n.endswith(".so")]
    kok = is_dizini / "gguf_denetim"
    shutil.rmtree(kok, ignore_errors=True)
    kok.mkdir(parents=True)
    with zipfile.ZipFile(yol) as z:
        z.extractall(kok / "acik")
    minos = {}
    for d in dylibler:
        cikti = calistir(["/usr/bin/otool", "-l", kok / "acik" / d]).stdout
        m = re.search(r"cmd LC_BUILD_VERSION.*?minos ([0-9.]+)", cikti, re.S)
        minos[d] = m.group(1) if m else ""
    yanlis = {d: v for d, v in minos.items() if v != min_macos}
    if yanlis:
        raise SurumHatasi(f"GGUF tekerleğinde minos {min_macos} olmayan kitaplık: {yanlis}")
    # Ayrı venv: kilitteki bağımlılıklar + tekerlek, çevrimdışı; model YÜKLENMEZ.
    venv = kok / "venv"
    calistir([pbs_python, "-m", "venv", venv])
    depo = kok / "depo"
    depo.mkdir()
    py = python_surumu()
    gerekenler = []
    for p in kilitler["gguf"]:
        if _ad(p) in SA.KAYNAKTAN_DERLENENLER:
            continue
        dosyalar, _ = pypi_tekerlekleri(on, p)
        kayit = tekerlek.sec(dosyalar, py, [int(platform.mac_ver()[0].split(".")[0])])
        secilen = next(iter(kayit.values()))
        k = on.dosya(secilen["url"], sha256=secilen["sha256"], bayt=secilen["size"],
                     ad=secilen["filename"])
        ob.klonla(k.yol, depo / secilen["filename"])
        gerekenler.append(secilen["filename"])
    ob.klonla(yol, depo / yol.name)
    calistir([venv / "bin" / "python", "-m", "pip", "install", "--no-index", "--no-deps",
              "--disable-pip-version-check", "-q",
              *[depo / ad for ad in gerekenler], depo / yol.name])
    sonuc = calistir([venv / "bin" / "python", "-I", "-c",
                      "from llama_cpp import llama_cpp as l; print(bool(l.llama_supports_gpu_offload()))"])
    gpu = sonuc.stdout.strip().splitlines()[-1] == "True"
    if not gpu:
        raise SurumHatasi("GGUF tekerleği Metal desteksiz derlenmiş (llama_supports_gpu_offload False)")
    shutil.rmtree(kok, ignore_errors=True)
    return {"zip": "tamam", "minos": sorted(set(minos.values())), "dylib_sayisi": len(dylibler),
            "gpu_offload": gpu}


# ===========================================================================
# Manifest kurgusu
# ===========================================================================
def bilesen(kimlik: str, *, ad: str, aciklama: str, zorunluluk: str, ogeler: List[Dict[str, Any]],
            adimlar: List[Dict[str, Any]], dogrulama: Any, boyut: Dict[str, Any],
            grup: Optional[str] = None, **ek: Any) -> Dict[str, Any]:
    b: Dict[str, Any] = {"kimlik": kimlik, "ad": ad, "aciklama": aciklama,
                         "zorunluluk": zorunluluk, "grup": grup}
    b.update({k: v for k, v in ek.items() if v is not None})
    b.update({"boyut": boyut, "ogeler": ogeler, "adimlar": adimlar, "dogrulama": dogrulama})
    return b


def indirme_bayt(m_manifest: Any, ogeler: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """Koşullu öğe varsa macOS'a göre, yoksa ``hepsi`` (manifest.dogrula kuralı)."""
    if not any(m_manifest.oge_kosullu_mu(o) for o in ogeler):
        return {"hepsi": sum(int(o["bayt"]) for o in ogeler)}
    return {str(ana): sum(int(o["bayt"]) for o in ogeler
                          if m_manifest.oge_uygun_mu(o, str(ana)))
            for ana in SA.MACOS_ANA_SURUMLERI}


def manifest_kur(*, surum: str, kaynak: Mapping[str, Any], sabitler: Mapping[str, str],
                 m: Mapping[str, Any], olcum_bilgisi: Mapping[str, str], marka: Mapping[str, str],
                 pbs: ob.Kayit, uv: ob.Kayit, tectonic: ob.Kayit, mactex: Mapping[str, Any],
                 tekerlekler: Mapping[str, List[Dict[str, Any]]], datalab: Mapping[str, Any],
                 hymt: Mapping[str, Any], disk: Mapping[str, int], sayac: Optional[int],
                 olcum_notu: str, kilit_surumleri: Mapping[str, str],
                 bagimli: Mapping[str, List[str]]) -> Dict[str, Any]:
    mf = m["manifest"]

    def boyut(kimlik: str, ogeler, *, not_=None, disk_bayt=None):
        return {"indirme_bayt": indirme_bayt(mf, ogeler),
                "disk_bayt": {"hepsi": int(disk_bayt if disk_bayt is not None else disk[kimlik])},
                "olcum_notu": not_ or olcum_notu}

    uv_oge = {"kimlik": f"uv-{SA.UV_SURUMU}", "url": SA.UV_URL, "bayt": uv.bayt,
              "sha256": uv.sha256, "hedef": f"indirilen/{SA.UV_ARSIV_ADI}", "tur": "arsiv"}
    cekirdek_ogeleri = [uv_oge] + tekerlekler["cekirdek"]
    tec_oge = {"kimlik": f"tectonic-{SA.TECTONIC_SURUMU}", "url": SA.TECTONIC_URL,
               "bayt": tectonic.bayt, "sha256": tectonic.sha256,
               "hedef": f"indirilen/{SA.TECTONIC_ARSIV_ADI}", "tur": "arsiv"}
    # sha512_url YAZILMAZ: yansıdaki .sha512 bayat (surum_ayarlari.MACTEX_TUG_SAYFASI notu);
    # kurulum sürümde ölçülen SHA-256 ile doğrular.
    mactex_oge = {"kimlik": "mactex", "url": SA.MACTEX_URL, "bayt": mactex["bayt"],
                  "sha256": mactex["sha256"], "hedef": "indirilen/MacTeX.pkg", "tur": "pkg"}
    pdf_ogeleri = list(datalab["ogeler"]) + tekerlekler["pdf"]
    ad = SA.BILESEN_METINLERI
    hymt_aciklama = (f"Önerilen yerel çeviri modeli; {marka['model_tirnakli']} "
                     f"({marka['MODEL_BETA_LABEL'].lower()}) için")
    marker_surum = kilit_surumleri["marker-pdf"]
    bilesenler = [
        bilesen("cekirdek", ad=ad["cekirdek"][0], aciklama=ad["cekirdek"][1], zorunluluk="zorunlu",
                surum=surum, bagimliliklar=[], donanim_kosulu=None,
                lisans=dict(SA.LISANSLAR["cekirdek"]),
                ogeler=cekirdek_ogeleri,
                adimlar=[{"tur": "uv_kur", "oge": uv_oge["kimlik"], "uye": SA.UV_UYESI},
                         {"tur": "tekerlek_kur", "grup": "cekirdek"}],
                dogrulama={"tur": "python_ice_aktar", "moduller": SA.DOGRULAMA_MODULLERI["cekirdek"]},
                boyut=boyut("cekirdek", cekirdek_ogeleri)),
        bilesen("mlx", ad=ad["mlx"][0], aciklama=ad["mlx"][1], zorunluluk="zorunlu",
                surum=kilit_surumleri["mlx"], bagimliliklar=bagimli["mlx"], donanim_kosulu=None,
                lisans=dict(SA.LISANSLAR["mlx"]), ogeler=tekerlekler["mlx"],
                adimlar=[{"tur": "tekerlek_kur", "grup": "mlx"}],
                dogrulama={"tur": "python_ice_aktar", "moduller": SA.DOGRULAMA_MODULLERI["mlx"]},
                boyut=boyut("mlx", tekerlekler["mlx"])),
        bilesen("pdf", ad=ad["pdf"][0], aciklama=ad["pdf"][1], zorunluluk="zorunlu",
                surum=marker_surum, bagimliliklar=bagimli["pdf"], donanim_kosulu=None,
                lisans={"spdx": SA.PDF_LISANS_SPDX, "onay_gerekli": True,
                        "metin_dosyasi": SA.DATALAB_LISANS_GORELI,
                        "metin_sha256": datalab["lisans_sha256"],
                        "metin_url": SA.MARKER_LISANS_GORUNEN.format(surum=marker_surum),
                        "dagitim": "indirilir",
                        "kaynak_kodu_url": SA.MARKER_KAYNAK_URL.format(surum=marker_surum)},
                ogeler=pdf_ogeleri,
                adimlar=[{"tur": "tekerlek_kur", "grup": "pdf"}, {"tur": "surya_manifest_yaz"}],
                dogrulama=[{"tur": "python_ice_aktar", "moduller": SA.DOGRULAMA_MODULLERI["pdf"]},
                           {"tur": "dosyalar_sha"}],
                boyut=boyut("pdf", pdf_ogeleri)),
        bilesen("tex-hafif", ad=ad["tex-hafif"][0], aciklama=ad["tex-hafif"][1],
                zorunluluk="secim", grup=SA.TEX_GRUBU[0], surum=SA.TECTONIC_SURUMU,
                bagimliliklar=["cekirdek"], donanim_kosulu=None,
                lisans=dict(SA.LISANSLAR["tex-hafif"],
                            kaynak_kodu_url=SA.TECTONIC_KAYNAK_URL),
                ogeler=[tec_oge],
                adimlar=[{"tur": "arsiv_ac", "oge": tec_oge["kimlik"], "hedef": "tex/bin",
                          "uyeler": {"tectonic": "tectonic"}},
                         {"tur": "dosya_yerlestir", "kaynak": "uygulama:packaging/tex/xelatex",
                          "hedef": "tex/shim/xelatex", "kip": "755"},
                         {"tur": "tectonic_isit"}],
                dogrulama={"tur": "tex_derle"}, kaldirilabilir=True,
                sayac_beklenen=({"birim": "dosya", "n": int(sayac)} if sayac is not None else None),
                boyut=boyut("tex-hafif", [tec_oge])),
        bilesen("tex-tam", ad=ad["tex-tam"][0], aciklama=ad["tex-tam"][1], zorunluluk="secim",
                grup=SA.TEX_GRUBU[0], bagimliliklar=[], donanim_kosulu=None,
                lisans=dict(SA.LISANSLAR["tex-tam"]), ogeler=[mactex_oge],
                adimlar=[{"tur": "mactex_pkg_ac", "oge": "mactex", "yol": SA.MACTEX_XELATEX},
                         {"tur": "tex_algila_sina", "yol": SA.MACTEX_XELATEX}],
                dogrulama={"tur": "tex_derle"},
                boyut=boyut("tex-tam", [mactex_oge], disk_bayt=mactex["bayt"],
                            not_=("İndirme baytı ve SHA-256 sürümde ölçüldü (CTAN, "
                                  f"Last-Modified {mactex['son_degisiklik'] or 'bilinmiyor'}; "
                                  "SHA-512 TUG'un yayımladığıyla aynı). Adres sürümsüzdür: "
                                  "sonraki MacTeX yayımlanınca bu özet tutmaz, yeni Kitapçevir "
                                  "sürümü gerekir. Kurulum macOS Installer ile /usr/local/texlive'a "
                                  "yapılır; kurulu boyut ÖLÇÜLMEDİ. Diskteki sayı yalnız paketin "
                                  "boyutudur (en az bu kadar yer gerekir)."))),
        bilesen("tex-kurulu", ad=ad["tex-kurulu"][0], aciklama=ad["tex-kurulu"][1],
                zorunluluk="secim", grup=SA.TEX_GRUBU[0], bagimliliklar=[], donanim_kosulu=None,
                on_kosul={"algila": "tex_kurulu"}, ogeler=[],
                adimlar=[{"tur": "tex_algila_sina"}], dogrulama={"tur": "tex_derle"},
                boyut={"indirme_bayt": {"hepsi": 0}, "disk_bayt": {"hepsi": 0},
                       "olcum_notu": "Öğe yok: bu Mac'te kurulu TeX kullanılır."}),
        bilesen("hymt", ad=ad["hymt"][0], aciklama=hymt_aciklama, zorunluluk="istege_bagli",
                surum=SA.HYMT_COMMIT[:7], bagimliliklar=["mlx"],
                donanim_kosulu=[{"tur": "model_sigar", "agirlik_bayt": hymt["agirlik_bayt"],
                                 "config": hymt["config"], "kv": hymt["kv"]},
                                {"tur": "yerel_model_genel_kapi"}],
                lisans={"spdx": SA.HYMT_LISANS_SPDX, "onay_gerekli": True,
                        "metin_dosyasi": SA.TENCENT_LISANS_GORELI,
                        "metin_sha256": hymt["lisans_sha256"], "metin_url": hymt["lisans_gorunen"],
                        "dagitim": "indirilir"},
                ogeler=hymt["ogeler"],
                adimlar=[{"tur": "lisans_yaz", "oge": "hymt-lisans", "dizin": SA.HYMT_KLASORU,
                          "lisans_klasoru": SA.TENCENT_LISANS_KLASORU}],
                dogrulama={"tur": "dosyalar_sha"}, kaldirilabilir=True,
                boyut={"indirme_bayt": {"hepsi": hymt["toplam_bayt"]},
                       "disk_bayt": {"hepsi": hymt["toplam_bayt"]},
                       "olcum_notu": (f"Hugging Face ağacından tam bayt ({SA.HYMT_DEPO}@"
                                      f"{SA.HYMT_COMMIT[:7]}); ağırlıklar ölçüm için indirilmedi, "
                                      "diskteki boyut indirmeye eşit sayıldı.")}),
        bilesen("gguf", ad=ad["gguf"][0], aciklama=ad["gguf"][1], zorunluluk="istege_bagli",
                surum=kilit_surumleri["llama-cpp-python"], bagimliliklar=bagimli["gguf"],
                donanim_kosulu={"tur": "yerel_model_genel_kapi"},
                lisans=dict(SA.LISANSLAR["gguf"]), ogeler=tekerlekler["gguf"],
                adimlar=[{"tur": "tekerlek_kur", "grup": "gguf",
                          "kaynaktan": sorted(SA.KAYNAKTAN_DERLENENLER),
                          "derleme_ortami": dict(next(iter(SA.KAYNAKTAN_DERLENENLER.values())))}],
                dogrulama={"tur": "python_ice_aktar", "moduller": SA.DOGRULAMA_MODULLERI["gguf"]},
                kaldirilabilir=True, boyut=boyut("gguf", tekerlekler["gguf"])),
    ]
    return {
        "sema_surumu": m["ayarlar"].MANIFEST_SEMA_SURUMU,
        "uygulama": {
            "surum": surum,
            "yapi": kaynak["kisa"] + ("-kirli" if kaynak["kirli"] else ""),
            "min_macos": sabitler["MIN_MACOS"], "mimari": mf.MIMARI,
            "calisma_zamani": {"kimlik": f"cpython-{SA.PBS_PYTHON}", "url": SA.PBS_URL,
                               "bayt": pbs.bayt, "sha256": pbs.sha256,
                               "hedef": f"calisma_zamani/{SA.PBS_ARSIV_ADI}", "tur": "arsiv"},
        },
        "olcum": dict(olcum_bilgisi),
        "gruplar": [{"kimlik": SA.TEX_GRUBU[0], "ad": SA.TEX_GRUBU[1], "tur": mf.GRUP_TURU,
                     "varsayilan": SA.TEX_GRUBU[2], "zorunlu": True}],
        "bilesenler": bilesenler,
    }


def manifest_dogrula(m: Mapping[str, Any], veri: Mapping[str, Any], kaynak_dizini: Path
                     ) -> Dict[str, Any]:
    """Stdlib doğrulayıcı (kurulumla aynı kod) + varsa gerçek jsonschema (Draft 2020-12)."""
    sorunlar = m["manifest"].dogrula(veri)
    if sorunlar:
        raise SurumHatasi("manifest stdlib doğrulamasından geçmedi:\n  " + "\n  ".join(sorunlar))
    try:
        import jsonschema
    except ImportError:
        return {"stdlib": "geçti", "jsonschema": "kurulu değil (yalnız stdlib doğrulandı)"}
    sema = json.loads((kaynak_dizini / SEMA_GORELI).read_text(encoding="utf-8"))
    hatalar = sorted(jsonschema.Draft202012Validator(sema).iter_errors(veri), key=str)
    if hatalar:
        raise SurumHatasi("manifest JSON şemasından geçmedi:\n  " + "\n  ".join(
            f"{'/'.join(map(str, h.absolute_path))}: {h.message}" for h in hatalar[:20]))
    from importlib import metadata
    return {"stdlib": "geçti", "jsonschema": f"geçti (jsonschema {metadata.version('jsonschema')}, "
                                              "Draft 2020-12)"}


# ===========================================================================
# 3. Uygulama arşivi, lisanslar, ölçüm koşusu
# ===========================================================================
def pbs_full_lisanslari(arsiv: Path, zstd: str) -> Dict[str, bytes]:
    """PBS "full" arşivinin ``python/licenses/`` klasörü (zstd'li tar; akışla okunur)."""
    surec = subprocess.Popen([zstd, "-dc", str(arsiv)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    assert surec.stdout is not None and surec.stderr is not None
    bulunan: Dict[str, bytes] = {}
    hata: Optional[BaseException] = None
    try:
        with tarfile.open(fileobj=surec.stdout, mode="r|") as tar:
            for uye in tar:
                ad = uye.name[len(SA.PBS_LISANS_KLASORU):]
                if uye.isfile() and uye.name.startswith(SA.PBS_LISANS_KLASORU) and "/" not in ad:
                    f = tar.extractfile(uye)
                    assert f is not None
                    bulunan[ad] = f.read()
    except tarfile.TarError as exc:
        hata = exc
    finally:
        surec.stdout.close()
        hata_metni = surec.stderr.read().decode("utf-8", "replace")
        kod = surec.wait()
    if kod != 0 or hata is not None:
        raise SurumHatasi(f"PBS full arşivi açılamadı ({arsiv.name}; zstd çıkış {kod}): "
                          f"{hata or hata_metni.strip()[-500:]}")
    if not bulunan:
        raise SurumHatasi(f"PBS full arşivinde {SA.PBS_LISANS_KLASORU} yok: {arsiv.name}")
    return bulunan


_TCL_GORUNEN_AD = {"tk": "Tk"}       # 00_OKU'da ürün adı (itcl, thread kendi adlarıyla)
_TCL_EKLENTI_KLASORU = re.compile(r"^python/lib/(itcl|thread)(\d+\.\d+\.\d+)/")
_TK_PAKET_DIZINI = re.compile(r"^python/lib/tk[0-9.]+/pkgIndex\.tcl$")
_TK_SURUMU = re.compile(r"package ifneeded tk (\d+\.\d+\.\d+)")


def tcl_eklenti_surumleri(pbs_arsivi: Path) -> Dict[str, str]:
    """DMG'deki Python'un Tk, itcl ve thread sürümleri (install_only arşivinden ölçülür)."""
    surumler: Dict[str, str] = {}
    with tarfile.open(pbs_arsivi) as tar:
        for uye in tar:
            bulunan = _TCL_EKLENTI_KLASORU.match(uye.name)
            if bulunan:
                surumler.setdefault(bulunan.group(1), bulunan.group(2))
            elif uye.isfile() and _TK_PAKET_DIZINI.match(uye.name):
                f = tar.extractfile(uye)
                assert f is not None
                tk = _TK_SURUMU.search(f.read().decode("utf-8", "replace"))
                if tk:
                    surumler["tk"] = tk.group(1)
    eksik = sorted(set(SA.TCL_EK_LISANS_URLLERI) - set(surumler))
    if eksik:
        raise SurumHatasi("PBS arşivinde sürümü bulunamayan Tcl bileşeni: " + ", ".join(eksik)
                          + " (surum_ayarlari.TCL_EK_LISANS_URLLERI denetlenmeli)")
    return surumler


_HACL_BASLIGI = re.compile(r"\A\s*/\*\s*MIT License.*?\*/", re.S)


def hacl_lisansi(c_kaynagi: str) -> str:
    """HACL* dosyasının başındaki MIT bildirimi (telif satırları dahil), yorum işaretsiz."""
    bulunan = _HACL_BASLIGI.search(c_kaynagi)
    if not bulunan or "Permission is hereby granted" not in bulunan.group(0):
        raise SurumHatasi("HACL* kaynağında MIT bildirimi bulunamadı (CPython Modules/_hacl)")
    satirlar = []
    for satir in bulunan.group(0).splitlines():
        satir = satir.strip()
        if satir.startswith("/*"):
            satir = satir[2:]
        if satir.endswith("*/"):
            satir = satir[:-2]
        satir = satir.strip()
        if satir.startswith("*"):
            satir = satir[1:]
        satirlar.append(satir.strip())
    return "\n".join(satirlar).strip() + "\n"


def ucuncu_taraf_lisanslari(on: Any, *, pbs: Any, pbs_full: Any, gguf_sdist: Path,
                            gguf_surum: str, zstd: str) -> Dict[str, Dict[str, Any]]:
    """``LICENSES/<grup>/`` altına konacak lisans dosyaları ve 00_OKU açıklamaları.

    Dönüş: ``{grup: {"aciklama": str, "dosyalar": {ad: bayt}, "kaynak": [adres…]}}``.
    Metinler özet değil, bileşenin kendi lisans dosyasıdır (KARAR 34).
    """
    python = dict(pbs_full_lisanslari(pbs_full.yol, zstd))
    kaynaklar = [f"{SA.PBS_FULL_URL} ({SA.PBS_LISANS_KLASORU})"]
    surumler = tcl_eklenti_surumleri(pbs.yol)
    for ad, sablon in SA.TCL_EK_LISANS_URLLERI.items():
        url = sablon.format(tire=surumler[ad].replace(".", "-"))
        python[f"LICENSE.{ad}.txt"] = on.dosya(url, ad=f"{ad}-license.terms",
                                               basla_denetle=False).yol.read_bytes()
        kaynaklar.append(url)
    hacl_url = SA.HACL_KAYNAK_URL.format(python=SA.PBS_PYTHON)
    hacl = on.dosya(hacl_url, ad="Hacl_Hash_SHA2.c", basla_denetle=False)
    python["LICENSE.hacl-star.txt"] = hacl_lisansi(
        hacl.yol.read_text(encoding="utf-8")).encode("utf-8")
    kaynaklar.append(hacl_url)
    uv = {ad: on.dosya(url, ad=ad, basla_denetle=False).yol.read_bytes()
          for ad, url in SA.UV_LISANS_URLLERI.items()}
    tectonic = {"LICENSE": on.dosya(SA.TECTONIC_LISANS_URL, ad="tectonic-LICENSE",
                                    basla_denetle=False).yol.read_bytes()}
    llama: Dict[str, bytes] = {}
    with tarfile.open(gguf_sdist) as tar:
        uyeler = {u.name.split("/", 1)[1]: u for u in tar.getmembers()
                  if u.isfile() and "/" in u.name}
        for goreli, ad in SA.GGUF_SDIST_LISANSLARI.items():
            if goreli not in uyeler:
                raise SurumHatasi(f"GGUF kaynak paketinde {goreli} yok "
                                  "(surum_ayarlari.GGUF_SDIST_LISANSLARI denetlenmeli)")
            f = tar.extractfile(uyeler[goreli])
            assert f is not None
            llama[ad] = f.read()
    tcl = ", ".join(f"{_TCL_GORUNEN_AD.get(ad, ad)} {surumler[ad]}"
                    for ad in SA.TCL_EK_LISANS_URLLERI)
    return {
        "python": {"dosyalar": python, "kaynak": kaynaklar, "aciklama": (
            f"Python {SA.PBS_PYTHON} (python-build-standalone {SA.PBS_YAYIN_ETIKETI}): CPython "
            "lisansı ve içine alınmış yazılımların bildirimleri (LICENSE.cpython.txt), Python'a "
            "derlenen kitaplıkların lisansları (python-build-standalone'un \"full\" arşivindeki "
            f"lisans klasörü), {tcl} ve HACL* lisansları (kaynak depolarından)")},
        "uv": {"dosyalar": uv, "kaynak": list(SA.UV_LISANS_URLLERI.values()), "aciklama": (
            f"uv {SA.UV_SURUMU} (çekirdek kurulumunda GitHub'dan iner): MIT ya da Apache-2.0")},
        "tectonic": {"dosyalar": tectonic, "kaynak": [SA.TECTONIC_LISANS_URL], "aciklama": (
            f"tectonic {SA.TECTONIC_SURUMU} (Hafif TeX; kurulumda GitHub'dan iner): MIT")},
        "llama-cpp-python": {"dosyalar": llama, "kaynak": [gguf_sdist.name], "aciklama": (
            f"GGUF desteği: llama-cpp-python {gguf_surum} tekerleği (sürüm makinesinde "
            "derlenir, Kitapçevir'in sürüm sayfasından iner); llama-cpp-python, llama.cpp/ggml, "
            "nlohmann/json ve cpp-httplib lisansları (kaynak paketten)")},
    }


def lisanslari_hazirla(hedef: Path, *, on: Any, kaynak: Mapping[str, Any],
                       m: Mapping[str, Any], datalab_lisans: ob.Kayit,
                       tencent_lisans: ob.Kayit, kilit_surumleri: Mapping[str, str],
                       surum: str, manifest_veri: Optional[Mapping[str, Any]],
                       ucuncu_taraf: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """``LICENSES/`` (lisans raporu §8.3 "DMG" satırları; K9 E2 üçüncü taraf dosyaları)."""
    if hedef.exists():
        shutil.rmtree(hedef)
    hedef.mkdir(parents=True)
    kaynak_dizini = Path(kaynak["dizin"])
    shutil.copyfile(kaynak_dizini / "LICENSE", hedef / "Kitapcevir_GPL-3.0.txt")
    for kimlik in SA.SPDX_METINLERI:
        k = on.dosya(SA.SPDX_METIN_URL.format(kimlik=kimlik), ad=f"{kimlik}.txt",
                     basla_denetle=False)   # sabit etiket: içerik değişmez
        shutil.copyfile(k.yol, hedef / f"{kimlik}.txt")
    (hedef / "datalab").mkdir()
    shutil.copyfile(datalab_lisans.yol, hedef / "datalab" / SA.MARKER_LISANS_DOSYASI)
    (hedef / SA.TENCENT_LISANS_KLASORU).mkdir()
    shutil.copyfile(tencent_lisans.yol,
                    hedef / SA.TENCENT_LISANS_KLASORU / SA.TENCENT_LISANS_DOSYASI)
    for grup, bilgi in ucuncu_taraf.items():
        (hedef / grup).mkdir(exist_ok=True)
        for ad, icerik in bilgi["dosyalar"].items():
            (hedef / grup / ad).write_bytes(icerik)
    depo_url = m["ayarlar"].KAYNAK_KODU_URL
    satirlar = [
        "Kitapçevir kaynak kodu",
        "======================",
        "",
        f"Depo: {depo_url}",
        f"Bu sürüm: {surum} (etiket v{surum}, commit {kaynak['commit']})",
        f"Sürüm etiketi: {depo_url}/tree/v{surum}",
    ]
    if kaynak.get("kirli"):
        satirlar.append("UYARI: bu paket commit edilmemiş değişiklikler içeren çalışma ağacından "
                        "üretildi (deneme sürümü); kaynak kodu yukarıdaki commit'le birebir değildir.")
    satirlar += [
        "",
        "Kitapçevir GNU GPL 3.0 ile dağıtılır (Kitapcevir_GPL-3.0.txt).",
        "",
        "Kurulum sırasında indirilen GPL/AGPL bileşenlerin kaynak kodu:",
    ]
    for paket, sablon in SA.AGPL_GPL_KAYNAKLARI.items():
        s = kilit_surumleri.get(paket)
        if s:
            satirlar.append(f"- {paket} {s}: {sablon.format(surum=s)}")
    satirlar += ["", "Bu dosya sürüm betiğince üretildi (packaging/surum/surum_cikar.py).", ""]
    (hedef / "KAYNAK_KODU.txt").write_text("\n".join(satirlar), encoding="utf-8")
    if manifest_veri is not None:
        (hedef / "00_OKU.txt").write_text(oku_beni(manifest_veri, surum, ucuncu_taraf),
                                          encoding="utf-8")
    return {"dosyalar": sorted(str(p.relative_to(hedef)) for p in hedef.rglob("*") if p.is_file())}


def oku_beni(veri: Mapping[str, Any], surum: str,
             ucuncu_taraf: Mapping[str, Mapping[str, Any]]) -> str:
    """``00_OKU.txt``: DMG'deki ve kurulumda indirilen bileşenler ile bu klasördeki dosyalar.

    Manifestten ve ``ucuncu_taraf_lisanslari``'ndan üretilir; yalnız klasörde gerçekten
    bulunan dosyaları anar (K9 E2: iddia ile dosyalar aynı olmalı).
    """
    def _grup(ad: str) -> List[str]:
        bilgi = ucuncu_taraf[ad]
        return [f"- {bilgi['aciklama']}:",
                *[f"    {ad}/{dosya}" for dosya in sorted(bilgi["dosyalar"])]]

    s = [f"Kitapçevir {surum}: lisanslar ve dağıtım", "=" * 40, "",
         "Bu klasör (LICENSES/) uygulamanın içinde gelir; kurulumdan sonra uygulama kodunun",
         "yanında durur ve Bileşenler ekranındaki \"Lisanslar\" bağlantısı burayı açar.", "",
         "DMG'de gelenler:",
         "- Kitapçevir uygulama kodu (GPL-3.0-only): Kitapcevir_GPL-3.0.txt; kaynak kodu ve "
         "GPL/AGPL bileşenlerin kaynak bağlantıları: KAYNAK_KODU.txt"]
    s += _grup("python") if "python" in ucuncu_taraf else []
    s += ["", "Kurulumda yayıncının kendi sunucusundan indirilenler (Kitapçevir barındırmaz):"]
    for b in veri["bilesenler"]:
        lis = b.get("lisans") or {}
        if not lis:
            continue          # hiçbir şey indirmeyen seçenek ("Kurulu olanı kullan")
        s.append(f"- {b['ad']} ({b['kimlik']}): lisans {lis.get('spdx', '—')}"
                 + (f", kaynak {lis['kaynak_kodu_url']}" if lis.get("kaynak_kodu_url") else "")
                 + ("; onay gerekir" if lis.get("onay_gerekli") else ""))
    for grup in ("uv", "tectonic", "llama-cpp-python"):
        if grup in ucuncu_taraf:
            s += _grup(grup)
    s += ["", "Lisans metinleri:",
          f"- Standart metinler (SPDX {SA.SPDX_ETIKETI}): "
          + ", ".join(f"{k}.txt" for k in SA.SPDX_METINLERI),
          f"- Datalab model lisansı (resmî metin): datalab/{SA.MARKER_LISANS_DOSYASI}",
          f"- Tencent HY Community License (resmî metin): {SA.TENCENT_LISANS_KLASORU}/"
          f"{SA.TENCENT_LISANS_DOSYASI}. HY-MT kurulursa lisansın kopyası ve NOTICE dosyası "
          f"modelin yanına ({SA.HYMT_KLASORU}/) ve kurulum kökündeki LICENSES/"
          f"{SA.TENCENT_LISANS_KLASORU}/ klasörüne de yazılır.",
          "- Kurulan Python paketlerinin kendi lisans dosyaları kurulum kökündeki venv'de, her "
          "paketin *.dist-info klasöründedir (venv/lib/python"
          f"{'.'.join(SA.PBS_PYTHON.split('.')[:2])}/site-packages/).", ""]
    return "\n".join(s)


def arsiv_yaz(kaynak_dizini: Path, hedef: Path, mtime: int) -> Dict[str, Any]:
    """Uygulama arşivi: içerik kökte (kabuk böyle bekler), sıralı, sabit zaman ve sahip
    (aynı kaynak → aynı bayt)."""
    adlar = sorted(str(p.relative_to(kaynak_dizini)) for p in kaynak_dizini.rglob("*")
                   if p.is_file())
    # Dışlama arşivde YENİDEN uygulanır: kaynak_hazirla'dan sonra kaynağa yazılan
    # dosyalar (içe aktarmanın __pycache__'i, .DS_Store …) arşive girmesin (K9 E1).
    # Bekçi süzülmüş listeyi denetler: dışlama listesi bozulursa sürüm durur.
    adlar = [a for a in adlar if not dislanmis_mi(a)]
    ihlal = bekci_ihlalleri(adlar)
    if ihlal:
        raise SurumHatasi("uygulama arşivine açık yayına girmeyecek dosya girdi: "
                          + ", ".join(ihlal[:10]))
    hedef.parent.mkdir(parents=True, exist_ok=True)
    with open(hedef, "wb") as ham:
        with gzip.GzipFile(filename="", fileobj=ham, mode="wb", compresslevel=9, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for ad in adlar:
                    yol = kaynak_dizini / ad
                    bilgi = tar.gettarinfo(str(yol), arcname=ad)
                    bilgi.uid = bilgi.gid = 0
                    bilgi.uname = bilgi.gname = ""
                    bilgi.mtime = mtime
                    bilgi.mode = 0o755 if os.access(yol, os.X_OK) else 0o644
                    with open(yol, "rb") as f:
                        tar.addfile(bilgi, f)
    return {"dosya_sayisi": len(adlar), "bayt": hedef.stat().st_size,
            "sha256": sha256_dosya(hedef), "acik_bayt": sum((kaynak_dizini / a).stat().st_size
                                                            for a in adlar)}


def olcum_kosusu(*, is_dizini: Path, m: Mapping[str, Any], pbs: ob.Kayit, surum: str,
                 arsiv: Path, manifest_yolu: Path, esleme: Mapping[str, str]) -> Dict[str, Any]:
    """Sahte köke temiz kurulum: bileşen başına du farkı (TASARIM C.1-3, G.1-7)."""
    if is_dizini.exists():
        shutil.rmtree(is_dizini)
    ev = is_dizini / "ev"
    kok = ev / "Library" / "Application Support" / "Kitapçevir"
    kok.mkdir(parents=True)
    (is_dizini / "tmp").mkdir()
    duzen = m["durum"].KokDuzeni(kok)
    gunluk("ölçüm: çalışma zamanı köke açılıyor")
    t0 = time.monotonic()
    python = m["arsiv"].calisma_zamani_hazirla(duzen, pbs.yol, pbs.sha256)
    etiket = m["durum"].calisma_zamani_etiketi({"url": SA.PBS_ARSIV_ADI})
    cz_bayt = m["disk"].disk_kullanimi_bayt(duzen.calisma_zamani(etiket))
    cz_sure = time.monotonic() - t0
    uyg = kok / "app" / surum
    uyg.mkdir(parents=True)
    calistir(["/usr/bin/tar", "-xzf", arsiv, "-C", uyg])
    (kok / "app" / "guncel").symlink_to(surum)
    uyg_bayt = m["disk"].disk_kullanimi_bayt(uyg)
    esleme_yolu = is_dizini / "esleme.json"
    esleme_yolu.write_text(json.dumps(esleme, ensure_ascii=False), encoding="utf-8")
    cikti = is_dizini / "olcum.json"
    ortam = {"HOME": str(ev), "CFFIXED_USER_HOME": str(ev), "PATH": m["ortak"].SISTEM_PATH,
             "TMPDIR": str(is_dizini / "tmp"),
             "KITAPCEVIR_GUNLUK_DIZINI": str(ev / "Library" / "Logs" / "Kitapçevir")}
    argv = [python, "-I", "-X", "utf8", OLCUM_SURUCUSU, "--kok", kok,
            "--uygulama", kok / "app" / "guncel", "--manifest", manifest_yolu,
            "--esleme", esleme_yolu, "--sira", ",".join(SA.OLCUM_SIRASI),
            "--lisans-kabul", "pdf", "--cikti", cikti, "--olaylar", is_dizini / "olaylar.jsonl"]
    gunluk("ölçüm: bileşenler sırayla kuruluyor (" + ", ".join(SA.OLCUM_SIRASI) + ")")
    surec = subprocess.run([str(a) for a in argv], env=ortam, text=True)
    if surec.returncode != 0:
        raise SurumHatasi(f"ölçüm koşusu başarısız (çıkış {surec.returncode}); günlük: "
                          f"{ev / 'Library' / 'Logs' / 'Kitapçevir'}")
    sonuc = json.loads(cikti.read_text(encoding="utf-8"))
    sonuc.update({"calisma_zamani_bayt": cz_bayt, "calisma_zamani_sn": round(cz_sure, 2),
                  "uygulama_bayt": uyg_bayt, "kok": str(kok)})
    return sonuc


# ===========================================================================
# 4. Paket ve DMG
# ===========================================================================
def paket_derle(*, cikti: Path, is_dizini: Path, surum: str, yapi: int, pbs: ob.Kayit,
                arsiv: Path, manifest_yolu: Path, lisanslar: Path) -> Dict[str, Any]:
    paket_dizini = cikti / "paket"
    shutil.rmtree(paket_dizini, ignore_errors=True)
    calisma_zamani = is_dizini / "calisma_zamani" / SA.PBS_ARSIV_ADI
    calisma_zamani.parent.mkdir(parents=True, exist_ok=True)
    ob.klonla(pbs.yol, calisma_zamani)
    t0 = time.monotonic()
    sonuc = calistir(["/bin/bash", DERLE_SH, "--cikti", paket_dizini, "--surum", surum,
                      "--yapi", str(yapi), "--calisma-zamani", calisma_zamani,
                      "--uygulama", arsiv, "--manifest", manifest_yolu, "--lisanslar", lisanslar])
    app = paket_dizini / SA.PAKET_ADI
    dogrula = calistir(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", app])
    imza = calistir(["/usr/bin/codesign", "-dv", "--verbose=2", app])
    politika = subprocess.run(["/usr/bin/syspolicy_check", "distribution", str(app)],
                              capture_output=True, text=True)
    politika_metni = (politika.stdout + politika.stderr).strip()
    if "Codesign Error" in politika_metni:
        raise SurumHatasi("syspolicy_check: Codesign Error\n" + politika_metni[-2000:])
    imza_metni = imza.stderr
    return {"app": str(app), "sure_sn": round(time.monotonic() - t0, 1),
            "derle_cikti": sonuc.stdout.strip().splitlines()[-2:],
            "codesign_verify": (dogrula.stderr or dogrula.stdout).strip().splitlines(),
            "imza": [s for s in imza_metni.splitlines()
                     if s.startswith(("Signature=", "Identifier=", "Format=", "CodeDirectory"))],
            "syspolicy_check_cikis": politika.returncode,
            "syspolicy_check": politika_metni.splitlines()[-12:],
            "paket_bayt": int(calistir(["/usr/bin/du", "-sk", app]).stdout.split()[0]) * 1024,
            "ikili_bayt": (app / "Contents" / "MacOS" / "Kitapcevir").stat().st_size}


# İmzasız dağıtım (Apple geliştirici hesabı yok): ilk açılışın iki yolu. macOS 14'te
# Finder'da sağ tık → Aç yeter; macOS 15 ve sonrasında Gatekeeper'ı yalnız Sistem
# Ayarları'ndaki "Yine de Aç" geçer (K9 O4, E6). Sürüm notu ve DMG metni aynı cümleyi kullanır.
ILK_ACILIS_NOTU = ("İmzasız dağıtılır (ad-hoc mühürlü). İlk açılış: Finder'da uygulamaya sağ tık "
                   "→ Aç; macOS 15 ve sonrasında gerekirse Sistem Ayarları → Gizlilik ve Güvenlik "
                   f"→ \"Yine de Aç\" (ayrıntı DMG'deki \"{SA.DMG_ILK_ACILIS_ADI}\").")


def ilk_acilis_metni(uygulama_adi: str, kaynak_url: str) -> str:
    """DMG'deki ilk açılış yönergesi (``SA.DMG_ILK_ACILIS_ADI``)."""
    return "\n".join([
        f"{uygulama_adi}: ilk açılış",
        "",
        f"{uygulama_adi} imzasız dağıtılır; macOS ilk açılışta uygulamayı engeller. Aşağıdaki "
        "adımlar yalnız bir kez gerekir.",
        "",
        f"1. {SA.PAKET_ADI} simgesini bu penceredeki Applications (Uygulamalar) klasörüne sürükle.",
        f"2. Finder'da Uygulamalar klasörünü aç, {uygulama_adi}'e sağ tık yap (ya da Control "
        "tuşuna basılıyken tıkla) ve \"Aç\"ı seç. Çıkan uyarıda yeniden \"Aç\"a bas.",
        "3. Uyarıda \"Aç\" düğmesi yoksa (macOS 15 ve sonrası): Sistem Ayarları → Gizlilik ve "
        f"Güvenlik bölümünü aç; aşağıda {uygulama_adi} için çıkan satırdaki \"Yine de Aç\"a bas "
        f"ve parolanı gir. Sonra {uygulama_adi}'i yeniden aç.",
        "",
        "Uygulama açılınca kurulum sihirbazı başlar.",
        "",
        f"Kaynak kodu: {kaynak_url}",
        "",
    ])


def dmg_sahnesi_hazirla(sahne: Path, app: Path, ilk_acilis: str) -> None:
    """DMG'nin içeriği: uygulama, /Applications bağı ve ilk açılış yönergesi."""
    shutil.rmtree(sahne, ignore_errors=True)
    sahne.mkdir(parents=True)
    calistir(["/usr/bin/ditto", app, sahne / SA.PAKET_ADI])
    (sahne / "Applications").symlink_to("/Applications")
    (sahne / SA.DMG_ILK_ACILIS_ADI).write_bytes(ilk_acilis.encode("utf-8-sig"))


def dmg_yap(*, cikti: Path, is_dizini: Path, surum: str, app: Path,
            ilk_acilis: str) -> Dict[str, Any]:
    sahne = is_dizini / "dmg_sahne"
    dmg_sahnesi_hazirla(sahne, app, ilk_acilis)
    dmg = cikti / SA.DMG_ADI_SABLONU.format(surum=surum)
    dmg.unlink(missing_ok=True)
    t0 = time.monotonic()
    calistir(["/usr/bin/hdiutil", "create", "-volname", SA.DMG_CILT_ADI, "-srcfolder", sahne,
              "-fs", "HFS+", "-format", SA.DMG_BICIMI, "-ov", dmg])
    sure = time.monotonic() - t0
    calistir(["/usr/bin/hdiutil", "verify", dmg])
    bilgi = calistir(["/usr/bin/hdiutil", "imageinfo", dmg]).stdout
    bicim = re.search(r"^Format: (\S+)", bilgi, re.M)
    shutil.rmtree(sahne, ignore_errors=True)
    return {"dmg": str(dmg), "bayt": dmg.stat().st_size, "sha256": sha256_dosya(dmg),
            "bicim": bicim.group(1) if bicim else "", "sure_sn": round(sure, 1)}


# ===========================================================================
# Ana akış
# ===========================================================================
def _makine_bilgisi() -> Dict[str, str]:
    cpu = calistir(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]).stdout.strip()
    macos = calistir(["/usr/bin/sw_vers", "-productVersion"]).stdout.strip()
    return {"makine": cpu, "macos": macos}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ay = argparse.ArgumentParser(description="Kitapçevir sürüm betiği (TASARIM G.1)")
    ay.add_argument("--surum", required=True, help="X.Y.Z")
    ay.add_argument("--kaynak", default="HEAD",
                    help="git commit/etiket (git archive) ya da 'calisma-agaci'")
    ay.add_argument("--cikti", help="çıktı klasörü (varsayılan output/_surum/<sürüm>)")
    ay.add_argument("--onbellek", default=str(Path.home() / "Library" / "Caches" /
                                              "Kitapcevir-surum"),
                    help="indirme önbelleği (sürümler arası yeniden kullanılır)")
    ay.add_argument("--lisans-kabul", default="",
                    help="ölçüm koşusunda kabul edilen lisanslar (Datalab modelleri: pdf)")
    ay.add_argument("--is-dizini", help="ara dosyalar (temiz kaynak, ölçüm kökü ~5 GB); "
                                        "varsayılan <çıktı>/is")
    ay.add_argument("--olcum", help="önceki ölçüm koşusunun olcum.json'u (girdiler aynıysa "
                                    "yeniden kullanılır; denetlenir)")
    args = ay.parse_args(argv)
    # Sürüm süreci hiçbir yere bytecode yazmaz (temiz kaynak dahil; K9 E1).
    sys.dont_write_bytecode = True
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.surum):
        ay.error("--surum X.Y.Z biçiminde olmalı")
    if "pdf" not in [p.strip() for p in args.lisans_kabul.split(",")] and not args.olcum:
        ay.error("ölçüm koşusu PDF desteğini kurar: Datalab model lisansının açık kabulü gerekir "
                 "(--lisans-kabul pdf)")
    cikti = Path(args.cikti or DEPO_KOKU / "output" / "_surum" / args.surum).resolve()
    cikti.mkdir(parents=True, exist_ok=True)
    is_dizini = Path(args.is_dizini).resolve() if args.is_dizini else cikti / "is"
    is_dizini.mkdir(parents=True, exist_ok=True)
    rapor: Dict[str, Any] = {"surum": args.surum, "baslangic": _dt.datetime.now().isoformat(
        timespec="seconds"), "komut": " ".join([Path(sys.executable).name, *sys.argv])}
    try:
        return _main(args, cikti, is_dizini, rapor)
    except (SurumHatasi, ob.IndirmeHatasi) as exc:
        gunluk(f"HATA: {exc}")
        rapor["hata"] = str(exc)
        (cikti / "surum_raporu.json").write_text(json.dumps(rapor, ensure_ascii=False, indent=1,
                                                            default=str), encoding="utf-8")
        return 1


def _main(args, cikti: Path, is_dizini: Path, rapor: Dict[str, Any]) -> int:
    surum = args.surum
    # -- 1. kaynak ------------------------------------------------------------------
    gunluk(f"kaynak hazırlanıyor ({args.kaynak})")
    kaynak = kaynak_hazirla(args.kaynak, is_dizini / "kaynak")
    gunluk(f"  commit {kaynak['kisa']}{' (çalışma ağacı değişiklikli)' if kaynak['kirli'] else ''}"
           f", {kaynak['alinan']} dosya, {len(kaynak['atlanan'])} dışlandı")
    rapor["kaynak"] = {k: v for k, v in kaynak.items() if k != "dizin"}
    # TASARIM G.1-1: yayın sürümü etiketli ve temiz bir commit'ten çıkar. Deneme
    # (çalışma ağacı ya da etiketsiz commit) durdurulmaz, ama raporda açıkça yazılır.
    uyarilar: List[str] = []
    if args.kaynak == "calisma-agaci":
        uyarilar.append("Çalışma ağacından üretildi (commit edilmemiş değişiklikler dahil): DENEME "
                        "sürümüdür, yayımlanmaz. Yayın: --kaynak v" + surum)
    elif f"v{surum}" not in git("tag", "--points-at", kaynak["commit"]).split():
        uyarilar.append(f"Commit {kaynak['kisa']} üzerinde v{surum} etiketi yok (TASARIM G.1-1).")
    rapor["uyarilar"] = uyarilar
    for u in uyarilar:
        gunluk(f"UYARI: {u}")
    kaynak_dizini: Path = kaynak["dizin"]
    m = yonetici_modulleri(kaynak_dizini)
    marka = branding_oku(kaynak_dizini)
    sabitler = derle_sabitleri()
    depo = m["ayarlar"].DEPO
    # PBS "full" arşivi zstd'li (üçüncü taraf lisans dosyaları; K9 E2): uzun indirmelerden
    # önce denetlenir.
    zstd = shutil.which("zstd")
    if not zstd:
        raise SurumHatasi("zstd bulunamadı: gömülü Python'un üçüncü taraf lisans dosyaları PBS "
                          "'full' arşivinden (zstd'li) çıkarılır. Kurulum: brew install zstd")
    on = ob.Onbellek(args.onbellek, SA.KULLANICI_ARACISI.format(surum=surum), SA.AG_ZAMAN_ASIMI_SN,
                     gunluk=gunluk)
    kilitler = kilitleri_oku(m, kaynak_dizini)
    kilit_surumleri = {_ad(p): p.surum for g in kilitler.values() for p in g}
    yeni, bagimli = grup_paketleri(kilitler)
    rapor["kilitler"] = {g: {"paket": len(kilitler[g]), "yeni": len(yeni[g])} for g in kilitler}

    # -- 2. ölçüm: kaynaklar --------------------------------------------------------
    t_ag = time.monotonic()
    # MacTeX (6,9 GB) arka planda iner; öteki ölçümler beklemez. Daemon iş parçacığı:
    # sürüm hatayla biterse süreç indirmeyi beklemeden kapanır (.part sürdürülür).
    mactex_isi = ArkaPlan(_mactex, on)
    gunluk("python-build-standalone, uv, tectonic")
    pbs_ozetleri = on.oku(SA.PBS_OZET_URL).decode()
    pbs = on.dosya(SA.PBS_URL, ad=SA.PBS_ARSIV_ADI,
                   sha256=ozet_dosyasindan(pbs_ozetleri, SA.PBS_ARSIV_ADI))
    pbs_full = on.dosya(SA.PBS_FULL_URL, ad=SA.PBS_FULL_ARSIV_ADI,
                        sha256=ozet_dosyasindan(pbs_ozetleri, SA.PBS_FULL_ARSIV_ADI))
    uv_ozet = ozet_dosyasindan(on.oku(SA.UV_OZET_URL).decode(), SA.UV_ARSIV_ADI)
    uv = on.dosya(SA.UV_URL, sha256=uv_ozet)
    tec_api = on.json_oku(SA.TECTONIC_API_URL)
    tec_varlik = next((a for a in tec_api.get("assets") or []
                       if a.get("name") == SA.TECTONIC_ARSIV_ADI), {})
    tec_ozet = str(tec_varlik.get("digest") or "").removeprefix("sha256:") or None
    tectonic = on.dosya(SA.TECTONIC_URL, sha256=tec_ozet)
    rapor["kaynak_ozetleri"] = {"pbs": "SHA256SUMS ile eşleşti", "uv": ".sha256 ile eşleşti",
                                "tectonic": ("GitHub digest ile eşleşti" if tec_ozet
                                             else "GitHub digest yok; sürümde ölçüldü")}
    # Tekerlekler önce çözülür (GGUF dışındakiler); GGUF tekerleği derlenir.
    pbs_python = _pbs_python(is_dizini, m, pbs)
    gguf = gguf_tekerlegi_hazirla(on, kilitler, pbs_python, sabitler["MIN_MACOS"], depo, surum,
                                  is_dizini, m["ortak"].SISTEM_PATH)
    rapor["gguf_tekerlegi"] = {"ad": gguf["oge"]["kimlik"], "bayt": gguf["oge"]["bayt"],
                               "sha256": gguf["oge"]["sha256"], "url": gguf["oge"]["url"],
                               "denetim": gguf["denetim"], "sdist_sha256": gguf["sdist_sha256"]}
    gunluk("üçüncü taraf lisans dosyaları (PBS full arşivi, Tcl/Tk, HACL*, uv, tectonic, GGUF)")
    ucuncu_taraf = ucuncu_taraf_lisanslari(on, pbs=pbs, pbs_full=pbs_full,
                                           gguf_sdist=gguf["sdist_yol"],
                                           gguf_surum=gguf["surum"], zstd=zstd)
    rapor["ucuncu_taraf_lisanslari"] = {g: {"dosyalar": sorted(b["dosyalar"]),
                                            "kaynak": b["kaynak"]}
                                        for g, b in ucuncu_taraf.items()}
    gunluk("PyPI: tekerlekler çözülüyor (kilit özetiyle çapraz denetim)")
    tekerlekler = tekerlek_ogeleri(on, yeni, gguf)
    tum_tekerlekler = [o for g in tekerlekler.values() for o in g if o is not gguf["oge"]]
    gunluk(f"  {len(tum_tekerlekler)} tekerlek indiriliyor/doğrulanıyor")
    with ThreadPoolExecutor(max_workers=SA.INDIRME_IS_PARCACIGI) as havuz:
        teker_kayit = dict(zip([o["kimlik"] for o in tum_tekerlekler], havuz.map(
            lambda o: on.dosya(o["url"], ad=o["kimlik"], sha256=o["sha256"], bayt=o["bayt"]),
            tum_tekerlekler)))
    # Surya/Marker modelleri
    surya_ad = next(o for o in tekerlekler["pdf"] if tekerlek.ayristir(o["kimlik"])[0] == "surya-ocr")
    marker_ad = next(o for o in tekerlekler["pdf"] if tekerlek.ayristir(o["kimlik"])[0] == "marker-pdf")
    dl = datalab_kaynaklari(teker_kayit[surya_ad["kimlik"]].yol, teker_kayit[marker_ad["kimlik"]].yol)
    gunluk(f"Surya/Marker modelleri: {len(dl['klasorler'])} klasör (indirilip SHA-256 ölçülür)")
    datalab_ogeleri, datalab_kayit = _datalab(on, dl, m)
    marker_surum = kilit_surumleri["marker-pdf"]
    datalab_lisans = on.dosya(SA.MARKER_LISANS_HAM.format(surum=marker_surum),
                              ad=SA.MARKER_LISANS_DOSYASI, basla_denetle=False)
    # Gözden geçirme kapısı (KARAR 34: ekranda özet yok, resmî metin gösterilir): metin
    # değiştiyse bir insan yeni metni okur, manifestteki lisans adını ve onay ekranının
    # giriş cümlesini denetler, sonra surum_ayarlari'ndaki SHA'yı günceller.
    if datalab_lisans.sha256 != SA.DATALAB_LISANS_SHA256:
        raise SurumHatasi(f"Datalab MODEL_LICENSE değişmiş ({datalab_lisans.sha256} ≠ "
                          f"{SA.DATALAB_LISANS_SHA256}): yeni resmî metni oku, PDF_LISANS_SPDX'i ve "
                          "lisans.py'deki giriş cümlesini denetle, DATALAB_LISANS_SHA256'yı güncelle")
    # HY-MT (HF ağacı)
    gunluk("HY-MT: Hugging Face ağacı (ağırlıklar indirilmez)")
    hymt = _hymt(on, m)
    if hymt["lisans_sha256"] != SA.TENCENT_LISANS_SHA256:
        raise SurumHatasi(f"Tencent lisans metni değişmiş ({hymt['lisans_sha256']} ≠ "
                          f"{SA.TENCENT_LISANS_SHA256}): yeni resmî metni oku, HYMT_LISANS_SPDX'i ve "
                          "lisans.py'deki giriş cümlesini denetle, TENCENT_LISANS_SHA256'yı güncelle")
    # MacTeX
    gunluk("MacTeX: boyut + SHA-256 (önbellekte yoksa 6,9 GB iner) + .sha512 doğrulaması")
    mactex = mactex_isi.bekle()
    rapor["ag"] = {"inen_bayt": on.inen_bayt, "sure_sn": round(time.monotonic() - t_ag, 1)}
    gunluk(f"  ölçüm indirmeleri: {on.inen_bayt / 1e9:.2f} GB ağdan "
           f"({time.monotonic() - t_ag:.0f} sn)")

    # -- 3. manifest (geçici), arşiv, ölçüm koşusu ----------------------------------
    makine = _makine_bilgisi()
    bugun = _dt.date.today().isoformat()
    olcum_bilgisi = {"tarih": bugun, "makine": makine["makine"], "macos": makine["macos"],
                     "yontem": ("sahte köke (sahte HOME) temiz kurulum; yöneticinin Kurulum "
                                "sınıfıyla bileşen başına, sıra " + " → ".join(SA.OLCUM_SIRASI)
                                + "; disk = adım öncesi/sonrası du farkı (çekirdeğe gömülü Python "
                                "ve uygulama kodu dahil)")}
    olcum_notu = (f"Sürümde ölçüldü: {bugun}, {makine['makine']}, macOS {makine['macos']} "
                  "(sahte köke temiz kurulum, du farkı).")
    ortak = dict(surum=surum, kaynak=kaynak, sabitler=sabitler, m=m, olcum_bilgisi=olcum_bilgisi,
                 marka=marka, pbs=pbs, uv=uv, tectonic=tectonic, mactex=mactex,
                 tekerlekler=tekerlekler, datalab={"ogeler": datalab_ogeleri,
                                                   "lisans_sha256": datalab_lisans.sha256},
                 hymt=hymt, olcum_notu=olcum_notu, kilit_surumleri=kilit_surumleri,
                 bagimli=bagimli)
    esleme = {k: str(v.yol) for k, v in teker_kayit.items()}
    esleme.update({k: str(v.yol) for k, v in datalab_kayit.items()})
    esleme[f"uv-{SA.UV_SURUMU}"] = str(uv.yol)
    esleme[f"tectonic-{SA.TECTONIC_SURUMU}"] = str(tectonic.yol)
    esleme[gguf["oge"]["kimlik"]] = str(gguf["yol"])
    girdi_ozeti = hashlib.sha256(json.dumps(
        {"ogeler": sorted((k, Path(v).name) for k, v in esleme.items()),
         "kilitler": {g: [(_ad(p), p.surum) for p in kilitler[g]] for g in kilitler}},
        sort_keys=True).encode()).hexdigest()

    lisanslar = is_dizini / "LICENSES"
    tencent_kayit = hymt["lisans_kaydi"]

    def _uygulama_hazirla(manifest_veri: Mapping[str, Any]) -> None:
        """Temiz kaynağa LICENSES/ ve packaging/manifest.json yazılır (arşivin içeriği)."""
        lisanslari_hazirla(lisanslar, on=on, kaynak=kaynak, m=m,
                           datalab_lisans=datalab_lisans, tencent_lisans=tencent_kayit,
                           kilit_surumleri=kilit_surumleri, surum=surum,
                           manifest_veri=manifest_veri, ucuncu_taraf=ucuncu_taraf)
        hedef_lis = kaynak_dizini / SA.LISANS_KLASORU
        shutil.rmtree(hedef_lis, ignore_errors=True)
        shutil.copytree(lisanslar, hedef_lis)
        (kaynak_dizini / MANIFEST_GORELI).write_text(
            json.dumps(manifest_veri, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    def _uygulama_kur(manifest_veri: Mapping[str, Any], arsiv_yolu: Path) -> Dict[str, Any]:
        _uygulama_hazirla(manifest_veri)
        return arsiv_yaz(kaynak_dizini, arsiv_yolu, kaynak["zaman"])

    if args.olcum:
        olcum = json.loads(Path(args.olcum).read_text(encoding="utf-8"))
        if olcum.get("girdi_ozeti") != girdi_ozeti:
            raise SurumHatasi("--olcum: önceki ölçümün girdileri (öğeler/kilitler) bu sürümle aynı "
                              "değil; ölçüm koşusu yeniden yapılmalı")
        gunluk(f"ölçüm koşusu yeniden kullanılıyor: {args.olcum}")
    else:
        gecici_disk = {b: 1 for b in ("cekirdek", "mlx", "pdf", "tex-hafif", "gguf")}
        gecici = manifest_kur(**ortak, disk=gecici_disk, sayac=None)
        manifest_dogrula(m, gecici, kaynak_dizini)
        gecici_yol = is_dizini / "manifest_gecici.json"
        gecici_yol.write_text(json.dumps(gecici, ensure_ascii=False, indent=1), encoding="utf-8")
        gecici_arsiv = is_dizini / "uygulama_gecici.tar.gz"
        _uygulama_kur(gecici, gecici_arsiv)
        olcum = olcum_kosusu(is_dizini=is_dizini / "olcum", m=m, pbs=pbs, surum=surum,
                             arsiv=gecici_arsiv, manifest_yolu=gecici_yol, esleme=esleme)
        olcum["girdi_ozeti"] = girdi_ozeti
        (cikti / "olcum.json").write_text(json.dumps(olcum, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    rapor["olcum_kosusu"] = olcum

    # -- 4. son manifest -------------------------------------------------------------
    adim = {k: int(v["disk_bayt"]) for k, v in olcum["bilesenler"].items()}
    # Çekirdeğin diskine DMG'den açılan Python ve uygulama kodu da girer (C.3). Uygulama kodunun
    # boyu son arşivden yeniden ölçülür (ölçüm koşusundaki geçici arşivle yalnız manifest farkı).
    disk_son = dict(adim)
    disk_son["cekirdek"] = olcum["calisma_zamani_bayt"] + olcum["uygulama_bayt"] + adim["cekirdek"]
    son = manifest_kur(**ortak, disk=disk_son, sayac=olcum.get("tectonic_dosya"))
    for _ in range(3):   # manifest kendi sayısını taşıyor: blok yuvarlamasıyla birkaç geçişte sabitlenir
        _uygulama_hazirla(son)
        cekirdek = (olcum["calisma_zamani_bayt"] + _uygulama_disk_bayti(m, kaynak_dizini)
                    + adim["cekirdek"])
        if cekirdek == disk_son["cekirdek"]:
            break
        disk_son["cekirdek"] = cekirdek
        son = manifest_kur(**ortak, disk=disk_son, sayac=olcum.get("tectonic_dosya"))
    rapor["dogrulama"] = manifest_dogrula(m, son, kaynak_dizini)
    manifest_yolu = cikti / "manifest.json"
    manifest_yolu.write_text(json.dumps(son, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    gunluk(f"manifest yazıldı ve doğrulandı: {manifest_yolu}")

    # -- 5. uygulama arşivi, paket, DMG ------------------------------------------------
    arsiv = is_dizini / SA.UYGULAMA_ARSIV_SABLONU.format(surum=surum)
    rapor["uygulama_arsivi"] = _uygulama_kur(son, arsiv)
    gunluk(f"uygulama arşivi: {rapor['uygulama_arsivi']['bayt']:,} B, "
           f"{rapor['uygulama_arsivi']['dosya_sayisi']} dosya")
    rapor["paket"] = paket_derle(cikti=cikti, is_dizini=is_dizini, surum=surum, yapi=kaynak["yapi_sayisi"], pbs=pbs,
                                 arsiv=arsiv, manifest_yolu=manifest_yolu, lisanslar=lisanslar)
    gunluk(f"paket: {rapor['paket']['app']} ({rapor['paket']['paket_bayt']:,} B)")
    rapor["dmg"] = dmg_yap(cikti=cikti, is_dizini=is_dizini, surum=surum,
                           app=Path(rapor["paket"]["app"]),
                           ilk_acilis=ilk_acilis_metni(marka["APP_NAME"],
                                                       m["ayarlar"].KAYNAK_KODU_URL))
    gunluk(f"DMG: {rapor['dmg']['dmg']} ({rapor['dmg']['bayt']:,} B, {rapor['dmg']['bicim']})")
    gguf_kopya = cikti / gguf["yol"].name
    ob.klonla(gguf["yol"], gguf_kopya)
    ozetler = [(rapor["dmg"]["sha256"], Path(rapor["dmg"]["dmg"]).name),
               (gguf["oge"]["sha256"], gguf_kopya.name)]
    (cikti / SA.OZET_DOSYASI).write_text("".join(f"{s}  {a}\n" for s, a in ozetler),
                                         encoding="utf-8")
    rapor["boyutlar"] = _boyut_ozeti(m, son)
    rapor["bitis"] = _dt.datetime.now().isoformat(timespec="seconds")
    (cikti / "surum_raporu.json").write_text(json.dumps(rapor, ensure_ascii=False, indent=1,
                                                        default=str), encoding="utf-8")
    notlar = cikti / f"notlar-{surum}.md"
    notlar.write_text(notlar_metni(surum, rapor, m, son), encoding="utf-8")
    gunluk("yayın komutu (Berke çalıştırır; depo ayarlar.DEPO):\n  gh release create "
           f"v{surum} {Path(rapor['dmg']['dmg']).name} {gguf_kopya.name} {SA.OZET_DOSYASI} "
           f"--repo {m['ayarlar'].DEPO} --title \"Kitapçevir {surum}\" --notes-file {notlar}")
    return 0


def notlar_metni(surum: str, rapor: Mapping[str, Any], m: Mapping[str, Any],
                 veri: Mapping[str, Any]) -> str:
    """Sürüm notu taslağı: yalnız bu sürümde ölçülen sayılar (gh release --notes-file)."""
    mf = m["manifest"]
    bicim = m["bicim"].format_size     # uygulamanın biçimi (ui/bicim.py; ondalık virgül, GB = 10⁹)
    zorunlu = [b for b in veri["bilesenler"]
               if b["zorunluluk"] == "zorunlu" or b["kimlik"] == SA.TEX_GRUBU[2]]
    satirlar = [f"# Kitapçevir {surum}", "",
                f"- DMG: {Path(rapor['dmg']['dmg']).name} ({bicim(rapor['dmg']['bayt'])}), "
                f"SHA-256 `{rapor['dmg']['sha256']}`",
                f"- {ILK_ACILIS_NOTU}",
                "- Bileşenler kurulumda yayıncılarının sunucularından iner (Kitapçevir "
                "barındırmaz). Zorunlu kurulum (Hafif TeX ile), bu sürümde ölçülen:"]
    for ana in SA.MACOS_ANA_SURUMLERI:
        ind = sum(mf.bayt_sec(b["boyut"]["indirme_bayt"], str(ana)) for b in zorunlu)
        satirlar.append(f"  - macOS {ana}{'+' if ana == SA.MACOS_ANA_SURUMLERI[-1] else ''}: "
                        f"indirme {bicim(ind)}")
    disk = sum(b["boyut"]["disk_bayt"]["hepsi"] for b in zorunlu)
    satirlar += [f"  - diskte: {bicim(disk)} (ölçüm: {veri['olcum']['tarih']}, "
                 f"{veri['olcum']['makine']}, macOS {veri['olcum']['macos']})",
                 "- \"Yerel model ile çevir\" beta; HY-MT 1.5 7B isteğe bağlı bileşendir.",
                 f"- GGUF tekerleği: {rapor['gguf_tekerlegi']['ad']} "
                 f"(SHA-256 `{rapor['gguf_tekerlegi']['sha256']}`)", ""]
    return "\n".join(satirlar)


def _pbs_python(is_dizini: Path, m: Mapping[str, Any], pbs: ob.Kayit) -> Path:
    """Sürüm makinesinde kullanılan taşınabilir Python (GGUF derlemesi ve denetimi)."""
    kok = is_dizini / "pbs"
    duzen = m["durum"].KokDuzeni(kok)
    return Path(m["arsiv"].calisma_zamani_hazirla(duzen, pbs.yol, pbs.sha256))


def _datalab(on: ob.Onbellek, dl: Mapping[str, Any], m: Mapping[str, Any]
             ) -> Tuple[List[Dict[str, Any]], Dict[str, ob.Kayit]]:
    kok = m["durum"].MODELLER_DATALAB
    isler: List[Tuple[str, str, str, List[str]]] = []
    for klasor in dl["klasorler"]:
        man = on.dosya(f"{dl['taban']}/{klasor}/manifest.json", ad="manifest.json")
        dosyalar = json.loads(man.yol.read_text(encoding="utf-8"))["files"]
        yk = [f"{SA.DATALAB_YENIDEN_KULLAN_KOKU}/{klasor}"]
        isler.append((klasor, "manifest.json", f"{dl['taban']}/{klasor}/manifest.json", yk))
        for d in dosyalar:
            isler.append((klasor, d, f"{dl['taban']}/{klasor}/{d}", yk))
    with ThreadPoolExecutor(max_workers=SA.INDIRME_IS_PARCACIGI) as havuz:
        kayitlar = list(havuz.map(lambda i: on.dosya(i[2], ad=i[1].lstrip(".") or i[1]), isler))
    ogeler: List[Dict[str, Any]] = []
    kayit_esleme: Dict[str, ob.Kayit] = {}
    for (klasor, dosya, url, yk), kayit in zip(isler, kayitlar):
        kimlik = guvenli_kimlik("datalab", klasor.replace("/", "-"), dosya)
        ogeler.append({"kimlik": kimlik, "url": url, "bayt": kayit.bayt, "sha256": kayit.sha256,
                       "hedef": f"{kok}/{klasor}/{dosya}", "tur": "dosya", "yeniden_kullan": yk})
        kayit_esleme[kimlik] = kayit
    yazitipi = on.dosya(dl["yazitipi_url"], ad=dl["yazitipi_adi"])
    font_goreli = m["durum"].FONT_GORELI
    if Path(font_goreli).name != dl["yazitipi_adi"]:
        raise SurumHatasi(f"marker yazıtipi adı ({dl['yazitipi_adi']}) durum.FONT_GORELI ile "
                          f"uyuşmuyor ({font_goreli})")
    ogeler.append({"kimlik": guvenli_kimlik("marker-yazitipi", dl["yazitipi_adi"]),
                   "url": dl["yazitipi_url"], "bayt": yazitipi.bayt, "sha256": yazitipi.sha256,
                   "hedef": font_goreli, "tur": "dosya"})
    kayit_esleme[ogeler[-1]["kimlik"]] = yazitipi
    return ogeler, kayit_esleme


def _hymt(on: ob.Onbellek, m: Mapping[str, Any]) -> Dict[str, Any]:
    agac = on.json_oku(f"{SA.HF_KOKU}/api/models/{SA.HYMT_DEPO}/tree/{SA.HYMT_COMMIT}"
                       "?recursive=true")
    ogeler: List[Dict[str, Any]] = []
    config: Dict[str, Any] = {}
    agirlik = 0
    for d in sorted((d for d in agac if d.get("type") == "file"), key=lambda d: d["path"]):
        yol = d["path"]
        url = f"{SA.HF_KOKU}/{SA.HYMT_DEPO}/resolve/{SA.HYMT_COMMIT}/{yol}"
        lfs = d.get("lfs") or {}
        if lfs.get("oid"):
            bayt, sha = int(lfs.get("size") or d["size"]), str(lfs["oid"])
        else:
            k = on.dosya(url, ad=Path(yol).name, basla_denetle=False)
            if k.bayt != int(d["size"]):
                raise SurumHatasi(f"HY-MT {yol}: ağaçtaki boyut {d['size']}, inen {k.bayt}")
            bayt, sha = k.bayt, k.sha256
            if yol == "config.json":
                config = json.loads(k.yol.read_text(encoding="utf-8"))
        if yol.endswith(".safetensors"):
            agirlik += bayt
        ogeler.append({"kimlik": guvenli_kimlik("hymt", yol), "url": url, "bayt": bayt,
                       "sha256": sha, "hedef": f"{SA.HYMT_KLASORU}/{yol}", "tur": "dosya",
                       "yeniden_kullan": list(SA.HYMT_YENIDEN_KULLAN)})
    if not config:
        raise SurumHatasi("HY-MT config.json okunamadı")
    lisans_url = (f"{SA.HF_KOKU}/{SA.TENCENT_LISANS_DEPO}/resolve/{SA.TENCENT_LISANS_COMMIT}/"
                  f"{SA.TENCENT_LISANS_DOSYASI}")
    lisans = on.dosya(lisans_url, ad=SA.TENCENT_LISANS_DOSYASI, basla_denetle=False)
    ogeler.append({"kimlik": "hymt-lisans", "url": lisans_url, "bayt": lisans.bayt,
                   "sha256": lisans.sha256, "hedef": f"indirilen/hymt/{SA.TENCENT_LISANS_DOSYASI}",
                   "tur": "dosya"})
    katman = int(config["num_hidden_layers"])
    kv_basligi = int(config.get("num_key_value_heads") or config["num_attention_heads"])
    head_dim = int(config.get("head_dim") or int(config["hidden_size"]) //
                   int(config["num_attention_heads"]))
    return {"ogeler": ogeler, "config": config, "agirlik_bayt": agirlik,
            "toplam_bayt": sum(o["bayt"] for o in ogeler),
            "kv": {"katman": katman, "kv_basligi": kv_basligi, "head_dim": head_dim,
                   "oge_bayt": SA.HYMT_KV_OGE_BAYT},
            "lisans_sha256": lisans.sha256, "lisans_kaydi": lisans,
            "lisans_gorunen": (f"{SA.HF_KOKU}/{SA.TENCENT_LISANS_DEPO}/blob/"
                               f"{SA.TENCENT_LISANS_COMMIT}/{SA.TENCENT_LISANS_DOSYASI}")}


def mactex_ozetleri(on: ob.Onbellek) -> Dict[str, str]:
    """Yayımlanan SHA-512'ler: TUG'un resmî sayfası ve CTAN yansısının .sha512 dosyası."""
    ozet: Dict[str, str] = {}
    try:
        sayfa = on.oku(SA.MACTEX_TUG_SAYFASI).decode("utf-8", "replace")
        m = re.search(r"Sha512 sum is\s*<b>\s*([0-9a-fA-F]{128})", sayfa)
        if m:
            ozet["tug"] = m.group(1).lower()
    except ob.IndirmeHatasi:
        pass
    try:
        metin = on.oku(SA.MACTEX_SHA512_URL).decode("utf-8", "replace").split()
        if metin and re.fullmatch(r"[0-9a-fA-F]{128}", metin[0]):
            ozet["yansi"] = metin[0].lower()
    except ob.IndirmeHatasi:
        pass
    return ozet


def _mactex(on: ob.Onbellek) -> Dict[str, Any]:
    """MacTeX.pkg: boyut + SHA-256 (manifest bunu sabitler) + SHA-512 doğrulaması.

    Doğrulama önce TUG'un sayfasıyla yapılır (yansıdaki .sha512 24 Mart 2026'dan beri
    bayat; surum_ayarlari.MACTEX_TUG_SAYFASI). Hiçbir yayımlanmış özet tutmazsa durur.
    """
    yayimlanan = mactex_ozetleri(on)
    if not yayimlanan:
        raise SurumHatasi("MacTeX'in yayımlanmış SHA-512'si okunamadı (TUG sayfası ve yansı)")
    k = on.dosya(SA.MACTEX_URL, ad="MacTeX.pkg", sha512=True, basla_denetle=False)
    if k.sha512 not in yayimlanan.values():
        # Önbellekteki kopya eski yıla ait olabilir: bir kez yeniden indir.
        shutil.rmtree(k.yol.parent, ignore_errors=True)
        k = on.dosya(SA.MACTEX_URL, ad="MacTeX.pkg", sha512=True, basla_denetle=False)
        if k.sha512 not in yayimlanan.values():
            raise SurumHatasi("MacTeX.pkg SHA-512 hiçbir yayımlanmış özetle uyuşmuyor: "
                              + json.dumps(yayimlanan))
    kaynak = [ad for ad, deger in yayimlanan.items() if deger == k.sha512]
    return {"bayt": k.bayt, "sha256": k.sha256, "sha512": k.sha512,
            "son_degisiklik": k.son_degisiklik, "dogrulayan": kaynak,
            "yansi_sha512_guncel": yayimlanan.get("yansi") == k.sha512}


def _uygulama_disk_bayti(m: Mapping[str, Any], kaynak_dizini: Path) -> int:
    """Kaynak klasörü (arşivin açılmış hâli) diskte ne kadar yer tutar (du ile aynı)."""
    return int(m["disk"].disk_kullanimi_bayt(kaynak_dizini))


def _boyut_ozeti(m: Mapping[str, Any], veri: Mapping[str, Any]) -> Dict[str, Any]:
    mf = m["manifest"]
    ozet = {}
    for b in veri["bilesenler"]:
        ozet[b["kimlik"]] = {
            "indirme_bayt": {str(a): mf.bayt_sec(b["boyut"]["indirme_bayt"], str(a))
                             for a in SA.MACOS_ANA_SURUMLERI},
            "disk_bayt": b["boyut"]["disk_bayt"]["hepsi"], "oge_sayisi": len(b["ogeler"])}
    return ozet


if __name__ == "__main__":
    sys.exit(main())
