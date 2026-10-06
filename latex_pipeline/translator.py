"""
LaTeX-aware translation module.

Translates text content in LaTeX body while preserving math, code,
references, and other LaTeX commands.  Works as part of the
PDF -> Markdown -> LaTeX -> Translation -> XeLaTeX -> PDF pipeline.

Algorithm:
    1. Stash   -- replace protected LaTeX with <<LATEX_N>> placeholders
    2. Segment -- split remaining text into translatable chunks
    3. Translate -- call BookTranslator for each segment
    4. Restore  -- put originals back (reverse order for nesting)

Usage:
    translator = LatexTranslator(pipeline_config)
    translated_body = translator.translate(latex_body)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time as _time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pathlib import Path

# Merkezi config (magic number tek kaynak — KURAL §11)
from config.defaults import (MAX_SEGMENT_LENGTH, SEGMENT_TIMEOUT_S,
                             TRANSLATION_MAX_FAIL_RATIO)

from latex_pipeline.config import PipelineConfig
# LatexTranslator MT ÇIKTISINI temizler (Yol B) — tam liste doğrudur
# ("Çeviri/Çevirmen notu" MT-only kalıpları dahil; A-1, 2026-07-27).
from latex_pipeline.patterns import ALL_PROMPT_LEAK_PATTERNS as PROMPT_LEAK_PATTERNS
from latex_pipeline.kutu_onarim import ORTAM as _KUTU_ORTAMI

logger = logging.getLogger(__name__)

# Checkpoint settings
_CHECKPOINT_INTERVAL = 50  # Save every N segments

# A5 fix: Hard-split chunk boyutu — TEK merkezi sabit.
# Eskiden 500 değeri üç ayrı yerde hardcode'du ve config.max_segment_chars
# (2000) ile tutarsızdı. Merkezi MAX_SEGMENT_LENGTH'ten türetilir
# (Magic Number kuralı §11): 2000 // 4 = 500, mevcut davranışı korur.
HARD_SPLIT_CHARS = MAX_SEGMENT_LENGTH // 4

# BATCH-06 fix: hard-split sırasında bölünmemesi gereken placeholder token'ları.
_HARD_SPLIT_PH_RE = re.compile(r"<<(?:LATEX|TERM|CMD)_\d+>>")

# B3 fix: Heading/body içinde kalan glossary placeholder artifact'leri.
# Hem <<TERM_N>> (kod içi format), hem @@TERMnnnn@@ (MT halüsinasyon formatı),
# hem çıplak TERM_N yakalar. Artifact sızıntısı önlemek için kullanılır.
_ARTIFACT_TERM_RE = re.compile(
    r"(?:<<TERM_\d+>>|@@TERM\d+@@|\bTERM_\d+\b)",
    re.IGNORECASE,
)

# Body tarafı için: LATEX/CMD/TERM placeholder tüm formatları yakalar.
_ARTIFACT_PLACEHOLDER_RE = re.compile(
    r"(?:<<(?:LATEX|CMD|TERM)_\d+>>|@@TERM\d+@@|\[\[[A-Z]+_\d+\]\])",
    re.IGNORECASE,
)

# Known acronyms: ALL-CAPS -> title case dönüşümünde korunacak kısaltmalar
_KNOWN_ACRONYMS = {
    s.upper()
    for s in (
        "GPU", "CPU", "API", "DNA", "RNA", "URL", "HTTP", "HTTPS", "SQL", "HTML",
        "CSS", "PDF", "XML", "JSON", "YAML", "TOML", "SSH", "SSL", "TLS",
        "AI", "ML", "DL", "NLP", "CFD", "FEM", "ODE", "PDE", "SDE",
        "NASA", "IEEE", "ASME", "AIAA", "MIT", "PhD", "MSc", "BSc",
        "RAM", "ROM", "SSD", "HDD", "USB", "HDMI", "RGB", "LED", "LCD",
        "TCP", "UDP", "IP", "DNS", "FPGA", "VHDL", "VLSI", "MEMS",
        "CAD", "CAM", "CAE", "CNC", "PLC", "SCADA", "IoT",
    )
}


# 2026-09-24: başlık yardımcıları (placeholder korumalı title case,
# yarım-çeviri kapıları).
_LATEX_PLACEHOLDER_RE = re.compile(r"<<LATEX_\d+>>")
_LATEX_PLACEHOLDER_ID_RE = re.compile(r"<<LATEX_(\d+)>>")
_HEADING_WORD_RE = re.compile(r"[A-Za-zÇĞİÖŞÜçğıöşü]+")


def _smart_title_case_keep_placeholders(text: str) -> str:
    """``_smart_title_case`` ama ``<<LATEX_N>>`` token'larına dokunmaz.

    ``str.title()`` "<<LATEX_0>>" -> "<<Latex_0>>" yapar ve placeholder
    restore edilemez; token'lar parça sınırı sayılır, yalnız düz metin
    parçaları title-case'lenir, parçaların kenar boşlukları korunur.
    """
    out: List[str] = []
    for part in re.split(r"(<<LATEX_\d+>>)", text):
        if not part:
            continue
        if _LATEX_PLACEHOLDER_RE.fullmatch(part):
            out.append(part)
            continue
        lead = part[:len(part) - len(part.lstrip())]
        trail = part[len(part.rstrip()):]
        core = part.strip()
        out.append(lead + (_smart_title_case(core) if core else "") + trail)
    return "".join(out)


def _heading_words(text: str, min_len: int = 3) -> List[str]:
    """Başlık karşılaştırması için alfabetik kelimeler (küçük harf).

    Placeholder'lar atılır; kesme işareti kelimeyi böler ("Orbit'nın" ->
    "orbit", "nın"), böylece Türkçe ek almış İngilizce kök de görünür.
    Kısa kelimeler (of, an, to, ve...) iki tarafta da düşer.
    """
    clean = _LATEX_PLACEHOLDER_RE.sub(" ", text or "")
    return [w.lower() for w in _HEADING_WORD_RE.findall(clean) if len(w) >= min_len]


def _heading_keeps_source_run(source: str, result: str, min_run: int = 3) -> bool:
    """Kaynağın >= *min_run* ARDIŞIK kelimesi çıktıda aynen sürüyor mu?

    Yarım çeviri dedektörü (2026-09-24): "Two–Phase Flow Dynamics" ->
    "İki–Phase Flow Dynamics" çıktısında kaynağın 3 ardışık kelimesi
    olduğu gibi durur. 2'li eşleşmeler ("Monte Carlo", "Gram Schmidt")
    meşru özel ad olabildiği için eşik 3'tür.
    """
    sw = _heading_words(source)
    rw = _heading_words(result)
    if len(sw) < min_run or len(rw) < min_run:
        return False
    for i in range(len(sw) - min_run + 1):
        window = sw[i:i + min_run]
        for j in range(len(rw) - min_run + 1):
            if rw[j:j + min_run] == window:
                return True
    return False


def _heading_fully_translated(source: str, result: str) -> bool:
    """Kaynağın hiçbir (>=3 harfli) kelimesi çıktıda aynen kalmamış mı?

    Kelime-sınırlı glossary geri dönüşü YALNIZ başlığın tamamını
    karşılıyorsa kabul edilir; tek kelimesi kalan sonuç ("Local
    Reference Izgara") yarımdır ve reddedilir.
    """
    sw = set(_heading_words(source))
    rw = set(_heading_words(result))
    return not (sw & rw)


# Denetim 2026-09-24 (M2#2): başlık yankısı noktalama, tanımlık ve büyük/küçük
# harf farkını görmez. Eski tam eşitlik 'Tidal Pools' -> 'Tidal Pools.',
# 'The Water Cycle' -> 'Water Cycle', 'Cold Front' -> 'Cold Front.'
# yakın yankılarını çeviri sanıp kabul ediyordu.
_HEADING_ECHO_ARTICLES = frozenset({"the", "a", "an"})
_HEADING_ECHO_TOKEN_RE = re.compile(r"\w+")
_HEADING_ECHO_PLACEHOLDER_TOKEN_RE = re.compile(r"latex_\d+")
# Kısa başlık kapısı: 2-3 kelimelik kaynakta en az bu kadar kelime aynen
# kalıyorsa çıktı çeviri değil yarım yankıdır ('Potential Energy' ->
# 'Potential Energy', 'Pebble Count Spread' -> 'Pebble Count Yayılım').
_SHORT_HEADING_MAX_WORDS = 3
_SHORT_HEADING_MIN_KEPT = 2
# Kapıdan muaf, çeviride meşru olarak aynen kalan kelimeler: büyük harfli
# kısaltmalar (GPU, MLP) ve tireli özel ad bileşikleri (Navier-Stokes,
# Runge-Kutta, Gram-Schmidt).
_HEADING_ACRONYM_RE = re.compile(r"\b[A-ZÇĞİÖŞÜ]{2,}\b")
_HEADING_EPONYM_RE = re.compile(
    r"\b[A-ZÇĞİÖŞÜ][a-zçğıöşü]+(?:[-–][A-ZÇĞİÖŞÜ][a-zçğıöşü]+)+\b"
)


def _heading_echo_key(text: str, drop_articles: bool) -> Tuple[str, ...]:
    """Yankı karşılaştırma anahtarı: küçük harf, noktalamasız kelimeler."""
    lowered = (text or "").replace("İ", "I").lower()
    words = _HEADING_ECHO_TOKEN_RE.findall(lowered)
    if drop_articles:
        words = [w for w in words if w not in _HEADING_ECHO_ARTICLES]
    return tuple(words)


def _heading_is_echo(source: str, result: str) -> bool:
    """MT sonucu kaynağın noktalama/tanımlık/harf büyüklüğü farkıyla yankısı mı?

    Yalnız tanımlığı düşen tek kelime yankı sayılmaz: 'The Model' ->
    'Model' Türkçede de doğru çeviridir.
    """
    key = _heading_echo_key(result, drop_articles=False)
    if key and key == _heading_echo_key(source, drop_articles=False):
        return True
    key = _heading_echo_key(result, drop_articles=True)
    # Sayı ve <<LATEX_N>> parçası kelime sayılmaz ('4.2.1 The Setup').
    n_words = sum(
        1 for w in key
        if not w.isdigit() and not _HEADING_ECHO_PLACEHOLDER_TOKEN_RE.fullmatch(w)
    )
    return n_words >= 2 and key == _heading_echo_key(source, drop_articles=True)


def _heading_exempt_words(source: str) -> set:
    """Kısa başlık kapısında sayılmayan kaynak kelimeleri (küçük harf)."""
    exempt: set = set()
    clean = _LATEX_PLACEHOLDER_RE.sub(" ", source or "")
    for rx in (_HEADING_ACRONYM_RE, _HEADING_EPONYM_RE):
        for m in rx.finditer(clean):
            exempt.update(w.lower() for w in _HEADING_WORD_RE.findall(m.group(0)))
    return exempt


def _heading_keeps_source_words(source: str, result: str) -> bool:
    """2-3 kelimelik kaynak başlığın >= 2 kelimesi çıktıda aynen sürüyor mu?

    ``_heading_keeps_source_run`` (3 ardışık kelime) kısa başlıkta hiç
    tetiklenemez; 'Pebble Filter’ın Temizlenmesi' gibi yarım çeviriler
    kapıdan geçiyordu. Sayım ardışıklık aramaz ('Control Akış
    Drift'). Kısaltmalar ve tireli özel ad bileşikleri sayılmaz.
    """
    sw = set(_heading_words(source))
    if not (_SHORT_HEADING_MIN_KEPT <= len(sw) <= _SHORT_HEADING_MAX_WORDS):
        return False
    kept = (sw & set(_heading_words(result))) - _heading_exempt_words(source)
    return len(kept) >= _SHORT_HEADING_MIN_KEPT


def _smart_title_case(text: str) -> str:
    """ALL-CAPS metni title case'e cevir, kisaltmalari koru.

    >>> _smart_title_case("GPU PERFORMANCE AND CPU OPTIMIZATION")
    'GPU Performance And CPU Optimization'
    >>> _smart_title_case("HEAT MODEL FOR SOLID-STATE COOLING")
    'Heat Model For Solid-State Cooling'
    """
    words = text.split()
    result = []
    for word in words:
        if word.upper() in _KNOWN_ACRONYMS or _looks_like_acronym(word):
            result.append(word.upper())
        else:
            # str.title() hyphen-aware: "SOLID-PROPELLANT" -> "Solid-Propellant"
            result.append(word.title())
    return " ".join(result)


# HEAD-04 fix: whack-a-mole akronim listesini büyütmek yerine kural.
# ALL-CAPS bir başlıkta 2-5 harflik, sesli harf içermeyen kelime büyük
# olasılıkla bir kısaltmadır (CNN, RNN, LSTM, SGD, SVM ...). str.title()
# 'Cnn' üretmesin diye bunları olduğu gibi korur. Sesli harf içeren
# akronimler (GAN, GPU, API vb.) zaten _KNOWN_ACRONYMS'te tutulur.
_VOWELS = frozenset("AEIOU")


def _looks_like_acronym(word: str) -> bool:
    core = word.strip(".,;:!?()[]{}\"'")
    if not (2 <= len(core) <= 5) or not core.isascii() or not core.isalpha():
        return False
    if not core.isupper():
        return False
    return not any(c in _VOWELS for c in core)


def _is_effectively_all_caps(text: str) -> bool:
    """Metin cogunlukla buyuk harf mi? (>=%80 buyuk harf oran).

    Strict `text == text.upper()` kontrolü "HEAT MODEL for X" gibi
    minor lowercase kelime içeren başlıkları kaçırır.  Bu yardımcı
    harf sayısına göre oran hesaplar — sadece alfabetik karakterleri
    değerlendirir, sayı ve noktalama göz ardı edilir.

    >>> _is_effectively_all_caps("HEAT MODEL FOR COOLING")
    True
    >>> _is_effectively_all_caps("HEAT MODEL for COOLING")
    True
    >>> _is_effectively_all_caps("Heat Model")
    False
    >>> _is_effectively_all_caps("AB")
    False
    """
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return False
    upper_count = sum(1 for c in letters if c.isupper())
    return upper_count / len(letters) >= 0.8


# ---------------------------------------------------------------------------
# İngilizce dışı kaynak metni cümlelere bölme (2026-09-28, K5 / KARARLAR 14)
# ---------------------------------------------------------------------------
# LatexTranslator._SENTENCE_SPLIT_RE İngilizce + Türkçe için yazıldı
# (2026-09-29 F3-11: kısaltma bakışları nokta dahil yapıldı; İngilizce
# kilit fikstürü kısaltma içermediğinden değişmedi). İngilizce dışı kaynakta
# onun iki sorunu var: büyük harf kümesinde Kiril yok (Rusça paragraf hiç
# bölünmez) ve Almanca "z. B. Die…", "im 19. Jahrhundert" gibi yerlerde
# cümle ortasından bölünür. Aşağıdaki desen yalnız KAYNAK metni bölerken
# (uzun segment, yankı sonrası yeniden çeviri) ve yalnız İngilizce dışı
# kaynakta kullanılır. Kısaltmalar nokta DAHİL arkadan bakılır (bölme
# noktası noktadan sonradır). Cümle sonunda da sık geçen kısaltmalar
# ("etc.", "usw.", "и т. д.") bilerek listede YOK: iki cümleyi birleştirmek
# zararsızdır, cümle ortasından bölmek çeviriyi bozar.
_SOURCE_SENTENCE_COMMON_ABBREVIATIONS = ("Dr", "Prof", "Fig", "et al", "vs")
_SOURCE_SENTENCE_ABBREVIATIONS: Dict[str, tuple] = {
    "de": ("z. B", "d. h", "u. a", "u. ä", "bzw", "vgl", "Abb", "Nr", "Kap",
           "Gl", "evtl", "sog", "Tab", "Bd", "Hrsg", "Jh", "ca"),
    "fr": ("M", "MM", "Mme", "Mlle", "cf", "chap", "éd", "env", "vol", "fig",
           "art", "p", "pp"),
    "es": ("Sr", "Sra", "Srta", "pág", "págs", "p. ej", "cap", "núm", "fig",
           "aprox", "vol", "Ud", "Uds", "Vd"),
    "it": ("Sig", "Sigg", "pag", "pagg", "cfr", "cap", "fig", "vol", "ca",
           "es", "dott"),
    "ru": ("т. е", "рис", "гл", "см", "стр", "проф", "акад", "им", "табл"),
}
# Sayıdan sonraki nokta sıra sayısıdır ("19. Jahrhundert", "3. Kapitel"):
# yalnız Almancada cümle ortasında bu kadar sık.
_SOURCE_SENTENCE_ORDINAL_LANGS = ("de",)
# Cümle başı olabilecek karakterler: Latin büyük harf (aksanlılar dahil),
# Türkçe büyük harfler, Kiril büyük harf, tırnak/parantez/ters ünlem-soru.
# V2 denetimi (2026-09-29, CLAUDE.md §11 tek kaynak): harf kümesi
# core.sentence_splitter.SENTENCE_START_UPPER ile AYNI (oradan alınır);
# açılış kümesinde '\\' var, '[' yok — bilinçli fark (Y2 notu).
from core.sentence_splitter import SENTENCE_START_UPPER as _SENTENCE_START_UPPER  # noqa: E402
_SOURCE_SENTENCE_START = _SENTENCE_START_UPPER + "\"'\\(\\\\«„“¿¡"


def _build_source_sentence_split_re(lang: str) -> "re.Pattern[str]":
    abbrs = []
    for abbr in _SOURCE_SENTENCE_COMMON_ABBREVIATIONS + \
            _SOURCE_SENTENCE_ABBREVIATIONS.get(lang, ()):
        # Çok parçalı kısaltmanın ilk parçaları da ("z. B" -> "z"): bölme
        # noktası "z."dan hemen sonra da denenir.
        parts = abbr.split(". ")
        for k in range(1, len(parts) + 1):
            head = ". ".join(parts[:k])
            for form in (head, head[:1].upper() + head[1:]):
                if form not in abbrs:
                    abbrs.append(form)
    behind = "".join(r"(?<!\b" + re.escape(a) + r"\.)" for a in abbrs)
    if lang in _SOURCE_SENTENCE_ORDINAL_LANGS:
        behind += r"(?<!\d\.)"
    return re.compile(behind + r"(?<=[.?!])\s+(?=[" + _SOURCE_SENTENCE_START
                      + r"])")


_SOURCE_SENTENCE_SPLIT_RES: Dict[str, "re.Pattern[str]"] = {
    lang: _build_source_sentence_split_re(lang)
    for lang in _SOURCE_SENTENCE_ABBREVIATIONS
}


# ---------------------------------------------------------------------------
# Translation stats
# ---------------------------------------------------------------------------

@dataclass
class TranslationStats:
    """Tracks translation progress."""
    segments_total: int = 0
    segments_translated: int = 0
    segments_skipped: int = 0
    segments_failed: int = 0
    placeholders_stashed: int = 0
    # B-11 fix: MT'nin yuttuğu (kaybettiği) placeholder sayısı. Kayıp olunca
    # segment orijinaliyle geri alınır (sona-ekleme yapılmaz) ve bu sayaç artar.
    placeholders_lost: int = 0
    # 2026-09-24 (denetim Mercek 2 #1, Mercek 3 #8): MT'nin kaynakta OLMAYAN
    # (halüsine) ya da KOPYA yer tutucu ürettiği token sayısı ("<<LATEX_N>>"
    # gibi bozuk biçimler dahil). Kayıpla aynı sözleşme: segment orijinaline
    # döner, bütün gönderilen satır parçalı yola düşer.
    placeholders_extra: int = 0
    # 2026-09-24 (parçalama kök nedeni): satır yer tutucularıyla BÜTÜN
    # gönderilir. Bütün çevirisi tutmayan yer tutuculu SATIR (gövde ya da
    # altyazı) sayısı (0 = hepsi bütün geçti). G2a-1 (2026-10-05): ad tarihî;
    # parçalı yol YOK, bu satırlar kurtarma sırasına girer (_recover_segment).
    placeholder_split_fallbacks: int = 0
    # G2a-1 (2026-10-05, mt okuma MT-1/MT-7/MT-17): bozuk çıktı bekçileri.
    # LaTeX iskeleti bozulan / sayı kaybeden ya da kaynakta olmayan ilişki
    # simgesi üreten çeviri orijinaline döndü:
    skeleton_reverts: int = 0
    number_reverts: int = 0
    # Kurtarma sırası: aynı metnin tekrarı / sade biçim (kısa değişken düz
    # metin, ham komut kabuğu sabit) tuttu; hiçbiri tutmadı ve parça (ya da
    # uzun parçanın tutmayan cümlesi) özgün dilinde bırakıldı (işaretli):
    recovered_by_retry: int = 0
    recovered_by_simplified: int = 0
    kept_source_segments: int = 0
    # FX3 (2026-10-05, V2 Y3): model düşünme payı dolup cevap üretemediği
    # için (bekçinin tek yeniden denemesinden sonra) özgün dilde kalan ya da
    # yarı çevrilmiş kalan parça; kurtarma zinciri bunlarda koşmaz.
    no_answer_segments: int = 0
    # İşaret kaydı: [{"neden", "kismi", "kaynak"}] — yalnız gerçek olay.
    # QC bu parçaları kaynak dilde kalmış satır olarak ayrıca raporlar.
    kept_source_list: list = field(default_factory=list)
    # G2a-2 (2026-10-05) — yalnız gerçek sayım:
    # kaynakça bölümü (girdileri) çevrilmeden korundu (mt okuma MT-19);
    bibliography_sections: int = 0
    # model çıktısından istem işareti (<<<TRANSLATE_START>>> ve bozuk
    # biçimleri) temizlendi (sızıntı olayı sayısı);
    prompt_marker_leaks: int = 0
    # display matematikten önceki kısa parça komşu bağlamla birlikte
    # çevrildi / bağlamlı çeviri tutmadı, parçalar ayrı çevrildi (MT-13);
    context_groups: int = 0
    context_group_fallbacks: int = 0
    # FX3 (2026-10-05): tam grup tutmadı, devam paragrafının yalnız İLK
    # cümlesiyle daraltılmış grup tuttu (context_groups'un alt sayımı).
    context_groups_reduced: int = 0
    # Türkçe düzyazıda ondalık ayırıcı virgüle çevrildi (MT-14/MT-15).
    decimal_separators_localized: int = 0
    # FX3 (2026-10-05, mt okuma MT-18): modelin eklediği, kaynakta olmayan
    # eşsiz markdown "**" artığı çıktıdan silindi.
    markdown_remnants_removed: int = 0
    # 2026-09-24: kaynak dili İngilizce olmayan (Latince alıntı vb.) segment
    # çevrilmeden geçildi (Yol A davranışı).
    segments_foreign_skipped: int = 0
    # 2026-09-24: [[UNIT/BIRIM]]/[[FORMULA]] kalıntısı onarılamadı, segment
    # silme yerine orijinaliyle geri alındı (veri kaybı yasağı).
    remnant_reverts: int = 0
    # 2026-09-24: kaynakta olmayan sondaki "…" silindi (model tiki).
    trailing_ellipsis_removed: int = 0
    # 2026-09-28 (KARARLAR 15-16): çevirmenin (core BookTranslator) ölçümleri
    # koşu sonunda buraya aktarılır (_collect_model_stats) — yalnız gerçek sayım.
    # Üretim sınırına dayanan çıktı (kabul edildi ama sessiz geçmedi):
    segments_truncated: int = 0
    # Düşünme görülen parça; akış parçası / sunucu sayacıyla sayılan ve
    # sayaç yokken karakterden tahmin edilen düşünme token'ı:
    thinking_segments: int = 0
    thinking_tokens: int = 0
    thinking_tokens_estimated: int = 0
    # Düşünme bütçesini aşan deneme (akışta hemen kesilir), sert kapatmalı yeniden deneme,
    # düşünme sınırı yüzünden çevrilmeyen parça:
    think_budget_cuts: int = 0
    think_retries: int = 0
    think_failed: int = 0
    # 2026-09-29 (V2 denetimi, F3-07 görünürlük): bu koşuda üretim sınırına
    # dayanan parçalar — core BookTranslator.truncation_report() öğeleri
    # ({source, output, used, limit}); translate_book kesik_parcalar.json'a
    # yazar, arayüz ve QC okur. Yalnız gerçek kayıt (uydurma yok).
    truncated_segments: list = field(default_factory=list)


# FINAL checkpoint dosyasının adı (checkpoint_dir altında). translate_book
# --force-mt onu yedeğe taşır; ad tek kaynaktan (V2 denetimi, §11).
FINAL_CHECKPOINT_NAME = "translated_body_final.tex"

# Kesilme raporunda günlüğe tek tek yazılan en çok parça (kalanı sayıyla).
TRUNCATION_LOG_ITEMS = 10

# Çevirmen istatistiğinden TranslationStats'e aktarılan ölçüm alanları
_MODEL_STAT_FIELDS = (
    "segments_truncated", "thinking_segments", "thinking_tokens",
    "thinking_tokens_estimated", "think_budget_cuts", "think_retries", "think_failed",
)


# ---------------------------------------------------------------------------
# Protected-content patterns (order matters -- broader patterns first)
# ---------------------------------------------------------------------------

# Math environments: equation, equation*, align, align*, gather, gather*,
# multline, multline*, split, flalign, flalign*, alignat, alignat*
_MATH_ENV_RE = re.compile(
    r"\\begin\{("
    r"equation\*?|align\*?|gather\*?|multline\*?|split|flalign\*?|alignat\*?"
    r"|eqnarray\*?|cases|pmatrix|bmatrix|vmatrix|Vmatrix|smallmatrix|array"
    r")\}"
    r"(.*?)"
    r"\\end\{\1\}",
    re.DOTALL,
)

# Display math: \[ ... \]
# 2026-09-29 (F3-01): _stash_protected ARTIK bu deseni kullanmaz (bkz.
# _stash_math_spans); yalnız geriye dönük içe aktaranlar için duruyor.
_DISPLAY_MATH_BRACKET_RE = re.compile(r"\\\[.*?\\\]", re.DOTALL)

# Display math: $$ ... $$ (aynı not: stash TeX okuma sırasıyla yapılır)
_DISPLAY_MATH_DOLLAR_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)

# 2026-09-29 (F3-01): Matematik aralıklarından sonra metinde kalan eşsiz
# matematik ayraçları: '$', '$$', '\[', '\]'. Kaçışlar ('\$', '\\' ve
# '\\[2pt]') tek belirteç olarak atlanır.
_STRAY_MATH_DELIM_TOKEN_RE = re.compile(r"\\\[|\\\]|\\.|\$\$|\$", re.DOTALL)


def _replace_spans(text: str, spans, push) -> str:
    """``text[başlangıç:bitiş]`` aralıklarını ``push(parça)`` dönüşüyle değiştir.

    Aralıklar belge sırasında ve çakışmasız olmalıdır (okuyucular böyle
    üretir). Yer tutucu numaraları belge sırasında artar."""
    if not spans:
        return text
    out: List[str] = []
    pos = 0
    for start, end in spans:
        if start < pos:
            continue
        out.append(text[pos:start])
        out.append(push(text[start:end]))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _stash_math_spans(text: str, push) -> str:
    r"""``\[…\]``, ``$$…$$`` ve ``$…$`` matematiğini TeX okuma sırasıyla stash'le.

    2026-09-29 (F3-01): eski düz regexler ('\\\[.*?\\\]', '\$\$(.+?)\$\$',
    DOTALL) bitişik iki satır içi atomun ortasındaki '$$'yi
    ('$C_{4}$$H_{10}$') ve Marker'ın atıf kaçışını ('\href{..}{\[ 63}]')
    display açılışı sayıp sonraki kapanışa kadar paragraflar boyunca
    düzyazıyı TEK "matematik" yer tutucusuna çeviriyordu: o paragraflar
    MT'ye hiç gitmiyor, kapanıştaki gerçek display MT'ye metin olarak
    gidiyordu (bir test kitabının gövdesinin %40'ı). Okuyucular QC ile ORTAK:
    ``core.lang_detect.bracket_display_spans`` ve ``dollar_math_spans``
    (``latex_pipeline.sanitizer``). Stash sırası eskisiyle aynı: önce
    ``\[…\]``, sonra ``$$…$$``, en son satır içi ``$…$`` (yer tutucu
    numaraları iyi biçimli metinde değişmez).
    """
    from core.lang_detect import bracket_display_spans, dollar_math_spans

    text = _replace_spans(text, bracket_display_spans(text), push)
    text = _replace_spans(
        text, [(s, e) for s, e, disp in dollar_math_spans(text) if disp], push)
    return _replace_spans(
        text, [(s, e) for s, e, disp in dollar_math_spans(text) if not disp],
        push)


def _stash_stray_math_delims(text: str, push) -> str:
    r"""Eşsiz kalan ``$`` / ``$$`` / ``\[`` / ``\]`` ayraçlarını yer tutucuya al.

    Kayıpsızlık (F3-01): matematik sayılmayan bir ayraç (Marker atıf kaçışı
    ``\[ 63}]``, paragrafta eşi olmayan ``$``) MT'ye çıplak gitseydi model
    onu düşürebilir ya da taşıyabilirdi; artık yerinde aynen durur, çevresindeki
    düzyazı çevrilir."""
    if "$" not in text and "\\[" not in text and "\\]" not in text:
        return text
    from core.lang_detect import literal_dollar_positions
    literal = literal_dollar_positions(text)
    out: List[str] = []
    pos = 0
    for m in _STRAY_MATH_DELIM_TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok[0] == "\\" and tok not in ("\\[", "\\]"):
            continue
        if m.start() in literal:
            continue  # '\\$4/day': düz dolar (bkz. lang_detect.dollar_math_spans)
        out.append(text[pos:m.start()])
        out.append(push(tok))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)

# Inline math: $...$ (avoid escaped \$ and display $$)
# Note: a preceding backslash escapes the dollar sign (\$), so the lookbehind
# excludes both '$' (display math) and '\\' (escaped). Fully handling
# sequences like "\\$" (escaped backslash followed by real dollar) requires
# variable-length lookbehind which Python does not support — the simple
# form covers the vast majority of real-world LaTeX usage.
_INLINE_MATH_RE = re.compile(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$(?!\$)")

# Tabular environments: the ENTIRE \begin{tabular}...\end{tabular} block
# must be stashed because column spec ({l l l}) and data rows (\toprule,
# \midrule, & separators) must never be modified by the translator.
_TABULAR_ENV_RE = re.compile(
    r"\\begin\{(tabular|tabularx|longtable)\}\s*(?:\{[^}]*\})?"
    r"(.*?)"
    r"\\end\{\1\}",
    re.DOTALL,
)

# Code environments
_CODE_ENV_RE = re.compile(
    r"\\begin\{(lstlisting|verbatim|minted)\}"
    r"(.*?)"
    r"\\end\{\1\}",
    re.DOTALL,
)

# Inline code: \texttt{...} (handles nested braces one level deep)
_TEXTTT_RE = re.compile(r"\\texttt\{(?:[^{}]|\{[^{}]*\})*\}")

# \textit{...} with SHORT content -- typically variable names
# like \textit{a}, \textit{Re}, \textit{f(x)}.  Longer \textit content
# is left for translation (it may contain translatable prose).
# Match single-word content up to 8 chars (no spaces) to avoid stashing
# multi-word English phrases like \textit{The function}.
# 2026-09-29 (F3-03): grup 1 = içerik; bütün stash YALNIZ değişken/sembol
# biçimindeyse (bkz. _short_italic_is_symbol).
_TEXTIT_SHORT_RE = re.compile(r"\\textit\{([^{}\s]{1,8})\}")

# 2026-09-29 (F3-03): kısa italik ya değişken/sembol ('\textit{x}',
# '\textit{Re}', '\textit{f(x)}', '\textit{DNA}') ya da vurgulanmış gerçek
# bir sözcüktür ('did \textit{not} allow', '\textit{always}', '\textit{God}').
# Eskiden ikisi de bütün hâlinde stash'leniyordu: vurgulu sözcük MT'ye hiç
# gitmiyor, çıktıda kaynak dilde kalıyordu (vurgulu olumsuzluk düşünce anlam
# tersine dönebiliyordu; bir test kitabında 1213 örnek). Artık yalnız
# değişken/sembol biçimi bütün stash'lenir; gerçek sözcüğün yalnız KABUĞU
# ('\textit{' ve '}') yer tutucuya alınır, sözcük cümlesiyle birlikte
# (bağlamında) MT'ye gider.
_SHORT_ITALIC_MATH_NAMES = frozenset((
    "sin", "cos", "tan", "cot", "sec", "csc", "sinh", "cosh", "tanh", "coth",
    "arcsin", "arccos", "arctan", "log", "ln", "lg", "exp", "max", "min",
    "sup", "inf", "lim", "liminf", "limsup", "det", "dim", "ker", "deg",
    "arg", "argmax", "argmin", "mod", "gcd", "lcm", "var", "cov", "tr",
    "rank", "span", "diag", "sgn", "sign", "erf", "erfc", "hom", "im", "re",
    "id", "const", "avg", "std", "rms", "eps", "grad", "div", "curl", "rot",
))
_SHORT_ITALIC_WORD_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)?")
_GREEK_LETTER_RE = re.compile(r"[\u0370-\u03FF\u1F00-\u1FFF]")
_CYRILLIC_LETTER_RE = re.compile(r"[\u0400-\u052F]")
_LATIN_VOWELS = frozenset("aeiouyàáâãäåæèéêëìíîïòóôõöøœùúûüýÿ")


# 2026-09-29 (V2 denetimi, F3-03 kabuğu): vurgulu kısa sözcüğün kabuğu iki
# AYRI yer tutucudur ('\textit{' ve '}', stash'te ardışık kimlikler). Model
# ikisinin yerini değiştirirse ('<<b>>değil<<a>>') geri yazım
# '}değil\textit{' üretir: süslü parantez SAYISI dengeli göründüğü için hiçbir
# denetim yakalamaz, ama sıra bozuktur (TeX "Too many }'s", italik paragrafın
# sonuna taşar). Kapanış açılıştan ÖNCE geliyorsa iki yer tutucu yer
# değiştirir; metin olduğu gibi kalır (kayıpsızlık).
SHORT_ITALIC_SHELL_OPEN = "\\textit{"
SHORT_ITALIC_SHELL_CLOSE = "}"


def repair_italic_shell_order(text: str, stash: List[str],
                              token_re: "re.Pattern[str]") -> str:
    """Yer değiştirmiş kısa italik kabuk yer tutucularını düzelt.

    *token_re* grup 1'i yer tutucunun kimliği (sayı) olan desendir
    (``<<LATEX_(\\d+)>>``, retranslator'da ``<<RT_(\\d+)>>``). Yalnız
    ``stash[k] == '\\textit{'`` ve ``stash[k+1] == '}'`` olan, metinde
    birer kez geçen çiftlere dokunulur; kapanış açılıştan önceyse iki token
    yer değiştirir. Başka hiçbir karakter değişmez.
    """
    if not stash or not text:
        return text
    positions: Dict[int, List[int]] = {}
    for m in token_re.finditer(text):
        positions.setdefault(int(m.group(1)), []).append(m.start())
    swap: Dict[int, int] = {}
    n = len(stash)
    for k, opens in positions.items():
        if (k + 1 >= n or stash[k] != SHORT_ITALIC_SHELL_OPEN
                or stash[k + 1] != SHORT_ITALIC_SHELL_CLOSE):
            continue
        closes = positions.get(k + 1)
        if len(opens) == 1 and closes and len(closes) == 1 and closes[0] < opens[0]:
            swap[k] = k + 1
            swap[k + 1] = k
    if not swap:
        return text
    logger.warning(
        "MT kısa italik kabuğunun ('\\textit{' / '}') yer tutucularını ters "
        "sıraya koymuş (%d çift); sıra düzeltildi, metin korundu.", len(swap) // 2)

    def _sub(m: "re.Match[str]") -> str:
        k = int(m.group(1))
        if k not in swap:
            return m.group(0)
        start, end = m.span(1)
        return (m.group(0)[:start - m.start()] + str(swap[k])
                + m.group(0)[end - m.start():])

    return token_re.sub(_sub, text)


def _short_italic_is_symbol(content: str, source_lang: str = "en") -> bool:
    """Kısa (≤8 karakter, boşluksuz) italik içerik değişken/sembol mü?

    Sembol (bütün stash'lenir, MT'ye gitmez): tek karakter; harf dışı
    karakter içeren (``f(x)``, ``x_1``, ``a2``, ``e.g.``, ``\\alpha``);
    Yunan harfi; iki+ harfli tamamı büyük harf (kısaltma: ``DNA``); ilk
    harften sonra büyük harf (``LaTeX``, ``pH``, ``dB``); matematik işleç
    adı (``sin``, ``max``, ``det``…); iki harfli olup kaynak dilin sık
    sözcüğü olmayan (``Re``, ``Nu``, ``Ma``; ama ``is``, ``no``, ``не``
    sözcüktür); Latin alfabeli olup hiç ünlüsü olmayan (``dx``, ``rms``).
    Geri kalan harf dizisi gerçek sözcüktür (``not``, ``always``, ``God``).
    """
    if len(content) <= 1:
        return True
    if not _SHORT_ITALIC_WORD_RE.fullmatch(content):
        return True
    if _GREEK_LETTER_RE.search(content):
        return True
    letters = [c for c in content if c.isalpha()]
    if len(letters) >= 2 and all(c.isupper() for c in letters):
        return True
    if any(c.isupper() for c in content[1:]):
        return True
    low = content.lower()
    if low in _SHORT_ITALIC_MATH_NAMES:
        return True
    if len(letters) == 2:
        from core.lang_detect import frequent_words
        return low not in frequent_words(source_lang or "en")
    if not _CYRILLIC_LETTER_RE.search(content):
        if not any(c in _LATIN_VOWELS for c in low):
            return True
    return False

# \textbf{...} with SHORT content (1-20 chars) -- typically figure/table
# references like \textbf{Fig. 5.1}, or single-word emphasis.
# Longer \textbf content may contain translatable text and is NOT stashed.
_TEXTBF_SHORT_RE = re.compile(r"\\textbf\{[^{}]{1,20}\}")

# \textit{...} and \textbf{...} with LONGER content.
# These are stashed to protect the wrapper command (it vs bf), but
# the inner content is translated in _translate_stashed_formatting().
# Supports one level of nested braces: \textit{the \emph{function}}
_TEXTIT_LONG_RE = re.compile(r"\\textit\{((?:[^{}]|\{[^{}]*\})*)\}")
_TEXTBF_LONG_RE = re.compile(r"\\textbf\{((?:[^{}]|\{[^{}]*\})*)\}")

# \includegraphics[...]{...}
_INCLUDEGRAPHICS_RE = re.compile(
    r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{[^}]*\}"
)

# --------------------------------------------------------------------------
# Academic name pattern building block.
# Matches surnames like: Hollingworth, LeCun, DeGroot, O'Brien, van der Waals.
# Breakdown:
#   (?:[A-Z][a-z]*){1,3}   -- 1-3 capitalized segments (Le+Cun, De+Groot)
#   (?:'[A-Z][a-z]+)?      -- optional apostrophe segment (O'Brien)
# This is used in both _AUTHOR_ETAL_RE and _PAREN_REF_RE below.
_SURNAME = r"(?:[A-Z][a-z]*){1,3}(?:'[A-Z][a-z]+)?"

# Author names with "et al." -- appears WITHOUT parentheses in running text.
# Examples: "Hollingworth et al.", "Goodfellow et al.", "Kingma and Welling et al."
# In _stash_protected this runs AFTER _PAREN_REF_RE so that parenthetical
# refs like (Author et al., 2020) are captured as whole units first.
# Extended pattern also captures optional trailing " (YYYY)" so that
# "Hollingworth et al. (2020)" is stashed as one unit.
#
# NOTE: The "et al." may be wrapped in \textit{} in some books,
# so we match both bare "et al." and "\textit{et al.}" variants.
_ET_AL = r"(?:\\textit\{)?et\s+al\.(?:\})?"
_AUTHOR_ETAL_RE = re.compile(
    _SURNAME                                            # Lastname (e.g. LeCun)
    + r"(?:\s+(?:and|&)\s+" + _SURNAME + r")*"         # optional: and Lastname
    + r"\s+" + _ET_AL                                   # et al. (bare or \textit{})
    + r"(?:\s*\(\d{4}[a-z]?\))?"                        # optional: (YYYY)
)

# Bare "Author (YYYY)" without "et al." -- catches inline citations like
# "LeCun (2015)" or "Marchetti (2019)" that appear in running text.
# Must run AFTER _PAREN_REF_RE and _AUTHOR_ETAL_RE.
#
# 2026-09-24 (FX3: sentetik örnek): "In Book I of his Treatise
# (1750)" — eser adı + yıl, yazar-yıl atfı sanılıp stash'leniyordu; satır
# üçe bölündü ve model "I"yı "beni" diye çevirdi. İyelik/tanımlık ("his
# Principia", "the Origin (1859)") bir atfın önünde gelmez; bu sözcüklerden
# sonra eşleşme yok.
# "LeCun (2015)", "Marchetti (2012)" gibi çıplak atıflar aynen yakalanır.
# Denetim 2026-09-24 (M2#5): engel büyük/küçük harf duyarsız (cümle başı
# "His/Her/Their/Its Principia (1687)") ve iyelik "-'s" öncülünü de kapsar
# ("Darwin's Origin (1859)"). Baştaki \b eşleşmenin kelime ortasından
# başlamasını önler: yoksa "his LeCun (2015)" engeli "Cun (2015)" ile aşılır.
_AUTHOR_YEAR_RE = re.compile(
    r"(?<!\b(?i:his)\s)(?<!\b(?i:her)\s)(?<!\b(?i:its)\s)(?<!\b(?i:the)\s)"
    r"(?<!\b(?i:their)\s)(?<!\b(?i:this)\s)"
    r"(?<!\w['’]s\s)"
    r"\b" + _SURNAME + r"\s+\(\d{4}[a-z]?\)"
)

# Parenthetical references: (Author, Year), (Author et al., Year),
# (Author and Author, Year), (A, 2020; B et al., 2019) -- common in
# academic text.  These must be stashed to prevent surname translation.
#
# Extended to support:
#   - Multiple references separated by semicolons
#   - Optional closing paren (segment may be cut mid-reference)
#   - CamelCase surnames (LeCun, DeGroot)
_PAREN_REF_RE = re.compile(
    r"\("
    + _SURNAME                                          # First author
    + r"(?:\s+(?:and|&)\s+" + _SURNAME + r")*"          # optional: and Author
    + r"(?:\s+" + _ET_AL + r")?"                        # optional: et al. (bare or \textit{})
    + r",\s*\d{4}[a-z]?"                                # , Year
    + r"(?:"                                            # -- additional refs --
    + r"\s*;\s*"                                         #   semicolon separator
    + _SURNAME                                           #   Author
    + r"(?:\s+(?:and|&)\s+" + _SURNAME + r")*"           #   optional: and Author
    + r"(?:\s+" + _ET_AL + r")?"                        #   optional: et al. (bare or \textit{})
    + r",\s*\d{4}[a-z]?"                                 #   , Year
    + r")*"                                             # repeat for more refs
    + r"\)?"                                            # closing paren OPTIONAL
)

# References: \label, \ref, \eqref, \cite, \pageref, \cref, \autoref
_REF_RE = re.compile(
    r"\\(?:label|ref|eqref|cite|pageref|cref|autoref)\s*\{[^}]*\}"
)

# \href{URL}{display_text} -- protect the whole thing; display_text
# is handled specially during segmentation.
# Supports one level of nested braces in display_text so that
# \href{url}{\textbf{bold text}} is matched correctly.
_HREF_RE = re.compile(r"\\href\s*\{[^}]*\}\s*\{(?:[^{}]|\{[^{}]*\})*\}")
# 2026-09-29 (V2 denetimi): \url{...} korunmuyordu; model adres metnini
# çevirince ('\url{https://…}' -> '\dönüş{dönüş://…}') adres kayboluyor,
# tanımsız komut derlemeyi düşürüyordu (bugav c04). Adres MT'ye gitmez.
_URL_CMD_RE = re.compile(r"\\(?:url|nolinkurl)\s*\{[^{}]*\}")

# ---------------------------------------------------------------------------
# Figure / Table / Equation reference patterns in running text
# ---------------------------------------------------------------------------
# Matches "Figure 6.11", "Fig. 3.2a", "Table 3.2", "Equation 4.5", etc.
# These are stashed to prevent the MT model from corrupting the numbers.
# During stash the English label is translated to Turkish and the number
# is preserved exactly; during restore the Turkish form is placed back.
_FIGURE_REF_RE = re.compile(
    r"(?:Figures?|Figs?\.|Tables?|Equations?|Eqs?\.|Theorems?|Lemma[s]?"
    r"|Corollar(?:y|ies)|Propositions?|Definitions?|Examples?"
    r"|Exercises?|Algorithms?|Remarks?|Proofs?|Propert(?:y|ies)"
    r"|Sections?|Chapters?|Appendix|Appendices)"
    # Number part: supports both bare "7.1" and parenthesised "(7.1)".
    # The parenthesised form requires BOTH parens to avoid capturing a
    # stray trailing ")" from surrounding text like "(see Fig. 7.1)".
    r"\s*(?:\(\d+(?:\.\d+)*(?:[a-z])?\)|\d+(?:\.\d+)*(?:[a-z])?(?:\([a-z]\))?)"
    # Continuation: captures chained references like "7.4 and 7.5",
    # "2.6, 2.7", "3.1-3.5", "3.1 ve 3.2", "(4.35) and (4.36)".
    # The number after the separator is mandatory so
    # "Figure 7.4 and the results" does NOT greedily consume "and the".
    r"(?:"
    r"\s*(?:,\s*|(?:and|ve|[-\u2013\u2014])\s*)"
    r"(?:\(\d+(?:\.\d+)*(?:[a-z])?\)|\d+(?:\.\d+)*(?:[a-z])?(?:\([a-z]\))?)"
    r")*",
    re.IGNORECASE,
)

# Standalone parenthetical cross-reference numbers in running text.
# Matches "(7.1)", "(4.35a)", "(3.2)-(3.5)" -- numbers with at least
# one decimal point inside parentheses.  Single-level numbers like "(7)"
# are intentionally excluded to avoid false positives on list items.
# The requirement for \d+\.\d+ ensures these are cross-ref numbers
# (e.g. equation, figure, section numbers) not arbitrary parentheticals.
_STANDALONE_PAREN_REF_RE = re.compile(
    r"\(\d+\.\d+(?:\.\d+)*[a-z]?\)"
    r"(?:\s*(?:,\s*|(?:and|ve|[-\u2013\u2014])\s*)\(\d+\.\d+(?:\.\d+)*[a-z]?\))*",
)

# LATEX-B20: Context-aware single-digit parenthetical cross-refs.
# _STANDALONE_PAREN_REF_RE above requires a decimal point to avoid false
# positives on list items like "(1)", but misses single-digit references
# such as "Figure (12)" or "Eq. (3a)". This complementary pattern captures
# those *only* when immediately preceded by a reference-label keyword
# (English + Turkish). Python's re module disallows variable-width
# lookbehinds, so we use a capture group for the label and only stash
# the paren part (group 2) during substitution -- the label itself is
# handled by _FIGURE_REF_RE earlier.
# TODO(LATEX-B20): wire this into _stash_protected alongside
# _STANDALONE_PAREN_REF_RE once integration side-effects are verified.
_CONTEXTUAL_PAREN_REF_RE = re.compile(
    r"(\b(?:Figure|Equation|Section|Table|Fig|Eq|Sec|Tab"
    r"|Şekil|Denklem|Bölüm|Tablo)[\s.]+)"
    r"(\(\d+[a-z]?(?:\.\d+)*\))"
)

# Page references: p. 234, pp. 45-67, page 12, Pages 100-200
# 2026-09-29 (F3-01 yan bulgusu): 'p.' sözcük içinde eşleşmez — "Chap. 10"
# eskiden "Cha" + "p. 10" diye bölünüp geri yazımda "Cha p. 10" oluyordu.
_PAGE_REF_RE = re.compile(
    r"(?:(?<![^\W\d_])pp?\.\s*|[Pp]ages?\s+)\d+(?:\s*[-\u2013\u2014]\s*\d+)?",
)

# Index entries: \index{...} -- must be stashed to preserve index structure
# (KURALLAR Madde 6: "Indeks girisleri korunacak")
_INDEX_RE = re.compile(r"\\index\{(?:[^{}]|\{[^{}]*\})*\}")

# Footnote: \footnote{...} with up to 2 levels of nested braces.
# Optional [N] (or [symbol]) marker is accepted: \footnote[3]{...}.
_FOOTNOTE_RE = re.compile(
    r"\\footnote\s*(?:\[[^\]]*\])?\s*"
    r"\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}"
)

# \footnotetext[N]{...} -- same nesting as \footnote, optional [N] marker
_FOOTNOTETEXT_RE = re.compile(
    r"\\footnotetext\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}"
)

# \footnotemark[N] -- bare command, no brace content to translate
_FOOTNOTEMARK_RE = re.compile(
    r"\\footnotemark\s*(?:\[[^\]]*\])?"
)

# English label -> Turkish translation map for figure/table references.
_FIGURE_LABEL_MAP: dict[str, str] = {
    # Plural forms first (longer match takes priority in sorted-by-length)
    "Figures": "\u015eekil",      # Turkish: singular form used for cross-refs
    "Figure": "\u015eekil",       # Sekil
    "Figs.": "\u015eek.",
    "Fig.": "\u015eek.",          # Sek.
    "Tables": "Tablo",
    "Table": "Tablo",
    "Equations": "Denklem",
    "Equation": "Denklem",
    "Eqs.": "Denk.",
    "Eq.": "Denk.",
    "Theorems": "Teorem",
    "Theorem": "Teorem",
    "Lemmas": "Lemma",
    "Lemma": "Lemma",             # Matematik: yardımcı teorem (Türkçe'de de Lemma)
    "Corollaries": "Sonu\u00e7",
    "Corollary": "Sonu\u00e7",   # Sonuc
    "Propositions": "\u00d6nerme",
    "Proposition": "\u00d6nerme", # Onerme
    "Definitions": "Tan\u0131m",
    "Definition": "Tan\u0131m",   # Tanim
    "Examples": "\u00d6rnek",
    "Example": "\u00d6rnek",      # Ornek
    "Exercises": "Al\u0131\u015ft\u0131rma",
    "Exercise": "Al\u0131\u015ft\u0131rma",  # Alistirma
    "Algorithms": "Algoritma",
    "Algorithm": "Algoritma",
    "Sections": "B\u00f6l\u00fcm",
    "Section": "B\u00f6l\u00fcm", # Bolum
    "Chapters": "B\u00f6l\u00fcm",
    "Chapter": "B\u00f6l\u00fcm", # Bolum
    "Appendices": "Ek",
    "Appendix": "Ek",
    "Remarks": "Not",
    "Remark": "Not",
    "Proofs": "İspat",
    "Proof": "İspat",
    "Properties": "\u00d6zellik",
    "Property": "\u00d6zellik",
}


def _translate_figure_ref(match: re.Match) -> str:
    """Translate an English figure/table reference to Turkish.

    Example: "Figure 6.11" -> "Sekil 6.11" (with proper Turkish chars).
    Also handles uppercase variants: "FIGURE 7.3" -> "Sekil 7.3".
    The number portion is preserved exactly as-is.
    """
    full = match.group(0)
    full_lower = full.lower()
    # Try longest labels first to avoid "Fig." matching before "Figure"
    for en_label in sorted(_FIGURE_LABEL_MAP.keys(), key=len, reverse=True):
        if full_lower.startswith(en_label.lower()):
            number_part = full[len(en_label):].lstrip()
            # Normalise continuation conjunctions to Turkish: "and" -> "ve"
            number_part = re.sub(
                r"\s+and\s+", " ve ", number_part, flags=re.IGNORECASE,
            )
            tr_label = _FIGURE_LABEL_MAP[en_label]
            return f"{tr_label}\u00a0{number_part}"  # non-breaking space
    return full  # fallback (should not happen)


# ---------------------------------------------------------------------------
# 2026-09-30 (F1, MT-10): \u0130ngilizce DI\u015eI kaynakta at\u0131f etiketleri
# ---------------------------------------------------------------------------
# _FIGURE_REF_RE yaln\u0131z \u0130ngilizce etiketleri tan\u0131r. Almanca kaynakta
# "(Abb. 1.1)", "Tab. 1.1", "Gl. (1.3)" korunmadan modele gidiyordu; model
# atf\u0131 yutabiliyor ("\u2026besitzen (Abb. 1.1). Im station\u00e4ren\u2026" par\u00e7as\u0131
# "Bir yerde s\u0131cakl\u0131k zamanla de\u011fi\u015fmez" oldu: hymt2-de, QC CRITICAL "1.1
# atf\u0131 eksik") ya da etiketi Almanca b\u0131rak\u0131yordu (gptoss-de: "Gl. (1.3)",
# "Tab. 1.1", "\(\text{Gl. (2.3)}\)"). Etiket s\u00f6zc\u00fcklerinin tek kayna\u011f\u0131
# patterns.XREF_LABEL_WORDS; at\u0131f \u0130ngilizce yoldaki gibi T\u00fcrk\u00e7e etiket +
# AYNEN numara olarak yer tutucuya al\u0131n\u0131r. \u0130ngilizce kaynakta bu ad\u0131m
# \u00c7ALI\u015eMAZ (\u0130ngilizce yol birebir ayn\u0131).
# kategori -> (tam etiket, k\u0131saltma etiketi); k\u0131saltma = kaynak etiket
# noktayla bitiyorsa ("Abb.", "Gl.", "\u0420\u0438\u0441.").
_LOCAL_XREF_TR_LABELS: Dict[str, Tuple[str, str]] = {
    "figure": ("\u015eekil", "\u015eek."),
    "table": ("Tablo", "Tablo"),
    "equation": ("Denklem", "Denk."),
    "section": ("B\u00f6l\u00fcm", "B\u00f6l\u00fcm"),
    "chapter": ("B\u00f6l\u00fcm", "B\u00f6l\u00fcm"),
}
_LOCAL_XREF_NUM = r"(?:\(\d+(?:\.\d+)*[a-z]?\)|\d+(?:\.\d+)*[a-z]?)"
# Zincir ba\u011fla\u00e7lar\u0131 (kaynak diller): "Abb. 1.1 und 1.2" -> "\u015eek. 1.1 ve 1.2".
_LOCAL_XREF_CONJ_RE = re.compile(
    r"\s+(?:and|und|et|y|e|\u0438)\s+", re.IGNORECASE)
_LOCAL_XREF_CACHE: Dict[str, List[Tuple["re.Pattern[str]", str]]] = {}


def _local_xref_patterns(lang: str) -> List[Tuple["re.Pattern[str]", str]]:
    """Kaynak dilin at\u0131f desenleri: [(desen, kategori), ...] (\u00f6nbellekli)."""
    if lang in _LOCAL_XREF_CACHE:
        return _LOCAL_XREF_CACHE[lang]
    from latex_pipeline.patterns import XREF_LABEL_WORDS
    out: List[Tuple["re.Pattern[str]", str]] = []
    for cat in _LOCAL_XREF_TR_LABELS:
        alt = XREF_LABEL_WORDS.get(cat, {}).get(lang)
        if not alt:
            continue
        out.append((re.compile(
            r"(?<![^\W\d_])(?P<label>" + alt + r")(?![^\W\d_])"
            r"(?:\s|~|\u00a0)*"
            r"(?P<num>" + _LOCAL_XREF_NUM
            + r"(?:\s*(?:,\s*|(?:and|und|et|y|e|\u0438)\s+|[-\u2013\u2014]\s*)"
            + _LOCAL_XREF_NUM + r")*)",
            re.IGNORECASE,
        ), cat))
    _LOCAL_XREF_CACHE[lang] = out
    return out


def _translate_local_xref(match: re.Match, category: str) -> str:
    """"Abb. 1.1" -> "\u015eek. 1.1", "Tabelle 2 und 3" -> "Tablo 2 ve 3"."""
    label = match.group("label")
    full, abbrev = _LOCAL_XREF_TR_LABELS[category]
    tr_label = abbrev if label.endswith(".") else full
    number_part = _LOCAL_XREF_CONJ_RE.sub(" ve ", match.group("num"))
    return f"{tr_label}\u00a0{number_part}"


# 2026-09-30 (F1, MT-10): k\u0131sa \textsuperscript{..} / \textsubscript{..}
# (dipnot i\u015fareti, \u00fcs, "1\textsuperscript{er}") MT'ye HAM LaTeX olarak
# gidiyordu; model komutu \u00e7evirip ('\d\u00fczlem{1}') ya da d\u00fcz metne '^{1}'
# yaz\u0131p (hymt-de 1. tur) bozabiliyordu. \u0130\u00e7 i\u00e7e s\u00fcsl\u00fcs\u00fcz, k\u0131sa i\u00e7erikli
# bi\u00e7im yer tutucuya al\u0131n\u0131r (i\u00e7erik \u00e7evrilecek d\u00fczyaz\u0131 de\u011fildir).
_TEXT_SCRIPT_RE = re.compile(r"\\text(?:super|sub)script\s*\{[^{}\n]{1,16}\}")


# 2026-09-30 (F1, kayıpsızlık): yer tutucu kaybında düşülen PARÇALI yolda
# parça kenarındaki, komşu yer tutucuya ait parantez modele gidiyordu; model
# parça sonundaki "(" ya da başındaki ")" işaretini düşürüyordu
# (bir model denemesinde: "constant ( $k$ ) and … ( $T_0$ )" ->
# "bulunur. $k$ ) ve … ( $T_0$ sıcaklığın…"). Baştaki kapanış ve
# sondaki açılış parantezleri (ve kapanıştan hemen sonraki tek noktalama)
# parçadan ayrılır, yalnız çekirdek çevrilir, kenarlar aynen geri eklenir.
# Parçanın kendi içindeki parantez çiftine dokunulmaz.
_PIECE_EDGE_RE = re.compile(
    r"^(\s*[)\]]+[.,;:]?\s*)?(.*?)(\s*[(\[]+\s*)?$", re.DOTALL)


def _split_piece_brackets(piece: str) -> Tuple[str, str, str]:
    """Parça -> (baş kenar, çekirdek, son kenar); çekirdek < 3 harfse kenar yok."""
    m = _PIECE_EDGE_RE.match(piece)
    lead, core, trail = (m.group(1) or ""), m.group(2), (m.group(3) or "")
    if len(core.strip()) < 3:
        return "", piece, ""
    return lead, core, trail


# Standalone LaTeX commands that carry no translatable text.
# Split into two patterns: commands WITH arguments (end with }) don't
# need \b, while bare commands need \b to avoid false matches
# (e.g. \par must NOT match \parbox).
_STANDALONE_CMD_WITH_ARG_RE = re.compile(
    r"\\(?:vspace\*?\{[^}]*\}"
    r"|hspace\*?\{[^}]*\}"
    r"|thispagestyle\{[^}]*\}"
    r"|pagestyle\{[^}]*\}"
    r"|setcounter\{[^}]*\}\{[^}]*\}"
    r"|rule\{[^}]*\}\{[^}]*\}"
    r")"
)
_STANDALONE_CMD_BARE_RE = re.compile(
    r"\\(?:par|noindent|newline|newpage|clearpage|cleardoublepage"
    r"|bigskip|medskip|smallskip"
    r"|vfill|hfill|centering"
    r"|raggedright|raggedleft|maketitle|tableofcontents"
    r"|listoffigures|listoftables"
    # G2a-2 (2026-10-05): \appendix (G1a-1 dönüştürücüsü basıyor) modele
    # gitmez; eskiden satır düz metin olarak MT'ye gidiyordu, model bozarsa
    # ekler numaralı bölüme dönerdi.
    r"|frontmatter|mainmatter|backmatter|appendix"
    r")\b"
)

# B4 fix: Çıplak booktabs / tablo kuralı komutları. Normalde bunlar
# _TABULAR_ENV_RE ile tüm tabular bloğu içinde stash'lenir. Ancak Marker
# PDF'den TOC'u bazen bozuk tabular'a çeviriyor ve \toprule / \midrule /
# \bottomrule komutları tabular bloğunun DIŞINDA kalabiliyor. Bu durumda
# MT'ye "toprule Chapter" metin olarak gidiyor ve anlamsız çeviri üretiyor.
# Bu regex çıplak kalan komutları yakalar ve _stash_protected içinde
# stash'lenir (tabular zaten önce stash'lendiği için burada kalan sadece
# "çıplak" olanlardır, çift stash riski yoktur).
_BOOKTABS_BARE_RE = re.compile(
    r"\\(?:toprule|midrule|bottomrule|hline|addlinespace|cmidrule|cline)"
    r"(?:\([^)]*\))?(?:\[[^\]]*\])?(?:\{[^}]*\})?"
)

# List/titlepage environment begin/end markers
# C (2026-10-06): \begin satırındaki seçenek ([start=N], enumitem) yer
# tutucunun İÇİNDE kalır. Dönüştürücü 1'den başlamayan numaralı listeyi
# \begin{enumerate}[start=N] yazar; eskiden yalnız \begin{enumerate} yer
# tutucuya alınıyor, '<<LATEX_n>>[start=2]' modele gidiyor, 'start' sözcüğü
# çevrilip enumitem anahtarı bozuluyordu ('[yüzey=2]': liste yine 1'den
# sayar, derleme günlüğünde tanımsız anahtar hatası).
_LIST_ENV_RE = re.compile(
    r"\\begin\{(?:itemize|enumerate|description)\}(?:\[[^\]\n]*\])?"
    r"|\\end\{(?:itemize|enumerate|description)\}"
    # E (2026-10-06, mt okuma MT-24): dönüştürücünün kaynak PDF kutusu
    # ortamı (kutu_onarim.ORTAM = kckutu) da yer tutucuya alınır; satırı
    # kendi başına durduğu için modele hiç gitmez (eskiden
    # "\begin{kckutu}" satırı modele gidiyor, model öne/arkaya metin
    # ekleyebiliyordu).
    r"|\\(?:begin|end)\{" + _KUTU_ORTAMI + r"\}"
)

# Titlepage: the ENTIRE block should be stashed so that brace structure
# and formatting commands are never corrupted by the translator.
_TITLEPAGE_ENV_RE = re.compile(
    r"\\begin\{titlepage\}.*?\\end\{titlepage\}",
    re.DOTALL,
)

# Bibliography: the entire thebibliography block should be stashed
# so that author names, titles, journal names are never translated.
_BIBLIOGRAPHY_ENV_RE = re.compile(
    r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# G2a-2 (2026-10-05): kaynakça bölümü — mt okuma MT-19, KARAR 20
# ---------------------------------------------------------------------------
# hymt2-de: "\section*{Literatur}" bölümünün girdileri modele gitti; eser
# adları çevrildi, "3. Aufl." "3. Auflage" oldu, başlık "Kütüphanecilik"
# (MT-32). Eski koruma yalnız İngilizce/Türkçe adları ve \chapter/\section
# düzeyini tanıyordu; üstelik başlığı da yer tutucuya alıp ÇEVİRMİYORDU
# (İngilizce kitapta "References" İngilizce kalıyordu). Yeni kural (her
# kaynak dil):
#   * başlık adı patterns.BIBLIOGRAPHY_HEADING_NAMES'te (altı kaynak dil +
#     Türkçe; dönüştürücü ve retranslator ile AYNI küme) olan \chapter …
#     \subsubsection başlığının ALTINDAKİ bölge — aynı ya da üst düzeyde
#     sonraki başlığa (ya da \appendix / \backmatter / \end{document}) kadar —
#     yer tutucuya alınır: yazar, eser adı, yayın bilgisi modele gitmez.
#     Bölgenin iç başlıkları ("Primary Sources") gövdede kalır, başlık
#     olarak çevrilir; aradaki girdiler ayrı yer tutucularda korunur;
#   * başlık satırı gövdede kalır, başlık metni modele GİTMEDEN çevrilir
#     (LatexTranslator._bibliography_heading_tr): İngilizce kaynakta başlık
#     sözlüğü (References → Kaynaklar, Bibliography → Kaynakça), öteki adlarda
#     BIBLIOGRAPHY_HEADING_TR. Numara öneki ve \addcontentsline aynen kalır
#     (KARAR 20: kaynaktaki gibi numaralı/numarasız, İçindekiler'de).
# thebibliography ortamı (başlığını LaTeX \refname/\bibname'den basar)
# eskisi gibi bütün olarak korunur (_BIBLIOGRAPHY_ENV_RE).
BIBLIOGRAPHY_HEADING_TR = "Kaynakça"
# Kümede zaten Türkçe olan adlar: başlık olduğu gibi kalır.
_BIB_TURKISH_KEYS = frozenset({"kaynakca", "kaynaklar"})
# Başlık düzeyleri: bölge, düzeyi kaynakça başlığınınkine eşit ya da daha
# üst (sayıca küçük) ilk başlıkta biter.
_BIB_HEADING_LEVELS = {"part": -1, "chapter": 0, "section": 1,
                       "subsection": 2, "subsubsection": 3}
_BIB_LINE_HEADING_RE = re.compile(
    r"[ \t]*\\(part|chapter|section|subsection|subsubsection)\*?"
    r"\s*(?:\[[^\]\n]*\])?\s*\{([^{}\n]*)\}")
_BIB_REGION_STOP_RE = re.compile(
    r"[ \t]*\\(?:appendix|backmatter|end\{document\})(?![A-Za-z])")
# Numaralı kaynakça başlığı ("5 Literatur", "2.4 References"): numara adın
# parçası değildir (ad eşlemesinden önce atılır, başlıkta aynen kalır).
_BIB_NUM_PREFIX_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[a-zA-Z]?\.?|[A-Z](?:\.\d+)+\.?)\s+")


def bibliography_heading_key(title: str) -> Optional[str]:
    """Başlık metni bir kaynakça adı mı? Evetse katlanmış ad (küçük harf,
    aksansız; ``patterns.BIBLIOGRAPHY_HEADING_NAMES`` biçimi), değilse None.

    Numara öneki ve sondaki `` .:`` atılır (G2a-2)."""
    from latex_pipeline.patterns import BIBLIOGRAPHY_HEADING_NAMES, fold_accents
    name = _BIB_NUM_PREFIX_RE.sub("", (title or "").strip())
    key = " ".join(fold_accents(name).lower().split()).strip(" .:")
    return key if key in BIBLIOGRAPHY_HEADING_NAMES else None


def bibliography_regions(text: str) -> List[Tuple[int, int, str]]:
    """Kaynakça bölgeleri: ``[(başlık_satırı_sonu, bölge_sonu, başlık)]``.

    Bölge başlık satırından SONRA başlar (başlık satırı bölgeye girmez) ve
    düzeyi başlığınkine eşit ya da daha üst ilk başlık satırında, ya da
    ``\\appendix`` / ``\\backmatter`` / ``\\end{document}`` satırında biter
    (#57: bölüm içi kaynakça sonraki \\section'ı yutmaz)."""
    lines = text.splitlines(keepends=True)
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    regions: List[Tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        m = _BIB_LINE_HEADING_RE.match(lines[i])
        if m is None or bibliography_heading_key(m.group(2)) is None:
            i += 1
            continue
        level = _BIB_HEADING_LEVELS[m.group(1)]
        j = i + 1
        while j < len(lines):
            mj = _BIB_LINE_HEADING_RE.match(lines[j])
            if mj is not None and _BIB_HEADING_LEVELS[mj.group(1)] <= level:
                break
            if _BIB_REGION_STOP_RE.match(lines[j]):
                break
            j += 1
        end = offsets[j] if j < len(lines) else len(text)
        regions.append((offsets[i] + len(lines[i]), end, m.group(2)))
        i = j
    return regions


def _stash_bibliography_sections(text: str, push) -> Tuple[str, int]:
    """Kaynakça bölgelerinin girdilerini yer tutucuya al (G2a-2).

    İç başlık satırları gövdede kalır; aralarındaki her öbek boşluk kenarları
    korunarak tek yer tutucu olur (paragraf ayrımı ve geri dönüş birebir).
    Döner ``(metin, bölge_sayısı)``."""
    regions = bibliography_regions(text)
    if not regions:
        return text, 0

    def flush(chunk: List[str], out: List[str]) -> None:
        block = "".join(chunk)
        core = block.strip()
        if not core:
            out.append(block)
            return
        lead = block[:len(block) - len(block.lstrip())]
        trail = block[len(block.rstrip()):]
        out.append(lead + push(core) + trail)

    out: List[str] = []
    pos = 0
    for start, end, title in regions:
        out.append(text[pos:start])
        chunk: List[str] = []
        n_lines = 0
        for line in text[start:end].splitlines(keepends=True):
            if _BIB_LINE_HEADING_RE.match(line):
                flush(chunk, out)
                chunk = []
                out.append(line)
                continue
            chunk.append(line)
            n_lines += bool(line.strip())
        flush(chunk, out)
        pos = end
        logger.info("Kaynakça bölümü korundu (girdiler modele gitmez, "
                    "yalnız başlık çevrilir): '%s', %d satır.",
                    title.strip(), n_lines)
    out.append(text[pos:])
    return "".join(out), len(regions)


# #57 (2026-07-17) sınır kuralının belgesi: bölüm içi \section{References}
# yalnız KENDİ section'ını kapsar (lookahead'de \section var). G2a-2'den beri
# _stash_protected bunun yerine bibliography_regions'ı (altı dil, her düzey,
# aynı sınır kuralı) kullanır; desen kuralı sabitleyen test için duruyor
# (tests/test_yol_audit_20260716.py).
_BIBLIOGRAPHY_SECTION_ONLY_RE = re.compile(
    r"\\section\*?\s*"
    r"\{(?:References|Bibliography|Kaynaklar|Kaynak[cç]a)\}"
    r"(?:.*?)"
    r"(?=\\(?:chapter|part|section|backmatter|appendix)\b"
    r"|\\end\{document\}|\Z)",
    re.DOTALL,
)

# figure/table environment wrappers (but NOT their content -- we peel
# captions out separately)
_FLOAT_BEGIN_RE = re.compile(r"\\begin\{(figure|table)\}\s*(?:\[[^\]]*\])?")
_FLOAT_END_RE = re.compile(r"\\end\{(figure|table)\}")


# ---------------------------------------------------------------------------
# Heading / caption patterns (translate inner text)
# ---------------------------------------------------------------------------

# \chapter{...}, \chapter*{...}, \section{...}, \section*{...}, etc.
# Supports up to TWO levels of nested braces so that constructs like
#   \section{The \texorpdfstring{$\alpha$}{alpha} Method}      (1 level)
#   \section{The \texorpdfstring{$\alpha$}{alpha} \textbf{Method}}  (2 levels)
# are captured correctly.
# Inner pattern breakdown:
#   [^{}]                           -- plain text (no braces)
#   \{(?:[^{}]|\{[^{}]*\})*\}      -- level-1 group, may contain level-2 groups
# 2026-07-11: \part eklendi + opsiyonel kısa başlık ([TOC başlığı]) desteği.
# Eskiden \part hiç eşleşmiyordu ve \chapter[kısa]{uzun} formunda regex
# takılıyordu -> bu başlıklar başlık tek-otoritesini tamamen atlıyor,
# TOC İngilizce kalıyordu.
_HEADING_RE = re.compile(
    r"(\\(?:part|chapter|section|subsection|subsubsection)\*?)"
    r"(\[(?:[^\[\]{}]|\{[^{}]*\})*\])?"
    r"\s*"
    r"\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}"
)

# \caption{...} (supports up to two levels of nested braces, consistent
# with _HEADING_RE above)
# 2026-09-30 (F1, MT-2): yıldızlı biçim (\caption*) ve isteğe bağlı kısa
# altyazı ([liste başlığı]) de eşleşir. Dönüştürücü görsele bağladığı
# altyazıyı \caption* yazar (numara metinde kalır); eski desen yalnız
# '\caption{' tanıyordu, paragraf aşaması da altyazı satırını "Phase 2'de
# çevrildi" diye atladığı için \caption* altyazıları HİÇ çevrilmiyordu
# (bir model denemesinde 9/9 altyazı İngilizce). Grup 1 = komut (+ kısa altyazı),
# grup 2 = altyazı metni (sıra korunmalı: tests/test_fix_F1_20260930.py
# grup 2'yi okur).
_CAPTION_RE = re.compile(
    r"(\\caption\*?(?:\s*\[(?:[^\[\]{}]|\{[^{}]*\})*\])?)"
    r"\s*\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}"
)
# _CAPTION_RE grup 1'inden kısa altyazıyı ayırır: (komut, "[...]" ya da "").
_CAPTION_CMD_SPLIT_RE = re.compile(r"(\\caption\*?)\s*(\[.*\])?\s*\Z", re.DOTALL)

# Paragraf aşamasının (Phase 3) Phase 2'de çevrildiği için ATLADIĞI satırlar:
# başlık / altyazı komutuyla başlayan satır. Kısa başlık/altyazı ([...])
# isteğe bağlıdır (F1, 2026-09-30): eskiden '\caption[kısa]{...}' satırı
# atlanmıyor, Phase 2'deki çevirinin ÜSTÜNE ham LaTeX'iyle yeniden MT'ye
# gidiyordu. Üç atlama noktasının tek kaynağı.
_STRUCTURED_LINE_RE = re.compile(
    r"\\(?:chapter|section|subsection|subsubsection|caption)\*?\s*"
    r"(?:\[(?:[^\[\]{}]|\{[^{}]*\})*\])?\s*\{"
)


# ---------------------------------------------------------------------------
# G2a-1 (2026-10-05): bozuk çıktı basılmasın — yerel model zinciri bekçileri
# ---------------------------------------------------------------------------
# Kanıt: iç okuma bulguları (mt okuma MT-1, MT-7, MT-17) ve model
# denemelerinin çıktıları (iç denetim notu).
#  (1) Bütün çevirisi tutmayan parça artık yer tutucu sınırlarından bölünüp
#      parça parça çevrilmez ("parçalı yol" bir model denemesinde 13 satırın 13'ünü,
#      hymt2-de'de 2 satırın 2'sini okunamaz bastı). Sıra: aynı metin bir kez
#      daha → sade biçim (kısa değişken yer tutucusu düz metin, ham komut
#      kabuğu yer tutucu) → hiçbiri tutmazsa parça özgün dilinde kalır ve
#      işaretlenir (LatexTranslator._recover_segment).
#  (2) Sayı kaybı ve kaynakta olmayan ilişki simgesi (≤ ≥ ≈ ≠ …) uyarıyla
#      geçmez, parça geri alınır ("Bi < 0,1" → "Bi < ≤ 0 , ≤ 1" basılmıştı).
#  (3) Her parçada LaTeX iskeleti (komut adları ve sırası, ayraç dengesi,
#      metnin hangi ayraç grubunda durduğu) kaynakla karşılaştırılır.

# Yer tutucuya benzeyen her belirteç: <<LATEX_3>>, <<CMD_0>>, retranslator'ın
# <<RT_2>> / <<RTENV_0>> belirteçleri ve iç işaretleri, bozuk biçimler
# ("<< LATEX_3 >>", talimat yankısı "<<LATEX_N>>"). Sayı, ilişki simgesi ve
# iskelet denetimleri bunları atomik sayar (içlerindeki rakam ve '<' '>'
# işaretleri metin değildir).
_ANY_PLACEHOLDER_RE = re.compile(
    r"<<\s*[A-Za-z][^<>\n]{0,24}>>|\uE100RTSTASH_\d+\uE100|\uE101RTENV_\d+\uE101")

# Çeviride kaynaktakinden FAZLA görünmesi uydurma sayılan ilişki simgeleri
# (mt okuma MT-7). Sayım kaynakla karşılaştırılır: kaynakta "x= const" varsa
# çeviride bir "=" serbesttir.
RELATION_SYMBOLS = "<>=≤≥≠≈≡∼≃≅∝≪≫⩽⩾≲≳"
_RELATION_SYMBOL_RE = re.compile("[" + re.escape(RELATION_SYMBOLS) + "]")
# Sayı bekçisinin ölçütü (NUM-RETRY, 2026-07-06 ile aynı): kaynaktaki 2+
# haneli her sayı çeviride bulunmalı. Tek hane yazıyla ("bir") yazılabilir.
_SIGNIFICANT_NUMBER_RE = re.compile(r"\d{2,}")


def _without_placeholders(text: str) -> str:
    """Yer tutucuları BOŞLUKLA değiştir (G2a-1).

    ``0<<LATEX_65>>1`` iki ayrı rakamdır; eski boşluksuz silme onu "01"
    sayısına çevirip doğru çeviride de yanlış "Numbers lost" üretiyordu
    (hymt2-de pipeline.log:87).
    """
    return _ANY_PLACEHOLDER_RE.sub(" ", text)


def missing_numbers(source: str, translated: str) -> set:
    """Kaynaktaki 2+ haneli sayılardan çeviride bulunmayanlar (boş = tamam).

    Türkçe ondalık virgül noktaya çevrilerek karşılaştırılır (eski davranış).
    """
    src = set(_SIGNIFICANT_NUMBER_RE.findall(_without_placeholders(source)))
    if not src:
        return set()
    out = set(_SIGNIFICANT_NUMBER_RE.findall(
        _without_placeholders(translated).replace(",", ".")))
    return src - out


def new_relation_symbols(source: str, translated: str) -> Counter:
    """Çeviride kaynaktakinden FAZLA görünen ilişki simgeleri (boş = tamam)."""
    src = Counter(_RELATION_SYMBOL_RE.findall(_without_placeholders(source)))
    out = Counter(_RELATION_SYMBOL_RE.findall(_without_placeholders(translated)))
    return out - src


# LaTeX iskeleti: komut adı (\textbf, \caption*, \% gibi kaçışlı simgeler),
# süslü ayraç ve çıplak '$'. Yer tutucu atomiktir (metin sayılır).
_SKELETON_TOKEN_RE = re.compile(
    r"(?P<ph>" + _ANY_PLACEHOLDER_RE.pattern + r")"
    r"|(?P<cmd>\\(?:[A-Za-z]+\*?|[^A-Za-z\s]))"
    r"|(?P<brace>[{}])"
    r"|(?P<dollar>\$)"
)
# İskelette "metin var" ölçütü: harf ya da rakam. Yalnız noktalama sayılmaz
# (":" ayraç dışına çıkabilir, anlamı ve dizgiyi bozmaz).
_SKELETON_TEXT_RE = re.compile(r"[^\W_]")
# Günlükte gösterilen en çok iskelet öğesi.
_SKELETON_LOG_ITEMS = 12


def latex_skeleton(text: str) -> Tuple[Tuple[str, ...], Tuple[bool, ...]]:
    """``(dizi, dolu)``: dizi komut adları, ``{``, ``}`` ve ``$`` sırayla;
    dolu her süslü ayraç grubunun (açılış sırasıyla; 0 = en dış düzey) harf,
    rakam ya da yer tutucu taşıyıp taşımadığı."""
    seq: List[str] = []
    filled: List[bool] = [False]
    stack: List[int] = [0]
    pos = 0
    for m in _SKELETON_TOKEN_RE.finditer(text):
        if _SKELETON_TEXT_RE.search(text, pos, m.start()):
            filled[stack[-1]] = True
        pos = m.end()
        kind = m.lastgroup
        if kind == "ph":
            filled[stack[-1]] = True
        elif kind == "cmd":
            seq.append(m.group(0))
        elif kind == "brace":
            seq.append(m.group(0))
            if m.group(0) == "{":
                filled.append(False)
                stack.append(len(filled) - 1)
            elif len(stack) > 1:
                stack.pop()
        else:
            seq.append("$")
    if _SKELETON_TEXT_RE.search(text, pos):
        filled[stack[-1]] = True
    return tuple(seq), tuple(filled)


def latex_skeleton_problem(source: str, translated: str) -> Optional[str]:
    """Çeviri kaynağın LaTeX iskeletini bozduysa kısa Türkçe neden, yoksa None.

    Karşılaştırılan: komut adları ve sırası, süslü ayraç dizisi (denge
    dahil), çıplak ``$`` ve metnin hangi ayraç grubunda durduğu. mt okuma
    MT-17: ``\\caption*{\\textbf{Şekil} 1.1:} Durağan …`` (altyazı metni grubun
    dışına çıkmış), gpt-oss'ta kabuk düşmüş, başka bir model denemesinde
    ``h\\textasciitilde{}`` düşmüş. Yer tutucular atomiktir; Türkçe söz dizimi
    onları ve en dış düzeydeki metni serbestçe taşıyabilir. F1'in yalnız
    altyazıya uyguladığı denetimin (retranslator._latex_skeleton_kept)
    her parçaya genelleştirilmiş, sıraya duyarlı biçimi.
    """
    s_seq, s_fill = latex_skeleton(source)
    t_seq, t_fill = latex_skeleton(translated)
    if s_seq != t_seq:
        return "komut/ayraç dizisi değişti (%s → %s)" % (
            " ".join(s_seq[:_SKELETON_LOG_ITEMS]) or "-",
            " ".join(t_seq[:_SKELETON_LOG_ITEMS]) or "-")
    if s_fill != t_fill:
        return "metin ayraç grubunun dışına ya da içine kaydı"
    return None


# Metin korumalı çeviri (iskelet tutmayınca): ham komut kabukları <<CMD_n>>
# yer tutucusuna alınır, argümanlarındaki METİN cümlede kalır. Kimlik tabanı:
# _stash_inline_commands aynı biçimi 0'dan sayar, çakışmasın.
LATEX_SHELL_ID_BASE = 900
_LATEX_SHELL_TOKEN = "<<CMD_{}>>"
_LATEX_SHELL_TOKEN_RE = re.compile(r"<<CMD_\d+>>")
_LATEX_CMD_AT_RE = re.compile(r"\\(?:[A-Za-z]+\*?|[^A-Za-z\s])")
_LATEX_SHELL_OPTARG_RE = re.compile(r"[ \t]*\[[^\[\]{}\n]*\]")


def _brace_group_end(text: str, start: int) -> int:
    """``text[start] == '{'`` ise eşleşen ``}``'ın SONRASI; dengesizse -1."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def protect_latex_shells(text: str, id_base: int = LATEX_SHELL_ID_BASE):
    """Ham LaTeX komut kabuklarını yer tutucuya al (metin korumalı çeviri).

    ``\\textbf{Tabelle 1.1:} Richtwerte`` →
    ``<<CMD_900>>Tabelle 1.1:<<CMD_901>> Richtwerte``. Komutun SON süslü
    argümanındaki metin cümlede kalır (bağlamıyla çevrilir); önceki
    argümanlar ve ``[...]`` seçenekleri açılış belirtecine girer
    (``\\href{url}{``). Argümansız komut, kaçışlı simge (``\\%``, ``\\&``),
    çıplak ``$``, eşsiz ayraç ve satır sonu yorumu atomik belirteçtir. Var
    olan yer tutucular olduğu gibi kalır.

    Döner: ``(korunan_metin, kabuklar)``; ham komut yoksa None. Kabuk öğesi:
    ``{"ph", "orig", "kind": "atom"|"open"|"close", "pair", "word"}``.
    """
    shells: List[Dict] = []
    out: List[str] = []
    closers: List[Tuple[int, int]] = []   # (çift no, kapanış '}' konumu)
    n = len(text)
    i = 0
    pairs = 0

    def emit(orig: str, kind: str, pair: Optional[int] = None) -> None:
        ph = _LATEX_SHELL_TOKEN.format(id_base + len(shells))
        shells.append({"ph": ph, "orig": orig, "kind": kind, "pair": pair,
                       # argümansız denetim sözcüğü: ardına harf gelirse '{}'
                       "word": kind == "atom" and orig[-1:].isalpha()})
        out.append(ph)

    while i < n:
        m = _ANY_PLACEHOLDER_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        c = text[i]
        if closers and c == "}" and i == closers[-1][1]:
            emit("}", "close", closers.pop()[0])
            i += 1
            continue
        if c == "\\":
            m = _LATEX_CMD_AT_RE.match(text, i)
            if m is None or not m.group(0)[1:2].isalpha():
                tok = m.group(0) if m else c
                emit(tok, "atom")
                i += len(tok)
                continue
            k = m.end()
            last: Optional[Tuple[int, int]] = None
            while True:
                mo = _LATEX_SHELL_OPTARG_RE.match(text, k)
                if mo:
                    k = mo.end()
                    continue
                ws = k
                while ws < n and text[ws] in " \t":
                    ws += 1
                if ws < n and text[ws] == "{":
                    end = _brace_group_end(text, ws)
                    if end < 0:
                        break
                    last = (ws, end)
                    k = end
                    continue
                break
            if last is None:
                emit(text[i:k], "atom")
                i = k
                continue
            emit(text[i:last[0] + 1], "open", pairs)
            closers.append((pairs, last[1] - 1))
            pairs += 1
            i = last[0] + 1
            continue
        if c == "{":
            end = _brace_group_end(text, i)
            if end < 0:
                emit(c, "atom")
            else:
                emit(c, "open", pairs)
                closers.append((pairs, end - 1))
                pairs += 1
            i += 1
            continue
        if c in "}$":
            emit(c, "atom")
            i += 1
            continue
        if c == "%":
            emit(text[i:], "atom")
            break
        out.append(c)
        i += 1
    if not shells:
        return None
    return "".join(out), shells


def _shell_group_fill(text: str, by_ph: Dict[str, Dict]) -> Optional[Dict[int, bool]]:
    """Kabuk çiftlerinin sırası/iç içeliği geçerliyse her çiftin dolu/boş
    durumu (harf, rakam ya da başka yer tutucu = dolu); geçersizse None."""
    fill: Dict[int, bool] = {}
    stack: List[int] = []
    pos = 0
    for m in _ANY_PLACEHOLDER_RE.finditer(text):
        if stack and _SKELETON_TEXT_RE.search(text, pos, m.start()):
            fill[stack[-1]] = True
        pos = m.end()
        sh = by_ph.get(m.group(0))
        if sh is None:
            if stack:
                fill[stack[-1]] = True
            continue
        if sh["kind"] == "open":
            stack.append(sh["pair"])
            fill.setdefault(sh["pair"], False)
        elif sh["kind"] == "close":
            if not stack or stack[-1] != sh["pair"]:
                return None
            stack.pop()
    if stack:
        return None
    return fill


def restore_latex_shells(translated: str, protected_source: str,
                         shells: List[Dict]) -> Optional[str]:
    """:func:`protect_latex_shells`'in tersi; çeviri tutarsızsa None.

    Reddedilir: kabuk belirteci kayıp ya da kopya; açılış/kapanış sırası ya da
    iç içeliği bozuk; bir grubun metni dışarı taşmış ya da boş grup dolmuş
    (kaynakla karşılaştırılır). Modelin kabuk içine eklediği kenar boşluğu
    kaynakta yoksa atılır; argümansız denetim sözcüğünün ardına harf gelirse
    ``{}`` eklenir (``\\ldots{}metin``; komut adı metne yapışmaz).
    """
    by_ph = {s["ph"]: s for s in shells}
    counts = Counter(m.group(0) for m in _LATEX_SHELL_TOKEN_RE.finditer(translated)
                     if m.group(0) in by_ph)
    if any(counts[s["ph"]] != 1 for s in shells):
        return None
    src_fill = _shell_group_fill(protected_source, by_ph)
    out_fill = _shell_group_fill(translated, by_ph)
    if out_fill is None or src_fill != out_fill:
        return None
    pad_after = {}
    pad_before = {}
    for s in shells:
        at = protected_source.index(s["ph"])
        pad_after[s["ph"]] = protected_source[at + len(s["ph"]):][:1].isspace()
        pad_before[s["ph"]] = protected_source[:at][-1:].isspace()
    pieces: List[str] = []
    pos = 0
    strip_next = False
    for m in _LATEX_SHELL_TOKEN_RE.finditer(translated):
        sh = by_ph.get(m.group(0))
        if sh is None:
            continue
        piece = translated[pos:m.start()]
        if strip_next:
            piece = piece.lstrip(" \t")
        if sh["kind"] == "close" and not pad_before[sh["ph"]]:
            piece = piece.rstrip(" \t")
        pieces.append(piece)
        orig = sh["orig"]
        if sh["word"] and translated[m.end():m.end() + 1].isalpha():
            orig += "{}"
        pieces.append(orig)
        strip_next = sh["kind"] == "open" and not pad_after[sh["ph"]]
        pos = m.end()
    tail = translated[pos:]
    pieces.append(tail.lstrip(" \t") if strip_next else tail)
    return "".join(pieces)


# Sade biçim (mt okuma MT-1 önerisi): kısa değişken yer tutucusu modele düz
# metin olarak gider; yer tutucu yükü azalınca model kümeyi yutmaz. Yalnız tek
# harf ya da Yunan harfi + en çok bu kadar karakterlik alt indis.
SIMPLE_VAR_SUBSCRIPT_MAX = 3
# Yunan harfi komutu → Unicode (düz metinde model harfi aynen taşır).
_GREEK_PLAIN = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "tau": "τ",
    "upsilon": "υ", "phi": "ϕ", "varphi": "φ", "chi": "χ", "psi": "ψ",
    "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω",
}
_SIMPLE_MATH_VAR_RE = re.compile(
    r"(?:(?P<base>[A-Za-z])|\\(?P<greek>[A-Za-z]+))"
    r"(?:_(?:(?P<sub1>[A-Za-z0-9])|\{(?P<sub2>[A-Za-z0-9]{1,%d})\}))?"
    % SIMPLE_VAR_SUBSCRIPT_MAX)
_INLINE_MATH_ITEM_RE = re.compile(r"\$\s*([^$]+?)\s*\$|\\\(\s*(.+?)\s*\\\)", re.S)
_SHORT_ITALIC_ITEM_RE = re.compile(r"\\textit\{([^\W\d_]{1,%d})\}"
                                   % SIMPLE_VAR_SUBSCRIPT_MAX)


def plain_variable(item: str) -> Optional[str]:
    """Stash öğesi kısa bir değişkense düz metin biçimi, değilse None.

    ``$m_1$`` → ``m_1``, ``$v_{L}$`` → ``v_L``, ``$\\mu$`` → ``μ``,
    ``\\textit{λ}`` → ``λ``. Formül (``$m_1 > m_2$``), aksanlı/vektör
    (``$\\vec{R}_1$``, ``$\\dot{Q}$``) ve harf olmayan simge
    (``\\textit{◦}``, ``\\textit{<}``) yer tutucu kalır.
    """
    item = item.strip()
    m = _INLINE_MATH_ITEM_RE.fullmatch(item)
    if m:
        v = _SIMPLE_MATH_VAR_RE.fullmatch(m.group(1) or m.group(2) or "")
        if v is None:
            return None
        base = (_GREEK_PLAIN.get(v.group("greek")) if v.group("greek")
                else v.group("base"))
        if base is None:
            return None
        sub = v.group("sub1") or v.group("sub2")
        return f"{base}_{sub}" if sub else base
    m = _SHORT_ITALIC_ITEM_RE.fullmatch(item)
    return m.group(1) if m else None


def _plain_token_re(plain: str) -> "re.Pattern[str]":
    """Düz değişken belirtecinin sözcük sınırlı deseni (``m_1`` ≠ ``m_12``;
    ``m_1'in`` eşleşir; ``\\emph`` içindeki ``e`` eşleşmez)."""
    return re.compile(r"(?<![\w\\])" + re.escape(plain) + r"(?!\w)")


def unsimplify_variables(text: str, var_map: Dict[str, List[str]]) -> Optional[str]:
    """Düz değişkenleri yerlerine (sırayla) yer tutucu olarak geri koy.

    Her düz belirteç, kaynakta yerine geçtiği yer tutucu sayısı kadar
    geçmeli (yer tutucu kümesi eşitliğinin düz metin karşılığı); değilse
    None. Yer tutucuların içi aranmaz.
    """
    for plain in sorted(var_map, key=len, reverse=True):
        rx = _plain_token_re(plain)
        tokens = var_map[plain]
        parts = re.split(f"({_ANY_PLACEHOLDER_RE.pattern})", text)
        found = sum(len(rx.findall(p)) for k, p in enumerate(parts) if k % 2 == 0)
        if found != len(tokens):
            return None
        it = iter(tokens)
        text = "".join(
            p if k % 2 else rx.sub(lambda _m: next(it), p)
            for k, p in enumerate(parts))
    return text


# Özgün dilinde bırakılan parçaların işaret kaydında saklanan kaynak uzunluğu.
KEPT_SOURCE_PREVIEW_CHARS = 200
# FX3 (2026-10-05, V2 Y3): cevapsız parçanın işaret nedeni (kept_source_list).
NO_ANSWER_REASON = "cevap yok (düşünme payı doldu)"
# FX3 (2026-10-05, V2 Y6): arka uç kullanılamıyorken (devre açık) bırakılan
# parçanın işaret nedeni.
BACKEND_DOWN_REASON = "model kullanılamıyor (devre açık)"
# Yankı (model girdiyi aynen döndürdü) ancak bu kadar 2+ harfli sözcük taşıyan
# yer tutuculu metinde hatadır; daha azı (sayı/sembol satırı) meşru yankıdır
# (B-11, 2026-07-27 _count_identical_result ile aynı eşik).
ECHO_PROSE_MIN_WORDS = 3
_PROSE_WORD_RE = re.compile(r"[^\W\d_]{2,}")


def is_failed_echo(result: str, text: str) -> bool:
    """Yer tutuculu düzyazı metin (boşluk farkı dışında) aynen döndü mü?

    Kurtarma sırasını tetikler (G2a-1). Yer tutucusuz metnin ya da sayı /
    sembol satırının yankısı meşrudur: tetiklemez, işaretlenmez.
    """
    if "<<LATEX_" not in text:
        return False
    if " ".join(result.split()) != " ".join(text.split()):
        return False
    return len(_PROSE_WORD_RE.findall(_without_placeholders(text))) >= ECHO_PROSE_MIN_WORDS


# ---------------------------------------------------------------------------
# G2a-2 (2026-10-05): istem işareti sızıntısı
# ---------------------------------------------------------------------------
# autofix._retranslate_with_context hedef metni <<<TRANSLATE_START>>> …
# <<<TRANSLATE_END>>> arasına koyup _safe_translate'e veriyor. Model işareti
# yankılıyor ya da bozuyor; patterns.py'deki <{2,3}[^>]*TRANSLATE[^>]*>{2,3}
# bozuk biçimi yakalamıyordu ve autofix'in satır sayısı sezgiseli işaretli
# satırı gövdeye yazıyordu: bir test kitabının çevrilmiş gövdesinde
# "… <<><TRANSLATE_END>> …" ve "<<><TRANSLATE_START>>…" kalıntıları; bir
# model denemesinin günlüğünde işaretli metin
# cümle düzeyi yeniden çeviriye temizliksiz gitti; iç denetim notu: çeviri
# belleğine de sızdı. Ayrıca işaretin '<' '>' karakterleri G2a-1'in ilişki
# simgesi bekçisini tetikleyip doğru çeviriyi geri aldırıyordu.
# Kural (her bozuk biçim; Türkçeleştirilmiş "ÇEVİRİ_…" dahil):
#   * _safe_translate girişinde metinden ayıklanır — model görmez, belleğe
#     girmez (autofix'in işaretle ayıklaması zaten işlemiyordu: başarılı
#     çeviride işaretleri _post_process_translation siliyordu);
#   * her MT çıktısından bekçilerden ÖNCE ve translate() sonunda gövdeden
#     temizlenir; sızıntı UYARI günlüğüne yazılır, stats.prompt_marker_leaks.
_PROMPT_MARKER_RE = re.compile(
    r"[<>]{1,4}[ \t]*(?:TRANSLAT\w*|ÇEV[İI]R\w*)(?:[ \t_\-]+\w+)?[ \t]*[<>]{1,4}",
    re.IGNORECASE)
_PROMPT_MARKER_SPAN_RE = re.compile(
    r"[ \t]*(?:" + _PROMPT_MARKER_RE.pattern + r")[ \t]*", re.IGNORECASE)


def strip_prompt_markers(text: str,
                         source: Optional[str] = None) -> Tuple[str, List[str]]:
    """İstem işaretlerini sil: ``(temiz_metin, silinen_işaretler)`` (G2a-2).

    Metnin geri kalanı birebir kalır: satır başında/sonunda işaretle
    birlikte kenar boşluğu da gider, iki sözcük arasındaysa tek boşluk
    kalır.

    FX3 (2026-10-05, V2 Y2): *source* verilirse KAYNAKTA GEÇEN işaret
    meşru içeriktir (ör. şablon kitabındaki "<translate>" etiketi): aynı
    işaretin kaynaktaki sayısı kadar geçişi korunur, yalnız fazlası
    (kaynakta olmayan = sızıntı) silinir."""
    if not text or not _PROMPT_MARKER_RE.search(text):
        return text, []
    found: List[str] = []
    allowed = Counter(m.group(0).strip()
                      for m in _PROMPT_MARKER_RE.finditer(source or ""))

    def repl(m: re.Match) -> str:
        token = m.group(0).strip()
        if allowed[token] > 0:
            allowed[token] -= 1
            return m.group(0)
        found.append(token)
        s = m.string
        left = s[m.start() - 1] if m.start() > 0 else "\n"
        right = s[m.end()] if m.end() < len(s) else "\n"
        if left == "\n" or right == "\n":
            return ""
        return " " if any(c in " \t" for c in m.group(0)) else ""

    return _PROMPT_MARKER_SPAN_RE.sub(repl, text), found


def sub_unless_in_source(pattern: "re.Pattern[str]", text: str,
                         source: Optional[str]) -> str:
    """*pattern* eşleşmelerini sil; KAYNAKTA geçen eşleşmenin kaynaktaki
    sayısı kadarını koru (FX3, V2 Y2: kaynakta olmayan = sızıntı)."""
    allowed = Counter(m.group(0) for m in pattern.finditer(source or ""))

    def repl(m: re.Match) -> str:
        token = m.group(0)
        if allowed[token] > 0:
            allowed[token] -= 1
            return token
        return ""

    return pattern.sub(repl, text)


# FX3 (2026-10-05, V2 Y2): autofix._retranslate_with_context'in bağlam
# protokolü (hedef bu iki işaret arasında). Girdide yalnız bunlar istem
# işaretidir; başka "<translate>" biçimi kaynağın meşru içeriğidir.
_PROTOCOL_START = "<<<TRANSLATE_START>>>"
_PROTOCOL_END = "<<<TRANSLATE_END>>>"
_PROTOCOL_MARKER_RE = re.compile(
    re.escape(_PROTOCOL_START) + "|" + re.escape(_PROTOCOL_END))


def _split_protocol_target(text: str) -> Optional[Tuple[str, str, str]]:
    """Tek START…END çifti varsa ``(önce, hedef, sonra)``; yoksa None."""
    if not text or text.count(_PROTOCOL_START) != 1 \
            or text.count(_PROTOCOL_END) != 1:
        return None
    i = text.index(_PROTOCOL_START)
    j = text.index(_PROTOCOL_END)
    if j < i:
        return None
    target = text[i + len(_PROTOCOL_START):j]
    if not target.strip():
        return None
    return text[:i], target, text[j + len(_PROTOCOL_END):]


# ---------------------------------------------------------------------------
# G2a-2 (2026-10-05): ondalık ayırıcı — mt okuma MT-14, MT-15
# ---------------------------------------------------------------------------
# Türkçe düzyazıda ondalık ayırıcı virgüldür ("1.327" Türk okura "bin üç yüz
# yirmi yedi"dir). Model kimi sayıyı virgüle çeviriyor kimini çevirmiyor
# (bir model denemesinde "0,0123" ile "7.91" aynı kitapta); Almanca kaynağın
# "0,24"ünü noktaya da çevirebiliyor. MUHAFAZAKÂR kural (segment düzeyi;
# matematik ve LaTeX zaten yer tutucuda, kapsam dışı):
#   * çıktıdaki "X.Y" ancak AYNI segmentin kaynağında aynı rakamlarla ondalık
#     sayı olarak geçiyorsa "X,Y" olur: İngilizce kaynakta "X.Y" (ondalık
#     nokta), öteki kaynak dillerde "X,Y" (ondalık virgül; çıktıda da virgül
#     kalır);
#   * kaynakta aynı rakamlar öbür ayırıcıyla da geçiyorsa (binlik/ondalık
#     belirsizliği) ya da kapsam dışı bağlamda geçiyorsa dokunulmaz;
#   * kapsam dışı bağlam (kaynakta da çıktıda da): atıf etiketinden sonra
#     (Şekil/Figure/Abb./Bölüm/Section/§/s./Version…; "2.3 numaralı",
#     "2.3. Bölüm"), tek başına ayraç içinde "(1.5)", çok noktalı dizi
#     (1.2.3, tarih 12.03.2021), saat (10.30 am / Uhr; "saat 10.30"), büyük
#     harfli sözcükten hemen sonra (sürüm/ürün adı: "Python 3.8", "USB 3.0"),
#     URL/e-posta/eğik çizgili belirteç içinde. Binlik ayırıcıya (virgül)
#     hiç dokunulmaz. Kaçan dönüşüm kabul, yanlış dönüşüm kabul değil.
_DOT_DECIMAL_RE = re.compile(r"(?<![\w.,])(\d+)\.(\d+)(?![\w]|[.,]\d)")
_COMMA_DECIMAL_RE = re.compile(r"(?<![\w.,])(\d+),(\d+)(?![\w]|[.,]\d)")
# Atıf/sürüm etiketi bu kadar boşluk/ayraçla sayıya bağlanır.
_DECIMAL_LABEL_GAP = r"[\s~\u00a0]*\(?[\s~\u00a0]*"
# Etiketten sonraki numara listesi/aralığı da atıftır ("Eqs. 7.15–7.17",
# "Figures 2.1 and 2.2", "Denklem 7.15 7.17'ye kadar"; bir test kitabında ölçüldü).
_DECIMAL_LABEL_LIST = (
    r"(?:\d+(?:[.,]\d+)*[a-z]?\)?[\s~\u00a0]*"
    r"(?:[–—\-−,;/&]|to|and|or|through|ve|veya|ile|ila|bis|und|oder|et|ou"
    r"|y|o|e|и|или)?[\s~\u00a0]*\(?[\s~\u00a0]*)*")
# Türkçe çıktı ve genel etiketler (kaynak dil etiketleri
# patterns.XREF_LABEL_WORDS'ten, Türkçe atıf karşılıkları _FIGURE_LABEL_MAP'ten
# gelir; burada yalnız orada olmayanlar).
_DECIMAL_EXTRA_LABELS = (
    r"Çizelge|Eşitlik|Formül|Kısım|Alt\s?bölüm|Problem|Soru|Madde|Sayfa|sf\."
    r"|s\.|ss\.|S\.|Seite|p\.|pp\.|pages?|§|No\.?|Nr\.|Nº|Vol\.|Bd\.|Heft"
    r"|Aufl\.|ed\.|edition|[Ss]ürüm\w*|[Vv]ersiyon\w*|[Vv]ersion|ver\.|v\."
    r"|Rev\.|[Rr]elease|Revision")
_DECIMAL_AFTER_EXCLUDE_RE = re.compile(
    r"\A(?:[\s\u00a0]*(?:numaral\w*|nolu|no['’]lu|sayıl\w*)"   # 2.3 numaralı
    r"|\.?[\s\u00a0]*(?:Bölüm|Kısım|Şekil|Tablo|Denklem|Ek)\b"   # 2.3. Bölüm
    r"|[\s\u00a0]*(?:a\.?\s?m\b\.?|p\.?\s?m\b\.?|Uhr\b|o['’]clock|h\b|hrs?\b))",
    re.IGNORECASE)
# Satır başı (yalnız boşluk, maskeli yer tutucu, ayraç ya da \item /
# \section{ gibi komut önekiyle): oradaki sayı numaradır ("10.3 Bir
# deneyde", "\item 10.4 ...", "\section{10.5 ...}"; bir test kitabında ölçüldü).
_DECIMAL_LINE_START_RE = re.compile(
    r"\A(?:[\s\x00{}\[\]]|\\[A-Za-z]+\*?(?:\[[^\]\n]*\])?)*\Z")
_DECIMAL_TIME_BEFORE_RE = re.compile(
    r"(?:\b[Ss]aat|\bum|\bà|\balle|\ba\s+las)[\s\u00a0]*\Z")


def _decimal_label_before_re() -> "re.Pattern[str]":
    """Sayıdan hemen önce atıf/sürüm etiketi (her kaynak dil + Türkçe)."""
    global _DECIMAL_LABEL_BEFORE
    if _DECIMAL_LABEL_BEFORE is None:
        from latex_pipeline.patterns import XREF_LABEL_WORDS
        alts = [alt for by_lang in XREF_LABEL_WORDS.values()
                for alt in by_lang.values()]
        alts += [re.escape(w) for w in sorted(
            set(_FIGURE_LABEL_MAP) | set(_FIGURE_LABEL_MAP.values()),
            key=len, reverse=True)]
        alts.append(_DECIMAL_EXTRA_LABELS)
        _DECIMAL_LABEL_BEFORE = re.compile(
            r"(?<![^\W\d_])(?:" + "|".join(alts) + r")" + _DECIMAL_LABEL_GAP
            + _DECIMAL_LABEL_LIST + r"\Z",
            re.IGNORECASE)
    return _DECIMAL_LABEL_BEFORE


_DECIMAL_LABEL_BEFORE: Optional["re.Pattern[str]"] = None


# Bağlam penceresi: etiket + numara listesi ya da saat sözcüğü sayının en
# çok bu kadar karakter gerisindedir (uzun segmentte her sayı için bütün
# metni taramamak için; 2179 karakter / 120 sayı ölçümü 0,15 sn → pencereyle
# düşer).
_DECIMAL_CONTEXT_CHARS = 160


def _mask_for_decimals(text: str) -> str:
    """Yer tutucuları aynı uzunlukta nötr karakterle maskele (konumlar
    korunur; içlerindeki rakam ve büyük harf karar dışı)."""
    return _ANY_PLACEHOLDER_RE.sub(lambda m: "\x00" * len(m.group(0)), text)


def _decimal_context_excluded(masked: str, start: int, end: int, *,
                              check_upper: bool = True) -> bool:
    """[start, end) aralığındaki sayı kapsam dışı bağlamda mı?

    *check_upper* (yalnız KAYNAK tarafında): hemen önceki sözcük büyük harf
    içeriyorsa sürüm/ürün adı sayılır ("Python 3.8", "USB 3.0"; cümle başı
    "About 3.8" de kaçar — muhafazakâr). Türkçe çıktıda bakılmaz: oradaki
    büyük harf çoğunlukla cümle başıdır ("Duvar 0.24 m"); uygunluğu zaten
    kaynak taraf belirler."""
    before = masked[max(0, start - _DECIMAL_CONTEXT_CHARS):start]
    after = masked[end:end + _DECIMAL_CONTEXT_CHARS]
    line_before = masked[:start].rsplit("\n", 1)[-1]
    if _DECIMAL_LINE_START_RE.match(line_before):
        return True
    if _decimal_label_before_re().search(before):
        return True
    if _DECIMAL_AFTER_EXCLUDE_RE.match(after):
        return True
    if _DECIMAL_TIME_BEFORE_RE.search(before):
        return True
    # Tek başına ayraç içinde: "(1.5)" denklem/bölüm atfıdır.
    if before.rstrip(" \u00a0").endswith("(") and after.lstrip(" \u00a0").startswith(")"):
        return True
    # Sayıyı taşıyan boşluksuz belirteç: URL, e-posta, eğik çizgili dizi.
    token = (re.split(r"\s", before)[-1] + masked[start:end]
             + re.split(r"\s", after)[0])
    if "://" in token or "www." in token or "@" in token or "/" in token:
        return True
    # Hemen önceki sözcük büyük harf içeriyor: sürüm/ürün adı ("Python 3.8").
    prev = before.rstrip(" \t~\u00a0")
    if check_upper and len(prev) < len(before):
        word = re.split(r"\s", prev)[-1]
        if any(c.isupper() for c in word):
            return True
    return False


def _decimal_keys(text: str, decimal_re: "re.Pattern[str]"):
    """``(uygun, kapsam_dışı)`` rakam çiftleri kümeleri ``{("7", "91")}``."""
    masked = _mask_for_decimals(text)
    ok, excluded = set(), set()
    for m in decimal_re.finditer(masked):
        key = (m.group(1), m.group(2))
        if _decimal_context_excluded(masked, m.start(), m.end()):
            excluded.add(key)
        else:
            ok.add(key)
    return ok, excluded


# Çıktıda cümle sonu (kaynak dilde kalan cümleyi bulmak için).
_DECIMAL_SENTENCE_END_RE = re.compile(r"[.!?…](?=\s|\Z)")


def _decimal_sentence(masked: str, start: int, end: int) -> str:
    """[start, end) sayısını içeren cümle (satır sınırı içinde; maskeli yer
    tutucular boşluk olur)."""
    line_start = masked.rfind("\n", 0, start) + 1
    line_end = masked.find("\n", end)
    line_end = len(masked) if line_end < 0 else line_end
    s = line_start
    for m in _DECIMAL_SENTENCE_END_RE.finditer(masked, line_start, start):
        s = m.end()
    m = _DECIMAL_SENTENCE_END_RE.search(masked, end, line_end)
    e = m.end() if m else line_end
    return masked[s:e].replace("\x00", " ")


# FX3 (2026-10-05, mt okuma MT-15 kısmen): YALNIZ sayıdan oluşan tablo
# hücresi modele gitmez (çevrilecek sözcük yok), bu yüzden düzyazı kuralı
# (localize_decimal_separators) ona hiç uğramıyordu: bir model denemesinin
# tablosundaki "1.327" Türkçe metinde "bin üç yüz yirmi yedi" okunuyordu. İngilizce
# kaynakta ondalık nokta virgüle, üçlü gruplu binlik virgül noktaya döner.
# Belirsiz biçime (grupsuz "1,2", çok noktalı sürüm "1.2.3", üslü "3.0e5")
# dokunulmaz; başlığı atıf/numara etiketi olan sütun (Section, No., Page…)
# çağıranda dışlanır. Matematik içindeki sayı kapsam dışı (yer tutucuda).
_NUMERIC_CELL_RE = re.compile(
    r"\A(\s*)([+\-\u2212]?)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(\s*)\Z")


def localize_numeric_cell(cell: str) -> Optional[str]:
    """İngilizce biçimli sayı hücresinin Türkçe biçimi; değişmiyorsa None."""
    m = _NUMERIC_CELL_RE.match(cell or "")
    if not m:
        return None
    lead, sign, int_part, frac, trail = m.groups()
    if frac is None and "," not in int_part:
        return None
    out = int_part.replace(",", ".")
    if frac is not None:
        out += "," + frac
    return lead + sign + out + trail


def _cell_header_label_re() -> "re.Pattern[str]":
    """Sütun başlığında atıf/numara etiketi (Section, No., Page, Bölüm…)."""
    global _CELL_HEADER_LABEL
    if _CELL_HEADER_LABEL is None:
        from latex_pipeline.patterns import XREF_LABEL_WORDS
        alts = [alt for by_lang in XREF_LABEL_WORDS.values()
                for alt in by_lang.values()]
        alts += [re.escape(w) for w in sorted(
            set(_FIGURE_LABEL_MAP) | set(_FIGURE_LABEL_MAP.values()),
            key=len, reverse=True)]
        alts.append(_DECIMAL_EXTRA_LABELS)
        alts.append(r"Sections?|Chapters?|Bölüm|Page|Pages|Version")
        _CELL_HEADER_LABEL = re.compile(
            r"(?<![^\W\d_])(?:" + "|".join(alts) + r")(?![^\W\d_])",
            re.IGNORECASE)
    return _CELL_HEADER_LABEL


_CELL_HEADER_LABEL: Optional["re.Pattern[str]"] = None


def localize_decimal_separators(translated: str, source: str,
                                source_lang: str = "en",
                                keep_fn=None) -> Tuple[str, int]:
    """Türkçe çıktıda ondalık ayırıcıyı virgüle çevir (muhafazakâr; G2a-2).

    Döner ``(metin, dönüşüm_sayısı)``. Kural ve kapsam dışı bağlamlar modül
    başındaki G2a-2 ondalık ayırıcı notunda. *keep_fn(cümle)* True dönerse
    o cümledeki sayıya dokunulmaz (çevirmen: cümle hâlâ kaynak dilde —
    kısmen çevrilmiş satırdaki kalıntı; yankının tamamı zaten atlanır)."""
    if not translated or not source or "." not in translated:
        return translated, 0
    if " ".join(translated.split()) == " ".join(source.split()):
        return translated, 0    # yankı: kaynak dilde kalan metne dokunulmaz
    dot_ok, dot_ex = _decimal_keys(source, _DOT_DECIMAL_RE)
    com_ok, com_ex = _decimal_keys(source, _COMMA_DECIMAL_RE)
    if (source_lang or "en") == "en":
        eligible = dot_ok - dot_ex - com_ok - com_ex
    else:
        eligible = (com_ok | com_ex) - dot_ok - dot_ex
    if not eligible:
        return translated, 0
    masked = _mask_for_decimals(translated)
    out = list(translated)
    count = 0
    for m in _DOT_DECIMAL_RE.finditer(masked):
        if (m.group(1), m.group(2)) not in eligible:
            continue
        if _decimal_context_excluded(masked, m.start(), m.end(),
                                     check_upper=False):
            continue
        if keep_fn is not None and keep_fn(
                _decimal_sentence(masked, m.start(), m.end())):
            continue
        out[m.start() + len(m.group(1))] = ","
        count += 1
    return ("".join(out) if count else translated), count


# ---------------------------------------------------------------------------
# G2a-2 (2026-10-05): bağlaç parçaları — mt okuma MT-13
# ---------------------------------------------------------------------------
# Display matematik cümleyi bölünce kısa parça kendi paragrafı olarak tek
# başına modele gidiyordu (bir model denemesinde dört olay; FX3: kitap alıntısı
# yerine sentetik örnek kalıbı): "where" → "nerede", "The cross term" →
# "Haç terimi noktası", "We obtain" → "Birisi bunu elde eder.", "Keeping in
# mind that" → "Bunu unutmamak gerekir.". Kural: KISA (en çok
# CONTEXT_FRAGMENT_MAX_WORDS sözcük),
# cümle sonu noktalamasıyla BİTMEYEN ve hemen ardından display matematik
# gelen paragraf, display yer tutucusu ve (tek satırlık) devam paragrafıyla
# birlikte TEK segment olarak çevrilir ("where <<LATEX_5>> is the sum …");
# zincir (parça, display, parça, display, devam) en çok
# CONTEXT_GROUP_MAX_DISPLAYS display. Çeviri display yer tutucularından geri
# bölünür: her display birebir ve sırasıyla, aradaki metinler kendi
# paragraflarına (Türkçe söz dizimi metni display'in öbür yanına
# taşıyabilir; boş kalan paragraf düşer). Tutmazsa (bekçi, display sırası,
# boş çeviri) parçalar ESKİSİ GİBİ ayrı ayrı çevrilir — kayıpsız, eski
# davranıştan kötü olamaz.
# Ölçüt sözcük sayısı: o denemedeki parçalar 1–8 sözcük (sentetik
# örnek: "where", "By grouping the terms on the right, we get"); daha uzun
# paragraf zaten kendi bağlamını taşır.
CONTEXT_FRAGMENT_MAX_WORDS = 8
# Zincirdeki en çok display (segment boyu sınırlı kalsın).
CONTEXT_GROUP_MAX_DISPLAYS = 3
# Stash öğesi display matematik mi (\[...\], $$...$$, equation/align/... ortamı).
_DISPLAY_STASH_RE = re.compile(
    r"\s*(?:\\\[|\$\$|\\begin\{(?:equation|align|gather|multline|flalign"
    r"|alignat|eqnarray|displaymath)\*?\})")
# Cümle sonu noktalaması (iki nokta ve noktalı virgül cümleyi bitirmez:
# "The solution is:" display'e bağlanır).
_FRAGMENT_END_RE = re.compile(r"[.!?…][\"'”’)»\]]*\Z")
_LONE_PLACEHOLDER_RE = re.compile(r"\s*(<<LATEX_(\d+)>>)\s*")


class _ContextGroup(str):
    """Bağlam grubu (G2a-2). ``str`` değeri kaynak paragrafların BİREBİR
    birleşimidir (ayraçlar dahil): grubu tanımayan kod onu özgün metin gibi
    görür. ``items``: ``[("text"|"sep"|"disp", metin), …]`` özgün sırayla;
    ``joined``: modele gidecek tek satır; ``displays``: display yer
    tutucuları sırayla."""

    def __new__(cls, items, joined, displays):
        obj = super().__new__(cls, "".join(t for _k, t in items))
        obj.items = items
        obj.joined = joined
        obj.displays = displays
        return obj


# ---------------------------------------------------------------------------
# LatexTranslator
# ---------------------------------------------------------------------------

class LatexTranslator:
    """Translate text in a LaTeX body while preserving math/code/refs.

    Parameters
    ----------
    pipeline_config : PipelineConfig
        Pipeline-level configuration (used for lang settings, etc.).
    mt_engine : str
        Machine translation engine name passed to BookTranslator.
    source_lang, target_lang : str
        Language pair.
    book_type : str
        Book genre hint for glossary/terminology selection.
    """

    # ------------------------------------------------------------------
    # Terminology glossary (EN -> TR, case-insensitive replacement)
    # ------------------------------------------------------------------
    # Applied as post-processing after MT output.  Longer phrases come
    # first in the list so that "stochastic gradient descent" is matched
    # before "gradient descent", and "neural networks" before "neural
    # network".  The dict is iterated in insertion order (Python 3.7+).
    _GLOSSARY: Dict[str, str] = {
        # Multi-word terms (longest first to avoid partial matches)
        "stochastic gradient descent": "stokastik gradyan inişi",
        "partial differential equation": "kısmi diferansiyel denklem",
        "ordinary differential equation": "adi diferansiyel denklem",
        "generative adversarial": "üretici çekişmeli",
        "batch normalization": "yığın normalleştirme",
        "attention mechanism": "dikkat mekanizması",
        "activation function": "aktivasyon fonksiyonu",
        "residual network": "artık ağ",
        "physics-informed": "fizik bilgili",
        "spectral method": "spektral yöntem",
        "operator learning": "operatör öğrenimi",
        "neural operator": "sinirsel operatör",
        "neural networks": "sinir ağları",
        "neural network": "sinir ağı",
        "supervised learning": "denetimli öğrenme",
        "representation learning": "temsil öğrenimi",
        "transfer learning": "aktarım öğrenimi",
        "unsupervised learning": "denetimsiz öğrenme",
        "reinforcement learning": "pekiştirmeli öğrenme",
        "deep learning": "derin öğrenme",
        "machine learning": "makine öğrenimi",
        "weight space": "ağırlık uzayı",
        "weight-space": "ağırlık uzayı",
        "gradient descent": "gradyan inişi",
        "back-propagation": "geri yayılım",
        "backpropagation": "geri yayılım",
        "decision trees": "karar ağaçları",
        "decision tree": "karar ağacı",
        "loss function": "kayıp fonksiyonu",
        "cost function": "maliyet fonksiyonu",
        "learning rate": "öğrenme oranı",
        "forward pass": "ileri geçiş",
        "backward pass": "geri geçiş",
        "training set": "eğitim kümesi",
        "validation set": "doğrulama kümesi",
        "test set": "test kümesi",
        "finite element": "sonlu eleman",
        "finite difference": "sonlu fark",
        "batch size": "yığın boyutu",
        "convolutional": "evrişimli",
        "hyperparameters": "hiperparametreler",
        "hyperparameter": "hiperparametre",
        "overfitting": "aşırı uyum",
        "underfitting": "yetersiz uyum",
        "normalization": "normalleştirme",
        "classification": "sınıflandırma",
        "autoencoder": "otokodlayıcı",
        "transformer": "dönüştürücü",
        "regression": "regresyon",
        "embedding": "gömme",
        "pooling": "havuzlama",
        "dropout": "seyreltme",
        "encoder": "kodlayıcı",
        "decoder": "kod çözücü",
        "feature": "öznitelik",
        "weight": "ağırlık",
        "epoch": "dönem",
        "label": "etiket",
        "bias": "yanlılık",
        # Probability/statistics terms
        "posterior probability": "sonsal olasılık",
        "prior probability": "önsel olasılık",
        "posterior": "sonsal",
        "prior": "önsel",
        "cumulative distribution function": "kümülatif dağılım fonksiyonu",
        "probability density function": "olasılık yoğunluk fonksiyonu",
        "probability mass function": "olasılık kütle fonksiyonu",
        "random variable": "rastgele değişken",
        "distribution function": "dağılım fonksiyonu",
        "distribution": "dağılım",
        "classifier": "sınıflandırıcı",
        "likelihood": "olabilirlik",
        "inference": "çıkarım",
        "convergence": "yakınsama",
        # Rocket propulsion / aerospace terms
        "combustion chamber": "yanma odası",
        "exhaust velocity": "egzoz hızı",
        "specific impulse": "özgül itki",
        "propellant": "yakıt/itici madde",
        "thrust coefficient": "itki katsayısı",
        "nozzle exit": "lüle çıkışı",
        "characteristic velocity": "karakteristik hız",
        "mass flow rate": "kütle akış debisi",
        "chamber pressure": "yanma odası basıncı",
        "oxidizer": "oksitleyici",
        "stoichiometric": "stokiyometrik",
        "adiabatic": "adyabatik",
        "isentropic": "izentropik",
        "boundary layer": "sınır tabaka",
        "turbulent flow": "türbülanslı akış",
        "laminar flow": "laminer akış",
        "Reynolds number": "Reynolds sayısı",
        "Mach number": "Mach sayısı",
        "heat transfer": "ısı transferi",
        "thermal conductivity": "ısı iletkenliği",
        # General physics/engineering
        "conservation of energy": "enerjinin korunumu",
        "conservation of momentum": "momentumun korunumu",
        "conservation of mass": "kütlenin korunumu",
        "equation of state": "durum denklemi",
        "steady state": "kararlı durum",
        "transient": "geçici rejim",
    }

    # Wrong terms produced by the MT model -> correct Turkish
    _WRONG_TERMS: Dict[str, str] = {
        "hipergecis parametreleri": "hiperparametreler",
        "hipergeçiş parametreleri": "hiperparametreler",
        "hipergecis parametresi": "hiperparametre",
        "hipergeçiş parametresi": "hiperparametre",
        "aşırı öğrenme": "aşırı uyum",  # overfitting
        "etkileme fonksiyonları": "aktivasyon fonksiyonları",
        "etkileme fonksiyonlari": "aktivasyon fonksiyonları",
    }

    # Pre-compiled regex for inline math segments (used to protect math
    # content during glossary replacement).
    _INLINE_MATH_SPLIT_RE = re.compile(r"(\$[^$]+?\$)")

    def __init__(
        self,
        pipeline_config: PipelineConfig,
        mt_engine: Optional[str] = None,
        source_lang: str = "en",
        target_lang: str = "tr",
        book_type: str = "science",
        extra_glossary_terms: Optional[Dict[str, str]] = None,
        project_name: str = "default",
        translation_overrides: Optional[Dict[str, object]] = None,
        kitap_sozlugu=None,
    ):
        self.pipeline_config = pipeline_config
        # Denetim 2026-09-24 (M3#4): verilmezse tek kaynak
        # core.mt_engines.registry.DEFAULT_ENGINE (çağrı anında okunur).
        if not mt_engine:
            from core.mt_engines import registry as _mt_registry
            mt_engine = _mt_registry.DEFAULT_ENGINE
        self.mt_engine = mt_engine
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.book_type = book_type
        # UI-3 fix (2026-07-27): arayüz/çağıran, TranslationConfig alanlarını
        # (glossary_mode, use_memory, run_qa, ...) buradan ezebilir. Eskiden
        # sidebar ayarları LaTeX yoluna hiç taşınmıyordu — kullanıcı
        # 'Terminoloji: Kapalı' seçse bile core varsayılanları koşuyordu.
        self.translation_overrides = dict(translation_overrides or {})
        # Ceviri bellegi havuz kimligi (2026-07-11): verilmezse tum kitaplar
        # 'default' havuzunu paylasiyor ve kitaplar-arasi cache kontaminasyonu
        # olusuyordu (CORE-15 find_similar'i proje icine kisitlar ama proje
        # hep ayniysa kisit anlamsiz). translate_book.py kitap adini gecirir.
        self.project_name = project_name
        # G2c (KARAR 25): kitap sözlüğü (glossary_shared.tsv; kaynak dilde).
        # Yüklü çevirmene geçer: segment istemine terim karşılıkları eklenir.
        self.kitap_sozlugu = kitap_sozlugu

        # Lazy-loaded
        self._translator: Optional[object] = None
        # B-7 (2026-07-27): lazy-init kilidi — timeout'la terk edilen batch
        # thread'i ile ana thread ayni anda _get_translator'a girerse 7B
        # model IKI kez yukleniyordu (bellek + GPU cakismasi riski).
        self._translator_init_lock = threading.Lock()

        self.stats = TranslationStats()

        # Glossary engine for pre-translation term stashing
        self._glossary_engine = None
        self._glossary_term_map: Dict[int, object] = {}
        try:
            from latex_pipeline.glossary import GlossaryEngine
            # Resolve glossary directory relative to project root
            project_root = Path(__file__).resolve().parent.parent
            glossary_dir = project_root / "glossary"
            if glossary_dir.exists():
                self._glossary_engine = GlossaryEngine(
                    glossary_dir=glossary_dir,
                    book_type=book_type,
                    extra_terms=extra_glossary_terms,
                )
                logger.info(
                    "GlossaryEngine loaded: %d terms (including %d auto-terms).",
                    self._glossary_engine.term_count,
                    len(extra_glossary_terms) if extra_glossary_terms else 0,
                )
            else:
                logger.info("No glossary directory found at %s", glossary_dir)
        except Exception as exc:
            logger.warning(
                "Failed to initialize GlossaryEngine: %s", exc,
            )

    # ------------------------------------------------------------------
    # Lazy loader for BookTranslator (heavy -- loads ML model)
    # ------------------------------------------------------------------

    def _get_translator(self):
        """Load BookTranslator on first use.

        B-7 (2026-07-27): çift-kontrollü kilit — terk edilmiş (zombi) batch
        thread'i ile ana thread yarışında modelin iki kez yüklenmesini önler.

        2026-09-25 (yerel model sunucuları): yalnız ``BackendUnavailable``
        (sunucu kapalı, model yok, llama-cpp-python kurulu değil...) önbelleğe
        alınır; sonraki çağrılar YENİDEN YÜKLEMEDEN aynı hatayı alır. Aksi
        hâlde her ``_single_mt_call`` sunucuya yeniden bağlanıp 600 sn'ye kadar
        ısınma bekleyebilirdi. ``translate()`` başında önbellek temizlenir
        (run_pipeline'ın deneme döngüsü her denemede bir kez yeniden dener).
        MLX yükleme hataları eskisi gibi önbelleğe ALINMAZ.
        """
        if self._translator is None:
            self._raise_cached_translator_error()
            with getattr(self, "_translator_init_lock", threading.Lock()):
                if self._translator is not None:
                    return self._translator
                self._raise_cached_translator_error()
                logger.info(
                    "Loading BookTranslator (engine=%s, %s->%s, type=%s)",
                    self.mt_engine, self.source_lang, self.target_lang,
                    self.book_type,
                )
                from core.translator import BookTranslator, TranslationConfig

                config = TranslationConfig(
                    mt_engine=self.mt_engine,
                    source_lang=self.source_lang,
                    target_lang=self.target_lang,
                    book_type=self.book_type,
                    project_name=self.project_name,
                )
                # UI-3 fix: yalnız TranslationConfig'te GERÇEKTEN var olan
                # alanlar ezilir; bilinmeyen anahtar sessizce yutulmaz, loglanır.
                for _key, _val in getattr(self, "translation_overrides", {}).items():
                    if hasattr(config, _key):
                        setattr(config, _key, _val)
                    else:
                        logger.warning(
                            "translation_overrides: TranslationConfig'te olmayan "
                            "alan '%s' yok sayıldı", _key)
                from core.mt_engines.contracts import BackendUnavailable
                try:
                    self._translator = BookTranslator(config)
                except BackendUnavailable as exc:
                    self._translator_error = exc
                    logger.error("Çeviri modeli kullanılamıyor: %s", exc)
                    raise
                # G2c: kitap sözlüğü yüklü çevirmene geçer (istem terimleri).
                self._translator.kitap_sozlugu = getattr(self, "kitap_sozlugu", None)
                # FX3 (2026-10-05, V2 Y1): çekirdek belleğe ancak bu
                # katmanın bekçileri kabul edince yazar (_settle_memory).
                defer = getattr(self._translator, "set_memory_deferred", None)
                if callable(defer):
                    defer(True)
                logger.info("BookTranslator ready.")
        return self._translator

    def _raise_cached_translator_error(self) -> None:
        """Önbellekteki yükleme hatasını (varsa) yeni bir örnek olarak fırlat.

        Aynı istisna nesnesini binlerce kez yeniden fırlatmak traceback'ini
        büyütürdü; ileti ve ipucu korunarak yeni örnek üretilir.
        """
        cached = getattr(self, "_translator_error", None)
        if cached is None:
            return
        from core.mt_engines.contracts import BackendUnavailable
        raise BackendUnavailable(str(cached), getattr(cached, "hint", "")) from None

    def kitap_sozlugu_ata(self, sozluk) -> None:
        """Kitap sözlüğünü bağla (yüklüyse çevirmene de; G2c, KARAR 25)."""
        self.kitap_sozlugu = sozluk
        if getattr(self, "_translator", None) is not None:
            self._translator.kitap_sozlugu = sozluk

    def kitap_sozlugu_hazirla(self, latex_body: str, kitap_klasoru):
        """Kitap sözlüğünü hazırla ve bağla (G2c, KARAR 25).

        Klasörde ``glossary_shared.tsv`` varsa o yüklenir (kullanıcının / TeX
        yolunun sözlüğü önceliklidir); yoksa adaylar kaynak dilde çıkarılır,
        seçili motorun genel çağrısıyla (``translate_batch_raw``) bağlamlı
        çevrilir ve dosyaya yazılır. Model kullanılamazsa dosya yazılmaz.
        """
        from latex_pipeline import kitap_sozlugu as _ks

        sozluk = _ks.kitap_sozlugu_hazirla(
            latex_body, self.source_lang, kitap_klasoru,
            cevirmen_saglayici=self._get_translator)
        self.kitap_sozlugu_ata(sozluk)
        return sozluk

    def backend_health(self) -> Optional[str]:
        """Model arka ucunun durumu: None = sağlıklı (ya da MLX yolu); aksi Türkçe neden.

        Önbellekteki yükleme hatası (``BackendUnavailable``) ya da yüklü
        çevirmenin ``backend_health()``'i (devre açık: sunucu koptu, bellek
        yetersiz, model sunucuda yok). translate_book F3 kapısında bunu
        okuyup "model kullanılamıyor" çıkış kodunu verir.
        """
        cached = getattr(self, "_translator_error", None)
        if cached is not None:
            return str(cached)
        translator = getattr(self, "_translator", None)
        if translator is None:
            return None
        probe = getattr(translator, "backend_health", None)
        if not callable(probe):
            return None
        try:
            reason = probe()
        except Exception:   # sağlık sorgusu koşuyu düşürmesin
            return None
        return reason if isinstance(reason, str) and reason else None

    def _collect_model_stats(self) -> None:
        """Çevirmenin (core BookTranslator) ölçüm sayaçlarını bu koşunun
        ``stats``'ına aktar ve sıfır değilse Türkçe özet satırı yaz.

        Sayaçlar çevirmen ömrü boyunca birikir; koşu başındaki değer
        (``_model_stats_base``) çıkarılarak yalnız bu koşunun payı alınır.
        """
        stats = getattr(self, "stats", None)
        source = self._translator_stats()
        if stats is None or not isinstance(source, dict):
            return
        # V2 denetimi (F3-07): sınıra dayanan parçaların kendisi (sayaç değil)
        # bu koşunun payıyla stats'a; her biri Türkçe uyarıyla günlüğe.
        report = self._translator_truncation_report()
        items = report[int(getattr(self, "_truncation_base", 0) or 0):]
        stats.truncated_segments = items
        for item in items[:TRUNCATION_LOG_ITEMS]:
            logger.warning(
                "Çıktı üretim sınırına dayandı (%s/%s token); paragrafın çevirisi "
                "eksik olabilir: %.80s", item.get("used", "?"), item.get("limit", "?"),
                " ".join(str(item.get("source") or "").split()))
        if len(items) > TRUNCATION_LOG_ITEMS:
            logger.warning("Üretim sınırına dayanan %d parça daha var (liste: "
                           "kesik_parcalar.json).", len(items) - TRUNCATION_LOG_ITEMS)
        # FX3 (2026-10-05, V2 Y2): çekirdek (_strip_prompt_leaks) istem
        # işaretini kendi günlüğüyle temizler; bu koşunun payı sızıntı
        # sayacına eklenir (eskiden gerçek çekirdek yolunda sayaç hep 0'dı).
        try:
            core_leaks = (int(source.get("prompt_marker_leaks", 0) or 0)
                          - int(getattr(self, "_core_marker_base", 0) or 0))
        except (TypeError, ValueError):
            core_leaks = 0
        if core_leaks > 0:
            stats.prompt_marker_leaks = (
                getattr(stats, "prompt_marker_leaks", 0) + core_leaks)
            logger.warning("İstem işareti sızıntısı: çekirdek %d çıktıda "
                           "temizledi (ayrıntı çekirdek günlüğünde).", core_leaks)
        base = getattr(self, "_model_stats_base", None) or {}
        values = {}
        for name in _MODEL_STAT_FIELDS:
            try:
                value = int(source.get(name, 0) or 0) - int(base.get(name, 0) or 0)
            except (TypeError, ValueError):
                value = 0
            values[name] = max(0, value)
            setattr(stats, name, values[name])
        if not any(values.values()):
            return
        # FX3 (V2 Y3): yeniden deneme hem kısa düşünmeli (reasoning "low")
        # hem sert kapatmalı olabilir — etiket ikisini de söyler; cevapsız
        # İSTEK sayısı (think_failed) ile özgün dilde işaretlenen PARÇA sayısı
        # (no_answer_segments) ayrı yazılır (V2 c3: 11 istek / 6 parça).
        logger.warning(
            "Model ölçümleri: üretim sınırına dayanan çıktı %d; düşünme görülen parça %d "
            "(düşünme token'ı: %d sayılan, %d tahmini); düşünme bütçesini aşan "
            "deneme %d; yeniden deneme (kısa düşünmeli ya da sert kapatmalı) %d; "
            "düşünme sınırı yüzünden cevapsız kalan istek %d, özgün dilde "
            "işaretlenen parça %d.",
            values["segments_truncated"], values["thinking_segments"],
            values["thinking_tokens"], values["thinking_tokens_estimated"],
            values["think_budget_cuts"], values["think_retries"], values["think_failed"],
            getattr(stats, "no_answer_segments", 0),
        )

    def _snapshot_model_stats(self) -> None:
        """Koşu başında çevirmenin ölçüm sayaçlarını kaydet (fark için)."""
        source = self._translator_stats()
        self._model_stats_base = (
            {name: source.get(name, 0) for name in _MODEL_STAT_FIELDS}
            if isinstance(source, dict) else {})
        # FX3 (V2 Y2): çekirdeğin istem işareti temizliği (fark için).
        self._core_marker_base = (
            source.get("prompt_marker_leaks", 0) if isinstance(source, dict) else 0)
        self._truncation_base = len(self._translator_truncation_report())

    def _translator_truncation_report(self) -> list:
        """Yüklü çevirmenin kesilme kaydı (``truncation_report``); yoksa boş liste.

        Eski / sahte çevirmende bu yöntem olmayabilir (V2 denetimi, F3-07)."""
        translator = getattr(self, "_translator", None)
        getter = getattr(translator, "truncation_report", None)
        if not callable(getter):
            return []
        try:
            data = getter()
        except Exception:   # rapor okuması koşuyu düşürmesin
            return []
        return [dict(item) for item in data if isinstance(item, dict)] \
            if isinstance(data, list) else []

    def _translator_stats(self) -> Optional[dict]:
        """Yüklü çevirmenin istatistiği (``get_stats`` düşünme bekçisini de yansıtır)."""
        translator = getattr(self, "_translator", None)
        if translator is None:
            return None
        getter = getattr(translator, "get_stats", None)
        if callable(getter):
            try:
                data = getter()
            except Exception:   # istatistik okuması koşuyu düşürmesin
                data = None
            if isinstance(data, dict):
                return data
        data = getattr(translator, "stats", None)
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def translate(
        self,
        latex_body: str,
        checkpoint_dir: str | Path | None = None,
        progress_callback: Optional[object] = None,
        resume: bool = False,
    ) -> str:
        """Translate text in *latex_body* while preserving LaTeX structure.

        Parameters
        ----------
        latex_body : str
            LaTeX body content to translate.
        checkpoint_dir : path, optional
            Directory for checkpoint files.  A resumable FINAL checkpoint is
            written on completion.  (The sequential fallback path also writes
            a non-resumable intermediate snapshot every 50 paragraphs for
            crash inspection only — see BATCH-13.)
        progress_callback : callable, optional
            Called as ``progress_callback(fraction, description)`` where
            *fraction* is 0.0–1.0 and *description* is a status string.
        resume : bool
            When True, an existing FINAL checkpoint in *checkpoint_dir* is
            reloaded and re-translation is skipped.  2026-07-11'e kadar bu
            davranis KOSULSUZDU: checkpoint_dir verilen her cagri eski final
            checkpoint'i sessizce donduruyordu — kullanici --resume istemeden
            bayat ceviri geri geliyordu.  Artik cagiran acikca istemeli.

        Returns the translated LaTeX body string.
        """
        if not latex_body or not latex_body.strip():
            return latex_body or ""

        # 2026-09-25: önceki çağrının model yükleme hatası (BackendUnavailable)
        # bu çağrıda bir kez yeniden denenir (bkz. _get_translator).
        self._translator_error = None
        self.stats = TranslationStats()  # reset per call
        # FX3 (V2 Y3/Y6): koşu başına durum (cevapsız parçalar, bir kez
        # yazılan "model kullanılamıyor" iletileri).
        self._no_answer_texts = set()
        self._backend_down_logged = set()
        self._backend_down_suppressed = 0
        self._snapshot_model_stats()
        self._checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        self._checkpoint_counter = 0
        self._progress_callback = progress_callback
        # B-2 (2026-07-27): checkpoint kimligi — KAYNAK govdenin sha1'i.
        # Meta'ya yazilir; resume'da mevcut kaynakla karsilastirilir. Eskiden
        # final checkpoint kaynak degisse bile (farkli kitap/yeniden cikarim)
        # sorgusuz geri donuyordu -> bayat ceviri sessizce sonuc oluyordu.
        self._source_body_sha1 = hashlib.sha1(
            latex_body.encode("utf-8", "replace")).hexdigest()

        # -- Resume from checkpoint: yalniz acik istekle --------------------------
        if resume and self._checkpoint_dir is not None:
            resumed = self._try_resume_from_checkpoint(latex_body)
            if resumed is not None:
                return resumed

        # 1. Stash protected content (math, code, refs, etc.)
        body, stash = self._stash_protected(latex_body)
        # FX3 (V2 Y2): istem işareti son süpürmesinin kaynağı (kod/matematik
        # yer tutucudayken; kaynakta geçen işaret meşru içeriktir).
        stashed_source = body
        self._stash_list = stash  # keep ref for checkpoint
        self.stats.placeholders_stashed = len(stash)
        logger.info("Stashed %d protected fragments.", len(stash))

        # 1b. Glossary term injection: KALDIRILDI (GLS-D5, 2026-07).
        #     inject_terms() tüm book_type'larda boş map döndürür
        #     (_SKIP_INJECT_BOOK_TYPES evrensel kapsar; ampirik doğrulama:
        #     her book_type için inject_map_size=0). Bağlı verify_terms
        #     çağrısı da ölüydü (boş map -> falsy koşul). Terminoloji
        #     zorlaması artık TEK otorite olan Phase 5.75'te (translate_book.py
        #     verify_terms) yapılıyor. _glossary_engine'in başlık ve
        #     _apply_glossary skip-check kullanımları KORUNDU.

        # 2. Translate headings/captions in-place (they have structure)
        body = self._translate_structured(body)

        # 3. Segment and translate free-form paragraphs
        body = self._translate_paragraphs(body)

        # 3b. Stash integrity check: verify placeholder count in body
        #     matches stash list size.  If MT consumed or duplicated
        #     placeholders, log a warning (restore still attempts best-effort).
        body_placeholder_ids = set(
            int(m.group(1))
            for m in re.finditer(r"<<LATEX_(\d+)>>", body)
        )
        expected_ids = set(range(len(stash)))
        # 2026-09-29 (F3-02): gövdeden ya da gövdedeki bir stash öğesinin
        # İÇİNDEN (özyineli) ulaşılan kimlik kayıp değildir: restore iç içe
        # öğeyi yerinde açar. Eskiden yalnız gövdeye bakılıyor, italik /
        # kalın / dipnot içindeki matematik "kayıp" sayılıp gövde sonuna
        # '% [RECOVERED from stash N]' kopyası olarak da basılıyordu.
        reachable_ids = self._reachable_stash_ids(body_placeholder_ids, stash)
        missing_ids = expected_ids - reachable_ids
        # Gerçekten kaybolan öğelerden yalnız KÖKLER geri eklenir; kaybolan
        # bir öğenin içindeki kimlikler onunla birlikte açılır (çift basım yok).
        missing_ids -= self._reachable_stash_ids(
            set(), stash, roots=missing_ids)
        extra_ids = body_placeholder_ids - expected_ids
        if missing_ids:
            logger.warning(
                "Stash integrity: %d placeholder(s) missing from body "
                "after translation: %s. Attempting recovery.",
                len(missing_ids),
                sorted(missing_ids)[:10],
            )
            # Structural (begin/end/chapter/mainmatter/frontmatter/backmatter/part)
            # placeholders MUST NOT be appended to body end -- that would
            # destroy book structure. Only "safe" placeholders (labels,
            # indexes, refs, inline math, citations) may be recovered.
            _structural_re = re.compile(
                r"^\s*\\(?:begin|end|chapter|section|subsection|subsubsection"
                r"|mainmatter|frontmatter|backmatter|appendix|part)\b"
            )
            recovered_parts: list[str] = []
            structural_lost: list[int] = []
            for mid in sorted(missing_ids):
                if mid < len(stash) and stash[mid]:
                    content = stash[mid]
                    # Kabuk yarısı ('\textit{' / '}') ya da süslü parantezi
                    # dengesiz öğe gövde sonuna eklenirse belge derlenmez:
                    # yapısal gibi korunur, eklenmez (F3-03 kabuğu).
                    if (_structural_re.match(content)
                            or not self._braces_balanced(content)):
                        structural_lost.append(mid)
                    else:
                        recovered_parts.append(
                            f"\n% [RECOVERED from stash {mid}]\n{content}"
                        )
            if structural_lost:
                logger.error(
                    "Stash integrity: %d STRUCTURAL placeholder(s) lost "
                    "(ids=%s); refusing to append to body end (would "
                    "corrupt book structure). Content preserved in stash "
                    "but NOT re-inserted.",
                    len(structural_lost), structural_lost[:10],
                )
            if recovered_parts:
                body += "\n" + "\n".join(recovered_parts)
                logger.info(
                    "Recovered %d safe placeholder(s) to end of body "
                    "(structural ones skipped).",
                    len(recovered_parts),
                )
        if extra_ids:
            logger.warning(
                "Stash integrity: %d placeholder ID(s) in body exceed "
                "stash list size (%d): %s",
                len(extra_ids), len(stash),
                sorted(extra_ids)[:10],
            )

        # 3c. G2a-2 (2026-10-05): istem işareti hiçbir yoldan gövdeye
        #     geçmemeli — son güvence (sızıntı günlüklenir ve sayılır).
        #     FX3 (2026-10-05, V2 Y2): kod/matematik/korumalı bloklar AÇILMADAN
        #     önce koşar; eskiden restore'dan sonra koşuyor, verbatim /
        #     lstlisting içindeki meşru "<translate>", "<<TRANSLATE>>"
        #     satırlarını siliyordu. Kaynakta geçen işaret korunur.
        body = self._sweep_prompt_markers(body, "gövde", source=stashed_source)

        # 4. Restore LaTeX placeholders (reverse order for nesting safety)
        body = self._restore_stash(body, stash)

        # 4b. Glossary verify: KALDIRILDI (GLS-D5). inject_terms artık
        #     çağrılmadığından glossary_term_map her zaman boştu ve bu blok
        #     hiç çalışmıyordu. Terminoloji zorlaması Phase 5.75
        #     (translate_book.py verify_terms) tek otoritesinde.

        # 5. Post-processing: fix brace imbalances introduced by translation
        body = self._fix_line_brace_balance(body)

        # 5b. (FX3: istem işareti son süpürmesi 3c'ye, restore öncesine taşındı.)

        # 2026-09-28 (KARARLAR 15-16): çevirmenin ölçümleri (sınıra dayanan
        # çıktı, düşünme) koşu istatistiğine ve checkpoint üst verisine.
        self._collect_model_stats()

        # FX3 (V2 Y1): bu katmanın onaylamadığı bekleyen bellek kayıtları
        # (bekçisiz iç yollar) yazılmadan düşer — zehir riski yerine eksik
        # önbellek.
        _discard = getattr(getattr(self, "_translator", None),
                           "memory_discard_pending", None)
        if callable(_discard):
            try:
                _n_pending = _discard()
                if isinstance(_n_pending, int) and _n_pending:
                    logger.debug("Onaylanmamış %d bellek kaydı yazılmadı.",
                                 _n_pending)
            except Exception as exc:
                logger.debug("Bekleyen bellek kayıtları düşürülemedi: %s", exc)

        # Save final checkpoint — yalniz ceviri gercekten basariliysa.
        # 2026-07-11: eskiden KOSULSUZ yazilirdi; %100 basarisiz bir kosu
        # (model yuklenemedi, tum segmentler MT_FAIL) bile 'FINAL' damgasi
        # aliyor ve sonraki resume Ingilizce govdeyi 'ceviri' diye donduruyordu.
        _total = self.stats.segments_total
        _fail_ratio = (self.stats.segments_failed / _total) if _total else 0.0
        # Vk 2026-09-28: model arka ucu koşu sırasında kullanılamaz olduysa
        # (sunucu koptu, devre açık) kalan segmentler hiç denenmedi; oran
        # eşiğin altında kalsa da gövde resume kaynağı olamaz. MLX yolunda
        # backend_health() her zaman None (davranış değişmez).
        _backend_problem = self.backend_health()
        if _backend_problem:
            logger.error(
                "FINAL checkpoint YAZILMADI: model arka ucu koşu sırasında "
                "kullanılamaz oldu (%s). Bu gövde resume kaynağı olamaz.",
                _backend_problem,
            )
        elif _total > 0 and self.stats.segments_translated == 0:
            logger.error(
                "FINAL checkpoint YAZILMADI: %d segmentin hicbiri cevrilemedi "
                "(tumu failed/skipped). Bu govde resume kaynagi olamaz.",
                _total,
            )
        elif _fail_ratio > TRANSLATION_MAX_FAIL_RATIO:
            logger.error(
                "FINAL checkpoint YAZILMADI: segmentlerin %%%.0f'i basarisiz "
                "(%d/%d). Bu govde resume kaynagi olamaz.",
                _fail_ratio * 100, self.stats.segments_failed, _total,
            )
        else:
            self._save_checkpoint(body, final=True)

        logger.info(
            "Translation complete: %d total, %d translated, %d skipped, %d failed.",
            self.stats.segments_total,
            self.stats.segments_translated,
            self.stats.segments_skipped,
            self.stats.segments_failed,
        )
        # G2a-1 (2026-10-05): bekçilerin özeti (yalnız gerçek sayaç).
        # FX3 (V2 Y6): nedenler işaret kaydından sayılır (eskiden sabit
        # "yer tutucu / LaTeX iskeleti / sayı bekçisi" yazılıyordu; asıl
        # neden kesici ya da cevapsızlık olsa bile).
        if self.stats.kept_source_segments:
            logger.warning(
                "UYARI: %d parça çevrilemedi ve özgün dilinde bırakıldı "
                "(neden: %s; kurtarma: tekrar %d, sade biçim %d). QC bunları "
                "çevrilmemiş satır olarak raporlar.",
                self.stats.kept_source_segments,
                self._kept_source_reasons(),
                self.stats.recovered_by_retry,
                self.stats.recovered_by_simplified)
        _suppressed = getattr(self, "_backend_down_suppressed", 0)
        if _suppressed:
            logger.warning(
                "Model kullanılamaz durumdayken %d çağrı daha denenmeden geçti "
                "(neden yukarıda bir kez yazıldı).", _suppressed)
        return body

    def _kept_source_reasons(self) -> str:
        """İşaret kaydındaki nedenlerin sayımı ("neden 3, neden 1")."""
        items = getattr(self.stats, "kept_source_list", None) or []
        counts = Counter(str(item.get("neden") or "yankı") for item in items
                         if isinstance(item, dict))
        if not counts:
            return "bilinmiyor"
        return ", ".join(f"{name} {n}" for name, n in counts.most_common())

    @staticmethod
    def _reachable_stash_ids(seed_ids: set, stash: List[str], *,
                             roots: Optional[set] = None) -> set:
        """Stash öğelerinin içinden (özyineli) ulaşılan kimlikler.

        *roots* verilmezse dönüş = *seed_ids* ∪ onların içinden ulaşılanlar;
        verilirse yalnız *roots* öğelerinin İÇİNDEN ulaşılanlar (kökler
        hariç, birbirinin içinde olanlar hariç değil)."""
        n = len(stash)
        seen = set(seed_ids) if roots is None else set()
        todo = list(seed_ids if roots is None else roots)
        visited_roots = set()
        while todo:
            idx = todo.pop()
            if idx in visited_roots or not (0 <= idx < n):
                continue
            visited_roots.add(idx)
            for m in _LATEX_PLACEHOLDER_ID_RE.finditer(stash[idx] or ""):
                inner = int(m.group(1))
                if inner not in seen:
                    seen.add(inner)
                    todo.append(inner)
        return seen

    @staticmethod
    def _braces_balanced(text: str) -> bool:
        r"""Kaçışsız süslü parantezler dengeli mi (kaçış: ``\{``, ``\}``)?"""
        from core.lang_detect import braces_balanced
        return braces_balanced(text or "")

    def _save_checkpoint(self, body: str, final: bool = False) -> None:
        """Save a translation checkpoint to disk.

        Wrapped in try-except to survive disk-full conditions without
        crashing the pipeline.
        """
        if self._checkpoint_dir is None:
            return
        try:
            self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
            if final:
                path = self._checkpoint_dir / FINAL_CHECKPOINT_NAME
            else:
                path = self._checkpoint_dir / "translated_body_checkpoint.tex"
            path.write_text(body, encoding="utf-8")
            self._save_checkpoint_meta()
            logger.info(
                "Checkpoint saved: %s (%d segments translated so far)",
                path.name, self.stats.segments_translated,
            )
        except OSError as exc:
            logger.warning(
                "Checkpoint save failed (disk full?): %s", exc,
            )

    def _save_checkpoint_meta(self) -> None:
        """Save checkpoint metadata as JSON for resume support.

        Historically also persisted a glossary term map for resume support,
        but glossary injection was removed (GLS-D5): ``inject_terms`` is no
        longer called from ``translate()`` so ``_glossary_term_map`` stays
        empty and the sibling JSON is skipped. Kept as a best-effort no-op
        for backward compatibility with older checkpoint dirs.
        """
        if self._checkpoint_dir is None:
            return
        # Glossary term map: best-effort persist as JSON (non-string keys
        # are converted to strings for portability).
        try:
            term_map = getattr(self, "_glossary_term_map", {}) or {}
            if term_map:
                serialisable = {
                    str(k): (v if isinstance(v, (str, int, float, bool)) else str(v))
                    for k, v in term_map.items()
                }
                gloss_path = self._checkpoint_dir / "glossary_term_map.json"
                gloss_path.write_text(
                    json.dumps(serialisable, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except OSError:
            pass  # best-effort
        meta = {
            "segment_index": self.stats.segments_translated,
            "segments_total": self.stats.segments_total,
            "segments_failed": self.stats.segments_failed,
            # 2026-09-28 (KARARLAR 15-16): model ölçümleri (koşu sonu değerleri)
            **{name: getattr(self.stats, name, 0) for name in _MODEL_STAT_FIELDS},
            "stash_count": getattr(self, '_stash_list', None) and len(self._stash_list) or 0,
            # B-2: kaynak govde kimligi (resume bayatlik kontrolu)
            "source_body_sha1": getattr(self, "_source_body_sha1", ""),
            "timestamp": _time.time(),
            "timestamp_human": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp_iso_utc": _time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", _time.gmtime()
            ),
        }
        meta_path = self._checkpoint_dir / "checkpoint_meta.json"
        try:
            meta_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # best-effort

    def _try_resume_from_checkpoint(self, latex_body: str) -> Optional[str]:
        """Attempt to resume translation from a checkpoint.

        Checks for ``translated_body_checkpoint.tex`` and
        ``checkpoint_meta.json`` in the checkpoint directory.  If the
        stash count in meta matches the current body's stash count, the
        checkpoint is considered valid and is returned as-is (the
        _translate_paragraphs call already saved partial progress).

        If the final checkpoint exists, returns it directly (previous
        run completed but pipeline crashed later).

        Returns the resumed body string, or None if no valid checkpoint.
        """
        assert self._checkpoint_dir is not None

        # Priority 1: final checkpoint means translation completed
        final_path = self._checkpoint_dir / FINAL_CHECKPOINT_NAME
        meta_path = self._checkpoint_dir / "checkpoint_meta.json"

        if final_path.exists():
            try:
                body = final_path.read_text(encoding="utf-8")
                if body.strip():
                    # B-2 (2026-07-27): kaynak kimligi dogrulamasi — meta'daki
                    # source_body_sha1 MEVCUT kaynakla uyusmuyorsa bu
                    # checkpoint BASKA bir govdenin cevirisidir (yeniden
                    # cikarim / farkli kitap ayni dizin). Bayat ceviriyi
                    # sonucmus gibi dondurme. (Eski checkpointlerde alan yok
                    # -> dogrulanamaz, eski davranis korunur.)
                    _stale = False
                    try:
                        _meta = json.loads(
                            meta_path.read_text(encoding="utf-8"))
                        _saved_sha = (_meta.get("source_body_sha1") or "").strip()
                        _cur_sha = getattr(self, "_source_body_sha1", "")
                        if _saved_sha and _cur_sha and _saved_sha != _cur_sha:
                            _stale = True
                    except (OSError, json.JSONDecodeError):
                        pass
                    if _stale:
                        logger.warning(
                            "FINAL checkpoint BAYAT (source_body_sha1 "
                            "uyusmuyor — kaynak govde degismis): %s — resume "
                            "atlandi, ceviri sifirdan basliyor.",
                            final_path.name,
                        )
                    # Dogrulama (2026-07-11): final checkpoint'te cozulmemis
                    # <<LATEX_N>> placeholder'i varsa restore edilmemis/bozuk
                    # bir govdedir — resume edilirse PDF'ye literal placeholder
                    # basilir. Boyle checkpoint yok sayilir.
                    elif re.search(r"<<LATEX_\d+>>", body):
                        logger.warning(
                            "FINAL checkpoint bozuk (cozulmemis <<LATEX_N>> "
                            "placeholder iceriyor): %s — resume atlandi, "
                            "ceviri sifirdan basliyor.",
                            final_path.name,
                        )
                    else:
                        logger.info(
                            "Resuming from FINAL checkpoint: %s (%d chars)",
                            final_path.name, len(body),
                        )
                        return body
            except OSError:
                pass

        # Priority 2: intermediate checkpoint — NOT resumable.
        # Intermediate checkpoints store partially-translated bodies with
        # <<LATEX_N>> placeholders still unresolved. Returning them
        # directly skips _restore_stash and produces corrupt output.
        # Safer to restart translation from scratch when only an
        # intermediate checkpoint exists.  (BATCH-13: the primary batched
        # path no longer writes these; only the sequential fallback and
        # legacy checkpoint dirs may still contain one.)
        ckpt_path = self._checkpoint_dir / "translated_body_checkpoint.tex"
        if ckpt_path.exists():
            logger.warning(
                "Intermediate checkpoint found at %s but unsafe to "
                "resume (placeholders not restored); restarting "
                "translation from scratch.",
                ckpt_path.name,
            )
            try:
                # Mark the stale checkpoint so downstream tooling sees it
                # was ignored; keep content for manual inspection.
                ckpt_path.rename(
                    ckpt_path.with_suffix(ckpt_path.suffix + ".unusable")
                )
            except OSError:
                pass  # best-effort cleanup
        return None

    # ------------------------------------------------------------------
    # Phase 1 -- Stash protected content
    # ------------------------------------------------------------------

    def _stash_protected(self, text: str) -> Tuple[str, List[str]]:
        """Replace protected LaTeX with ``<<LATEX_N>>`` placeholders.

        Returns (modified_text, list_of_originals).
        """
        stash: List[str] = []

        def _replace(match: re.Match) -> str:
            idx = len(stash)
            stash.append(match.group(0))
            return f"<<LATEX_{idx}>>"

        def _push(fragment: str) -> str:
            idx = len(stash)
            stash.append(fragment)
            return f"<<LATEX_{idx}>>"

        # Order matters: broader / multi-line patterns first.
        # 0a. Bibliography block (entire thebibliography environment).
        #     Must come BEFORE math environments so that any math inside
        #     the bibliography is stashed as part of the whole block.
        text = _BIBLIOGRAPHY_ENV_RE.sub(_replace, text)
        # 0a2. Kaynakça bölümü (G2a-2, 2026-10-05; mt okuma MT-19, KARAR 20):
        #      başlığın altındaki girdiler (yazar, eser adı, yayın bilgisi)
        #      aynı ya da üst düzeydeki sonraki başlığa kadar yer tutucuya
        #      alınır — altı kaynak dil, her başlık düzeyi (#57 sınır kuralı
        #      aynen). Başlık satırı gövdede kalır; başlık metni Phase 2'de
        #      modele gitmeden çevrilir (_bibliography_heading_tr). Eskiden
        #      yalnız İngilizce/Türkçe ad tanınıyor ve başlık da korunup
        #      çevrilmiyordu.
        text, n_bib = _stash_bibliography_sections(text, _push)
        if n_bib:
            stats = getattr(self, "stats", None)
            if stats is not None:
                stats.bibliography_sections += n_bib
        # 0c. Titlepage block (entire \begin{titlepage}...\end{titlepage}).
        #     Stash the whole block to prevent translator from eating
        #     closing braces in {\Huge\bfseries ...}\par patterns.
        text = _TITLEPAGE_ENV_RE.sub(_replace, text)
        # 1. Math environments
        text = _MATH_ENV_RE.sub(_replace, text)
        # 1b. Tabular environments (entire block -- column spec + data rows)
        text = _TABULAR_ENV_RE.sub(_replace, text)
        # 1b2. B4 fix: Tabular dışında kalmış çıplak \toprule/\midrule/
        #      \bottomrule/\hline vs. komutlarını da stash'le. Marker
        #      PDF'den TOC'u bozuk tabular'a dönüştürdüğünde bu komutlar
        #      başlık/paragraf metnine sızıyor ve MT'ye "toprule Chapter"
        #      gibi anlamsız metinler gidiyor. Tabular bloğu yukarıda
        #      stash'lendiği için burada sadece ÇIPLAK komutlar kalır.
        text = _BOOKTABS_BARE_RE.sub(_replace, text)
        # 2. Code environments
        text = _CODE_ENV_RE.sub(_replace, text)
        # 3. Display math \[...\], 3b. $$...$$, 4. inline $...$ — TeX okuma
        #    sırasıyla (F3-01: bitişik atom ve atıf kaçışı düzyazı yutmaz).
        text = _stash_math_spans(text, _push)
        # 4b. Footnotes: stash entire \footnote{...} block.
        #     Runs AFTER math environments so any math inside footnotes
        #     is already stashed.  Inner text is translated later in
        #     _translate_stashed_formatting().
        text = _FOOTNOTE_RE.sub(_replace, text)
        # 4c. \footnotetext{...}: same logic as \footnote (wrapper stash,
        #     inner text translated in _translate_stashed_formatting).
        text = _FOOTNOTETEXT_RE.sub(_replace, text)
        # 4d. \footnotemark[N]: bare command, no content to translate.
        text = _FOOTNOTEMARK_RE.sub(_replace, text)
        # 4e. Kısa \textsuperscript{..} / \textsubscript{..} (F1 MT-10,
        #     2026-09-30): dipnot işareti / üs MT'ye ham LaTeX gitmez.
        text = _TEXT_SCRIPT_RE.sub(_replace, text)
        # 5. \includegraphics
        text = _INCLUDEGRAPHICS_RE.sub(_replace, text)
        # 6. \texttt{...}
        text = _TEXTTT_RE.sub(_replace, text)
        # 6b. Short \textit{...}: değişken/sembol bütün stash'lenir; vurgulu
        #     gerçek sözcükte yalnız kabuk ('\textit{' + '}') yer tutucuya
        #     alınır, sözcük cümlesiyle MT'ye gider (F3-03).
        _src = self._source_code()

        def _stash_short_italic(m: re.Match) -> str:
            content = m.group(1)
            if _short_italic_is_symbol(content, _src):
                return _push(m.group(0))
            return _push("\\textit{") + content + _push("}")

        text = _TEXTIT_SHORT_RE.sub(_stash_short_italic, text)
        # 6c. Short \textbf{...} (figure refs, single-word emphasis)
        text = _TEXTBF_SHORT_RE.sub(_replace, text)
        # 6d-pre. Author citation patterns that may contain \textit{et al.}
        #     MUST run BEFORE long \textit stash, otherwise \textit{et al.}
        #     gets stashed as a textit fragment and author regex can't see it.
        #     7-pre. \href also before long textit for same reason.
        text = _HREF_RE.sub(_replace, text)
        #     7-pre-b. \url{...} (V2): adres metni çevrilmez.
        text = _URL_CMD_RE.sub(_replace, text)
        #     8-pre. References (\label, \ref, \cite, ...)
        text = _REF_RE.sub(_replace, text)
        #     8a. Parenthetical references: (Author \textit{et al.}, Year)
        #         Supports both bare "et al." and \textit{et al.} variants.
        #         Must come BEFORE _AUTHOR_ETAL_RE so that "et al." inside
        #         parenthetical refs is captured as part of the whole unit.
        text = _PAREN_REF_RE.sub(_replace, text)
        #     8b. Author names with "et al." (no parens) -- catches bare
        #         "Hollingworth et al." and "Hollingworth \textit{et al.}" in
        #         running text.  Runs AFTER _PAREN_REF_RE.
        text = _AUTHOR_ETAL_RE.sub(_replace, text)
        #     8b2. Bare "Author (YYYY)" without "et al."
        text = _AUTHOR_YEAR_RE.sub(_replace, text)
        # 6d. Long \textit{...} (translatable prose -- wrapper protected)
        #     Runs AFTER author/citation stash so \textit{et al.} inside
        #     author citations is already protected as part of the citation.
        text = _TEXTIT_LONG_RE.sub(_replace, text)
        # 6e. Long \textbf{...} (translatable prose -- wrapper protected)
        text = _TEXTBF_LONG_RE.sub(_replace, text)
        # 8c. Figure/Table/Equation references (e.g. "Figure 6.11").
        #     The label is translated to Turkish and the number preserved.
        #     We stash the TRANSLATED form so that restore puts back
        #     "Sekil 6.11" instead of "Figure 6.11".
        def _replace_figure_ref(match: re.Match) -> str:
            idx = len(stash)
            translated = _translate_figure_ref(match)
            stash.append(translated)
            return f"<<LATEX_{idx}>>"

        text = _FIGURE_REF_RE.sub(_replace_figure_ref, text)
        # 8c-loc. İngilizce DIŞI kaynağın atıf etiketleri ("Abb. 1.1",
        #     "Gl. (1.3)", "рис. 2"): Türkçe etiket + aynen numara (F1 MT-10,
        #     2026-09-30). _STANDALONE_PAREN_REF_RE'den ÖNCE: "Gl. (1.1)"
        #     bütün olarak alınır. İngilizce kaynakta çalışmaz.
        if _src != "en":
            for _xref_re, _cat in _local_xref_patterns(_src):
                text = _xref_re.sub(
                    lambda m, _c=_cat: _push(_translate_local_xref(m, _c)),
                    text,
                )
        # 8c2. Standalone parenthetical cross-ref numbers: (7.1), (4.35a)
        #      These are common in physics/math where "Eq." is omitted.
        #      Runs AFTER _FIGURE_REF_RE so "Eq. (7.1)" is captured first.
        text = _STANDALONE_PAREN_REF_RE.sub(_replace, text)
        # 8d. Page references (p. 234, pp. 45-67, page 12)
        text = _PAGE_REF_RE.sub(_replace, text)
        # 8e. Index entries: \index{...} -- preserve index structure
        text = _INDEX_RE.sub(_replace, text)
        # 9. Standalone commands (two patterns: with-arg and bare)
        text = _STANDALONE_CMD_WITH_ARG_RE.sub(_replace, text)
        text = _STANDALONE_CMD_BARE_RE.sub(_replace, text)
        # 10. Float begin/end markers
        text = _FLOAT_BEGIN_RE.sub(_replace, text)
        text = _FLOAT_END_RE.sub(_replace, text)
        # 11. List environment markers (itemize/enumerate/description)
        text = _LIST_ENV_RE.sub(_replace, text)
        # 12. Eşsiz matematik ayraçları (F3-01): '\href{..}{\[ 63}]'
        #     kaçışındaki '\[' ya da eşi olmayan '$' yerinde korunur.
        text = _stash_stray_math_delims(text, _push)

        # Kaynak bitişikliği (F3-01 kayıpsızlık): restore'un boşluk garantisi
        # kaynakta harf/rakama BİTİŞİK olan yer tutucuya boşluk eklemesin
        # ('$H_{2}$O', '\textit{i}th', '8\textit{g}').
        self._stash_glue_ref = stash
        self._stash_glue = self._source_glue(text, stash)
        # FX3 (V2 Y7): kaynakta yer tutucunun yanında BOŞLUK var mıydı?
        self._stash_spacing = self._source_spacing(text, stash)
        return text, stash

    @staticmethod
    def _source_spacing(masked: str, stash: List[str]) -> Dict[int, Tuple[bool, bool]]:
        """FX3 (2026-10-05, V2 Y7): her yer tutucu için kaynakta (sol, sağ)
        komşusu boşluk (ya da metin başı/sonu) muydu? Geri yazımın boşluk
        garantisi kaynakta boşluk OLMAYAN yana yalnız harf-harfe yapışmayı
        ayırmak için boşluk koyar ("W/$m^{2}$", "\\textit{Tn}+1" aynen)."""
        spacing: Dict[int, Tuple[bool, bool]] = {}
        for piece in [masked] + stash:
            if "<<LATEX_" not in piece:
                continue
            for m in _LATEX_PLACEHOLDER_ID_RE.finditer(piece):
                left = piece[m.start() - 1] if m.start() > 0 else ""
                right = piece[m.end()] if m.end() < len(piece) else ""
                spacing[int(m.group(1))] = (left == "" or left.isspace(),
                                            right == "" or right.isspace())
        return spacing

    @staticmethod
    def _source_glue(masked: str, stash: List[str]) -> Dict[int, Tuple[bool, bool]]:
        """Her yer tutucu için kaynakta (sol, sağ) komşusu harf/rakam mıydı?

        Gövdede ve stash öğelerinin içinde (iç içe yer tutucu) bakılır."""
        glue: Dict[int, Tuple[bool, bool]] = {}
        for piece in [masked] + stash:
            if "<<LATEX_" not in piece:
                continue
            for m in _LATEX_PLACEHOLDER_ID_RE.finditer(piece):
                left = piece[m.start() - 1] if m.start() > 0 else ""
                right = piece[m.end()] if m.end() < len(piece) else ""
                glue[int(m.group(1))] = (left.isalnum(), right.isalnum())
        return glue

    # ------------------------------------------------------------------
    # Phase 2 -- Translate structured commands (headings, captions)
    # ------------------------------------------------------------------

    # Section number prefix regex for heading translation
    # Supports: "6.2.2 ", "6.3. ", "A.1 ", "B.2.3 " etc.
    # The trailing dot+space pattern (e.g. "6.3. ") is needed for
    # headings like \chapter{6.3. Deep Networks}.
    _SECTION_NUM_PREFIX_RE = re.compile(
        r"^(\d+(?:\.\d+)*[a-zA-Z]?\.?\s+|[A-Z](?:\.\d+)+\.?\s+)"
    )

    # Common heading text -> Turkish glossary (for headings that MT echoes)
    _HEADING_GLOSSARY: Dict[str, str] = {
        # General
        "Introduction": "Giriş",
        "Conclusion": "Sonuç",
        "Conclusions": "Sonuçlar",
        "Summary": "Özet",
        "Discussion": "Tartışma",
        "Results": "Sonuçlar",
        "Methods": "Yöntemler",
        "Methodology": "Yöntem",
        "Background": "Arka Plan",
        "Overview": "Genel Bakış",
        "Exercises": "Alıştırmalar",
        "Examples": "Örnekler",
        "References": "Kaynaklar",
        "Bibliography": "Kaynakça",
        "Appendix": "Ek",
        "Notation": "Gösterim",
        "Preliminaries": "Ön Bilgiler",
        "Definitions": "Tanımlar",
        "Properties": "Özellikler",
        "Remarks": "Açıklamalar",
        "Proof": "İspat",
        # Math/ML specific
        "Distribution Functions": "Dağılım Fonksiyonları",
        "Cumulative Distribution Function (CDF)": "Kümülatif Dağılım Fonksiyonu (CDF)",
        "Cumulative Distribution Function": "Kümülatif Dağılım Fonksiyonu",
        "Probability Density (PDF) and Mass Functions (PMF)": "Olasılık Yoğunluk (PDF) ve Kütle Fonksiyonları (PMF)",
        "Probability Density Function": "Olasılık Yoğunluk Fonksiyonu",
        "Probability Mass Function": "Olasılık Kütle Fonksiyonu",
        "Warning Continuous and Categorical Features": "Uyarı: Sürekli ve Kategorik Öznitelikler",
        "Naive Bayes Classifier": "Naif Bayes Sınıflandırıcısı",
        "Neural Networks": "Sinir Ağları",
        "Neural Network": "Sinir Ağı",
        "Deep Learning": "Derin Öğrenme",
        "Machine Learning": "Makine Öğrenimi",
        "Gradient Descent": "Gradyan İnişi",
        "Stochastic Gradient Descent": "Stokastik Gradyan İnişi",
        "Back-Propagation": "Geri Yayılım",
        "Backpropagation": "Geri Yayılım",
        "Loss Functions": "Kayıp Fonksiyonları",
        "Loss Function": "Kayıp Fonksiyonu",
        "Activation Functions": "Aktivasyon Fonksiyonları",
        "Activation Function": "Aktivasyon Fonksiyonu",
        "Convolutional Neural Networks": "Evrişimli Sinir Ağları",
        "Recurrent Neural Networks": "Tekrarlayan Sinir Ağları",
        "Supervised Learning": "Denetimli Öğrenme",
        "Unsupervised Learning": "Denetimsiz Öğrenme",
        "Reinforcement Learning": "Pekiştirmeli Öğrenme",
        "Linear Regression": "Doğrusal Regresyon",
        "Logistic Regression": "Lojistik Regresyon",
        "Decision Trees": "Karar Ağaçları",
        "Random Forest": "Rastgele Orman",
        "Support Vector Machine": "Destek Vektör Makinesi",
        "Support Vector Machines": "Destek Vektör Makineleri",
        "Batch Normalization": "Yığın Normalleştirme",
        "Overfitting": "Aşırı Uyum",
        "Underfitting": "Yetersiz Uyum",
        "Regularization": "Düzenlileştirme",
        "Optimization": "Eniyileme",
        "Feature Engineering": "Öznitelik Mühendisliği",
        # Physics/Engineering
        "Boundary Conditions": "Sınır Koşulları",
        "Initial Conditions": "Başlangıç Koşulları",
        "Numerical Methods": "Sayısal Yöntemler",
        "Finite Element Method": "Sonlu Eleman Yöntemi",
        "Finite Difference Method": "Sonlu Fark Yöntemi",
        "Computational Exercises": "Hesaplamalı Alıştırmalar",
        "Computational Exercise": "Hesaplamalı Alıştırma",
        # Rocket/Propulsion
        "Rocket Propulsion": "Roket İtişi",
        "Thrust": "İtme Kuvveti",
        "Nozzle Design": "Lüle Tasarımı",
        "Combustion": "Yanma",
        "Specific Impulse": "Özgül İtki",
        "Propellants": "İtici Yakıtlar",
        "Expansion Area Ratio": "Genişleme Alan Oranı",
        "Nozzle Flow": "Lüle Akışı",
        "Underexpansion": "Yetersiz Genişleme",
        "Overexpansion": "Aşırı Genişleme",
        "Effect of Ambient Pressure": "Çevresel Basıncın Etkisi",
        # Rocket/Propulsion -- UPPERCASE variants (all-caps headings)
        "OVEREXPANSION": "AŞIRI GENİŞLEME",
        "UNDEREXPANSION": "YETERSİZ GENİŞLEME",
        "EFFECT OF AMBIENT PRESSURE": "ÇEVRESEL BASINCIN ETKİSİ",
        "NOZZLE FLOW": "LÜLE AKIŞI",
        "THRUST": "İTME KUVVETİ",
        # Deep Learning / Neural Networks
        "Deep Networks": "Derin Ağlar",
        "Hierarchical Representations": "Hiyerarşik Temsiller",
        "Distributed Representations": "Dağıtılmış Temsiller",
        "Representation Learning": "Temsil Öğrenimi",
        "representation learning": "Temsil Öğrenimi",
        "Transfer Learning": "Aktarım Öğrenimi",
        "Weight-Space Symmetries": "Ağırlık Uzayı Simetrileri",
        "Weight Space Symmetries": "Ağırlık Uzayı Simetrileri",
        "Weight-space symmetries": "Ağırlık Uzayı Simetrileri",
        "weight-space symmetries": "Ağırlık Uzayı Simetrileri",
        "Hidden Unit Activation Functions": "Gizli Birim Aktivasyon Fonksiyonları",
        "Universal Approximation": "Evrensel Yaklaşım",
        "Parameter Matrices": "Parametre Matrisleri",
        "Multi-Head Attention": "Çok Başlıklı Dikkat",
        "Attention Mechanism": "Dikkat Mekanizması",
        "attention mechanism": "Dikkat Mekanizması",
        "Transformer Block": "Dönüştürücü Bloğu",
        # AI Engineering
        "Prefill": "Önceden Doldurma",
        "Decode": "Kod Çözme",
        "Model Architecture": "Model Mimarisi",
        "Tokenization": "Belirteçleme",
        "Embedding": "Gömme",
        "Embeddings": "Gömmeler",
        "Fine-Tuning": "İnce Ayar",
        "Pre-Training": "Ön Eğitim",
        "Other Model Architectures": "Diğer Model Mimarileri",
        # Convolutional Networks
        "Convolutional Networks": "Evrişimli Ağlar",
        "Convolutional Network": "Evrişimli Ağ",
        "Convolutional Layers": "Evrişimli Katmanlar",
        "Convolutional Layer": "Evrişimli Katman",
        # Bias
        "Bias Parameters": "Önyargı Parametreleri",
        "Bias Parameter": "Önyargı Parametresi",
        "Bias and Variance": "Önyargı ve Varyans",
        "Bias-Variance Tradeoff": "Önyargı-Varyans Ödünleşimi",
        # Transformer / NLP
        "Transformer": "Dönüştürücü",
        "Transformers": "Dönüştürücüler",
        "Transformer Architecture": "Dönüştürücü Mimarisi",
        "Transformer Models": "Dönüştürücü Modelleri",
        "Tokens": "Belirteçler",
        "Token Embeddings": "Belirteç Gömmeleri",
        "Attention Heads": "Dikkat Başlıkları",
        "Self-Attention": "Öz Dikkat",
        "Multi-Head Self-Attention": "Çok Başlıklı Öz Dikkat",
        "Dot Product Attention": "İç Çarpım Dikkat Mekanizması",
        "Scaled Dot Product Attention": "Ölçeklenmiş İç Çarpım Dikkat Mekanizması",
        "Feedforward Network": "İleri Beslemeli Ağ",
        "Feed-Forward Network": "İleri Beslemeli Ağ",
        "Positional Encoding": "Konumsal Kodlama",
        "Positional Embeddings": "Konumsal Gömmeler",
        # Physics / Flow
        "Subsonic Flow": "Ses Altı Akış",
        "Supersonic Flow": "Ses Üstü Akış",
        "Transonic Flow": "Transonik Akış",
        "Hypersonic Flow": "Hipersonik Akış",
        "Compressible Flow": "Sıkıştırılabilir Akış",
        "Incompressible Flow": "Sıkıştırılamaz Akış",
        "Convergent-Divergent Nozzle": "Yakınsak-Iraksak Lüle",
        "Choked Flow": "Tıkanmış Akış",
        # Linear Algebra
        "Closest Point": "En Yakın Nokta",
        "Gram-Schmidt Process": "Gram-Schmidt Süreci",
        "Orthogonal Projection": "Ortogonal İzdüşüm",
        "Inner Product": "İç Çarpım",
        "Dot Product": "İç Çarpım",
        "Linear Independence": "Doğrusal Bağımsızlık",
        "Basis": "Baz",
        "Subspace": "Alt Uzay",
        "Eigenvalue": "Özdeğer",
        "Eigenvector": "Özvektör",
        "Determinant": "Determinant",
        "Matrix Decomposition": "Matris Ayrışımı",
        "Singular Value Decomposition": "Tekil Değer Ayrışımı",
        "Principal Component Analysis": "Temel Bileşen Analizi",
        # Astrophysics
        "Binary Stars": "Çift Yıldızlar",
        "Light Curve": "Işık Eğrisi",
        "Spectroscopic Binary": "Spektroskopik Çift Yıldız",
        "Eclipsing Binary": "Tutulma Çift Yıldızı",
        "Mass-Luminosity Relation": "Kütle-Parlaklık İlişkisi",
        "Hertzsprung-Russell Diagram": "Hertzsprung-Russell Diyagramı",
        "Radial Velocity": "Radyal Hız",
        "Radial Velocity Measurements": "Radyal Hız Ölçümleri",
        "Eccentricity": "Eksantriklik",
        "Orbital Elements": "Yörünge Elemanları",
        "Stellar Masses": "Yıldız Kütleleri",
        "Mass Function": "Kütle Fonksiyonu",
        "Mass Ratio": "Kütle Oranı",
        "Luminosity": "Parlaklık",
        # Compound headings (iterative partial match'e ek olarak hızlı exact match)
        "Orthogonal Projection and the Closest Point": "Dikgen İzdüşümü ve En Yakın Nokta",
        "The Gram-Schmidt Process": "Gram-Schmidt Süreci",
        "Deep Neural Networks": "Derin Sinir Ağları",
        "Review Exercises": "Tekrar Alıştırmaları",
        "Further Reading": "İleri Okuma",
        "Bibliographic Notes": "Kaynakça Notları",
        "Chapter Summary": "Bölüm Özeti",
        "Key Concepts": "Temel Kavramlar",
        "Worked Examples": "Çözülmüş Örnekler",
        "Problem Set": "Problem Seti",
        "Practice Problems": "Alıştırma Problemleri",
        # --- Book-generic headings (Forward, Preface, etc.) ---
        "Forward": "Önsöz",
        "Foreword": "Önsöz",
        "Preface": "Önsöz",
        "Acknowledgments": "Teşekkür",
        "Acknowledgements": "Teşekkür",
        "Epilogue": "Sonsöz",
        "Prologue": "Prolog",
        "Afterword": "Sonsöz",
        "Dedication": "İthaf",
        "About the Author": "Yazar Hakkında",
        "About the Authors": "Yazarlar Hakkında",
        "Table of Contents": "İçindekiler",
        "Contents": "İçindekiler",
        "Index": "Dizin",
        "Glossary": "Sözlük",
        "Notes": "Notlar",
        "Endnotes": "Son Notlar",
        "Footnotes": "Dipnotlar",
        "List of Figures": "Şekiller Listesi",
        "List of Tables": "Tablolar Listesi",
        "Copyright": "Telif Hakkı",
        "Part One": "Birinci Bölüm",
        "Part Two": "İkinci Bölüm",
        "Part Three": "Üçüncü Bölüm",
        "Part Four": "Dördüncü Bölüm",
        # --- FAZ B4: Rocket / Thermodynamics / Index headings ---
        # Roket Temelleri
        "Basic Definitions": "Temel Tanımlar",
        "Ideal Gas Mixture": "İdeal Gaz Karışımı",
        "Adiabatic Flame Temperature": "Adyabatik Alev Sıcaklığı",
        "Chemical Equilibrium": "Kimyasal Denge",
        "Stoichiometry": "Stokiyometri",
        "Stokiyometri": "Stokiyometri",
        "Effective Exhaust Velocity": "Etkin Egzoz Hızı",
        "Maximum Thrust": "Maksimum İtki",
        "Minimum Thrust": "Minimum İtki",
        "Conservation Equations": "Korunum Denklemleri",
        "Steady Quasi-One-Dimensional Flow": "Kararlı Yarı Tek Boyutlu Akış",
        "Normal Shocks": "Normal Şoklar",
        "Oblique Shocks": "Eğik Şoklar",
        "Rocket Engines": "Roket Motorları",
        "Rocket Engine": "Roket Motoru",
        "Rocket Nozzle": "Roket Lülesi",
        "Rocket Nozzles": "Roket Lüleleri",
        # Nozüller / Akış
        "Exhaust Velocity": "Egzoz Hızı",
        "Mass Flow Rate": "Kütlesel Akış Hızı",
        "Mass Flow Coefficient": "Kütlesel Akış Katsayısı",
        "Mass Flow Parameter": "Kütlesel Akış Parametresi",
        "Thrust Coefficient": "İtki Katsayısı",
        "Specific Propellant Consumption": "Özgül İtici Tüketimi",
        "Impulse-Weight Ratio": "İtki-Ağırlık Oranı",
        "Impulse-to-Weight Ratio": "İtki-Ağırlık Oranı",
        "Isentropic Flow": "İzentropik Akış",
        "Variable Area Duct": "Değişken Kesitli Kanal",
        "Ideal Rocket": "İdeal Roket",
        "Ideal Rocket Engine": "İdeal Roket Motoru",
        "The Ideal Rocket Engine": "İdeal Roket Motoru",
        "IDEAL ROCKET ENGINE": "İdeal Roket Motoru",
        "IDEAL ROCKET": "İdeal Roket",
        "Specific Impulse Efficiency": "Özgül İtki Verimi",
        "Volumetric Specific Impulse": "Hacimsel Özgül İtki",
        "Total Impulse": "Toplam İtki",
        "Characteristic Velocity": "Karakteristik Hız",
        "Propulsive Efficiency": "İtici Verim",
        "Thermal Efficiency": "Isıl Verim",
        "Overall Efficiency": "Genel Verim",
        "Divergence Angle": "Iraksama Açısı",
        "CD Nozzle": "CD Lülesi",
        # Termodinamik
        "First Law of Thermodynamics": "Termodinamiğin Birinci Yasası",
        "Second Law of Thermodynamics": "Termodinamiğin İkinci Yasası",
        "Third Law of Thermodynamics": "Termodinamiğin Üçüncü Yasası",
        "Termodinamik Yasaları": "Termodinamik Yasaları",
        "First Law": "Birinci Yasa",
        "Second Law": "İkinci Yasa",
        "Control Volume": "Kontrol Hacmi",
        "Reaction System": "Tepkime Sistemi",
        "Heat of Formation": "Oluşum Isısı",
        "Heat of Reaction": "Tepkime Isısı",
        "Heats of Formation": "Oluşum Isıları",
        "Heats of Reaction": "Tepkime Isıları",
        # Katı Yakıt / Yanma
        "Solid Propellant": "Katı İtici",
        "Solid Propellants": "Katı İticiler",
        "Liquid Propellant": "Sıvı İtici",
        "Liquid Propellants": "Sıvı İticiler",
        "Solid-Propellant Rocket Engine": "Katı İtici Roket Motoru",
        "Liquid-Propellant Rocket Engine": "Sıvı İtici Roket Motoru",
        "Hybrid Propellant Rocket Engine": "Hibrit İtici Roket Motoru",
        "Solid-Propellant Combustion": "Katı İtici Yanması",
        "Solid-Propellant Burning": "Katı İtici Yanması",
        "Thermal Model": "Isıl Model",
        "Particle Size Effects": "Parçacık Boyutu Etkileri",
        "Burning Rate Modifiers": "Yanma Hızı Değiştiricileri",
        "Burning Rate": "Yanma Hızı",
        "Propellant Burning": "İtici Yanması",
        "Internal Ballistics": "İç Balistik",
        # HEAD-10: lookup zaten case-insensitive (bkz. exact-match döngüsü),
        # bu yüzden case-duplicate giriş kaldırıldı.
        "Ignition System": "Ateşleme Sistemi",
        "Ignition Systems": "Ateşleme Sistemleri",
        "Combustion Chamber": "Yanma Odası",
        "Combustion Instability": "Yanma Kararsızlığı",
        # Ateşleme / Roket Tipleri
        "Pyrotechnic Igniters": "Piroteknik Ateşleyiciler",
        "Pyrogen Igniter": "Pirojen Ateşleyici",
        "Catalytic Igniter": "Katalitik Ateşleyici",
        "Resonance Igniters": "Rezonans Ateşleyicileri",
        "Match-Stick Devices": "Kibrit Çubuğu Cihazları",
        "Electric Propulsion": "Elektrikli İtki",
        "Electrostatic Thrusters": "Elektrostatik İticiler",
        "Electrothermal Thrusters": "Elektrotermal İticiler",
        "Nuclear Rocket Engines": "Nükleer Roket Motorları",
        "Solar Rockets": "Güneş Enerjili Roketler",
        "Multistage Rocket Engines": "Çok Kademeli Roket Motorları",
        "Single Stage Rocket Engines": "Tek Kademeli Roket Motorları",
        "Spacecraft Flight Performance": "Uzay Aracının Uçuş Performansı",
        "Flight Trajectory": "Uçuş Yörüngesi",
        "Rocket Equation": "Roket Denklemi",
        # Index / Kısa Terimler
        "Recommended Reading": "Önerilen Okumalar",
        "Suggested Readings": "Önerilen Okumalar",
        "Review Questions": "İnceleme Soruları",
        "Problems": "Problemler",  # HEAD-10: ders kitabı bağlamı (alıştırma bölümü)
        "Chapter": "Bölüm",
        "Section": "Kısım",
        "Appendices": "Ekler",
        "Abbreviations": "Kısaltmalar",
        "Symbols": "Semboller",
        "Nomenclature": "Terimler",
        # Genel
        "Energy Balance": "Enerji Dengesi",
        "Energy Equation": "Enerji Denklemi",
        "Mass Balance": "Kütle Dengesi",
        "Momentum Equation": "Momentum Denklemi",
        "Continuity Equation": "Süreklilik Denklemi",
        "Reynolds Number": "Reynolds Sayısı",
        "Mach Number": "Mach Sayısı",
        "Heat Transfer": "Isı Transferi",
        "Heat Transfer Analysis": "Isı Transferi Analizi",
        "Cooling Systems": "Soğutma Sistemleri",
        "Regenerative Cooling": "Rejeneratif Soğutma",
        "Film Cooling": "Film Soğutma",
        "Ablative Cooling": "Ablatif Soğutma",
        "Fundamentals": "Temel İlkeler",
        "Basic Principles": "Temel İlkeler",
    }

    # Turkish Title Case helper: capitalize first letter of each word.
    # Handles Turkish special chars (i->İ, ı->I) correctly.
    _TR_UPPER_MAP = str.maketrans("iışöüçğ", "İIŞÖÜÇĞ")

    @staticmethod
    def _turkish_title_case(text: str) -> str:
        """Apply Turkish Title Case to heading text.

        Capitalizes first letter of each word, handling Turkish-specific
        characters correctly (i->İ, ı->I, ş->Ş, etc.).
        Small connector words (ve, ile, bir, de, da, için) stay lowercase
        UNLESS they are the first word.
        """
        if not text:
            return text
        # Words that should stay lowercase (unless first word)
        _SMALL_WORDS = {"ve", "ile", "bir", "de", "da", "için", "ya", "veya"}

        def _tr_lower(word: str) -> str:
            # HEAD-06 fix: Türkçe-bilinçli küçültme. 'İLE'.lower() Python'da
            # combining-dot'lu 'i̇le' üretir ve _SMALL_WORDS'e eşleşmez; önce
            # İ->i, I->ı çevirip öyle küçültürüz.
            return word.replace("İ", "i").replace("I", "ı").lower()

        def _capitalize_turkish(word: str) -> str:
            if not word:
                return word
            first = word[0]
            # Turkish-specific uppercase mapping
            upper_map = {"i": "İ", "ı": "I", "ş": "Ş", "ö": "Ö",
                         "ü": "Ü", "ç": "Ç", "ğ": "Ğ"}
            upper_first = upper_map.get(first, first.upper())
            return upper_first + word[1:]

        words = text.split()
        result = []
        for i, w in enumerate(words):
            if i == 0:
                result.append(_capitalize_turkish(w))
            elif _tr_lower(w) in _SMALL_WORDS:
                result.append(_tr_lower(w))
            else:
                result.append(_capitalize_turkish(w))
        return " ".join(result)

    @staticmethod
    def _fix_turkish_ascii(text: str) -> str:
        """Fix common ASCII corruption in Turkish text from MT output.

        Detects and fixes cases where Turkish special characters were
        replaced by ASCII equivalents (e.g. Aglar -> Ağlar).
        Only fixes words that are clearly wrong (not valid Turkish).
        """
        # Common ASCII corruptions produced by MT models
        _ASCII_FIXES = {
            "Aglar": "Ağlar",
            "aglar": "ağlar",
            "Ogrenim": "Öğrenim",
            "ogrenim": "öğrenim",
            "Ogrenme": "Öğrenme",
            "ogrenme": "öğrenme",
            "Ogrenimi": "Öğrenimi",
            "ogrenimi": "öğrenimi",
            "Giris": "Giriş",
            "giris": "giriş",
            "Cikis": "Çıkış",
            "cikis": "çıkış",
            "Fonksiyonlari": "Fonksiyonları",
            "fonksiyonlari": "fonksiyonları",
            "Islem": "İşlem",
            "islem": "işlem",
            "Optimizasyon": "Optimizasyon",  # already correct
            "Dagitik": "Dağıtık",
            "dagitik": "dağıtık",
            "Yaklasim": "Yaklaşım",
            "yaklasim": "yaklaşım",
            "Uzayi": "Uzayı",
            "uzayi": "uzayı",
            "Bosluk": "Boşluk",
            "bosluk": "boşluk",
            # Additional ASCII corruptions
            "Gosterim": "Gösterim",
            "gosterim": "gösterim",
            "Genisleme": "Genişleme",
            "genisleme": "genişleme",
            "Bolum": "Bölüm",
            "bolum": "bölüm",
            "Butunluk": "Bütünlük",
            "butunluk": "bütünlük",
            "Isaret": "İşaret",
            "isaret": "işaret",
            "Istatistik": "İstatistik",
            "istatistik": "istatistik",
            "Dagilim": "Dağılım",
            "dagilim": "dağılım",
            "Duzenlilestirme": "Düzenlileştirme",
            "duzenlilestirme": "düzenlileştirme",
            "Lule": "Lüle",
            "lule": "lüle",
            "Itme": "İtme",
            "itme": "itme",  # keep lowercase -- not İ
            "Ozgul": "Özgül",
            "ozgul": "özgül",
            "Olasilik": "Olasılık",
            "olasilik": "olasılık",
            "Yogunluk": "Yoğunluk",
            "yogunluk": "yoğunluk",
            "Kumülatif": "Kümülatif",  # partial corruption
            "Siniflandirici": "Sınıflandırıcı",
            "siniflandirici": "sınıflandırıcı",
        }
        # NOT (HEAD-05): substring `replace` bilerek KORUNDU. Türkçe eklemeli
        # bir dil olduğu için düzeltilecek kökler neredeyse her zaman ekle
        # birlikte gelir ('Bolum' -> 'Bolumu', 'Islem' -> 'Islemi'); iki
        # yanlı \b eşleşme bu asıl kullanımı bozar (bkz. test_heading_
        # translation TestFixTurkishAscii). Dar kapsamlı yan etki ('Girish',
        # 'Luleå') bunu gerekçelendirmiyor; kök çözüm MT'nin ASCII üretmemesi.
        for wrong, correct in _ASCII_FIXES.items():
            if wrong in text:
                text = text.replace(wrong, correct)
        return text

    def _resolve_term_placeholders_for_heading(self, text: str) -> str:
        """Resolve term placeholders in heading text using glossary.

        When GlossaryEngine stashes terms before Phase 2 (heading
        translation), headings may contain <<TERM_N>> placeholders
        instead of actual terms.  This method restores them with
        the correct Turkish equivalent from the glossary term map.

        B3 fix: Ayrıca MT hallüsinasyonu sonucu oluşan @@TERMnnnn@@
        ve çıplak TERM_N formatlarını da yakalar.  Resolve edemediği
        artifact'i silerek heading çıktısına sızmasını engeller.
        """
        if not text:
            return text
        if not _ARTIFACT_TERM_RE.search(text):
            return text

        # Hem <<TERM_N>>, hem @@TERMnnnn@@, hem çıplak TERM_N yakalar.
        # İç grup: sayıyı çıkar (hangi formatta olursa olsun).
        combined_re = re.compile(
            r"<<TERM_(\d+)>>|@@TERM(\d+)@@|\bTERM_(\d+)\b",
            re.IGNORECASE,
        )

        def _replace_term(m: re.Match) -> str:
            # Sayı çıkarması — üç grup'tan hangisi eşleşirse
            num_str = m.group(1) or m.group(2) or m.group(3)
            if not num_str:
                return ""
            try:
                idx = int(num_str)
            except (TypeError, ValueError):
                return ""

            term_map = getattr(self, "_glossary_term_map", None) or {}
            if idx in term_map:
                match = term_map[idx]
                # TermMatch objesi — tr_term attribute'ı var
                tr_term = getattr(match, "tr_term", None)
                en_term = getattr(match, "en_term", "")
                if tr_term:
                    logger.info(
                        "Heading: resolved TERM_%d '%s' -> '%s'",
                        idx, en_term, tr_term,
                    )
                    return tr_term

            # Resolve edemediyse artifact'i sil (heading'e bozuk sızmasın).
            logger.warning(
                "Heading: unresolved term placeholder TERM_%d - stripping",
                idx,
            )
            return ""

        return combined_re.sub(_replace_term, text)

    def _sanitize_heading_artifacts(self, text: str) -> str:
        """B3 fix: heading çıktısında kalan glossary artifact'lerini temizler.

        Hiç artifact yoksa text'i değiştirmeden döner.  Varsa (resolve
        edilemediği için kalan kırıntılar) siler ve çift boşluk/leading/
        trailing noktalama kalıntılarını toparlar.
        """
        if not text or not _ARTIFACT_TERM_RE.search(text):
            return text
        logger.warning("Heading: sanitizing residual artifact in output: %r", text)
        cleaned = _ARTIFACT_TERM_RE.sub("", text)
        # Art arda boşlukları tek boşluğa indirge
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        # Tire öncesinde/sonrasında artakalan garip kombinasyonları düzelt
        cleaned = re.sub(r"-\s+", "-", cleaned)
        cleaned = re.sub(r"\s+-", "-", cleaned)
        # Dangling tire (başta/sonda) temizle
        cleaned = cleaned.strip(" -")
        return cleaned.strip()

    def _heading_is_english(self, text: str) -> bool:
        """B-1 fix: başlık dil kapısı — kanonik dedektöre delege.

        latex_pipeline/retranslator.is_english_line KANONIK dedektördür
        (arayüz sözleşmesi §4). Başlıklar kısa olduğu için is_heading=True
        ile gevşek eşiklerle çağrılır. Lazy import — circular import önleme.
        """
        try:
            from latex_pipeline.retranslator import is_english_line
        except Exception as exc:
            # 2026-07-11: eskiden sessiz fail-open'di (her şeye 'İngilizce
            # değil') — kapı fiilen devre dışı kalıyordu. Yerleşik minimal
            # dedektörle devam et: Türkçe karakter varsa Türkçe kabul,
            # yoksa >=2 ASCII kelime İngilizce sayılır (başlıklar için yeterli).
            logger.error(
                "Dil kapısı: is_english_line import edilemedi (%s) — "
                "yerleşik fallback dedektör devrede.", exc,
            )
            if re.search(r"[çğıöşüÇĞİÖŞÜ]", text):
                return False
            return len(re.findall(r"[A-Za-z]{3,}", text)) >= 2
        if not self._source_is_english():
            # 2026-09-28 (K5): başlık kaynak dilde (ya da İngilizce) kaldı mı.
            from latex_pipeline.retranslator import is_untranslated_line
            return is_untranslated_line(text, self._source_code(),
                                        is_heading=True)
        return is_english_line(text, is_heading=True)

    def _apply_heading_glossary_wb(self, text: str) -> str:
        """HEAD-01 fix: kelime-SINIRLI (\\b) heading glossary değişimi.

        Eski partial-match dalı substring (`en in text`) kullanıyordu ve
        'Section' -> 'Sectional' içinde eşleşip 'Kısımal' üretiyordu.
        Burada yalnızca tam-kelime eşleşme yapılır; stop-word silme YOK
        (HEAD-02: kelime silerek sahte çeviri üretme). Çok kelimeli terimler
        önce (longest-first) uygulanır.
        """
        if not self._source_is_english():
            # 2026-09-28 (K5): sözlük EN->TR; İngilizce dışı kaynakta kapalı.
            return text
        result = text
        terms = sorted(
            self._HEADING_GLOSSARY.items(),
            key=lambda kv: len(kv[0]), reverse=True,
        )
        for en, tr in terms:
            if len(en) < 3:
                continue
            pattern = r"\b" + re.escape(en) + r"\b"
            if re.search(pattern, result, re.IGNORECASE):
                result = re.sub(
                    pattern, lambda m, _tr=tr: _tr, result,
                    count=1, flags=re.IGNORECASE,
                )
        return re.sub(r"\s{2,}", " ", result).strip()

    def _bibliography_heading_tr(self, text: str) -> Optional[str]:
        """Kaynakça başlığının Türkçesi (G2a-2); kaynakça adı değilse None.

        Modele gitmez: İngilizce kaynakta ``_HEADING_GLOSSARY`` (References
        → Kaynaklar, Bibliography → Kaynakça), öteki adlarda
        ``BIBLIOGRAPHY_HEADING_TR``; zaten Türkçe ad aynen kalır. Numara öneki
        korunur; tamamı büyük harfli başlık Türkçe büyük harfle yazılır."""
        key = bibliography_heading_key(text)
        if key is None:
            return None
        stripped = (text or "").strip()
        m = _BIB_NUM_PREFIX_RE.match(stripped)
        prefix = m.group(0) if m else ""
        name = stripped[len(prefix):]
        if key in _BIB_TURKISH_KEYS:
            return stripped
        result = BIBLIOGRAPHY_HEADING_TR
        if self._source_is_english():
            for en, tr in self._HEADING_GLOSSARY.items():
                if en.lower() == name.lower():
                    result = tr
                    break
        if name.isupper():
            result = result.replace("i", "İ").replace("ı", "I").upper()
        logger.info("Kaynakça başlığı modele gitmeden çevrildi: '%s' -> '%s'",
                    stripped, prefix + result)
        return prefix + result

    def _translate_heading_text(self, text: str) -> str:
        """Translate heading text with glossary fallback.

        Headings are short and MT models often echo them unchanged.
        This method first checks a heading-specific glossary, then
        falls back to MT.  It does NOT apply mixed-language rejection
        since heading echoing is expected and we want to force translation.

        After translation, Turkish Title Case and ASCII fix are applied.
        """
        text_clean = text.strip()

        # G2a-2 (2026-10-05; KARAR 20, mt okuma MT-19/MT-32): kaynakça
        # başlığı modele gitmez ("Literatur" → "Kütüphanecilik" olmuştu).
        bib = self._bibliography_heading_tr(text_clean)
        if bib is not None:
            return bib

        # B3 fix: Input'ta artifact varsa (MT hallüsinasyonundan) önce resolve
        # etmeye çalış.  Resolve edemediği kısım strip edilir.
        if _ARTIFACT_TERM_RE.search(text_clean):
            logger.warning(
                "Heading contains term placeholder artifact on input: %r",
                text_clean,
            )
            text_clean = self._resolve_term_placeholders_for_heading(text_clean)
            # Double-check: hala artifact varsa strip et.
            if _ARTIFACT_TERM_RE.search(text_clean):
                logger.error(
                    "Heading artifact still present after resolve - stripping: %r",
                    text_clean,
                )
                text_clean = _ARTIFACT_TERM_RE.sub("", text_clean).strip()
                text_clean = re.sub(r"\s{2,}", " ", text_clean).strip(" -")

        # 0. Resolve <<TERM_N>> placeholders from glossary engine
        #    This restores terms that were stashed in Phase 1b before
        #    heading translation gets a chance to use the heading glossary.
        text_for_lookup = self._resolve_term_placeholders_for_heading(text_clean)

        # 1. Exact match in heading glossary (case-insensitive)
        #    Try both the original text and the resolved text.
        #    2026-09-28 (K5): sözlük EN->TR; İngilizce dışı kaynakta atlanır.
        for lookup_text in ([text_for_lookup, text_clean]
                            if self._source_is_english() else []):
            for en, tr in self._HEADING_GLOSSARY.items():
                if lookup_text.strip().lower() == en.lower():
                    logger.info("Heading glossary hit: '%s' -> '%s'", text_clean, tr)
                    return self._sanitize_heading_artifacts(tr)

        # 1b. If text had TERM placeholders and they were resolved,
        #     apply Title Case to the resolved text (already Turkish).
        if text_for_lookup != text_clean:
            result = self._turkish_title_case(text_for_lookup)
            result = self._fix_turkish_ascii(result)
            logger.info(
                "Heading from glossary terms: '%s' -> '%s'",
                text_clean, result,
            )
            return self._sanitize_heading_artifacts(result)

        # 2. MT çevirisi — TEK OTORİTE (A2 + B-1 + HEAD-01/02/12).
        #    Eski erken-return eden "partial glossary + stop-word silme"
        #    ve "kelime-kelime salata" dalları KALDIRILDI: onlar MT'yi hiç
        #    çağırmadan 'Kısımal', 'Bölüms', yarı-İngilizce başlık üretiyordu
        #    (bilinen Bug 2/Bug 4). Artık akış: MT (başlık modu) -> dil kapısı
        #    -> kelime-sınırlı glossary post-pass.
        #    ALL-CAPS başlıkları önce title case'e çevir (MT ALL-CAPS'i echo eder).
        #    2026-09-24: <<LATEX_N>> placeholder'ları (başlıktaki \textit{n},
        #    $...$ vb.) oran hesabına girmez ve title-case'lenmez; aksi hâlde
        #    "<<Latex_0>>" olup restore edilemezdi.
        _ph_free = _LATEX_PLACEHOLDER_RE.sub("", text_clean)
        if _is_effectively_all_caps(_ph_free) and len(_ph_free) > 5:
            text_for_mt = _smart_title_case_keep_placeholders(text_clean)
        else:
            text_for_mt = text_clean

        # A2 fix: başlık modu — MT instruction'ına "bu bir bölüm
        # başlığıdır" bilgisi geçir.
        # B-1 (2026-07-27): başlık çevirisi artık zaman aşımı korumalı —
        # eskiden ana thread'de korumasız translate_text çağrısıydı; asılı
        # generate tüm pipeline'ı süresiz kilitliyordu.
        mt_result = self._mt_call_with_timeout(text_for_mt, is_heading=True)

        if mt_result and mt_result.strip():
            result = mt_result.strip()
            # Apply prompt leak cleanup (G2a-2: istem işareti dahil)
            result = self._sweep_prompt_markers(result, "başlık",
                                                source=text_for_mt).strip()
            for pattern in self._PROMPT_LEAK_PATTERNS:
                if pattern.search(result):
                    # FX3 (V2 Y2): kaynakta geçen biçim meşru içeriktir.
                    result = sub_unless_in_source(
                        pattern, result, text_for_mt).strip()
            result = self._fix_turkish_ascii(result)

            # B-1 fix: DİL KAPISI. MT sonucu hâlâ İngilizce/echo ise kabul etme.
            result_lower = result.lower()
            # Denetim 2026-09-24 (M2#2): yankı noktalama/tanımlık/harf
            # büyüklüğü duyarsız ('Rivers and Lakes' -> 'Rivers and Lakes.').
            is_echo = (
                result_lower == text_for_mt.strip().lower()
                or result_lower == text_clean.lower()
                or _heading_is_echo(text_for_mt, result)
                or _heading_is_echo(text_clean, result)
            )
            # 2026-09-24: yarım çeviri kapısı. is_english_line tek Türkçe
            # karakter görünce "Türkçe" diyor; "İki–Phase Flow Dynamics"
            # (kaynağın 3 ardışık kelimesi aynen duruyor) kapıdan geçiyordu.
            # Kaynağın >=3 ardışık kelimesi çıktıda aynen sürüyorsa bu bir
            # çeviri değil, çevrilmemiş parça taşıyan yarım başlıktır.
            # 2-3 kelimelik başlıkta eşik 2 kelimedir (M2#2); sayım sözlük
            # sonrası metinde yapılır: sözlüğün Türkçeleştirdiği kelime
            # ('Numerical Methods' -> 'Sayısal Yöntemler') kalmış sayılmaz.
            keeps_run = (not is_echo) and (
                _heading_keeps_source_run(text_for_mt, result)
                or _heading_keeps_source_words(
                    text_for_mt, self._apply_heading_glossary_wb(result))
            )
            if keeps_run:
                logger.warning(
                    "Heading MT half-translated (source run survives), "
                    "rejected: %r -> %r", text_clean, result,
                )
            if not is_echo and not keeps_run and not self._heading_is_english(result):
                # MT kabul edildi. HEAD-01 fix: kelime-SINIRLI glossary
                # post-pass ile MT'nin bıraktığı bilinen İngilizce terimleri
                # düzelt (substring değil; 'Section' 'Sectional'ı bozmaz).
                result = self._apply_heading_glossary_wb(result)
                result = self._turkish_title_case(result.strip())
                return self._sanitize_heading_artifacts(result)

            # MT başarısız (echo/İngilizce/yarım). Kelime-sınırlı glossary
            # yalnız başlığın TAMAMINI karşılıyorsa kabul edilir.
            # 2026-09-24: eski hâli tek kelime bile eşleşince kabul ediyordu
            # ('Local Reference Grid' -> 'Local Reference Izgara');
            # kaynağın herhangi bir kelimesi çıktıda sürüyorsa yarım demektir,
            # reddedilir (kelime-kelime sözlük çevirisi çözüm DEĞİLDİR).
            glossed = self._apply_heading_glossary_wb(text_clean)
            if (glossed != text_clean
                    and not self._heading_is_english(glossed)
                    and _heading_fully_translated(text_clean, glossed)):
                glossed = self._fix_turkish_ascii(glossed)
                glossed = self._turkish_title_case(glossed.strip())
                logger.info(
                    "Heading via word-boundary glossary: '%s' -> '%s'",
                    text_clean, glossed,
                )
                return self._sanitize_heading_artifacts(glossed)

        # Son çare: çeviremedik. HEAD-12 fix: yarı-İngilizce salata ÜRETME —
        # orijinal başlığı olduğu gibi koru (düşük-güven).
        if self._core_no_answer_for(text_for_mt):
            # FX3 (V2 Y3): model başlıkta cevap üretemedi — işaretlenir.
            self._keep_no_answer(text_clean)
        logger.warning(
            "Heading left untranslated (low-confidence): %r", text_clean,
        )
        return self._sanitize_heading_artifacts(text_clean)

    def _translate_heading_with_placeholders(self, title: str) -> str:
        """``<<LATEX_N>>`` içeren başlık metnini başlık otoritesiyle çevir.

        Placeholder'lar MT için opak token'dır (gövde yolu da böyle
        çalışır). Çeviri sonrası placeholder kümesi birebir korunmamışsa
        (MT yuttu/çoğalttı/bozdu) orijinal başlık korunur — restore
        aşamasında yapı kaybı olmaz. Dil kapısı ve yarım-çeviri kapısı
        ``_translate_heading_text`` içinde uygulanır.
        """
        ph_in = sorted(_LATEX_PLACEHOLDER_RE.findall(title))
        translated = self._translate_heading_text(title)
        ph_out = sorted(_LATEX_PLACEHOLDER_RE.findall(translated))
        if ph_in != ph_out:
            logger.warning(
                "Placeholder'lı başlık: MT placeholder kümesini bozdu "
                "(%s -> %s) — orijinal korundu: %r", ph_in, ph_out, title,
            )
            return title
        return translated

    def _translate_structured(self, body: str) -> str:
        """Translate inner text of headings and captions."""

        def _translate_heading(match: re.Match) -> str:
            cmd = match.group(1)       # e.g. \section*
            opt = match.group(2)       # e.g. [Kısa TOC başlığı] veya None
            inner = match.group(3).strip()

            # Opsiyonel kısa başlık ([...]) TOC'a gider — o da çevrilir.
            opt_out = ""
            if opt:
                opt_inner = opt[1:-1].strip()
                if opt_inner and "<<LATEX_" not in opt_inner:
                    opt_out = f"[{self._translate_heading_text(opt_inner)}]"
                else:
                    opt_out = opt

            # Separate section number prefix from title text
            # e.g. "2.1 Introduction" -> num_prefix="2.1 ", title="Introduction"
            # Also handles "6.3. Shallow Models" -> num_prefix="6.3. "
            # 2026-09-24: numara öneki placeholder dalından ÖNCE ayrılır.
            # Eskiden "1.4 The <<LATEX_0>>-Axis Rule" numarasıyla birlikte
            # placeholder dalına giriyor, "1.4" dil kapısındaki kelime
            # oranını sulandırıp İngilizce çıktıyı kabul ettiriyordu.
            num_prefix = ""
            title_text = inner
            num_match = self._SECTION_NUM_PREFIX_RE.match(inner)
            if num_match:
                num_prefix = num_match.group(1)
                title_text = inner[len(num_prefix):].strip()
            if not title_text:
                return f"{cmd}{opt_out}{{{inner}}}"

            # If heading contains <<LATEX_N>> placeholders (e.g. stashed
            # \textit{n} / \href inside heading), translate it as ONE heading
            # through the heading authority and verify the placeholders
            # survived. 2026-09-24: eski dal metni placeholder etrafında
            # parçalara bölüp ("The", "-Axis Rule") gövde yoluyla
            # çeviriyordu — parçalar anlamsızdı, başlık otoritesi ve başlık
            # dil kapısı atlanıyordu.
            # NOTE: <<TERM_N>> placeholders are handled inside
            # _translate_heading_text via _resolve_term_placeholders_for_heading.
            if "<<LATEX_" in title_text:
                translated = self._translate_heading_with_placeholders(title_text)
                return f"{cmd}{opt_out}{{{num_prefix}{translated}}}"

            # Use heading-specific translation (glossary + MT)
            translated = self._translate_heading_text(title_text)
            return f"{cmd}{opt_out}{{{num_prefix}{translated}}}"

        # NOT (F18): Basliklar TEKLI yolda kalir — _translate_heading_text
        # is_heading=True MT modu + baslik dil kapisi kullanir;
        # translate_batch_protected'ta is_heading parametresi yok, batch'e
        # tasimak baslik kalite zincirini degistirirdi.
        body = _HEADING_RE.sub(_translate_heading, body)

        # Caption'lar (F18): tek tek _safe_translate yerine once toplanir,
        # duz metin parcalari TOPLU cevrilir. Placeholder koruma sozlesmesi
        # _translate_line_preserving_placeholders ile birebir aynidir.
        caption_matches = list(_CAPTION_RE.finditer(body))
        if caption_matches:
            # 2026-09-30 (F1, MT-2): \caption* ve kısa altyazı ([...]) de
            # buradan geçer. Kısa altyazı (şekil listesine giden metin)
            # altyazılarla AYNI toplu çağrıda çevrilir.
            _cap_inners = [m.group(2).strip() for m in caption_matches]
            _cap_cmds: List[Tuple[str, Optional[int]]] = []
            for m in caption_matches:
                split = _CAPTION_CMD_SPLIT_RE.match(m.group(1))
                cmd = split.group(1) if split else m.group(1)
                opt = split.group(2) if split else None
                opt_idx: Optional[int] = None
                if opt and opt[1:-1].strip():
                    opt_idx = len(_cap_inners)
                    _cap_inners.append(opt[1:-1].strip())
                _cap_cmds.append((cmd, opt_idx))
            _cap_translated = self._translate_lines_preserving_placeholders_batch(
                _cap_inners,
            )
            _cap_iter = iter(range(len(caption_matches)))

            def _caption_repl(m: re.Match) -> str:
                k = next(_cap_iter)
                cmd, opt_idx = _cap_cmds[k]
                opt_out = (f"[{_cap_translated[opt_idx]}]"
                           if opt_idx is not None else "")
                return f"{cmd}{opt_out}{{{_cap_translated[k]}}}"

            body = _CAPTION_RE.sub(_caption_repl, body)

        # Handle \href display text inside stashed placeholders.
        # We translate the display_text portion of each stashed \href.
        self._translate_href_display_texts()

        # Handle \textit{...} and \textbf{...} with long content:
        # translate the inner text while preserving the wrapper command.
        self._translate_stashed_formatting()

        return body

    def _translate_href_display_texts(self) -> None:
        """For stashed \\href{URL}{display}, translate the display part.

        Edits ``self._stash_list`` in place so restored placeholders
        already contain translated display text.

        F18 (2026-07-28): MT gerektiren display metinleri once toplanir,
        ``_safe_translate_batch`` ile TOPLU cevrilir (oge basina ayri
        prefill+generate kuyrugu yerine); cross-ref koruma/geri-yazma
        oge bazinda aynen korunur.
        """
        if not hasattr(self, '_stash_list') or not self._stash_list:
            return

        # Support one level of nested braces in display text so that
        # \href{url}{\textbf{bold}} is captured correctly.
        href_re = re.compile(r"(\\href\s*\{[^}]*\}\s*\{)((?:[^{}]|\{[^{}]*\})*)(\})")

        # 1. gecis: MT'siz olanlari hemen yaz, MT gerekenleri topla.
        mt_jobs: List[Dict] = []
        for i, item in enumerate(self._stash_list):
            m = href_re.match(item)
            if not m:
                continue
            display = m.group(2).strip()
            if not display or len(display) < 3:
                continue
            # Protect cross-ref labels+numbers inside href display text.
            ref_m = _FIGURE_REF_RE.search(display)
            if ref_m and ref_m.start() == 0 and ref_m.end() >= len(display.rstrip('. ')):
                # Display is purely a cross-ref (e.g. "Figure 4.10.")
                translated = _translate_figure_ref(ref_m)
                trailing = display[ref_m.end():]
                translated = translated + trailing
                self._stash_list[i] = f"{m.group(1)}{translated}{m.group(3)}"
            elif ref_m:
                # Mixed: cross-ref + prose — use REF placeholders
                _ref_stash: list[str] = []
                def _stash_ref_href(m_inner: re.Match,
                                    _rs: list = _ref_stash) -> str:
                    idx = len(_rs)
                    _rs.append(_translate_figure_ref(m_inner))
                    return f"<<REF_{idx}>>"
                protected = _FIGURE_REF_RE.sub(_stash_ref_href, display)
                mt_jobs.append({
                    "i": i, "pre": m.group(1), "post": m.group(3),
                    "ref_stash": _ref_stash, "text": protected,
                })
            else:
                mt_jobs.append({
                    "i": i, "pre": m.group(1), "post": m.group(3),
                    "ref_stash": [], "text": display,
                })

        if not mt_jobs:
            return

        # 2. gecis: toplu MT + oge bazinda geri-yazma.
        outs = self._safe_translate_batch([j["text"] for j in mt_jobs])
        for job, translated in zip(mt_jobs, outs):
            for idx, tr_ref in enumerate(job["ref_stash"]):
                translated = translated.replace(f"<<REF_{idx}>>", tr_ref)
            self._stash_list[job["i"]] = f"{job['pre']}{translated}{job['post']}"

    # Regex for EPUB placeholder hallucinations that MT may inject.
    # Tightened to the specific vocabulary the MT is known to emit
    # (KNOWN/UNKNOWN/BILIN[MEYEN]/NEXT) so arbitrary ``@user42`` tokens in
    # code samples or email addresses are not caught.
    _EPUB_HALLUCINATION_RE = re.compile(
        r"@{1,2}(?:KNOWN|UNKNOWN|BILIN(?:MEYEN)?|NEXT)\d*@{0,2}"
        r"|\b(?:KNOWN|UNKNOWN|BILIN(?:MEYEN)?)\d+@@",
        re.IGNORECASE,
    )

    @staticmethod
    def _validate_stash_item(original: str, translated: str) -> bool:
        """Validate that a translated stash item is structurally sound.

        Checks:
        1. Brace balance: open and close braces must match.
        2. Command preserved: must still start with \\textit or \\textbf.
        3. No placeholder leakage: no <<LATEX_N>> or <<REF_N>> leftovers.
        4. No EPUB placeholder hallucinations: @Bilinmeyen, @@KNOWN, etc.

        Returns True if valid, False if the translation should be discarded.
        """
        # Must start with a LaTeX formatting command
        if not re.match(r"\\text(?:it|bf)\{", translated):
            return False
        # Brace balance
        open_count = translated.count("{") - translated.count("\\{")
        close_count = translated.count("}") - translated.count("\\}")
        if open_count != close_count:
            return False
        # Yer tutucular: <<REF_n>> kalıntısı yetim demektir; <<LATEX_n>>
        # ise kaynakta iç içe stash'lenmiş öğedir (italik/kalın içindeki
        # '$C_D$' gibi) ve çeviride BİREBİR aynı küme olarak durmalıdır.
        # 2026-09-29 (F3-02): eskiden her <<LATEX_n>> reddediliyordu —
        # matematikli \textbf/\textit/\footnote hiç çevrilmiyordu.
        if not LatexTranslator._stash_item_placeholders_ok(original, translated):
            return False
        # No EPUB placeholder hallucinations from MT
        if LatexTranslator._EPUB_HALLUCINATION_RE.search(translated):
            # Only reject if the original did NOT contain these patterns
            # (i.e., MT hallucinated them, they were not in the source)
            if not LatexTranslator._EPUB_HALLUCINATION_RE.search(original):
                logger.warning(
                    "Stash validation: MT hallucinated EPUB placeholder in "
                    "translated stash item. Rejecting: %.100s",
                    translated,
                )
                return False
        return True

    # Yer tutucuya BENZEYEN her token (bozuk biçimler dahil).
    _STASH_ITEM_PH_LIKE_RE = re.compile(r"<<\s*(?:LATEX|REF)[^<>\n]{0,16}>>")
    _STASH_ITEM_LATEX_PH_RE = re.compile(r"<<LATEX_\d+>>")

    @classmethod
    def _stash_item_placeholders_ok(cls, original: str, translated: str) -> bool:
        """Çevrilmiş stash öğesinin yer tutucuları kaynaktakiyle aynı mı?

        Kabul: çeviride yalnız tam biçimli ``<<LATEX_n>>`` token'ları var ve
        çoklu kümesi kaynaktakiyle BİREBİR aynı (sıra değişebilir: çeviri
        sözcük sırasını değiştirir). Red: eksik/fazla/kopya ``<<LATEX_n>>``,
        herhangi bir ``<<REF_n>>`` kalıntısı ya da bozuk biçim
        (``<< LATEX_3 >>``) — o durumda öğe kaynak hâliyle kalır (F3-02)."""
        like = cls._STASH_ITEM_PH_LIKE_RE.findall(translated)
        exact = cls._STASH_ITEM_LATEX_PH_RE.findall(translated)
        if len(like) != len(exact):
            return False
        return (Counter(exact)
                == Counter(cls._STASH_ITEM_LATEX_PH_RE.findall(original)))

    def _commit_fmt_candidate(
        self, i: int, cmd: str, inner: str, translated: str,
        original_item: str,
    ) -> None:
        """textit/textbf adayini dogrula ve yerine yaz (rollback dahil)."""
        candidate = f"{cmd}{{{translated}}}"
        if self._validate_stash_item(original_item, candidate):
            self._stash_list[i] = candidate
            logger.debug(
                "Translated %s content: '%.40s' -> '%.40s'",
                cmd, inner, translated,
            )
        else:
            # Translation broke the structure -- keep original
            logger.warning(
                "Stash integrity: translation of item %d "
                "failed validation, keeping original. "
                "Original: '%.60s', Candidate: '%.60s'",
                i, original_item, candidate,
            )

    def _commit_footnote_candidate(
        self, i: int, fn_cmd: str, opt_bracket: str, inner: str,
        translated: str, original_item: str,
    ) -> None:
        """footnote adayini dogrula ve yerine yaz (rollback dahil)."""
        candidate = f"{fn_cmd}{opt_bracket}{{{translated}}}"
        # Validate: brace balance and placeholder set (F3-02: iç içe
        # <<LATEX_n>> kaynaktakiyle birebir aynıysa meşrudur).
        open_b = candidate.count("{") - candidate.count("\\{")
        close_b = candidate.count("}") - candidate.count("\\}")
        has_orphan = not self._stash_item_placeholders_ok(
            original_item, candidate)
        if open_b == close_b and not has_orphan:
            self._stash_list[i] = candidate
            logger.debug(
                "Translated footnote content: '%.40s' -> '%.40s'",
                inner, translated,
            )
        else:
            logger.warning(
                "Stash integrity: footnote item %d "
                "failed validation, keeping original. "
                "Original: '%.60s', Candidate: '%.60s'",
                i, original_item, candidate,
            )

    def _leading_xref(self, text: str) -> Optional[Tuple[int, str]]:
        """Metin atıf etiketi + numarayla mı başlıyor? -> (bitiş, Türkçe biçim).

        İngilizce etiketler (_FIGURE_REF_RE) her kaynakta; İngilizce DIŞI
        kaynakta ayrıca kaynak dilin etiketleri ("Abbildung 1.1:" ->
        "Şekil 1.1:"; F1 MT-10, 2026-09-30). İngilizce kaynakta davranış
        eskisiyle aynı (yalnız _FIGURE_REF_RE, eşleşme 0. konumda).
        """
        ref_m = _FIGURE_REF_RE.search(text)
        if ref_m and ref_m.start() == 0:
            return ref_m.end(), _translate_figure_ref(ref_m)
        src = self._source_code()
        if src != "en":
            for xref_re, cat in _local_xref_patterns(src):
                m = xref_re.match(text)
                if m:
                    return m.end(), _translate_local_xref(m, cat)
        return None

    def _protect_xrefs_for_mt(self, text: str, ref_stash: List[str]) -> str:
        """Metindeki atıfları Türkçe biçimleriyle ``<<REF_n>>``'e al.

        İngilizce etiketler her kaynakta; İngilizce DIŞI kaynakta kaynak
        dilin etiketleri de (F1 MT-10). Çağıran ``ref_stash``'i geri yazar.
        """
        def _stash(m: re.Match, tr: str) -> str:
            ref_stash.append(tr)
            return f"<<REF_{len(ref_stash) - 1}>>"

        text = _FIGURE_REF_RE.sub(
            lambda m: _stash(m, _translate_figure_ref(m)), text)
        src = self._source_code()
        if src != "en":
            for xref_re, cat in _local_xref_patterns(src):
                text = xref_re.sub(
                    lambda m, _c=cat: _stash(m, _translate_local_xref(m, _c)),
                    text)
        return text

    def _translate_stashed_formatting(self) -> None:
        """Translate inner text of stashed \\textit, \\textbf, \\footnote.

        Long \\textit and \\textbf commands are fully stashed to protect
        the wrapper command (preventing textit->textbf corruption by MT).
        \\footnote commands are stashed to protect the wrapper; inner text
        is translated here.

        Safety: Each translated item is validated for structural integrity
        (brace balance, command preservation, no orphan placeholders).
        If validation fails, the original stash item is kept unchanged
        to prevent downstream restore failures.

        Edits ``self._stash_list`` in place.

        F18 (2026-07-28): MT gerektiren iceler once toplanir,
        ``_safe_translate_batch`` ile TOPLU cevrilir (dipnot/italik yogun
        kitapta oge basina ayri prefill+generate kuyrugu dakikalar
        suruyordu); dogrulama/rollback oge bazinda aynen korunur.
        """
        if not hasattr(self, '_stash_list') or not self._stash_list:
            return

        # Match \textit{content} or \textbf{content} with one level nesting
        fmt_re = re.compile(
            r"(\\text(?:it|bf))\{((?:[^{}]|\{[^{}]*\})*)\}"
        )

        # Match \footnote{content} or \footnotetext[N]{content}
        # with up to 2 levels of nested braces
        footnote_re = re.compile(
            r"(\\footnote(?:text)?)\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}"
        )

        # 1. gecis: MT'siz olanlari hemen isle, MT gerekenleri topla.
        mt_jobs: List[Dict] = []

        for i, item in enumerate(self._stash_list):
            # --- \textit / \textbf ---
            m = fmt_re.match(item)
            if m:
                cmd = m.group(1)       # \textit or \textbf
                inner = m.group(2).strip()
                # Only translate if content looks like prose (>= 3 chars,
                # contains spaces or is long enough to be a phrase).
                # Single-word short content was already handled by
                # _TEXTIT_SHORT_RE / _TEXTBF_SHORT_RE and stays untouched.
                # Short-form fast path: translate \textbf{Fig.5} / \textit{Eq.(4)}
                # that don't match the prose gate below (no space, <=8 chars)
                # but DO contain a figure/table/equation ref. Pure label+number
                # forms are converted directly without calling MT.
                if inner and len(inner) >= 2 and not (
                    ' ' in inner or len(inner) > 8
                ):
                    short_ref_m = _FIGURE_REF_RE.fullmatch(inner)
                    if short_ref_m is not None:
                        tr_short = _translate_figure_ref(short_ref_m)
                        candidate_short = f"{cmd}{{{tr_short}}}"
                        if self._validate_stash_item(item, candidate_short):
                            self._stash_list[i] = candidate_short
                            logger.debug(
                                "Short-form %s ref: '%.40s' -> '%.40s'",
                                cmd, inner, tr_short,
                            )
                        continue

                # 2026-09-29 (F3-03 kardeşi): tek sözcüklük kısa vurgu
                # ('\textbf{Note}', '\textbf{Solution}') değişken/sembol
                # değilse o da çevrilir; eskiden ≤8 karakterlik sözcük kaynak
                # dilde kalıyor, 9+ karakterlik ('Definition') çevriliyordu.
                if inner and len(inner) >= 3 and (
                    ' ' in inner or len(inner) > 8
                    or not _short_italic_is_symbol(inner, self._source_code())
                ):
                    # Protect cross-ref labels+numbers BEFORE MT.
                    # E.g. "Definition 2.14." -> label translated,
                    # number preserved exactly.
                    _lead = self._leading_xref(inner)
                    if _lead is not None:
                        # Cross-ref label+number at the start
                        _ref_end, tr_ref = _lead
                        rest = inner[_ref_end:]
                        if rest and rest.strip() and len(rest.strip()) >= 3:
                            # Trailing prose after cross-ref: MT isi
                            mt_jobs.append({
                                "kind": "fmt_rest", "i": i, "cmd": cmd,
                                "inner": inner, "tr_ref": tr_ref,
                                "rest": rest, "text": rest.strip(),
                            })
                        else:
                            # Just cross-ref (+ optional punctuation)
                            self._commit_fmt_candidate(
                                i, cmd, inner, tr_ref + rest, item,
                            )
                    else:
                        # Protect inline cross-refs from MT corruption.
                        # Replace with placeholders, translate prose, restore.
                        _ref_stash: list[str] = []
                        protected = self._protect_xrefs_for_mt(inner, _ref_stash)
                        mt_jobs.append({
                            "kind": "fmt_prose", "i": i, "cmd": cmd,
                            "inner": inner, "ref_stash": _ref_stash,
                            "text": protected,
                        })
                continue

            # --- \footnote / \footnotetext ---
            fn_m = footnote_re.match(item)
            if fn_m:
                fn_cmd = fn_m.group(1)     # \footnote or \footnotetext
                inner = fn_m.group(2).strip()
                if inner and len(inner) >= 3:
                    # Preserve optional [N] marker from original stash item
                    # e.g. \footnotetext[3]{...} -> extract "[3]"
                    _opt_bracket = ""
                    _opt_m = re.match(
                        r"\\footnote(?:text)?\s*(\[[^\]]*\])", item
                    )
                    if _opt_m:
                        _opt_bracket = _opt_m.group(1)
                    # Protect cross-refs inside footnote content
                    _fn_ref_stash: list[str] = []
                    protected = self._protect_xrefs_for_mt(inner, _fn_ref_stash)
                    mt_jobs.append({
                        "kind": "footnote", "i": i, "fn_cmd": fn_cmd,
                        "opt": _opt_bracket, "inner": inner,
                        "ref_stash": _fn_ref_stash, "text": protected,
                    })

            # --- tabular / tabularx / longtable ---
            # Translate text cells inside tabular environments.
            # Structural elements (\toprule, \midrule, \hline, \\, &,
            # \begin, \end, column specs) are preserved.
            tab_m = re.match(
                r"(\\begin\{(?:tabular|tabularx|longtable)\}\s*(?:\{[^}]*\})?)"
                r"(.*)"
                r"(\\end\{(?:tabular|tabularx|longtable)\})",
                item, re.DOTALL,
            )
            if tab_m:
                tab_begin = tab_m.group(1)
                tab_body = tab_m.group(2)
                tab_end = tab_m.group(3)
                translated_body = self._translate_tabular_body(tab_body)
                self._stash_list[i] = tab_begin + translated_body + tab_end
                logger.debug(
                    "Translated tabular content (item %d, %d chars)",
                    i, len(tab_body),
                )

        if not mt_jobs:
            return

        # 2. gecis: toplu MT + oge bazinda dogrulama/geri-yazma (F18).
        outs = self._safe_translate_batch([j["text"] for j in mt_jobs])
        for job, out in zip(mt_jobs, outs):
            i = job["i"]
            original_item = self._stash_list[i]
            if job["kind"] == "fmt_rest":
                translated = job["tr_ref"] + " " + out
                self._commit_fmt_candidate(
                    i, job["cmd"], job["inner"], translated, original_item,
                )
            elif job["kind"] == "fmt_prose":
                translated = out
                # Restore cross-ref placeholders
                for idx, tr_ref in enumerate(job["ref_stash"]):
                    translated = translated.replace(f"<<REF_{idx}>>", tr_ref)
                self._commit_fmt_candidate(
                    i, job["cmd"], job["inner"], translated, original_item,
                )
            else:  # footnote
                translated = out
                for idx, tr_ref in enumerate(job["ref_stash"]):
                    translated = translated.replace(f"<<REF_{idx}>>", tr_ref)
                self._commit_footnote_candidate(
                    i, job["fn_cmd"], job["opt"], job["inner"],
                    translated, original_item,
                )

    # Regex for detecting structural-only tabular content (not translatable)
    _TABULAR_STRUCTURAL_RE = re.compile(
        r"^\s*(?:"
        r"\\(?:toprule|midrule|bottomrule|hline|cline\{[^}]*\}|noalign\{[^}]*\})"
        r"|\\\\(?:\s*\[[\d.]+(?:pt|mm|em|ex|in|cm|bp|pc)\])?"  # \\ or \\[3pt]
        r"|\\tabularnewline\b"
        r"|\\(?:begin|end)\{[^}]*\}"
        r"|\\(?:multicolumn|multirow)\{[^}]*\}\{[^}]*\}"  # just the command
        r")\s*$"
    )

    # Regex for detecting purely numeric / reference cells (don't translate)
    _TABULAR_SKIP_CELL_RE = re.compile(
        r"^\s*(?:"
        r"[\d.,;:+\-*/=<>()%°\s]+"           # numbers, punctuation
        r"|\\[a-zA-Z]+(?:\{[^}]*\})*\s*"     # pure LaTeX commands
        r"|<<LATEX_\d+>>"                      # placeholders
        r"|\\dotfill|\\dots|\\cdots|\\ldots"
        r")\s*$"
    )

    # 2026-09-24 (envanter H1): birim/sembol hücresi
    # ("km3 −2 s", "kg m/s²", "N·m", "Hz") MT'ye gitmez — çevrilecek söz
    # yok; model core'un [[UNIT_n]] yer tutucusunu bozup hücreyi
    # kaybettiriyordu. Ölçüt: sesli harf içeren ≥3 harfli bir sözcük yoksa
    # hücre birim/sembol görünümlüdür ("Sun", "Moon", "rad" çevrilmeye gider).
    _CELL_WORD_RE = re.compile(r"[A-Za-zÇĞİÖŞÜçğıöşü]{3,}")
    _CELL_VOWEL_RE = re.compile(r"[aeiouyAEIOUYıİöÖüÜ]")

    @classmethod
    def _is_unit_like_cell(cls, text: str) -> bool:
        """Hücre yalnız birim/sembol/sayı mı (çevrilecek sözcük yok)?"""
        cleaned = re.sub(r"<<(?:LATEX|CMD)_\d+>>", " ", text or "")
        # FX3 (2026-10-05): komut adı ("\textsuperscript") ve satır içi
        # matematik sözcük değildir — "km\textsuperscript{3} $s^{-2}$" birim
        # hücresi modele gidiyor, iskelet bekçisi üç kez geri alıp hücreyi
        # "özgün dilde bırakıldı" diye işaretliyordu (bir model denemesinde).
        cleaned = re.sub(r"\$[^$]*\$|\\[A-Za-z@]+\*?", " ", cleaned)
        for w in cls._CELL_WORD_RE.findall(cleaned):
            if cls._CELL_VOWEL_RE.search(w):
                return False
        return True

    # B13: Stop words for heading-glossary aware cell translation
    _TABULAR_CELL_STOP_WORDS: Dict[str, str] = {
        "the": "", "a": "", "an": "",
        "and": "ve", "or": "veya",
        "of": "", "in": "", "for": "için",
        "with": "ile", "by": "", "to": "",
        "on": "", "at": "", "from": "",
        "is": "", "are": "", "as": "",
        "not": "değil",
    }

    # FAZ F1 / B20: Leftover single-word fallback. After partial-match glossary
    # translation, a handful of English words may remain (e.g. "Oluşum Isıları
    # ve Reaction"). This dictionary translates such leftovers word-by-word so
    # the final TOC/tabular cell is fully Turkish. Only applied when the cell
    # is already partially Turkish -- pure English cells still fall through to
    # MT (adım 4).
    _SINGLE_WORD_FALLBACK: Dict[str, str] = {
        # Common technical single words
        "Reaction": "Tepkime",
        "Reactions": "Tepkimeler",
        "Equation": "Denklem",
        "Equations": "Denklemleri",
        "Variation": "Değişim",
        "Effect": "Etkisi",
        "Effects": "Etkileri",
        "Elements": "Öğeleri",
        "Element": "Öğe",
        "Chemistry": "Kimyası",
        "Chemical": "Kimyasal",
        "Physical": "Fiziksel",
        "Mathematical": "Matematiksel",
        "Geometry": "Geometrisi",
        "Dynamics": "Dinamiği",
        "Basics": "Temelleri",
        "Fundamentals": "Temelleri",
        "Introduction": "Giriş",
        "Conclusion": "Sonuç",
        "Analysis": "Analizi",
        "Aerothermochemistry": "Aerotermokimyası",
        "Thermodynamics": "Termodinamik",
        "Propulsion": "İtkisi",
        "Combustion": "Yanması",
        "Performance": "Performansı",
        "Parameters": "Parametreleri",
        "Parameter": "Parametresi",
        "Principles": "İlkeleri",
        "Methods": "Yöntemleri",
        "Method": "Yöntemi",
        "Types": "Türleri",
        "Type": "Türü",
        "Design": "Tasarımı",
        "Applications": "Uygulamaları",
        "Properties": "Özellikleri",
        "Property": "Özelliği",
        "Weight": "Ağırlık",
        "Mass": "Kütle",
        "Velocity": "Hız",
        "Pressure": "Basınç",
        "Temperature": "Sıcaklık",
        "Flow": "Akış",
        "Heat": "Isı",
        "Energy": "Enerji",
        "Force": "Kuvvet",
        "Ratio": "Oranı",
        "Rate": "Hızı",
        # ALL-CAPS variants
        "CHEMICAL": "Kimyasal",
        "THERMODYNAMICS": "Termodinamik",
        "GAS": "Gaz",
        "DYNAMICS": "Dinamiği",
        "EQUATION": "Denklem",
        "GEOMETRY": "Geometrisi",
        "BASICS": "Temelleri",
        "INTRODUCTION": "Giriş",
        "COMBUSTION": "Yanma",
        "PROPULSION": "İtki",
        "PERFORMANCE": "Performans",
        "EFFECT": "Etki",
        "VARIATION": "Değişim",
    }

    # FAZ F1 / B20: Turkish character set used to decide whether a cell is
    # "already partially Turkish" (adım 3.5 guard).
    _TURKISH_CHARS = frozenset("ığüşöçİĞÜŞÖÇ")

    # B13: Matches leading numeric prefix like "2.2.1 " or "A.1. "
    _TABULAR_NUMBERED_PREFIX_RE = re.compile(
        r"^(\d+(?:\.\d+)*[a-zA-Z]?\.?\s+|[A-Z](?:\.\d+)+\.?\s+)(.+)$"
    )

    def _sweep_single_word_fallbacks(self, text: str) -> str:
        """FAZ F1 / B20: translate isolated English words in a partially
        Turkish phrase using ``_SINGLE_WORD_FALLBACK``.

        Iterates over whitespace-split tokens, preserving:
        - Words containing Turkish characters (already translated / native)
        - Tokens without any alphabetic character (numbers, punctuation)
        - Tokens whose stripped form is not in the fallback dictionary

        For translatable tokens, strips surrounding punctuation to look up the
        dictionary (case-preserving: exact, Capitalized, UPPERCASE), then
        re-inserts the translation inside the original token so any adjacent
        punctuation survives.
        """
        if not text or not text.strip():
            return text
        words = text.split()
        cleaned: List[str] = []
        for w in words:
            # Keep Turkish words untouched
            if any(c in self._TURKISH_CHARS for c in w):
                cleaned.append(w)
                continue
            # Keep pure punctuation / number tokens
            if not any(c.isalpha() for c in w):
                cleaned.append(w)
                continue
            stripped = w.strip(".,;:()[]{}\"'")
            tr = self._SINGLE_WORD_FALLBACK.get(stripped)
            if tr is None:
                tr = self._SINGLE_WORD_FALLBACK.get(stripped.capitalize())
            if tr is None:
                tr = self._SINGLE_WORD_FALLBACK.get(stripped.upper())
            if tr is not None and stripped:
                cleaned.append(w.replace(stripped, tr, 1))
            else:
                cleaned.append(w)
        result = re.sub(r"\s{2,}", " ", " ".join(cleaned)).strip()
        return result

    def _translate_cell(self, cell_text: str) -> str:
        """Tabular/TOC cell translation with heading glossary priority (B13).

        TOC entries (extracted into tabular blocks by the TOC marker) are
        short titles that often exist in ``_HEADING_GLOSSARY`` verbatim.
        Before falling back to MT (which tends to echo short inputs), we
        try glossary lookups in this order:

        1. Exact case-insensitive match on trimmed text
        2. Numbered entry -> translate the title portion after the number
        3. Greedy longest-first multi-term partial match + stop-word cleanup
        4. MT fallback (via ``_safe_translate``) preserving whitespace

        Leading/trailing whitespace from the original cell is preserved.
        Returns the cell string on failure (never raises).
        """
        if not cell_text or not cell_text.strip():
            return cell_text

        # 2026-09-24: birim/sembol hücresi hiç çevrilmez (H1 veri kaybı).
        if self._is_unit_like_cell(cell_text):
            return cell_text

        trimmed = cell_text.strip()
        leading = cell_text[: len(cell_text) - len(cell_text.lstrip())]
        trailing = cell_text[len(cell_text.rstrip()):]

        # 2026-09-28 (K5): adım 1–3.5 EN->TR sözlüğüdür; İngilizce dışı
        # kaynakta hücre doğrudan MT'ye gider ("Material und Methode" ->
        # "Malzeme und Methode" gibi melez üretilmesin).
        if not self._source_is_english():
            return self._translate_cell_mt(cell_text, trimmed, leading,
                                           trailing)

        # 1. Exact match in heading glossary
        for en, tr in self._HEADING_GLOSSARY.items():
            if trimmed.lower() == en.lower():
                logger.debug("Tabular cell glossary hit: %r -> %r", trimmed, tr)
                return leading + tr + trailing

        # 2. Numbered entry: "2.2.1 Core Terms" -> "2.2.1 Ana Terimler"
        num_match = self._TABULAR_NUMBERED_PREFIX_RE.match(trimmed)
        if num_match:
            num_prefix = num_match.group(1)
            title_part = num_match.group(2).strip()
            for en, tr in self._HEADING_GLOSSARY.items():
                if title_part.lower() == en.lower():
                    logger.debug(
                        "Tabular numbered cell glossary hit: %r -> %r",
                        trimmed, tr,
                    )
                    return leading + num_prefix + tr + trailing

        # 3. Greedy longest-first multi-term partial match.
        #    2026-07-11: substring eşleşme kelime-SINIRLI (\b) yapıldı —
        #    HEAD-01 ile başlık yolunda kapatılan bug burada yaşıyordu:
        #    'Proof'⊂'Proofreading' -> 'İspatreading' gibi sözcük içi
        #    eşleşme üretiyordu (ikinci başlık otoritesi tutarsızlığı).
        matches: List[tuple] = []
        for en, tr in self._HEADING_GLOSSARY.items():
            if len(en) >= 4 and re.search(
                r"\b" + re.escape(en) + r"\b", trimmed, re.IGNORECASE
            ):
                matches.append((en, tr, len(en)))

        if matches:
            matches.sort(key=lambda x: x[2], reverse=True)
            result = trimmed
            matched_any = False
            for en, tr, _ in matches:
                pattern = r"\b" + re.escape(en) + r"\b"
                if re.search(pattern, result, re.IGNORECASE):
                    result = re.sub(
                        pattern, tr, result,
                        flags=re.IGNORECASE, count=1,
                    )
                    matched_any = True

            if matched_any:
                # Clean leftover English stop-words
                words = result.split()
                cleaned: List[str] = []
                for w in words:
                    low = w.lower()
                    if low in self._TABULAR_CELL_STOP_WORDS:
                        repl = self._TABULAR_CELL_STOP_WORDS[low]
                        if repl:
                            cleaned.append(repl)
                    else:
                        cleaned.append(w)
                result = re.sub(r"\s{2,}", " ", " ".join(cleaned)).strip()

                # FAZ F1 / B20: leftover single-word sweep. Partial glossary
                # match often leaves isolated English words (e.g. "Reaction",
                # "CHEMICAL"). Translate them word-by-word via
                # _SINGLE_WORD_FALLBACK while preserving Turkish words and
                # punctuation.
                result = self._sweep_single_word_fallbacks(result)
                logger.debug(
                    "Tabular cell partial glossary: %r -> %r", trimmed, result,
                )
                return leading + result + trailing

        # 3.5 FAZ F1 / B20: no partial glossary match, but the cell is already
        # partially Turkish. A common case is tabular TOC cells whose long
        # phrase missed the glossary but whose single English leftovers
        # ("Variation Isı Akışı ile", "Isı Akışı EQUATION") can still be
        # translated word-by-word. Pure English cells skip this branch and
        # fall through to MT (adım 4).
        if any(c in self._TURKISH_CHARS for c in trimmed):
            swept = self._sweep_single_word_fallbacks(trimmed)
            if swept != trimmed:
                logger.debug(
                    "Tabular cell single-word fallback: %r -> %r",
                    trimmed, swept,
                )
                return leading + swept + trailing

        # 4. MT fallback (preserves original behaviour)
        return self._translate_cell_mt(cell_text, trimmed, leading, trailing)

    def _translate_cell_mt(self, cell_text: str, trimmed: str, leading: str,
                           trailing: str) -> str:
        """``_translate_cell`` adım 4: MT (baştaki/sondaki boşluk korunur)."""
        try:
            tr = self._safe_translate(trimmed)
            if tr and tr.strip() and tr != trimmed:
                return leading + tr.strip() + trailing
        except Exception as exc:
            logger.debug(
                "Tabular cell MT translation failed: %s (keeping original)",
                exc,
            )

        return cell_text

    # F3-10: hücrenin başındaki / sonundaki tablo kuralı komutları
    # (\toprule, \midrule, \bottomrule, \hline, \cline{..}, \cmidrule..,
    # \addlinespace). Komut kümesinin tek kaynağı _BOOKTABS_BARE_RE.
    _CELL_LEADING_RULES_RE = re.compile(
        r"\A(?:\s*" + _BOOKTABS_BARE_RE.pattern + r")+\s*")
    _CELL_TRAILING_RULES_RE = re.compile(
        r"(?:\s*" + _BOOKTABS_BARE_RE.pattern + r")+\s*\Z")

    @classmethod
    def _split_cell_rules(cls, cell: str) -> Tuple[str, str, str]:
        """Hücreyi ``(baştaki kurallar, metin, sondaki kurallar)`` diye böl.

        Kural komutu yoksa ``("", cell, "")``. Baştaki parça komutla birlikte
        çevresindeki boşluk/satır sonunu da taşır (birebir geri eklenir)."""
        head = ""
        m = cls._CELL_LEADING_RULES_RE.match(cell)
        if m:
            head, cell = cell[:m.end()], cell[m.end():]
        tail = ""
        m = cls._CELL_TRAILING_RULES_RE.search(cell)
        if m and m.start() > 0:
            cell, tail = cell[:m.start()], cell[m.start():]
        return head, cell, tail

    def _translate_cell_keep_rules(self, cell: str) -> str:
        """Tek hücreyi (kural komutları ayrılmış) eski kurallarla çevir."""
        head, core, tail = self._split_cell_rules(cell)
        stripped = core.strip()
        if (len(stripped) < 3
                or self._TABULAR_SKIP_CELL_RE.match(stripped)
                or self._TABULAR_STRUCTURAL_RE.match(stripped)
                or self._is_unit_like_cell(stripped)):
            return cell
        try:
            tr = self._translate_cell(core)
            if tr and tr.strip() and tr.strip() != stripped:
                return head + tr + tail
        except Exception as e:
            logger.debug("Tabular cell translation failed: %s (keeping original)", e)
        return cell

    def _tabular_label_columns(self, rows: List[str]) -> set:
        """FX3 (MT-15): başlık satırında atıf/numara etiketi (Section, No.,
        Page, Bölüm…) taşıyan sütunların sırası — o sütundaki "1.10" bir
        numaradır, ondalık değil. Başlık: harf içeren ilk '&'li satır."""
        for row in rows:
            if "&" not in row or not re.search(r"[^\W\d_]{2,}", row):
                continue
            cells = row.split("&")
            label_re = _cell_header_label_re()
            return {i for i, c in enumerate(cells) if label_re.search(c)}
        return set()

    def _translate_tabular_body(self, body: str) -> str:
        """Translate text cells inside a tabular environment body.

        Processes the content between \\begin{tabular} and \\end{tabular}.
        Each row is split by ``\\\\``, then each cell by ``&``.
        Text cells (3+ chars, not pure numbers/commands) are translated.
        Structural elements are preserved exactly.

        B13: Each cell goes through ``_translate_cell`` first, which tries
        ``_HEADING_GLOSSARY`` lookups before MT. This ensures TOC entries
        (extracted as tabular blocks by the TOC marker) are translated
        consistently with real headings.
        """
        # Split into rows by \\ (keeping the \\ delimiter). Also accept
        # \tabularnewline which is semantically identical in tabular/array
        # environments.
        rows = re.split(
            r"(\\\\(?:\s*\[[\d.]+(?:pt|mm|em|ex|in|cm|bp|pc)\])?|\\tabularnewline\b)",
            body,
        )

        # FX3 (MT-15 kısmen): sayı hücresi yerelleştirmesi yalnız İngilizce
        # kaynakta ve başlığı atıf/numara etiketi olmayan sütunda.
        localize_cells = (self._source_is_english()
                          and getattr(self, "target_lang", "tr") == "tr")
        label_cols = (self._tabular_label_columns(rows)
                      if localize_cells else set())

        translated_rows: List[str] = []
        for row_part in rows:
            # If this is a \\ or \tabularnewline delimiter, keep as-is
            if re.match(r"^\s*(?:\\\\|\\tabularnewline\b)", row_part):
                translated_rows.append(row_part)
                continue

            # If this is a structural-only row, keep as-is
            if self._TABULAR_STRUCTURAL_RE.match(row_part):
                translated_rows.append(row_part)
                continue

            # Check if row contains & (i.e. it has cells)
            if '&' not in row_part:
                # Single-cell row or structural command -- check if
                # it's a structural line (hline etc.)
                stripped = row_part.strip()
                if not stripped or self._TABULAR_STRUCTURAL_RE.match(stripped):
                    translated_rows.append(row_part)
                    continue
                # Could be a single-cell text row -- try translating.
                # B13: Route through _translate_cell so heading glossary fires.
                # F3-10: satır başındaki/sonundaki kural komutu (\midrule…)
                # MT'ye gitmez; yalnız aradaki metin çevrilir.
                translated_rows.append(self._translate_cell_keep_rules(row_part))
                continue

            # Split into cells by &
            cells = row_part.split('&')
            translated_cells: List[str] = []
            for col, cell in enumerate(cells):
                # F3-10 (2026-09-29): '\\' ile bölünen satırda ayrı satırdaki
                # kural komutu sonraki satırın ilk hücresine yapışır
                # ('\midrule\n1.1'); komut MT'ye metin olarak gidiyor, model
                # adını çevirince ('\akış') tanımsız komut derlemeyi
                # düşürüyordu. Baştaki/sondaki kural komutları ayrılır,
                # yalnız aradaki metin çevrilir, komutlar AYNEN geri eklenir.
                rule_head, cell, rule_tail = self._split_cell_rules(cell)
                if rule_head or rule_tail:
                    translated_cells.append(
                        rule_head + self._translate_cell_keep_rules(cell)
                        + rule_tail)
                    continue
                cell_stripped = cell.strip()

                # FX3 (MT-15 kısmen): yalnız sayıdan oluşan hücre.
                if localize_cells and col not in label_cols:
                    localized = localize_numeric_cell(cell)
                    if localized is not None:
                        self._bump_stat("decimal_separators_localized")
                        translated_cells.append(localized)
                        continue

                # Skip empty cells, short cells, numbers-only, LaTeX commands,
                # unit/symbol-only cells (2026-09-24, H1)
                if (
                    len(cell_stripped) < 3
                    or self._TABULAR_SKIP_CELL_RE.match(cell_stripped)
                    or self._is_unit_like_cell(cell_stripped)
                ):
                    translated_cells.append(cell)
                    continue

                # This cell has translatable text content.
                # B13: _translate_cell tries _HEADING_GLOSSARY first (exact,
                # numbered, partial) then falls back to _safe_translate.
                try:
                    tr = self._translate_cell(cell)
                    if tr and tr.strip() and tr.strip() != cell_stripped:
                        translated_cells.append(tr)
                    else:
                        translated_cells.append(cell)
                except Exception as e:
                    logger.debug("Tabular cell translation failed: %s (keeping original)", e)
                    translated_cells.append(cell)  # keep original on failure

            translated_rows.append('&'.join(translated_cells))

        return "".join(translated_rows)

    # ------------------------------------------------------------------
    # Phase 3 -- Translate free-form paragraph text
    # ------------------------------------------------------------------

    # Batch translation settings.
    # _BATCH_SIZE is the FALLBACK default; the authoritative value is
    # PipelineConfig.mlx_batch_size (BATCH-14), read at call time via
    # _get_batch_size(). Kept here for instances without a pipeline_config.
    _BATCH_SIZE = 8  # Segments per sub-batch (conservative for GPU/MLX memory)

    def _get_batch_size(self) -> int:
        """Return the sub-batch size.

        Precedence: an explicit per-instance ``_BATCH_SIZE`` override (set on
        the instance, e.g. by tests) wins; otherwise the authoritative default
        is ``PipelineConfig.mlx_batch_size`` (BATCH-14); finally the class
        constant is the last-resort fallback.
        """
        inst_override = self.__dict__.get("_BATCH_SIZE")
        if inst_override:
            return int(inst_override)
        return int(getattr(self.pipeline_config, "mlx_batch_size", self._BATCH_SIZE)
                   or self._BATCH_SIZE)

    def _translate_paragraphs(self, body: str) -> str:
        """Split body into paragraphs and translate each.

        Uses batch translation when available: collects translatable
        segments across paragraphs, sends them to
        ``core/translator.py::_translate_batch_hymt()`` in one call,
        then distributes results back.  Falls back to sequential
        ``_safe_translate()`` on any error.
        """
        # Split on blank lines (standard LaTeX paragraph separator)
        paragraphs = re.split(r"(\n\s*\n)", body)
        # G2a-2 (2026-10-05, mt okuma MT-13): display matematikten önceki kısa
        # parça ("where", "One obtains") komşu bağlamıyla tek segment olur
        # (_ContextGroup; str değeri özgün metin, iki yol da tanır).
        paragraphs = self._join_context_groups(paragraphs)

        # Estimate total translatable lines for progress tracking
        self._total_lines_estimate = sum(
            1 for line in body.split("\n")
            if line.strip()
            and not self._is_placeholder_only(line.strip())
            and not _STRUCTURED_LINE_RE.match(line.strip())
        )
        self._lines_processed = 0

        # --- Try batch mode first ---
        try:
            return self._translate_paragraphs_batched(paragraphs)
        except Exception as exc:
            logger.warning(
                "Batch paragraph translation failed, falling back to "
                "sequential: %s", exc,
            )
            # Reset progress counter for sequential fallback
            self._lines_processed = 0

        # --- Fallback: original sequential mode ---
        return self._translate_paragraphs_sequential(paragraphs)

    def _translate_paragraphs_sequential(self, paragraphs: List[str]) -> str:
        """Original sequential paragraph translation (fallback)."""
        translated_parts: List[str] = []
        paragraph_count = 0

        for i, part in enumerate(paragraphs):
            if isinstance(part, _ContextGroup):
                translated_parts.append(self._translate_context_group(part))
                paragraph_count += 1
                continue
            if re.fullmatch(r"\n\s*\n", part):
                translated_parts.append(part)
                continue

            translated_block = self._translate_paragraph_block(part)
            translated_parts.append(translated_block)
            paragraph_count += 1

            if (
                self._checkpoint_dir is not None
                and paragraph_count > 0
                and paragraph_count % _CHECKPOINT_INTERVAL == 0
            ):
                partial = "".join(translated_parts)
                remaining = "".join(paragraphs[i + 1:])
                self._save_checkpoint(partial + remaining)

        return "".join(translated_parts)

    # ------------------------------------------------------------------
    # Batch translation implementation
    # ------------------------------------------------------------------

    def _translate_paragraphs_batched(self, paragraphs: List[str]) -> str:
        """Translate paragraphs using batch MT for better throughput.

        Two-pass approach:
        1. Collect pass -- scan all paragraphs, identify translatable
           segments (same skip logic as ``_translate_paragraph_block``).
           Each segment gets inline-cmd stashing applied.
        2. Batch MT pass -- send collected segments to
           ``_translate_batch_hymt()`` in groups of ``_BATCH_SIZE``.
           Long segments (> ``pipeline_config.max_segment_chars``) fall back to
           sentence-splitting individually.
        3. Reconstruct pass -- place translated results back into the
           paragraph structure, fire progress callbacks, save checkpoints.

        On any error during collect/batch, raises so that the caller
        falls back to ``_translate_paragraphs_sequential``.
        """
        # ---- Data structures for collect pass ----
        # For each paragraph: list of line entries
        # Each line entry: (original_line, action)
        # action = None (keep as-is) or list of piece descriptors
        #
        # Piece descriptor types:
        #   ("item", seg_global_idx, prefix, leading)
        #   ("full", seg_global_idx, leading, original_stripped)
        #   ("whole", seg_global_idx, leading, original_stripped) — yer tutuculu
        #     satır BÜTÜN (2026-09-24). G2a-1 (2026-10-05): eski "split"
        #     (parçalı yol) tanımlayıcısı kaldırıldı; tutmayan parça
        #     _recover_segment sırasına girer.

        para_entries: List[Optional[List]] = []  # None = blank separator
        all_segments: List[Dict] = []  # [{text, inline_cmds, ...}, ...]

        def _add_segment(text: str) -> int:
            """Stash inline commands and add to segment list.
            Returns the global index."""
            stashed, cmds = self._stash_inline_commands(text)
            cleaned = re.sub(r"<<(?:LATEX|CMD)_\d+>>", "", stashed).strip()
            idx = len(all_segments)
            all_segments.append({
                "text": stashed,
                "inline_cmds": cmds,
                "original": text,
                "clean_len": len(cleaned),
            })
            return idx

        # ---- Collect pass ----
        for part in paragraphs:
            if isinstance(part, _ContextGroup):
                # G2a-2: bağlam grubu toplu geçişe girmez, yeniden kurmada
                # tekli yoldan çevrilir (seyrek; tutmazsa parçalar ayrı).
                para_entries.append(("group", part))
                continue
            if re.fullmatch(r"\n\s*\n", part):
                para_entries.append(None)
                continue

            lines = part.split("\n")
            line_list = []

            for line in lines:
                stripped = line.strip()
                leading = line[: len(line) - len(line.lstrip())]

                # Skip empty
                if not stripped:
                    line_list.append((line, None))
                    continue

                # Skip placeholder-only
                if self._is_placeholder_only(stripped):
                    line_list.append((line, None))
                    continue

                # Skip heading/caption
                if _STRUCTURED_LINE_RE.match(stripped):
                    line_list.append((line, None))
                    continue

                # \item lines
                item_match = re.match(
                    r"(\\item\s*(?:\[[^\]]*\])?\s*)(.*)", stripped,
                )
                if item_match:
                    prefix = item_match.group(1)
                    text = item_match.group(2)
                    if text and not self._is_placeholder_only(text):
                        seg_idx = _add_segment(text)
                        if all_segments[seg_idx]["clean_len"] < 3:
                            # Nothing left after stash
                            line_list.append((line, None))
                        else:
                            line_list.append((
                                line,
                                [("item", seg_idx, prefix, leading)],
                            ))
                    else:
                        line_list.append((line, None))
                    continue

                # Regular text line
                cleaned = re.sub(r"<<LATEX_\d+>>", "", stripped).strip()
                if len(cleaned) < 3:
                    line_list.append((line, None))
                    continue

                if "<<LATEX_" not in stripped:
                    # Whole line translatable
                    seg_idx = _add_segment(stripped)
                    if all_segments[seg_idx]["clean_len"] < 3:
                        line_list.append((line, None))
                    else:
                        line_list.append((
                            line,
                            [("full", seg_idx, leading, stripped)],
                        ))
                else:
                    # 2026-09-24 (bir test koşusunun kök nedeni): satır yer
                    # tutucularıyla BÜTÜN gönderilir. Eskiden satır
                    # <<LATEX_N>> sınırlarından bölünüp her parça modele
                    # bağımsız cümle olarak gidiyordu (72 satır → 177 parça;
                    # "where" → "nerede" ×6, "is"/"at" çevrilmeden geçti,
                    # parça sonuna "…", altyazıda ":" kaybı, parantez
                    # dengesizliği, satır başı boşluk). Core katmanı
                    # <<LATEX_N>>'e dokunmuyor, bellek bu satırları
                    # saklamıyor; model yer tutucu kümesini bozarsa
                    # _execute_batch_translation segmenti "failed" işaretler
                    # ve aşağıdaki ikinci geçiş O SATIRI kurtarma sırasına
                    # sokar (G2a-1: parçalı yol yok; en kötü durum = satır
                    # özgün dilinde, işaretli).
                    seg_idx = _add_segment(stripped)
                    all_segments[seg_idx]["whole_line"] = True
                    line_list.append((
                        line,
                        [("whole", seg_idx, leading, stripped)],
                    ))

            para_entries.append(line_list)

        # If nothing to translate, just rejoin (G2a-2: bağlam grubu varsa
        # yeniden kurma geçişi onu çevirir — toplu geçiş boş kalır)
        has_groups = any(isinstance(e, tuple) for e in para_entries)
        if not all_segments and not has_groups:
            return "".join(paragraphs)

        # ---- Batch MT pass ----
        translated = (self._execute_batch_translation(all_segments)
                      if all_segments else [])

        # ---- Bütün çevirisi tutmayan parçalar: kurtarma sırası ----
        # G2a-1 (2026-10-05, mt okuma MT-1): toplu geçişte bir bekçiye
        # takılan (yer tutucu kaybı/fazlası, kalıntı, LaTeX iskeleti, sayı /
        # ilişki simgesi) ya da yankılanan yer tutuculu parça artık yer
        # tutucu sınırlarından bölünüp parça parça ÇEVRİLMEZ (eski parçalı
        # yol bir model denemesinde 13 satırın 13'ünü okunamaz bastı). Her biri
        # _recover_segment sırasına girer: aynı metin bir kez daha (tekli
        # yol) → sade biçim → özgün dilde bırak + işaret.
        timeout = getattr(getattr(self, "pipeline_config", None),
                          "segment_timeout", SEGMENT_TIMEOUT_S)
        translated = list(translated)
        for seg_idx, seg in enumerate(all_segments):
            if not seg.get("failed"):
                continue
            translated[seg_idx] = self._recover_segment(
                seg["original"], partial=seg.get("partial"),
                deadline=_time.monotonic() + timeout,
                reason=seg.get("fail_reason"),
                whole_line=bool(seg.get("whole_line")))

        # ---- Reconstruct pass ----
        result_parts: List[str] = []
        paragraph_count = 0
        para_idx = 0  # index into paragraphs (for checkpoint remaining)

        for entry_i, entry in enumerate(para_entries):
            if entry is None:
                result_parts.append(paragraphs[entry_i])
                continue
            if isinstance(entry, tuple):            # ("group", _ContextGroup)
                result_parts.append(self._translate_context_group(entry[1]))
                paragraph_count += 1
                continue

            rlines: List[str] = []
            for original_line, action in entry:
                if action is None:
                    rlines.append(original_line)
                    self._fire_line_progress(original_line)
                    continue

                kind = action[0][0]

                if kind == "item":
                    _, seg_idx, prefix, leading = action[0]
                    tr = translated[seg_idx]
                    rlines.append(f"{leading}{prefix}{tr}")
                    self._fire_line_progress(rlines[-1])

                elif kind in ("full", "whole"):
                    # "whole": yer tutuculu satır bütün çevrildi (2026-09-24);
                    # yeniden birleştirme yok, satır başı boşluk kalıntısı yok.
                    _, seg_idx, leading, orig_stripped = action[0]
                    tr = translated[seg_idx]
                    if tr != orig_stripped:
                        rlines.append(f"{leading}{tr}")
                    else:
                        self.stats.segments_skipped += 1
                        rlines.append(original_line)
                    self._fire_line_progress(rlines[-1])

            result_parts.append("\n".join(rlines))
            paragraph_count += 1

            # BATCH-13: intermediate checkpoint yazımı KALDIRILDI.
            # Ara checkpoint gövdesi (partial + ham remaining) hâlâ çözülmemiş
            # <<LATEX_N>> placeholder'ları içerdiğinden resume'da _restore_stash
            # atlanır ve _try_resume_from_checkpoint bunu .unusable olarak
            # reddederdi (boşa I/O + yanlış "ilerleme kaydedildi" güveni).
            # Resume yalnızca FINAL checkpoint'ten yapılır (translate() sonunda
            # _save_checkpoint(..., final=True)). Sequential fallback yolu
            # (_translate_paragraphs_sequential) kendi ara checkpoint'ini korur.

        return "".join(result_parts)

    # ------------------------------------------------------------------
    # G2a-2 (2026-10-05): bağlaç parçaları komşu bağlamla (mt okuma MT-13)
    # ------------------------------------------------------------------

    def _display_placeholder(self, para: str) -> Optional[str]:
        """Paragraf yalnız bir display matematik yer tutucusuysa onu döndür."""
        m = _LONE_PLACEHOLDER_RE.fullmatch(para)
        if m is None:
            return None
        stash = getattr(self, "_stash_list", None) or []
        idx = int(m.group(2))
        if (idx < len(stash) and isinstance(stash[idx], str)
                and _DISPLAY_STASH_RE.match(stash[idx])):
            return m.group(1)
        return None

    def _is_plain_text_para(self, para: str) -> bool:
        """Tek satırlık düz metin paragrafı mı (komut/başlık/madde değil)?"""
        s = para.strip()
        if not s or "\n" in s or s.startswith(("\\", "%")):
            return False
        if self._is_placeholder_only(s):
            return False
        return bool(_PROSE_WORD_RE.search(_without_placeholders(s)))

    def _is_context_fragment(self, para: str) -> bool:
        """Display matematikten önce bağlamsız kalacak kısa parça mı? (harfle
        başlar, cümle sonu noktalamasıyla bitmez, en çok
        CONTEXT_FRAGMENT_MAX_WORDS sözcük)."""
        if not self._is_plain_text_para(para):
            return False
        s = para.strip()
        if not re.match(r"[^\W\d_]", s) or _FRAGMENT_END_RE.search(s):
            return False
        words = [w for w in s.split() if not _ANY_PLACEHOLDER_RE.fullmatch(w)]
        return 0 < len(words) <= CONTEXT_FRAGMENT_MAX_WORDS

    def _join_context_groups(self, paragraphs: List[str]) -> List[str]:
        """``re.split(r"(\\n\\s*\\n)", body)`` listesinde parça + display (+
        devam) dizilerini tek :class:`_ContextGroup` öğesine indir. Liste
        içerik/ayraç sırasını korur (grup bir içerik öğesidir)."""
        out: List[str] = []
        n = len(paragraphs)
        i = 0
        while i < n:
            if i % 2 == 0 and self._is_context_fragment(paragraphs[i]):
                items = [("text", paragraphs[i])]
                displays: List[str] = []
                j = i
                while j + 2 < n:
                    disp = self._display_placeholder(paragraphs[j + 2])
                    if disp is None:
                        break
                    items += [("sep", paragraphs[j + 1]), ("disp", paragraphs[j + 2])]
                    displays.append(disp)
                    j += 2
                    if j + 2 < n and self._is_plain_text_para(paragraphs[j + 2]):
                        nxt = paragraphs[j + 2]
                        items += [("sep", paragraphs[j + 1]), ("text", nxt)]
                        j += 2
                        if (self._is_context_fragment(nxt)
                                and len(displays) < CONTEXT_GROUP_MAX_DISPLAYS):
                            continue
                    break
                if displays:
                    joined = " ".join(t.strip() for k, t in items if k != "sep")
                    out.append(_ContextGroup(items, joined, displays))
                    i = j + 1
                    continue
            out.append(paragraphs[i])
            i += 1
        return out

    def _translate_group_text(self, joined: str) -> Optional[str]:
        """Grubun birleşik satırını çevir: tek deneme + kurtarma (tekrar, sade
        biçim) — özgün dilde BIRAKMADAN; tutmazsa None."""
        timeout = getattr(getattr(self, "pipeline_config", None),
                          "segment_timeout", SEGMENT_TIMEOUT_S)
        started = _time.monotonic()
        before = self._revert_snapshot()
        na_before = self._core_no_answer_count()
        result, called = self._safe_translate_once(joined, timeout)
        if not called:
            return None
        if self._core_no_answer_count() != na_before:
            self._note_no_answer(joined)        # FX3 (V2 Y3)
        reason = self._revert_reason(before)
        if (reason is None and not is_failed_echo(result, joined)
                and self._output_problem(result, joined) is None):
            return result
        return self._recover_segment(joined, deadline=started + timeout,
                                     reason=reason, keep_source=False)

    @staticmethod
    def _split_context_group(group: "_ContextGroup", result: str) -> Optional[str]:
        """Çeviriyi display yer tutucularından geri böl; display'ler birebir
        ve sırayla. Tutarsızlıkta (eksik/kopya/yer değiştirmiş display, hiç
        metin yok) None."""
        positions = []
        for ph in group.displays:
            if result.count(ph) != 1:
                return None
            positions.append(result.index(ph))
        if positions != sorted(positions):
            return None
        segs: List[str] = []
        prev = 0
        for ph, p in zip(group.displays, positions):
            segs.append(result[prev:p].strip())
            prev = p + len(ph)
        segs.append(result[prev:].strip())
        if not any(_PROSE_WORD_RE.search(_without_placeholders(s)) for s in segs):
            return None
        # Kaynağın metin yuvaları özgün ayraç ve girintileriyle kurulur:
        # her display'den önce bir yuva, kaynakta devam paragrafı varsa sonda
        # bir yuva daha. Çeviri parçası boş kalan yuva düşer (Türkçe söz
        # dizimi içeriği display'in öbür yanına taşıdı) — ayracıyla birlikte.
        # FX3 (2026-10-05, bir model denemesi): kaynakta devam paragrafı YOKKEN
        # son display'den sonra gelen Türkçe parça ("… <display> elde
        # edilir.") eskiden AYRI paragraf basılıyordu: kaynakta karşılığı
        # olmayan paragraf QC 29a hizasını kaydırıyor (30 sahte CRITICAL,
        # puan 0) ve dizgide fiil yeni paragraf girintisiyle başlıyordu. Bu
        # parça aynı paragrafın devamıdır: display'e BOŞ SATIRSIZ eklenir.
        has_tail_slot = group.items[-1][0] == "text"
        seg_iter = iter(segs)
        pieces: List[str] = []
        sep_before = ""
        for kind, text in group.items:
            if kind == "sep":
                sep_before = text if pieces else ""
                continue
            if kind == "text":
                seg = next(seg_iter)
                if not seg:
                    continue
                lead = text[:len(text) - len(text.lstrip())]
                text = lead + seg
            pieces.append(sep_before + text)
            sep_before = ""
        tail = "" if has_tail_slot else segs[-1]
        if tail:
            pieces[-1] = pieces[-1].rstrip() + "\n" + tail
        return "".join(pieces)

    def _reduced_context_group(
        self, group: "_ContextGroup",
    ) -> Optional[Tuple["_ContextGroup", str, str]]:
        """FX3 (2026-10-05, MT-13): tutmayan grubun komşu cümleyle
        daraltılmış hâli. Kaynakta devam paragrafı varsa ve birden çok
        cümleyse grup yalnız onun İLK cümlesiyle kurulur; ``(alt grup, kalan
        cümleler, devamdan önceki ayraç)`` döner, yoksa None.

        Olgu (bir model denemesi): grup, devam paragrafının SONRAKİ bir
        cümlesindeki satır içi yer tutucuyu model dört kez yuttuğu için
        tutmadı; parça eskisi gibi tek başına çevrildi (bağlamsız, "… elde
        edilir." biçiminde askıda). Parçanın bağlamı display'in hemen
        ardındaki cümledir; kalan cümleler kendi kurtarma zinciriyle ayrı
        çevrilir."""
        if group.items[-1][0] != "text" or len(group.items) < 2:
            return None
        tail = group.items[-1][1]
        body = tail.strip()
        parts = self._source_sentence_split_re().split(body, maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            return None
        lead = tail[:len(tail) - len(tail.lstrip())]
        items = list(group.items[:-1]) + [("text", lead + parts[0])]
        joined = " ".join(t.strip() for k, t in items if k != "sep")
        return (_ContextGroup(items, joined, group.displays), parts[1],
                group.items[-2][1])

    def _translate_reduced_group(self, group: "_ContextGroup") -> Optional[str]:
        """Daraltılmış grubu çevir; tutarsa tam grubun çevirisi, yoksa None."""
        reduced = self._reduced_context_group(group)
        if reduced is None:
            return None
        sub, rest, tail_sep = reduced
        result = self._translate_group_text(sub.joined)
        split = (self._split_context_group(sub, result)
                 if result is not None else None)
        if split is None:
            return None
        self._bump_stat("context_groups")
        self._bump_stat("context_groups_reduced")
        logger.info("Bağlaç parçası komşu cümleyle birlikte çevrildi "
                    "(daraltılmış bağlam, %d display): %.70s",
                    len(sub.displays), sub.joined)
        for kind, text in sub.items[:-1]:
            if kind == "text":
                self._fire_line_progress(text)
        # Kalan cümleler (kendi satır ilerlemesini bildirir) devam
        # paragrafına eklenir; devamın ilk cümlesine düşen çeviri boşsa
        # (Türkçe söz dizimi onu display'den önceye taşıdı) kaynağın
        # ayracıyla kendi paragrafını kurar.
        rest_tr = self._translate_paragraph_block(rest)
        if split.rstrip().endswith(sub.displays[-1]):
            return split + tail_sep + rest_tr
        return split + " " + rest_tr

    def _translate_context_group(self, group: "_ContextGroup") -> str:
        """Bağlam grubunu çevir; tutmazsa (FX3) devam paragrafının ilk
        cümlesiyle daraltılmış grubu dene; o da tutmazsa parçaları eskisi
        gibi ayrı çevir (kayıpsız)."""
        result = self._translate_group_text(group.joined)
        if result is not None:
            split = self._split_context_group(group, result)
            if split is not None:
                self._bump_stat("context_groups")
                logger.info("Bağlaç parçası komşu bağlamla birlikte çevrildi "
                            "(%d display): %.70s", len(group.displays),
                            group.joined)
                for kind, text in group.items:
                    if kind == "text":
                        self._fire_line_progress(text)
                return split
        if self._had_no_answer(group.joined):
            # FX3 (V2 Y3): model grupta cevap üretemedi (bekçi tek yeniden
            # denemeyi yaptı): daraltılmış grup ya da parçaların ayrı
            # çevirisi aynı içeriğin TÜREVİNİ yeniden gönderirdi. Grup
            # kaynağı birebir kalır ve işaretlenir (kayıpsız).
            self._keep_no_answer(str(group))
            for kind, text in group.items:
                if kind == "text":
                    self._fire_line_progress(text)
            return str(group)
        reduced = self._translate_reduced_group(group)
        if reduced is not None:
            return reduced
        self._bump_stat("context_group_fallbacks")
        logger.info("Bağlamlı çeviri tutmadı; parçalar ayrı çevriliyor "
                    "(kayıpsız): %.70s", group.joined)
        return "".join(self._translate_paragraph_block(text) if kind == "text"
                       else text for kind, text in group.items)

    def _foreign_segment_code(self, text: str) -> Optional[str]:
        """Kaynak segment kaynak dilden BAŞKA dildeyse dil kodu (2026-09-24, envanter F).

        Karar core/lang_detect.classify_foreign_segment'te (Latince profili,
        Avrupa dili, Latin dışı alfabe). İngilizce kaynakta davranış AYNEN;
        2026-09-28 (K5, KARARLAR 14) İngilizce dışı kaynakta yalnız kaynağın
        alfabesinden farklı yazılmış alıntı ayrılır (ayrıntı fonksiyonun
        belgesinde). Yol A'da (manuel çeviri) MT hiç çağrılmadığı için
        etkisizdir.
        """
        try:
            from core.lang_detect import classify_foreign_segment
            return classify_foreign_segment(text, self._source_code())
        except Exception:
            return None

    def _source_code(self) -> str:
        """Kaynak dil kodu ("en" varsayılan; testler iskeleti __new__ ile kurar)."""
        return getattr(self, "source_lang", "en") or "en"

    def _source_is_english(self) -> bool:
        """İngilizce kaynak mı? (EN->TR sözlükleri yalnız o zaman çalışır; K5)"""
        return self._source_code() == "en"

    def _source_sentence_split_re(self) -> "re.Pattern[str]":
        """KAYNAK metni cümlelere bölen desen (K5, 2026-09-28).

        İngilizcede ``_SENTENCE_SPLIT_RE`` AYNEN; İngilizce dışı kaynakta
        dile özgü kısaltmaları ve Kiril büyük harfi bilen desen (bkz.
        ``_SOURCE_SENTENCE_ABBREVIATIONS``). Türkçe çıktıyı bölenler
        (``_fix_english_remnants``) eski deseni kullanır.
        """
        code = self._source_code()
        if code == "en":
            return self._SENTENCE_SPLIT_RE
        return _SOURCE_SENTENCE_SPLIT_RES.get(code, self._SENTENCE_SPLIT_RE)

    def _skip_foreign_segment(self, text: str) -> bool:
        """Segment kitabın kaynak dilinde değilse say + logla ve True döndür."""
        code = self._foreign_segment_code(text)
        if not code:
            return False
        self._bump_stat("segments_foreign_skipped")
        logger.info(
            "Segment kitabın kaynak dilinde değil (%s), çevrilmeden "
            "geçildi: %.70s",
            code, text,
        )
        return True

    def _bump_stat(self, field: str) -> None:
        """stats alanını 1 artır (test iskeletlerinde stats olmayabilir)."""
        self._bump_stat_by(field, 1)

    def _bump_stat_by(self, field: str, amount: int) -> None:
        """stats alanını *amount* kadar artır (stats yoksa sessiz)."""
        stats = getattr(self, "stats", None)
        if stats is not None and amount:
            setattr(stats, field, getattr(stats, field, 0) + amount)

    def _execute_batch_translation(
        self, segments: List[Dict],
    ) -> List[str]:
        """Translate collected segments using batch MT where possible.

        For each segment:
        - If clean text <= ``pipeline_config.max_segment_chars``: queue for batch MT
        - If clean text > ``pipeline_config.max_segment_chars``: translate individually
          via ``_translate_by_sentences()``

        After raw MT, per-segment post-processing is applied:
        ``_post_process_translation``, ``_apply_glossary``,
        ``_fix_english_remnants``, inline command restore.

        Returns a list of translated strings (same length as *segments*).
        """
        n = len(segments)
        results: List[Optional[str]] = [None] * n

        # Separate short (batchable) vs long (need sentence splitting)
        short_indices: List[int] = []
        short_texts: List[str] = []

        max_segment_chars = getattr(
            self.pipeline_config, "max_segment_chars", self._MAX_SEGMENT_CHARS
        )
        for i, seg in enumerate(segments):
            # 2026-09-24: İngilizce olmayan kaynak segment (Latince alıntı
            # vb.) MT'ye gitmez, olduğu gibi kalır (Yol A davranışı).
            if self._skip_foreign_segment(seg["original"]):
                results[i] = seg["original"]
                continue
            if seg["clean_len"] > max_segment_chars:
                # Long segment: translate with sentence splitting
                text = seg["text"]
                reverts_before = self._revert_snapshot()
                na_before = self._core_no_answer_count()
                try:
                    # BATCH-12 fix: _translate_by_sentences cümle başına zaten
                    # post_process + glossary uyguluyor ve sonda _fix_english_
                    # remnants çağırıyor; dıştan tekrar uygulama SİLİNDİ.
                    r = self._translate_by_sentences(text)
                    if r and r.strip() and r != text:
                        self.stats.segments_translated += 1
                    else:
                        r = text
                        # B-11 (2026-07-27): çevrilemez içerik 'failed' değil
                        self._count_identical_result(text)
                except Exception as exc:
                    self._log_mt_exception(logging.WARNING,
                                           "Long segment batch failed: %s", exc,
                                           exc=exc)
                    r = text
                    self.stats.segments_failed += 1
                # 2026-09-24 (denetim Mercek 2 #4a): uzun satırda cümle
                # düzeyindeki bekçi yalnız o cümleyi orijinaline döndürür.
                # G2a-1 (2026-10-05): cümle geri alındıysa, sonuç kümesi
                # bozuksa ya da yer tutuculu satır hiç çevrilmediyse parça
                # kurtarma sırasına girer; kısmi sonuç (yalnız tutmayan cümle
                # kaynak dilde) son çare olarak saklanır.
                reason = self._revert_reason(reverts_before)
                if self._missing_placeholders(r, text):
                    reason = reason or "yer tutucu kümesi"
                # FX3 (V2 Y3): cümlelerden biri cevapsız kaldıysa parça
                # kurtarmaya girmez; yarı çevrilmiş sonuç işaretlenir.
                no_answer = self._core_no_answer_count() != na_before
                if no_answer:
                    self._note_no_answer(seg["original"])
                if reason is not None or (seg.get("whole_line")
                                          and is_failed_echo(r, text)):
                    self._mark_segment_failed(seg, r, reason)
                    r = text
                elif no_answer:
                    self._keep_no_answer(seg["original"], partial=True)
                results[i] = self._restore_inline_commands(
                    r.strip(), seg["inline_cmds"],
                )
            else:
                short_indices.append(i)
                short_texts.append(seg["text"])

        # Batch translate short segments
        if short_texts:
            raw_results = self._batch_mt_call(short_texts)

            for j, raw in enumerate(raw_results):
                idx = short_indices[j]
                seg = segments[idx]
                text = seg["text"]
                inline_cmds = seg["inline_cmds"]

                reverts_before = self._revert_snapshot()
                if raw and raw.strip() and raw != text:
                    r = self._post_process_translation(raw, text)
                    # 2026-09-24 (denetim Mercek 2 #4b): onarılamayan
                    # [[UNIT/FORMULA]] kalıntısı ya da (G2a-1) sayı / ilişki
                    # simgesi bekçisi segmenti orijinaline döndürdüyse boşa
                    # sözlük/kalıntı geçişi yapılmaz; parça kurtarılır.
                    if self._revert_snapshot() == reverts_before:
                        r = self._apply_glossary(r)
                        r = self._fix_english_remnants(r)
                        # B-11 fix: placeholder kaybında sona-ekleme yerine
                        # segmenti orijinaliyle geri al (tek yardımcı).
                        # 2026-09-24: küme EŞİTLİĞİ (kayıp, fazla, kopya);
                        # G2a-1: + LaTeX iskeleti, ilişki simgesi, sayı.
                        r = self._revert_on_placeholder_loss(r, text)
                    self.stats.segments_translated += 1
                else:
                    r = text
                    # B-11 (2026-07-27): çevrilemez içerik 'failed' değil
                    self._count_identical_result(text)
                # G2a-1 (2026-10-05, mt okuma MT-1): bir bekçi segmenti ya da
                # içindeki bir cümleyi geri aldıysa ya da yer tutuculu bütün
                # satır yankılandıysa parça kurtarma sırasına girer (eski
                # parçalı yol yok). Düz satırın yankısı meşrudur (sayı/sembol).
                reason = self._revert_reason(reverts_before)
                # FX3 (V2 Y1): onaylı bellek yazımı (çekirdeğe giden metin).
                self._settle_memory(text, reason is None)
                # FX3 (V2 Y3): bu öğenin isteği cevapsız kaldıysa kurtarma
                # zinciri koşmaz (_recover_segment işaretleyip bırakır).
                no_answer = self._core_no_answer_for(text)
                if no_answer:
                    self._note_no_answer(seg["original"])
                if reason is not None or (seg.get("whole_line")
                                          and is_failed_echo(r, text)):
                    self._mark_segment_failed(seg, r, reason)
                    r = text
                elif no_answer:
                    # Satır parçası (bütün satır değil) yankısı da işaretlenir.
                    self._keep_no_answer(seg["original"], partial=(r != text))

                results[idx] = self._restore_inline_commands(
                    r.strip(), inline_cmds,
                )

        self.stats.segments_total += n
        return results

    def _mark_segment_failed(self, seg: Dict, result: str,
                             reason: Optional[str]) -> None:
        """Toplu geçişte tutmayan segmenti kurtarma sırası için işaretle
        (G2a-1). Sonuç kaynaktan farklıysa (uzun parçada yalnız bir cümle
        geri alındı, ya da içteki bir kalıntı denemesi tutmadı) kısmi sonuç
        olarak saklanır; _recover_segment yapısı sağlamsa son çare kullanır."""
        seg["failed"] = True
        seg["fail_reason"] = reason
        seg["partial"] = (
            self._restore_inline_commands(result.strip(), seg["inline_cmds"])
            if result and result.strip() and result != seg["text"] else None)

    def _count_identical_result(self, text: str) -> None:
        """Aynı çıktıyla dönen segmenti doğru sayaça yaz (B-11, 2026-07-27).

        Sayı/sembol/özel-ad ağırlıklı segmentlerin (tablo hücresi, '42',
        'Einstein 1905' vb.) aynen dönmesi DOĞRU davranıştır — bunlar
        'failed' sayılınca sayı-yoğun kitaplarda fail-ratio > 0.5 oluyor ve
        BAŞARILI koşunun final checkpoint'i yazılmıyordu. Gerçek düz yazı
        (>= 3 harfli kelime) aynen dönerse o hâlâ echo hatasıdır -> failed.
        """
        _words = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]{2,}", text)
        if len(_words) >= 3:
            self.stats.segments_failed += 1
        else:
            self.stats.segments_skipped += 1

    def _batch_mt_call(self, texts: List[str]) -> List[str]:
        """Call the core translator's batch MT for a list of short segments.

        Splits *texts* into sub-batches of ``_BATCH_SIZE`` and sends each
        to ``BookTranslator._translate_batch_hymt()`` which uses
        ``mlx_lm.batch_generate()`` under the hood.

        Timeout: ``segment_timeout * batch_size`` per sub-batch.
        Fallback on any error: translate each segment individually.
        """
        translator = self._get_translator()

        # A1 fix: Batch ANA YOLU tam koruma zincirinden geçsin.
        # SÖZLEŞME §2: core ajanı translate_batch_protected ekliyor — her text
        # için translate_text'in AYNI koruma zinciri (memory, NER, glossary,
        # ref/formül/birim protect+restore) uygulanır, MT kısmı toplu yapılır.
        # Böylece batch çıktısı ile timeout/hata fallback'inin (translate_text
        # tabanlı) kalite profili EŞİTLENİR. hasattr ile güvenli bağla; yoksa
        # eski ham batch yoluna düş.
        use_protected = hasattr(translator, 'translate_batch_protected')
        has_raw_batch = hasattr(translator, '_translate_batch_hymt')
        if not use_protected and not has_raw_batch:
            logger.info(
                "BookTranslator has neither translate_batch_protected nor "
                "_translate_batch_hymt; using sequential fallback.",
            )
            return [self._single_mt_call(t) for t in texts]

        all_results: List[str] = []
        timeout_per_seg = getattr(self.pipeline_config, 'segment_timeout', SEGMENT_TIMEOUT_S)
        batch_size = self._get_batch_size()

        for batch_start in range(0, len(texts), batch_size):
            batch = texts[batch_start:batch_start + batch_size]
            batch_timeout = timeout_per_seg * len(batch)

            # Run batch MT in a thread with timeout protection
            result_holder: List[Optional[List[str]]] = [None]
            exc_holder: List[Optional[Exception]] = [None]

            def _do_batch(b=batch):
                try:
                    if use_protected:
                        result_holder[0] = translator.translate_batch_protected(b)
                    else:
                        result_holder[0] = translator._translate_batch_hymt(b)
                except Exception as e:
                    exc_holder[0] = e

            t = threading.Thread(target=_do_batch, daemon=True)
            t.start()
            t.join(timeout=batch_timeout)

            if t.is_alive():
                # Grace join (2026-07-11): thread'i terk edip HEMEN sirali
                # fallback'e gecmek cifte is + generate kilidi yarisi demek
                # (zombi kilidi tutarken fallback ayni kilitte bekliyordu;
                # core tarafinda artik sureli acquire var ama gec biten
                # sonucu kullanmak hala en ucuz yol). Kisa bir ek sans ver.
                grace = min(60.0, batch_timeout * 0.5)
                logger.warning(
                    "Batch MT timeout (%ds for %d segments); %ds grace "
                    "bekleniyor.",
                    batch_timeout, len(batch), int(grace),
                )
                t.join(timeout=grace)

            if t.is_alive():
                logger.warning(
                    "Batch MT timeout (grace dahil) — zombi thread terk "
                    "ediliyor, sirali fallback basliyor. (Kilit core'da "
                    "sureli: asili generate MT_FAIL'e duser, pipeline "
                    "asilmaz.)"
                )
                for text in batch:
                    all_results.append(self._single_mt_call(text))
            elif exc_holder[0]:
                self._log_mt_exception(
                    logging.WARNING,
                    "Batch MT error: %s. Falling back to sequential.",
                    exc_holder[0], exc=exc_holder[0],
                )
                for text in batch:
                    all_results.append(self._single_mt_call(text))
            elif result_holder[0] is not None:
                batch_results = result_holder[0]
                if len(batch_results) != len(batch):
                    logger.warning(
                        "Batch MT returned %d results for %d inputs. "
                        "Falling back to sequential.",
                        len(batch_results), len(batch),
                    )
                    for text in batch:
                        all_results.append(self._single_mt_call(text))
                else:
                    all_results.extend(batch_results)
            else:
                for text in batch:
                    all_results.append(self._single_mt_call(text))

            done = min(batch_start + batch_size, len(texts))
            logger.info(
                "[BATCH] Translated %d/%d segments (batch_size=%d)",
                done, len(texts), batch_size,
            )
            # B-3 (2026-07-27): batched ana yol MT fazi boyunca progress
            # callback'i HIC ateslemiyordu — UI saatlerce donuk kaliyor,
            # kosu asili saniliyordu. Alt-batch bitiminde bildir.
            _cb = getattr(self, "_progress_callback", None)
            if _cb is not None:
                try:
                    _cb(done / max(len(texts), 1),
                        f"translate: Segment {done}/{len(texts)}")
                except Exception:
                    pass  # UI callback hatasi ceviriyi durdurmasin

        return all_results

    def _mt_call_with_timeout(self, text: str, *, is_heading: bool = False,
                              timeout: Optional[float] = None) -> Optional[str]:
        """translate_text'i ZAMAN AŞIMI korumalı thread'de çalıştır (B-1).

        Asılı bir Metal/MPS generate çağrısı ana thread'i süresiz
        kilitleyebiliyordu (core'daki 300s kilit zaman aşımı ancak İKİNCİ
        bir çağrı kilide çarptığında devreye girer; asılı çağrının kendisi
        hiç dönmez). Zaman aşımında/hatada None döner — çağıran orijinal
        metinle devam eder, pipeline asılmaz.
        """
        if timeout is None:
            # getattr zinciri: test iskeletleri (__new__) pipeline_config
            # set etmeyebilir — savunmacı erişim, varsayılan SEGMENT_TIMEOUT_S.
            _cfg = getattr(self, "pipeline_config", None)
            timeout = getattr(_cfg, 'segment_timeout', SEGMENT_TIMEOUT_S)

        # Reviewer R3 (2026-07-27): model YÜKLEMESİ timeout penceresinin
        # DIŞINDA yapılır — 7B modelin soğuk diskten yüklenmesi üretim
        # bütçesinden yemesin (asılma koruması generate'i hedefler; yükleme
        # zaten eski kodda da ana thread'deydi).
        try:
            translator = self._get_translator()
        except Exception as exc:
            logger.warning("MT translator load failed: %s", exc)
            return None

        result_holder: List[Optional[str]] = [None]

        def _do():
            try:
                try:
                    result_holder[0] = translator.translate_text(
                        text, is_heading=is_heading)
                except TypeError:
                    # Eski imza (is_heading yok)
                    result_holder[0] = translator.translate_text(text)
            except Exception as exc:
                self._log_mt_exception(logging.WARNING, "MT call failed: %s",
                                       exc, exc=exc)

        t = threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            logger.warning(
                "MT call timeout (%ds, is_heading=%s) — zombi thread terk "
                "edildi, orijinal metinle devam.", int(timeout), is_heading,
            )
            return None
        return result_holder[0]

    def _single_mt_call(self, text: str) -> str:
        """Translate a single segment via the core translator (fallback).

        Used when batch translation fails for a sub-batch.

        B-1 (2026-07-27): zaman aşımı korumalı — batch timeout'u zaten
        'asılı generate' işaretiyken korumasız ana-thread çağrısına düşmek
        tam da süresiz kilitlenme senaryosuydu.
        """
        result = self._mt_call_with_timeout(text)
        return result if result and result.strip() else text

    def _translate_paragraph_block(self, block: str) -> str:
        """Translate a single paragraph block (may contain \\item lines)."""
        lines = block.split("\n")
        result_lines: List[str] = []

        for line in lines:
            stripped = line.strip()

            # Skip empty lines
            if not stripped:
                result_lines.append(line)
                continue

            # Skip lines that are only placeholders / whitespace
            if self._is_placeholder_only(stripped):
                result_lines.append(line)
                continue

            # Skip heading/caption lines (already translated in Phase 2)
            if _STRUCTURED_LINE_RE.match(stripped):
                result_lines.append(line)
                continue

            # Handle \item lines: translate text after \item
            item_match = re.match(r"(\\item\s*(?:\[[^\]]*\])?\s*)(.*)", stripped)
            if item_match:
                prefix = item_match.group(1)
                text = item_match.group(2)
                if text and not self._is_placeholder_only(text):
                    translated = self._safe_translate(text)
                    # Preserve leading whitespace
                    leading = line[: len(line) - len(line.lstrip())]
                    result_lines.append(f"{leading}{prefix}{translated}")
                else:
                    result_lines.append(line)
                self._fire_line_progress(result_lines[-1] if result_lines else "")
                continue

            # Regular text line: split around placeholders, translate text parts
            translated_line = self._translate_line_preserving_placeholders(stripped)
            if translated_line != stripped:
                leading = line[: len(line) - len(line.lstrip())]
                result_lines.append(f"{leading}{translated_line}")
            else:
                self.stats.segments_skipped += 1
                result_lines.append(line)

            # Per-line progress callback
            self._fire_line_progress(result_lines[-1] if result_lines else "")

        return "\n".join(result_lines)

    def _fire_line_progress(self, last_line: str) -> None:
        """Fire progress callback after each translated line (throttled)."""
        import time

        self._lines_processed = getattr(self, "_lines_processed", 0) + 1
        total_est = getattr(self, "_total_lines_estimate", 0)
        cb = getattr(self, "_progress_callback", None)

        if not cb or total_est <= 0:
            return

        # Throttle: at most once per second (except first and last)
        now = time.monotonic()
        last_fire = getattr(self, "_last_progress_fire", 0)
        is_last = self._lines_processed >= total_est
        is_first = self._lines_processed == 1
        if not is_first and not is_last and (now - last_fire) < 1.0:
            return
        self._last_progress_fire = now

        frac = min(self._lines_processed / total_est, 1.0)
        # Preview: last translated text (non-LaTeX, non-empty)
        preview = ""
        ln_s = last_line.strip() if last_line else ""
        if ln_s and not ln_s.startswith("\\") and len(ln_s) > 10:
            preview = ln_s[:200]

        cb(
            frac,
            f"translate: Segment {self.stats.segments_translated}"
            f"/{total_est}"
            f"|||{preview}",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _translate_line_preserving_placeholders(self, line: str) -> str:
        """Satırı ``<<LATEX_N>>`` yer tutucularıyla BÜTÜN çevir (sıralı yol).

        Yer tutucular modele opak gider ve aynen dönmelidir. Hiçbir şey
        çevrilmediyse *line* aynen döner (çağıran özdeşlik denetler).
        G2a-1 (2026-10-05): eski "parçalara böl, parçaları çevir" yolu yok.
        """
        # Quick check: anything translatable?
        cleaned = re.sub(r"<<LATEX_\d+>>", "", line).strip()
        if len(cleaned) < 3:
            return line  # nothing to translate

        # If no placeholders, translate the whole line
        if "<<LATEX_" not in line:
            return self._safe_translate(line)

        # 2026-09-24: İngilizce olmayan satır (Latince alıntı) hiç çevrilmez.
        # Kapı satır düzeyinde: parçalara düşülürse kısa parçalar (<4 kelime)
        # sınıflanamaz ve MT'ye kaçardı.
        if self._skip_foreign_segment(line):
            return line

        # 2026-09-24 (bir test koşusunun kök nedeni): satır yer tutucularıyla BÜTÜN
        # gönderilir. G2a-1 (2026-10-05, mt okuma MT-1): bütün çeviri bir
        # bekçiye takılırsa (yer tutucu kaybı/fazlası, kalıntı, LaTeX
        # iskeleti, sayı/ilişki simgesi) ya da yankılanırsa kurtarma sırası
        # işler (_translate_with_recovery): aynı satır bir kez daha → sade biçim →
        # özgün dilde bırak + işaret. Eski parçalı yol (satırı yer tutucu
        # sınırlarından bölüp parça parça çevirmek) bozuk metin bastığı için
        # KALDIRILDI. Satırın TOPLAM süre bütçesi segment_timeout'tur
        # (denetim Mercek 2 #4c); kurtarma denemeleri kalanı paylaşır.
        return self._translate_with_recovery(line, whole_line=True)

    def _translate_line_pieces(
        self, line: str, deadline: Optional[float] = None,
    ) -> str:
        """ESKİ parçalı yol: ``<<LATEX_N>>`` sınırlarından böl, parçaları tek tek
        çevir.

        G2a-1 (2026-10-05, mt okuma MT-1): boru hattı bunu ARTIK ÇAĞIRMAZ —
        parçalar bağlamsız çevrildiği için okunamaz metin basıyordu (yerine
        :meth:`_recover_segment`). Yalnız doğrudan çağıran eski testler için
        duruyor; kaldırılması testleriyle birlikte önerildi
        (iç öneri yaması, G2a-1).

        *deadline* (``time.monotonic`` değeri) verilirse her parça kalan süreyi
        zaman aşımı olarak alır; süre bittiyse kalan parçalar MT'ye gitmez,
        kaynak hâliyle kalır (2026-09-24, düşüş bütçesi).
        """
        # Split: ["text", "<<LATEX_5>>", " more text", "<<LATEX_6>>", ""]
        parts = re.split(r"(<<LATEX_\d+>>)", line)
        any_translated = False
        result_parts: List[str] = []
        budget_exhausted = False

        for p in parts:
            if re.fullmatch(r"<<LATEX_\d+>>", p):
                result_parts.append(p)
            elif p.strip() and len(p.strip()) >= 3:
                # F1 (2026-09-30): kenar parantezleri MT'ye gitmez.
                lead, core, trail = _split_piece_brackets(p.strip())
                if deadline is None:
                    translated = self._safe_translate(core)
                else:
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        if not budget_exhausted:
                            budget_exhausted = True
                            logger.warning(
                                "Satır süre bütçesi bitti; kalan parçalar "
                                "çevrilmeden bırakıldı: %.60s", line,
                            )
                        result_parts.append(p)
                        continue
                    translated = self._safe_translate(core, timeout=remaining)
                if translated != core:
                    any_translated = True
                translated = f"{lead}{translated}{trail}"
                result_parts.append(f" {translated} " if p.startswith(" ") or p.endswith(" ") else translated)
            else:
                result_parts.append(p)

        if any_translated:
            return "".join(result_parts)
        return line

    def _translate_lines_preserving_placeholders_batch(
        self, lines: List[str],
    ) -> List[str]:
        """``_translate_line_preserving_placeholders``'in toplu esdegeri (F18).

        Satir basina ayni kapilar (<3 harf atla, placeholder'lar
        dokunulmaz); tum satirlar TEK toplu MT cagrisinda cevrilir. Donen
        liste *lines* ile ayni uzunluktadir.

        2026-09-24 (envanter I3: 11/11 altyazıda ":" kaybı): yer tutuculu
        altyazı da BÜTÜN gönderilir ("<<LATEX_7>>: Kepler's second law.").
        G2a-1 (2026-10-05, mt okuma MT-1): bütün çevirisi tutmayan satır
        artık yer tutucu sınırlarından bölünüp parça parça çevrilmez;
        ``_safe_translate_batch`` o öğeyi kurtarma sırasına sokar (aynı satır
        bir kez daha → sade biçim → özgün dilde bırak + işaret).
        """
        prose_texts: List[str] = []
        plans: List[Optional[int]] = []

        for line in lines:
            cleaned = re.sub(r"<<LATEX_\d+>>", "", line).strip()
            if len(cleaned) < 3:
                plans.append(None)  # nothing to translate
                continue
            # 2026-09-24: İngilizce olmayan altyazı çevrilmez (satır düzeyi kapı).
            if "<<LATEX_" in line and self._skip_foreign_segment(line):
                plans.append(None)
                continue
            plans.append(len(prose_texts))
            prose_texts.append(line)

        outs = (self._safe_translate_batch(prose_texts, whole_line=True)
                if prose_texts else [])
        return [line if plan is None else outs[plan]
                for line, plan in zip(lines, plans)]

    def _is_placeholder_only(self, text: str) -> bool:
        """True if *text* contains only placeholders and whitespace."""
        return not re.sub(r"<<LATEX_\d+>>", "", text).strip()

    def _revert_on_placeholder_loss(
        self, translated: str, original: str,
    ) -> str:
        """B-11 fix: MT bir placeholder'ı yuttuysa segmenti ORİJİNALİYLE geri al.

        Eskiden kayıp placeholder üç ayrı yerde ``r.rstrip() + " " + ph`` ile
        satır SONUNA ekleniyordu — cümle ortasındaki formül/referans sona
        taşınıp sıra bozuluyordu. Artık tek yardımcı: kayıp varsa çeviriyi
        atıp orijinali (placeholder'lar ve sıra bozulmadan) döndürür ve
        ``stats.placeholders_lost`` sayacını artırır (sona-ekleme yok).

        2026-09-24 (denetim Mercek 2 #1, Mercek 3 #8): KÜME EŞİTLİĞİ bekçisi.
        Kayba ek olarak kaynakta olmayan (halüsine) id, kopya id ve
        "<<LATEX_N>>" gibi bozuk biçimli token da segmenti orijinaline döndürür
        (``stats.placeholders_extra``). Eskiden fazladan id kabul ediliyordu;
        restore'un "ilk geçiş restore, kopyalar silinir" kuralı (#73) o id'nin
        formülünü bu satıra taşıyıp asıl satırından siliyordu. Sıra kontrolü
        YOK: Türkçe söz dizimi yer tutucuların yerini meşru olarak değiştirir.

        G2a-1 (2026-10-05): bütün MT zincirlerinin ortak son bekçisi olarak
        ayrıca LaTeX iskeleti (mt okuma MT-17), kaynakta olmayan ilişki
        simgesi ve sayı kaybı (MT-7) denetlenir (:meth:`_structure_problem`).
        Bozuk çeviri yine orijinaline döner; sayaçlar ``skeleton_reverts`` /
        ``number_reverts`` (:meth:`_segment_revert_count` bunları da sayar,
        bütün gönderilen parça kurtarma sırasına girer).
        """
        lost, extra = self._placeholder_mismatch(translated, original)
        if not lost and not extra:
            problem = self._structure_problem(translated, original)
            if problem is None:
                return translated
            kind, detail = problem
            if kind == "iskelet":
                self._bump_stat("skeleton_reverts")
            else:
                self._bump_stat("number_reverts")
            logger.warning(
                "Çeviri bekçisi (%s): %s -- segment orijinaline döndürüldü: %.60s",
                kind, detail, original,
            )
            return original
        if lost:
            logger.warning(
                "MT lost %d placeholder(s): %s -- reverting segment to original",
                sum(lost.values()), sorted(lost),
            )
            self.stats.placeholders_lost += sum(lost.values())
        if extra:
            logger.warning(
                "MT fazladan/kopya yer tutucu üretti (%d): %s -- segment "
                "orijinaline döndürüldü", sum(extra.values()), sorted(extra),
            )
            self.stats.placeholders_extra += sum(extra.values())
        return original

    def _placeholder_mismatch(
        self, translated: str, original: str,
    ) -> Tuple[Counter, Counter]:
        """Yer tutucu ÇOKLU KÜME karşılaştırması: ``(kayıp, fazla)`` (2026-09-24).

        kayıp: *original*'daki tam biçimli yer tutuculardan *translated*'da
        eksik kalanlar. fazla: *translated*'da *original*'e göre fazladan
        görünenler — kaynakta olmayan id, kopya id ve tam biçim dışında kalan
        yer tutucu benzeri token ("<<LATEX_N>>", "<< LATEX_3 >>"). Bozuk biçim
        restore'da ancak tahminle eşlenebildiği için eşitlik bozulmuş sayılır.
        """
        src = Counter(self._PLACEHOLDER_RE.findall(original))
        out = Counter(self._PLACEHOLDER_RE.findall(translated))
        lost = src - out
        extra = out - src
        odd_src = Counter(t for t in self._PLACEHOLDER_LIKE_RE.findall(original)
                          if not self._PLACEHOLDER_RE.fullmatch(t))
        odd_out = Counter(t for t in self._PLACEHOLDER_LIKE_RE.findall(translated)
                          if not self._PLACEHOLDER_RE.fullmatch(t))
        extra.update(odd_out - odd_src)
        return lost, extra

    def _missing_placeholders(self, translated: str, original: str) -> set:
        """Yer tutucu küme eşitliği bozulduysa sorunlu token'lar (kayıp ∪ fazla).

        Eşitse boş küme. Çağıranlar bunu "segment bozuk mu" diye kullanır
        (2026-09-24: eskiden yalnız KAYBI görüyordu; fazladan/kopya id geçiyordu).
        """
        lost, extra = self._placeholder_mismatch(translated, original)
        return set(lost) | set(extra)

    def _segment_revert_count(self) -> int:
        """Çevirinin orijinaline döndürüldüğü olay sayacı (2026-09-24).

        Yer tutucu bekçisi (kayıp + fazla/kopya) ve onarılamayan
        [[UNIT/FORMULA]] kalıntısı (remnant revert). Çağıran bir çeviri
        öncesi/sonrası farkına bakar: artış = bu çeviri sırasında bir segment
        ya da cümle (İngilizce kalıntı yeniden denemesi dahil) orijinaline
        döndü, satırın bir kısmı İngilizce kaldı → bütün gönderilen parça
        kurtarma sırasına girer (G2a-1: parçalı yol yok). Batched ve sıralı
        yolun ortak ölçütü. G2a-1: iskelet ve sayı/ilişki simgesi geri
        almaları da sayılır.
        """
        return sum(self._revert_snapshot())

    # _revert_snapshot sırası ve her sayacın kurtarma nedeni (G2a-1).
    _REVERT_FIELDS = (
        ("placeholders_lost", "yer tutucu kaybı"),
        ("placeholders_extra", "fazladan yer tutucu"),
        ("remnant_reverts", "onarılamayan kalıntı"),
        ("skeleton_reverts", "LaTeX iskeleti"),
        ("number_reverts", "sayı / ilişki simgesi"),
    )

    # ------------------------------------------------------------------
    # FX3 (2026-10-05, V2 Y1): onaylı bellek yazımı
    # ------------------------------------------------------------------
    def _settle_memory(self, sent: str, accepted: bool) -> None:
        """Çekirdeğe giden *sent* metninin bekleyen bellek kaydını
        kesinleştir: bekçiler kabul ettiyse yazılır, reddettiyse bekleyen
        düşer ve bellekteki kayıt silinir (eski koşudan kalan bozuk kayıt
        dahil). Çekirdek bu arayüzü sunmuyorsa (sahte çevirmen) sessizce
        geçer."""
        translator = getattr(self, "_translator", None)
        if translator is None:
            return
        fn = getattr(translator, "memory_commit" if accepted else "memory_reject",
                     None)
        if not callable(fn):
            return
        try:
            fn(sent)
        except Exception as exc:  # bellek hatası çeviriyi durdurmaz
            logger.debug("Bellek kaydı kesinleştirilemedi: %s", exc)

    # ------------------------------------------------------------------
    # FX3 (2026-10-05, V2 Y3): cevapsız parça (düşünme payı doldu)
    # ------------------------------------------------------------------
    def _core_no_answer_count(self) -> int:
        """Çekirdeğin cevapsız istek olay sayacı (sahte çevirmende 0)."""
        translator = getattr(self, "_translator", None)
        getter = getattr(translator, "no_answer_count", None)
        if not callable(getter):
            return 0
        try:
            value = getter()
        except Exception:
            return 0
        return value if isinstance(value, int) else 0

    def _core_no_answer_for(self, sent: str) -> bool:
        """Çekirdeğe giden *sent* metni cevapsız mı kaldı?"""
        translator = getattr(self, "_translator", None)
        getter = getattr(translator, "no_answer_for", None)
        if not callable(getter):
            return False
        try:
            return getter(sent) is True
        except Exception:
            return False

    def _note_no_answer(self, text: str) -> None:
        """Bu katmandaki *text* parçasının denemesinde cevapsız istek oldu."""
        seen = getattr(self, "_no_answer_texts", None)
        if not isinstance(seen, set):
            seen = self._no_answer_texts = set()
        seen.add(text)

    def _had_no_answer(self, text: str) -> bool:
        seen = getattr(self, "_no_answer_texts", None)
        return isinstance(seen, set) and text in seen

    def _keep_no_answer(self, text: str, partial: bool = False) -> None:
        """Cevapsız parçayı işaretle (sayaç + kept_source_list + UYARI)."""
        self._bump_stat("no_answer_segments")
        self._keep_source(text, NO_ANSWER_REASON, partial=partial)

    # ------------------------------------------------------------------
    # FX3 (2026-10-05, V2 Y6): "model kullanılamıyor" iletisi bir kez
    # ------------------------------------------------------------------
    def _log_mt_exception(self, level: int, fmt: str, *args,
                          exc: BaseException, exc_info: bool = False) -> None:
        """MT hatasını günlüğe yaz. Arka uç kullanılamıyorsa (devre açık:
        düşünme kesicisi, sunucu koptu — ``BackendUnavailable``) aynı neden
        koşuda BİR KEZ tam yazılır; sonraki tekrarlar DEBUG'a iner ve
        sayılır (V2 c4: aynı ileti 58 kez). Öteki hatalar eskisi gibi."""
        from core.mt_engines.contracts import BackendUnavailable

        if isinstance(exc, BackendUnavailable):
            seen = getattr(self, "_backend_down_logged", None)
            if not isinstance(seen, set):
                seen = self._backend_down_logged = set()
            key = str(exc)
            if key in seen:
                self._backend_down_suppressed = (
                    getattr(self, "_backend_down_suppressed", 0) + 1)
                logger.debug(fmt, *args)
                return
            seen.add(key)
            logger.log(level, fmt, *args)
            return
        logger.log(level, fmt, *args, exc_info=exc_info)

    @contextmanager
    def _memory_bypassed(self):
        """Kurtarma denemelerinde çekirdek belleği OKUMAZ (tekrar gerçekten
        modele gider; bozuk kayıt aynı çıktıyı geri vermez)."""
        translator = getattr(self, "_translator", None)
        factory = getattr(translator, "memory_bypass", None) if translator else None
        if not callable(factory):
            yield
            return
        with factory():
            yield

    def _revert_snapshot(self) -> Tuple[int, ...]:
        """Geri alma sayaçlarının anlık değerleri (``_REVERT_FIELDS`` sırası)."""
        stats = getattr(self, "stats", None)
        return tuple(getattr(stats, name, 0) for name, _ in self._REVERT_FIELDS)

    def _revert_reason(self, before: Tuple[int, ...]) -> Optional[str]:
        """*before* anlık değerinden bu yana artan geri alma sayaçlarının adı
        (virgülle); artış yoksa None."""
        now = self._revert_snapshot()
        names = [label for (_, label), a, b in zip(self._REVERT_FIELDS, before, now)
                 if b > a]
        return ", ".join(names) if names else None

    # ------------------------------------------------------------------
    # G2a-1 (2026-10-05): bozuk çıktı bekçileri ve kurtarma sırası
    # ------------------------------------------------------------------

    @staticmethod
    def _structure_problem(translated: str, original: str) -> Optional[Tuple[str, str]]:
        """Çevirinin yer tutucu DIŞINDAKİ yapısal sorunu: ``(tür, ayrıntı)``.

        Tür ``"iskelet"`` (:func:`latex_skeleton_problem`), ``"simge"``
        (kaynakta olmayan ilişki simgesi) ya da ``"sayı"`` (kaynaktaki 2+
        haneli sayı kayıp). Sorun yoksa None. Yer tutucu kümesi
        :meth:`_placeholder_mismatch` ile ayrıca denetlenir.
        """
        detail = latex_skeleton_problem(original, translated)
        if detail is not None:
            return "iskelet", detail
        rel = new_relation_symbols(original, translated)
        if rel:
            return "simge", "kaynakta olmayan ilişki simgesi %s" % dict(rel)
        lost = missing_numbers(original, translated)
        if lost:
            return "sayı", "kayıp sayı %s" % sorted(lost)
        return None

    def _output_problem(self, translated: str, original: str) -> Optional[str]:
        """Kurtarma denemesinin sonucu kabul edilebilir mi? Sorun varsa kısa
        neden, yoksa None (yer tutucu kümesi + yapısal denetim; sayaç YOK)."""
        if not translated or not translated.strip():
            return "boş çıktı"
        if self._missing_placeholders(translated, original):
            return "yer tutucu kümesi"
        problem = self._structure_problem(translated, original)
        return problem[1] if problem else None

    def _simplified_form(self, text: str):
        """Kurtarmanın ikinci denemesi için sade biçim (mt okuma MT-1 önerisi).

        * Kısa değişken yer tutucuları (``$m_1$``, ``$\\mu$``, ``\\textit{λ}``;
          bkz. :func:`plain_variable`) düz metin olur — model daha az yer
          tutucu taşır. Düz biçimi satırın metninde zaten geçen ya da farklı
          içerikli iki yer tutucunun paylaştığı değişken sadeleşmez.
        * Ham LaTeX komut kabukları yer tutucuya alınır (metin korumalı
          çeviri, :func:`protect_latex_shells`; mt okuma MT-17).

        Döner: ``(gönderilecek_metin, geri_kur)`` — ``geri_kur(çeviri)``
        tutarsızlıkta None verir; değişecek bir şey yoksa None.
        """
        stash = getattr(self, "_stash_list", None) or []
        found = list(re.finditer(r"<<LATEX_(\d+)>>", text))
        plain_of: Dict[str, str] = {}
        content_of: Dict[str, str] = {}
        rejected: set = set()
        for m in found:
            idx = int(m.group(1))
            if idx >= len(stash) or not isinstance(stash[idx], str):
                continue
            plain = plain_variable(stash[idx])
            if plain is None:
                continue
            if content_of.setdefault(plain, stash[idx]) != stash[idx]:
                rejected.add(plain)
            plain_of[m.group(0)] = plain
        outside = _without_placeholders(text)
        for plain in set(plain_of.values()):
            if _plain_token_re(plain).search(outside):
                rejected.add(plain)
        var_map: Dict[str, List[str]] = {}
        pieces: List[str] = []
        pos = 0
        for m in found:
            plain = plain_of.get(m.group(0))
            if plain is None or plain in rejected:
                continue
            pieces.append(text[pos:m.start()])
            pieces.append(plain)
            var_map.setdefault(plain, []).append(m.group(0))
            pos = m.end()
        pieces.append(text[pos:])
        simple = re.sub(r"[ \t]{2,}", " ", "".join(pieces)) if var_map else text
        shelled = protect_latex_shells(simple)
        if not var_map and shelled is None:
            return None
        sent, shells = shelled if shelled is not None else (simple, None)

        def _restore(translated: str) -> Optional[str]:
            out = translated
            if var_map:
                out = unsimplify_variables(out, var_map)
                if out is None:
                    return None
            if shells is not None:
                out = restore_latex_shells(out, sent, shells)
            return out

        return sent, _restore

    def _attempt_translation(self, text: str, deadline: Optional[float]):
        """Kurtarma denemesi: tekli yol, kendi içinde kurtarma YOK.

        Döner ``(sonuç, tuttu, geri_alındı)``; süre bütçesi bittiyse MT'ye
        gidilmez (``(text, False, False)``). ``_safe_translate`` üzerinden
        GEÇMEZ (yerine konmuş sahte/gözcü ``_safe_translate`` kurtarmayı
        bozmaz)."""
        timeout = None
        if deadline is not None:
            timeout = deadline - _time.monotonic()
            if timeout <= 0:
                return text, False, False
        before = self._revert_snapshot()
        result, _ = self._safe_translate_once(text, timeout)
        reverted = self._revert_snapshot() != before
        ok = (not reverted
              and " ".join(result.split()) != " ".join(text.split())
              and self._output_problem(result, text) is None)
        return result, ok, reverted

    def _usable_partial(self, candidate: Optional[str], text: str) -> bool:
        """Uzun parçanın kısmi sonucu (yalnız tutmayan cümlesi kaynak dilde)
        basılabilir mi: metinden (boşluk farkı dışında) farklı ve yer tutucu
        kümesi/yapısı sağlam."""
        if not candidate or not candidate.strip():
            return False
        if " ".join(candidate.split()) == " ".join(text.split()):
            return False
        return self._output_problem(candidate, text) is None

    def _recover_segment(self, text: str, *, partial: Optional[str] = None,
                         deadline: Optional[float] = None,
                         reason: Optional[str] = None,
                         whole_line: bool = False,
                         keep_source: bool = True) -> Optional[str]:
        """Bütün çevirisi tutmayan parçayı kurtar ya da özgün dilinde bırak.

        G2a-1 (2026-10-05, mt okuma MT-1): eski "parçalı yol" satırı
        ``<<LATEX_N>>`` sınırlarından bölüp parçaları bağlamsız çeviriyordu
        (bir model denemesinde: "$a$ ve $b$ Sırasıyla. …’nin değeri. … $a$ is").
        Yeni sıra:

        1. aynı metin bir kez daha (tekli yol; ilk deneme toplu yoldaysa
           farklı kod çözme yolu — greedy çözmede aynı girdinin tekli tekrarı
           çoğunlukla aynı çıktıyı verir);
        2. sade biçim (:meth:`_simplified_form`): kısa değişkenler düz metin,
           ham komut kabukları yer tutucu (metin korumalı çeviri);
        3. hiçbiri tutmazsa geçerli kısmi sonuç (uzun parçada yalnız tutmayan
           cümle kaynak dilde) ya da özgün metin — işaretlenir
           (:meth:`_keep_source`; QC kaynak dilde kalan satırı ayrıca raporlar).

        Bozuk birleştirme BASILMAZ. *deadline* (``time.monotonic``) verilirse
        denemeler kalan süreyi paylaşır. *whole_line*: gövde/altyazı satırı
        (``placeholder_split_fallbacks`` yalnız bunlarda sayılır).
        *keep_source* False (G2a-2, bağlam grubu): 1–2 tutmazsa 3. adım
        yapılmaz, None döner (çağıran parçaları ayrı çevirir).
        """
        # FX3 (2026-10-05, V2 Y3): model bu parçada düşünme payı dolup cevap
        # üretemedi (bekçi TEK yeniden denemeyi zaten yaptı, KARAR 22).
        # Kurtarma zinciri koşmaz: tekrar bekçinin önbelleğinden yine
        # cevapsız döner, sade biçim / cümle düzeyi ise aynı içeriğin
        # TÜREVİNİ modele yeniden gönderiyordu (V2 c3: 6 parça -> 11 cevapsız
        # öğe, her biri iki deneme). Parça (kısmi sonuç geçerliyse o) özgün
        # dilde kalır ve işaretlenir.
        if self._had_no_answer(text):
            if not keep_source:
                return None
            if self._usable_partial(partial, text):
                self._keep_no_answer(text, partial=True)
                return partial
            self._keep_no_answer(text)
            return text
        # FX3 (V2 Y6): arka uç kullanılamıyor (devre açık): denemeler ağa
        # gitmeden düşer, işaret nedeni "yankı" yerine gerçek neden olur.
        if self.backend_health():
            if not keep_source:
                return None
            if self._usable_partial(partial, text):
                self._keep_source(text, BACKEND_DOWN_REASON, partial=True)
                return partial
            self._keep_source(text, BACKEND_DOWN_REASON)
            return text
        if whole_line and "<<LATEX_" in text:
            self._bump_stat("placeholder_split_fallbacks")
        logger.info("Bütün çeviri tutmadı (%s); kurtarma sırası: %.60s",
                    reason or "yankı", text)
        # FX3 (V2 Y1): kurtarma denemeleri belleği okumaz.
        with self._memory_bypassed():
            return self._recover_segment_attempts(
                text, partial=partial, deadline=deadline, reason=reason,
                keep_source=keep_source)

    def _recover_segment_attempts(self, text: str, *, partial: Optional[str],
                                  deadline: Optional[float],
                                  reason: Optional[str],
                                  keep_source: bool) -> Optional[str]:
        """:meth:`_recover_segment`'in deneme sırası (bellek atlanırken)."""
        best = partial if self._usable_partial(partial, text) else None
        result, ok, _ = self._attempt_translation(text, deadline)
        if ok:
            self._bump_stat("recovered_by_retry")
            logger.info("Kurtarma: tekrar denemede tuttu: %.60s", text)
            return result
        if best is None and self._usable_partial(result, text):
            best = result
        form = self._simplified_form(text)
        if form is not None:
            sent, restore = form
            out, ok, _ = self._attempt_translation(sent, deadline)
            if ok:
                final = restore(out)
                if final is not None and self._output_problem_relaxed(final, text) is None:
                    self._bump_stat("recovered_by_simplified")
                    logger.info("Kurtarma: sade biçimde tuttu: %.60s", text)
                    return final
        if not keep_source:
            # G2a-2: çağıranın kendi geri dönüşü var (bağlam grubu → parçalar
            # ayrı çevrilir); kısmi sonuç da kabul edilmez, işaret konmaz.
            return None
        if best is not None:
            self._keep_source(text, reason, partial=True)
            return best
        self._keep_source(text, reason)
        return text

    def _output_problem_relaxed(self, translated: str, original: str) -> Optional[str]:
        """Sade biçim sonucunun denetimi: yer tutucu kümesi, ilişki simgesi,
        sayı. Komut sırası denetlenmez: kabuk çiftlerinin sırası ve iç içeliği
        :func:`restore_latex_shells`'te zaten doğrulandı; Türkçe söz dizimi
        iki kabuğun yerini meşru olarak değiştirebilir."""
        if not translated or not translated.strip():
            return "boş çıktı"
        if self._missing_placeholders(translated, original):
            return "yer tutucu kümesi"
        if new_relation_symbols(original, translated):
            return "ilişki simgesi"
        if missing_numbers(original, translated):
            return "sayı"
        return None

    def _keep_source(self, text: str, reason: Optional[str],
                     partial: bool = False) -> None:
        """Parçayı (ya da uzun parçanın tutmayan cümlelerini) özgün dilinde
        bırakıldı diye işaretle: sayaç, işaret kaydı, UYARI günlüğü."""
        self._bump_stat("kept_source_segments")
        stats = getattr(self, "stats", None)
        if stats is not None:
            items = getattr(stats, "kept_source_list", None)
            if isinstance(items, list):
                # Önizleme okunur olsun: yer tutucular stash içeriğiyle açılır.
                stash = getattr(self, "_stash_list", None) or []
                preview = re.sub(
                    r"<<LATEX_(\d+)>>",
                    lambda m: (stash[int(m.group(1))]
                               if int(m.group(1)) < len(stash)
                               and isinstance(stash[int(m.group(1))], str)
                               else m.group(0)),
                    text)
                items.append({"neden": reason or "yankı", "kismi": partial,
                              "kaynak": preview[:KEPT_SOURCE_PREVIEW_CHARS]})
        logger.warning(
            "Çeviri tutmadı (%s): %s özgün dilinde bırakıldı (işaretli; QC "
            "çevrilmemiş satır olarak görür): %.80s", reason or "yankı",
            "parçanın tutmayan cümlesi" if partial else "parça", text)

    def _sweep_prompt_markers(self, text, where: str,
                              source: Optional[str] = None):
        """MT çıktısındaki istem işaretlerini temizle; sızıntıyı günlüğe yaz
        ve say (G2a-2). Metin değilse (None vb.) aynen döner. *source*
        (FX3, V2 Y2): kaynakta geçen işaret meşru içeriktir, korunur."""
        if not isinstance(text, str):
            return text
        cleaned, found = strip_prompt_markers(text, source)
        if found:
            self._bump_stat("prompt_marker_leaks")
            logger.warning(
                "İstem işareti sızıntısı temizlendi (%s, %d işaret: %s): %.80s",
                where, len(found), ", ".join(found[:4]), cleaned)
        return cleaned

    # Regex to find placeholders. BATCH-11 fix: hem <<LATEX_N>> hem <<CMD_N>>
    # yakalar — eskiden yalnız LATEX yakalıyordu, bu yüzden _translate_by_sentences
    # CMD-stashed metinde MT'nin yuttuğu \newpage/\centering gibi komutları
    # tespit edemiyordu (yollar arası tutarsız kapsam).
    _PLACEHOLDER_RE = re.compile(r"<<(?:LATEX|CMD)_\d+>>")
    # Yer tutucuya BENZEYEN her token (bozuk biçimler dahil): talimat
    # örneğinin yankısı "<<LATEX_N>>", boşluklu "<< LATEX_3 >>" vb.
    # _placeholder_mismatch tam biçim dışındakileri fazlalık sayar.
    _PLACEHOLDER_LIKE_RE = re.compile(r"<<\s*(?:LATEX|CMD)[^<>\n]{0,16}>>")

    # Sentence splitting regex — split on period/question/exclamation
    # followed by whitespace, but NOT inside common abbreviations or numbers.
    # Negative lookbehind excludes Fig., Eq., Ref., etc., e.g., i.e., vs., et al.
    # 2026-09-29 (F3-11 / K5-3): kısaltma bakışları NOKTA DAHİL yazılır.
    # Bölme noktası noktadan SONRAKİ boşluktur; eski '(?<!Fig)' biçimi o
    # konumda son karakteri '.' olan metne bakıp hiç eşleşmiyordu —
    # "Dr. Smith", "Fig. A", "e.g. Newton" cümle ortasından bölünüyor, model
    # 'Prof.' gibi parçaları bağlamsız çeviriyordu (_build_source_sentence_
    # split_re ile aynı biçim). Liste AYNI; yalnız bakışlar etkili oldu.
    _SENTENCE_SPLIT_RE = re.compile(
        r"(?<!\bFig\.)(?<!\bFigs\.)(?<!\bEq\.)(?<!\bEqs\.)(?<!\bRef\.)"
        r"(?<!\bRefs\.)(?<!\bet al\.)(?<!\bvs\.)(?<!\betc\.)(?<!\be\.g\.)"
        r"(?<!\bi\.e\.)(?<!\bProf\.)(?<!\bDr\.)(?<!\bMr\.)(?<!\bMrs\.)"
        r"(?<!\bapprox\.)"
        # Turkish abbreviations: vb. (ve benzeri), bkz. (bakınız),
        # örn. (örneğin), yy. (yüzyıl), Doç. (Doçent), Sn. (Sayın),
        # Av. (Avukat), Sok. (Sokak), Cad. (Cadde).
        r"(?<!\bvb\.)(?<!\bbkz\.)(?<!\börn\.)(?<!\byy\.)(?<!\bDo[çc]\.)"
        r"(?<!\bSn\.)(?<!\bAv\.)(?<!\bSok\.)(?<!\bCad\.)"
        r"(?<=[.?!])\s+(?=[A-ZÇĞİÖŞÜ\"'\(\\])"
    )

    # Max chars for a single MT call.  Longer segments are split into
    # sentences for better translation quality.
    # 2000 char ~ 500 token, HY-MT 7B 256K context window rahat halleder.
    # 500->2000 degisikligi: 500 cok dusuktu, uzun paragraflar cumle bolme
    # basarisiz olunca cevrilmeden geciyordu (%70 cevrilmemis segment sorunu).
    # Artık config.max_segment_chars üzerinden okunuyor; dış kod hâlâ
    # sınıf sabitine bakıyor olabilir diye geriye dönük uyumluluk için
    # MAX_SEGMENT_LENGTH modül sabitiyle aynı değer tutuluyor.
    _MAX_SEGMENT_CHARS = MAX_SEGMENT_LENGTH  # DEPRECATED: use self.pipeline_config.max_segment_chars

    # ------------------------------------------------------------------
    # Pre-translation LaTeX command stash/restore
    # ------------------------------------------------------------------
    # Catches LaTeX structural commands that may appear inline within
    # text segments (e.g. after \item or mixed with prose).  The Phase 1
    # _stash_protected() handles whole-line matches, but commands that
    # are embedded mid-sentence need per-segment protection.
    #
    # Pattern: standalone commands (no translatable content) that MT
    # should never see.  Uses <<CMD_N>> placeholders (distinct from
    # <<LATEX_N>> to avoid index collision).
    _INLINE_CMD_STASH_RE = re.compile(
        r"(?:"
        # Commands with braced arguments
        r"\\(?:thispagestyle|pagestyle)\{[^}]*\}"
        r"|\\setcounter\{[^}]*\}\{[^}]*\}"
        r"|\\rule\{[^}]*\}\{[^}]*\}"
        r"|\\(?:vspace|hspace)\*?\{[^}]*\}"
        # Bare commands (word boundary to avoid partial match)
        r"|\\(?:centering|noindent|raggedright|raggedleft"
        r"|vfill|hfill|newpage|clearpage|cleardoublepage"
        r"|frontmatter|mainmatter|backmatter|appendix"
        r"|maketitle|tableofcontents|listoffigures|listoftables"
        r"|bigskip|medskip|smallskip"
        # 2026-10-06 (D kulvarı): düzyazı kod çitinin alıntı biçimi
        # (kod_vurgu / sanitizer.listing_to_quote) "\\raggedright\\ttfamily"
        # ile başlar; yazı tipi bildirimi modele gitmez.
        r"|ttfamily)\b"
        # Environment begin/end that may leak into segments
        # 2026-10-06 (D kulvarı): quote/quotation da (ortam adı modele
        # gidiyordu; model "alıntı" yazarsa ortam bozulur).
        r"|\\(?:begin|end)\{(?:titlepage|center|flushleft|flushright"
        r"|quote|quotation)\}"
        # E (2026-10-06): kaynak PDF kutusu (bkz. _LIST_ENV_RE)
        r"|\\(?:begin|end)\{" + _KUTU_ORTAMI + r"\}"
        r")"
    )

    @classmethod
    def _stash_inline_commands(cls, text: str):
        """Stash LaTeX structural commands in *text* before MT.

        Returns (stashed_text, cmd_list) where cmd_list[i] is the
        original command string for ``<<CMD_i>>``.
        """
        cmds = []

        def _repl(m):
            idx = len(cmds)
            cmds.append(m.group(0))
            return f"<<CMD_{idx}>>"

        result = cls._INLINE_CMD_STASH_RE.sub(_repl, text)
        return result, cmds

    @staticmethod
    def _restore_inline_commands(text: str, cmds: list) -> str:
        """Restore ``<<CMD_N>>`` placeholders back to original commands."""
        if not cmds:
            return text
        for idx, cmd in enumerate(cmds):
            placeholder = f"<<CMD_{idx}>>"
            text = text.replace(placeholder, cmd)
        # Catch mangled placeholders (MT may add spaces)
        text = re.sub(
            r"<\s*<\s*CMD[_\s]*(\d+)\s*>\s*>",
            lambda m: cmds[int(m.group(1))] if int(m.group(1)) < len(cmds) else m.group(0),
            text,
        )
        return text

    def _safe_translate(self, text: str, timeout: Optional[float] = None) -> str:
        """Translate *text* via BookTranslator with timeout protection.

        Falls back to the original text if translation times out or fails.
        Timeout is read from ``pipeline_config.segment_timeout`` (default 60s);
        *timeout* verilirse o kullanılır (düşüş parçalarının kalan bütçesi).

        Pre-translation: inline LaTeX structural commands are stashed to
        ``<<CMD_N>>`` placeholders so MT never sees them.
        Post-translation: placeholders are restored to original commands.

        G2a-1 (2026-10-05): çeviri bir bekçiye takılıp orijinaline döndüyse
        (yer tutucu, LaTeX iskeleti, sayı / ilişki simgesi, kalıntı) ya da
        yer tutuculu metin yankılandıysa :meth:`_recover_segment` sırası
        işler: aynı metin bir kez daha → sade biçim → özgün dilde bırak +
        işaret. Toplam süre bütçesi *timeout*'tur, kurtarma kalanı paylaşır.

        G2a-2 (2026-10-05): girdideki istem işaretleri (autofix'in bağlam
        işareti <<<TRANSLATE_START>>> …) modele gönderilmeden ayıklanır;
        çıktı son kez işaret temizliğinden geçer (sızıntı günlüklenir).

        FX3 (2026-10-05, V2 Y2): girdide YALNIZ autofix'in protokol işareti
        ele alınır; kaynaktaki meşru "<translate>" biçimi modele aynen gider
        ve çıktıda korunur (kayıpsızlık). Protokol (tek START…END çifti)
        varsa yalnız işaretli hedef çevrilir ve işaretlerin arasına konur:
        model işareti zaten koruyamıyordu (G2a-2 ayıklıyordu), autofix'in
        bağlamlı yeniden çevirisi hedefi hiç çıkaramıyor, bağlam + hedef
        çevirisi boşa gidiyordu (bir model denemesinde 5 blokta "Could not extract
        target", ~4 dk). Bağlam satırları modele gitmez, aynen döner.
        """
        parts = _split_protocol_target(text)
        if parts is not None:
            before, target, after = parts
            logger.debug("Bağlam işaretli istek: yalnız hedef çevriliyor "
                         "(%d karakter).", len(target))
            out = self._translate_with_recovery(target, timeout)
            out = self._sweep_prompt_markers(out, "çıktı", source=target)
            return (before + _PROTOCOL_START + out + _PROTOCOL_END + after)
        clean_in = _PROTOCOL_MARKER_RE.sub("", text)
        if clean_in != text:
            logger.debug("Eşlenmemiş protokol işareti modele gönderilmedi.")
        out = self._translate_with_recovery(clean_in, timeout)
        return self._sweep_prompt_markers(out, "çıktı", source=clean_in)

    def _translate_with_recovery(self, text: str, timeout: Optional[float] = None,
                                 *, whole_line: bool = False) -> str:
        """Tek deneme (:meth:`_safe_translate_once`) + tutmazsa kurtarma sırası.

        *whole_line*: metin bir gövde/altyazı SATIRI ise bütün çevirisi
        tutmadığında ``placeholder_split_fallbacks`` sayılır (eski sayaç
        anlamı korunur; dipnot / biçim / hücre parçaları sayılmaz).
        """
        if timeout is None:
            timeout = getattr(getattr(self, "pipeline_config", None),
                              "segment_timeout", SEGMENT_TIMEOUT_S)
        started = _time.monotonic()
        reverts_before = self._revert_snapshot()
        na_before = self._core_no_answer_count()
        result, called = self._safe_translate_once(text, timeout)
        if not called:
            return result
        # FX3 (V2 Y3): denemede cevapsız istek olduysa parça işaretlenir.
        no_answer = self._core_no_answer_count() != na_before
        if no_answer:
            self._note_no_answer(text)
        reason = self._revert_reason(reverts_before)
        if reason is None and not is_failed_echo(result, text):
            if no_answer:
                # Yarı çevrilmiş (cevapsız cümle özgün dilde) ya da yer
                # tutucusuz yankı: "tuttu" DEĞİL, işaretlenir.
                self._keep_no_answer(
                    text, partial=(" ".join(result.split())
                                   != " ".join(text.split())))
            return result
        return self._recover_segment(text, partial=result,
                                     deadline=started + timeout, reason=reason,
                                     whole_line=whole_line)

    def _safe_translate_once(self, text: str,
                             timeout: Optional[float] = None) -> Tuple[str, bool]:
        """``_safe_translate``'in kurtarmasız tek denemesi (G2a-1 ayrımı).

        Döner ``(sonuç, mt_denendi)``; kısa / yalnız yer tutuculu / kaynak
        dili dışı metinde MT'ye gidilmez (``mt_denendi`` False).
        """
        if not text or len(text.strip()) < 3:
            self.stats.segments_skipped += 1
            return text, False

        # Pre-translation: stash inline LaTeX commands
        text, inline_cmds = self._stash_inline_commands(text)

        # If the text is now only placeholders, skip MT
        cleaned_for_check = re.sub(r"<<(?:LATEX|CMD)_\d+>>", "", text).strip()
        if len(cleaned_for_check) < 3:
            self.stats.segments_skipped += 1
            return self._restore_inline_commands(text, inline_cmds), False

        # 2026-09-24: İngilizce olmayan kaynak segment (Latince alıntı vb.)
        # çevrilmez. Bu yol retranslator/autofix/hücre/dipnot çağrılarının
        # ortak kapısıdır.
        if self._skip_foreign_segment(text):
            return self._restore_inline_commands(text, inline_cmds), False

        self.stats.segments_total += 1

        if timeout is None:
            timeout = getattr(self.pipeline_config, 'segment_timeout', SEGMENT_TIMEOUT_S)

        result_holder = [text]  # default = original
        exc_holder = [None]

        def _do_translate():
            try:
                # For long segments, split into sentences
                clean_text = re.sub(r"<<(?:LATEX|CMD)_\d+>>", "", text).strip()
                max_segment_chars = getattr(
                    self.pipeline_config, "max_segment_chars", self._MAX_SEGMENT_CHARS
                )
                if len(clean_text) > max_segment_chars:
                    r = self._translate_by_sentences(text)
                    if r and r != text:
                        result_holder[0] = r.strip()
                        return

                # Short segment: translate as a single chunk
                result_holder[0] = self._translate_single_chunk(text)
            except Exception as e:
                exc_holder[0] = e

        t = threading.Thread(target=_do_translate, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            logger.warning(
                "Segment timeout (%ds): attempting hard-split retry (%d chars)",
                timeout, len(text),
            )
            # Retry: split into smaller chunks and translate each
            result = self._retry_with_hard_split(text)
            if result == text:
                self.stats.segments_failed += 1
        elif exc_holder[0]:
            self._log_mt_exception(
                logging.WARNING,
                "Segment translation failed: %s -- attempting hard-split retry",
                exc_holder[0], exc=exc_holder[0],
            )
            # Retry: split into smaller chunks and translate each
            result = self._retry_with_hard_split(text)
            if result == text:
                self.stats.segments_failed += 1
        else:
            result = result_holder[0]

        # Post-translation: restore inline LaTeX commands
        result = self._restore_inline_commands(result, inline_cmds)
        return result, True

    def _safe_translate_batch(self, texts: List[str], *,
                              whole_line: bool = False) -> List[str]:
        """``_safe_translate``'in TOPLU esdegeri (F18, 2026-07-28).

        Oge basina sozlesme tekli yolla birebir: <3 karakter ve
        yalniz-placeholder ogeler atlanir, inline komutlar <<CMD_N>>'e
        stash'lenir, uzun ogeler mevcut tekli yola (cumle bolme +
        hard-split retry) dusurulur. Kisa ogelerin MT'si ``_batch_mt_call``
        uzerinden TOPLU yapilir — timeout korumasi, alt-batch bolme ve
        sirali fallback orada. MT sonrasi zincir
        (``_post_process_translation`` -> ``_apply_glossary`` ->
        ``_fix_english_remnants`` -> ``_revert_on_placeholder_loss``)
        ``_translate_single_chunk`` ile ayni sirayla oge bazinda uygulanir.

        0 veya 1 batchlenebilir oge varsa batch kazanci yok — mevcut
        tekli yol aynen kullanilir.
        """
        n = len(texts)
        results: List[Optional[str]] = [None] * n
        batch_indices: List[int] = []
        batch_stashed: List[str] = []
        batch_cmds: List[list] = []

        max_segment_chars = getattr(
            getattr(self, "pipeline_config", None),
            "max_segment_chars", self._MAX_SEGMENT_CHARS,
        )

        for i, text in enumerate(texts):
            if not text or len(text.strip()) < 3:
                self.stats.segments_skipped += 1
                results[i] = text
                continue
            stashed, inline_cmds = self._stash_inline_commands(text)
            cleaned = re.sub(r"<<(?:LATEX|CMD)_\d+>>", "", stashed).strip()
            if len(cleaned) < 3:
                self.stats.segments_skipped += 1
                results[i] = self._restore_inline_commands(stashed, inline_cmds)
                continue
            # 2026-09-24: İngilizce olmayan kaynak segment çevrilmez (tekli yolla aynı).
            if self._skip_foreign_segment(stashed):
                results[i] = text
                continue
            if len(cleaned) > max_segment_chars:
                # Uzun oge: cumle-bolmeli tekli yol aynen korunur.
                results[i] = self._translate_with_recovery(
                    text, whole_line=whole_line)
                continue
            batch_indices.append(i)
            batch_stashed.append(stashed)
            batch_cmds.append(inline_cmds)

        if len(batch_indices) == 1:
            # Tek oge: tekli yol korunur (istatistikler dahil).
            i = batch_indices[0]
            results[i] = self._translate_with_recovery(
                texts[i], whole_line=whole_line)
            batch_indices = []

        if batch_indices:
            self.stats.segments_total += len(batch_indices)
            raw_results = self._batch_mt_call(batch_stashed)
            # G2a-1: bekçiye takılan / yer tutuculu yankı öğeleri; toplu
            # geçişten sonra tek tek kurtarma sırasına girer (parça yolu yok).
            to_recover: List[Tuple[int, Optional[str], str]] = []
            for j, raw in enumerate(raw_results):
                i = batch_indices[j]
                stashed = batch_stashed[j]
                before = self._revert_snapshot()
                # R1 (2026-07-29): echo (raw == stashed) 'çevrildi' SAYILMAZ —
                # ana gövde yoluyla aynı sözleşme. Core [MT_FAIL] önekini
                # sınırdan geçirmeden sildiği için başarısızlık buradan ancak
                # echo olarak görülebilir; aksi hâlde caption/dipnot/stash
                # ağırlıklı kitapta F3 fail-ratio kapısı iyimser sapıyordu.
                if raw and raw.strip() and raw != stashed:
                    self.stats.segments_translated += 1
                    r = self._post_process_translation(raw, stashed)
                    r = self._apply_glossary(r)
                    r = self._fix_english_remnants(r)
                    # Placeholder kaybinda segmenti orijinaliyle geri al.
                    r = self._revert_on_placeholder_loss(r, stashed)
                    r = r.strip()
                elif raw and raw.strip():
                    # Echo: sayı/sembol segmentinde doğru davranış, düz
                    # yazıda hata — ayrımı B-11 sayacı yapar.
                    self._count_identical_result(stashed)
                    r = stashed
                else:
                    logger.warning(
                        "Empty translation result for: %.60s", stashed,
                    )
                    self.stats.segments_failed += 1
                    r = stashed
                results[i] = self._restore_inline_commands(r, batch_cmds[j])
                reason = self._revert_reason(before)
                # FX3 (V2 Y1): çekirdeğe giden metnin (stashed) bellek kaydı
                # ancak bekçiler kabul edince yazılır.
                self._settle_memory(stashed, reason is None)
                # FX3 (V2 Y3): cevapsız öğe kurtarma zincirine girmez;
                # yer tutucusuz yankısı da (meşru yankı sayılıp) sessiz geçmez.
                no_answer = self._core_no_answer_for(stashed)
                if no_answer:
                    self._note_no_answer(texts[i])
                if reason is not None or is_failed_echo(results[i], texts[i]):
                    to_recover.append((i, reason, results[i]))
                elif no_answer:
                    self._keep_no_answer(
                        texts[i], partial=(" ".join(results[i].split())
                                           != " ".join(texts[i].split())))
            timeout = getattr(getattr(self, "pipeline_config", None),
                              "segment_timeout", SEGMENT_TIMEOUT_S)
            for i, reason, first in to_recover:
                results[i] = self._recover_segment(
                    texts[i], partial=first,
                    deadline=_time.monotonic() + timeout, reason=reason,
                    whole_line=whole_line)

        return [
            r if r is not None else texts[k]
            for k, r in enumerate(results)
        ]

    def _retry_with_hard_split(self, text: str) -> str:
        """Retry failed/timed-out segment by hard-splitting into smaller chunks.

        Each chunk is translated individually with a shorter timeout.
        If all chunks fail, returns the original text unchanged.
        """
        chunks = self._hard_split(text, max_chars=HARD_SPLIT_CHARS)
        if len(chunks) <= 1:
            # Can't split further -- nothing to retry
            return text

        logger.info(
            "Hard-split retry: %d-char segment -> %d chunks",
            len(text), len(chunks),
        )
        translated_chunks: List[str] = []
        any_success = False
        for chunk in chunks:
            chunk_stripped = chunk.strip()
            if not chunk_stripped or len(chunk_stripped) < 3:
                translated_chunks.append(chunk)
                continue
            try:
                # BATCH-12 fix: _translate_single_chunk zaten post_process +
                # glossary + remnants uyguluyor; dıştan tekrar uygulama SİLİNDİ
                # (çift işlem).
                tr = self._translate_single_chunk(chunk_stripped)
                if tr and tr.strip() and tr != chunk_stripped:
                    translated_chunks.append(tr.strip())
                    any_success = True
                else:
                    translated_chunks.append(chunk_stripped)
            except Exception as exc:
                self._log_mt_exception(logging.WARNING,
                                       "Hard-split chunk failed: %s", exc, exc=exc)
                translated_chunks.append(chunk_stripped)

        if any_success:
            result = " ".join(translated_chunks)
            result = self._fix_english_remnants(result)
            # BATCH-15 fix: iç katman (_translate_single_chunk) her chunk için
            # zaten segments_translated++ yapıyor; buradaki fazladan +1 SİLİNDİ
            # (segments_translated > segments_total olmasına yol açıyordu).
            return result
        return text

    def _translate_single_chunk(self, text: str, _depth: int = 0) -> str:
        """Translate a single chunk of text via MT.

        *_depth* tracks re-translation depth to prevent cascading MT calls.
        At depth > 0, mixed-language and remnant checks are skipped.
        """
        try:
            translator = self._get_translator()
            reverts_before = self._revert_snapshot()
            result = translator.translate_text(text)
            if self._core_no_answer_for(text):
                # FX3 (V2 Y3): model cevap üretemedi; yankıya sonradan işlem
                # (cümle düzeyi karışık dil denetimi, kalıntı tekrarı) aynı
                # içeriğin türevlerini modele yeniden gönderiyordu.
                self._settle_memory(text, False)
                return text
            if result and result.strip():
                self.stats.segments_translated += 1
                result = self._post_process_translation(
                    result, text, _skip_retranslation=(_depth > 0),
                )

                # Apply terminology glossary (fix wrong terms + EN->TR)
                result = self._apply_glossary(result)

                # Post-translation: fix remaining English remnants
                # (skip at depth > 0 to prevent cascade)
                if _depth == 0:
                    result = self._fix_english_remnants(result)

                # B-11 fix: placeholder kaybında sona-ekleme yerine segmenti
                # orijinaliyle geri al (tek yardımcı).
                result = self._revert_on_placeholder_loss(result, text)
                # FX3 (V2 Y1): bekçiler kabul ettiyse belleğe yazılır,
                # reddettiyse bekleyen düşer ve eski kayıt silinir.
                self._settle_memory(
                    text, self._revert_snapshot() == reverts_before)
                return result.strip()
            else:
                logger.warning("Empty translation result for: %.60s", text)
                self.stats.segments_failed += 1
                return text
        except Exception as exc:
            # Vk 2026-09-28: model kullanılamıyorsa (BackendUnavailable:
            # önbellekteki yükleme hatası / açık devre) ileti Türkçe ve
            # yeterli; segment başına traceback pipeline.log'u binlerce
            # özdeş yığın izine boğuyordu.
            # FX3 (V2 Y6): arka uç kullanılamıyorsa ileti koşuda bir kez.
            self._log_mt_exception(
                logging.ERROR, "Translation failed for segment (%.60s...): %s",
                text, exc, exc=exc, exc_info=True,
            )
            self.stats.segments_failed += 1
            return text  # keep original on failure

    @staticmethod
    def _hard_split(text: str, max_chars: int = HARD_SPLIT_CHARS) -> List[str]:
        """Split *text* into chunks of at most *max_chars* characters.

        Splits at the nearest space boundary to avoid breaking words.
        Used as a fallback when sentence splitting fails (e.g. returns
        only a single chunk for a long segment).
        """
        chunks: List[str] = []
        while len(text) > max_chars:
            # Find the last space within the first max_chars characters
            split_pos = text.rfind(' ', 0, max_chars)
            if split_pos <= 0:
                # No space found: force split at max_chars
                split_pos = max_chars
            # BATCH-06 fix: split_pos bir placeholder'ın ORTASINA düşerse
            # (özellikle boşluksuz metinde zorunlu split), placeholder'ı
            # ikiye bölme — tüm token'ı span sınırına it. Eski heuristik
            # yalnız ileri bakan bir pencereyle placeholder prefix'ini arıyor
            # ve boşluk yoksa geri çekilemiyordu; span-tabanlı düzeltme her
            # durumda çalışır.
            for m in _HARD_SPLIT_PH_RE.finditer(text):
                if m.start() < split_pos < m.end():
                    # Placeholder'dan önce boşlukta bölmeyi dene; yoksa
                    # placeholder'ı bütün hâlde bir sonraki parçaya bırak.
                    prev = text.rfind(' ', 0, m.start())
                    if prev > 0:
                        split_pos = prev
                    elif m.start() > 0:
                        split_pos = m.start()
                    else:
                        split_pos = m.end()
                    break
                if m.start() >= split_pos:
                    break  # placeholder'lar sıralı; ileri gitmeye gerek yok
            chunks.append(text[:split_pos].strip())
            text = text[split_pos:].strip()
        if text.strip():
            chunks.append(text.strip())
        return chunks

    def _translate_by_sentences(self, text: str) -> str:
        """Split *text* into sentences, translate each, and rejoin.

        This produces much better quality for long paragraphs because
        HY-MT 7B handles shorter segments more reliably.

        IMPORTANT: Placeholders (``<<LATEX_N>>``) are kept INSIDE the
        sentences they belong to so that MT sees the full syntactic
        context (e.g. ``(see <<LATEX_0>>)`` stays as one unit).
        Splitting around placeholders first would send fragments like
        ``"(see "`` to MT, causing it to close parentheses prematurely
        and drop the cross-reference number.

        Fallback: If sentence splitting produces only one chunk (i.e.
        the regex couldn't find sentence boundaries), use hard splitting
        at ~500 char boundaries to ensure the segment gets translated.
        """
        # Split into sentences, keeping placeholders inside their
        # surrounding sentence context.  The sentence-split regex
        # already has negative lookbehinds for Fig., Eq., etc.
        sentences = self._source_sentence_split_re().split(text)

        # Fallback: if sentence splitting didn't actually split
        # (single sentence >= pipeline_config.max_segment_chars), use hard split.
        if len(sentences) <= 1 and len(text) > HARD_SPLIT_CHARS:
            logger.debug(
                "Sentence split failed for %d-char segment, using hard split.",
                len(text),
            )
            sentences = self._hard_split(text, max_chars=HARD_SPLIT_CHARS)

        translated_sentences: List[str] = []
        for sent in sentences:
            sent_stripped = sent.strip()
            if not sent_stripped or len(sent_stripped) < 3:
                translated_sentences.append(sent)
                continue

            # If this sentence is only placeholders + punctuation,
            # skip MT entirely to avoid placeholder corruption.
            # BATCH-11 fix: _PLACEHOLDER_RE artık <<CMD_N>>'yi de yakalar,
            # böylece CMD-only cümle de (LATEX-only gibi) MT'ye gönderilmez.
            non_placeholder = self._PLACEHOLDER_RE.sub("", sent_stripped).strip()
            if len(non_placeholder) < 3:
                translated_sentences.append(sent_stripped)
                continue

            try:
                translator = self._get_translator()
                reverts_before = self._revert_snapshot()
                tr = translator.translate_text(sent_stripped)
                if self._core_no_answer_for(sent_stripped):
                    # FX3 (V2 Y3): cevapsız cümle özgün dilde kalır (çağıran
                    # paragrafı yarı çevrilmiş diye işaretler).
                    translated_sentences.append(sent_stripped)
                    continue
                if tr and tr.strip():
                    tr = self._post_process_translation(tr, sent_stripped)
                    tr = self._apply_glossary(tr)
                    # B-11 fix: placeholder kaybında sona-ekleme yerine cümleyi
                    # orijinaliyle geri al (tek yardımcı).
                    tr = self._revert_on_placeholder_loss(tr, sent_stripped)
                    # FX3 (V2 Y1): onaylı bellek yazımı.
                    self._settle_memory(
                        sent_stripped, self._revert_snapshot() == reverts_before)
                    translated_sentences.append(tr.strip())
                else:
                    translated_sentences.append(sent_stripped)
            except Exception as exc:
                self._log_mt_exception(logging.WARNING,
                                       "Sentence translation failed: %s", exc,
                                       exc=exc)
                translated_sentences.append(sent_stripped)

        result = " ".join(translated_sentences)
        result = self._fix_english_remnants(result)
        return result

    # C2 fix: _TECHNICAL_ENGLISH_TERMS ve _TURKISH_SUFFIXES (eski, yerel
    # dil-tespit mantığının verisi) SİLİNDİ — dil tespiti artık kanonik
    # retranslator.is_english_line'a delege ediliyor (tek kaynak).

    def _is_english_sentence(self, text: str) -> bool:
        """Check if *text* is mostly English (not Turkish).

        C2 fix: KANONIK dedektöre (retranslator.is_english_line) delege eder
        (arayüz sözleşmesi §4). Eskiden bu metodun kendi ~50 satırlık kopyası
        vardı (üç ayrı dil-tespit mantığından biri). Lazy import — circular
        import önleme. Eski 15-karakter alt eşiği min_length ile korunur.
        """
        try:
            from latex_pipeline.retranslator import is_untranslated_line
        except Exception:
            return False
        # 2026-09-28 (K5): cümle kaynak dilde (ya da İngilizce) kaldı mı.
        # G2a-2 (2026-10-05): İngilizce kaynakta da is_untranslated_line —
        # İngilizcede is_english_line'ın AYNISI, yalnız segment kapısının
        # başka dile ayırdığı cümle (Latince alıntı) "kalıntı" sayılmaz;
        # kalıntı yeniden çevirisi (_fix_english_remnants, autofix) kapının
        # bilerek bıraktığı dili çevirmeye kalkmaz.
        return is_untranslated_line(text, self._source_code(),
                                    min_words=4, min_length=15)

    def _fix_english_remnants(self, result: str) -> str:
        """Detect sentences still in English after translation and re-translate.

        This is a safety net: after main translation, any sentence that is
        still mostly English gets individually re-translated. Retries are
        capped at 1 attempt per unique segment hash (fingerprint of the
        sentence contents) to prevent cascade re-translation when the MT
        model keeps echoing English.
        """
        if self.target_lang != "tr":
            return result

        # Per-segment retry tracking (set of already-retried hashes).
        retried = getattr(self, "_remnant_retried", None)
        if retried is None:
            retried = set()
            self._remnant_retried = retried

        def _should_retry(sent: str) -> bool:
            # FX3 (V2 Y3): model bu cümlede cevap üretemediyse yeniden
            # gönderilmez (bekçinin tek yeniden denemesi yapıldı).
            if self._core_no_answer_for(sent.strip()):
                return False
            h = hash(sent.strip())
            if h in retried:
                return False
            retried.add(h)
            return True

        # Split into sentences
        sentences = self._SENTENCE_SPLIT_RE.split(result)
        if len(sentences) <= 1:
            # Single sentence — check it
            if (
                self._is_english_sentence(result)
                and len(result.strip()) > 30
                and _should_retry(result)
            ):
                try:
                    translator = self._get_translator()
                    retry = translator.translate_text(result.strip())
                    retry = self._sweep_prompt_markers(retry, "kalıntı tekrarı",
                                                       source=result)
                    if retry and retry.strip() and not self._is_english_sentence(retry):
                        retry = self._apply_glossary(retry)
                        # 2026-07-11: retry MT'si placeholder yutarsa geri al —
                        # bu yol revert guard'inin ARKASINDA calisiyordu,
                        # sessiz stash kaybi vektoruydu.
                        retry = self._revert_on_placeholder_loss(retry, result.strip())
                        logger.info("Re-translated English remnant: %.50s", result[:50])
                        return retry.strip()
                except Exception:
                    pass
            return result

        fixed_sentences: List[str] = []
        any_fixed = False

        for sent in sentences:
            if (
                self._is_english_sentence(sent)
                and len(sent.strip()) > 30
                and _should_retry(sent)
            ):
                # Try to re-translate this sentence
                try:
                    translator = self._get_translator()
                    retry = translator.translate_text(sent.strip())
                    retry = self._sweep_prompt_markers(retry, "kalıntı tekrarı",
                                                       source=sent)
                    if retry and retry.strip() and not self._is_english_sentence(retry):
                        retry = self._apply_glossary(retry)
                        # 2026-07-11: retry placeholder yutarsa cumleyi geri al
                        # (revert-guard'i baypas eden tek yol burasiydi).
                        retry = self._revert_on_placeholder_loss(retry, sent.strip())
                        fixed_sentences.append(retry.strip())
                        any_fixed = True
                        logger.info("Re-translated English remnant: %.50s", sent[:50])
                        continue
                except Exception:
                    pass
            fixed_sentences.append(sent)

        if any_fixed:
            return " ".join(fixed_sentences)
        return result

    # ------------------------------------------------------------------
    # Post-processing: clean up BookTranslator artifacts
    # ------------------------------------------------------------------

    # Patterns for BookTranslator placeholder remnants.
    # The MT model sometimes translates [[UNIT_0]] -> [[BIRIM_0]] (Turkish)
    # or mangles it into [[BİRİM_0]], [[FORMULA_0]] etc.
    _UNIT_PLACEHOLDER_RE = re.compile(
        r"\[\[(?:UNIT|BİRİM|BIRIM|PGREF)_(\d+)\]\]"
    )
    # FX3 (2026-10-05): son bekçi bozuk gövdeyi de görür ("[[FORMÜLA_0]]",
    # "[[Formül 2]]"); eskiden yalnız kanonik biçim yakalanıyor, onarılamayan
    # kalıntı çıktıya basılıyordu (bir model denemesinde QC 3 HIGH).
    _FORMULA_PLACEHOLDER_RE = re.compile(
        r"\[\[\s*(?:FORM[ÜU]LA?|Form[üu]la?|form[üu]la?)[_\s]*(\d+)\s*\]\]"
    )

    # Markdown bold: **some text** -> \textbf{some text}
    _MARKDOWN_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
    # FX3 (MT-18): eşi kalmamış markdown kalın işareti.
    _MARKDOWN_STARS_RE = re.compile(r"\*\*")

    # System prompt leakage patterns -- imported from patterns.py (DRY)
    _PROMPT_LEAK_PATTERNS = PROMPT_LEAK_PATTERNS

    def _post_process_translation(
        self, result: str, original: str, *, _skip_retranslation: bool = False,
    ) -> str:
        """Clean up known BookTranslator artifacts from *result*.

        Fixes applied (in order):
        0. Strip system prompt leakage from MT output
        1. Strip @...@@ proper-noun markers
        2. Remove [[UNIT_N]] / [[BIRIM_N]] / [[FORMULA_N]] remnants
        3. Convert Markdown **bold** to LaTeX \\textbf{...}
        4. Mixed-language detection (skipped if *_skip_retranslation*)
        """
        # 0. System prompt leakage detection and removal
        #    HY-MT 7B sometimes echoes its instruction into the output,
        #    either in English or translated to Turkish.
        #    G2a-2 (2026-10-05): istem işareti (<<<TRANSLATE_START>>> ve bozuk
        #    biçimleri) önce ve günlüklü temizlenir; aşağıdaki desen bozuk
        #    biçimi yakalamıyordu.
        result = self._sweep_prompt_markers(result, "MT çıktısı", source=original)
        for pattern in self._PROMPT_LEAK_PATTERNS:
            if pattern.search(result):
                # FX3 (V2 Y2): kaynakta geçen biçim meşru içeriktir, korunur.
                cleaned = sub_unless_in_source(pattern, result, original).strip()
                if cleaned != result.strip() and cleaned and len(cleaned) > 5:
                    logger.warning(
                        "Removed system prompt leakage from translation: %.80s",
                        pattern.pattern[:80],
                    )
                    result = cleaned

        # 1. @...@@ proper-noun markers from BookTranslator
        result = re.sub(r"@([^@]+)@@", r"\1", result)

        # 1b. EPUB placeholder hallucinations from MT
        #     HY-MT 7B sometimes hallucinates EPUB-style placeholders like
        #     @Bilinmeyen0000, @Unknown123, @@KNOWN0001@@, KNOWN0001@@
        #     These were never in the source text -- pure MT hallucination.
        if self._EPUB_HALLUCINATION_RE.search(result):
            if not self._EPUB_HALLUCINATION_RE.search(original):
                cleaned = self._EPUB_HALLUCINATION_RE.sub("", result).strip()
                if cleaned and len(cleaned) > 3:
                    logger.warning(
                        "Removed MT-hallucinated EPUB placeholder(s) from "
                        "translation: %.80s -> %.80s",
                        result[:80], cleaned[:80],
                    )
                    result = cleaned

        # 2. [[UNIT_N]] / [[BIRIM_N]] / [[FORMULA_N]] remnants
        #    These appear when BookTranslator's internal _protect_units or
        #    _protect_formulas stashes content but the MT model translates
        #    or mangles the placeholder text instead of passing it through.
        #    2026-09-24 (envanter H1): kalıntı SİLİNMEZ.
        #    Eskiden silme "km3 −2 s" hücresini "km³ −" yapıyordu (veri
        #    kaybı). Core artık [[BIRIM_n]]→[[UNIT_n]] onarıyor; buraya kadar
        #    gelen kalıntı onarılamamıştır (indeks yok / uydurma) — segment
        #    orijinaliyle geri alınır (B-11 ile aynı ilke: kayıpta orijinal).
        for _re, _label in (
            (self._UNIT_PLACEHOLDER_RE, "[[UNIT/BIRIM]]"),
            (self._FORMULA_PLACEHOLDER_RE, "[[FORMULA]]"),
        ):
            if _re.search(result) and not _re.search(original):
                logger.warning(
                    "%s kalıntısı onarılamadı; segment orijinaliyle geri "
                    "alındı (silme yok): %.60s", _label, original,
                )
                self._bump_stat("remnant_reverts")
                return original

        # 2b. Kaynakta olmayan sondaki "…" (2026-09-24, envanter J1).
        #     Kaynak display-math öncesi noktalamasız biter ("Therefore, the
        #     total energy"); model askıda cümleyi "…" ile kapatıyor
        #     (62 adayın 23'ünde). Yapısal çözüm (paragraf + formül + devamı
        #     tek segment) kapsam dışı; kozmetik: kaynak "…" ile bitmiyorsa sil.
        _ell = re.search(r"\s*(?:…|\.\.\.)\s*$", result)
        if _ell and not re.search(r"(?:…|\.\.\.)\s*$", original):
            result = result[:_ell.start()].rstrip()
            self._bump_stat("trailing_ellipsis_removed")

        # 3. Markdown **bold** -> \textbf{...}
        #    Some MT models output Markdown formatting instead of LaTeX.
        result = self._MARKDOWN_BOLD_RE.sub(r"\\textbf{\1}", result)
        # 3a. FX3 (2026-10-05, mt okuma MT-18): eşi kalmayan "**" (hymt2-de
        #     tablo hücresi "Fensterglas" -> "Cam**") modelin ekidir; QC
        #     görüyordu, temizleyen yoktu. Kaynakta geçen "**" (ör. Python
        #     üs işleci anlatan düzyazı) meşru içeriktir, sayısı kadar korunur.
        if "**" in result:
            cleaned = sub_unless_in_source(self._MARKDOWN_STARS_RE, result, original)
            if cleaned != result:
                removed = (result.count("**") - cleaned.count("**"))
                self._bump_stat_by("markdown_remnants_removed", removed)
                logger.info("Model ekli markdown artığı silindi (%d \"**\"): %.60s",
                            removed, cleaned)
                result = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

        # 3b. Fix common untranslated English sentence starters
        #     HY-MT 7B sometimes leaves short English words at sentence boundaries.
        result = self._fix_untranslated_sentence_starters(result)

        # 4. Mixed-language detection (skip at depth > 0 to prevent cascade)
        if not _skip_retranslation:
            result = self._check_mixed_language(result, original)
            # G2a-2: cümle düzeyi yeniden çevirinin çıktısı temizliksizdi
            # (qwen-de); sayı/simge bekçisinden önce temizlenir.
            result = self._sweep_prompt_markers(result, "cümle düzeyi çeviri",
                                                source=original)

        # 5. Numerical value preservation check
        #    All numbers in the original MUST appear in the translation.
        #    MT models sometimes hallucinate numbers (e.g. 128 -> 32).
        result = self._check_numerical_preservation(result, original)
        if result is original:
            # G2a-1: bekçi segmenti geri aldı — özgün metin BİREBİR döner
            # (boşluk temizliği kaynağı değiştirip "çeviri" gibi göstermesin).
            return original

        # 5b. G2a-2 (2026-10-05, mt okuma MT-14/MT-15): Türkçe düzyazıda
        #     ondalık ayırıcı virgül — yalnız kaynakta aynı rakamlarla ondalık
        #     olarak geçen sayı (muhafazakâr kural: localize_decimal_separators).
        if self.target_lang == "tr":
            result, n_dec = localize_decimal_separators(
                result, original, self._source_code(),
                keep_fn=self._is_english_sentence)
            if n_dec:
                stats = getattr(self, "stats", None)
                if stats is not None:
                    stats.decimal_separators_localized += n_dec

        # 6. Clean up extra whitespace left by placeholder removal
        result = re.sub(r"  +", " ", result)

        return result

    # Technical terms that legitimately stay in English (or Latin) in
    # Turkish scientific text.  These are stripped from BOTH source and
    # translation before computing sequence similarity so that their
    # presence does not inflate the "untranslated" score.
    _TECHNICAL_TERM_WHITELIST: set = {
        # ML / DL
        "machine learning", "deep learning", "reinforcement learning",
        "supervised learning", "unsupervised learning", "transfer learning",
        "neural network", "neural networks", "convolutional neural network",
        "recurrent neural network", "generative adversarial network",
        "transformer", "attention", "encoder", "decoder", "autoencoder",
        "dropout", "batch normalization", "layer normalization",
        "backpropagation", "back-propagation", "softmax", "sigmoid",
        "relu", "pooling", "embedding", "fine-tuning", "fine tuning",
        "overfitting", "underfitting", "cross-validation",
        # Math / physics
        "gradient descent", "stochastic gradient descent",
        "loss function", "cost function", "activation function",
        "kernel", "eigenvalue", "eigenvector",
        "fourier transform", "laplace transform",
        "navier-stokes", "reynolds number", "mach number",
        "finite element", "finite difference", "finite volume",
        "boundary layer", "mesh", "grid",
        # Proper nouns / frameworks that never translate
        "python", "pytorch", "tensorflow", "numpy", "scipy",
        "cuda", "gpu", "cpu", "api", "url", "http", "https",
        "resnet", "vgg", "lstm", "gru", "gan", "vae", "pinn",
        "adam", "sgd", "rmsprop",
    }

    def _check_mixed_language(self, result: str, original: str) -> str:
        """Detect translations that are identical/near-identical to source.

        Turkish uses Latin alphabet, so ASCII-ratio heuristics don't work.
        Instead, check if the translation is substantively different from
        the original -- if result ~ original, the MT model just echoed.

        Unlike the old version, this does NOT revert to original (keeping
        text in English is the worst outcome).  Instead, it tries sentence-
        level re-translation.

        Technical terms that legitimately remain in English (e.g. "machine
        learning", "Fourier transform") are stripped from both sides before
        computing similarity so they do not trigger false positives.
        """
        if self.target_lang not in ("tr",):
            return result

        # Strip placeholders for comparison
        clean_orig = re.sub(r"<<LATEX_\d+>>", "", original).strip()
        clean_result = re.sub(r"<<LATEX_\d+>>", "", result).strip()

        # Skip short segments (too unreliable for overlap)
        if len(clean_orig) < 40:
            return result

        orig_words = clean_orig.lower().split()

        # Need at least 5 words for meaningful comparison
        if len(orig_words) < 5:
            return result

        # --- Strip whitelisted technical terms before similarity check ---
        cmp_orig = clean_orig.lower()
        cmp_result = clean_result.lower()
        for term in sorted(self._TECHNICAL_TERM_WHITELIST, key=len, reverse=True):
            cmp_orig = cmp_orig.replace(term, "")
            cmp_result = cmp_result.replace(term, "")
        # Collapse residual whitespace
        cmp_orig = re.sub(r"\s+", " ", cmp_orig).strip()
        cmp_result = re.sub(r"\s+", " ", cmp_result).strip()

        # If after stripping technical terms almost nothing remains,
        # the segment is mostly jargon -- skip the check.
        if len(cmp_orig) < 20:
            return result

        # Use sequence similarity rather than set overlap
        from difflib import SequenceMatcher

        ratio = SequenceMatcher(
            None, cmp_orig, cmp_result,
        ).ratio()

        if ratio > 0.85:
            logger.warning(
                "Translation appears untranslated (%.0f%% sequence similarity). "
                "Trying sentence-level re-translation. Segment: %.60s",
                ratio * 100, original,
            )
            # DO NOT revert to original — try sentence-by-sentence re-translation
            try:
                sentences = self._source_sentence_split_re().split(original)
                if len(sentences) > 1:
                    retranslated_parts = []
                    translator = self._get_translator()
                    for sent in sentences:
                        if len(sent.strip()) < 3:
                            retranslated_parts.append(sent)
                            continue
                        tr = self._retranslate_sentence_protected(translator, sent)
                        retranslated_parts.append(tr if tr is not None else sent)
                    return " ".join(retranslated_parts)
                else:
                    # Single sentence that echoed — try once more.
                    # 2026-10-05 (G1a-2 önerisi): eskiden yer tutucular silinmiş
                    # metin (clean_orig) çevrilip DÖNDÜRÜLÜYORDU: <<LATEX_n>>
                    # içeriği (\textit{eser adı} vb.) çıktıdan düşüyordu.
                    translator = self._get_translator()
                    retry = self._retranslate_sentence_protected(translator, original)
                    if retry:
                        clean_retry = re.sub(r"<<LATEX_\d+>>", "", retry)
                        retry_ratio = SequenceMatcher(
                            None, clean_orig.lower(), clean_retry.lower()
                        ).ratio()
                        if retry_ratio < 0.85:
                            return retry
            except Exception as exc:
                self._log_mt_exception(logging.WARNING,
                                       "Re-translation attempt failed: %s", exc,
                                       exc=exc)

            # If all else fails, still return the result (partially translated
            # is better than keeping English)
            logger.warning("Could not improve translation, keeping partial result.")

        return result

    def _retranslate_sentence_protected(self, translator, sent: str) -> Optional[str]:
        """Cümle düzeyi yeniden çeviri, LaTeX korumalı (G1a-2 önerisi, mt okuma
        MT-16).

        Eskiden cümle modele ÇIPLAK gidiyordu: kaçışlı simge ve komut
        kabukları korunmuyor, dönüş denetlenmiyordu ("Berlin: Rehfeld \\&
        Söhne" -> "Rehfeld & Söhne": xelatex "Misplaced alignment tab", &
        sessizce düştü). Kabuklar protect_latex_shells ile yer tutucuya
        alınır; geri yazım tutmazsa ya da var olan <<LATEX_n>> yer tutucuları
        eksik/fazla dönerse None (cümle özgün dilde kalır).
        """
        src = sent.strip()
        shelled = protect_latex_shells(src)
        work, shells = shelled if shelled is not None else (src, None)
        tr = translator.translate_text(work)
        if not tr or not tr.strip():
            return None
        tr = tr.strip()
        if shells is not None:
            tr = restore_latex_shells(tr, work, shells)
            if tr is None:
                return None
        if (Counter(re.findall(r"<<LATEX_\d+>>", tr))
                != Counter(re.findall(r"<<LATEX_\d+>>", src))):
            return None
        return self._apply_glossary(tr)

    # Map of common English sentence starters/connectors → Turkish equivalents
    _SENTENCE_STARTER_MAP = {
        # Connectors
        r"\bThus\b": "Dolayısıyla",
        r"\bTherefore\b": "Bu nedenle",
        r"\bHence\b": "Dolayısıyla",
        r"\bMoreover\b": "Ayrıca",
        r"\bFurthermore\b": "Ayrıca",
        r"\bHowever\b": "Ancak",
        r"\bNevertheless\b": "Bununla birlikte",
        r"\bConversely\b": "Tersine",
        r"\bSimilarly\b": "Benzer şekilde",
        r"\bAlternatively\b": "Alternatif olarak",
        r"\bOtherwise\b": "Aksi takdirde",
        r"\bMeanwhile\b": "Bu arada",
        r"\bSubsequently\b": "Ardından",
        r"\bConsequently\b": "Sonuç olarak",
        # Phrases - matched at sentence start only
        r"\bIn particular\b": "Özellikle",
        r"\bIn other words\b": "Başka bir deyişle",
        r"\bIn fact\b": "Aslında",
        r"\bIn general\b": "Genel olarak",
        r"\bIn contrast\b": "Aksine",
        r"\bIn addition\b": "Buna ek olarak",
        r"\bFor example\b": "Örneğin",
        r"\bFor instance\b": "Örneğin",
        r"\bAs a result\b": "Sonuç olarak",
        r"\bOn the other hand\b": "Öte yandan",
    }

    def _fix_untranslated_sentence_starters(self, result: str) -> str:
        """Fix common English words left untranslated at sentence boundaries.

        HY-MT 7B sometimes leaves short English connectors/starters
        untranslated when they appear at the beginning of a Turkish sentence.
        Example: 'Thus Denklem 5.30...' → 'Dolayısıyla Denklem 5.30...'
        """
        if self.target_lang != "tr":
            return result

        turkish_chars = set("çğıöşüÇĞİÖŞÜ")

        for en_pattern, tr_replacement in self._SENTENCE_STARTER_MAP.items():
            regex = re.compile(
                r"(?:^|(?<=\.\s)|(?<=\n))" + en_pattern,
                re.MULTILINE,
            )

            # Build a replacement closure with explicitly bound variables.
            # Python closures use late binding: loop variables (tr_replacement,
            # result) would refer to whatever value they hold at CALL time,
            # not at definition time.  Using default arguments forces early
            # binding so each iteration gets its own snapshot.
            def _make_replacer(text: str, replacement: str):
                def _replace_if_turkish(m: re.Match) -> str:
                    after = text[m.end():m.end() + 50]
                    if any(c in turkish_chars for c in after):
                        return replacement
                    return m.group(0)  # keep original if context is English
                return _replace_if_turkish

            new_result = regex.sub(_make_replacer(result, tr_replacement), result)
            if new_result != result:
                logger.debug(
                    "Fixed untranslated sentence starter: %s → %s",
                    en_pattern, tr_replacement,
                )
                result = new_result

        return result

    def _check_numerical_preservation(self, result: str, original: str) -> str:
        """Ensure all numbers from *original* appear in *result*.

        MT models sometimes hallucinate or drop numbers (e.g. "128 filters"
        becomes "32 filters").  Numbers inside placeholders are excluded
        (they are LaTeX-protected content and will be restored later).

        G2a-1 (2026-10-05, mt okuma MT-7): hymt2-de "Bi < 0,1"i
        "Bi < ≤ 0 , ≤ 1" yaptı; bekçi "Numbers lost" uyarısını verip
        bozuk çeviriyi BASTI. Artık:

        * kaynakta olmayan ilişki simgesi (≤ ≥ ≈ ≠ … ; bkz.
          ``RELATION_SYMBOLS``) de sayı kaybı gibi bozukluktur;
        * yer tutucu boşlukla silinir ('0<<LATEX_65>>1' "01" sayısı
          değildir — eski yanlış alarm);
        * NUM-RETRY bir kez denenir (sayılar tam, ilişki simgesi yok, yer
          tutucu kümesi eşitse kabul); tutmazsa parça ORİJİNALİNE döner
          (``stats.number_reverts``) ve çağıran zincir onu kurtarma sırasına
          sokar (aynı metin → sade biçim → özgün dilde bırak + işaret).
          2026-07-06'daki "bozuk çeviriyi koru" kararı Berke'nin kayıpsızlık
          kararıyla (uydurma sayı/simge basılmaz) değişti.
        """
        missing = missing_numbers(original, result)
        extra_rel = new_relation_symbols(original, result)
        if not missing and not extra_rel:
            return result

        if missing:
            logger.warning(
                "Numbers lost in translation: %s not found in result. "
                "Original: %.60s",
                missing, original,
            )
        if extra_rel:
            logger.warning(
                "Çeviride kaynakta olmayan ilişki simgesi: %s. Kaynak: %.60s",
                dict(extra_rel), original,
            )
        # NUM-RETRY (2026-07-06 sutton smoke): faks numarası (572-4002) final
        # çıktıdan kaybolmuştu. Tek deneme; retry TÜM sayıları içeriyor,
        # uydurma ilişki simgesi taşımıyor ve yer tutucu kümesi eşitse kullan.
        try:
            translator = self._get_translator()
            # FX3 (V2 Y1): tekrar belleği okumaz (bozuk kayıt aynı çıktıyı
            # geri vermesin; çekirdek ertelenmiş kipte zaten yazmamıştır).
            with self._memory_bypassed():
                retry = translator.translate_text(original)
            # G2a-2: istem işareti bekçiden önce temizlenir ('<' '>' simge
            # sayılıp doğru tekrarı reddettirmesin).
            retry = self._sweep_prompt_markers(retry, "sayı tekrarı",
                                               source=original)
            if (retry and retry.strip()
                    and not missing_numbers(original, retry)
                    and not new_relation_symbols(original, retry)
                    and not self._missing_placeholders(retry, original)):
                logger.info(
                    "Number retry succeeded (%s recovered), "
                    "using retry translation.", missing or dict(extra_rel),
                )
                return self._apply_glossary(retry.strip())
        except Exception as exc:
            logger.warning("Number retry attempt failed: %s", exc)

        self.stats.num_warnings = getattr(self.stats, "num_warnings", 0) + 1
        self._bump_stat("number_reverts")
        logger.warning(
            "Sayı bekçisi: bozuk çeviri basılmadı, segment orijinaline "
            "döndürüldü: %.60s", original,
        )
        return original

    # ------------------------------------------------------------------
    # Glossary post-processing
    # ------------------------------------------------------------------

    def _target_wrong_alternatives(self) -> List[Tuple["re.Pattern[str]", str]]:
        """Sözlük girdilerinin Türkçe taraf yanlış karşılıkları (derlenmiş).

        [(desen, doğru Türkçe), ...]; sözlük motoru yoksa boş. Desen
        ``GlossaryEngine.verify_terms`` Pass 2 ile aynıdır (``\\b`` +
        IGNORECASE); sıra uzun terimden kısaya (``get_all_terms``), çok
        sözcüklü yanlış karşılık tek sözcüklüden önce denenir. Önbellek
        örnek üzerindedir (F1 MT-9, 2026-09-30).
        """
        cached = getattr(self, "_wrong_alt_cache", None)
        if cached is not None:
            return cached
        pairs: List[Tuple["re.Pattern[str]", str]] = []
        engine = getattr(self, "_glossary_engine", None)
        if engine is not None:
            try:
                for entry in engine.get_all_terms():
                    alts = getattr(entry, "wrong_alternatives", None) or ()
                    for wrong in alts:
                        if not wrong:
                            continue
                        pairs.append((re.compile(
                            r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE),
                            entry.effective_tr))
            except Exception as exc:  # sözlük hatası çeviriyi durdurmaz
                logger.warning("Sözlük yanlış karşılık listesi okunamadı: %s",
                               exc)
                pairs = []
        self._wrong_alt_cache = pairs
        return pairs

    def _apply_glossary(self, text: str) -> str:
        """Apply terminology glossary to *text*, skipping math segments.

        Splits on inline math ``$...$`` so that glossary replacements
        only affect prose text, not mathematical notation.

        Two passes:
        1. Fix known WRONG terms the MT model produces (exact Turkish
           phrases that are incorrect).  **Always runs** regardless of
           GlossaryEngine status, because this corrects MT *output*
           errors (Turkish-side), not input terms.
        2. Replace remaining English terms with correct Turkish equivalents
           (case-insensitive, whole-word boundaries).  **Skipped** when
           GlossaryEngine is active, because the engine already handles
           EN->TR term replacement via pre-translation stash/restore.
           Kept as fallback when GlossaryEngine is unavailable (e.g.,
           glossary/ directory missing or import failure).
        """
        if not text:
            return text

        # Split around inline math to protect it
        parts = self._INLINE_MATH_SPLIT_RE.split(text)
        for i, part in enumerate(parts):
            # Odd indices are math segments ($...$) -- skip
            if i % 2 == 1:
                continue

            # Pass 1: fix wrong MT terms (case-insensitive)
            # ALWAYS runs -- corrects Turkish-side MT errors
            # 2026-07-17 (#51/#82, eski #31): kelime siniri + case
            # korunumu — sinirsiz substring ekli formlari melezliyordu
            # ('asiri ogrenmeye' -> 'asiri uyumye') ve cumle basindaki
            # buyuk harf sabit-case ikameyle dusuyordu.
            from latex_pipeline.glossary import _match_case as _mc
            for wrong, correct in self._WRONG_TERMS.items():
                part = re.sub(
                    r"(?<!\w)" + re.escape(wrong) + r"(?!\w)",
                    lambda m, _c=correct: _mc(m.group(0), _c),
                    part,
                    flags=re.IGNORECASE,
                )

            # Pass 1b (F1 MT-9, 2026-09-30): sözlüğün Türkçe taraf yanlış
            # karşılıkları ("nozül" -> "lüle"; wrong_alternatives) kaynak
            # dilden BAĞIMSIZDIR. İngilizcede bunu Phase 5.75 verify_terms
            # yapar; İngilizce dışı kaynakta o geçiş atlandığı için (sözlüğün
            # EN->TR yarısı yanıltıcı) Türkçe taraf düzeltmesi de hiç
            # yapılmıyordu (hymt-ru: "nozül"). Burada yalnız İngilizce DIŞI
            # kaynakta, verify_terms Pass 2 ile AYNI desen ve harf büyüklüğü
            # korumasıyla uygulanır; İngilizce yol değişmez.
            if not self._source_is_english():
                for _wrong_re, _correct in self._target_wrong_alternatives():
                    part = _wrong_re.sub(
                        lambda m, _c=_correct: _mc(m.group(0), _c), part)

            # Pass 2: EN -> TR glossary (case-insensitive, word boundary)
            # SKIP when GlossaryEngine is active -- terminology enforcement is
            # owned by the Phase 5.75 verify_terms pass (translate_book.py),
            # which applies the full glossary post-translation. Running this
            # small curated map too would double-process (DRY violation).
            # Kept as fallback only when GlossaryEngine is unavailable.
            # 2026-09-28 (K5): EN->TR yedek sözlük İngilizce dışı kaynakta
            # çalışmaz (kaynak metin İngilizce değil; eşleşme yanıltıcı).
            if not self._glossary_engine and self._source_is_english():
                for en_term, tr_term in self._GLOSSARY.items():
                    # Use word boundaries to avoid partial matches.
                    # Example: "neural" should not match inside "neurality".
                    pattern = r"\b" + re.escape(en_term) + r"\b"
                    part = re.sub(pattern, tr_term, part, flags=re.IGNORECASE)

            parts[i] = part

        return "".join(parts)

    # ------------------------------------------------------------------
    # Phase 4 -- Restore stash
    # ------------------------------------------------------------------

    # Unicode subscript/superscript digit maps used by _restore_stash
    # to detect MT-mangled placeholder numbers (e.g. LATEX₁27 instead
    # of LATEX_127).
    _UNICODE_DIGIT_MAP: Dict[str, str] = {
        # subscripts
        "\u2080": "0", "\u2081": "1", "\u2082": "2", "\u2083": "3",
        "\u2084": "4", "\u2085": "5", "\u2086": "6", "\u2087": "7",
        "\u2088": "8", "\u2089": "9",
        # superscripts
        "\u2070": "0", "\u00b9": "1", "\u00b2": "2", "\u00b3": "3",
        "\u2074": "4", "\u2075": "5", "\u2076": "6", "\u2077": "7",
        "\u2078": "8", "\u2079": "9",
        # fullwidth digits
        "\uff10": "0", "\uff11": "1", "\uff12": "2", "\uff13": "3",
        "\uff14": "4", "\uff15": "5", "\uff16": "6", "\uff17": "7",
        "\uff18": "8", "\uff19": "9",
    }

    @classmethod
    def _normalize_unicode_digits(cls, text: str) -> str:
        """Replace Unicode subscript/superscript/fullwidth digits with ASCII."""
        for uc, asc in cls._UNICODE_DIGIT_MAP.items():
            if uc in text:
                text = text.replace(uc, asc)
        return text

    # Broad regex for mangled placeholders: covers spacing variations,
    # case variations (latex/Latex/LATEX), unicode angle brackets, and
    # mixed underscore/space before the number.
    _MANGLED_PLACEHOLDER_RE = re.compile(
        r"[<\u00ab\u2039\u226a\xab]"      # opening angle bracket variants
        r"\s*"
        r"[<\u00ab\u2039\u226a\xab]"
        r"\s*"
        r"[Ll][Aa][Tt][Ee][Xx]"            # case-insensitive LATEX
        r"[_\s\u2000-\u200b]*"             # underscore or various spaces
        r"([\d\u2080-\u2089\u2070-\u2079\u00b9\u00b2\u00b3\uff10-\uff19]+)"  # digits (ASCII + Unicode)
        r"\s*"
        r"[>\u00bb\u203a\u226b\xbb]"       # closing angle bracket variants
        r"\s*"
        r"[>\u00bb\u203a\u226b\xbb]"
    )

    # Token ayirici karakterler (F16'da _ensure_spacing yerelinden sinif
    # sabitine tasindi \u2014 tek-gecisli restore ayni kumeleri kullanir).
    # 2026-09-24: tire/\u00e7izgi de ay\u0131r\u0131c\u0131 \u2014 "\textit{x}-Axis", "$x$-ekseni"
    # gibi bile\u015fik yaz\u0131mlarda restore "x} -Axis" diye bo\u015fluk sokuyordu
    # (bir test ko\u015fusu: "The \textit{x} -Axis Rule").
    # 2026-09-28 (Vson; Vk bulgusu 2): "~" (TeX bağı) da ayırıcı — bağdan
    # sonra/önce boşluk eklenince "Section~ \ref{x}" oluyordu: bağ kırılabilir
    # boşluğa döner, çift boşluk çıkar ("Şekil~\ref{...}" her bilimsel
    # kitapta var).
    _RESTORE_SEP_RIGHT = frozenset(
        " \t\n\r.,;:!?)]}'\"\u00bb\u203a\\-\u2013\u2011~"
    )
    _RESTORE_SEP_LEFT = _RESTORE_SEP_RIGHT | {"{", "(", "["}

    # 2026-09-29 (F3-01): eşsiz matematik ayracı yer tutucusu kaynakta
    # komşusuna bitişikti ('$5', '\\[ 63}'); geri yazımda boşluk eklenmez.
    _GLUED_STASH_ITEMS = frozenset(("$", "$$", "\\[", "\\]"))

    @classmethod
    def _ensure_spacing(cls, body: str, start: int, end: int, replacement: str) -> str:
        """Insert *replacement* at body[start:end], adding spaces around it
        if the adjacent characters would otherwise run into the restored
        content.  Spaces are NOT added when the adjacent character is
        already whitespace or punctuation.
        """
        # 2026-09-29 (F3-03): kabuk yarıları ('\textit{' / '}') içteki
        # sözcüğe yapışıktır; araya boşluk girmez.
        needs_left = (
            start > 0
            and body[start - 1] not in cls._RESTORE_SEP_LEFT
            and not replacement.startswith(" ")
            and not replacement.startswith("}")
            and replacement not in cls._GLUED_STASH_ITEMS
        )
        needs_right = (
            end < len(body)
            and body[end] not in cls._RESTORE_SEP_RIGHT
            and not replacement.endswith(" ")
            and not replacement.endswith("\n")
            and not replacement.endswith("{")
            and replacement != "}"
            and replacement not in cls._GLUED_STASH_ITEMS
        )

        prefix = " " if needs_left else ""
        suffix = " " if needs_right else ""
        return body[:start] + prefix + replacement + suffix + body[end:]

    # Tam-formlu placeholder (fast path). Yakalanan grup: id rakamlari.
    _EXACT_PLACEHOLDER_RE = re.compile(r"<<LATEX_(\d+)>>")

    # Ic ice placeholder ozyineleme tavani (mesru derinlik 1-3; tavan
    # patolojik zincirlere karsi guvenlik agi).
    _RESTORE_MAX_NEST_DEPTH = 50

    def _restore_exact_placeholders(
        self,
        text: str,
        stash: List[str],
        restored_ids: set,
        dup_counts: Dict[int, int],
        *,
        bound: int,
        depth: int,
    ) -> str:
        """Tam-formlu ``<<LATEX_N>>`` token'larini TEK gecişte restore et (F16).

        Sozlesmeler (eski idx-basina donguyle birebir):
        - Belge sirasinda ILK gecis restore edilir; ayni id'nin sonraki
          kopyalari SILINIR (#73). Silinenler *dup_counts*'a yazilir.
        - Bosluk garantisi ``_ensure_spacing`` kurallariyla ayni: sol komsu
          o ana kadar uretilen ciktinin son karakteri, sag komsu ham
          metindeki sonraki karakterdir (olusan cift bosluklar
          ``_restore_stash`` sonundaki collapse gecisiyle tekillesir).
        - Ic ice placeholder: stash iceriğindeki token'lar yalniz id <
          *bound* ise ozyineli acilir (eski ters-sira semantigi — dis
          placeholder'in stash sirasi kucuk olan icini tasimasi mesru,
          tersi orphan supurucuye kalir).
        - stash araligi disindaki ya da sifir-dolgulu id'ler ("007")
          DOKUNULMADAN birakilir (orphan temizligi halleder).
        """
        if "<<LATEX_" not in text:
            return text
        if depth > self._RESTORE_MAX_NEST_DEPTH:
            logger.warning(
                "Stash restore: ic ice placeholder derinligi %d asildi -- "
                "kalan token'lar orphan temizligine birakildi.", depth,
            )
            return text

        out: List[str] = []
        prev_char = ""
        last_end = 0
        # Kaynak bitişikliği yalnız bu stash'in kendi _stash_protected
        # çağrısından biliniyorsa kullanılır (başka stash listesiyle değil).
        glue = (self.__dict__.get("_stash_glue")
                if self.__dict__.get("_stash_glue_ref") is stash else None)
        # FX3 (V2 Y7): kaynaktaki boşluk bilgisi (aynı stash için).
        spacing = (self.__dict__.get("_stash_spacing")
                   if self.__dict__.get("_stash_glue_ref") is stash else None)
        # 2026-09-29 (F3-01): kaynakta BİTİŞİK iki korunan parça ('$C_{4}$'
        # '$H_{10}$' bitişik atomu, '$x$\footnote{..}') arasına boşluk
        # eklenmez — bitişiklik kaynaktandır, boşluk dizgiyi değiştirir.
        prev_ph_end = -1

        for m in self._EXACT_PLACEHOLDER_RE.finditer(text):
            digits = m.group(1)
            idx = int(digits)
            if digits != str(idx) or idx >= bound or idx >= len(stash):
                continue  # ham birakilir; sonraki segmentle kopyalanir

            adjacent_left = m.start() == prev_ph_end
            prev_ph_end = m.end()
            segment = text[last_end:m.start()]
            if segment:
                out.append(segment)
                prev_char = segment[-1]
            last_end = m.end()

            if idx in restored_ids:
                dup_counts[idx] = dup_counts.get(idx, 0) + 1
                continue  # kopya silindi (icerik ilk konumda basildi)
            restored_ids.add(idx)

            replacement = stash[idx]
            if "<<LATEX_" in replacement:
                replacement = self._restore_exact_placeholders(
                    replacement, stash, restored_ids, dup_counts,
                    bound=idx, depth=depth + 1,
                )

            src_glue = glue.get(idx) if glue else None
            # FX3 (V2 Y7): kaynakta bu yanda boşluk yoktuysa ("W/$m^{2}$",
            # "\textit{Tn}+1", "\textit{r}/3") boşluk YALNIZ harf-harfe
            # yapışmada eklenir; eskiden "W/ $m^{2}$" basılıyordu.
            src_sp = spacing.get(idx) if spacing else None
            needs_left = (
                prev_char != ""
                and not adjacent_left
                and not (src_glue and src_glue[0])
                and prev_char not in self._RESTORE_SEP_LEFT
                and not replacement.startswith(" ")
                and not replacement.startswith("}")
                and replacement not in self._GLUED_STASH_ITEMS
                and (src_sp is None or src_sp[0] or prev_char.isalnum())
            )
            next_char = text[m.end()] if m.end() < len(text) else ""
            needs_right = (
                next_char != ""
                and not text.startswith("<<LATEX_", m.end())
                and not (src_glue and src_glue[1])
                and next_char not in self._RESTORE_SEP_RIGHT
                and not replacement.endswith(" ")
                and not replacement.endswith("\n")
                and not replacement.endswith("{")
                # Kapanan kabuk ('}'): Türkçe ek vurgulu sözcüğe bitişik
                # gelebilir ('\textit{kitap}lar') — boşluk girmez.
                and replacement != "}"
                and replacement not in self._GLUED_STASH_ITEMS
                and (src_sp is None or src_sp[1] or next_char.isalnum())
            )
            piece = (
                (" " if needs_left else "")
                + replacement
                + (" " if needs_right else "")
            )
            if piece:
                out.append(piece)
                prev_char = piece[-1]

        out.append(text[last_end:])
        return "".join(out)

    def _restore_stash(self, body: str, stash: List[str]) -> str:
        """Replace ``<<LATEX_N>>`` placeholders with originals.

        F16 (2026-07-28): saglam placeholder'lar tek soldan-saga gecişte
        restore edilir (``_restore_exact_placeholders``); ic ice
        placeholder'lar ozyinelemeyle acilir (eski ters-sira semantigi:
        yalniz daha kucuk id'li icler). Eksik/mangled id'ler icin eski
        slow-path korunur.

        Improvements over naive str.replace:
        1. **Unicode-mangled digits** -- MT models sometimes convert ASCII
           digits inside placeholders to Unicode subscripts/superscripts
           (e.g. ``LATEX₁27`` instead of ``LATEX_127``).  We normalise
           these before matching.
        2. **Spacing guarantee** -- After restoring a placeholder we check
           that adjacent characters do not collide with the restored
           content (e.g. ``wordLATEX_5word`` -> ``word <content> word``).
        3. **Broader orphan cleanup** -- Both ASCII and Unicode-mangled
           leftover placeholders are removed.

        Safety: After restoring all known placeholders, any remaining
        ``<<LATEX_N>>`` tokens with IDs outside the stash range are
        removed to prevent orphan placeholder leakage into the output.
        """
        stash_size = len(stash)

        # V2 denetimi (2026-09-29): MT'nin ters sıraya koyduğu kısa italik
        # kabuğu ('<<b>>sözcük<<a>>') restore'dan ÖNCE düzeltilir; aksi hâlde
        # '}sözcük\textit{' gövdeye girer (sayı dengeli, sıra bozuk).
        body = repair_italic_shell_order(body, stash, _LATEX_PLACEHOLDER_ID_RE)

        # Kozmetik on-gecis (2026-07-11, #74): MT'nin dusurdugu bosluklari
        # restore'dan ONCE onar — placeholder'lar bu asamada opak token
        # oldugu icin korunan icerik (URL, verbatim, math) etkilenmez.
        #  a) Kapanan paren/koseli + harf (")Sorgu", "]veri")
        #  b) Nokta + BUYUK harf (".Bu", ".Sekil") -- ".pdf" degil
        # "}" haric: LaTeX'te }\komut dizilimleri kasitli.
        body = re.sub(
            r"([)\]])"
            r"(?=[A-ZÇĞİÖŞÜa-zçğıöşü])",
            r"\1 ",
            body,
        )
        # (#50) Noktali kisaltmalarin ici ACILMAZ; '.Bu' gibi gercek
        # yapisiklik onarilir. Iki koruma: (a) nokta oncesi BUYUK harf
        # ('M.Ö.', 'T.C.', 'A.B.D.' zinciri), (b) nokta sonrasi
        # 'BUYUK harf + nokta' ('Ph.D.' — once kucuk 'h' oldugu icin
        # lookbehind tek basina yetmiyor).
        body = re.sub(
            r"(?<![A-ZÇĞİÖŞÜ])(\.)"
            r"(?=[A-ZÇĞİÖŞÜ])(?![A-ZÇĞİÖŞÜ]\.)",
            r"\1 ",
            body,
        )

        # --- Fast path (F16, 2026-07-28): TEK soldan-saga gecis ---
        # Eski dongu her idx icin body.find + tam-govde yeniden insa
        # yapiyordu: O(stash x govde) — 8k stash'te saniyeler. Simdi tum
        # saglam <<LATEX_N>> token'lari tek gecişte restore edilir;
        # sozlesmeler (ilk gecis restore + kopya silme #73, bosluk
        # garantisi, ic ice placeholder ters-sira semantigi) korunur.
        restored_ids: set = set()
        dup_counts: Dict[int, int] = {}
        body = self._restore_exact_placeholders(
            body, stash, restored_ids, dup_counts,
            bound=stash_size, depth=0,
        )
        for _idx in sorted(dup_counts):
            logger.warning(
                "MT duplicated placeholder <<LATEX_%d>> x%d -- kopyalar "
                "silindi (icerik yalniz ilk konumda restore edildi).",
                _idx, dup_counts[_idx],
            )

        # Slow-path on filtresi (F16): TEK genis taramayla govdede mangled
        # gorunumlu token'larin id'lerini topla. MT'nin tamamen YUTTUGU
        # id'ler icin (govdede hicbir izi yok) eski akis yine de id basina
        # 2 tam regex taramasi yapiyordu — binlerce eksikte saniyeler.
        mangled_candidate_ids: set = set()
        if len(restored_ids) < stash_size:
            for _m in self._MANGLED_PLACEHOLDER_RE.finditer(body):
                try:
                    mangled_candidate_ids.add(
                        int(self._normalize_unicode_digits(_m.group(1)))
                    )
                except ValueError:
                    continue

        # --- Slow path: yalniz gercekten eksik id'ler (mangled aday) ---
        for idx in range(stash_size - 1, -1, -1):
            if idx in restored_ids:
                continue
            placeholder = f"<<LATEX_{idx}>>"
            replacement = stash[idx]

            # Ic ice restore sirasinda govdeye j >= bound kuraliyla
            # DOKUNULMADAN gelen tam-formlu token eski akista da restore
            # edilmezdi (pass'i coktan gecmisti) — orphan supurucuye kalir.
            if placeholder in body:
                continue

            logger.debug(
                "Placeholder %s not found in body (possibly mangled by MT).",
                placeholder,
            )
            if idx not in mangled_candidate_ids:
                continue  # govdede bu id'ye ait mangled aday da yok

            def _expand_nested(rep: str, _idx: int = idx) -> str:
                # Ic ice placeholder: eski akista mangled restore'un govdeye
                # soktugu <<LATEX_j>> (j < idx) token'lari sonraki
                # pass'lerde acilirdi; tek-gecis duzeninde insertion
                # aninda acilir. Yalniz GERCEK insertion'da cagrilir ki
                # restored_ids bos yere kirletilmesin.
                if "<<LATEX_" not in rep:
                    return rep
                return self._restore_exact_placeholders(
                    rep, stash, restored_ids, dup_counts,
                    bound=_idx, depth=1,
                )

            # 1. Try spacing-only mangling: << LATEX_42 >> etc.
            mangled_space = re.compile(
                r"<\s*<\s*LATEX[_\s]*" + str(idx) + r"\s*>\s*>"
            )
            m = mangled_space.search(body)
            if m:
                replacement = _expand_nested(replacement)
                body = self._ensure_spacing(
                    body, m.start(), m.end(), replacement,
                )
                # 2026-07-11 (#73): sonraki mangled kopyalar da (cift basim
                # yerine) silinir — icerik yalniz ilk konumda restore edilir.
                body, _dups = mangled_space.subn("", body)
                if _dups:
                    logger.warning(
                        "Mangled placeholder <<LATEX_%d>> x%d kopya silindi.",
                        idx, _dups,
                    )
                continue

            # 2. Try Unicode-digit mangling: LATEX₁27 etc.
            #    Build a digit pattern that matches both ASCII and Unicode
            #    representations of this index number.
            idx_str = str(idx)
            digit_pat_parts = []
            for ch in idx_str:
                # Match the ASCII digit OR any Unicode variant of it
                variants = [re.escape(ch)]
                for uc, asc in self._UNICODE_DIGIT_MAP.items():
                    if asc == ch:
                        variants.append(re.escape(uc))
                digit_pat_parts.append("(?:" + "|".join(variants) + ")")
            digit_pat = "".join(digit_pat_parts)

            mangled_unicode = re.compile(
                r"[<\u00ab\u2039\u226a\xab]"
                r"\s*"
                r"[<\u00ab\u2039\u226a\xab]"
                r"\s*"
                r"[Ll][Aa][Tt][Ee][Xx]"
                r"[_\s\u2000-\u200b]*"
                + digit_pat +
                r"\s*"
                r"[>\u00bb\u203a\u226b\xbb]"
                r"\s*"
                r"[>\u00bb\u203a\u226b\xbb]"
            )
            m = mangled_unicode.search(body)
            if m:
                logger.info(
                    "Restored mangled placeholder %s (matched %r).",
                    placeholder, m.group(0),
                )
                replacement = _expand_nested(replacement)
                body = self._ensure_spacing(
                    body, m.start(), m.end(), replacement,
                )
                # Handle at most one further occurrence (hard cap).
                _iter = 0
                while _iter < 1:
                    m = mangled_unicode.search(body, m.start() + len(replacement) + 2)
                    if not m:
                        break
                    body = self._ensure_spacing(
                        body, m.start(), m.end(), replacement,
                    )
                    _iter += 1

        # Safety net: remove any orphan <<LATEX_N>> placeholders that
        # survived restoration.  These can occur when MT duplicates a
        # placeholder or generates a bogus ID outside the stash range.
        # Check both ASCII and Unicode-mangled forms.
        orphan_re = re.compile(
            r"[<\u00ab\u2039\u226a\xab]\s*"
            r"[<\u00ab\u2039\u226a\xab]\s*"
            r"[Ll][Aa][Tt][Ee][Xx]"
            r"[_\s\u2000-\u200b]*"
            r"[\d\u2080-\u2089\u2070-\u2079\u00b9\u00b2\u00b3\uff10-\uff19]+"
            r"\s*"
            r"[>\u00bb\u203a\u226b\xbb]\s*"
            r"[>\u00bb\u203a\u226b\xbb]"
        )
        orphan_count = len(orphan_re.findall(body))
        if orphan_count > 0:
            logger.warning(
                "Stash restore: removing %d orphan placeholder(s) "
                "that survived restoration (including mangled forms).",
                orphan_count,
            )
            body = orphan_re.sub("", body)

        # Also clean standard ASCII orphans (belt-and-suspenders)
        std_orphan_count = len(re.findall(r"<<LATEX_\d+>>", body))
        if std_orphan_count > 0:
            logger.warning(
                "Stash restore: removing %d standard orphan placeholder(s).",
                std_orphan_count,
            )
            body = re.sub(r"<<LATEX_\d+>>", "", body)

        # EPUB placeholder hallucination safety net:
        # MT (HY-MT 7B) sometimes hallucinates EPUB-style placeholders
        # like @Bilinmeyen0000, @@KNOWN0001@@, KNOWN0001@@ into output.
        # These are not real placeholders -- they are MT artifacts.
        # Remove them here as the last line of defense.
        _epub_hall_count = len(self._EPUB_HALLUCINATION_RE.findall(body))
        if _epub_hall_count > 0:
            logger.warning(
                "Stash restore: removing %d EPUB placeholder hallucination(s) "
                "from final output.",
                _epub_hall_count,
            )
            body = self._EPUB_HALLUCINATION_RE.sub("", body)

        # Final pass: collapse multiple spaces introduced by spacing
        # insertion (but preserve newlines and indentation).
        # NOT: MT'nin dusurdugu bosluklari onaran kozmetik regex'ler
        # (')Sorgu', '.Bu') 2026-07-11'de restore ONCESINE tasindi —
        # restore SONRASI kosunca stash'ten donen URL/verbatim icine
        # bosluk enjekte ediyordu ('wiki/X.Method' -> 'X. Method');
        # stash koruma sozlesmesinin ihlaliydi (#74).
        body = re.sub(r"([^ \n]) {2,}([^ \n])", r"\1 \2", body)

        return body

    # ------------------------------------------------------------------
    # Post-processing: brace balance fix
    # ------------------------------------------------------------------

    def _fix_line_brace_balance(self, text: str) -> str:
        """Fix brace imbalances introduced by translation.

        DISABLED: Line-by-line brace balancing corrupts multi-line LaTeX
        constructs (e.g. ``\\caption{...}`` spanning multiple lines,
        multi-line ``\\begin{tabular}{...}`` column specs). XeLaTeX's
        own auto-fix in compiler.py already handles isolated missing
        braces safely using log-guided line targeting.

        Kept as a no-op to preserve the call site (line ~755 in
        translate()).  The old heuristic logic below is unreachable
        (guarded by ``if False:``) and will be removed in a follow-up.
        """
        return text
