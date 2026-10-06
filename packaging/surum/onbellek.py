"""Sürüm betiğinin indirme önbelleği: gerçek bayt + akış içinde SHA-256 (yalnız stdlib).

Kurallar (TASARIM G.1-6, lisans raporu K2/K3):

- Her dosya bir kez indirilir; SHA-256 (gerekirse SHA-512) akış içinde ölçülür,
  ikinci okuma geçişi olmaz. Sonuç ``<kök>/dosyalar/<anahtar>/`` altında durur,
  yanında ``meta.json`` (adres, bayt, özetler, ETag, Last-Modified, tarih).
- Beklenen SHA-256 biliniyorsa (PyPI, HF LFS, GitHub özet dosyası) indirilen
  dosya onunla karşılaştırılır; uyuşmazsa sürüm durur. Önbellekteki dosya ancak
  boyutu ve özeti beklenenle aynıysa yeniden kullanılır.
- Beklenen özet bilinmiyorsa (datalab modelleri, lisans metinleri) önbellekteki
  kopya ancak sunucunun HEAD yanıtı (boyut, ETag, Last-Modified) kayıttakiyle
  aynıysa kullanılır; değilse yeniden indirilir. "En yeni sürüm" sorgusu yoktur:
  adresler sabittir, yalnız içeriğin değişip değişmediği denetlenir.
- Yarım kalan büyük indirme ``.part`` + ``Range`` ile sürer (MacTeX 6,9 GB).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

# Parça boyu: akış okuması ve özet güncellemesi bu kadar baytla yapılır.
PARCA_BAYT = 1 << 20
# Ağ hatasında yeniden deneme ve üstel bekleme tabanı (sürüm makinesi; yöneticiden bağımsız).
AG_DENEME_SAYISI = 4
BEKLEME_TABANI_SN = 2.0
META_ADI = "meta.json"
KILIT_ADI = ".kilit"
PART_UZANTISI = ".part"


class IndirmeHatasi(RuntimeError):
    """Sürümü durduran indirme ya da doğrulama hatası (Türkçe ileti)."""


@dataclass(frozen=True)
class Kayit:
    """Önbellekteki dosya: yol + ölçülen bayt ve özetler."""

    url: str
    yol: Path
    bayt: int
    sha256: str
    sha512: str = ""
    etag: str = ""
    son_degisiklik: str = ""


def _anahtar(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def dosya_ozetleri(yol: Path, sha512: bool = False) -> Dict[str, Any]:
    """Diskteki dosyanın baytı ve özetleri (yeniden kullanım denetimi için)."""
    h256 = hashlib.sha256()
    h512 = hashlib.sha512() if sha512 else None
    bayt = 0
    with open(yol, "rb") as f:
        while True:
            parca = f.read(PARCA_BAYT)
            if not parca:
                break
            bayt += len(parca)
            h256.update(parca)
            if h512 is not None:
                h512.update(parca)
    return {"bayt": bayt, "sha256": h256.hexdigest(),
            "sha512": h512.hexdigest() if h512 is not None else ""}


class Onbellek:
    """İndirme önbelleği. İş parçacığı güvenlidir (adres başına kilit)."""

    def __init__(self, kok: os.PathLike | str, kullanici_aracisi: str, zaman_asimi: float,
                 gunluk=print):
        self.kok = Path(kok)
        self.kok.mkdir(parents=True, exist_ok=True)
        self.kullanici_aracisi = kullanici_aracisi
        self.zaman_asimi = zaman_asimi
        self.gunluk = gunluk
        self._kilitler: Dict[str, threading.Lock] = {}
        self._genel_kilit = threading.Lock()
        # Ölçüm: bu koşuda ağdan gerçekten inen bayt (önbellekten gelenler sayılmaz).
        self.inen_bayt = 0

    # -- yardımcılar ---------------------------------------------------------------
    def _istek(self, url: str, yontem: str = "GET",
               basliklar: Optional[Mapping[str, str]] = None) -> urllib.request.Request:
        b = {"User-Agent": self.kullanici_aracisi, "Accept-Encoding": "identity"}
        b.update(basliklar or {})
        return urllib.request.Request(url, method=yontem, headers=b)

    def _kilit(self, url: str) -> threading.Lock:
        with self._genel_kilit:
            return self._kilitler.setdefault(url, threading.Lock())

    def _dene(self, is_, aciklama: str):
        son: Optional[BaseException] = None
        for deneme in range(1, AG_DENEME_SAYISI + 1):
            try:
                return is_()
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 401, 403, 404, 410):
                    raise IndirmeHatasi(f"{aciklama}: HTTP {exc.code}") from exc
                son = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                son = exc
            bekle = BEKLEME_TABANI_SN * (2 ** (deneme - 1))
            self.gunluk(f"  yeniden denenecek ({deneme}/{AG_DENEME_SAYISI}, {bekle:.0f} sn): "
                        f"{aciklama}: {son}")
            time.sleep(bekle)
        raise IndirmeHatasi(f"{aciklama}: {son}")

    # -- küçük istekler --------------------------------------------------------------
    def bas(self, url: str) -> Dict[str, Any]:
        """HEAD (yönlendirmeler izlenir): son adres, boyut, ETag, Last-Modified."""
        def _is():
            with urllib.request.urlopen(self._istek(url, "HEAD"), timeout=self.zaman_asimi) as y:
                return {"url": y.geturl(), "durum": y.status,
                        "bayt": int(y.headers.get("Content-Length") or -1),
                        "etag": (y.headers.get("ETag") or "").strip(),
                        "son_degisiklik": (y.headers.get("Last-Modified") or "").strip(),
                        "x_linked_size": y.headers.get("X-Linked-Size") or "",
                        "x_linked_etag": (y.headers.get("X-Linked-ETag") or "").strip('"')}
        return self._dene(_is, f"HEAD {url}")

    def oku(self, url: str, en_cok: int = 50_000_000) -> bytes:
        """Küçük yanıtı (JSON, özet dosyası) belleğe okur; önbelleğe yazmaz."""
        def _is():
            with urllib.request.urlopen(self._istek(url), timeout=self.zaman_asimi) as y:
                veri = y.read(en_cok + 1)
            if len(veri) > en_cok:
                raise IndirmeHatasi(f"{url}: yanıt {en_cok} bayttan büyük")
            return veri
        return self._dene(_is, f"GET {url}")

    def json_oku(self, url: str) -> Any:
        return json.loads(self.oku(url).decode("utf-8"))

    # -- dosyalar ---------------------------------------------------------------------
    def _meta_oku(self, klasor: Path) -> Dict[str, Any]:
        try:
            return json.loads((klasor / META_ADI).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def dosya(self, url: str, *, ad: Optional[str] = None, sha256: Optional[str] = None,
              bayt: Optional[int] = None, sha512: bool = False,
              basla_denetle: bool = True) -> Kayit:
        """Dosyayı önbelleğe indirir (ya da önbellekten verir) ve ölçer.

        ``sha256``/``bayt`` verilirse sonuç onlarla karşılaştırılır; uyuşmazlık
        ``IndirmeHatasi``. ``sha512=True`` SHA-512'yi de ölçer (MacTeX).
        ``basla_denetle``: özet bilinmiyorsa önbellek kopyası HEAD ile doğrulanır.
        """
        ad = ad or url.rstrip("/").rsplit("/", 1)[-1] or "dosya"
        klasor = self.kok / "dosyalar" / _anahtar(url)
        hedef = klasor / ad
        with self._kilit(url):
            klasor.mkdir(parents=True, exist_ok=True)
            # Süreçler arası kilit: aynı önbelleği kullanan iki sürüm koşusu aynı .part'a
            # yazmasın (ikincisi bekler).
            with open(klasor / KILIT_ADI, "w") as kilit_dosyasi:
                fcntl.flock(kilit_dosyasi, fcntl.LOCK_EX)
                meta = self._meta_oku(klasor)
                if hedef.is_file() and meta.get("url") == url:
                    kayit = self._yeniden_kullan(url, hedef, meta, sha256=sha256, bayt=bayt,
                                                 sha512=sha512, basla_denetle=basla_denetle)
                    if kayit is not None:
                        return kayit
                return self._indir(url, hedef, klasor, sha256=sha256, bayt=bayt, sha512=sha512)

    def _yeniden_kullan(self, url: str, hedef: Path, meta: Mapping[str, Any], *,
                        sha256: Optional[str], bayt: Optional[int], sha512: bool,
                        basla_denetle: bool) -> Optional[Kayit]:
        st = hedef.stat()
        if st.st_size != meta.get("bayt") or (bayt is not None and st.st_size != bayt):
            return None
        if sha512 and not meta.get("sha512"):
            return None
        # Kayıt aynı dosyaya mı ait (boyut + değişiklik zamanı)? Değilse yeniden ölç.
        if meta.get("mtime_ns") != st.st_mtime_ns:
            olcum = dosya_ozetleri(hedef, sha512=sha512)
            if olcum["sha256"] != meta.get("sha256"):
                return None
        if sha256 is not None:
            if meta.get("sha256") != sha256:
                return None
        elif basla_denetle:
            bas = self.bas(url)
            if bas["bayt"] != st.st_size:
                return None
            for alan in ("etag", "son_degisiklik"):
                if meta.get(alan) and bas.get(alan) and meta[alan] != bas[alan]:
                    return None
        return Kayit(url=url, yol=hedef, bayt=st.st_size, sha256=str(meta["sha256"]),
                     sha512=str(meta.get("sha512") or ""), etag=str(meta.get("etag") or ""),
                     son_degisiklik=str(meta.get("son_degisiklik") or ""))

    def _indir(self, url: str, hedef: Path, klasor: Path, *, sha256: Optional[str],
               bayt: Optional[int], sha512: bool) -> Kayit:
        part = hedef.with_name(hedef.name + PART_UZANTISI)
        h256 = hashlib.sha256()
        h512 = hashlib.sha512() if sha512 else None
        mevcut = part.stat().st_size if part.is_file() else 0
        if mevcut:
            # Sürdürme: var olan kısım okunarak özet durumu kurulur (TASARIM D.2-3).
            with open(part, "rb") as f:
                while True:
                    parca = f.read(PARCA_BAYT)
                    if not parca:
                        break
                    h256.update(parca)
                    if h512 is not None:
                        h512.update(parca)
        bilgi: Dict[str, str] = {}

        def _is():
            nonlocal mevcut, h256, h512
            basliklar = {"Range": f"bytes={mevcut}-"} if mevcut else {}
            with urllib.request.urlopen(self._istek(url, basliklar=basliklar),
                                        timeout=self.zaman_asimi) as y:
                if mevcut and y.status != 206:
                    # Sunucu Range'i yok saydı: baştan.
                    mevcut = 0
                    h256 = hashlib.sha256()
                    h512 = hashlib.sha512() if sha512 else None
                    kip = "wb"
                else:
                    kip = "ab" if mevcut else "wb"
                bilgi["etag"] = (y.headers.get("ETag") or "").strip()
                bilgi["son_degisiklik"] = (y.headers.get("Last-Modified") or "").strip()
                with open(part, kip) as f:
                    while True:
                        parca = y.read(PARCA_BAYT)
                        if not parca:
                            break
                        f.write(parca)
                        mevcut += len(parca)
                        with self._genel_kilit:
                            self.inen_bayt += len(parca)
                        h256.update(parca)
                        if h512 is not None:
                            h512.update(parca)
            return True

        self._dene(_is, f"GET {url}")
        olculen_bayt = part.stat().st_size
        ozet = h256.hexdigest()
        if bayt is not None and olculen_bayt != bayt:
            part.unlink(missing_ok=True)
            raise IndirmeHatasi(f"{url}: boyut uyuşmuyor (beklenen {bayt}, inen {olculen_bayt})")
        if sha256 is not None and ozet != sha256:
            part.unlink(missing_ok=True)
            raise IndirmeHatasi(f"{url}: SHA-256 uyuşmuyor (beklenen {sha256}, inen {ozet})")
        os.replace(part, hedef)
        st = hedef.stat()
        meta = {"url": url, "ad": hedef.name, "bayt": olculen_bayt, "sha256": ozet,
                "sha512": h512.hexdigest() if h512 is not None else "",
                "etag": bilgi.get("etag", ""), "son_degisiklik": bilgi.get("son_degisiklik", ""),
                "mtime_ns": st.st_mtime_ns,
                "tarih": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        gecici = klasor / (META_ADI + ".yeni")
        gecici.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(gecici, klasor / META_ADI)
        return Kayit(url=url, yol=hedef, bayt=olculen_bayt, sha256=ozet,
                     sha512=meta["sha512"], etag=meta["etag"],
                     son_degisiklik=meta["son_degisiklik"])


def klonla(kaynak: Path, hedef: Path) -> None:
    """APFS klonu (``cp -c``); olmazsa düz kopya. Hedefin klasörü açılır."""
    hedef.parent.mkdir(parents=True, exist_ok=True)
    if hedef.exists():
        hedef.unlink()
    try:
        import subprocess
        sonuc = subprocess.run(["/bin/cp", "-c", str(kaynak), str(hedef)],
                               capture_output=True, text=True)
        if sonuc.returncode == 0:
            return
    except OSError:
        pass
    shutil.copy2(kaynak, hedef)
