"""
Ana Çeviri Motoru
Tüm bileşenleri entegre eder
"""

import re
import os
import json
import logging
import tempfile
import hashlib
import threading
import time
from collections import Counter
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Optional, Callable, List, Dict, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import sys

# Parent dizini path'e ekle (relative import sorunu için)
_parent_dir = str(Path(__file__).parent.parent)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

from core.glossary import GlossaryManager
from core.memory import (  # noqa: F401 (MEMORY_LANG_SEP/memory_pool_project: geriye uyum)
    MEMORY_LANG_SEP, TranslationMemory, is_echo_output, memory_pool_key,
    memory_pool_project,
)
from core.sentence_splitter import SentenceSplitter
from core.qa_checker import QAChecker, QAReport
from core.consistency import ConsistencyChecker, DocumentConsistencyAnalyzer, ConsistencyReport
from core.naturalizer import TurkishNaturalizer
from core.ner_protector import NERProtector
# Yer tutucu koruma cümlesinin yankı kalıbı — tek kaynak (2026-09-24).
from latex_pipeline.patterns import PLACEHOLDER_CLAUSE_LEAK_RE as _PLACEHOLDER_CLAUSE_LEAK_RE

# Merkezi config (magic number tek kaynak — KURAL §11)
from config.defaults import (
    DEFAULT_BATCH_SIZE_HYMT_LOCAL,
    DEFAULT_RETRY_COUNT,
    GENERATE_LOCK_TIMEOUT_S,
    MT_HTTP_TIMEOUT_S,
    MT_OUTPUT_TOKENS_CAP,
    MT_OUTPUT_TOKENS_FLOOR,
)

# Logger import
try:
    from core.logger import logger
except ImportError:
    # Fallback: logger import edilemezse sessizce yutan no-op logger
    class _NoopLogger:
        def _noop(self, *args, **kwargs):
            pass
        translator = glossary = memory = parser = pdf = epub = ui = model = pipeline = qa = _noop
        cache_hit = cache_miss = step = _noop
    logger = _NoopLogger()

# Standart günlük (2026-09-28): pipeline.log'a (translate_book'un dosya
# işleyicisi) düşmesi gereken rapor satırları için; renkli terminal çıktısı
# yukarıdaki core.logger'dadır.
_report_log = logging.getLogger(__name__)

# Checkpoint dizini (EPUB/TXT için)
_project_root = str(Path(__file__).parent.parent)
CHECKPOINT_DIR = Path(_project_root) / "checkpoints"

# CORE-03: Prompt-leak temizligi bir MT ciktisinin bu orandan AZINI
# birakiyorsa (yani yarisindan fazlasi silindiyse), leak pattern'i mesru
# Turkce icerigi yanlislikla yakalamis kabul edilir ve temizlik geri alinir.
_PROMPT_LEAK_KEEP_RATIO = 0.5

# F21: glossary on-filtresi icin metin sadelestirme tablosu.
# re.IGNORECASE Turkce i-ailesini ('ı', 'İ') ASCII 'i' ile ESLESTIRIR,
# duz casefold() eslestirmez ('İ'.casefold() = 'i' + U+0307). On-filtre bu
# yuzden i-ailesini tek harfe indirger.
_SCAN_KEY_TABLE = str.maketrans({"ı": "i", "̇": ""})


def _glossary_scan_key(text: str) -> str:
    """Terim on-filtresi anahtari — yalnizca 'alt-dize iceriyor mu' testi icin.

    Sonuc kume GENISTIR (yanlis-pozitif olabilir, yanlis-negatif olamaz):
    gercek eslesme kararini her zaman derlenmis \\b deseni verir.
    """
    return text.casefold().translate(_SCAN_KEY_TABLE)


@lru_cache(maxsize=8192)
def _glossary_term_pattern(term_en: str):
    """term_en icin derlenmis \\b desenini dondur (surec omurlu cache).

    F21 (2026-07-29): desen SEGMENT dongusunun icinde derleniyordu;
    bilimsel kitapta terim sayisi (844+) re modulunun ic cache'ini (512
    kalip) astigi icin her segmentte GERCEK derleme yapiliyordu. Desen
    yalnizca term_en'e bagli (tr/keep_original her cagride taze okunur),
    bu yuzden cache bayatlayamaz ve modul seviyesinde tutulabilir.
    """
    return re.compile(r'\b' + re.escape(term_en) + r'\b', re.IGNORECASE)


# CORE-B4: HY-MT dil isimleri (ISO 639-1 -> Ingilizce ad). Eskiden bu sozluk
# 3 ayri metod govdesinde (_translate_with_hymt / _translate_batch_hymt /
# _translate_batch_mlx) birebir kopyalanmisti -> tutarsizlik riski. Tek
# module-level sabite indirildi. NOT: prompt FORMATI (Transformers vs MLX)
# degismedi -- yalnizca isim tablosu tek kaynaga baglandi.
_HYMT_LANG_NAMES = {
    "en": "English", "tr": "Turkish", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "ar": "Arabic", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
}

# BATCH-04: max_tokens olcekleme ust siniri (bellek/sure guvenligi). Cok uzun
# girdilerde bile uretim bu token sinirini asmaz; OOM olursa batch bolunur.
# 2026-09-28 (KARARLAR 15): değer config/defaults.py'de (tek kaynak); ad geriye
# uyum için kalır. Hesabın kendisi core.mt_engines.backends.output_token_limit.
_MAX_TOKENS_CAP = MT_OUTPUT_TOKENS_CAP

# 2026-09-24 (parçalama kök nedeni): yer tutuculu segmentin talimatına eklenen
# cümle, yer tutucu tespiti ve cümlenin olası yankısını silen kalıp. Bkz.
# BookTranslator._build_translation_instruction / _strip_prompt_leaks.
# Örnek token gerçek id uzayında DEĞİL ("<<LATEX_N>>"; denetim 24 Eyl, Mercek 2
# #1): eski "<<LATEX_12>>" örneğini model yankılarsa restore, gerçek 12 numaralı
# formülü bu satıra taşıyıp asıl satırından siliyordu. "<<LATEX_N>>" yankısı hiçbir
# stash öğesine eşlenmez; latex_pipeline bekçisi (_placeholder_mismatch) onu
# fazlalık sayıp segmenti orijinaline döndürür ve satır parçalı yola düşer.
_PLACEHOLDER_CLAUSE = "Keep placeholder tokens like <<LATEX_N>> exactly as they are. "
_PLACEHOLDER_TOKEN_RE = re.compile(r"<<[A-Z]+_\d+>>")
# _PLACEHOLDER_CLAUSE_LEAK_RE (yankı kalıbı) latex_pipeline/patterns.py'den
# içe aktarılır (dosya başı): Yol B çıktı temizliği de aynı kalıbı kullanır.


# CORE-B24: Similarity fonksiyonu -- rapidfuzz varsa (C, çok daha hızlı) onu,
# yoksa stdlib SequenceMatcher'ı kullan. Module-level tanım: her çağrıda
# import yeniden yapılmasın diye.
try:
    from rapidfuzz.fuzz import ratio as _fuzz_ratio  # type: ignore

    def _similarity(a: str, b: str) -> float:
        return _fuzz_ratio(a, b) / 100.0
except ImportError:
    from difflib import SequenceMatcher as _SequenceMatcher  # noqa: N813

    def _similarity(a: str, b: str) -> float:
        return _SequenceMatcher(None, a, b).ratio()


def get_file_hash(file_path: str) -> str:
    """Dosya kimlik hash'i (checkpoint eşleştirme için).

    B-10 (2026-07-27): eskiden yalnız İLK 1MB okunuyordu — büyük EPUB/TXT'te
    1MB sonrası değişiklik bayat checkpoint'i geçerli sanıyordu. Artık
    ilk 1MB + son 1MB + dosya boyutu birlikte hash'lenir (yine hızlı,
    ama kuyruk değişiklikleri de kimliği değiştirir).
    """
    hash_md5 = hashlib.md5()
    size = os.path.getsize(file_path)
    hash_md5.update(str(size).encode())
    with open(file_path, "rb") as f:
        hash_md5.update(f.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            f.seek(-1024 * 1024, 2)
            hash_md5.update(f.read(1024 * 1024))
    return hash_md5.hexdigest()[:16]


def _default_mt_engine() -> str:
    """Varsayılan MT motoru — tek kaynak ``core.mt_engines.registry.DEFAULT_ENGINE``.

    Fonksiyon içi import: değer çağrı anında okunur (sabit değişirse ya da
    testte monkeypatch edilirse izlenir) ve core.translator modül düzeyinde
    core.mt_engines'e bağlanmaz.
    """
    from core.mt_engines import registry
    return registry.DEFAULT_ENGINE


# Çeviri belleği havuzu ve kaynak dil (K5, 2026-09-28; KARARLAR 14): İngilizce
# dışı kaynakta havuz adına "#<dil>" eklenir ("kitap#de@ollama:qwen3:8b").
# 2026-09-29 (bug avı F3-15/F4-12, K5-5): anahtar TEK yardımcıda
# (core.memory.memory_pool_key; proje adındaki ayraçlar kaçışlanır).
# memory_pool_project ve MEMORY_LANG_SEP geriye uyum için buradan da alınabilir.


def _source_is_english(config) -> bool:
    """Çeviri yapılandırmasının kaynak dili İngilizce mi? (EN->TR sözlükleri için)"""
    return (getattr(config, "source_lang", "en") or "en") == "en"


def _complete_lang_names(engine) -> None:
    """Motorun kendi dil adı tablosunda olmayan dillere varsayılan adı ekle (K5).

    ``MTEngine.lang_name`` tabloda olmayan kodda "English" döndürür; kullanıcı
    kaydı (config/mt_engines.json) yalnız ``{"en", "tr"}`` verdiyse Almanca
    kitabın talimatı "Translate from English" olurdu. Kullanıcı değerleri
    önceliklidir; tabloyu vermeyen motor (yerleşik HY-MT) zaten varsayılanı
    kullanır, dokunulmaz. İngilizce/Türkçe adları değişmez.
    """
    spec = getattr(engine, "spec", None)
    table = getattr(spec, "languages", None)
    if not isinstance(table, dict) or not table:
        return
    from core.mt_engines.base import DEFAULT_LANG_NAMES

    if all(code in table for code in DEFAULT_LANG_NAMES):
        return
    spec.languages = {**DEFAULT_LANG_NAMES, **table}


def _active_backend(translator):
    """Çevirmenin sunucu / llama.cpp arka ucu; MLX-transformers yolunda None.

    2026-09-25 ("Yerel model ile çevir" dört kaynak): arka uç nesnesi yalnız
    ``_init_mt_model`` ile ``self._backend``'e konur. Örnek sözlüğünden
    (``vars``) okunur: testler bu metotları MagicMock ``self`` ile de çağırıyor
    ve MagicMock'un kendiliğinden ürettiği ``_backend`` özniteliği arka uç
    sanılırsa bugünkü MLX yolu yanlışlıkla atlanırdı.
    """
    try:
        return vars(translator).get("_backend")
    except TypeError:
        return None


@dataclass
class TranslationConfig:
    """Çeviri yapılandırması"""
    # Temel ayarlar
    source_lang: str = "en"
    target_lang: str = "tr"

    # Çeviri motoru: core/mt_engines registry anahtarı; varsayılanı
    # registry.DEFAULT_ENGINE (denetim 2026-09-24 M3#4). Kullanıcı motorları
    # config/mt_engines.json ile eklenir (2026-07-27).
    mt_engine: str = field(default_factory=_default_mt_engine)

    # Model isimleri
    hymt_7b_model: str = "tencent/HY-MT1.5-7B"  # ~14GB, WMT25 sampiyonu, en iyi kalite

    # (KARAR 35, 2026-10-05: dış AI API düzeltme yolu — OpenAI / Anthropic /
    # Gemini istemcileri ve use_ai_review / ai_* alanları — silindi. Çeviri
    # yalnız yerel modelle; yerel sunucunun "OpenAI uyumlu" HTTP protokolü
    # core/mt_engines'de durur.)

    # Terminoloji - YENİ STRATEJİ (WMT25 DuTerm yaklaşımı)
    # Modlar: "off" = kapalı, "soft" = hint olarak ver, "hard" = placeholder ile zorla, "post" = sadece sonunda düzelt
    glossary_mode: str = "post"  # Varsayılan: post (çeviri bitince kalan İngilizce terimler düzeltilir)
    glossary_dir: str = None
    max_terms_per_segment: int = 5  # Segment başına max terim (fazlası = glossary devre dışı)
    use_curated_glossary: bool = True  # True = curated.json, False = technical.json
    book_type: str = "general"  # Kitap türü: general, novel, technical, psychology, science

    # Çeviri belleği
    use_memory: bool = True
    memory_db_path: str = None
    memory_threshold: int = 90  # Benzerlik eşiği

    # Batch processing - hız optimizasyonu (config.defaults merkezi)
    batch_size: int = DEFAULT_BATCH_SIZE_HYMT_LOCAL  # Aynı anda kaç segment çevrilecek

    # QA ve Tutarlılık
    run_qa: bool = True
    check_consistency: bool = True
    auto_fix_consistency: bool = True

    # Retry mekanizması - ACIK (sadece kritik hatalar icin)
    # Sadece sayi eksik, birim bozuk, [?] olan segmentler yeniden cevrilir
    retry_low_score: bool = True
    retry_threshold: float = 50.0  # Daha dusuk esik - sadece ciddi sorunlar
    max_retries: int = 2

    # Proje
    project_name: str = "default"


class BookTranslator:
    """
    Profesyonel Kitap Çevirici

    Özellikler:
    - HY-MT 7B (WMT25 sampiyonu) temel çeviri
    - Terminoloji yönetimi
    - Çeviri belleği
    - Kalite kontrol (QA)
    - Tutarlılık kontrolü
    - Düşük skorlu segment yeniden çevirisi
    - Çoklu format desteği
    """

    # BATCH-03: Tek GPU zaten paralel uretemez. MT uretim cagrilarini
    # (MLX generate/batch_generate + transformers generate) class-level tek
    # kilitle serilestir. Bir uretim cagrisi timeout ile disaridan terk
    # edilse bile (zombi thread) altta generate calismaya devam eder; yeni
    # bir cagri bu kilidi bekleyerek AYNI ANDA ikinci generate baslatmaz
    # (GPU state bozulmasi / crash onlenir). Kilit class seviyesinde -> tum
    # instance'lar tek GPU icin ayni kilidi paylasir. Kilit DAR tutulur
    # (sadece uretim satiri sarilir) -> restore/koruma asamalari kilitsiz,
    # deadlock ve gereksiz serilestirme yok.
    _mt_generate_lock = threading.Lock()

    # Kilit acquire ust siniri (saniye). 2026-07-11: Batch MT timeout'unda
    # terk edilen zombi thread kilidi tutmaya devam ediyor; fallback yolu
    # kilitte SURESIZ bekleyince batch timeout guard'i fiilen islevsizdi
    # (asili generate = tum pipeline asili). Sinir asilirsa RuntimeError
    # firlatilir -> _translate_with_mt yakalar -> segment [MT_FAIL] olur,
    # pipeline yasamaya devam eder.
    _GENERATE_LOCK_TIMEOUT_S = GENERATE_LOCK_TIMEOUT_S

    # DEVRE KESICI (F20, 2026-07-29): kilit ard arda bu kadar kez timeout
    # ederse zombi generate KALICI kabul edilir ve sonraki cagrilar kilidi
    # HIC beklemeden aninda duser. Oncesinde her segment bastan 300 sn
    # bekliyordu: 5000 segmentlik kitap ~416 saat "calisiyor gorunen" olu
    # kosuya kilitleniyordu (fallback zincirleri metin basina tekrar
    # bekledigi icin batch timeout guard'i da islevsizdi).
    # Deger DEFAULT_RETRY_COUNT (config/defaults.py) ile ayni mantikta:
    # ucuncu ustuste basarisizlik "gecici degil" demektir.
    _GENERATE_LOCK_MAX_TIMEOUTS = DEFAULT_RETRY_COUNT

    # Devre kesici durumu class seviyesinde — kilidin kendisi de class
    # seviyesinde (tek GPU), yani ariza tum instance'lari ilgilendirir.
    _generate_circuit_open = False
    _generate_lock_timeouts = 0
    _generate_circuit_lock = threading.Lock()

    @classmethod
    def reset_generate_circuit(cls):
        """Devre kesiciyi sifirla (yeni kosu / test kurulumu)."""
        with BookTranslator._generate_circuit_lock:
            BookTranslator._generate_circuit_open = False
            BookTranslator._generate_lock_timeouts = 0

    @contextmanager
    def _generate_lock(self):
        """_mt_generate_lock'i sureli al; alinamazsa RuntimeError.

        Devre kesici acikken kilit HIC beklenmez, dogrudan RuntimeError
        firlatilir -> _translate_with_mt yakalar -> segment MT_FAIL olur ->
        F3 kalite kapisi (fail-ratio) kosuyu durdurur.

        Mesaj bilerek 'out of memory/oom/cuda/mps' icermez —
        _translate_batch_hymt'nin OOM-bolme dali yanlislikla tetiklenmesin.
        """
        if BookTranslator._generate_circuit_open:
            raise RuntimeError(
                "MT generate devre kesici ACIK: kilit ard arda "
                f"{BookTranslator._GENERATE_LOCK_MAX_TIMEOUTS} kez "
                "alinamadi (asili/zombi generate thread'i); segment "
                "beklemeden MT_FAIL'e dusuruluyor"
            )

        acquired = self._mt_generate_lock.acquire(
            timeout=self._GENERATE_LOCK_TIMEOUT_S
        )
        if not acquired:
            with BookTranslator._generate_circuit_lock:
                BookTranslator._generate_lock_timeouts += 1
                n = BookTranslator._generate_lock_timeouts
                if n >= BookTranslator._GENERATE_LOCK_MAX_TIMEOUTS:
                    BookTranslator._generate_circuit_open = True
            if BookTranslator._generate_circuit_open:
                logger.translator(
                    f"[MT DEVRE KESICI] Kilit {n} kez ust uste alinamadi; "
                    "kalan segmentler beklemeden MT_FAIL sayilacak "
                    "(model/GPU asili). Kosu kalite kapisinda durdurulacak.",
                    "ERROR")
            raise RuntimeError(
                f"MT generate kilidi {self._GENERATE_LOCK_TIMEOUT_S}s icinde "
                "alinamadi (muhtemel asili/zombi generate thread'i); "
                "segment MT_FAIL'e dusuruluyor"
            )
        # Basarili acquire -> ardisik timeout sayaci sifirlanir (gecici
        # yogunluk devre kesiciyi tetiklemesin).
        if BookTranslator._generate_lock_timeouts:
            with BookTranslator._generate_circuit_lock:
                BookTranslator._generate_lock_timeouts = 0
        try:
            yield
        finally:
            self._mt_generate_lock.release()

    # HY-MT dil kodları (ISO 639-1)
    HYMT_LANG_CODES = {
        "en": "en",
        "tr": "tr",
        "de": "de",
        "fr": "fr",
        "es": "es",
        "it": "it",
        "pt": "pt",
        "ru": "ru",
        "ar": "ar",
        "zh": "zh",
        "ja": "ja",
        "ko": "ko",
    }

    @property
    def engine(self):
        """Aktif MT motoru (core/mt_engines katmanı) — lazy çözülür.

        Model-özel atomlar (prompt biçimi, özel token temizliği, model yolu,
        dil adları) bu nesneden gelir; üretim mekaniği (kilit, OOM bölme,
        timeout) BookTranslator'da kalır. Lazy olmasının sebebi: testler
        __new__ ile iskelet kurup _engine set etmez; ayrıca bilinmeyen motor
        adı ancak gerçekten kullanılınca hata versin.
        """
        eng = getattr(self, "_engine", None)
        if eng is None:
            from core.mt_engines import create_engine
            name = None
            if getattr(self, "config", None) is not None:
                name = getattr(self.config, "mt_engine", None)
            name = name or _default_mt_engine()
            eng = create_engine(name)
            _complete_lang_names(eng)
            self._engine = eng
        return eng


    def __init__(self, config: TranslationConfig = None):
        self.config = config or TranslationConfig()

        # Bileşenler
        self.mt_model = None
        self.mt_tokenizer = None
        # Sunucu / llama.cpp arka ucu (2026-09-25); MLX-transformers yolunda None
        self._backend = None
        self.glossary = None
        self.memory = None
        self.splitter = SentenceSplitter()
        self.qa_checker = None
        self.consistency_analyzer = None
        self.naturalizer = TurkishNaturalizer(aggressive=False)
        # 2026-09-29 (F3-04): büyük harf dizisi sezgisi kaynak dile bağlı;
        # adayları MT'den saklanmaz (bkz. NERProtector.HIDE_UNKNOWN_DEFAULT).
        self.ner_protector = NERProtector(
            source_lang=getattr(self.config, "source_lang", "en") or "en")

        # Lazy-init cache'ler
        self._dict_loader = None  # DictionaryLoader her çağrıda yeniden oluşturulmasın

        # Durum
        self.is_initialized = False
        self.stats = {
            "segments_translated": 0,
            "segments_from_memory": 0,
            "terms_enforced": 0,
            "retried_segments": 0,
            "consistency_fixes": 0,
            "glossary_terms_loaded": 0,
            "memory_entries_loaded": 0,
            # 2026-09-24: kaynak dili İngilizce olmayan (Latince alıntı vb.)
            # segmentler MT'ye gönderilmeden geçilir; bkz. _foreign_source_segment.
            "segments_foreign_skipped": 0,
            # 2026-09-24: MT'nin bozduğu [[UNIT_n]]/[[FORMULA_n]] gövdesi onarıldı.
            "placeholders_repaired": 0,
            # 2026-09-25: arka uç çıktısı üretim sınırında kesildi (finish_reason
            # "length"); kabul edilir ama sayılır ve günlüğe yazılır (TASARIM G).
            # 2026-09-28 (KARARLAR 15): üretilen token sınırın
            # MT_OUTPUT_NEAR_LIMIT_RATIO oranına varan çıktı da (MLX dahil).
            "segments_truncated": 0,
            # 2026-09-28 (KARARLAR 16) düşünme ölçümü — yalnız gerçek sayım:
            # düşünme görülen parça; akış parçası / sunucu sayacıyla sayılan
            # düşünme token'ı; sayaç yokken karakterden tahmin edilen token;
            # bütçeyi aşan deneme (akışta hemen kesilir); sert kapatmalı yeniden deneme; düşünme
            # sınırı yüzünden çevrilmeyen parça.
            "thinking_segments": 0,
            "thinking_tokens": 0,
            "thinking_tokens_estimated": 0,
            "think_budget_cuts": 0,
            "think_retries": 0,
            "think_failed": 0,
            # FX3 (2026-10-05, V2 Y1): üst katmanın bekçisi reddettiği için
            # bellekten (bekleyen ya da kayıtlı) silinen çeviri.
            "memory_rejected": 0,
        }

        # FX3 (2026-10-05, V2 Y1): onaylı bellek yazımı. Ertelenmiş kipte
        # (set_memory_deferred) çeviri belleğe hemen yazılmaz, kaynak metne
        # göre bekler; üst katman bekçilerden geçirince memory_commit ile
        # yazar, reddedince memory_reject ile siler. memory_bypass içinde
        # bellek OKUNMAZ (kurtarma denemesi modele gider).
        self._memory_deferred = False
        self._memory_pending: Dict[str, Tuple[str, Optional[str]]] = {}
        self._memory_bypass_on = False
        # FX3 (2026-10-05, V2 Y3): düşünme payı dolup CEVAP GELMEYEN istekler
        # (backends.is_no_answer). Üst katman bu kaynağın kurtarma zincirini
        # (aynı metnin türevleri: sade biçim, cümle düzeyi) atlar; parça özgün
        # dilde kalır ve işaretlenir (KARAR 22: bir yeniden deneme bekçide).
        self._no_answer_sent: set = set()
        self._no_answer_sources: set = set()
        self._no_answer_events = 0

        self._initialize()

    def _initialize(self):
        """Bileşenleri başlat"""
        logger.translator("Kitap Çevirici Pro başlatılıyor...", "STEP")

        # F20: yeni çevirici = yeni koşu -> devre kesici sıfırlanır. Aksi
        # hâlde uzun ömürlü süreçte (Streamlit) bir kez açılan devre sonraki
        # tüm çevirileri kalıcı olarak MT_FAIL'e düşürürdü.
        BookTranslator.reset_generate_circuit()

        # 1. MT modeli (HY-MT 7B)
        self._init_mt_model()

        # 2. Terminoloji (yeni strateji: soft/hard/off). Sözlükler EN->TR:
        # İngilizce dışı kaynakta (K5, 2026-09-28) yüklenmez — kaynak metin
        # İngilizce anahtarlarla ancak ortak yazılışlı (çoğu yanıltıcı)
        # sözcükte eşleşir (de "Gift"=zehir, "Art"=tür).
        if self.config.glossary_mode != "off":
            if _source_is_english(self.config):
                self._init_glossary()
            else:
                logger.translator(
                    "Terminoloji sözlüğü (İngilizce->Türkçe) kaynak dil "
                    f"'{self.config.source_lang}' olduğu için kapalı", "INFO")

        # 3. Çeviri belleği
        if self.config.use_memory:
            self._init_memory()

        # 4. QA
        if self.config.run_qa:
            self.qa_checker = QAChecker(self.glossary)

        # 5. Tutarlılık kontrolü
        if self.config.check_consistency:
            self.consistency_analyzer = DocumentConsistencyAnalyzer(self.glossary)

        self.is_initialized = True
        logger.translator("Sistem hazır!", "SUCCESS")

    # ==================== CHECKPOINT SİSTEMİ ====================

    def _get_checkpoint_path(self, file_hash: str, file_type: str) -> Path:
        """Checkpoint dosya yolunu döndür"""
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        return CHECKPOINT_DIR / f"{file_type}_{file_hash}.json"

    def _save_text_checkpoint(self, file_path: str, file_type: str,
                               translated_segments: List[Dict],
                               total_segments: int,
                               metadata: Dict = None) -> None:
        """EPUB/TXT için checkpoint kaydet"""
        file_hash = get_file_hash(file_path)
        checkpoint_path = self._get_checkpoint_path(file_hash, file_type)

        data = {
            "file_hash": file_hash,
            "file_type": file_type,
            "input_path": file_path,
            "total_segments": total_segments,
            "completed_segments": len(translated_segments),
            "translated_segments": translated_segments,
            "created_at": None,
            "updated_at": datetime.now().isoformat(),
            "metadata": metadata or {}
        }

        # created_at: süreç boyunca bellekte tutulur (F50, 2026-07-29).
        # Eskiden HER kayıtta (her 5 batch) büyüyen dosyanın TAMAMI yalnızca
        # bu tek alan için json.load ediliyordu -> yazma+okuma O(n^2).
        created_cache = getattr(self, "_ckpt_created_at", None)
        if not isinstance(created_cache, dict):
            created_cache = {}
            self._ckpt_created_at = created_cache
        cache_key = str(checkpoint_path)
        created_at = created_cache.get(cache_key)
        if created_at is None:
            if checkpoint_path.exists():
                # Yalnızca süreçteki İLK kayıtta okunur (resume senaryosu)
                try:
                    with open(checkpoint_path, 'r', encoding='utf-8') as f:
                        created_at = json.load(f).get("created_at")
                except (IOError, json.JSONDecodeError, KeyError) as e:
                    logger.translator(f"[CHECKPOINT] Mevcut checkpoint okunamadı: {e}", "WARNING")
                    created_at = None
            if not created_at:
                created_at = datetime.now().isoformat()
            created_cache[cache_key] = created_at
        data["created_at"] = created_at

        # CORE-B22: Atomic write - temp dosyaya yaz, sonra rename (kesintide yarim dosya birakma)
        import tempfile
        temp_path = checkpoint_path.parent / f".{checkpoint_path.name}.tmp"
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                # F50: kompakt dump — indent=2 dosyayı ~2 katına çıkarıyordu.
                # Okuma tarafı json.load olduğu için ESKİ (indent'li)
                # checkpoint dosyaları aynen okunmaya devam eder.
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            temp_path.replace(checkpoint_path)
        except Exception:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

        logger.translator(f"[CHECKPOINT] Kaydedildi: {len(translated_segments)}/{total_segments} segment ({file_type})", "DEBUG")

    def _load_text_checkpoint(self, file_path: str, file_type: str) -> Optional[Dict]:
        """EPUB/TXT için checkpoint yükle"""
        try:
            file_hash = get_file_hash(file_path)
        except FileNotFoundError:
            return None

        checkpoint_path = self._get_checkpoint_path(file_hash, file_type)

        if not checkpoint_path.exists():
            return None

        try:
            with open(checkpoint_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Dosya değişmiş mi kontrol et
            if data.get("file_hash") != file_hash:
                logger.translator(f"[CHECKPOINT] Dosya değişmiş, checkpoint geçersiz", "WARNING")
                return None

            logger.translator(f"[CHECKPOINT] Yüklendi: {data['completed_segments']}/{data['total_segments']} segment ({file_type})", "INFO")
            return data
        except Exception as e:
            logger.translator(f"[CHECKPOINT] Yükleme hatası: {e}", "ERROR")
            return None

    def _delete_text_checkpoint(self, file_path: str, file_type: str) -> None:
        """Başarılı çeviri sonrası checkpoint sil"""
        try:
            file_hash = get_file_hash(file_path)
            checkpoint_path = self._get_checkpoint_path(file_hash, file_type)
            # F50: created_at cache'i de düşsün — silinen checkpoint yeniden
            # oluşturulursa eski koşunun tarihi taşınmasın.
            created_cache = getattr(self, "_ckpt_created_at", None)
            if isinstance(created_cache, dict):
                created_cache.pop(str(checkpoint_path), None)
            if checkpoint_path.exists():
                checkpoint_path.unlink()
                logger.translator(f"[CHECKPOINT] Temizlendi ({file_type})", "DEBUG")
        except Exception as e:
            logger.translator(f"[CHECKPOINT] Silme hatası: {e}", "WARNING")

    def _init_mt_model(self):
        """MT modelini yükle (HY-MT 7B)"""
        # Motor adı registry'den çözülür (bilinmeyen ad -> ValueError,
        # mesajda kullanılabilir motor listesi var). Eski sabit
        # "sadece hymt-7b" kapısının yerine geçti (2026-07-27).
        self._engine = None  # config.mt_engine değişmiş olabilir; taze çöz
        engine = self.engine  # ValueError("Desteklenmeyen MT motoru ...") olabilir

        # 2026-09-25: sunucu (Ollama / OpenAI uyumlu) ya da llama.cpp arka ucu.
        # MLX motorlarında create_backend None döner ve aşağıdaki BUGÜNKÜ yol
        # aynen yürür. BackendUnavailable (sunucu kapalı, model yok, modül
        # kurulu değil...) RuntimeError'a SARILMADAN yukarı çıkar: translate_book
        # isinstance ile tanıyıp EXIT_MODEL_UNAVAILABLE verir.
        self._backend = None
        from core.mt_engines.backends import create_backend
        backend = create_backend(engine.spec)
        if backend is not None:
            backend.load()
            self._backend = backend
            self._use_mlx = False
            self.mt_model = None
            self.mt_tokenizer = None
            BookTranslator._validate_langs(self)
            logger.translator(f"Backend: {backend.describe()}", "INFO")
            return

        # hymt-7b için config.hymt_7b_model geriye dönük override olarak
        # korunur; diğer motorlar model kimliğini spec'ten alır.
        if self.config.mt_engine == "hymt-7b":
            model_id = self.config.hymt_7b_model
        else:
            model_id = engine.spec.model_id or engine.spec.name
        try:
            self._init_hymt_model(model_id)
        except Exception as e:
            error_msg = f"MT modeli yüklenemedi: {e}"
            logger.translator(error_msg, "ERROR")
            # Mesajda hymt-7b için "HY-MT 7B" adı korunur (test/log sözleşmesi)
            engine_label = "HY-MT 7B" if self.config.mt_engine == "hymt-7b" else self.config.mt_engine
            raise RuntimeError(f"{engine_label} modeli yüklenemedi: {e}")

    def _engine_log_label(self) -> str:
        """Günlükte motorun adı ("HY-MT1.5 7B (MLX)", "Qwen2.5-7B-Instruct-MLX (MLX)").

        2026-09-29 (F4-09): her MLX modeli günlükte "HY-MT" diye anılıyordu.
        Ad keşif/arayüzle aynı yardımcıdan (discovery.engine_label) gelir.
        """
        spec = getattr(self.engine, "spec", None)
        try:
            from core.mt_engines.discovery import engine_label
            return engine_label(spec)
        except Exception:   # etiket günlüğü düşürmesin
            return getattr(spec, "name", None) or str(self.config.mt_engine)

    def _init_hymt_model(self, model_name: str):
        """MLX / Transformers modelini yükle (motor spec'inden; varsayılan HY-MT1.5 7B)."""
        import platform

        # F4-09: ileti motorun kendi adıyla; "WMT25 şampiyonu" gibi tanıtım
        # yalnız yerleşik kaydın açıklamasından (bugün yalnız hymt-7b).
        label = self._engine_log_label()
        spec = getattr(self.engine, "spec", None)
        logger.translator(f"MT modeli yükleniyor: {label} [{model_name}]", "STEP")
        if getattr(spec, "source", None) == "builtin" and getattr(spec, "description", None):
            logger.translator(f"({spec.description})", "INFO")

        # Mac'te MLX kullan (Apple Silicon optimize)
        if platform.system() == "Darwin":
            self._init_hymt_mlx(model_name)
        else:
            self._init_hymt_transformers(model_name)

        # Dil kodlarını ayarla ve validate et (arka uç yoluyla ortak yardımcı)
        BookTranslator._validate_langs(self)

        backend = "MLX (Neural Engine + GPU)" if getattr(self, '_use_mlx', False) else "Transformers (MPS)"
        logger.translator(f"{label} hazır ({self._hymt_src_lang} → {self._hymt_tgt_lang})", "SUCCESS")
        logger.translator(f"Backend: {backend}", "INFO")

    def _validate_langs(self):
        """Dil kodlarını doğrula ve ``_hymt_src_lang`` / ``_hymt_tgt_lang``'ı kur.

        MLX/transformers yükleyicisi (``_init_hymt_model``) ve sunucu arka ucu
        aynı kuralı kullanır. Hedef dil yalnız Türkçe; yerel yolun kaynak dili
        core.lang_detect.TEX_SOURCE_LANGS'ten biri (KARARLAR 14; dil kapısı
        translate_book'ta, burada değişmez).
        """
        if self.config.source_lang not in self.HYMT_LANG_CODES:
            logger.translator(f"Bilinmeyen kaynak dil: {self.config.source_lang}, 'en' kullanılıyor", "WARNING")
        if self.config.target_lang not in self.HYMT_LANG_CODES:
            logger.translator(f"Bilinmeyen hedef dil: {self.config.target_lang}, 'tr' kullanılıyor", "WARNING")
        self._hymt_src_lang = self.HYMT_LANG_CODES.get(self.config.source_lang, "en")
        self._hymt_tgt_lang = self.HYMT_LANG_CODES.get(self.config.target_lang, "tr")

    def _init_hymt_mlx(self, model_name: str):
        """HY-MT'yi MLX ile yükle (Apple Silicon)"""
        from mlx_lm import load
        from pathlib import Path

        logger.translator("(Mac tespit edildi - MLX modu, Apple Silicon optimize)", "INFO")

        # Lokal MLX modeli var mı kontrol et — yol motor spec'inden gelir
        # (hymt-7b: models/hymt-7b-mlx; .nosync varyantı otomatik denenir)
        # Model kökü veri kökünden (TASARIM F1; paketli kipte <kök>/veri/models).
        from config.paths import model_koku
        mlx_path = model_koku(Path(__file__).parent.parent)
        local_path = self.engine.resolve_local_model_dir(mlx_path)

        if local_path is not None:
            logger.translator(f"Lokal MLX modeli kullanılıyor: {local_path}", "INFO")
            self.mt_model, self.mt_tokenizer = load(str(local_path))
        else:
            missing = mlx_path / (self.engine.spec.local_dir or f"{self.config.mt_engine}-mlx")
            logger.translator(f"MLX modeli bulunamadı: {missing}", "WARNING")
            logger.translator(f"Dönüştürmek için: {self.engine.convert_hint(mlx_path)}", "INFO")
            raise FileNotFoundError(f"MLX model not found: {missing}")

        self._use_mlx = True

    def _init_hymt_transformers(self, model_name: str):
        """HY-MT'yi Transformers ile yükle (Linux/Windows)"""
        from transformers import AutoModelForCausalLM, AutoTokenizer
        import torch

        self.mt_tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        self.mt_model = AutoModelForCausalLM.from_pretrained(
            model_name,
            torch_dtype=torch.bfloat16,
            device_map="auto",
            trust_remote_code=True
        )
        self._use_mlx = False


    def _init_glossary(self):
        """Terminoloji yöneticisini başlat - YENİ STRATEJİ (WMT25 DuTerm)"""
        try:
            glossary_dir = self.config.glossary_dir
            if not glossary_dir:
                glossary_dir = str(Path(__file__).parent.parent / "glossary")

            # Path validation
            glossary_path = Path(glossary_dir)
            if not glossary_path.exists():
                logger.translator(f"Glossary dizini bulunamadı: {glossary_dir}, oluşturuluyor...", "WARNING")
                glossary_path.mkdir(parents=True, exist_ok=True)

            # Yeni GlossaryManager: mod, limit, curated, book_type
            self.glossary = GlossaryManager(
                glossary_dir=glossary_dir,
                mode=self.config.glossary_mode,
                max_terms_per_segment=self.config.max_terms_per_segment,
                use_curated=self.config.use_curated_glossary,
                book_type=self.config.book_type
            )

            mode_str = self.config.glossary_mode.upper()
            curated_str = "curated" if self.config.use_curated_glossary else "full"
            self.stats["glossary_terms_loaded"] = len(self.glossary.terms)
            logger.translator(f"Terminoloji hazır: {len(self.glossary.terms)} terim ({mode_str} mod, {curated_str})", "SUCCESS")

        except Exception as e:
            logger.translator(f"Terminoloji yüklenemedi: {e}", "ERROR")
            self.glossary = None

    def mine_terms_from_book(self, full_text: str, min_freq: int = 2) -> int:
        """
        Kitap metninden otomatik terim çıkar ve glossary'ye ekle

        Bu metod çeviri başlamadan önce çağrılmalı.
        Tüm kitap metni taranır, terimler bulunur ve korunmak üzere eklenir.

        Args:
            full_text: Kitabın tam metni
            min_freq: Minimum frekans (varsayılan 2)

        Returns:
            Eklenen terim sayısı
        """
        if not self.glossary:
            logger.translator("[Term Mining] Glossary yok, terim madenciliği atlandı", "WARNING")
            return 0

        # Glossary modunu geçici olarak "hard" yap, sonra eski haline getir
        old_mode = self.glossary.mode
        try:
            from core.term_miner import TermMiner

            logger.translator(f"[Term Mining] Kitap taranıyor ({len(full_text):,} karakter)...", "STEP")

            # Terimleri çıkar
            miner = TermMiner(min_freq=min_freq)
            terms = miner.extract_terms(full_text)

            logger.translator(f"[Term Mining] {len(terms)} aday terim bulundu", "INFO")

            # Glossary'ye ekle (keep_original=True - İngilizce kalsın)
            # Glossary mode'u hard yap ki placeholder koruması çalışsın
            self.glossary.mode = "hard"

            count = self.glossary.add_mined_terms(terms, keep_original=True)

            # İlk 20 terimi göster
            if terms:
                logger.translator(f"[Term Mining] En sık terimler:", "DEBUG")
                for t in terms[:20]:
                    logger.translator(f"    {t.term}: {t.frequency}x", "DEBUG")

            return count

        except ImportError as e:
            logger.translator(f"[Term Mining] term_miner modülü yüklenemedi: {e}", "ERROR")
            return 0
        except Exception as e:
            logger.translator(f"[Term Mining] Hata: {e}", "ERROR")
            return 0
        finally:
            self.glossary.mode = old_mode

    # ------------------------------------------------------------------
    # FX3 (2026-10-05, V2 Y1): onaylı (ertelenmiş) bellek yazımı
    # ------------------------------------------------------------------
    # Kök neden: bellek translate_text / translate_batch_protected içinde,
    # üst katmanın (latex_pipeline) sayı / iskelet / ilişki simgesi
    # bekçilerinden ÖNCE yazılıyordu. Yer tutucusuz satırda "1250"yi düşüren
    # ya da "\&"i "&" yapan çeviri belleğe giriyor, kurtarmanın "tekrar"
    # adımı modele gitmeden bellekten aynı bozuk çeviriyi alıyor, satır
    # özgün dilde kalıyordu; kayıt sonraki koşularda da kalıcıydı.

    def set_memory_deferred(self, enabled: bool = True) -> None:
        """Ertelenmiş bellek kipini aç/kapat. Açıkken çeviri belleğe yazılmaz,
        kaynak metne göre bekler (:meth:`memory_commit` / :meth:`memory_reject`).
        Kapatınca bekleyenler YAZILMADAN düşer (onaylanmamıştır)."""
        self._memory_deferred = bool(enabled)
        if not enabled:
            self._memory_pending = {}

    def _memory_hold(self, source: str, target: str,
                     context: Optional[str] = None) -> None:
        """Ertelenmiş kipte çeviriyi beklet (aynı kaynağın son çevirisi)."""
        pending = getattr(self, "_memory_pending", None)
        if pending is None:
            pending = self._memory_pending = {}
        pending[source] = (target, context)

    def memory_commit(self, source: str) -> bool:
        """Bekleyen çeviriyi (üst katman kabul etti) belleğe yaz. Bekleyen
        yoksa (bellekten geldi, yankı, kesik...) False."""
        pending = getattr(self, "_memory_pending", None) or {}
        item = pending.pop(source, None)
        if item is None:
            return False
        target, context = item
        memory = getattr(self, "memory", None)
        return bool(memory.add(source, target, context=context)) if memory else False

    def memory_reject(self, source: str) -> None:
        """Üst katman çeviriyi REDDETTİ: bekleyen düşer, bellekte bu kaynağa
        ait kayıt varsa silinir (eski koşudan kalmış bozuk kayıt dahil)."""
        pending = getattr(self, "_memory_pending", None) or {}
        dropped = pending.pop(source, None) is not None
        deleted = False
        memory = getattr(self, "memory", None)
        if memory:
            try:
                deleted = bool(memory.delete(source))
            except Exception as exc:  # bellek hatası çeviriyi durdurmaz
                logger.translator(f"Bellek kaydı silinemedi: {exc}", "WARNING")
        if dropped or deleted:
            stats = getattr(self, "stats", None)
            if isinstance(stats, dict):
                stats["memory_rejected"] = stats.get("memory_rejected", 0) + 1
            if deleted:
                logger.translator(
                    "Reddedilen çeviri bellekten silindi: "
                    f"\"{source[:50]}\"", "INFO")

    def memory_discard_pending(self) -> int:
        """Onaylanmamış bekleyenleri YAZMADAN düşür; düşen sayısı."""
        pending = getattr(self, "_memory_pending", None) or {}
        n = len(pending)
        pending.clear()
        return n

    @contextmanager
    def memory_bypass(self):
        """Bu blokta bellek OKUNMAZ (kurtarma denemesi modele gider); yazım
        kuralları (ertelenmiş kip dahil) aynen sürer. İç içe güvenli."""
        prev = getattr(self, "_memory_bypass_on", False) is True
        self._memory_bypass_on = True
        try:
            yield self
        finally:
            self._memory_bypass_on = prev

    # ------------------------------------------------------------------
    # FX3 (2026-10-05, V2 Y3): cevapsız istek bilgisi üst katmana
    # ------------------------------------------------------------------
    def _mark_no_answer_source(self, source: str) -> None:
        sources = getattr(self, "_no_answer_sources", None)
        if isinstance(sources, set):
            sources.add(source)

    def no_answer_for(self, source: str) -> bool:
        """*source* (translate_text / translate_batch_protected'a verilen
        metin) için model düşünme payı dolup cevap üretemedi mi?"""
        sources = getattr(self, "_no_answer_sources", None)
        return isinstance(sources, set) and source in sources

    def no_answer_count(self) -> int:
        """Koşu boyunca cevapsız kalan üretim isteği sayısı (olay sayacı)."""
        value = getattr(self, "_no_answer_events", 0)
        return value if isinstance(value, int) else 0

    def _init_memory(self):
        """Çeviri belleğini başlat"""
        try:
            db_path = self.config.memory_db_path
            if not db_path:
                # Veri kökünden (TASARIM F1; tek kaynak config.paths.bellek_yolu:
                # TranslationMemory() ve ui.runner.memory_db_path ile aynı yol).
                # Paketli kipte veri kökü henüz yok olabilir: üst klasörler de açılır.
                from config.paths import bellek_yolu
                default_db = bellek_yolu(Path(__file__).parent.parent)
                default_db.parent.mkdir(parents=True, exist_ok=True)
                db_path = str(default_db)

            # Path traversal koruması
            if ".." in db_path:
                raise ValueError(f"Güvenli olmayan path (.. içeriyor): {db_path}")

            # Write permission kontrolü
            db_dir = Path(db_path).parent
            if not os.access(str(db_dir), os.W_OK):
                raise PermissionError(f"Veritabanı dizinine yazma izni yok: {db_dir}")

            # KARARLAR 8 (2026-09-25): çeviri belleği model başına ayrı.
            # Varsayılan motor bugünkü proje anahtarını AYNEN kullanır; diğer
            # motorlar "<proje>@<motor kimliği>" havuzuna yazar/okur.
            # K5 (2026-09-28): İngilizce dışı kaynakta havuz adına kaynak dil
            # eklenir; İngilizcede anahtar AYNEN. 2026-09-29: tek yardımcı
            # memory_pool_key (ayraç kaçışı, F3-15/F4-12) ve kaynak dil
            # belleğe verilir: kaynak dilde kalmış yarım çeviri ne yazılır ne
            # okunur (F3-06).
            source_lang = getattr(self.config, "source_lang", "en") or "en"
            self.memory = TranslationMemory(
                db_path,
                memory_pool_key(self.config.project_name, source_lang,
                                self.config.mt_engine),
                source_lang=source_lang)
            stats = self.memory.get_stats()
            self.stats["memory_entries_loaded"] = stats['total_entries']
            logger.translator(f"Çeviri belleği hazır: {stats['total_entries']} kayıt", "SUCCESS")

        except Exception as e:
            logger.translator(f"Çeviri belleği başlatılamadı: {e}", "ERROR")

    # ==================== ANA ÇEVİRİ FONKSİYONLARI ====================

    def translate(self, text: str, source_lang: str = None, target_lang: str = None, context: str = None) -> str:
        """CORE-B20: translate_text için takma ad (ilk çağıranı core/book_translator_v2.py
        5 Ekim 2026'da ölü kod olarak silindi; arayüz korunuyor)."""
        return self.translate_text(text, context=context)

    def translate_text(self, text: str, context: str = None, is_heading: bool = False) -> str:
        """
        Tek bir metin parcasini cevir - 3 asamali pipeline

        Pipeline:
        1. ASAMA: Literal MT ceviri
        2. ASAMA: Terminoloji + NER
        3. ASAMA: Dogallastirma (kural tabanli)

        Args:
            text: Cevrilecek metin
            context: Baglam bilgisi (terminoloji secimi icin)
            is_heading: True ise MT talimatina baslik modu eklenir
                (kisa, cevirisi zorunlu, kaynagi echo etme). Geriye uyumlu.

        Returns:
            Cevrilmis metin
        """
        # Önce None/boş kontrolü
        if not text or not text.strip():
            if logger:
                logger.translator(f"Bos metin atlandi", "DEBUG")
            return text or ""

        text_preview = text[:50].replace('\n', ' ') + "..." if len(text) > 50 else text.replace('\n', ' ')

        # Cok kisa veya sadece sembol
        if len(text.strip()) < 3 or self._is_non_translatable(text):
            if logger:
                logger.translator(f"Cevrilmeyecek (kisa/sembol): \"{text_preview}\"", "DEBUG")
            return text

        # 2026-09-24: kaynak İngilizce değilse (Latince alıntı vb.) çevirme —
        # Yol A çevirmeni de bırakıyor. Ayrıntı: _foreign_source_segment.
        foreign = self._foreign_source_segment(text)
        if isinstance(foreign, str) and foreign:
            self.stats["segments_foreign_skipped"] = (
                self.stats.get("segments_foreign_skipped", 0) + 1
            )
            if logger:
                logger.translator(
                    f"Segment kitabın kaynak dilinde değil ({foreign}), "
                    f"çevrilmeden geçildi: "
                    f"\"{text_preview}\"", "INFO")
            return text

        original_text = text

        # 2026-09-24 (başlık kök nedeni, bir test koşusu): başlık modunda çeviri
        # belleği ve NER koruması DEVRE DIŞI.
        #  - NER: Title Case bir başlık ("Potential Energy", "Local
        #    Reference Frame") NERProtector'ın "ardışık büyük harfli kelime =
        #    özel isim" sezgisine birebir uyar. Başlığın tamamı
        #    @@UNKNOWN0000@@ olup MT'ye hiç ulaşmıyordu (echo -> çevrilmemiş
        #    başlık); kısmen eşleşince de "İki–Body Orbital Mechanics",
        #    "Relating Energy ve Yarı-Büyük Eksen" gibi yarım başlıklar
        #    çıkıyordu (kanıt: translation_memory.db kayıtları). Başlıktaki
        #    gerçek özel adları (Newton, Kepler) MT zaten korur.
        #  - Bellek: kısa başlıkta %90 benzerlik eşiği güvenilmez; ayrıca
        #    yanlış kaydedilmiş bir başlık çevirisi sonraki koşularda "exact"
        #    olarak geri döner ve kendini asla düzeltemez. Başlık sayısı az,
        #    MT maliyeti ihmal edilebilir.
        use_memory = bool(self.memory) and not is_heading

        # 0. Ceviri belleginden ara (tam veya benzer esleme)
        # FX3 (V2 Y1): memory_bypass içinde (kurtarma denemesi) okunmaz.
        if use_memory and getattr(self, "_memory_bypass_on", False) is not True:
            cached, source = self.memory.get_or_similar(
                text, self.config.memory_threshold
            )
            if cached and source in ("exact", "similar"):
                self.stats["segments_from_memory"] += 1
                if logger:
                    logger.cache_hit(text_preview)
                return cached
            else:
                if logger:
                    logger.cache_miss(text_preview)

        # ========== KORUMA ASAMASI ==========

        # 1. NER koruma (özel isimler) — başlıkta uygulanmaz (yukarıya bak)
        ner_mapping = {}
        if self.ner_protector and not is_heading:
            text, ner_mapping = self.ner_protector.protect(text)
            if logger and ner_mapping:
                logger.translator(f"{len(ner_mapping)} ozel isim korundu", "DEBUG")

        # 2. Terimleri koru (glossary) - YENİ STRATEJİ
        # "post" modunda çeviri sırasında koruma yapma, sonunda post-process yapılacak
        protected_terms = []
        if self.glossary and self.config.glossary_mode not in ("off", "post"):
            text, protected_terms = self.glossary.protect_terms(text)
            if logger and protected_terms:
                mode = self.config.glossary_mode
                logger.translator(f"{len(protected_terms)} terim bulundu ({mode} mod)", "DEBUG")

        # 3. LaTeX referanslarini koru (\ref, \cite, \label vb.)
        text, references = self._protect_references(text)
        if logger and references:
            logger.translator(f"{len(references)} LaTeX referansi korundu", "DEBUG")

        # 4. Formulleri koru
        text, formulas = self._protect_formulas(text)
        if logger and formulas:
            logger.translator(f"{len(formulas)} formul korundu", "DEBUG")

        # 5. Birimleri koru
        text, units = self._protect_units(text)
        if logger and units:
            logger.translator(f"{len(units)} birim korundu", "DEBUG")

        # 6. Sayfa ayraçlarını koru (TOC ' / 214' kalıbı)
        text, page_refs = self._protect_page_refs(text)
        if logger and page_refs:
            logger.translator(f"{len(page_refs)} sayfa ayracı korundu", "DEBUG")

        # ========== ASAMA 1: LITERAL MT CEVIRI ==========
        if logger:
            logger.translator(f"[PASS 1] MT ceviri basliyor: \"{text_preview}\"", "DEBUG")
        _na_before = getattr(self, "_no_answer_events", 0)
        translated = self._translate_with_mt(text, is_heading=is_heading)
        # FX3 (V2 Y3): MT sırasında cevapsız istek olduysa (cümle parçası
        # dahil) üst katmanın gönderdiği kaynak işaretlenir.
        if getattr(self, "_no_answer_events", 0) != _na_before:
            BookTranslator._mark_no_answer_source(self, original_text)

        # ========== ASAMA 2: TERMINOLOJI + NER ==========
        # Korunanlari geri koy (koruma sirasinin tersi)
        translated = self._restore_page_refs(translated, page_refs)
        translated = self._restore_units(translated, units)
        translated = self._restore_formulas(translated, formulas)
        translated = self._restore_references(translated, references)

        # Glossary restore - HARD ve HYBRID modda XML tag var
        if self.glossary and protected_terms:
            if self.config.glossary_mode in ("hard", "hybrid"):
                # Hard/Hybrid mod: XML tag'leri geri koy
                translated, not_found = self.glossary.restore_terms(translated, protected_terms, use_turkish=True)

                # Codex C çözümü: Fallback - tag kaybolmuşsa post-processing
                if not_found:
                    translated = self._post_process_missing_terms(translated, protected_terms, not_found, original_text)

                # Sadece hard mode olanları say
                hard_terms = [t for t in protected_terms if t.get("mode") == "hard"]
                self.stats["terms_enforced"] += len(hard_terms)
            # Soft mod: placeholder yok, terimler AI'a hint olarak gidecek

        if self.ner_protector and ner_mapping:
            translated = self.ner_protector.restore(translated, ner_mapping, use_translation=True)

        # ========== ASAMA 3: DOGALLASTIRMA ==========
        if self.naturalizer:
            # Kural tabanli dogallastirma (fallback)
            if logger:
                logger.translator("[PASS 3] Kural tabanli dogallastirma", "DEBUG")
            translated = self.naturalizer.naturalize(translated)

        # ========== SON ISLEMLER ==========

        # MT_FAIL kontrolu: ceviri basarisiz olduysa memory'e kaydetme (zehirli cache onleme)
        _mt_failed = translated.startswith("[MT_FAIL]")
        # F3-07 (2026-09-29): üretim sınırına dayanan (kesik olabilir) çeviri
        # kabul edilir ama belleğe YAZILMAZ; kayıt segmente bağlanır (rapor).
        _truncated = BookTranslator._take_truncation(
            self, text, original_text, translated, parts=True)

        # Bellege kaydet - sadece basarili ceviriler.
        # Echo guard (2026-07-11): kaynak==hedef ise MT cevirmemis demektir;
        # batch yollari bunu zaten reddediyordu, tekil yol acikti (DB'de 141
        # Ingilizce echo kaydinin kaynagi buydu).
        # F19 (2026-07-29): tam esitlik yetmiyordu — MT'nin noktayi dusurmesi
        # ya da ilk harfi kucultmesi guard'i atlatip zehri kalici cache'liyordu.
        # 2026-09-24: başlık çevirisi belleğe de yazılmaz (use_memory False).
        if (use_memory and not _mt_failed and not _truncated
                and not is_echo_output(original_text, translated)):
            # FX3 (V2 Y1): ertelenmiş kipte üst katmanın onayını bekler.
            if getattr(self, "_memory_deferred", False) is True:
                self._memory_hold(original_text, translated, context)
            else:
                self.memory.add(original_text, translated, context=context)

        # MT_FAIL prefix'ini temizle - Ingilizce metin korunur ama memory'e yazilmaz
        if _mt_failed:
            translated = translated[len("[MT_FAIL]"):]
            if logger:
                logger.translator(f"MT basarisiz, orijinal metin korunuyor (memory'e kaydedilmedi): \"{text_preview}\"", "WARNING")

        self.stats["segments_translated"] += 1

        if logger:
            tgt_preview = translated[:50].replace('\n', ' ') + "..." if len(translated) > 50 else translated.replace('\n', ' ')
            logger.translation_result(text_preview, tgt_preview, "MT")

        return translated

    def translate_batch_protected(self, texts: List[str]) -> List[str]:
        """
        translate_text'in koruma zincirini HER metne uygular, ama MT kismini
        _translate_batch_hymt ile TOPLU yapar (SOZLESME 2).

        Her text icin translate_text ile AYNI zincir:
          memory lookup -> NER protect -> glossary protect -> _protect_references
          -> _protect_formulas -> _protect_units -> _protect_page_refs
          -> (BATCH MT) -> restore (page_refs/units/formulas/references)
          -> glossary restore -> NER restore (use_translation=True)
          -> naturalize -> MT_FAIL kontrol -> memory.add

        Cikti uzunlugu girdiyle AYNIDIR (1:1 sozlesme).
        """
        if not texts:
            return []

        results: List[Optional[str]] = [None] * len(texts)
        mt_order: List[int] = []          # MT'ye giden orijinal index'ler (sirali)
        texts_for_mt: List[str] = []      # korunmus, MT'ye gidecek metinler
        payloads: List[tuple] = []        # (ner_mapping, protected_terms, references, formulas, units, original_text)

        for i, text in enumerate(texts):
            # translate_text ile ayni erken-donusler
            if not text or not text.strip():
                results[i] = text or ""
                continue
            if len(text.strip()) < 3 or self._is_non_translatable(text):
                results[i] = text
                continue

            # 2026-09-24: İngilizce olmayan kaynak segment (tekil yolla aynı kapı)
            foreign = self._foreign_source_segment(text)
            if isinstance(foreign, str) and foreign:
                self.stats["segments_foreign_skipped"] = (
                    self.stats.get("segments_foreign_skipped", 0) + 1
                )
                if logger:
                    logger.translator(
                        f"Segment kitabın kaynak dilinde değil ({foreign}), "
                        f"çevrilmeden geçildi: "
                        f"\"{text[:50]}\"", "INFO")
                results[i] = text
                continue

            original_text = text

            # 0. Memory (tekil yolla ayni esik; FX3: memory_bypass içinde okunmaz)
            if self.memory and getattr(self, "_memory_bypass_on", False) is not True:
                cached, source = self.memory.get_or_similar(text, self.config.memory_threshold)
                if cached and source in ("exact", "similar"):
                    self.stats["segments_from_memory"] += 1
                    results[i] = cached
                    continue

            # Koruma asamasi (translate_text sirasi)
            ner_mapping = {}
            if self.ner_protector:
                text, ner_mapping = self.ner_protector.protect(text)

            protected_terms = []
            if self.glossary and self.config.glossary_mode not in ("off", "post"):
                text, protected_terms = self.glossary.protect_terms(text)

            text, references = self._protect_references(text)
            text, formulas = self._protect_formulas(text)
            text, units = self._protect_units(text)
            text, page_refs = self._protect_page_refs(text)

            mt_order.append(i)
            texts_for_mt.append(text)
            payloads.append((ner_mapping, protected_terms, references, formulas, units, page_refs, original_text))

        # BATCH MT (tek toplu cagri)
        if texts_for_mt:
            translated_batch = self._translate_batch_hymt(texts_for_mt)

            _na_sent = getattr(self, "_no_answer_sent", None)
            for k, orig_idx in enumerate(mt_order):
                trans = translated_batch[k]
                ner_mapping, protected_terms, references, formulas, units, page_refs, original_text = payloads[k]
                # FX3 (V2 Y3): bu öğenin isteği cevapsız kaldıysa kaynak işaretlenir.
                if isinstance(_na_sent, set) and texts_for_mt[k] in _na_sent:
                    BookTranslator._mark_no_answer_source(self, original_text)

                # Restore (translate_text sirasi: page_refs -> units -> formulas -> references)
                trans = self._restore_page_refs(trans, page_refs)
                trans = self._restore_units(trans, units)
                trans = self._restore_formulas(trans, formulas)
                trans = self._restore_references(trans, references)

                # Glossary restore - hard/hybrid modda XML tag geri konur
                if self.glossary and protected_terms and self.config.glossary_mode in ("hard", "hybrid"):
                    trans, not_found = self.glossary.restore_terms(trans, protected_terms, use_turkish=True)
                    if not_found:
                        trans = self._post_process_missing_terms(trans, protected_terms, not_found, original_text)
                    hard_terms = [t for t in protected_terms if t.get("mode") == "hard"]
                    self.stats["terms_enforced"] += len(hard_terms)

                # NER restore (CORE-06: use_translation=True)
                if self.ner_protector and ner_mapping:
                    trans = self.ner_protector.restore(trans, ner_mapping, use_translation=True)

                # Dogallastirma (translate_text ile ayni: kural tabanli)
                if self.naturalizer:
                    trans = self.naturalizer.naturalize(trans)

                # MT_FAIL + echo kontrolu -> memory (CORE-05 ile ayni koruma)
                # F19: near-echo (noktalama/harf buyuklugu farki) da kesilir.
                _mt_failed = trans.startswith("[MT_FAIL]")
                # F3-07: sınıra dayanan çeviri belleğe yazılmaz (kayıt segmente bağlanır)
                _truncated = BookTranslator._take_truncation(
                    self, texts_for_mt[k], original_text, trans)
                if (self.memory and not _mt_failed and not _truncated
                        and not is_echo_output(original_text, trans)):
                    # FX3 (V2 Y1): ertelenmiş kipte onay bekler.
                    if getattr(self, "_memory_deferred", False) is True:
                        self._memory_hold(original_text, trans)
                    else:
                        self.memory.add(original_text, trans)
                if _mt_failed:
                    trans = trans[len("[MT_FAIL]"):]

                results[orig_idx] = trans
                self.stats["segments_translated"] += 1

        # 1:1 sozlesme guvencesi: hicbir index None kalmasin
        for i, r in enumerate(results):
            if r is None:
                results[i] = texts[i]

        return results

    # Küçük harf Roma rakamı sayfa numarası grameri (ön söz/TOC sayfaları).
    # Alfabe bilinçli olarak [ivxl] ile sınırlı (ön madde sayfaları 89'u
    # geçmez); c/d/m dahil edilmedi ki "mix", "cm", "cd" gibi gerçek
    # sözcük/kısaltmalar yanlışlıkla atlanmasın.
    _ROMAN_PAGE_RE = re.compile(r'(?=[ivxl])(?:xl|l?x{0,3})(?:ix|iv|v?i{0,3})')

    def _foreign_source_segment(self, text: str) -> Optional[str]:
        """Segment kaynak dilden BAŞKA dildeyse dil kodu.

        2026-09-24 (bir test koşusu): İngilizce kitabın içindeki 6 Latince alıntı
        (Kepler, Newton Lex I–III) MT'ye gitti; model Türkçeye çevirip iki
        yerde ağır halüsinasyon üretti ("sesquialtera" → "2:1"). Yol A
        çevirmeni alıntıları Latince bırakmıştı. Karar core/lang_detect
        ``classify_foreign_segment``'te (Latince profili + Avrupa dili +
        Latin dışı alfabe); kapı yalnız source_lang="en" iken çalışır, başka
        kaynak dilli işlerde hiç devreye girmez. Bu katmanda da olması şart:
        retranslator/autofix Latince satırı "çevrilmemiş İngilizce" sanıp
        (retranslator.is_english_line 6 bloğun 5'ine True diyor) buraya geri
        getiriyor.

        2026-09-28 (K5, KARARLAR 14): kaynak dil artık TEX_SOURCE_LANGS'ten
        biri olabilir; karar ``classify_foreign_segment(text, source_lang)``
        (İngilizcede AYNEN; başka dilde yalnız kaynağın alfabesinden farklı
        yazılmış alıntı ayrılır).
        """
        source = getattr(self.config, "source_lang", "en") or "en"
        try:
            from core.lang_detect import classify_foreign_segment
        except Exception:
            return None
        try:
            return classify_foreign_segment(text, source)
        except Exception:
            return None

    def _is_non_translatable(self, text: str) -> bool:
        """Çevrilmemesi gereken metin mi?"""
        # Sadece sayı/sembol
        if re.match(r'^[\d\s\.\,\-\+\=\*\/\(\)\[\]\{\}\<\>\^\%\$\#\@\!\&\|\~\`\:\;\"\']+$', text.strip()):
            return True

        # Sadece formül - CORE-B13: greedy $.*$ yerine $[^$]*$ ile tek formul matche
        if re.match(r'^\$[^$]*\$$', text.strip()):
            return True

        # Roma rakamı sayfa numarası ("xvii", "vi", ...): TOC/ön söz
        # hücrelerinde tek başına gelir; MT bunu söze çeviriyordu
        # ("xvii" → "on yedi", sutton smoke 2026-07-06). Yalnızca küçük
        # harf ve tam eşleşme — "Mix", "XI." gibi metinler etkilenmez.
        stripped = text.strip()
        if 2 <= len(stripped) <= 8 and self._ROMAN_PAGE_RE.fullmatch(stripped):
            return True

        return False

    def _scale_max_tokens(self, texts: List[str], base: int = MT_OUTPUT_TOKENS_FLOOR) -> int:
        """
        BATCH-04: Batch/tekil MT uretimi icin max_tokens'i girdi uzunluguna
        gore olcekle. Sabit 512 token siniri uzun paragraflari CIKTIDA kesiyordu
        (icerik kaybi).

        2026-09-28 (KARARLAR 15): kural config/defaults.py'deki üç sayıyla
        (MT_OUTPUT_TOKENS_FLOOR / _FACTOR / _CAP) ve MT_CONTEXT_CHARS_PER_TOKEN
        ile TEK yerde hesaplanır: ``core.mt_engines.backends.output_token_limit``
        (en uzun metnin karakteri / 3 × 2,2; en az ``base``, en çok tavan).
        Bütün yollar (MLX tekil/toplu, Ollama, OpenAI uyumlu, llama.cpp) bu
        metottan sınır alır.

        Not: max_tokens bir UST sinirdir; kisa girdiler EOS'ta erken durur,
        sadece uzun girdiler daha fazla token uretebilir.
        """
        from core.mt_engines.backends import output_token_limit

        longest = 0
        for t in texts:
            if t:
                n = len(t)
                if n > longest:
                    longest = n
        return output_token_limit(longest, base)

    def _translate_with_mt(self, text: str, is_heading: bool = False) -> str:
        """MT ile çeviri (HY-MT 7B)"""
        # Sunucu / llama.cpp arka ucunda süreç içi model ve tokenizer yoktur;
        # kısa devre yalnız MLX-transformers yolunda (model yüklenmemişse).
        if _active_backend(self) is None and (not self.mt_model or not self.mt_tokenizer):
            return text

        try:
            # BATCH-04 olceklemesi tekil yolda da uygulanir (#24): sabit
            # 512, splitter'in dondurebildigi ~1000 karakterlik cumlelerde
            # Turkce ciktiyi ortadan kesiyordu (batch yollari :1284/:1404
            # zaten olcekliyor, tekil yol kapsam disi kalmisti).
            max_length = self._scale_max_tokens([text], base=MT_OUTPUT_TOKENS_FLOOR)

            # Kısa metin
            if len(text) <= 512:
                return self._translate_single(text, max_length, is_heading=is_heading)

            # Uzun metin - cümlelere böl. 2026-09-29 (F3-14 / K5-2): parçalar
            # ÖZGÜN ayraçla (boşluk / satır sonu / paragraf arası) birleşir;
            # eskiden hepsi tek boşluğa iniyordu ve "% yorum" satırından
            # sonraki cümlenin çevirisi yorumun içine düşüyordu.
            pairs = BookTranslator._sentence_pairs(self, text)
            translated_parts = []

            for sent, sep in pairs:
                if sent.strip():
                    translated_parts.append(
                        self._translate_single(
                            sent,
                            self._scale_max_tokens([sent], base=MT_OUTPUT_TOKENS_FLOOR),
                            is_heading=is_heading,
                        ) + sep
                    )
                elif translated_parts and sep:
                    translated_parts[-1] += sep

            return ''.join(translated_parts).rstrip()

        except Exception as e:
            # G2b-2 (KARARLAR 22, RAPOR MT-13): arka uç tümden kullanılamaz
            # (devre açık: düşünme devre kesicisi, sunucu koptu, model yok,
            # bellek) — YUTULMAZ, yukarı çıkar. latex_pipeline her çağrı yerinde
            # parçayı özgün bırakıp başarısız sayar, açık devrede kalan parçalar
            # ağa gitmeden geçer ve translate_book çıkış 5 verir. Öteki hatalar
            # (tek parçanın hatası, kilit zaman aşımı) eskisi gibi [MT_FAIL].
            from core.mt_engines.contracts import BackendUnavailable
            if isinstance(e, BackendUnavailable):
                raise
            # Hata tipine göre uyarı ver
            error_str = str(e).lower()
            if "cuda" in error_str or "memory" in error_str:
                logger.translator(f"[MT HATA] GPU bellek hatası - segment atlandı: {text[:50]}...", "ERROR")
            elif "timeout" in error_str:
                logger.translator(f"[MT HATA] Timeout - segment atlandı: {text[:50]}...", "ERROR")
            else:
                logger.translator(f"[MT HATA] {type(e).__name__}: {e} - orijinal metin korunuyor", "ERROR")

            # Hata sayacı (varsa)
            if not hasattr(self, '_mt_error_count'):
                self._mt_error_count = 0
            self._mt_error_count += 1

            # Çok fazla hata varsa uyar
            if self._mt_error_count == 10:
                logger.translator("[MT UYARI] 10 segment çevrilemedi! Model veya bellek sorunu olabilir.", "WARNING")
            elif self._mt_error_count == 50:
                logger.translator("[MT KRİTİK] 50+ segment çevrilemedi! Çeviri kalitesi ciddi şekilde etkilenecek.", "ERROR")

            return f"[MT_FAIL]{text}"

    def _sentence_pairs(self, text: str) -> List[Tuple[str, str]]:
        """Cümleler + ardından gelen özgün ayraç (``SentenceSplitter.split_with_separators``).

        Ayraç API'si olmayan (eski / sahte) bölücüde cümleler tek boşlukla
        birleşir (eski davranış).
        """
        splitter = self.splitter
        pairs_fn = getattr(splitter, "split_with_separators", None)
        if callable(pairs_fn):
            pairs = pairs_fn(text)
            if isinstance(pairs, list):
                return pairs
        sentences = list(splitter.split(text))
        return [(sent, " " if i < len(sentences) - 1 else "")
                for i, sent in enumerate(sentences)]

    def _translate_single(self, text: str, max_length: int = 512, is_heading: bool = False) -> str:
        """Tek bir metin parçasını çevir"""
        return self._translate_with_hymt(text, max_length, is_heading=is_heading)

    # 2026-09-24 (parçalama kök nedeni, bir test koşusu): latex_pipeline artık
    # yer tutuculu satırı BÜTÜN gönderiyor. Ölçüm (24 gerçek satır, HY-MT 7B):
    # mevcut talimatla 6/24 satırda model bir <<LATEX_N>> yer tutucusunu
    # yuttu (bozma değil, tamamen atlama); talimata bu tek cümle eklenince
    # 2/24. Cümle YALNIZ metinde yer tutucu varsa eklenir (uzun prompt =
    # sızıntı riski; yer tutucusuz segmentin talimatı değişmez).
    # Sabitler modül düzeyinde (_PLACEHOLDER_CLAUSE, _PLACEHOLDER_TOKEN_RE,
    # _PLACEHOLDER_CLAUSE_LEAK_RE): testler bu metotları MagicMock self ile
    # çağırıyor, self.<sabit> orada Mock dönerdi.

    @staticmethod
    def _has_placeholder_tokens(text: str) -> bool:
        return bool(text) and bool(_PLACEHOLDER_TOKEN_RE.search(text))

    def _build_translation_instruction(self, src_lang: str, tgt_lang: str,
                                       is_heading: bool = False,
                                       placeholders: bool = False) -> str:
        """
        Kitap turune gore ceviri talimat metni olustur.
        Bilimsel/teknik kitaplar icin domain-aware prompt, digerleri icin genel prompt.
        Token maliyetini dusuk tutmak icin kisa ve etkili talimatlar kullanilir.

        is_heading=True iken kisa baslik modu: cevirisi zorunlu, kaynagi echo etme.
        placeholders=True iken (metinde <<LATEX_N>>/<<CMD_N>> var) yer tutucuyu
        aynen koruma cümlesi eklenir (2026-09-24); varsayılan çıktı değişmez.
        """
        # Baslik modu: kisa baslik, cevirisi zorunlu, echo yasak (SOZLESME 1).
        # book_type'tan bagimsiz — basliklarda dogal, tam ceviri istiyoruz.
        if is_heading:
            return (
                f"Translate this heading from {src_lang} to {tgt_lang}. "
                f"It is a short title; translate it fully into {tgt_lang}. "
                f"Do not repeat the {src_lang} text. Output only the translated heading."
            )
        base = BookTranslator._build_body_instruction(self, src_lang, tgt_lang)
        if placeholders is True and "Output only the translation." in base:
            return base.replace(
                "Output only the translation.",
                _PLACEHOLDER_CLAUSE + "Output only the translation.",
            )
        return base

    # G2c (KARAR 25): kitap sözlüğü (latex_pipeline.kitap_sozlugu.KitapSozlugu);
    # LatexTranslator bağlar. None -> istem bugünkü gibi (değişmez).
    kitap_sozlugu = None

    def _kitap_terimli_talimat(self, instruction: str, text: str) -> str:
        """Segmentte geçen kitap sözlüğü terimleri talimatın ÖNÜNE (G2c).

        HY-MT biçiminde resmî "terminology intervention" bloğu
        ("参考下面的翻译：" + satır başına "{kaynak} 翻译成 {hedef}"); sohbet
        modellerinde aynı anlamda İngilizce blok. Sözlük ya da terim yoksa
        talimat AYNEN döner. ``BookTranslator._kitap_terimli_talimat(self, …)``
        biçiminde çağrılır (testlerin MagicMock self'i yeni ad üretmesin).
        """
        sozluk = getattr(self, "kitap_sozlugu", None)
        if sozluk is None or not isinstance(instruction, str) or not text:
            return instruction
        bicim = getattr(getattr(getattr(self, "engine", None), "spec", None),
                        "format", "")
        try:
            blok = sozluk.istem_bloku(
                text, bicim=bicim if isinstance(bicim, str) else "")
        except Exception:  # sözlük hatası çeviriyi durdurmaz
            return instruction
        if not isinstance(blok, str) or not blok:
            return instruction
        return blok + instruction

    def _build_body_instruction(self, src_lang: str, tgt_lang: str) -> str:
        """Gövde (başlık dışı) talimatı — kitap türüne göre (eski gövde aynen)."""

        # Bilimsel/teknik kategoriler icin genisletilmis talimat
        scientific_types = {"science", "engineering", "technical", "computer", "medical"}
        # Edebiyat/fikir kitaplari icin domain-aware prompt
        literary_types = {
            "philosophy", "literature", "psychology",
            "history", "biography", "self_help", "fiction", "novel",
            "sociology", "politics", "memoir",
        }

        if self.config.book_type in scientific_types:
            # Bilimsel metin icin KISA prompt (uzun prompt = leakage riski)
            return (
                f"Translate from {src_lang} to {tgt_lang}. "
                f"Preserve math notation. Output only the translation."
            )
        elif self.config.book_type in literary_types:
            # Edebiyat/fikir kitaplari icin: dogal Turkce, kavram koruma
            return (
                f"Translate from {src_lang} to {tgt_lang}. "
                f"Use natural, literary Turkish. "
                f"Keep proper nouns unchanged. "
                f"Output only the translation."
            )
        else:
            # Genel metin icin standart prompt
            return (
                f"Translate from {src_lang} to {tgt_lang}. "
                f"Output only the translation."
            )

    def _translate_with_hymt(self, text: str, max_length: int = 512,
                             is_heading: bool = False) -> str:
        """Aktif MT motoruyla çeviri (varsayılan: HY-MT1.5, WMT25 şampiyonu).

        NOT: metot adı tarihseldir (API kararlılığı — testler ve
        latex_pipeline bu ada bağlı); motor artık self.engine'den gelir.
        """
        # Dil isimleri motor tablosundan (varsayılan tablo = _HYMT_LANG_NAMES)
        src_lang = self.engine.lang_name(self.config.source_lang, "English")
        tgt_lang = self.engine.lang_name(self.config.target_lang, "Turkish")

        # Sunucu / llama.cpp arka ucu (2026-09-25) — MLX yolundan ÖNCE.
        if _active_backend(self) is not None:
            return self._translate_with_backend(text, src_lang, tgt_lang, max_length, is_heading)

        # MLX mi Transformers mi?
        if getattr(self, '_use_mlx', False):
            # 2026-09-28 (KARARLAR 16): düşünen sohbet biçimli MLX modeli akışla
            # (stream_generate) ve düşünme bütçesiyle üretir. HY-MT (hymt
            # biçimi) bu yola ASLA girmez; bugünkü MLX yolu aşağıda aynen.
            if self._mlx_stream_active():
                return self._translate_with_mlx_stream(text, src_lang, tgt_lang,
                                                       max_length, is_heading)
            result = self._translate_with_hymt_mlx(text, src_lang, tgt_lang, max_length, is_heading)
            # KARARLAR 15: sınıra dayanan çıktı uyarı + sayaçla görünür (çıktı değişmez)
            self._check_mlx_output_limit(text, result, max_length)
            return result
        else:
            return self._translate_with_hymt_transformers(text, src_lang, tgt_lang, max_length, is_heading)

    def _translate_with_hymt_mlx(self, text: str, src_lang: str, tgt_lang: str,
                                 max_length: int, is_heading: bool = False) -> str:
        """HY-MT MLX ile çeviri (Apple Silicon)"""
        try:
            from mlx_lm import generate
        except ImportError as e:
            logger.translator(f"[MLX] mlx_lm import hatası, Transformers'a fallback: {e}", "WARNING")
            self._use_mlx = False
            return self._translate_with_hymt_transformers(text, src_lang, tgt_lang, max_length, is_heading)

        # Prompt biçimi motordan gelir (hymt-7b:
        # <|startoftext|>{instruction}<|extra_4|>{text}<|extra_0|> — <|extra_4|>
        # sistem talimatını kullanıcı metninden ayırır; chat motorlarında ise
        # tokenizer'ın kendi chat template'i kullanılır).
        instruction = self._build_translation_instruction(
            src_lang, tgt_lang, is_heading,
            placeholders=self._has_placeholder_tokens(text))
        instruction = BookTranslator._kitap_terimli_talimat(self, instruction, text)
        prompt = self.engine.build_mlx_prompt(self.mt_tokenizer, instruction, text)

        # BATCH-03: uretim cagrisini serilestir (dar kilit)
        with self._generate_lock():
            result = generate(self.mt_model, self.mt_tokenizer, prompt=prompt, max_tokens=max_length)

        # Motor-özel token temizliği (hymt-7b: <|eos|>, <|extra_0|>, ...).
        # 2026-09-29 (F4-02): kaynak verilir — sohbet biçimli MLX modelinde
        # önsöz / sona eklenen not / sarmalayıcı tırnak sunucu arka ucundaki
        # gibi temizlenir. HYMTFormatEngine kaynağı kullanmaz (HY-MT aynen).
        result = self.engine.clean_output(result, source=text)

        # [?] varsa orijinal kelimeyi bulmaya çalış
        result = self._fix_unknown_tokens(text, result)

        # Defense-in-depth: prompt leak temizligi (sızan talimat metni)
        result = self._strip_prompt_leaks(result, original=text)

        return result.strip()

    # ==================== SUNUCU / LLAMA.CPP ARKA UCU (2026-09-25) ====================
    # MLX yolu (yukarıda) DEĞİŞMEDİ; bu dal yalnız create_backend bir nesne
    # döndürdüğünde (ollama / openai / llamacpp) kullanılır. Koruma zinciri MLX
    # dalıyla birebir aynı sırada: talimat (+ yer tutucu cümlesi) -> üretim
    # (kilitli) -> motor temizliği (kaynağa bakarak) -> [?] onarımı ->
    # prompt-leak temizliği (TASARIM B.9).

    def _translate_with_backend(self, text: str, src_lang: str, tgt_lang: str,
                                max_length: int, is_heading: bool = False) -> str:
        """Tek segment: arka uç üretimi. Hata -> GenerationError (-> [MT_FAIL])."""
        from core.mt_engines.contracts import (
            MSG_EMPTY_OUTPUT, GenerationError, GenerationRequest,
        )
        backend = _active_backend(self)
        instruction = self._build_translation_instruction(
            src_lang, tgt_lang, is_heading,
            placeholders=self._has_placeholder_tokens(text))
        instruction = BookTranslator._kitap_terimli_talimat(self, instruction, text)
        # BATCH-03 ile aynı: tek "GPU kullanıcısı" bu süreç (sunucuda da).
        with self._generate_lock():
            result = backend.generate(
                self.engine, GenerationRequest(instruction, text, max_length))
        error = self._account_generation(result, text, max_length)
        if error:
            raise GenerationError(error)
        out = self._clean_backend_output(result.text, text)
        if not out.strip():
            raise GenerationError(MSG_EMPTY_OUTPUT)
        return out

    def _clean_backend_output(self, raw: str, original: str) -> str:
        """MLX _clean_result zinciriyle AYNI sıra; motor temizliği kaynağı görür."""
        result = self.engine.clean_output(raw or "", source=original)
        result = self._fix_unknown_tokens(original, result)
        result = self._strip_prompt_leaks(result, original=original)
        return result.strip()

    def _note_truncated(self, text: str, used: Optional[int] = None,
                        limit: Optional[int] = None) -> None:
        """Üretim sınırına dayanan çıktı: kabul edilir ama SESSİZ geçmez (KARARLAR 15).

        ``segments_truncated`` sayacı artar (latex_pipeline koşu özetine ve
        checkpoint üst verisine taşır) ve parça günlükte uyarıyla görünür.
        """
        stats = getattr(self, "stats", None)
        if isinstance(stats, dict):
            stats["segments_truncated"] = stats.get("segments_truncated", 0) + 1
        detail = (f"{used}/{limit} token" if used is not None and limit
                  else "finish_reason=length")
        # Standart günlük: pipeline.log'a (rapor) ve konsola düşer.
        _report_log.warning("[MT] Çıktı üretim sınırına dayandı (%s); çeviri kesilmiş "
                            "olabilir: %s...", detail, text[:50])
        # 2026-09-29 (bug avı F3-07): parça kaydı — kesik çıktı belleğe
        # yazılmaz (_take_truncation) ve rapora/QC'ye taşınabilsin diye
        # truncation_report() ile dışarı verilir.
        records = BookTranslator._truncation_records_list(self)
        if records is not None:
            records.append({"text": text or "", "used": used, "limit": limit,
                            "segment": None, "output": None})

    # ---- Kesik çıktı kayıtları (2026-09-29, bug avı F3-07) ----
    def _truncation_records_list(self) -> Optional[list]:
        """Örneğin kesilme kayıt listesi (yoksa kurulur); ``vars`` alınamazsa None."""
        try:
            state = vars(self)
        except TypeError:
            return None
        records = state.get("_truncation_records")
        if not isinstance(records, list):
            records = []
            self._truncation_records = records
        return records

    def _take_truncation(self, mt_text: str, original: str, output: str,
                         parts: bool = False) -> bool:
        """``mt_text``'in üretimi sınıra dayandı mı?

        Dayandıysa kayıt(lar) bu segmente bağlanır (kaynak + çıktı) ve True
        döner: çağıran çıktıyı belleğe YAZMAZ. Kayıt, üretime giden metinle
        (koruma sonrası) tutulur; eşleştirme boşluğu sadeleştirilmiş metinle
        yapılır. ``parts=True`` (tekil yol: uzun metin cümlelere bölünerek
        üretilir) iken cümle parçası da eşleşir; toplu yollarda üretime giden
        metin öğenin kendisidir, yalnız birebir eşitlik sayılır.
        """
        try:
            records = vars(self).get("_truncation_records")
        except TypeError:
            return False
        if not records or not mt_text:
            return False
        whole = " ".join(mt_text.split())
        hit = False
        for rec in records:
            if rec.get("segment") is not None:
                continue
            piece = " ".join(str(rec.get("text") or "").split())
            if piece and (piece == whole or (parts and piece in whole)):
                rec["segment"] = original
                rec["output"] = output
                hit = True
        if hit:
            _report_log.warning(
                "[MT] Sınıra dayanan çeviri belleğe yazılmadı (kesik olabilir): %s...",
                (original or "")[:50])
        return hit

    def truncation_report(self) -> List[Dict]:
        """Üretim sınırına dayanan parçalar (koşu boyunca birikir; en eski önce).

        Her öğe: ``source`` (segmentin özgün metni; segmente bağlanamadıysa
        üretime giden metin), ``output`` (kabul edilen çeviri ya da None),
        ``used`` / ``limit`` (token; bilinmiyorsa None). Çağıran koşu
        başındaki uzunluğu saklayıp yalnız kendi payını alır (sayaç
        ``segments_truncated`` ile aynı mantık).
        """
        try:
            records = vars(self).get("_truncation_records") or []
        except TypeError:
            return []
        return [{"source": rec.get("segment") or rec.get("text"),
                 "output": rec.get("output"), "used": rec.get("used"),
                 "limit": rec.get("limit")}
                for rec in list(records)]

    def _account_generation(self, result, text: str, limit: int) -> Optional[str]:
        """Üretim sonucunu koşu istatistiğine işle; öğe hatası ya da None (kabul).

        KARARLAR 16: düşünme ölçümü (``thinking_*``, ``think_*`` sayaçları).
        KARARLAR 15: ``finish_reason == "length"`` ya da üretilen token
        sınırın ``MT_OUTPUT_NEAR_LIMIT_RATIO`` oranına varmışsa uyarı + sayaç
        (``_note_truncated``); çıktı kabul edilir.
        """
        from core.mt_engines.backends import is_no_answer, near_output_limit

        self._sync_think_stats()
        if result.error:
            if is_no_answer(result):
                # FX3 (V2 Y3): bu metin için model cevap üretemedi.
                sent = getattr(self, "_no_answer_sent", None)
                if isinstance(sent, set):
                    sent.add(text)
                self._no_answer_events = getattr(self, "_no_answer_events", 0) + 1
            return result.error
        used = getattr(result, "output_tokens", None)
        if result.finish_reason == "length" or near_output_limit(used, limit):
            self._note_truncated(text, used, limit)
        return None

    def thinking_monitor(self):
        """Etkin düşünme sınıfı / kapatma doğrulaması (G2b-1): arka ucunki ya da
        düşünen MLX modelininki (``thinking.ThinkingOffMonitor``); yoksa None.
        ``effective_class`` kapatma tutmadıysa "kapatilamaz"dır (yapışkan)."""
        backend = _active_backend(self)
        if backend is not None:
            return getattr(backend, "thinking_monitor", None)
        try:
            return vars(self).get("_mlx_thinking_monitor")
        except TypeError:
            return None

    def _think_guard(self):
        """Etkin düşünme bekçisi: arka ucunki ya da düşünen MLX modelininki; yoksa None."""
        backend = _active_backend(self)
        if backend is not None:
            return getattr(backend, "think_guard", None)
        try:
            return vars(self).get("_mlx_think_guard")
        except TypeError:
            return None

    def _sync_think_stats(self) -> None:
        """Bekçinin koşu sayaçlarını (düşünme ölçümü) ``self.stats``'a yansıt."""
        guard = self._think_guard()
        guard_stats = getattr(guard, "stats", None)
        stats = getattr(self, "stats", None)
        if isinstance(guard_stats, dict) and isinstance(stats, dict):
            stats.update(guard_stats)

    def _translate_batch_backend(self, texts: List[str], max_length: int = 512) -> List[str]:
        """Toplu arka uç üretimi — 1:1 sözleşmesi.

        Öğe hatası ya da boş temiz çıktı -> girdi AYNEN (echo; ``[MT_FAIL]``
        YOK: ``_translate_batch_segments`` öneki silmez) ve ``_mt_error_count``
        artar; F3 kalite kapısı echo'ları sayar. ``BackendUnavailable`` yukarı
        çıkar: latex_pipeline sıralı yedeğe düşer, devre açık olduğu için her
        tekil çağrı ağa gitmeden düşer.
        """
        from core.mt_engines.contracts import GenerationRequest

        backend = _active_backend(self)
        max_length = self._scale_max_tokens(texts, base=max_length)
        src_lang = self.engine.lang_name(self.config.source_lang, "English")
        tgt_lang = self.engine.lang_name(self.config.target_lang, "Turkish")
        instruction = self._build_translation_instruction(src_lang, tgt_lang)
        instruction_ph = self._build_translation_instruction(
            src_lang, tgt_lang, placeholders=True)
        requests = [
            GenerationRequest(
                BookTranslator._kitap_terimli_talimat(
                    self,
                    instruction_ph if self._has_placeholder_tokens(text) else instruction,
                    text),
                text, max_length)
            for text in texts
        ]
        # Kilit tüm batch'i sarar (batch_generate gibi); paralellik (varsa)
        # arka ucun kendi havuzunda, kilidin içinde.
        with self._generate_lock():
            results = backend.generate_batch(self.engine, requests)
        return self._finish_generation_batch(texts, results, max_length)

    def _finish_generation_batch(self, texts: List[str], results, limit: int) -> List[str]:
        """Toplu ``GenerationResult`` listesini 1:1 çıktıya çevir (arka uç + düşünen MLX).

        Öğe hatası ya da boş temiz çıktı -> girdi AYNEN (echo) + ``_mt_error_count``.
        """
        from core.mt_engines.contracts import MSG_EMPTY_OUTPUT

        out: List[str] = []
        for i, text in enumerate(texts):
            res = results[i] if i < len(results) else None
            error = None if res is not None else "sonuç yok"
            cleaned = ""
            if res is not None:
                error = self._account_generation(res, text, limit)
                if not error:
                    cleaned = self._clean_backend_output(res.text, text)
                    if not cleaned.strip():
                        error = MSG_EMPTY_OUTPUT
            if error:
                self._mt_error_count = getattr(self, "_mt_error_count", 0) + 1
                logger.translator(
                    f"[MT HATA] {error} — orijinal metin korunuyor: {text[:50]}...",
                    "WARNING")
                out.append(text)
            else:
                out.append(cleaned)
        return out

    # ==================== DÜŞÜNEN MLX MODELİ (2026-09-28, KARARLAR 16) ====================
    # Yalnız sohbet biçimli (chat) MLX motorlarında ve tokenizer'ın sohbet
    # şablonu düşünme belirtisi taşıyorsa (ya da düşünme ölçüldüyse). HY-MT
    # (hymt biçimi) bu yola girmez. mlx_lm.stream_generate token başına yanıt
    # verir; düşünme bütçeyi aşınca döngüden çıkılıp üreteç kapatılır.

    def _mlx_stream_active(self) -> bool:
        """Chat biçimli MLX motoru düşünen model mi (akışlı yol kullanılsın mı)."""
        from core.mt_engines.backends import template_signals_thinking
        from core.mt_engines.contracts import FORMAT_CHAT

        try:
            state = vars(self)
        except TypeError:
            return False
        spec = getattr(self.engine, "spec", None)
        if getattr(spec, "format", None) != FORMAT_CHAT:
            return False
        cached = state.get("_mlx_thinking")
        if cached is None:
            template = getattr(getattr(self, "mt_tokenizer", None), "chat_template", None)
            cached = template_signals_thinking(template)
            self._mlx_thinking = cached
        guard = state.get("_mlx_think_guard")
        return bool(cached or (guard is not None and guard.seen))

    def _mlx_monitor(self):
        """Düşünen MLX modelinin düşünme sınıfı + kapatma doğrulaması (G2b-1).

        Sınıf tokenizer'ın sohbet şablonundan (keşifle aynı kural): şablonda
        ``enable_thinking`` varsa "kapatilabilir" (``chat_template_kwargs`` ile
        ``enable_thinking=False`` geçer).
        """
        monitor = vars(self).get("_mlx_thinking_monitor")
        if monitor is None:
            from core.mt_engines.backends import supports_no_think
            from core.mt_engines.contracts import BACKEND_MLX
            from core.mt_engines.thinking import ThinkingOffMonitor, template_profile
            spec = self.engine.spec
            monitor = ThinkingOffMonitor(f"MLX · {getattr(spec, 'name', '')}")
            monitor.set_profile(template_profile(
                getattr(getattr(self, "mt_tokenizer", None), "chat_template", None),
                backend=BACKEND_MLX, soft_switch=supports_no_think(spec)))
            self._mlx_thinking_monitor = monitor
        return monitor

    def _mlx_off_requested(self, hard: bool) -> bool:
        """Bu MLX denemesinde resmî kapatma isteği gitti mi: şablon anahtarına
        ``enable_thinking=False`` ya da sert denemede Qwen3 "/no_think"."""
        from core.mt_engines.backends import supports_no_think, wants_thinking_off
        from core.mt_engines.contracts import THINKING_OFF_TEMPLATE_SWITCH
        spec = self.engine.spec
        if not wants_thinking_off(spec):
            return False
        kwargs = getattr(spec, "chat_template_kwargs", None) or {}
        switch = (self._mlx_monitor().profile.off_path == THINKING_OFF_TEMPLATE_SWITCH
                  and kwargs.get("enable_thinking") is False)
        return switch or (bool(hard) and supports_no_think(spec))

    def _mlx_guard(self):
        """Düşünen MLX modelinin bekçisi (bütçe / düşünme payı, tek yeniden deneme,
        ölçülen hız, devre kesici)."""
        guard = vars(self).get("_mlx_think_guard")
        if guard is None:
            from core.mt_engines.backends import (
                ThinkingGuard, resolve_think_budget, resolve_think_cap,
            )
            spec = self.engine.spec
            guard = ThinkingGuard(resolve_think_budget(spec), resolve_think_cap(spec))
            self._mlx_think_guard = guard
        return guard

    def _mlx_thinking_mode(self) -> bool:
        """Düşünen MLX modelinde düşünerek çeviri kipi açık mı (G2b-2, KARARLAR 22).

        ``MTBackend.thinking_mode`` ile aynı kural: etkin sınıf "kapatilamaz"
        (şablonda kapatma anahtarı yok ya da koşuda kapatma tutmadı — yapışkan
        bayrak) ya da kayıt düşünmeyi istiyor ve model düşünüyor. HY-MT bu yola
        hiç girmez (``_mlx_stream_active``).
        """
        from core.mt_engines.backends import wants_thinking_off
        from core.mt_engines.contracts import THINKING_ALWAYS

        if self._mlx_monitor().effective_class == THINKING_ALWAYS:
            return True
        state = vars(self)
        guard = state.get("_mlx_think_guard")
        return (not wants_thinking_off(self.engine.spec)) and bool(
            state.get("_mlx_thinking") or (guard is not None and guard.seen))

    def _mlx_stream_attempt(self, text: str, instruction: str, limit: int, hard: bool,
                            deadline: float, budget: Optional[int] = None,
                            time_limit: Optional[float] = None):
        """Tek akışlı MLX denemesi -> GenerationResult (ölçüm alanlarıyla).

        *budget*: denemenin düşünme sınırı (G2b-2: kipte parçanın payı; verilmezse
        bekçinin yoklama bütçesi). Üretim sınırı = çeviri sınırı + düşünme sınırı.
        *time_limit*: kipte zaman aşımı iletisindeki süre (parçanın süre sınırı).
        """
        from mlx_lm import stream_generate

        from core.mt_engines.backends import (
            ThinkingMeter, format_seconds, prompt_opens_thinking, supports_no_think,
            with_no_think,
        )
        from core.mt_engines.contracts import (
            MSG_EMPTY_OUTPUT, MSG_THINK_BUDGET, MSG_TIMEOUT, GenerationResult,
        )

        guard = self._mlx_guard()
        budget = guard.budget if budget is None else int(budget)
        messages = None
        if hard and supports_no_think(self.engine.spec):
            messages = with_no_think(self.engine.build_chat_messages(instruction, text))
        prompt = self.engine.build_mlx_prompt(self.mt_tokenizer, instruction, text,
                                              messages=messages)
        # Şablon düşünmeyi istemin içinde açtıysa (istem '<think>' ile biter)
        # yanıt </think>'e kadar düşünmedir: bütçe ilk parçadan işler (VTD).
        meter = ThinkingMeter(budget, text,
                              forced_open=prompt_opens_thinking(prompt))
        finish = None
        timed_out = False
        counted = 0
        gen = stream_generate(self.mt_model, self.mt_tokenizer, prompt=prompt,
                              max_tokens=int(limit) + int(budget))
        try:
            for resp in gen:
                n = getattr(resp, "generation_tokens", None)
                step = (n - counted) if isinstance(n, int) and n >= counted else 1
                counted = n if isinstance(n, int) else counted + 1
                meter.add_content(getattr(resp, "text", ""), tokens=step)
                finish = getattr(resp, "finish_reason", None) or finish
                if meter.exceeded:
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    break
        finally:
            close = getattr(gen, "close", None)
            if callable(close):
                close()
        fields = meter.result_fields()
        if meter.exceeded:
            _report_log.warning("[MT DÜŞÜNME] MLX: düşünme %d token'da kesildi (sınır %d); "
                                "üretim durduruldu", meter.tokens, budget)
            return GenerationResult(error=MSG_THINK_BUDGET.format(
                tokens=meter.tokens, budget=budget), **fields)
        if timed_out:
            seconds = (format_seconds(time_limit) if time_limit
                       else int(MT_HTTP_TIMEOUT_S))
            return GenerationResult(error=MSG_TIMEOUT.format(seconds=seconds), **fields)
        if not meter.content.strip() or not meter.answer_text().strip():
            return GenerationResult(text="", finish_reason=finish, error=MSG_EMPTY_OUTPUT,
                                    output_tokens=meter.answer_tokens, **fields)
        return GenerationResult(text=meter.content, finish_reason=finish,
                                output_tokens=meter.answer_tokens, **fields)

    def _mlx_stream_generate(self, text: str, instruction: str, limit: int):
        """Bekçiden geçen akışlı MLX üretimi; devre kesici açıksa ThinkingBudgetExceeded.

        G2b-2 (KARARLAR 22): sunucu arka uçlarıyla (``MTBackend.generate``)
        aynı plan — kipte parçanın düşünme payı, paya ve ölçülen hıza göre
        ölçekli süre, pay dolup cevap yoksa TEK yeniden deneme (Qwen3
        "/no_think" varsa onunla), cevapsız istek koşu boyunca hatırlanır.
        """
        from core.mt_engines.backends import (
            is_no_answer, short_item, supports_no_think, wants_thinking_off,
        )
        from core.mt_engines.contracts import THINKING_OFF_QWEN3_SOFT, ThinkingBudgetExceeded

        guard = self._mlx_guard()
        if guard.tripped:
            raise ThinkingBudgetExceeded(guard.tripped)
        cached = guard.cached_no_answer(instruction, text)
        if cached is not None:
            return cached
        spec = self.engine.spec
        start = time.monotonic()
        chars = len(text or "")
        can_harden = wants_thinking_off(spec) and supports_no_think(spec)
        if (can_harden and not guard.hard_mode
                and self._mlx_monitor().profile.off_path == THINKING_OFF_QWEN3_SOFT):
            # KARARLAR 22: resmî kapatma yolu Qwen3 "/no_think" ise ilk denemeden
            guard.adopt_hard_mode("MLX: Qwen3 '/no_think' resmî kapatma yolu ilk "
                                  "denemeden kullanılıyor (KARARLAR 22)")

        def attempt(hard: bool):
            mode = self._mlx_thinking_mode()
            budget, span = guard.plan(chars, limit, mode, float(MT_HTTP_TIMEOUT_S))
            return self._mlx_monitor().observe(
                self._mlx_stream_attempt(text, instruction, limit, hard, start + span,
                                         budget=budget, time_limit=span if mode else None),
                off_requested=self._mlx_off_requested(hard))

        with self._generate_lock():
            result = guard.run(attempt, can_harden, thinking_mode=self._mlx_thinking_mode,
                               short=short_item(chars), shorter=lambda: can_harden,
                               chars=chars)
        if guard.tripped:
            raise ThinkingBudgetExceeded(guard.tripped)
        if is_no_answer(result) and self._mlx_thinking_mode():
            guard.remember_no_answer(instruction, text, result)
        return result

    def _translate_with_mlx_stream(self, text: str, src_lang: str, tgt_lang: str,
                                   max_length: int, is_heading: bool = False) -> str:
        """Tek segment, düşünen MLX modeli. Hata -> GenerationError (-> [MT_FAIL])."""
        from core.mt_engines.contracts import MSG_EMPTY_OUTPUT, GenerationError

        instruction = self._build_translation_instruction(
            src_lang, tgt_lang, is_heading,
            placeholders=self._has_placeholder_tokens(text))
        instruction = BookTranslator._kitap_terimli_talimat(self, instruction, text)
        result = self._mlx_stream_generate(text, instruction, max_length)
        error = self._account_generation(result, text, max_length)
        if error:
            raise GenerationError(error)
        out = self._clean_backend_output(result.text, text)
        if not out.strip():
            raise GenerationError(MSG_EMPTY_OUTPUT)
        return out

    def _translate_batch_mlx_stream(self, texts: List[str],
                                    max_length: int = MT_OUTPUT_TOKENS_FLOOR) -> List[str]:
        """Toplu çeviri, düşünen MLX modeli: sıralı akışlı üretim (1:1 sözleşmesi)."""
        limit = self._scale_max_tokens(texts, base=max_length)
        src_lang = self.engine.lang_name(self.config.source_lang, "English")
        tgt_lang = self.engine.lang_name(self.config.target_lang, "Turkish")
        instruction = self._build_translation_instruction(src_lang, tgt_lang)
        instruction_ph = self._build_translation_instruction(
            src_lang, tgt_lang, placeholders=True)
        results = [
            self._mlx_stream_generate(
                text, BookTranslator._kitap_terimli_talimat(
                    self,
                    instruction_ph if self._has_placeholder_tokens(text) else instruction,
                    text),
                limit)
            for text in texts
        ]
        return self._finish_generation_batch(texts, results, limit)

    # ---- MLX HY-MT: sınıra dayanan çıktının görünürlüğü (KARARLAR 15) ----
    def _mlx_output_tokens(self, text: str) -> Optional[int]:
        """Çıktının modelin kendi tokenizer'ıyla token sayısı; ölçülemezse None."""
        encode = getattr(getattr(self, "mt_tokenizer", None), "encode", None)
        if not callable(encode) or not text:
            return None
        try:
            try:
                ids = encode(text, add_special_tokens=False)
            except TypeError:
                ids = encode(text)
            count = len(ids)
        except Exception:   # ölçüm çeviriyi düşürmesin
            return None
        return count if isinstance(count, int) else None

    def _check_mlx_output_limit(self, text: str, result: str, limit: int) -> None:
        """MLX çıktısı sınıra dayandıysa uyar + say (çıktı DEĞİŞMEZ)."""
        from core.mt_engines.backends import near_output_limit

        if not isinstance(result, str):
            return
        used = self._mlx_output_tokens(result)
        if near_output_limit(used, limit):
            self._note_truncated(text, used, limit)

    def backend_health(self) -> Optional[str]:
        """Arka uç sağlığı: None = sağlıklı ya da MLX yolu; aksi Türkçe neden (devre açık).

        MLX yolunda yalnız düşünen modelin koşu devre kesicisi (KARARLAR 16)
        açıldıysa onun nedeni döner; HY-MT yolunda her zaman None.
        """
        backend = _active_backend(self)
        if backend is None:
            try:
                guard = vars(self).get("_mlx_think_guard")
            except TypeError:
                return None
            return getattr(guard, "tripped", None) or None
        try:
            return backend.health()
        except Exception:   # sağlık sorgusu koşuyu düşürmesin
            return None

    def _fix_unknown_tokens(self, original: str, translated: str) -> str:
        """
        MT ciktisindaki [?]/[UNK]/<unk> token'larini GUVENLI sekilde ele al.

        CORE-02 / B-10: Eski Strateji 1 (pozisyon-bazli sozluk ikamesi) ve
        Strateji 2 (fuzzy sozluk enjeksiyonu) SILINDI. Turkce SOV kelime
        sirasinda Ingilizce kelime listesini Turkce kelime pozisyonuyla
        indekslemek anlamsizdi; sozlukten skor-90 ile basilan "ilk anlam"
        siklikla yanlis kelime, tekrar kelime, hatta sozluk TANIM cumlesi
        enjekte ediyordu (ornek: "[?] mindere oturdu." -> "belirli
        durumlarda isimden once kullanilir mindere oturdu.").

        Geriye sadece SOZLUK KULLANMAYAN ozel-isim koruma kaldi: [?] yerine
        orijinaldeki henuz cevride gecmeyen ilk buyuk-harfli kelimeyi koy.
        Hicbir token bu sekilde guvenle cozulemezse (CORE-B06 gerekcesi),
        yanlis kelime uydurmak yerine segmenti ORIJINALIYLE geri dondur
        (dusuk guven; audit onerisi).
        """
        unknown_pattern = r'\[\s*\?\s*\]|\[UNK\]|<unk>'
        matches = list(re.finditer(unknown_pattern, translated, re.IGNORECASE))
        if not matches:
            return translated

        # Orijinaldeki kelimeler (ozel isim adayi icin)
        original_words = re.findall(r'\b[a-zA-Z][a-zA-Z\'-]*[a-zA-Z]\b|\b[a-zA-Z]\b', original)

        result = translated
        unresolved = 0

        # Ters sirada: onceki match'lerin index'leri bozulmasin
        for match in reversed(matches):
            replacement = None
            # Ozel isim koruma: cevride henuz gecmeyen ilk buyuk-harfli orijinal kelime.
            # (Sozluk kullanilmaz -> DATA-08 halusinasyonu tetiklenmez.)
            for orig_word in original_words:
                if (len(orig_word) > 1 and orig_word[0].isupper()
                        and orig_word.lower() not in result.lower()):
                    replacement = orig_word
                    break

            if replacement:
                result = result[:match.start()] + replacement + result[match.end():]
            else:
                unresolved += 1

        # Cozulemeyen [?] kaldiysa: yanlis kelime atamaktansa segmenti geri al
        if unresolved:
            if logger:
                logger.translator(
                    "[?]/[UNK] guvenle cozulemedi, segment orijinaliyle geri "
                    "donduruluyor (dusuk guven)", "WARNING")
            return original

        # Cift bosluklari temizle
        return ' '.join(result.split())

    def _context_match_score(self, word: str, context_before: str, context_after: str, original: str) -> int:
        """Kelimenin baglama uyum skorunu hesapla"""
        score = 50  # Baz skor

        # Kelime orijinal metinde varsa
        if word.lower() in original.lower():
            score += 20

        # Baglam kelimelerini kontrol et
        context_words = (context_before + " " + context_after).lower().split()

        # Turkce karsiligi varsa ve baglamda geciyorsa
        tr_word = self._get_dictionary_translation(word)
        if tr_word and any(tr_word.lower() in cw for cw in context_words):
            score += 15

        return score
    
    def _get_dictionary_translation(self, word: str) -> str:
        """Sözlükten kelime çevirisi al.

        P2-2: İki glossary sistemi çakışmasını çözer -- önce kullanıcı
        GlossaryManager'ına (curated, yüksek öncelik) sor, bulunamazsa
        DictionaryLoader'a (1.46M kelimelik genel sözlük) düş.

        Böylece aynı terim için kullanıcı custom çevirisi DictionaryLoader
        tarafından ezilmez. GlossaryManager birincil, DictionaryLoader
        fallback.
        """
        # TODO(P2-2): Glossary'yi tamamen GlossaryManager altında birleştir;
        # DictionaryLoader ham veri kaynağı olarak kalsın (ayrı sprint).

        # 1) Birincil: GlossaryManager (kullanıcı custom + curated)
        try:
            gm = getattr(self, "glossary", None)
            if gm is not None:
                term_info = gm.get_term(word.lower())
                if term_info:
                    # term_info genelde {'tr': '...', 'context': ..., ...}
                    tr = term_info.get("tr") if isinstance(term_info, dict) else None
                    if tr:
                        return tr
        except Exception:
            pass

        # 2) Fallback: DictionaryLoader (1.46M genel wordlist)
        try:
            if self._dict_loader is None:
                from core.dictionary_loader import DictionaryLoader
                self._dict_loader = DictionaryLoader()

            translation = self._dict_loader.get_translation(word.lower())
            if translation:
                return translation
        except Exception:
            pass

        return None

    def _strip_prompt_leaks(self, result: str, original: str = "") -> str:
        """
        MT ciktisindaki prompt leak kalintilari temizle.
        Defense-in-depth: downstream (sanitizer/autofix) de temizler ama
        en erken noktada yakalamak daha guvenli.

        Args:
            result: MT ciktisi
            original: Orijinal metin (bos donerse fallback icin)

        Returns:
            Temizlenmis metin (bos donerse original'i korur)
        """
        # G2c (KARAR 25): kitap sözlüğü terim bloğunun yankısı — yalnız
        # bloğun kendi satırları silinir (Türkçe çeviri satırına dokunulmaz).
        if result:
            try:
                from latex_pipeline.kitap_sozlugu import istem_sizintisini_temizle
                result = istem_sizintisini_temizle(result)
            except ImportError:
                pass
        if not result or not result.strip():
            return result

        try:
            # MT çıktısı temizliği — tam liste ("Çeviri/Çevirmen notu"
            # MT-only kalıpları dahil; A-1, 2026-07-27).
            from latex_pipeline.patterns import ALL_PROMPT_LEAK_PATTERNS as PROMPT_LEAK_PATTERNS

            # CORE-03: Silinen span'lari yakala ki fazla-silmeyi ayirt edebilelim.
            removed_parts = []
            # FX3 (2026-10-05, V2 Y2): istem işareti biçimli silinenler
            # (günlük + stats["prompt_marker_leaks"]; eskiden sessizdi).
            marker_parts = []

            # 2026-09-24: yer tutucu koruma cümlesinin (_PLACEHOLDER_CLAUSE)
            # olası yankısı — İngilizce ve Türkçe biçim. Yalnız emir kipli
            # talimat kalıbı; "yer tutucu" geçen meşru cümle etkilenmez.
            # Bu silme aşağıdaki Türkçe-harf guard'ından MUAF (denetim 24 Eyl,
            # Mercek 2 #3): talimat yankısı olduğu kesin, Türkçe harf içermesi
            # meşruiyet kanıtı değil — eskiden kısa segmentte guard sızıntıyı
            # geri koyuyordu. Cümle ortasındaki yankıda bağlaç silinir, kapanış
            # noktası kalır; kalıp sondaki boşluğu yutmaz (metin yapışmaz).
            cleaned = _PLACEHOLDER_CLAUSE_LEAK_RE.sub("", result)
            if cleaned != result:
                logger.translator(
                    "Yer tutucu talimatı yankısı silindi: %.80s" % result,
                    "WARNING")
            base = cleaned.strip()
            for pattern in PROMPT_LEAK_PATTERNS:
                if pattern.pattern == _PLACEHOLDER_CLAUSE_LEAK_RE.pattern:
                    continue  # yukarıda guard'sız uygulandı
                # FX3 (V2 Y2): KAYNAKTA geçen eşleşme meşru içeriktir (ör.
                # şablon kitabındaki "<translate>" etiketi); kaynaktaki sayısı
                # kadarı korunur, yalnız fazlası (sızıntı) silinir.
                allowed = Counter(m.group(0)
                                  for m in re.finditer(pattern, original or ""))
                is_marker = "TRANSLAT" in pattern.pattern

                def _capture(m, allowed=allowed, is_marker=is_marker):
                    token = m.group(0)
                    if allowed[token] > 0:
                        allowed[token] -= 1
                        return token
                    removed_parts.append(token)
                    if is_marker:
                        marker_parts.append(token.strip())
                    return ''

                cleaned = re.sub(pattern, _capture, cleaned)
            cleaned = cleaned.strip()

            # CORE-03: Fazla-silme korumasi. Leak pattern'lerinin bir kismi
            # (`orijinal metin`, `metni Turkceye cevir`, `bu bir ceviri` gibi
            # [^\n]* kuyruklu Turkce formlar) mesru Turkce ceviri cumlelerini
            # cumle-ici yakalayip siliyor -> segment tek kelimeye iniyor ya da
            # bosaliyor. Ayirt edici sinyal: SILINEN metin Turkce'ye ozgu harf
            # (ç ğ ı İ ö ş ü ...) iceriyorsa ve buyuk bir kisim gittiyse, bu bir
            # yanlis-pozitiftir (mesru Turkce icerik) -> temizlenmemis MT
            # ciktisini koru. Ingilizce leak'ler ("As an AI language model",
            # "Here is the translation:") Turkce harf icermez, onlar silinir.
            # base: yer tutucu yankısı silinmiş hâl (guard onu geri koymaz).
            removed_text = " ".join(removed_parts)
            turkish_removed = bool(re.search(r"[çğıİöşüÇĞÖŞÜ]", removed_text))
            if (turkish_removed and base
                    and len(cleaned) < len(base) * _PROMPT_LEAK_KEEP_RATIO):
                return base

            # Tam leak (cleaned bosaldi) ve silinen Ingilizce ise: eski davranis,
            # orijinali koru (downstream yine temizler).
            if not cleaned and original:
                return original
            # FX3 (V2 Y2): her temizlik günlükte ve sayaçta (olay sayısı).
            if removed_parts:
                key = "prompt_marker_leaks" if marker_parts else "prompt_leaks_stripped"
                stats = getattr(self, "stats", None)
                if isinstance(stats, dict):
                    stats[key] = stats.get(key, 0) + 1
                shown = marker_parts or [p.strip() for p in removed_parts]
                logger.translator(
                    "İstem sızıntısı temizlendi (çekirdek, %d parça: %s): %.60s"
                    % (len(removed_parts), ", ".join(s[:40] for s in shown[:4]),
                       (cleaned or base).replace("\n", " ")), "WARNING")
            return cleaned if cleaned else base
        except ImportError:
            # patterns module yuklenemezse downstream yakalayacak
            return result

    def _translate_with_hymt_transformers(self, text: str, src_lang: str, tgt_lang: str,
                                          max_length: int, is_heading: bool = False) -> str:
        """Transformers ile çeviri (Linux/Windows) — mesaj biçimi motordan"""
        instruction = self._build_translation_instruction(
            src_lang, tgt_lang, is_heading,
            placeholders=self._has_placeholder_tokens(text))
        instruction = BookTranslator._kitap_terimli_talimat(self, instruction, text)
        messages = self.engine.build_chat_messages(instruction, text)

        # Tokenize with chat template
        input_text = self.mt_tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True
        )

        inputs = self.mt_tokenizer(input_text, return_tensors="pt")

        # GPU'ya taşı
        if hasattr(self.mt_model, 'device'):
            inputs = {k: v.to(self.mt_model.device) for k, v in inputs.items()}

        # Generate -- BATCH-03: uretim cagrisini serilestir (dar kilit)
        with self._generate_lock():
            outputs = self.mt_model.generate(
                **inputs,
                max_new_tokens=max_length,
                do_sample=False,
                pad_token_id=self.mt_tokenizer.eos_token_id
            )

        # Sadece yeni üretilen kısmı al
        generated = outputs[0][inputs['input_ids'].shape[1]:]
        result = self.mt_tokenizer.decode(generated, skip_special_tokens=True)
        
        # [?] varsa orijinal kelimeyi bulmaya çalış
        result = self._fix_unknown_tokens(text, result)

        # Defense-in-depth: prompt leak temizligi (sızan talimat metni)
        result = self._strip_prompt_leaks(result, original=text)

        return result.strip()

    def translate_batch_raw(self, texts: List[str], max_length: int = 512) -> List[str]:
        """Ham batch MT (koruma zinciri YOK) — public/stabil ad.

        latex_pipeline gibi dış çağıranlar için _translate_batch_hymt'nin
        resmi kapısı (2026-07-27). Eski private ad tarihsel sebeple ve test
        sözleşmeleri için aynen durur; ikisi aynı davranıştır.
        """
        return self._translate_batch_hymt(texts, max_length)

    def _translate_batch_hymt(self, texts: List[str], max_length: int = 512) -> List[str]:
        """
        Batch processing ile çoklu çeviri

        Args:
            texts: Çevrilecek metin listesi
            max_length: Maksimum çıktı uzunluğu

        Returns:
            Çevrilmiş metin listesi
        """
        if not texts:
            return []

        # Sunucu / llama.cpp arka ucu (2026-09-25) — MLX yolundan ÖNCE.
        if _active_backend(self) is not None:
            return self._translate_batch_backend(texts, max_length)

        # MLX kullanılıyorsa - ardışık generate (Neural Engine zaten hızlı)
        if getattr(self, '_use_mlx', False):
            # Düşünen sohbet biçimli MLX modeli: akışlı, bütçeli sıralı üretim
            # (KARARLAR 16). HY-MT bugünkü _translate_batch_mlx yolunda kalır.
            if self._mlx_stream_active():
                return self._translate_batch_mlx_stream(texts, max_length)
            results = self._translate_batch_mlx(texts, max_length)
            # KARARLAR 15: sınıra dayanan çıktı uyarı + sayaçla görünür (çıktı değişmez)
            limit = self._scale_max_tokens(texts, base=max_length)
            for text, result in zip(texts, results):
                self._check_mlx_output_limit(text, result, limit)
            return results

        # BATCH-04: max_tokens'i girdi uzunluguna gore olcekle (512 truncation fix)
        max_length = self._scale_max_tokens(texts, base=max_length)

        # Transformers API (Linux/Windows için batch processing)
        # Dil isimleri motor tablosundan (varsayılan = _HYMT_LANG_NAMES)
        src_lang = self.engine.lang_name(self.config.source_lang, "English")
        tgt_lang = self.engine.lang_name(self.config.target_lang, "Turkish")
        instruction = self._build_translation_instruction(src_lang, tgt_lang)
        instruction_ph = self._build_translation_instruction(
            src_lang, tgt_lang, placeholders=True)

        # Her metin için chat formatı oluştur (mesaj biçimi motordan)
        all_messages = []
        for text in texts:
            messages = self.engine.build_chat_messages(
                BookTranslator._kitap_terimli_talimat(
                    self,
                    instruction_ph if self._has_placeholder_tokens(text) else instruction,
                    text),
                text)
            input_text = self.mt_tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True
            )
            all_messages.append(input_text)

        # Batch tokenization ve GPU işlemi - OOM korumalı
        try:
            inputs = self.mt_tokenizer(
                all_messages,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=2048
            )

            # GPU'ya taşı
            if hasattr(self.mt_model, 'device'):
                inputs = {k: v.to(self.mt_model.device) for k, v in inputs.items()}

            # Batch generate -- BATCH-03: uretim cagrisini serilestir (dar kilit).
            # Kilit SADECE generate'i sarar; asagidaki except (OOM -> batch bolme
            # rekursiyonu) kilit DISINDA calisir -> tekrar-giris deadlock'u yok.
            with self._generate_lock():
                outputs = self.mt_model.generate(
                    **inputs,
                    max_new_tokens=max_length,
                    do_sample=False,
                    pad_token_id=self.mt_tokenizer.eos_token_id
                )

        except RuntimeError as e:
            error_msg = str(e).lower()
            if 'out of memory' in error_msg or 'oom' in error_msg or 'cuda' in error_msg or 'mps' in error_msg:
                logger.translator(f"[BATCH-OOM] GPU bellek hatası, batch ikiye bölünüyor: {len(texts)} -> {len(texts)//2}", "WARNING")
                # GPU belleği temizle
                try:
                    import torch
                    if hasattr(torch, 'cuda') and torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    if hasattr(torch, 'mps') and hasattr(torch.mps, 'empty_cache'):
                        torch.mps.empty_cache()
                except ImportError:
                    pass

                # Batch'i ikiye böl ve tekrar dene
                if len(texts) > 1:
                    mid = len(texts) // 2
                    first_half = self._translate_batch_hymt(texts[:mid], max_length)
                    second_half = self._translate_batch_hymt(texts[mid:], max_length)
                    return first_half + second_half
                else:
                    # Tek metin kaldı, tek tek çevir
                    return [self._translate_with_hymt_transformers(texts[0], src_lang, tgt_lang, max_length)]
            else:
                raise  # OOM değilse hatayı fırlat

        # Her çıktıyı decode et
        results = []
        input_length = inputs['input_ids'].shape[1]

        for i, output in enumerate(outputs):
            # Sadece yeni üretilen kısmı al
            generated = output[input_length:]
            result = self.mt_tokenizer.decode(generated, skip_special_tokens=True)

            # [?] varsa orijinal kelimeyi bulmaya çalış
            result = self._fix_unknown_tokens(texts[i], result)
            # Defense-in-depth: prompt leak temizligi (sızan talimat metni)
            result = self._strip_prompt_leaks(result, original=texts[i])
            results.append(result.strip())

        return results

    def _translate_batch_mlx(self, texts: List[str], max_length: int = 512) -> List[str]:
        """
        MLX batch ceviri -- batch_generate ile paralel uretim.

        batch_generate, birden fazla prompt'u ayni anda isler (continuous batching).
        Tek tek generate() yerine batch_generate kullanmak, ozellikle cok sayida
        kisa segment icin onemli hiz kazanci saglar (prefill paralellesir).

        Fallback: batch_generate kullanilamazsa (import hatasi, runtime hatasi)
        eski sirali generate() yontemine geri doner.
        """
        # --- Import ---
        try:
            from mlx_lm import generate
        except ImportError as e:
            logger.translator(f"[MLX] mlx_lm import hatasi, tek tek Transformers ceviriye geciliyor: {e}", "WARNING")
            self._use_mlx = False
            # CORE-B4: dil isimleri module-level tek sabitten
            sl = self.engine.lang_name(self.config.source_lang, "English")
            tl = self.engine.lang_name(self.config.target_lang, "Turkish")
            return [self._translate_with_hymt_transformers(t, sl, tl, max_length) for t in texts]

        # --- batch_generate import ---
        _has_batch_generate = False
        try:
            from mlx_lm import batch_generate
            _has_batch_generate = True
        except ImportError:
            pass

        # BATCH-04: max_tokens'i girdi uzunluguna gore olcekle (512 truncation fix)
        max_length = self._scale_max_tokens(texts, base=max_length)

        # Dil isimleri motor tablosundan (varsayılan = _HYMT_LANG_NAMES)
        src_lang = self.engine.lang_name(self.config.source_lang, "English")
        tgt_lang = self.engine.lang_name(self.config.target_lang, "Turkish")
        instruction = self._build_translation_instruction(src_lang, tgt_lang)
        # 2026-09-24: yer tutuculu metne "aynen koru" cümleli talimat.
        instruction_ph = self._build_translation_instruction(
            src_lang, tgt_lang, placeholders=True)

        def _instr_for(text: str) -> str:
            return BookTranslator._kitap_terimli_talimat(
                self,
                instruction_ph if self._has_placeholder_tokens(text) else instruction,
                text)

        # --- Yardimci: motor-özel token temizliği + model-bağımsız onarım ---
        def _clean_result(raw: str, original: str) -> str:
            # F4-02: kaynak verilir (sohbet modeli temizliği; HY-MT aynen)
            result = self.engine.clean_output(raw, source=original)
            result = self._fix_unknown_tokens(original, result)
            # Defense-in-depth: prompt leak temizligi (sızan talimat metni)
            result = self._strip_prompt_leaks(result, original=original)
            return result.strip()

        # =====================================================================
        # YOL A: batch_generate (paralel uretim -- hizli)
        # =====================================================================
        if _has_batch_generate and len(texts) > 1:
            try:
                # Her metin icin prompt olustur ve tokenize et (biçim motordan)
                prompts_tokenized = []
                for text in texts:
                    prompt_str = self.engine.build_mlx_prompt(self.mt_tokenizer, _instr_for(text), text)
                    # batch_generate List[List[int]] istiyor
                    token_ids = self.mt_tokenizer.encode(prompt_str, add_special_tokens=False)
                    if isinstance(token_ids, list):
                        prompts_tokenized.append(token_ids)
                    else:
                        # CORE-B09: mx.array/tensor elemanlarini int'e donustur
                        prompts_tokenized.append([int(t) for t in token_ids])

                # batch_generate cagir -- BATCH-03: uretimi serilestir (dar kilit).
                # Hata olursa kilit context manager ile birakilir; asagidaki
                # sirali fallback kilit disinda tekrar acquire eder (deadlock yok).
                with self._generate_lock():
                    batch_response = batch_generate(
                        self.mt_model,
                        self.mt_tokenizer,
                        prompts=prompts_tokenized,
                        max_tokens=max_length,
                    )

                # Sonuclari temizle
                results = []
                for i, raw_text in enumerate(batch_response.texts):
                    results.append(_clean_result(raw_text, texts[i]))

                return results

            except Exception as e:
                # batch_generate basarisiz -- sirali fallback'e dus
                logger.translator(f"[MLX] batch_generate hatasi, sirali generate'e geciliyor: {e}", "WARNING")

        # =====================================================================
        # YOL B: Sirali generate (fallback veya tek metin)
        # =====================================================================
        results = []
        for text in texts:
            prompt = self.engine.build_mlx_prompt(self.mt_tokenizer, _instr_for(text), text)
            # BATCH-03: her uretim cagrisini serilestir (dar kilit)
            with self._generate_lock():
                result = generate(self.mt_model, self.mt_tokenizer, prompt=prompt, max_tokens=max_length)
            results.append(_clean_result(result, text))

        return results

    def translate_batch(self, texts: List[str], show_progress: bool = False) -> List[str]:
        """
        Çoklu metin çevirisi - batch processing ile hızlı

        Args:
            texts: Çevrilecek metin listesi
            show_progress: İlerleme göster

        Returns:
            Çevrilmiş metin listesi
        """
        if not texts:
            return []

        results = []
        batch_size = self.config.batch_size

        # Batch'lere böl
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]

            if show_progress:
                logger.translator(f"Batch {i//batch_size + 1}/{(len(texts) + batch_size - 1)//batch_size}", "DEBUG")

            # Her batch'i cevir
            batch_results = self._translate_batch_hymt(batch)

            results.extend(batch_results)

        return results

    def _translate_batch_segments(self, texts: List[str]) -> List[str]:
        """
        Segment listesini batch halinde çevir (NER, glossary, formül koruma dahil)

        Bu metod translate_text'in batch versiyonu - tüm koruma işlemlerini yapar.
        + Translation memory desteği eklendi
        """
        if not texts:
            return []

        # 0. Memory kontrolü - hangi segmentler cache'de?
        results = [None] * len(texts)  # Sonuçlar için placeholder
        texts_to_translate_indices = []  # MT'ye gidecek segment index'leri

        for i, text in enumerate(texts):
            # CORE-14: tekil yoldaki (translate_text) erken-donus guard'larini
            # batch'te de uygula -> salt sayi, '$x$' formul-only ve bos/kisa
            # segmentler 7B modele gitmesin (halusinasyon + bosa maliyet).
            if (not text or not text.strip() or len(text.strip()) < 3
                    or self._is_non_translatable(text)):
                results[i] = text if text else ""
                continue

            # CORE-06: memory esigini tekil yolla ayni tut (config.memory_threshold)
            if (self.memory and self.config.use_memory
                    and getattr(self, "_memory_bypass_on", False) is not True):
                cached, source = self.memory.get_or_similar(text, self.config.memory_threshold)
                if cached and source in ("exact", "similar"):
                    results[i] = cached
                    self.stats["segments_from_memory"] += 1
                    continue

            texts_to_translate_indices.append(i)

        # Hepsi cache'den geldiyse direkt dön
        if not texts_to_translate_indices:
            return results

        # MT'ye gidecek metinleri topla
        texts_for_mt = [texts[i] for i in texts_to_translate_indices]

        # 1. Her segment için koruma işlemlerini yap
        protected_data = []  # [(ner_mapping, glossary_terms, references, formulas, units), ...]
        texts_to_translate = []

        for text in texts_for_mt:
            # NER koruma
            ner_mapping = {}
            if self.ner_protector:
                text, ner_mapping = self.ner_protector.protect(text)

            # Glossary koruma - post modunda çeviriye müdahale yok
            glossary_terms = []
            if self.glossary and self.config.glossary_mode not in ("off", "post"):
                text, glossary_terms = self.glossary.protect_terms(text)
                # CORE-B5/RT-B5: Eski '[Terimler: ...]' soft-hint metin onekI SILINDI.
                # Hint user-text'in basina metin olarak ekleniyordu ve MT echo
                # ederse ciktidan temizleyen hicbir guard yoktu (leak vektoru).
                # Tekil yol zaten hint eklemiyordu -> silme ayrica asimetriyi de
                # kapatir (SOZLESME: iki yol ayni davransin).

            # LaTeX referans koruma
            text, references = self._protect_references(text)

            # Formül ve birim koruma
            text, formulas = self._protect_formulas(text)
            text, units = self._protect_units(text)

            # Sayfa ayraci koruma (2026-07-11: tekil yol ve
            # translate_batch_protected'da vardi, bu yolda eksikti — asimetri)
            text, page_refs = self._protect_page_refs(text)

            protected_data.append((ner_mapping, glossary_terms, references, formulas, units, page_refs))
            texts_to_translate.append(text)

        # 2. Batch MT cevirisi
        translated_texts = self._translate_batch_hymt(texts_to_translate)

        # 3. Her segment için restore işlemlerini yap + stats güncelle + memory'e ekle
        for i, trans_text in enumerate(translated_texts):
            ner_mapping, glossary_terms, references, formulas, units, page_refs = protected_data[i]
            original_idx = texts_to_translate_indices[i]  # Orijinal texts listesindeki index
            original_text = texts[original_idx]

            # Restore sirasi = protect sirasinin TERSI (translate_text ile ayni
            # sozlesme; 2026-07-11'e kadar bu yol NER/glossary'yi formul/ref
            # placeholder'lari daha metindeyken restore ediyordu — ic ice
            # stash durumunda terim kaybi olusuyordu).

            # Sayfa ayraci, formül, birim ve referans restore
            trans_text = self._restore_page_refs(trans_text, page_refs)
            trans_text = self._restore_units(trans_text, units)
            trans_text = self._restore_formulas(trans_text, formulas)
            trans_text = self._restore_references(trans_text, references)

            # Glossary restore
            if glossary_terms and self.glossary:
                trans_text, not_found = self.glossary.restore_terms(trans_text, glossary_terms)
                # Fallback: kayıp terimleri post-processing ile düzelt
                if not_found:
                    trans_text = self._post_process_missing_terms(trans_text, glossary_terms, not_found, original_text)
                # Stats: hard mod terimleri say
                hard_terms = [t for t in glossary_terms if t.get("mode") == "hard"]
                self.stats["terms_enforced"] += len(hard_terms)

            # NER restore
            # CORE-06: tekil yolla ayni -> use_translation=True (kanonik Turkce
            # karsiligi olan ozel isimler batch'te Ingilizce kalmasin).
            if ner_mapping and self.ner_protector:
                trans_text = self.ner_protector.restore(trans_text, ner_mapping, use_translation=True)

            # Doğallaştırma (single translation ile tutarlı)
            if self.naturalizer:
                # Kural tabanlı doğallaştırma
                trans_text = self.naturalizer.naturalize(trans_text)

            # Sonucu doğru index'e yerleştir
            results[original_idx] = trans_text

            # Memory'e ekle - CORE-B05: MT_FAIL/None zehirlemesin
            # CORE-05: kaynak==hedef (echo) durumunda da yazma -- cevrilmemis
            # Ingilizce echo'yu "ceviri" olarak cache'lemek sonraki exact hit'te
            # ayni echo'yu geri dondurur (kalici zehirlenme).
            # F19: tam esitlik + near-echo (noktalama/harf buyuklugu) birlikte
            # F3-07: sınıra dayanan çeviri belleğe yazılmaz (kayıt segmente bağlanır)
            _truncated = BookTranslator._take_truncation(
                self, texts_to_translate[i], original_text, trans_text)
            if self.memory and self.config.use_memory and not _truncated:
                if (trans_text and not trans_text.startswith("[MT_FAIL]")
                        and not is_echo_output(original_text, trans_text)):
                    # FX3 (V2 Y1): ertelenmiş kipte onay bekler.
                    if getattr(self, "_memory_deferred", False) is True:
                        self._memory_hold(original_text, trans_text)
                    else:
                        self.memory.add(original_text, trans_text)

            # Stats: segment çevrildi
            self.stats["segments_translated"] += 1

        # None kontrolu: MT basarisiz olan index'lerde orijinal metni koru
        for i, r in enumerate(results):
            if r is None:
                results[i] = texts[i]  # orijinal metni koru
                if logger:
                    logger.translator(f"Batch segment {i} returned None, keeping original", "WARNING")
                else:
                    logger.translator(f"[BATCH] Segment {i} None döndü, orijinal metin korunuyor", "WARNING")

        return results

    # ==================== KORUMA FONKSİYONLARI ====================

    # LaTeX referans komutlari pattern'i (\ref, \cite, \label, \eqref, \pageref, \cref, \autoref, ...)
    # CORE-B23: Daha fazla referans komutu (citet, citep, fullcite, textcite, nameref, Cref)
    _REF_RE = re.compile(
        r"\\(?i:label|ref|eqref|cite[tp]?|fullcite|textcite|pageref|[cC]ref|autoref|nameref)\s*\{[^}]*\}"
    )

    def _protect_references(self, text: str) -> Tuple[str, List[str]]:
        """LaTeX referans komutlarini (\\ref, \\cite, \\label vb.) koruma altina al."""
        references = []

        def replace_ref(match):
            references.append(match.group(0))
            return f"[[REF_{len(references)-1}]]"

        text = self._REF_RE.sub(replace_ref, text)
        return text, references

    def _restore_references(self, text: str, references: List[str]) -> str:
        """Korunan LaTeX referanslarini geri yerlestir."""
        for i, ref in enumerate(references):
            text = text.replace(f"[[REF_{i}]]", ref)
        return text

    def _protect_formulas(self, text: str) -> Tuple[str, List[str]]:
        """Formülleri koru"""
        formulas = []

        def replace_formula(match):
            formulas.append(match.group(0))
            return f"[[FORMULA_{len(formulas)-1}]]"

        def replace_inline_formula(match):
            content = match.group(0)  # $...$ dahil
            inner = content[1:-1]
            # CORE-09: '$5 million in 2020 and $8 million' gibi para/olcum
            # metinleri iki $ arasinda kaliyor ve formul saniliyordu -> icteki
            # duz metin cevrilmeden Ingilizce kaliyordu. LaTeX matematik isareti
            # (\ ^ _ { }) YOKSA ve icerik rakamla baslayip bosluklu alfabetik
            # kelime (million, dollars, kg...) iceriyorsa formul sayma.
            if not re.search(r'[\\^_{}]', inner):
                stripped = inner.strip()
                if re.match(r'^\d', stripped) and ' ' in stripped and re.search(r'[A-Za-z]{2,}', stripped):
                    return content  # dokunma, formul degil
            formulas.append(content)
            return f"[[FORMULA_{len(formulas)-1}]]"

        # LaTeX formülleri
        text = re.sub(r'\$\$[\s\S]*?\$\$', replace_formula, text)
        text = re.sub(r'\$[^\$\n]+?\$', replace_inline_formula, text)
        text = re.sub(r'\\begin\{[^}]+\}[\s\S]*?\\end\{[^}]+\}', replace_formula, text)
        text = re.sub(r'\\\[[\s\S]*?\\\]', replace_formula, text)
        text = re.sub(r'\\\([\s\S]*?\\\)', replace_formula, text)

        return text, formulas

    # 2026-09-24 (bir test kitabında tablo verisi kaybı): MT, yer tutucunun GÖVDESİNİ
    # çevirebiliyor — [[UNIT_0]] → [[BIRIM_0]] / [[BİRİM_0]] / [[ UNIT 0 ]],
    # [[FORMULA_2]] → [[FORMÜL_2]]. Eskiden restore bunları bulamıyor, üst
    # katman (latex_pipeline) kalıntıyı SİLİYORDU → "km3 −2 s" hücresi "km³ −"
    # oldu. Onarım kaynağında: bozuk gövde, listede karşılığı olan bir
    # indeksi gösteriyorsa kanonik ada çevrilir, sonra normal restore çalışır.
    # Sınıflar açık yazıldı (re.IGNORECASE Türkçe İ/ı ile güvenilmez).
    _UNIT_MANGLED_RE = re.compile(
        r"\[\[\s*(?:UNIT|Unit|unit|B[İI]R[İI]M|B[iı]r[iı]m|b[iı]r[iı]m)"
        r"[_\s]*(\d+)\s*\]\]"
    )
    # FX3 (2026-10-05, bir model testi): HY-MT "[[FORMULA_0]]"ı "[[FORMÜLA_0]]"
    # yaptı; desen "FORMÜL" + rakam beklediği için "A" yüzünden tutmuyordu
    # (kalıntı basıldı, formül yer değiştirdi). Sondaki A/a isteğe bağlı.
    _FORMULA_MANGLED_RE = re.compile(
        r"\[\[\s*(?:FORM[ÜU]LA?|Form[üu]la?|form[üu]la?)"
        r"[_\s]*(\d+)\s*\]\]"
    )

    def _repair_mangled_placeholders(self, text: str, pattern: "re.Pattern[str]",
                                     canonical: str, count: int) -> str:
        """Bozuk ``[[...]]`` gövdesini ``[[<canonical>_n]]``'e çevir (n < count).

        Yalnız listede karşılığı olan indeksler onarılır; kaynakta hiç
        olmayan uydurma indeks olduğu gibi kalır (üst katman karar verir).
        """
        if not count or "[[" not in text:
            return text

        def _fix(m: "re.Match[str]") -> str:
            idx = int(m.group(1))
            fixed = f"[[{canonical}_{idx}]]"
            if idx < count and m.group(0) != fixed:
                self.stats["placeholders_repaired"] = (
                    self.stats.get("placeholders_repaired", 0) + 1
                )
                return fixed
            return m.group(0)

        return pattern.sub(_fix, text)

    def _restore_formulas(self, text: str, formulas: List[str]) -> str:
        text = self._repair_mangled_placeholders(
            text, self._FORMULA_MANGLED_RE, "FORMULA", len(formulas))
        for i, formula in enumerate(formulas):
            text = text.replace(f"[[FORMULA_{i}]]", formula)
        return text

    def _protect_units(self, text: str) -> Tuple[str, List[str]]:
        """Birimleri koru"""
        units = []

        pattern = r'\b(\d+\.?\d*\s*(?:kg|g|mg|m|cm|mm|km|nm|s|ms|Hz|kHz|MHz|GHz|N|kN|J|kJ|MJ|eV|W|kW|MW|Pa|kPa|MPa|K|mol|A|mA|V|kV|Ω|m/s|m/s²|km/h|kg/m³|rad|deg|°C|°F))\b'

        def replace_unit(match):
            units.append(match.group(0))
            return f"[[UNIT_{len(units)-1}]]"

        text = re.sub(pattern, replace_unit, text, flags=re.IGNORECASE)
        return text, units

    def _restore_units(self, text: str, units: List[str]) -> str:
        text = self._repair_mangled_placeholders(
            text, self._UNIT_MANGLED_RE, "UNIT", len(units))
        for i, unit in enumerate(units):
            text = text.replace(f"[[UNIT_{i}]]", unit)
        return text

    def _protect_page_refs(self, text: str) -> Tuple[str, List[str]]:
        """TOC/dizin satırlarındaki ' / 214' sayfa ayraçlarını koru.

        MT modeli 'Required Mass of Pressurizing Gas / 214' kalıbındaki
        ayracı bölme işlemi sanabiliyor ('... 214 ile böl', sutton smoke
        2026-07-06). Ayraç + sayfa numarası stash'lenir, MT'ye gitmez.
        """
        page_refs = []

        def replace_page_ref(match):
            page_refs.append(match.group(0))
            return f"[[PGREF_{len(page_refs)-1}]]"

        text = re.sub(
            r'(?<=\S)\s+/\s+\d{1,4}(?=$|[\s,;.…)\]])',
            replace_page_ref, text,
        )
        return text, page_refs

    def _restore_page_refs(self, text: str, page_refs: List[str]) -> str:
        for i, ref in enumerate(page_refs):
            text = text.replace(f"[[PGREF_{i}]]", ref)
        return text

    # ==================== DOKÜMAN ÇEVİRİSİ ====================

    def _normalize_text(self, text: str) -> str:
        """Metin normalizasyonu - encoding sorunlarını düzelt"""
        if not text:
            return text

        # Soft hyphen kaldır
        text = text.replace('\u00ad', '')

        # CORE-B12: Kiril -> Latin sadece kaynak dil Ingilizce ise
        # (Rusca kaynak metinde Kiril karakterlerini bozmayalim)
        if getattr(self.config, 'source_lang', 'en') == 'en':
            cyrillic_to_latin = {
                'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y',
                'х': 'x', 'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M',
                'Н': 'H', 'О': 'O', 'Р': 'P', 'С': 'C', 'Т': 'T', 'У': 'Y',
                'Х': 'X', 'З': '3', 'з': '3',
            }
            for cyr, lat in cyrillic_to_latin.items():
                text = text.replace(cyr, lat)

        # MT artifact'larını temizle - ama quiz/test kitaplarındaki gerçek [?]'leri koru
        # Sadece etrafında alfanumerik olmayan (izole) [?]'leri temizle
        text = re.sub(r'(?<!\w)\[\?\](?!\w)', '', text)
        text = re.sub(r'(?<!\w)\[ \? \](?!\w)', '', text)
        text = text.replace('[UNK]', '')  # [UNK] her zaman MT artifact
        text = text.replace('<unk>', '')  # <unk> her zaman MT artifact

        return text

    def _dedup_segments(self, segments: List[Dict]) -> List[Dict]:
        """Ardışık tekrar eden segmentleri kaldır.

        CORE-07 NOTU: Bu yardimci artik translate_segments TARAFINDAN
        CAGRILMIYOR (segment silmek 1:1 sozlesmeyi bozuyordu -> zip/checkpoint
        hizasizligi). translate_segments tekrar eden metinleri silmeden ceviri
        onbellegi ile ele alir. Metod geriye-uyum ve bagimsiz kullanim icin
        korunuyor; cagrildiginda halen ardisik tekrarlari kaldirir.
        """
        if not segments:
            return segments

        deduped = []
        prev_normalized = None

        for segment in segments:
            # Normalize et (whitespace temizle)
            text = segment.get("text", "")
            normalized = re.sub(r'\s+', ' ', text).strip().lower()

            # Öncekiyle aynıysa atla
            if normalized == prev_normalized and len(normalized) > 10:
                continue

            deduped.append(segment)
            prev_normalized = normalized

        removed = len(segments) - len(deduped)
        if removed > 0:
            logger.translator(f"[Dedup] {removed} tekrar eden segment kaldırıldı ({len(segments)} -> {len(deduped)})", "INFO")

        return deduped

    def translate_segments(self, segments: List[Dict],
                           progress_callback: Callable = None,
                           run_document_passes: bool = True) -> List[Dict]:
        """
        Segment listesini çevir

        Args:
            segments: [{"text": "...", "type": "...", ...}, ...]
            progress_callback: (progress, message) callback
            run_document_passes: Tutarlılık + glossary-final fazları bu
                çağrının sonunda koşsun mu? EPUB/TXT akışları segmentleri
                20'lik batch'ler hâlinde gönderdiği için False geçer ve
                fazları TÜM doküman bitince bir kez çalıştırır (F41).

        Returns:
            Çevrilmiş segmentler
        """
        if logger:
            logger.step("translate_segments", f"{len(segments)} segment")
        
        # === 0. ÖN İŞLEME: Normalize (CORE-07: 1:1 SOZLESME) ===
        # ESKI: segments = self._dedup_segments(segments) -> ardisik tekrar eden
        # segmentleri SILIYORDU, boylece translate_segments GIRDIDEN AZ segment
        # donduruyordu. Cagiran taraf (_translate_pdf / _translate_epub) donen
        # listeyi ORIJINAL segments ile zip'liyor (QA ciftleri + export sozlugu)
        # ve EPUB checkpoint resume offset'ini (start_index = len(cached_segments))
        # donen uzunluga gore hesapliyordu -> hizasizlik + sessiz icerik kaybi.
        # ARTIK segment DUSURULMEZ. Tekrar eden metinler yalnizca ceviri
        # VERIMLILIGI icin bir kez cevrilir, sonra index map ile TUM pozisyonlara
        # geri dagitilir; cikti uzunlugu HER ZAMAN girdi uzunluguna esittir.

        # Glyph normalize
        for seg in segments:
            if "text" in seg:
                seg["text"] = self._normalize_text(seg["text"])

        translated = []
        total = len(segments)
        segment_scores = []  # QA skorları

        if logger:
            logger.translator(f"Çeviri başlıyor: {total} segment", "INFO")

        # === 1. İLK GEÇİŞ: BATCH ÇEVİRİ (3-4x hızlı) ===
        import time as _time
        batch_size = getattr(self.config, 'batch_size', DEFAULT_BATCH_SIZE_HYMT_LOCAL)

        # Tüm metinleri topla
        all_texts = [seg["text"] for seg in segments]

        # CORE-07: Ceviri onbellegi -- ayni metni iki kez MT'ye SOKMA (verimlilik)
        # ama ciktidan DUSURME. key = whitespace-normalize edilmis (case-sensitive)
        # metin; ayni key'li tum segmentler ayni cevriyi paylasir.
        unique_texts: List[str] = []
        unique_index_of: Dict[str, int] = {}
        seg_to_unique: List[int] = []  # her segment -> unique_texts index
        for t in all_texts:
            key = re.sub(r'\s+', ' ', t).strip() if isinstance(t, str) else t
            u = unique_index_of.get(key)
            if u is None:
                u = len(unique_texts)
                unique_index_of[key] = u
                unique_texts.append(t)
            seg_to_unique.append(u)

        n_unique = len(unique_texts)
        total_batches = (n_unique + batch_size - 1) // batch_size
        logger.translator(
            f"[BATCH] Batch çeviri başlıyor: {total} segment "
            f"({n_unique} benzersiz), {total_batches} batch, batch_size={batch_size}", "STEP")

        # unique_texts uzunlugunda ceviri + basarisizlik takibi
        unique_translated: List[Optional[str]] = [None] * n_unique
        unique_failed = set()

        # Süre takibi
        start_time = _time.time()
        batch_times = []

        # Batch'ler halinde çevir (BENZERSIZ metinler uzerinde)
        for batch_start in range(0, n_unique, batch_size):
            batch_start_time = _time.time()

            batch_end = min(batch_start + batch_size, n_unique)
            batch_texts = unique_texts[batch_start:batch_end]
            batch_num = batch_start // batch_size + 1

            if progress_callback:
                progress = 0.1 + (batch_start / max(n_unique, 1) * 0.7)
                progress_callback(progress, f"Batch {batch_num}/{total_batches} | {total} segment")

            elapsed = _time.time() - start_time

            # Tahmini kalan süre
            if batch_times:
                avg_batch_time = sum(batch_times) / len(batch_times)
                remaining_batches = total_batches - batch_num + 1
                eta = avg_batch_time * remaining_batches
                eta_str = f", ETA: {int(eta//60)}dk {int(eta%60)}sn"
            else:
                eta_str = ""

            logger.translator(f"[BATCH] {batch_num}/{total_batches} işleniyor ({batch_end - batch_start} benzersiz segment) - Geçen: {int(elapsed)}sn{eta_str}", "INFO")

            # Batch çeviri yap (_translate_batch_segments N->N garanti eder)
            try:
                batch_translated = self._translate_batch_segments(batch_texts)
                for k, tr in enumerate(batch_translated):
                    unique_translated[batch_start + k] = tr
            except Exception as e:
                logger.translator(f"[BATCH] Hata, tek tek çeviriye geçiliyor: {e}", "WARNING")
                # Fallback: tek tek çevir
                for k, text in enumerate(batch_texts):
                    pos = batch_start + k
                    try:
                        unique_translated[pos] = self.translate_text(text)
                    except Exception as e2:
                        logger.translator(f"[BATCH] Tek segment çeviri hatası: {e2}", "ERROR")
                        # CORE-08: orijinal (Ingilizce) metin kaliyor -> _mt_failed isaretle
                        unique_failed.add(pos)
                        unique_translated[pos] = text  # orijinal metni koru
                        if logger:
                            logger.translator("Batch tek segment çeviri başarısız, orijinal metin korunuyor", "WARNING")

            # Batch süresini kaydet
            batch_time = _time.time() - batch_start_time
            batch_times.append(batch_time)

        # CORE-07: BENZERSIZ cevirileri TUM pozisyonlara geri dagit -> 1:1 garanti.
        # all_translated uzunlugu HER ZAMAN len(segments)'e esit; hicbir zip site'i
        # (QA / export) veya checkpoint offset'i kaymaz.
        all_translated: List[str] = [None] * total
        # CORE-08: MT basarisiz olup orijinal metinle doldurulan index'ler.
        mt_failed_indices = set()
        for i, u in enumerate(seg_to_unique):
            tr = unique_translated[u]
            if tr is None:
                # Beklenmedik (her batch pozisyonu doldurulur): orijinali koru.
                tr = all_texts[i]
                mt_failed_indices.add(i)
            all_translated[i] = tr
            if u in unique_failed:
                mt_failed_indices.add(i)

        for i, (segment, trans_text) in enumerate(zip(segments, all_translated)):
            new_segment = segment.copy()
            new_segment["text"] = trans_text
            new_segment["original"] = segment["text"]
            if i in mt_failed_indices:
                new_segment["_mt_failed"] = True
                self.stats["segments_failed"] = self.stats.get("segments_failed", 0) + 1
            translated.append(new_segment)

            # Segment QA skoru hesapla
            if self.qa_checker:
                issues = self.qa_checker.check_segment(segment["text"], trans_text, i)
                error_count = sum(1 for iss in issues if iss.severity == "error")
                warning_count = sum(1 for iss in issues if iss.severity == "warning")
                score = max(0, 100 - error_count * 15 - warning_count * 5)
                segment_scores.append(score)
            else:
                segment_scores.append(100)

        total_time = _time.time() - start_time
        avg_per_segment = total_time / len(translated) if translated else 0
        logger.translator(f"[BATCH] Tamamlandı: {len(translated)} segment, {int(total_time//60)}dk {int(total_time%60)}sn ({avg_per_segment:.2f}sn/segment)", "SUCCESS")

        # === 2. LOW-SCORE RETRY KALDIRILDI (A7 / CORE-A7) ===
        # Eski retry dongusu dusuk-skorlu segmentleri "farkli beam_size ile"
        # yeniden ceviriyordu. Ancak beam_size HICBIR backend'de uretime
        # ulasmiyordu (olu parametre zinciri) ve HY-MT decode deterministik
        # (do_sample=False / greedy) oldugundan ayni girdi HER retry'da ayni
        # ciktiyi veriyordu -> new_score == eski skor -> koşul asla saglanmaz.
        # Yani tum retry dongusu, tam inference maliyetiyle calisan bir NO-OP'ti.
        # Gercek varyasyon icin sampling (temperature>0) gerekir; o yoktu, bu
        # yuzden dongu tamamen silindi ("best code is no code"). Bu ayni zamanda
        # CORE-12 (retry'da eksik koruma seti) ve CORE-05'in retry kismini da
        # kokten cozer.

        # === 3-4. DOKÜMAN SEVİYESİ FAZLAR (tutarlılık + glossary-final) ===
        # F41: EPUB/TXT akışında bu fazlar batch başına koşuyordu; analiz
        # penceresi 20 segmenti aşmadığı için "doküman seviyesi" tutarlılık
        # fiilen batch-yerel kalıyordu. Çağıran run_document_passes=False
        # geçerse fazlar tüm doküman bitince bir kez çalıştırılır.
        # Not: cagri BILEREK unbound — bu fazlar eskiden bu govdenin
        # icindeydi; mock'lanmis self ile cagiran mevcut testler/araclar
        # ayni gercek kod yolunda kalsin.
        if run_document_passes:
            translated = BookTranslator._run_document_passes(
                self, translated, progress_callback)

        if progress_callback:
            progress_callback(1.0, "Tamamlandı")

        logger.translator(f"[SEGMENT] translate_segments tamamlandı: {len(translated)} segment döndürülüyor", "SUCCESS")
        return translated

    def _run_document_passes(self, translated: List[Dict],
                             progress_callback: Callable = None) -> List[Dict]:
        """Doküman seviyesi son fazlar: tutarlılık analizi + glossary-final.

        translate_segments'in sonundan ayrıldı (F41): batch'ler hâlinde
        çeviren akışlar (EPUB/TXT) bunu tüm segmentler bittikten sonra TEK
        SEFER çağırır, böylece 'force -> güç' (5. bölüm) / 'force -> kuvvet'
        (20. bölüm) gibi kitap çapı tutarsızlıkları gerçekten görülür.
        """
        # === 1. TUTARLILIK KONTROLÜ ===
        logger.translator(f"[SEGMENT] Tutarlılık kontrolü: check_consistency={self.config.check_consistency}, analyzer={self.consistency_analyzer is not None}", "DEBUG")
        if self.config.check_consistency and self.consistency_analyzer:
            if progress_callback:
                progress_callback(0.92, "Tutarlılık kontrolü yapılıyor...")
            logger.translator("[SEGMENT] Tutarlılık analizi başlıyor...", "DEBUG")

            translated, consistency_report = self.consistency_analyzer.fix_inconsistencies(
                translated,
                auto_fix=self.config.auto_fix_consistency
            )
            self.stats["consistency_fixes"] = consistency_report.auto_fixed
            logger.translator(f"[SEGMENT] Tutarlılık analizi tamamlandı: {consistency_report.auto_fixed} düzeltme", "SUCCESS")

        # === 2. GLOSSARY POST-PROCESS: kalan İngilizce terimleri düzelt ===
        # "post" modunda her zaman çalışır, diğer modlarda da ek güvenlik olarak çalışır
        if self.glossary and self.config.glossary_mode in ("post", "soft", "hard", "hybrid"):
            if progress_callback:
                progress_callback(0.95, "Glossary post-process...")
            translated = self._post_process_glossary_final(translated)

        return translated

    def translate_file(self, input_path: str, output_path: str = None,
                       output_format: str = "same",
                       progress_callback: Callable = None) -> Tuple[str, QAReport]:
        """
        Dosyayı çevir

        Args:
            input_path: Giriş dosyası
            output_path: Çıkış dosyası (None = otomatik)
            output_format: Çıkış formatı (same, markdown, docx, epub)
            progress_callback: İlerleme callback

        Returns:
            (çıkış dosya yolu, QA raporu)
        """
        input_path = Path(input_path)
        suffix = input_path.suffix.lower()

        if not input_path.exists():
            raise FileNotFoundError(f"Dosya bulunamadı: {input_path}")

        # Çıkış yolu
        if output_path is None:
            output_dir = tempfile.mkdtemp()
            if output_format == "same":
                output_path = os.path.join(output_dir, f"{input_path.stem}_TR{suffix}")
            elif output_format == "markdown":
                output_path = os.path.join(output_dir, f"{input_path.stem}_TR.md")
            elif output_format == "docx":
                output_path = os.path.join(output_dir, f"{input_path.stem}_TR.docx")
            elif output_format == "epub":
                output_path = os.path.join(output_dir, f"{input_path.stem}_TR.epub")

        # Format'a göre işle
        if suffix == '.pdf':
            return self._translate_pdf(str(input_path), output_path, output_format, progress_callback)
        elif suffix == '.epub':
            return self._translate_epub(str(input_path), output_path, output_format, progress_callback)
        elif suffix in ['.txt', '.md']:
            return self._translate_text_file(str(input_path), output_path, progress_callback)
        else:
            raise ValueError(f"Desteklenmeyen format: {suffix}")

    def _translate_pdf(self, input_path: str, output_path: str,
                       output_format: str, progress_callback: Callable) -> Tuple[str, QAReport]:
        """PDF çevir"""
        from parsers.pdf_parser import PDFParser
        from exporters.markdown_exporter import MarkdownExporter
        from exporters.docx_exporter import DOCXExporter
        from exporters.epub_exporter import EPUBExporter

        # 1. Parse
        if progress_callback:
            progress_callback(0.05, "PDF okunuyor...")

        parser = PDFParser()
        pdf_doc = parser.parse(input_path)

        # 2. Segmentlere ayır
        if progress_callback:
            progress_callback(0.1, "Metin çıkarılıyor...")

        segments = []
        seen_texts = set()  # Duplicate kontrolü için
        for block in pdf_doc.iter_blocks():
            # Duplicate kontrolü
            text_hash = hash(block.text.strip())
            if text_hash in seen_texts:
                continue  # Duplicate, atla
            seen_texts.add(text_hash)

            segments.append({
                "text": block.text,
                "type": block.block_type,
                "page": block.page_num
            })

        logger.translator(f"[OLD-PATH] {len(segments)} unique segment (duplicate'lar filtrelendi)", "INFO")

        # 3. Çevir
        def translation_progress(p, msg):
            if progress_callback:
                progress_callback(0.1 + p * 0.8, msg)

        translated_segments = self.translate_segments(segments, translation_progress)

        # 4. QA
        qa_report = None
        if self.qa_checker:
            if progress_callback:
                progress_callback(0.92, "Kalite kontrolü...")

            qa_pairs = [(s["text"], t["text"]) for s, t in zip(segments, translated_segments)]
            qa_report = self.qa_checker.check_document(qa_pairs)

        # 5. Export
        if progress_callback:
            progress_callback(0.95, "Dosya oluşturuluyor...")

        # PDF - Orijinal layout korunarak çeviri
        if output_format == "pdf":
            from exporters.pdf_exporter import PDFExporter
            exporter = PDFExporter()

            # Çeviri sözlüğü oluştur {orijinal: çeviri}
            translations = {}
            for orig, trans in zip(segments, translated_segments):
                orig_text = orig.get("text", "").strip()
                trans_text = trans.get("text", "").strip()
                if orig_text and trans_text:
                    translations[orig_text] = trans_text

            # Orijinal PDF üzerine çeviri yaz
            exporter.export(input_path, translations, output_path)

        # Same = Yeni layout PDF
        elif output_format == "same":
            from exporters.pdf_exporter import PDFExporter
            exporter = PDFExporter()
            exporter.export_from_segments(translated_segments, output_path, title=pdf_doc.title)
        elif output_format == "markdown":
            exporter = MarkdownExporter()
            exporter.export(translated_segments, output_path, title=pdf_doc.title)
        elif output_format == "docx":
            exporter = DOCXExporter()
            exporter.export(translated_segments, output_path, title=pdf_doc.title)
        elif output_format == "epub":
            exporter = EPUBExporter()
            exporter.export(translated_segments, output_path, title=pdf_doc.title)

        if progress_callback:
            progress_callback(1.0, "Tamamlandı!")

        return output_path, qa_report

    def _translate_epub(self, input_path: str, output_path: str,
                        output_format: str, progress_callback: Callable) -> Tuple[str, QAReport]:
        """EPUB çevir - checkpoint destekli"""
        # print(f"[EPUB-DEBUG] ========== _translate_epub BAŞLADI ==========", flush=True)
        # print(f"[EPUB-DEBUG] input={input_path}, output={output_path}, format={output_format}", flush=True)

        from parsers.epub_parser import EPUBParser
        from exporters.markdown_exporter import MarkdownExporter
        from exporters.docx_exporter import DOCXExporter
        from exporters.epub_exporter import EPUBExporter

        # 1. Parse
        if progress_callback:
            progress_callback(0.05, "EPUB okunuyor...")

        parser = EPUBParser()
        epub_doc = parser.parse(input_path)

        # 2. Segmentlere ayır
        if progress_callback:
            progress_callback(0.1, "Bölümler çıkarılıyor...")

        segments = parser.extract_segments(epub_doc)
        # print(f"[EPUB-DEBUG] {len(segments)} segment çıkarıldı", flush=True)

        # 3. Checkpoint kontrolü - kaldığı yerden devam
        checkpoint = self._load_text_checkpoint(input_path, "epub")
        start_index = 0
        translated_segments = []

        if checkpoint and checkpoint.get("translated_segments"):
            cached_segments = checkpoint["translated_segments"]
            # Segment sayısı eşleşiyor mu?
            if checkpoint.get("total_segments") == len(segments):
                start_index = len(cached_segments)
                translated_segments = cached_segments
                logger.translator(f"[EPUB-CHECKPOINT] Kaldığı yerden devam: {start_index}/{len(segments)} segment", "INFO")
                if progress_callback:
                    progress_callback(0.1 + (start_index / len(segments) * 0.8), f"Checkpoint'ten devam: {start_index}/{len(segments)}")
            else:
                logger.translator("[EPUB-CHECKPOINT] Segment sayısı değişmiş, baştan başlanıyor", "WARNING")

        # 4. Çevir (kaldığı yerden)
        if start_index < len(segments):
            remaining_segments = segments[start_index:]

            # Batch çeviri - her batch'ten sonra checkpoint kaydet
            batch_size = getattr(self.config, 'batch_size', DEFAULT_BATCH_SIZE_HYMT_LOCAL)
            checkpoint_interval = 5  # Her 5 batch'te bir checkpoint
            total_batches = (len(remaining_segments) + batch_size - 1) // batch_size
            total_segments = len(segments)
            # print(f"[EPUB-BATCH] Toplam {total_batches} batch işlenecek ({len(remaining_segments)} segment, batch_size={batch_size})", flush=True)

            for batch_idx, i in enumerate(range(0, len(remaining_segments), batch_size)):
                batch = remaining_segments[i:i + batch_size]
                current_translated = start_index + i + len(batch)

                # Progress callback - toplam segment bilgisi ile
                def batch_progress(p, msg):
                    if progress_callback:
                        total_progress = (start_index + i + p * len(batch)) / total_segments
                        # Mesaja toplam segment bilgisi ekle
                        progress_callback(0.1 + total_progress * 0.8, f"Segment {int(start_index + i + p * len(batch))}/{total_segments}")

                # print(f"[EPUB-BATCH] Batch {batch_idx+1}/{total_batches} başlıyor ({len(batch)} segment)", flush=True)
                # F41: doküman fazları batch başına DEĞİL, döngü bitince
                # tüm segmentler üzerinde bir kez koşar.
                batch_translated = self.translate_segments(
                    batch, batch_progress, run_document_passes=False)
                translated_segments.extend(batch_translated)
                # print(f"[EPUB-BATCH] Batch {batch_idx+1}/{total_batches} tamamlandı, toplam çevrilen: {len(translated_segments)}", flush=True)

                # Her batch sonunda ana progress'i güncelle ve son çevrilen metni gönder
                if progress_callback:
                    # Son çevrilen metni al (varsa)
                    last_text = ""
                    if batch_translated:
                        last_item = batch_translated[-1]
                        if isinstance(last_item, dict):
                            last_text = last_item.get("text", "")[:200]  # Max 200 char
                        elif isinstance(last_item, str):
                            last_text = last_item[:200]
                    # Format: "Segment X/Y|||çevrilen metin"
                    progress_callback(0.1 + (len(translated_segments) / total_segments) * 0.8, f"Segment {len(translated_segments)}/{total_segments}|||{last_text}")

                # Checkpoint kaydet (her 5 batch'te veya son batch)
                if (batch_idx + 1) % checkpoint_interval == 0 or i + batch_size >= len(remaining_segments):
                    self._save_text_checkpoint(
                        input_path, "epub",
                        translated_segments, len(segments),
                        {"output_path": output_path, "output_format": output_format}
                    )

        # print(f"[EPUB-DEBUG] ✅ Çeviri tamamlandı: {len(translated_segments)} segment", flush=True)

        # 3.5 Doküman seviyesi fazlar — TÜM segmentler üzerinde tek sefer (F41).
        # Checkpoint'ten gelen (önceki koşuda çevrilmiş) segmentler de dahil.
        translated_segments = BookTranslator._run_document_passes(
            self, translated_segments, progress_callback)

        # 4. QA
        qa_report = None
        # print(f"[EPUB-DEBUG] QA başlıyor... qa_checker={self.qa_checker is not None}", flush=True)
        if self.qa_checker:
            if progress_callback:
                progress_callback(0.92, "Kalite kontrolü...")

            qa_pairs = [(s["text"], t["text"]) for s, t in zip(segments, translated_segments)]
            # print(f"[EPUB-DEBUG] QA pairs oluşturuldu: {len(qa_pairs)}", flush=True)
            qa_report = self.qa_checker.check_document(qa_pairs)
            # print(f"[EPUB-DEBUG] ✅ QA tamamlandı", flush=True)

        # 5. Export
        # print(f"[EPUB-DEBUG] Export başlıyor... format={output_format}", flush=True)
        if progress_callback:
            progress_callback(0.95, "Dosya oluşturuluyor...")

        if output_format == "markdown":
            exporter = MarkdownExporter()
            exporter.export(translated_segments, output_path,
                           title=epub_doc.title, author=epub_doc.author)
        elif output_format == "docx":
            exporter = DOCXExporter()
            exporter.export(translated_segments, output_path,
                           title=epub_doc.title, author=epub_doc.author)
        elif output_format in ["epub", "same"]:
            exporter = EPUBExporter()
            exporter.export(translated_segments, output_path,
                           title=epub_doc.title, author=epub_doc.author)

        # print(f"[EPUB-DEBUG] ✅ Export tamamlandı: {output_path}", flush=True)

        # 6. Başarılı - checkpoint temizle
        self._delete_text_checkpoint(input_path, "epub")

        if progress_callback:
            progress_callback(1.0, "Tamamlandı!")

        # print(f"[EPUB-DEBUG] ✅ EPUB çeviri fonksiyonu tamamlandı", flush=True)
        return output_path, qa_report

    def _translate_text_file(self, input_path: str, output_path: str,
                             progress_callback: Callable) -> Tuple[str, QAReport]:
        """Metin dosyası çevir - checkpoint destekli"""
        # 1. Oku
        if progress_callback:
            progress_callback(0.1, "Dosya okunuyor...")

        with open(input_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 2. Paragraflara böl; cümleler paragraf İÇİNDE bölünür (#26).
        # Eskiden dosyanın tamamı cümlelere bölünüp '\n\n'.join ile
        # yazılıyordu: her cümle ayrı paragraf oluyor, kaynak paragraf
        # yapısı tamamen kayboluyordu.
        paragraphs = content.split("\n\n")
        segments = []
        para_index = []  # her segmentin ait olduğu paragraf sırası
        for p_idx, para in enumerate(paragraphs):
            if not para.strip():
                continue
            for s in self.splitter.split(para):
                segments.append({"text": s, "type": "paragraph"})
                para_index.append(p_idx)

        # 3. Checkpoint kontrolü - kaldığı yerden devam
        checkpoint = self._load_text_checkpoint(input_path, "txt")
        start_index = 0
        translated_segments = []

        if checkpoint and checkpoint.get("translated_segments"):
            cached_segments = checkpoint["translated_segments"]
            if checkpoint.get("total_segments") == len(segments):
                start_index = len(cached_segments)
                translated_segments = cached_segments
                logger.translator(f"[TXT-CHECKPOINT] Kaldığı yerden devam: {start_index}/{len(segments)} segment", "INFO")
                if progress_callback:
                    progress_callback(0.1 + (start_index / len(segments) * 0.8), f"Checkpoint'ten devam: {start_index}/{len(segments)}")
            else:
                logger.translator("[TXT-CHECKPOINT] Segment sayısı değişmiş, baştan başlanıyor", "WARNING")

        # 4. Çevir (kaldığı yerden)
        if start_index < len(segments):
            remaining_segments = segments[start_index:]

            def translation_progress(p, msg):
                if progress_callback:
                    total_progress = (start_index + p * len(remaining_segments)) / len(segments)
                    progress_callback(0.1 + total_progress * 0.8, msg)

            # Batch çeviri - her batch'ten sonra checkpoint kaydet
            batch_size = getattr(self.config, 'batch_size', DEFAULT_BATCH_SIZE_HYMT_LOCAL)
            checkpoint_interval = 5

            for batch_idx, i in enumerate(range(0, len(remaining_segments), batch_size)):
                batch = remaining_segments[i:i + batch_size]
                # F41: doküman fazları batch başına DEĞİL, döngü sonunda
                batch_translated = self.translate_segments(
                    batch, translation_progress, run_document_passes=False)
                translated_segments.extend(batch_translated)

                # Checkpoint kaydet
                if (batch_idx + 1) % checkpoint_interval == 0 or i + batch_size >= len(remaining_segments):
                    self._save_text_checkpoint(
                        input_path, "txt",
                        translated_segments, len(segments),
                        {"output_path": output_path}
                    )

        # 3.5 Doküman seviyesi fazlar — TÜM segmentler üzerinde tek sefer (F41)
        translated_segments = BookTranslator._run_document_passes(
            self, translated_segments, progress_callback)

        # 4. QA
        qa_report = None
        if self.qa_checker:
            qa_pairs = [(s["text"], t["text"]) for s, t in zip(segments, translated_segments)]
            qa_report = self.qa_checker.check_document(qa_pairs)

        # 5. Kaydet
        if progress_callback:
            progress_callback(0.95, "Kaydediliyor...")

        # Paragraf yapısını geri kur (#26): aynı paragrafın cümleleri
        # boşlukla birleşir, paragraflar arası çift satır korunur.
        out_paras = {}
        for seg, p_idx in zip(translated_segments, para_index):
            out_paras.setdefault(p_idx, []).append(seg["text"])
        translated_text = "\n\n".join(
            " ".join(out_paras[p]) for p in sorted(out_paras)
        )
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(translated_text)

        # 6. Başarılı - checkpoint temizle
        self._delete_text_checkpoint(input_path, "txt")

        if progress_callback:
            progress_callback(1.0, "Tamamlandı!")

        return output_path, qa_report

    # ==================== YARDIMCI ====================

    def _post_process_missing_terms(self, translated: str, protected_terms: List[dict],
                                     not_found: List[int], original: str) -> str:
        """
        Codex C çözümü - Fallback: Tag kaybolmuşsa post-processing ile düzelt

        Strateji:
        1. Orijinal metindeki İngilizce terimi çeviride ara
        2. Bulunursa Türkçe karşılığıyla değiştir
        3. Bulunamazsa olduğu gibi bırak (model zaten doğru çevirmiş olabilir)
        """
        result = translated

        for idx in not_found:
            # Bu index'e ait terimi bul
            term_item = None
            for item in protected_terms:
                if item['index'] == idx and item.get('mode') == 'hard':
                    term_item = item
                    break

            if not term_item:
                continue

            term_en = term_item['term_en']
            term_tr = term_item['term_tr']
            keep_original = term_item.get('keep_original', False)

            replacement = term_en if keep_original else term_tr

            # 1. İngilizce terim çeviride kalmış mı? (model kormuş olabilir)
            # CORE-B04: word boundary ile "cell" -> "acceleration" icinde match etmesin
            term_pattern = re.compile(r'\b' + re.escape(term_en) + r'\b', re.IGNORECASE)
            if term_pattern.search(result):
                # Case-insensitive değiştir
                result = term_pattern.sub(replacement, result, count=1)
                if logger:
                    logger.translator(f"[POST-PROCESS] '{term_en}' -> '{replacement}' (İngilizce bulundu)", "DEBUG")
                continue

            # 2. Terim zaten doğru Türkçe karşılığıyla mı var?
            if term_tr.lower() in result.lower():
                # Zaten doğru, bir şey yapma
                if logger:
                    logger.translator(f"[POST-PROCESS] '{term_en}' zaten '{term_tr}' olarak mevcut", "DEBUG")
                continue

            # 3. Bulunamadı - sadece orijinal metni yeniden çevir, prompt gönderme
            # HY-MT bir çeviri modeli, talimat modeli değil. Prompt göndermek = prompt leak.
            if logger:
                logger.translator(f"[POST-PROCESS] '{term_en}' bulunamadı, re-translate deneniyor", "WARNING")

            try:
                # Sadece orijinal metni tekrar çevir (prompt yok, prompt leak yok)
                strict_result = self._translate_with_mt(original)

                # Sonra terimi post-process ile zorla
                if strict_result and term_en and term_tr:
                    # CORE-B04: word boundary ile partial match'leri engelle
                    strict_result = re.sub(
                        r'\b' + re.escape(term_en) + r'\b', replacement, strict_result, flags=re.IGNORECASE
                    )

                # Sonuçta terim var mı kontrol et
                if strict_result and (term_tr.lower() in strict_result.lower() or
                                       (keep_original and term_en.lower() in strict_result.lower())):
                    result = strict_result
                    if logger:
                        logger.translator(f"[POST-PROCESS] Re-translate basarili: '{term_en}' -> '{replacement}'", "INFO")
                else:
                    if logger:
                        logger.translator(f"[POST-PROCESS] Re-translate'de de terim bulunamadi", "WARNING")
            except Exception as e:
                if logger:
                    logger.translator(f"[POST-PROCESS] Re-translate hatasi: {e}", "ERROR")

        return result

    def _post_process_glossary_final(self, translated_segments: List[Dict]) -> List[Dict]:
        """
        Çeviri tamamlandıktan sonra glossary terimlerini düzelt.

        Yeni yaklaşım: Çeviri sırasında placeholder kullanmak yerine,
        tüm çeviri bittikten sonra kalan İngilizce terimleri glossary'den düzelt.

        Bu sayede:
        - Model özgürce çeviri yapar
        - Placeholder karmaşası olmaz
        - Sadece gerçekten çevrilmemiş terimler düzeltilir
        """
        if not self.glossary or not translated_segments:
            return translated_segments

        if self.config.glossary_mode == "off":
            return translated_segments

        fixed_count = 0
        total_terms_found = 0

        logger.translator(f"[GLOSSARY-FINAL] Post-process başlıyor: {len(translated_segments)} segment", "DEBUG")

        # F29 (2026-07-29): terimler UZUNDAN KISAYA uygulanir. Ham dict
        # ekleme sirasi JSON sirasiydi ve curated'da 'bias' (idx 34) 'bias
        # vector'dan (idx 38) once geliyordu -> 'bias vector' iceren segment
        # once 'önyargı vector' oluyor, cok kelimeli terim bir daha
        # eslesmiyordu. Eslesme semantigi ayni; yalnizca SIRA degisti.
        prepared = []
        for term_lower, term_data in self.glossary.terms.items():
            term_en = term_data.get("en", term_lower) or term_lower
            prepared.append((len(term_en), term_en, _glossary_scan_key(term_en),
                             term_data))
        prepared.sort(key=lambda x: x[0], reverse=True)

        for segment in translated_segments:
            # CORE-B02: MT basarisiz olmus segmentleri atla (orijinal metin kaldi)
            if segment.get("_mt_failed"):
                continue

            text = segment.get("text", "")
            if not text:
                continue

            original_text = text
            scan_text = _glossary_scan_key(text)

            # Glossary'deki tüm terimleri kontrol et
            for _len, term_en, term_key, term_data in prepared:
                # F21: ucuz on-filtre — alt-dize yoksa regex hic calismaz.
                # Genis kume (yanlis-pozitif olabilir), karari desen verir.
                if term_key not in scan_text:
                    continue

                term_tr = term_data.get("tr", term_en)
                keep_original = term_data.get("keep_original", False)

                # CORE-01: kelime siniri (\b) ile substring ikamesini engelle.
                # \b olmadan 'fin'->'kanatcik', 'labor'->'iscilik' gibi Turkce
                # kelimelerin ICINE yaziliyordu ('finansal'->'kanatciksal').
                # CORE-B04'un birebir reuse'u (o da :2302'de \b kullaniyor).
                # F21: desen artik SEGMENT dongusunde derlenmiyor (kalici cache).
                term_pattern = _glossary_term_pattern(term_en)
                if term_pattern.search(text):
                    total_terms_found += 1

                    # Değiştir: İngilizce → Türkçe (veya orijinal kalacaksa dokunma)
                    if not keep_original:
                        new_text = term_pattern.sub(term_tr, text, count=1)

                        if new_text != text:
                            text = new_text
                            scan_text = _glossary_scan_key(text)
                            fixed_count += 1
                            if logger:
                                logger.translator(f"[GLOSSARY-FINAL] '{term_en}' → '{term_tr}'", "DEBUG")

            # Güncelle
            if text != original_text:
                segment["text"] = text

        if fixed_count > 0 or total_terms_found > 0:
            logger.translator(f"[GLOSSARY-FINAL] Tamamlandı: {total_terms_found} terim bulundu, {fixed_count} düzeltildi", "SUCCESS")

        return translated_segments

    def get_stats(self) -> Dict:
        """İstatistikleri getir"""
        self._sync_think_stats()
        stats = self.stats.copy()

        # MT hata sayısını ekle
        if hasattr(self, '_mt_error_count'):
            stats["mt_errors"] = self._mt_error_count

        if self.memory:
            stats["memory"] = self.memory.get_stats()

        if self.glossary:
            stats["glossary"] = self.glossary.get_stats()

        stats["mt_engine"] = self.config.mt_engine
        # Arka uçta hangi sunucu/modelin çevirdiği (MLX yolunda eski değer)
        backend = _active_backend(self)
        stats["mt_model"] = backend.describe() if backend is not None else self.config.hymt_7b_model

        return stats

    def add_custom_terms(self, terms: Dict[str, str]):
        """Özel terim ekle"""
        if self.glossary:
            self.glossary.add_terms_from_dict(terms)

    def export_memory(self, output_path: str):
        """Çeviri belleğini dışa aktar"""
        if self.memory:
            self.memory.export_to_json(output_path)

    def import_memory(self, input_path: str):
        """Çeviri belleğini içe aktar"""
        if self.memory:
            self.memory.import_from_json(input_path)
