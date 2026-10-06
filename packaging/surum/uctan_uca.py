#!/usr/bin/env python3
"""Sürüm DMG'sinin uçtan uca sınaması (packaging/TASARIM.md H.1, H.3, H.4, H.8, H.9, H.12).

SAHTE KÖKTE çalışır: gerçek ``~/Library``, ``~/Applications`` ya da ``/Applications``'a
kurmaz (H kuralları). Akış:

1. DMG salt okunur bağlanır; ``Kitapçevir.app`` sahte ``~/Applications``'a kopyalanır,
   gömülü arşivlere karantina yazılır (indirilmiş DMG taklidi, K1 yöntemi).
2. Kabuk (``Contents/MacOS/Kitapcevir``) sahte HOME ile açılır: çalışma zamanı ve kod
   köke açılır, yönetici kalkar (ilk açılış süresi); güvenlik denetimleri (H.12);
   SIGTERM ile kapatılır; ikinci açılış (açma yok) ölçülür.
3. Sihirbazın API'si, kabuğun başlattığı yöneticide sayfanın yaptığı sırayla sürülür:
   lisans metni (yerel kopya doğrulanmalı) → Datalab onayı → plan (tex-hafif) → kur;
   olay akışından (SSE) adım süreleri ve inen baytlar ölçülür. İsteğe bağlı H.3:
   indirme sürerken yönetici ``kill -9`` → yeniden açılış → sürdürme.
4. Uygulama: ``POST /api/uygulama`` → Streamlit ``/_stcore/health``; duman testi
   "PDF'den TeX çıkar" (Streamlit'in ortamıyla ``1_cikar.py`` → kimlik çevirisi →
   ``2_derle.py`` → PDF); sentetik EPUB ve isteğe bağlı sentetik PDF (H.4: Surya kendi
   indiricisini çalıştırmamalı). Diskteki boyut manifestle karşılaştırılır (H.1 ±%2).
5. Kaldırma: ``/api/kaldirma_plani`` → ``/api/tumunu_kaldir`` → kabuk kapanır → kökte,
   günlüklerde ve sahte ``~/Library``'de kalan dosyalar sayılır.

Model YÜKLENMEZ (HY-MT indirilmez). Sonuçlar ``--cikti``'daki ``uctan_uca.json``'a yazılır.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BURASI = Path(__file__).resolve().parent
sys.path.insert(0, str(BURASI))
import surum_ayarlari as SA  # noqa: E402

KLASOR_ADI = "Kitapçevir"                 # kök ve günlük klasörü (yonetici.UYGULAMA_KLASOR_ADI)
ANAHTAR_BASLIGI = "X-Kitapcevir-Anahtar"  # sunucu.ANAHTAR_BASLIGI
HAZIR_BEKLEME_SN = 120
KURULUM_BEKLEME_SN = 3600
KAPANIS_BEKLEME_SN = 60
OLAY_ZAMAN_ASIMI_SN = 30

_T0 = time.monotonic()


def gunluk(ileti: str) -> None:
    g = time.monotonic() - _T0
    print(f"[{int(g // 60):02d}:{g % 60:04.1f}] {ileti}", flush=True)


def calistir(argv, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kw)


def du_bayt(yol: Path) -> int:
    if not yol.exists():
        return 0
    return int(calistir(["/usr/bin/du", "-sk", yol]).stdout.split()[0]) * 1024


def karantinali_say(yol: Path) -> int:
    sonuc = calistir(["/usr/bin/xattr", "-r", "-l", yol])
    return sum(1 for s in sonuc.stdout.splitlines() if "com.apple.quarantine" in s)


def dosyalari_listele(kok: Path) -> List[str]:
    if not kok.exists():
        return []
    return sorted(str(p.relative_to(kok)) for p in kok.rglob("*"))


# ---------------------------------------------------------------------------
# Yönetici API'si (sayfanın kullandığı uçlar)
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, url: str):
        p = urllib.parse.urlsplit(url)
        self.kapi = p.port
        self.anahtar = urllib.parse.parse_qs(p.query)["anahtar"][0]
        self.son_gorunum: Dict[str, Any] = {}

    def istek(self, yontem: str, yol: str, govde: Optional[dict] = None, *,
              anahtar: Optional[str] = "", host: Optional[str] = None,
              zaman_asimi: float = 120) -> Tuple[int, Any]:
        b = http.client.HTTPConnection("127.0.0.1", self.kapi, timeout=zaman_asimi)
        basliklar = {"Host": host or f"127.0.0.1:{self.kapi}"}
        if anahtar is not None:
            basliklar[ANAHTAR_BASLIGI] = anahtar or self.anahtar
        veri = None
        if govde is not None:
            veri = json.dumps(govde).encode()
            basliklar["Content-Type"] = "application/json"
        b.request(yontem, yol, body=veri, headers=basliklar)
        y = b.getresponse()
        ham = y.read()
        b.close()
        try:
            return y.status, json.loads(ham.decode("utf-8") or "null")
        except ValueError:
            return y.status, ham.decode("utf-8", "replace")

    def olay_dinle(self, hedef: List[Dict[str, Any]], dur: threading.Event) -> threading.Thread:
        """SSE akışını okur; her olay ``{"t", "tur", "veri"}`` olarak eklenir."""
        def _oku():
            b = http.client.HTTPConnection("127.0.0.1", self.kapi, timeout=OLAY_ZAMAN_ASIMI_SN * 4)
            b.request("GET", "/api/olaylar", headers={"Host": f"127.0.0.1:{self.kapi}",
                                                      ANAHTAR_BASLIGI: self.anahtar})
            y = b.getresponse()
            tur, satirlar = None, []
            while not dur.is_set():
                try:
                    ham = y.fp.readline()
                except (TimeoutError, OSError):
                    break
                if not ham:
                    break
                satir = ham.decode("utf-8").rstrip("\n")
                if satir.startswith("event: "):
                    tur = satir[7:]
                elif satir.startswith("data: "):
                    satirlar.append(satir[6:])
                elif satir == "" and tur:
                    try:
                        veri = json.loads("\n".join(satirlar))
                    except ValueError:
                        veri = {}
                    # Her olay sayfanın bütün görünümünü taşır: kayda yalnız sonuncusu girer.
                    if isinstance(veri, dict) and isinstance(veri.get("gorunum"), dict):
                        self.son_gorunum = veri.pop("gorunum")
                    hedef.append({"t": time.monotonic(), "tur": tur, "veri": veri})
                    tur, satirlar = None, []
            b.close()
        t = threading.Thread(target=_oku, daemon=True)
        t.start()
        return t


# ---------------------------------------------------------------------------
# Kabuk
# ---------------------------------------------------------------------------
class Kabuk:
    def __init__(self, app: Path, ev: Path, kok: Path, tmp: Path):
        self.app, self.ev, self.kok, self.tmp = app, ev, kok, tmp
        self.surec: Optional[subprocess.Popen] = None

    def ortam(self) -> Dict[str, str]:
        return {"HOME": str(self.ev), "CFFIXED_USER_HOME": str(self.ev),
                "KITAPCEVIR_KOK": str(self.kok), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "TMPDIR": str(self.tmp), "USER": os.environ.get("USER", ""),
                "LOGNAME": os.environ.get("LOGNAME", "")}

    def ac(self) -> float:
        """Kabuğu açar; yönetici hazır olana dek (calisan.json'da adres) bekler. Süre döner."""
        calisan = self.kok / "durum" / "calisan.json"
        if calisan.exists():
            calisan.unlink()
        t0 = time.monotonic()
        self.surec = subprocess.Popen([str(self.app / "Contents" / "MacOS" / "Kitapcevir")],
                                      env=self.ortam(), stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
        while time.monotonic() - t0 < HAZIR_BEKLEME_SN:
            if self.surec.poll() is not None:
                raise RuntimeError(f"kabuk erken çıktı: {self.surec.returncode}")
            try:
                kayit = json.loads(calisan.read_text(encoding="utf-8"))
                if kayit.get("url"):
                    return time.monotonic() - t0
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        raise RuntimeError("yönetici hazır olmadı (calisan.json yok)")

    def url(self) -> str:
        return json.loads((self.kok / "durum" / "calisan.json").read_text(encoding="utf-8"))["url"]

    def kapat(self) -> Dict[str, Any]:
        """SIGTERM (K1: onaysız düzenli kapanış) → çıkış ve yetim süreç denetimi."""
        assert self.surec is not None
        t0 = time.monotonic()
        self.surec.send_signal(signal.SIGTERM)
        try:
            kod = self.surec.wait(timeout=KAPANIS_BEKLEME_SN)
        except subprocess.TimeoutExpired:
            self.surec.kill()
            kod = "zaman_asimi"
        sure = time.monotonic() - t0
        time.sleep(1.0)
        kalan = calistir(["/usr/bin/pgrep", "-f", str(self.kok)]).stdout.split()
        return {"cikis": kod, "sure_sn": round(sure, 2), "kalan_surec": kalan}



# ---------------------------------------------------------------------------
# Yardımcılar: sentetik girdi, Streamlit ortamı
# ---------------------------------------------------------------------------
def sentetik_epub(venv_python: Path, hedef: Path) -> None:
    """Kendi yazdığımız cümlelerle küçük bir EPUB (KARAR 26: telifli metin yok)."""
    betik = r'''
import sys
from ebooklib import epub
k = epub.EpubBook()
k.set_identifier("kitapcevir-duman-1"); k.set_title("Smoke Test Book"); k.set_language("en")
k.add_author("Kitapcevir Test")
b1 = epub.EpubHtml(title="Chapter One", file_name="c1.xhtml", lang="en")
b1.content = ("<h1>Chapter One</h1><p>The small lighthouse stood on a quiet hill above the "
              "harbor.</p><p>Every evening the keeper counted 12 ships and wrote the number "
              "in a blue notebook.</p>")
b2 = epub.EpubHtml(title="Chapter Two", file_name="c2.xhtml", lang="en")
b2.content = ("<h1>Chapter Two</h1><p>In winter the wind carried salt over the old stone "
              "steps.</p><p>The keeper measured the temperature: 3.5 degrees at dawn.</p>")
for b in (b1, b2): k.add_item(b)
k.toc = (b1, b2); k.spine = ["nav", b1, b2]
k.add_item(epub.EpubNcx()); k.add_item(epub.EpubNav())
epub.write_epub(sys.argv[1], k)
'''
    sonuc = calistir([venv_python, "-c", betik, hedef])
    if sonuc.returncode != 0:
        raise RuntimeError("sentetik EPUB yazılamadı: " + sonuc.stderr[-1000:])


def streamlit_ortami(runtime_python: Path, uygulama: Path, kok: Path, url: str,
                     temel: Dict[str, str]) -> Dict[str, str]:
    """Yöneticinin kendi ``surecler.streamlit_ortami``'ı (tek kaynak), ayrı süreçte."""
    betik = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(uygulama / 'packaging')!r})\n"
        "from yonetici import durum, surecler\n"
        f"d = durum.KokDuzeni({str(kok)!r})\n"
        f"print(json.dumps(surecler.streamlit_ortami(d, {url!r}, temel=json.loads(sys.argv[1]))))\n")
    sonuc = calistir([runtime_python, "-I", "-c", betik, json.dumps(temel)])
    if sonuc.returncode != 0:
        raise RuntimeError("Streamlit ortamı alınamadı: " + sonuc.stderr[-1000:])
    return json.loads(sonuc.stdout.strip().splitlines()[-1])


def duman(ad: str, girdi: Path, venv_python: Path, uygulama: Path, ortam: Dict[str, str],
          veri: Path, cikti: Path) -> Dict[str, Any]:
    """"PDF'den TeX çıkar": 1_cikar → kimlik çevirisi → 2_derle (arayüzün çağırdığı kapılar)."""
    s: Dict[str, Any] = {"girdi": girdi.name}
    t0 = time.monotonic()
    r1 = calistir([venv_python, uygulama / "1_cikar.py", girdi, "--name", ad, "--json"],
                  env=ortam, cwd=uygulama, timeout=1800)
    s["cikar_sn"] = round(time.monotonic() - t0, 1)
    s["cikar_cikis"] = r1.returncode
    (cikti / f"duman_{ad}_cikar.log").write_text(r1.stdout + "\n--- stderr ---\n" + r1.stderr,
                                                 encoding="utf-8")
    kitap = veri / "output" / ad
    export = kitap / "EXPORT.tex"
    s["export_bayt"] = export.stat().st_size if export.exists() else 0
    birlesik = r1.stdout + r1.stderr
    s["surya_indirme_satiri"] = len(re.findall(r"Downloading", birlesik))
    if not export.exists():
        return s
    tr = kitap / "EXPORT_TR.tex"
    shutil.copyfile(export, tr)          # kimlik çevirisi (TASARIM G.1-7)
    t0 = time.monotonic()
    r2 = calistir([venv_python, uygulama / "2_derle.py", tr, "--json"], env=ortam, cwd=uygulama,
                  timeout=1800)
    s["derle_sn"] = round(time.monotonic() - t0, 1)
    s["derle_cikis"] = r2.returncode
    (cikti / f"duman_{ad}_derle.log").write_text(r2.stdout + "\n--- stderr ---\n" + r2.stderr,
                                                 encoding="utf-8")
    pdfler = sorted(kitap.rglob("*.pdf"))
    s["pdf"] = [{"ad": str(p.relative_to(kitap)), "bayt": p.stat().st_size} for p in pdfler]
    if pdfler:
        sayfa = calistir(["/usr/bin/mdls", "-raw", "-name", "kMDItemNumberOfPages", pdfler[0]])
        s["pdf_sayfa"] = sayfa.stdout.strip()
    return s


# ---------------------------------------------------------------------------
# Ana akış
# ---------------------------------------------------------------------------
def main() -> int:
    ay = argparse.ArgumentParser(description="Kitapçevir DMG uçtan uca sınaması (sahte kök)")
    ay.add_argument("--dmg", required=True)
    ay.add_argument("--manifest", required=True, help="sürümün manifest.json'u (H.1 karşılaştırma)")
    ay.add_argument("--is", dest="is_", required=True, help="karalama klasörü (sahte ev burada)")
    ay.add_argument("--cikti", required=True, help="sonuçlar (uctan_uca.json, günlükler)")
    ay.add_argument("--pdf", help="H.4 için küçük sentetik PDF (ör. de_sentetik.pdf)")
    ay.add_argument("--kesinti", action="store_true",
                    help="H.3: indirme sürerken yönetici kill -9, yeniden açılış, sürdürme")
    ay.add_argument("--kaldirma-yok", action="store_true", help="kurulum yerinde bırakılır")
    ay.add_argument("--gguf", action="store_true",
                    help="H.10: GGUF bileşenini ekle (hazır tekerlek yoksa kaynaktan derleme) ve kaldır")
    a = ay.parse_args()

    is_ = Path(a.is_).resolve()
    cikti = Path(a.cikti).resolve()
    cikti.mkdir(parents=True, exist_ok=True)
    if is_.exists():
        shutil.rmtree(is_)
    ev = is_ / "ev"
    kok = ev / "Library" / "Application Support" / KLASOR_ADI
    gunluk_dizini = ev / "Library" / "Logs" / KLASOR_ADI
    tmp = is_ / "tmp"
    for d in (ev / "Applications", tmp):
        d.mkdir(parents=True)
    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    sonuc: Dict[str, Any] = {"dmg": a.dmg, "tarih": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "kok": str(kok)}

    def kaydet() -> None:
        (cikti / "uctan_uca.json").write_text(json.dumps(sonuc, ensure_ascii=False, indent=1,
                                                         default=str), encoding="utf-8")

    try:
        _akis(a, is_, cikti, ev, kok, gunluk_dizini, tmp, manifest, sonuc, kaydet)
    except Exception as exc:  # noqa: BLE001 — sınamanın kendisi: hata sonuca yazılır
        import traceback
        sonuc["hata"] = f"{type(exc).__name__}: {exc}"
        sonuc["iz"] = traceback.format_exc()[-4000:]
        gunluk(f"HATA: {exc}")
        kaydet()
        return 1
    finally:
        sonuc["temizlik"] = temizle(ev, is_ / "mnt")
        kaydet()
    return 0


def temizle(ev: Path, mnt: Path) -> Dict[str, Any]:
    """Sınamanın açtığı her süreç (kabuk, yönetici, Streamlit, işler; hepsi sahte evin yolunu
    taşır) kapatılır, DMG bağlıysa çözülür. Hata durumunda Berke'nin ekranında pencere kalmasın."""
    def _pidler() -> List[int]:
        return [int(x) for x in calistir(["/usr/bin/pgrep", "-f", str(ev)]).stdout.split()
                if int(x) != os.getpid()]
    ilk = _pidler()
    for pid in ilk:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    t0 = time.monotonic()
    while _pidler() and time.monotonic() - t0 < 10:
        time.sleep(0.5)
    zorla = _pidler()
    for pid in zorla:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if os.path.ismount(mnt):
        calistir(["/usr/bin/hdiutil", "detach", "-force", mnt])
    return {"sonlandirilan": ilk, "zorla": zorla}


def _akis(a, is_: Path, cikti: Path, ev: Path, kok: Path, gunluk_dizini: Path, tmp: Path,
          manifest: Dict[str, Any], sonuc: Dict[str, Any], kaydet) -> None:
    # -- 1. DMG ------------------------------------------------------------------------
    mnt = is_ / "mnt"
    mnt.mkdir()
    t0 = time.monotonic()
    r = calistir(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
                  "-mountpoint", mnt, a.dmg])
    if r.returncode != 0:
        raise RuntimeError("DMG bağlanamadı: " + r.stderr)
    dmg = {"baglama_sn": round(time.monotonic() - t0, 2), "icerik": sorted(os.listdir(mnt))}
    app_dmg = mnt / SA.PAKET_ADI
    dmg["uygulamalar_bagi"] = os.readlink(mnt / "Applications") if (mnt / "Applications").is_symlink() else None
    v = calistir(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", app_dmg])
    dmg["codesign_verify"] = (v.returncode, (v.stderr or v.stdout).strip().splitlines()[-2:])
    app = ev / "Applications" / SA.PAKET_ADI
    t0 = time.monotonic()
    calistir(["/usr/bin/ditto", app_dmg, app])
    dmg["kopyalama_sn"] = round(time.monotonic() - t0, 2)
    calistir(["/usr/bin/hdiutil", "detach", mnt])
    dmg["paket_bayt"] = du_bayt(app)
    # İndirilmiş DMG taklidi: gömülü arşivlere karantina (K1 sınama yöntemi).
    damga = "0083;%08x;Safari;" % int(time.time())
    arsivler = sorted((app / "Contents" / "Resources").glob("*/*.tar.gz"))
    for p in arsivler:
        calistir(["/usr/bin/xattr", "-w", "com.apple.quarantine", damga, p])
    dmg["karantina_yazilan_arsiv"] = [p.name for p in arsivler]
    sonuc["dmg_adimi"] = dmg
    gunluk(f"DMG: bağlandı, içerik {dmg['icerik']}, codesign {dmg['codesign_verify'][0]}")
    kaydet()

    # -- 2. kabuk: ilk açılış, güvenlik, kapanış, ikinci açılış ----------------------------
    kabuk = Kabuk(app, ev, kok, tmp)
    ilk = {"hazir_sn": round(kabuk.ac(), 2)}
    api = Api(kabuk.url())
    pbs = next(p for p in (app / "Contents" / "Resources" / "calisma_zamani").glob("*.tar.gz"))
    etiket = re.search(r"cpython-([0-9.]+\+[0-9]+)-", pbs.name).group(1)
    cz = kok / "calisma_zamani" / f"python-{etiket}"
    ilk["tamam_isareti"] = (cz / ".tamam").read_text(encoding="utf-8").strip()
    ilk["calisma_zamani_sha_eslesir"] = ilk["tamam_isareti"] == manifest["uygulama"]["calisma_zamani"]["sha256"]
    ilk["guncel_bagi"] = os.readlink(kok / "app" / "guncel")
    ilk["karantinali_oge_kokte"] = karantinali_say(kok)
    durum_kodu, d = api.istek("GET", "/api/durum")
    ilk["durum_http"] = durum_kodu
    ilk["durum"] = {"surum": d.get("surum"), "macos": d.get("macos"), "internet": d.get("internet"),
                    "uygulama_hazir": d.get("uygulama_hazir"),
                    "donanim": d.get("donanim"),
                    "bilesenler": {b["kimlik"]: b.get("durum") for b in d.get("bilesenler", [])}}
    # H.12: anahtarsız, yanlış anahtar, yanlış Host → 403; kurulum başlamaz.
    ilk["guvenlik"] = {
        "anahtarsiz_durum": api.istek("GET", "/api/durum", anahtar=None)[0],
        "yanlis_anahtar_kur": api.istek("POST", "/api/kur", {"plan_kimligi": "x"},
                                        anahtar="0" * 64)[0],
        "yanlis_host": api.istek("GET", "/api/durum", host=f"evil.example:{api.kapi}")[0],
    }
    ilk["kapanis"] = kabuk.kapat()
    sonuc["ilk_acilis"] = ilk
    gunluk(f"kabuk ilk açılış: yönetici {ilk['hazir_sn']} sn'de hazır; güvenlik {ilk['guvenlik']}")
    kaydet()
    if a.kesinti:
        # H.3 (b): kabuksuz yöneticiyle (NSAlert penceresi açılmasın) indirme → kill -9 →
        # yeniden açılış → sürdürme denetimi. Bu kipte kurulum tamamlanmaz.
        sonuc["kesinti"] = kesinti_akisi(kok, cz / "bin" / "python3", ev, tmp, gunluk_dizini)
        kaydet()
        return
    ikinci = {"hazir_sn": round(kabuk.ac(), 2)}
    api = Api(kabuk.url())
    sonuc["ikinci_acilis"] = ikinci
    gunluk(f"kabuk ikinci açılış: {ikinci['hazir_sn']} sn")

    # -- 3. sihirbaz: lisans → plan → kur ------------------------------------------------
    sih: Dict[str, Any] = {}
    kod, lm = api.istek("GET", "/api/lisans_metni?bilesen=pdf")
    sih["lisans_metni"] = {"http": kod, "dogrulandi": lm.get("dogrulandi"),
                           "url": lm.get("url"), "uzunluk": len(lm.get("metin") or "")}
    kod, lm2 = api.istek("GET", "/api/lisans_metni?bilesen=hymt")
    sih["hymt_lisans_metni"] = {"http": kod, "dogrulandi": lm2.get("dogrulandi")}
    sih["lisans_kabul"] = api.istek("POST", "/api/lisans", {"bilesen": "pdf", "kabul": True})[0]
    kod, plan = api.istek("POST", "/api/plan", {"tex": "tex-hafif", "istege_bagli": []})
    sih["plan_http"] = kod
    sih["plan"] = {k: plan.get(k) for k in ("plan_kimligi", "indirme_bayt", "disk_bayt",
                                            "bos_disk_bayt", "yeter", "eksik_onaylar",
                                            "indirme_metni", "disk_metni")}
    sih["plan_adimlari"] = [s.get("bilesen") for s in plan.get("adimlar") or []]
    olaylar: List[Dict[str, Any]] = []
    dur = threading.Event()
    api.olay_dinle(olaylar, dur)
    time.sleep(0.5)
    t_kur = time.monotonic()
    sih["kur_http"] = api.istek("POST", "/api/kur", {"plan_kimligi": plan["plan_kimligi"]})[0]
    gunluk(f"kurulum başladı: plan {sih['plan']['plan_kimligi']}, indirme "
           f"{sih['plan']['indirme_metni']}, disk {sih['plan']['disk_metni']}")
    bitti = None
    son_yazi = time.monotonic()
    while time.monotonic() - t_kur < KURULUM_BEKLEME_SN:
        bitti = next((o for o in olaylar if o["tur"] == "bitti"), None)
        if bitti:
            break
        if time.monotonic() - son_yazi > 60:
            son = next((o for o in reversed(olaylar) if o["tur"] in ("adim_basladi", "ilerleme")),
                       None)
            gunluk(f"  sürüyor: {len(olaylar)} olay; son: "
                   f"{(son or {}).get('tur')} {json.dumps((son or {}).get('veri', {}), ensure_ascii=False)[:160]}")
            son_yazi = time.monotonic()
        time.sleep(1)
    dur.set()
    sih["kurulum_sn"] = round(time.monotonic() - t_kur, 1)
    sih["bitti"] = bitti["veri"] if bitti else None
    sih.update(_olay_ozeti(olaylar, t_kur))
    genel = (api.son_gorunum or {}).get("genel") or {}
    sih["ekran_genel"] = {k: genel.get(k) for k in ("bayt", "toplam_bayt", "metin")}
    (cikti / "kurulum_olaylari.jsonl").write_text(
        "".join(json.dumps({"t": round(o["t"] - t_kur, 3), "tur": o["tur"], "veri": o["veri"]},
                           ensure_ascii=False, default=str) + "\n" for o in olaylar),
        encoding="utf-8")
    sonuc["sihirbaz"] = sih
    kaydet()
    if not bitti or (bitti["veri"].get("sonuc") != "tamam"):
        raise RuntimeError(f"kurulum tamamlanmadı: {bitti['veri'] if bitti else 'bitti olayı yok'}")
    gunluk(f"kurulum bitti: {sih['kurulum_sn']} sn, ağdan {sih.get('inen_bayt', 0) / 1e9:.2f} GB")

    # -- H.1: diskteki boyut manifestle --------------------------------------------------
    kod, d = api.istek("GET", "/api/durum")
    tex_hafif_secili = True
    beklenen = sum(b["boyut"]["disk_bayt"]["hepsi"] for b in manifest["bilesenler"]
                   if b["zorunluluk"] == "zorunlu" or (tex_hafif_secili and b["kimlik"] == "tex-hafif"))
    gercek = du_bayt(kok)
    sonuc["disk"] = {"kok_du_bayt": gercek, "manifest_toplam_bayt": beklenen,
                     "fark_yuzde": round(100.0 * (gercek - beklenen) / beklenen, 2),
                     "parcalar": {p.name: du_bayt(p) for p in sorted(kok.iterdir())},
                     "durum_sonrasi": {b["kimlik"]: b.get("durum") for b in d.get("bilesenler", [])},
                     "uygulama_hazir": d.get("uygulama_hazir")}
    kaydet()

    # -- 4. uygulama + duman testi ---------------------------------------------------------
    t0 = time.monotonic()
    kod, uyg = api.istek("POST", "/api/uygulama", {}, zaman_asimi=180)
    st = {"http": kod, "url": uyg.get("url") if isinstance(uyg, dict) else uyg,
          "sure_sn": round(time.monotonic() - t0, 1)}
    if kod == 200 and st["url"]:
        p = urllib.parse.urlsplit(st["url"])
        taban = p.path.rstrip("/")         # yöneticinin rastgele taban yolu (K9 D8)
        for ad, yol in (("saglik", f"{taban}/_stcore/health"), ("ana_sayfa", f"{taban}/"),
                        ("kok_tabansiz", "/")):
            b = http.client.HTTPConnection("127.0.0.1", p.port, timeout=30)
            b.request("GET", yol)
            y = b.getresponse()
            govde = y.read()
            st[ad] = (y.status, govde.decode().strip() if ad == "saglik" else len(govde))
            b.close()
        # Taban yolunu bilmeyen istek (DNS yeniden bağlama) arayüze ulaşmamalı.
        st["kok_404"] = st["kok_tabansiz"][0] == 404
    sonuc["streamlit"] = st
    gunluk(f"Streamlit: {st}")
    kaydet()
    venv_python = kok / "venv" / "bin" / "python"
    uygulama = kok / "app" / "guncel"
    runtime_python = cz / "bin" / "python3"
    temel = {"HOME": str(ev), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": str(tmp),
             "USER": os.environ.get("USER", "")}
    ortam = streamlit_ortami(runtime_python, uygulama, kok, kabuk.url(), temel)
    girdi = is_ / "duman.epub"
    sentetik_epub(venv_python, girdi)
    sonuc["duman_epub"] = duman("duman_epub", girdi, venv_python, uygulama, ortam, kok / "veri",
                                cikti)
    gunluk(f"duman (EPUB): {sonuc['duman_epub']}")
    kaydet()
    if a.pdf:
        kopya = is_ / "duman.pdf"
        shutil.copyfile(a.pdf, kopya)
        modeller_once = du_bayt(kok / "modeller")
        sonuc["duman_pdf"] = duman("duman_pdf", kopya, venv_python, uygulama, ortam, kok / "veri",
                                   cikti)
        sonuc["duman_pdf"]["modeller_once_bayt"] = modeller_once
        sonuc["duman_pdf"]["modeller_sonra_bayt"] = du_bayt(kok / "modeller")
        sonuc["duman_pdf"]["datalab_varsayilan_onbellek_var"] = (
            ev / "Library" / "Caches" / "datalab").exists()
        gunluk(f"duman (PDF): {sonuc['duman_pdf']}")
        kaydet()

    # -- H.5: bu Mac'te kurulu TeX (algıla + şablon sınaması) --------------------------------
    _, d = api.istek("GET", "/api/durum")
    algilanan = d.get("tex_algilanan") or []
    h5: Dict[str, Any] = {"algilanan": [{k: x.get(k) for k in ("yol", "surum")} for x in algilanan]}
    if algilanan:
        kod, ts = api.istek("POST", "/api/tex_sina", {"yol": algilanan[0].get("yol")},
                            zaman_asimi=300)
        h5["sinama_http"] = kod
        h5["sinama"] = ts.get("sinama") if isinstance(ts, dict) else ts
    try:
        h5["tex_json"] = json.loads((kok / "durum" / "tex.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        h5["tex_json"] = None
    sonuc["tex"] = h5
    kaydet()

    # -- H.10: GGUF ekle / kaldır ----------------------------------------------------------
    if a.gguf:
        sonuc["gguf"] = gguf_akisi(api, kok, cikti)
        gunluk(f"GGUF: {json.dumps(sonuc['gguf'], ensure_ascii=False)[:300]}")
        kaydet()

    # -- H.9: kabuk kill -9 → yönetici ve torunları kapanıyor mu, yeniden açılış ---------------
    h9: Dict[str, Any] = {}
    assert kabuk.surec is not None
    kabuk.surec.kill()
    kabuk.surec.wait()
    t0 = time.monotonic()
    kalan: List[str] = []
    while time.monotonic() - t0 < 30:
        kalan = calistir(["/usr/bin/pgrep", "-f", str(kok)]).stdout.split()
        if not kalan:
            break
        time.sleep(0.5)
    h9["kabuk_kill9_sonrasi_kapanma_sn"] = round(time.monotonic() - t0, 1)
    h9["kalan_surec"] = kalan
    h9["yeniden_acilis_sn"] = round(kabuk.ac(), 2)
    api = Api(kabuk.url())
    _, d = api.istek("GET", "/api/durum")
    h9["uygulama_hazir"] = d.get("uygulama_hazir")
    sonuc["yasam_dongusu"] = h9
    gunluk(f"H.9: {h9}")
    kaydet()

    # -- 5. kaldırma -----------------------------------------------------------------------
    if a.kaldirma_yok:
        sonuc["kapanis_son"] = kabuk.kapat()
        return
    kod, kp = api.istek("GET", "/api/kaldirma_plani")
    kal: Dict[str, Any] = {"plan_http": kod, "izin": kp.get("izin"), "neden": kp.get("neden"),
                           "silinecek_bayt": kp.get("silinecek_bayt"),
                           "veri_bayt": (kp.get("veri") or {}).get("bayt"),
                           "ogeler": [(o["tur"], o["yol_metni"], o["bayt"]) for o in kp.get("ogeler", [])]}
    kod, kr = api.istek("POST", "/api/tumunu_kaldir", {"onay": True, "veri_de_sil": True})
    kal["kaldir_http"] = kod
    kal["yanit"] = kr
    kal["kapanis"] = kabuk.kapat()
    kal["kok_var"] = kok.exists()
    kal["kok_kalan"] = dosyalari_listele(kok)[:50]
    kal["gunluk_dizini_var"] = gunluk_dizini.exists()
    kal["gunluk_kalan"] = dosyalari_listele(gunluk_dizini)
    kal["ev_kalan_ust"] = sorted(str(p.relative_to(ev)) for p in ev.rglob("*")
                                 if not str(p.relative_to(ev)).startswith("Applications/"))
    kal["ev_library_bayt"] = du_bayt(ev / "Library")
    sonuc["kaldirma"] = kal
    gunluk(f"kaldırma: kök var mı {kal['kok_var']}, günlük var mı {kal['gunluk_dizini_var']}, "
           f"ev/Library {kal['ev_library_bayt']} B")


def _olay_ozeti(olaylar: List[Dict[str, Any]], t0: float) -> Dict[str, Any]:
    """Olaylardan adım süreleri ve ağdan inen bayt (öğe başına son 'bayt')."""
    oge_bayt: Dict[str, int] = {}
    genel = 0
    adimlar: List[Dict[str, Any]] = []
    acik: Dict[Tuple[str, str], float] = {}
    sayac_son: Dict[str, int] = {}
    for o in olaylar:
        v = o["veri"] if isinstance(o["veri"], dict) else {}
        if o["tur"] == "ilerleme":
            if isinstance(v.get("bayt"), int):
                oge_bayt[f"{v.get('bilesen')}/{v.get('oge')}"] = v["bayt"]
            if isinstance(v.get("genel_bayt"), int):
                genel = max(genel, v["genel_bayt"])
        elif o["tur"] == "adim_basladi":
            acik[(str(v.get("bilesen")), str(v.get("adim")))] = o["t"]
        elif o["tur"] == "adim_bitti":
            k = (str(v.get("bilesen")), str(v.get("adim")))
            adimlar.append({"bilesen": k[0], "adim": k[1], "sure_sn": v.get("sure_sn"),
                            "atlandi": v.get("atlandi"),
                            "bitis_sn": round(o["t"] - t0, 1)})
        elif o["tur"] == "sayac":
            sayac_son[str(v.get("bilesen"))] = max(sayac_son.get(str(v.get("bilesen")), 0),
                                                   int(v.get("i") or 0))
    bilesen_bayt: Dict[str, int] = {}
    for anahtar, bayt in oge_bayt.items():
        b = anahtar.split("/", 1)[0]
        bilesen_bayt[b] = bilesen_bayt.get(b, 0) + bayt
    return {"inen_bayt": sum(oge_bayt.values()), "genel_bayt_son": genel,
            "bilesen_inen_bayt": bilesen_bayt, "adimlar": adimlar, "sayac_son": sayac_son,
            "olay_sayisi": len(olaylar),
            "hatalar": [o["veri"] for o in olaylar if o["tur"] == "hata"]}


def gguf_akisi(api: Api, kok: Path, cikti: Path) -> Dict[str, Any]:
    """H.10: GGUF desteğini kur (sürüm varlığı yoksa kaynaktan derleme yolu), keşif
    llama_cpp'yi görüyor mu, kaldır, görmüyor mu."""
    g: Dict[str, Any] = {}
    kod, plan = api.istek("POST", "/api/plan", {"tex": "tex-hafif", "istege_bagli": ["gguf"]})
    g["plan_http"] = kod
    g["plan_adimlari"] = [x.get("bilesen") for x in plan.get("adimlar") or []]
    olaylar: List[Dict[str, Any]] = []
    dur = threading.Event()
    api.olay_dinle(olaylar, dur)
    time.sleep(0.5)
    t0 = time.monotonic()
    g["kur_http"] = api.istek("POST", "/api/kur", {"plan_kimligi": plan["plan_kimligi"]})[0]
    bitti = None
    while time.monotonic() - t0 < 1800:
        bitti = next((o for o in olaylar if o["tur"] == "bitti"), None)
        if bitti:
            break
        time.sleep(1)
    dur.set()
    g["sure_sn"] = round(time.monotonic() - t0, 1)
    g["bitti"] = bitti["veri"] if bitti else None
    g["hatalar"] = [o["veri"] for o in olaylar if o["tur"] == "hata"]
    python = kok / "venv" / "bin" / "python"
    yokla = [python, "-I", "-c", "import importlib.util as u; print(u.find_spec('llama_cpp') is not None)"]
    g["llama_cpp_var"] = calistir(yokla).stdout.strip()
    try:
        kayit = json.loads((kok / "durum" / "kurulum.json").read_text(encoding="utf-8"))
        g["kayit"] = (kayit.get("bilesenler") or {}).get("gguf")
    except (OSError, ValueError):
        g["kayit"] = None
    kod, kr = api.istek("POST", "/api/kaldir", {"bilesen": "gguf"})
    g["kaldir_http"] = kod
    g["kaldir"] = kr
    g["llama_cpp_var_sonra"] = calistir(yokla).stdout.strip()
    (cikti / "gguf_olaylari.jsonl").write_text(
        "".join(json.dumps({"t": round(o["t"] - t0, 2), "tur": o["tur"], "veri": o["veri"]},
                           ensure_ascii=False, default=str) + "\n" for o in olaylar), encoding="utf-8")
    return g


class Yonetici:
    """Kabuksuz yönetici: kabuğun komut satırıyla aynı (TASARIM B.3), ilk satır = adres."""

    def __init__(self, python: Path, kok: Path, ev: Path, tmp: Path, gunluk_dizini: Path):
        uygulama = kok / "app" / "guncel"
        ortam = {"HOME": str(ev), "CFFIXED_USER_HOME": str(ev), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                 "TMPDIR": str(tmp), "KITAPCEVIR_GUNLUK_DIZINI": str(gunluk_dizini)}
        self.surec = subprocess.Popen(
            [str(python), "-I", "-X", "utf8", str(uygulama / "packaging" / "yonetici" / "yonetici.py"),
             "--kok", str(kok), "--port", "0"], cwd=str(uygulama), env=ortam,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        assert self.surec.stdout is not None
        ilk = json.loads(self.surec.stdout.readline())
        self.url = ilk["url"]
        threading.Thread(target=lambda: self.surec.stdout.read(), daemon=True).start()


def kesinti_akisi(kok: Path, python: Path, ev: Path, tmp: Path, gunluk_dizini: Path
                  ) -> Dict[str, Any]:
    """H.3 (b): gerçek ağda büyük öğe inerken yönetici ``kill -9``; yeniden açılışta
    ``.part`` korunmuş mu, indirme kaldığı yerden mi sürüyor (ilk ilerleme baytı)?"""
    hedef_bayt = 300_000_000   # bu kadar indikten sonra kes
    y = Yonetici(python, kok, ev, tmp, gunluk_dizini)
    api = Api(y.url)
    api.istek("POST", "/api/lisans", {"bilesen": "pdf", "kabul": True})
    _, plan = api.istek("POST", "/api/plan", {"tex": "tex-hafif", "istege_bagli": []})
    olaylar: List[Dict[str, Any]] = []
    dur = threading.Event()
    api.olay_dinle(olaylar, dur)
    time.sleep(0.5)
    api.istek("POST", "/api/kur", {"plan_kimligi": plan["plan_kimligi"]})
    t0 = time.monotonic()
    genel = 0
    while time.monotonic() - t0 < 1800:
        genel = max([o["veri"].get("genel_bayt", 0) for o in olaylar
                     if o["tur"] == "ilerleme" and isinstance(o["veri"], dict)] or [0])
        if genel >= hedef_bayt:
            break
        time.sleep(0.5)
    os.kill(y.surec.pid, signal.SIGKILL)
    y.surec.wait()
    dur.set()
    # .part adı: "<öğe kimliği>.<SHA öneki>.part" (indirici); öğe kimliğine göre boy
    parcalar = {p.name.rsplit(".", 2)[0]: p.stat().st_size
                for p in (kok / "indirilen").rglob("*.part")}
    sonuc: Dict[str, Any] = {"kesilen_genel_bayt": genel, "kill9_sn": round(time.monotonic() - t0, 1),
                             "part_sayisi": len(parcalar), "part_bayt": sum(parcalar.values()),
                             "part_oncesi": parcalar}
    time.sleep(1)
    y2 = Yonetici(python, kok, ev, tmp, gunluk_dizini)
    api2 = Api(y2.url)
    _, plan2 = api2.istek("POST", "/api/plan", {"tex": "tex-hafif", "istege_bagli": []})
    sonuc["ikinci_plan_indirme_bayt"] = plan2.get("indirme_bayt")
    olaylar2: List[Dict[str, Any]] = []
    dur2 = threading.Event()
    api2.olay_dinle(olaylar2, dur2)
    time.sleep(0.5)
    sonuc["ikinci_kur_http"] = api2.istek("POST", "/api/kur", {"plan_kimligi": plan2["plan_kimligi"]})[0]
    time.sleep(20)                              # sürdürülen öğelerin ilk ilerlemesi gelsin
    ilk_bayt: Dict[str, int] = {}
    for o in olaylar2:
        v = o["veri"] if isinstance(o["veri"], dict) else {}
        if o["tur"] == "ilerleme" and isinstance(v.get("bayt"), int):
            ilk_bayt.setdefault(str(v.get("oge")), v["bayt"])
    # Sürdürme kanıtı: kesilen öğelerin ikinci koşudaki İLK ilerleme baytı ≥ .part boyu
    # (baştan inseydi küçük bir sayıdan başlardı).
    sonuc["surdurulen"] = {k: {"part_bayt": v, "ikinci_kosu_ilk_ilerleme": ilk_bayt.get(k),
                               "kaldigi_yerden": (ilk_bayt.get(k) or 0) >= v}
                           for k, v in parcalar.items()}
    sonuc["iptal_http"] = api2.istek("POST", "/api/iptal", {})[0]
    time.sleep(3)
    sonuc["kapat_http"] = api2.istek("POST", "/api/kapat", {})[0]
    try:
        y2.surec.wait(timeout=KAPANIS_BEKLEME_SN)
    except subprocess.TimeoutExpired:
        y2.surec.kill()
    dur2.set()
    sonuc["kalan_part_bayt"] = sum(p.stat().st_size for p in (kok / "indirilen").rglob("*.part"))
    return sonuc


if __name__ == "__main__":
    sys.exit(main())
