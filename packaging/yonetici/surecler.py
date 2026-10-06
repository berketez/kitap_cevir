"""Süreç yönetimi: Streamlit, ortamı, süren iş sayımı, yetim süreçler, dış açıcı.

Dayanak: TASARIM B.3 (süreç düzeni), B.6 (tek örnek, günlük, kapanış),
B.8 (Streamlit'e verilen ortam: TEK KAYNAK bu modüldür). Yalnız stdlib.

- Streamlit ayrı bir oturumda (``start_new_session``) başlar: grubu
  bütün olarak durdurulabilir ve grup kimliği ``durum/calisan.json``'a
  yazılır (kabuk yönetici ölürse oradan okur, B.6-3).
- Kapanış: Streamlit'e SIGTERM (işleyicisi ``atexit`` → ``ui.runner``
  işlerini kendisi iptal eder), bekleme, gerekirse grubuna ve torun iş
  gruplarına SIGKILL.
- Süren iş sayısı: Streamlit'in torunlarından komut satırında
  ``1_cikar.py`` / ``2_derle.py`` / ``translate_book.py`` geçenler
  (``ps -o pid,ppid,pgid,command``; B.5 ``/api/is_durumu``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

try:
    from . import ayarlar, durum
except ImportError:  # düz betik olarak yüklendi (sys.path[0] = packaging/yonetici)
    import ayarlar  # type: ignore[no-redef]
    import durum  # type: ignore[no-redef]

logger = logging.getLogger("kitapcevir.yonetici.surecler")
streamlit_gunlugu = logging.getLogger("kitapcevir.streamlit")

# ---------------------------------------------------------------------------
# Sabitler (yalnız bu modülde)
# ---------------------------------------------------------------------------
# durum/ altındaki süreç dosyalarının adları durum.py'dedir (KokDuzeni:
# calisan_dosyasi, kilit_dosyasi; tek kaynak).
# Finder'dan açılan uygulamanın PATH'i yalnız sistem yollarıdır (prototip K2).
SISTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
# Kullanıcının ortamından Streamlit'e geçmemesi gereken değişkenler.
SILINECEK_ORTAM = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
                   "VIRTUAL_ENV", "__PYVENV_LAUNCHER__", "CONDA_PREFIX",
                   "CONDA_DEFAULT_ENV")
# Uygulamanın uzun iş süreçleri (ui.runner bunları ayrı oturumda başlatır).
IS_BETIKLERI = ("1_cikar.py", "2_derle.py", "translate_book.py")
UYGULAMA_BETIGI = "app.py"
SAGLIK_YOLU = "/_stcore/health"
# Streamlit Host başlığını denetlemiyor; DNS yeniden bağlamayla bir web sayfası
# rastgele kapıdaki arayüze ulaşabilirdi (K9 D8). Yönetici her açılışta tahmin
# edilemez bir taban yolu verir (--server.baseUrlPath): yolu bilmeyen istek 404 alır.
# Yol yalnız kabuğun penceresine giden adreste durur. .streamlit/config.toml'daki
# sabit ayarlar (yalnız yerel adres, XSRF, CORS) bunun yerine geçmez.
TABAN_YOLU_ONEKI = "k-"
TABAN_YOLU_BAYT = 16
# Streamlit'in sağlık denetimini geçmesi için beklenen en uzun süre. Ölçülen
# soğuk açılış 2,2 sn (TASARIM A.1); ilk açılışta .pyc derlemesi uzatabilir.
STREAMLIT_HAZIR_ZAMAN_ASIMI_SN = 90
SAGLIK_YOKLAMA_SN = 0.25
SAGLIK_ISTEK_ZAMAN_ASIMI_SN = 2
# Yönetici Streamlit'i, kabuğun toplam bekleme süresinden bu kadar az bekler:
# kendisi de kapanıp çıkabilsin (ayarlar.KAPANIS_SURESI_SN; B.6-3).
KAPANIS_PAYI_SN = 5
# SIGKILL'den sonra sürecin toplanması için beklenen süre.
OLDURME_BEKLEME_SN = 2
# Torun iş gruplarının calisan.json'a yazılma aralığı.
TORUN_YOKLAMA_SN = 5
ACICI = "/usr/bin/open"
PS = "/bin/ps"


def streamlit_bekleme_sn() -> float:
    """Kapanışta Streamlit'e SIGTERM sonrası tanınan süre."""
    return max(1.0, float(ayarlar.KAPANIS_SURESI_SN - KAPANIS_PAYI_SN))


# ---------------------------------------------------------------------------
# Streamlit ortamı (TASARIM B.8 tablosu; tek kaynak)
# ---------------------------------------------------------------------------
def tex_path_dizini(duzen: "durum.KokDuzeni") -> Optional[str]:
    """Etkin TeX'in PATH'e eklenecek klasörü (``durum/tex.json``); yoksa None."""
    kayit = durum.tex_oku(duzen)
    if not kayit:
        return None
    dizin = kayit.get("path_dizini") or str(Path(kayit["xelatex"]).parent)
    return str(dizin)


def streamlit_ortami(duzen: "durum.KokDuzeni", yonetici_url: str,
                     temel: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Streamlit sürecinin ortamı (TASARIM B.8).

    ``temel`` verilmezse ``os.environ``; kullanıcının Python değişkenleri
    (``PYTHONPATH`` vb.) atılır, tablodaki değerler yazılır.
    """
    ortam = dict(os.environ if temel is None else temel)
    for ad in SILINECEK_ORTAM:
        ortam.pop(ad, None)
    yollar: List[str] = []
    tex = tex_path_dizini(duzen)
    if tex:
        yollar.append(tex)
    yollar.append(str(duzen.venv / "bin"))
    yollar.extend(SISTEM_PATH)
    ortam.update({
        "PATH": os.pathsep.join(yollar),
        "KITAPCEVIR_VERI_KOKU": str(duzen.veri),
        "KITAPCEVIR_YONETICI_URL": yonetici_url,
        "MODEL_CACHE_DIR": str(duzen.modeller_datalab),
        "FONT_PATH": str(duzen.font_yolu),
        "TECTONIC_CACHE_DIR": str(duzen.tex_cache),
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "STREAMLIT_SERVER_HEADLESS": "true",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    })
    return ortam


def bos_kapi() -> int:
    """İşletim sisteminin verdiği boş bir yerel kapı (Streamlit için)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as soket:
        soket.bind(("127.0.0.1", 0))
        return int(soket.getsockname()[1])


# ---------------------------------------------------------------------------
# Süreç tablosu (ps)
# ---------------------------------------------------------------------------
def surec_tablosu(calistir: Callable[..., Any] = subprocess.run) -> List[Dict[str, Any]]:
    """``ps -ax -o pid=,ppid=,pgid=,command=``: ``[{pid, ppid, pgid, komut}]``."""
    try:
        sonuc = calistir([PS, "-ax", "-o", "pid=,ppid=,pgid=,command="],
                         capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ps çalıştırılamadı: %s", exc)
        return []
    satirlar = []
    for satir in (sonuc.stdout or "").splitlines():
        parca = satir.strip().split(None, 3)
        if len(parca) < 3:
            continue
        try:
            pid, ppid, pgid = int(parca[0]), int(parca[1]), int(parca[2])
        except ValueError:
            continue
        satirlar.append({"pid": pid, "ppid": ppid, "pgid": pgid,
                         "komut": parca[3] if len(parca) > 3 else ""})
    return satirlar


def torunlar(tablo: Sequence[Mapping[str, Any]], kok_pid: int) -> List[Dict[str, Any]]:
    """``kok_pid``'in bütün alt süreçleri (çocuk, torun…); kendisi hariç."""
    cocuklar: Dict[int, List[Mapping[str, Any]]] = {}
    for satir in tablo:
        cocuklar.setdefault(int(satir["ppid"]), []).append(satir)
    sonuc: List[Dict[str, Any]] = []
    bekleyen = [int(kok_pid)]
    gorulen = {int(kok_pid)}
    while bekleyen:
        ebeveyn = bekleyen.pop()
        for satir in cocuklar.get(ebeveyn, []):
            pid = int(satir["pid"])
            if pid in gorulen:
                continue
            gorulen.add(pid)
            sonuc.append(dict(satir))
            bekleyen.append(pid)
    return sonuc


# Betik adı bir yol bileşeni olarak geçmeli: öncesinde "/" ya da boşluk, sonrasında
# boşluk ya da satır sonu. Kök yolu boşluk içerir ("Application Support"); ps
# argümanları boşlukla birleştirdiği için komut satırı boşlukla bölünmez.
_IS_BETIGI_DESENI = re.compile(
    r"(?:^|[\s/])(?:" + "|".join(re.escape(b) for b in IS_BETIKLERI) + r")(?=\s|$)")


def is_sureci_mi(komut: str) -> bool:
    """Komut satırı uygulamanın uzun iş betiklerinden birini çalıştırıyor mu?"""
    return bool(_IS_BETIGI_DESENI.search(komut))


def calisan_isler(tablo: Sequence[Mapping[str, Any]], kok_pid: int) -> List[Dict[str, Any]]:
    """``kok_pid``'in torunları içinde iş betiği koşanlar."""
    return [s for s in torunlar(tablo, kok_pid) if is_sureci_mi(str(s.get("komut", "")))]


def surec_yasiyor(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# calisan.json (TASARIM B.6)
# ---------------------------------------------------------------------------
def calisan_dosyasi(duzen: "durum.KokDuzeni") -> Path:
    return duzen.calisan_dosyasi


def calisan_oku(duzen: "durum.KokDuzeni") -> Optional[Dict[str, Any]]:
    veri = durum.json_oku(calisan_dosyasi(duzen), None)
    return veri if isinstance(veri, dict) else None


def calisan_yaz(duzen: "durum.KokDuzeni", kayit: Mapping[str, Any]) -> None:
    """Yalnız bu kullanıcının okuyabileceği kipte (içinde anahtarlı adres var)."""
    metin = json.dumps(dict(kayit), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    durum.atomik_yaz(calisan_dosyasi(duzen), metin.encode("utf-8"), kip=0o600)


def calisan_sil(duzen: "durum.KokDuzeni") -> None:
    try:
        calisan_dosyasi(duzen).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Streamlit'in taban yolu (K9 D8)
# ---------------------------------------------------------------------------
def yeni_taban_yolu() -> str:
    """Her Streamlit açılışı için tahmin edilemez taban yolu (``k-`` + 32 onaltılık)."""
    return TABAN_YOLU_ONEKI + secrets.token_hex(TABAN_YOLU_BAYT)


# ---------------------------------------------------------------------------
# Yetim süreçler (B.6-4: çökme sonrası; kendiliğinden öldürülmez)
# ---------------------------------------------------------------------------
def _komut(pid: int, tablo: Sequence[Mapping[str, Any]]) -> Optional[str]:
    for satir in tablo:
        if int(satir["pid"]) == int(pid):
            return str(satir.get("komut", ""))
    return None


def yetimleri_bul(duzen: "durum.KokDuzeni", onceki: Optional[Mapping[str, Any]],
                  tablo: Optional[Sequence[Mapping[str, Any]]] = None,
                  kendi_pid: Optional[int] = None) -> List[Dict[str, Any]]:
    """Önceki oturumun ``calisan.json``'undaki, hâlâ yaşayan ve komut satırı
    bu köke işaret eden süreçler (grup üyeleri dahil): ``[{pid, komut}]``.

    Komut satırında kök yolu geçmeyen süreç (PID yeniden kullanılmış olabilir)
    listelenmez.
    """
    if not onceki:
        return []
    tablo = surec_tablosu() if tablo is None else tablo
    kendi = os.getpid() if kendi_pid is None else int(kendi_pid)
    kok = str(duzen.kok)
    adaylar: List[int] = []
    if isinstance(onceki.get("pid"), int):
        adaylar.append(onceki["pid"])
    st = onceki.get("streamlit")
    if isinstance(st, Mapping) and isinstance(st.get("pid"), int):
        adaylar.append(st["pid"])
    gruplar = set()
    if isinstance(st, Mapping) and isinstance(st.get("pgid"), int):
        gruplar.add(st["pgid"])
    for grup in onceki.get("is_gruplari") or []:
        if isinstance(grup, int):
            gruplar.add(grup)
    for satir in tablo:
        if int(satir["pgid"]) in gruplar:
            adaylar.append(int(satir["pid"]))
    sonuc: List[Dict[str, Any]] = []
    gorulen = set()
    for pid in adaylar:
        if pid in gorulen or pid == kendi or pid <= 1:
            continue
        gorulen.add(pid)
        komut = _komut(pid, tablo)
        if komut is None or kok not in komut:
            continue
        sonuc.append({"pid": pid, "komut": komut})
    return sonuc


def surecleri_durdur(pidler: Iterable[int], bekleme_sn: float = OLDURME_BEKLEME_SN) -> List[int]:
    """SIGTERM, bekleme, hâlâ yaşayana SIGKILL. Durdurulamayan PID'leri döndürür."""
    pidler = [int(p) for p in pidler]
    for pid in pidler:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.warning("süreç %s durdurulamadı: izin yok", pid)
    son = time.monotonic() + bekleme_sn
    while time.monotonic() < son and any(surec_yasiyor(p) for p in pidler):
        time.sleep(0.05)
    kalan = []
    for pid in pidler:
        if surec_yasiyor(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    time.sleep(0.05)
    for pid in pidler:
        if surec_yasiyor(pid) and not _zombi_mi(pid):
            kalan.append(pid)
    return kalan


def _zombi_mi(pid: int) -> bool:
    """Ölmüş ama ebeveyni toplamamış süreç: ``ps`` durumunda Z."""
    try:
        sonuc = subprocess.run([PS, "-o", "stat=", "-p", str(pid)], capture_output=True,
                               text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return sonuc.stdout.strip().startswith("Z")


# ---------------------------------------------------------------------------
# Dış açıcı (Finder / varsayılan tarayıcı)
# ---------------------------------------------------------------------------
def ac(hedef: str, calistir: Callable[..., Any] = subprocess.run) -> None:
    """``open <hedef>``: klasörü Finder'da, adresi varsayılan tarayıcıda açar.

    Kabuk yoktur (argüman listesi); hedef çağıranın bildiği yerlerden gelir.
    """
    calistir([ACICI, hedef], check=False, timeout=10,
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Streamlit süreci
# ---------------------------------------------------------------------------
class StreamlitHatasi(RuntimeError):
    """Streamlit başlatılamadı ya da sağlık denetimini geçmedi (ileti Türkçe)."""


class StreamlitSureci:
    """Streamlit'in yaşam döngüsü: başlat, sağlık, iş sayımı, durdur.

    ``komut`` testte sahte bir sunucuyla değiştirilir; varsayılan
    ``<venv>/bin/python -m streamlit run app.py …`` (cwd = ``app/guncel``).
    """

    def __init__(self, duzen: "durum.KokDuzeni", uygulama: Path, *,
                 ortam: Optional[Mapping[str, str]] = None,
                 komut: Optional[Sequence[str]] = None,
                 hazir_zaman_asimi_sn: float = STREAMLIT_HAZIR_ZAMAN_ASIMI_SN):
        self.duzen = duzen
        self.uygulama = Path(uygulama)
        self._ortam = dict(ortam) if ortam is not None else None
        self._komut = list(komut) if komut is not None else None
        self._hazir_zaman_asimi = float(hazir_zaman_asimi_sn)
        self._kilit = threading.RLock()
        self._surec: Optional[subprocess.Popen] = None
        self.kapi: Optional[int] = None
        # Bu açılışın taban yolu (K9 D8). Sınamadaki özel komut "{taban}" taşımıyorsa yok.
        self.taban: Optional[str] = None
        self._okuyucu: Optional[threading.Thread] = None
        # Sağlık denetimi geçti mi? Açılış sürerken gelen ikinci ``baslat``
        # (ör. "Bileşenler…" menüsünden yeniden yükleme) hazır olmayan adresi
        # döndürmesin; o da sağlığı bekler.
        self._hazir = threading.Event()

    # -- durum ------------------------------------------------------------------
    @property
    def pid(self) -> Optional[int]:
        return self._surec.pid if self._surec is not None else None

    @property
    def pgid(self) -> Optional[int]:
        # start_new_session: grup kimliği = PID.
        return self.pid

    @property
    def url(self) -> Optional[str]:
        if not self.kapi:
            return None
        return f"http://127.0.0.1:{self.kapi}/{self.taban}/" if self.taban \
            else f"http://127.0.0.1:{self.kapi}/"

    def calisiyor(self) -> bool:
        return self._surec is not None and self._surec.poll() is None

    def hazir(self) -> bool:
        """Çalışıyor ve sağlık denetimini geçti."""
        return self._hazir.is_set() and self.calisiyor()

    def calisan_isler(self, tablo: Optional[Sequence[Mapping[str, Any]]] = None) -> List[Dict[str, Any]]:
        if not self.calisiyor():
            return []
        tablo = surec_tablosu() if tablo is None else tablo
        return calisan_isler(tablo, int(self.pid))

    def torun_gruplari(self, tablo: Optional[Sequence[Mapping[str, Any]]] = None) -> List[int]:
        """Streamlit'in kendi grubundan ayrı gruplarda koşan torunlarının grupları."""
        if not self.calisiyor():
            return []
        tablo = surec_tablosu() if tablo is None else tablo
        return sorted({int(s["pgid"]) for s in torunlar(tablo, int(self.pid))
                       if int(s["pgid"]) != self.pgid})

    def kayit(self) -> Optional[Dict[str, Any]]:
        if not self.calisiyor():
            return None
        return {"pid": self.pid, "pgid": self.pgid, "kapi": self.kapi}

    # -- başlatma -----------------------------------------------------------------
    def _varsayilan_komut(self, kapi: int, taban: str) -> List[str]:
        return [str(self.duzen.venv_python), "-m", "streamlit", "run", UYGULAMA_BETIGI,
                "--server.port", str(kapi), "--server.address", "127.0.0.1",
                "--server.headless", "true", "--server.baseUrlPath", taban]

    def baslat(self, yonetici_url: str) -> str:
        """Çalışmıyorsa başlatır, sağlık denetimini bekler; adresini döndürür.

        Açılış sürerken çağrılırsa yeni süreç başlatmaz, aynı sağlık denetimini
        bekler: adres ancak Streamlit gerçekten yanıt verirken döner.
        """
        with self._kilit:
            if self.calisiyor():
                if self._hazir.is_set():
                    return str(self.url)
            else:
                self._surec_baslat(yonetici_url)
            surec, kapi, taban = self._surec, self.kapi, self.taban
        self.saglik_bekle(surec, kapi, taban)
        return str(self.url)

    def _surec_baslat(self, yonetici_url: str) -> None:
        """Süreci başlatır (``_kilit`` altında çağrılır)."""
        if self._komut is None and not self.duzen.venv_python.exists():
            raise StreamlitHatasi("Python ortamı (venv) kurulu değil; önce kurulumu tamamla.")
        if self._komut is None and not (self.uygulama / UYGULAMA_BETIGI).is_file():
            raise StreamlitHatasi(f"Uygulama kodu bulunamadı: {self.uygulama}")
        kapi = bos_kapi()
        taban: Optional[str] = yeni_taban_yolu()
        if self._komut is not None:
            if not any("{taban}" in k for k in self._komut):
                taban = None
            komut = [k.replace("{kapi}", str(kapi)).replace("{taban}", taban or "")
                     for k in self._komut]
        else:
            komut = self._varsayilan_komut(kapi, str(taban))
        ortam = (dict(self._ortam) if self._ortam is not None
                 else streamlit_ortami(self.duzen, yonetici_url))
        logger.info("Streamlit başlatılıyor (kapı %s)", kapi)
        self._hazir.clear()
        try:
            self._surec = subprocess.Popen(
                komut, cwd=str(self.uygulama), env=ortam, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise StreamlitHatasi(f"Streamlit başlatılamadı: {exc}") from exc
        self.kapi = kapi
        self.taban = taban
        self._okuyucu = threading.Thread(target=self._cikti_oku, args=(self._surec,),
                                         name="streamlit-cikti", daemon=True)
        self._okuyucu.start()

    def _cikti_oku(self, surec: subprocess.Popen) -> None:
        akis = surec.stdout
        if akis is None:
            return
        for ham in iter(akis.readline, b""):
            streamlit_gunlugu.info(ham.decode("utf-8", "replace").rstrip())
        akis.close()

    def saglik_bekle(self, surec: Optional[subprocess.Popen] = None,
                     kapi: Optional[int] = None, taban: Optional[str] = None) -> None:
        """``/<taban>/_stcore/health`` 200 dönene dek bekler; süreç ölürse ya da süre
        dolarsa ``StreamlitHatasi``. Aynı anda birden çok çağıran bekleyebilir."""
        if surec is None:
            surec, taban = self._surec, self.taban
        kapi = self.kapi if kapi is None else kapi
        son = time.monotonic() + self._hazir_zaman_asimi
        adres = f"http://127.0.0.1:{kapi}{'/' + taban if taban else ''}{SAGLIK_YOLU}"
        while time.monotonic() < son:
            if surec is None or surec.poll() is not None or self._surec is not surec:
                kod = surec.returncode if surec is not None else None
                raise StreamlitHatasi(f"Streamlit açılırken kapandı (çıkış kodu {kod}); "
                                      "ayrıntı streamlit.log günlüğünde.")
            if self._hazir.is_set():
                return
            try:
                with urllib.request.urlopen(adres, timeout=SAGLIK_ISTEK_ZAMAN_ASIMI_SN) as yanit:
                    if yanit.status == 200:
                        with self._kilit:
                            if self._surec is surec:
                                self._hazir.set()
                        # Taban yolu bir erişim anahtarıdır: günlüğe yalnız kapı yazılır.
                        logger.info("Streamlit hazır (kapı %s)", kapi)
                        return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(SAGLIK_YOKLAMA_SN)
        with self._kilit:
            if self._surec is surec:
                self.durdur(OLDURME_BEKLEME_SN)
        raise StreamlitHatasi(f"Streamlit {int(self._hazir_zaman_asimi)} sn içinde hazır "
                              "olmadı; ayrıntı streamlit.log günlüğünde.")

    # -- durdurma -----------------------------------------------------------------
    def durdur(self, bekleme_sn: Optional[float] = None) -> Optional[int]:
        """SIGTERM → bekleme → SIGKILL (Streamlit grubu + torun iş grupları).

        Çıkış kodunu döndürür (zaten kapalıysa None).
        """
        with self._kilit:
            surec = self._surec
            self._hazir.clear()
            if surec is None:
                return None
            if surec.poll() is not None:
                self._surec = None
                self.kapi = None
                return surec.returncode
            bekleme = streamlit_bekleme_sn() if bekleme_sn is None else float(bekleme_sn)
            gruplar = self.torun_gruplari()
            logger.info("Streamlit durduruluyor (SIGTERM, en çok %.1f sn)", bekleme)
            try:
                surec.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                kod = surec.wait(timeout=bekleme)
            except subprocess.TimeoutExpired:
                logger.warning("Streamlit SIGTERM'e %.1f sn'de yanıt vermedi; SIGKILL", bekleme)
                for grup in [surec.pid, *gruplar]:
                    try:
                        os.killpg(grup, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    kod = surec.wait(timeout=OLDURME_BEKLEME_SN)
                except subprocess.TimeoutExpired:
                    kod = None
            # Streamlit kapandıysa da ayrı gruptaki işler kalmış olabilir.
            for grup in gruplar:
                try:
                    os.killpg(grup, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            self._surec = None
            self.kapi = None
            return kod


__all__ = [
    "IS_BETIKLERI", "StreamlitHatasi",
    "StreamlitSureci", "ac", "bos_kapi", "calisan_isler", "calisan_oku", "calisan_sil",
    "calisan_yaz", "is_sureci_mi", "streamlit_bekleme_sn", "streamlit_ortami",
    "surec_tablosu", "surec_yasiyor", "surecleri_durdur", "tex_path_dizini", "torunlar",
    "yetimleri_bul",
]
