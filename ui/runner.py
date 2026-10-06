"""Arayüzün pipeline koşucusu.

Arayüz iş mantığı kopyalamaz (2026-07-30 kararı): her iş komut satırı
kapılarından (1_cikar.py, 2_derle.py, translate_book.py) ayrı bir süreçte
koşar, yani arayüz ile CLI tek davranış kaynağını paylaşır. İlerleme iki
gerçek kaynaktan okunur:

  1. KITAPCEVIR_EVENT_LOG dosyası: faz / QC / tur olayları (core/events.py);
     yerel model çevirisinin sayacı ("[BATCH] Translated 40/472 segments")
     da buradan gelir (F5-07).
  2. stdout: çıkarma ve çeviri sayaçları ("Batch: pages 40-59 / 152",
     "[BATCH] Translated 40/472 segments", "Segment 3/40"). Çıkarma sayaçları
     logging'den değil print'ten geldiği için olay dosyasında yoktur.
     Toplamsız eski biçim ("Batch: pages 40-59") de okunur; o zaman oran
     hesaplanmaz.

Değeri olmayan gösterge üretilmez: oran yalnız gerçek bir sayaç (current /
total) varken hesaplanır, yoksa None döner ve arayüz çubuk çizmez.

Bu modül Streamlit'e bağlı değildir; düz Python olarak test edilir.
"""

from __future__ import annotations

import atexit
import functools
import importlib
import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.events import EVENT_LOG_ENV, classify  # noqa: E402
# Yerel model katmanının sözleşmesi (hafif modül: yalnız stdlib + config).
from core.mt_engines.contracts import (  # noqa: E402
    MSG_BUILTIN_MISSING,
    MT_SELECTION_FILENAME,
    DiscoveredModel,
    DiscoveryReport,
    EngineCheck,
    SourceStatus,
)
# --mt-engine verilmediğinde kullanılan motor: tek kaynak registry.
from core.mt_engines.registry import DEFAULT_ENGINE  # noqa: E402
# Yerel model koşusunun kaynak dil kapısı çıkış kodu (4): kitabın dili
# desteklenmiyor (2026-09-28, KARARLAR 14; eski adı EXIT_SOURCE_NOT_ENGLISH,
# aynı değer — eski içe aktarımlar kırılmasın diye o ad da buradan geçer).
# Değer CLI'da tanımlıdır; arayüz onu buradan okur, kendi kopyasını tutmaz.
from translate_book import EXIT_SOURCE_NOT_ENGLISH  # noqa: E402,F401
from translate_book import EXIT_SOURCE_UNSUPPORTED  # noqa: E402,F401
# Çıkış 5: seçili model kullanılamıyor (motor bilinmiyor, sunucu kapalı, model
# sunucuda yok, dosya/klasör yok, koşu sırasında bağlantı koptu).
from translate_book import EXIT_MODEL_UNAVAILABLE  # noqa: E402,F401
from ui import branding  # noqa: E402
# Çıkış 2 hem derleme hatası hem Yol A kilidi (klasörde "PDF'den TeX çıkar"
# çevirisi var, yerel model onu ezmemek için durdu). CLI iki durumu aynı
# kodla bildirir; arayüz ayrımı kilit dosyasının varlığından yapar
# (manual_lock). Kod ve işaret dosyasının adı CLI'dan okunur.
from translate_book import EXIT_MANUAL_LOCK, MANUAL_MARKER_NAME  # noqa: E402,F401
# V2 denetimi (F3-07): üretim sınırına dayanan parçaların listesi (tek kaynak ad).
from translate_book import TRUNCATION_REPORT_NAME  # noqa: E402
# Kitap klasöründeki ayrıntılı günlüğün adı (CLI'ın "Ayrıntı için
# pipeline.log'a bak" dediği dosya); arayüz hata iletisinde yolunu gösterir.
from translate_book import PIPELINE_LOG_NAME  # noqa: E402,F401
# assistant_prompt: kitaba özel talimatı yapay zekâ asistanına veren istem;
# 1_cikar.py da aynısını basar (tek kaynak, S6).
# meta_body: kimlik başlığı atılmış .tex gövdesi (1_cikar'ın export_changed
# kuralı); çevrilmemiş kopya denetimi aynı kuralı kullanır.
from yol_a_ortak import (  # noqa: E402,F401
    assistant_prompt,
    meta_body,
    read_sidecar,
    sha1_of_file,
)
# Ekran biçimleri (Türkçe ondalık, yüzde, dosya boyutu) ui/bicim.py'dedir
# (TASARIM F12: yalnız stdlib, paketli kurulumun yöneticisi de kullanabilir).
# Adlar buradan da dışa verilir; app.py ve testler runner.format_size vb. okur.
from ui.bicim import SIZE_UNITS, format_decimal, format_percent, format_size  # noqa: E402,F401
# Veri kökü (TASARIM F1): kitap klasörleri, çeviri belleği ve git dışı ayar
# dosyaları KITAPCEVIR_VERI_KOKU'dan (yoksa PROJECT_ROOT'tan) türer. Betikler
# ve alt süreçlerin çalışma klasörü her iki kipte de kod kökündedir.
from config.paths import (  # noqa: E402
    BELLEK_DOSYASI,
    BELLEK_KLASORU,
    ayar_koku,
    bellek_yolu,
    cikti_koku,
    paketli_kip,
)

# 2_derle.py / translate_book.py çıkış kodu 3: PDF üretildi, QC eşiğin altında.
EXIT_QC_BELOW_THRESHOLD = 3
# 2_derle.py / translate_book.py çıkış kodu 2: XeLaTeX derlemesi başarısız.
# translate_book'ta aynı kod Yol A kilidi de olabilir (EXIT_MANUAL_LOCK);
# ayrımı arayüz kilit dosyasından yapar (manual_lock).
EXIT_COMPILE_FAILED = 2
# argparse'ın komut satırı hatası çıkış kodu (Python standardı); CLI'larda
# derleme hatasıyla aynı değer, ayrımı usage_error yapar (F5-06).
EXIT_USAGE_ERROR = 2

# Başarısız bir koşuda kullanıcıya gösterilecek son çıktı satırı sayısı.
TAIL_LINES = 40
# Koşunun olay dosyası dizininin öneki ve dosyanın adı; dizin koşu bitince
# silinir.
EVENT_DIR_PREFIX = "kitapcevir-olay-"
EVENT_FILE_NAME = "events.jsonl"
# İptalde SIGTERM'den sonra SIGKILL'e geçmeden önce beklenen süre (sn).
CANCEL_GRACE_SEC = 10

# tqdm çubukları ("Recognizing Text:  42%|####| 64/152 [...]") hata kuyruğuna
# alınmaz: yüzlercesi gelir ve asıl hata satırını kuyruktan iterdi.
_TQDM_LINE = re.compile(r"\d+%\|.*\|\s*\d+/\d+")


def platform_problem() -> str | None:
    """Bu makinede çalışılamıyorsa nedenini döndür; çalışılabiliyorsa None.

    Uygulama yalnız Apple Silicon Mac'i hedefler: yerel çeviri modeli MLX ile,
    çıkarımın hızlandırıcısı MPS bu donanıma bağlı. Rosetta altında koşan x86
    Python da ``machine() == "x86_64"`` döndürür ve burada yakalanır.
    """
    system = platform.system()
    if system != "Darwin":
        return f"Kitapçevir yalnızca macOS'ta çalışır (bu sistem: {system})."
    machine = platform.machine()
    if machine != "arm64":
        return (
            "Kitapçevir Apple Silicon (M serisi) bir Mac ister "
            f"(bu işlemci: {machine})."
        )
    return None


# ---------------------------------------------------------------------------
# Komutlar ve çıktı yolları -- CLI'ın kendi fonksiyonlarıyla hesaplanır
# ---------------------------------------------------------------------------

# 1_cikar.py bayrağı: mevcut full_book.md yok sayılır, çıkarım baştan yapılır.
FRESH_EXTRACT_FLAG = "--yeniden"
# translate_book.py bayrağı: çeviri motoru (model kimliği).
MT_ENGINE_FLAG = "--mt-engine"
# 1_cikar.py ve translate_book.py bayrağı: çıktı klasörünün adı (Kitap adı).
NAME_FLAG = "--name"
# translate_book.py bayrağı: kaynak dili elle ver (KARARLAR 19; dil kapısı
# ölçüm yapmaz, dil o olur). Değerler core.lang_detect.TEX_SOURCE_LANGS.
SOURCE_LANG_FLAG = "--kaynak-dil"
# 1_cikar.py --json "error" kodu: çıktı klasöründe aynı adla BAŞKA kitap var.
EXTRACT_ERROR_NAME_CONFLICT = "name_conflict"


def _flag_value(flag: str, value: str) -> list[str]:
    """Bayrak ve değeri: değer '-' ile başlıyorsa ``--bayrak=<değer>``.

    argparse '-' ile başlayan ayrı argümanı bayrak sanıyor: "-Kuantum" Kitap
    adıyla CLI çıkış 2 veriyordu, arayüz bunu derleme hatası sanıyordu
    (F5-06, 2026-09-29). Öteki değerlerde komut eskisi gibi iki argümandır.
    """
    if value.startswith("-"):
        return [f"{flag}={value}"]
    return [flag, value]


def extract_command(book: Path, name: str | None = None, *,
                    fresh: bool = False) -> list[str]:
    """TeX yolu, adım 1: kitaptan çevrilecek EXPORT.tex'i çıkar.

    ``fresh=True`` önceki çıkarımı (full_book.md) kullanmadan baştan çıkarır.
    """
    command = [sys.executable, str(PROJECT_ROOT / "1_cikar.py"), str(book), "--json"]
    if name:
        command += _flag_value(NAME_FLAG, name)
    if fresh:
        command.append(FRESH_EXTRACT_FLAG)
    return command


def compile_command(translated_tex: Path) -> list[str]:
    """TeX yolu, adım 2: çevrilmiş EXPORT_TR.tex'i dizip PDF üret."""
    return [sys.executable, str(PROJECT_ROOT / "2_derle.py"), str(translated_tex), "--json"]


def translate_command(book: Path, name: str | None = None,
                      engine: str | None = None,
                      source_lang: str | None = None) -> list[str]:
    """Yerel model: kitabı baştan sona çevir.

    *engine* (model seçicinin kimliği) varsayılan motordan farklıysa sona
    ``--mt-engine <kimlik>`` eklenir; varsayılan motorda komut değişmez.
    Kimlik '-' ile başlıyorsa ``--mt-engine=<kimlik>`` yazılır (argparse onu
    bayrak sanmasın). *source_lang* (kullanıcının elle seçtiği kaynak dil,
    KARARLAR 19) verilirse sona ``--kaynak-dil <kod>`` eklenir; verilmezse
    komut değişmez ve dili CLI'ın kendi dil kapısı ölçer.
    """
    command = [sys.executable, str(PROJECT_ROOT / "translate_book.py"), str(book)]
    if name:
        command += _flag_value(NAME_FLAG, name)
    if engine and engine != DEFAULT_ENGINE:
        command += _flag_value(MT_ENGINE_FLAG, engine)
    if source_lang:
        command += _flag_value(SOURCE_LANG_FLAG, source_lang)
    return command


def book_slug(book: Path, name: str | None = None) -> str:
    """CLI'ın çıktı dizinine verdiği ad (translate_book'un fonksiyonlarıyla)."""
    from translate_book import guess_book_name, slugify

    return slugify(name or guess_book_name(str(book)))


# Kaynak kitabın çıktı klasörüne kopyası (1_cikar.py ve translate_book.py
# aynen kopyalar); klasörün hangi kitaba ait olduğunu sidecar yoksa bu söyler.
SOURCE_COPY_NAMES = ("source_book.pdf", "source_book.epub")


@dataclass(frozen=True)
class TexFiles:
    """TeX yolunun bir kitap için kullandığı dosyalar (1_cikar.py ile aynı adlar)."""

    output_dir: Path
    export_tex: Path
    export_tr_tex: Path
    directive: Path
    # 2_derle.py'nin klasör köküne koyduğu Türkçe PDF (<slug>_TR.pdf).
    translated_pdf: Path
    # Klasörde yapılmış "PDF'den TeX çıkar" çevirisinin kalıcı işareti.
    manual_marker: Path


def tex_files(book: Path, name: str | None = None) -> TexFiles:
    slug = book_slug(book, name)
    output_dir = cikti_koku(PROJECT_ROOT) / slug
    return TexFiles(
        output_dir=output_dir,
        export_tex=output_dir / "EXPORT.tex",
        export_tr_tex=output_dir / "EXPORT_TR.tex",
        directive=output_dir / "CEVIRI_TALIMATI.md",
        translated_pdf=output_dir / f"{slug}_TR.pdf",
        manual_marker=output_dir / "source" / MANUAL_MARKER_NAME,
    )


def manual_lock(book: Path, name: str | None = None) -> bool:
    """Klasörde "PDF'den TeX çıkar" çevirisi var mı (yerel model onu ezmez)."""
    return tex_files(book, name).manual_marker.exists()


# ---------------------------------------------------------------------------
# Klasör kimliği: aynı adla başka bir kitap mı çıkarılmış?
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=32)
def _cached_sha1(path: str, size: int, mtime_ns: int) -> str:
    return sha1_of_file(path)


def file_sha1(path: Path) -> str:
    """Dosyanın SHA-1'i; dosya değişmedikçe (boyut + mtime) yeniden okunmaz.

    Arayüz kartı birkaç saniyede bir yeniden çizilir; yüzlerce MB'lık kitabı
    her seferinde okumamak için önbellek dosyanın kimliğine bağlıdır.
    """
    stat = path.stat()
    return _cached_sha1(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def folder_source_sha1s(output_dir: Path) -> set[str]:
    """Çıktı klasörünün hangi kitaba ait olduğunu söyleyen SHA-1'ler.

    1_cikar.py'nin kuralı: önce kitap.json'daki ``source_sha1``; yoksa klasöre
    kopyalanmış kaynak kitap (source_book.pdf / .epub). Kimlik yoksa boş küme.
    """
    recorded = str(read_sidecar(output_dir).get("source_sha1") or "").strip()
    if recorded:
        return {recorded}
    hashes = set()
    for copy_name in SOURCE_COPY_NAMES:
        copy = output_dir / copy_name
        if copy.is_file():
            try:
                hashes.add(file_sha1(copy))
            except OSError:
                continue
    return hashes


def name_conflict(book: Path, name: str | None = None) -> bool:
    """Bu adın klasöründe BAŞKA bir kitap mı var (SHA-1 uyuşmuyor)?

    Klasör yoksa ya da kimliği bilinmiyorsa çakışma sayılmaz (1_cikar.py de
    öyle geçer). Kitabın kendisi okunamıyorsa da çakışma denmez: karar CLI'da.
    """
    output_dir = tex_files(book, name).output_dir
    if not output_dir.is_dir():
        return False
    theirs = folder_source_sha1s(output_dir)
    if not theirs:
        return False
    try:
        return file_sha1(book) not in theirs
    except OSError:
        return False


@functools.lru_cache(maxsize=8)
def _tex_body(path: str, size: int, mtime_ns: int) -> str:
    """.tex dosyasının kimlik başlığı (% KITAPCEVIR-META) atılmış gövdesi.

    BOM ve CRLF satır sonları gövdeyi değiştirmez: editör bunları kendiliğinden
    ekleyebilir, çeviri yapılmış sayılmaz.
    """
    text = Path(path).read_bytes().decode("utf-8-sig", errors="replace")
    return meta_body(text.replace("\r\n", "\n"))


def _cached_body(path: Path) -> str:
    stat = path.stat()
    return _tex_body(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def untranslated_copy(export_tex: Path, export_tr_tex: Path) -> bool:
    """EXPORT_TR.tex'in gövdesi EXPORT.tex'inkiyle BİREBİR aynı mı?

    Kullanıcı EXPORT.tex'i yeni adla kaydedip çevirmeden bırakınca kart
    "hazır; derleyebilirsin" diyordu (U7). Karşılaştırma kimlik başlığını
    dışarıda bırakır (1_cikar.py'nin export_changed kuralı, meta_body):
    yeniden çıkarımda başlıktaki ``created`` değişir, gövde aynı kalır; o
    durumda da dosya çevrilmemiştir. Kart birkaç saniyede bir yeniden
    çizildiği için gövdeler dosyanın kimliğine (boyut + mtime) bağlı
    önbellekten gelir. Dosya okunamıyorsa False (uyarı uydurulmaz).
    """
    try:
        return _cached_body(export_tex) == _cached_body(export_tr_tex)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Ekran biçimleri (Streamlit'siz; arayüz ve testler buradan okur). Sayı,
# yüzde ve boyut biçimleri ui/bicim.py'de (yukarıda içe aktarıldı, F12).
# ---------------------------------------------------------------------------

def suffix_list_tr(suffixes: tuple[str, ...] | list[str]) -> str:
    """Dosya uzantıları Türkçe sıralamayla: ".pdf", ".epub" -> "PDF ya da EPUB"."""
    names = [suffix.lstrip(".").upper() for suffix in suffixes]
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} ya da {names[-1]}"


def model_pdf(book: Path, name: str | None = None) -> Path:
    """Yerel modelin ürettiği Türkçe PDF'nin yolu (translate_book derleme adımı)."""
    slug = book_slug(book, name)
    return cikti_koku(PROJECT_ROOT) / slug / "compiled" / f"{slug}_TR.pdf"


def local_model_dir() -> Path | None:
    """Varsayılan çeviri motorunun yerel model dizini; kurulu değilse None.

    Model kökü çağrı anında ``registry.MODELS_DIR``'dan okunur (motor
    katmanının tek kaynağı; testler onu yalıtır).
    """
    from core.mt_engines import registry

    return registry.create_engine(DEFAULT_ENGINE).resolve_local_model_dir(
        registry.MODELS_DIR)


# ---------------------------------------------------------------------------
# Çeviri modeli seçimi ("Yerel model ile çevir" kartı)
# ---------------------------------------------------------------------------
# Keşif ve ön kontrol core/mt_engines/discovery.py'dedir; buradaki
# fonksiyonlar onu çağıran ince sarmalayıcılardır (arayüz iş mantığı
# kopyalamaz). Modül çağrı anında içe aktarılır: keşfin ağ ve dosya
# sistemi yükü uygulama açılırken değil, tarama istenince gelir.

DISCOVERY_MODULE = "core.mt_engines.discovery"
# Kurulum rehberi (varsayılan model kurulu değilken kartta anılır).
SETUP_GUIDE_RELPATH = Path("docs") / "KURULUM_VE_KULLANIM.md"
# Seçili kimlik son taramada yok (sunucu kapandı, dosya silindi…).
ENGINE_NOT_SCANNED_REASON = (
    "Seçili model son taramada bulunamadı; güncel liste için "
    f"{branding.quoted(branding.MODEL_SCAN_BUTTON)} düğmesini kullan."
)
# Keşif modeli kullanılamaz saydı ama neden yazmadı (sözleşme dışı durum).
UNUSABLE_NO_REASON = "Bu model kullanılamıyor (tarama neden bildirmedi)."


def _discovery():
    return importlib.import_module(DISCOVERY_MODULE)


def discover_models() -> DiscoveryReport:
    """Bilgisayardaki modelleri tara (discovery.discover_all).

    Yalnız gerçekten bulunan modeller döner; sunucu kapalıysa o sunucunun
    modeli listelenmez, durumu ``report.sources``'ta yazar.
    """
    return _discovery().discover_all()


def engine_check(engine_id: str) -> EngineCheck:
    """"Çevir" öncesi son kontrol (discovery.check_engine); modeli yüklemez."""
    return _discovery().check_engine(engine_id)


def engine_choice_path() -> Path:
    """Arayüzün son model seçiminin dosyası (git dışı, .gitignore).

    Yalnız arayüzün başlattığı komuta ``--mt-engine`` olarak geçer; komut
    satırı bu dosyayı okumaz (CLI davranışı dosya durumuna bağlı olmasın).
    Veri kökünün ``config/`` klasöründedir (paketli kipte ``<kök>/veri/config``).
    """
    return ayar_koku(PROJECT_ROOT) / MT_SELECTION_FILENAME


def load_engine_choice() -> str | None:
    """Kaydedilmiş model kimliği; dosya yok, bozuk ya da boşsa None.

    Kimlik burada doğrulanmaz: arayüz onu yalnız son taramada bulunursa
    seçer (bulunmazsa varsayılan motora düşer).
    """
    try:
        # V2 denetimi (F4-14 ile aynı kök; M kulvarı notu): elle düzenlenip
        # BOM'la kaydedilmiş dosya da okunur (eskiden seçim sessizce düşüyordu).
        data = json.loads(engine_choice_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    engine = data.get("engine") if isinstance(data, dict) else None
    if isinstance(engine, str) and engine.strip():
        return engine.strip()
    return None


def save_engine_choice(engine_id: str) -> bool:
    """Seçimi atomik yaz (geçici dosya + os.replace); yazılamazsa False.

    Yarım yazılmış dosya kalmaz: yazım başarısız olursa eski dosya aynen
    durur, geçici dosya silinir.
    """
    path = engine_choice_path()
    payload = {"engine": engine_id,
               "saved_at": datetime.now().isoformat(timespec="seconds")}
    temp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp",
                                             dir=path.parent)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        temp_name = None
        return True
    except OSError:
        return False
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _builtin_missing_reason() -> str:
    """Varsayılan model kurulu değil: beklenen klasör ve nereden kurulacağı.

    Geliştirici kipinde kurulum rehberi (docs/…) anılır. Paketli kipte
    (DMG; config.paths.paketli_kip) rehber geliştirici kurulumunu anlatır,
    model uygulamanın "Bileşenler" ekranından indirilir (TASARIM F5).
    """
    from core.mt_engines import registry

    spec = registry.create_engine(DEFAULT_ENGINE).spec
    expected = registry.MODELS_DIR / (spec.local_dir or spec.name)
    if paketli_kip():
        hint = branding.MODEL_INSTALL_HINT_PACKAGED
    else:
        hint = f"Kurulum rehberi: {SETUP_GUIDE_RELPATH.as_posix()}."
    return f"{MSG_BUILTIN_MISSING.format(path=expected)} {hint}"


# ---------------------------------------------------------------------------
# Donanım kapısı (KARARLAR 17, 17a): core/hardware.py'nin ince sarmalayıcıları.
# Ölçüm süreç başına bir kez yapılır (önbellek); ağ yok. Arayüz iş mantığı
# kopyalamaz: CLI (translate_book) ve keşif aynı fonksiyonları kullanır.
# ---------------------------------------------------------------------------

def hardware_gate():
    """Genel kapı: bu Mac referans 4 bit 7B'yi çalıştırabiliyor mu
    (``core.hardware.general_gate``; ``allowed`` / ``reason`` / ``warning``)."""
    from core import hardware

    return hardware.general_gate()


def hardware_caption() -> str:
    """Kenar çubuğunun donanım satırı (gerçek ölçüm; ölçülemediyse söyler)."""
    from core import hardware

    return hardware.hardware_caption()


def model_memory(model: DiscoveredModel | None):
    """Keşif kaydının bellek hükmü (``MemoryFit``); tahmin yoksa None."""
    return getattr(model, "memory", None) if model is not None else None


def memory_batch_note(engine_id: str, report: DiscoveryReport | None) -> str:
    """Seçili modelde toplu iş belleğe göre küçülecekse not; yoksa ""."""
    from core import hardware

    model = report.model_by_id(engine_id) if report is not None else None
    return hardware.batch_note(model_memory(model))


def engine_ready(engine_id: str,
                 report: DiscoveryReport | None) -> tuple[bool, str]:
    """Seçili model çevirmeye hazır mı; değilse Türkçe neden.

    Önce donanım kapısı: bu Mac yerel model için yetersizse hiçbir model
    hazır değildir. Varsayılan motor için yerel model klasörüne bakılır
    (local_model_dir; ağ yok) ve son tarama modelin bu Mac'e sığmadığını
    söylüyorsa hazır sayılmaz. Diğerleri için son taramanın ``usable`` /
    ``reason`` alanı kullanılır; taramada yoksa hazır sayılmaz.
    """
    gate = hardware_gate()
    if not gate.allowed:
        return False, gate.reason
    model = report.model_by_id(engine_id) if report is not None else None
    if engine_id == DEFAULT_ENGINE:
        if local_model_dir() is not None:
            fit = model_memory(model)
            if fit is not None and fit.fits is False:
                from core import hardware

                return False, hardware.too_big_message(fit)
            return True, ""
        if model is not None and model.reason:
            return False, model.reason
        return False, _builtin_missing_reason()
    if model is None:
        return False, ENGINE_NOT_SCANNED_REASON
    if model.usable:
        return True, ""
    return False, model.reason or UNUSABLE_NO_REASON


def model_option_label(model: DiscoveredModel) -> str:
    """Seçicideki etiket: ad · kaynak · (ayrıntı) · (boyut) [· kullanılamaz].

    Ayrıntı ve boyut yalnız keşfin gerçek üst verisinden; yoksa yazılmaz.
    Model bu Mac'e sığmıyorsa (donanım kapısı) "kullanılamaz" yerine
    "bu Mac'e sığmaz (gerekli ~X GB, kullanılabilir Y GB)" yazar.
    """
    parts = [model.label or model.engine_id,
             branding.SOURCE_LABELS.get(model.source, model.source)]
    if model.detail:
        parts.append(model.detail)
    size = model.size_bytes
    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
        parts.append(format_size(size))
    fit = model_memory(model)
    if fit is not None and fit.fits is False:
        from core import hardware

        parts.append(hardware.too_big_label(fit))
    elif not model.usable:
        parts.append(branding.MODEL_UNUSABLE_LABEL)
    return " · ".join(parts)


def engine_label(engine_id: str, report: DiscoveryReport | None) -> str:
    """Kimliğin ekrandaki sade adı (son taramadan); taramada yoksa kimlik."""
    model = report.model_by_id(engine_id) if report is not None else None
    return model.label if model is not None and model.label else engine_id


# Makine çevirisi fazının günlük başlığındaki kaynak dil kodu
# ("MT Translation (IT -> TR, engine=…)").
_PHASE_SOURCE_LANG = re.compile(r"\(\s*([A-Za-z]{2,3})\s*-+>\s*TR\b")


def phase_label(number: str, title: str) -> str:
    """Faz satırının Türkçe başlığı (branding.PHASE_TITLES; F5-08).

    Bilinmeyen fazda günlükteki başlık aynen döner (bilgi düşmez). Makine
    çevirisi fazında başlıktaki kaynak dil Türkçe adıyla eklenir.
    """
    from core.lang_detect import language_name_tr

    key = str(number).strip()
    label = branding.PHASE_TITLES.get(key)
    if label is None:
        return title
    if key == branding.PHASE_TRANSLATION:
        match = _PHASE_SOURCE_LANG.search(title or "")
        if match:
            code = match.group(1).lower()
            name = language_name_tr(code)
            if name != code:
                label = f"{label} ({name} → Türkçe)"
    return label


# ---------------------------------------------------------------------------
# Kaynak dil ("Yerel model ile çevir" kartı, KARARLAR 19)
# ---------------------------------------------------------------------------
# Ölçüm translate_book'un dil ÖN kapısıyla aynı işlevlerle yapılır:
# _pdf_text_sample / _epub_text_sample (gömülü metin; OCR ve çıkarım yok) +
# core.lang_detect.analyze_language(clean_plain_text_for_detection(...)).
# Ölçülen değer gerçektir; ölçülemediyse (metin katmanı yok) bu söylenir,
# tahmin uydurulmaz. Güven eşiği CLI'ınkiyle aynı (GATE_STOP_CONFIDENCE).


@dataclass(frozen=True)
class SourceLanguage:
    """Kitabın kaynak dili ölçümü (kartta gösterilir, seçiciyi doldurur)."""

    code: str | None            # tespit ("de", "und", "cjk"…); ölçüm düştüyse None
    confidence: float           # 0-1
    letters: int                # ölçülen harf sayısı
    # Çıkarım gerektirmeden ölçülebildi mi: dedektörün en az harf sayısına
    # (DETECT_MIN_LETTERS) ulaşan gömülü metin var mı. Yoksa (taranmış PDF,
    # boş kitap) dil kapısı çıkarımdan sonra CLI'da ölçer.
    measured: bool
    error: str | None = None

    @property
    def supported(self) -> bool:
        from core.lang_detect import TEX_SOURCE_LANGS

        return self.code in TEX_SOURCE_LANGS

    @property
    def confident(self) -> bool:
        """Desteklenen dil, güven eşiğin (CLI kapısıyla aynı) üstünde."""
        from core.lang_detect import GATE_STOP_CONFIDENCE

        return (self.measured and self.supported
                and self.confidence >= GATE_STOP_CONFIDENCE)

    @property
    def needs_choice(self) -> bool:
        """Ölçüldü ama sonuç belirsiz ya da desteklenmiyor: "Çevir"den önce
        kullanıcı dili seçmeli (KARARLAR 19)."""
        return self.measured and not self.confident


@functools.lru_cache(maxsize=16)
def _measure_source_language(path: str, size: int, mtime_ns: int) -> SourceLanguage:
    from core.lang_detect import (
        DETECT_MIN_LETTERS,
        analyze_language,
        clean_plain_text_for_detection,
    )
    from translate_book import _epub_text_sample, _pdf_text_sample

    try:
        if path.lower().endswith(".epub"):
            text = _epub_text_sample(path)
        else:
            text = _pdf_text_sample(path)
        est = analyze_language(clean_plain_text_for_detection(text),
                               already_clean=True)
    except Exception as exc:  # ölçüm hatası kartı düşürmez; söylenir
        return SourceLanguage(None, 0.0, 0, measured=False,
                              error=str(exc) or type(exc).__name__)
    return SourceLanguage(est.code, float(est.confidence), int(est.letters),
                          measured=est.letters >= DETECT_MIN_LETTERS)


def measure_source_language(book: Path) -> SourceLanguage:
    """Kitabın kaynak dili (translate_book'un ön kapısıyla aynı ölçüm).

    Kart birkaç saniyede bir yeniden çizilir; ölçüm dosyanın kimliğine
    (boyut + mtime) bağlı önbellekten gelir.
    """
    try:
        stat = book.stat()
    except OSError as exc:
        return SourceLanguage(None, 0.0, 0, measured=False,
                              error=str(exc) or type(exc).__name__)
    return _measure_source_language(str(book.resolve()), stat.st_size,
                                    stat.st_mtime_ns)


def forced_source_lang(measured: SourceLanguage, chosen: str | None) -> str | None:
    """Komuta ``--kaynak-dil`` olarak geçecek dil; geçmeyecekse None.

    Kullanıcı tespit edilen (güvenli) dili değiştirmediyse bayrak eklenmez:
    CLI dili kendi kapısıyla aynı işlevle ölçer, komut eskisiyle aynı kalır
    (İngilizce yolu birebir). Tespit güvensizken ya da ölçülemezken yapılan
    seçim, ya da tespitten farklı seçim bayrakla geçer.
    """
    if not chosen:
        return None
    if measured.confident and chosen == measured.code:
        return None
    return chosen


# ---------------------------------------------------------------------------
# Koşu sonuçları: CLI çıktı dosyaları (2_derle'nin okuyucularıyla)
# ---------------------------------------------------------------------------
# 2_derle --json sonucunu şu alanlarla verir: qc_score/qc_report/qc_top (S4),
# tex_hatalari/eksik_glif (R5), epub/epub_note/epub_kapsam (S5/S7). Yerel model
# (translate_book) JSON basmaz; aynı alanlar bu koşuda yazılmış dosyalardan,
# 2_derle'nin KENDİ okuyucularıyla çıkarılır (arayüz iş mantığı kopyalamaz):
# _read_qc_summary, _read_compile_counts, _read_epub_report,
# _read_epub_skip_note, _compile_issue_line, _compile_log_path. Bu adlar
# değişirse tests/test_fix_U_20260929.py kırılır.

DERLE_MODULE = "2_derle"
# 1_cikar --json / kitap.json anahtarı (F1-03 sözleşmesi, X kulvarı): çıkarımda
# okunamayıp atlanan sayfalar, 1'den başlayan sayfa numaraları (int listesi).
FAILED_PAGES_KEY = "failed_pages"


def _derle():
    return importlib.import_module(DERLE_MODULE)


def compile_log_path(output_dir: Path) -> Path:
    """XeLaTeX günlüğünün gerçek yeri (2_derle._compile_log_path)."""
    return _derle()._compile_log_path(Path(output_dir))


def compile_issue_text(result: dict) -> str | None:
    """TeX hatası / eksik glif uyarısı, 2_derle'nin CLI satırıyla AYNI metin."""
    counts = {}
    for key in ("tex_hatalari", "eksik_glif"):
        value = result.get(key)
        counts[key] = (value if isinstance(value, int)
                       and not isinstance(value, bool) and value > 0 else 0)
    return _derle()._compile_issue_line(counts)


def brace_fix_text(output_dir: Path, since: float | None) -> str | None:
    """Çeviride kapanmamış '{' onarıldıysa 2_derle'nin CLI uyarısı (F2-01).

    X kulvarı (29 Eylül 2026) uyarıyı 2_derle konsol özetine koydu, --json alan
    kümesine eklemedi ("arayüz kulvarının işi"). Sayı bu koşunun book_tr.tex'inden
    2_derle'nin kendi okuyucusuyla (_read_brace_fixes) okunur, metin onun
    satırıdır (_brace_fix_line). 2_derle'de bu işlevler yoksa None (uydurulmaz).
    """
    derle = _derle()
    reader = getattr(derle, "_read_brace_fixes", None)
    line = getattr(derle, "_brace_fix_line", None)
    if reader is None or line is None:
        return None
    return line(reader(Path(output_dir), since or 0.0))


def epub_report(output_dir: Path, since: float | None) -> dict | None:
    """Bu koşuda yazılmış compiled/epub_rapor.json (S7); yoksa None."""
    return _derle()._read_epub_report(Path(output_dir), since or 0.0)


def epub_coverage_text(kapsam: Any, report: dict | None = None) -> str | None:
    """EPUB kapsamı 1'in altındaysa uyarı; 2_derle'nin CLI satırıyla aynı metin.

    ``UYARI: EPUB'da 1/108 blok kaynak dilde kaldı (kapsam %99,1).`` satırının
    "UYARI: " sonrası. Blok sayıları rapordan (varsa); kapsam 1 ya da
    geçersizse None.
    """
    if (isinstance(kapsam, bool) or not isinstance(kapsam, (int, float))
            or not 0 <= kapsam < 1):
        return None
    oran = f"%{kapsam * 100:.1f}".replace(".", ",")
    n_bad = report.get("cevrilmemis_blok") if report else None
    n_all = report.get("toplam_blok") if report else None
    blok = (f"{n_bad}/{n_all} blok kaynak dilde kaldı"
            if n_bad is not None and n_all is not None
            else "bazı bloklar kaynak dilde kaldı")
    return f"EPUB'da {blok} (kapsam {oran})."


def model_outputs(book: Path, name: str | None, since: float | None) -> dict:
    """Yerel model koşusunun çıktıları, 2_derle --json alanlarıyla.

    Yalnız bu koşuda (``since``'ten sonra) yazılmış dosyalar okunur: önceki
    koşudan kalan rapor/EPUB sonuç sayılmaz. Alanlar: output_dir, epub,
    epub_note, epub_kapsam, qc_score, qc_report, qc_top, tex_hatalari,
    eksik_glif, kesik_parca (üretim sınırına dayanan parça sayısı).
    """
    derle = _derle()
    moment = since or 0.0
    output_dir = tex_files(book, name).output_dir
    slug = book_slug(book, name)
    fields: dict[str, Any] = {"output_dir": str(output_dir)}
    fields.update(derle._read_qc_summary(output_dir, moment))
    fields.update(derle._read_compile_counts(output_dir, moment))
    epub = output_dir / "compiled" / f"{slug}_TR.epub"
    if derle._fresh_file(epub, moment):
        report = derle._read_epub_report(output_dir, moment)
        fields.update(epub=str(epub), epub_note=None,
                      epub_kapsam=report["kapsam"] if report else None)
    else:
        fields.update(epub=None, epub_kapsam=None,
                      epub_note=derle._read_epub_skip_note(output_dir, moment))
    fields["kesik_parca"] = truncated_count(output_dir, moment)
    return fields


def truncated_count(output_dir: Path, since: float | None) -> int:
    """Bu koşuda üretim sınırına dayanan parça sayısı (V2 denetimi, F3-07).

    translate_book'un TRUNCATION_REPORT_NAME listesinden; dosya yoksa, bu
    koşudan eskiyse ya da okunamıyorsa 0 (sayı uydurulmaz).
    """
    path = Path(output_dir) / TRUNCATION_REPORT_NAME
    if not _derle()._fresh_file(path, since or 0.0):
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return len([item for item in data if isinstance(item, dict)]) \
        if isinstance(data, list) else 0


def failed_pages(output_dir: Path, result: dict | None = None) -> list[int]:
    """Çıkarımda atlanan sayfalar (F1-03 sözleşmesi); yoksa boş liste.

    Önce koşunun --json sonucu, sonra klasörün kitap.json'u okunur. Liste
    değilse ya da içinde sayı olmayan öğe varsa o öğeler alınmaz (uyarı
    uydurulmaz).
    """
    for source in (result or {}, read_sidecar(Path(output_dir))):
        value = source.get(FAILED_PAGES_KEY) if isinstance(source, dict) else None
        if isinstance(value, list):
            pages = [page for page in value
                     if isinstance(page, int) and not isinstance(page, bool)]
            if pages:
                return sorted(set(pages))
    return []


# argparse kullanım hatası satırı: "translate_book.py: error: argument --name:
# expected one argument". CLI bu durumda 2 ile çıkar (derleme hatasının kodu).
_USAGE_ERROR_LINE = re.compile(r"^\S+\.py: error: (?P<message>.+)$")


def usage_error(run: "Run") -> str | None:
    """CLI komut satırını reddetti mi (argparse, çıkış 2); iletisini döndür.

    Çıkış 2 derleme hatası da olabilir; ayrım: JSON özeti yok ve kuyrukta
    argparse'ın "<betik>.py: error: …" satırı var (F5-06).
    """
    if run.returncode != EXIT_USAGE_ERROR or run.result:
        return None
    for line in reversed(run.tail()):
        match = _USAGE_ERROR_LINE.match(line.strip())
        if match:
            return match.group("message")
    return None


def open_path(path: Path, *, reveal: bool = False) -> None:
    """Dosyayı varsayılan uygulamasıyla, dizini Finder'da aç.

    ``reveal=True`` dosyayı açmak yerine Finder'da seçili gösterir.
    """
    args = ["open", "-R", str(path)] if reveal else ["open", str(path)]
    subprocess.run(args, check=False)


# ---------------------------------------------------------------------------
# Kitap yükleme (APP-B05: yol aşımı ve boyut sınırı)
# ---------------------------------------------------------------------------

BOOK_SUFFIXES = (".pdf", ".epub")
# Ekranda yüklenebilir türler ("PDF ya da EPUB"); liste BOOK_SUFFIXES'ten türer.
BOOK_TYPES_LABEL = suffix_list_tr(BOOK_SUFFIXES)
# Yüklenen kitabın geçici kopyasının dizin öneki. discard_upload yalnız bu
# önekli ve bu süreçte save_upload'ın açtığı dizinleri siler.
UPLOAD_DIR_PREFIX = "kitapcevir-yukleme-"
# Streamlit'in server.maxUploadSize "MB"ı 1024 x 1024 bayttır (yükleyici ve
# sunucu sınırı bu çarpanla uygular). Ekrana ondalık MB ile yazılır
# (format_size): 500 -> "524 MB", Finder'ın göstereceği sayı.
STREAMLIT_UPLOAD_MB = 1024 * 1024


def upload_file_types(suffixes: tuple[str, ...] = BOOK_SUFFIXES) -> list[str]:
    """st.file_uploader'ın ``type`` listesi: ".pdf" -> "pdf"."""
    return [suffix.lstrip(".") for suffix in suffixes]


def upload_limit_bytes(max_upload_mb: int | float) -> int:
    """server.maxUploadSize (Streamlit MB'ı) -> bayt: yükleyicinin gerçek sınırı."""
    return int(max_upload_mb * STREAMLIT_UPLOAD_MB)


def format_upload_limit(limit_bytes: int) -> str:
    """Yükleme sınırı ekranda tam sayı: 524 288 000 bayt -> "524 MB".

    Aşağı yuvarlanır: ekrandaki sınıra kadar olan her dosya gerçekten kabul
    edilir (yukarı yuvarlansa sınırın hemen altında görünen dosya reddedilirdi).
    """
    for unit, size in SIZE_UNITS:
        if limit_bytes >= size:
            return f"{limit_bytes // size} {unit}"
    return f"{limit_bytes} bayt"

# save_upload'ın açtığı, henüz silinmemiş dizinler. Streamlit oturumları ayrı
# iş parçacıklarında koştuğu için küme kilitle korunur.
_upload_dirs: set[Path] = set()
_upload_lock = threading.Lock()
_exit_handler_registered = False


def upload_problem(name: str, size: int, max_bytes: int) -> str | None:
    """Yüklenen dosya kabul edilemiyorsa nedenini döndür; ediliyorsa None.

    Ad, CLI kitap adını ondan çıkardığı için diske aynen yazılır; bu yüzden
    yol ayırıcı ya da '..' içeren ad hiç kabul edilmez.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return f"Geçersiz dosya adı: {name!r}"
    if Path(name).suffix.lower() not in BOOK_SUFFIXES:
        return f"Yalnızca {BOOK_TYPES_LABEL} yüklenebilir."
    if size > max_bytes:
        return (f"Dosya çok büyük ({format_size(size)}); "
                f"sınır {format_upload_limit(max_bytes)}.")
    return None


def save_upload(name: str, data: bytes, max_bytes: int, *,
                replaces: Path | None = None) -> Path:
    """Kitabı özgün adıyla yeni bir geçici dizine yaz; kabul edilmezse ValueError.

    *replaces* (iş yuvasındaki önceki kitap) verilirse onun geçici dizini
    yeni kopya yazıldıktan SONRA silinir: reddedilen yükleme eski kitabı
    götürmez. Çağıran, eski kitabın koşan bir işte kullanılmadığını garanti
    eder. Silinmeden kalan dizinler süreç kapanırken silinir (atexit).
    """
    global _exit_handler_registered
    problem = upload_problem(name, len(data), max_bytes)
    if problem:
        raise ValueError(problem)
    folder = Path(tempfile.mkdtemp(prefix=UPLOAD_DIR_PREFIX))
    with _upload_lock:
        _upload_dirs.add(folder)
        if not _exit_handler_registered:
            # Kapanış işleyicisi (_on_exit) önce koşan işleri durdurur, sonra
            # yükleme dizinlerini siler (F5-04): sıra tek fonksiyonda sabit.
            atexit.register(_on_exit)
            _exit_handler_registered = True
    path = folder / name
    try:
        path.write_bytes(data)
    except OSError:
        _remove_upload_dir(folder)
        raise
    if replaces is not None:
        discard_upload(replaces)
    return path


def discard_upload(book: Path | None) -> bool:
    """save_upload'ın açtığı geçici kitap dizinini sil; silindiyse True.

    Yalnız bu süreçte save_upload'ın açtığı dizin silinir. None, kullanıcının
    kendi dosyası, '..' içeren yol ya da elle açılmış benzer adlı dizin
    dokunulmadan False döner.
    """
    if book is None:
        return False
    return _remove_upload_dir(Path(book).parent)


def _remove_upload_dir(folder: Path) -> bool:
    with _upload_lock:
        if folder not in _upload_dirs:
            return False
        _upload_dirs.discard(folder)
    # Kayıt tek başına yetmez: dizin bu arada bağlantıyla değiştirilmişse
    # ya da yok olmuşsa hiçbir şey silinmez.
    if (not folder.name.startswith(UPLOAD_DIR_PREFIX)
            or folder.is_symlink() or not folder.is_dir()):
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def _cleanup_uploads() -> None:
    """Sunucu kapanırken kalan yükleme dizinlerini sil (atexit).

    Hâlâ koşan bir işin girdisi olan dizin silinmez (F5-04): kapanış
    işleyicisi işi önce durdurur, durduramadıysa iş girdisiz kalmasın.
    """
    with _upload_lock:
        folders = list(_upload_dirs)
    in_use = _folders_in_use()
    for folder in folders:
        if folder in in_use:
            continue
        _remove_upload_dir(folder)


# Önceki sunuculardan kalan yükleme dizini bu yaştan büyükse süpürülür (sn).
# atexit sunucu öldürülünce (kill -9, çökme) çalışmaz; dizin kalır.
UPLOAD_SWEEP_AGE_SEC = 24 * 60 * 60
# tempfile.mkdtemp'in ürettiği ad: önek + 8 karakter [a-z0-9_].
_UPLOAD_DIR_NAME = re.compile(re.escape(UPLOAD_DIR_PREFIX) + r"[a-z0-9_]{8}")


def _looks_like_our_upload(folder: Path) -> bool:
    """save_upload'ın açtığı biçimde mi: adı birebir, içinde tek kitap dosyası."""
    if not _UPLOAD_DIR_NAME.fullmatch(folder.name):
        return False
    if folder.is_symlink() or not folder.is_dir():
        return False
    try:
        if folder.stat().st_uid != os.getuid():
            return False
        entries = list(folder.iterdir())
    except OSError:
        return False
    if len(entries) > 1:
        return False
    return all(entry.is_file() and not entry.is_symlink()
               and entry.suffix.lower() in BOOK_SUFFIXES for entry in entries)


def sweep_stale_uploads(max_age_sec: float = UPLOAD_SWEEP_AGE_SEC, *,
                        root: Path | None = None,
                        now: float | None = None) -> list[Path]:
    """Önceki sunuculardan kalmış eski yükleme dizinlerini sil; silinenleri döndür.

    Yalnız save_upload'ın açtığı biçimdeki (önekli ad, içinde tek PDF/EPUB)
    ve *max_age_sec*'ten eski dizinler silinir. Bu süreçte açılmış dizinlere
    (iş yuvasındaki kitap) dokunulmaz.
    """
    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    moment = time.time() if now is None else now
    with _upload_lock:
        ours_now = set(_upload_dirs)
    removed: list[Path] = []
    try:
        candidates = list(base.glob(f"{UPLOAD_DIR_PREFIX}*"))
    except OSError:
        return removed
    for folder in candidates:
        if folder in ours_now or not _looks_like_our_upload(folder):
            continue
        try:
            age = moment - folder.stat().st_mtime
        except OSError:
            continue
        if age <= max_age_sec:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            removed.append(folder)
    return removed


# ---------------------------------------------------------------------------
# Araçlar sütunu: çeviri belleği ve sözlük (salt okunur)
# ---------------------------------------------------------------------------

# Çeviri belleğinin dosyası, veri köküne göre. Tek kaynak config/paths.py
# (bellek_yolu): core/memory.py'deki TranslationMemory()'nin varsayılan yolu ve
# core/translator.py'nin belleği de oradan türer; ikisinin ayrışmadığını
# tests/test_dalga_uic_20260925.py ve tests/test_K5_veri_koku.py denetler.
# Ad geriye uyum için durur.
MEMORY_DB_RELPATH = Path(BELLEK_KLASORU) / BELLEK_DOSYASI
# Bellekte metin arama: bundan kısa sorgu aranmaz (hemen her kayıtta geçer).
MEMORY_SEARCH_MIN_CHARS = 3
# Ekranda gösterilen en çok eşleşme; toplam eşleşme sayısı ayrıca söylenir.
MEMORY_SEARCH_LIMIT = 10
# Sorgu hiçbir kayıtta geçmiyorsa (ör. yazım hatalı cümle) en yakın kayıtlar
# gösterilir; benzerliği bu eşiğin altındaki kayıt gösterilmez. Eski Bellek
# sekmesinin bulanık aramasının eşiği; yazım hatalı sorguyu o bulabiliyordu.
MEMORY_FUZZY_THRESHOLD = 70
# Bellekteki proje adı verilmemiş kayıtların proje adı (core/memory.py).
MEMORY_DEFAULT_PROJECT = "default"
# Sıralamada eşleşmeyen alanın uzunluğu: SQLite'ın çok argümanlı MIN()'i
# argümanlardan biri NULL ise NULL döndürür, bu yüzden NULL yerine bu değer.
_SQL_NO_LENGTH = 2 ** 31 - 1


def memory_db_path() -> Path:
    """Arayüzün gösterdiği çeviri belleği (pipeline'ın varsayılan belleği)."""
    return bellek_yolu(PROJECT_ROOT)


def fold_text(text: str) -> str:
    """Aramada büyük/küçük harf farkı ve Türkçe I/İ/ı/i ayrımı yok sayılır.

    SQLite'ın LIKE'ı yalnız ASCII harflerde büyük/küçük ayırmaz ("Ü" ile
    "ü" eşleşmezdi). "İ".lower() iki karakter verdiği için önce elle çevrilir.
    """
    return text.replace("İ", "i").replace("I", "i").replace("ı", "i").lower()


def _sql_fold(value: Any) -> str:
    return fold_text(value) if isinstance(value, str) else ""


def _like_pattern(needle: str) -> str:
    """Sorgu harfiyen aransın: LIKE'ın % ve _ jokerleri kaçışlanır."""
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@contextmanager
def _memory_readonly(path: Path):
    """Belleğe SALT OKUNUR bağlantı (mode=ro): arayüz belleğe asla yazmaz.

    TranslationMemory() açılışta tablo/indeks oluşturur, dosya yoksa yaratır;
    Bellek sekmesi yalnız baktığı için onu kullanmaz.
    """
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        conn.create_function("kc_fold", 1, _sql_fold, deterministic=True)
        yield conn
    finally:
        conn.close()


@dataclass(frozen=True)
class MemoryStats:
    total: int
    confirmed: int


@dataclass(frozen=True)
class MemoryHit:
    source: str
    target: str
    project: str
    confirmed: bool
    # Sorgunun eşleşen metne (kaynak ya da çeviri) benzerliği, 0-100.
    similarity: float


@dataclass(frozen=True)
class MemorySearch:
    total: int                      # sorgunun geçtiği (bulanıkta: yakın) kayıt sayısı
    hits: tuple[MemoryHit, ...]     # ilk MEMORY_SEARCH_LIMIT kayıt
    # True: sorgu hiçbir kayıtta geçmiyor; hits benzerliği en yüksek kayıtlar.
    fuzzy: bool = False


def memory_stats(path: Path | None = None) -> MemoryStats | None:
    """Kayıt ve onaylı kayıt sayısı; bellek dosyası yoksa None."""
    path = memory_db_path() if path is None else path
    if not path.is_file():
        return None
    with _memory_readonly(path) as conn:
        total, confirmed = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(confirmed = 1), 0) FROM translations"
        ).fetchone()
    return MemoryStats(int(total), int(confirmed))


def search_memory(query: str, path: Path | None = None,
                  limit: int = MEMORY_SEARCH_LIMIT) -> MemorySearch | None:
    """Sorgunun kaynak ya da çeviri metninde GEÇTİĞİ kayıtlar (U13).

    Eski arama yalnız bulanık benzerlikti: yalnız "default" projesine
    bakıyor, sorgunun bütün kayda %70 benzemesini istiyor ve rakamı farklı
    kaydı eliyordu; "the" ya da başındaki "(3)" olmadan yazılan cümle hiç
    bulunmuyordu. Şimdi metin içi arama (SQL LIKE) yapılır; kısa kayıt önce
    gelir (sorguyu içeren en kısa kayıt ona en çok benzeyendir). Sorgu hiçbir
    kayıtta geçmiyorsa en yakın kayıtlar döner (``fuzzy=True``): yazım hatalı
    sorguyu eski arama buluyordu, bu yetenek kaybolmasın. Sorgu
    MEMORY_SEARCH_MIN_CHARS'tan kısaysa None.
    """
    needle = fold_text(query.strip())
    if len(needle) < MEMORY_SEARCH_MIN_CHARS:
        return None
    path = memory_db_path() if path is None else path
    if not path.is_file():
        return MemorySearch(0, ())
    params = {"pattern": _like_pattern(needle), "limit": int(limit)}
    in_source = "kc_fold(source_text) LIKE :pattern ESCAPE '\\'"
    in_target = "kc_fold(target_text) LIKE :pattern ESCAPE '\\'"
    with _memory_readonly(path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM translations WHERE {in_source} OR {in_target}",
            params).fetchone()[0]
        if not total:
            rows = conn.execute("SELECT source_text, target_text, project, confirmed "
                                "FROM translations ORDER BY id").fetchall()
            return _closest_records(needle, rows, limit)
        rows = conn.execute(
            f"""SELECT source_text, target_text, project, confirmed,
                       {in_source} AS hit_source, {in_target} AS hit_target
                FROM translations
                WHERE {in_source} OR {in_target}
                ORDER BY MIN(CASE WHEN {in_source} THEN LENGTH(source_text)
                                  ELSE {_SQL_NO_LENGTH} END,
                             CASE WHEN {in_target} THEN LENGTH(target_text)
                                  ELSE {_SQL_NO_LENGTH} END),
                         id
                LIMIT :limit""", params).fetchall()
    from rapidfuzz import fuzz

    hits = []
    for source, target, project, confirmed, hit_source, hit_target in rows:
        matched = [text for text, hit in ((source, hit_source), (target, hit_target))
                   if hit and isinstance(text, str)]
        similarity = max((fuzz.ratio(needle, fold_text(text)) for text in matched),
                         default=0.0)
        hits.append(_memory_hit(source, target, project, confirmed, similarity))
    return MemorySearch(int(total), tuple(hits))


def _memory_hit(source: Any, target: Any, project: Any, confirmed: Any,
                similarity: float) -> MemoryHit:
    return MemoryHit(source=source or "", target=target or "",
                     project=project or MEMORY_DEFAULT_PROJECT,
                     confirmed=bool(confirmed), similarity=float(similarity))


def _closest_records(needle: str, rows: list, limit: int) -> MemorySearch:
    """Sorgu hiçbir kayıtta geçmiyor: kaynak ya da çeviri metni ona en çok
    benzeyen (MEMORY_FUZZY_THRESHOLD ve üstü) kayıtlar, benzerliğe göre."""
    from rapidfuzz import fuzz

    scored = []
    for index, (source, target, project, confirmed) in enumerate(rows):
        similarity = max(fuzz.ratio(needle, fold_text(text))
                         for text in (source or "", target or ""))
        if similarity >= MEMORY_FUZZY_THRESHOLD:
            scored.append((-similarity, index, source, target, project, confirmed))
    scored.sort()
    hits = tuple(_memory_hit(source, target, project, confirmed, -negative)
                 for negative, _index, source, target, project, confirmed
                 in scored[:limit])
    return MemorySearch(len(scored), hits, fuzzy=True)


def glossary_example(terms: dict) -> str | None:
    """“Terim ara” alanının örneği: sözlükte GERÇEKTEN olan bir terim (U14).

    Alanın eski örneği ("orbit") sözlükte yoktu. Seçim sabit kuralla yapılır:
    alfabetik sırada Türkçe karşılığı kendisinden farklı ilk terim; öyle bir
    terim yoksa alfabetik ilk terim; sözlük boşsa None.
    """
    if not terms:
        return None
    keys = sorted(terms)

    def english(key: str) -> str:
        data = terms[key] if isinstance(terms[key], dict) else {}
        return str(data.get("en") or key)

    for key in keys:
        data = terms[key] if isinstance(terms[key], dict) else {}
        turkish = str(data.get("tr") or "")
        if turkish and turkish.casefold() != english(key).casefold():
            return english(key)
    return english(keys[0])


# ---------------------------------------------------------------------------
# Tek bir CLI koşusu
# ---------------------------------------------------------------------------

class Run:
    """Ayrı süreçte koşan tek bir CLI işi ve onun gerçek ilerleme durumu.

    stdout ayrı bir iş parçacığında sürekli okunur; böylece arayüz hiç
    bakmasa bile boru dolup alt süreç kilitlenmez. ``poll()`` olay dosyasını
    işler ve sürecin bitip bitmediğine bakar; arayüz onu periyodik çağırır.
    """

    def __init__(self, kind: str, command: list[str]) -> None:
        self.kind = kind
        self.command = command
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.returncode: int | None = None
        self.cancelled = False
        self.phase_number = ""
        self.phase_title = ""
        self.progress_current: int | None = None
        self.progress_total: int | None = None
        self.progress_from: int | None = None    # sayfa aralığının başı
        self.progress_unit = ""
        self._progress_stamp = 0.0               # son sayacın zaman damgası
        self.last_error = ""
        # CLI sözleşmesi: makine-okunur özet son satırda JSON olarak gelir.
        self.result: dict[str, Any] | None = None

        # Olay dosyasının dizini start()'ta açılır, koşu bitince silinir
        # (denetçi B, 24 Eyl 2026: her koşu $TMPDIR'de bir dizin bırakıyordu).
        self.event_path: Path | None = None
        self._event_offset = 0
        self._event_tail = ""
        self._lines: deque[str] = deque(maxlen=TAIL_LINES)
        self._lock = threading.Lock()
        # Aynı koşuyu birden çok oturum yoklar (iş yuvası süreç düzeyinde;
        # her sekmenin ilerleme parçası ayrı iş parçacığında): olay dosyası
        # ofseti ve bitiş kaydı tek elden güncellensin.
        self._poll_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None

    # -- yaşam döngüsü -----------------------------------------------------

    def start(self) -> None:
        self.event_path = Path(tempfile.mkdtemp(prefix=EVENT_DIR_PREFIX)) / EVENT_FILE_NAME
        env = dict(os.environ)
        env[EVENT_LOG_ENV] = str(self.event_path)
        env["PYTHONUNBUFFERED"] = "1"      # sayaçlar tamponda beklemesin
        self._process = subprocess.Popen(
            self.command,
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # Kendi süreç grubu: iptal, Marker'ın açtığı alt süreçleri de durdurur.
            start_new_session=True,
        )
        self.started_at = time.time()
        _track_run(self)
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    @property
    def running(self) -> bool:
        return self._process is not None and self.returncode is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def poll(self) -> None:
        """Yeni olayları uygula; süreç bittiyse dönüş kodunu kaydet."""
        with self._poll_lock:
            self._drain_events()
            if self._process is None or self.returncode is not None:
                return
            code = self._process.poll()
            if code is None:
                return
            if self._reader is not None:
                self._reader.join(timeout=5)   # son satırlar (JSON özeti) okunsun
            self._drain_events()
            self.finished_at = time.time()
            self.returncode = code
            self._discard_event_dir()
            _untrack_run(self)

    def cancel(self, grace: float = CANCEL_GRACE_SEC) -> None:
        """Süreç grubunu durdur: önce SIGTERM, yanıt yoksa SIGKILL.

        ``running`` yalnız poll() ile güncellenir; süreç kendiliğinden bitmiş
        ama henüz yoklanmamışsa (en çok bir yenileme aralığı) iş iptal
        SAYILMAZ: önce yoklanır, sonucu (PDF) gizlenmez (F5-13, 2026-09-29).
        Sinyal tam bitişe denk gelirse (süreç 0 ile çıktı) de iptal sayılmaz:
        varsayılan SIGTERM işleyicisi 0 ile çıkmaz, iş kendi başına bitmiştir.
        """
        if not self.running:
            return
        if self._process.poll() is not None:     # bitmiş, yoklanmamış
            self.poll()
            return
        self.cancelled = True
        pid = self._process.pid
        try:
            os.killpg(pid, signal.SIGTERM)
            self._process.wait(timeout=grace)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._process.wait()
        if self._process.poll() == 0:
            self.cancelled = False
        self.poll()

    # -- arayüzün okuduğu durum -------------------------------------------

    def percent(self) -> float | None:
        """Gerçek sayaç varsa 0-1 arası oran; yoksa None (gösterge çizilmez).

        Sayfa sayacı ("Batch: pages a-b / N") parti BAŞLARKEN basılır: a-b
        işlenmekte, bitmiş sayfa sayısı a'dır (sayfalar 0'dan sayılır). Oran
        bu yüzden a/N'dir; b/N henüz bitmemiş sayfaları bitmiş gösterirdi.
        """
        with self._lock:
            current, total = self.progress_current, self.progress_total
            start, unit = self.progress_from, self.progress_unit
        if unit == "page" and start is not None:
            current = start
        if current is None or total is None or total <= 0 or current < 0:
            return None
        return min(current / total, 1.0)

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def tail(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    # -- iç işler ----------------------------------------------------------

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for raw in self._process.stdout:
            line = raw.strip()
            if line:
                self._handle_stdout_line(line)

    def _handle_stdout_line(self, line: str) -> None:
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                with self._lock:
                    self.result = parsed
                return
        if not _TQDM_LINE.search(line):
            with self._lock:
                self._lines.append(line)
        # stdout'tan YALNIZ ilerleme sayaçları alınır; faz/QC olayları olay
        # dosyasından gelir, buradan da alınsa çiftlenirdi.
        event = classify(line)
        if event and event.get("kind") == "progress":
            with self._lock:
                self._apply_event(event, received=time.time())

    def _discard_event_dir(self) -> None:
        """Bitmiş koşunun olay dizinini sil (olaylar zaten okundu)."""
        folder = self.event_path.parent if self.event_path is not None else None
        if (folder is None or not folder.name.startswith(EVENT_DIR_PREFIX)
                or folder.is_symlink()):
            return
        shutil.rmtree(folder, ignore_errors=True)

    def _drain_events(self) -> None:
        """Olay dosyasına eklenen yeni satırları oku ve uygula."""
        if self.event_path is None:
            return          # koşu başlamadı
        try:
            with self.event_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._event_offset)
                data = handle.read()
                self._event_offset = handle.tell()
        except OSError:
            return          # dosya henüz yok: pipeline daha kurulumda
        if not data:
            return
        self._event_tail += data
        # Son satır yarım yazılmış olabilir; tamponda beklet.
        *lines, self._event_tail = self._event_tail.split("\n")
        with self._lock:
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self._apply_event(event, received=time.time())

    def _apply_event(self, event: dict[str, Any], received: float) -> None:
        """Tek olayı duruma işle (kilit çağıranda tutulur).

        Olay dosyası poll() anında, stdout sayaçları geldikleri anda işlenir;
        yani sıra karışabilir. Bu yüzden zaman damgasına bakılır: faz olayı
        yalnız kendisinden ÖNCE gelmiş sayacı sıfırlar, eski bir sayaç da
        yenisini ezmez.
        """
        kind = event.get("kind")
        epoch = event.get("epoch")
        stamp = float(epoch) if isinstance(epoch, (int, float)) else received
        if kind == "phase":
            self.phase_number = str(event.get("phase", ""))
            self.phase_title = str(event.get("title", ""))
            if stamp >= self._progress_stamp:
                self.progress_current = None
                self.progress_total = None
                self.progress_from = None
                self.progress_unit = ""
        elif kind == "progress":
            if stamp < self._progress_stamp:
                return
            self._progress_stamp = stamp
            current, total = event.get("current"), event.get("total")
            start = event.get("from")
            self.progress_current = current if isinstance(current, int) else None
            self.progress_total = total if isinstance(total, int) else None
            self.progress_from = start if isinstance(start, int) else None
            self.progress_unit = str(event.get("unit", ""))
        elif kind == "error":
            self.last_error = str(event.get("message", ""))


# ---------------------------------------------------------------------------
# Sunucu kapanırken koşan işler (F5-04, 2026-09-29)
# ---------------------------------------------------------------------------
# İş kendi süreç grubunda koşar (start_new_session=True: İptal, Marker'ın
# alt süreçlerini de durdurur); bu yüzden arayüz sunucusu Ctrl+C ile
# kapanınca iş PPID 1 ile yaşamaya devam ediyordu, yükleme kopyası (girdisi)
# atexit'te altından siliniyor, olay dizini kalıyordu. Artık başlatılan her
# koşu izlenir; süreç kapanırken (atexit) koşan işler durdurulur, bitmiş ama
# yoklanmamış koşuların olay dizinleri silinir, ardından yükleme dizinleri.
# Durdurulan iş terminale (stderr) yazılır: sessiz kalmaz.
# Streamlit betiği ana iş parçacığında koşmadığı için sinyal işleyicisi
# kurulamaz; Streamlit SIGINT/SIGTERM'de düzgün kapanıp atexit'i çalıştırır.
# kill -9 / çökme atexit'i atlar: o zaman kalan olay dizinlerini bir sonraki
# açılış süpürür (sweep_stale_event_dirs).

_active_runs: set[Run] = set()
_active_lock = threading.Lock()
# Kapanışta her iş için SIGTERM'den sonra beklenen süre (sn); sonra SIGKILL.
SHUTDOWN_GRACE_SEC = CANCEL_GRACE_SEC
SHUTDOWN_NOTICE = "Kitapçevir arayüzü kapanıyor: koşan iş durduruldu ({kind}, pid {pid})."


def _track_run(run: Run) -> None:
    global _exit_handler_registered
    with _active_lock:
        _active_runs.add(run)
    with _upload_lock:
        if not _exit_handler_registered:
            atexit.register(_on_exit)
            _exit_handler_registered = True


def _untrack_run(run: Run) -> None:
    with _active_lock:
        _active_runs.discard(run)


def active_runs() -> list[Run]:
    """İzlenen (başlamış, olay dizini henüz silinmemiş) koşular."""
    with _active_lock:
        return list(_active_runs)


def _folders_in_use() -> set[Path]:
    """Hâlâ koşan işlerin komutlarında geçen dosyaların dizinleri."""
    folders: set[Path] = set()
    for run in active_runs():
        if not run.running:
            continue
        for arg in run.command:
            if os.sep in arg:
                folders.add(Path(arg).parent)
    return folders


def shutdown_runs(grace: float = SHUTDOWN_GRACE_SEC) -> list[Run]:
    """Koşan işleri durdur, bitmiş koşuların olay dizinlerini sil.

    Durdurulan koşuları döndürür; her biri için stderr'e bir satır yazılır.
    """
    stopped: list[Run] = []
    for run in active_runs():
        try:
            if run.running:
                pid, kind = run.pid, run.kind
                run.cancel(grace=grace)
                if run.cancelled:
                    stopped.append(run)
                    print(SHUTDOWN_NOTICE.format(kind=kind, pid=pid),
                          file=sys.stderr, flush=True)
            else:
                run.poll()
        except Exception:        # kapanış işleyicisi başka temizliği düşürmesin
            continue
        finally:
            _untrack_run(run)
    return stopped


def _on_exit() -> None:
    """Süreç kapanırken: önce koşan işler, sonra yükleme dizinleri (atexit)."""
    shutdown_runs()
    _cleanup_uploads()


# Önceki sunuculardan kalan olay dizini (kill -9, çökme) bu yaştan büyükse
# süpürülür (sn). Yaş, dizindeki en yeni dosyanın zamanıdır: yetim kalıp hâlâ
# yazan bir işin dizini silinmez.
EVENT_DIR_SWEEP_AGE_SEC = UPLOAD_SWEEP_AGE_SEC
_EVENT_DIR_NAME = re.compile(re.escape(EVENT_DIR_PREFIX) + r"[a-z0-9_]{8}")


def _looks_like_our_event_dir(folder: Path) -> bool:
    """Run.start'ın açtığı biçimde mi: adı birebir, içinde yalnız olay dosyası."""
    if not _EVENT_DIR_NAME.fullmatch(folder.name):
        return False
    if folder.is_symlink() or not folder.is_dir():
        return False
    try:
        if folder.stat().st_uid != os.getuid():
            return False
        entries = list(folder.iterdir())
    except OSError:
        return False
    return all(entry.name == EVENT_FILE_NAME and entry.is_file()
               and not entry.is_symlink() for entry in entries)


def sweep_stale_event_dirs(max_age_sec: float = EVENT_DIR_SWEEP_AGE_SEC, *,
                           root: Path | None = None,
                           now: float | None = None) -> list[Path]:
    """Önceki sunuculardan kalmış eski olay dizinlerini sil; silinenleri döndür.

    Yalnız Run.start'ın açtığı biçimdeki (önekli ad, içinde yalnız
    events.jsonl) ve en yeni dosyası *max_age_sec*'ten eski dizinler silinir.
    Bu süreçte izlenen koşuların dizinlerine dokunulmaz.
    """
    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    moment = time.time() if now is None else now
    ours = {run.event_path.parent for run in active_runs()
            if run.event_path is not None}
    removed: list[Path] = []
    try:
        candidates = list(base.glob(f"{EVENT_DIR_PREFIX}*"))
    except OSError:
        return removed
    for folder in candidates:
        if folder in ours or not _looks_like_our_event_dir(folder):
            continue
        try:
            newest = max([folder.stat().st_mtime]
                         + [entry.stat().st_mtime for entry in folder.iterdir()])
        except OSError:
            continue
        if moment - newest <= max_age_sec:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            removed.append(folder)
    return removed
