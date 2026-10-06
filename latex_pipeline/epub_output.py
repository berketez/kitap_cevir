"""
EPUB Output Generator

LaTeX pipeline çevirisi tamamlandıktan sonra, çevrilmiş metni
orijinal EPUB yapısına geri yazarak EPUB çıktı oluşturur.

Strateji (E2, 2026-09-25: içerik çapalı hizalama)
-------------------------------------------------
Eskiden translated_body.tex paragrafları segmentlere POZİSYONEL (i == i)
eşleniyordu. to_markdown'ın kitap adı/yazar bloğu segmentlerde yok (+3 sabit
kayma), dönüştürücü ve sanitizer paragraf ekleyip birleştiriyor; işaretli
sözde çeviriyle ölçüldüğünde on kitabın hiçbirinde öğelerin %1'i bile doğru
yere düşmüyordu. Sayı sapması bekçisi (%5) kaymayı yakalamıyordu. Yeni zincir
iki hizalamadan oluşur:

1. Segmentler kaynak EPUB'dan rebuild_epub ile AYNI ayrıştırıcıyla yeniden
   çıkarılır (indeksler birebir aynı; eski ayrıştırıcının yazdığı
   epub_segment_mapping.json'a güvenilmez).
2. A1 — aynı dil: segment ↔ kaynak LaTeX birimi (source/body.tex; çevirmenin
   gördüğü EXPORT.tex gövdesi). Sözcük dizisi eşit olanlar doğrudan eşlenir
   (SequenceMatcher), aradaki boşluklar sözcük Dice benzerliğiyle dinamik
   programlamayla hizalanır (1:1, 1:2, 2:1). Aynı dildeki metin
   karşılaştırıldığı için güvenilirdir; dönüştürücünün eklediği (kapak,
   yazar satırı) ya da attığı birimler boşta kalır.
3. A2 — diller arası: kaynak birim ↔ çeviri birimi (translated_body.tex).
   Çevirmen LaTeX iskeletini korur (başlık komutları, \\item, ortamlar,
   satır içi komutlar, sayılar); metin uzunluğu çeviride orantılı kalır.
   Gale–Church uzunluk modeli + yapı özellikleriyle bantlı ileri-geri
   dinamik programlama yapılır (1:1, 1:0, 0:1, 2:1, 1:2). Bölüm/kısım
   başlıkları iki tarafta aynı sıradaysa kafes bölüm bölüm kurulur (bir
   bölümdeki kayma ötekine taşınmaz). Yalnız sonsal olasılığı
   MIN_PAIR_POSTERIOR'u geçen eşleşmeler uygulanır: belirsiz paragraf kaynak
   dilde kalır, yanlış yere yazılmaz.

Segment kimliğini EXPORT.tex'e gömmek yerine hizalama seçildi: kimlik
yorumları dönüştürücüden (PDF yolu da etkilenir) ve çevirmenden (Claude
Code, insan, yerel model) sağ çıkmak zorunda olurdu, EXPORT.tex'i
kullanıcıya gürültü olarak gösterirdi ve eski çıkarımlarda yoktur.

Güven eşiği (MIN_ALIGNED_COVERAGE) altında EPUB ÜRETİLMEZ: yanlış EPUB, EPUB
olmamasından kötüdür. Neden, sözleşme S5 gereği çıktı klasörüne
(compiled/EPUB_ATLANDI.txt) tek satır Türkçe yazılır; EPUB üretilirse eski
not silinir.

E2 (2026-09-28):
- Dipnotlar: \footnote birim metninden ayrılır. Çevirmen notu
  (\footnote{Çevirmen notu: …}) hizalama ölçülerine girmez, Türkçe EPUB'da
  belge sonuna <aside epub:type="footnote"> olur; kaynak dipnotu (E1:
  EPUB dipnotu -> [^kc-N] -> \footnote) EPUB'daki not öğesine yazılır,
  paragraftaki özgün işaret bağlantısı yerinde kalır.
- Kapsam (S7): compiled/epub_rapor.json {"kapsam", "cevrilmemis_blok",
  "toplam_blok"} — EPUB'a gerçekten yazılan çeviriden; günlüğe de yazılır.
  KARAR 18 (2026-09-29): çeviride aynen kalan blok QC'nin dil
  sınıflandırıcılarıyla kaynak dilde düzyazı çıkarsa çevrilmemiş sayılır
  (_kept_is_source_prose); EPUB kapsamı ile QC çelişmez.

2026-09-29 (E kulvarı): tablo hücresi ayrı birimdir (F1-01; hücre
segmentleri hücre birimleriyle hizalanır); formül/görsel nesneleri ve
alt simge Türkçe EPUB'a özgün <math>/<img>/<sub> olarak döner (F1-07/F1-08);
düz metindeki markdown karakterleri kaçışlanır (F1-09).
- Satır içi biçim (R9): \textit/\textbf/\textsuperscript -> <em>/<strong>/
  <sup>; dil, kitap adı ve İçindekiler (R7) rebuild_epub'da.

Akış: EPUB → ... → kitap_tr.epub
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from latex_pipeline.config import PipelineConfig
from latex_pipeline.patterns import TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE
from parsers.epub_parser import (
    EPUB_EM_CLOSE,
    EPUB_EM_OPEN,
    EPUB_NOTE_REF_CLOSE,
    EPUB_NOTE_REF_OPEN,
    EPUB_SOURCE_NOTE_CLOSE,
    EPUB_SOURCE_NOTE_OPEN,
    EPUB_STRONG_CLOSE,
    EPUB_STRONG_OPEN,
    EPUB_OBJ_CLOSE,
    EPUB_OBJ_OPEN,
    EPUB_SUB_CLOSE,
    EPUB_SUB_OPEN,
    EPUB_SUP_CLOSE,
    EPUB_SUP_OPEN,
    EPUB_TOC_TITLE_TR,
)


# ---------------------------------------------------------------------------
# Sabitler (tek yer)
# ---------------------------------------------------------------------------

# S5: EPUB üretilmediğinde çıktı klasörüne (compiled/) yazılan tek satırlık
# Türkçe neden. 2_derle.py aynı adı okur.
EPUB_SKIP_NOTE_NAME = "EPUB_ATLANDI.txt"
# S7 (2026-09-25 sözleşmesi, E2 2026-09-28'de yazıldı): Türkçe EPUB üretilince
# compiled/ altına kapsam raporu {"kapsam": 0..1, "cevrilmemis_blok": int,
# "toplam_blok": int}. 2_derle.py aynı adı (kendi sabitiyle) okur; blok =
# çevrilebilir EPUB segmenti (paragraf/başlık/liste öğesi). Kapsam, EPUB'a
# gerçekten YAZILAN çeviriden (ve çeviride aynen kalan öğelerden) hesaplanır.
EPUB_REPORT_NAME = "epub_rapor.json"
# Türkçe EPUB'un dili (html lang, dc:language): hattın tek hedef dili.
EPUB_OUTPUT_LANGUAGE = PipelineConfig.target_lang

# Çevrilebilir segmentlerin en az bu oranı güvenle çeviriye bağlanmalı; yoksa
# EPUB üretilmez. Kalan (en çok %10) segment kaynak dilde kalır; bunlar
# dönüştürücünün attığı öğeler ve belirsiz bölgelerdir, yanlış yere yazılmış
# çeviri değildir.
MIN_ALIGNED_COVERAGE = 0.90
# A2'de bir eşleşmenin uygulanması için gereken en düşük sonsal olasılık.
MIN_PAIR_POSTERIOR = 0.90
# A1 (aynı dil): sözcük Dice benzerliği bunun altındaysa eşleşme yok.
SAME_LANG_MIN_SIMILARITY = 0.5
# A1 boşluk DP'sinin hücre üst sınırı (daha büyük boşluk eşlenmeden kalır;
# bu durumda kapsam eşiği EPUB'u zaten durdurur).
MAX_GAP_CELLS = 250_000

# A2 uzunluk modeli: δ = (l_ç − c·l_k) / √(s²·l + (k·c·l)²). İlk terim
# Gale–Church'ün toplamsal gürültüsü (s² karakter başına varyans), ikincisi
# çevirmenin paragraf başına serbestliği (uzunluk çarpanının göreli std'si
# k). c ve k her kitapta veriden kestirilir (iki geçiş): ilk geçiş
# varsayılanlarla hizalar, yoldaki 1:1 çiftlerin log(l_ç/l_k) medyanı c'yi,
# MAD'i k'yı verir; ikinci geçiş bu modelle hizalar ve sonsalları hesaplar.
LENGTH_VARIANCE = 12.0
DEFAULT_LENGTH_SPREAD = 0.20
MIN_LENGTH_SPREAD = 0.05
# Kestirilen k bunun üstündeyse paragraf uzunlukları kaynağınkilerle
# ilişkisizdir: çeviri bu kaynağın (ya da bu çıkarımın) çevirisi değildir
# ve EPUB üretilmez. Ölçüm (2026-09-25): gerçek çevirilerde k 0,07–0,13
# (dokuz kitap, Yol A ve B); uzunluk dağılımı aynı sentetik ilişkisiz
# metinde 0,29–0,35. Asıl savunma kapsam eşiğidir (ilişkisiz metinde
# güvenli eşleşme %7–37); bu eşik ikinci kilittir.
MAX_LENGTH_SPREAD = 0.30
# Kestirim için en az çift sayısı ve çiftteki en kısa kaynak uzunluğu.
MIN_SPREAD_SAMPLE = 20
MIN_SPREAD_LENGTH = 80
# Oranın uç değerleri (l_ç/l_k için sınır).
MIN_LENGTH_RATIO = 0.4
MAX_LENGTH_RATIO = 2.5
# A2 geçiş önselleri (log): 1:1, atlama (1:0 / 0:1), birleşme (2:1 / 1:2).
LOG_PRIOR_MATCH = math.log(0.94)
LOG_PRIOR_SKIP = math.log(0.01)
LOG_PRIOR_MERGE = math.log(0.02)
# Satır içi komut sayısı (\textit, \textbf, \href, \footnote ...) farkı
# başına ve eksik/fazla sayı başına maliyet (nat).
FEATURE_MISMATCH_COST = 0.7
NUMBER_MISMATCH_COST = 1.0
MAX_NUMBER_MISMATCH = 5
# Çeviride değişmeyen çapalar (\href/\url adresi, \label/\ref/\cite
# anahtarı) farkı başına maliyet: EPUB içindekiler sayfasındaki kısa bağlantı
# satırlarını uzunluk ayırt edemez, adresleri ayırt eder.
ANCHOR_MISMATCH_COST = 3.0
MAX_ANCHOR_MISMATCH = 4
# Ortak sözcük uyumu (Simard vd. 1992'deki "cognate" fikri): çevirmenin
# aynen bıraktığı ad, terim, kısaltma ('Kepler', 'GPT', 'Bayesian') iki
# tarafta da varsa çifti destekler, yalnız birinde varsa zayıflatır. Sözcük
# ağırlığı 1 / (içinde geçtiği birim sayısı): her yerde geçen ad az, tek
# paragraflık terim çok katkı verir. Çeviride ortak sözcük yoksa (her şey
# çevrilmiş) terim 0'dır, model uzunluk + yapıya kalır. Ölçüm: benzer
# kısa alıntılar arasında not eklenip bir alıntı düşünce uzunluk + yapı
# üç alıntıyı kaydırıyordu (deneme EPUB'u, sözde çeviri %2 bozma).
COGNATE_MIN_LENGTH = 4
COGNATE_WEIGHT = 1.5
COGNATE_CAP = 2.0
# Tek bir çift için uzunluk maliyeti üst sınırı (sayısal taşmayı önler).
MAX_LENGTH_COST = 60.0
# A2 bandı: köşegenden en az bu kadar birim (bloktaki |n − m| eklenir).
BAND_MIN_HALF_WIDTH = 40
# A2 kafesinin hücre üst sınırları: aşılırsa hizalama yapılmaz, EPUB
# üretilmez. Ölçüm (2026-09-25, bir test kitabı): iki geçiş ~8 µs/hücre, tepe
# bellek ~300 B/hücre (bloklar sırayla işlenir, tablolar bırakılır; tepe
# en büyük bloğunki). Toplam 10 milyon hücre ~80 sn; blok başına 1,5
# milyon hücre ~0,45 GB. O kitap (4700 birim, 26 blok) 0,41 milyon
# hücredir.
MAX_BAND_CELLS = 10_000_000
MAX_BLOCK_CELLS = 1_500_000
# A2 bölme noktaları: bu başlık türleri iki tarafta aynı sıradaysa kafes
# bloklara bölünür.
_BLOCK_HEADING_KINDS = frozenset({"h:part", "h:chapter"})

# Dönüştürücü bölüm etiketini başlıktan atar ("Chapter 7. Calibration" ->
# \chapter{Calibration}; PDF'de etiketi LaTeX basar). EPUB'da etiket yoksa
# numara kaybolur: çeviriye Türkçe etiketle geri eklenir ("Bölüm 7: …").
EPUB_CHAPTER_LABEL_TR = "Bölüm"
EPUB_PART_LABEL_TR = "Kısım"
# Dipnotları yerlerine taşınan (dönüştürücüde yazılmayan) not bölümü başlığı
# ('NOTAS', 'Notes'): EPUB'da öğe durur, Türkçe adı bu olur.
EPUB_NOTES_HEADING_TR = "Notlar"

# A2 yapı özelliği farkı üst sınırı (özellik başına). Sanitizer'ın Yunanca
# sözcüğü harf harf matematiğe çevirmesi ('σελήνη' -> '$\\sigma$$\\epsilon$…')
# tek paragrafta 10 '$' farkı doğuruyordu (7 nat): doğru çift ve komşusu
# (kısa paragraf) sonsal eşiğin altına düşüp kaynak dilde kalıyordu
# (Fransızca bir test kitabında, 2026-09-28). Tek özellikte 2'den büyük fark
# ek kanıt değildir.
MAX_FEATURE_MISMATCH = 2

# Dipnot türleri (birim notları). Çevirmen notu (patterns.
# TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE) kaynakta yoktur: uzunluk/yapı
# hesaplarına girmez, EPUB'a yeni not olarak yazılır. Kaynak dipnotu
# (EPUB'daki dipnotun dönüştürücüde \footnote olmuş hâli) iki tarafta vardır;
# çevirisi EPUB'daki not öğesine yazılır.
NOTE_KIND_TRANSLATOR = "cevirmen"
NOTE_KIND_SOURCE = "kaynak"


def save_segment_mapping(
    epub_path: str,
    output_dir: str,
) -> Optional[str]:
    """EPUB extraction sırasında segment mapping kaydet.

    EPUBExtractor ile aynı sırada segment metinleri çıkarır
    ve JSON olarak kaydeder. (E2: generate_epub_output segmentleri artık
    kaynak EPUB'dan yeniden çıkarır; dosyadan yalnız ``epub_path`` okunur.)

    Returns:
        Kaydedilen JSON dosyasının yolu, hata durumunda None.
    """
    try:
        from parsers.epub_parser import EPUBParser
    except ImportError:
        return None

    out = Path(output_dir)
    mapping_path = out / "epub_segment_mapping.json"

    try:
        parser = EPUBParser()
        epub_doc = parser.parse(epub_path)
        segments = parser.extract_segments(epub_doc)

        mapping_data = {
            "epub_path": str(Path(epub_path).resolve()),
            "segment_count": len(segments),
            "segments": [
                {
                    "index": s.get("index", i),
                    "text": s.get("text", ""),
                    "type": s.get("type", "paragraph"),
                }
                for i, s in enumerate(segments)
            ],
        }

        mapping_path.write_text(
            json.dumps(mapping_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(mapping_path)

    except Exception as exc:
        print(f"[EPUB Output] Segment mapping kaydedilemedi: {exc}")
        return None


def generate_epub_output(
    original_epub: str,
    output_dir: str,
    book_name: str,
    mt_notice: Optional[Sequence[str]] = None,
    mt_contributor: Optional[str] = None,
) -> Optional[str]:
    """Çevrilmiş LaTeX içeriğinden EPUB çıktı oluştur.

    Parameters
    ----------
    original_epub : str
        Orijinal EPUB dosya yolu.
    output_dir : str
        Pipeline output dizini.
    book_name : str
        Kitap adı (çıktı dosya adı için).
    mt_notice : sequence of str, optional
        F3 (2026-09-30): makine çevirisi künyesinin satırları (başlık,
        açıklama, alanlar; ``ui.branding.mt_notice_lines``). Verilirse EPUB
        kurulduktan SONRA kapak / başlık sayfasının arkasına görünür bir
        künye sayfası ve üst veri (``dc:contributor`` = *mt_contributor*,
        ``dc:description``) eklenir (``add_front_notice_to_epub``). Yalnız
        "Yerel model ile çevir" verir; "PDF'den TeX çıkar" (2_derle) vermez,
        o çıktı değişmez. Hizalama ve kapsam raporu künyeden önce, çeviri
        gövdesinden hesaplanır (künye sayılmaz).

    Returns
    -------
    str or None
        Oluşturulan EPUB yolu. Çeviri varken EPUB üretilmezse None döner ve
        neden ``compiled/EPUB_ATLANDI.txt``'ye tek satır yazılır (S5).
        Henüz çeviri yoksa (``--export-tex`` koşusu) hiçbir şey yazılmaz.
    """
    out = Path(output_dir)
    slug = re.sub(r"[^\w\s-]", "", book_name)
    slug = re.sub(r"[\s_]+", "_", slug.strip())[:80]
    epub_out_path = out / "compiled" / f"{slug}_TR.epub"

    def skip(reason: str) -> None:
        _write_skip_note(out, reason, epub_out_path)
        return None

    # 1. Çevrilmiş gövde. Yoksa üretilecek bir şey yoktur (çıkarım koşusu):
    # not yazılmaz, önceki çıktılara dokunulmaz.
    translated_tex = out / "source" / "translated_body.tex"
    if not translated_tex.exists():
        print("[EPUB Output] translated_body.tex bulunamadı; EPUB çıktı atlandı")
        return None

    # 2. Kaynak gövde (çevirmenin gördüğü metin): body.tex, yoksa EXPORT.tex
    source_tex_path = out / "source" / "body.tex"
    if not source_tex_path.exists():
        source_tex_path = out / "EXPORT.tex"
    if not source_tex_path.exists():
        print("[EPUB Output] kaynak LaTeX (source/body.tex) bulunamadı")
        return skip("Kaynak LaTeX (source/body.tex) bulunamadı; "
                    "çeviri kaynak EPUB'la eşlenemiyor.")

    # 3. Orijinal EPUB: parametre, segment eşlem dosyası ya da kopya
    epub_path = _resolve_source_epub(original_epub, out)
    if epub_path is None:
        print("[EPUB Output] Orijinal EPUB dosyası bulunamadı")
        return skip("Kaynak EPUB dosyası bulunamadı.")

    try:
        from parsers.epub_parser import EPUBParser
    except ImportError as exc:  # pragma: no cover
        return skip(f"EPUB ayrıştırıcısı yüklenemedi ({exc}).")

    try:
        parser = EPUBParser()
        epub_doc = parser.parse(epub_path)
        # Segmentler rebuild_epub ile AYNI ayrıştırıcıdan: indeksler birebir
        # aynı olur. epub_segment_mapping.json eski ayrıştırıcıyla yazılmış
        # olabilir (farklı segment sayısı), bu yüzden indeks kaynağı değildir.
        segments = parser.extract_segments(epub_doc)
        source_tex = source_tex_path.read_text(encoding="utf-8")
        translated = translated_tex.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[EPUB Output] Girdi okunamadı: {exc}")
        return skip(f"Girdi okunamadı ({exc}).")

    try:
        translations, report = align_translations(segments, source_tex, translated)
    except Exception as exc:
        print(f"[EPUB Output] Hizalama hatası: {exc}")
        return skip(f"Çeviri kaynak EPUB'la hizalanırken hata "
                    f"oluştu ({exc}).")
    # F5-15 (V1, 2026-09-29): Türkçe ondalık virgül (satırın öbür sayıları
    # gibi; _fmt_spread).
    coverage_pct = f"{report['coverage'] * 100:.1f}".replace(".", ",")
    print(
        f"[EPUB Output] Hizalama: {report['translated']}/{report['counted']} "
        f"çevrilebilir segment çeviriye bağlandı "
        f"(kapsam %{coverage_pct}; "
        f"kaynak eşleşmesi {report['source_matched']}, "
        f"çeviri eşleşmesi {report['pair_confident']}/{report['pair_total']}, "
        f"uzunluk yayılımı {_fmt_spread(report['length_spread'])}, "
        f"{report['blocks']} blok)"
    )

    reason = _alignment_problem(report)
    if reason is not None:
        print(f"[EPUB Output] {reason}")
        return skip(reason)

    # rebuild_epub markdown işaretli metni (bağlantı) HTML olarak ayrıştırır;
    # o metindeki '<', '&' kaçışlanır (düz metin olduğu gibi yazılır).
    translations = {k: _html_safe(parser, v) for k, v in translations.items()}
    new_notes = {k: _html_safe(parser, v) for k, v in report["notes"].items()}

    # 4. EPUB rebuild (R7: dil, kitap adı, İçindekiler Türkçe)
    rebuild_stats: Dict = {}
    try:
        epub_out_path.parent.mkdir(parents=True, exist_ok=True)
        parser.rebuild_epub(
            epub_doc, translations, str(epub_out_path),
            notes=new_notes, language=EPUB_OUTPUT_LANGUAGE,
            title=_translated_book_title(translated), stats=rebuild_stats)
    except Exception as exc:
        print(f"[EPUB Output] EPUB oluşturulamadı: {exc}")
        return skip(f"EPUB yeniden kurulurken hata oluştu ({exc}).")

    _clear_skip_notes(out)
    # 5. S7 kapsam raporu: EPUB'a gerçekten yazılan çeviriden; KARAR 18:
    # çeviride aynen kalan blok kaynak dilde düzyazıysa çevrilmemiş sayılır.
    details: Dict = {}
    coverage = _coverage_report(report, rebuild_stats.get("written", set()),
                                _source_language(source_tex, epub_doc.language),
                                segments, details)
    _write_coverage_report(out, coverage, details.get("kaynak_dilde_aynen_kalan", 0))
    if mt_notice:
        try:
            add_front_notice_to_epub(epub_out_path, mt_notice, mt_contributor)
            print("[EPUB Output] Makine çevirisi künyesi eklendi.")
        except Exception as exc:  # künye eklenemezse EPUB yine üretilmiş sayılır
            print(f"[EPUB Output] UYARI: makine çevirisi künyesi eklenemedi: {exc}")
    print(f"[EPUB Output] EPUB oluşturuldu: {epub_out_path}")
    return str(epub_out_path)


# ---------------------------------------------------------------------------
# Makine çevirisi künyesi (F3, 2026-09-30; Berke kararı)
# ---------------------------------------------------------------------------
# Kurulmuş EPUB'a zip düzeyinde eklenir: ebooklib ile yeniden okuyup yazmak
# belgelerin head'ini (CSS bağlantıları) yeniden düşürürdü (rebuild_epub'daki
# E2 notu). Yalnız OPF (manifest, spine, metadata) değişir ve bir XHTML
# sayfası eklenir; öteki girdiler baytı baytına kopyalanır.
MT_NOTICE_EPUB_FILE = "kitapcevir_kunye.xhtml"
MT_NOTICE_EPUB_ID = "kitapcevir-kunye"
MT_NOTICE_EPUB_CONTRIBUTOR_ID = "kitapcevir-mt"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_OPF_ROOTFILE_RE = re.compile(r'<(?:\w+:)?rootfile\b[^>]*\bfull-path="([^"]+)"')
_OPF_ITEM_RE = re.compile(r"<(?:\w+:)?item\b[^>]*>")
_OPF_ITEMREF_RE = re.compile(r"<(?:\w+:)?itemref\b[^>]*>")
_OPF_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
# Kapak / başlık sayfası: künye bunların arkasına konur
_FRONT_PAGE_RE = re.compile(r"(?<![a-z])(?:cover|title|kapak)", re.IGNORECASE)


def _opf_attrs(tag: str) -> Dict[str, str]:
    return dict(_OPF_ATTR_RE.findall(tag))


def _notice_xhtml(lines: Sequence[str]) -> str:
    import html

    title, *rest = [str(line) for line in lines]
    paras = "\n".join(f"<p>{html.escape(line)}</p>" for line in rest if line.strip())
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml" lang="{EPUB_OUTPUT_LANGUAGE}" '
        f'xml:lang="{EPUB_OUTPUT_LANGUAGE}">\n'
        f"<head><title>{html.escape(title)}</title></head>\n"
        "<body>\n"
        '<div class="kitapcevir-kunye" style="border: 1px solid #888; padding: 0.8em; '
        'margin: 2em 1em; font-size: 0.85em;">\n'
        f"<p><strong>{html.escape(title)}</strong></p>\n{paras}\n"
        "</div>\n</body>\n</html>\n"
    )


def _opf_with_notice(opf: str, href: str, lines: Sequence[str],
                     contributor: Optional[str]) -> Optional[str]:
    """OPF'ye künye sayfasını (manifest + spine) ve üst veriyi ekle; biçim
    tanınmazsa None."""
    import html

    manifest_end = re.search(r"</((?:\w+:)?)manifest>", opf)
    spine_open = re.search(r"<(?:\w+:)?spine\b[^>]*>", opf)
    if manifest_end is None or spine_open is None:
        return None
    prefix = manifest_end.group(1)
    items = {}
    for tag in _OPF_ITEM_RE.findall(opf):
        attrs = _opf_attrs(tag)
        if "id" in attrs:
            items[attrs["id"]] = attrs.get("href", "")
    # Spine'ın başındaki kapak / başlık sayfalarının arkası
    insert_at = spine_open.end()
    for m in _OPF_ITEMREF_RE.finditer(opf, spine_open.end()):
        between = opf[insert_at:m.start()]
        if between.strip():                      # itemref'ler bitişik değil (spine sonu)
            break
        idref = _opf_attrs(m.group(0)).get("idref", "")
        if not _FRONT_PAGE_RE.search(idref + " " + items.get(idref, "")):
            break
        insert_at = m.end()
    itemref = f'\n    <{prefix}itemref idref="{MT_NOTICE_EPUB_ID}"/>'
    item = (f'  <{prefix}item id="{MT_NOTICE_EPUB_ID}" href="{html.escape(href)}" '
            'media-type="application/xhtml+xml"/>\n  ')
    # Sıra: önce spine (manifest'ten sonra gelir), sonra manifest
    out = opf[:insert_at] + itemref + opf[insert_at:]
    manifest_end = re.search(r"</((?:\w+:)?)manifest>", out)
    out = out[:manifest_end.start()] + item + out[manifest_end.start():]
    # Üst veri: dc önekiyle (OPF'de tanımlı değilse eklenmez)
    dc = re.search(r'xmlns:(\w+)="' + re.escape(_DC_NS) + '"', out)
    metadata_end = re.search(r"</(?:\w+:)?metadata>", out)
    if dc and metadata_end:
        p = dc.group(1)
        meta = ""
        if contributor:
            meta += (f'  <{p}:contributor id="{MT_NOTICE_EPUB_CONTRIBUTOR_ID}">'
                     f"{html.escape(contributor)}</{p}:contributor>\n  ")
        description = " ".join(str(line) for line in lines if str(line).strip())
        meta += f"<{p}:description>{html.escape(description)}</{p}:description>\n  "
        out = out[:metadata_end.start()] + meta + out[metadata_end.start():]
    return out


def add_front_notice_to_epub(epub_path, lines: Sequence[str],
                             contributor: Optional[str] = None) -> bool:
    """Kurulmuş EPUB'a makine çevirisi künyesini ekle (bir kez).

    Kapak / başlık sayfası (spine'ın başındaki ``cover`` / ``title`` adlı
    sayfalar) varsa onların arkasına, yoksa kitabın en başına görünür bir
    künye sayfası; OPF üst verisine ``dc:contributor`` (*contributor*) ve
    ``dc:description``. ``mimetype`` ilk ve sıkıştırmasız kalır. Künye zaten
    varsa ya da OPF biçimi tanınmazsa dosyaya dokunulmaz (False).
    """
    import os
    import posixpath
    import tempfile
    import zipfile

    if not lines:
        return False
    path = Path(epub_path)
    with zipfile.ZipFile(path) as zin:
        container = zin.read("META-INF/container.xml").decode("utf-8", errors="replace")
        m = _OPF_ROOTFILE_RE.search(container)
        if m is None:
            return False
        opf_name = m.group(1)
        opf = zin.read(opf_name).decode("utf-8")
        if f'id="{MT_NOTICE_EPUB_ID}"' in opf:
            return False
        opf_dir = posixpath.dirname(opf_name)
        names = set(zin.namelist())
        href = MT_NOTICE_EPUB_FILE
        page_name = posixpath.join(opf_dir, href) if opf_dir else href
        if page_name in names:
            return False
        new_opf = _opf_with_notice(opf, href, lines, contributor)
        if new_opf is None:
            return False
        fd, tmp = tempfile.mkstemp(suffix=".epub", dir=str(path.parent))
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w") as zout:
                infos = zin.infolist()
                ordered = ([i for i in infos if i.filename == "mimetype"]
                           + [i for i in infos if i.filename != "mimetype"])
                for info in ordered:
                    data = (new_opf.encode("utf-8") if info.filename == opf_name
                            else zin.read(info.filename))
                    if info.filename == "mimetype":
                        zout.writestr(info, data, compress_type=zipfile.ZIP_STORED)
                    else:
                        zout.writestr(info, data)
                zout.writestr(page_name, _notice_xhtml(lines).encode("utf-8"),
                              compress_type=zipfile.ZIP_DEFLATED)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    os.replace(tmp, path)          # okuma kapandıktan sonra, tek adımda
    return True


def _coverage_report(report: Dict, written, source_lang: Optional[str] = None,
                     segments: Optional[Sequence[Dict]] = None,
                     details: Optional[Dict] = None) -> Dict:
    """S7: {"kapsam", "cevrilmemis_blok", "toplam_blok"}.

    Blok = çevrilebilir EPUB segmenti. Çevrilmiş sayılan: çevirisi öğeye
    yazılan ya da çeviride kaynakla aynı kalan (ad, bilerek özgün bırakılan
    alıntı). Hizalanıp da yazılamayan (öğe atlandı) segment çevrilmemiştir.

    KARAR 18 (2026-09-29): çeviride AYNEN kalan blok, QC'nin dil
    sınıflandırıcılarıyla (_kept_is_source_prose) kaynak dilde düzyazı
    çıkarsa çevrilmemiş sayılır (sayısı *details*
    ["kaynak_dilde_aynen_kalan"]'e yazılır, günlük satırında görünür;
    rapor dosyasının S7 alanları değişmez); tek sözcük,
    özel ad, Latince alıntı, başka dilde alıntı ve tablo hücresi muaftır
    (QC de tablo satırlarını dil denetiminden muaf tutar). Eskiden aynen
    kalan her blok çevrilmiş sayılıyordu: negatif kontrolde EPUB kapsamı %100
    derken QC CRITICAL veriyordu.
    """
    counted = report.get("counted_ids") or []
    kept = set(report.get("kept_ids") or [])
    kept_src = report.get("kept_src") or {}
    counted_set = set(counted)
    prose = set()
    if source_lang:
        for sid in kept & counted_set:
            raw, kind = kept_src.get(sid, ("", ""))
            heading = bool(segments) and segments[sid].get("type") == "heading"
            if _kept_is_source_prose(raw, kind, source_lang, heading=heading):
                prose.add(sid)
    covered = (kept - prose) | set(written)
    missing = sum(1 for i in counted if i not in covered)
    total = len(counted)
    kapsam = (total - missing) / total if total else 1.0
    if details is not None:
        details["kaynak_dilde_aynen_kalan"] = len(prose)
    return {"kapsam": round(kapsam, 4), "cevrilmemis_blok": missing,
            "toplam_blok": total}


def _source_language(source_tex: str, epub_language: str = "") -> Optional[str]:
    """Kaynağın dili: QC'nin kaynak bağlamıyla aynı ölçüm
    (qc_engine._SourceContext: temizlenmiş kaynak gövde, güven eşiği);
    ölçülemezse EPUB'un dc:language'ının birincil alt etiketi."""
    try:
        from latex_pipeline.qc_engine import _SourceContext
        lang = _SourceContext(source_tex).lang
    except Exception:  # pragma: no cover - ölçüm hatası kapsamı durdurmasın
        lang = None
    if lang:
        return "ru" if lang == "cyrillic" else lang
    primary = (epub_language or "").split("-")[0].split("_")[0].strip().lower()
    return primary or None


def _kept_is_source_prose(raw: str, kind: str, source_lang: str,
                          heading: bool = False) -> bool:
    """Çeviride aynen kalan kaynak birim, kaynak dilde düzyazı mı?

    QC'nin satır kararıyla aynı araçlar (İÇE AKTARILIR): metin
    qc_engine._clean_for_language ile temizlenir; İngilizce kaynakta
    retranslator.is_english_line (QC _is_likely_english), öteki dillerde
    lang_detect.classify_script_line ("block") / classify_latin_line.
    Muaf: tablo hücresi, Latince alıntı (lang_detect.classify_foreign_segment
    'la'), kaynağın dilinden açıkça başka dildeki alıntı (QC
    _is_source_foreign_quote'un baskınlık kuralı: SOURCE_QUOTE_LANG_DOMINANCE),
    ve sınıflandırıcıların eşiğine (en az 4 sözcük, işlev sözcüğü) takılmayan
    tek sözcük / özel ad.
    """
    if not raw or not source_lang or kind in ("cell", "code"):
        return False
    from core import lang_detect as ld
    from latex_pipeline.qc_engine import (SOURCE_QUOTE_LANG_DOMINANCE,
                                          _clean_for_language, _same_language)
    from latex_pipeline.retranslator import is_english_line

    text = " ".join(_clean_for_language(raw).split())
    if not text:
        return False
    if ld.classify_foreign_segment(text, source_lang=source_lang) == "la":
        return False
    if source_lang == "en":
        return is_english_line(text, min_words=4, min_length=20, is_heading=heading)
    verdict = ld.classify_script_line(text)
    code = verdict.code if verdict is not None and verdict.kind == "block" else None
    if code is None:
        code = ld.classify_latin_line(text, heading=heading)
    if code is None:
        return False
    if _same_language(code, source_lang):
        return True
    if code in ld.LATIN_PROFILE_LANGS and source_lang in ld.LATIN_PROFILE_LANGS:
        hits, _family = ld.profile_hits(ld.tokenize_words(text))
        return (hits.get(code, 0)
                < SOURCE_QUOTE_LANG_DOMINANCE * max(1, hits.get(source_lang, 0)))
    return False


def _write_coverage_report(out: Path, coverage: Dict, prose: int = 0) -> None:
    """compiled/epub_rapor.json (S7) + günlük satırı (pipeline.log)."""
    try:
        (out / "compiled").mkdir(parents=True, exist_ok=True)
        (out / "compiled" / EPUB_REPORT_NAME).write_text(
            json.dumps(coverage, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"[EPUB Output] {EPUB_REPORT_NAME} yazılamadı: {exc}")
    oran = f"%{coverage['kapsam'] * 100:.1f}".replace(".", ",")
    detail = (f" ({prose} blok çeviride aynen kalmış, dil denetimi kaynak "
              f"dilde düzyazı buldu)" if prose else "")
    _log_line(
        f"[EPUB Output] Türkçe EPUB kapsamı {oran}: "
        f"{coverage['toplam_blok'] - coverage['cevrilmemis_blok']}/"
        f"{coverage['toplam_blok']} blok çevrildi, "
        f"{coverage['cevrilmemis_blok']} blok kaynak dilde kaldı{detail}")


def _log_line(msg: str) -> None:
    """Satırı günlüğe (kök kaydedicinin işleyicileri: pipeline.log) yaz;
    konsol işleyicisi yoksa ayrıca ekrana bas (iki kez görünmesin)."""
    import logging
    import sys

    root = logging.getLogger()
    console = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, "stream", None) in (sys.stdout, sys.stderr)
        for h in root.handlers)
    if root.handlers:
        logging.getLogger(__name__).info(msg)
    if not console:
        print(msg)


_TITLEPAGE_RE = re.compile(r"\\begin\{titlepage\}(.*?)\\end\{titlepage\}", re.S)
_TITLE_HUGE_RE = re.compile(r"\{\\Huge\b")
_TITLE_CMD_RE = re.compile(r"\\title\s*\{")


def _translated_book_title(translated_tex: str) -> Optional[str]:
    """Çevirideki kitap adı (Türkçe dc:title / İçindekiler başlığı).

    Dönüştürücünün başlık sayfası: \\begin{titlepage} içindeki
    '{\\Huge\\bfseries Ad}' grubu; yoksa \\title{…}. Bulunamazsa None
    (EPUB'un özgün adı kalır).
    """
    m = _TITLEPAGE_RE.search(translated_tex)
    if m:
        page = m.group(1)
        h = _TITLE_HUGE_RE.search(page)
        if h:
            title = _tex_to_epub_text("{" + _braced_arg(page, h.start()) + "}")
            if title:
                return title
    t = _TITLE_CMD_RE.search(translated_tex)
    if t:
        title = _tex_to_epub_text(_braced_arg(translated_tex, t.end() - 1))
        if title:
            return title
    return None


def _resolve_source_epub(original_epub: str, out: Path) -> Optional[str]:
    """Orijinal EPUB yolu: parametre, eşlem dosyasındaki yol, output kopyası."""
    if original_epub and Path(original_epub).is_file():
        return original_epub
    mapping_path = out / "epub_segment_mapping.json"
    if mapping_path.exists():
        try:
            cand = json.loads(mapping_path.read_text(encoding="utf-8")).get(
                "epub_path", "")
        except (OSError, ValueError, AttributeError):
            cand = ""
        if cand and Path(cand).is_file():
            return cand
    fallback = out / "source_book.epub"
    if fallback.is_file():
        return str(fallback)
    return None


def _alignment_problem(report: Dict) -> Optional[str]:
    """Hizalama raporu EPUB üretmeye yetmiyorsa tek satır Türkçe neden.

    Neden "EPUB üretilmedi" önekini taşımaz: 2_derle ve arayüz notu bu
    önekle gösterir (S4/S5).
    """
    if report["counted"] == 0:
        return "Kaynak EPUB'da çevrilecek paragraf bulunamadı."
    if report.get("too_large"):
        return (
            "Çevirinin paragraf yapısı kaynaktan hizalama "
            "sınırını aşacak kadar farklı (çok sayıda paragraf eklenmiş, "
            "silinmiş ya da birleştirilmiş). PDF etkilenmedi."
        )
    spread = report.get("length_spread")
    if spread is not None and spread > MAX_LENGTH_SPREAD:
        return (
            "Çevrilmiş metnin paragraf uzunlukları kaynakla "
            f"ilişkisiz görünüyor (yayılım {_fmt_float(spread)}, en çok "
            f"{_fmt_float(MAX_LENGTH_SPREAD)}); çeviri bu kaynağın ya da bu "
            "çıkarımın çevirisi olmayabilir. PDF etkilenmedi."
        )
    if report["coverage"] < MIN_ALIGNED_COVERAGE:
        # Oran aşağı yuvarlanır: %89,7 '%90' görünüp eşikle çelişmesin.
        shown = math.floor(report["coverage"] * 100)
        return (
            "Kaynak EPUB'daki paragrafların çeviriyle güvenle eşleşme oranı "
            f"%{shown} (en az %{MIN_ALIGNED_COVERAGE * 100:.0f} gerekir); yanlış "
            "yere yazılmış çeviri içeren bir EPUB üretmek yerine atlandı. PDF "
            "etkilenmedi."
        )
    return None


def _fmt_float(value: float) -> str:
    """Türkçe ondalık gösterim (virgül): 0.45 -> '0,45'."""
    return f"{value:.2f}".replace(".", ",")


def _fmt_spread(spread: Optional[float]) -> str:
    return "kestirilemedi" if spread is None else _fmt_float(spread)


def _write_skip_note(out: Path, reason: str, stale_epub: Path) -> None:
    """S5: nedeni compiled/EPUB_ATLANDI.txt'ye tek satır yaz (yalnız neden;
    "EPUB üretilmedi" önekini okuyan taraf koyar).

    Önceki koşudan kalan Türkçe EPUB (aynı ad) silinir: not "EPUB
    üretilmedi" derken yanında eski (ör. pozisyonel eşlemeyle kaymış) EPUB
    durmamalı. Başarılı koşu bu dosyanın üzerine zaten yazar.
    """
    line = " ".join(reason.split())
    try:
        if stale_epub.is_file():
            stale_epub.unlink()
            line += f" Önceki koşudan kalan {stale_epub.name} silindi."
        # Eski EPUB'un kapsam raporu (S7) da gider: EPUB yokken kapsam yok.
        stale_report = out / "compiled" / EPUB_REPORT_NAME
        if stale_report.is_file():
            stale_report.unlink()
    except OSError:
        pass
    try:
        (out / "compiled").mkdir(parents=True, exist_ok=True)
        (out / "compiled" / EPUB_SKIP_NOTE_NAME).write_text(
            line + "\n", encoding="utf-8")
        root_note = out / EPUB_SKIP_NOTE_NAME
        if root_note.is_file():
            root_note.unlink()
    except OSError as exc:
        print(f"[EPUB Output] {EPUB_SKIP_NOTE_NAME} yazılamadı: {exc}")


def _clear_skip_notes(out: Path) -> None:
    """EPUB üretildi: eski atlama notlarını sil (S5)."""
    for note in (out / "compiled" / EPUB_SKIP_NOTE_NAME, out / EPUB_SKIP_NOTE_NAME):
        try:
            if note.is_file():
                note.unlink()
        except OSError:
            pass


def _html_safe(parser, text: str) -> str:
    """rebuild_epub'a verilecek metin.

    rebuild_epub metinde markdown işareti (bağlantı, **kalın**, *italik*)
    bulursa sonucu HTML olarak ayrıştırır; o zaman metindeki '<' etiket,
    '&' varlık sanılır ('a<b' metni '<b>' öğesi olurdu). Yalnız bu durumda
    metin HTML kaçışlanır; işaretsiz metin öğeye düz yazıldığı için
    olduğu gibi kalır.
    """
    return parser._html_safe_text(text)


# ---------------------------------------------------------------------------
# Hizalama (E2)
# ---------------------------------------------------------------------------

def align_translations(
    segments: Sequence[Dict],
    source_tex: str,
    translated_tex: str,
) -> Tuple[Dict[int, str], Dict]:
    """Segment indeksi -> çeviri metni eşlemi ve hizalama raporu.

    *segments*: EPUBParser.extract_segments çıktısı (rebuild_epub ile aynı
    sıra). *source_tex*: kaynak LaTeX gövdesi (body.tex / EXPORT.tex).
    *translated_tex*: çevrilmiş gövde (translated_body.tex).

    Değer "" olan segment, çevirisi bir önceki segmentin çevirisinde
    olandır (çevirmen iki paragrafı tek paragrafta vermiş ya da dönüştürücü
    iki segmenti tek birime birleştirmiş): öğesi boşaltılır. Boşaltma yalnız
    birleşik çeviri önceki segmente gerçekten yazıldıysa yapılır (metin
    kaybolmaz). Eşlemde olmayan segment kaynak dilde kalır.
    """
    src_units = _tex_units(source_tex)
    tr_units = _tex_units(translated_tex)

    code_flags = _code_segment_flags(segments)
    seg_tokens: List[Optional[List[str]]] = []
    for seg, is_code in zip(segments, code_flags):
        # Basılı içindekiler tablosunun hücreleri (dönüştürücü bilerek atar;
        # epub_parser._mark_printed_toc) kod gibi çevrilebilir sayılmaz:
        # EPUB'da özgün kalır, kapsama girmez (önceden hiç segment değildi).
        toks = (None if is_code or seg.get("role") == "printed_toc"
                else _segment_tokens(seg.get("text", "")))
        seg_tokens.append(toks or None)
    unit_tokens = [None if u.kind == "code" else (u.tokens or None)
                   for u in src_units]

    groups = _align_same_language(seg_tokens, unit_tokens)
    unit_out, pair_stats = _align_cross_language(src_units, tr_units)

    notes = _NoteResolver(segments, src_units, tr_units)
    translations: Dict[int, str] = {}
    kept: set = set()     # hizalandı, çeviri kaynakla aynı: öğe özgün kalır
    # KARAR 18: aynen kalan segmentin kaynak LaTeX'i ve birim türü (kapsam
    # raporunun dil denetimi için).
    kept_src: Dict[int, Tuple[str, str]] = {}
    placed: set = set()   # metni bir segmente yazılmış kaynak birimler
    for seg_ids, unit_ids, _sim in groups:
        if (any(src_units[u].titlepage for u in unit_ids)
                and all(segments[s].get("role") == "title_page" for s in seg_ids)):
            continue    # başlık sayfası: _place_title_page yerleştirir
        if not all(u in unit_out for u in unit_ids):
            continue
        vals = [unit_out[u] for u in unit_ids]
        # "" = birim çeviride bir önceki kaynak birimle birleşmiş: metni o
        # birimin segmentindedir. O birim (bu grupta ya da önceki bir
        # grupta) yazılmadıysa boşaltmak metni kaybettirir; grup atlanır.
        ok = True
        for pos, (u, v) in enumerate(zip(unit_ids, vals)):
            if v == "" and not (u - 1 in placed
                                or (pos > 0 and vals[pos - 1] != "")):
                ok = False
                break
        if not ok:
            continue
        if all(v is None for v in vals):
            kept.update(seg_ids)
            raw = "\n".join(src_units[u].raw for u in unit_ids)
            for sid in seg_ids:
                kept_src[sid] = (raw, src_units[unit_ids[0]].kind)
            placed.update(unit_ids)
            continue
        # Dipnot yer tutucuları birim başına çözülür (kaynak birim metni
        # kaynağın, çeviri metni çevirinin numaralarını taşır).
        refs = notes.refs_of(seg_ids)
        texts = []
        for u, v in zip(unit_ids, vals):
            if v == "":
                continue
            if v is None:
                texts.append(notes.resolve(src_units[u].text, refs, source=True))
            else:
                texts.append(notes.resolve(v, refs, source=False))
        text = " ".join(t for t in texts if t)
        if not text:
            # Bütün birimler öncekine birleşmiş: segmentler boşaltılır.
            for sid in seg_ids:
                translations[sid] = ""
            placed.update(unit_ids)
            continue
        first = segments[seg_ids[0]]
        unit0 = src_units[unit_ids[0]]
        if len(seg_ids) == 1:
            if len(unit_ids) == 1 and first.get("type") == "heading":
                text = _restore_heading_label(
                    first.get("text", ""), unit0.tokens, text)
            elif len(unit_ids) == 1:
                text = _restore_item_number(first.get("text", ""), unit0.kind, text)
            translations[seg_ids[0]] = text
        else:
            head, sub = (_split_subtitle(segments, seg_ids, unit0.kind, text)
                         or (text, None))
            if sub is not None:
                # Başlık + alt ad dönüştürücüde tek başlık olmuş ("Başlık —
                # alt ad", C1/R8b): iki öğeye ayrı yazılır. Etiket geri
                # koyma kaynak başlığın alt adsız sözcükleriyle denetlenir.
                head_tokens = _tex_tokens(_split_subtitle_raw(unit0.raw))
                translations[seg_ids[0]] = _restore_heading_label(
                    first.get("text", ""), head_tokens, head)
                translations[seg_ids[1]] = sub
            else:
                # İki segment dönüştürücüde tek birime birleşmiş: çeviri ilk
                # segmente, sonraki boşaltılır (metni ilkinde).
                translations[seg_ids[0]] = text
                for sid in seg_ids[1:]:
                    translations[sid] = ""
        placed.update(unit_ids)

    # Kaynak dipnotlarının çevirisi EPUB'daki not öğelerine; yalnız dipnot
    # taşıyan (dönüştürücüde yazılmayan) not bölümü başlığı Türkçeye; bölüm
    # alt adı TeX başlığı olmuş yalın etiket ("CAPÍTULO VII") Türkçe etikete.
    notes.fill_note_segments(translations, kept)
    _place_title_page(segments, seg_tokens, source_tex, translated_tex,
                      translations, kept)
    for i, seg in enumerate(segments):
        if i in translations or i in kept or not seg_tokens[i]:
            continue
        if seg.get("role") == "notes_heading" and notes.any_note_written:
            translations[i] = EPUB_NOTES_HEADING_TR
        elif seg.get("role") == "printed_toc_heading":
            translations[i] = EPUB_TOC_TITLE_TR
        elif seg.get("type") == "heading":
            label = _bare_label_tr(seg.get("text", ""))
            if label is not None:
                translations[i] = label

    counted = sum(1 for t in seg_tokens if t)
    done = sum(1 for i in set(translations) | kept if seg_tokens[i])
    matched = sum(len(s) for s, _u, _x in groups)
    report = {
        "segments": len(segments),
        "counted": counted,
        "source_matched": matched,
        "translated": done,
        "kept_original": len(kept),
        "coverage": (done / counted) if counted else 0.0,
        "source_units": len(src_units),
        "translated_units": len(tr_units),
        **pair_stats,
        # E2 (2026-09-28): çeviride aynen kalan segmentler (S7 kapsamında
        # çevrilmiş sayılır), sayılan (çevrilebilir) segmentler ve yeni
        # notlar (numara -> metin; rebuild_epub belge sonuna yazar).
        "kept_ids": sorted(kept),
        "kept_src": kept_src,
        "counted_ids": [i for i, t in enumerate(seg_tokens) if t],
        "notes": dict(notes.new_notes),
    }
    return translations, report


# --- LaTeX birimleri --------------------------------------------------------

# İsteğe bağlı kısa başlık argümanı da tanınır: dönüştürücü dipnotlu
# başlığı ``\section[{kısa}]{uzun\footnote{…}}`` biçiminde yazar (C2); eskiden
# desen yalnız ``\section{`` bekliyordu, başlık "para" birimi oluyordu
# (Vson 28 Eylül 2026; Vq/C2-2). Birim metni ZORUNLU argümandır (dipnot
# _make_unit'te ayrılır).
_UNIT_HEADING_RE = re.compile(
    r"^\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)"
    r"\*?\s*(?:\[(?:[^\[\]{}]|\{[^{}]*\})*\]\s*)?\{"
)
_UNIT_CAPTION_RE = re.compile(r"^\\caption\*?\s*\{")
_UNIT_ITEM_RE = re.compile(r"^\\item\b\s*(?:\[[^\]]*\])?\s*")
_UNIT_ENV_RE = re.compile(r"^\\(begin|end)\{([^}]*)\}")
_UNIT_CODE_ENVS = frozenset({"verbatim", "verbatim*", "lstlisting", "Verbatim",
                             "minted", "alltt"})
# \begin{ortam} satırında ortam argümanı olan ({...} sayısı) ortamlar: sütun
# tanımı, genişlik metin değildir ('\begin{tabular}{l l}' -> birim yok).
_ENV_BRACE_ARGS = {
    "tabular": 1, "tabular*": 2, "tabularx": 2, "tabulary": 2,
    "longtable": 1, "array": 1, "minipage": 1, "multicols": 1,
    "wrapfigure": 2, "wraptable": 2,
}
# A2 yapı özellikleri: çevirmen satır içi komutları korur.
_FEATURE_RES = (
    re.compile(r"\\(?:textit|emph)\b"),
    re.compile(r"\\textbf\b"),
    re.compile(r"\\(?:href|url)\b"),
    re.compile(r"\\footnote\b"),
    re.compile(r"\\(?:cite|ref|eqref|pageref)\b"),
    re.compile(r"(?<!\\)\$"),
    # Dönüştürücü alıntı paragrafını '> ' önekiyle yazar; çevirmen korur.
    re.compile(r"^\s*>", re.M),
)
# _FEATURE_RES'teki \footnote sayacının yeri (kaynak dipnotu sayısı eklenir).
_FOOTNOTE_FEATURE = 3
_NUMBER_TOKEN_RE = re.compile(r"\d+")
_HREF_URL_RE = re.compile(r"\\href\s*\{[^{}]*\}")
_NON_TEXT_CMD_RE = re.compile(
    r"\\(?:url|label|ref|eqref|pageref|cite|includegraphics)\*?"
    r"(?:\[[^\]]*\])?\{[^{}]*\}"
)
_ANY_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?")
_ANCHOR_ARG_RE = re.compile(
    r"\\(?:href|url|label|ref|eqref|pageref|cite)\*?\s*(?:\[[^\]]*\])?\{([^{}]*)\}")
_WORD_RE = re.compile(r"\w+")


@dataclass
class _Unit:
    """Kaynak/çeviri LaTeX gövdesinin hizalama birimi.

    kind: 'h:<komut>' (başlık), 'caption', 'item', 'para' ya da 'code'.
    raw: birimin LaTeX metni (başlıkta argüman, \\item'da öğe metni).
    notes: birimdeki dipnotlar [(genel no, tür, EPUB metni)] (E2,
    2026-09-28). Dipnot metni ``text``'e girmez; yerinde _NOTE_PH yer
    tutucusu durur. tokens (A1) dipnotsuzdur: EPUB'da dipnot ayrı öğedir.
    """
    kind: str
    raw: str
    text: str = ""
    tokens: List[str] = field(default_factory=list)
    length: int = 0
    feats: Tuple[int, ...] = ()
    numbers: Counter = field(default_factory=Counter)
    anchors: Counter = field(default_factory=Counter)
    rare: frozenset = frozenset()
    key: str = ""
    notes: List[Tuple[int, str, str]] = field(default_factory=list)
    # \begin{titlepage} içinden (başlık sayfası: kitap adı + yazarlar)
    titlepage: bool = False


def _tex_units(tex: str) -> List[_Unit]:
    """LaTeX gövdesini sıralı birimlere böl.

    Paragraf (boş satırla ayrılan metin), \\item, başlık komutu, \\caption ve
    kod ortamı ayrı birimdir; \\begin/\\end satırı paragrafı bitirir, yorum
    satırı atlanır. _extract_text_from_tex'ten farkı: liste öğeleri ve
    ortam sınırları birleşmez (her EPUB liste öğesi ayrı segmenttir).
    """
    units: List[_Unit] = []
    cur: List[str] = []
    cur_kind = "para"
    code: Optional[List[str]] = None
    table: Optional[List[str]] = None   # tabular içeriği (F1-01)
    n_notes = 0   # dipnot yer tutucularının genel numarası (belgede tekil)
    in_titlepage = False

    def add(kind: str, raw: str) -> None:
        nonlocal n_notes
        unit = _make_unit(kind, raw, note_base=n_notes)
        unit.titlepage = in_titlepage
        n_notes += len(unit.notes)
        units.append(unit)

    def flush() -> None:
        nonlocal cur, cur_kind
        if cur:
            add(cur_kind, "\n".join(cur))
        cur = []
        cur_kind = "para"

    for line in tex.split("\n"):
        s = line.strip()
        if code is not None:
            m = _UNIT_ENV_RE.match(s)
            if m and m.group(1) == "end" and m.group(2) in _UNIT_CODE_ENVS:
                add("code", "\n".join(code))
                code = None
            else:
                code.append(line)
            continue
        if table is not None:
            # F1-01 (2026-09-29): tablo hücresi ayrı birimdir (EPUB'da her
            # hücre ayrı segment); satır '\\', hücre '&' ile ayrılır.
            end = _TABULAR_END_RE.search(line)
            if end is None:
                table.append(line)
                continue
            table.append(line[:end.start()])
            for cell in _table_cells("\n".join(table)):
                add("cell", cell)
            table = None
            rest = line[end.end():].strip()
            if rest:
                cur.append(rest)
            continue
        if not s:
            flush()
            continue
        if s.startswith("%"):
            continue
        m = _UNIT_ENV_RE.match(s)
        if m:
            flush()
            if m.group(2) == "titlepage":
                in_titlepage = m.group(1) == "begin"
            if m.group(1) == "begin" and m.group(2) in _UNIT_CODE_ENVS:
                code = []
                continue
            rest = s[m.end():]
            if m.group(1) == "begin" and m.group(2) in _TABULAR_ENVS:
                table = [_skip_env_args(rest, m.group(2))]
                end = _TABULAR_END_RE.search(table[0])
                if end is not None:     # tek satırlık tablo
                    for cell in _table_cells(table[0][:end.start()]):
                        add("cell", cell)
                    table = None
                continue
            if m.group(1) == "begin":
                rest = _skip_env_args(rest, m.group(2))
            rest = rest.strip()
            if rest:
                cur.append(rest)
            continue
        m = _UNIT_HEADING_RE.match(s)
        if m:
            flush()
            add("h:" + m.group(1), _braced_arg(s, m.end() - 1))
            continue
        m = _UNIT_CAPTION_RE.match(s)
        if m:
            flush()
            add("caption", _braced_arg(s, m.end() - 1))
            continue
        m = _UNIT_ITEM_RE.match(s)
        if m:
            flush()
            cur_kind = "item"
            cur.append(s[m.end():])
            continue
        cur.append(s)
    flush()
    if code is not None:
        add("code", "\n".join(code))
    if table is not None:
        for cell in _table_cells("\n".join(table)):
            add("cell", cell)
    return units


# F1-01 (2026-09-29): tablo ortamları ve hücre ayrıştırması.
_TABULAR_ENVS = frozenset({"tabular", "tabular*", "tabularx", "tabulary",
                           "longtable", "longtable*"})
_TABULAR_END_RE = re.compile(r"\\end\{(?:tabular\*?|tabularx|tabulary|longtable\*?)\}")
_TABLE_RULE_RE = re.compile(
    r"\\(?:toprule|midrule|bottomrule|hline|endhead|endfirsthead|endfoot|endlastfoot)\b"
    r"(?:\[[^\]]*\])?|\\(?:cline|cmidrule)(?:\([^)]*\))?\{[^}]*\}|\\addlinespace(?:\[[^\]]*\])?")


def _split_top(text: str, sep: str) -> List[str]:
    """*text*'i ayraç derinliği 0'daki kaçışsız *sep*'ten böl ('\\\\' ya
    da '&'); '\\href{…&…}' içindeki '&' bölmez."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            if sep == "\\\\" and depth == 0 and text.startswith("\\\\", i):
                parts.append(text[start:i])
                i += 2
                # '\\\\[2pt]' satır aralığı seçeneği
                if i < len(text) and text[i] == "[":
                    close = text.find("]", i)
                    if close > 0:
                        i = close + 1
                start = i
                continue
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _table_cells(body: str) -> List[str]:
    """Tablo gövdesindeki dolu hücreler (okuma sırasıyla; çizgi
    komutları atılır, boş hücre birim değildir)."""
    body = _TABLE_RULE_RE.sub(" ", body)
    cells = []
    for row in _split_top(body, "\\\\"):
        for cell in _split_top(row, "&"):
            cell = " ".join(cell.split())
            if cell:
                cells.append(cell)
    return cells


def _skip_env_args(rest: str, env: str) -> str:
    """\\begin{ortam}'dan sonraki seçenek ([...]) ve ortam argümanlarını
    ({...}, _ENV_BRACE_ARGS) at; kalan metin döner ('[H]', '{l l}' ->
    '')."""
    braces = _ENV_BRACE_ARGS.get(env, 0)
    while True:
        stripped = rest.lstrip()
        if stripped.startswith("["):
            end = stripped.find("]")
            if end < 0:
                return stripped
            rest = stripped[end + 1:]
            continue
        if braces and stripped.startswith("{"):
            arg = _braced_arg(stripped, 0)
            rest = stripped[len(arg) + 2:]
            braces -= 1
            continue
        return rest


def _make_unit(kind: str, raw: str, note_base: int = 0) -> _Unit:
    """Birim ve hizalama özellikleri.

    Dipnotlar (E2, 2026-09-28) ayrılır: çevirmen notu uzunluk, yapı, sayı ve
    sözcük hesaplarına girmez (kaynakta yoktur; İtalyanca bir kitapta notlu iki paragraf
    uzunluk farkı yüzünden eşlenmiyor, komşusunu da düşürüyordu). Kaynak
    dipnotu iki tarafta da olduğundan uzunluk/yapı/sayı hesabında kalır ama
    A1 sözcüklerine girmez (EPUB'da dipnot ayrı öğedir). ``text``'te dipnotun
    yerinde yer tutucu durur.
    """
    unit = _Unit(kind=kind, raw=raw)
    unit.key = " ".join(raw.split())
    if kind == "code":
        unit.text = raw
        unit.length = len(unit.key)
        return unit
    main_ph, notes = _split_footnotes(raw, note_base)
    main = _NOTE_PH_RE.sub(" ", main_ph)
    src_notes = [body for _g, k, body in notes if k == NOTE_KIND_SOURCE]
    metric = " ".join([main] + src_notes)
    unit.notes = [(g, k, _tex_to_epub_text(body, emphasis=True))
                  for g, k, body in notes]
    unit.text = _tex_to_epub_text(main_ph, emphasis=True)
    unit.tokens = _tex_tokens(main)
    plain = _HREF_URL_RE.sub("", metric)
    plain = _ANY_CMD_RE.sub("", _NON_TEXT_CMD_RE.sub(" ", plain))
    unit.length = len(" ".join(re.sub(r"[{}]", "", plain).split()))
    feats = [len(r.findall(metric)) for r in _FEATURE_RES]
    feats[_FOOTNOTE_FEATURE] += len(src_notes)
    unit.feats = tuple(feats)
    unit.numbers = Counter(_NUMBER_TOKEN_RE.findall(_HREF_URL_RE.sub("", metric)))
    unit.anchors = Counter(a.strip() for a in _ANCHOR_ARG_RE.findall(metric))
    unit.rare = frozenset(t for t in _tex_tokens(metric)
                          if len(t) >= COGNATE_MIN_LENGTH and not t.isdigit())
    return unit


# Birim metninde dipnotun yeri: özel kullanım alanı karakterleri arasında
# genel dipnot numarası (kitap metninde bulunmaz; TeX komutu değildir).
_NOTE_PH_OPEN, _NOTE_PH_CLOSE = "\ue000", "\ue001"
_NOTE_PH_RE = re.compile(_NOTE_PH_OPEN + r"(\d+)" + _NOTE_PH_CLOSE)
_FOOTNOTE_OPEN_RE = re.compile(r"\\footnote\s*(?:\[[^\]]*\]\s*)?\{")


def _brace_end(text: str, open_idx: int) -> int:
    """``text[open_idx]`` '{' ise eşleşen '}' konumu; kapanmıyorsa -1
    (kaçışlı ayraçlar sayılmaz)."""
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_footnotes(raw: str, note_base: int = 0
                     ) -> Tuple[str, List[Tuple[int, str, str]]]:
    """\\footnote{…}'ları ayır: (yer tutuculu metin, [(genel no, tür, gövde)]).

    Tür çevirmen notu açılışıyla (patterns.TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE)
    belirlenir; öteki dipnotlar kaynak dipnotudur. Kapanmayan dipnot
    ayrılmaz (metinde kalır).
    """
    out: List[str] = []
    notes: List[Tuple[int, str, str]] = []
    pos = 0
    for m in _FOOTNOTE_OPEN_RE.finditer(raw):
        if m.start() < pos:
            continue            # önceki dipnotun içinde
        end = _brace_end(raw, m.end() - 1)
        if end < 0:
            break
        kind = (NOTE_KIND_TRANSLATOR
                if TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE.match(raw, m.start())
                else NOTE_KIND_SOURCE)
        gid = note_base + len(notes)
        out.append(raw[pos:m.start()])
        out.append(f"{_NOTE_PH_OPEN}{gid}{_NOTE_PH_CLOSE}")
        notes.append((gid, kind, raw[m.end():end]))
        pos = end + 1
    if not notes:
        return raw, []
    out.append(raw[pos:])
    return "".join(out), notes


def _fold(text: str) -> str:
    """Karşılaştırma biçimi: aksansız, büyük/küçük harf duyarsız."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


def _tex_tokens(raw: str) -> List[str]:
    """LaTeX biriminin sözcükleri (komut adları, bağlantı adresleri hariç)."""
    text = _HREF_URL_RE.sub("", raw)
    text = _NON_TEXT_CMD_RE.sub(" ", text)
    text = _ANY_CMD_RE.sub(" ", text)
    return _WORD_RE.findall(_fold(text))


def _segment_tokens(text: str) -> List[str]:
    """Segment (markdown) sözcükleri (bağlantı adresi, görsel hariç).

    2026-09-29 (E kulvarı): <sup>/<sub> işaretinin etiket adı ve formüldeki
    ('$\\frac{a}{b}$', MathML'den) TeX komut adı sözcük sayılmaz (LaTeX
    birimi tarafında da komut adları atılır; _tex_tokens)."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"</?(?:sup|sub)>", " ", text)
    if "$" in text:
        # Kaçışlı '\$' (düz dolar, F1-02) formül sınırı değildir.
        text = text.replace("\\$", " ")
        text = re.sub(r"\$\$?[^$]+\$\$?",
                      lambda m: _ANY_CMD_RE.sub(" ", m.group(0)), text)
    return _WORD_RE.findall(_fold(text))


def _code_segment_flags(segments: Sequence[Dict]) -> List[bool]:
    """Kod bloğu segmentleri (``` ile açılan blok boş satırda bölünmüşse
    aradaki parçalar da). Kod çevrilmez; EPUB'da özgün kalır."""
    flags = []
    open_chapter = None
    for seg in segments:
        text = (seg.get("text") or "").strip()
        chapter = seg.get("chapter")
        if open_chapter is not None and chapter != open_chapter:
            open_chapter = None
        if open_chapter is not None:
            flags.append(True)
            if text.endswith("```"):
                open_chapter = None
            continue
        if text.startswith("```"):
            flags.append(True)
            if not (len(text) > 3 and text.endswith("```")):
                open_chapter = chapter
            continue
        flags.append(False)
    return flags


_HEADING_NUMBER_RE = re.compile(r"^\s*(?:[IVXLCDM]+|\d+)[.):]?\s+")


def _restore_heading_label(seg_text: str, unit_tokens: List[str],
                           translated: str) -> str:
    """Dönüştürücünün başlıktan attığı bölüm etiketini/numarasını geri koy.

    - Etiket ("Chapter 7. Calibration", "CAPÍTULO VII. X", "Zweites
      Kapitel: X"; converter.parse_heading_label): Türkçe etiketle ("Bölüm
      7: Ayar"). Çevirmen etiketi zaten yazdıysa dokunulmaz.
    - Yalın numara ("I LE GUN-CLUB" -> \\chapter{LE GUN-CLUB}): kaynaktaki
      Roma/Arap rakamı öneki olduğu gibi.

    Yalnız etiketten sonraki metin birimin sözcükleriyle birebir aynıysa
    (etiketi dönüştürücü atmışsa) uygulanır.
    """
    try:
        from latex_pipeline.converter import parse_heading_label
    except ImportError:  # pragma: no cover
        parse_heading_label = None
    if parse_heading_label is not None:
        label = parse_heading_label(seg_text)
        if (label is not None and label.title
                and _segment_tokens(label.title) == unit_tokens):
            if parse_heading_label(translated) is not None:
                return translated
            word = (EPUB_PART_LABEL_TR if label.kind == "part"
                    else EPUB_CHAPTER_LABEL_TR)
            return f"{word} {label.value}: {translated}"
    m = _HEADING_NUMBER_RE.match(seg_text)
    if not m:
        return translated
    if _segment_tokens(seg_text[m.end():]) != unit_tokens:
        return translated
    prefix = m.group(0).strip()
    if translated.lstrip().startswith(prefix):
        return translated
    return f"{prefix} {translated}"


_ITEM_NUMBER_RE = re.compile(r"^(\d{1,3}[.)])\s+")


def _restore_item_number(seg_text: str, unit_kind: str, translated: str) -> str:
    """Numaralı paragrafın numarasını geri koy (C1: '<p>1. …</p>' TeX'te
    tek enumerate'in \\item'ı olur, numara metinden atılır; EPUB öğesinde
    numara metnin parçasıdır). Liste öğesi (li, '- ' önekli) dokunulmaz."""
    if unit_kind != "item":
        return translated
    m = _ITEM_NUMBER_RE.match(seg_text)
    if not m or translated.lstrip().startswith(m.group(1)):
        return translated
    return f"{m.group(1)} {translated}"


def _split_subtitle(segments: Sequence[Dict], seg_ids: Sequence[int],
                    unit_kind: str, text: str) -> Optional[Tuple[str, str]]:
    """Başlık + alt ad (role 'subtitle') tek TeX başlığına birleşmişse
    ("Başlık — alt ad", converter._TITLE_JOIN_SEP) çeviriyi ikiye böl;
    ayraç çeviride yoksa None."""
    if (len(seg_ids) != 2 or not unit_kind.startswith("h:")
            or segments[seg_ids[1]].get("role") != "subtitle"):
        return None
    try:
        from latex_pipeline.converter import _TITLE_JOIN_SEP as sep
    except ImportError:  # pragma: no cover
        return None
    if sep not in text:
        return None
    head, sub = text.rsplit(sep, 1)
    if not head.strip() or not sub.strip():
        return None
    return head.strip(), sub.strip()


def _place_title_page(segments: Sequence[Dict], seg_tokens, source_tex: str,
                      translated_tex: str, translations: Dict[int, str],
                      kept: set) -> None:
    """Başlık sayfasına taşınan (E1 R18, role 'title_page') ve gövdede
    hizalanmayan segmentler: kitap adı çevirideki başlık sayfası adını alır;
    başlık sayfasındaki yazar adı (çevrilmez) özgün kalır ve çevrilmiş
    sayılır."""
    pending = [i for i, seg in enumerate(segments)
               if seg.get("role") == "title_page" and seg_tokens[i]
               and i not in translations and i not in kept]
    if not pending:
        return
    m = _TITLEPAGE_RE.search(source_tex)
    page_tokens = set(_tex_tokens(m.group(1))) if m else set()
    src_title = _translated_book_title(source_tex)
    tr_title = _translated_book_title(translated_tex)
    title_tokens = Counter(_segment_tokens(src_title)) if src_title else Counter()
    for i in pending:
        toks = seg_tokens[i]
        if (tr_title and title_tokens and _dice_counts(
                Counter(toks), len(toks), title_tokens,
                sum(title_tokens.values())) >= SAME_LANG_MIN_SIMILARITY):
            translations[i] = tr_title
        elif page_tokens and set(toks) <= page_tokens:
            kept.add(i)


def _split_subtitle_raw(raw: str) -> str:
    """Kaynak başlığın alt adsız kısmı ("Başlık — alt ad" -> "Başlık")."""
    try:
        from latex_pipeline.converter import _TITLE_JOIN_SEP as sep
    except ImportError:  # pragma: no cover
        return raw
    return raw.rsplit(sep, 1)[0] if sep in raw else raw


def _bare_label_tr(seg_text: str) -> Optional[str]:
    """Yalın bölüm/kısım etiketi ("CAPÍTULO VII", "Zweites Kapitel") ->
    Türkçe etiket ("Bölüm 7"); etiket değilse ya da adı varsa None.
    Dönüştürücü yalın etiketi atıp alt adı bölüm adı yaptığında (R8b)
    EPUB'daki etiket öğesi hizalanmaz; kaynak dilde kalmasın."""
    try:
        from latex_pipeline.converter import parse_heading_label
    except ImportError:  # pragma: no cover
        return None
    label = parse_heading_label(seg_text)
    if label is None or label.title:
        return None
    word = EPUB_PART_LABEL_TR if label.kind == "part" else EPUB_CHAPTER_LABEL_TR
    return f"{word} {label.value}"


class _NoteResolver:
    """Birim metnindeki dipnot yer tutucularını EPUB işaretlerine çevirir.

    - Çevirmen notu ve işareti bulunamayan kaynak dipnotu: yeni not
      (EPUB_NOTE_REF; metni ``new_notes``, rebuild_epub belge sonuna
      <aside epub:type="footnote"> yazar).
    - Kaynak dipnotu: paragraf segmentinin ``note_refs`` sırasıyla (E1
      sözleşmesi: işaret sırası = \\footnote sırası) EPUB'daki notun
      anahtarı (EPUB_SOURCE_NOTE; rebuild_epub özgün işaret bağlantısını
      yerine koyar); çevirisi ``fill_note_segments``'le not öğesine yazılır.
    """

    def __init__(self, segments: Sequence[Dict], src_units: Sequence[_Unit],
                 tr_units: Sequence[_Unit]):
        self.segments = segments
        self.src = {g: (k, t) for u in src_units for g, k, t in u.notes}
        self.tr = {g: (k, t) for u in tr_units for g, k, t in u.notes}
        self.note_segs: Dict[str, List[int]] = {}
        for i, seg in enumerate(segments):
            key = seg.get("note")
            if key:
                self.note_segs.setdefault(key, []).append(i)
        self.pending: Dict[str, str] = {}
        self.new_notes: Dict[int, str] = {}
        self.any_note_written = False

    def refs_of(self, seg_ids: Sequence[int]) -> List[str]:
        """Grubun dipnot işaretleri (sırayla); resolve tükettikçe azalır."""
        refs: List[str] = []
        for sid in seg_ids:
            refs.extend(self.segments[sid].get("note_refs") or [])
        return refs

    def resolve(self, text: str, refs: List[str], source: bool) -> str:
        if _NOTE_PH_OPEN not in text:
            return text
        table = self.src if source else self.tr

        def sub(m: "re.Match") -> str:
            kind, body = table.get(int(m.group(1)), (None, ""))
            if kind is None:
                return ""
            if kind == NOTE_KIND_SOURCE and refs:
                key = refs.pop(0)
                if key in self.note_segs:
                    if not source and body.strip():
                        self.pending.setdefault(key, body)
                    return f"{EPUB_SOURCE_NOTE_OPEN}{key}{EPUB_SOURCE_NOTE_CLOSE}"
            if not body.strip():
                return ""
            n = len(self.new_notes) + 1
            self.new_notes[n] = body
            return f"{EPUB_NOTE_REF_OPEN}{n}{EPUB_NOTE_REF_CLOSE}"

        return _NOTE_PH_RE.sub(sub, text)

    def fill_note_segments(self, translations: Dict[int, str], kept: set) -> None:
        """Çevrilmiş kaynak dipnotlarını not öğelerine yaz. Notun
        segmentlerinden biri başka yoldan (A1/A2: notu gövdede paragraf olan
        eski çıkarım) yazıldıysa dokunulmaz. Çok paragraflı notun metni ilk
        öğeye yazılır, ötekiler boşaltılır; Gutenberg '[* …]' / '[N …]'
        ayraçları korunur."""
        for key, body in self.pending.items():
            sids = self.note_segs.get(key) or []
            if not sids or any(i in translations or i in kept for i in sids):
                continue
            first = (self.segments[sids[0]].get("text") or "").strip()
            last = (self.segments[sids[-1]].get("text") or "").strip()
            mark = _gutenberg_note_mark(first)
            if mark is not None and last.endswith("]"):
                body = f"[{mark} {body}]"
            translations[sids[0]] = body
            for i in sids[1:]:
                translations[i] = ""
            self.any_note_written = True


def _gutenberg_note_mark(text: str) -> Optional[str]:
    """Gutenberg düz metin notunun işareti ('[* …' -> '*', '[12 …' -> '12');
    değilse None. Biçim epub_parser'ın not tanımasından (tek kaynak)."""
    from parsers.epub_parser import _GUT_NUM_OPEN_RE, _GUT_STAR_OPEN_RE

    if _GUT_STAR_OPEN_RE.match(text):
        return "*"
    m = _GUT_NUM_OPEN_RE.match(text)
    return m.group(1) if m else None


def _dice_counts(a: Counter, la: int, b: Counter, lb: int) -> float:
    if not la or not lb:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    common = sum(min(c, b[w]) for w, c in a.items() if w in b)
    return 2.0 * common / (la + lb)


# --- A1: aynı dil ------------------------------------------------------------

def _align_same_language(
    seg_tokens: Sequence[Optional[List[str]]],
    unit_tokens: Sequence[Optional[List[str]]],
) -> List[Tuple[Tuple[int, ...], Tuple[int, ...], float]]:
    """Segment ↔ kaynak birim grupları: [(segmentler, birimler, benzerlik)].

    Sözcük dizisi birebir aynı olanlar SequenceMatcher'la eşlenir; aradaki
    boşluklar ağırlıklı dinamik programlamayla (1:1, 1:2, 2:1; benzerlik
    SAME_LANG_MIN_SIMILARITY'nin altındaki çift eşlenmez) hizalanır. Sıra
    korunur (monoton). Sözcüksüz (None) öğeler hiçbir şeyle eşlenmez.
    """
    ks = [" ".join(t) if t else f"\x00s{i}" for i, t in enumerate(seg_tokens)]
    ku = [" ".join(t) if t else f"\x00u{j}" for j, t in enumerate(unit_tokens)]
    groups: List[Tuple[Tuple[int, ...], Tuple[int, ...], float]] = []
    sm = SequenceMatcher(None, ks, ku, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            groups.extend(((i1 + k,), (j1 + k,), 1.0) for k in range(i2 - i1))
        elif i2 > i1 and j2 > j1 and (i2 - i1) * (j2 - j1) <= MAX_GAP_CELLS:
            groups.extend(_align_gap(seg_tokens, unit_tokens, i1, i2, j1, j2))
    groups.sort(key=lambda g: g[0][0])
    return groups


def _align_gap(seg_tokens, unit_tokens, i1, i2, j1, j2):
    """A1 boşluğu: toplam benzerliği en büyük monoton eşleme."""
    n, m = i2 - i1, j2 - j1
    thr = SAME_LANG_MIN_SIMILARITY

    def bags(seq, lo, hi):
        out = []
        for x in seq[lo:hi]:
            out.append((Counter(x), len(x)) if x else None)
        return out

    sb, ub = bags(seg_tokens, i1, i2), bags(unit_tokens, j1, j2)

    def joined(bs, a, d):
        """bs[a-d .. a-1] birleşimi (biri sözcüksüzse None)."""
        parts = bs[a - d:a]
        if any(p is None for p in parts):
            return None
        if d == 1:
            return parts[0]
        c = Counter()
        n_tok = 0
        for pc, pn in parts:
            c.update(pc)
            n_tok += pn
        return c, n_tok

    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    for a in range(n + 1):
        for b in range(m + 1):
            if a == 0 and b == 0:
                continue
            best, arg = -1.0, None
            if a and score[a - 1][b] > best:
                best, arg = score[a - 1][b], (1, 0, 0.0)
            if b and score[a][b - 1] > best:
                best, arg = score[a][b - 1], (0, 1, 0.0)
            for da, db in ((1, 1), (1, 2), (2, 1)):
                if a < da or b < db:
                    continue
                st = joined(sb, a, da)
                ut = joined(ub, b, db)
                if st is None or ut is None:
                    continue
                sim = _dice_counts(st[0], st[1], ut[0], ut[1])
                if sim >= thr and score[a - da][b - db] + sim > best:
                    best, arg = score[a - da][b - db] + sim, (da, db, sim)
            score[a][b], back[a][b] = best, arg
    out = []
    a, b = n, m
    while a or b:
        da, db, sim = back[a][b]
        if da and db:
            out.append((tuple(range(i1 + a - da, i1 + a)),
                        tuple(range(j1 + b - db, j1 + b)), sim))
        a, b = a - da, b - db
    return out


# --- A2: diller arası --------------------------------------------------------

_NEG_INF = float("-inf")
# Geçiş türleri: (kaynak adımı, çeviri adımı). Sıra sabittir: 0=1:1,
# 1=1:0 (kaynak birimi çeviride yok), 2=0:1 (çeviride fazladan birim),
# 3=2:1 (iki kaynak birimi tek çeviri biriminde), 4=1:2 (bölünmüş çeviri).
_MOVES = ((1, 1), (1, 0), (0, 1), (2, 1), (1, 2))
_MERGEABLE_KINDS = frozenset({"para", "item"})


def _logsumexp(values) -> float:
    top = max(values)
    if top == _NEG_INF:
        return _NEG_INF
    return top + math.log(sum(math.exp(v - top) for v in values))


def _counter_diff(x: Counter, tx: int, y: Counter, ty: int) -> int:
    """Çok kümeler arası simetrik fark büyüklüğü (tx, ty: toplamlar)."""
    if not tx or not ty:
        return tx + ty
    small, big = (x, y) if len(x) <= len(y) else (y, x)
    common = sum(min(c, big[k]) for k, c in small.items() if k in big)
    return tx + ty - 2 * common


class _Side:
    """Bir tarafın (kaynak ya da çeviri) birim özellikleri.

    ``info[i]``: units[i]'nin yapı özellikleri (satır içi komut sayıları,
    sayılar, çapalar, ortak sözcükler); ``pair_info[i]``: units[i] +
    units[i+1] birleşimi (2:1 / 1:2 için; yalnız ikisi de birleşebilir
    türdeyse, değilse None). ``df``: nadir sözcüğün geçtiği birim sayısı.
    ``info`` iki taraf görüldükten sonra ``finalize`` ile kurulur.
    """

    def __init__(self, units: Sequence[_Unit]):
        self.units = units
        self.kind = [u.kind for u in units]
        self.key = [u.key for u in units]
        self.length = [u.length for u in units]
        self.df: Counter = Counter()
        for u in units:
            self.df.update(u.rare)
        self.pair_length = [units[i].length + units[i + 1].length
                            for i in range(len(units) - 1)]
        self.info: List[tuple] = []
        self.pair_info: List[Optional[tuple]] = []

    def finalize(self, weights: Dict[str, float]) -> None:
        units = self.units

        def info(feats, numbers, anchors, rare):
            cog = frozenset(w for w in rare if w in weights)
            return (feats, numbers, sum(numbers.values()), anchors,
                    sum(anchors.values()), cog, sum(weights[w] for w in cog))

        self.info = [info(u.feats, u.numbers, u.anchors, u.rare) for u in units]
        self.pair_info = []
        for i in range(len(units) - 1):
            a, b = units[i], units[i + 1]
            if a.kind == b.kind and a.kind in _MERGEABLE_KINDS:
                self.pair_info.append(info(
                    tuple(x + y for x, y in zip(a.feats, b.feats)),
                    a.numbers + b.numbers, a.anchors + b.anchors,
                    a.rare | b.rare))
            else:
                self.pair_info.append(None)


def _cognate_weights(src: "_Side", tgt: "_Side") -> Dict[str, float]:
    """İki tarafta da geçen (çevirmenin aynen bıraktığı) nadir sözcüğün
    ağırlığı: 1 / en büyük df."""
    return {w: 1.0 / max(src.df[w], tgt.df[w])
            for w in src.df.keys() & tgt.df.keys()}


def _feature_cost(ia: tuple, ib: tuple, weights: Dict[str, float]) -> float:
    """Yapı özellikleri farkı: satır içi komut sayıları, sayılar, çapalar ve
    ortak sözcük uyumu (değer negatif olabilir)."""
    fa, na, nta, aa, ata, ca, wa = ia
    fb, nb, ntb, ab, atb, cb, wb = ib
    cost = 0.0
    if fa != fb:
        cost = FEATURE_MISMATCH_COST * sum(min(abs(x - y), MAX_FEATURE_MISMATCH)
                                           for x, y in zip(fa, fb))
    if nta or ntb:
        cost += NUMBER_MISMATCH_COST * min(_counter_diff(na, nta, nb, ntb),
                                           MAX_NUMBER_MISMATCH)
    if ata and atb:
        # Yalnız iki tarafta da çapa varken: sanitizer'ın düzelttiği ya da
        # çevirmenin düşürdüğü bağlantı tek başına ceza almaz.
        cost += ANCHOR_MISMATCH_COST * min(_counter_diff(aa, ata, ab, atb),
                                           MAX_ANCHOR_MISMATCH)
    if wa or wb:
        # Çevirmenin başka yerde aynen bıraktığı sözcük (ad, terim) bu çiftte
        # iki tarafta da varsa ödül, yalnız birinde varsa ceza.
        shared = sum(weights[w] for w in ca & cb) if ca and cb else 0.0
        missing = wa + wb - 2.0 * shared
        cost += COGNATE_WEIGHT * (min(COGNATE_CAP, missing)
                                  - min(COGNATE_CAP, shared))
    return cost


class _LengthModel:
    """A2 uzunluk maliyeti (−log iki yanlı normal kuyruk olasılığı)."""

    def __init__(self, ratio: float, spread: float):
        self.ratio = min(MAX_LENGTH_RATIO, max(MIN_LENGTH_RATIO, ratio))
        self.spread = spread

    def delta(self, ls: int, lt: int) -> float:
        c = self.ratio
        scale = max(1.0, (ls + lt / c) / 2.0)
        var = LENGTH_VARIANCE * scale + (self.spread * c * scale) ** 2
        return (lt - c * ls) / math.sqrt(var)

    def cost(self, ls: int, lt: int) -> float:
        d = abs(self.delta(ls, lt))
        tail = math.erfc(d / math.sqrt(2.0))
        if tail <= 0.0:
            return MAX_LENGTH_COST
        return min(MAX_LENGTH_COST, -math.log(tail))


class _Block:
    """A2'nin bir bloğu: kaynak [s0, s1) ↔ çeviri [t0, t1) ve bantlı kafes.

    Hücre (i, j): bloğun kaynağından i, çevirisinden j birim tüketildi.
    Bant köşegenin iki yanında BAND_MIN_HALF_WIDTH + |n − m| birimdir (tek
    bir büyük atlama da sığar). Her geçiş puanları yeniden hesaplar ve
    bitince tabloları bırakır (bellek: bloklar sırayla, tek blok kadar).
    """

    def __init__(self, src: _Side, tgt: _Side, s0: int, s1: int, t0: int, t1: int,
                 weights: Optional[Dict[str, float]] = None):
        self.src, self.tgt = src, tgt
        self.weights = weights if weights is not None else {}
        self.s0, self.t0 = s0, t0
        n, m = s1 - s0, t1 - t0
        self.n, self.m = n, m
        if n and m:
            half = BAND_MIN_HALF_WIDTH + abs(n - m)
            self.lo = [max(0, round(i * m / n) - half) for i in range(n + 1)]
            self.hi = [min(m, round(i * m / n) + half) for i in range(n + 1)]
        else:
            self.lo = [0] * (n + 1)
            self.hi = [m] * (n + 1)
        self.cells = sum(h - lo + 1 for lo, h in zip(self.lo, self.hi))
        self.fwd = self.vit = self.ptr = self.bwd = None
        self.sc = None
        self.total = _NEG_INF

    # -- ön hesap --------------------------------------------------------
    def _features(self):
        """Hücre başına (1:1, 2:1, 1:2) yapı maliyetleri; geçiş yoksa None."""
        src, tgt, s0, t0 = self.src, self.tgt, self.s0, self.t0
        weights = self.weights
        rows = []
        for i in range(self.n + 1):
            row = []
            a = s0 + i - 1                             # 1:1 / 1:2 -> src[a]
            for j in range(self.lo[i], self.hi[i] + 1):
                f11 = f21 = f12 = None
                b = t0 + j - 1                         # 1:1 / 2:1 -> tgt[b]
                if i >= 1 and j >= 1 and src.kind[a] == tgt.kind[b]:
                    if src.key[a] == tgt.key[b]:
                        f11 = 0.0
                    else:
                        f11 = _feature_cost(src.info[a], tgt.info[b], weights)
                    # 2:1 -> src[a-1], src[a] (aynı birleşebilir tür)
                    if i >= 2 and src.pair_info[a - 1] is not None:
                        f21 = _feature_cost(src.pair_info[a - 1], tgt.info[b],
                                            weights)
                    # 1:2 -> tgt[b-1], tgt[b]
                    if j >= 2 and tgt.pair_info[b - 1] is not None:
                        f12 = _feature_cost(src.info[a], tgt.pair_info[b - 1],
                                            weights)
                row.append((f11, f21, f12))
            rows.append(row)
        return rows

    def _scores(self, model: _LengthModel):
        """Hücre başına (1:1, 2:1, 1:2) geçiş puanları (log olasılık)."""
        feat = self._features()
        src, tgt, s0, t0 = self.src, self.tgt, self.s0, self.t0
        cost = model.cost
        sc = []
        for i in range(self.n + 1):
            row = []
            lo = self.lo[i]
            for jj, (f11, f21, f12) in enumerate(feat[i]):
                j = lo + jj
                a, b = s0 + i - 1, t0 + j - 1
                s11 = s21 = s12 = _NEG_INF
                if f11 is not None:
                    if src.key[a] == tgt.key[b]:
                        s11 = LOG_PRIOR_MATCH
                    else:
                        s11 = (LOG_PRIOR_MATCH
                               - cost(src.length[a], tgt.length[b]) - f11)
                if f21 is not None:
                    s21 = (LOG_PRIOR_MERGE
                           - cost(src.pair_length[a - 1], tgt.length[b]) - f21)
                if f12 is not None:
                    s12 = (LOG_PRIOR_MERGE
                           - cost(src.length[a], tgt.pair_length[b - 1]) - f12)
                row.append((s11, s21, s12))
            sc.append(row)
        return sc

    # -- kafes -----------------------------------------------------------
    def get(self, mat, i, j) -> float:
        if i < 0 or i > self.n or j < self.lo[i] or j > self.hi[i]:
            return _NEG_INF
        return mat[i][j - self.lo[i]]

    def score(self, i, j, k) -> float:
        """(i, j) hücresine k türündeki geçişin puanı."""
        if k == 1 or k == 2:
            return LOG_PRIOR_SKIP
        cell = self.sc[i][j - self.lo[i]]
        return cell[0] if k == 0 else (cell[1] if k == 3 else cell[2])

    def run(self, model: _LengthModel, posteriors: bool) -> None:
        n, m, lo, hi = self.n, self.m, self.lo, self.hi
        self.sc = self._scores(model)
        self.fwd = [[_NEG_INF] * (hi[i] - lo[i] + 1) for i in range(n + 1)]
        self.vit = [[_NEG_INF] * (hi[i] - lo[i] + 1) for i in range(n + 1)]
        self.ptr = [[-1] * (hi[i] - lo[i] + 1) for i in range(n + 1)]
        self.bwd = None
        get, score = self.get, self.score
        self.fwd[0][0 - lo[0]] = 0.0
        self.vit[0][0 - lo[0]] = 0.0
        for i in range(n + 1):
            for j in range(lo[i], hi[i] + 1):
                if i == 0 and j == 0:
                    continue
                f_terms = []
                best, arg = _NEG_INF, -1
                for k, (di, dj) in enumerate(_MOVES):
                    pi, pj = i - di, j - dj
                    if pi < 0 or pj < 0:
                        continue
                    s = score(i, j, k)
                    if s == _NEG_INF:
                        continue
                    pv = get(self.vit, pi, pj)
                    if pv != _NEG_INF and pv + s > best:
                        best, arg = pv + s, k
                    if posteriors:
                        pf = get(self.fwd, pi, pj)
                        if pf != _NEG_INF:
                            f_terms.append(pf + s)
                if f_terms:
                    self.fwd[i][j - lo[i]] = _logsumexp(f_terms)
                self.vit[i][j - lo[i]] = best
                self.ptr[i][j - lo[i]] = arg
        self.total = get(self.fwd, n, m) if posteriors else _NEG_INF
        if posteriors and self.total != _NEG_INF:
            self._backward()

    def _backward(self) -> None:
        n, m, lo, hi = self.n, self.m, self.lo, self.hi
        self.bwd = [[_NEG_INF] * (hi[i] - lo[i] + 1) for i in range(n + 1)]
        self.bwd[n][m - lo[n]] = 0.0
        for i in range(n, -1, -1):
            for j in range(hi[i], lo[i] - 1, -1):
                if i == n and j == m:
                    continue
                terms = []
                for k, (di, dj) in enumerate(_MOVES):
                    ni, nj = i + di, j + dj
                    if ni > n or nj > m or nj < lo[ni] or nj > hi[ni]:
                        continue
                    nb = self.bwd[ni][nj - lo[ni]]
                    if nb == _NEG_INF:
                        continue
                    s = self.score(ni, nj, k)
                    if s != _NEG_INF:
                        terms.append(s + nb)
                if terms:
                    self.bwd[i][j - lo[i]] = _logsumexp(terms)

    def release(self) -> None:
        """Kafes tablolarını bırak (büyük kitapta bellek)."""
        self.fwd = self.vit = self.ptr = self.bwd = self.sc = None

    def path(self) -> List[Tuple[int, int, int]]:
        """Viterbi yolu, baştan sona: [(k, i, j)] — (i, j) hedef hücre."""
        out = []
        i, j = self.n, self.m
        if self.vit is None or self.get(self.vit, i, j) == _NEG_INF:
            return out
        while i or j:
            k = self.ptr[i][j - self.lo[i]]
            if k < 0:
                return []
            out.append((k, i, j))
            di, dj = _MOVES[k]
            i, j = i - di, j - dj
        out.reverse()
        return out

    def post(self, i, j, k) -> float:
        """(i, j) hücresine k türündeki geçişin sonsal olasılığı."""
        di, dj = _MOVES[k]
        if (self.bwd is None or i > self.n or j > self.m or j < self.lo[i]
                or j > self.hi[i] or i - di < 0 or j - dj < 0):
            return 0.0
        s = self.score(i, j, k)
        prev = self.get(self.fwd, i - di, j - dj)
        after = self.bwd[i][j - self.lo[i]]
        if s == _NEG_INF or prev == _NEG_INF or after == _NEG_INF:
            return 0.0
        return math.exp(min(0.0, prev + s + after - self.total))


def _block_bounds(src: Sequence[_Unit], tgt: Sequence[_Unit]
                  ) -> List[Tuple[int, int, int, int]]:
    """A2 blokları [(s0, s1, t0, t1)]. Bölüm/kısım başlıkları iki tarafta
    aynı sayıda ve aynı sırada türdeyse her biri bir blok başlatır; değilse
    tek blok (bütün kitap)."""
    si = [i for i, u in enumerate(src) if u.kind in _BLOCK_HEADING_KINDS]
    ti = [j for j, u in enumerate(tgt) if u.kind in _BLOCK_HEADING_KINDS]
    if (not si or len(si) != len(ti)
            or [src[i].kind for i in si] != [tgt[j].kind for j in ti]):
        return [(0, len(src), 0, len(tgt))]
    s_cuts = [0] + si + [len(src)]
    t_cuts = [0] + ti + [len(tgt)]
    out = []
    for k in range(len(s_cuts) - 1):
        s0, s1, t0, t1 = s_cuts[k], s_cuts[k + 1], t_cuts[k], t_cuts[k + 1]
        if s1 > s0 or t1 > t0:
            out.append((s0, s1, t0, t1))
    return out


def _estimate_length_model(pairs: Sequence[Tuple[_Unit, _Unit]]
                           ) -> Tuple[Optional[float], Optional[float], int]:
    """1:1 çiftlerden (c, k, örnek sayısı): c = log-oran medyanının üsteli,
    k = log-oran MAD'i × 1,4826 (normal dağılımda std). Örnek yetmezse
    (None, None, n)."""
    logs = []
    for s, t in pairs:
        if (s.kind == "code" or s.key == t.key or s.length < MIN_SPREAD_LENGTH
                or t.length <= 0):
            continue
        logs.append(math.log(t.length / s.length))
    if len(logs) < MIN_SPREAD_SAMPLE:
        return None, None, len(logs)
    logs.sort()
    med = logs[len(logs) // 2]
    dev = sorted(abs(x - med) for x in logs)
    mad = dev[len(dev) // 2]
    return math.exp(med), 1.4826 * mad, len(logs)


def _align_cross_language(
    src: Sequence[_Unit], tgt: Sequence[_Unit],
) -> Tuple[Dict[int, Optional[str]], Dict]:
    """Kaynak birim indeksi -> çeviri metni (yalnız güvenli eşleşmeler).

    None: birim çeviride birebir aynı kalmış (özgün EPUB öğesi korunur).
    "": birim çeviride bir önceki kaynak birimle birleşmiş (metni onda).
    İki geçiş: (1) varsayılan uzunluk modeliyle Viterbi yolu, c ve k
    kestirimi; (2) kestirilen modelle ileri-geri DP. Viterbi yolundaki her
    eşleşme (1:1, 2:1, 1:2) için kaynak birim a'nın çevirisinin b ile
    başlama sonsalı (1:1 + a'nın başı olduğu 2:1 + 1:2) MIN_PAIR_POSTERIOR'u
    geçmelidir; geçmezse a kaynak dilde kalır (yanlış yere yazılmaz).
    İçerik yolun kararıdır (1:2'de iki parça). 2:1'de a+1 yalnız birleşmenin
    kendi sonsalı eşiği geçerse boşaltılır; geçmezse kaynak dilde kalır
    (metin kaybolmaz, en kötü iki dilde görünür).
    """
    stats = {"pair_total": 0, "pair_confident": 0, "spread_sample": 0,
             "length_spread": None, "length_ratio": None, "blocks": 0,
             "band_cells": 0, "too_large": False}
    if not src or not tgt:
        return {}, stats

    s_side, t_side = _Side(src), _Side(tgt)
    weights = _cognate_weights(s_side, t_side)
    s_side.finalize(weights)
    t_side.finalize(weights)
    blocks = [_Block(s_side, t_side, *b, weights=weights)
              for b in _block_bounds(src, tgt)]
    stats["blocks"] = len(blocks)
    stats["band_cells"] = sum(b.cells for b in blocks)
    if (stats["band_cells"] > MAX_BAND_CELLS
            or any(b.cells > MAX_BLOCK_CELLS for b in blocks)):
        stats["too_large"] = True
        return {}, stats

    total_s = sum(u.length for u in src if u.kind != "code")
    total_t = sum(u.length for u in tgt if u.kind != "code")
    default = _LengthModel((total_t / total_s) if total_s and total_t else 1.0,
                           DEFAULT_LENGTH_SPREAD)

    # Geçiş 1: uzunluk modeli kestirimi
    pairs = []
    for blk in blocks:
        if not blk.n or not blk.m:
            continue
        blk.run(default, posteriors=False)
        for k, i, j in blk.path():
            if k == 0:
                pairs.append((src[blk.s0 + i - 1], tgt[blk.t0 + j - 1]))
        blk.release()
    ratio, spread, sample = _estimate_length_model(pairs)
    stats["spread_sample"] = sample
    if spread is not None:
        stats["length_spread"] = spread
        stats["length_ratio"] = ratio
        model = _LengthModel(ratio, max(MIN_LENGTH_SPREAD, spread))
    else:
        model = default

    # Geçiş 2: sonsallar
    out: Dict[int, Optional[str]] = {}
    pair_total = confident = 0
    thr = MIN_PAIR_POSTERIOR
    for blk in blocks:
        if not blk.n or not blk.m:
            continue
        blk.run(model, posteriors=True)
        if blk.total == _NEG_INF:
            continue
        for k, i, j in blk.path():
            if k not in (0, 3, 4):
                continue
            di, dj = _MOVES[k]
            pi, pj = i - di, j - dj              # blok içi başlangıç hücresi
            a, b = blk.s0 + pi, blk.t0 + pj      # genel indeksler
            pair_total += 1
            p21 = blk.post(pi + 2, pj + 1, 3)
            # a'nın çevirisi b ile mi başlıyor (1:1, a'nın başı olduğu 2:1
            # ya da 1:2)? Yer bu olasılıkla güvenceye alınır; içerik
            # Viterbi yolunun kararıdır.
            first = (blk.post(pi + 1, pj + 1, 0) + p21
                     + blk.post(pi + 1, pj + 2, 4))
            if first < thr:
                continue
            if k == 0:
                # Çeviride birebir aynı kalmış birim (ad, kod, çevrilmemiş
                # paragraf): EPUB öğesi özgün hâliyle (biçimiyle) kalır.
                out[a] = None if src[a].key == tgt[b].key else tgt[b].text
            elif k == 4:
                out[a] = " ".join(x for x in (tgt[b].text, tgt[b + 1].text) if x)
            else:
                if not tgt[b].text:
                    continue
                out[a] = tgt[b].text
                # İkinci kaynak birim yalnız birleşme güvenliyse boşaltılır;
                # değilse özgün (kaynak dil) hâliyle kalır.
                if p21 >= thr:
                    out[a + 1] = ""
            confident += 1
        blk.release()
    stats.update(pair_total=pair_total, pair_confident=confident)
    return out, stats


# --- EPUB metni --------------------------------------------------------------

_HREF_RE = re.compile(r"\\href\s*\{([^{}]*)\}\s*\{((?:[^{}]|\{[^{}]*\})*)\}")
_URL_RE = re.compile(r"\\url\s*\{([^{}]*)\}")
_INNER_ARG_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}")
# Aynısı, komut adıyla (R9: biçim komutları işaretle sarılır).
_NAMED_ARG_CMD_RE = re.compile(r"\\([a-zA-Z]+)\*?(?:\[[^\]]*\])?\{([^{}]*)\}")
_EMPHASIS_WRAP = {
    "textit": (EPUB_EM_OPEN, EPUB_EM_CLOSE),
    "emph": (EPUB_EM_OPEN, EPUB_EM_CLOSE),
    "textsl": (EPUB_EM_OPEN, EPUB_EM_CLOSE),
    "textbf": (EPUB_STRONG_OPEN, EPUB_STRONG_CLOSE),
    "textsuperscript": (EPUB_SUP_OPEN, EPUB_SUP_CLOSE),
    "textsubscript": (EPUB_SUB_OPEN, EPUB_SUB_CLOSE),
}
# Argümansız özel karakter komutları ve kaçışlar -> karakter. Ayraç ve ters
# bölü yer tutucuyla korunur ki komut argümanı sanılmasın.
_TEX_CHAR_COMMANDS = (
    (r"\textasciitilde{}", "~"), (r"\textasciicircum{}", "^"),
    (r"\textbackslash{}", "\x03"),
    ("\\{", "\x01"), ("\\}", "\x02"),
    ("\\&", "&"), ("\\%", "%"), ("\\$", "$"), ("\\#", "#"), ("\\_", "_"),
)
# EPUB metnine girmeyen komutlar (argümanlarıyla birlikte atılır).
_DROP_CMD_RE = re.compile(
    r"\\(?:label|index|includegraphics|vspace|hspace)\*?\s*(?:\[[^\]]*\])?\{[^{}]*\}"
    # C kulvarı (KARAR 20/21, 2026-09-29): numarasız başlığın İçindekiler
    # girdisi (üç argüman) EPUB metni değildir; çevirmen onu başlıktan ayrı
    # satıra taşısa da EPUB'a "tocsection…" yazılmaz.
    r"|\\addcontentsline\s*\{[^{}]*\}\s*\{[^{}]*\}\s*\{(?:[^{}]|\{[^{}]*\})*\}")
_TEX_SPACING_RE = re.compile(r"\\\\(?:\[[^\]]*\])?|\\[,;:! ]|(?<!\\)~")


def _tex_to_epub_text(raw: str, emphasis: bool = False) -> str:
    """Birimin EPUB'a yazılacak düz metni.

    _strip_latex_commands'tan farkı: iç içe komutlar ('\\textbf{\\textit{X}}'
    eskiden '{X}' kalıyordu) içten dışa açılır, \\href bağlantısı markdown
    bağlantısı olur ('[metin](adres)'; rebuild_epub <a href>'e çevirir,
    eskiden 'adres{metin}' yazılıyordu), TeX kaçışları ('\\%', '\\&') geri
    alınır, gruplama ayraçları ('{\\Huge X}') atılır.

    *emphasis* (R9, 2026-09-28): satır içi biçim korunur — \\textit/\\emph/
    \\textsl rebuild_epub'da <em>, \\textbf <strong>, \\textsuperscript <sup>
    olur (epub_parser.EPUB_* işaretleri). Kapalıysa düz metne iner (eski
    davranış). Hizalama birimleri açık kullanır.
    """
    text = _TEXORPDF_RE.sub(lambda m: m.group(1), raw)
    text = _replace_simple_math(text)
    # 2026-09-29 (E kulvarı, F1-07/F1-08): biçimli kipte (hizalama birimleri)
    # - basit üst/alt simge matematiği ('$m^{2}$', '$H_{2}$', '${}^{a}$';
    #   dönüştürücünün <sup>/<sub> karşılığı) <sup>/<sub> işareti olur;
    # - kalan matematik ve \\includegraphics nesne yer tutucusuna alınır:
    #   rebuild_epub kaynaktaki özgün <math>/<img>'yi yerine koyar (eskiden
    #   formül komutları soyulup 'E=mc2' kalıyordu, görsel atılıyordu);
    # - düz metindeki markdown karakterleri kaçışlanır (F1-09): '2*3*4' ya da
    #   '[x](y)' Türkçe EPUB'da vurgu/bağlantı olmasın.
    objects: List[str] = []
    urls: List[str] = []

    def _obj(kind: str, payload: str) -> str:
        objects.append(f"{EPUB_OBJ_OPEN}{kind}:{payload}{EPUB_OBJ_CLOSE}")
        return f"{_OBJ_STASH_OPEN}{len(objects) - 1}{_OBJ_STASH_CLOSE}"

    if emphasis:
        text = _SCRIPT_MATH_RE.sub(_script_math_marks, text)
        text = _MATH_SPAN_RE.sub(lambda m: _obj("m", m.group(0)), text)
        text = _INCLUDEGRAPHICS_RE.sub(lambda m: _obj("g", m.group(1).strip()), text)
    text = _DOTLESS_I_RE.sub("ı", text)
    text = _DROP_CMD_RE.sub("", text)
    text = _TEX_SPACING_RE.sub(" ", text.replace("\\-", ""))
    for src, dst in _TEX_CHAR_COMMANDS:
        text = text.replace(src, dst)

    def _href(m: "re.Match") -> str:
        url, label = m.group(1).strip(), _INNER_ARG_CMD_RE.sub(r"\1", m.group(2))
        if (url and label.strip() and not re.search(r"[\s()\[\]\x01\x02]", url)
                and not re.search(r"[\[\]]", label)):
            if emphasis:
                urls.append(url)
                return (f"{_LINK_STASH_OPEN}{label}{_LINK_STASH_MID}"
                        f"{len(urls) - 1}{_LINK_STASH_CLOSE}")
            return f"[{label}]({url})"
        return label

    def _inner(m: "re.Match") -> str:
        arg = m.group(2)
        wrap = _EMPHASIS_WRAP.get(m.group(1)) if emphasis else None
        if wrap is None or not arg.strip():
            return arg
        return f"{wrap[0]}{arg}{wrap[1]}"

    for _ in range(8):
        new = _HREF_RE.sub(_href, text)
        new = _URL_RE.sub(lambda m: m.group(1), new)
        new = _NAMED_ARG_CMD_RE.sub(_inner, new)
        if new == text:
            break
        text = new
    text = _CMD_BARE_RE.sub("", text)
    # Kalan ayraçlar yalnız gruplamadır (kaçışlı olanlar yer tutucuda).
    text = text.replace("{", "").replace("}", "")
    text = text.replace("\x01", "{").replace("\x02", "}").replace("\x03", "\\")
    if emphasis:
        text = _EPUB_LITERAL_RE.sub(lambda m: "\\" + m.group(0), text)
        if urls:
            text = re.sub(re.escape(_LINK_STASH_OPEN) + r"(.*?)" + re.escape(_LINK_STASH_MID)
                          + r"(\d+)" + re.escape(_LINK_STASH_CLOSE),
                          lambda m: f"[{m.group(1)}]({urls[int(m.group(2))]})", text,
                          flags=re.S)
        if objects:
            text = re.sub(re.escape(_OBJ_STASH_OPEN) + r"(\d+)" + re.escape(_OBJ_STASH_CLOSE),
                          lambda m: objects[int(m.group(1))], text)
    return re.sub(r"\s+", " ", text).strip()


# _tex_to_epub_text geçici yer tutucuları (özel kullanım alanı).
_OBJ_STASH_OPEN, _OBJ_STASH_CLOSE = "\ue0f2", "\ue0f3"
_LINK_STASH_OPEN, _LINK_STASH_MID, _LINK_STASH_CLOSE = "\ue0f4", "\ue0f5", "\ue0f6"
# Türkçe EPUB metninde kaçışlanan markdown karakterleri (rebuild_epub'ın
# _markdown_to_inline_html'i vurgu/bağlantı sanmasın; çözer).
_EPUB_LITERAL_RE = re.compile(r"[\\*\[\]`]")
# Basit üst/alt simge matematiği: dönüştürücünün '<sup>x</sup>' -> '$b^{x}$',
# '<sub>x</sub>' -> '$b_{x}$' karşılığı (taban: harf/rakam dizisi ya da '{}').
_SCRIPT_MATH_RE = re.compile(
    r"(?<![\\$])\$(?!\$)(\{\}|[A-Za-z0-9]*)([\^_])\{([^{}$\\^_]+)\}\$(?!\$)")
# Kalan matematik: $$…$$, $…$, \(…\), \[…\].
_MATH_SPAN_RE = re.compile(
    r"\$\$.+?\$\$|(?<![\\$])\$(?!\$)[^$\n]+?(?<!\\)\$(?!\$)|\\\(.+?\\\)|\\\[.+?\\\]",
    re.S)
_INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics\*?\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}")


def _script_math_marks(m: "re.Match") -> str:
    base = "" if m.group(1) == "{}" else m.group(1)
    o, c = ((EPUB_SUP_OPEN, EPUB_SUP_CLOSE) if m.group(2) == "^"
            else (EPUB_SUB_OPEN, EPUB_SUB_CLOSE))
    return f"{base}{o}{m.group(3).strip()}{c}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# LaTeX structural commands to skip
_STRUCTURAL_RE = re.compile(
    r"^\\(?:begin|end|documentclass|usepackage|newcommand|renewcommand"
    r"|setlength|geometry|pagestyle|pagenumbering|maketitle"
    r"|tableofcontents|listoffigures|listoftables|printbibliography"
    r"|label|graphicspath|frontmatter|mainmatter|backmatter|appendix"
    r"|hypersetup|definecolor|setmainfont|setsansfont|setmonofont"
    r"|setmathfont)\b"
)

# Heading command
_HEADING_RE = re.compile(
    r"\\(?:chapter|section|subsection|subsubsection)\*?\s*\{(.+)\}"
)

# Caption command
_CAPTION_RE = re.compile(r"\\caption\s*\{(.+)\}")

# General LaTeX command stripper (for plain text extraction)
_CMD_STRIP_RE = re.compile(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^}]*)\}")
_CMD_BARE_RE = re.compile(r"\\[a-zA-Z]+\*?")

# \texorpdfstring{TeX}{düz metin}: EPUB düz metindir, ikinci (yer imi)
# dal alınır. Sanitizer başlıktaki sembolü böyle sarar
# ('\texorpdfstring{$\rightarrow$}{→}'); eskiden EPUB'a ham giriyordu.
_TEXORPDF_RE = re.compile(
    r"\\texorpdfstring\s*\{(?:[^{}]|\{[^{}]*\})*\}"
    r"\s*\{((?:[^{}]|\{[^{}]*\})*)\}"
)
# Satır içi matematik: tek dolarlı $...$ (display $$ hariç) ve \(...\).
_INLINE_MATH_RE = re.compile(
    r"(?<![\\$])\$(?!\$)([^$\n]+?)(?<!\\)\$(?!\$)|\\\((.+?)\\\)"
)
_MATH_CMD_RE = re.compile(r"\\([A-Za-z]+)")
# Dönüşümden sonra bunlardan biri kalıyorsa matematik "basit" değildir
# (kesir, üs/alt indis, gruplama...): olduğu gibi bırakılır.
_NON_SIMPLE_MATH_RE = re.compile(r"[\\{}^_&%#~$]")
# {\i}, \i{}, '\i ' -> ı (komuttan sonraki tek boşluk TeX'te yutulur).
_DOTLESS_I_RE = re.compile(r"\{\\i\}|\\i(?![A-Za-z])(?:\{\}| )?")


def _extract_text_from_tex(tex_content: str) -> List[str]:
    """translated_body.tex'ten metin paragraflarını çıkar.

    LaTeX yapı komutlarını atlar, heading/caption içeriğini çıkarır,
    paragrafları sıralı liste olarak döner. (E2: EPUB çıktısı artık
    _tex_units + hizalama kullanır; bu işlev geriye dönük uyumluluk ve
    testler için korunur.)
    """
    lines = tex_content.split("\n")
    paragraphs: List[str] = []
    current_para: List[str] = []

    for line in lines:
        stripped = line.strip()

        # Boş satır → paragraf ayracı
        if not stripped:
            if current_para:
                paragraphs.append(" ".join(current_para))
                current_para = []
            continue

        # Yorum satırı
        if stripped.startswith("%"):
            continue

        # Yapısal komut → atla
        if _STRUCTURAL_RE.match(stripped):
            continue

        # \begin{...} / \end{...} satırları
        if stripped.startswith("\\begin{") or stripped.startswith("\\end{"):
            continue

        # Heading → metin çıkar. Argüman ayraç dengeli alınır ve gövde
        # satırı gibi düz metne çevrilir (K1d, 2026-09-25): eskiden greedy
        # grup ham giriyordu ('\texorpdfstring{$\rightarrow$}{→}',
        # '\section{X}\label{s}' -> 'X}\label{s').
        heading_m = _HEADING_RE.match(stripped)
        if heading_m:
            if current_para:
                paragraphs.append(" ".join(current_para))
                current_para = []
            paragraphs.append(_strip_latex_commands(
                _braced_arg(stripped, heading_m.start(1) - 1)))
            continue

        # Caption → metin çıkar
        caption_m = _CAPTION_RE.match(stripped)
        if caption_m:
            if current_para:
                paragraphs.append(" ".join(current_para))
                current_para = []
            paragraphs.append(_strip_latex_commands(
                _braced_arg(stripped, caption_m.start(1) - 1)))
            continue

        # Normal satır → LaTeX komutlarını soy. Kısa satır eleği dönüşümden
        # ÖNCEKİ soyma uzunluğuna bakar: pozisyonel eşleme için paragraf
        # sayısı K1d öncesiyle aynı kalmalı ('$\pi$.' eskiden '$$.' olarak
        # geçiyordu; Unicode'lu 'π.' elekten düşüp paragrafı siliyordu).
        clean = _strip_latex_commands(stripped)
        if len(_bare_strip(stripped)) > 2:
            current_para.append(clean)

    # Son paragraf
    if current_para:
        paragraphs.append(" ".join(current_para))

    return paragraphs


def _braced_arg(text: str, open_idx: int) -> str:
    """``text[open_idx]`` '{' ise ayraç dengeli argümanı döndür.

    Kaçışlı ayraçlar (``\\{``, ``\\}``) sayılmaz. Argüman satırda
    kapanmıyorsa kalan metin döner.
    """
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    return text[open_idx + 1:]


def _simple_math_to_unicode(math: str) -> Optional[str]:
    """Basit matematiği Unicode düz metne çevir; basit değilse None.

    Basit: en az bir bilinen sembol/Yunan harfi komutu içerir ve dönüşümden
    sonra komut, ayraç, üs/alt indis kalmaz ('\\alpha' -> 'α', 'm \\ll M' ->
    'm ≪ M', '\\Delta t' -> 'Δ t'). Komutsuz matematik ('$x$') ve karmaşık
    ifadeler ('\\frac{a}{b}', 'x^2') dokunulmadan kalır. Eşlem sanitizer'ın
    sembol/Yunan tablolarından gelir (tek kaynak).
    """
    from latex_pipeline.sanitizer import LATEX_SYMBOL_TO_UNICODE

    found = False

    def _sub(m: "re.Match") -> str:
        nonlocal found
        char = LATEX_SYMBOL_TO_UNICODE.get(m.group(1))
        if char is None:
            return m.group(0)
        found = True
        return char

    out = _MATH_CMD_RE.sub(_sub, math)
    if not found or _NON_SIMPLE_MATH_RE.search(out):
        return None
    return re.sub(r"\s+", " ", out).strip()


def _replace_simple_math(text: str) -> str:
    """Satır içi basit matematiği ($...$, \\(...\\)) Unicode'a çevir."""
    def _sub(m: "re.Match") -> str:
        body = m.group(1) if m.group(1) is not None else m.group(2)
        converted = _simple_math_to_unicode(body)
        return m.group(0) if converted is None else converted

    return _INLINE_MATH_RE.sub(_sub, text)


def _bare_strip(text: str) -> str:
    """Yalnız komut soyma (matematik/ı dönüşümü yok; K1d öncesi soyucu):
    kısa satır eleğinin ölçüsü. Elek buna baktığı için dönüşüm hangi
    satırın paragrafa gireceğini değiştirmez."""
    result = _CMD_BARE_RE.sub("", _CMD_STRIP_RE.sub(r"\1", text))
    return re.sub(r"\s+", " ", result).strip()


def _strip_latex_commands(text: str) -> str:
    """LaTeX komutlarını soyarak düz metin çıkar."""
    # \texorpdfstring{TeX}{metin} → metin; basit matematik → Unicode
    # (K1d: eskiden '$\alpha$' komut silinip '$$' kalıyordu).
    result = _TEXORPDF_RE.sub(lambda m: m.group(1), text)
    result = _replace_simple_math(result)
    # Noktasız ı'nın LaTeX yazımı ('Duyarl{\i}l{\i}k'); komut soyucu '{}'
    # bırakıyordu.
    result = _DOTLESS_I_RE.sub("ı", result)
    # \cmd{content} → content
    result = _CMD_STRIP_RE.sub(r"\1", result)
    # Kalan bare komutları sil
    result = _CMD_BARE_RE.sub("", result)
    # Çoklu boşlukları temizle
    result = re.sub(r"\s+", " ", result).strip()
    return result
