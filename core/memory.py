"""
Çeviri Belleği (Translation Memory)
Daha önce çevrilmiş cümleleri saklar ve yeniden kullanır
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from rapidfuzz import fuzz

# Varsayılan bellek dosyasının yolu veri kökünden türer (TASARIM F1):
# KITAPCEVIR_VERI_KOKU varsa <kök>/data/..., yoksa proje kökündeki data/.
from config.paths import bellek_yolu

# Stash placeholder pattern: <<TYPE_N>>, @@TERMN@@, [[TYPE_N]] formatindaki tum placeholder'lar
# Bunlar cache'e YAZILMAMALI ve fuzzy match sonuclarindan FILTRELENMELI
# CORE-B08: Yeni format @@TERM0000@@ ve [[REF_0]] ayni sekilde bloklanmali
# 2026-07-06 (sutton smoke): MT, NER placeholder'inin sozcuk kismini
# cevirebiliyor (@@UNKNOWN0000@@ → MİYOR0000@@ / isaretsiz MİYOR0000).
# Bozuk kalintilar da cache'i zehirliyordu — genisletildi.
# 2026-07-11: [[UNIT_0]] gibi placeholder'larin ic sozcugunu de MT
# cevirebiliyor ([[BİRİM_0]] — DB'de 16 canli ornek). [A-Z] ASCII siniri
# Turkce harfleri kacirdigi icin [[...]] kolu Unicode harf sinifina genisletildi.
_PLACEHOLDER_RE = re.compile(
    r"<<[A-Z]+_\d+>>|@@TERM\d+@@|\[\[[^\W\d]{1,24}_\d+\]\]"
    r"|@@[^\s@]{0,24}?\d{1,6}@@"          # @@<bozuk-sozcuk>0001@@ (tam sarili)
    r"|[^\W\d_]{2,24}\d{4}@@"             # MİYOR0001@@ (bas @@ dusmus)
    r"|@@[^\W\d_]{2,24}\d{4}(?!\d)"       # @@MİYOR0001 (son @@ dusmus)
    r"|\b(?:UNKNOWN|B[İI]L[İI]NMEYEN|B[İI]L[İI]NM[İI]YOR|M[İI]YOR)"
    r"[-_]?\d{4}(?!\d)\b"                 # isaretsiz kalinti (MİYOR0000)
    # 2026-07-16 (#18): PlaceholderManager'in {{vN}} formati (v2 fallback
    # yolu translate_text'e protected metin gonderiyor) ve tek-brace'e
    # bozulmus hali — TM'yi zehirliyordu.
    r"|\{\{\s*v\s*\d+\s*\}\}"             # {{v0}}, {{ v 0 }}
    r"|\{\s*v\d+\s*\}"                    # {v0} (bozuk tek brace)
)

# Echo zehiri tespiti: kaynak==hedef VE kaynak cevrilebilir Ingilizce gorunuyorsa
# bu bir "ceviri" degil MT echo'sudur. Yayinci satiri / ozel isim / sayi gibi
# mesru birebir kayitlari ayirmak icin Ingilizce stopword araniyor.
_ENGLISH_STOPWORD_RE = re.compile(
    r"\b(?:the|and|of|to|in|is|are|was|were|for|with|that|this|from|by|at|as|it|be|or|an|a|on|not|but|which|when)\b",
    re.IGNORECASE,
)


_TURKISH_CHARS_RE = re.compile(r"[çğıöşüÇĞİÖŞÜ]")
_ALPHA_WORD_RE = re.compile(r"[A-Za-z]{3,}")

# Near-echo (2026-07-29, F19): tam esitlik disinda kalan "MT hic cevirmemis"
# vakalari icin esik. Normalize edilmis metinler esit degilse hedef HALA
# Ingilizce gorunumundeyken bu orandan yuksek benzerlik zehir sayilir.
_NEAR_ECHO_MIN_RATIO = 95

_NON_WORD_RE = re.compile(r"[\W_]+")
_DIGITS_RE = re.compile(r"\d+")

# find_similar satir-turev cache'i (id -> (source_text, lower, rakamlar)).
# Ust sinir asilinca tumden bosaltilir; amac uzun kosuda sinirsiz buyumeyi
# engellemek, isabet orani zaten aday penceresi (max_candidates) kadar.
_ROW_CACHE_MAX_ENTRIES = 20000


def _normalize_echo(text: str) -> str:
    """Echo karsilastirmasi icin sadelestir: noktalama/bosluk/harf buyuklugu.

    'The force acts.' ve 'The force acts' ayni metne iner; sayilar ve
    Turkce harfler korunur (yalnizca noktalama/alt tire sadelesir).
    """
    return _NON_WORD_RE.sub(" ", text or "").strip().casefold()


def _is_poison_echo(source: str, target: str) -> bool:
    """Hedef, kaynagin (yakin) echo'su ve kaynak Ingilizce prose ise True.

    Boyle bir kayit exact-hit'te ayni Ingilizce'yi geri dondurup
    retranslator'in duzeltme sansini kalici olarak kapatir (2026-07-11
    denetimi: DB'de 141 echo kaydi, 'The Anchor Escapement',
    'Kernel Regression' vb. TOC basliklari dahil).

    2026-07-29 (F19): tam esitlik sarti near-echo'yu kaciriyordu — MT'nin
    noktayi dusurmesi ya da ilk harfi kucultmesi zehri kalici cache'liyordu.
    Artik iki kapi var: (a) noktalama/harf-buyuklugu normalize edilmis
    esitlik, (b) hedef hala Ingilizce gorunumdeyken (Turkce harf YOK +
    Ingilizce stopword VAR) kaynakla >%95 benzerlik.

    Siniflandirma bilerek genis: yanlis-pozitifin maliyeti yalnizca cache
    miss (yeniden ceviri), yanlis-negatifin maliyeti kalici Ingilizce cikti.
    Kural: stopword iceriyorsa YA DA (Turkce karakter yok VE >=2 alfabetik
    kelime) -> zehir. Tek-token URL/isim ve zaten-Turkce segmentler kalir.
    """
    s = (source or "").strip()
    t = (target or "").strip()
    if not s or not t:
        return False
    # URL/DOI hicbir zaman cevrilmez — birebir kalmasi dogru sonuctur
    if "://" in s or s.lower().startswith(("www.", "doi.org", "doi:")):
        return False

    if s != t:
        ns = _normalize_echo(s)
        if not ns or ns != _normalize_echo(t):
            # Normalize esitlik yok: yalnizca hedef HALA Ingilizce
            # gorunumdeyken ve kaynakla neredeyse ayniyken zehir say.
            # (Gercek Turkce ceviri bu uc kosulu birden saglamaz.)
            if _TURKISH_CHARS_RE.search(t):
                return False
            if not _ENGLISH_STOPWORD_RE.search(t):
                return False
            if fuzz.ratio(s.casefold(), t.casefold()) <= _NEAR_ECHO_MIN_RATIO:
                return False

    if _ENGLISH_STOPWORD_RE.search(s):
        return True
    if _TURKISH_CHARS_RE.search(s):
        return False
    return len(_ALPHA_WORD_RE.findall(s)) >= 2


def is_echo_output(source: str, target: str) -> bool:
    """MT ciktisi kaynagin echo'su mu? (cagiran-taraf guard'i, F19)

    translate_text/translate_batch_protected memory.add'i cagirmadan ONCE
    bunu sorar; add()'in kendi zehir guard'i son savunma hattidir. Tam
    esitlik VE noktalama/harf-buyuklugu farkli near-echo burada da kesilir.
    """
    s = (source or "").strip()
    t = (target or "").strip()
    if not s or not t:
        return False
    if s == t:
        return True
    ns = _normalize_echo(s)
    if ns and ns == _normalize_echo(t):
        return True
    return _is_poison_echo(source, target)


def is_untranslated_output(target: str, source_lang: Optional[str]) -> bool:
    """Çıktı hâlâ büyük ölçüde kaynak dilde mi (yarım çeviri)?

    2026-09-29 (bug avı F3-06): echo guard'ı çıktıda tek bir Türkçe harf
    görünce kaydı zehir saymıyordu; çoğu kaynak dilde kalmış yarım çeviri
    ("... to reach the higher yörünge safely.") belleğe yazılıyor, Phase 5.7
    yeniden çevirisi aynı yarımı bellekten "exact" alıp satırı düzeltemiyordu.
    Karar, yeniden çevirinin kendi dedektörüyle AYNIDIR
    (``latex_pipeline.retranslator.is_untranslated_line``; İngilizcede
    ``is_english_line`` aynen): yeniden çevirinin "çevrilmemiş" dediği metin
    bellekte "çeviri" sayılmaz. Dedektör yüklenemez ya da hata verirse
    False (bugünkü davranış).
    """
    if not target or not target.strip():
        return False
    try:
        from latex_pipeline.retranslator import is_untranslated_line
    except Exception:   # modül yüklenemezse bellek eski kurallarla çalışır
        return False
    try:
        return bool(is_untranslated_line(target, source_lang or "en"))
    except Exception:
        return False


# Eşdeğer kaynak anahtarı (2026-09-29, bug avı F3-05): sözcükler (rakamlar
# dahil) ve noktalama AYNI sırada, yalnız boşluk ve harf büyüklüğü serbest.
# casefold değil lower: casefold Almanca "Maße" (ölçüler) ile "Masse" (kütle)
# gibi farklı sözcükleri eşitlerdi.
_EQUIV_TOKEN_RE = re.compile(r"[^\W_]+|[^\w\s]|_")
# find_equivalent SQL ön süzgeci: boşluksuz ve ASCII küçük harfli kaynak
# (SQLite LOWER yalnız ASCII'yi küçültür) birebir eşit olmalı; bu eşdeğerliğin
# GEREKLİ koşuludur, kesin karar Python'daki sözcük/noktalama anahtarıdır.
# Uzunluk penceresi [n, 2n + 16] (n: boşluksuz uzunluk) yalnız taramayı
# daraltır; üst sınır bol boşluğa pay bırakır. Algoritma içi sabit (ayar değil).
_EQUIV_WS_CHARS = " \n\t\r"
_EQUIV_LEN_MAX_FACTOR = 2
_EQUIV_LEN_MAX_SLACK = 16


def _equivalence_prefilter(text: str) -> str:
    """SQL ön süzgecinin Python eşi: boşluk atılır, yalnız ASCII A-Z küçülür."""
    return "".join(ch.lower() if "A" <= ch <= "Z" else ch
                   for ch in (text or "") if ch not in _EQUIV_WS_CHARS)


def memory_equivalence_key(text: str) -> str:
    """Anlamı değişmeden yalnız boşluk/harf büyüklüğüyle ayrışan metinlerin ortak anahtarı."""
    return " ".join(_EQUIV_TOKEN_RE.findall((text or "").lower()))


# Bellek havuzu anahtarı (tek biçim, tek yardımcı: ``memory_pool_key``):
#     <kaçışlı proje adı>[#<kaynak dil>][@<motor kimliği>]
# KARARLAR 8 (2026-09-25) motor eki, K5 (2026-09-28) dil eki. 2026-09-29
# (bug avı F3-15/F4-12, K1-5): proje adı (kitap adı; translate_book dosya
# adından slug'lamadan alır) ayraç karakterleri içerebildiği için anahtar
# ayrıştırılamıyordu: ("Notlar#de", en, motor) ile ("Notlar", de, motor) ve
# ("Kitap@ollama:qwen3:8b", en, hymt-7b) ile ("Kitap", en, ollama:qwen3:8b)
# AYNI havuza düşüp birbirinin kayıtlarını tam eşleşmeyle geri alıyordu.
# Artık proje adındaki '%', '#', '@' yüzde kodlamasıyla kaçışlanır; kaçışlı
# adda ham ayraç bulunmadığı için ilk '#' dil ekini, ilk '@' motor ekini
# başlatır (``parse_memory_pool_key``). Ayraç içermeyen adın anahtarı
# (varsayılan motor + İngilizce -> ad AYNEN; mevcut 1423 HY-MT kaydı
# 'default' havuzunda) değişmez.
MEMORY_ENGINE_SEP = "@"
MEMORY_LANG_SEP = "#"
_POOL_NAME_ESCAPES = {"%": "%25", MEMORY_LANG_SEP: "%23", MEMORY_ENGINE_SEP: "%40"}
_POOL_NAME_UNESCAPE_RE = re.compile(r"%(25|23|40)")
_POOL_NAME_UNESCAPES = {"25": "%", "23": MEMORY_LANG_SEP, "40": MEMORY_ENGINE_SEP}


def _escape_pool_name(name: str) -> str:
    return "".join(_POOL_NAME_ESCAPES.get(ch, ch) for ch in name)


def memory_pool_key(project_name: Optional[str], source_lang: Optional[str] = None,
                    engine_id: Optional[str] = None) -> str:
    """Çeviri belleği havuz anahtarı — TEK yardımcı (proje, kaynak dil, motor).

    * Proje adı yoksa ``"default"``; adın içindeki ``%``/``#``/``@``
      kaçışlanır (``%25``/``%23``/``%40``).
    * Kaynak dil İngilizce değilse ``"#<dil>"`` (K5, KARARLAR 14): aynı kitap
      yanlış dille çevrilip doğru dille yeniden koşulursa eski çeviriler
      dönmesin.
    * Motor varsayılan değilse ``"@<motor kimliği>"`` (KARARLAR 8): her model
      yalnız kendi kayıtlarını görür. Varsayılan motor
      (``registry.DEFAULT_ENGINE``, çağrı anında okunur) ek almaz.
    """
    key = _escape_pool_name(project_name or "default")
    source = (source_lang or "en").strip() or "en"
    if source != "en":
        key += f"{MEMORY_LANG_SEP}{source}"
    engine = engine_id.strip() if isinstance(engine_id, str) else ""
    if engine:
        from core.mt_engines import registry   # çağrı anında: tek kaynak DEFAULT_ENGINE
        if engine != registry.DEFAULT_ENGINE:
            key += f"{MEMORY_ENGINE_SEP}{engine}"
    return key


def parse_memory_pool_key(key: str) -> Tuple[str, str, Optional[str]]:
    """``memory_pool_key``'in tersi: (proje adı, kaynak dil, motor ya da None).

    Motor eki yoksa motor ``None`` (varsayılan motor ya da motor bilinmiyor).
    """
    head, sep, engine = key.partition(MEMORY_ENGINE_SEP)
    name, _, lang = head.partition(MEMORY_LANG_SEP)
    name = _POOL_NAME_UNESCAPE_RE.sub(lambda m: _POOL_NAME_UNESCAPES[m.group(1)], name)
    return name, (lang or "en"), (engine if sep else None)


def memory_pool_project(project_name: Optional[str],
                        source_lang: Optional[str]) -> str:
    """Motor eki OLMAYAN havuz adı: ``memory_pool_key(ad, dil)`` (geriye uyum).

    İngilizcede ad aynen (kaçışlı), başka dilde ``<ad>#<dil>``. Sonucu
    ``memory_project_key`` ile birleştirmek ``memory_pool_key`` ile aynıdır.
    """
    return memory_pool_key(project_name, source_lang, None)


def memory_project_key(pool_name: Optional[str], engine_id: Optional[str]) -> str:
    """Havuz adına motor ekini ekle (geriye uyum; yeni kod ``memory_pool_key``).

    ``pool_name`` ``memory_pool_project``'in ürettiği (kaçışlanmış) addır;
    yeniden kaçışlanmaz. Ayraç içermeyen adda sonuç
    ``memory_pool_key(ad, "en", motor)`` ile aynıdır: varsayılan motor ->
    ad AYNEN, diğerleri ``"<ad>@<motor>"`` (ör. ``"kitap@ollama:qwen3:8b"``).
    """
    pool = pool_name or "default"
    if not isinstance(engine_id, str) or not engine_id.strip():
        return pool
    engine = engine_id.strip()
    from core.mt_engines import registry   # çağrı anında: tek kaynak DEFAULT_ENGINE
    if engine == registry.DEFAULT_ENGINE:
        return pool
    return f"{pool}{MEMORY_ENGINE_SEP}{engine}"


# Logger import
try:
    from core.logger import logger
except ImportError:
    logger = None


class TranslationMemory:
    """
    SQLite tabanlı çeviri belleği

    - Çevrilmiş cümleleri saklar
    - Benzer cümleleri bulur (fuzzy matching)
    - Proje bazlı segmentasyon

    Context manager olarak kullanılabilir:
        with TranslationMemory() as tm:
            tm.add("hello", "merhaba")
    """

    def __init__(self, db_path: str = None, project_name: str = "default",
                 auto_clean: bool = True, source_lang: Optional[str] = None):
        self.db_path = db_path or self._get_default_path()
        self.project_name = project_name
        # 2026-09-29 (F3-06): kaynak dil verilirse (çevirmen verir) kaynak
        # dilde kalmış yarım çeviri ne yazılır ne okunur
        # (``is_untranslated_output``). None -> bu kapı yok (eski davranış).
        self.source_lang = source_lang
        self.conn = None
        # check_same_thread=False ile TEK baglanti birden fazla thread'den
        # kullanilabiliyor (batch timeout fallback'i ana thread'e duserken
        # zombi MT thread'i hala yazabiliyor). sqlite3 baglantisi kendi
        # icinde thread-safe DEGIL — tum DB erisimi bu kilitle serialize edilir.
        self._lock = threading.RLock()
        # F49: find_similar satir-turev cache'i (id -> (source, lower, rakamlar))
        self._similar_row_cache: Dict[int, Tuple[str, str, List[str]]] = {}
        self._init_db()
        if auto_clean:
            # DB, add()'in bugun reddettigi hicbir kaydi barindirmamali:
            # eski kod doneminden kalan placeholder/echo zehirleri temizlenir.
            # 2026-09-29: yarım çeviri kapısı (F3-06) bilerek silme DIŞINDA:
            # sezgiseldir (yanlış pozitif iyi kaydı sildirirdi); okuma
            # tarafında süzülür, doğru çeviri gelince aynı kayıt güncellenir.
            try:
                self.clean_poisoned_entries()
            except sqlite3.Error:
                pass  # temizlik best-effort; okuma tarafi guard'lari zaten var

    def __enter__(self):
        """Context manager girişi"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager çıkışı - bağlantıyı kapat"""
        self.close()
        return False

    def _get_default_path(self) -> str:
        """Varsayılan veritabanı yolu: ``<veri kökü>/data/translation_memory.db``.

        Veri kökü ``config.paths.veri_koku``: paketli kipte
        ``KITAPCEVIR_VERI_KOKU``, geliştirici kipinde proje kökü (eski yol).
        Paketli kipte veri kökü henüz oluşmamış olabilir; üst klasörler de
        açılır.
        """
        path = bellek_yolu(Path(__file__).parent.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _init_db(self):
        """Veritabanını başlat"""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        if not self.conn:
            raise RuntimeError(f"SQLite bağlantısı kurulamadı: {self.db_path}")
        self.conn.row_factory = sqlite3.Row

        cursor = self.conn.cursor()

        # WAL mode - daha iyi eşzamanlılık
        cursor.execute("PRAGMA journal_mode=WAL")

        # Ana tablo - hash+project kombinasyonu unique
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS translations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hash TEXT NOT NULL,
                source_text TEXT NOT NULL,
                target_text TEXT NOT NULL,
                source_lang TEXT DEFAULT 'en',
                target_lang TEXT DEFAULT 'tr',
                project TEXT DEFAULT 'default',
                context TEXT,
                confirmed INTEGER DEFAULT 0,
                usage_count INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(hash, project)
            )
        """)

        # İndeksler
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hash_project ON translations(hash, project)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_project ON translations(project)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_source ON translations(source_text)")

        self.conn.commit()

    def _hash_text(self, text: str) -> str:
        """Metin için hash oluştur"""
        if not text:
            raise ValueError("Cannot hash empty or None text")
        normalized = text.strip().lower()
        return hashlib.md5(normalized.encode('utf-8')).hexdigest()

    def add(self, source: str, target: str, context: str = None,
            confirmed: bool = False) -> bool:
        """
        Çeviri ekle veya güncelle

        Args:
            source: Kaynak metin (İngilizce)
            target: Hedef metin (Türkçe)
            context: Bağlam bilgisi (opsiyonel)
            confirmed: İnsan tarafından onaylandı mı?

        Returns:
            Başarılı ise True
        """
        # Guard: placeholder iceren cevirileri cache'e yazma
        if _PLACEHOLDER_RE.search(target):
            return False

        # Guard: source'ta da placeholder varsa yazma
        if _PLACEHOLDER_RE.search(source):
            return False

        # Guard (2026-07-11): Ingilizce echo (kaynak==hedef) "ceviri" degildir.
        # confirmed=True (insan onayli, bilerek birebir) haric yazilmaz —
        # cagiran kod unutsa bile son savunma hatti burasi.
        if not confirmed and _is_poison_echo(source, target):
            return False

        # Guard (2026-09-29, F3-06): kaynak dilde kalmış yarım çeviri yazılmaz.
        if not confirmed and self._untranslated(target):
            return False

        text_hash = self._hash_text(source)

        with self._lock:
            cursor = self.conn.cursor()

            # Var mı kontrol et - proje bazlı
            cursor.execute(
                "SELECT id, usage_count, confirmed FROM translations "
                "WHERE hash = ? AND project = ?",
                (text_hash, self.project_name)
            )
            existing = cursor.fetchone()

            if existing:
                if existing["confirmed"] and not confirmed:
                    # Insan onayli ceviri, sonradan gelen MT ciktisiyla EZILMEZ
                    # (2026-07-11: eski kod target_text'i kosulsuz guncelleyip
                    # confirmed=1 bayragini birakiyordu — onay anlamsizlasiyordu).
                    cursor.execute("""
                        UPDATE translations
                        SET usage_count = usage_count + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE hash = ? AND project = ?
                    """, (text_hash, self.project_name))
                else:
                    # Güncelle - proje bazlı
                    cursor.execute("""
                        UPDATE translations
                        SET target_text = ?,
                            context = COALESCE(?, context),
                            confirmed = MAX(confirmed, ?),
                            usage_count = usage_count + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE hash = ? AND project = ?
                    """, (target, context, int(confirmed), text_hash, self.project_name))
            else:
                # Yeni ekle
                cursor.execute("""
                    INSERT INTO translations
                    (hash, source_text, target_text, project, context, confirmed)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (text_hash, source.strip(), target.strip(),
                      self.project_name, context, int(confirmed)))

            self.conn.commit()
        return True

    def get(self, source: str) -> Optional[dict]:
        """
        Tam eşleşme ara (proje bazlı)

        Returns:
            Eşleşme varsa çeviri bilgisi, yoksa None
        """
        text_hash = self._hash_text(source)

        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM translations WHERE hash = ? AND project = ?
            """, (text_hash, self.project_name))
            row = cursor.fetchone()

        if row:
            # Guard: placeholder iceren sonucu donme
            if _PLACEHOLDER_RE.search(row["target_text"]):
                return None
            # Guard: eski donemden kalan Ingilizce echo kaydini donme —
            # donulurse retranslator ayni Ingilizce'yi sonsuza dek geri alir.
            if not row["confirmed"] and _is_poison_echo(row["source_text"], row["target_text"]):
                return None
            # Guard (2026-09-29, F3-06): eski yarım çeviri kaydı da dönmez
            # (aksi hâlde yeniden çeviri aynı yarımı "exact" alır).
            if not row["confirmed"] and self._untranslated(row["target_text"]):
                return None
            return dict(row)
        return None

    def _untranslated(self, target: str) -> bool:
        """Kaynak dil biliniyorsa: hedef hâlâ kaynak dilde mi (``is_untranslated_output``)."""
        source_lang = getattr(self, "source_lang", None)
        if not source_lang:
            return False
        return is_untranslated_output(target, source_lang)

    def find_similar(self, source: str, threshold: int = 92,
                     limit: int = 5, max_candidates: int = 500) -> List[dict]:
        """
        Benzer çevirileri bul (fuzzy matching) - optimize edilmiş

        Args:
            source: Aranacak metin
            threshold: Minimum benzerlik skoru (0-100)
            limit: Maksimum sonuç sayısı
            max_candidates: Taranacak maksimum aday sayısı (performans için)

        Returns:
            Benzer çevirilerin listesi
        """
        # B-9 (2026-07-27): cursor da kilit İÇİNDE oluşturulur — diğer tüm
        # metodlar kilit içinde açıyor; paylaşılan bağlantıda kilitsiz cursor
        # oluşturma tutarsızlıktı (#25 taramasının kaçırdığı yer).
        source_lower = source.strip().lower()
        source_len = len(source_lower)

        # Performans optimizasyonu:
        # 1. Uzunluk bazlı filtreleme - çok farklı uzunluktaki metinler benzer olamaz
        #    %85 benzerlik için uzunluk farkı en fazla %30 olabilir
        min_len = int(source_len * 0.7)
        max_len = int(source_len * 1.3)

        # 2. Son N kayıt ile sınırla ve uzunluk filtresi uygula
        # CORE-15: Kapsam get() ile ayni olsun -> yalnizca AKTIF proje.
        # Eski `project = ? OR project = 'default'` her projeye 'default'
        # havuzundaki (proje adi verilmemis eski kitaplarin) kayitlarini
        # siziyor, farkli kitabin cevirisi 'similar' olarak donebiliyordu.
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM translations
                WHERE project = ?
                  AND LENGTH(source_text) BETWEEN ? AND ?
                ORDER BY id DESC
                LIMIT ?
            """, (self.project_name, min_len, max_len, max_candidates))
            rows = cursor.fetchall()

        # CORE-04: Sayi guard'i icin kaynaktaki rakam dizilerini onceden cikar.
        # Iki metnin rakam COKLUKUMELERI (multiset) farkliysa (orn. '1990'/'5
        # percent' vs '1991'/'7 percent', ya da '5 and 5' vs '5') yuksek fuzzy
        # skoru olsa bile YANLIS-SAYILI ceviri donmesin -> similar sayilmaz.
        # (set yerine sorted list: tekrar sayisi da eslesmeli, 2026-07-11)
        source_numbers = sorted(_DIGITS_RE.findall(source_lower))

        results = []

        # F49 (2026-07-29): filtre SIRASI maliyete gore — once ucuz ve secici
        # olan rakam-coklukumesi reddi, sonra fuzz, en son zehir guard'lari.
        # Guard'lar (2 placeholder regex + poison-echo) eskiden HER aday
        # satirda kosuyordu; artik yalnizca esigi GECEN adaylara uygulanir.
        # Hepsi AND filtresi oldugundan sonuc kumesi degismez.
        # Satir basina turetilen degerler (lower + rakam listesi) id bazli
        # cache'lenir; source_text degistiyse cache tazelenir.
        row_cache = getattr(self, "_similar_row_cache", None)
        if not isinstance(row_cache, dict):
            row_cache = {}
            self._similar_row_cache = row_cache
        elif len(row_cache) > _ROW_CACHE_MAX_ENTRIES:
            row_cache.clear()

        for row in rows:
            src = row["source_text"]
            entry = row_cache.get(row["id"])
            if entry is None or entry[0] != src:
                stored_source = src.lower()
                entry = (src, stored_source, sorted(_DIGITS_RE.findall(stored_source)))
                row_cache[row["id"]] = entry
            stored_source, stored_numbers = entry[1], entry[2]

            # CORE-04: rakam coklukumeleri esit degilse esleme reddedilir
            if source_numbers != stored_numbers:
                continue

            score = fuzz.ratio(source_lower, stored_source)
            if score < threshold:
                continue

            # Guard: placeholder iceren sonuclari atla
            if _PLACEHOLDER_RE.search(row["target_text"]):
                continue
            if _PLACEHOLDER_RE.search(src):
                continue
            # Guard: Ingilizce echo kaydi 'benzer ceviri' olarak donmesin
            if not row["confirmed"] and _is_poison_echo(src, row["target_text"]):
                continue

            results.append({
                "source": src,
                "target": row["target_text"],
                "score": score,
                "confirmed": bool(row["confirmed"]),
                "usage_count": row["usage_count"],
                "context": row["context"]
            })

        # CORE-B18: Erken cikis mantigi bozuktu (results[0] henuz sort edilmemisti), kaldirildi
        # Skora göre sırala
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def find_equivalent(self, source: str) -> Optional[dict]:
        """Kaynağı ``source`` ile EŞDEĞER (yalnız boşluk / harf büyüklüğü farkı)
        yeniden kullanılabilir kayıt; yoksa None.

        Eşdeğerlik ``memory_equivalence_key``: sözcükler, rakamlar ve noktalama
        aynı sırada. Adaylar SQL'de boşluksuz metin eşitliğiyle ön süzülür
        (bkz. ``_equivalence_prefilter``); tarama SQLite içinde kalır.
        """
        key = memory_equivalence_key(source)
        packed = _equivalence_prefilter(source)
        if not key or not packed:
            return None
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM translations
                WHERE project = ?
                  AND LENGTH(source_text) BETWEEN ? AND ?
                  AND REPLACE(REPLACE(REPLACE(REPLACE(LOWER(source_text),
                        ' ', ''), char(10), ''), char(9), ''), char(13), '') = ?
                ORDER BY id DESC
            """, (self.project_name, len(packed),
                  len(packed) * _EQUIV_LEN_MAX_FACTOR + _EQUIV_LEN_MAX_SLACK, packed))
            rows = cursor.fetchall()

        for row in rows:
            src = row["source_text"]
            if memory_equivalence_key(src) != key:
                continue
            target = row["target_text"]
            if _PLACEHOLDER_RE.search(target) or _PLACEHOLDER_RE.search(src):
                continue
            if not row["confirmed"] and (_is_poison_echo(src, target)
                                         or self._untranslated(target)):
                continue
            return {"source": src, "target": target, "confirmed": bool(row["confirmed"]),
                    "usage_count": row["usage_count"], "context": row["context"]}
        return None

    def get_or_similar(self, source: str, similarity_threshold: int = 90) -> Tuple[Optional[str], str]:
        """Otomatik yeniden kullanım: önce tam eşleşme, yoksa EŞDEĞER kaynak.

        Returns:
            (çeviri veya None, kaynak: "exact", "similar", "none")

        2026-09-29 (bug avı F3-05): bulanık eşleşme (rapidfuzz oranı
        ``similarity_threshold``'ı geçen BAŞKA kaynağın çevirisi) otomatik
        yeniden kullanımda KAPATILDI. Tek sözcük farkı anlamı tersine
        çeviriyordu ve segment modele hiç gitmiyordu: "increases/decreases"
        94,6, "efficient/not efficient" 95,5, "maximum/minimum" 93,9,
        "stable/unstable" 94,7 — hepsi %90 eşiğinin üstünde; rakam koruması
        (CORE-04) sözcük düzeyindeki bu farkları göremez. Olumsuzluk ve
        karşıt sözcük listeleri her kaynak dil için eksiksiz tutulamaz; çeviri
        belleği araçları da bulanık eşleşmeyi yalnız insan onayına sunar.
        "similar" artık yalnız anlamı değiştiremeyen farkta döner: boşluk ve
        harf büyüklüğü (``find_equivalent``); sözcük, rakam ya da noktalama
        ("?" / ".") farkı varsa segment modele gider. Maliyet: yalnız yeniden
        çeviri süresi. ``find_similar`` (insan incelemesi için liste)
        değişmedi; ``similarity_threshold`` geriye uyum için kabul edilir.
        """
        # Tam eşleşme
        exact = self.get(source)
        if exact:
            return exact["target_text"], "exact"

        # Eşdeğer kaynak (yalnız boşluk / harf büyüklüğü farkı)
        equivalent = self.find_equivalent(source)
        if equivalent:
            return equivalent["target"], "similar"

        return None, "none"

    def bulk_add(self, translations: List[Tuple[str, str]], context: str = None):
        """
        Toplu çeviri ekle

        Args:
            translations: [(kaynak, hedef), ...] listesi
        """
        for source, target in translations:
            self.add(source, target, context=context)

    def confirm(self, source: str) -> bool:
        """Çeviriyi onayla (insan kontrolü geçti) - proje bazlı"""
        text_hash = self._hash_text(source)

        # 2026-07-16 (#25): tum DB erisimi kilitli olmali — sqlite
        # baglantisi paylasimli ve kendi icinde thread-safe degil.
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                UPDATE translations SET confirmed = 1, updated_at = CURRENT_TIMESTAMP
                WHERE hash = ? AND project = ?
            """, (text_hash, self.project_name))

            self.conn.commit()
            return cursor.rowcount > 0

    def delete(self, source: str) -> bool:
        """Çeviriyi sil - proje bazlı"""
        text_hash = self._hash_text(source)

        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM translations WHERE hash = ? AND project = ?",
                          (text_hash, self.project_name))

            self.conn.commit()
            return cursor.rowcount > 0

    def clean_unknown_tokens(self) -> int:
        """
        [?] ve benzeri unknown token içeren çevirileri sil.
        Bu kayıtlar tekrar çevrilecek.
        """
        with self._lock:
            cursor = self.conn.cursor()

            # [?] içeren kayıtları bul ve sil
            cursor.execute("""
                DELETE FROM translations
                WHERE target_text LIKE '%[?]%'
                   OR target_text LIKE '%[ ? ]%'
                   OR target_text LIKE '%[UNK]%'
            """)

            deleted = cursor.rowcount
            self.conn.commit()
        
        if deleted > 0:
            print(f"[Memory] {deleted} adet [?] içeren kayıt silindi")
        
        return deleted

    def clean_placeholder_entries(self) -> int:
        """
        Stash placeholder iceren kirli cache entry'lerini sil.

        2026-07-11: Eski LIKE listesi guncel bozuk formlari ([[BİRİM_0]],
        MİYOR0000 vb.) kacirdigi icin tek otorite olan _PLACEHOLDER_RE'ye
        devredildi. clean_poisoned_entries placeholder + echo'yu birlikte
        temizler; burada yalniz placeholder sayisi raporlanir.
        """
        return self.clean_poisoned_entries(include_echo=False)

    def clean_poisoned_entries(self, include_echo: bool = True) -> int:
        """
        add()'in bugunku guard'larinin reddettigi tum zehirli kayitlari sil:
        - source/target'ta _PLACEHOLDER_RE eslesen kayitlar (Turkce'lesmis
          placeholder govdesi dahil: [[BİRİM_0]])
        - Ingilizce echo kayitlari (kaynak==hedef + Ingilizce stopword;
          confirmed=1 olanlara dokunulmaz)

        Python tarafinda filtrelenir ki regex/heuristik tek otorite kalsin.
        """
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT id, source_text, target_text, confirmed FROM translations"
            )
            poisoned_ids = []
            for row in cursor.fetchall():
                if (_PLACEHOLDER_RE.search(row["target_text"])
                        or _PLACEHOLDER_RE.search(row["source_text"])):
                    poisoned_ids.append(row["id"])
                elif (include_echo and not row["confirmed"]
                        and _is_poison_echo(row["source_text"], row["target_text"])):
                    poisoned_ids.append(row["id"])

            for pid in poisoned_ids:
                cursor.execute("DELETE FROM translations WHERE id = ?", (pid,))
            self.conn.commit()

        deleted = len(poisoned_ids)
        if deleted > 0:
            print(f"[Memory] {deleted} adet zehirli kayit silindi (placeholder/echo)")
        return deleted

    def get_stats(self) -> dict:
        """İstatistikleri getir"""
        with self._lock:
            cursor = self.conn.cursor()

            cursor.execute("SELECT COUNT(*) as total FROM translations")
            total = cursor.fetchone()["total"]

            cursor.execute("SELECT COUNT(*) as confirmed FROM translations WHERE confirmed = 1")
            confirmed = cursor.fetchone()["confirmed"]

            cursor.execute("""
                SELECT project, COUNT(*) as count
                FROM translations
                GROUP BY project
            """)
            by_project = {row["project"]: row["count"] for row in cursor.fetchall()}

            cursor.execute("SELECT SUM(usage_count) as total_usage FROM translations")
            total_usage = cursor.fetchone()["total_usage"] or 0

        return {
            "total_entries": total,
            "confirmed_entries": confirmed,
            "by_project": by_project,
            "total_usage": total_usage
        }

    def export_to_json(self, output_path: str, project: str = None):
        """JSON'a aktar"""
        with self._lock:
            cursor = self.conn.cursor()

            if project:
                cursor.execute("SELECT * FROM translations WHERE project = ?", (project,))
            else:
                cursor.execute("SELECT * FROM translations")
            rows = cursor.fetchall()

        data = []
        for row in rows:
            data.append({
                "source": row["source_text"],
                "target": row["target_text"],
                "context": row["context"],
                "confirmed": bool(row["confirmed"]),
                "usage_count": row["usage_count"]
            })

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def import_from_json(self, input_path: str, confirm_all: bool = False):
        """JSON'dan içe aktar"""
        with open(input_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        for item in data:
            self.add(
                item["source"],
                item["target"],
                context=item.get("context"),
                confirmed=confirm_all or item.get("confirmed", False)
            )

    def clear_project(self, project: str):
        """Proje verilerini temizle"""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM translations WHERE project = ?", (project,))
            self.conn.commit()

    def close(self):
        """Bağlantıyı kapat"""
        if self.conn:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass  # Zaten kapalı olabilir
            finally:
                self.conn = None


# Test
if __name__ == "__main__":
    with TranslationMemory(project_name="test_project") as tm:
        # Test ekle
        tm.add(
            "The force acting on the object",
            "Nesneye etkiyen kuvvet",
            context="physics"
        )

        tm.add(
            "Newton's second law states that F equals ma",
            "Newton'un ikinci yasası F'nin ma'ya eşit olduğunu belirtir"
        )

        # Tam eşleşme
        result = tm.get("The force acting on the object")
        print("Tam eşleşme:", result)

        # Benzer ara
        similar = tm.find_similar("The force acting on an object", threshold=80)
        print("\nBenzer sonuçlar:")
        for s in similar:
            print(f"  [{s['score']}%] {s['source']} → {s['target']}")

        # İstatistikler
        stats = tm.get_stats()
        print("\nİstatistikler:", stats)
