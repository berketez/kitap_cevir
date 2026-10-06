#!/usr/bin/env python3
"""
Kitap çeviri pipeline'ı — PDF/EPUB → Türkçe PDF.

İKİ ÇEVİRİ YOLU VAR:

  {tex} — çeviriyi yapay zekâ asistanı ya da kullanıcı yapar
    (ÖNERİLEN · düşük donanım · model/GPU GEREKMEZ)
    Çıkarımı ve derlemeyi pipeline yapar, çeviriyi Claude/LLM veya insan yapar.
    En basit giriş bu betik DEĞİL, iki numaralı script'tir:
        python 1_cikar.py kitap.pdf          # → output/<ad>/EXPORT.tex (kaynak dilde)
        (EXPORT.tex'i çevir → EXPORT_TR.tex)
        python 2_derle.py .../EXPORT_TR.tex  # → Türkçe PDF
    Claude Code ile otomatik: bkz. CEVIRI_TALIMATI.md.
    (Bu betikte eşdeğeri: --export-tex ... sonra --import-tex EXPORT_TR.tex)

  {model} — otomatik çeviri  (yerel model · Apple Silicon; varsayılan HY-MT 7B MLX)
    python translate_book.py kitap.pdf                 # Tam otomatik: extract+translate+compile
    python translate_book.py kitap.epub                # EPUB → Türkçe PDF (+ EPUB çıktı)
    python translate_book.py kitap.pdf --mt-engine ollama:qwen3:8b   # başka yerel model
    Modeli seçmek için: python -m core.mt_engines.discovery (kimlikleri listeler).
    KAYNAK DİL: {langs}
    (core.lang_detect.TEX_SOURCE_LANGS; hedef hep Türkçe). Çıkarımdan önce
    PDF metin katmanı / EPUB metni üzerinde bir ön kapı, çıkarımdan sonra
    (MT'den önce) tam kapı kaynak dili ölçer; ölçülen dil zincire (istem,
    çevrilmemiş satır denetimi) akar. Kitap güvenle desteklenmeyen bir
    dildeyse (Çince, Portekizce...) iş çıkış kodu 4 ile durur. Metin katmanı
    olmayan taramada ön kapı sessizce geçer, karar tam kapınındır. Elle:
    --kaynak-dil KOD.

"{model}" seçenekleri:
    --name "Kitap Adı"     Çıktı adı (varsayılan: dosya adından otomatik)
    --book-type TÜR        Kitap türü (glossary seçimi; varsayılan: otomatik)
    --resume               Çökme sonrası kaldığı yerden devam
    --yeniden              Klasördeki önceki çıkarımı (full_book.md) kullanma,
                           kitabı baştan çıkar (eskisi full_book.onceki.md).
                           Varsayılan: aynı kaynak dosyadan yapılmış
                           kullanılabilir çıkarım yeniden kullanılır.
    --extract-only         Sadece çıkarım (PDF/EPUB → Markdown)
    --skip-compile         Sadece çevrilmiş .tex üret (derleme yok)
    --device cpu           Apple Silicon yoksa CPU (varsayılan: mps)
    --batch N              Birden çok kitabı sırayla işle
    --dil-kontrolu-yok     Kaynak dil ön + tam kapısını atla (tespit yanlışsa;
                           kaynak İngilizce varsayılır)
    --kaynak-dil KOD       Kaynak dili elle ver ({codes});
                           iki dil kapısı da atlanır
    --mt-engine KİMLİK     Çeviri modeli: adlı motor (yerleşik varsayılan ya da
                           config/mt_engines.json kaydı) · mlx:<klasör> ·
                           gguf:<dosya> · ollama:<model> · lmstudio:<model> ·
                           openai:<adres>|<model> (yalnız bu bilgisayar). Model
                           kullanılamıyorsa çıkış kodu 5 (önekli/kayıtlı
                           kimliklerde çıkarımdan ÖNCE).

"{tex}" bayrakları (1_cikar.py/2_derle.py bunları sarar):
    --export-tex           body.tex'i EXPORT.tex olarak çıkar (çeviri/derleme yok)
    --import-tex PATH       Çevrilmiş .tex'i al → sanitize+QC+derle (manuel mod)

Desteklenen girdi: .pdf (Marker OCR) · .epub (EPUB Parser)

Çıkış kodları:
    0    başarı
    1    hata (çıkarım/çeviri/ön kontrol)
    2    derleme başarısız (ya da klasörde "{tex}" ile yapılmış bir
         çeviri var ve yerel model onu ezmemek için durdu)
    3    QC skoru eşiğin altında (PDF yine üretilir)
    4    kaynak dil desteklenmiyor — yerel model dil kapısı
         (EXIT_SOURCE_UNSUPPORTED; eski adı EXIT_SOURCE_NOT_ENGLISH)
    5    seçilen çeviri modeli kullanılamıyor — motor/kimlik geçersiz, model
         klasörü/dosyası yok, sunucu kapalı, model sunucuda yok,
         llama-cpp-python kurulu değil, bu Mac'in belleği yerel model için
         yetersiz ya da model bu Mac'e sığmıyor (donanım kapısı, çıkarımdan
         ÖNCE) veya koşu sırasında sunucu bağlantısı koptu
         (EXIT_MODEL_UNAVAILABLE)
    130  kullanıcı kesmesi (Ctrl+C)
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import multiprocessing
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# -- Project root -------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# -- Çıkış kodları ------------------------------------------------------------
# 0 başarı · 1 hata · 2 derleme başarısız · 3 QC eşiği altı · 130 Ctrl+C.
# 4 (2026-09-24): "Yerel model ile çevir" (Yol B) kaynak dil kapısı, MT
# başlatılmadı (bkz. _source_language_decision). 2026-09-28 (K5, KARARLAR 14):
# anlamı "kitabın dili desteklenmiyor" (TEX_SOURCE_LANGS dışında, güvenle
# ölçüldü); eski ad EXIT_SOURCE_NOT_ENGLISH aynı değerle takma ad olarak kalır
# (arayüz ve eski testler içe aktarıyor).
EXIT_SOURCE_UNSUPPORTED = 4
EXIT_SOURCE_NOT_ENGLISH = EXIT_SOURCE_UNSUPPORTED
# 2 aynı zamanda "Yol A kilidi": klasörde TeX yoluyla (branding.TEX_TITLE)
# yapılmış çeviri var (MANUAL_TRANSLATION.marker), yerel model onu ezmemek
# için durdu. 2026-09-25 (U4): kilit çıkarımdan ÖNCE denetlenir
# (_yol_a_lock_applies).
EXIT_MANUAL_LOCK = 2
# 5 (2026-09-25): "Yerel model ile çevir" için seçilen model kullanılamıyor
# (kimlik geçersiz, klasör/dosya yok, sunucu kapalı, model sunucuda yok,
# llama-cpp-python yok, koşu sırasında sunucu koptu). Arayüz bu kodla
# "model kullanılamıyor + Modelleri tara" der. Sıra: Yol A kilidi (2) ve dil
# kapısı (4) önce; model kontrolü çıkarımdan ÖNCE (_engine_precheck_applies,
# _engine_check_needed), varsayılan motorda run_pipeline ön kontrolünde.
# 2026-09-28 (KARARLAR 17): donanım kapısı da 5 döner — bu Mac referans 4 bit
# 7B'yi çalıştıramıyorsa ya da seçilen model toplu iş 1 ile bile sığmıyorsa;
# her motorda (varsayılan dahil) çıkarımdan ÖNCE (_hardware_check).
EXIT_MODEL_UNAVAILABLE = 5

# Yol A kilidinin kalıcı işareti (output/<ad>/source/ altında).
MANUAL_MARKER_NAME = "MANUAL_TRANSLATION.marker"
# LatexTranslator'ın FINAL checkpoint dosyası (checkpoints/ altında). Tek
# kaynak latex_pipeline/translator.py (V2 denetimi, §11). --force-mt onu
# yedeğe taşır (F3-08).
from latex_pipeline.translator import FINAL_CHECKPOINT_NAME  # noqa: E402
# V2 denetimi (2026-09-29, F3-07 görünürlük): bu koşuda üretim sınırına
# dayanan (çevirisi eksik olabilecek) parçaların listesi, kitap klasöründe.
# Her MT koşusunda yeniden yazılır (boş liste = kesilme yok); arayüz ve QC
# okur. Öğe: {"source", "output", "used", "limit"} (core BookTranslator
# truncation_report biçimi).
TRUNCATION_REPORT_NAME = "kesik_parcalar.json"
# F3 (2026-09-30, Berke kararı): makine çevirisi künyesinin kaydı
# (output/<ad>/source/ altında). Yerel model çeviriyi (Phase 5) başarıyla
# yazınca: {"engine", "model", "source_lang", "date"}. Künye (PDF ve EPUB)
# bundan kurulur; --resume çeviriyi yapan modeli buradan bilir (bu koşunun
# modeli yazılmaz). Kayıt yoksa model "kaydedilmemiş" yazılır, uydurulmaz.
MT_NOTICE_INFO_NAME = "mt_kunye.json"
# Resume'un bir ara dosyayı "kullanılabilir" saydığı en küçük boyut (bayt);
# run_pipeline resume mantığı ve Yol A ön kilidi aynı eşiği kullanır.
RESUMABLE_MIN_BYTES = 100

# pipeline.log EKLEME kipinde yazılır, her koşunun başına ayraç konur
# (2026-09-25, U4: reddedilen koşu önceki koşunun günlüğünü siliyordu).
# Bu boyutu aşan günlük koşu başında pipeline.onceki.log'a taşınır.
PIPELINE_LOG_NAME = "pipeline.log"
PIPELINE_LOG_PREVIOUS_NAME = "pipeline.onceki.log"
PIPELINE_LOG_MAX_BYTES = 20 * 1024 * 1024

# QC özetinin (QCReport.print_summary) yazıldığı dosya; 2_derle --json
# qc_score / qc_top alanlarını buradan okur (S4).
QC_REPORT_NAME = "qc_report.txt"

# Varsayılan çeviri motorunun TEK kaynağı core/mt_engines/registry.py'deki
# DEFAULT_ENGINE: --mt-engine varsayılanı, run_pipeline/_preflight_check
# imzaları ve run_batch/main yedekleri buradan okur (arayüz translate_book'u
# --mt-engine vermeden başlatır; ui/runner.local_model_dir de aynı sabite
# bakar). Modül özniteliği olarak okunur: parse_args değeri çağrı anında alır.
from core.mt_engines import registry as mt_registry  # noqa: E402
# Model kullanılamıyor hatası (RuntimeError alt sınıfı; hafif sözleşme modülü)
from core.mt_engines.contracts import BackendUnavailable  # noqa: E402
# Kitap klasörü veri kökünün output/'undadır (TASARIM F1): paketli kipte
# KITAPCEVIR_VERI_KOKU/output, geliştirici kipinde PROJECT_ROOT/output.
from config.paths import cikti_koku  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Klasör adının en fazla uzunluğu (karakter).
SLUG_MAX_LEN = 80
# F5-05 (2026-09-29): harf ya da rakam içermeyen kitap adı ("...", "?", "—")
# boş klasör adı veriyordu ve kitap output/ köküne yazılıyordu. Böyle adlarda
# klasör adı bu önek + adın SHA-1'inin ilk 8 hanesi olur (deterministik;
# farklı adlar farklı klasöre gider). Tek kaynak slugify: arayüz ve iki CLI
# kapısı klasör adını bununla hesaplar.
SLUG_FALLBACK_PREFIX = "kitap"


def _raw_slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "_", text.strip())
    return text[:SLUG_MAX_LEN]


def slug_is_fallback(text: str) -> bool:
    """Ad klasör adı için harf/rakam içermiyor mu (slugify yedek ad verir)?"""
    return not re.search(r"[^\W_]", _raw_slug(text))


def slugify(text: str) -> str:
    """Convert text to a safe directory/file name.

    Harf/rakam içermeyen adlarda (yalnız noktalama, tire, alt çizgi) boş ya da
    anlamsız ad yerine ``kitap_<sha1[:8]>`` döner (F5-05).
    """
    if slug_is_fallback(text):
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        return f"{SLUG_FALLBACK_PREFIX}_{digest}"
    return _raw_slug(text)


def detect_pages(pdf_path: str) -> int:
    """Get total page count from PDF."""
    import fitz
    doc = fitz.open(pdf_path)
    n = len(doc)
    doc.close()
    return n


# ---------------------------------------------------------------------------
# Çıkarım dosyası (full_book.md): ortak adlar, kullanılabilirlik, kimlik
# ---------------------------------------------------------------------------

# Çıkarımın ara dosyası ve --yeniden ile saklanan önceki sürümü (1_cikar.py
# bu adları buradan alır; tek kaynak).
EXTRACTION_NAME = "full_book.md"
PREVIOUS_EXTRACTION_NAME = "full_book.onceki.md"
# F1-11 (2026-09-29): kullanılamayan çıkarım metni (başarısız çıkarımın az
# metni ya da eski boş full_book.md) SİLİNMEZ; bu adla kenara alınır ve
# full_book.md olarak asla yeniden kullanılmaz.
UNUSABLE_EXTRACTION_NAME = "full_book.kullanilamaz.md"
# Çıkarımın kaynak kimliği (SHA-1). Başarılı çıkarımla birlikte yazılır;
# translate_book önceki çıkarımı yalnız aynı kaynak dosyadan yapılmışsa
# yeniden kullanır (F3-12, F5-09).
EXTRACTION_INFO_NAME = "full_book.kaynak.json"
# Çıkarımın kullanılabilir sayıldığı en az metin (karakter; çıkarılamayan
# sayfa yer tutucuları sayılmaz). run_extraction, run_epub_extraction,
# yeniden kullanım denetimi ve run_pipeline aynı eşiği kullanır.
EXTRACTION_MIN_CHARS = 100
# F1-03 (2026-09-29): Marker'ın tek sayfaya inse de çıkaramadığı sayfa
# SESSİZCE atlanmaz: full_book.md'de sayfanın yerinde bu görünür yer tutucu
# durur (EXPORT.tex'e ve PDF'ye kalın satır olarak geçer). Sayfa numarası
# 1'den başlar (PDF görüntüleyicideki numara). Düşen sayfaların tek kaynağı
# bu yer tutuculardır (failed_pages_in_markdown): yeniden kullanılan
# çıkarımda da görünür; kullanıcı metni elle eklerse uyarı kalkar.
FAILED_PAGE_PLACEHOLDER = (
    "**[ÇIKARILAMAYAN SAYFA: PDF sayfa {page} — metin otomatik "
    "çıkarılamadı; bu sayfayı kaynak PDF'den elle ekleyin.]**"
)
_FAILED_PAGE_RE = re.compile(
    re.escape(FAILED_PAGE_PLACEHOLDER).replace(re.escape("{page}"), r"(\d+)")
)


def failed_page_placeholder(page_no: int) -> str:
    """*page_no* (1'den başlar) için full_book.md yer tutucusu."""
    return FAILED_PAGE_PLACEHOLDER.format(page=page_no)


def failed_pages_in_markdown(markdown: str) -> list[int]:
    """Çıkarım metnindeki çıkarılamayan sayfa numaraları (1'den, sıralı)."""
    return sorted({int(m.group(1)) for m in _FAILED_PAGE_RE.finditer(markdown)})


def extraction_text_length(markdown: str) -> int:
    """Yer tutucular dışındaki gerçek çıkarım metninin uzunluğu."""
    return len(_FAILED_PAGE_RE.sub("", markdown).strip())


def extraction_usable(md_path: str | Path) -> bool:
    """*md_path* kullanılabilir bir çıkarım mı (var ve yeterli metin)?"""
    try:
        text = Path(md_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return extraction_text_length(text) >= EXTRACTION_MIN_CHARS


def _unused_path(path: Path) -> Path:
    """*path* boşsa kendisi; doluysa ``ad.1.uzantı``, ``ad.2.uzantı`` …
    (kenara alınan dosyalar birbirini ezmesin — kayıpsızlık)."""
    if not path.exists():
        return path
    suffix = path.suffix
    stem = path.name[:-len(suffix)] if suffix else path.name
    n = 1
    while True:
        candidate = path.with_name(f"{stem}.{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def set_aside_unusable_extraction(output_dir: str | Path) -> Path | None:
    """Kullanılamayan full_book.md'yi UNUSABLE_EXTRACTION_NAME'e taşı.

    Yeniden kullanılıp klasörü "zehirlemesin" (F1-11) diye; silinmez.
    Taşındıysa yeni yolu, dosya yoksa ya da kullanılabilirse None döner.
    """
    md = Path(output_dir) / EXTRACTION_NAME
    if not md.exists() or extraction_usable(md):
        return None
    dest = _unused_path(md.with_name(UNUSABLE_EXTRACTION_NAME))
    try:
        md.replace(dest)
    except OSError:
        return None
    return dest


def write_extraction_info(output_dir: str | Path, source_path: str | Path,
                          source_format: str) -> None:
    """Başarılı çıkarımın kaynak kimliğini EXTRACTION_INFO_NAME'e yaz."""
    from yol_a_ortak import sha1_of_file

    try:
        source_sha1 = sha1_of_file(source_path)
    except OSError:
        return  # kaynak okunamıyorsa kimlik yazılmaz; yeniden kullanım olmaz
    info = {
        "source_sha1": source_sha1,
        "source_format": source_format,
        "source_name": Path(source_path).name,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        (Path(output_dir) / EXTRACTION_INFO_NAME).write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    except OSError:
        pass  # kimlik yardımcıdır; yazılamazsa çıkarım yeniden yapılır


def extraction_source_sha1(output_dir: str | Path) -> str | None:
    """Klasördeki çıkarımın kaynak SHA-1'i: önce EXTRACTION_INFO_NAME, yoksa
    1_cikar'ın kitap.json'u (1_cikar ad çakışmasını reddettiği için oradaki
    kimlik çıkarımınkiyle aynıdır). Bilinmiyorsa None."""
    out = Path(output_dir)
    for name in (EXTRACTION_INFO_NAME, "kitap.json"):
        try:
            data = json.loads((out / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sha1 = data.get("source_sha1") if isinstance(data, dict) else None
        if isinstance(sha1, str) and sha1.strip():
            return sha1.strip()
    return None


def extraction_reuse_decision(output_dir: str | Path, input_path: str | Path,
                              *, resume: bool) -> tuple[bool, str]:
    """translate_book: klasördeki önceki çıkarım yeniden kullanılsın mı?

    (F3-12, F5-09, 2026-09-29) Kullanılabilir full_book.md, kimliği bu
    kaynak dosyayla doğrulanırsa (EXTRACTION_INFO_NAME ya da kitap.json)
    yeniden kullanılır — "1 · TeX'i çıkar"ın çıkarımı "Çevir"de, iptal
    edilen koşunun çıkarımı yeniden başlatmada. Kimlik dosyası yoksa yalnız
    --resume ile ve klasördeki kaynak kopyası bu dosyayla aynıysa kullanılır;
    başka kaynağa aitse kullanılmaz. Yan etkisizdir (dosyaya dokunmaz).
    Dönüş: (kullan, açıklama).
    """
    md = Path(output_dir) / EXTRACTION_NAME
    if not md.exists() or not extraction_usable(md):
        # Kullanılamayan dosya çıkarım yerinde kenara alınır
        # (_run_book_extraction); karar yan etkisizdir.
        return False, ""
    known = extraction_source_sha1(output_dir)
    if known:
        from yol_a_ortak import sha1_of_file

        if known == sha1_of_file(input_path):
            return True, ""
        return False, (f"Klasördeki önceki çıkarım ({EXTRACTION_NAME}) başka "
                       f"bir kaynak dosyadan yapılmış; kitap baştan "
                       f"çıkarılıyor.")
    if resume:
        # Kimlik dosyası olmayan (eski sürümle çıkarılmış) klasör: --resume
        # ile klasördeki kaynak kopyası (source_book.*) bu dosyayla aynıysa
        # kullanılır. Kopya yoksa ya da farklıysa çıkarım yeniden yapılır
        # (sonraki koşular yazılan kimlikle yeniden kullanır).
        from yol_a_ortak import sha1_of_file

        copy = Path(output_dir) / ("source_book" + Path(input_path).suffix.lower())
        try:
            if copy.is_file() and sha1_of_file(copy) == sha1_of_file(input_path):
                return True, "kaynak kopyasıyla doğrulandı, --resume"
        except OSError:
            pass
    return False, ""


def input_problem(input_path: str, input_format: str) -> str | None:
    """Girdi dosyası açılabiliyor mu? Açılamıyorsa Türkçe neden, yoksa None.

    F1-11 (2026-09-29): bozuk PDF, çıkarımın içinde ham traceback'le
    düşüyor ve klasörde bozuk kaynağın kopyasını bırakıyordu.
    """
    if input_format == "pdf":
        try:
            pages = detect_pages(input_path)
        except Exception as exc:  # fitz: FileDataError, RuntimeError...
            return (f"PDF açılamadı; dosya bozuk ya da gerçek bir PDF değil. "
                    f"Ayrıntı: {exc}")
        if pages <= 0:
            return "PDF'de hiç sayfa yok."
        return None
    if input_format == "epub":
        import zipfile

        if not zipfile.is_zipfile(input_path):
            return ("EPUB açılamadı; dosya bozuk ya da gerçek bir EPUB değil "
                    "(EPUB bir zip arşividir).")
    return None


def _tr_decimal(value: float, digits: int = 1) -> str:
    """Türkçe ondalık yazım ("2,5"); ui.runner.format_decimal ile aynı kural
    (translate_book arayüz modülünü içe aktarmaz: döngüsel içe aktarma)."""
    rounded = round(float(value), digits)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{digits}f}".replace(".", ",")


def guess_book_name(input_path: str) -> str:
    """Extract a human-readable book name from the input filename (PDF or EPUB)."""
    stem = Path(input_path).stem
    # Remove common patterns: ISBN, hashes, "Anna's Archive", publisher info
    name = re.sub(r"\s*--\s*.*", "", stem)  # everything after first --
    name = re.sub(r"\s*\d{10,13}\s*", " ", name)  # ISBN
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name if name else stem[:60]


def detect_input_format(input_path: str) -> str:
    """Detect input file format from extension.

    Returns 'pdf' or 'epub'. Raises ValueError for unsupported formats.
    """
    ext = Path(input_path).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    elif ext == ".epub":
        return "epub"
    else:
        raise ValueError(
            f"Desteklenmeyen dosya biçimi: {ext or '(uzantı yok)'}. "
            "Desteklenen biçimler: .pdf, .epub"
        )


#: Kitap türleri: tür sezicisinin dönüş değerleri ve ``--book-type``
#: seçenekleri (tek kaynak). "philosophy": felsefe ve kişisel gelişim.
KITAP_TURLERI: tuple[str, ...] = ("science", "philosophy", "novel")

def detect_book_type(input_path: str, markdown_text: str | None = None) -> str:
    """Auto-detect book type from input filename and markdown content.

    Returns one of ``KITAP_TURLERI``: "science", "philosophy", "novel"
    ("philosophy": felsefe ve kişisel gelişim kitapları).
    Falls back to "science" if uncertain (safest default -- preserves formulas).

    Detection strategy:
    1. Check input filename for keywords (works for both PDF and EPUB)
    2. If markdown available, scan first 5000 chars for content signals
    """
    # -- Keyword sets for each type -------------------------------------------
    _NOVEL_KEYWORDS = {
        "novel", "fiction", "story", "stories", "roman", "tale", "tales",
        "narrative", "thriller", "mystery", "horror", "fantasy", "romance",
        "adventure", "detective", "saga", "memoir", "autobiography",
    }
    _PHILOSOPHY_KEYWORDS = {
        "philosophy", "felsefe", "metaphysics", "ontology", "epistemology",
        "ethics", "stoic", "stoicism", "nietzsche", "heidegger", "kant",
        "plato", "aristotle", "socrates", "existentialism", "phenomenology",
        "manifesto", "ideology", "political",
        "capitalism", "marxism", "anarchism", "libertarian",
        "psychology", "consciousness", "mindset",
        # Kişisel gelişim
        "self-help", "self help", "personal development", "personal growth",
        "self-improvement", "self improvement", "mindfulness",
        "kişisel gelişim",
    }
    _SCIENCE_KEYWORDS = {
        "equation", "theorem", "proof", "algorithm", "computational",
        "physics", "chemistry", "biology", "engineering", "mathematics",
        "calculus", "differential", "integral", "quantum", "thermodynamics",
        "mechanics", "dynamics", "fluid", "structural", "numerical",
        "machine learning", "deep learning", "neural", "optimization",
        "propulsion", "aerospace", "rocket", "turbine",
    }

    # -- Step 1: Check filename -----------------------------------------------
    # (#5) Kelime-sinirli eslesme + birlesik skor: eski substring kontrolu
    # 'story' ⊂ 'history', 'roman' ⊂ 'romania' carpismalariyla tarih/bilim
    # kitaplarini 'novel' siniflandiriyordu; novel listesinin erken-donus
    # avantaji da vardi. Simdi uc kategori birlikte puanlanir, esitlikte
    # 'science' (en guvenli: formul korumasi acik) kazanir.
    name_lower = Path(input_path).stem.lower()
    name_tokens = set(re.findall(r"[a-zçğıöşü]+", name_lower))

    def _name_hits(keywords: set) -> int:
        hits = 0
        for kw in keywords:
            if " " in kw:
                if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])",
                             name_lower):
                    hits += 1
            elif kw in name_tokens:
                hits += 1
        return hits

    name_scores = {
        "science": _name_hits(_SCIENCE_KEYWORDS),
        "philosophy": _name_hits(_PHILOSOPHY_KEYWORDS),
        "novel": _name_hits(_NOVEL_KEYWORDS),
    }
    best_name = max(name_scores, key=name_scores.get)
    if name_scores[best_name] > 0:
        return best_name

    # -- Step 2: Check markdown content (first 5000 chars) --------------------
    if markdown_text:
        sample = markdown_text[:5000].lower()

        scores = {"science": 0, "philosophy": 0, "novel": 0}
        for kw in _SCIENCE_KEYWORDS:
            if kw in sample:
                scores["science"] += 1
        for kw in _PHILOSOPHY_KEYWORDS:
            if kw in sample:
                scores["philosophy"] += 1
        for kw in _NOVEL_KEYWORDS:
            if kw in sample:
                scores["novel"] += 1

        # Also check for math content signals (strong science indicator)
        math_signals = len(re.findall(r"\$[^$]+\$|\\\[|\\\(|\\begin\{equation", sample))
        scores["science"] += math_signals * 2

        best = max(scores, key=scores.get)
        if scores[best] >= 2:  # Need at least 2 hits to be confident
            return best

    # -- Default: science (safest -- formula preservation active) -------------
    return "science"


def check_brace_balance(tex_content: str) -> tuple[int, int, int]:
    """Count { and } in tex_content, ignoring those inside comments and verbatim.

    Returns (open_count, close_count, difference).
    difference > 0 means more { than }, < 0 means more } than {.
    """
    # Strip comments (lines starting with % or inline % not preceded by \)
    lines = tex_content.split("\n")
    clean_lines = []
    in_verbatim = False
    for line in lines:
        stripped = line.strip()
        # Track verbatim/lstlisting environments (skip brace counting inside)
        if re.match(r"\\begin\{(verbatim|lstlisting|minted)\}", stripped):
            in_verbatim = True
            clean_lines.append("")
            continue
        if re.match(r"\\end\{(verbatim|lstlisting|minted)\}", stripped):
            in_verbatim = False
            clean_lines.append("")
            continue
        if in_verbatim:
            clean_lines.append("")
            continue

        # \url{...} ve \href{...}{...} ICINDEKI % bir YORUM DEGILDIR (url/hyperref
        # bunlari verbatim isler). Komutlar gruplama-dengeli oldugu icin tumuyle
        # cikar -> hem brace sayimi korunur hem % yanlislikla yorum sayilmaz.
        # (Yoksa \url{...%20...} icindeki } yorum strip'le yenir -> sahte +1.)
        # 2026-07-11 (#3): href display kismi ic ice brace toleransli yapildi —
        # eski '[^}]*' kalibi \\href{u}{\\textbf{link}} icinde yarim eslesip artik
        # '}' birakiyor, DENGELI dosya -1 sayilip try_fix mesru '}' satirini
        # siliyordu. Daha derin ic ice gecis eslesmezse komut yerinde kalir —
        # dengeli oldugu icin sayim yine dogru olur.
        line = re.sub(r"\\url\{[^{}]*\}", "", line)
        line = re.sub(
            r"\\href\{[^{}]*\}\{(?:[^{}]|\{[^{}]*\})*\}", "", line
        )

        # Remove comment portion (% not preceded by \)
        cleaned = re.sub(r"(?<!\\)%.*$", "", line)
        clean_lines.append(cleaned)

    full_clean = "\n".join(clean_lines)

    # Count braces (ignore escaped \{ and \})
    # Remove escaped braces first
    no_escaped = full_clean.replace("\\{", "").replace("\\}", "")
    open_count = no_escaped.count("{")
    close_count = no_escaped.count("}")

    return open_count, close_count, open_count - close_count


# F2-01 (2026-09-29): kapanmamış '{' için eklenen '}' işaretinin yorumu.
# Eskiden eksik '}' işaretleri \end{document}'ten hemen önceye ekleniyordu:
# kapanmamış \textit{ belgenin sonuna kadar uzanıyor, XeLaTeX hata
# kurtarmasında kitabın geri kalanı İKİ KEZ basılıyordu (sözcük +%57, bölüm
# başlıkları 3 yerine 5 kez), çıkış 0. Artık '}' kapanmayan '{'nin kendi
# paragrafının sonuna konur: metin kaybolmaz, çoğalmaz; yer günlükte ve
# book_tr.tex'te bu yorumla görünür.
BRACE_FIX_COMMENT = "% auto-fix: kapanmamış '{' paragraf sonunda kapatıldı"
# Paragrafı bitiren satırlar: boş satır ya da yapı komutuyla başlayan satır.
_PARAGRAPH_END_RE = re.compile(
    r"\n(?=[ \t]*(?:\n|\\(?:part|chapter|section|subsection|subsubsection|"
    r"paragraph|begin|end|item)\b))"
)
_VERBATIM_BEGIN_RE = re.compile(r"\\begin\{(verbatim|lstlisting|minted)\}")
_VERBATIM_END_RE = re.compile(r"\\end\{(verbatim|lstlisting|minted)\}")


def _brace_scan_mask(tex_content: str) -> str:
    """check_brace_balance'ın saymadığı her şeyi AYNI UZUNLUKTA boşlukla ört.

    Konumlar korunur (satır sonları yerinde): yorumlar, verbatim/lstlisting/
    minted ortamları, \\url{...}, \\href{...}{...} ve kaçışlı \\{ \\}.
    Kural check_brace_balance ile birebir aynıdır; bu yüzden örtülmüş
    metindeki '{' / '}' sayısı onun sayımına eşittir.
    """
    def blank(match: re.Match) -> str:
        return " " * len(match.group(0))

    out_lines = []
    in_verbatim = False
    for line in tex_content.split("\n"):
        stripped = line.strip()
        if _VERBATIM_BEGIN_RE.match(stripped):
            in_verbatim = True
            out_lines.append(" " * len(line))
            continue
        if _VERBATIM_END_RE.match(stripped):
            in_verbatim = False
            out_lines.append(" " * len(line))
            continue
        if in_verbatim:
            out_lines.append(" " * len(line))
            continue
        line = re.sub(r"\\url\{[^{}]*\}", blank, line)
        line = re.sub(r"\\href\{[^{}]*\}\{(?:[^{}]|\{[^{}]*\})*\}", blank, line)
        line = re.sub(r"(?<!\\)%.*$", blank, line)
        out_lines.append(line)
    masked = "\n".join(out_lines)
    return re.sub(r"\\[{}]", "  ", masked)


def brace_fix_count(tex_content: str) -> int:
    """Metinde try_fix_brace_balance'ın paragraf sonunda kapattığı '{' sayısı
    (2_derle özetinde ve --json'da gösterilir)."""
    return sum(len(m.group(1)) for m in re.finditer(
        r"(\}+)" + re.escape(BRACE_FIX_COMMENT), tex_content))


def _unmatched_braces(tex_content: str) -> tuple[list[int], list[int]]:
    """(kapanmamış '{' konumları, eşi olmayan '}' konumları) — özgün metinde."""
    masked = _brace_scan_mask(tex_content)
    stack: list[int] = []
    orphans: list[int] = []
    for pos, ch in enumerate(masked):
        if ch == "{":
            stack.append(pos)
        elif ch == "}":
            if stack:
                stack.pop()
            else:
                orphans.append(pos)
    return stack, orphans


def _paragraph_close_position(tex_content: str, masked: str, opener: int) -> int:
    """*opener*'daki '{'nin paragrafının bittiği konum (son satırın sonu).

    Satırdaki yorumun (``%``) önüne düşer ki eklenen '}' yoruma girmesin.
    Paragraf sonu bulunamazsa (belge sonu) \\end{document}'in öncesi ya da
    metnin sonu.
    """
    # Paragraf sonu ÖZGÜN metinde aranır: yalnız yorum içeren satır TeX'te
    # paragrafı bitirmez (örtülü metinde boş görünürdü).
    match = _PARAGRAPH_END_RE.search(tex_content, opener)
    if match:
        pos = match.start()
    else:
        end_doc = re.search(r"\\end\{document\}", masked[opener:])
        pos = opener + end_doc.start() if end_doc else len(tex_content)
        # \end{document} satırının önündeki satır sonuna çekil
        while pos > opener and tex_content[pos - 1] in " \t\n":
            pos -= 1
    line_start = tex_content.rfind("\n", 0, pos) + 1
    line_start = max(line_start, opener + 1)
    comment = re.search(r"(?<!\\)%", tex_content[line_start:pos])
    if comment and masked[line_start + comment.start()] == " ":
        pos = line_start + comment.start()
    # sondaki boşluklar '}' işaretinden sonra kalsın
    while pos > opener + 1 and tex_content[pos - 1] in " \t":
        pos -= 1
    return pos


def try_fix_brace_balance(tex_content: str, logger_obj=None,
                          source_name: str = "") -> str:
    """Attempt to fix brace imbalance.

    Eksik '}' (diff > 0): kapanmayan her '{' kendi paragrafının sonunda
    kapatılır (F2-01); yeri bulunamazsa onarım yapılmaz, yapı hatası olarak
    günlüğe yazılır (belge sonuna '}' eklemek metni çoğaltıyordu).
    Fazla '}' (diff < 0): yalnız tek başına satırdaki '}' yoruma çevrilir.
    Returns the (possibly fixed) content.
    """
    open_c, close_c, diff = check_brace_balance(tex_content)

    if diff == 0:
        return tex_content  # Already balanced

    if abs(diff) > 20:
        # Too many mismatches -- likely a structural problem, don't touch
        if logger_obj:
            logger_obj.warning(
                "Brace imbalance too large (%+d), skipping auto-fix.", diff
            )
        return tex_content

    if diff > 0:
        openers, orphans = _unmatched_braces(tex_content)
        if len(openers) != diff or orphans:
            # Kapanmayan '{'ler güvenle konumlanamadı: belge sonuna '}'
            # eklemek (eski davranış) metni çoğaltıyordu. Onarım yok; satırlar
            # görünür yazılır, derleme XeLaTeX hatasını raporlar.
            if logger_obj:
                lines = [tex_content.count("\n", 0, p) + 1 for p in openers]
                logger_obj.error(
                    "Parantez dengesizliği (+%d) onarılamadı: kapanmayan '{' "
                    "yeri güvenle bulunamadı (aday satırlar: %s). Çeviride "
                    "eksik '}' işaretini elle ekleyin.",
                    diff, ", ".join(map(str, lines)) or "yok",
                )
            return tex_content
        masked = _brace_scan_mask(tex_content)
        fixes = []
        notes = []
        for opener in openers:
            pos = _paragraph_close_position(tex_content, masked, opener)
            fixes.append((opener, pos))
            snippet = " ".join(tex_content[max(0, opener - 30):opener + 40].split())
            notes.append((tex_content.count("\n", 0, opener) + 1,
                          tex_content.count("\n", 0, pos) + 1, snippet))
        # Sondan başa ekle: önceki konumlar kaymasın. Aynı paragraftaki iç
        # içe '{'ler aynı yerde ardışık '}' alır (içteki önce kapanır).
        per_position: dict[int, int] = {}
        for _opener, pos in fixes:
            per_position[pos] = per_position.get(pos, 0) + 1
        for pos in sorted(per_position, reverse=True):
            # Yorum satır sonunu yer; eklenen satır sonu paragraf ayrımını
            # korur. Özgün metinden hiçbir karakter silinmez.
            insert = "}" * per_position[pos] + BRACE_FIX_COMMENT + "\n"
            tex_content = tex_content[:pos] + insert + tex_content[pos:]
        if logger_obj:
            for line_no, close_line, snippet in notes:
                logger_obj.warning(
                    "Kapanmamış '{' (%ssatır %d: …%s…) kendi paragrafının "
                    "sonunda (satır %d) kapatıldı; çeviride eksik '}' "
                    "işaretini denetleyin.",
                    f"{source_name} " if source_name else "", line_no,
                    snippet, close_line,
                )
    else:
        # More } than { -- remove trailing orphan }
        # Only remove } that appear alone on a line or at document end
        lines = tex_content.split("\n")
        removed = 0
        for i in range(len(lines) - 1, -1, -1):
            if removed >= abs(diff):
                break
            if lines[i].strip() == "}":
                lines[i] = "% auto-fix: removed orphan }"
                removed += 1
        if removed > 0:
            tex_content = "\n".join(lines)
            if logger_obj:
                logger_obj.info(
                    "Brace balance: removed %d orphan closing brace(s).", removed
                )

    return tex_content


# ---------------------------------------------------------------------------
# Phase 1: Extraction (PDF → Markdown)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _extraction_log(output_dir: str | Path):
    """Çıkarım evresinin günlüğü: pipeline.log (EKLEME kipi, koşu ayracıyla).

    F1-12 (2026-09-29): çıkarım hatası hiçbir dosyaya yazılmıyordu; 1_cikar
    "Ayrıntı için pipeline.log'a bak" diyordu ama dosya yoktu, traceback
    atılıyordu. Bu evrede ``pipeline.extract`` ve ``latex_pipeline.extractor``
    kayıtları (traceback dahil) YALNIZ bu kitabın pipeline.log'una gider
    (toplu kipte önceki kitabın kök günlüğüne sızmaz); konsola print'ler yazar.
    """
    log = logging.getLogger("pipeline.extract")
    extractor_log = logging.getLogger("latex_pipeline.extractor")
    saved = [(lg, lg.level, lg.propagate) for lg in (log, extractor_log)]
    handler = None
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        handler = _pipeline_log_handler(output_dir)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
    except OSError:
        handler = None
    for lg, _level, _prop in saved:
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if handler is not None:
            lg.addHandler(handler)
    try:
        yield log
    finally:
        for lg, level, prop in saved:
            if handler is not None:
                lg.removeHandler(handler)
            lg.setLevel(level)
            lg.propagate = prop
        if handler is not None:
            handler.close()


def _pages_label(pages: list[int]) -> str:
    """0 tabanlı sayfa listesini kullanıcıya 1 tabanlı yaz ("sayfa 4–6").

    İlerleme sayacının biçimi ("pages a-b") KULLANILMAZ: arayüz o kalıbı
    ilerleme sanar (core/events._PAGE_PROGRESS).
    """
    if len(pages) == 1:
        return f"sayfa {pages[0] + 1}"
    return f"sayfa {pages[0] + 1}–{pages[-1] + 1}"


def _extract_pages(extractor, pdf_path: str, pages: list[int], pieces: list[str],
                   failed: list[int], log: logging.Logger) -> int:
    """*pages* aralığını çıkar; düşerse İKİYE BÖLEREK geri çekil (F1-12).

    Eskiden toplu iş düşünce aynı aralık alt süreçte tam model yüklemesiyle
    yeniden deneniyor (deterministik hata tekrarlıyordu), sonra her sayfa
    tek tek (düşen her sayfa yine ayrı alt süreç + model) deneniyordu.
    Artık 20 → 10 → 5 → … → 1: modeller ana süreçte bir kez yüklenir, yalnız
    düşen yarı yeniden denenir. Tek sayfa da düşerse sayfanın yerine görünür
    yer tutucu konur ve sayfa *failed*'a (1 tabanlı) eklenir (F1-03).
    Dönüş: bu aralıktan kaydedilen görsel sayısı.
    """
    try:
        extraction = extractor.extract(pdf_path, page_range=pages)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("Marker düştü (%s, %d sayfa): %s", _pages_label(pages),
                    len(pages), detail, exc_info=True)
        if len(pages) == 1:
            page_no = pages[0] + 1
            print(f"  Sayfa {page_no} ÇIKARILAMADI: {exc}", flush=True)
            log.error("PDF sayfa %d çıkarılamadı; full_book.md'de yerine "
                      "görünür yer tutucu kondu.", page_no)
            pieces.append(failed_page_placeholder(page_no))
            failed.append(page_no)
            return 0
        half = len(pages) // 2
        print(f"  Parça düştü ({_pages_label(pages)}): {exc} — ikiye bölünüp "
              f"yeniden deneniyor ({half} + {len(pages) - half} sayfa)",
              flush=True)
        return (_extract_pages(extractor, pdf_path, pages[:half], pieces,
                               failed, log)
                + _extract_pages(extractor, pdf_path, pages[half:], pieces,
                                 failed, log))
    pieces.append(extraction.markdown)
    images = len(extraction.images)
    print(f"  OK ({_pages_label(pages)}): {len(extraction.markdown)} chars, "
          f"{images} images", flush=True)
    return images


def run_extraction(pdf_path: str, output_dir: str, batch_size: int, device: str) -> bool:
    """Extract PDF to Markdown in batches. Returns True on success.

    Başarıda ``full_book.md`` + çıkarım kimliği (EXTRACTION_INFO_NAME) yazılır.
    Çıkarılamayan sayfalar full_book.md'de görünür yer tutucuyla işaretlenir
    (failed_pages_in_markdown okur). Kullanılabilir metin çıkmazsa
    full_book.md YAZILMAZ (sonraki koşuyu zehirlemesin; az metin varsa
    UNUSABLE_EXTRACTION_NAME'e saklanır) ve False döner.
    """

    # Set device before importing torch-dependent modules
    os.environ["TORCH_DEVICE"] = device
    if device != "cpu":
        # Avoid MPS + fork issues
        try:
            multiprocessing.set_start_method("spawn", force=True)
        except RuntimeError:
            pass
    else:
        try:
            multiprocessing.set_start_method("fork", force=True)
        except RuntimeError:
            pass

    from latex_pipeline.config import PipelineConfig
    from latex_pipeline.extractor import MarkerExtractor

    with _extraction_log(output_dir) as log:
        # F1-11: bozuk PDF ham traceback'le düşmesin.
        try:
            total_pages = detect_pages(pdf_path)
        except Exception as exc:
            print(f"HATA: PDF açılamadı; dosya bozuk ya da gerçek bir PDF "
                  f"değil. Ayrıntı: {exc}", flush=True)
            log.error("PDF açılamadı: %s", exc, exc_info=True)
            return False
        print(f"\n{'='*60}")
        print(f"  EXTRACTION: {total_pages} pages, batch={batch_size}, device={device}")
        print(f"{'='*60}\n")
        log.info("Çıkarım başladı: %s (%d sayfa, parti %d, cihaz %s)",
                 Path(pdf_path).name, total_pages, batch_size, device)

        config = PipelineConfig(input_pdf=pdf_path, output_dir=output_dir, document_class="book")
        config.ensure_dirs()

        pieces: list[str] = []
        failed: list[int] = []
        extractor = None

        for batch_start in range(0, total_pages, batch_size):
            batch_end = min(batch_start + batch_size, total_pages)
            page_range = list(range(batch_start, batch_end))

            # S1 (2026-09-25, U9): satır PDF'nin toplam sayfa sayısını da taşır
            # ("Batch: pages 40-59 / 152"); arayüz oranı yalnız bu gerçek
            # sayaçtan hesaplar (core/events._PAGE_PROGRESS iki biçimi de okur).
            print(f"[{time.strftime('%H:%M:%S')}] Batch: pages {batch_start}-{batch_end-1} "
                  f"/ {total_pages} ({len(page_range)} pages)...", flush=True)

            batch_config = PipelineConfig(
                input_pdf=pdf_path,
                output_dir=output_dir,
                document_class="book",
                page_range=page_range,
            )
            batch_config.ensure_dirs()

            if extractor is None:
                extractor = MarkerExtractor(batch_config)

            _extract_pages(extractor, pdf_path, page_range, pieces, failed,
                           log)

        full_md = "\n".join(pieces)
        out = Path(output_dir)
        md_path = out / EXTRACTION_NAME
        usable_chars = extraction_text_length(full_md)

        # (#65) Tüm parçalar düşerse boş full_book.md yazılıp True dönüyordu.
        # F1-11 (2026-09-29): kullanılamayan çıkarım full_book.md olarak
        # HİÇ yazılmaz — eskiden 0 baytlık dosya sonraki koşuda "önceki
        # çıkarım" sayılıp Marker'ı hiç çalıştırmadan body_missing veriyordu.
        if usable_chars < EXTRACTION_MIN_CHARS:
            if usable_chars:  # yalnız yer tutucuysa saklanacak metin yok
                try:
                    _unused_path(out / UNUSABLE_EXTRACTION_NAME).write_text(
                        full_md, encoding="utf-8")
                except OSError:
                    pass
            print(f"HATA: Çıkarım kullanılabilir metin üretmedi "
                  f"({usable_chars} karakter; {len(failed)}/{total_pages} "
                  f"sayfa çıkarılamadı). {EXTRACTION_NAME} yazılmadı.",
                  flush=True)
            log.error("Çıkarım kullanılabilir metin üretmedi (%d karakter, "
                      "%d/%d sayfa düştü); %s yazılmadı.", usable_chars,
                      len(failed), total_pages, EXTRACTION_NAME)
            return False

        md_path.write_text(full_md, encoding="utf-8")
        write_extraction_info(out, pdf_path, "pdf")

        # Copy images to source/images
        img_dst = out / "source" / "images"
        img_dst.mkdir(parents=True, exist_ok=True)
        img_count = 0
        for src_dir in [out / "images", img_dst]:
            if src_dir.exists() and src_dir != img_dst:
                for f in src_dir.iterdir():
                    if f.is_file() and not (img_dst / f.name).exists():
                        shutil.copy2(f, img_dst / f.name)
                        img_count += 1

        print(f"\n[{time.strftime('%H:%M:%S')}] Extraction complete: "
              f"{len(full_md)} chars, {img_count} images", flush=True)
        log.info("Çıkarım bitti: %d karakter, %d görsel.", len(full_md),
                 img_count)
        if failed:
            pages_text = ", ".join(str(p) for p in failed)
            print(f"UYARI: {len(failed)} sayfa çıkarılamadı (PDF sayfa "
                  f"{pages_text}). {EXTRACTION_NAME}'de bu sayfaların yerinde "
                  f"görünür yer tutucu var; metni kaynak PDF'den elle "
                  f"ekleyin ya da --yeniden ile yeniden deneyin. Ayrıntı: "
                  f"{out / PIPELINE_LOG_NAME}", flush=True)
            log.warning("%d sayfa çıkarılamadı: PDF sayfa %s.", len(failed),
                        pages_text)
        return True


# ---------------------------------------------------------------------------
# Phase 1-EPUB: Extraction (EPUB -> Markdown)
# ---------------------------------------------------------------------------

def run_epub_extraction(epub_path: str, output_dir: str) -> bool:
    """Extract EPUB to Markdown + images. Returns True on success."""
    from latex_pipeline.epub_extractor import EPUBExtractor

    print(f"\n{'='*60}")
    print(f"  EPUB EXTRACTION: {Path(epub_path).name}")
    print(f"{'='*60}\n")

    extractor = EPUBExtractor()

    with _extraction_log(output_dir) as log:
        try:
            result = extractor.extract(epub_path, output_dir)
        except Exception as e:
            print(f"HATA: EPUB çıkarımı başarısız: {type(e).__name__}: {e}")
            # F1-12: traceback pipeline.log'a (1_cikar oraya yönlendirir).
            log.error("EPUB çıkarımı başarısız: %s", e, exc_info=True)
            return False

        # F1-11 (2026-09-29): kullanılamayan çıkarım full_book.md olarak
        # yazılmaz (sonraki koşu onu "önceki çıkarım" sayıyordu).
        out = Path(output_dir)
        usable_chars = extraction_text_length(result.markdown)
        if usable_chars < EXTRACTION_MIN_CHARS:
            if usable_chars:
                try:
                    _unused_path(out / UNUSABLE_EXTRACTION_NAME).write_text(
                        result.markdown, encoding="utf-8")
                except OSError:
                    pass
            print(f"HATA: EPUB'dan kullanılabilir metin çıkmadı "
                  f"({usable_chars} karakter). {EXTRACTION_NAME} yazılmadı.")
            log.error("EPUB'dan kullanılabilir metin çıkmadı (%d karakter).",
                      usable_chars)
            return False
        log.info("EPUB çıkarımı bitti: %d karakter.", len(result.markdown))

    # Save markdown to full_book.md (same path as PDF pipeline)
    md_path = out / EXTRACTION_NAME
    md_path.write_text(result.markdown, encoding="utf-8")
    write_extraction_info(out, epub_path, "epub")

    print(f"[{time.strftime('%H:%M:%S')}] EPUB extraction complete:")
    print(f"  Title:    {result.metadata.get('title', 'N/A')}")
    print(f"  Author:   {result.metadata.get('author', 'N/A')}")
    print(f"  Chapters: {result.metadata.get('chapters', 0)}")
    print(f"  Markdown: {len(result.markdown)} chars")
    print(f"  Images:   {len(result.images)}")

    # EPUB çıktı için segment mapping kaydet
    try:
        from latex_pipeline.epub_output import save_segment_mapping
        mapping = save_segment_mapping(epub_path, output_dir)
        if mapping:
            print(f"  Segment mapping: {mapping}")
    except Exception as exc:
        print(f"  Segment mapping kaydedilemedi: {exc}")

    return True


# ---------------------------------------------------------------------------
# Phase 2-7: Pipeline (Markdown -> LaTeX -> Translate -> Compile)
# ---------------------------------------------------------------------------

class _PreflightFailures(list):
    """``_preflight_check`` sonucu: hata iletileri (list[str]) + model bayrağı.

    ``model_unavailable`` True ise hatalardan biri seçilen modelin
    kullanılamaması (``engine_check``: başarısız ``EngineCheck``,
    ``model_message``: listedeki satırı); run_pipeline bu durumda 1 yerine
    ``EXIT_MODEL_UNAVAILABLE`` (5) döner.
    """
    model_unavailable: bool = False
    engine_check = None
    model_message: str = ""


def _check_engine(mt_engine: str):
    """Seçilen motoru modeli YÜKLEMEDEN denetle (``discovery.check_engine``).

    Sunucu kimliklerinde en çok ``ENGINE_CHECK_HTTP_TIMEOUT_S`` sürer; ağ
    yalnız bu bilgisayara (loopback) gider. Dönüş: ``contracts.EngineCheck``.
    """
    from core.mt_engines import discovery as mt_discovery
    return mt_discovery.check_engine(mt_engine)


def _engine_failure_lines(check) -> list[str]:
    """Model kullanılamıyor iletisi: ``HATA: <neden>`` + (varsa) ``İpucu: <ipucu>``."""
    lines = [f"HATA: {check.reason}"]
    if check.hint:
        lines.append(f"İpucu: {check.hint}")
    return lines


def _engine_stop(check, output_dir: str | Path | None = None) -> int:
    """Model kullanılamıyor: iletiyi yaz, ``EXIT_MODEL_UNAVAILABLE`` dön.

    ``HATA: <neden>`` (+ varsa ``İpucu: <ipucu>``) stderr'e yazılır. Çıktı
    klasörü biliniyorsa aynı ileti pipeline.log'a ve olay akışına da
    (arayüz "error" olayı) eklenir — konsola ikinci kez basılmaz. Kitap
    dosyalarına dokunulmaz.
    """
    lines = _engine_failure_lines(check)
    for line in lines:
        print(line, file=sys.stderr)
    if output_dir is not None:
        try:
            with _stop_logger(output_dir, "pipeline.model", console=False) as log:
                log.error("%s", check.reason)
                if check.hint:
                    log.info("İpucu: %s", check.hint)
        except OSError:  # günlük yardımcı kanal; çıkış kodu değişmez
            pass
    return EXIT_MODEL_UNAVAILABLE


def _resume_skips_translation(output_dir: str | Path, resume: bool,
                              force_mt: bool) -> bool:
    """--resume çeviriyi (Phase 5) tamamen atlayacak mı?

    run_pipeline'ın resume kuralıyla aynı: ``source/translated_body.tex``
    ``RESUMABLE_MIN_BYTES``'tan büyükse Phase 5.5'e atlanır; --force-mt bu
    dosyayı yedeğe taşıyıp çeviriyi baştan koşturduğu için o durumda False.
    Model bu koşuda hiç kullanılmayacaksa denetlenmez (sunucu kapalı diye
    yalnız derleme kalan bir koşu durdurulmasın).
    """
    if not resume or force_mt:
        return False
    tr_body = Path(output_dir) / "source" / "translated_body.tex"
    try:
        return tr_body.is_file() and tr_body.stat().st_size > RESUMABLE_MIN_BYTES
    except OSError:
        return False


def _engine_precheck_applies(args, output_dir: str | Path | None = None) -> bool:
    """Motor kontrolü çıkarımdan ÖNCE bu koşuda yapılmalı mı?

    Yalnız çeviri gerçekten koşacaksa: --skip-translate, --export-tex,
    --import-tex ve --extract-only dışarıda. Klasörde Yol A kilidi
    (MANUAL_TRANSLATION.marker) varken --force-mt yoksa MT koşmaz (kilit 2
    ile durdurur ya da --resume Phase 5.5'e atlar) — denetim yapılmaz;
    --resume çeviriyi bitmiş bulacaksa da (``_resume_skips_translation``).
    Sıra (main): Yol A kilidi (2) -> dil ön kapısı (4) -> motor (5) -> çıkarım.
    """
    if (getattr(args, "skip_translate", False)
            or getattr(args, "import_tex", None)
            or getattr(args, "export_tex", False)
            or getattr(args, "extract_only", False)):
        return False
    force_mt = getattr(args, "force_mt", False)
    if output_dir is not None and not force_mt:
        if (Path(output_dir) / "source" / MANUAL_MARKER_NAME).exists():
            return False
    if output_dir is not None and _resume_skips_translation(
            output_dir, getattr(args, "resume", False), force_mt):
        return False
    return True


def _engine_check_needed(mt_engine: str) -> bool:
    """Erken (çıkarım öncesi) model kontrolü bu motor için gerekli mi?

    Varsayılan motorun (``registry.DEFAULT_ENGINE``, yerleşik MLX) modeli
    run_pipeline'daki ``_preflight_check``'te AYNI ``check_engine`` ile
    denetlenir (başarısızlık yine çıkış 5); arayüz "Çevir"i zaten
    ``local_model_dir`` ile kapatır. Önekli / kayıtlı motorlar (sunucu,
    GGUF, başka MLX klasörü) çıkarımdan ÖNCE denetlenir.
    """
    return mt_engine != mt_registry.DEFAULT_ENGINE


def _engine_description(mt_engine: str) -> str:
    """Günlük için motorun okunur adı ("qwen3:8b (Ollama)"); çözülemezse ""."""
    try:
        from core.mt_engines import discovery as mt_discovery
        return mt_discovery.engine_label(mt_registry.resolve_spec(mt_engine))
    except Exception:  # yalnız günlük süsü; koşuyu etkilemez
        return ""


def _hardware_gate_hint() -> str:
    """Genel donanım kapısı kapandığında komut satırının seçeneği: model
    gerektirmeyen TeX yolu (adı ``ui.branding``'den)."""
    from ui import branding

    return (f"Model gerektirmeyen {branding.quoted(branding.TEX_TITLE)} yolu bu Mac'te "
            "çalışır: python 1_cikar.py <kitap> (ayrıntı: CEVIRI_TALIMATI.md).")


def _hardware_check(mt_engine: str):
    """Donanım kapısı (KARARLAR 17): ağsız, modeli YÜKLEMEDEN.

    ``discovery.hardware_check``: bu Mac referans 4 bit 7B'yi çalıştıramıyorsa
    ya da süreç içi modelin (MLX/GGUF) dosyası toplu iş 1 ile bile sığmıyorsa
    ``ok=False``. Varsayılan motor dahil HER motor için çıkarımdan önce koşar;
    model dosyası yoksa hüküm vermez (onu ön kontrol söyler).
    """
    from core.mt_engines import discovery as mt_discovery
    return mt_discovery.hardware_check(mt_engine, hint=_hardware_gate_hint())


def _memory_fit(mt_engine: str):
    """Süreç içi motorun bellek hükmü (``discovery.local_memory_fit``); hesap
    yapılamazsa ya da hata olursa None (toplu iş değişmez)."""
    try:
        from core.mt_engines import discovery as mt_discovery
        return mt_discovery.local_memory_fit(mt_engine)
    except Exception:  # yardımcı hesap; koşuyu düşürmez
        return None


def _apply_memory_batch(config, fit, log: logging.Logger) -> dict:
    """Dar bellekte toplu iş boyutunu küçült (KARARLAR 17a).

    Sığan en büyük toplu iş ``DEFAULT_BATCH_SIZE_HYMT_LOCAL``'dan küçükse
    ``PipelineConfig.mlx_batch_size`` (LatexTranslator alt toplu işi) ona
    indirilir ve ``TranslationConfig.batch_size`` için ``translation_overrides``
    döner. Sığıyorsa (ör. 36 GB'lık Mac'te HY-MT) HİÇBİR şey değişmez: boş
    sözlük döner ve LatexTranslator'a ek argüman geçmez.
    """
    from config.defaults import DEFAULT_BATCH_SIZE_HYMT_LOCAL
    from core import hardware

    if fit is None or fit.mode != hardware.MODE_BATCHED or not fit.batch_size:
        return {}
    if fit.batch_size >= DEFAULT_BATCH_SIZE_HYMT_LOCAL:
        return {}
    config.mlx_batch_size = min(int(config.mlx_batch_size), int(fit.batch_size))
    log.info("%s", hardware.batch_note(fit))
    return {"translation_overrides": {"batch_size": int(fit.batch_size)}}


def _preflight_check(
    skip_translate: bool,
    skip_compile: bool,
    pdf_path: str | None = None,
    mt_engine: str = mt_registry.DEFAULT_ENGINE,
    *,
    engine_checked: bool = False,
) -> list[str]:
    """Pre-flight health check before pipeline starts.

    Returns a list of failure messages. Empty list = all checks passed.
    Dönüş ``_PreflightFailures``: model kullanılamıyorsa ``model_unavailable``
    True (çıkış 5). *engine_checked* True ise model denetimi atlanır (çağıran
    ``_check_engine``'i zaten koştu).
    """
    import subprocess

    failures = _PreflightFailures()

    # 1. XeLaTeX installed?
    if not skip_compile:
        if shutil.which("xelatex") is None:
            failures.append(
                "XeLaTeX not found on PATH. Install TeX Live or MacTeX."
            )

    # 2. Seçilen model kullanılabilir mi? — core'un gerçekten yükleyeceği
    # yolla birebir (MAP-2, 2026-07-27: eski kod 'hymt-1.8b-mlx' varlığını da
    # yeterli sayıyordu, preflight geçip dakikalar süren extraction SONRASI
    # çöküyordu). 2026-09-25: denetim discovery.check_engine'de (model
    # YÜKLENMEZ): MLX dalında klasör motorun resolve_local_model_dir'iyle
    # bulunur (config.json + *.safetensors + tokenizer + mlx-lm mimari
    # desteği); gguf: dosya + başlık + llama-cpp-python; sunucu kimliklerinde
    # sunucu açık mı ve model listede mi. Başarısızlık = çıkış 5.
    if not skip_translate and not engine_checked:
        _check = _check_engine(mt_engine)
        if not _check.ok:
            message = _check.reason + (f" İpucu: {_check.hint}" if _check.hint else "")
            failures.append(message)
            failures.model_unavailable = True
            failures.engine_check = _check
            failures.model_message = message

    # 3. Font installed? (STIX Two Text)
    if not skip_compile:
        try:
            result = subprocess.run(
                ["fc-list", ":family=STIX Two Text"],
                capture_output=True, text=True, timeout=10,
            )
            if "STIX Two Text" not in result.stdout:
                failures.append(
                    "Font 'STIX Two Text' not found. "
                    "Install STIX Two fonts for XeLaTeX."
                )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # fc-list not available -- skip font check
            pass

    # 4. Input PDF exists and readable?
    if pdf_path is not None:
        p = Path(pdf_path)
        if not p.exists():
            failures.append(f"Input PDF not found: {pdf_path}")
        elif not p.is_file():
            failures.append(f"Input PDF is not a file: {pdf_path}")
        elif p.stat().st_size == 0:
            failures.append(f"Input PDF is empty (0 bytes): {pdf_path}")

    # 5. Disk space check (min 5 GB free)
    try:
        usage = shutil.disk_usage(PROJECT_ROOT)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 5.0:
            failures.append(
                f"Low disk space: {free_gb:.1f} GB free (min 5 GB required). "
                "Free up space before running the pipeline."
            )
    except OSError:
        pass  # disk_usage not available on some systems

    # 6. MPS GPU quick test (allocate + dealloc a small tensor)
    if not skip_translate:
        try:
            import torch
            if torch.backends.mps.is_available():
                _t = torch.zeros(1, device="mps")
                del _t
                # Success -- MPS is working
            # If MPS not available, CPU fallback will be used -- not a failure
        except Exception as exc:
            failures.append(
                f"MPS GPU test failed: {exc}. "
                "Translation may fail. Consider using --device cpu."
            )

    return failures


# ---------------------------------------------------------------------------
# Çeviri kalite kapısı (F3, 2026-07-28)
# ---------------------------------------------------------------------------
# Eşik, latex_pipeline/translator.py'deki FINAL checkpoint reddiyle AYNI olmak
# ZORUNDA: orası ">%50 başarısız" gövdeyi checkpoint'e yazmayı reddediyor,
# burası ise aynı gövdeyi koşulsuz translated_body.tex'e yazıp faz düzeyindeki
# --resume'a 'çeviri' diye yediriyordu (checkpoint reddi fiilen bypass).
# Eşik config/defaults.py'de tek kaynak; translator.py de oradan import eder.
from config.defaults import TRANSLATION_MAX_FAIL_RATIO  # noqa: E402


def _translator_backend_problem(translator) -> str | None:
    """Çevirmenin arka uç sağlığı (``LatexTranslator.backend_health``): sorun
    yoksa ya da çevirmen bunu sunmuyorsa None, varsa Türkçe neden."""
    health = getattr(translator, "backend_health", None)
    if not callable(health):
        return None
    try:
        problem = health()
    except Exception as exc:  # sağlık sorgusu koşuyu düşürmez
        logging.getLogger("pipeline").warning("backend_health sorgusu başarısız: %s", exc)
        return None
    return problem if isinstance(problem, str) and problem.strip() else None


def _translation_body_usable(stats) -> tuple[bool, str]:
    """Phase 5 çıktısı 'çeviri' sayılıp diske/resume'a verilebilir mi?

    translator.py'deki FINAL checkpoint kararının aynısı: hiç segment
    çevrilmediyse ya da segmentlerin %50'sinden fazlası başarısızsa gövde
    çeviri değildir.  ``(True, "")`` veya ``(False, sebep)`` döner.
    """
    total = int(getattr(stats, "segments_total", 0) or 0)
    if total <= 0:
        # Segment sayacı yoksa karar veremeyiz; eski davranışta kal.
        return True, ""
    translated = int(getattr(stats, "segments_translated", 0) or 0)
    failed = int(getattr(stats, "segments_failed", 0) or 0)
    if translated == 0:
        return False, f"{total} segmentin hiçbiri çevrilemedi"
    ratio = failed / total
    if ratio > TRANSLATION_MAX_FAIL_RATIO:
        return False, (
            f"segmentlerin %{ratio * 100:.0f}'i başarısız ({failed}/{total})"
        )
    return True, ""


# ---------------------------------------------------------------------------
# Çevrilmiş gövde okuma — kodlama güvenliği (F14, 2026-07-28)
# ---------------------------------------------------------------------------

def _read_translated_body(path: Path) -> str:
    """Çevrilmiş .tex gövdesini oku: BOM yutulur, yanlış kodlama SESSİZ geçmez.

    - ``utf-8-sig``: editörler dosyayı BOM'lu kaydedebiliyor; düz utf-8
      okumada BOM gövdenin başına ``\\ufeff`` olarak sızıp book_tr.tex'e
      taşınıyordu (A-13 yalnız META başlığını düzeltmişti).
    - ``errors=`` YOK: Latin-1/Windows-1254 kaydedilmiş dosya 'replace' ile
      sessizce bozuk okunmaz; UnicodeDecodeError yükselir, çağıran taraf
      kullanıcıya net Türkçe hata + sıfır dışı çıkış kodu verir.
    """
    return path.read_text(encoding="utf-8-sig")


def _encoding_error_hint(path: Path, exc: UnicodeDecodeError) -> str:
    """UnicodeDecodeError için kullanıcıya dönecek Türkçe yönerge."""
    return (
        f"{path} dosyası UTF-8 değil (bayt konumu {exc.start}: {exc.reason}). "
        "Çeviri dosyasını UTF-8 olarak kaydedip tekrar deneyin. "
        "VS Code: 'Save with Encoding' -> UTF-8. "
        "Terminal: iconv -f WINDOWS-1254 -t UTF-8 dosya.tex > dosya_utf8.tex. "
        "Dosya bilerek bozuk okunmadı: yanlış kodlamada Türkçe karakterler "
        "sessizce bozulur ve PDF'ye bozuk girer."
    )


def _load_curated_terms(glossary_dir: Path) -> Dict[str, str]:
    """Load the hand-curated EN->TR term set from ``glossary/curated.json``.

    GLS-14: TerminologyEngine.build_glossary(curated_terms=None) leaves the
    curated-collision filter INACTIVE, so auto-mined dictionary terms could
    silently override deliberately curated translations (D5).  Feeding the real
    curated set makes filter_terms() drop any auto-term that collides with a
    curated entry -- the curated translation wins.

    Returns ``{en_lower: tr}``.  Missing/malformed file -> empty dict (the
    caller then behaves as before, so this can never break the pipeline).

    F11 (2026-07-28): tek çağıranı olan Phase 4.5 kaldırıldı; fonksiyon
    korundu — app.py'nin auto-terms bloğu ve olası Phase 5.75 bağlantısı için
    gereken curated set yükleyicisi budur (davranışını
    tests/test_yolb2_chain2.py kilitliyor).
    """
    import json
    curated_path = glossary_dir / "curated.json"
    if not curated_path.exists():
        return {}
    try:
        data = json.loads(curated_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: Dict[str, str] = {}
    terms = data.get("terms", {})
    if isinstance(terms, dict):
        for en, info in terms.items():
            if isinstance(info, dict):
                tr = str(info.get("tr", "")).strip()
            elif isinstance(info, str):
                tr = info.strip()
            else:
                continue
            if tr:
                out[en.lower()] = tr
    return out


# G2c (KARAR 25, 2026-10-05): kitap sözlüğü — sabit genel sözlük yok; her
# kitap kendi sözlüğünü kaynak dilde çıkarır (latex_pipeline/kitap_sozlugu.py;
# kitap klasöründe glossary_shared.tsv, TeX yoluyla ortak
# biçim).

def _kitap_sozlugu_istemi_acik() -> bool:
    """İstem düzeyi terim desteği açık mı? (varsayılan KAPALI; G2c)"""
    try:
        from latex_pipeline.kitap_sozlugu import istem_terimleri_acik
        return istem_terimleri_acik()
    except Exception:
        return False


def _kitap_sozlugunu_hazirla(translator, latex_body: str, out: Path,
                             log: logging.Logger):
    """Sözlüğü hazırla ve çevirmene bağla: klasördeki dosya varsa yükle,
    yoksa çıkar + seçili motorla bağlamlı çevir + yaz.

    Çevirmende ``kitap_sozlugu_hazirla`` yoksa (sahte çevirmen) ya da hata
    verirse None — koşu sözlüksüz sürer (eski davranış).
    """
    hazirla = getattr(translator, "kitap_sozlugu_hazirla", None)
    if not callable(hazirla):
        return None
    try:
        sozluk = hazirla(latex_body, out)
        adet = len(sozluk)
    except Exception as exc:  # sözlük çeviriyi durdurmaz
        log.warning("Kitap sözlüğü hazırlanamadı: %s", exc)
        return None
    log.info("  Kitap sözlüğü: %d terim (%s)", adet, out / "glossary_shared.tsv")
    return sozluk if adet else None


def _kitap_sozlugunu_yukle(out: Path, source_lang: str, translator,
                           log: logging.Logger):
    """Sürdürülen koşu: klasördeki glossary_shared.tsv (varsa) yüklenir ve
    yeniden çeviri fazlarının çevirmenine bağlanır."""
    try:
        from latex_pipeline.kitap_sozlugu import KitapSozlugu, SOZLUK_DOSYA_ADI
        yol = out / SOZLUK_DOSYA_ADI
        if not yol.is_file():
            return None
        sozluk = KitapSozlugu.dosyadan_yukle(yol, source_lang or "en")
    except Exception as exc:
        log.warning("Kitap sözlüğü okunamadı: %s", exc)
        return None
    if not len(sozluk):
        return None
    ata = getattr(translator, "kitap_sozlugu_ata", None)
    if callable(ata):
        ata(sozluk)
    log.info("  Kitap sözlüğü yüklendi: %d terim (%s)", len(sozluk), yol)
    return sozluk


# F36 (2026-07-29): aynı gövde art arda 4+ kez tam analiz ediliyordu
# (Phase 5.5 initial, qc_report.txt, dış döngü skoru, çıkış kapısı). Analiz
# ~30 check'i tüm belgede koşuyor; ölçümde 663 KB gövdede 4,4 s. analyze
# deterministiktir (aynı gövde+kaynak+bayrak -> aynı rapor), bu yüzden
# sonuç içerik hash'iyle bellekte tutulur.
# H1/R10 (2026-07-29): cache CANLI nesne paylaşır — mutasyon yapacak her
# tüketici KENDİ kopyası üzerinde çalışmak zorundadır. Bilinen tek mutasyon
# noktası AutoFixer'ın AF-09 line_num remap'iydi; artık kopyalarla çalışıyor
# (autofix.py H1). Yeni bir tüketici issue/report değiştirecekse önce
# kopyalasın, yoksa cache'ten dönen sonraki okumalar zehirlenir.
_QC_CACHE_MAX = 8


def _qc_cache_key(translated_body: str, source_body: str | None,
                  manual_translation: bool) -> tuple:
    """Memoization anahtarı: gövde + kaynak içeriği + manuel bayrağı."""
    return (
        hashlib.sha1((translated_body or "").encode("utf-8")).hexdigest(),
        hashlib.sha1((source_body or "").encode("utf-8")).hexdigest(),
        bool(manual_translation),
    )


def _qc_analyze(qc_engine, translated_body: str, source_body: str,
                manual_translation: bool = False, cache: dict | None = None):
    """Call QCEngine.analyze with the manual_translation flag.

    2026-07-17 (#12): eski TypeError fallback'i kaldirildi — imza artik
    kalici (qc_engine.py analyze(manual_translation=...)). Fallback,
    analyze ICINDEKI gercek bir TypeError'i imza uyusmazligi sanip cagriyi
    manual_translation OLMADAN tekrarliyordu: Yol A kitabi sessizce Yol B
    modunda QC'leniyor ve asil hata yutuluyordu.

    *cache* verilirse (run_pipeline koşu-ömürlü sözlüğü) aynı gövde+kaynak+
    bayrak üçlüsü için analiz TEKRARLANMAZ; sonuç aynen döner (F36).
    """
    if cache is None:
        return qc_engine.analyze(
            translated_body, source_body,
            manual_translation=manual_translation,
        )

    key = _qc_cache_key(translated_body, source_body, manual_translation)
    hit = cache.get(key)
    if hit is not None:
        return hit
    report = qc_engine.analyze(
        translated_body, source_body,
        manual_translation=manual_translation,
    )
    if len(cache) >= _QC_CACHE_MAX:
        # Koşu başına farklı gövde sayısı azdır; yine de sınırsız büyümesin.
        cache.clear()
    cache[key] = report
    return report


def _supported_languages_text() -> str:
    """Desteklenen kaynak dillerin Türkçe listesi (tek kaynak TEX_SOURCE_LANGS)."""
    from core.lang_detect import TEX_SOURCE_LANGS, language_list_tr

    return language_list_tr(TEX_SOURCE_LANGS, " ve ")


def _log_source_unsupported(est, log: logging.Logger) -> None:
    """Kaynak dil kapısının durdurma iletisi (ön kapı ile tam kapı ORTAK).

    Arayüz ERROR kaydını core/events.py üzerinden "error" olayı olarak
    gösterir; iki kapı aynı iletiyi verir ki kullanıcı hangi aşamada
    durulduğundan bağımsız aynı yönlendirmeyi görsün. 2026-09-28 (K5):
    yerel model 6 kaynak dili çevirir; TeX yolunun (ui.branding.TEX_TITLE)
    listesi de aynı olduğu için o yol önerilmez, tespit yanlışsa bayraklar
    önerilir.
    """
    from core.lang_detect import GATE_STOP_CONFIDENCE

    log.error(
        "Bu kitabın dili desteklenmiyor (tespit: %s). Yerel model yalnız şu "
        "dillerden Türkçeye çevirir: %s.",
        est.name_tr, _supported_languages_text(),
    )
    log.info(
        "Tespitin yanlış olduğundan eminsen kitabın dilini --kaynak-dil ile "
        "verebilir ya da kapıyı --dil-kontrolu-yok ile atlayabilirsin "
        "(güven=%s, eşik=%s).",
        _tr_decimal(est.confidence, 2), _tr_decimal(GATE_STOP_CONFIDENCE, 2),
    )


def _source_language_decision(source_text: str, log: logging.Logger, *,
                              check: bool = True,
                              forced: str | None = None,
                              stop: bool = True) -> tuple[int | None, str]:
    """"Yerel model ile çevir" kaynak dil kapısı + zincire akacak kaynak dil.

    Dönüş ``(çıkış_kodu, kaynak_dil)``: çıkış kodu None ise çeviri sürer ve
    kaynak dil (``TEX_SOURCE_LANGS``'ten biri) PipelineConfig /
    LatexTranslator / BookTranslator'a akar (istemdeki dil adı, çevrilmemiş
    satır denetimi, bellek havuzu). Karar (KARARLAR 14, 2026-09-28):

    * *forced* (``--kaynak-dil``): ölçüm kararı bağlamaz, dil o olur.
    * *check* False (``--dil-kontrolu-yok``): kapı yok, İngilizce varsayılır
      (bayrağın eski anlamı; başka dil için ``--kaynak-dil``).
    * Tespit İngilizce -> "en" (eskisi gibi, güvenden bağımsız).
    * Tespit desteklenen başka bir dil ve güven >= ``GATE_STOP_CONFIDENCE``
      -> o dil.
    * Desteklenmeyen dil güvenle ölçüldü -> ``EXIT_SOURCE_UNSUPPORTED``
      (*stop* False ise yalnız uyarı + İngilizce varsayımı: resume yolu).
    * Belirsiz / düşük güven -> uyarı + İngilizce varsayımı (eski davranış;
      yanlış pozitifin maliyeti yüksek).

    ERROR/uyarılar arayüzde core/events.py üzerinden görünür. Yol A'da
    (--export-tex / --import-tex, manual_translation) ÇAĞRILMAZ.
    """
    from core.lang_detect import (
        GATE_STOP_CONFIDENCE,
        TEX_SOURCE_LANGS,
        analyze_language,
        language_name_tr,
        should_stop_for_source_language,
    )

    if forced:
        log.info("Kaynak dil elle verildi (--kaynak-dil): %s (kod=%s); dil "
                 "kapısı atlandı.", language_name_tr(forced), forced)
        return None, forced
    if not check:
        log.warning(
            "Kaynak dil kapısı --dil-kontrolu-yok ile atlandı; kitabın "
            "İngilizce olduğu varsayılıyor (başka bir dilse --kaynak-dil ile "
            "belirt)."
        )
        return None, "en"
    try:
        est = analyze_language(source_text)
    except Exception as exc:  # ölçüm hatası çeviriyi düşürmez
        log.warning("Kaynak dil ölçülemedi (%s); dil kapısı atlandı, "
                    "İngilizce varsayılıyor.", exc)
        return None, "en"

    log.info(
        "Kaynak dil kapısı: %s (kod=%s, güven=%s, ölçülen harf=%d)",
        est.name_tr, est.code, _tr_decimal(est.confidence, 2), est.letters,
    )
    if est.code == "en":
        return None, "en"
    if (est.code in TEX_SOURCE_LANGS
            and est.confidence >= GATE_STOP_CONFIDENCE):
        log.info("Kaynak dil: %s — çeviri %s -> Türkçe yapılacak.",
                 est.name_tr, est.name_tr)
        return None, est.code
    if should_stop_for_source_language(est):
        if stop:
            _log_source_unsupported(est, log)
            return EXIT_SOURCE_UNSUPPORTED, "en"
        log.warning(
            "Kaynak dil desteklenmiyor görünüyor (tespit: %s, güven=%s); "
            "İngilizce varsayılarak devam ediliyor.",
            est.name_tr, _tr_decimal(est.confidence, 2),
        )
        return None, "en"
    if est.code == "und":
        log.warning(
            "Kaynak dil belirlenemedi (ölçülebilir düzyazı çok az); "
            "İngilizce varsayılarak devam ediliyor. Kitap başka bir "
            "dildeyse dilini --kaynak-dil ile belirt (İngilizce varsayımında "
            "başka dildeki paragraflar alıntı sayılıp çevrilmeden kalır)."
        )
    else:
        log.warning(
            "Kaynak dil belirsiz (en olası: %s, güven=%s < eşik %s); "
            "İngilizce varsayılarak devam ediliyor. Kitap başka bir "
            "dildeyse dilini --kaynak-dil ile belirt.",
            est.name_tr, _tr_decimal(est.confidence, 2),
            _tr_decimal(GATE_STOP_CONFIDENCE, 2),
        )
    return None, "en"


def _source_language_gate(source_text: str, log: logging.Logger) -> int | None:
    """Kaynak dil kapısının yalnız çıkış kodu (bkz. _source_language_decision)."""
    return _source_language_decision(source_text, log)[0]


def _pdf_text_sample(pdf_path: str) -> str:
    """PDF'nin gömülü metin katmanından eşit aralıklı sayfa örneği (OCR yok).

    En çok ``PREGATE_MAX_PAGES`` sayfa belgeye yayılarak seçilir (yalnız ilk
    sayfalar değil: kapak, künye ve içindekiler çoğu zaman düzyazı değildir);
    her sayfadan en çok ``PREGATE_MAX_CHARS / PREGATE_MAX_PAGES`` karakter
    alınır. Metin katmanı olmayan taramada boş döner.
    """
    import fitz
    from core.lang_detect import PREGATE_MAX_CHARS, PREGATE_MAX_PAGES

    per_page = PREGATE_MAX_CHARS // PREGATE_MAX_PAGES
    doc = fitz.open(pdf_path)
    try:
        n = len(doc)
        k = min(PREGATE_MAX_PAGES, n)
        indices = sorted({int((i + 0.5) * n / k) for i in range(k)})
        return "\n".join(doc[i].get_text("text")[:per_page] for i in indices)
    finally:
        doc.close()


_EPUB_SKIP_BLOCK_RE = re.compile(
    r"<(head|script|style)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_EPUB_TAG_RE = re.compile(r"<[^>]+>")


def _epub_text_sample(epub_path: str) -> str:
    """EPUB'daki (X)HTML belgelerinin düz metninden sınırlı örnek.

    Belgeler arşiv sırasıyla okunur, etiketler atılır; toplam
    ``PREGATE_MAX_CHARS`` karakter belgelere eşit aralıklı pencerelerle
    dağıtılır (tek bir uzun bölüm örneği ele geçirmez).
    """
    import html
    import zipfile
    from core.lang_detect import (PREGATE_MAX_CHARS, PREGATE_MAX_PAGES,
                                  sample_text)

    texts: list[str] = []
    with zipfile.ZipFile(epub_path) as zf:
        for name in zf.namelist():
            if not name.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            raw = zf.read(name).decode("utf-8", errors="replace")
            raw = _EPUB_SKIP_BLOCK_RE.sub(" ", raw)
            texts.append(html.unescape(_EPUB_TAG_RE.sub(" ", raw)))
    return sample_text("\n".join(texts), PREGATE_MAX_CHARS, PREGATE_MAX_PAGES)


def _pregate_applies(args, output_dir: str, *, extraction_runs: bool) -> bool:
    """Çıkarım öncesi dil ön kapısı bu koşuda çalışmalı mı?

    Yalnız yerel model yolunda ve MT'nin gerçekten koşacağı TAZE koşuda:
    --export-tex / --import-tex (skip_translate), --extract-only,
    --dil-kontrolu-yok, --kaynak-dil (dil elle verildi), --resume ve
    çıkarımın atlandığı koşular dışarıda.
    Yol A kilidi (MANUAL_TRANSLATION.marker) varken --force-mt yoksa kilidin
    kendi iletisi (exit 2) öncelikli kalır — tam kapıyla aynı sıra.
    1_cikar.py / 2_derle.py bu fonksiyonu hiç çağırmaz.
    """
    if not extraction_runs:
        return False
    if (getattr(args, "dil_kontrolu_yok", False)
            or getattr(args, "kaynak_dil", None)):
        return False
    if (args.skip_translate or getattr(args, "import_tex", None)
            or getattr(args, "export_tex", False)
            or getattr(args, "extract_only", False)
            or getattr(args, "resume", False)):
        return False
    marker = Path(output_dir) / "source" / MANUAL_MARKER_NAME
    if marker.exists() and not getattr(args, "force_mt", False):
        return False
    return True


def _source_language_pregate(input_path: str, input_format: str,
                             output_dir: str) -> int | None:
    """Kaynak dil ÖN kapısı: Marker OCR / çıkarım başlamadan önce.

    PDF'nin metin katmanı ya da EPUB metni üzerinde ölçer; karar
    :func:`core.lang_detect.should_stop_before_extraction` ile verilir (tam
    kapıdan sıkı). Kitap güvenle desteklenmeyen bir dildeyse (2026-09-28:
    TEX_SOURCE_LANGS dışında) tam kapıyla AYNI ileti ve
    ``EXIT_SOURCE_UNSUPPORTED`` ile durur; desteklenen dil, metin katmanı
    yok/az, ölçüm hatası ya da belirsiz sonuçta SESSİZCE ``None`` döner —
    tam kapı çıkarımdan sonra aynen çalışır.
    """
    from core.lang_detect import (
        analyze_language,
        clean_plain_text_for_detection,
        should_stop_before_extraction,
    )

    try:
        if input_format == "pdf":
            text = _pdf_text_sample(input_path)
        else:
            text = _epub_text_sample(input_path)
        est = analyze_language(clean_plain_text_for_detection(text),
                               already_clean=True)
    except Exception:  # ön kapı yalnız hızlandırıcı; hata çeviriyi düşürmez
        return None
    if not should_stop_before_extraction(est):
        return None

    # Yalnız durdururken günlük kurulur: geçen koşuda pipeline.log'a ve olay
    # akışına dokunulmaz (run_pipeline onları kendisi kurar).
    with _stop_logger(output_dir, "pipeline.pregate") as log:
        log.info(
            "Kaynak dil ön kapısı (çıkarımdan önce, %s): %s "
            "(kod=%s, güven=%s, ölçülen harf=%d)",
            ("PDF metin katmanından" if input_format == "pdf"
             else "EPUB metninden"),
            est.name_tr, est.code, _tr_decimal(est.confidence, 2), est.letters,
        )
        _log_source_unsupported(est, log)
    return EXIT_SOURCE_UNSUPPORTED


# ---------------------------------------------------------------------------
# pipeline.log ve çıkarım öncesi durdurma günlüğü
# ---------------------------------------------------------------------------

def _run_separator() -> str:
    """pipeline.log'da bir koşunun başını gösteren ayraç satırı."""
    return (f"{'=' * 20} Yeni koşu · "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
            f"{Path(sys.argv[0]).name or 'python'} · pid {os.getpid()} "
            f"{'=' * 20}\n")


def _pipeline_log_handler(output_dir: str | Path) -> logging.FileHandler:
    """``pipeline.log``'u EKLEME kipinde açar, başına koşu ayracı yazar.

    2026-09-25 (U4, S3): eskiden ``mode="w"`` idi — Yol A kilidiyle
    reddedilen bir yerel model koşusu bile önceki derlemenin günlüğünü
    siliyordu (14 KB → 1,6 KB). Artık her koşu dosyanın sonuna eklenir.
    Günlük ``PIPELINE_LOG_MAX_BYTES``'ı aşarsa önce
    ``PIPELINE_LOG_PREVIOUS_NAME``'e taşınır (sınırsız büyümez).
    """
    path = Path(output_dir) / PIPELINE_LOG_NAME
    try:
        if path.exists() and path.stat().st_size > PIPELINE_LOG_MAX_BYTES:
            path.replace(path.with_name(PIPELINE_LOG_PREVIOUS_NAME))
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as stream:
        if stream.tell() > 0:
            stream.write("\n")
        stream.write(_run_separator())
    return logging.FileHandler(path, mode="a", encoding="utf-8")


@contextlib.contextmanager
def _stop_logger(output_dir: str | Path, name: str, *, console: bool = True):
    """Çıkarımdan ÖNCE duran kapılar için geçici günlük (konsol + pipeline.log
    + olay akışı). run_pipeline'ın kök günlüğüne dokunmaz; çıkışta söküler.

    Arayüz ERROR kaydını core/events.py üzerinden "error" olayı olarak alır.
    *console* False ise konsola yazılmaz (ileti zaten stderr'e basıldıysa).
    """
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.propagate = False
    handlers: list[logging.Handler] = []
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
        handlers.append(console_handler)
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        file_handler = _pipeline_log_handler(output_dir)
        file_handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        handlers.append(file_handler)
    except OSError:
        pass
    for handler in handlers:
        log.addHandler(handler)
    try:
        from core.events import install_event_log
        install_event_log(logger=log)
    except Exception:  # olay akışı yardımcı kanal
        pass
    try:
        yield log
    finally:
        for handler in list(log.handlers):
            log.removeHandler(handler)
            handler.close()


# ---------------------------------------------------------------------------
# Yol A kilidi (TeX yoluyla yapılmış çeviri) — çıkarımdan ÖNCE
# ---------------------------------------------------------------------------

def _branding_title(name: str) -> str:
    """Kullanıcıya dönük yol adı (tek kaynak ``ui.branding``; "Yol A/B" iç adı
    ekranda ve komut satırı çıktısında kullanılmaz)."""
    from ui import branding

    return getattr(branding, name)


def _cli_epilog() -> str:
    """``--help`` sonundaki açıklama: modül belgesi; yol adları branding'den,
    dil listesi ``core.lang_detect.TEX_SOURCE_LANGS``'ten (tek kaynak)."""
    from core.lang_detect import TEX_SOURCE_LANGS, language_list_tr

    return (__doc__ or "").format(tex=_branding_title("TEX_TITLE"),
                                  model=_branding_title("MODEL_TITLE"),
                                  langs=language_list_tr(TEX_SOURCE_LANGS),
                                  codes=", ".join(TEX_SOURCE_LANGS))


def yol_a_lock_message() -> str:
    """Kilidin kullanıcıya dönük iletisi (ön kilit ve pipeline içi ORTAK).

    Yolun adı arayüzdeki kartla aynı olsun diye ``ui.branding.TEX_TITLE``'dan
    okunur ("Yol A" iç adı ekranda kullanılmaz).
    """
    from ui.branding import TEX_TITLE

    return (f"Bu klasörde '{TEX_TITLE}' ile yapılmış bir çeviri var "
            f"(source/{MANUAL_MARKER_NAME}). Yerel model onu ezmemek için "
            f"durdu.")


def _yol_a_lock_hint() -> str:
    """Komut satırı kullanıcısı için kilidin seçenekleri."""
    return ("Seçenekler: yerel modeli denemek için farklı bir --name ver; "
            "mevcut çeviriyi yeniden dizmek için 2_derle.py ile "
            "EXPORT_TR.tex'i derle (ya da --resume); çeviriyi bilerek "
            "yerel modelle ezmek için --force-mt.")


def _yol_a_lock_applies(args, output_dir: str | Path) -> bool:
    """Bu koşu Yol A kilidine takılacak mı? (çıkarımdan ÖNCE sorulur)

    run_pipeline içindeki kilitle aynı koşul: klasörde
    ``source/MANUAL_TRANSLATION.marker`` var, ``--force-mt`` yok ve MT
    gerçekten koşacak. MT'nin koşmadığı koşular kilide takılmaz:
    --export-tex / --import-tex / --skip-translate (skip_translate),
    --extract-only ve --resume ile kullanılabilir ``translated_body.tex``
    (Phase 5.5'e atlanır, mevcut çeviri korunur).

    2026-09-25 (U4, S3): kilit eskiden yalnız pipeline içinde, çıkarım ve
    Markdown→LaTeX dönüşümünden SONRA denetleniyordu; reddedilen koşu
    full_book.md, epub_segment_mapping.json, source/body.tex ve book.tex'i
    yeniden yazıp pipeline.log'u siliyordu.
    """
    source = Path(output_dir) / "source"
    if not (source / MANUAL_MARKER_NAME).exists():
        return False
    if getattr(args, "force_mt", False):
        return False
    if (getattr(args, "skip_translate", False)
            or getattr(args, "import_tex", None)
            or getattr(args, "export_tex", False)
            or getattr(args, "extract_only", False)):
        return False
    if getattr(args, "resume", False):
        translated = source / "translated_body.tex"
        try:
            if (translated.exists()
                    and translated.stat().st_size > RESUMABLE_MIN_BYTES):
                return False
        except OSError:
            pass
    return True


def _yol_a_lock_stop(output_dir: str | Path) -> int:
    """Kilit iletisini (konsol + pipeline.log + olay akışı) yaz, çıkış 2.

    Hiçbir kitap dosyasına dokunulmaz; pipeline.log'a yalnız EKLENİR.
    """
    with _stop_logger(output_dir, "pipeline.lock") as log:
        log.error(yol_a_lock_message())
        log.info(_yol_a_lock_hint())
    return EXIT_MANUAL_LOCK


def _write_mt_notice_info(source_dir: str | Path, mt_engine: str, source_lang: str,
                          log: logging.Logger) -> None:
    """Yerel model çevirisinin künye kaydını yaz (MT_NOTICE_INFO_NAME).

    Yalnız Phase 5 çeviriyi başarıyla ``translated_body.tex``'e yazınca
    çağrılır. Yazılamazsa koşu düşmez (künye "kaydedilmemiş" der).
    """
    info = {
        "engine": mt_engine,
        "model": _engine_description(mt_engine) or mt_engine,
        "source_lang": source_lang,
        "date": datetime.now().date().isoformat(),
    }
    try:
        (Path(source_dir) / MT_NOTICE_INFO_NAME).write_text(
            json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("%s yazılamadı: %s", MT_NOTICE_INFO_NAME, exc)


def _read_mt_notice_info(source_dir: Path) -> dict | None:
    """Künye kaydı (bozuk / eksik alanlı kayıt yok sayılır); yoksa None."""
    try:
        data = json.loads((source_dir / MT_NOTICE_INFO_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        day = datetime.strptime(str(data.get("date", "")), "%Y-%m-%d").date()
    except ValueError:
        return None
    model = data.get("model")
    lang = data.get("source_lang")
    return {"model": model if isinstance(model, str) and model.strip() else None,
            "source_lang": lang if isinstance(lang, str) and lang.strip() else None,
            "date": day}


def _measured_source_lang(source_dir: Path) -> str | None:
    """Kayıt yoksa künyenin kaynak dili: kaynak gövdenin (body.tex) ölçümü.

    Dil kapısının kuralıyla (güvenle ölçülen desteklenen dil, İngilizce her
    güvende); belirsizse None ("belirlenemedi").
    """
    try:
        text = (source_dir / "body.tex").read_text(encoding="utf-8", errors="replace")
        from core.lang_detect import GATE_STOP_CONFIDENCE, TEX_SOURCE_LANGS, analyze_language
        est = analyze_language(text)
    except Exception:  # künye yardımcısı; ölçülemezse "belirlenemedi"
        return None
    if est.code == "en" or (est.code in TEX_SOURCE_LANGS
                            and est.confidence >= GATE_STOP_CONFIDENCE):
        return est.code
    return None


def mt_notice_fields(output_dir: str | Path) -> dict | None:
    """Klasördeki çevirinin makine çevirisi künyesi alanları; künye yoksa None.

    TeX yoluyla (branding.TEX_TITLE; MANUAL_TRANSLATION.marker) çevrilmiş
    ya da çevirisi olmayan klasörde None: künye YALNIZ "Yerel model ile
    çevir" çıktısına konur. Alanlar: {"model", "source_lang", "date"}. Kayıt
    (MT_NOTICE_INFO_NAME) yoksa (kayıt tutulmadan önce çevrilmiş klasör) model
    None ("kaydedilmemiş"), dil kaynak gövdeden ölçülür, tarih
    ``translated_body.tex``'in değişme günüdür — hiçbiri uydurulmaz.
    """
    source_dir = Path(output_dir) / "source"
    if (source_dir / MANUAL_MARKER_NAME).exists():
        return None
    translated = source_dir / "translated_body.tex"
    if not translated.is_file():
        return None
    info = _read_mt_notice_info(source_dir)
    if info is not None:
        return info
    try:
        day = datetime.fromtimestamp(translated.stat().st_mtime).date()
    except OSError:
        return None
    return {"model": None, "source_lang": _measured_source_lang(source_dir), "date": day}


def mt_notice_lines(output_dir: str | Path) -> tuple[str, ...] | None:
    """Künyenin satırları (metin kalıbı ``ui.branding.mt_notice_lines``); yoksa None."""
    fields = mt_notice_fields(output_dir)
    if fields is None:
        return None
    from ui import branding

    return branding.mt_notice_lines(fields["model"], fields["source_lang"], fields["date"])


def mt_notice_epub_kwargs(output_dir: str | Path) -> dict:
    """``generate_epub_output``'a künye argümanları (künye yoksa {})."""
    fields = mt_notice_fields(output_dir)
    if fields is None:
        return {}
    from ui import branding

    return {"mt_notice": branding.mt_notice_lines(fields["model"], fields["source_lang"],
                                                   fields["date"]),
            "mt_contributor": branding.mt_notice_contributor(fields["model"])}


def _add_mt_notice(book_tex: str, output_dir: str | Path,
                   log: logging.Logger) -> str:
    """Tam belgeye (book_tr.tex) makine çevirisi künyesini ekle (F3).

    Şablon temizliğinden SONRA çağrılır: künye çeviri gövdesinin parçası
    değildir, makine çevirisi temizlik kuralları metnine dokunmaz.
    """
    lines = mt_notice_lines(output_dir)
    if lines is None:
        return book_tex
    from latex_pipeline.template_engine import insert_front_notice

    log.info("Makine çevirisi künyesi eklendi: %s", lines[-1])
    return insert_front_notice(book_tex, lines)


def _write_truncation_report(output_dir: str | Path, stats,
                             log: logging.Logger) -> None:
    """Üretim sınırına dayanan parçaları TRUNCATION_REPORT_NAME'e yaz ve uyar.

    V2 denetimi (2026-09-29, F3-07): kesik olabilecek çeviri KARARLAR 15
    gereği kabul edilir ve belleğe yazılmaz (core), ama yalnız günlükteki
    sayaçla kalıyordu — QC 100, çıkış 0, arayüzde iz yok. Artık liste kitap
    klasöründe (her MT koşusunda yeniden; boşsa []), sayı konsolda ve
    pipeline.log'da UYARI olarak görünür. Yazılamazsa koşu düşmez.
    """
    items = getattr(stats, "truncated_segments", None)
    items = [dict(item) for item in items if isinstance(item, dict)] \
        if isinstance(items, list) else []
    path = Path(output_dir) / TRUNCATION_REPORT_NAME
    try:
        path.write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    except OSError as exc:
        log.warning("%s yazılamadı: %s", TRUNCATION_REPORT_NAME, exc)
        return
    if items:
        log.warning(
            "UYARI: %d parçanın çevirisi üretim sınırına dayandı; bu paragraflar "
            "eksik çevrilmiş olabilir (liste: %s). Kesik çeviri belleğe "
            "yazılmadı; paragrafı elle denetleyin ya da daha kısa parçalarla "
            "yeniden çevirin.", len(items), path)


def _print_extraction_reused(note: str = "") -> None:
    """Önceki çıkarımın yeniden kullanıldığını söyle (1_cikar ile aynı ileti)."""
    extra = f" ({note})" if note else ""
    print(f"UYARI: Bu klasördeki önceki çıkarım ({EXTRACTION_NAME}) yeniden "
          f"kullanıldı{extra}; kitap baştan çıkarılmadı. Baştan çıkarmak için "
          f"--yeniden ver.", flush=True)


def _run_book_extraction(input_path: str, input_format: str,
                         output_dir: str | Path, args) -> bool:
    """translate_book (tek kitap ve toplu kip): kitabı çıkar.

    --yeniden: önceki full_book.md PREVIOUS_EXTRACTION_NAME adıyla saklanır,
    çıkarım başarısız olursa geri yüklenir (1_cikar ile aynı). Kullanılamayan
    (neredeyse boş) eski full_book.md silinmez, kenara alınır (F1-11).
    """
    out = Path(output_dir)
    md = out / EXTRACTION_NAME
    previous = out / PREVIOUS_EXTRACTION_NAME
    moved_previous = False
    if getattr(args, "yeniden", False) and md.exists():
        md.replace(previous)
        moved_previous = True
        print(f"--yeniden: önceki çıkarım {PREVIOUS_EXTRACTION_NAME} adıyla "
              f"saklandı; kitap baştan çıkarılıyor.", flush=True)
    moved = set_aside_unusable_extraction(out)
    if moved is not None:
        print(f"UYARI: Klasördeki önceki çıkarım ({EXTRACTION_NAME}) "
              f"kullanılamaz, neredeyse boş ({moved.name} adıyla saklandı); "
              f"kitap baştan çıkarılıyor.", flush=True)
    if input_format == "pdf":
        ok = run_extraction(input_path, str(out), args.batch_size, args.device)
    else:
        ok = run_epub_extraction(input_path, str(out))
    if not ok and moved_previous and previous.exists() and not md.exists():
        previous.replace(md)
        print(f"      Önceki çıkarım geri yüklendi ({EXTRACTION_NAME}).",
              flush=True)
    return ok


def _pdf_ile_markdown_onar(markdown_text: str, config, logger) -> str:
    """FX2 (2026-10-05): Marker'ın düşürdüğü/bozduğu içeriği dönüştürücüden
    ÖNCE kaynak PDF'den kurtar (kayıpsızlık). Sıra:

    1. sekil_kirpma: kesilmiş şekil görselleri (grafik başlığı, lejant, logo)
       PDF'den genişletilir (source/images'taki dosya değişir, özgünü
       marker_ozgun/ altında); yinelenen parça görselin satırı çıkar.
    2. kayip_kurtarma: PDF metin katmanında olup Markdown'da hiç olmayan
       satırlar (ör. sayfa sonundaki ara başlık) yerine konur; şekil
       bölgeleri (1. adımın raporu) şekil içi yazı sayılır, eklenmez.
    3. pdf_metin_onarim.matematik_tablo_onar: matematikte ok simgesi ve
       ondalık ayraç, bölünmüş satır içi matematik ("d*t*"), tablo
       hücresindeki dipnot işareti.

    Her adım kendi içinde güvenli (hata/PDF yok -> Markdown aynen); burada
    da ayrıca korunur. EPUB'da (source_book.pdf yok) Markdown aynen döner.
    full_book.md diske yazılmaz: onarım her dönüşümde bellekte yapılır.
    """
    pdf = getattr(config, "input_pdf", None)
    if not pdf or not str(pdf).lower().endswith(".pdf") or not Path(str(pdf)).is_file():
        return markdown_text
    md = markdown_text
    sekil_bolgeleri = None
    try:
        from latex_pipeline import sekil_kirpma
        md, kirpma_raporu = sekil_kirpma.onar_raporlu(md, str(pdf), str(config.images_dir))
        sekil_bolgeleri = kirpma_raporu.sekil_bolgeleri()
        if kirpma_raporu.genisletilen or kirpma_raporu.kaldirilan_parcalar:
            logger.info("FX2 şekil kırpma: %d görsel bu koşuda genişletildi (%d önceki "
                        "koşudan), %d yinelenen parça kaldırıldı",
                        len(kirpma_raporu.bu_kosuda_genisletilen),
                        len(kirpma_raporu.genisletilen)
                        - len(kirpma_raporu.bu_kosuda_genisletilen),
                        len(kirpma_raporu.kaldirilan_parcalar))
    except Exception as exc:  # noqa: BLE001 - onarım boru hattını durdurmaz
        logger.warning("FX2 şekil kırpma atlandı: %s", exc)
    try:
        from latex_pipeline import kayip_kurtarma
        md, kayip_raporu = kayip_kurtarma.onar_raporlu(md, str(pdf), sekil_bolgeleri)
        if kayip_raporu.eklemeler:
            logger.info("FX2 kayıp metin: %d parça PDF'den geri kondu",
                        len(kayip_raporu.eklemeler))
    except Exception as exc:  # noqa: BLE001
        logger.warning("FX2 kayıp metin kurtarma atlandı: %s", exc)
    try:
        from latex_pipeline import pdf_metin_onarim
        md = pdf_metin_onarim.matematik_tablo_onar(md, str(pdf))
    except Exception as exc:  # noqa: BLE001
        logger.warning("FX2 matematik/tablo onarımı atlandı: %s", exc)
    return md if md != markdown_text else markdown_text


def run_pipeline(output_dir: str, book_name: str,
                 skip_translate: bool, skip_compile: bool,
                 skip_qc: bool = False,
                 book_type: str | None = None,
                 pdf_path: str | None = None,
                 resume: bool = False,
                 manual_translation: bool = False,
                 force_mt: bool = False,
                 mt_engine: str = mt_registry.DEFAULT_ENGINE,
                 source_lang_check: bool = True,
                 source_lang: str | None = None) -> int:
    """Run post-extraction pipeline. Returns 0 on success, 1/2/3/4/5 on failure.

    Çıkış kodları modül docstring'inde; 4 = ``EXIT_SOURCE_UNSUPPORTED``
    (yerel model kaynak dil kapısı: dil desteklenmiyor), 5 =
    ``EXIT_MODEL_UNAVAILABLE`` (seçilen model kullanılamıyor: ön kontrol,
    yükleme hatası ya da koşu sırasında sunucu bağlantısının kopması).
    *source_lang_check* False ise kapı atlanır ve İngilizce varsayılır
    (CLI: ``--dil-kontrolu-yok``); *source_lang* (CLI: ``--kaynak-dil``)
    verilirse kapı atlanır, dil o olur. Kapı Yol A'da (manual_translation,
    --export-tex, --import-tex) zaten hiç çalışmaz. Kapının verdiği kaynak
    dil PipelineConfig, LatexTranslator (istem, çevrilmemiş satır denetimi,
    bellek havuzu) ve Phase 5.7/5.8'e akar (KARARLAR 14, 2026-09-28).

    When *resume* is True, the pipeline attempts to skip phases whose
    output already exists on disk:
      - If ``translated_body.tex`` exists -> skip to Phase 5.5 (sanitize+QC)
      - If ``body.tex`` exists but no ``translated_body.tex`` -> start at Phase 5
      - Checkpoint-level resume is handled inside LatexTranslator.translate()

    *force_mt* (F47): çeviri atlanmıyorsa mevcut ``translated_body.tex``
    ``.bak`` olarak yedeklenir ve Phase 5 gerçekten koşar — resume bu dosyayı
    görüp MT'yi sessizce atlayamaz. ``skip_translate`` ile birlikte
    verilirse yok sayılır (uyarı loglanır).
    """

    from latex_pipeline.config import PipelineConfig
    from latex_pipeline.converter import MarkdownToLatex
    from latex_pipeline.template_engine import TemplateEngine
    from latex_pipeline.translator import LatexTranslator
    from latex_pipeline.compiler import LatexCompiler
    from latex_pipeline.inspector import QualityInspector
    from latex_pipeline.sanitizer import LatexSanitizer
    from latex_pipeline.qc_engine import QCEngine
    from latex_pipeline.autofix import AutoFixer

    # -- Motor kontrolü (savunma hattı, 2026-09-25) ----------------------------
    # translate_book.main bunu çıkarımdan ÖNCE yapar; burası run_pipeline'ı
    # doğrudan çağıranlar için. Varsayılan motorun modeli _preflight_check'te
    # denetlenir (aynı check_engine). Yol A (manuel) koşusunda MT koşmaz.
    _engine_checked = False
    _engine_warning = ""        # MT-12: olumlu ön kontrolün uyarısı (günlüğe aşağıda)
    _mt_runs = (not skip_translate and not manual_translation
                and (force_mt or not (Path(output_dir) / "source"
                                      / MANUAL_MARKER_NAME).exists())
                and not _resume_skips_translation(output_dir, resume, force_mt))
    if _mt_runs and _engine_check_needed(mt_engine):
        _engine_checked = True
        _check = _check_engine(mt_engine)
        if not _check.ok:
            return _engine_stop(_check, output_dir)
        # MT-12 (2026-09-30): sunucu modelinin kaba bellek tahmini
        # "sığmayabilir" diyorsa engel değil; gerekçe günlükte kalır
        # (günlükçü aşağıda kurulunca yazılır).
        _engine_warning = getattr(_check, "warning", "")

    # -- Pre-flight health check ----------------------------------------------
    preflight_errors = _preflight_check(skip_translate, skip_compile, pdf_path,
                                        mt_engine=mt_engine,
                                        engine_checked=_engine_checked or not _mt_runs)
    # Dar bellekte toplu iş küçülür (KARARLAR 17a); hesap ağsız, modeli
    # yüklemez. Sığıyorsa hiçbir ayar değişmez.
    _memory = _memory_fit(mt_engine) if (_mt_runs and not preflight_errors) else None

    if preflight_errors:
        # Model kullanılamıyorsa ileti erken kontrolle AYNI yoldan gider
        # (stderr "HATA:/İpucu:" + pipeline.log + olay akışı); diğer ön kontrol
        # hataları eskisi gibi.
        _model_check = getattr(preflight_errors, "engine_check", None)
        _model_message = getattr(preflight_errors, "model_message", "")
        if _model_check is not None:
            _engine_stop(_model_check, output_dir)
        # Set up minimal logging so errors are visible
        _pf_logger = logging.getLogger("pipeline.preflight")
        for err in preflight_errors:
            if _model_check is not None and err == _model_message:
                continue
            _pf_logger.critical("PRE-FLIGHT FAIL: %s", err)
        print(
            f"\nPre-flight check failed ({len(preflight_errors)} error(s)). "
            "Fix the issues above before running the pipeline.",
            file=sys.stderr,
        )
        if getattr(preflight_errors, "model_unavailable", False):
            return EXIT_MODEL_UNAVAILABLE
        return 1

    t_global = time.time()
    out = Path(output_dir).resolve()
    source_dir = out / "source"
    images_dir = source_dir / "images"
    compiled_dir = out / "compiled"
    checkpoint_dir = out / "checkpoints"
    md_path = out / "full_book.md"

    for d in [source_dir, compiled_dir, checkpoint_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # Setup logging
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    # Clear old handlers
    root_logger.handlers.clear()

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
    root_logger.addHandler(ch)

    # pipeline.log EKLEME kipinde + koşu ayracı (2026-09-25, U4/S3; eskiden
    # mode="w" önceki koşunun günlüğünü siliyordu).
    fh = _pipeline_log_handler(out)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
                                       datefmt="%Y-%m-%d %H:%M:%S"))
    root_logger.addHandler(fh)

    # Masaüstü arayüzün canlı göstergeleri için yapılandırılmış olay akışı.
    # Yalnızca KITAPCEVIR_EVENT_LOG ortam değişkeni doluysa kurulur; yoksa
    # hiçbir şey değişmez. handlers.clear() yukarıda çalıştığı için kurulum
    # buradan yapılmak zorunda.
    try:
        from core.events import install_event_log
        install_event_log()
    except Exception:  # olay akışı yardımcı kanal; çeviriyi asla düşürmez
        pass

    logger = logging.getLogger("pipeline")
    if _engine_warning:
        logger.warning("%s", _engine_warning)

    # -- Read markdown --------------------------------------------------------
    if not md_path.exists():
        logger.error("full_book.md not found in %s. Run extraction first.", out)
        return 1

    markdown_text = md_path.read_text(encoding="utf-8")
    logger.info("Read full_book.md: %d chars, %d lines",
                len(markdown_text), markdown_text.count("\n") + 1)

    if extraction_text_length(markdown_text) < EXTRACTION_MIN_CHARS:
        logger.error("Markdown too short (%d chars). Aborting.", len(markdown_text))
        return 1
    # F1-03 (2026-09-29): çıkarılamayan sayfalar her koşuda görünür (yer
    # tutucular EXPORT.tex'e / PDF'ye kalın satır olarak geçer).
    _failed_pages = failed_pages_in_markdown(markdown_text)
    if _failed_pages:
        logger.warning(
            "UYARI: Çıkarımda %d sayfa eksik (PDF sayfa %s); bu sayfaların "
            "yerinde '[ÇIKARILAMAYAN SAYFA …]' satırı var. Metni kaynak "
            "PDF'den full_book.md'ye elle ekleyin ya da kitabı --yeniden ile "
            "yeniden çıkarın.", len(_failed_pages),
            ", ".join(str(n) for n in _failed_pages),
        )

    # -- Auto-detect book type ------------------------------------------------
    if book_type is None:
        # Use input path for filename-based detection.
        # For EPUB flow, pdf_path is None -- fall back to source_book.epub or .pdf.
        if pdf_path:
            _input = pdf_path
        elif (out / "source_book.epub").exists():
            _input = str(out / "source_book.epub")
        else:
            _input = str(out / "source_book.pdf")
        book_type = detect_book_type(_input, markdown_text)
        _book_type_auto = True
    else:
        _book_type_auto = False
    # (#70) Etiket tespitin kendisine bagli — eski kosul pdf_path'e
    # bakiyordu: PDF'de otomatik tespite ragmen etiket hic cikmiyor,
    # EPUB'da elle verilen ture bile '(auto-detected)' yaziyordu.
    logger.info("Book type: %s%s", book_type,
                " (auto-detected)" if _book_type_auto else "")

    # -- Config ---------------------------------------------------------------
    config = PipelineConfig(
        input_pdf=str(out / "source_book.pdf"),
        output_dir=str(out),
        document_class="book",
        translate=not skip_translate,
        source_lang="en",
        target_lang="tr",
        font_main="STIX Two Text",
        font_math="STIX Two Math",
        book_type=book_type,
    )
    # Donanım kapısı: sığan en büyük toplu iş (yalnız küçülmesi gerekiyorsa
    # config + translation_overrides değişir; LatexTranslator'a ek argüman
    # yalnız o zaman geçer).
    _translator_memory_kwargs = _apply_memory_batch(config, _memory, logger)

    # -- Phase 1.5: Layout Detection (sadece PDF akisi) ----------------------
    if pdf_path:
        try:
            from latex_pipeline.layout_detector import LayoutDetector
            _layout_detector = LayoutDetector()
            _layout_info = _layout_detector.detect(pdf_path)
            config.apply_layout(_layout_info)
            logger.info(
                "Phase 1.5: Layout detected -- %s, %sx%smm, margins=%s/%s/%s/%s, font=%s %s",
                _layout_info.paper_size_name or "custom",
                f"{_layout_info.page_width_mm:.0f}", f"{_layout_info.page_height_mm:.0f}",
                f"{_layout_info.margin_top_mm:.0f}", f"{_layout_info.margin_bottom_mm:.0f}",
                f"{_layout_info.margin_left_mm:.0f}", f"{_layout_info.margin_right_mm:.0f}",
                _layout_info.mapped_font_name or _layout_info.dominant_font_name,
                f"{_layout_info.dominant_font_size_pt:.1f}",
            )
        except Exception as exc:
            logger.warning("Layout detection failed, using defaults: %s", exc)

    # -- Yol A kilidi: manuel ceviri kalici isareti ----------------------------
    # 2026-07-11: --import-tex bilgisi yalniz o kosunun arguman'inda yasiyordu.
    # Sonraki kosular (resume veya duz Yol B) ajan cevirisini HY-MT profili
    # fix'leriyle bozuyor ya da MT ile eziyordu. Import aninda diske marker
    # yazilir; marker gorulen her sonraki kosu manuel modda kalir.
    _manual_marker = source_dir / MANUAL_MARKER_NAME
    if manual_translation:
        try:
            _manual_marker.write_text(
                f"import: {datetime.now().isoformat()}\n", encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("Manuel çeviri işareti yazılamadı: %s", exc)
    elif _manual_marker.exists():
        manual_translation = True
        logger.info(
            "MANUAL_TRANSLATION.marker bulundu — bu kitap '%s' ile "
            "(yapay zekâ asistanı ya da kullanıcı) çevrildi; sanitize+QC "
            "manuel modda koşulacak.", _branding_title("TEX_TITLE")
        )

    # -- F47 (2026-07-29): --force-mt + mevcut translated_body.tex ------------
    # Eski davranış: resume mevcut gövdeyi görüp Phase 5.5'e atlıyor, MT hiç
    # koşmuyor, marker silinmiyor ve HİÇBİR uyarı basılmıyordu — kullanıcı
    # (2_derle'nin önerdiği `--force-mt` + alışkanlıktan `--resume`) kitabın
    # HY-MT ile yeniden çevrildiğini sanıyordu. Artık mevcut çeviri .bak'a
    # TAŞINIR (silinmez) ve MT gerçekten koşar.
    if force_mt and not skip_translate:
        _tr_existing = source_dir / "translated_body.tex"
        if _tr_existing.exists() and _tr_existing.stat().st_size > 100:
            _bak = source_dir / "translated_body.tex.bak"
            if _bak.exists():
                _bak = source_dir / (
                    "translated_body."
                    + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                    + ".tex.bak"
                )
            try:
                _tr_existing.replace(_bak)
            except OSError as _bak_exc:
                logger.critical(
                    "--force-mt: mevcut translated_body.tex yedeklenemedi "
                    "(%s). Üzerine yazma riski nedeniyle durduruldu; dosyayı "
                    "elle taşıyıp tekrar deneyin.", _bak_exc,
                )
                return 1
            logger.warning(
                "--force-mt: mevcut çeviri %s olarak yedeklendi; MT çevirisi "
                "(Phase 5) baştan koşacak.", _bak.name,
            )
            # R5 (2026-07-29): zaman damgalı yedekler sınırsız birikmesin —
            # en yeni 3 tanesi kalır (düz .tex.bak her zaman korunur).
            _stamped = sorted(
                source_dir.glob("translated_body.*.tex.bak"),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            for _old_bak in _stamped[3:]:
                try:
                    _old_bak.unlink()
                    logger.info("--force-mt: eski yedek silindi: %s",
                                _old_bak.name)
                except OSError:
                    pass
    if force_mt and not skip_translate:
        # F3-08 (2026-09-29): --force-mt --resume önceki modelin FINAL
        # checkpoint'ini döndürüyordu (checkpoint yalnız kaynak gövdenin
        # sha1'ini doğruluyor; motor kimliği yok) — MT hiç koşmuyor, günlük
        # yeni modeli yazıyordu. FINAL checkpoint de yedeğe TAŞINIR (silinmez)
        # ve çevirmen checkpoint'ten sürdürmez (aşağıda resume=False).
        _final_ckpt = checkpoint_dir / FINAL_CHECKPOINT_NAME
        if _final_ckpt.exists():
            _ckpt_bak = checkpoint_dir / (
                FINAL_CHECKPOINT_NAME + "."
                + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".bak")
            try:
                _final_ckpt.replace(_ckpt_bak)
                logger.warning(
                    "--force-mt: önceki koşunun FINAL checkpoint'i %s olarak "
                    "yedeklendi; kullanılmayacak.", _ckpt_bak.name)
            except OSError as _ckpt_exc:
                logger.warning(
                    "--force-mt: FINAL checkpoint yedeklenemedi (%s); "
                    "çevirmen yine de checkpoint'ten sürdürmeyecek.",
                    _ckpt_exc)
    elif force_mt:
        logger.warning(
            "--force-mt yok sayıldı: bu koşuda çeviri zaten atlanıyor "
            "(skip_translate/--import-tex). Mevcut çeviri korunuyor."
        )

    # -- Resume logic: detect which phases can be skipped ---------------------
    _resume_from_phase5_5 = False   # skip translation, go to sanitize+QC
    _resume_from_phase5 = False     # skip md->latex, go to translation

    if resume:
        tr_body_path_check = source_dir / "translated_body.tex"
        body_path_check = source_dir / "body.tex"

        if (tr_body_path_check.exists()
                and tr_body_path_check.stat().st_size > RESUMABLE_MIN_BYTES):
            # F3: son koşu kalite kapısında reddedildiyse (translated_body.
            # FAILED.tex daha YENİ) resume eski/başka bir koşunun gövdesiyle
            # devam ediyor demektir — kullanıcı bunu bilmeli.
            _failed_check = source_dir / "translated_body.FAILED.tex"
            if (_failed_check.exists()
                    and _failed_check.stat().st_mtime
                    > tr_body_path_check.stat().st_mtime):
                logger.warning(
                    "DİKKAT: %s dosyası translated_body.tex'ten YENİ — son "
                    "çeviri koşusu kalite kapısında reddedildi. RESUME bu "
                    "koşunun çıktısıyla DEĞİL, önceki gövdeyle devam ediyor.",
                    _failed_check.name,
                )
            logger.info(
                "RESUME: translated_body.tex found (%d chars). "
                "Skipping to Phase 5.5 (sanitize+QC).",
                tr_body_path_check.stat().st_size,
            )
            _resume_from_phase5_5 = True
        elif body_path_check.exists() and body_path_check.stat().st_size > 100:
            logger.info(
                "RESUME: body.tex found (%d chars). "
                "Skipping to Phase 5 (translation).",
                body_path_check.stat().st_size,
            )
            _resume_from_phase5 = True
        else:
            logger.info("RESUME: No resumable artifacts found. Starting from scratch.")

    # -- Phase 2: Markdown -> LaTeX -------------------------------------------
    if _resume_from_phase5_5 or _resume_from_phase5:
        # Resume: read existing body.tex instead of re-converting
        body_path = source_dir / "body.tex"
        if body_path.exists():
            latex_body = body_path.read_text(encoding="utf-8")
            logger.info("RESUME: Loaded existing body.tex: %d chars", len(latex_body))
        else:
            # body.tex missing but translated_body exists -- need it for QC
            logger.info("=" * 60)
            logger.info("  Phase 2: Markdown -> LaTeX")
            logger.info("=" * 60)
            converter = MarkdownToLatex(config)
            latex_body = converter.convert(_pdf_ile_markdown_onar(markdown_text, config, logger))
            # (#7) Taze dalla birebir ayni sira: convert -> relevel -> write.
            # Resume'da relevel atlaninca EXPORT (relevel'li body'den
            # uretilmisti) ile QC referansi uyusmayip sahte 'bolum
            # uyusmazligi' exit 3'leri cikiyordu.
            if pdf_path:
                from latex_pipeline.toc_releveler import relevel_from_pdf
                latex_body, _rel = relevel_from_pdf(latex_body, pdf_path)
                if _rel.get("applied"):
                    logger.info(
                        "RESUME TOC re-level: chapter %s->%s, eşleşen=%s",
                        _rel.get("body_chapters_before"),
                        _rel.get("body_chapters_after"),
                        _rel.get("matched"),
                    )
            body_path = source_dir / "body.tex"
            body_path.write_text(latex_body, encoding="utf-8")
            logger.info("body.tex: %d chars", len(latex_body))
    else:
        logger.info("=" * 60)
        logger.info("  Phase 2: Markdown -> LaTeX")
        logger.info("=" * 60)

        converter = MarkdownToLatex(config)
        latex_body = converter.convert(_pdf_ile_markdown_onar(markdown_text, config, logger))

        # Marker heading over-promotion fix: PDF TOC outline'ını yer-gerçeği
        # alıp başlık seviyelerini düzelt (TOC yoksa body aynen kalır).
        if pdf_path:
            from latex_pipeline.toc_releveler import relevel_from_pdf
            latex_body, _rel = relevel_from_pdf(latex_body, pdf_path)
            if _rel.get("applied"):
                logger.info("TOC re-level: chapter %s->%s, eşleşen=%s indirilen=%s",
                            _rel.get("body_chapters_before"), _rel.get("body_chapters_after"),
                            _rel.get("matched"), _rel.get("demoted"))

        body_path = source_dir / "body.tex"
        body_path.write_text(latex_body, encoding="utf-8")
        logger.info("body.tex: %d chars", len(latex_body))

    # -- Determine cover PDF path (only for PDF input, not EPUB) --------------
    _cover_pdf = None
    if pdf_path:
        _candidate = out / "source_book.pdf"
        if _candidate.exists() and _candidate.stat().st_size > 1024:
            _cover_pdf = str(_candidate.resolve())
            logger.info("Cover PDF source: %s", _cover_pdf)
        elif Path(pdf_path).exists():
            _cover_pdf = str(Path(pdf_path).resolve())
            logger.info("Cover PDF source (original): %s", _cover_pdf)

    # -- Phase 3: Template wrap -----------------------------------------------
    template_engine = TemplateEngine(config)
    book_tex = template_engine.wrap_content(latex_body, cover_pdf_path=_cover_pdf)
    book_en_path = source_dir / "book.tex"
    if not _resume_from_phase5_5:
        book_en_path.write_text(book_tex, encoding="utf-8")
        logger.info("book.tex (source): %d chars", len(book_tex))

    # -- Phase 4.5: Auto-terminology: KALDIRILDI (F11, 2026-07-28) ------------
    # TerminologyEngine auto-terms'in TEK tüketicisi LatexTranslator'ın
    # extra_glossary_terms parametresiydi; GLS-D5 ile inject_terms/verify_terms
    # çifti kaldırılınca bu parametre ölü kaldı: translator.py'de
    # _glossary_engine yalnız (a) term_count log satırı ve (b) 'motor var mı'
    # boolean kontrolü için kullanılıyor, _glossary_term_map hep boş.
    # Terminoloji zorlaması Phase 5.75'te (kendi motorunu extra_terms OLMADAN
    # kurar) yapılıyor. Yani blok TEK BİR ÇIKTI BAYTINI değiştirmiyordu ama her
    # Yol B koşusunda 122 MB / 1,46M kayıtlı dictionary.json'u yüklüyordu:
    # ölçüldü (büyük bir test kitabının body.tex'i, 930K karakter) -> ~1,9 s ve +1,05 GB RSS;
    # üstelik term_engine run_pipeline bitene kadar canlı bir yerel değişken
    # olduğu için bu bellek MLX modeliyle YARIŞARAK tüm koşu boyunca
    # tutuluyordu. Auto-terms gerçekten istenirse doğru yer Phase 5.75 motoru
    # (GlossaryEngine extra_terms'e priority='high' verdiği için Pass 3
    # tek-kelime taramasını genişletir; kalite etkisi ayrıca ölçülmeli).
    # Regresyon kilidi: tests/test_fixav20260728_runtime.py.

    # -- Phase 5: Translation -------------------------------------------------
    translated_body = None
    translator = None
    _kitap_sozlugu = None  # G2c: kitap sözlüğü (KitapSozlugu ya da None)

    if _resume_from_phase5_5:
        # Load existing translated body -- skip translation entirely
        tr_body_path = source_dir / "translated_body.tex"
        # F14: Yol A'da bu dosya KULLANICIDAN gelir (2_derle / --import-tex
        # EXPORT_TR.tex'i buraya kopyalar). Düz utf-8 okuma, Latin-1
        # kaydedilmiş dosyada ham UnicodeDecodeError traceback'i fırlatıyor
        # (2_derle'nin --json sözleşmesi bozuluyordu) ve BOM'u gövdeye
        # taşıyordu.
        try:
            translated_body = _read_translated_body(tr_body_path)
        except UnicodeDecodeError as exc:
            logger.critical("KODLAMA HATASI: %s",
                            _encoding_error_hint(tr_body_path, exc))
            return 1
        logger.info(
            "RESUME: Loaded translated_body.tex (%d chars). "
            "Proceeding to Phase 5.5.",
            len(translated_body),
        )
        # Still create translator for retranslator/segment-retry phases
        if not skip_translate:
            # K5 (2026-09-28): gövde aynı olduğu için karar taze koşuyla
            # aynıdır; resume burada DURDURMAZ (çeviri zaten var), yalnız
            # kaynak dili Phase 5.7/5.8'e taşır. Yol A kitabında (manuel)
            # bu fazlar koşmaz; eski İngilizce varsayımı kalır.
            if not manual_translation:
                _, _source_code = _source_language_decision(
                    latex_body, logger, check=source_lang_check,
                    forced=source_lang, stop=False)
                config.source_lang = _source_code
            # extra_glossary_terms verilmiyor (F11): auto-terms'in tüketicisi
            # GLS-D5'te kaldırıldı, parametre ölü.
            translator = LatexTranslator(
                pipeline_config=config,
                mt_engine=mt_engine,
                source_lang=config.source_lang,
                target_lang="tr",
                book_type=config.book_type,
                project_name=book_name,
                **_translator_memory_kwargs,
            )

    if not skip_translate and not _resume_from_phase5_5:
        # -- Kaynak dil kapısı (Yol B, 2026-09-24) -------------------------
        # Çıkarımdan SONRA, MT'den ÖNCE ve yalnız Yol B'de: yerel model
        # desteklenmeyen dildeki kaynağa sessizce çöp üretir. --export-tex ve
        # --import-tex bu bloğa hiç girmez (skip_translate); manual_translation
        # ayrıca açıkça dışlanır. Yol A kilidinden (marker) ÖNCE koşar ki
        # --force-mt verilmiş, dili desteklenmeyen kitapta marker silinmeden durulsun;
        # marker'lı ve --force-mt'siz kitapta ise kilidin kendi mesajı (exit 2)
        # öncelikli kalır. (Marker varken manual_translation yukarıda True
        # yapıldı; --force-mt ile aşağıda False'a dönüp MT koşacağı için kapı
        # o durumda da uygulanır.)
        if _manual_marker.exists():
            _mt_will_run = force_mt
        else:
            _mt_will_run = not manual_translation
        # K5 (2026-09-28, KARARLAR 14): kapı yalnız desteklenmeyen dilde
        # durdurur; ölçülen (ya da --kaynak-dil ile verilen) dil zincire akar.
        if _mt_will_run or not source_lang_check or source_lang:
            _gate_exit, _source_code = _source_language_decision(
                latex_body, logger, check=source_lang_check,
                forced=source_lang)
            if _gate_exit is not None:
                return _gate_exit
            config.source_lang = _source_code

        # Yol A kilidi (2026-07-11): marker'li kitapta MT cevirisi calistirmak
        # ajan cevirisini (translated_body.tex) HY-MT ciktisiyla EZER. Acik
        # --force-mt olmadan reddedilir.
        if _manual_marker.exists():
            if force_mt:
                logger.warning(
                    "--force-mt: MANUAL_TRANSLATION.marker siliniyor, kitap "
                    "yerel modelle ('%s') YENİDEN çevrilecek — mevcut çeviri "
                    "translated_body.tex üzerine yazılacak.", mt_engine
                )
                try:
                    _manual_marker.unlink()
                except OSError:
                    pass
                manual_translation = False
            else:
                # translate_book.main bu durumu çıkarımdan ÖNCE yakalar
                # (_yol_a_lock_applies); burası run_pipeline'ı doğrudan
                # çağıranlar için savunma hattı. İleti ön kilitle aynı.
                logger.error(yol_a_lock_message())
                logger.info(_yol_a_lock_hint())
                return EXIT_MANUAL_LOCK

        logger.info("=" * 60)
        logger.info("  Phase 5: MT Translation (%s -> TR, engine=%s)",
                    config.source_lang.upper(), mt_engine)
        _engine_desc = _engine_description(mt_engine)
        if _engine_desc:
            # Hangi modelin çevirdiği pipeline.log'da kalsın (2026-09-25)
            logger.info("  Model: %s", _engine_desc)
        logger.info("=" * 60)
        t_trans = time.time()

        # extra_glossary_terms verilmiyor (F11): bkz. Phase 4.5 notu.
        translator = LatexTranslator(
            pipeline_config=config,
            mt_engine=mt_engine,
            source_lang=config.source_lang,
            target_lang="tr",
            book_type=config.book_type,
            project_name=book_name,
            **_translator_memory_kwargs,
        )

        # G2c (KARAR 25): kitap sözlüğü. İstem desteği (3a) açıksa sözlük
        # çeviriden ÖNCE gerekir: klasördeki glossary_shared.tsv yüklenir ya da
        # kaynak dilde çıkarılıp seçili motorla bağlamlı çevrilir. Kapalıyken
        # (varsayılan) burada yalnız mevcut dosya okunur (model çağrısı yok);
        # dosya yoksa sözlük çeviri bittikten SONRA hazırlanır (QC döngüsü
        # öncesi). Hata çeviriyi durdurmaz.
        if _kitap_sozlugu_istemi_acik():
            _kitap_sozlugu = _kitap_sozlugunu_hazirla(translator, latex_body,
                                                      out, logger)
        else:
            _kitap_sozlugu = _kitap_sozlugunu_yukle(out, config.source_lang,
                                                    translator, logger)

        MAX_TRANSLATE_RETRIES = 3
        last_exc = None
        for attempt in range(1, MAX_TRANSLATE_RETRIES + 1):
            try:
                translated_body = translator.translate(
                    latex_body,
                    checkpoint_dir=str(checkpoint_dir),
                    # F3-08: --force-mt checkpoint'ten sürdürmez.
                    resume=resume and not force_mt,
                )

                stats = translator.stats
                logger.info("Translation complete (%.1fs): %d total, %d translated, "
                            "%d skipped, %d failed",
                            time.time() - t_trans,
                            stats.segments_total, stats.segments_translated,
                            stats.segments_skipped, stats.segments_failed)
                _write_truncation_report(output_dir, stats, logger)

                # -- F3 kalite kapısı ------------------------------------
                # translator.py aynı koşulda FINAL checkpoint'i YAZMIYOR;
                # burada gövdeyi koşulsuz translated_body.tex'e yazmak o
                # reddi faz düzeyinde bypass ediyordu (bir sonraki --resume
                # yalnız dosya varlığına bakıp İngilizce gövdeyi 'çeviri'
                # kabul ediyor, Phase 5'i tamamen atlıyordu).
                tr_body_path = source_dir / "translated_body.tex"
                _usable, _fail_reason = _translation_body_usable(stats)
                # Koşu sırasında model sunucusu koptuysa (devre açık) neden
                # budur: kalite kapısı reddeder, çıkış 5 olur. Vk 2026-09-28:
                # başarısız oran eşiğin ALTINDA kalsa da (sunucu geç koptu)
                # gövde çeviri sayılmaz — kalan segmentler hiç denenmedi;
                # translated_body.tex'e yazılsaydı --resume Phase 5'i atlayıp
                # İngilizce kalan kısmı bir daha çevirmezdi.
                _backend_problem = _translator_backend_problem(translator)
                if _usable and _backend_problem:
                    _usable = False
                    _fail_reason = ("model arka ucu koşu sırasında kullanılamaz oldu "
                                    f"({_backend_problem})")
                if not _usable:
                    _failed_path = source_dir / "translated_body.FAILED.tex"
                    try:
                        _failed_path.write_text(translated_body,
                                                encoding="utf-8")
                    except OSError as _fexc:
                        logger.error("Başarısız gövde kaydedilemedi (%s): %s",
                                     _failed_path.name, _fexc)
                    logger.critical(
                        "ÇEVİRİ KALİTE KAPISI REDDETTİ: %s. Gövde "
                        "translated_body.tex olarak YAZILMADI (yazılsaydı bir "
                        "sonraki --resume onu 'çeviri' sanıp Phase 5'i "
                        "atlardı); teşhis için %s dosyasına kaydedildi. "
                        "Önce modeli/motoru düzeltin (pipeline.log'daki "
                        "MT_FAIL satırlarına bakın), sonra tekrar çalıştırın.",
                        _fail_reason, _failed_path.name,
                    )
                    if tr_body_path.exists():
                        logger.warning(
                            "Önceki koşudan kalan translated_body.tex duruyor "
                            "— --resume ONU kullanır; bu koşunun çıktısı "
                            "DEĞİLDİR."
                        )
                    if _backend_problem:
                        logger.critical("MODEL KULLANILAMADI: %s", _backend_problem)
                        return EXIT_MODEL_UNAVAILABLE
                    return 1

                tr_body_path.write_text(translated_body, encoding="utf-8")
                # F3: künye kaydı (hangi yerel model, kaynak dil, gün).
                _write_mt_notice_info(source_dir, mt_engine, config.source_lang, logger)
                # Başarılı koşu: önceki koşunun teşhis dosyası artık yanıltıcı.
                _stale_failed = source_dir / "translated_body.FAILED.tex"
                if _stale_failed.exists():
                    try:
                        _stale_failed.unlink()
                        logger.info("Bayat %s silindi.", _stale_failed.name)
                    except OSError:
                        pass
                last_exc = None
                break  # Success -- exit retry loop
            except BackendUnavailable as exc:
                # Model yüklenemedi / sunucu kapalı / model sunucuda yok:
                # yeniden denemek (2 sn bekleme × 3) çözmez — hemen 5.
                logger.critical("MODEL KULLANILAMADI: %s", exc)
                _hint = getattr(exc, "hint", "")
                if _hint:
                    logger.info("İpucu: %s", _hint)
                return EXIT_MODEL_UNAVAILABLE
            except Exception as exc:
                last_exc = exc
                logger.error(
                    "Translation attempt %d/%d failed: %s",
                    attempt, MAX_TRANSLATE_RETRIES, exc, exc_info=True,
                )
                if attempt < MAX_TRANSLATE_RETRIES:
                    logger.info("Retrying translation in 2 seconds...")
                    time.sleep(2)

        if last_exc is not None:
            logger.critical(
                "Translation failed after %d attempts. Pipeline ABORTED.",
                MAX_TRANSLATE_RETRIES,
            )
            return 1
    else:
        logger.info("Translation skipped.")

    # -- Outer QC Loop (Phase 5.5 - 5.8) ----------------------------------------
    # Kalite döngüsü: QC skoru eşiğe ulaşana (ya da ilerleme durana) kadar
    # iyileştirme fazları tekrarlanır.
    # Translation (Phase 5) runs ONCE.  Only the quality-improvement phases
    # (sanitize, autofix, retranslator, glossary verify, segment retry)
    # are repeated until the QC score meets the threshold or no progress.
    qc_engine = None  # initialized in Phase 5.5, used by later phases
    # Fail-closed guard: if Phase 5.5 (sanitize/QC/autofix setup) crashes,
    # the QC gate at the end must NOT silently pass with exit 0.
    _phase55_failed = False
    _outer_max = getattr(config, "max_outer_iterations", 3)
    _outer_iter = 0
    _prev_outer_score: float = -1.0
    # F36: koşu-ömürlü QC memoizasyonu (bkz. _qc_analyze). Gövde değişmediği
    # sürece aynı analiz tekrar edilmez; her yeni gövde yeni anahtar üretir.
    _qc_cache: dict = {}

    # G2c (KARAR 25): kitap sözlüğü henüz yoksa burada — çeviri bittikten
    # SONRA, QC döngüsünden önce — hazırlanır: klasördeki glossary_shared.tsv
    # yüklenir, yoksa kaynak dilde çıkarılır, seçili motorla bağlamlı çevrilir
    # ve yazılır (QC tutarlılık denetimi, Phase 5.75, yeniden çeviri
    # istemleri). QC atlanıyorsa model çağrısı yapılmaz. TeX yolu
    # (manuel) kitabında YÜKLENMEZ: o yol değişmez (QC puanı aynı kalır).
    if (_kitap_sozlugu is None and not manual_translation
            and translated_body is not None and not skip_qc):
        if translator is not None:
            _kitap_sozlugu = _kitap_sozlugunu_hazirla(translator, latex_body,
                                                      out, logger)
        else:
            _kitap_sozlugu = _kitap_sozlugunu_yukle(
                out, getattr(config, "source_lang", "en"), None, logger)

    # KARAR 25 (kitap sözlüğü önceliklidir, 2026-10-06): QC, genel sözlüğün
    # (glossary/curated.json) kitap sözlüğüyle ÇELİŞEN kuralını uygulamaz
    # (bir test kitabında kitap sözlüğü "token -> jeton (token)" seçmişken
    # genel sözlüğün "jeton yanlış" kuralı tek CRITICAL bulguydu; QC 79,
    # çıkış 3).
    # TeX yolunda sözlük QC'ye YALNIZ bu muafiyet için verilir: terim
    # tutarlılığı denetimi o yolda kapalı kalır (G2c), sözlük çevirmene
    # bağlanmaz ve Phase 5.75 o yolda zaten koşmaz.
    _qc_kitap_sozlugu = _kitap_sozlugu
    if (_qc_kitap_sozlugu is None and manual_translation
            and translated_body is not None and not skip_qc):
        _qc_kitap_sozlugu = _kitap_sozlugunu_yukle(
            out, getattr(config, "source_lang", "en"), None, logger)
        if _qc_kitap_sozlugu is not None:
            logger.info("  TeX yolu: kitap sözlüğü QC'de yalnız genel sözlükle "
                        "çelişki muafiyeti için kullanılır (terim tutarlılığı "
                        "denetimi kapalı).")

    # F2 (2026-07-28): preamble_ozel.tex'te tanımlanan ortam adları
    # sanitizer/QC/AutoFixer/Phase 5.71'in 'çevrilmiş ortam adı'
    # düzeltmesinden muaf tutulur — gövde-içi tanımları guard'lar kendisi
    # tarıyor ama preamble tanımları gövdenin dışında kalıyordu.
    _user_env_names: set = set()
    _preamble_env_path = out / "preamble_ozel.tex"
    if _preamble_env_path.exists():
        try:
            from latex_pipeline.qc_engine import collect_defined_env_names
            _user_env_names = set(collect_defined_env_names(
                _preamble_env_path.read_text(encoding="utf-8",
                                             errors="replace")))
        except OSError as _env_exc:
            logger.warning("preamble_ozel.tex okunamadı (ortam adı "
                           "taraması): %s", _env_exc)

    while _outer_iter < _outer_max:
        _outer_iter += 1
        # F36: iterasyon gövdeyi gerçekten değiştirdi mi? (manuel modda
        # değiştirmiyorsa sonraki iterasyon tanımsal olarak aynı sonucu
        # üretir — bkz. döngü sonundaki erken çıkış.)
        _iter_start_body = translated_body
        if _outer_iter > 1:
            logger.info("=" * 60)
            logger.info("  Outer iteration %d/%d (prev score=%.0f)",
                        _outer_iter, _outer_max, _prev_outer_score)
            logger.info("=" * 60)

        # -- Phase 5.5: Sanitize translated body --------------------------------
        if translated_body is not None and not skip_qc:
            logger.info("=" * 60)
            logger.info("  Phase 5.5: Sanitize + QC + AutoFix")
            logger.info("=" * 60)
            try:
                # Sanitize: remove publisher artifacts, normalize captions, fix prompt leaks
                sanitizer = LatexSanitizer(manual_translation=manual_translation,
                                           user_defined_envs=_user_env_names)
                translated_body, san_report = sanitizer.sanitize(translated_body)
                logger.info("Sanitizer: %d fixes (artifacts=%d, captions=%d, "
                             "headers=%d, prompts=%d, hrefs=%d)",
                             san_report.total_fixes,
                             san_report.artifacts_removed,
                             san_report.captions_normalized,
                             san_report.headers_cleaned,
                             san_report.prompt_leaks_fixed,
                             san_report.hrefs_fixed)

                # QC Engine + AutoFixer loop
                glossary_dir_path = Path(__file__).resolve().parent / "glossary"
                # QC-N2: pass book_type so QCEngine can apply genre-aware
                # checks (e.g. _NON_ACADEMIC_TYPES gating). Previously the
                # constructor param defaulted to None, disabling that logic.
                qc_engine = QCEngine(
                    glossary_dir=str(glossary_dir_path) if glossary_dir_path.exists() else None,
                    book_type=book_type,
                    user_defined_envs=_user_env_names,
                    kitap_sozlugu=_qc_kitap_sozlugu,
                    tutarlilik_denetimi=not manual_translation,
                )
                initial_qc = _qc_analyze(qc_engine, translated_body, latex_body,
                                         manual_translation, cache=_qc_cache)
                logger.info("Initial QC: score=%.0f/100, issues=%d (critical=%d, high=%d, "
                             "medium=%d, low=%d)",
                             initial_qc.score, len(initial_qc.issues),
                             initial_qc.critical_count, initial_qc.high_count,
                             initial_qc.medium_count, initial_qc.low_count)

                # Pass translator to AutoFixer for re-translation of untranslated blocks.
                # manual_translation=True when --import-tex is used: human/Claude-translated
                # body is already clean, so glossary_compliance and wrong_translation
                # handlers (designed for HY-MT) would only introduce half-translations.
                fixer = AutoFixer(
                    qc_engine=qc_engine,
                    translator=translator,
                    min_qc_score=config.min_qc_score,
                    max_iterations=config.max_autofix_iterations,
                    manual_translation=manual_translation,
                    user_defined_envs=_user_env_names,
                )
                # F17: initial_qc yukarıda AYNI gövde+kaynak+manuel bayrakla
                # üretildi — run() baştaki analizi atlayıp onu devralır.
                translated_body, fix_report = fixer.run(
                    translated_body, latex_body, initial_report=initial_qc)

                logger.info("AutoFix: %d iterations, %d fixes, score %.0f -> %.0f, "
                             "converged=%s, threshold=%.0f (met=%s)",
                             fix_report.iterations, fix_report.total_fixes,
                             fix_report.initial_score, fix_report.final_score,
                             fix_report.converged,
                             fix_report.min_qc_score, fix_report.met_threshold)

                if fix_report.remaining_issues:
                    logger.info("Remaining issues after autofix: %d",
                                len(fix_report.remaining_issues))
                    for issue in fix_report.remaining_issues[:5]:
                        logger.info("  [%s] L%d: %s", issue.severity.upper(),
                                   issue.line_num, issue.message[:80])

                # Save sanitized+fixed body
                tr_body_path = source_dir / "translated_body.tex"
                tr_body_path.write_text(translated_body, encoding="utf-8")
                logger.info("Saved sanitized+fixed translated_body.tex: %d chars",
                             len(translated_body))

                # Save QC report
                qc_report_path = out / QC_REPORT_NAME
                import io, contextlib
                qc_buf = io.StringIO()
                with contextlib.redirect_stdout(qc_buf):
                    final_qc = _qc_analyze(qc_engine, translated_body, latex_body,
                                           manual_translation, cache=_qc_cache)
                    final_qc.print_summary()
                qc_report_path.write_text(qc_buf.getvalue(), encoding="utf-8")
                _phase55_failed = False
            except Exception as exc:
                _phase55_failed = True
                logger.error("Phase 5.5 failed: %s", exc, exc_info=True)
                logger.info("Continuing with current translated_body.tex "
                            "(exit code will reflect the skipped QC gate)")

        # -- Phase 5.7: Second Pass (Retranslator) ----------------------------
        # İkinci geçiş (yeniden gözden geçirme): çeviride kaynak dilde
        # kalmış satırlar taranır ve yerel modelle yeniden çevrilir.
        # Runs AFTER autofix, BEFORE compile.  scan_and_retranslate HY-MT 7B
        # kullanir -> SADECE Yol B (HY-MT) icin. Manual/ajan cevirisinde (Yol A)
        # ajan otoriter; bilincli birakilan Ingilizce (akronim/terim) bu pass'te
        # HY-MT ile yeniden cevrilirse BOZULUR (örn. "transfer orbit" ->
        # "aktarim aktarmak", "escape velocity" -> "kacis hizi"). Iki-kol ayrimi:
        # manual_translation modunda Phase 5.7 atlanir (2026-06-15).
        if (translated_body is not None and not skip_qc and translator is not None
                and not manual_translation):
            try:
                from latex_pipeline.retranslator import scan_and_retranslate
                import io, contextlib

                if qc_engine is not None:
                    pre_rt_qc = _qc_analyze(qc_engine, translated_body, latex_body,
                                            manual_translation, cache=_qc_cache)
                    pre_score = pre_rt_qc.score
                else:
                    pre_score = 0.0

                logger.info("=" * 60)
                logger.info("  Phase 5.7: Second Pass (Retranslator)")
                logger.info("  Pre-retranslation score: %.0f/100", pre_score)
                logger.info("=" * 60)

                def _translator_fn(text: str) -> str:
                    """Bridge retranslator's API to LatexTranslator."""
                    if hasattr(translator, '_safe_translate'):
                        return translator._safe_translate(text)
                    elif hasattr(translator, 'translate'):
                        return translator.translate(text)
                    return text

                rt_result = scan_and_retranslate(
                    translated_body,
                    translator_fn=_translator_fn,
                    # 2026-09-24: başlık satırları başlık otoritesiyle
                    # (başlık modu MT + dil kapısı); gövde yolu Title Case
                    # başlığı NER'e yediriyor, echo ile başarısız oluyordu.
                    heading_translator_fn=getattr(
                        translator, "_translate_heading_text", None),
                    # K5: çevrilmemiş satır kaynak dilde aranır.
                    source_lang=config.source_lang,
                )

                # RT-07: adopt the output when EITHER lines were retranslated
                # OR the final heading sentence-case pass changed something.
                # Previously heading-case fixes on 0 retranslations were thrown
                # away (inconsistent: a heading got fixed only if some unrelated
                # line happened to be retranslated).
                if (rt_result.retranslated_lines > 0
                        or rt_result.heading_case_fixes > 0):
                    translated_body = rt_result.output_tex
                    logger.info(
                        "Retranslator: %d lines scanned, %d English found, "
                        "%d retranslated, %d heading-case, %d failed",
                        rt_result.scanned_lines,
                        rt_result.english_lines_found,
                        rt_result.retranslated_lines,
                        rt_result.heading_case_fixes,
                        rt_result.failed_lines,
                    )

                    # Save retranslated body
                    tr_body_path = source_dir / "translated_body.tex"
                    tr_body_path.write_text(translated_body, encoding="utf-8")

                    # Re-run QC after second pass
                    if qc_engine is not None:
                        post_rt_qc = _qc_analyze(qc_engine, translated_body, latex_body,
                                                 manual_translation, cache=_qc_cache)
                        logger.info(
                            "Post-retranslation QC: score %.0f/100 (was %.0f)",
                            post_rt_qc.score, pre_score,
                        )

                        # Update QC report
                        qc_report_path = out / QC_REPORT_NAME
                        qc_buf2 = io.StringIO()
                        with contextlib.redirect_stdout(qc_buf2):
                            post_rt_qc.print_summary()
                        qc_report_path.write_text(qc_buf2.getvalue(), encoding="utf-8")
                else:
                    logger.info(
                        "Retranslator: no English lines found to retranslate."
                    )
            except Exception as exc:
                logger.error("Phase 5.7 failed: %s", exc, exc_info=True)
                logger.info("Continuing with current translated_body.tex")

        # -- Phase 5.71: Post-Retranslation Env Name Guard ---------------------
        if translated_body is not None and not skip_qc:
            try:
                from latex_pipeline.autofix import AutoFixer as _PostRtAutoFixer
                _env_fix_map = _PostRtAutoFixer._TRANSLATED_ENV_FIX_MAP
                _env_fix_count = 0
                # A-9 (2026-07-27): gövdede \newenvironment/\newtheorem ile
                # tanımlı adlara dokunma (sanitizer'daki guard ile aynı) —
                # kullanıcı tanımlı Türkçe ortamın adını değiştirmek
                # tanım-kullanım tutarlılığını bozup derlemeyi kırar.
                _defined_envs = set(re.findall(
                    r"\\(?:newenvironment|renewenvironment|newtheorem\*?)\s*\{([^}]+)\}",
                    translated_body,
                )) | _user_env_names
                for wrong_name, correct_name in _env_fix_map.items():
                    if wrong_name in _defined_envs:
                        continue
                    _pat = re.compile(
                        r"(\\(?:begin|end))\{"
                        + re.escape(wrong_name)
                        + r"(\*?)\}"
                    )
                    if _pat.search(translated_body):
                        translated_body = _pat.sub(
                            lambda m, c=correct_name: f"{m.group(1)}{{{c}{m.group(2)}}}",
                            translated_body,
                        )
                        _env_fix_count += 1
                if _env_fix_count > 0:
                    logger.info(
                        "  Phase 5.71: Fixed %d translated env name type(s) "
                        "after retranslation.", _env_fix_count,
                    )
                    tr_body_path = source_dir / "translated_body.tex"
                    tr_body_path.write_text(translated_body, encoding="utf-8")
            except Exception as exc:
                logger.error("Phase 5.71 failed: %s", exc, exc_info=True)
                logger.info("Continuing with current translated_body.tex")

        # -- Phase 5.75: Kitap sözlüğü doğrulama geçişi -----------------------
        # G2c (KARAR 25, 2026-10-05): genel glossary/ (2956 İngilizce terim)
        # YERİNE kitabın kendi sözlüğü (glossary_shared.tsv; kaynak dilde, altı
        # dilin hepsinde): yalnız çıktıda ÖZGÜN dilde kalmış terim tam
        # eşleşmeyle karşılığına çevrilir (matematik/başlık korunur; Türkçe ekli
        # geçişe ve çevrilmemiş cümleye dokunulmaz). Genel alan sözlüğü
        # yüklenmez ("transfer orbit" -> "aktarım aktarmak" bozulması bitti).
        # İngilizce dışı kaynakta İngilizce->Türkçe curated sözlük kitap
        # sözlüğüne girmez; günlükteki "sözlük geçişi atlandı" o kısmı söyler.
        # Manual/ajan modunda (Yol A) atlanır (iki-kol ayrımı, 2026-06-15).
        if translated_body is not None and not skip_qc and not manual_translation:
            if config.source_lang != "en":
                logger.info(
                    "  Phase 5.75: sözlük geçişi atlandı (İngilizce->Türkçe "
                    "curated sözlük, kaynak dil '%s'); kitap sözlüğü geçişi "
                    "koşar.", config.source_lang)
            try:
                if _kitap_sozlugu is not None:
                    translated_body = _kitap_sozlugu.verify_terms(
                        translated_body, term_map={})
                _n_terim = len(_kitap_sozlugu) if _kitap_sozlugu is not None else 0
                _n_gecis = getattr(_kitap_sozlugu, "son_verify_sayisi", 0)
                if config.source_lang == "en":
                    logger.info(
                        "  Phase 5.75: Post-edit glossary pass complete (kitap "
                        "sözlüğü: %d terim; %d geçiş karşılığına çevrildi)",
                        _n_terim, _n_gecis)
                else:
                    logger.info(
                        "  Phase 5.75: kitap sözlüğü geçişi tamam (%d terim; %d "
                        "geçiş karşılığına çevrildi)", _n_terim, _n_gecis)
            except Exception as exc:
                logger.warning("Post-edit glossary pass failed: %s", exc)

        # -- Phase 5.8: Targeted Segment Retry --------------------------------
        # QC score < 99 olan segmentleri HY-MT ile yeniden cevirir -> Yol B icin.
        # Manual modda (Yol A) QC score zaten dusuk (bilincli Ingilizce terimler
        # "untranslated" sayilir) -> cok segment HY-MT'ye gider, ajan cevirisini
        # bozar. Manual modda atlanir (iki-kol ayrimi, 2026-06-15).
        if (translated_body is not None and not skip_qc and translator is not None
                and qc_engine is not None and not manual_translation):
            try:
                post_all_qc = _qc_analyze(qc_engine, translated_body, latex_body,
                                          manual_translation, cache=_qc_cache)
                if post_all_qc.score < 99:
                    retry_issues = [
                        i for i in post_all_qc.issues
                        if i.check_name in (
                            "number_hallucination", "untranslated",
                            "prompt_leakage", "foreign_language_leakage",
                        )
                        and i.line_num > 0
                    ]
                    if retry_issues:
                        logger.info("=" * 60)
                        logger.info("  Phase 5.8: Targeted Segment Retry (%d issues)",
                                   len(retry_issues))
                        logger.info("=" * 60)

                        lines = translated_body.split("\n")
                        en_lines = latex_body.split("\n") if latex_body else []
                        retried = 0

                        # Import env name protection from retranslator
                        from latex_pipeline.retranslator import (
                            _stash_env_names, _restore_env_names,
                            _clean_prompt_leaks, is_untranslated_line,
                        )

                        # -- Helper: find EN counterpart for a TR line -----
                        _STRUCT_RE = re.compile(
                            r"\\(?:section|subsection|subsubsection|paragraph"
                            r"|chapter|begin|end|label|caption|item)\b"
                            r"|\\(?:textbf|textit|emph)\{[^}]{0,60}\}"
                        )

                        def _find_en_counterpart(tr_line: str, tr_idx: int,
                                                 en_lines: list[str],
                                                 search_radius: int = 15) -> str:
                            """Return the best-matching EN line for a given TR line."""
                            if not en_lines:
                                return ""
                            tr_structs = set(_STRUCT_RE.findall(tr_line))
                            lo = max(0, tr_idx - search_radius)
                            hi = min(len(en_lines), tr_idx + search_radius + 1)

                            best_idx = -1
                            best_overlap = 0
                            for ei in range(lo, hi):
                                en_structs = set(_STRUCT_RE.findall(en_lines[ei]))
                                overlap = len(tr_structs & en_structs)
                                if overlap > best_overlap:
                                    best_overlap = overlap
                                    best_idx = ei
                                elif overlap == best_overlap and overlap > 0:
                                    if abs(ei - tr_idx) < abs(best_idx - tr_idx):
                                        best_idx = ei

                            if best_idx >= 0 and best_overlap > 0:
                                return en_lines[best_idx]
                            # RT-05: NO structural anchor -> do not guess.
                            # TR/EN line counts drift after sanitize/autofix, so
                            # a positional pick (en_lines[tr_idx]) silently
                            # returns the WRONG English line and its translation
                            # gets written over an unrelated TR line. Skip
                            # instead of corrupting content.
                            return ""
                        # -- end helper ------------------------------------

                        for issue in retry_issues[:10]:  # max 10 retries
                            li = issue.line_num - 1  # 0-based (TR index)
                            if 0 <= li < len(lines):
                                old_line = lines[li]
                                en_line = _find_en_counterpart(old_line, li, en_lines)
                                if en_line and hasattr(translator, '_safe_translate'):
                                    stashed_en, env_stash = _stash_env_names(en_line)
                                    new_tr = translator._safe_translate(stashed_en)
                                    if new_tr and new_tr != stashed_en and new_tr != old_line:
                                        new_tr = _restore_env_names(new_tr, env_stash)
                                        new_tr = _clean_prompt_leaks(new_tr)
                                        # RT-05/RT-B6: acceptance gate -- only
                                        # overwrite if the result is actually
                                        # Turkish now (mirrors Phase 5.7 line
                                        # 935). Otherwise keep the original.
                                        if (new_tr and new_tr.strip()
                                                and not is_untranslated_line(
                                                    new_tr, config.source_lang)):
                                            lines[li] = new_tr
                                            retried += 1
                                            logger.info(
                                                "  Retry L%d [%s]: retranslated",
                                                issue.line_num, issue.check_name,
                                            )

                        if retried > 0:
                            translated_body = "\n".join(lines)
                            tr_body_path = source_dir / "translated_body.tex"
                            tr_body_path.write_text(translated_body, encoding="utf-8")
                            post_retry_qc = _qc_analyze(qc_engine, translated_body, latex_body,
                                                        manual_translation, cache=_qc_cache)
                            logger.info(
                                "  Segment retry: %d lines retried, score %.0f -> %.0f",
                                retried, post_all_qc.score, post_retry_qc.score,
                            )
            except Exception as exc:
                logger.error("Phase 5.8 failed: %s", exc, exc_info=True)
                logger.info("Continuing with current translated_body.tex")

        # -- End-of-iteration QC check for outer loop -------------------------
        # Evaluate current score; break if threshold met or no progress.
        _current_outer_score = -1.0
        if translated_body is not None and not skip_qc and qc_engine is not None:
            try:
                _outer_qc = _qc_analyze(qc_engine, translated_body, latex_body,
                                        manual_translation, cache=_qc_cache)
                _current_outer_score = _outer_qc.score
            except Exception:
                pass  # keep -1 so loop exits on next check

        logger.info("Outer iteration %d/%d: score=%.0f (prev=%.0f, target=%.0f)",
                     _outer_iter, _outer_max, _current_outer_score,
                     _prev_outer_score, config.min_qc_score)

        # Condition 1: score meets threshold
        if _current_outer_score >= config.min_qc_score:
            logger.info("QC score %.0f >= threshold %.0f. Outer loop done.",
                        _current_outer_score, config.min_qc_score)
            break

        # Condition 2: no improvement (stagnation guard)
        if _current_outer_score <= _prev_outer_score and _outer_iter > 1:
            logger.info("QC score not improving (%.0f <= %.0f). "
                        "Stopping outer loop to avoid wasted iterations.",
                        _current_outer_score, _prev_outer_score)
            break

        # Condition 2b (F36, 2026-07-29): manuel modda (Yol A) iyileştirme
        # fazlarının çoğu kapalı (5.7/5.75/5.8 atlanır). Bu iterasyon gövdeyi
        # HİÇ değiştirmediyse aynı girdiyle koşacak bir sonraki iterasyon
        # tanımsal olarak aynı gövdeyi ve aynı skoru üretir; eskiden bu boşa
        # tur koşulup ancak 'not improving' ile duruyordu. Skor/çıkış kodu
        # davranışı aynı, yalnız beyhude sanitize+autofix turu kalkar.
        if manual_translation and translated_body == _iter_start_body:
            logger.info(
                "Manuel mod: iterasyon %d gövdeyi değiştirmedi — sonraki "
                "iterasyon aynı sonucu üreteceği için dış döngü burada "
                "bitiriliyor (skor=%.0f, eşik=%.0f).",
                _outer_iter, _current_outer_score, config.min_qc_score)
            break

        _prev_outer_score = _current_outer_score

        # Condition 3: nothing to loop over (QC was skipped or no body)
        if translated_body is None or skip_qc:
            break
    # -- End outer QC loop ----------------------------------------------------

    # -- Phase 6: Compile -----------------------------------------------------
    # Kitaba özel preamble (Yol A serbestliği, 2026-07-27):
    # output/<slug>/preamble_ozel.tex varsa içeriği preamble'a AYNEN eklenir —
    # kullanıcı kendi \usepackage / \newcommand / \newtheorem satırlarını
    # şablonu değiştirmeden ekleyebilir. \write18 içeren dosya yok sayılır.
    _extra_preamble = None
    _preamble_path = out / "preamble_ozel.tex"
    if _preamble_path.exists():
        try:
            _pp_text = _preamble_path.read_text(encoding="utf-8", errors="replace")
            if "\\write18" in _pp_text:
                logger.warning(
                    "preamble_ozel.tex '\\write18' içeriyor — güvenlik "
                    "gereği YOK SAYILDI.")
            elif _pp_text.strip():
                _extra_preamble = _pp_text
                logger.info("Kitaba özel preamble yüklendi: %s (%d karakter)",
                            _preamble_path, len(_pp_text))
        except OSError as _pp_exc:
            logger.warning("preamble_ozel.tex okunamadı: %s", _pp_exc)

    tr_result = None
    if translated_body is not None and not skip_compile:
        logger.info("=" * 60)
        logger.info("  Phase 6: Compile Turkish PDF")
        logger.info("=" * 60)

        book_tr_tex = template_engine.wrap_content(
            translated_body, cover_pdf_path=_cover_pdf,
            extra_preamble=_extra_preamble)

        # -- Phase 6 Sanitize: run sanitizer on the FULL template output ------
        # The body was already sanitized in Phase 5.5, but the template
        # engine may introduce preamble/wrapper text that has not been
        # sanitized (e.g. prompt leaks in template strings).
        # manual_translation MUST be forwarded here: the full document
        # contains the agent-translated body, and running MT-profile fixes
        # (english-leak / stuck-word / foreign-word) on it would corrupt
        # deliberate English kept by the agent (Yol A).
        template_sanitizer = LatexSanitizer(manual_translation=manual_translation,
                                            user_defined_envs=_user_env_names)
        book_tr_tex, san_template_report = template_sanitizer.sanitize(book_tr_tex)
        if san_template_report.total_fixes > 0:
            logger.info(
                "Template sanitize: %d fixes (prompts=%d, artifacts=%d)",
                san_template_report.total_fixes,
                san_template_report.prompt_leaks_fixed,
                san_template_report.artifacts_removed,
            )
        # F3 (2026-09-30): "Yerel model ile çevir" çıktısına makine çevirisi
        # künyesi (TeX yoluyla / manuel çeviride KONMAZ). Temizlikten sonra.
        if not manual_translation:
            book_tr_tex = _add_mt_notice(book_tr_tex, output_dir, logger)

        # -- Phase 6 Guard: Brace balance check -------------------------------
        open_c, close_c, diff = check_brace_balance(book_tr_tex)
        if diff != 0:
            logger.warning(
                "Brace imbalance detected: { = %d, } = %d (diff = %+d). "
                "Attempting auto-fix...", open_c, close_c, diff
            )
            book_tr_tex = try_fix_brace_balance(book_tr_tex, logger_obj=logger,
                                                source_name="book_tr.tex")
            # Re-check after fix
            open_c2, close_c2, diff2 = check_brace_balance(book_tr_tex)
            if diff2 == 0:
                logger.info("Brace balance restored: { = %d, } = %d", open_c2, close_c2)
            else:
                logger.warning(
                    "Brace imbalance persists after fix: { = %d, } = %d (diff = %+d). "
                    "Proceeding with compile (nonstop mode).",
                    open_c2, close_c2, diff2,
                )
        else:
            logger.info("Brace balance OK: { = %d, } = %d", open_c, close_c)

        book_tr_path = source_dir / "book_tr.tex"
        book_tr_path.write_text(book_tr_tex, encoding="utf-8")

        compiler = LatexCompiler(config)
        tr_result = compiler.compile(book_tr_path, compiled_dir)

        if tr_result.success:
            # Rename to book name
            final_name = slugify(book_name) + "_TR.pdf"
            final_path = compiled_dir / final_name
            src_pdf = compiled_dir / "book_tr.pdf"
            if src_pdf.exists():
                shutil.copy2(src_pdf, final_path)
                logger.info("Turkish PDF: %s", final_path)
        else:
            logger.warning("Compile failed (%d errors)", len(tr_result.errors))
            for err in tr_result.errors[:10]:
                logger.warning("  > %s", err)
    elif translated_body is not None:
        book_tr_tex = template_engine.wrap_content(
            translated_body, cover_pdf_path=_cover_pdf,
            extra_preamble=_extra_preamble)
        # Sanitize template output even when compile is skipped
        # (manual_translation forwarded — same reason as the compile branch)
        template_sanitizer = LatexSanitizer(manual_translation=manual_translation,
                                            user_defined_envs=_user_env_names)
        book_tr_tex, san_template_report = template_sanitizer.sanitize(book_tr_tex)
        if san_template_report.total_fixes > 0:
            logger.info(
                "Template sanitize (no-compile): %d fixes",
                san_template_report.total_fixes,
            )
        if not manual_translation:      # F3: künye (derleme dalıyla aynı)
            book_tr_tex = _add_mt_notice(book_tr_tex, output_dir, logger)
        book_tr_path = source_dir / "book_tr.tex"
        book_tr_path.write_text(book_tr_tex, encoding="utf-8")
        logger.info("book_tr.tex saved (compile skipped)")

    # -- Phase 7: Quality Inspection ------------------------------------------
    logger.info("=" * 60)
    logger.info("  Phase 7: Quality Inspection")
    logger.info("=" * 60)

    inspector = QualityInspector(images_dir=images_dir)
    if tr_result is not None and tr_result.success:
        inspect_tex = source_dir / "book_tr.tex"
    else:
        inspect_tex = book_en_path

    report = inspector.inspect(inspect_tex)
    report.print_summary()

    report_path = out / "quality_report.txt"
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report.print_summary()
        report.print_details()
    report_path.write_text(buf.getvalue(), encoding="utf-8")

    # -- Summary & exit code ---------------------------------------------------
    elapsed = time.time() - t_global
    logger.info("")
    logger.info("=" * 60)
    logger.info("  PIPELINE COMPLETE (%.1fs = %.1f min)", elapsed, elapsed / 60)
    logger.info("=" * 60)

    # Determine exit code based on compile + QC results
    # 0 = success, 2 = compile failed, 3 = QC score below threshold
    if tr_result is not None and not tr_result.success:
        logger.warning("Exit code 2: Compile FAILED.")
        return 2

    # Fail-closed: QC was requested but never (successfully) ran -> the
    # quality gate could not be evaluated. Do not report success (issue #58,
    # eski #17/#21: Phase 5.5 kurulum hatasi exit 0'a dusuyordu).
    if translated_body is not None and not skip_qc and (
        qc_engine is None or _phase55_failed
    ):
        logger.warning(
            "Exit code 3: Phase 5.5 (sanitize/QC) did not complete — "
            "quality gate was never evaluated."
        )
        return 3

    # Check final QC score if QC was run
    if translated_body is not None and not skip_qc and qc_engine is not None:
        final_qc_check = _qc_analyze(qc_engine, translated_body, latex_body,
                                     manual_translation, cache=_qc_cache)
        if final_qc_check.score < config.min_qc_score:
            logger.warning(
                "Exit code 3: Final QC score %.0f < threshold %.0f.",
                final_qc_check.score, config.min_qc_score,
            )
            return 3

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    from core.lang_detect import TEX_SOURCE_LANGS, language_list_tr
    from ui.branding import TEX_TITLE

    parser = argparse.ArgumentParser(
        description="Tek komutla kitap çevirisi: PDF/EPUB -> LaTeX -> çeviri -> derleme",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_cli_epilog(),
    )
    parser.add_argument("input", help="Kaynak dosya yolu (PDF veya EPUB)")
    parser.add_argument("--name", default=None,
                        help="Kitap adı (çıktı klasörü ve PDF adı; varsayılan: dosya adından otomatik)")
    parser.add_argument("--output-dir", default=None,
                        help="Çıktı klasörü (varsayılan: output/<kitap_adı>/)")
    parser.add_argument("--device", default="mps", choices=["mps", "cpu", "cuda"],
                        help="Torch cihazı (varsayılan: mps)")
    parser.add_argument("--batch-size", type=int, default=20,
                        help="Çıkarım parti boyutu (varsayılan: 20, yalnız PDF)")
    parser.add_argument("--extract-only", action="store_true",
                        help="Yalnız çıkarım yap (MD -> LaTeX -> çeviri atlanır)")
    parser.add_argument("--skip-extract", action="store_true",
                        help="Çıkarımı atla (full_book.md zaten var)")
    parser.add_argument("--yeniden", action="store_true",
                        help="Klasördeki önceki çıkarımı (full_book.md) "
                             "kullanma; kitabı baştan çıkar (eskisi "
                             f"{PREVIOUS_EXTRACTION_NAME} adıyla saklanır). "
                             "Varsayılan: aynı kaynak dosyadan yapılmış "
                             "kullanılabilir çıkarım yeniden kullanılır.")
    parser.add_argument("--skip-translate", action="store_true",
                        help="Çeviriyi atla (yalnız kaynak dilde LaTeX)")
    parser.add_argument("--skip-compile", action="store_true",
                        help="XeLaTeX derlemesini atla (yalnız .tex üret)")
    parser.add_argument("--skip-qc", action="store_true",
                        help="QC + AutoFix fazını atla")
    parser.add_argument("--book-type", default=None,
                        choices=list(KITAP_TURLERI),
                        help="Kitap türü: science (bilim ve teknik), "
                             "philosophy (felsefe ve kişisel gelişim), "
                             "novel (roman); varsayılan: otomatik tespit")
    parser.add_argument("--resume", action="store_true",
                        help="Çökme sonrası kaldığı yerden devam et "
                             "(checkpoint ya da faz düzeyinde resume; "
                             "klasördeki çıkarım da yeniden kullanılır)")
    parser.add_argument("--export-tex", action="store_true",
                        help="Manuel çeviri akışı: çıkarım+dönüşüm çalışır, "
                             "MT atlanır, body.tex EXPORT.tex olarak yazılır. "
                             "(--skip-translate --skip-compile eşdeğeri + kopya)")
    parser.add_argument("--import-tex", default=None, metavar="PATH",
                        help="Manuel çeviri akışı: verilen .tex dosyasını "
                             "translated_body.tex olarak yerleştirir, sanitize+QC+"
                             "derleme akışını çalıştırır. Kitabın çıktı klasörü "
                             "girdi dosyasından türetilir.")
    parser.add_argument("--force-mt", action="store_true",
                        help=f"Kilidi kaldır: '{TEX_TITLE}' ile çevrilmiş "
                             f"({MANUAL_MARKER_NAME}'lı) kitabı bilerek "
                             "yerel modelle yeniden çevir (mevcut çeviri "
                             "translated_body.tex.bak olarak yedeklenir)")
    # (#72) Olu '--epub-output' bayragi kaldirildi: store_true + default=True
    # kombinasyonu hicbir zaman kapatilamiyordu; tek etkili anahtar zaten
    # --no-epub-output idi. EPUB cikti varsayilan ACIK.
    parser.add_argument("--no-epub-output", action="store_true",
                        help="EPUB girdide EPUB çıktı ÜRETME (yalnız PDF; "
                             "varsayılan: EPUB çıktı AÇIK)")
    parser.add_argument("--batch", nargs="*", default=None,
                        help="Toplu kip: dosya listesi ya da klasör. "
                             "Örnek: --batch kitap1.pdf kitap2.epub "
                             "ya da --batch /yol/kitaplar/")
    parser.add_argument("--mt-engine", default=mt_registry.DEFAULT_ENGINE,
                        metavar="KİMLİK",
                        help="Çeviri modeli (varsayilan: "
                             f"{mt_registry.DEFAULT_ENGINE}). Adlı motorlar "
                             "(yerleşik + config/mt_engines.json) ya da önekli "
                             "kimlik: mlx:<klasör>, gguf:<dosya>, "
                             "ollama:<model>, lmstudio:<model>, "
                             "openai:<adres>|<model> (yalnız bu bilgisayar). "
                             "Liste: python -m core.mt_engines.discovery; "
                             "ayrıntı: docs/YENI_MODEL_EKLEME.md. Model "
                             "kullanılamıyorsa çıkış kodu 5.")
    parser.add_argument("--dil-kontrolu-yok", dest="dil_kontrolu_yok",
                        action="store_true",
                        help="Yerel model kaynak dil kapılarını (çıkarım "
                             "öncesi ön kapı ve çıkarım sonrası tam kapı) "
                             "atla; kaynak İngilizce varsayılır. Varsayılan "
                             "olarak kitabın dili ölçülür, desteklenmeyen "
                             "bir dilse çeviri başlamadan durulur (çıkış "
                             "kodu 4); bu bayrak yalnız tespit yanlışsa "
                             "kullanılır.")
    parser.add_argument("--kaynak-dil", dest="kaynak_dil",
                        choices=TEX_SOURCE_LANGS, default=None,
                        metavar="KOD",
                        help="Kitabın kaynak dilini elle ver ("
                             f"{', '.join(TEX_SOURCE_LANGS)}: "
                             f"{language_list_tr(TEX_SOURCE_LANGS)}); iki "
                             "dil kapısı da atlanır. Hedef dil her zaman "
                             "Türkçe.")
    return parser.parse_args()


def _batch_output_dir(file_path: str, args) -> str:
    """Toplu kipte bir kitabın çıktı klasörü (tek kaynak: run_batch ve model
    ön kontrolü aynı yolu hesaplar). --output-dir yalnız tek kitapta verilir
    (main çoklu kitapta reddeder)."""
    if getattr(args, "output_dir", None):
        return str(Path(args.output_dir).resolve())
    book_name = getattr(args, "name", None) or guess_book_name(file_path)
    return str(cikti_koku(PROJECT_ROOT) / slugify(book_name))


def _batch_needs_model(file_list: list, args) -> bool:
    """Toplu kipte en az bir kitap MT'ye gerçekten girecek mi? (F3-13/F5-12)

    Kitap klasörü başına _engine_precheck_applies: --resume ile çevirisi
    bitmiş (translated_body.tex kullanılabilir) ya da Yol A kilitli kitap
    modeli kullanmaz. Var olmayan / biçimi tanınmayan dosyalar atlanır
    (run_batch onları "atlandı" sayar).
    """
    for fp in file_list:
        path = Path(fp).resolve()
        if not path.exists():
            continue
        try:
            detect_input_format(str(path))
        except ValueError:
            continue
        if _engine_precheck_applies(args, _batch_output_dir(str(path), args)):
            return True
    return False


def run_batch(file_list: list, args) -> dict:
    """Birden fazla kitabı sıralı işle.

    Returns:
        {"success": [...], "failed": [...], "skipped": [...]}
    """
    results = {"success": [], "failed": [], "skipped": []}

    print(f"\n{'='*60}")
    print(f"  TOPLU KİP: {len(file_list)} kitap")
    print(f"{'='*60}")
    for i, fp in enumerate(file_list, 1):
        print(f"  {i}. {Path(fp).name}")
    print(f"{'='*60}\n")

    for i, file_path in enumerate(file_list, 1):
        file_path = str(Path(file_path).resolve())

        if not Path(file_path).exists():
            print(f"\n[{i}/{len(file_list)}] ATLANDI (dosya yok): {file_path}")
            results["skipped"].append(file_path)
            continue

        try:
            input_format = detect_input_format(file_path)
        except ValueError:
            print(f"\n[{i}/{len(file_list)}] ATLANDI (biçim tanınmadı; "
                  f"yalnız .pdf ve .epub): {file_path}")
            results["skipped"].append(file_path)
            continue

        print(f"\n{'='*60}")
        print(f"  [{i}/{len(file_list)}] {Path(file_path).name}")
        print(f"{'='*60}")

        book_name = args.name or guess_book_name(file_path)
        output_dir = _batch_output_dir(file_path, args)
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        t0 = time.time()
        try:
            # F3-12 (2026-09-29): toplu kip de tek kitapla aynı çıkarım
            # kuralını izler: aynı kaynaktan yapılmış kullanılabilir çıkarım
            # (ya da --resume) yeniden kullanılır, --skip-extract uygulanır,
            # --yeniden baştan çıkarır.
            skip_extract = bool(getattr(args, "skip_extract", False))
            reused, reuse_note = False, ""
            if not skip_extract and not getattr(args, "yeniden", False):
                reused, reuse_note = extraction_reuse_decision(
                    output_dir, file_path,
                    resume=bool(getattr(args, "resume", False)))
                skip_extract = reused

            # Yol A kilidi (bkz. main): klasördeki TeX yolu
            # çevirisi çıkarımdan ÖNCE korunur, kitap başarısız sayılır.
            if _yol_a_lock_applies(args, output_dir):
                _lock_exit = _yol_a_lock_stop(output_dir)
                elapsed = time.time() - t0
                results["failed"].append(
                    (file_path, f"exit={_lock_exit}", elapsed))
                print(f"  BAŞARISIZ (çıkış {_lock_exit}, {elapsed:.0f} sn)")
                continue

            # Kaynak dil ön kapısı (bkz. main): dili desteklenmeyen kitap
            # çıkarıma girmeden başarısız sayılır, sıradaki kitaba geçilir.
            if _pregate_applies(args, output_dir,
                                extraction_runs=not skip_extract):
                _pre_exit = _source_language_pregate(file_path, input_format,
                                                     output_dir)
                if _pre_exit is not None:
                    elapsed = time.time() - t0
                    results["failed"].append(
                        (file_path, f"exit={_pre_exit}", elapsed))
                    print(f"  BAŞARISIZ (çıkış {_pre_exit}, {elapsed:.0f} sn)")
                    continue

            # Extraction
            dest = Path(output_dir) / (
                "source_book.pdf" if input_format == "pdf"
                else "source_book.epub")
            if not dest.exists():
                shutil.copy2(file_path, dest)
            if reused:
                _print_extraction_reused(reuse_note)
            elif skip_extract:
                if not (Path(output_dir) / EXTRACTION_NAME).exists():
                    raise RuntimeError(
                        f"--skip-extract verildi ama {EXTRACTION_NAME} yok")
            else:
                ok = _run_book_extraction(file_path, input_format, output_dir,
                                          args)
                if not ok:
                    raise RuntimeError("Çıkarım başarısız")

            pipeline_pdf = file_path if input_format == "pdf" else None
            exit_code = run_pipeline(
                output_dir, book_name,
                skip_translate=args.skip_translate,
                skip_compile=args.skip_compile,
                skip_qc=args.skip_qc,
                book_type=args.book_type,
                pdf_path=pipeline_pdf,
                resume=args.resume,
                manual_translation=bool(getattr(args, "import_tex", None)),
                force_mt=bool(getattr(args, "force_mt", False)),
                mt_engine=getattr(args, "mt_engine",
                                  mt_registry.DEFAULT_ENGINE),
                source_lang_check=not getattr(args, "dil_kontrolu_yok", False),
                source_lang=getattr(args, "kaynak_dil", None),
            )

            elapsed = time.time() - t0
            if exit_code == 0:
                results["success"].append((file_path, elapsed))
                print(f"  BAŞARILI ({elapsed:.0f} sn)")
            else:
                results["failed"].append((file_path, f"exit={exit_code}", elapsed))
                print(f"  BAŞARISIZ (çıkış {exit_code}, {elapsed:.0f} sn)")
                # 2026-09-28 (Vson; Vk bulgusu 3): model koşu sırasında
                # kullanılamaz olduysa (sunucu koptu) kalan kitaplar da aynı
                # yerde düşer — her biri önce pahalı çıkarımı yapıp sonra 5
                # alırdı. Toplu kip durur, kalanlar "atlandı" sayılır ve
                # çıkış kodu 5 olur (main).
                if exit_code == EXIT_MODEL_UNAVAILABLE:
                    rest = [str(Path(fp).resolve()) for fp in file_list[i:]]
                    if rest:
                        print(f"  Çeviri modeli kullanılamıyor; kalan "
                              f"{len(rest)} kitap atlandı.")
                    results["skipped"].extend(rest)
                    break

        except KeyboardInterrupt:
            print("\n\nToplu kip iptal edildi (Ctrl+C).")
            results["failed"].append((file_path, "interrupted", time.time() - t0))
            break
        except Exception as exc:
            elapsed = time.time() - t0
            results["failed"].append((file_path, str(exc), elapsed))
            print(f"  HATA: {exc} ({elapsed:.0f} sn)")

        # GC: model bellek sızıntısını önle
        import gc
        gc.collect()

    # Özet (F5-15: Türkçe harfler, ondalık virgül)
    print(f"\n{'='*60}")
    print("  TOPLU KİP ÖZETİ")
    print(f"{'='*60}")
    print(f"  Başarılı:  {len(results['success'])}")
    print(f"  Başarısız: {len(results['failed'])}")
    print(f"  Atlanan:   {len(results['skipped'])}")
    if results["success"]:
        total_t = sum(t for _, t in results["success"])
        print(f"  Toplam:    {total_t:.0f} sn ({_tr_decimal(total_t / 60)} dk)")
    for fp, err, _ in results["failed"]:
        print(f"  BAŞARISIZ: {Path(fp).name} — {err}")
    for fp in results["skipped"]:
        print(f"  ATLANDI:   {Path(fp).name}")
    print(f"{'='*60}")

    return results


def _batch_exit_code(results: dict) -> int:
    """Toplu kipin çıkış kodu: hepsi başarılı 0; bir kitapta model
    kullanılamaz olduysa ``EXIT_MODEL_UNAVAILABLE`` (tek kitap koşusuyla aynı
    anlam; arayüz/otomasyon model iletisini gösterebilsin); başka başarısızlık
    1 (Vson, 28 Eylül 2026; Vk bulgusu 3).

    F5-11 (2026-09-29): atlanan kitap varsa (dosya yok, biçim tanınmadı) da
    1 — eskiden hiçbir kitap işlenmese bile ("Başarılı: 0, Atlanan: 3")
    çıkış 0'dı, otomasyon başarı görüyordu.
    """
    failed = results.get("failed") or []
    if failed:
        if any(err == f"exit={EXIT_MODEL_UNAVAILABLE}" for _fp, err, *_ in failed):
            return EXIT_MODEL_UNAVAILABLE
        return 1
    if results.get("skipped"):
        return 1
    return 0


def main() -> None:
    args = parse_args()

    # --- Manuel ceviri convenience flag'leri ---
    # F5-15 (2026-09-29): iletiler Türkçe harflerle (eskiden ASCII'ye kaçıyordu).
    if args.export_tex and args.import_tex:
        print("HATA: --export-tex ve --import-tex aynı anda kullanılamaz.")
        sys.exit(1)
    # RT-11: --batch + --import-tex sessizce bozuk (batch dali import-tex'i
    # translated_body.tex'e KOPYALAMADAN once donuyor -> Ingilizce cikti
    # 'BASARILI' raporlaniyor). Tek-kitap import akisi zorunlu.
    if args.batch is not None and args.import_tex:
        print("HATA: --batch ve --import-tex aynı anda kullanılamaz. "
              "--import-tex tek kitap içindir (toplu kip çeviri dosyasını "
              "yerleştirmez).")
        sys.exit(1)
    if args.export_tex:
        args.skip_translate = True
        args.skip_compile = True
    if args.import_tex:
        _imp = Path(args.import_tex).resolve()
        if not _imp.exists():
            print(f"HATA: --import-tex dosyası bulunamadı: {_imp}")
            sys.exit(1)
        # (eski ASCII ileti: "--import-tex dosyasi cok kisa"; F5-15)
        if _imp.stat().st_size < 100:
            print(f"HATA: --import-tex dosyası çok kısa "
                  f"({_imp.stat().st_size} bayt): {_imp}")
            sys.exit(1)
        # F14: kodlamayı extraction/pipeline'dan ÖNCE doğrula — yanlış
        # kodlamalı dosya eskiden dakikalar sonra, pipeline derinliğinde ham
        # UnicodeDecodeError traceback'i olarak patlıyordu.
        try:
            _read_translated_body(_imp)
        except UnicodeDecodeError as _uexc:
            print(f"HATA: {_encoding_error_hint(_imp, _uexc)}")
            sys.exit(1)
        args.import_tex = str(_imp)
        args.skip_translate = True
        args.resume = True

    # --- Batch mode ---
    if args.batch is not None:
        if len(args.batch) == 0:
            # --batch argümansız: input dosyasını tek başına işle
            file_list = [args.input]
        elif len(args.batch) == 1 and Path(args.batch[0]).is_dir():
            batch_dir = Path(args.batch[0])
            file_list = sorted(
                str(f) for f in batch_dir.iterdir()
                if f.suffix.lower() in (".pdf", ".epub")
            )
            if not file_list:
                print(f"HATA: {batch_dir} klasöründe PDF/EPUB bulunamadı.")
                sys.exit(1)
        else:
            file_list = args.batch

        # (#66) --batch (coklu kitap) + --name: HER kitap ayni slug/output
        # dizinine yazilip kapak/cikti capraz bulasiyordu (ilk kitabin
        # source_book.pdf'si yerinde kaliyor, digerleri onu kapak saniyor).
        # F5-15: ileti Türkçe harflerle. Eski ASCII biçimi:
        # "--batch ile birden fazla kitap islerken --name kullanilamaz"
        if args.name and len(file_list) > 1:
            print("HATA: --batch ile birden fazla kitap işlenirken --name "
                  "kullanılamaz; bütün kitaplar aynı çıktı klasörüne "
                  "yazılırdı. Adlar dosya adlarından otomatik türetilir.")
            sys.exit(1)
        # F3-12 (2026-09-29): --output-dir toplu kipte sessizce yok
        # sayılıyordu. Tek kitapta uygulanır; birden fazla kitapta --name
        # gibi reddedilir (hepsi aynı klasöre yazılırdı).
        if args.output_dir and len(file_list) > 1:
            print("HATA: --batch ile birden fazla kitap işlenirken "
                  "--output-dir kullanılamaz; bütün kitaplar aynı klasöre "
                  "yazılırdı. Klasörler output/<kitap adı> olarak türetilir.")
            sys.exit(1)

        # Model kontrolü döngüden ÖNCE bir kez (2026-09-25): kullanılamayan
        # modelle hiçbir kitabın çıkarımı başlamaz. Donanım kapısı (2026-09-28,
        # KARARLAR 17) her motorda, varsayılan dahil.
        # F3-13 / F5-12 (2026-09-29): yalnız en az bir kitap MT'ye gerçekten
        # girecekse (kitap klasörü başına _engine_precheck_applies); --resume
        # ile çevirisi bitmiş kitaplar sunucu kapalı diye 5 ile durmaz (tek
        # kitap kipiyle aynı).
        _batch_engine = getattr(args, "mt_engine", mt_registry.DEFAULT_ENGINE)
        _batch_mt = (_engine_precheck_applies(args)
                     and _batch_needs_model(file_list, args))
        if _batch_mt:
            _hw = _hardware_check(_batch_engine)
            if not _hw.ok:
                sys.exit(_engine_stop(_hw))
        if _batch_mt and _engine_check_needed(_batch_engine):
            _check = _check_engine(_batch_engine)
            if not _check.ok:
                sys.exit(_engine_stop(_check))

        results = run_batch(file_list, args)
        sys.exit(_batch_exit_code(results))

    input_path = str(Path(args.input).resolve())
    if not Path(input_path).exists():
        print(f"HATA: Dosya bulunamadı: {input_path}")
        sys.exit(1)

    # Detect input format (pdf or epub)
    try:
        input_format = detect_input_format(input_path)
    except ValueError as e:
        print(f"HATA: {e}")
        sys.exit(1)

    # Determine book name
    book_name = args.name or guess_book_name(input_path)
    book_slug = slugify(book_name)
    if slug_is_fallback(book_name) and not args.output_dir:
        print(f"UYARI: Kitap adı ({book_name!r}) harf ya da rakam içermiyor; "
              f"klasör adı olarak '{book_slug}' kullanıldı.")

    # Determine output directory
    if args.output_dir:
        output_dir = str(Path(args.output_dir).resolve())
    else:
        output_dir = str(cikti_koku(PROJECT_ROOT) / book_slug)

    # Create output dir and copy source file
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # --import-tex: extraction zaten yapilmissa atla
    if args.import_tex and (Path(output_dir) / EXTRACTION_NAME).exists():
        if not args.skip_extract:
            print("--import-tex: full_book.md mevcut, çıkarım kendiliğinden atlandı.")
            args.skip_extract = True

    # F3-12 / F5-09 (2026-09-29): klasörde AYNI kaynak dosyadan yapılmış
    # kullanılabilir çıkarım varsa (1_cikar'ın ya da iptal edilen önceki
    # koşunun) yeniden kullanılır; --resume de çıkarımı baştan koşturmaz.
    # Kitabı baştan çıkarmak için --yeniden (1_cikar ile aynı sözleşme).
    # Karar burada yan etkisiz verilir; ileti çıkarımın yerinde basılır.
    _extraction_reused = False
    _reuse_note = ""
    if not args.skip_extract and not getattr(args, "yeniden", False):
        _extraction_reused, _reuse_note = extraction_reuse_decision(
            output_dir, input_path, resume=bool(args.resume))
        if _extraction_reused:
            args.skip_extract = True

    # Yol A kilidi ÇIKARIMDAN ÖNCE (2026-09-25, U4/S3): klasörde TeX
    # yoluyla yapılmış çeviri varken MT koşusu hiçbir kitap dosyasına
    # dokunmadan durur (eskiden çıkarım + dönüşüm dosyaları yeniden yazıp
    # pipeline.log'u sildikten sonra duruyordu). Dil ön kapısından önce:
    # kilidin iletisi öncelikli (tam kapıyla aynı sıra).
    if _yol_a_lock_applies(args, output_dir):
        sys.exit(_yol_a_lock_stop(output_dir))

    # Kaynak dil ön kapısı (yalnız yerel model yolu): dili desteklenmeyen kitap
    # Marker OCR'a girmeden durur. Tam kapı çıkarımdan sonra aynen çalışır.
    if _pregate_applies(args, output_dir,
                        extraction_runs=not args.skip_extract):
        _pre_exit = _source_language_pregate(input_path, input_format,
                                             output_dir)
        if _pre_exit is not None:
            sys.exit(_pre_exit)

    # Model kontrolü ÇIKARIMDAN ÖNCE (2026-09-25): sunucu kapalıysa / model
    # yoksa kullanıcı Marker OCR'ın bitmesini beklemeden çıkış 5 alır. Yol A
    # kilidi (2) ve dil ön kapısı (4) önceliklidir: dili desteklenmeyen
    # kitapta model değiştirmek çözüm değildir.
    _engine = getattr(args, "mt_engine", mt_registry.DEFAULT_ENGINE)
    # Donanım kapısı (2026-09-28, KARARLAR 17): her motorda (varsayılan dahil),
    # ağsız; bu Mac yerel model için yetersizse ya da süreç içi model toplu iş
    # 1 ile bile sığmıyorsa Marker OCR'a girmeden çıkış 5.
    if _engine_precheck_applies(args, output_dir):
        _hw = _hardware_check(_engine)
        if not _hw.ok:
            sys.exit(_engine_stop(_hw, output_dir))
    if _engine_precheck_applies(args, output_dir) and _engine_check_needed(_engine):
        _check = _check_engine(_engine)
        if not _check.ok:
            sys.exit(_engine_stop(_check, output_dir))

    # F1-11 (2026-09-29): bozuk dosya, kaynak kopyası ve çıkarımdan ÖNCE
    # açık iletiyle reddedilir (eskiden detect_pages ham traceback'le
    # düşüyordu). Kilit (2), dil ön kapısı (4) ve model (5) önceliklidir:
    # ön kapı açılamayan dosyada sessizce geçer.
    _problem = input_problem(input_path, input_format)
    if _problem:
        print(f"HATA: {_problem}")
        sys.exit(1)

    if input_format == "pdf":
        dest_pdf = Path(output_dir) / "source_book.pdf"
        if not dest_pdf.exists():
            shutil.copy2(input_path, dest_pdf)

        total_pages = detect_pages(input_path)
        print(f"\n{'='*60}")
        print(f"  Kitap:  {book_name}")
        print(f"  Format: PDF ({total_pages} sayfa)")
        print(f"  Cihaz:  {args.device}")
        print(f"  Çıktı:  {output_dir}")
        print(f"{'='*60}\n")

    elif input_format == "epub":
        dest_epub = Path(output_dir) / "source_book.epub"
        if not dest_epub.exists():
            shutil.copy2(input_path, dest_epub)

        print(f"\n{'='*60}")
        print(f"  Kitap:  {book_name}")
        print(f"  Format: EPUB")
        print(f"  Çıktı:  {output_dir}")
        print(f"{'='*60}\n")

    # Phase 1: PDF/EPUB Extraction (ya da önceki çıkarımın yeniden kullanımı)
    if _extraction_reused:
        _print_extraction_reused(_reuse_note)
    elif not args.skip_extract:
        ok = _run_book_extraction(input_path, input_format, output_dir, args)
        if not ok:
            print(f"HATA: {input_format.upper()} çıkarımı başarısız. Ayrıntı: "
                  f"{Path(output_dir) / PIPELINE_LOG_NAME}")
            sys.exit(1)

    if args.extract_only:
        print("\n--extract-only: pipeline atlandı. full_book.md hazır.")
        sys.exit(0)

    # Phase 2-7: Pipeline (same for both PDF and EPUB -- operates on full_book.md)
    # For EPUB, pdf_path is None (no source PDF for preflight check)
    pipeline_pdf_path = input_path if input_format == "pdf" else None

    # --- --import-tex: cevrilmis .tex'i translated_body.tex olarak yerlestir ---
    if args.import_tex:
        _src_dir = Path(output_dir) / "source"
        _src_dir.mkdir(parents=True, exist_ok=True)
        _target = _src_dir / "translated_body.tex"
        shutil.copy2(args.import_tex, _target)
        print(f"\n--import-tex: {args.import_tex}")
        print(f"             -> {_target} ({_target.stat().st_size} byte)")
        print(f"             resume=True ile sanitize+QC+derleme akışı başlayacak.\n")

    try:
        exit_code = run_pipeline(
            output_dir, book_name,
            skip_translate=args.skip_translate,
            skip_compile=args.skip_compile,
            skip_qc=args.skip_qc,
            book_type=args.book_type,
            pdf_path=pipeline_pdf_path,
            resume=args.resume,
            manual_translation=bool(args.import_tex),
            force_mt=bool(getattr(args, "force_mt", False)),
            mt_engine=getattr(args, "mt_engine", mt_registry.DEFAULT_ENGINE),
            source_lang_check=not getattr(args, "dil_kontrolu_yok", False),
            source_lang=getattr(args, "kaynak_dil", None),
        )
    except KeyboardInterrupt:
        print("\nKullanıcı tarafından iptal edildi (Ctrl+C).")
        exit_code = 130
    except Exception as exc:
        print(f"\nBeklenmeyen hata: {exc}")
        import traceback
        traceback.print_exc()
        exit_code = 1

    # --- --export-tex: body.tex'i META baslikli EXPORT.tex olarak yaz ---
    # (#8) 1_cikar ile ESDEGER cikti: % KITAPCEVIR-META basligi + sidecar.
    # Eski ciplak kopya (a) tasinan dosyada kimligi kaybettiriyor,
    # (b) 1_cikar'in META'li EXPORT.tex'ini basliksiz kopyayla eziyordu.
    if args.export_tex and exit_code in (0, 3):
        _body_path = Path(output_dir) / "source" / "body.tex"
        if _body_path.exists() and _body_path.stat().st_size > 100:
            from datetime import datetime as _dt

            from yol_a_ortak import (build_meta_header, read_sidecar,
                                     sha1_of_file, write_sidecar)

            _body_tex = _body_path.read_text(encoding="utf-8")
            _chapters = (_body_tex.count("\\chapter{")
                         + _body_tex.count("\\part{"))
            _meta = {
                "slug": slugify(book_name),
                "book_name": book_name,
                "output_dir": str(Path(output_dir).resolve()),
                "source_format": input_format,
                "source_sha1": sha1_of_file(input_path),
                "body_chapters": _chapters,
                # (F33, 2026-07-29) Tür bilgisi 1_cikar.py ile AYNI şekilde
                # (gerçek dosya adından) taşınır. Yoksa 2_derle META/sidecar'da
                # bulamayıp 'source_book.pdf' adından tespit deniyor ve
                # 'science' profiline düşüyordu -> roman/felsefe kitabında QC
                # yanlış profille koşup sahte exit 3 üretebiliyordu.
                "book_type": args.book_type or detect_book_type(input_path),
                "created": _dt.now().isoformat(timespec="seconds"),
            }
            _export_path = Path(output_dir) / "EXPORT.tex"
            _export_path.write_text(build_meta_header(_meta) + _body_tex,
                                    encoding="utf-8")
            # (F33) Sidecar KOMPLE ezilmez: mevcut alanlar (örneğin önceki
            # 1_cikar koşusunun yazdıkları) korunur, bu koşunun alanları
            # üzerine yazılır.
            _side = read_sidecar(output_dir)
            _side.update(_meta)
            write_sidecar(output_dir, _side)
            print(f"\n--export-tex: {_export_path} ({_export_path.stat().st_size} byte)")
            print(f"              Bu dosyayı çeviriyle doldurup")
            print(f"              --import-tex EXPORT_TR.tex ile geri verebilirsin.")
            print(f"              (Baştaki % KITAPCEVIR-META satırlarını KORU.)\n")
        else:
            print(f"\nUYARI: --export-tex istendi, fakat body.tex bulunamadı/çok kısa: {_body_path}")

    # EPUB çıktısı (isteğe bağlı): EPUB girişli kitapta çeviri, kaynak EPUB'un
    # yapısıyla yeniden EPUB olarak da yazılır (--no-epub-output kapatır).
    if (
        input_format == "epub"
        and not getattr(args, "no_epub_output", False)
        and exit_code in (0, 3)
    ):
        try:
            from latex_pipeline.epub_output import generate_epub_output
            # F3 (2026-09-30): yerel model çevirisinde künye; TeX yoluyla
            # çevrilmiş (MANUAL_TRANSLATION.marker) klasörde {} (künye yok).
            epub_out = generate_epub_output(
                original_epub=input_path,
                output_dir=output_dir,
                book_name=book_name,
                **mt_notice_epub_kwargs(output_dir),
            )
            if epub_out:
                print(f"\nEPUB çıktı: {epub_out}")
        except Exception as exc:
            print(f"\nEPUB çıktı oluşturulamadı: {exc}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
