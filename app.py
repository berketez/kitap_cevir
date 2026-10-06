"""Kitapçevir -- sade arayüz (Streamlit).

Mart 2026 sürümünün düzeni korunur: solda kitap bilgisi, ortada "Çeviri",
sağda "Araçlar" (Terimler / Bellek / Karşılaştırma; adlar ui/branding.py'de).
Çeviri alanında iki yol durur:

  - PDF'den TeX çıkar: 1) TeX'i çıkar, 2) çevrilmiş TeX'i derle. Çeviriyi
    kullanıcı yapar ya da bir yapay zekâ asistanına yaptırır.
  - Yerel model ile çevir: model kitabı baştan sona kendi çevirir.

Arayüz iş mantığı kopyalamaz: her iş ayrı süreçte CLI kapılarından koşar
(ui/runner.py). Ekrandaki her sayı pipeline'ın gerçek çıktısıdır; sayacı
olmayan aşamada çubuk çizilmez.

Çalıştırma:  ./venv/bin/streamlit run app.py
"""

from __future__ import annotations

import hashlib
import html
import os
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st

from config.paths import YONETICI_URL_DEGISKENI, paketli_kip
from core.lang_detect import (
    GATE_STOP_CONFIDENCE,
    TEX_SOURCE_LANGS,
    language_list_tr,
    language_name_tr,
)
from core.mt_engines import registry
from core.mt_engines.contracts import SOURCE_BUILTIN, THINKING_ALWAYS, THINKING_UNKNOWN
from core.mt_engines.http_util import is_loopback_host
from core.mt_engines.thinking import profile_of
from ui import branding
from ui.runner import (
    BOOK_SUFFIXES,
    BOOK_TYPES_LABEL,
    DEFAULT_ENGINE,
    EXIT_COMPILE_FAILED,
    EXIT_MANUAL_LOCK,
    EXIT_MODEL_UNAVAILABLE,
    EXIT_QC_BELOW_THRESHOLD,
    EXIT_SOURCE_UNSUPPORTED,
    EXTRACT_ERROR_NAME_CONFLICT,
    MEMORY_DEFAULT_PROJECT,
    MEMORY_FUZZY_THRESHOLD,
    MEMORY_SEARCH_MIN_CHARS,
    PIPELINE_LOG_NAME,
    TRUNCATION_REPORT_NAME,
    DiscoveryReport,
    MemoryHit,
    Run,
    SourceLanguage,
    SourceStatus,
    TexFiles,
    assistant_prompt,
    book_slug,
    brace_fix_text,
    compile_command,
    compile_issue_text,
    compile_log_path,
    discard_upload,
    discover_models,
    engine_check,
    engine_choice_path,
    engine_label,
    engine_ready,
    epub_coverage_text,
    epub_report,
    extract_command,
    failed_pages,
    fold_text,
    forced_source_lang,
    format_decimal,
    format_percent,
    format_size,
    format_upload_limit,
    glossary_example,
    hardware_caption,
    hardware_gate,
    load_engine_choice,
    manual_lock,
    measure_source_language,
    memory_batch_note,
    memory_stats,
    model_option_label,
    model_outputs,
    model_pdf,
    name_conflict,
    open_path,
    phase_label,
    platform_problem,
    save_engine_choice,
    save_upload,
    search_memory,
    suffix_list_tr,
    sweep_stale_event_dirs,
    sweep_stale_uploads,
    tex_files,
    translate_command,
    untranslated_copy,
    upload_file_types,
    upload_limit_bytes,
    usage_error,
)

# Koşan işin ekranda yenilenme aralığı (sn).
REFRESH_SEC = 2
# TeX kartının EXPORT.tex / EXPORT_TR.tex'i yeniden yoklama aralığı (sn): çeviri
# uygulama dışında (asistan ya da elle) yapılır, dosya düşünce düğme açılsın.
FILE_CHECK_SEC = 5

JOB_LABELS = {
    "translate": "Çeviri sürüyor",
    "extract": "TeX çıkarılıyor",
    "compile": "Kitap diziliyor",
}
UNIT_LABELS = {"segment": "segment", "page": "sayfa"}

# Kenar çubuğundaki kitap adı alanının oturum anahtarı. Değer iş yuvasında da
# tutulur (_job()["name"]): sayfa yenilenince (yeni oturum) alan oradan dolar.
NAME_KEY = "book_name"
# TeX'i yeniden çıkarma onayı bekleyen istek (oturum anahtarı).
PENDING_EXTRACT_KEY = "pending_extract"
# Çıkarımda bu kadar ya da daha az bölüm çıkarsa yapı zayıf sayılır.
WEAK_STRUCTURE_MAX_CHAPTERS = 1

# Durum iletileri. Kart ve alan adları ui/branding.py'den okunur.
NAME_CONFLICT_MESSAGE = (
    "Bu adla başka bir kitap çıkarılmış; farklı bir "
    f"{branding.BOOK_NAME_LABEL} verin."
)
MANUAL_LOCK_MESSAGE = (
    f"Bu klasörde {branding.quoted(branding.TEX_TITLE)} ile yapılmış bir "
    "çeviri var; yerel model onu ezmemek için durdu. Yerel modeli denemek için "
    f"farklı bir {branding.BOOK_NAME_LABEL} kullanın."
)
MANUAL_LOCK_NOTE = (
    f"Bu klasörde {branding.quoted(branding.TEX_TITLE)} ile yapılmış bir "
    "çeviri var; yerel model onu ezmez. Yerel modeli denemek için farklı bir "
    f"{branding.BOOK_NAME_LABEL} kullanın."
)
WEAK_STRUCTURE_MESSAGE = (
    "Kitabın bölüm yapısı zayıf çıkmış olabilir ({chapters} bölüm bulundu). "
    "EXPORT.tex'teki başlıkları kontrol et."
)
EXTRACTION_REUSED_MESSAGE = (
    "Önceki çıkarım kullanıldı (klasördeki full_book.md). Kitap ya da çıkarım "
    "hatalıysa sıfırdan çıkarabilirsin."
)
EXPORT_TR_OVERWRITE_WARNING = (
    "Bu klasörde çevrilmiş EXPORT_TR.tex var. TeX yeniden çıkarılırsa "
    "EXPORT.tex yeniden yazılır; EXPORT_TR.tex silinmez ama yeni EXPORT.tex "
    "ile uyuşmayabilir."
)
# 1_cikar --json "export_changed": EXPORT.tex'in gövdesi değişti mi (True /
# False; ölçülemediyse null). Klasörde EXPORT_TR.tex varsa üçü ayrı söylenir.
EXPORT_TR_KEPT_NOTE = (
    "EXPORT.tex yeniden yazıldı ama içeriği değişmedi; klasördeki "
    "EXPORT_TR.tex geçerli."
)
EXPORT_TR_STALE_NOTE = (
    "EXPORT.tex'in içeriği değişti; klasördeki EXPORT_TR.tex eski "
    "EXPORT.tex'ten çevrilmişti. Derlemeden önce bölüm yapısını karşılaştır."
)
EXPORT_TR_UNKNOWN_NOTE = (
    "EXPORT.tex yeniden yazıldı; klasördeki EXPORT_TR.tex'in bu EXPORT.tex'ten "
    "çevrildiği doğrulanamadı. Derlemeden önce bölüm yapısını karşılaştır."
)
# EXPORT_TR.tex'in gövdesi EXPORT.tex'inkinin birebir kopyası (U7): "hazır"
# denmez. Kimlik başlığı karşılaştırmaya girmez (runner.untranslated_copy).
UNTRANSLATED_COPY_WARNING = (
    "EXPORT_TR.tex'in içeriği EXPORT.tex ile birebir aynı: dosya henüz "
    "çevrilmemiş görünüyor. Türkçeye çevirip aynı adla kaydet."
)

# "Yerel model ile çevir" kartının model seçicisi (TASARIM F). Seçicinin
# oturum anahtarı; değer iş yuvasında (_job()["engine"]) ve
# config/mt_selection.json'da da tutulur (runner.save_engine_choice).
ENGINE_KEY = "mt_engine"
# Seçim dosyası yazılamadıysa dosyanın yolu (oturum anahtarı; kartta yazılır).
ENGINE_SAVE_FAILED_KEY = "mt_engine_save_failed"
MODEL_SCAN_SPINNER = "Modeller taranıyor…"
MODEL_CHECK_SPINNER = "Model denetleniyor…"
MODEL_SCAN_FAILED = "Modeller taranamadı: {error}"
MODEL_CHECK_FAILED = "Model denetlenemedi: {error}"
MODEL_LAST_SCAN = "Son tarama: {time}"
MODEL_CHOICE_MISSING_NOTE = ("Son seçilen model ({engine_id}) bu taramada bulunamadı; "
                             "varsayılan model seçildi.")
MODEL_CHOICE_SAVE_FAILED = ("Model seçimi kaydedilemedi: {path} yazılamıyor. Seçim bu "
                            "oturumda geçerli; uygulama yeniden başlatılınca hatırlanmaz.")
MODEL_REASONING_CAPTION = f"Seçili model: {branding.MODEL_REASONING_NOTE}"
# Seçili modelin düşünme sınıfı (G2b-1, KARARLAR 22; 2026-10-05): keşfin
# gerçek üst verisinden (core.mt_engines.thinking.profile_of). Sınıfı
# bilinmeyen ama keşfin "düşünür" dediği model eski notla yazılır
# (MODEL_REASONING_CAPTION); düşünmesi kapatılamayan modelde düşünerek çeviri
# notu da (branding.THINKING_MODE_NOTE) gösterilir.
MODEL_THINKING_CAPTION = "Seçili model: {label}"
MODEL_RUN_CAPTION = "Model: {label}"
SIDEBAR_MODEL_CAPTION = "Çeviri modeli: {label} — {state}"
# Donanım kapısı kapalıyken kenar çubuğu (KARARLAR 17).
SIDEBAR_MODEL_CLOSED_CAPTION = f"Yerel model: {branding.MODEL_HW_CLOSED_LABEL}"
SIDEBAR_SYSTEM_CAPTION = "macOS · {hardware}"
# "Bileşenler" bağlantısı (TASARIM §F, F4): yalnız yönetici adresi verilmişse
# (paketli kip) ve adres bu bilgisayarın kendisini gösteriyorsa (yönetici
# yalnız 127.0.0.1'e düz http ile bağlanır; TASARIM B.5; yerellik denetimi
# motor katmanınınkiyle aynı: http_util.is_loopback_host). Başka adres
# bağlantı olmaz.
MANAGER_URL_SCHEME = "http"

# Aynı anda tek iş (F5-01, 2026-09-29). Düğmelerin kapalı değeri çizim anında
# hesaplanır; başka bir sekmede ya da yenilenmemiş sayfada açık görünen
# düğmenin tıklaması yine gelir. İş yuvası doluysa yeni iş başlatılmaz, bu
# ileti bir kez gösterilir (oturum anahtarı).
JOB_BUSY_KEY = "job_busy_notice"
JOB_BUSY_MESSAGE = (
    "Başka bir iş sürüyor (başka bir sekmede ya da pencerede başlatılmış "
    "olabilir); yeni iş başlatılmadı. Önce onun bitmesini bekle ya da İptal et."
)

# "Yerel model ile çevir" kartında kaynak dil (KARARLAR 19). Ölçüm
# translate_book'un dil ön kapısıyla aynı işlevdir (runner.measure_source_language);
# güven eşiği CLI kapısınınki (GATE_STOP_CONFIDENCE). Seçicinin oturum anahtarı
# kitabın geçici yolundan türer: yeni kitap yeni seçim ister.
SOURCE_LANG_KEY = "source_lang"
SOURCE_LANG_LABEL = "Kaynak dil"
SOURCE_LANG_PLACEHOLDER = "Kaynak dili seç"
SOURCE_LANG_HELP = ("Kitabın dili. Tespit edilen dili değiştirirsen çeviri seçtiğin "
                    "dille yapılır; hedef dil her zaman Türkçe.")
# Seçicinin altındaki satır: otomatik tespitin gerçek sonucu ve güveni.
SOURCE_LANG_DETECTED = "Otomatik tespit: {name} (güven {confidence})."
# Belirsiz / desteklenmeyen tespitte "Çevir" kapalıdır; seçim isteği bir kez,
# düğmenin altında yazılır (SOURCE_LANG_CHOICE_REQUIRED).
SOURCE_LANG_UNCERTAIN = ("Otomatik tespit belirsiz: en olası {name}, güven "
                         "{confidence} (eşik {threshold}).")
SOURCE_LANG_UNDETERMINED = "Otomatik tespit: dil belirlenemedi (güven {confidence})."
SOURCE_LANG_UNSUPPORTED = ("Otomatik tespit: {name} (güven {confidence}); bu dil "
                           "desteklenmiyor. Kitap desteklenen bir dildeyse elle seç.")
SOURCE_LANG_UNMEASURED = ("Otomatik tespit yapılamadı: kitabın gömülü metni yok ya da "
                          "çok az; dil çeviri başlayınca ölçülür. İstersen elle seç.")
SOURCE_LANG_FAILED = ("Otomatik tespit yapılamadı ({error}); dil çeviri başlayınca "
                      "ölçülür. İstersen elle seç.")
SOURCE_LANG_CHOICE_REQUIRED = "Çevirmek için önce kaynak dili seç."
SOURCE_LANG_FORCED_NOTE = "Kaynak dil elle seçildi: {name}; çeviri bu dille yapılır."
SOURCE_LANG_RUN_CAPTION = "Kaynak dil: {name} (elle seçildi)"

# Başarısız koşuların iletileri (U6). Hepsi noktasız yazılır: _failure_text
# ham kodu ve çıkış kodunu parantez içinde ekleyip cümleyi noktayla bitirir
# ("... (encoding, çıkış kodu 1)."). Ham kod yalnız parantez içinde durur.
EXTRACT_FAILED_MESSAGE = "TeX çıkarılamadı"
PDF_FAILED_MESSAGE = "PDF üretilemedi"
# 2_derle.py / translate_book.py çıkış 2 (kilit değilse): XeLaTeX düştü.
COMPILE_FAILED_MESSAGE = "PDF üretilemedi: XeLaTeX derlemesi başarısız oldu"
# 1_cikar.py --json "error" kodları. name_conflict ayrıca NAME_CONFLICT_MESSAGE
# ile anlatılır (farklı Kitap adı vermek dışında yol yok).
EXTRACT_ERROR_MESSAGES = {
    "input_missing": "Kitap dosyası bulunamadı; kitabı yeniden yükle",
    "input_format": "Kitap dosyası tanınmadı; yalnızca PDF ya da EPUB "
                    "çıkarılabilir",
    "extraction_failed": "Kitaptan metin çıkarılamadı",
    "body_missing": "Çıkarılan metinden LaTeX gövdesi (body.tex) üretilemedi",
}
# 2_derle.py --json "error" kodları.
COMPILE_ERROR_MESSAGES = {
    "input_missing": "EXPORT_TR.tex bulunamadı; çeviriyi kitabın klasörüne bu "
                     "adla kaydet",
    "input_too_small": "EXPORT_TR.tex neredeyse boş; çeviri dosyaya "
                       "kaydedilmemiş olabilir",
    "book_dir_unresolved": "EXPORT_TR.tex'in hangi kitaba ait olduğu "
                           "çözülemedi; dosyanın başındaki kimlik satırları "
                           "(% KITAPCEVIR-META) silinmiş ya da başka bir "
                           "kitabı gösteriyor olabilir",
    "extraction_missing": "Kitabın çıkarım dosyası (full_book.md) yok; önce "
                          f"{branding.quoted(branding.TEX_EXTRACT_BUTTON)} "
                          f"adımını aynı {branding.BOOK_NAME_LABEL} ile "
                          "çalıştır",
    "encoding": "EXPORT_TR.tex UTF-8 değil; dosyayı UTF-8 olarak kaydedip "
                "yeniden derle",
    "structure_mismatch": "Çevrilmiş dosyadaki bölüm sayısı kaynakla "
                          "uyuşmuyor; bölüm başlıklarını EXPORT.tex ile "
                          "karşılaştır",
}
# Bu kodlarda CLI "Ayrıntı için pipeline.log'a bak" der; arayüz yolu gösterir.
LOG_HINT_ERRORS = ("extraction_failed", "body_missing")
LOG_HINT_LABEL = "Ayrıntılı günlük:"
# CLI komut satırını reddetti (argparse, çıkış 2; F5-06): derleme hatası değil.
USAGE_ERROR_MESSAGE = "İş başlatılamadı: komut satırı seçeneği reddedildi"

# Derleme sonucu (U7, U8): 2_derle.py --json S4 alanları.
QC_BELOW_THRESHOLD_MESSAGE = "PDF üretildi ama kalite denetimi eşiğin altında kaldı"
QC_TOP_LABEL = "En çok bulgu veren denetimler"
QC_REPORT_LABEL = "Kalite denetimi raporu:"
EPUB_DOWNLOAD_LABEL = "EPUB dosyasını indir"
EPUB_SKIPPED_LABEL = "EPUB üretilmedi"
# Derleme çıkış 0/3'te günlükteki TeX hatası / eksik glif (F5-02): uyarının
# metni 2_derle'nin CLI satırıdır (runner.compile_issue_text); altında günlük.
COMPILE_LOG_LABEL = "Derleme günlüğü:"
# Çıkarımda okunamayıp atlanan sayfalar (F1-03 sözleşmesi: 1_cikar --json ya da
# kitap.json "failed_pages", 1'den başlayan sayfa numaraları). Kayıpsızlık:
# kullanıcı eksik sayfayı görmeden çeviriye geçmesin.
FAILED_PAGES_MESSAGE = ("Çıkarımda {count} sayfa okunamadı; bu sayfaların metni "
                        "çeviriye girmedi (sayfa {pages}).")
FAILED_PAGES_SHOWN = 10
FAILED_PAGES_MORE = " ve {more} sayfa daha"
# Üretim sınırına dayanan parçalar (V2 denetimi, F3-07): kesik olabilecek
# çeviri kabul edilir (KARARLAR 15) ama sessiz geçmez; sayı translate_book'un
# bu koşuda yazdığı listeden.
TRUNCATED_MESSAGE = ("{count} paragrafın çevirisi modelin üretim sınırına dayandı; "
                     "bu paragraflar eksik çevrilmiş olabilir. Liste:")

# Kitap yükleyici (U11, U15). Widget anahtarı "Kitabı kaldır"da değişir
# (_upload_key): Streamlit yükleyiciyi koddan boşaltmaya izin vermez.
UPLOAD_WIDGET_KEY = "book_upload"
# Karşılaştırma sekmesinin iki yükleyicisi.
COMPARE_UPLOAD_KEYS = ("qa_original", "qa_translated")
COMPARE_SUFFIXES = (".pdf",)
# Streamlit yükleyicisinin İngilizce iç metinleri ("Drag and drop file here",
# "Limit 500MB per file • PDF, EPUB", "Browse files", "... files are not
# allowed.") Python'dan değiştirilemiyor; CSS ile gizlenip yerine bunlar
# yazılır (_uploader_css). {types}: türler, {limit}: sınır ("524 MB").
UPLOAD_DROP_TEXT = "Dosyayı buraya sürükleyip bırak"
UPLOAD_BROWSE_TEXT = "Dosya seç"
UPLOAD_DETAIL_TEXT = "{types} · en çok {limit}"
# Yükleyicinin hata satırı: Streamlit tür, boyut ya da ağ hatasında aynı
# yere yazar ve CSS hangisi olduğunu ayıramaz; ileti nedeni uydurmaz, kuralı
# söyler.
UPLOAD_ERROR_TEXT = ("Bu dosya yüklenemedi. Yalnızca {types}, en çok {limit} "
                     "yüklenebilir.")
# Kitap yokken yükleyicinin altında. Türleri ve sınırı yükleyicinin kendi
# satırı söyler (UPLOAD_DETAIL_TEXT); burada tekrarlanmaz, sonraki adım yazılır.
UPLOAD_HINT = (f"Kitap yüklenince {branding.quoted(branding.TEX_TITLE)} ve "
               f"{branding.quoted(branding.MODEL_TITLE)} kartları açılır.")
REMOVE_BOOK_LABEL = "Kitabı kaldır"
REMOVE_BOOK_HELP = ("Yüklenen kitabı bırakır; output/ altındaki çıktı klasörüne "
                    "dokunulmaz.")

# Düzen (U12): iki satır CSS'i bu kapların anahtarlarıyla sütunları seçer.
LAYOUT_KEY = "kc_layout"
CARDS_KEY = "kc_cards"
# Bu genişlikte ve altında Çeviri ile Araçlar sütunları ve iki kart alt alta
# dizilir.
STACK_BELOW_PX = 900
# Araçlar sütununun en dar genişliği: daha geniş ekranlarda da sütun bunun
# altına inecekse (ör. 1000-1200 piksel) Araçlar Çeviri'nin altına geçer;
# yoksa sekmeler, arama alanları ve yükleyiciler sıkışıyordu.
TOOLS_MIN_WIDTH_REM = 14
# TeX ve yerel model kartlarının en dar genişliği: daha dar sütunda kartlar
# yan yana sıkışıp sözcük ortasından kırılacağına alt alta dizilir.
CARD_MIN_WIDTH_REM = 14

# Araçlar sütunu (U13, U14).
# G2c (KARAR 25): Terimler sekmesi seçili kitabın kendi sözlüğünü gösterir
# (kitap klasöründe glossary_shared.tsv; kaynak dilde terim -> Türkçe).
TERMS_PLACEHOLDER_FALLBACK = "kaynak terim"
TERMS_NOTE = ("Bu kitabın sözlüğünde {count} terim var, {shown} tanesi "
              "gösteriliyor; kaynak dildeki terimde ya da Türkçe karşılıkta ara.")
TERMS_NO_BOOK = ("Terimler seçili kitabın kendi sözlüğünden gösterilir; önce "
                 "bir kitap yükleyin.")
TERMS_NO_GLOSSARY = (
    "Bu kitabın sözlüğü henüz yok (kitap klasöründe glossary_shared.tsv). "
    f"{branding.quoted(branding.MODEL_TITLE)} çeviriye başlarken çıkarır; "
    f"{branding.quoted(branding.TEX_TITLE)} yolunda çevirmen yazar.")
# Tabloda en çok gösterilen satır (uzun sözlükte arama daraltır).
TERMS_MAX_ROWS = 200
# Eşleşme yokken "En yakın" önerisinin en düşük benzerliği (eski
# GlossaryManager.suggest_term eşiğiyle aynı, rapidfuzz ratio).
TERMS_SUGGEST_MIN_SCORE = 80
MEMORY_SEARCH_PLACEHOLDER = "Kaynak ya da çeviri metninde ara…"
MEMORY_MISSING_NOTE = ("Çeviri belleği henüz oluşmamış; yerel model ile yapılan "
                       "çeviriler buraya kaydedilir.")
MEMORY_QUERY_TOO_SHORT = f"Aramak için en az {MEMORY_SEARCH_MIN_CHARS} karakter yaz."
MEMORY_NO_MATCH = "Bu metin bellekte geçmiyor; benzer kayıt da yok."
MEMORY_MATCH_COUNT = "{total} kayıtta geçiyor."
MEMORY_MATCH_COUNT_LIMITED = "{total} kayıtta geçiyor; en kısa {shown} kayıt gösteriliyor."
# Metin hiçbir kayıtta geçmiyor (ör. yazım hatası): en yakın kayıtlar.
MEMORY_FUZZY_COUNT = ("Bu metin bellekte geçmiyor; benzerliği en az {threshold} olan "
                      "{total} kayıt var.")
MEMORY_FUZZY_COUNT_LIMITED = (
    "Bu metin bellekte geçmiyor; benzerliği en az {threshold} olan {total} kayıt "
    "var, en yakın {shown} kayıt gösteriliyor."
)

# Mart 2026 arayüzünün görünümü (koyu zemin, indigo vurgu). Veriye bağlı
# olmayan süs animasyonu (o sürümdeki "shimmer") bilerek alınmadı. Büyük
# harfe çevrilen etiketler lang="tr" taşır: yoksa tarayıcı "i"yi "I" yapar.
CSS = """
<style>
/* Soluk metnin tek rengi (U17): koyu zeminde kontrast 4,5:1'in üstünde
   kalsın diye alfa en az 0,55 (zeminlere göre ~6:1). 0,35-0,40 alfa ~3:1
   veriyordu. */
:root { --kc-muted: rgba(255,255,255,0.55); }
* { font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif; }
/* Üst çubuk (header) GİZLENMEZ: kenar çubuğunu geri açan düğme
   (stExpandSidebarButton) onun içinde durur; header gizlenince daraltılan
   kenar çubuğu bir daha açılamıyordu. Yalnız geliştirici menüsü, Deploy
   düğmesi ve alt bilgi gizlenir. */
header[data-testid="stHeader"] { background: transparent; }
#MainMenu, [data-testid="stMainMenu"], [data-testid="stAppDeployButton"],
[data-testid="stToolbarActions"], [data-testid="stDecoration"], footer {
    display: none;
}
[data-testid="stExpandSidebarButton"] { visibility: visible; }
.stApp { background: linear-gradient(180deg, #0a0a0f 0%, #12121a 100%); }
.block-container { padding-top: 2.5rem; max-width: 1400px; }
section[data-testid="stSidebar"] {
    background: #0d0d12; border-right: 1px solid rgba(255,255,255,0.05);
}
h1, h2, h3 { font-weight: 600 !important; letter-spacing: -0.02em; color: #ffffff; }
.logo {
    text-align: center; padding: 1.5rem 0 2rem;
    border-bottom: 1px solid rgba(255,255,255,0.06); margin-bottom: 1.5rem;
}
.logo-text { font-size: 1.4rem; font-weight: 600; color: #fff; letter-spacing: -0.03em; }
.logo-sub {
    font-size: 0.75rem; color: var(--kc-muted); margin-top: 0.3rem;
    letter-spacing: 0.1em; text-transform: uppercase;
}
.section-header {
    font-size: 0.7rem; font-weight: 600; color: var(--kc-muted);
    text-transform: uppercase; letter-spacing: 0.12em; margin: 1.5rem 0 0.8rem;
    padding-bottom: 0.5rem; border-bottom: 1px solid rgba(255,255,255,0.06);
}
.file-card {
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px; padding: 1.1rem 1.4rem; margin: 1rem 0;
}
/* Dosya adı boşluksuz olabilir: yalnız sığmadığında kırılır (break-all
   her satır sonunda sözcüğü bölüyordu). */
.file-name {
    font-weight: 500; color: #fff; font-size: 0.95rem; overflow-wrap: anywhere;
}
.file-meta { color: var(--kc-muted); font-size: 0.8rem; margin-top: 0.2rem; }
.upload-hint { color: var(--kc-muted); font-size: 0.9rem; margin: 0.5rem 0 1rem; }
/* Kart metni sözcük ortasından kırılmaz (U12); kartlar en dar genişliğin
   altına inmediği için (CARD_MIN_WIDTH_REM) sözcük her zaman sığar. */
.path-title, .path-desc { overflow-wrap: normal; word-break: normal; }
.path-title { font-size: 1.1rem; font-weight: 600; color: #fff; }
.path-desc {
    font-size: 0.85rem; color: var(--kc-muted); line-height: 1.5;
    margin: 0.4rem 0 1rem;
}
[data-testid="stBaseButton-primary"] {
    background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); border: none;
    border-radius: 10px; font-weight: 500; box-shadow: 0 4px 15px rgba(99,102,241,0.3);
}
[data-testid="stBaseButton-primary"]:disabled {
    background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.35); box-shadow: none;
}
[data-testid="stBaseButton-secondary"] {
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.1);
    border-radius: 10px;
}
.stDownloadButton > button {
    background: rgba(34,197,94,0.15); border: 1px solid rgba(34,197,94,0.3);
    color: #22c55e; border-radius: 10px;
}
.stTabs [data-baseweb="tab-list"] {
    gap: 0; background: rgba(255,255,255,0.03); border-radius: 10px; padding: 4px;
}
/* Sekmeler taşmaz (U12): sütun daralınca kaydırma okuna saklanmak yerine
   alt satıra geçer. Kaydırmalı listeye göre konumlanan alt çizgi ile sınır
   çizgisi, satır kırılınca yanlış yere düşeceği için gizli; seçili sekme
   zemin rengiyle belli. */
.stTabs [data-baseweb="tab-list"] { flex-wrap: wrap; overflow: visible; }
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] {
    display: none;
}
/* Yatay iç boşluk 0.5rem: üç sekme 1280 piksel genişlikteki sütuna sığar. */
.stTabs [data-baseweb="tab"] {
    border-radius: 8px; padding: 0.5rem 0.5rem; font-size: 0.85rem; font-weight: 500;
}
.stTabs [aria-selected="true"] { background: rgba(99,102,241,0.2); }
/* Model seçicinin açılır listesi: kart dar (1280 pikselde ~210 piksel),
   seçenek etiketi (ad · kaynak · ayrıntı · boyut) kesiliyor, kaynak ve boyut
   hiç görünmüyordu (Playwright, 28 Eylül 2026). Liste seçiciden geniş açılır.
   Liste sanal (sabit satır yüksekliği): metni alt satıra kaydırmak satırları
   üst üste bindirirdi. Açılır pencere gövdeye taşındığı için seçicinin
   anahtarıyla kapsanamaz; kural kaynak dil seçicisinin (KARARLAR 19) kısa
   listesine de uygulanır, zararsız. 36rem'den 48rem'e (K6, 5 Ekim 2026):
   etikete "önerilen" ve düşünme sınıfı eklenince 36rem'de ayrıntı ve boyut
   yine kesiliyordu (Playwright, 1280 piksel; ~70 karakter sığıyordu). */
[data-baseweb="popover"] div:has(> [data-testid="stSelectboxVirtualDropdown"]) {
    min-width: min(48rem, calc(100vw - 2rem));
}
.term-badge {
    display: inline-block; background: rgba(99,102,241,0.15); color: #a5b4fc;
    padding: 0.3rem 0.8rem; border-radius: 6px; font-size: 0.85rem; margin: 0.2rem 0;
}
/* "Beta" etiketi (KARARLAR 23): kart adının yanında düz etiket, animasyon yok. */
.path-badge {
    display: inline-block; margin-left: 0.5rem; padding: 0.05rem 0.5rem;
    border-radius: 999px; font-size: 0.7rem; font-weight: 600; vertical-align: middle;
    color: #fbbf24; background: rgba(251,191,36,0.12);
    border: 1px solid rgba(251,191,36,0.35);
}
/* Kenar çubuğunun alt bölümü (F4): "Hakkında" metni ve bağlantılar. */
.about-text { font-size: 0.8rem; color: var(--kc-muted); line-height: 1.5; }
.sidebar-link { font-size: 0.85rem; margin: 0.3rem 0; }
.sidebar-link a { color: #a5b4fc; text-decoration: none; }
.sidebar-link a:hover { text-decoration: underline; }
.memory-hit { font-size: 0.9rem; line-height: 1.5; margin: 0 0 0.9rem; }
.memory-target { color: #c7d2fe; }
.memory-hit mark { background: rgba(99,102,241,0.35); color: #fff; border-radius: 3px; }
</style>
"""


def _css_string(text: str) -> str:
    """CSS ``content`` değeri: çift tırnaklı, ters bölü ve tırnak kaçışlı."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _layout_css() -> str:
    """Dar ekran düzeni (U12): sütun seçicileri kap anahtarlarından türer."""
    def columns(key: str) -> str:
        return (f'.st-key-{key} > [data-testid="stLayoutWrapper"] > '
                '[data-testid="stHorizontalBlock"] > [data-testid="stColumn"]')

    return (
        "<style>\n"
        f"@media (max-width: {STACK_BELOW_PX}px) {{\n"
        f"  {columns(LAYOUT_KEY)},\n"
        f"  {columns(CARDS_KEY)} {{\n"
        "    flex: 1 1 100% !important; min-width: 100% !important;\n"
        "  }\n"
        "}\n"
        f"{columns(LAYOUT_KEY)}:last-child "
        f"{{ min-width: min(100%, {TOOLS_MIN_WIDTH_REM}rem); }}\n"
        f"{columns(CARDS_KEY)} {{ min-width: min(100%, {CARD_MIN_WIDTH_REM}rem); }}\n"
        "</style>"
    )


def _uploader_css(limit: str) -> str:
    """Streamlit yükleyicisinin İngilizce metinleri yerine Türkçesi (U11).

    İngilizce metin görünmez ve erişilebilirlik ağacından çıkar
    (visibility: hidden); Türkçesi aynı öğenin ::after içeriğidir. Türler ve
    sınır her yükleyici için anahtarından (st-key-...) seçilir. Kitap
    yükleyicisinde yüklenmiş dosyanın satırı da gizlenir (U15): kitap
    aşağıdaki dosya kartında bir kez görünür; hata ve yükleme ilerlemesi
    satırları görünür kalır.
    """
    zone = '[data-testid="stFileUploaderDropzone"]'
    lines = '[data-testid="stFileUploaderDropzoneInstructions"] > div > span'
    error = '[data-testid="stFileUploaderFileErrorMessage"]'
    rules = [
        f"{lines} {{ visibility: hidden; font-size: 0 !important; line-height: 0; }}",
        f"{lines}::after {{ visibility: visible; display: block; line-height: 1.5; }}",
        f"{lines}:first-child::after {{ font-size: 1rem; "
        f"content: {_css_string(UPLOAD_DROP_TEXT)}; }}",
        f"{lines}:last-child::after {{ font-size: 0.875rem; }}",
        f"{zone} button {{ font-size: 0 !important; }}",
        f"{zone} button::after {{ font-size: 0.875rem; "
        f"content: {_css_string(UPLOAD_BROWSE_TEXT)}; }}",
        f"{error} {{ visibility: hidden; font-size: 0 !important; line-height: 0; }}",
        f"{error}::after {{ visibility: visible; display: block; font-size: 0.875rem; "
        "line-height: 1.4; white-space: normal; }",
        # Türkçe ileti uzun: Streamlit'in satırı tek satır yüksekliğinde ve
        # taşanı gizliyor; dosya adı sıkışıp kayboluyor, iletinin sonu
        # kesiliyordu. Hatalı satır uzar, ileti adın altına geçer.
        f'[data-testid="stFileUploaderFile"]:has({error}) '
        "{ height: auto; min-height: 2.5rem; }",
        f".stFileUploaderFileData:has({error}) {{ flex-wrap: wrap; overflow: visible; }}",
        f".stFileUploaderFileData small:has({error}) "
        "{ white-space: normal; flex-basis: 100%; height: auto; }",
        # Yüklenmiş kitabın satırı (hata ya da ilerleme çubuğu taşımayan).
        f'[class*="st-key-{UPLOAD_WIDGET_KEY}"] [data-testid="stFileUploaderFile"]'
        ':not(:has([data-testid="stFileUploaderFileErrorMessage"]))'
        ':not(:has([role="progressbar"])) { display: none; }',
    ]
    uploaders = [(f'[class*="st-key-{UPLOAD_WIDGET_KEY}"]', BOOK_TYPES_LABEL)]
    uploaders += [(f".st-key-{key}", suffix_list_tr(COMPARE_SUFFIXES))
                  for key in COMPARE_UPLOAD_KEYS]
    for scope, types in uploaders:
        detail = UPLOAD_DETAIL_TEXT.format(types=types, limit=limit)
        message = UPLOAD_ERROR_TEXT.format(types=types, limit=limit)
        rules.append(f"{scope} {lines}:last-child::after "
                     f"{{ content: {_css_string(detail)}; }}")
        rules.append(f"{scope} {error}::after {{ content: {_css_string(message)}; }}")
    return "<style>\n" + "\n".join(rules) + "\n</style>"


# ---------------------------------------------------------------------------
# Durum
# ---------------------------------------------------------------------------

@st.cache_resource
def _job() -> dict:
    """Sunucu ömrünce tek iş yuvası.

    Yerel, tek kullanıcılı bir uygulama: sayfa yenilense de (yeni oturum)
    koşan iş kaybolmasın diye durum oturumda değil süreç düzeyinde tutulur.
    Model seçicinin alanları: son tarama raporu ("discovery") ya da tarama
    hatası ("discovery_error"), kullanıcının seçtiği model ("engine"), son
    taramada bulunamayan kayıtlı seçim ("engine_missing"), koşan çevirinin
    modeli ("run_engine") ve "Çevir"in olumlu ön kontrolünün uyarısı
    ("run_warning"; ör. kaba bellek tahmini). Alanlar .get() ile okunur:
    testlerin yuvaları bunları taşımayabilir.
    """
    return {"book": None, "upload_key": None, "run": None,
            "run_book": None, "run_name": None, "name": None, "upload_nonce": 0,
            "discovery": None, "discovery_error": None, "engine": None,
            "engine_missing": None, "run_engine": None, "run_warning": None}


@st.cache_resource
def _swept_uploads() -> list[Path]:
    """Sunucu açılışında bir kez: önceki sunuculardan kalan eski yükleme ve
    olay dizinleri (olay dizinleri kill -9 / çökmede kalır; F5-04)."""
    return sweep_stale_uploads() + sweep_stale_event_dirs()


@st.cache_resource
def _start_lock() -> threading.Lock:
    """İş yuvasının denetle-başlat kilidi (F5-01); sunucu ömrünce tek.

    Streamlit her oturumun betiğini ayrı iş parçacığında koşar; iki oturum
    aynı anda tıklasa da yuvayı yalnız biri doldurur.
    """
    return threading.Lock()


def _busy() -> bool:
    run = _job()["run"]
    return run is not None and run.running


def _busy_now() -> bool:
    """Kilit altında karar için: koşan işi önce yokla (bitmiş olabilir)."""
    run = _job()["run"]
    if run is not None:
        run.poll()
    return _busy()


def _upload_key(job: dict) -> str:
    """Kitap yükleyicisinin anahtarı; "Kitabı kaldır" sayacı artırıp yeniler."""
    nonce = job.get("upload_nonce") or 0
    return f"{UPLOAD_WIDGET_KEY}_{nonce}" if nonce else UPLOAD_WIDGET_KEY


def _upload_limit() -> tuple[int, str]:
    """Yükleme sınırı: bayt ve ekrandaki biçimi ("524 MB")."""
    limit = upload_limit_bytes(st.get_option("server.maxUploadSize"))
    return limit, format_upload_limit(limit)


def _remove_book() -> None:
    """Yüklenen kitabı bırak (U15): geçici kopya silinir, yükleyici boşalır.

    Çıktı klasörüne (output/<ad>) dokunulmaz. İş koşarken çağrılmaz: koşan
    iş kitabın geçici kopyasını kullanıyordur.
    """
    job = _job()
    with _start_lock():
        if _busy_now():
            return
        discard_upload(job["book"])
        job.update(book=None, upload_key=None, run=None,
                   upload_nonce=(job.get("upload_nonce") or 0) + 1)
    st.session_state.pop(PENDING_EXTRACT_KEY, None)
    st.rerun()


@st.cache_data
def _book_glossary_rows(path: str, mtime_ns: int) -> list:
    """Kitap sözlüğünün ``(kaynak, Türkçe)`` satırları (dosya değişince yenilenir)."""
    from latex_pipeline.kitap_sozlugu import tsv_oku

    return [(g.kaynak, g.hedef) for g in tsv_oku(path)]


def _filter_terms(rows: list, query: str) -> list:
    """Kaynak ya da Türkçe tarafta sorgu geçen satırlar (Türkçe harf duyarlı)."""
    from latex_pipeline.kitap_sozlugu import turkce_kucuk as _tr_kucuk

    q = _tr_kucuk((query or "").strip())
    if not q:
        return list(rows)
    return [(k, v) for k, v in rows if q in _tr_kucuk(k) or q in _tr_kucuk(v)]


def _closest_term(rows: list, query: str):
    """Eşleşme yokken en yakın satır: ``(kaynak, Türkçe, benzerlik)`` ya da None."""
    from rapidfuzz import fuzz, process

    from latex_pipeline.kitap_sozlugu import turkce_kucuk as _tr_kucuk

    q = _tr_kucuk((query or "").strip())
    if not q or not rows:
        return None
    choices = {}
    for k, v in rows:
        choices.setdefault(_tr_kucuk(k), (k, v))
        choices.setdefault(_tr_kucuk(v), (k, v))
    best = process.extractOne(q, list(choices), scorer=fuzz.ratio)
    if not best or best[1] < TERMS_SUGGEST_MIN_SCORE:
        return None
    k, v = choices[best[0]]
    return k, v, best[1]


@st.cache_data
def _page_count(path: str) -> int | None:
    try:
        import fitz

        with fitz.open(path) as doc:
            return doc.page_count
    except Exception:
        return None


def _duration(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} sa {minutes:02d} dk"
    return f"{minutes} dk {sec:02d} sn"


def _start(kind: str, command: list[str], book: Path, name: str | None,
           engine: str | None = None, source_lang: str | None = None,
           warning: str | None = None) -> None:
    """İşi başlat; iş yuvasında koşan iş varsa BAŞLATMA (F5-01, 2026-09-29).

    Düğmelerin kapalı değeri çizim anında hesaplanır ve Streamlit o çizimde
    kapalı görünmeyen düğmenin tıklamasını iletir: ikinci sekmeden gelen
    tıklama ikinci CLI sürecini başlatıyor, ilk iş yuvadan düşüyordu (İptal
    ona ulaşmıyordu). Denetim ve başlatma aynı kilit altında yapılır.

    *warning*: ön kontrolün uyarısı. Başlatmanın hemen ardından gelen
    st.rerun() o anda çizileni siler; uyarı bu yüzden iş yuvasında durur,
    ilerleme kutusu ve (başarısız sonuçta) sonuç onu gösterir.
    """
    st.session_state.pop(PENDING_EXTRACT_KEY, None)
    with _start_lock():
        if _busy_now():
            st.session_state[JOB_BUSY_KEY] = True
        else:
            run = Run(kind, command)
            run.start()
            _job().update(run=run, run_book=book, run_name=name, run_engine=engine,
                          run_source_lang=source_lang, run_warning=warning or None)
    st.rerun()


def _request_extract(book: Path, name: str | None, *, fresh: bool) -> None:
    """TeX çıkarmayı başlat; klasörde EXPORT_TR.tex varsa önce onay iste.

    1_cikar EXPORT.tex'i yeniden yazar. Çevrilmiş EXPORT_TR.tex eski
    EXPORT.tex'ten çevrildiği için bu, kullanıcıya sorulmadan yapılmaz.
    """
    files = tex_files(book, name)
    if files.export_tr_tex.exists():
        st.session_state[PENDING_EXTRACT_KEY] = {
            "output_dir": str(files.output_dir), "fresh": fresh}
        st.rerun()
    _start("extract", extract_command(book, name, fresh=fresh), book, name)



# ---------------------------------------------------------------------------
# Model seçimi: tarama raporu ve seçili model (TASARIM F.2, F.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EngineChoice:
    """Bu çizimde model seçicinin durumu (tarama raporu ve seçili kimlik)."""

    report: DiscoveryReport | None
    error: str | None              # tarama çöktüyse nedeni
    options: list[str]             # seçicideki kimlikler, rapor sırasıyla
    labels: dict[str, str]         # kimlik -> seçenek etiketi
    engine: str                    # seçili kimlik
    missing: str | None            # son taramada bulunamayan kayıtlı seçim


def _scan_models() -> None:
    """Modelleri tara; sonuç süreç düzeyinde iş yuvasında tutulur.

    Tarama çökerse arayüz düşmez: neden yuvaya yazılır, kartta gösterilir
    (model uydurulmaz; varsayılan motor seçilebilir kalır).
    """
    job = _job()
    with st.spinner(MODEL_SCAN_SPINNER):
        try:
            report = discover_models()
        except Exception as exc:
            job.update(discovery=None, discovery_error=str(exc) or type(exc).__name__)
            return
    job.update(discovery=report, discovery_error=None)


def _ensure_scan() -> None:
    """İlk açılışta bir kez tara (F.2); sonra yalnız "Modelleri tara" ile.

    Streamlit her yeniden çizimde buraya gelir; rapor (ya da tarama hatası)
    yuvada varsa ağa gidilmez. İş koşarken taranmaz.
    """
    job = _job()
    if job.get("discovery") is not None or job.get("discovery_error") or _busy():
        return
    if not hardware_gate().allowed:
        return      # donanım kapısı kapalı: model aranmaz (kart kapalı)
    _scan_models()


def _option_label(model) -> str:
    """Seçenek etiketi: ad · kaynak · [önerilen ·] düşünme sınıfı · ayrıntı · boyut …

    Taban etiket keşfinkidir (runner.model_option_label: "ad · kaynak ·
    (ayrıntı) · (boyut) [· sığmaz / kullanılamaz]"). Önerilen işareti
    (KARARLAR 29: registry'nin varsayılan motoru, HY-MT 1.5 7B; bulunduğunda
    varsayılan seçim de odur, _select_engine) ve düşünme sınıfı (G2b-1; keşfin
    gerçek üst verisi) adla kaynağın hemen ardına girer: açılır listede satırın
    sonu kesilse de görünür kalırlar. Taban etiket bu biçimde değilse
    eklemeler sona yazılır.
    """
    base = model_option_label(model)
    extras = [branding.MODEL_RECOMMENDED_LABEL] if model.engine_id == DEFAULT_ENGINE else []
    extras.append(branding.thinking_label(profile_of(model).cls))
    head = " · ".join((model.label or model.engine_id,
                       branding.SOURCE_LABELS.get(model.source, model.source)))
    if base == head or base.startswith(head + " · "):
        return " · ".join((head, *extras)) + base[len(head):]
    return " · ".join((base, *extras))


def _engine_options(report: DiscoveryReport | None) -> tuple[list[str], dict[str, str]]:
    """Seçicinin kimlikleri ve etiketleri: yalnız son taramada bulunan modeller.

    Keşif yerleşik modeli her zaman listeler; tarama çöktüyse (rapor yok)
    varsayılan motor yine seçilebilir, hazır olup olmadığına model klasörü
    karar verir (runner.engine_ready). O durumda model bilgisi olmadığı için
    düşünme sınıfı yazılmaz (uydurulmaz), yalnız "önerilen" işareti kalır.
    Aynı etiketi taşıyan iki modelin etiketine kimlik eklenir: seçici
    seçeneği etiketinden tanır.
    """
    options: list[str] = []
    labels: dict[str, str] = {}
    for model in (report.models if report is not None else ()):
        if model.engine_id not in labels:
            options.append(model.engine_id)
            labels[model.engine_id] = _option_label(model)
    if DEFAULT_ENGINE not in labels:
        options.insert(0, DEFAULT_ENGINE)
        labels[DEFAULT_ENGINE] = " · ".join((DEFAULT_ENGINE,
                                             branding.SOURCE_LABELS[SOURCE_BUILTIN],
                                             branding.MODEL_RECOMMENDED_LABEL))
    counts = Counter(labels.values())
    for engine_id in options:
        if counts[labels[engine_id]] > 1:
            labels[engine_id] = f"{labels[engine_id]} · {engine_id}"
    return options, labels


def _select_engine(options: list[str]) -> tuple[str, str | None]:
    """Seçili kimlik ve (varsa) son taramada bulunamayan kayıtlı seçim.

    Açılışta sıra: iş yuvasındaki seçim -> config/mt_selection.json ->
    varsayılan motor (F.3). Kayıtlı seçim taramada yoksa varsayılan motor
    seçilir ve dosya ezilmez; seçim yeni bir taramada yine bulunursa (ör.
    sunucu açıldı) kullanıcı bu arada başka model seçmediyse geri gelir.
    """
    job = _job()
    current = st.session_state.get(ENGINE_KEY)
    missing = job.get("engine_missing")
    if missing and missing in options and current in (None, DEFAULT_ENGINE):
        chosen = missing
    elif current in options:
        return current, missing
    else:
        chosen = current or job.get("engine") or load_engine_choice() or DEFAULT_ENGINE
    if chosen in options:
        job.update(engine=chosen, engine_missing=None)
    else:
        job["engine_missing"] = chosen
        chosen = DEFAULT_ENGINE
    st.session_state[ENGINE_KEY] = chosen
    return chosen, job.get("engine_missing")


def _engine_choice() -> EngineChoice:
    job = _job()
    report = job.get("discovery")
    options, labels = _engine_options(report)
    engine, missing = _select_engine(options)
    return EngineChoice(report=report, error=job.get("discovery_error"),
                        options=options, labels=labels, engine=engine, missing=missing)


def _engine_changed() -> None:
    """Seçici değişti: iş yuvasına ve config/mt_selection.json'a yaz."""
    engine_id = st.session_state.get(ENGINE_KEY)
    if not engine_id:
        return
    _job().update(engine=engine_id, engine_missing=None)
    if save_engine_choice(engine_id):
        st.session_state.pop(ENGINE_SAVE_FAILED_KEY, None)
    else:
        st.session_state[ENGINE_SAVE_FAILED_KEY] = str(engine_choice_path())


# ---------------------------------------------------------------------------
# Kenar çubuğu
# ---------------------------------------------------------------------------

def _sidebar(choice: EngineChoice) -> str | None:
    job = _job()
    # Yeni oturum (sayfa yenilendi): alan iş yuvasındaki addan dolar. Yoksa
    # alan boşalır, kartlar dosya adından türeyen BAŞKA bir klasöre bakardı.
    if NAME_KEY not in st.session_state:
        st.session_state[NAME_KEY] = job.get("name") or ""
    with st.sidebar:
        st.markdown(
            f'<div class="logo"><div class="logo-text">◎ {branding.APP_NAME}</div>'
            f'<div class="logo-sub" lang="tr">{branding.APP_TAGLINE}</div></div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="section-header" lang="tr">'
                    f'{branding.BOOK_NAME_LABEL}</div>', unsafe_allow_html=True)
        name = st.text_input(
            branding.BOOK_NAME_LABEL,
            key=NAME_KEY,
            label_visibility="collapsed",
            placeholder="Dosya adından çıkarılır",
            help="Çıktı klasörünün adı. Aynı adda başka bir kitap varsa ayırmak için kullan.",
            disabled=_busy(),
        )
        st.markdown('<div class="section-header" lang="tr">Sistem</div>',
                    unsafe_allow_html=True)
        # Gerçek ölçüm (core/hardware.py): çip ve yerel model için bellek.
        st.caption(SIDEBAR_SYSTEM_CAPTION.format(hardware=hardware_caption()))
        # Ağ çağrısı yok: son taramanın sonucu ve varsayılan modelin klasörü.
        if not hardware_gate().allowed:
            st.caption(SIDEBAR_MODEL_CLOSED_CAPTION)
        else:
            ready, _reason = engine_ready(choice.engine, choice.report)
            state = branding.MODEL_READY_LABEL if ready else branding.MODEL_UNUSABLE_LABEL
            st.caption(SIDEBAR_MODEL_CAPTION.format(
                label=engine_label(choice.engine, choice.report), state=state))
        manager = _manager_url()
        if manager:
            # Yönetici sayfası bu pencerede açılır: kabuk yeni pencere
            # isteklerini varsayılan tarayıcıya gönderir, anahtarlı adres
            # oraya gitmesin. Sayfadaki "Kitapçevir'i aç" buraya döndürür.
            st.markdown(_sidebar_link(manager, branding.COMPONENTS_SCREEN, "_self"),
                        unsafe_allow_html=True)
        _about()
    cleaned = name.strip() or None
    job["name"] = cleaned
    return cleaned


def _manager_url() -> str | None:
    """Yönetici "Bileşenler" ekranının adresi (TASARIM B.8: KITAPCEVIR_YONETICI_URL).

    Değişken yoksa (geliştirici kipi) ya da adres bu bilgisayarın kendisini
    göstermiyorsa None: bağlantı çizilmez.
    """
    url = os.environ.get(YONETICI_URL_DEGISKENI, "").strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme != MANAGER_URL_SCHEME or not is_loopback_host(host):
        return None
    return url


def _sidebar_link(url: str, label: str, target: str) -> str:
    """Kenar çubuğunda tek satırlık bağlantı (adres ve ad HTML kaçışlı)."""
    rel = ' rel="noopener noreferrer"' if target == "_blank" else ""
    return (f'<div class="sidebar-link" lang="tr"><a href="{html.escape(url)}" '
            f'target="{target}"{rel}>{html.escape(label)}</a></div>')


def _about() -> None:
    """"Hakkında" bölümü: sağlayıcı ve Tencent'le bağlantısızlık beyanı (HY
    lisansı m.3(e)) ile "Kaynak kodu" bağlantısı (AGPL m.13). Her kipte."""
    st.markdown(f'<div class="section-header" lang="tr">{branding.ABOUT_LABEL}</div>',
                unsafe_allow_html=True)
    st.markdown(f'<div class="about-text" lang="tr">{html.escape(branding.ABOUT_TEXT)}'
                '</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="about-text" lang="tr">'
                f'{html.escape(branding.ABOUT_LICENSE_TEXT)}</div>', unsafe_allow_html=True)
    st.markdown(_sidebar_link(branding.SOURCE_CODE_URL, branding.SOURCE_CODE_LABEL,
                              "_blank"), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Çeviri sütunu
# ---------------------------------------------------------------------------

def _file_card(book: Path, name: str | None) -> None:
    # Boyut Türkçe ondalıkla (U16) ve KB/MB'ye uyarlı (U15): 73 KB'lık EPUB
    # "0,1 MB" yazıyordu.
    meta = [book.suffix[1:].upper(), format_size(book.stat().st_size)]
    if book.suffix.lower() == ".pdf":
        pages = _page_count(str(book))
        if pages:
            meta.append(f"{pages} sayfa")
    # Kartların baktığı klasör: kitap adı alanı (kenar çubuğu) değiştirir.
    folder = f"Çıktı klasörü: output/{book_slug(book, name)}"
    st.markdown(
        f'<div class="file-card"><div class="file-name">{html.escape(book.name)}</div>'
        f'<div class="file-meta">{" · ".join(meta)}</div>'
        f'<div class="file-meta">{html.escape(folder)}</div></div>',
        unsafe_allow_html=True,
    )


def _path_header(title: str, description: str, badge: str | None = None) -> None:
    """Kartın adı ve açıklaması; *badge* (ör. "Beta") adın yanında küçük etiket."""
    tag = f'<span class="path-badge" lang="tr">{badge}</span>' if badge else ""
    st.markdown(
        f'<div class="path-title">{title}{tag}</div>'
        f'<div class="path-desc">{description}</div>',
        unsafe_allow_html=True,
    )


def _scan_time(scanned_at: float) -> str:
    """Taramanın gerçek zamanı: bugünse "14:32", değilse tarihiyle."""
    moment = datetime.fromtimestamp(scanned_at)
    if moment.date() == datetime.now().date():
        return moment.strftime("%H:%M")
    return moment.strftime("%d.%m.%Y %H:%M")


def _source_line(status: SourceStatus) -> str:
    """Kaynak durum satırı: "LM Studio: 2 model"; ileti kaynak adıyla
    başlıyorsa ad yinelenmez ("Ollama sunucusu kapalı …")."""
    if status.message.startswith(status.label):
        return status.message
    return f"{status.label}: {status.message}"


def _scan_status(choice: EngineChoice) -> None:
    """Son tarama zamanı ve kaynak durumları. Hepsi st.caption: uyarı/hata
    listeleri bu satırlarla karışmaz (kaynak kapalı olması bir hata değil)."""
    if choice.report is not None:
        st.caption(MODEL_LAST_SCAN.format(time=_scan_time(choice.report.scanned_at)))
        for status in choice.report.sources:
            st.caption(_source_line(status))
    elif choice.error:
        st.caption(MODEL_SCAN_FAILED.format(error=choice.error))


def _translate(book: Path, name: str | None, engine: str,
               measured: SourceLanguage | None = None,
               chosen: str | None = None) -> None:
    """"Çevir": son kontrol (modeli yüklemez, en çok birkaç saniye), sonra iş.

    Kaynak dil belirsizken seçim yapılmadıysa iş başlamaz (KARARLAR 19;
    bayat sekmeden gelen tıklamada düğme açık görünmüş olabilir).
    """
    if measured is not None and measured.needs_choice and not chosen:
        st.error(SOURCE_LANG_CHOICE_REQUIRED)
        return
    source_lang = forced_source_lang(measured, chosen) if measured is not None else None
    try:
        with st.spinner(MODEL_CHECK_SPINNER):
            check = engine_check(engine)
    except Exception as exc:
        st.error(MODEL_CHECK_FAILED.format(error=exc))
        return
    if not check.ok:
        st.error(check.reason or branding.MODEL_UNAVAILABLE_MESSAGE)
        hint = _install_hint(engine, check.hint)
        if hint:
            st.caption(hint)
        return
    # Olumlu kontrolün uyarısı (WarnedEngineCheck; ör. sunucu modelinin kaba
    # bellek tahmini, MT-12): iş yuvasında durur, koşu boyunca görünür.
    _start("translate",
           translate_command(book, name, engine=engine, source_lang=source_lang),
           book, name, engine=engine, source_lang=source_lang,
           warning=getattr(check, "warning", "") or None)


def _install_hint(engine: str, hint: str) -> str:
    """Ön kontrolün kurulum ipucu (K5 açık kalanı 3, TASARIM F5).

    Yerleşik model eksikken keşif ``mlx_lm.convert`` komutunu önerir; paketli
    kurulumda model "Bileşenler" ekranından indirilir. Kullanıcının kendi
    kaydettiği modelin ipucu değişmez (Bileşenler yalnız HY-MT'yi kurar).
    """
    if hint and engine == DEFAULT_ENGINE and paketli_kip():
        return branding.MODEL_INSTALL_HINT_PACKAGED
    return hint


def _run_warning(run: Run) -> None:
    """Çevirinin ön kontrol uyarısı (iş yuvasındaki; yalnız çeviri işinde)."""
    warning = _job().get("run_warning")
    if run.kind == "translate" and warning:
        st.warning(warning)


def _percent0(value: float) -> str:
    """0-1 oranı ekranda tam sayı yüzde: 0,97 -> "%97"."""
    return format_percent(value * 100, 0)


def _source_language_caption(measured: SourceLanguage) -> str:
    """Kaynak dil ölçümünün kart satırı (gerçek ölçüm; uydurma yok)."""
    if measured.error:
        return SOURCE_LANG_FAILED.format(error=measured.error)
    if not measured.measured:
        return SOURCE_LANG_UNMEASURED
    name = language_name_tr(measured.code or "und")
    confidence = _percent0(measured.confidence)
    if measured.confident:
        return SOURCE_LANG_DETECTED.format(name=name, confidence=confidence)
    if measured.code in (None, "und"):
        return SOURCE_LANG_UNDETERMINED.format(confidence=confidence)
    if not measured.supported and measured.confidence >= GATE_STOP_CONFIDENCE:
        return SOURCE_LANG_UNSUPPORTED.format(name=name, confidence=confidence)
    return SOURCE_LANG_UNCERTAIN.format(name=name, confidence=confidence,
                                        threshold=_percent0(GATE_STOP_CONFIDENCE))


def _source_language_key(book: Path) -> str:
    """Seçicinin oturum anahtarı: yüklenen kitabın geçici yolundan türer."""
    digest = hashlib.sha1(str(book).encode("utf-8")).hexdigest()[:12]
    return f"{SOURCE_LANG_KEY}_{digest}"


def _source_language(book: Path, busy: bool) -> tuple[SourceLanguage, str | None]:
    """Kaynak dil satırı ve seçici (KARARLAR 19): ölçüm ve kullanıcının seçimi.

    Tespit güvenliyse seçici o dille dolu gelir; belirsiz ya da
    desteklenmiyorsa boş gelir ve "Çevir" seçim yapılana dek kapalıdır.
    """
    measured = measure_source_language(book)
    options = list(TEX_SOURCE_LANGS)
    index = options.index(measured.code) if measured.confident else None
    chosen = st.selectbox(SOURCE_LANG_LABEL, options, index=index,
                          format_func=language_name_tr,
                          placeholder=SOURCE_LANG_PLACEHOLDER,
                          help=SOURCE_LANG_HELP, key=_source_language_key(book),
                          disabled=busy)
    st.caption(_source_language_caption(measured))
    return measured, chosen


def _model_card_closed(reason: str) -> None:
    """Donanım kapısı kapalı (KARARLAR 17): kart açıklaması, gerçek ölçümlü
    neden ve TeX yolu önerisi; model seçici ve tarama yok, "Çevir" kapalı."""
    st.warning(reason)
    st.caption(branding.MODEL_HW_TEX_HINT)
    st.button("Çevir", key="model_translate", type="primary", disabled=True,
              width="stretch")


def _capitalized(text: str) -> str:
    """Cümle başı büyük harf, Türkçe kuralıyla ("i" -> "İ", "ı" -> "I")."""
    if not text:
        return text
    first = {"i": "İ", "ı": "I"}.get(text[0], text[0].upper())
    return first + text[1:]


def _thinking_captions(model) -> None:
    """Seçili modelin düşünme sınıfı (G2b-1) ve gerekiyorsa düşünerek çeviri
    notu (G2b-2, KARARLAR 22). Yalnız keşfin gerçek üst verisinden: model
    taramada yoksa hiçbir şey yazılmaz."""
    if model is None:
        return
    cls = profile_of(model).cls
    if cls == THINKING_UNKNOWN and model.reasoning is True:
        st.caption(MODEL_REASONING_CAPTION)
        return
    st.caption(MODEL_THINKING_CAPTION.format(label=branding.thinking_label(cls)))
    if cls == THINKING_ALWAYS:
        st.caption(_capitalized(branding.THINKING_MODE_NOTE))


def _model_card(book: Path, name: str | None, busy: bool, conflict: bool,
                choice: EngineChoice) -> None:
    with st.container(border=True):
        # Beta (KARARLAR 23): kartın adı değişmez; etiket ve kısa not ayrı.
        _path_header(branding.MODEL_TITLE, branding.MODEL_DESCRIPTION,
                     badge=branding.MODEL_BETA_LABEL)
        st.caption(branding.MODEL_BETA_NOTE)
        gate = hardware_gate()
        if not gate.allowed:
            _model_card_closed(gate.reason)
            return
        if gate.warning:
            st.caption(gate.warning)
        # İş koşarken seçici ve tarama kapalı (koşan işin modeli değişmez).
        st.selectbox(branding.MODEL_PICKER_LABEL, choice.options,
                     format_func=lambda engine_id: choice.labels.get(engine_id, engine_id),
                     key=ENGINE_KEY, on_change=_engine_changed, disabled=busy)
        # Açıklamada keşfin taradığı klasörün gerçek yolu (veri kökünden; F1).
        if st.button(branding.MODEL_SCAN_BUTTON, key="model_scan",
                     help=branding.model_scan_help(registry.MODELS_DIR),
                     disabled=busy, width="stretch"):
            _scan_models()
            st.rerun()
        _scan_status(choice)
        if choice.missing:
            st.caption(MODEL_CHOICE_MISSING_NOTE.format(engine_id=choice.missing))
        save_failed = st.session_state.get(ENGINE_SAVE_FAILED_KEY)
        if save_failed:
            st.caption(MODEL_CHOICE_SAVE_FAILED.format(path=save_failed))
        model = (choice.report.model_by_id(choice.engine)
                 if choice.report is not None else None)
        _thinking_captions(model)
        ready, reason = engine_ready(choice.engine, choice.report)
        # Klasörde TeX yolunun çevirisi varsa CLI yerel modeli çalıştırmaz
        # (çıkış 2); düğme baştan kapalı, nedeni yazılı.
        locked = not conflict and manual_lock(book, name)
        measured, chosen = _source_language(book, busy)
        language_missing = measured.needs_choice and not chosen
        forced = forced_source_lang(measured, chosen)
        if st.button("Çevir", key="model_translate", type="primary",
                     disabled=(busy or not ready or conflict or locked
                               or language_missing),
                     width="stretch"):
            _translate(book, name, choice.engine, measured, chosen)
        if not ready:
            st.caption(reason)
        elif locked:
            st.caption(MANUAL_LOCK_NOTE)
        elif language_missing:
            st.caption(SOURCE_LANG_CHOICE_REQUIRED)
        else:
            if forced:
                st.caption(SOURCE_LANG_FORCED_NOTE.format(name=language_name_tr(forced)))
            note = memory_batch_note(choice.engine, choice.report)
            if note:
                st.caption(note)


def _extract_confirmation(book: Path, name: str | None, files: TexFiles) -> None:
    """EXPORT_TR.tex varken istenen yeniden çıkarma için onay alanı."""
    pending = st.session_state.get(PENDING_EXTRACT_KEY)
    if not pending:
        return
    if (pending.get("output_dir") != str(files.output_dir)
            or not files.export_tr_tex.exists()):
        # Kitap adı değişti ya da çeviri dosyası kalktı: istek bayatladı.
        st.session_state.pop(PENDING_EXTRACT_KEY, None)
        return
    fresh = bool(pending.get("fresh"))
    st.warning(EXPORT_TR_OVERWRITE_WARNING)
    label = "Yine de sıfırdan çıkar" if fresh else "Yine de yeniden çıkar"
    if st.button(label, key="tex_extract_confirm", type="primary", width="stretch"):
        _start("extract", extract_command(book, name, fresh=fresh), book, name)
    if st.button("Vazgeç", key="tex_extract_cancel", width="stretch"):
        st.session_state.pop(PENDING_EXTRACT_KEY, None)
        st.rerun()


def _tex_paths(files: TexFiles) -> None:
    """EXPORT.tex'in ve kitaba özel talimatın mutlak yolları, hazır istem."""
    st.caption("Çevrilecek dosya:")
    st.code(str(files.export_tex), language=None, wrap_lines=True)
    if files.directive.exists():
        st.caption("Bu kitabın çeviri talimatı:")
        st.code(str(files.directive), language=None, wrap_lines=True)
        st.caption("Claude Code'a şunu yaz:")
        st.code(assistant_prompt(files.directive), language=None, wrap_lines=True)


def _tex_pdf(files: TexFiles, translated: bool) -> None:
    """Klasörde derlenmiş Türkçe PDF varsa göster ve açtır."""
    pdf = files.translated_pdf
    try:
        pdf_mtime = pdf.stat().st_mtime if pdf.is_file() else None
        tr_mtime = files.export_tr_tex.stat().st_mtime if translated else None
    except OSError:                     # dosya tam bu arada silindi/taşındı
        return
    if pdf_mtime is None:
        return
    stamp = datetime.fromtimestamp(pdf_mtime).strftime("%d.%m.%Y %H:%M")
    st.caption(f"Derlenmiş PDF: {pdf.name} ({stamp})")
    if tr_mtime is not None and tr_mtime > pdf_mtime:
        st.caption("EXPORT_TR.tex bu PDF'den sonra değişti; yeniden derleyebilirsin.")
    if st.button("PDF'yi aç", key="tex_pdf_open", width="stretch"):
        open_path(pdf)


@st.fragment(run_every=FILE_CHECK_SEC)
def _tex_card(book: Path, name: str | None) -> None:
    busy = _busy()
    files = tex_files(book, name)
    # Aynı adla başka kitap: klasördeki dosyalar bu kitabın değil (uyarı
    # kartların üstünde); ne çıkarma ne derleme açılır.
    conflict = name_conflict(book, name)
    translated = not conflict and files.export_tr_tex.exists()
    with st.container(border=True):
        _path_header(branding.TEX_TITLE, branding.TEX_DESCRIPTION)
        if st.button(branding.TEX_EXTRACT_BUTTON, key="tex_extract", type="primary",
                     disabled=busy or conflict, width="stretch"):
            _request_extract(book, name, fresh=False)
        if not busy and not conflict:
            _extract_confirmation(book, name, files)
        if st.button(branding.TEX_COMPILE_BUTTON, key="tex_compile", type="primary",
                     disabled=busy or not translated, width="stretch"):
            _start("compile", compile_command(files.export_tr_tex), book, name)
        if conflict or not files.export_tex.exists():
            return
        # Çıkarımda okunamayan sayfalar (F1-03): kart durdukça görünür.
        _failed_pages_warning(failed_pages(files.output_dir))
        if translated and untranslated_copy(files.export_tex, files.export_tr_tex):
            # EXPORT.tex yeni adla kaydedilmiş ama çevrilmemiş (U7). Derleme
            # açık kalır (bilerek denenebilir); yollar yine göz önünde.
            st.warning(UNTRANSLATED_COPY_WARNING)
            _tex_paths(files)
        elif translated:
            st.caption("EXPORT_TR.tex hazır; derleyebilirsin.")
            with st.expander("Dosya yolları ve Claude Code istemi"):
                _tex_paths(files)
        else:
            st.caption("EXPORT.tex hazır. Türkçeye çevirip aynı klasöre "
                       "EXPORT_TR.tex adıyla kaydet.")
            _tex_paths(files)
        _tex_pdf(files, translated)
        if st.button("Klasörü aç", key="tex_open", width="stretch"):
            open_path(files.output_dir)


@st.fragment(run_every=REFRESH_SEC)
def _progress() -> None:
    """Koşan işin kutusu: kartların ÜSTÜNDE, İptal başlık satırında (U9).

    Kartların altındayken TeX kartı uzayınca (yollar, istem) kutu ve İptal
    ekranın dışına düşüyordu.
    """
    run: Run | None = _job()["run"]
    if run is None or not run.running:
        return
    run.poll()
    if not run.running:
        st.rerun()                      # bitti: sonucu tüm sayfada göster
    with st.container(border=True):
        title, action = st.columns([3, 1], vertical_alignment="center")
        title.markdown(f"**{JOB_LABELS[run.kind]}**")
        if action.button("İptal", key="job_cancel", width="stretch"):
            run.cancel()
            st.rerun()
        job = _job()
        if run.kind == "translate" and job.get("run_engine"):
            st.caption(MODEL_RUN_CAPTION.format(
                label=engine_label(job["run_engine"], job.get("discovery"))))
        _run_warning(run)
        if run.kind == "translate" and job.get("run_source_lang"):
            st.caption(SOURCE_LANG_RUN_CAPTION.format(
                name=language_name_tr(job["run_source_lang"])))
        if run.phase_title:
            # Faz numarasının Türkçe adı (F5-08); günlüğün İngilizce metni değil.
            title = phase_label(run.phase_number, run.phase_title)
            st.caption(f"Faz {run.phase_number} · {title}")
        ratio = run.percent()
        pages = run.progress_unit == "page" and run.progress_from is not None
        if ratio is not None and pages:
            # Parti başında basılan sayaç: a-b işleniyor, a sayfa bitti.
            st.progress(ratio, text=f"Sayfa {run.progress_from + 1}–"
                                    f"{run.progress_current + 1} / "
                                    f"{run.progress_total} işleniyor")
        elif ratio is not None:
            unit = UNIT_LABELS.get(run.progress_unit, run.progress_unit)
            st.progress(ratio, text=f"{run.progress_current}/{run.progress_total} {unit}")
        elif run.progress_unit == "page" and run.progress_current is not None:
            first = (run.progress_from or 0) + 1
            st.caption(f"Sayfa {first}–{run.progress_current + 1} işleniyor")
        st.caption(f"Geçen süre: {_duration(run.elapsed())}")


def _output_tail(run: Run) -> None:
    tail = run.tail()
    if tail:
        with st.expander("Son çıktı"):
            st.code("\n".join(tail), language=None)


def _failure_text(message: str, code: str | None, returncode: int | None) -> str:
    """Hata cümlesi: Türkçe ileti, ham kod ve çıkış kodu parantez içinde.

    "PDF üretilemedi (çıkış kodu 2)." -- nokta parantezden sonra (U16).
    """
    details = [code] if code else []
    if returncode:
        details.append(f"çıkış kodu {returncode}")
    return f"{message} ({', '.join(details)})." if details else f"{message}."


def _log_hint(result: dict) -> None:
    """Kitap klasöründeki ayrıntılı günlüğün yolu (yalnız gerçekten varsa)."""
    output_dir = result.get("output_dir")
    log = Path(output_dir) / PIPELINE_LOG_NAME if output_dir else None
    if log is not None and log.is_file():
        st.caption(LOG_HINT_LABEL)
        st.code(str(log), language=None, wrap_lines=True)


def _failure(run: Run, message: str, code: str | None = None,
             result: dict | None = None) -> None:
    st.error(_failure_text(message, code, run.returncode))
    if run.last_error:
        st.caption(run.last_error)
    if result is not None:
        _log_hint(result)
    _output_tail(run)


def _language_gate_failure(run: Run) -> None:
    """Yerel model koşusu kaynak dil kapısında durdu: kitabın dili desteklenmiyor.

    2026-09-28 (KARARLAR 14): yerel model TeX yoluyla aynı kaynak dilleri
    çevirir (core.lang_detect.TEX_SOURCE_LANGS); çıkış 4 artık "desteklenmeyen
    dil" demektir, TeX kartı çözüm olarak önerilmez (dil listesi aynı).
    Tespit yanlışsa çözüm kartın kaynak dil seçicisidir (KARARLAR 19; arayüz
    ``--kaynak-dil`` geçer); CLI bayrakları ikincil bilgi olarak anılır.
    """
    st.error("Yerel model bu kitabı çevirmedi: kitabın dili desteklenmiyor "
             f"(kaynak dil kapısı, çıkış kodu {run.returncode}).")
    if run.last_error:
        st.caption(run.last_error)
    st.info("Desteklenen kaynak diller: "
            f"{language_list_tr(TEX_SOURCE_LANGS, ' ve ')}. Hedef dil her "
            "zaman Türkçe.")
    st.caption("Kitabın dili aslında destekleniyorsa ve tespit yanlışsa, "
               f"{branding.quoted(branding.MODEL_TITLE)} kartında kaynak dili "
               "elle seçip yeniden çevir. Komut satırında dili `--kaynak-dil` "
               "ile verebilir ya da kapıyı `--dil-kontrolu-yok` ile "
               "atlayabilirsin.")
    _output_tail(run)


def _model_unavailable_failure(run: Run) -> None:
    """Çıkış 5: seçili model kullanılamadı (sunucu kapalı, model yok…).

    Nedeni CLI yazar ("HATA: …" satırı, çıktı kuyruğunda); arayüz
    kullanıcıyı model listesini yenilemeye yönlendirir.
    """
    st.error(branding.MODEL_UNAVAILABLE_MESSAGE)
    if run.last_error:
        st.caption(run.last_error)
    _output_tail(run)


def _pdf_actions(pdf: Path) -> None:
    left, right = st.columns(2)
    if left.button("PDF'yi aç", key="pdf_open", type="primary", width="stretch"):
        open_path(pdf)
    right.download_button("İndir", data=pdf.read_bytes(), file_name=pdf.name,
                          mime="application/pdf", key="pdf_download", width="stretch")
    st.caption(str(pdf))


def _epub_actions(result: dict, since: float | None = None) -> None:
    """Türkçe EPUB (S4 "epub") ya da üretilmeme nedeni (U8).

    Kapsam 1'in altındaysa (S7) 2_derle'nin CLI uyarısı aynen gösterilir
    (F5-02); blok sayıları bu koşuda yazılmış epub_rapor.json'dan.
    """
    epub = Path(result["epub"]) if result.get("epub") else None
    if epub is not None and epub.is_file():
        output_dir = result.get("output_dir")
        report = epub_report(Path(output_dir), since) if output_dir else None
        coverage = epub_coverage_text(result.get("epub_kapsam"), report)
        if coverage:
            st.warning(coverage)
        st.download_button(EPUB_DOWNLOAD_LABEL, data=epub.read_bytes(),
                           file_name=epub.name, mime="application/epub+zip",
                           key="epub_download", width="stretch")
        st.caption(str(epub))
        return
    note = result.get("epub_note")
    if isinstance(note, str) and note.strip():
        st.caption(f"{EPUB_SKIPPED_LABEL}: {note.strip()}")


def _qc_score(result: dict) -> str | None:
    """S4 "qc_score" ekranda "62/100" biçiminde; yoksa None (uydurulmaz)."""
    score = result.get("qc_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return f"{format_decimal(score)}/100"


def _qc_details(result: dict) -> None:
    """QC eşiği altı (U7): en çok bulgu veren denetimler ve rapor dosyası."""
    top = [item for item in result.get("qc_top") or []
           if isinstance(item, dict) and isinstance(item.get("check"), str)
           and isinstance(item.get("count"), int)]
    if top:
        listed = ", ".join(f"{branding.qc_check_label(item['check'])} "
                           f"({item['count']})" for item in top)
        st.caption(f"{QC_TOP_LABEL}: {listed}")
    report = Path(result["qc_report"]) if result.get("qc_report") else None
    if report is not None and report.is_file():
        st.caption(QC_REPORT_LABEL)
        st.code(str(report), language=None, wrap_lines=True)
        if st.button("Raporu aç", key="qc_report_open"):
            open_path(report)


def _compile_warnings(details: dict, since: float | None) -> None:
    """Derleme günlüğündeki TeX hatası / eksik glif (F5-02, R5 alanları) ve
    çeviride onarılan kapanmamış '{' (F2-01).

    Çıkış 0'da da gösterilir: CLI bu durumda "UYARI" basar, arayüz yalnız
    "PDF hazır" diyordu. Metinler 2_derle'nin satırları; altında günlükler.
    """
    output_dir = details.get("output_dir")
    issue = compile_issue_text(details)
    if issue:
        st.warning(issue)
        log = compile_log_path(Path(output_dir)) if output_dir else None
        if log is not None and log.is_file():
            st.caption(COMPILE_LOG_LABEL)
            st.code(str(log), language=None, wrap_lines=True)
    braces = brace_fix_text(Path(output_dir), since) if output_dir else None
    if braces:
        st.warning(braces)
        _log_hint(details)


def _failed_pages_warning(pages: list[int]) -> None:
    """Çıkarımda atlanan sayfalar (F1-03): yoksa hiçbir şey yazılmaz."""
    if not pages:
        return
    shown = ", ".join(str(page) for page in pages[:FAILED_PAGES_SHOWN])
    more = len(pages) - FAILED_PAGES_SHOWN
    if more > 0:
        shown += FAILED_PAGES_MORE.format(more=more)
    st.warning(FAILED_PAGES_MESSAGE.format(count=len(pages), pages=shown))


def _truncated_warning(details: dict) -> None:
    """Üretim sınırına dayanan (eksik çevrilmiş olabilecek) parçalar (F3-07).

    Sayı translate_book'un bu koşuda yazdığı listeden (runner.model_outputs);
    yoksa hiçbir şey yazılmaz."""
    count = details.get("kesik_parca")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return
    st.warning(TRUNCATED_MESSAGE.format(count=count))
    st.caption(str(Path(details["output_dir"]) / TRUNCATION_REPORT_NAME))


def _usage_failure(run: Run, message: str) -> None:
    """CLI komut satırını reddetti (F5-06): derleme hatası denmez."""
    st.error(_failure_text(USAGE_ERROR_MESSAGE, None, run.returncode))
    st.caption(message)
    _output_tail(run)


def _extract_result(run: Run, result: dict, name: str | None) -> None:
    """1_cikar koşusunun sonucu (--json özetinin alanlarıyla).

    F1-03 (X kulvarı, 2026-09-29): bazı PDF sayfaları çıkarılamadıysa 1_cikar
    EXPORT.tex'i yine üretir ("ok" true, "failed_pages" dolu) ama sıfırdan
    farklı kodla çıkar; bu bir hata değil, eksikli üründür: sonuç başarı
    olarak gösterilir, eksik sayfalar uyarıyla (kart ya da burada).
    """
    job = _job()
    pages = failed_pages(Path(result["output_dir"]), result) \
        if result.get("output_dir") else []
    partial = bool(result.get("failed_pages")) and result.get("ok") is True
    if not ((run.returncode == 0 or partial) and result.get("ok")):
        code = result.get("error")
        code = code if isinstance(code, str) and code else None
        if code == EXTRACT_ERROR_NAME_CONFLICT:
            st.error(NAME_CONFLICT_MESSAGE)
            _output_tail(run)
        else:
            _failure(run, EXTRACT_ERROR_MESSAGES.get(code, EXTRACT_FAILED_MESSAGE),
                     code, result if code in LOG_HINT_ERRORS else None)
        return
    chapters = result.get("chapters")
    counted = isinstance(chapters, int) and not isinstance(chapters, bool)
    summary = f"{chapters} bölüm, " if counted else ""
    st.success(f"TeX çıkarıldı: {summary}{_duration(run.elapsed())}.")
    # Eksik sayfalar kitap.json'da da varsa TeX kartı gösterir (tek uyarı);
    # yalnız --json'da varsa burada.
    if pages and not failed_pages(Path(result["output_dir"])):
        _failed_pages_warning(pages)
    if counted and chapters <= WEAK_STRUCTURE_MAX_CHAPTERS:
        st.warning(WEAK_STRUCTURE_MESSAGE.format(chapters=chapters))
    if result.get("export_tr_exists") is True:
        changed = result.get("export_changed")
        if changed is False:
            st.info(EXPORT_TR_KEPT_NOTE)
        elif changed is True:
            st.warning(EXPORT_TR_STALE_NOTE)
        else:
            st.warning(EXPORT_TR_UNKNOWN_NOTE)
    if result.get("extraction_reused") is True:
        st.info(EXTRACTION_REUSED_MESSAGE)
        # Düğme yalnız sonuç kartın baktığı klasöre aitse: ad sonradan
        # değiştiyse sıfırdan çıkarma başka bir klasörü hedeflerdi.
        book, run_book = job["book"], job["run_book"]
        same_folder = (book is not None and run_book is not None
                       and tex_files(run_book, job["run_name"]).output_dir
                       == tex_files(book, name).output_dir)
        if same_folder and st.button("Sıfırdan çıkar", key="extract_fresh"):
            _request_extract(book, name, fresh=True)
    export = Path(result["export_tex"]) if result.get("export_tex") else None
    if export is not None and export.exists():
        st.download_button("EXPORT.tex'i indir", data=export.read_bytes(),
                           file_name=export.name, mime="application/x-tex",
                           key="export_download")


def _result(name: str | None) -> None:
    job = _job()
    run: Run | None = job["run"]
    if run is None or run.running:
        return
    if run.cancelled:
        st.warning("İş iptal edildi.")
        return
    result = run.result or {}
    if run.returncode:
        # Çeviri temiz bitmediyse ön kontrolün uyarısı (ör. bellek) sonuçta da
        # durur: başarısızlığın olası nedenidir. Başarılı koşuda gösterilmez.
        _run_warning(run)

    usage = usage_error(run)
    if usage:
        _usage_failure(run, usage)
        return

    if run.kind == "extract":
        _extract_result(run, result, name)
        return

    if run.kind == "translate" and run.returncode == EXIT_SOURCE_UNSUPPORTED:
        _language_gate_failure(run)
        return

    if run.kind == "translate" and run.returncode == EXIT_MODEL_UNAVAILABLE:
        _model_unavailable_failure(run)
        return

    # Çıkış 2 hem derleme hatası hem Yol A kilidi; kilit dosyası ayırır.
    if (run.kind == "translate" and run.returncode == EXIT_MANUAL_LOCK
            and job["run_book"] is not None
            and manual_lock(job["run_book"], job["run_name"])):
        st.error(MANUAL_LOCK_MESSAGE)
        _output_tail(run)
        return

    if run.kind == "compile":
        pdf = Path(result["pdf"]) if result.get("pdf") else None
    else:
        pdf = model_pdf(job["run_book"], job["run_name"])
        # Eski bir koşudan kalan PDF bu koşunun ürünü sayılmaz.
        if pdf.exists() and pdf.stat().st_mtime < (run.started_at or 0):
            pdf = None
    if pdf is None or not pdf.exists():
        code = result.get("error") if run.kind == "compile" else None
        code = code if isinstance(code, str) and code else None
        if code:
            message = COMPILE_ERROR_MESSAGES.get(code, PDF_FAILED_MESSAGE)
        elif run.returncode == EXIT_COMPILE_FAILED:
            message = COMPILE_FAILED_MESSAGE
        else:
            message = PDF_FAILED_MESSAGE
        # Kod yoksa hata pipeline'da (derleme ya da ara adım): günlük yolu.
        # Yerel model JSON basmaz; klasörü kitabın çıktı klasörüdür.
        log_source = result
        if run.kind == "translate" and job["run_book"] is not None:
            log_source = {"output_dir": str(
                tex_files(job["run_book"], job["run_name"]).output_dir)}
        _failure(run, message, code, log_source if code is None else None)
        return
    # Sonucun ayrıntıları 2_derle --json alanlarıyla. Yerel model JSON basmaz:
    # aynı alanlar bu koşunun yazdığı dosyalardan okunur (F5-03); ileride
    # translate_book da JSON basarsa onun alanları önceliklidir.
    if run.kind == "compile":
        details = dict(result)
    else:
        details = {**model_outputs(job["run_book"], job["run_name"], run.started_at),
                   **result}
    score = _qc_score(details)
    if run.returncode == 0:
        st.success(f"PDF hazır ({_duration(run.elapsed())}).")
        if score:
            st.caption(f"Kalite puanı: {score}")
    elif run.returncode == EXIT_QC_BELOW_THRESHOLD:
        suffix = f" (puan {score})" if score else ""
        st.warning(f"{QC_BELOW_THRESHOLD_MESSAGE}{suffix}.")
        _qc_details(details)
    else:
        st.warning(f"PDF üretildi ama iş temiz bitmedi (çıkış kodu {run.returncode}).")
    _compile_warnings(details, run.started_at)
    if run.kind == "translate" and details.get("output_dir"):
        _failed_pages_warning(failed_pages(Path(details["output_dir"]), result))
        _truncated_warning(details)
    _pdf_actions(pdf)
    _epub_actions(details, run.started_at)
    if run.returncode != 0:
        _output_tail(run)


def _translation_column(name: str | None, choice: EngineChoice) -> None:
    st.markdown("## Çeviri")
    job = _job()
    busy = _busy()
    uploaded = st.file_uploader("Kitap", type=upload_file_types(BOOK_SUFFIXES),
                                key=_upload_key(job), label_visibility="collapsed",
                                disabled=busy)
    max_bytes = _upload_limit()[0]
    if uploaded is not None:
        key = (uploaded.name, uploaded.size)
        if key != job["upload_key"] and not busy:
            # Önceki kitabın geçici kopyası yeni kopya yazılınca silinir; kilit
            # altında yeniden bakılır: başka sekmede bu arada iş başlamış
            # olabilir (kopya o zaman kullanımdadır; F5-01).
            with _start_lock():
                if not _busy_now():
                    try:
                        job["book"] = save_upload(uploaded.name, uploaded.getvalue(),
                                                  max_bytes, replaces=job["book"])
                    except ValueError as exc:
                        st.error(str(exc))
                        return
                    # Önceki kitabın sonucu taşınmasın.
                    job.update(upload_key=key, run=None)
    # Kitap oturumda değil iş yuvasında tutulur: TeX yolu saatler sürer
    # (çıkar -> dışarıda çevir -> derle); sayfa yenilenince kaybolmasın.
    book: Path | None = job["book"]
    if book is not None and not book.exists():
        book = None

    if book is None:
        st.markdown(f'<div class="upload-hint">{html.escape(UPLOAD_HINT)}</div>',
                    unsafe_allow_html=True)
    else:
        _file_card(book, name)
        if st.button(REMOVE_BOOK_LABEL, key="book_remove", disabled=busy,
                     help=REMOVE_BOOK_HELP):
            _remove_book()
        conflict = name_conflict(book, name)
        if conflict:
            st.warning(NAME_CONFLICT_MESSAGE)
    # Bayat bir sekmeden gelen tıklama yeni iş başlatmadı (F5-01): bir kez söyle.
    if st.session_state.pop(JOB_BUSY_KEY, False):
        st.warning(JOB_BUSY_MESSAGE)
    # Koşan işin ilerlemesi (İptal ile) ve son sonuç kartların ÜSTÜNDE: TeX
    # kartı yollar ve istemle uzayınca altta kalanlar ekrandan taşıyordu (U9).
    # Sayfa yenilense de ikisi görünür kalır (iş yuvası).
    _progress()
    _result(name)
    if book is not None:
        with st.container(key=CARDS_KEY):
            left, right = st.columns(2, gap="medium")
        with left:
            _tex_card(book, name)
        with right:
            _model_card(book, name, busy, conflict, choice)


# ---------------------------------------------------------------------------
# Araçlar sütunu (Mart 2026 sekmeleri)
# ---------------------------------------------------------------------------

def _terms_tab(name: str | None = None) -> None:
    from latex_pipeline.kitap_sozlugu import SOZLUK_DOSYA_ADI

    book = _job().get("book")
    if book is None or not Path(book).exists():
        st.caption(TERMS_NO_BOOK)
        return
    path = tex_files(Path(book), name).output_dir / SOZLUK_DOSYA_ADI
    try:
        rows = (_book_glossary_rows(str(path), path.stat().st_mtime_ns)
                if path.is_file() else [])
    except OSError as exc:
        st.error(f"Sözlük açılamadı: {exc}")
        return
    if not rows:
        st.caption(TERMS_NO_GLOSSARY)
        return
    # Örnek sözlükte gerçekten olan bir terim (U14).
    example = glossary_example({k: {"en": k, "tr": v} for k, v in rows})
    placeholder = f"örn. {example}" if example else TERMS_PLACEHOLDER_FALLBACK
    query = st.text_input("Terim ara", placeholder=placeholder,
                          label_visibility="collapsed", key="term_query")
    shown = _filter_terms(rows, query)
    if shown:
        # Tam genişlik yeni parametreyle (eski "container width" bayrağı
        # Streamlit 1.55'te kullanımdan kalktı; her çizimde konsola uyarı yazıyordu).
        st.dataframe([{"Kaynak terim": k, "Türkçe": v}
                      for k, v in shown[:TERMS_MAX_ROWS]],
                     hide_index=True, width="stretch")
    else:
        suggestion = _closest_term(rows, query)
        if suggestion:
            k, v, score = suggestion
            st.info(f"Tam eşleşme yok. En yakın: {k} → {v} "
                    f"(benzerlik {format_percent(score)})")
        else:
            st.caption("Bulunamadı.")
    st.caption(TERMS_NOTE.format(count=len(rows), shown=min(len(shown),
                                                             TERMS_MAX_ROWS)))


# HTML'in ve markdown'ın özel saydığı karakterler sayısal varlık olarak
# yazılır: bellek metni olduğu gibi görünür ("$x$" formüle, "*a*" vurguya,
# "<b>" etikete dönmez).
_LITERAL_CHARS = {char: f"&#{ord(char)};" for char in "&<>\"'\\`*_[]$~#|"}


def _plain_html(text: str) -> str:
    """Metni HTML içinde harfiyen göster; satır sonları korunur."""
    return "".join(_LITERAL_CHARS.get(char, char) for char in text).replace("\n", "<br>")


def _marked_html(text: str, query: str) -> str:
    """Metin, sorgunun geçtiği yerler <mark> ile (büyük/küçük harf ayrılmaz)."""
    needle = fold_text(query.strip())
    folded = fold_text(text)
    # Katlama harf sayısını değiştirdiyse konumlar tutmaz: işaretlenmez.
    if not needle or len(folded) != len(text):
        return _plain_html(text)
    parts, cursor = [], 0
    while (found := folded.find(needle, cursor)) >= 0:
        parts.append(_plain_html(text[cursor:found]))
        parts.append(f"<mark>{_plain_html(text[found:found + len(needle)])}</mark>")
        cursor = found + len(needle)
    parts.append(_plain_html(text[cursor:]))
    return "".join(parts)


def _memory_hit(hit: MemoryHit, query: str) -> None:
    details = [f"{format_percent(hit.similarity, 0)} benzerlik"]
    if hit.confirmed:
        details.append("onaylı")
    if hit.project != MEMORY_DEFAULT_PROJECT:
        details.append(hit.project)
    st.caption(" · ".join(details))
    st.markdown(
        f'<div class="memory-hit"><div>{_marked_html(hit.source, query)}</div>'
        f'<div class="memory-target">→ {_marked_html(hit.target, query)}</div></div>',
        unsafe_allow_html=True,
    )


def _memory_tab() -> None:
    """Çeviri belleği, SALT OKUNUR (U13): sayılar ve metin içi arama.

    Bellek runner'ın salt okunur bağlantısıyla açılır; sekme belleğe yazmaz,
    dosya yoksa oluşturmaz. Arama sorgunun kaynak ya da çeviri metninde
    geçtiği kayıtları bulur (SQL LIKE); hiçbirinde geçmiyorsa en yakın
    kayıtları gösterir. Benzerlik yüzdesi tam sayı.
    """
    try:
        stats = memory_stats()
    except Exception as exc:
        st.error(f"Çeviri belleği açılamadı: {exc}")
        return
    if stats is None:
        st.caption(MEMORY_MISSING_NOTE)
        return
    left, right = st.columns(2)
    left.metric("Kayıt", stats.total)
    right.metric("Onaylı", stats.confirmed)
    query = st.text_input("Bellekte ara", placeholder=MEMORY_SEARCH_PLACEHOLDER,
                          label_visibility="collapsed", key="memory_query")
    if not query.strip():
        return
    try:
        result = search_memory(query)
    except Exception as exc:
        st.error(f"Çeviri belleğinde aranamadı: {exc}")
        return
    if result is None:
        st.caption(MEMORY_QUERY_TOO_SHORT)
        return
    if not result.total:
        st.caption(MEMORY_NO_MATCH)
        return
    shown = len(result.hits)
    if result.fuzzy:
        template = (MEMORY_FUZZY_COUNT_LIMITED if result.total > shown
                    else MEMORY_FUZZY_COUNT)
        st.caption(template.format(threshold=format_percent(MEMORY_FUZZY_THRESHOLD, 0),
                                   total=result.total, shown=shown))
    elif result.total > shown:
        st.caption(MEMORY_MATCH_COUNT_LIMITED.format(total=result.total, shown=shown))
    else:
        st.caption(MEMORY_MATCH_COUNT.format(total=result.total))
    for hit in result.hits:
        _memory_hit(hit, query)


def _qa_tab() -> None:
    st.caption("Orijinal ve çevrilmiş PDF'yi sayfa sayfa piksel düzeyinde karşılaştırır.")
    original_key, translated_key = COMPARE_UPLOAD_KEYS
    types = upload_file_types(COMPARE_SUFFIXES)
    original = st.file_uploader("Orijinal PDF", type=types, key=original_key)
    translated = st.file_uploader("Çevrilmiş PDF", type=types, key=translated_key)
    with st.expander("Karşılaştırma ayarları"):
        all_pages = st.checkbox("Tüm sayfalar", value=False)
        max_pages = st.number_input("En çok sayfa", min_value=1, max_value=2000,
                                    value=50, disabled=all_pages)
        dpi = st.slider("DPI", 72, 300, 150, help="Yüksek DPI daha hassas ama daha yavaş.")
        include_images = st.checkbox("Görselleri rapora ekle", value=True)
    if not (original and translated):
        st.caption("İki PDF'yi de yükle.")
        return
    if not st.button("Karşılaştır", key="qa_compare", type="primary", width="stretch"):
        return
    from tools.pixel_diff import compare_pdfs, generate_report

    with st.spinner("Karşılaştırılıyor…"), \
            tempfile.TemporaryDirectory(prefix="kitapcevir-qa-") as tmp:
        first, second = Path(tmp) / "orijinal.pdf", Path(tmp) / "ceviri.pdf"
        first.write_bytes(original.getvalue())
        second.write_bytes(translated.getvalue())
        # Sayfa görselleri de bu geçici dizine: output_dir verilmezse
        # pixel_diff kendi mkdtemp dizinini açıyor ve hiç silinmiyordu. Rapor
        # görselleri base64 gömdüğü için dizin kapanınca bir şey kaybolmaz.
        comparison = compare_pdfs(str(first), str(second),
                                  output_dir=str(Path(tmp) / "sayfalar"), dpi=dpi,
                                  max_pages=None if all_pages else int(max_pages))
        report = Path(tmp) / "qa_rapor.html"
        generate_report(comparison, str(report), include_images=include_images)
        report_html = report.read_text(encoding="utf-8")
    st.success(f"{comparison.compared_pages} sayfa karşılaştırıldı.")
    # Sayfa sayısı farkı (U18): pixel_diff yalnız ortak ilk sayfaları
    # karşılaştırır; fazla/eksik sayfa ekranda söylenmezse görünmüyordu.
    page_note = getattr(comparison, "page_count_note", None)
    if page_note:
        st.warning(page_note)
    left, right = st.columns(2)
    left.metric("Ortalama fark", format_percent(comparison.average_difference * 100))
    right.metric("Sorunlu sayfa", len(comparison.pages_with_issues))
    if comparison.pages_with_issues:
        shown = ", ".join(str(page + 1) for page in comparison.pages_with_issues[:10])
        st.warning(f"Sorunlu sayfalar: {shown}")
    st.download_button("Raporu indir (HTML)", data=report_html, file_name="qa_rapor.html",
                       mime="text/html", key="qa_download")


def _tools_column(name: str | None = None) -> None:
    st.markdown("## Araçlar")
    terms, memory, qa = st.tabs([branding.TERMS_TAB, branding.MEMORY_TAB,
                                 branding.COMPARE_TAB])
    with terms:
        _terms_tab(name)
    with memory:
        _memory_tab()
    with qa:
        _qa_tab()


# ---------------------------------------------------------------------------

st.set_page_config(page_title=branding.APP_NAME, layout="wide")
st.markdown(CSS + _layout_css() + _uploader_css(_upload_limit()[1]),
            unsafe_allow_html=True)

_problem = platform_problem()
if _problem:
    st.error(_problem)
    st.stop()

_swept_uploads()
_ensure_scan()
_choice = _engine_choice()
_name = _sidebar(_choice)
with st.container(key=LAYOUT_KEY):
    _main, _side = st.columns([2, 1], gap="large")
with _main:
    _translation_column(_name, _choice)
with _side:
    _tools_column(_name)
