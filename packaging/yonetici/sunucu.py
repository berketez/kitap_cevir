"""Yöneticinin HTTP sunucusu: sihirbaz sayfası, JSON API ve olay akışı.

Dayanak: TASARIM B.5 (API ve güvenlik), E (ekranlar). Yalnız stdlib: DMG'deki
taşınabilir Python'la çalışır (TASARIM A.2).

Güvenlik (B.5, H.12):
- Yalnız ``127.0.0.1``'e bağlanır; kapı 0 verilirse işletim sistemi seçer.
- Her istekte ``Host`` başlığı ``127.0.0.1:<P>`` ya da ``localhost:<P>``
  olmalıdır (DNS yeniden bağlama saldırısına karşı).
- Her ``/api/*`` isteğinde ``X-Kitapcevir-Anahtar`` başlığı süreç başına
  üretilen anahtara eşit olmalıdır. Özel başlık başka kökenden gelen isteği
  CORS ön denetimine takar; sunucu CORS başlığı hiç göndermez.
- POST gövdesi yalnız ``application/json`` ve en çok ``GOVDE_EN_COK_BAYT``.
- Olay akışı (``/api/olaylar``) da aynı başlığı ister; sayfa akışı
  ``EventSource`` yerine ``fetch`` ile okur (``EventSource`` başlık taşıyamaz).

Bu modül iş mantığı taşımaz: yolları ``Api`` nesnesinin (``yonetici.Yonetici``)
yöntemlerine yönlendirir. Sayı biçimleri (boyut, hız, süre) de buradadır:
arayüzde gösterilen her sayı sunucuda biçimlenir, sayfa sayı hesaplamaz.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import queue
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("kitapcevir.yonetici.sunucu")

# ---------------------------------------------------------------------------
# Sabitler (yalnız bu modülde)
# ---------------------------------------------------------------------------
ANAHTAR_BASLIGI = "X-Kitapcevir-Anahtar"
IZINLI_HOSTLAR = ("127.0.0.1", "localhost")
# Sayfanın gönderdiği JSON gövdeleri küçüktür (seçim listesi); daha büyüğü reddedilir.
GOVDE_EN_COK_BAYT = 64 * 1024
# Olay akışında bu süre olay yoksa yorum satırı gönderilir (bağlantı canlı kalsın).
OLAY_CANLI_TUT_SN = 15
# Abone başına bekleyen olay sınırı: yavaş okuyan abone düşürülür, yeniden
# bağlanınca özetle kaldığı yere gelir.
OLAY_KUYRUK_EN_COK = 2000
WEB_DIZINI = Path(__file__).resolve().parent / "web"
ANA_SAYFA = "index.html"
TEMA_YOLU = "/tema.css"
STATIK_TURLER = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
GUVENLIK_BASLIKLARI = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
     "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
     "form-action 'none'"),
)


# ---------------------------------------------------------------------------
# Sayı biçimleri (Türkçe ondalık virgül)
#
# GEÇİCİ KOPYA (TASARIM F12): ``ui.runner.format_decimal`` / ``SIZE_UNITS`` /
# ``format_size`` ile birebir aynı kural. Yönetici ``ui.runner``'ı içe
# aktaramaz (``translate_book``'u çekiyor). ``test_sunucu.py`` bu kopyanın
# ``ui/runner.py``'deki işlevle aynı sonucu verdiğini denetler; F12 ile
# işlevler hafif bir modüle (``ui/bicim.py``) taşınınca bu kopya silinir.
# ---------------------------------------------------------------------------
BOYUT_BIRIMLERI = (("GB", 10 ** 9), ("MB", 10 ** 6), ("KB", 10 ** 3))


def bicim_ondalik(deger: float, basamak: int = 1) -> str:
    """``ui.runner.format_decimal`` kopyası: "32,7"; tam sayıysa ondalıksız."""
    yuvarlak = round(float(deger), basamak)
    if yuvarlak != yuvarlak or yuvarlak in (float("inf"), float("-inf")):
        return str(deger)
    if yuvarlak == int(yuvarlak):
        return str(int(yuvarlak))
    return f"{yuvarlak:.{basamak}f}".replace(".", ",")


def bicim_boyut(bayt: Optional[float], basamak: int = 1) -> str:
    """``ui.runner.format_size`` kopyası: "72,9 KB", "3,66 GB" değil "3,7 GB".

    ``None`` (ölçülmedi) için "—" döner: olmayan bilgi uydurulmaz.
    """
    if bayt is None:
        return "—"
    deger = float(bayt)
    for sira, (birim, boyut) in enumerate(BOYUT_BIRIMLERI):
        olcekli = deger / boyut
        if round(olcekli, basamak) < 1:
            continue
        if round(olcekli, basamak) >= 1000 and sira > 0:
            ust, ust_boyut = BOYUT_BIRIMLERI[sira - 1]
            return f"{bicim_ondalik(deger / ust_boyut, basamak)} {ust}"
        return f"{bicim_ondalik(olcekli, basamak)} {birim}"
    return f"{bicim_ondalik(deger, 0)} bayt"


def bicim_boyut_hassas(bayt: Optional[float]) -> str:
    """Büyük boyutlar iki ondalıkla ("15,03 GB"; E.3 maketindeki gibi)."""
    if bayt is None:
        return "—"
    return bicim_boyut(bayt, 2 if bayt >= BOYUT_BIRIMLERI[0][1] else 1)


def bicim_hiz(bayt_sn: Optional[float]) -> str:
    """"11,8 MB/sn"; hız ölçülemediyse "—" (TASARIM E)."""
    if bayt_sn is None or bayt_sn <= 0:
        return "—"
    return f"{bicim_boyut(bayt_sn)}/sn"


def bicim_sure(saniye: Optional[float]) -> str:
    """Geçen ya da kalan süre: "7 sn", "2 dk 5 sn", "1 sa 3 dk"."""
    if saniye is None or saniye != saniye or saniye < 0:
        return "—"
    toplam = int(math.floor(saniye + 0.5))
    if toplam < 60:
        return f"{toplam} sn"
    dakika, sn = divmod(toplam, 60)
    if dakika < 60:
        return f"{dakika} dk {sn} sn" if sn else f"{dakika} dk"
    saat, dk = divmod(dakika, 60)
    return f"{saat} sa {dk} dk" if dk else f"{saat} sa"


# ---------------------------------------------------------------------------
# API hatası: sayfaya Türkçe iletiyle döner
# ---------------------------------------------------------------------------
class ApiHatasi(Exception):
    """İstek yerine getirilemedi. ``ileti`` kullanıcıya gösterilebilir Türkçe metindir."""

    def __init__(self, durum_kodu: int, kod: str, ileti: str, **ek: Any):
        super().__init__(ileti)
        self.durum_kodu = int(durum_kodu)
        self.kod = kod
        self.ileti = ileti
        self.ek = ek

    def sozluk(self) -> Dict[str, Any]:
        return {"hata": {"kod": self.kod, "ileti": self.ileti, **self.ek}}


# ---------------------------------------------------------------------------
# Olay yayını (SSE aboneleri)
# ---------------------------------------------------------------------------
class OlayYayini:
    """Kurulum olaylarını bütün açık akışlara dağıtır.

    Her abone kendi kuyruğunu alır. Kuyruğu dolan (okumayan) abone düşürülür;
    sayfa yeniden bağlanınca önce güncel özeti alır, olay kaybı ekrana yansımaz.
    """

    def __init__(self, kuyruk_siniri: int = OLAY_KUYRUK_EN_COK):
        self._kilit = threading.Lock()
        self._aboneler: set = set()
        self._sayac = 0
        self._kapali = False
        self._sinir = kuyruk_siniri

    def abone_ol(self) -> "queue.Queue":
        kuyruk: queue.Queue = queue.Queue(maxsize=self._sinir)
        with self._kilit:
            if self._kapali:
                kuyruk.put_nowait(None)
            else:
                self._aboneler.add(kuyruk)
        return kuyruk

    def ayril(self, kuyruk: "queue.Queue") -> None:
        with self._kilit:
            self._aboneler.discard(kuyruk)

    @property
    def abone_sayisi(self) -> int:
        with self._kilit:
            return len(self._aboneler)

    def yayinla(self, tur: str, veri: Mapping[str, Any]) -> int:
        with self._kilit:
            self._sayac += 1
            kimlik = self._sayac
            olay = (kimlik, tur, dict(veri))
            dusen = []
            for kuyruk in self._aboneler:
                try:
                    kuyruk.put_nowait(olay)
                except queue.Full:
                    dusen.append(kuyruk)
            for kuyruk in dusen:
                self._aboneler.discard(kuyruk)
                logger.warning("olay akışı okunmuyor; abone düşürüldü")
                _kuyruga_son_koy(kuyruk)
        return kimlik

    def kapat(self) -> None:
        """Bütün akışları bitirir (sunucu kapanırken)."""
        with self._kilit:
            self._kapali = True
            aboneler, self._aboneler = self._aboneler, set()
        for kuyruk in aboneler:
            _kuyruga_son_koy(kuyruk)


def _kuyruga_son_koy(kuyruk: "queue.Queue") -> None:
    """Kuyruğa akışı bitiren ``None``'ı koyar; doluysa önce bir öğe atar."""
    while True:
        try:
            kuyruk.put_nowait(None)
            return
        except queue.Full:
            try:
                kuyruk.get_nowait()
            except queue.Empty:
                pass


# ---------------------------------------------------------------------------
# Yönlendirme tablosu: yol → Api yöntemi
# ---------------------------------------------------------------------------
# GET yöntemleri ``(sorgu: dict[str, str])``, POST yöntemleri ``(govde: dict)``
# alır; ikisi de ``dict`` döndürür ya da ``ApiHatasi`` fırlatır. POST yanıtı
# ``(durum_kodu, dict)`` demeti de olabilir (ör. 202).
GET_YOLLARI = {
    "/api/durum": "durum",
    "/api/is": "is_ozeti",
    "/api/is_durumu": "is_durumu",
    "/api/lisans_metni": "lisans_metni",
    "/api/kaldirma_plani": "kaldirma_plani",
}
POST_YOLLARI = {
    "/api/lisans": "lisans",
    "/api/plan": "plan",
    "/api/kur": "kur_istegi",
    "/api/iptal": "iptal",
    "/api/kaldir": "kaldir",
    "/api/uygulama": "uygulama_baslat",
    "/api/kapat": "kapat",
    "/api/ac": "ac",
    "/api/guncelleme": "guncelleme",
    "/api/tex_sina": "tex_sina",
    "/api/yetimler": "yetimleri_durdur",
    "/api/tumunu_kaldir": "tumunu_kaldir",
}
OLAY_YOLU = "/api/olaylar"


class _Isleyici(BaseHTTPRequestHandler):
    server: "_HttpSunucusu"
    server_version = "Kitapcevir-yonetici"
    sys_version = ""

    # -- günlük: anahtar içeren sorgu dizgesi yazılmaz -----------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (stdlib imzası)
        logger.debug("%s %s", self.address_string(), format % args)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        yol = urlsplit(self.path).path
        logger.debug("%s %s %s", self.command, yol, code)

    # -- yanıt yardımcıları ---------------------------------------------------
    def _baslik_yaz(self, durum_kodu: int, tur: str, uzunluk: Optional[int]) -> None:
        self.send_response(durum_kodu)
        self.send_header("Content-Type", tur)
        if uzunluk is not None:
            self.send_header("Content-Length", str(uzunluk))
        for ad, deger in GUVENLIK_BASLIKLARI:
            self.send_header(ad, deger)
        self.end_headers()

    def _json(self, durum_kodu: int, veri: Mapping[str, Any]) -> None:
        govde = json.dumps(veri, ensure_ascii=False).encode("utf-8")
        self._baslik_yaz(durum_kodu, "application/json; charset=utf-8", len(govde))
        if self.command != "HEAD":
            self.wfile.write(govde)

    def _hata(self, hata: ApiHatasi) -> None:
        self._json(hata.durum_kodu, hata.sozluk())

    def _bayt(self, durum_kodu: int, tur: str, govde: bytes) -> None:
        self._baslik_yaz(durum_kodu, tur, len(govde))
        if self.command != "HEAD":
            self.wfile.write(govde)

    # -- denetimler -----------------------------------------------------------
    def _host_gecerli(self) -> bool:
        host = self.headers.get("Host", "")
        kapi = self.server.server_address[1]
        return host in {f"{ad}:{kapi}" for ad in IZINLI_HOSTLAR}

    def _koken_gecerli(self) -> bool:
        """``Origin`` gönderildiyse bizim kökenimiz olmalı (ek savunma)."""
        koken = self.headers.get("Origin")
        if koken is None:
            return True
        kapi = self.server.server_address[1]
        return koken in {f"http://{ad}:{kapi}" for ad in IZINLI_HOSTLAR}

    def _anahtar_gecerli(self) -> bool:
        gelen = self.headers.get(ANAHTAR_BASLIGI, "")
        return hmac.compare_digest(gelen.encode("utf-8", "replace"),
                                   self.server.anahtar.encode("ascii"))

    def _on_denetim(self, api_mi: bool) -> bool:
        if not self._host_gecerli():
            self._hata(ApiHatasi(HTTPStatus.FORBIDDEN, "host", "Geçersiz Host başlığı."))
            return False
        if not self._koken_gecerli():
            self._hata(ApiHatasi(HTTPStatus.FORBIDDEN, "koken", "Başka kökenden istek."))
            return False
        if api_mi and not self._anahtar_gecerli():
            self._hata(ApiHatasi(HTTPStatus.FORBIDDEN, "anahtar", "Geçersiz ya da eksik anahtar."))
            return False
        return True

    # -- yöntemler ------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802 (stdlib adı)
        # CORS ön denetimi hiçbir zaman geçmez: izin başlığı gönderilmez.
        self._hata(ApiHatasi(HTTPStatus.FORBIDDEN, "cors", "Başka kökenden istek kabul edilmez."))

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parca = urlsplit(self.path)
        yol = parca.path
        api_mi = yol.startswith("/api/")
        if not self._on_denetim(api_mi):
            return
        try:
            if yol == OLAY_YOLU:
                if self.command == "HEAD":   # akış gövdesiz olamaz; bağlantı asılı kalmasın
                    raise ApiHatasi(HTTPStatus.METHOD_NOT_ALLOWED, "yontem",
                                    "Olay akışı yalnız GET ile okunur.")
                self._olay_akisi()
                return
            if api_mi:
                yontem = GET_YOLLARI.get(yol)
                if yontem is None:
                    raise ApiHatasi(HTTPStatus.NOT_FOUND, "yol_yok", "Böyle bir API yolu yok.")
                sorgu = {k: v[-1] for k, v in parse_qs(parca.query).items()}
                self._json(HTTPStatus.OK, self._cagir(yontem, sorgu)[1])
                return
            self._statik(yol)
        except ApiHatasi as hata:
            self._hata(hata)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001 — sayfaya iç ayrıntı sızmaz, günlüğe yazılır
            logger.exception("GET %s işlenemedi", yol)
            self._hata(ApiHatasi(HTTPStatus.INTERNAL_SERVER_ERROR, "ic_hata",
                                 "Beklenmeyen bir hata oldu; ayrıntı günlükte."))

    def do_POST(self) -> None:  # noqa: N802
        yol = urlsplit(self.path).path
        if not self._on_denetim(True):
            return
        try:
            yontem = POST_YOLLARI.get(yol)
            if yontem is None:
                raise ApiHatasi(HTTPStatus.NOT_FOUND, "yol_yok", "Böyle bir API yolu yok.")
            govde = self._govde_oku()
            durum_kodu, yanit = self._cagir(yontem, govde)
            self._json(durum_kodu, yanit)
        except ApiHatasi as hata:
            self._hata(hata)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("POST %s işlenemedi", yol)
            self._hata(ApiHatasi(HTTPStatus.INTERNAL_SERVER_ERROR, "ic_hata",
                                 "Beklenmeyen bir hata oldu; ayrıntı günlükte."))

    # -- ayrıntılar -------------------------------------------------------------
    def _govde_oku(self) -> Dict[str, Any]:
        tur = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if tur != "application/json":
            raise ApiHatasi(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "icerik_turu",
                            "Gövde application/json olmalı.")
        try:
            uzunluk = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "uzunluk", "Geçersiz Content-Length.")
        if uzunluk < 0 or uzunluk > GOVDE_EN_COK_BAYT:
            raise ApiHatasi(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "govde_buyuk", "Gövde çok büyük.")
        ham = self.rfile.read(uzunluk) if uzunluk else b"{}"
        try:
            veri = json.loads(ham.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "json", "Gövde geçerli JSON değil.")
        if not isinstance(veri, dict):
            raise ApiHatasi(HTTPStatus.BAD_REQUEST, "json", "Gövde bir JSON nesnesi olmalı.")
        return veri

    def _cagir(self, yontem: str, girdi: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        sonuc = getattr(self.server.api, yontem)(girdi)
        if isinstance(sonuc, tuple):
            return int(sonuc[0]), sonuc[1]
        return HTTPStatus.OK, sonuc

    def _statik(self, yol: str) -> None:
        if yol == TEMA_YOLU:
            self._bayt(HTTPStatus.OK, STATIK_TURLER[".css"],
                       self.server.api.tema_css().encode("utf-8"))
            return
        ad = ANA_SAYFA if yol == "/" else yol.lstrip("/")
        # Yalnız web/ klasörünün kendisindeki izinli türde dosyalar; alt klasör,
        # gizli dosya ve '..' yok.
        if ("/" in ad or "\\" in ad or ad.startswith(".") or "\x00" in ad
                or Path(ad).suffix not in STATIK_TURLER):
            raise ApiHatasi(HTTPStatus.NOT_FOUND, "dosya_yok", "Dosya bulunamadı.")
        dosya = self.server.web_dizini / ad
        try:
            govde = dosya.read_bytes()
        except OSError:
            raise ApiHatasi(HTTPStatus.NOT_FOUND, "dosya_yok", "Dosya bulunamadı.")
        self._bayt(HTTPStatus.OK, STATIK_TURLER[dosya.suffix], govde)

    def _olay_yaz(self, kimlik: Optional[int], tur: str, veri: Mapping[str, Any]) -> None:
        satirlar = []
        if kimlik is not None:
            satirlar.append(f"id: {kimlik}")
        satirlar.append(f"event: {tur}")
        for satir in json.dumps(veri, ensure_ascii=False).splitlines() or [""]:
            satirlar.append(f"data: {satir}")
        self.wfile.write(("\n".join(satirlar) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _olay_akisi(self) -> None:
        yayin = self.server.yayin
        kuyruk = yayin.abone_ol()
        try:
            self._baslik_yaz(HTTPStatus.OK, "text/event-stream; charset=utf-8", None)
            # Önce abone olunur, sonra özet alınır: aradaki olaylar iki kez
            # gelebilir; sayfa olayı her zaman güncel görünümle ezdiği için zararsız.
            self._olay_yaz(None, "ozet", self.server.api.is_ozeti({}))
            while True:
                try:
                    olay = kuyruk.get(timeout=self.server.canli_tut_sn)
                except queue.Empty:
                    self.wfile.write(b": canli\n\n")
                    self.wfile.flush()
                    continue
                if olay is None:
                    break
                kimlik, tur, veri = olay
                self._olay_yaz(kimlik, tur, veri)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            yayin.ayril(kuyruk)


class _HttpSunucusu(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    api: Any
    anahtar: str
    web_dizini: Path
    yayin: OlayYayini
    canli_tut_sn: float


class Sunucu:
    """Yalnız ``127.0.0.1``'de dinleyen sihirbaz sunucusu.

    ``api`` nesnesi ``GET_YOLLARI``/``POST_YOLLARI``'daki yöntemleri ve
    ``is_ozeti``, ``tema_css`` yöntemlerini taşır (``yonetici.Yonetici``).
    """

    def __init__(self, api: Any, anahtar: str, *, kapi: int = 0,
                 yayin: Optional[OlayYayini] = None, web_dizini: Path = WEB_DIZINI,
                 canli_tut_sn: float = OLAY_CANLI_TUT_SN):
        if not anahtar:
            raise ValueError("anahtar boş olamaz")
        self.yayin = yayin or OlayYayini()
        self._http = _HttpSunucusu(("127.0.0.1", int(kapi)), _Isleyici)
        self._http.api = api
        self._http.anahtar = anahtar
        self._http.web_dizini = Path(web_dizini)
        self._http.yayin = self.yayin
        self._http.canli_tut_sn = canli_tut_sn
        self._is_parcacigi: Optional[threading.Thread] = None
        # serve_forever hiç başlamadıysa shutdown() sonsuza dek bekler (stdlib
        # BaseServer.shutdown, __is_shut_down olayı yalnız döngü içinde kurulur).
        self._dongu_basladi = threading.Event()
        self._kapandi = False
        self._kapat_kilidi = threading.Lock()

    @property
    def kapi(self) -> int:
        return int(self._http.server_address[1])

    @property
    def anahtar(self) -> str:
        return self._http.anahtar

    def adres(self, **sorgu: str) -> str:
        """Kabuğun yükleyeceği adres: ``http://127.0.0.1:<P>/?anahtar=<tok>[&…]``."""
        ekler = "".join(f"&{k}={v}" for k, v in sorgu.items())
        return f"http://127.0.0.1:{self.kapi}/?anahtar={self.anahtar}{ekler}"

    def calistir(self) -> None:
        """Kapanana dek istekleri işler (çağıran iş parçacığında)."""
        with self._kapat_kilidi:
            if self._kapandi:
                return
            self._dongu_basladi.set()
        self._http.serve_forever(poll_interval=0.2)

    def arka_planda_baslat(self) -> None:
        self._is_parcacigi = threading.Thread(target=self.calistir, name="sunucu", daemon=True)
        self._is_parcacigi.start()

    def kapat(self) -> None:
        """Akışları bitirir, dinlemeyi durdurur, soketi kapatır. İki kez çağrılabilir.

        Döngüyü koşan iş parçacığından çağrılmamalıdır (``shutdown`` döngünün
        bitmesini bekler); yönetici bunu ayrı iş parçacığından yapar.
        """
        with self._kapat_kilidi:
            ilk = not self._kapandi
            self._kapandi = True
            basladi = self._dongu_basladi.is_set()
        self.yayin.kapat()
        if not ilk:
            return
        if basladi:
            self._http.shutdown()
        self._http.server_close()
        if self._is_parcacigi is not None and self._is_parcacigi is not threading.current_thread():
            self._is_parcacigi.join(timeout=5)


def zaman_damgasi() -> float:
    """Olaylara eklenen sunucu zamanı (sayfa geçen süreyi buna göre sayar)."""
    return time.time()


__all__ = [
    "ApiHatasi", "OlayYayini", "Sunucu", "bicim_boyut", "bicim_boyut_hassas", "bicim_hiz",
    "bicim_ondalik", "bicim_sure", "zaman_damgasi", "ANAHTAR_BASLIGI", "GET_YOLLARI",
    "POST_YOLLARI", "OLAY_YOLU",
]

