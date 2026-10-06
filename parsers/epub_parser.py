"""
EPUB Parser
EPUB dosyalarından yapısal metin çıkarma
"""

import bisect
import codecs
import logging
import posixpath
import re
import sys
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Generator, Tuple
from pathlib import Path
from urllib.parse import unquote, urlsplit
import warnings

logger = logging.getLogger(__name__)


# ebooklib öğe türü: XHTML belge (ebooklib.ITEM_DOCUMENT). Modül içe
# aktarılırken ebooklib'i yüklememek için sabit olarak tutulur.
_ITEM_DOCUMENT = 9


def _warn(message: str) -> None:
    """Kullanıcıya görünür uyarı: günlüğe (pipeline.log) ve konsola.

    Kayıpsızlık ilkesi (Berke): bir şey atlanır ya da bozuk okunursa sessiz
    geçilmez. Kök kaydedicinin konsol işleyicisi varsa ileti oradan görünür
    (iki kez basılmaz); yoksa ayrıca ekrana basılır.
    """
    root = logging.getLogger()
    console = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, "stream", None) in (sys.stdout, sys.stderr)
        for h in root.handlers)
    if root.handlers or logger.handlers:
        # İşleyici yoksa Python'un son çare işleyicisi iletiyi ikinci kez
        # stderr'e basardı (epub_output._log_line ile aynı kural).
        logger.warning(message)
    if not console:
        print(f"[EPUB Parser] UYARI: {message}")


# --- Markdown kaçışı (F1-02 / F1-09, 2026-09-29) ------------------------------
# EPUB metni DÜZ METİNDİR: markdown'a CommonMark ters bölü kaçışıyla yazılır ki
# dönüştürücü '$…$'ı matematik, '*'/'_' çiftini vurgu, '[..](..)'yu bağlantı,
# '`'yu kod, '\x'i TeX komutu sanmasın (eskiden '$100 … $320' matematik,
# 'C:\Users' "Undefined control sequence", '2*3*4' italik oluyordu). Kaçış
# yalnız METİN DÜĞÜMLERİNE uygulanır; ayrıştırıcının ürettiği işaretler
# (vurgu, bağlantı, <sup>, dipnot) kaçışsızdır. Kaçışlananlar (en az küme;
# CommonMark'ta anlamı olan yerde):
#   '\', '`', '*', '$'   her zaman;
#   '_'                  sözcük içinde değilse ('snake_case' kaçışsız,
#                        '_private_' / '__dunder__' kaçışlı);
#   ']'                  yalnız '(' önünde ('[metin](değil)' bağlantı olmasın;
#                        Gutenberg '[*]', '[12]', '[12 not]' kaçışsız kalır).
# Sözleşme (C kulvarı): dönüştürücü '\X'i her satır içi bağlamda (paragraf,
# başlık, liste, tablo hücresi, dipnot tanımı, alt ad, başlık sayfası) düz X
# olarak basar ve vurgu/bağlantı/kod/matematik ayrıştırmasında saymaz.
_MD_ESCAPE_RE = re.compile(r"[\\`*$]|\](?=\()|_+")
# Çözülen kaçışlar: yazılanlar + satır başı '\#' (_escape_heading_like), '\['
# ve '\|'.
_MD_UNESCAPE_RE = re.compile(r"\\([\\`*_\[\]$#|])")


def _md_escape_sub(m: "re.Match") -> str:
    tok = m.group(0)
    if tok[0] != "_":
        return "\\" + tok
    s, e = m.start(), m.end()
    text = m.string
    if (s > 0 and e < len(text) and _WORD_CHAR_RE.match(text[s - 1])
            and _WORD_CHAR_RE.match(text[e])):
        return tok          # sözcük içi alt çizgi: vurgu olamaz
    return "\\_" * len(tok)


_WORD_CHAR_RE = re.compile(r"[^\W_]")


def md_escape(text: str) -> str:
    """Düz metni markdown'a kaçışlı yaz ('2*3' -> '2\\*3', '$5' -> '\\$5',
    '_x_' -> '\\_x\\_', 'a_b' değişmez)."""
    return _MD_ESCAPE_RE.sub(_md_escape_sub, text)


def md_unescape(text: str) -> str:
    """md_escape'in tersi (bağlantı etiketi, dipnot işareti karşılaştırması,
    başlık sayfası satırı, Türkçe EPUB'a geri yazım)."""
    return _MD_UNESCAPE_RE.sub(r"\1", text)


# --- Belge kodlaması (F1-14, 2026-09-29) ---------------------------------------
# ebooklib belgeyi lxml.html ile yeniden üretir; UTF-16 belgenin gövdesi
# boşalıyor (bölüm sessizce düşüyordu), ISO-8859-1 bildirimli belgede aksanlı
# harfler U+FFFD oluyordu. Belge baytları okunurken BOM / XML bildirimi /
# meta charset'e göre çözülüp UTF-8'e çevrilir (bildirim de 'utf-8' olur).
_XML_DECL_ENC_RE = re.compile(
    rb"^\s*<\?xml[^>]*?\bencoding\s*=\s*[\"']([A-Za-z0-9._:-]+)[\"']", re.I)
_META_CHARSET_RE = re.compile(
    rb"<meta\b[^>]*?\bcharset\s*=\s*[\"']?([A-Za-z0-9._:-]+)", re.I)
_BOMS = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
         (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
         (codecs.BOM_UTF16_BE, "utf-16"))


def _declared_encoding(raw: bytes) -> Optional[str]:
    """Belgenin kodlaması: BOM, BOM'suz UTF-16 ('<\\0?\\0'), XML bildirimi,
    meta charset; bulunamazsa None (UTF-8 varsayılır)."""
    for bom, name in _BOMS:
        if raw.startswith(bom):
            return name
    head = raw[:4]
    if head[:4] == b"<\x00?\x00":
        return "utf-16-le"
    if head[:4] == b"\x00<\x00?":
        return "utf-16-be"
    m = _XML_DECL_ENC_RE.match(raw[:512])
    if m:
        return m.group(1).decode("ascii", "replace")
    m = _META_CHARSET_RE.search(raw[:4096])
    if m:
        return m.group(1).decode("ascii", "replace")
    return None


def _normalize_document_encoding(item, name: str = "") -> Optional[str]:
    """Belgenin ham baytlarını (item.content) UTF-8'e çevir.

    UTF-8 ve geçerli belgeye dokunulmaz (bayt bayt aynı kalır). Döner: uyarı
    metni (çözülemeyen bayt '�' olduysa) ya da None.
    """
    raw = getattr(item, "content", None)
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return None
    raw = bytes(raw)
    enc = _declared_encoding(raw)
    try:
        canon = codecs.lookup(enc).name if enc else "utf-8"
    except LookupError:
        canon = None
    if canon == "utf-8" and not raw.startswith(codecs.BOM_UTF8):
        try:
            raw.decode("utf-8")
            return None
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            problem = (f"{name or 'belge'} geçerli UTF-8 değil; çözülemeyen "
                       f"baytlar '\ufffd' olarak okundu.")
    else:
        problem = None
        try:
            text = raw.decode(enc or "utf-8")
        except (LookupError, UnicodeDecodeError):
            text = raw.decode(enc if canon else "utf-8", errors="replace") \
                if canon else raw.decode("utf-8", errors="replace")
            problem = (f"{name or 'belge'} bildirilen kodlamayla ({enc}) "
                       f"çözülemedi; çözülemeyen baytlar '\ufffd' oldu.")
    text = text.lstrip("\ufeff")
    text = re.sub(r"^(\s*<\?xml[^>]*?\bencoding\s*=\s*[\"'])[^\"']*([\"'])",
                  r"\1utf-8\2", text, count=1, flags=re.I)
    text = re.sub(r"(<meta\b[^>]*?\bcharset\s*=\s*[\"']?)[A-Za-z0-9._:-]+",
                  r"\1utf-8", text, count=1, flags=re.I)
    item.content = text.encode("utf-8")
    return problem

_HEADING_TAGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
# Metin taşıyan blok etiketleri (div hariç: div kapsayıcıdır, içerik taşımaz).
_CONTENT_TAGS = list(_HEADING_TAGS) + ['p', 'li', 'blockquote', 'pre', 'code']
# Bir kapsayıcının metnini taşıyabilen alt bloklar. Satır içi 'code' blok
# sayılmaz: p/li/başlık içindeki code paragrafın parçasıdır, pre içindeki
# code pre'nin kendisidir.
_BLOCK_TAGS = frozenset(_HEADING_TAGS) | {'p', 'li', 'blockquote', 'pre'}
# Kapsayıcı olabilen içerik etiketleri: bütün metni alt bloklardaysa
# (blockquote>p, li>p, li>ul>li ...) kendisi blok üretmez, alt blokları
# üretir (E1a, 2026-09-25: eskiden hem kapsayıcı hem alt blok yazılıyor,
# metin iki kez çıkıyordu).
_CONTAINER_TAGS = frozenset({'blockquote', 'li'})
# Kapsayıcı içindeki alt bloğa konan önek: alıntı '> ', liste öğesi '- '
# (yalnız öğenin ilk bloğunda; sonraki bloklar düz paragraf).
_QUOTE_PREFIX = "> "
_LIST_PREFIX = "- "
# _inline_to_markdown'da iç etiket kenarındaki boşluğun geçici işareti
# (XML metninde NUL bulunamaz).
_EDGE_SPACE = "\x00"
# Satır içine düzleştirilen blok düzeyi HTML öğeleri: sınırlarında sözcük
# ayrılır (gevşek metinli li'nin iç listesi: 'adapters<ul><li>finetuning'
# eskiden 'adaptersfinetuning').
_BLOCK_LEVEL_HTML = frozenset({
    'p', 'div', 'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'blockquote', 'pre',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'caption',
    'section', 'article', 'aside', 'header', 'footer', 'nav', 'figure',
    'figcaption', 'address', 'hr',
}) | frozenset(_HEADING_TAGS)
# F2-08 (2026-09-29): çeviride metni aynen kalırsa yeniden sarılan, yalnız
# biçim taşıyan satır içi öğeler (bkz. EPUBParser._style_wrappers).
_STYLE_WRAPPER_TAGS = frozenset({
    'small', 'big', 'u', 's', 'strike', 'del', 'ins', 'abbr', 'acronym',
    'cite', 'dfn', 'kbd', 'samp', 'var', 'mark', 'tt', 'q', 'time', 'span',
    'font'})
# F1-01 (2026-09-29): gevşek metin bloğu. Karışık kapsayıcıdaki metinli
# satır içi koşunun geçici sarmalayıcısı (rebuild_epub yazımdan önce açar).
_KC_BLOCK_TAG = "kc-blok"
# Gevşek metin koşusunu bölen (blok düzeyi) öğeler.
_LOOSE_BOUNDARY_TAGS = _BLOCK_LEVEL_HTML | frozenset({
    'body', 'html', 'main', 'center', 'hgroup', 'details', 'summary',
    'fieldset', 'legend', 'dir', 'menu', 'form', 'colgroup', 'col',
    'optgroup', 'select', 'option', 'iframe', 'object', 'video', 'audio',
    'canvas'})
_LOOSE_BOUNDARY_FIND = sorted((_LOOSE_BOUNDARY_TAGS
                               | frozenset(t for t in _CONTENT_TAGS if t != 'code')
                               | {_KC_BLOCK_TAG}) - {'code'})
# Metni gövde metni sayılmayan öğeler (görsel/betik/biçem; içindeki metin
# koşuyu metinli yapmaz).
_LOOSE_SKIP_TAGS = frozenset({'script', 'style', 'head', 'meta', 'link',
                              'title', 'template', 'svg', 'noscript'})
# F1-07: tek başına duran display formül ($\displaystyle …$ -> $$…$$).
_DISPLAY_ONLY_RE = re.compile(r"\$\\displaystyle (.*)\$", re.S)

# --- to_markdown sözleşmeleri (kalite dalgası E1, 2026-09-25) ---------------
# Dipnot (R11/R20). Paragraf içinde işaretin yerinde '[^kc-N]'; o EPUB
# belgesinin (bölümün) son bloğundan sonra tek satır '[^kc-N]: metin'.
# Çok paragraflı dipnot tek satıra (boşlukla) iner; dipnot metnindeki satır
# içi biçimler (*italik*, **kalın**, [bağlantı](adres)) markdown olarak kalır.
# Tanımı bulunamayan dipnot bağlantısı (hedefi kitapta yok — kesit — ya da
# hedefi dipnot sayılamıyor/metinsiz) ham '[etiket](adres)' yerine
# '<sup>N</sup>' olur; N işaretin ayraçsız metnidir ('[1]' -> '1', '(b)' ->
# 'b', '*' -> '*'). Dönüştürücü '[^kc-N]'yi \footnote{...}'a çevirir; tanım
# satırı gövdeden çıkar.
NOTE_LABEL_PREFIX = "kc-"
# Bölüm alt adı (R8): başlık satırının hemen ALTINDAKİ satır (boş satır
# yok), tek satır: '<!-- kc:altad -->Alt ad'. Dönüştürücü başlığa birleştirir.
SUBTITLE_MARKER = "<!-- kc:altad -->"

# Türkçe EPUB çıktısı (E2, 2026-09-28): epub_output'un rebuild_epub'a verdiği
# çeviri metnindeki satır içi işaretler. Unicode özel kullanım alanı
# karakterleridir (kitap metninde bulunmaz, markdown yıldızıyla karışmaz);
# _markdown_to_inline_html HTML'e çevirir.
EPUB_EM_OPEN, EPUB_EM_CLOSE = "", ""            # -> <em>
EPUB_STRONG_OPEN, EPUB_STRONG_CLOSE = "", ""    # -> <strong>
EPUB_SUP_OPEN, EPUB_SUP_CLOSE = "", ""          # -> <sup>
# F1-07 (2026-09-29): alt simge ve satır içi nesne (MathML formülü / görsel).
EPUB_SUB_OPEN, EPUB_SUB_CLOSE = "\ue016", "\ue017"          # -> <sub>
# Nesne yer tutucusu: OPEN + tür ('m' matematik, 'g' görsel) + ':' + yedek
# metin + CLOSE. rebuild_epub kaynak öğedeki özgün <math>/<img>'yi sırayla
# yerine koyar; kaynakta karşılığı yoksa matematikte yedek metin (TeX) kalır.
EPUB_OBJ_OPEN, EPUB_OBJ_CLOSE = "\ue018", "\ue019"
# Yeni not (çevirmen notu ya da işareti bulunamayan dipnot): arada notun
# numarası; metni rebuild_epub(notes=) eşlemindedir, belgenin sonuna
# <aside epub:type="footnote"> olarak yazılır.
EPUB_NOTE_REF_OPEN, EPUB_NOTE_REF_CLOSE = "", ""
# Kaynak dipnotunun işareti: arada dipnot anahtarı ('kc-N'); özgün işaret
# (bağlantı ya da Gutenberg '[*]' metni) buraya konur.
EPUB_SOURCE_NOTE_OPEN, EPUB_SOURCE_NOTE_CLOSE = "", ""
# Türkçe EPUB'un İçindekiler (nav) başlıkları.
EPUB_TOC_TITLE_TR = "İçindekiler"
EPUB_PAGE_LIST_TITLE_TR = "Sayfalar"
# rebuild_epub'ın eklediği notların kimlik önekleri (belge içinde tekil).
EPUB_NOTE_ID_PREFIX = "kc-not-"
EPUB_NOTE_REF_ID_PREFIX = "kc-notref-"
_EPUB_WRAPS = (((EPUB_EM_OPEN, EPUB_EM_CLOSE), 'em'),
               ((EPUB_STRONG_OPEN, EPUB_STRONG_CLOSE), 'strong'),
               ((EPUB_SUP_OPEN, EPUB_SUP_CLOSE), 'sup'),
               ((EPUB_SUB_OPEN, EPUB_SUB_CLOSE), 'sub'))
_EPUB_MARKS = (EPUB_EM_OPEN, EPUB_EM_CLOSE, EPUB_STRONG_OPEN, EPUB_STRONG_CLOSE,
               EPUB_SUP_OPEN, EPUB_SUP_CLOSE, EPUB_NOTE_REF_OPEN,
               EPUB_NOTE_REF_CLOSE, EPUB_SOURCE_NOTE_OPEN, EPUB_SOURCE_NOTE_CLOSE,
               EPUB_SUB_OPEN, EPUB_SUB_CLOSE, EPUB_OBJ_OPEN, EPUB_OBJ_CLOSE)
# Nesne yer tutucusu (EPUB_OBJ_*): tür 'm' (matematik) ya da 'g' (görsel).
_EPUB_OBJ_RE = re.compile(re.escape(EPUB_OBJ_OPEN) + r"([mg]):(.*?)"
                          + re.escape(EPUB_OBJ_CLOSE), re.S)
# _markdown_to_inline_html: kaçışlı karakterin geçici yer tutucusu (özel
# kullanım alanı; EPUB_* işaretleriyle çakışmaz).
_ESC_PH_BASE = 0xE100
_ESC_PH_CHARS = "\\`*_[]$#|"
_ESC_PH = {c: chr(_ESC_PH_BASE + i) for i, c in enumerate(_ESC_PH_CHARS)}
_ESC_PH_BACK = {v: k for k, v in _ESC_PH.items()}
# _markdown_to_inline_html: nesne (formül/görsel) indeksli yer tutucusu.
_OBJ_PH_OPEN, _OBJ_PH_CLOSE = "\ue0f0", "\ue0f1"

# Dipnot işareti sayılan bağlantı metni: [1], (2), 3, *, †, a, iv ...
_NOTE_MARKER_RE = re.compile(
    r'^[\[\(]?\s*(?:\d{1,4}|[*†‡§¶]{1,3}|[a-zA-Z]|[ivxlcIVXLC]{1,6})\s*[\]\)]?\.?$')
# Bağlantının (ya da onu saran sup/span'in) dipnot işareti olduğunu söyleyen
# sınıf adları.
_NOTE_REF_CLASS_RE = re.compile(
    r'(?i)^(?:fnanchor|fn-?ref|noteref|note-?ref|footnote-?ref(?:erence)?|'
    r'footnote-?link|footnote-?anchor|footnote-?call|endnote-?ref|'
    r'endnote-?link|endnote-?anchor|note-?anchor|note-?call)$')
# Dipnot hedefi işaretleri: tek dipnot / dipnot kapsayıcısı (çoğul).
_NOTE_TYPES_SINGLE = frozenset({'footnote', 'endnote', 'rearnote', 'note'})
_NOTE_TYPES_PLURAL = frozenset({'footnotes', 'endnotes', 'rearnotes', 'notes'})
_NOTE_ROLES_SINGLE = frozenset({'doc-footnote', 'doc-endnote'})
_NOTE_ROLES_PLURAL = frozenset({'doc-endnotes', 'doc-footnotes'})
# data-type (O'Reilly HTMLBook): 'note' orada bilgi kutusudur, dipnot değil.
_NOTE_DATATYPES_SINGLE = frozenset({'footnote', 'endnote'})
_NOTE_DATATYPES_PLURAL = frozenset({'footnotes', 'endnotes'})
_NOTE_CLASS_SINGLE_RE = re.compile(
    r'(?i)^(?:footnote|endnote|fntext|footnote-?text|endnote-?text|'
    r'footnote-?body|note-?text)$')
_NOTE_CLASS_PLURAL_RE = re.compile(
    r'(?i)^(?:footnotes|endnotes|footnote-?section|endnote-?section|'
    r'footnote-?list)$')
# Dipnot hedefi kimliği: Footnote_1, fn3, ftn2, _ftn2, endnote-4, note_5 ...
_NOTE_ID_RE = re.compile(
    r'(?i)^_?(?:footnote|fn|ftn|endnote|en|note|nt)[-_:.]?\d+[a-z]?$')
# Geri dönüş bağlantısı metni (dipnot metninden atılır).
_BACKLINK_LABEL_RE = re.compile(r'^(?:↩︎?|↩️?|↑|\^|⏎|back|return|\[return\])$', re.I)
# Dipnot bölgesi sınırları: daha büyük "dipnot" bölge dipnot değildir
# (yanlış hedef tespiti bütün bölümü dipnota taşımasın).
MAX_NOTE_BLOCKS = 8
MAX_NOTE_CHARS = 4000
# Markdown bağlantısı (etikette bir düzey iç içe köşeli ayraç: '[[1]](a#b)').
# F1-09 (2026-09-29): etiketteki kaçışlı karakter ('\*', '\]') etiketin
# parçasıdır; kaçışlı '\[' bağlantı açmaz.
_MD_LINK_RE = re.compile(
    r'(?<!\\)\[((?:\\.|[^\[\]\\]|\[(?:\\.|[^\[\]\\])*\])*)\]\(([^()\s]+)\)')
# Çeviri metnindeki bağlantı (etiket boş olamaz; '[[1]](adres)' tanınır).
_MD_LINK_HTML_RE = re.compile(r'\[((?:[^\[\]]|\[[^\[\]]*\])+)\]\(([^()\s]+)\)')
# Gutenberg düz metin notları (R20): gövdede '[*]' işareti ve hemen
# ardından '[* not metni]' paragrafı; ya da '[N]' işareti ve (çoğu kez kitap
# sonunda) '[N not metni]' paragrafı. Bağlantının parçası olan işaret
# ('[[1]](..)', '[1](..)') sayılmaz. F1-09 (2026-09-29): blok metni kaçışlıdır;
# düz metindeki '[*]' markdown'da '[\*]', '[* not]' '[\* not]'.
_GUT_STAR_MARK_RE = re.compile(r'(?<!\[)\[\\\*\](?![\(\[\]])')
_GUT_NUM_MARK_RE = re.compile(r'(?<!\[)\[(\d{1,3})\](?![\(\[\]])')
_GUT_STAR_NOTE_RE = re.compile(r'\[\\\*\s+(.+)\]', re.S)
_GUT_NUM_NOTE_RE = re.compile(r'\[(\d{1,3})\s+(.+)\]', re.S)
# Çok paragraflı not: '[N metin' ile açılır, sonraki bir paragrafın sonundaki
# ']' ile kapanır (en çok MAX_NOTE_BLOCKS blok / MAX_NOTE_CHARS karakter).
_GUT_STAR_OPEN_RE = re.compile(r'\[\\\*\s+\S')
_GUT_NUM_OPEN_RE = re.compile(r'\[(\d{1,3})\s+\S')
# '[*]' işaretinin notu en çok bu kadar blok sonra gelir.
GUT_STAR_LOOKBACK = 3
# Bölüm alt adı (R8): başlıktan hemen sonraki paragrafın sınıfı.
_SUBTITLE_CLASS_RE = re.compile(
    r'(?i)^(?:subh\d*[a-z]?|sub-?title|subhead(?:ing)?|chapter-?sub\w*|'
    r'ch-?sub\w*)$')
# Sınıf alt ad sayılırsa, o sınıflı paragrafların en az bu oranı bir
# başlığın hemen ardından gelmeli (Calibre kitaplarında bütün paragraflar
# class="subtitle" olabiliyor).
SUBTITLE_CLASS_MIN_SHARE = 0.8
SUBTITLE_MAX_CHARS = 300
# F1-01 (2026-09-29): colspan/rowspan'ın dikkate alınan en büyük değeri
# (bozuk öznitelik tabloyu binlerce boş sütuna şişirmesin).
TABLE_MAX_SPAN = 50
# Gövde görselleri (R13): XeLaTeX'in doğrudan alabildiği biçimler dışındaki
# görseller epub_extractor'da PNG'ye çevrilir ya da atlanır.
_IMAGE_TAGS = ('img', 'image')
# Markdown görsel başvurusu ('![alt](yol)'; alt metin kaçışlı olabilir).
_MD_IMAGE_RE = re.compile(r'!\[((?:\\.|[^\[\]\\])*)\]\(([^()\s]+)\)')
# Yalnız görsel(ler)den oluşan metin (metinsiz bağlantı etiketi).
_IMAGES_ONLY_RE = re.compile(r'(?:\s*!\[(?:\\.|[^\[\]\\])*\]\([^()\s]+\))+\s*')
# F1-07: '<sup>..</sup>' / '<sub>..</sub>' işaretine yazılabilen içerik.
# Dönüştürücü içeriği ya \textsuperscript'e ya matematik indisine yazar:
# '_', '^', '{', '}', '$', '\\', '<', '&', '%', '#', '~' güvenli değildir
# (öğe işaretsiz satır içi metin olarak kalır; eski davranış).
_SCRIPT_SAFE_RE = re.compile(r"(?!.*_)[\w\s.,;:!?'’\"()+\-−=/*†‡§¶°′″]+")


def _note_marker_text(label: str) -> str:
    """Dipnot işaretinin ayraçsız metni: '[1]' -> '1', '(b).' -> 'b'.
    Markdown etiketi kaçışlı olabilir ('\\[1\\]', '\\*'; F1-09): çözülür."""
    text = re.sub(r'\s+', '', md_unescape(label or "")).rstrip('.')
    if len(text) >= 2 and text[0] + text[-1] in ('[]', '()'):
        text = text[1:-1]
    return text


_SOURCE_LINE_BREAK_RE = re.compile(r'[ \t\r\f\v]*\n[ \t\r\n\f\v]*')


def _join_source_lines(text: str) -> str:
    """Kaynak HTML'deki satır sonunu (girintisiyle) tek boşluk yap, kenarları
    kırp. Bölünemez boşluk (U+00A0) ve öteki boşluklar korunur (R12)."""
    return _SOURCE_LINE_BREAK_RE.sub(" ", text).strip()


def _one_line(text: str) -> str:
    """Tek satır: ASCII boşluk/satır sonu dizileri tek boşluk; bölünemez
    boşluk (U+00A0) korunur (dipnot tanımı, başlık sayfası satırı)."""
    return re.sub(r'[ \t\r\n\f\v]+', ' ', text or "").strip()


@dataclass
class EPUBChapter:
    """EPUB bölümü (spine'daki bir XHTML belgesi).

    ``blocks``: gövdedeki içerik blokları, okuma sırasıyla
    ``(tür, h_düzeyi, markdown_metni)``. tür 'heading' ise h_düzeyi 1-6 ve
    metin tek satırdır ('#' öneki yok); 'text' ise düzey 0 ve metin
    paragrafın markdown'ıdır (liste/alıntı/kod önekiyle). ``content`` bu
    blokların ham (EPUB'daki h düzeyiyle) markdown birleşimidir.

    Aşağıdaki alanlar yalnız to_markdown'ı etkiler; ``blocks``/``content``
    ve dolayısıyla extract_segments/rebuild_epub segmentleri değişmez:

    - ``name``: belgenin EPUB içi yolu (bağlantı/görsel adreslerini çözmek için).
    - ``roles``: {blok_i: rol}. 'subtitle' (bölüm alt adı, R8),
      'note:<anahtar>' (dipnot gövdesi; metni dipnot tanımına gider),
      'notes_heading' (yalnız dipnot gövdesi taşıyan bölüm başlığı).
    - ``note_links``: {bağlantı adresi: dipnot anahtarı}; '' = tanımı
      bulunamayan dipnot işareti ('<sup>N</sup>' yazılır).
    - ``text_note_refs``: {blok_i: [(konum, uzunluk, anahtar)]} Gutenberg
      düz metin işaretleri ('[*]', '[3]').
    - ``images``: [(blok_i, iç_yol)] bu bloktan ÖNCE yazılacak bağımsız gövde
      görselleri (blok_i == len(blocks): bölüm sonu).
    - ``tables`` (F1-01, 2026-09-29): markdown tablosu olarak yazılan
      tabloların satır/hücre düzeni (hücre blokları ayrı bloktur; bkz.
      EPUBParser._table_layouts).
    - ``printed_toc`` / ``printed_toc_heading``: basılı içindekiler tablosunun
      hücre blokları ve başlığı (dönüştürücü atar; segment ek bilgisi).
    """
    id: str
    title: str = ""
    content: str = ""
    html_content: str = ""
    order: int = 0
    blocks: List[Tuple[str, int, str]] = field(default_factory=list)
    name: str = ""
    roles: Dict[int, str] = field(default_factory=dict)
    note_links: Dict[str, str] = field(default_factory=dict)
    text_note_refs: Dict[int, List[Tuple[int, int, str]]] = field(default_factory=dict)
    images: List[Tuple[int, str]] = field(default_factory=list)
    # F1-01 (2026-09-29): markdown tablosu olarak yazılacak tablolar (bkz.
    # EPUBParser._table_layouts). Yalnız to_markdown'ı etkiler.
    tables: List[Dict] = field(default_factory=list)
    # Tablo hücresine yazılan görsellerin (ayrıştırma ağacındaki) öğe
    # kimlikleri: bağımsız gövde görseli sayılmaz (yalnız parse içinde).
    _table_images: frozenset = field(default_factory=frozenset, repr=False)
    # Basılı içindekiler tablosunun hücre blokları ve başlığı (bkz.
    # EPUBParser._mark_printed_toc); segment ek bilgisi (role).
    printed_toc: set = field(default_factory=set)
    printed_toc_heading: set = field(default_factory=set)


@dataclass
class EPUBDocument:
    """EPUB dokümanı"""
    path: str
    title: str = ""
    author: str = ""
    language: str = ""
    chapters: List[EPUBChapter] = field(default_factory=list)
    metadata: Dict = field(default_factory=dict)
    # Tüm dc:creator değerleri (author yalnız ilki). Başlık sayfası
    # tespitinde yazar adı başlıklarını tanımak için kullanılır.
    creators: List[str] = field(default_factory=list)
    # Dipnot anahtarı -> dipnot metni (markdown, tek satır). R11/R20.
    notes: Dict[str, str] = field(default_factory=dict)
    # Ayrıştırma uyarıları (2026-09-29): kodlama, MathML, çıkarılamayan
    # görsel, gövdeye alınmayan gezinme metni. Kullanıcıya gösterilir.
    warnings: List[str] = field(default_factory=list)

    @property
    def total_chapters(self) -> int:
        return len(self.chapters)

    def get_all_text(self) -> str:
        """Tüm metni birleştir.

        Bölüm başlığı gövdedeki başlık etiketinden gelir ve ``content``
        içinde zaten vardır; ayrıca yazılmaz (eskiden başlık iki kez
        çıkıyordu). Blok bilgisi olmayan (elle kurulmuş) bölümde eski
        davranış korunur.
        """
        texts = []
        for chapter in self.chapters:
            if chapter.title and not chapter.blocks:
                texts.append(f"# {chapter.title}")
            texts.append(chapter.content)
        return "\n\n".join(texts)

    def iter_chapters(self) -> Generator[EPUBChapter, None, None]:
        """Tüm bölümleri iterate et"""
        for chapter in self.chapters:
            yield chapter


class EPUBParser:
    """
    EPUB'dan yapısal metin çıkarıcı

    - Bölümleri ayırır
    - HTML'i temiz metne çevirir
    - Başlıkları tanır
    - Formülleri korur
    """

    # Korunacak HTML etiketleri
    PRESERVE_TAGS = {'math', 'svg', 'code', 'pre'}

    # Temizlenecek etiketler
    REMOVE_TAGS = {'script', 'style', 'head', 'meta', 'link'}

    # --- Başlık sayfası / başlık düzeyi kuralları (to_markdown) ---------
    # Başlık sayfası başlığı (kitap adı / yazar) yalnız kitabın ilk başlıklı
    # belgesinin ilk bu kadar başlığı içinde aranır: başlık sayfası kitabın
    # başındadır; önünde en çok yayıncı üst bilgisi gibi bir-iki başlık olur
    # (Gutenberg: "The Project Gutenberg eBook of ...").
    FRONT_TITLE_SCAN = 3
    # Başlık sözcüklerinin en az bu oranı dc:title / dc:creator
    # sözcüklerinden oluşmalı (ör. "De la Terre à la Lune ... par Jules
    # Verne": 16 sözcüğün 15'i).
    TITLE_TOKEN_COVERAGE = 0.75
    # Başlık, dc:title'ın ana kısmını (':', ' – ', '/', '(' öncesi) en az bu
    # oranda ya da tam dc:title'ı en az TITLE_FULL_RECALL oranında içermeli.
    TITLE_MAIN_RECALL = 0.8
    TITLE_FULL_RECALL = 0.6
    # Bölüm düzeyi seçimi: en üst düzey en çok LEVEL_RARE_MAX kez geçiyor ve
    # bir alt düzey en az LEVEL_DOMINANCE katı sıklıktaysa bölüm düzeyi alt
    # düzeydir (Gutenberg: h2 önsöz + h2 lisans, h3 x 28 bölüm). h1 bölümlü
    # kitaplarda (h1 >= 3) devreye girmez.
    LEVEL_RARE_MAX = 2
    LEVEL_DOMINANCE = 5

    def __init__(self, preserve_formatting: bool = True):
        self.preserve_formatting = preserve_formatting
        # Ayrıştırma uyarıları (kodlama, MathML, çıkarılamayan görsel ...):
        # parse() EPUBDocument.warnings'e aktarır (kayıpsızlık: sessiz değil).
        self._warnings: List[str] = []

    def parse(self, epub_path: str) -> EPUBDocument:
        """
        EPUB dosyasını parse et

        Args:
            epub_path: EPUB dosya yolu

        Returns:
            EPUBDocument
        """
        from ebooklib import epub
        from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

        self._warnings = []
        book = epub.read_epub(epub_path)
        self._normalize_encodings(book)

        creators = [c[0] for c in (book.get_metadata('DC', 'creator') or []) if c and c[0]]
        epub_doc = EPUBDocument(
            path=epub_path,
            title=book.get_metadata('DC', 'title')[0][0] if book.get_metadata('DC', 'title') else Path(epub_path).stem,
            author=creators[0] if creators else "",
            language=book.get_metadata('DC', 'language')[0][0] if book.get_metadata('DC', 'language') else "en",
            creators=creators,
        )

        # Metadata
        for key in ['publisher', 'date', 'description', 'subject']:
            meta = book.get_metadata('DC', key)
            if meta:
                epub_doc.metadata[key] = meta[0][0]

        # Bölümleri OKUMA SIRASIYLA (spine) parse et. Eskiden book.get_items()
        # manifest sırası kullanılıyordu; manifest sırası okuma sırası
        # değildir (ör. başlık sayfası manifestte en sonda olabilir).
        # Bütün belgeler önce okunur: dipnot hedefi başka belgede olabilir
        # (Gutenberg 'Notas' belgesi, kitap sonu notları).
        docs = [(item, self._read_soup(item)) for item in self._ordered_documents(book)]
        cover_images = self._cover_image_names(book)
        cover_docs = self._guide_names(book, 'cover')

        entries = []  # (bölüm, blok öğeleri, soup)
        carried: List[str] = []  # ilk bölümden önceki metinsiz belgelerin görselleri
        order = 0
        for item, soup in docs:
            chapter, elements = self._chapter_from_soup(item, order, soup)
            images = []
            if chapter.name not in cover_docs:
                images = [(pos, name) for pos, name in
                          self._image_positions(soup, elements, chapter.name,
                                                chapter._table_images)
                          if name not in cover_images]
            if chapter.content.strip():  # Boş olmayan bölümler
                chapter.images = [(0, name) for name in carried] + images
                carried = []
                epub_doc.chapters.append(chapter)
                entries.append((chapter, elements, soup))
                order += 1
            elif epub_doc.chapters:
                # Yalnız görsel taşıyan belge (tam sayfa resim): okuma
                # sırasındaki yeri önceki bölümün sonudur (sonraki bölümün
                # başlığından önce). Sonraki bölümün başına konsaydı, bölüm
                # başlığının önündeki görsel kuralı onu başlığın altına
                # taşırdı (okuma sırası bozulurdu).
                prev = epub_doc.chapters[-1]
                prev.images.extend((len(prev.blocks), name) for _pos, name in images)
            else:
                # İlk metinli bölümden önceki görsel (ön kapak arkası resim):
                # ilk bölümün başına taşınır, kaybolmaz.
                carried.extend(name for _pos, name in images)

        self._link_notes(epub_doc, docs, entries)
        self._link_text_notes(epub_doc)
        self._mark_notes_headings(epub_doc)
        self._mark_subtitles(entries)
        self._mark_printed_toc(epub_doc)
        epub_doc.warnings = list(self._warnings)
        return epub_doc

    def _mark_printed_toc(self, epub_doc: EPUBDocument) -> None:
        """F1-01 yan etkisi (2026-09-29): basılı içindekiler tablosu.

        İçindekiler başlığının ('ÍNDICE', 'Contents', 'Inhalt' ...; dönüştürücü
        kuralı converter._is_contents_heading) hemen ardından gelen tablo
        dönüştürücüde başlığıyla birlikte BİLEREK atılır (kitap kendi
        \\tableofcontents'ını üretir). Eskiden bu tabloların hücre metni hiç
        çıkarılmıyordu; artık hücreler segmenttir, ama TeX'e girmedikleri için
        Türkçe EPUB'da çevrilemezler. Hücreler 'printed_toc' (hizalamaya ve
        kapsama girmez, özgün kalır: önceki durumla aynı), başlık
        'printed_toc_heading' (Türkçe EPUB'da 'İçindekiler') işaretlenir.
        """
        try:
            from latex_pipeline.converter import _is_contents_heading
        except ImportError:  # pragma: no cover
            return
        for chapter in epub_doc.chapters:
            for layout in chapter.tables:
                cells = [b for r in layout["rows"] for c in r if c is not None
                         for b in c["blocks"]]
                if not cells or layout["caption"]:
                    continue
                head = min(cells) - 1
                if head < 0 or chapter.blocks[head][0] != 'heading':
                    continue
                if any(img[0] == head + 1 for img in chapter.images):
                    continue    # arada görsel: dönüştürücü tabloyu atmaz
                title = md_unescape(self._plain_line(chapter.blocks[head][2]))
                if not _is_contents_heading(title):
                    continue
                chapter.printed_toc.update(cells)
                chapter.printed_toc_heading.add(head)

    def _normalize_encodings(self, book) -> None:
        """F1-14: UTF-8 olmayan belgeleri (UTF-16, ISO-8859-1 …) UTF-8'e
        çevir; çözülemeyen bayt varsa uyar. parse() ve rebuild_epub() aynı
        kitabı aynı biçimde okur (segment indeksleri aynı kalsın)."""
        for item in book.get_items():
            if item.get_type() != _ITEM_DOCUMENT:
                continue
            problem = _normalize_document_encoding(item, self._doc_name(item))
            if problem:
                self._note_warning(problem)

    def _read_soup(self, item):
        """Belgeyi okuma kopyası olarak ayrıştır (script/style/head atılır)."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(item.get_content().decode('utf-8'), 'lxml')
        for tag in soup.find_all(self.REMOVE_TAGS):
            tag.decompose()
        return soup

    @staticmethod
    def _doc_name(item) -> str:
        return (item.get_name() or '').replace('\\', '/')

    @staticmethod
    def _cover_image_names(book) -> set:
        """Kapak görselinin EPUB iç yolları (gövdeye yazılmaz: kapak ayrıca
        _inject_cover ile başlık sayfasına gider)."""
        names = set()
        cover_ids = set()
        opf = 'http://www.idpf.org/2007/opf'
        for _val, attrs in (book.metadata.get(opf, {}).get('meta') or []):
            if isinstance(attrs, dict) and attrs.get('name') == 'cover' and attrs.get('content'):
                cover_ids.add(attrs['content'])
        for item in book.get_items():
            props = getattr(item, 'properties', None) or []
            if (item.get_type() == 10  # ebooklib.ITEM_COVER
                    or 'cover-image' in props or item.get_id() in cover_ids):
                names.add((item.get_name() or '').replace('\\', '/'))
        return names

    @staticmethod
    def _guide_names(book, kind: str) -> set:
        """EPUB2 guide'da type=*kind* gösterilen belge adları."""
        names = set()
        for ref in getattr(book, 'guide', None) or []:
            if isinstance(ref, dict) and (ref.get('type') or '').strip().lower() == kind:
                href = unquote((ref.get('href') or '').split('#', 1)[0]).strip()
                if href:
                    names.add(posixpath.normpath(href.replace('\\', '/')))
        return names

    @classmethod
    def _ordered_documents(cls, book, warn: bool = True) -> list:
        """XHTML belgelerini okuma sırasıyla döndür.

        Önce spine sırası (linear="no" öğeler de spine'daki yerinde kalır).
        Spine'da olmayan belgeler atılmaz; spine belgelerinden SONRA
        manifest sırasıyla eklenir. İstisna: spine dışı gezinme/içindekiler
        belgesi (EPUB3 nav, guide'da type="toc") gövde değildir, eklenmez
        (E1b, 2026-09-25: Gutenberg toc.xhtml kitabın sonuna 'xhtml#...'
        bağlantılı içindekiler listesi olarak giriyordu). Spine İÇİNDEKİ
        içindekiler sayfası (guide type="toc") kitabın parçasıdır, kalır;
        EPUB3 gezinme belgesi (properties="nav") spine'da da olsa atlanır
        (F1-13, 2026-09-29).
        parse() ve rebuild_epub() bu sırayı paylaşır: segment indeksleri
        iki tarafta aynı belgeye düşmelidir.
        """
        ordered = []
        seen = set()
        for entry in getattr(book, 'spine', None) or []:
            idref = entry[0] if isinstance(entry, (tuple, list)) else entry
            if not idref:
                continue
            item = book.get_item_with_id(idref)
            if item is None or item.get_type() != _ITEM_DOCUMENT:
                continue
            if id(item) in seen:
                continue
            seen.add(id(item))
            if cls._is_epub3_nav(item):
                # F1-13 (2026-09-29): spine'daki EPUB3 gezinme belgesi
                # (ebooklib/Calibre üretimi) gövde değildir: PDF'e fazladan
                # '\chapter*{Inhalt}' ve kırık '\href{ch1.xhtml}' giriyordu.
                # İçindekiler LaTeX'te \tableofcontents, Türkçe EPUB'da
                # _rebuild_navigation'dır. <nav> dışında metni varsa uyarılır.
                if warn:
                    cls._warn_nav_text(item)
                continue
            ordered.append(item)
        toc_names = cls._guide_toc_names(book)
        for item in book.get_items():
            if item.get_type() == _ITEM_DOCUMENT and id(item) not in seen:
                seen.add(id(item))
                if cls._is_navigation_document(item, toc_names):
                    continue
                ordered.append(item)
        return ordered

    @staticmethod
    def _is_epub3_nav(item) -> bool:
        """EPUB3 gezinme belgesi mi (properties="nav"; ebooklib EpubNav)?"""
        try:
            from ebooklib.epub import EpubNav
        except ImportError:  # pragma: no cover
            EpubNav = None
        if EpubNav is not None and isinstance(item, EpubNav):
            return True
        return 'nav' in (getattr(item, 'properties', None) or [])

    @staticmethod
    def _warn_nav_text(item) -> None:
        """Gövdeye alınmayan gezinme belgesinde <nav> dışında metin varsa
        uyar (kayıpsızlık: sessiz atlama yok)."""
        from bs4 import BeautifulSoup

        raw = getattr(item, 'content', b'') or b''
        try:
            text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
            soup = BeautifulSoup(text, 'lxml')
        except Exception:
            return
        body = soup.body
        if body is None:
            return
        for tag in body.find_all(['nav', 'script', 'style']):
            tag.decompose()
        rest = " ".join(body.get_text(" ").split())
        if rest:
            _warn(f"Gezinme belgesi {item.get_name()} gövdeye alınmadı; <nav> "
                  f"dışındaki metni de alınmadı: {rest[:80]!r}")

    @staticmethod
    def _guide_toc_names(book) -> set:
        """EPUB2 guide'da type="toc" olarak gösterilen belge adları."""
        from urllib.parse import unquote

        names = set()
        for ref in getattr(book, 'guide', None) or []:
            if not isinstance(ref, dict):
                continue
            if (ref.get('type') or '').strip().lower() != 'toc':
                continue
            href = unquote((ref.get('href') or '').split('#', 1)[0]).strip()
            if href:
                names.add(href.lstrip('./'))
        return names

    @staticmethod
    def _is_navigation_document(item, toc_names=frozenset()) -> bool:
        """Belge gezinme/içindekiler belgesi mi? (EPUB3 nav ya da guide toc)

        ebooklib properties="nav" belgesini EpubNav olarak okur (özellik
        listesini atar); EpubNav.is_chapter() False döner.
        """
        try:
            from ebooklib.epub import EpubNav
        except ImportError:  # pragma: no cover
            EpubNav = None
        if EpubNav is not None and isinstance(item, EpubNav):
            return True
        props = getattr(item, 'properties', None) or []
        if 'nav' in props:
            return True
        name = (item.get_name() or '').replace('\\', '/').lstrip('./')
        return bool(name) and any(
            name == t or name.endswith('/' + t) for t in toc_names)

    def _parse_chapter(self, item, order: int) -> EPUBChapter:
        """Tek bir bölümü parse et"""
        chapter, _elements = self._chapter_from_soup(item, order, self._read_soup(item))
        return chapter

    def _chapter_from_soup(self, item, order: int, soup):
        """(bölüm, blok öğeleri) — blok öğeleri ``chapter.blocks`` ile aynı
        sırada, gövdedeki kaynak öğelerdir (dipnot/görsel/alt ad tespiti)."""
        html_content = item.get_content().decode('utf-8')
        root = soup.body if soup.body else soup

        # Başlık: gövdedeki ilk h1/h2. Metni gövde bloğuyla AYNI yoldan
        # üretilir (_heading_markdown) ki extract_segments'teki tekrar
        # denetimi başlığı içerikteki başlık bloğuyla eşleyebilsin.
        # <title> kullanılmaz: ebooklib okunan belgenin <head>'ini atar,
        # ayrıca <title> çoğu EPUB'da her belgede kitap adıdır.
        self._doc_ctx = self._doc_name(item)
        title_tag = self._find_title_tag(root)
        title = self._heading_markdown(title_tag) if title_tag is not None else ""

        produced = list(self._content_blocks(root))
        blocks = [(kind, level, text) for _el, kind, level, text in produced]
        elements = [el for el, _k, _l, _t in produced]

        chapter = EPUBChapter(
            id=item.get_id(),
            title=title,
            content=self._blocks_to_raw_markdown(blocks),
            html_content=html_content,
            order=order,
            blocks=blocks,
            name=self._doc_name(item),
        )
        chapter.tables, chapter._table_images = self._table_layouts(root, elements, blocks)
        return chapter, elements

    @staticmethod
    def _resolve_href(doc_name: str, href: str) -> Optional[Tuple[str, str]]:
        """Belge içi göreli adresi (EPUB iç yolu, parça) olarak çöz.

        Dış adres (şema/alan adı), data: ve boş adres için None. Parça yoksa
        ikinci öğe ''.
        """
        href = (href or '').strip()
        if not href or href.startswith('data:'):
            return None
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            return None
        path = unquote(parts.path)
        if path:
            path = posixpath.normpath(posixpath.join(posixpath.dirname(doc_name), path))
        else:
            path = doc_name
        return path, unquote(parts.fragment)

    # ------------------------------------------------------------------
    # Dipnotlar (R11/R20) — yalnız to_markdown'ı etkiler
    # ------------------------------------------------------------------

    @staticmethod
    def _attr_tokens(el, name: str) -> List[str]:
        value = el.get(name) if hasattr(el, 'get') else None
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(v).lower() for v in value]
        return str(value).lower().split()

    def _has_ref_signal(self, a) -> bool:
        """Bağlantı (ya da onu saran sup/span) dipnot işareti olarak mı
        işaretlenmiş? (epub:type=noteref, role=doc-noteref,
        data-type=noteref, class=fnanchor/footnote-ref ...)"""
        candidates = [a]
        if a.parent is not None and a.parent.name in ('sup', 'span', 'small', 'sub'):
            candidates.append(a.parent)
        for el in candidates:
            if 'noteref' in self._attr_tokens(el, 'epub:type'):
                return True
            if 'doc-noteref' in self._attr_tokens(el, 'role'):
                return True
            if 'noteref' in self._attr_tokens(el, 'data-type'):
                return True
            if any(_NOTE_REF_CLASS_RE.match(c) for c in self._attr_tokens(el, 'class')):
                return True
        return False

    def _note_signal(self, el) -> Optional[str]:
        """'single' (tek dipnot), 'plural' (dipnot kapsayıcısı) ya da None."""
        etype = set(self._attr_tokens(el, 'epub:type'))
        roles = set(self._attr_tokens(el, 'role'))
        dtype = set(self._attr_tokens(el, 'data-type'))
        classes = self._attr_tokens(el, 'class')
        if (etype & _NOTE_TYPES_SINGLE or roles & _NOTE_ROLES_SINGLE
                or dtype & _NOTE_DATATYPES_SINGLE
                or any(_NOTE_CLASS_SINGLE_RE.match(c) for c in classes)):
            return 'single'
        if (etype & _NOTE_TYPES_PLURAL or roles & _NOTE_ROLES_PLURAL
                or dtype & _NOTE_DATATYPES_PLURAL
                or any(_NOTE_CLASS_PLURAL_RE.match(c) for c in classes)):
            return 'plural'
        return None

    def _note_region(self, target):
        """(bölge, işaretli_mi): hedeften yukarı çıkılır; tekil dipnot işaretli
        ilk öğe bölgedir, önce çoğul kapsayıcıya varılırsa yoldaki çocuğu
        bölgedir. İşaret yoksa (bölge=None, False)."""
        prev = None
        node = target
        while node is not None and node.name not in (None, 'body', 'html', '[document]'):
            sig = self._note_signal(node)
            if sig == 'single':
                return node, True
            if sig == 'plural':
                return (prev if prev is not None else node), True
            prev = node
            node = node.parent
        return None, False

    def _link_notes(self, epub_doc: EPUBDocument, docs, entries) -> None:
        """Dipnot bağlantılarını (fnanchor/noteref) hedef metinleriyle eşle.

        Eşleşen bağlantı ``note_links``'e anahtarıyla, hedef bölgenin
        blokları ``roles``'a 'note:<anahtar>' olarak yazılır; metin
        ``epub_doc.notes``'a gider. Hedefi kitapta bulunmayan (kesit) ya da
        dipnot sayılamayan bölgeye giden işaret '' ('<sup>N</sup>') olur.
        """
        id_index = {}
        for item, soup in docs:
            name = self._doc_name(item)
            for el in soup.find_all(True, attrs={'id': True}):
                id_index.setdefault((name, el.get('id')), el)
            for el in soup.find_all('a', attrs={'name': True}):
                id_index.setdefault((name, el.get('name')), el)
        block_at: Dict[int, Tuple[int, int]] = {}
        for ci, (_chapter, elements, _soup) in enumerate(entries):
            for bi, el in enumerate(elements):
                block_at[id(el)] = (ci, bi)
        chapters = [c for c, _e, _s in entries]

        def enclosing_block(el):
            for anc in el.parents:
                if id(anc) in block_at:
                    return block_at[id(anc)]
            return None

        region_key: Dict[int, str] = {}
        # anahtar -> (bloklar, bölge öğesi, bölgenin belgesi)
        info: Dict[str, Tuple[List[Tuple[int, int]], object, str]] = {}
        backlinks = set()
        claimed: Dict[Tuple[int, int], str] = {}
        marker_anchors = set()  # dipnot işareti bağlantılarının (belge, id)'leri

        def remember_marker(chapter_name, a):
            for node in (a, a.parent):
                if node is not None and hasattr(node, 'get') and node.get('id'):
                    marker_anchors.add((chapter_name, node.get('id')))

        for ci, (chapter, _elements, soup) in enumerate(entries):
            root = soup.body if soup.body else soup
            for a in root.find_all('a', href=True):
                loc = enclosing_block(a)
                if loc is None or loc[0] != ci or chapter.roles.get(loc[1], '').startswith('note:'):
                    continue
                label = " ".join(a.get_text(" ").split())
                if not _NOTE_MARKER_RE.match(label):
                    continue
                href = a.get('href')
                target = self._resolve_href(chapter.name, href)
                if target is None or not target[1]:
                    continue
                ref_sig = self._has_ref_signal(a)
                id_sig = bool(_NOTE_ID_RE.match(target[1]))
                el = id_index.get(target)
                if el is None:
                    if ref_sig or id_sig:
                        chapter.note_links.setdefault(href, "")
                        remember_marker(chapter.name, a)
                    continue
                region, marked = self._note_region(el)
                if not (ref_sig or id_sig or marked):
                    continue  # sıradan iç bağlantı
                remember_marker(chapter.name, a)
                if region is None:
                    if id(el) in block_at:
                        region = el
                    else:
                        outer = enclosing_block(el)
                        region = entries[outer[0]][1][outer[1]] if outer else el
                elif enclosing_block(region) is not None:
                    region = None  # satır içi not (paragrafın parçası)
                key = region_key.get(id(region)) if region is not None else None
                if key is None and region is not None:
                    key = self._new_note_region(region, loc, block_at, chapters,
                                                claimed, info, epub_doc, target[0])
                    if key:
                        region_key[id(region)] = key
                chapter.note_links.setdefault(href, key or "")
                if key:
                    # Dipnot metnindeki geri dönüş bağlantısının hedefleri
                    node = a
                    while node is not None and id(node) not in block_at:
                        if node.get('id'):
                            backlinks.add((chapter.name, node.get('id')))
                        node = node.parent
                    if node is not None and node.get('id'):
                        backlinks.add((chapter.name, node.get('id')))

        for key, (blocks, region, doc_name) in info.items():
            if blocks:
                raw = " ".join(chapters[c].blocks[b][2] for c, b in blocks)
            else:
                raw = self._inline_to_markdown(region, " ")
            text = self._clean_note_text(raw, doc_name, backlinks)
            if not text:
                # Metinsiz hedef: dipnot yok, işaretler '<sup>N</sup>' olur.
                for c, b in blocks:
                    chapters[c].roles.pop(b, None)
                for ch in chapters:
                    for h, k in list(ch.note_links.items()):
                        if k == key:
                            ch.note_links[h] = ""
                continue
            epub_doc.notes[key] = text

        # Dipnota dönüşmeyen notlardaki geri dönüş bağlantısı (tablo içi not,
        # metinsiz ya da çok büyük hedef): hedefi bir dipnot İŞARETİ olan,
        # işaret metinli bağlantı ham '[a](…#id-marker)' kalmasın; tanımı
        # bulunamayan bağlantı gibi '<sup>N</sup>' olur (R11).
        for ci, (chapter, _elements, soup) in enumerate(entries):
            root = soup.body if soup.body else soup
            for a in root.find_all('a', href=True):
                href = a.get('href')
                if href in chapter.note_links:
                    continue
                loc = enclosing_block(a)
                if loc is None or loc[0] != ci or chapter.roles.get(loc[1], '').startswith('note:'):
                    continue
                if not _NOTE_MARKER_RE.match(" ".join(a.get_text(" ").split())):
                    continue
                target = self._resolve_href(chapter.name, href)
                if target is None or not target[1]:
                    continue
                el = id_index.get(target)
                if target in marker_anchors or (
                        el is not None and el.name == 'a' and self._has_ref_signal(el)):
                    chapter.note_links[href] = ""

    def _new_note_region(self, region, ref_loc, block_at, chapters, claimed,
                         info, epub_doc, doc_name) -> Optional[str]:
        """Bölgeyi dipnot olarak kaydet; uygun değilse None."""
        from bs4 import Tag

        ids = {id(region)} | {id(d) for d in region.descendants if isinstance(d, Tag)}
        blocks = sorted(block_at[i] for i in ids if i in block_at)
        if ref_loc in blocks or len(blocks) > MAX_NOTE_BLOCKS:
            return None
        total = 0
        for c, b in blocks:
            kind, _level, text = chapters[c].blocks[b]
            if kind != 'text' or text.startswith("```") or (c, b) in claimed:
                return None
            if chapters[c].roles.get(b):
                return None
            total += len(text)
        if total > MAX_NOTE_CHARS:
            return None
        key = f"{NOTE_LABEL_PREFIX}{len(info) + 1}"
        for c, b in blocks:
            claimed[(c, b)] = key
            chapters[c].roles[b] = f"note:{key}"
        info[key] = (blocks, region, doc_name)
        return key

    def _clean_note_text(self, text: str, doc_name: str, backlinks) -> str:
        """Dipnot metni: geri dönüş bağlantıları atılır, tek satır."""
        def repl(m):
            label, href = m.group(1), m.group(2)
            target = self._resolve_href(doc_name, href)
            if target is not None and target in backlinks:
                return ""
            if _BACKLINK_LABEL_RE.match(md_unescape(label).strip()):
                return ""
            return m.group(0)

        text = _MD_LINK_RE.sub(repl, text)
        return _one_line(text)

    def _link_text_notes(self, epub_doc: EPUBDocument) -> None:
        """Gutenberg düz metin notları (R20): '[*]' + '[* not]' ve '[N]' +
        '[N not]' paragrafları. Not paragrafı dipnot gövdesi olur, işaretin
        yeri ``text_note_refs``'e yazılır."""
        chapters = epub_doc.chapters
        pending_star = deque()   # (ci, bi, konum, sıra)
        pending_num: Dict[str, List[Tuple[int, int, int]]] = {}
        seq = 0
        counter = len(epub_doc.notes)

        def add_ref(ci, bi, start, length, key):
            chapters[ci].text_note_refs.setdefault(bi, []).append((start, length, key))

        for ci, chapter in enumerate(chapters):
            for bi, (kind, _level, text) in enumerate(chapter.blocks):
                seq += 1
                if kind != 'text' or chapter.roles.get(bi) or text.startswith("```"):
                    continue
                stripped = text.strip()
                note = self._gut_note(chapter, bi, stripped)
                if note is not None:
                    number, body, span = note
                    ref = None
                    if number is None:  # '[* ...]'
                        while pending_star and seq - pending_star[0][3] > GUT_STAR_LOOKBACK:
                            pending_star.popleft()
                        if pending_star:
                            rc, rb, pos, _s = pending_star.popleft()
                            ref = (rc, rb, pos, len("[\\*]"))
                    elif pending_num.get(number):
                        rc, rb, pos = pending_num[number].pop()
                        ref = (rc, rb, pos, len(number) + 2)
                    if ref is not None:
                        counter += 1
                        key = f"{NOTE_LABEL_PREFIX}{counter}"
                        epub_doc.notes[key] = body
                        for bj in span:
                            chapter.roles[bj] = f"note:{key}"
                        add_ref(ref[0], ref[1], ref[2], ref[3], key)
                        continue
                for mm in _GUT_STAR_MARK_RE.finditer(text):
                    pending_star.append((ci, bi, mm.start(), seq))
                for mm in _GUT_NUM_MARK_RE.finditer(text):
                    pending_num.setdefault(mm.group(1), []).append((ci, bi, mm.start()))

    def _gut_note(self, chapter: EPUBChapter, bi: int, stripped: str):
        """Blok bir Gutenberg not gövdesi mi? (numara ya da '*' için None,
        tek satır metin, bloklar) ya da None.

        Tek bloklu not '[N metin]' / '[* metin]'. Çok paragraflı not '[N'
        ile açılan bloktan, sonu ']' olan ilk sonraki bloğa kadar sürer
        (arada yeni bir not açılmaz; en çok MAX_NOTE_BLOCKS blok ve
        MAX_NOTE_CHARS karakter); paragraflar boşlukla tek satıra iner
        (dipnot sözleşmesi).
        """
        m = _GUT_STAR_NOTE_RE.fullmatch(stripped)
        if m:
            return None, _one_line(m.group(1)), [bi]
        m = _GUT_NUM_NOTE_RE.fullmatch(stripped)
        if m:
            return m.group(1), _one_line(m.group(2)), [bi]
        if not (_GUT_STAR_OPEN_RE.match(stripped) or _GUT_NUM_OPEN_RE.match(stripped)):
            return None
        parts = [stripped]
        total = len(stripped)
        blocks = chapter.blocks
        for bj in range(bi + 1, min(len(blocks), bi + MAX_NOTE_BLOCKS)):
            kind, _level, text = blocks[bj]
            part = text.strip()
            if (kind != 'text' or text.startswith("```") or chapter.roles.get(bj)
                    or _GUT_STAR_OPEN_RE.match(part) or _GUT_NUM_OPEN_RE.match(part)):
                return None
            if part.startswith((_QUOTE_PREFIX, _LIST_PREFIX)):
                part = part[2:].strip()
            parts.append(part)
            total += len(part)
            if total > MAX_NOTE_CHARS:
                return None
            if part.endswith(']'):
                joined = " ".join(parts)
                span = list(range(bi, bj + 1))
                m = _GUT_STAR_NOTE_RE.fullmatch(joined)
                if m:
                    return None, _one_line(m.group(1)), span
                m = _GUT_NUM_NOTE_RE.fullmatch(joined)
                if m:
                    return m.group(1), _one_line(m.group(2)), span
                return None
        return None

    def _mark_notes_headings(self, epub_doc: EPUBDocument) -> None:
        """Kapsamındaki bütün metin blokları dipnot gövdesi olan başlık
        ('NOTAS', 'Notes', bölüm adlı not grupları) 'notes_heading' olur:
        dipnotlar yerlerine taşınınca başlık boş kalır."""
        chapters = epub_doc.chapters
        flat = [(ci, bi, kind, level)
                for ci, ch in enumerate(chapters)
                for bi, (kind, level, _t) in enumerate(ch.blocks)]
        for k, (ci, bi, kind, level) in enumerate(flat):
            if kind != 'heading' or chapters[ci].roles.get(bi):
                continue
            has_note = False
            only_notes = True
            for cj, bj, kj, lj in flat[k + 1:]:
                if kj == 'heading':
                    if lj <= level:
                        break
                    continue
                if chapters[cj].roles.get(bj, '').startswith('note:'):
                    has_note = True
                else:
                    only_notes = False
                    break
            if has_note and only_notes:
                chapters[ci].roles[bi] = 'notes_heading'

    def _mark_subtitles(self, entries) -> None:
        """Bölüm alt adı (R8): başlığın hemen ardından gelen, alt ad olarak
        işaretli kısa paragraf ('p.subh2', epub:type=subtitle,
        role=doc-subtitle, hgroup içi). Sınıf ölçütünde o sınıflı
        paragrafların en az SUBTITLE_CLASS_MIN_SHARE'ı başlıktan hemen sonra
        gelmelidir."""
        total = Counter()
        after = Counter()
        candidates = []
        for chapter, elements, _soup in entries:
            for bi, el in enumerate(elements):
                kind, _level, text = chapter.blocks[bi]
                if kind != 'text':
                    continue
                tokens = [c for c in self._attr_tokens(el, 'class') if _SUBTITLE_CLASS_RE.match(c)]
                follows = bi > 0 and chapter.blocks[bi - 1][0] == 'heading'
                for t in tokens:
                    total[t] += 1
                    if follows:
                        after[t] += 1
                strong = ('subtitle' in self._attr_tokens(el, 'epub:type')
                          or 'doc-subtitle' in self._attr_tokens(el, 'role')
                          or (el.parent is not None and el.parent.name == 'hgroup'))
                if follows and (tokens or strong):
                    candidates.append((chapter, bi, tokens, strong))
        for chapter, bi, tokens, strong in candidates:
            ok = strong or any(after[t] / total[t] >= SUBTITLE_CLASS_MIN_SHARE for t in tokens)
            text = chapter.blocks[bi][2]
            one_line = _one_line(text)
            if (ok and one_line and len(one_line) <= SUBTITLE_MAX_CHARS
                    and not re.search(r'\n\s*\n', text)
                    and not text.startswith(("```", _QUOTE_PREFIX, _LIST_PREFIX))
                    and not chapter.roles.get(bi)):
                chapter.roles[bi] = 'subtitle'

    def _image_positions(self, soup, elements, doc_name: str,
                         skip_ids=frozenset()) -> List[Tuple[int, str]]:
        """Bağımsız gövde görselleri: [(önündeki blok indeksi, EPUB iç yolu)].

        R13 (2026-09-25): <img>/<svg><image> gövdede hiç yazılmıyordu. Metin
        bloğunun İÇİNDEKİ görsel alınmaz: F1-08 (2026-09-29) ile metnin
        içinde '![](…)' olarak yazılır. Başlığın içindeki görsel (başlık
        satır içi görsel taşımaz) ve blok dışındaki (div.figcenter, figure,
        metinsiz p) görsel, belge sırasında önüne düştüğü bloğun indeksiyle
        döner. *skip_ids*: tablo hücresine yazılan görseller.
        """
        from bs4 import Tag

        root = soup.body if soup.body else soup
        order = {}
        for i, node in enumerate(root.descendants):
            if isinstance(node, Tag):
                order[id(node)] = i
        block_ids = {id(e) for e in elements if e.name not in _HEADING_TAGS}
        starts = [order.get(id(e), -1) for e in elements]
        out = []
        for img in root.find_all(_IMAGE_TAGS):
            if id(img) in skip_ids or any(id(a) in block_ids for a in img.parents):
                continue
            src = img.get('src') or img.get('xlink:href') or img.get('href') or ''
            target = self._resolve_href(doc_name, src)
            if target is None or not target[0] or target[0] == doc_name:
                continue
            out.append((bisect.bisect_left(starts, order[id(img)]), target[0]))
        return out

    @staticmethod
    def _find_title_tag(root):
        """Bölüm başlığı etiketi: gövdedeki ilk h1/h2 (yoksa None)."""
        return root.find(['h1', 'h2'])

    def _content_blocks(self, root, prefixes: Optional[Dict[int, str]] = None):
        """İçerik bloklarını belge sırasıyla üret: (eleman, tür, düzey, metin).

        Blok-etiket bazlı çıkarma (NavigableString yerine): her kelime ayrı
        segment olmaz, paragraflar bütün kalır. Metni boş bloklar atlanır.
        parse() (içerik) ile rebuild_epub() (geri yazma) aynı blokları bu
        fonksiyondan alır.

        İç içe içerik (E1a, 2026-09-25): her metin YALNIZ BİR blokta çıkar.
        - Bütün metni alt bloklarda olan blockquote/li kapsayıcıdır: kendisi
          yazılmaz, alt blokları yazılır (alıntıdaki p '> ' önekli, liste
          öğesinin ilk bloğu '- ' önekli). Eskiden blockquote '> metin' ve
          içindeki p 'metin' olarak iki kez çıkıyordu.
        - Başka her içerik bloğunun içindeki içerik etiketleri ayrıca
          yazılmaz (p içindeki satır içi code, pre içindeki code, metni
          karışık li'nin iç listesi); metinleri dış bloğun metnindedir.

        *prefixes* verilirse her yazılan öğenin önek metni ('> ', '- ' ya da
        '') ``prefixes[id(öğe)]``'ye yazılır: rebuild_epub çeviriyi geri
        yazarken öneki öğe adından değil buradan bilir.
        """
        # id(öğe) -> 'container' | 'block' | 'skip' (belge sırasında atalar
        # torunlardan önce gelir).
        roles: Dict[int, str] = {}
        # İlk bloğu yazılmış kapsayıcılar ('- ' öneki yalnız ilk bloğa).
        started = set()
        remove_tags = list(self.REMOVE_TAGS)
        # F1-01 (2026-09-29): <p>'siz blok metni (td/th/caption/dt/dd/
        # figcaption, yaprak div/section, karışık kapsayıcıdaki gevşek metin).
        loose = self._mark_loose_blocks(root)

        for element in root.find_all(True):
            if id(element) in loose:
                text = self._text_block_markdown(element)
                if not text:
                    continue
                if prefixes is not None:
                    prefixes[id(element)] = ""
                yield element, 'text', 0, text
                continue
            if element.name not in _CONTENT_TAGS:
                continue
            # script/style içindekiler (parse'ta zaten silinir; rebuild
            # ağacı değiştirmediği için burada atlanır)
            if element.find_parent(remove_tags):
                roles[id(element)] = 'skip'
                continue
            if loose and any(id(a) in loose for a in element.parents):
                # Gevşek metin bloğunun satır içi parçası (<div>…<code>x</code>…)
                roles[id(element)] = 'skip'
                continue

            content_ancestors = [a for a in element.parents
                                 if a.name in _CONTENT_TAGS]
            if content_ancestors and roles.get(id(content_ancestors[0])) != 'container':
                # Yazılan (ya da yazılanın içindeki) bir bloğun parçası
                roles[id(element)] = 'skip'
                continue

            if self._is_container(element):
                roles[id(element)] = 'container'
                continue
            roles[id(element)] = 'block'

            nearest = content_ancestors[0] if content_ancestors else None
            prefix = ""
            kind, level = 'text', 0
            # Başlıklar: tek satır, satır kırılımı boşluk olur
            if element.name in _HEADING_TAGS:
                kind, level = 'heading', int(element.name[1])
                text = self._heading_markdown(element)
            # Code blokları (format marker eklenmez)
            elif element.name in ('pre', 'code'):
                code = self._code_text(element)
                text = f"```\n{code}\n```" if code else ""
            else:
                text = self._text_block_markdown(element)
                if element.name == 'li':
                    prefix = _LIST_PREFIX
                elif element.name == 'blockquote':
                    prefix = _QUOTE_PREFIX
                elif nearest is not None and nearest.name == 'blockquote':
                    prefix = _QUOTE_PREFIX
                elif (nearest is not None and nearest.name == 'li'
                        and id(nearest) not in started):
                    prefix = _LIST_PREFIX
            if not text:
                continue
            started.update(id(a) for a in content_ancestors)
            if prefixes is not None:
                prefixes[id(element)] = prefix
            yield element, kind, level, f"{prefix}{text}"

    def _text_block_markdown(self, element) -> str:
        """Metin bloğunun markdown'ı (başlık ve kod dışı).

        - Yalnız görsel taşıyan blok (metni yok) blok değildir: '' döner,
          görseli bağımsız gövde görseli olarak kalır (_image_positions).
        - F1-07: tek başına duran display formül '$$…$$' olur.
        - F1-08: satır başındaki/sonundaki görsel kendi satırına alınır
          (şekil + altyazı: '<p><img/><br/>Şekil 2 …</p>'); satır ortasındaki
          (satır içi formül görseli) yerinde kalır.
        """
        text = self._inline_to_markdown(element)
        if not text or not _MD_IMAGE_RE.sub("", text).strip():
            return ""
        if element.name in ('td', 'th') or element.find_parent(['td', 'th']) is not None:
            # Tablo hücresi tek segmenttir (F1-01): '<br/>' + kaynak satır sonu
            # boş satır üretip hücreyi paragraflara bölüyordu; markdown/TeX
            # hücresi tek satırdır, hizalama hücre birimiyle 1:1 olmalı.
            text = re.sub(r"\n\s*\n+", "\n", text)
        m = _DISPLAY_ONLY_RE.fullmatch(text)
        if m:
            return f"$${m.group(1)}$$"
        if "![" in text:
            text = self._place_block_images(text)
        return text

    @staticmethod
    def _place_block_images(text: str) -> str:
        """Satır başı/sonu görseller kendi satırına; tek başına satırdaki
        görselin alt metni atılır (dönüştürücü satırdaki görselin alt
        metnini \\caption yapar; alt metin açıklamadır, altyazı değil)."""
        out = []
        for line in text.split("\n"):
            if "![" not in line:
                out.append(line)
                continue
            lead, trail = [], []
            rest = line.strip()
            while True:
                m = _MD_IMAGE_RE.match(rest)
                if not m:
                    break
                lead.append(m.group(2))
                rest = rest[m.end():].lstrip()
            while rest:
                tail = None
                for m in _MD_IMAGE_RE.finditer(rest):
                    if not rest[m.end():].strip():
                        tail = m
                if tail is None:
                    break
                trail.insert(0, tail.group(2))
                rest = rest[:tail.start()].rstrip()
            out.extend(f"![]({p})" for p in lead)
            if rest:
                out.append(rest)
            out.extend(f"![]({p})" for p in trail)
        return "\n".join(out)

    def _mark_loose_blocks(self, root) -> set:
        """F1-01 (2026-09-29): <p>'siz blok metnini blok yap; döner: gevşek
        metin bloklarının id kümesi.

        Eskiden yalnız h1-6/p/li/blockquote/pre/code blok sayılıyor, başka
        öğenin doğrudan metni (tablo hücresi ve başlığı, yaprak div/section,
        dt/dd, figcaption, gövdedeki gevşek metin) hiç yazılmıyordu (çıkış 0,
        uyarı yok). Blok düzeyi (içerik etiketi olmayan) bir öğenin satır içi
        çocukları dizisi ("koşu") metin taşıyorsa:
        - öğenin bütün çocukları tek koşuysa öğenin KENDİSİ bloktur (<td>,
          <div class="x">metin</div>, <caption>, <dd> ...);
        - karışıksa (<section>metin<p>…</p></section>) her metinli koşu
          <kc-blok> sarmalayıcısına alınır; rebuild_epub yazımdan sonra
          sarmalayıcıyı açar (çıktı yapısı değişmez).
        İçerik etiketlerinin (p, li, blockquote, başlık, pre) içi eski
        mantığa kalır. Satır içi <code> koşunun parçasıdır; koşunun bütün
        metni tek bir <code>'daysa koşu blok olmaz (eski kod bloğu yolu).
        parse() ve rebuild_epub() aynı ağaçta aynı kararı verir (işlev
        yinelenebilir: var olan sarmalayıcı yeniden sarılmaz).
        """
        from bs4 import NavigableString, Tag
        from bs4.element import PreformattedString

        loose: set = set()

        def is_boundary(tag) -> bool:
            name = tag.name
            if name == _KC_BLOCK_TAG or (name in _CONTENT_TAGS and name != 'code'):
                return True
            if _local_name(tag) in _LOOSE_BOUNDARY_TAGS:
                return True
            return tag.find(_LOOSE_BOUNDARY_FIND) is not None

        def run_text(nodes) -> Tuple[bool, bool]:
            """(metin var mı, metnin tamamı <code> içinde mi)"""
            has, outside_code = False, False
            for n in nodes:
                if isinstance(n, NavigableString):
                    if not isinstance(n, PreformattedString) and n.strip():
                        has = outside_code = True
                    continue
                if _local_name(n) in _LOOSE_SKIP_TAGS:
                    continue
                for st in n.find_all(string=True):
                    if isinstance(st, PreformattedString) or not st.strip():
                        continue
                    if any(_local_name(a) in _LOOSE_SKIP_TAGS
                           for a in st.parents if a is not n.parent):
                        continue
                    has = True
                    if not (n.name == 'code' or st.find_parent('code') is not None):
                        outside_code = True
                    if outside_code:
                        break
            return has, has and not outside_code

        def visit(node) -> None:
            name = node.name
            if name in _CONTENT_TAGS or _local_name(node) in _LOOSE_SKIP_TAGS:
                return
            if name == _KC_BLOCK_TAG:
                loose.add(id(node))
                return
            runs, cur, mixed = [], [], False
            for child in list(node.children):
                if isinstance(child, Tag) and is_boundary(child):
                    mixed = True
                    if cur:
                        runs.append(cur)
                        cur = []
                    visit(child)
                elif isinstance(child, (Tag, NavigableString)):
                    cur.append(child)
            if cur:
                runs.append(cur)
            for run in runs:
                has, code_only = run_text(run)
                if not has or code_only:
                    continue
                if (not mixed and len(runs) == 1 and node is not root
                        and name not in ('body', 'html', '[document]')):
                    loose.add(id(node))
                    return
                wrapper = _new_tag(_KC_BLOCK_TAG)
                run[0].insert_before(wrapper)
                for n in run:
                    wrapper.append(n.extract())
                loose.add(id(wrapper))

        visit(root)
        return loose

    def _table_layouts(self, root, elements, blocks) -> Tuple[List[Dict], frozenset]:
        """F1-01 (2026-09-29): markdown tablosu olarak yazılacak tablolar.

        Hücre metinleri ayrı bloklardır (segment başına bir hücre: Türkçe
        EPUB'da çeviri hücreye yazılır); to_markdown bu bloklardan satır/
        hücre yapısını koruyan markdown tablosu yazar. Döner: (tablolar,
        hücreye yazılan görsellerin öğe kimlikleri).

        Tablo ancak hücre yapısı markdown tablosuna kayıpsız sığıyorsa
        tablodur; sığmıyorsa hücre blokları eskisi gibi okuma sırasıyla
        paragraf olur (metin kaybolmaz):
        - iç içe tablo, hücre ya da başlık dışında blok (bozuk yapı),
        - birden çok bloklu hücre, başlık/kod/liste/alıntı bloklu hücre,
        - hücre metninde '|' (markdown tablosunun hücre ayracı),
        - tek sütunlu tablo (düzen kutusu; <td> içindeki metin paragraftır).
        Tablo sözlüğü: {"blocks": [tablonun bütün blokları], "caption":
        [başlık blokları], "rows": [[hücre ya da None (colspan/rowspan
        dolgusu)]], "header": ilk satır başlık mı}; hücre = {"blocks":
        [blok_i], "images": [iç yol]}.
        """
        index = {id(el): i for i, el in enumerate(elements)}
        out: List[Dict] = []
        used_images = set()
        for table in root.find_all('table'):
            if table.find_parent('table') is not None:
                continue
            member = [index[id(d)] for d in table.descendants if id(d) in index]
            if not member:
                continue
            layout = self._table_layout(table, member, elements, blocks)
            if layout is None:
                continue
            for row in layout["rows"]:
                for cell in row:
                    if cell is None or cell["blocks"]:
                        continue
                    paths = []
                    for img in cell.pop("el").find_all(_IMAGE_TAGS):
                        src = (img.get('src') or img.get('xlink:href')
                               or img.get('href') or '')
                        target = self._resolve_href(self._doc_ctx, src)
                        if target is not None and target[0] and target[0] != self._doc_ctx:
                            paths.append(target[0])
                            used_images.add(id(img))
                    cell["images"] = paths
            for row in layout["rows"]:
                for cell in row:
                    if cell is not None:
                        cell.pop("el", None)
            out.append(layout)
        return out, frozenset(used_images)

    @staticmethod
    def _span(cell, attr: str, cap: int) -> int:
        try:
            return max(1, min(cap, int(str(cell.get(attr) or 1).strip())))
        except ValueError:
            return 1

    def _table_layout(self, table, member, elements, blocks) -> Optional[Dict]:
        """Tek tablonun düzeni ya da None (paragraf yolu; bkz. _table_layouts)."""
        if table.find('table') is not None:
            return None
        caption, cell_of = [], {}
        for i in sorted(member):
            el = elements[i]
            cap = el if el.name == 'caption' else el.find_parent('caption')
            if cap is not None and cap.find_parent('table') is table:
                caption.append(i)
                continue
            cell = el if el.name in ('td', 'th') else el.find_parent(['td', 'th'])
            if cell is None or cell.find_parent('table') is not table:
                return None
            cell_of.setdefault(id(cell), []).append(i)
        for ids in cell_of.values():
            if len(ids) != 1:
                return None
            kind, _level, text = blocks[ids[0]]
            if (kind != 'text' or text.startswith(("```", _QUOTE_PREFIX, _LIST_PREFIX))
                    or re.search(r'(?<!\\)\|', text)):
                return None
        rows = [tr for tr in table.find_all('tr') if tr.find_parent('table') is table]
        grid: List[List[Optional[Dict]]] = []
        pending: Dict[int, int] = {}    # sütun -> rowspan'dan kalan satır sayısı
        for tr in rows:
            row: List[Optional[Dict]] = []

            def pad_pending() -> None:
                while pending.get(len(row), 0) > 0:
                    pending[len(row)] -= 1
                    row.append(None)

            for cell in tr.find_all(['td', 'th'], recursive=False):
                pad_pending()
                colspan = self._span(cell, 'colspan', TABLE_MAX_SPAN)
                rowspan = self._span(cell, 'rowspan', TABLE_MAX_SPAN)
                for k in range(colspan):
                    if rowspan > 1:
                        pending[len(row)] = rowspan - 1
                    row.append({"blocks": list(cell_of.get(id(cell), [])), "el": cell,
                                "th": cell.name == 'th'} if k == 0 else None)
            # Satırın sonundaki rowspan dolguları
            while any(n > 0 for c, n in pending.items() if c >= len(row)):
                if pending.get(len(row), 0) > 0:
                    pending[len(row)] -= 1
                row.append(None)
            grid.append(row)
        ncols = max((len(r) for r in grid), default=0)
        if ncols < 2 or not any(c is not None and c["blocks"] for r in grid for c in r):
            return None
        for r in grid:
            r.extend([None] * (ncols - len(r)))
        first = rows[0] if rows else None
        header = bool(grid) and first is not None and (
            (first.parent is not None and first.parent.name == 'thead')
            or all(c is None or c["th"] for c in grid[0]))
        for r in grid:
            for c in r:
                if c is not None:
                    c.pop("th", None)
        return {"blocks": sorted(member), "caption": caption, "rows": grid,
                "header": header}

    def _is_container(self, element) -> bool:
        """blockquote/li bütün metnini alt bloklarda (p, li, pre, başlık,
        blockquote) mı taşıyor? Doğrudan (gevşek) metni olan öğe kapsayıcı
        değildir: tek blok olarak yazılır, metin kaybolmaz ('<li>Giriş
        <ul><li>alt</li></ul></li>'). script/style metni ve yorumlar
        sayılmaz (parse ile rebuild aynı kararı versin)."""
        from bs4.element import PreformattedString

        if element.name not in _CONTAINER_TAGS:
            return False
        has_block_text = False
        for s in element.find_all(string=True):
            if isinstance(s, PreformattedString) or not s.strip():
                continue
            node = s.parent
            in_block = False
            while node is not None and node is not element:
                if node.name in self.REMOVE_TAGS:
                    break
                if node.name in _BLOCK_TAGS:
                    in_block = True
                    break
                node = node.parent
            else:
                return False  # gevşek metin
            if in_block:
                has_block_text = True
        return has_block_text

    @staticmethod
    def _code_text(element) -> str:
        """pre/code metni, boşluk ve satır sonları KORUNARAK.

        Eskiden get_text(strip=True) her metin düğümünü kırpıp bitiştiriyordu:
        '<strong>Problem</strong>\\n\\nfrom typing' -> 'Problemfrom typing',
        sözdizimi renklendirmeli kodda 'def' + ' ' + 'f' -> 'deff'. Yalnız
        baştaki boş satırlar ve sondaki boşluk atılır (ilk satırın girintisi
        kalır).
        """
        text = element.get_text()
        text = re.sub(r'\A(?:[ \t\r\f\v]*\n)+', '', text).rstrip()
        return text if text.strip() else ""

    @staticmethod
    def _block_markdown(kind: str, level: int, text: str) -> str:
        """Bloğun ham markdown'ı (başlıkta EPUB'daki h düzeyiyle)."""
        if kind == 'heading':
            return f"{'#' * level} {text}"
        return text

    def _blocks_to_raw_markdown(self, blocks) -> str:
        """Blokları çift satırla birleştir; çoklu boş satırı teke indir."""
        result = "\n\n".join(self._block_markdown(k, lv, t) for k, lv, t in blocks)
        result = re.sub(r'\n{3,}', '\n\n', result)
        return result.strip()

    def _extract_text(self, soup) -> str:
        """HTML'den temiz metin çıkar (ham h düzeyli markdown)."""
        # Gereksiz etiketleri kaldır
        for tag in soup.find_all(self.REMOVE_TAGS):
            tag.decompose()
        root = soup.body if soup.body else soup
        blocks = [(k, lv, t) for _el, k, lv, t in self._content_blocks(root)]
        return self._blocks_to_raw_markdown(blocks)

    def _heading_markdown(self, element) -> str:
        """Başlık etiketinin tek satırlık markdown metni.

        <br/> tek boşluk olur, tüm boşluk/satır sonları teke iner:
        'I<br/><br/>LE GUN-CLUB' -> 'I LE GUN-CLUB' (eskiden '### I' +
        ayrı 'LE GUN-CLUB' paragrafı); 'Lune<br/>Trajet' eskiden başlıkta
        'LuneTrajet' diye yapışıyordu.
        """
        if element is None:
            return ""
        # Başlıktaki görsel satır içi yazılmaz: bağımsız gövde görselidir
        # (_image_positions; bölüm başlığının ardına taşınır).
        saved = self._inline_images_on
        self._inline_images_on = False
        try:
            text = self._inline_to_markdown(element, br=" ")
        finally:
            self._inline_images_on = saved
        return " ".join(text.split())

    def extract_segments(self, epub_doc: EPUBDocument, include_headings: bool = True) -> List[Dict]:
        """
        Çeviri için segmentleri çıkar

        Returns:
            [{"text": "...", "type": "heading/paragraph", "chapter": 0, "index": 0}, ...]

        Ek anahtarlar (E1, 2026-09-25; yalnız bilgi, segment metni/sayısı/
        sırası değişmez): to_markdown'da yeri değişen ya da yazılmayan
        segmentler işaretlenir ki hizalama (epub_output) onları tanısın.
          "note": "kc-N"       dipnot gövdesi; metni to_markdown'da '[^kc-N]: …'
                               tanım satırındadır (işaretin bölümünün sonunda)
          "note_refs": [...]   paragraftaki dipnot işaretleri, sırayla
          "role": "subtitle"   bölüm alt adı (başlığın altında kc:altad satırı)
          "role": "notes_heading"  yazılmayan dipnot bölümü başlığı
          "role": "title_page" başlık sayfasına taşınan kitap adı/yazar başlığı
        """
        segments = []
        segment_index = 0
        meta = self._segment_meta(epub_doc)

        for ci, chapter in enumerate(epub_doc.chapters):
            chapter_meta = meta.get(ci) or {}
            para_meta = chapter_meta.get("paras")
            # CHAPTER-LOCAL seen_texts (Codex önerisi)
            # Her bölümde aynı cümle farklı bölümde tekrar edebilir
            seen_texts = set()

            def emit_title() -> None:
                nonlocal segment_index
                # Tekrar kontrolü - # temizleyerek normalize et (Codex önerisi)
                norm_title = self._normalize_for_dedup(chapter.title)
                if norm_title not in seen_texts:
                    seen_texts.add(norm_title)
                    segments.append({
                        "text": chapter.title,
                        "type": "heading",
                        "level": 1,  # Bölüm başlıkları level 1
                        "chapter": chapter.order,
                        "index": segment_index,
                        **(chapter_meta.get("title") or {}),
                    })
                    segment_index += 1

            # Başlık segmenti belgedeki YERİNDE (2026-09-29, E kulvarı): başlıktan
            # önce metin varsa (Gutenberg üst bilgisi, epigraf, F1-01 ile artık
            # çıkan gevşek metin) segment sırası markdown/TeX sırasıyla aynı
            # olur; eskiden başlık hep ilk segmentti, sıralı hizalama (E2) onu
            # eşleyemiyordu. Başlık belgenin ilk bloğuysa sıra değişmez.
            title_pi = (self._title_paragraph_index(chapter)
                        if include_headings and chapter.title else None)
            if include_headings and chapter.title and not title_pi:
                emit_title()

            # İçerik paragrafları
            paragraphs = self._split_paragraphs(chapter.content)
            if para_meta is not None and len(para_meta) != len(paragraphs):
                para_meta = None  # beklenmeyen bölünme: ek bilgi verilmez
            for pi, para in enumerate(paragraphs):
                if title_pi and pi == title_pi:
                    emit_title()
                if para.strip():
                    # Tekrar kontrolü - # temizleyerek normalize et (Codex önerisi)
                    norm_para = self._normalize_for_dedup(para)
                    if norm_para in seen_texts:
                        continue  # Tekrar eden paragrafı atla
                    seen_texts.add(norm_para)

                    # Heading tespiti
                    para_type, level = self._detect_heading(para)

                    segment_data = {
                        "text": para.strip().lstrip('#').strip(),
                        "type": para_type,
                        "chapter": chapter.order,
                        "index": segment_index
                    }
                    if para_type == "heading":
                        segment_data["level"] = level
                    if para_meta is not None and para_meta[pi]:
                        segment_data.update(para_meta[pi])

                    segments.append(segment_data)
                    segment_index += 1

        print(f"[EPUB Parser] {segment_index} segment çıkarıldı")
        return segments

    def _title_paragraph_index(self, chapter: EPUBChapter) -> Optional[int]:
        """Bölüm başlığı bloğunun (gövdedeki ilk h1/h2) ilk paragrafının
        ``_split_paragraphs(chapter.content)`` içindeki sırası; yoksa None."""
        return self._title_block_paragraph(chapter.blocks, chapter.title)

    def _title_block_paragraph(self, blocks, title: str) -> Optional[int]:
        pi = 0
        for kind, level, text in blocks:
            if kind == 'heading' and level <= 2 and text == title:
                return pi
            pi += len(self._split_paragraphs(self._block_markdown(kind, level, text)))
        return None

    def _segment_meta(self, epub_doc: EPUBDocument) -> Dict[int, List[Dict]]:
        """{bölüm_i: {"paras": [paragraf başına ek anahtarlar], "title": bölüm
        başlığı segmentinin ek anahtarları}} (extract_segments).

        Paragraflar ``_split_paragraphs(chapter.content)`` ile aynı sırada:
        content blokların birleşimi olduğundan her bloğun paragrafları ardışık
        gelir.
        """
        consumed = {(ci, bi) for ci, bi, _t, _k in self._front_title_entries(epub_doc)}
        if not consumed and not any(ch.roles or ch.note_links or ch.text_note_refs
                                    or ch.printed_toc or ch.printed_toc_heading
                                    for ch in epub_doc.chapters):
            return {}
        used = self._referenced_notes(epub_doc, consumed)
        out: Dict[int, List[Dict]] = {}
        for ci, chapter in enumerate(epub_doc.chapters):
            if not chapter.blocks:
                continue
            rows: List[Dict] = []
            title_meta: Optional[Dict] = None
            for bi, (kind, level, text) in enumerate(chapter.blocks):
                paras = self._split_paragraphs(self._block_markdown(kind, level, text))
                role = chapter.roles.get(bi, "")
                base: Dict = {}
                if (ci, bi) in consumed:
                    base["role"] = "title_page"
                elif role.startswith("note:") and role[5:] in used:
                    base["note"] = role[5:]
                elif role == "subtitle":
                    base["role"] = "subtitle"
                elif role == "notes_heading" and self._notes_heading_empty(epub_doc, ci, bi, used):
                    base["role"] = "notes_heading"
                elif bi in chapter.printed_toc:
                    base["role"] = "printed_toc"
                elif bi in chapter.printed_toc_heading:
                    base["role"] = "printed_toc_heading"
                keys_by_para = self._note_keys_by_paragraph(chapter, bi, text, paras, used)
                for pi, _para in enumerate(paras):
                    row = dict(base)
                    if keys_by_para[pi]:
                        row["note_refs"] = keys_by_para[pi]
                    rows.append(row)
                # Bölüm başlığı segmenti chapter.title'dan (gövdedeki ilk
                # h1/h2) üretilir; o bloğun ek bilgisini alır.
                if (title_meta is None and paras and kind == 'heading'
                        and level <= 2 and chapter.title and text == chapter.title):
                    title_meta = dict(rows[len(rows) - len(paras)])
            out[ci] = {"paras": rows, "title": title_meta or {}}
        return out

    def _note_keys_by_paragraph(self, chapter: EPUBChapter, bi: int, text: str,
                                paras: List[str], used) -> List[List[str]]:
        """Blok paragraflarının her birindeki dipnot anahtarları (sırayla)."""
        found: List[Tuple[int, str]] = []
        for start, _length, key in chapter.text_note_refs.get(bi, ()):
            if key in used:
                found.append((start, key))
        if chapter.note_links:
            for m in _MD_LINK_RE.finditer(text):
                key = chapter.note_links.get(m.group(2))
                if key and key in used and _NOTE_MARKER_RE.match(md_unescape(m.group(1)).strip()):
                    found.append((m.start(), key))
        result: List[List[str]] = [[] for _ in paras]
        if not found:
            return result
        # Paragrafın blok metnindeki başlangıç konumu
        md = self._block_markdown(chapter.blocks[bi][0], chapter.blocks[bi][1], text)
        offset = len(md) - len(text)  # başlıkta '#' öneki
        starts, pos = [], 0
        for para in paras:
            at = md.find(para, pos)
            starts.append(at if at >= 0 else pos)
            pos = (at if at >= 0 else pos) + len(para)
        for start, key in sorted(found):
            idx = max(0, bisect.bisect_right(starts, start + offset) - 1)
            if key not in result[idx]:
                result[idx].append(key)
        return result

    def _normalize_for_dedup(self, text: str) -> str:
        """
        Dedup için metin normalize et (Codex önerisi)
        - Boşlukları normalize et
        - # karakterlerini temizle
        - Küçük harfe çevir
        """
        normalized = re.sub(r'\s+', ' ', text).strip()
        normalized = normalized.lstrip('#').strip()
        # Markdown inline format işaretlerini temizle (dedup tutarlılığı için)
        normalized = re.sub(r'\*{1,2}(.+?)\*{1,2}', r'\1', normalized)
        normalized = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', normalized)
        return normalized.lower()

    def _detect_heading(self, para: str) -> tuple:
        """
        Heading tespiti
        - # ile başlayanlar heading (markdown format from _extract_text)
        - Heuristik kaldırıldı: EPUB'da heading bilgisi HTML tag'lerinde mevcut

        Returns:
            (para_type, level)
        """
        stripped = para.strip()

        # 1. Markdown heading (#)
        if stripped.startswith('#'):
            hashes = ""
            for ch in stripped:
                if ch == '#':
                    hashes += ch
                else:
                    break
            level = len(hashes)
            level = max(1, min(level, 6))  # 1-6 arası
            return "heading", level

        # 2. Normal paragraf (heuristik kaldırıldı - EPUB'da heading bilgisi HTML tag'lerinde zaten mevcut)
        return "paragraph", 0

    def _split_paragraphs(self, text: str) -> List[str]:
        """Metni paragraflara böl"""
        # Çift satır sonuyla böl
        paragraphs = re.split(r'\n\s*\n', text)
        return [p.strip() for p in paragraphs if p.strip()]

    def _inline_to_markdown(self, element, br: str = "\n") -> str:
        """
        Element içeriğini inline formatting koruyarak markdown marker'lara çevir.
        <em>/<i> → *text*, <strong>/<b> → **text**, <a> → [text](url)

        *br*: <br/> yerine konan metin. Paragrafta satır sonu ('\\n');
        başlıkta tek boşluk (_heading_markdown).

        2026-09-29 (E kulvarı):
        - F1-02/F1-09: metin düğümleri kaçışlanır (md_escape); yorum/CDATA
          düğümleri metin değildir (eskiden '<!-- x -->' metne giriyordu).
        - F1-07: <sup>/<sub> '<sup>..</sup>' / '<sub>..</sub>' olur (içerik
          güvenli düz metinse); MathML '$…$' TeX'i olur (mathml_to_latex).
        - F1-08: metin bloğundaki <img> '![alt](iç/yol)' olur (başlıkta
          değil: başlık görseli bağımsız gövde görselidir).
        """
        from bs4 import NavigableString, Tag
        from bs4.element import PreformattedString

        # İç etiketin kenarındaki boşluk/<br/> dışarı taşınır (iç metin
        # strip edilir): '<span>I<br/></span>LE' başlıkta 'ILE',
        # '<i>Blue Shift </i>analysis' paragrafta '*Blue Shift*analysis'
        # diye yapışıyordu. Başlıkta (Y2) ve paragrafta (E1c, 2026-09-25)
        # uygulanır; kenardaki <br/> *br* olur (paragrafta satır sonu).
        # Kenar boşluğu önce işaretle konur, yanında zaten boşluk/satır
        # sonu yoksa tek boşluk olur (satır başına/sonuna boşluk eklenmez).
        def _edge(child, at_start: bool) -> str:
            kind = self._edge_kind(child, at_start)
            if kind == 'br':
                return br
            return _EDGE_SPACE if kind == 'space' else ""

        def _wrap(child, piece: str) -> str:
            lead, tail = _edge(child, True), _edge(child, False)
            if child.name in _BLOCK_LEVEL_HTML:
                lead, tail = lead or _EDGE_SPACE, tail or _EDGE_SPACE
            return f"{lead}{piece}{tail}"

        parts = []
        for child in element.children:
            if isinstance(child, NavigableString):
                if isinstance(child, PreformattedString):
                    continue    # yorum, CDATA, işlem yönergesi: metin değil
                parts.append(md_escape(str(child)))
            elif isinstance(child, Tag):
                name = _local_name(child)
                if name == 'math':
                    piece = self._math_markdown(child)
                    if piece:
                        parts.append(_wrap(child, piece))
                    continue
                if name in _IMAGE_TAGS:
                    piece = self._image_markdown(child)
                    if piece:
                        parts.append(piece)
                    continue
                if child.name != 'br' and not child.get_text().strip():
                    # Yalnız görsel taşıyan iç öğe (<span><img/></span>,
                    # <a href><img/></a>): görsel yazılır (F1-08).
                    if self._inline_images_on and child.find(_IMAGE_TAGS) is not None:
                        inner = self._inline_to_markdown(child, br)
                        if inner.strip():
                            parts.append(_wrap(child, inner))
                            continue
                    # Metni yalnız boşluk olan iç etiket ('</b><i> </i>for',
                    # '</sup><sup> </sup>As'): boşluk/satır sonu korunur,
                    # yoksa iki sözcük yapışıyordu (E1c).
                    kind = self._edge_kind(child, True)
                    if kind == 'br':
                        parts.append(br)
                    elif kind == 'space':
                        parts.append(_EDGE_SPACE)
                    continue
                if child.name in ['em', 'i']:
                    piece = self._emphasis_markdown(child, '*', br)
                    if piece:
                        parts.append(_wrap(child, piece))
                elif child.name in ['strong', 'b']:
                    piece = self._emphasis_markdown(child, '**', br)
                    if piece:
                        parts.append(_wrap(child, piece))
                elif child.name == 'a':
                    href = child.get('href', '')
                    # Bağlantı etiketi tek satır (R12): kaynaktaki satır
                    # sonu etiketi ikiye bölüyor, dönüştürücü satır satır
                    # okuduğu için '[..](..)' tanınmıyordu.
                    inner = _join_source_lines(self._inline_to_markdown(child, " "))
                    if inner and href and not _IMAGES_ONLY_RE.fullmatch(inner):
                        parts.append(_wrap(child, f'[{inner}]({href})'))
                    elif inner:
                        # Metinsiz (yalnız görsel) bağlantı: görsel yazılır,
                        # büyük görsele giden bağlantı PDF'te anlamsızdır.
                        parts.append(_wrap(child, inner))
                elif child.name == 'br':
                    parts.append(br)
                elif child.name in ('sup', 'sub'):
                    piece = self._script_markdown(child, br)
                    if piece.strip():
                        parts.append(_wrap(child, piece))
                else:
                    inner = self._inline_to_markdown(child, br)
                    if inner.strip():
                        parts.append(_wrap(child, inner))

        result = ''.join(parts)
        if _EDGE_SPACE in result:
            result = re.sub(f'(?<=\\s){_EDGE_SPACE}+|{_EDGE_SPACE}+(?=\\s)', '', result)
            result = re.sub(f'{_EDGE_SPACE}+', ' ', result)
        result = re.sub(r' +', ' ', result)
        return result.strip()

    # --- Satır içi nesneler (F1-07 / F1-08, 2026-09-29) -----------------------

    # Metin bloğundaki görseller yazılsın mı? Başlıkta kapalı (başlık görseli
    # bağımsız gövde görseli olarak _image_positions'tan gelir).
    _inline_images_on = True
    # Görsel adreslerinin çözüldüğü belge (EPUB iç yolu); _chapter_from_soup
    # ve rebuild_epub belge başına kurar.
    _doc_ctx = ""

    def _image_markdown(self, img) -> str:
        """Metin bloğundaki görsel: '![alt](iç/yol)'. Çıkarılamayan görsel
        (data: adresi, adressiz) alt metniyle kalır ve uyarılır."""
        if not self._inline_images_on:
            return ""
        src = (img.get('src') or img.get('xlink:href') or img.get('href') or '').strip()
        alt = " ".join((img.get('alt') or "").split())
        alt = re.sub(r"[\[\]\\]", "", alt)
        target = self._resolve_href(self._doc_ctx, src) if src else None
        if target is None or not target[0] or target[0] == self._doc_ctx:
            if src:
                self._note_warning(
                    f"{self._doc_ctx or 'belge'}: metin içindeki görsel "
                    f"çıkarılamıyor ({src[:40]}…); "
                    + (f"yerine alt metni yazıldı: '{alt}'." if alt else "alt metni yok."))
            return md_escape(alt) if alt else ""
        return f"![{md_escape(alt)}]({target[0]})"

    def _math_markdown(self, math) -> str:
        """MathML -> '$TeX$' (display ise '$\\displaystyle …$'; blokta tek
        başınaysa _content_blocks '$$…$$' yapar). Çevrilemeyen öğe varsa
        uyarılır; TeX'e çevrilemeyen formül alt metniyle ya da düz metniyle
        kalır (kaybolmaz)."""
        tex, display, unsupported = mathml_to_latex(math)
        if unsupported:
            self._note_warning(
                f"{self._doc_ctx or 'belge'}: MathML formülünde tanınmayan öğe "
                f"({', '.join(sorted(unsupported))}); içeriği düz yazıldı: "
                f"{tex[:60]!r}.")
        if not tex:
            alt = " ".join((math.get('alttext') or math.get_text(" ")).split())
            return md_escape(alt)
        tex = tex.replace("$", r"\$")
        return f"${'\\displaystyle ' if display else ''}{tex}$"

    def _script_markdown(self, element, br: str) -> str:
        """<sup>/<sub>: içerik güvenli düz metinse '<sup>..</sup>'.

        Bağlantı (dipnot işareti), görsel, formül ya da dönüştürücünün
        <sup> kuralının kaldıramayacağı karakter ('<', '$', '\\', '_', '{')
        içeren öğe eski yoldan (işaretsiz satır içi metin) yazılır."""
        tag = element.name
        raw = " ".join(element.get_text().split())
        if (raw and _SCRIPT_SAFE_RE.fullmatch(raw)
                and element.find(['a', 'img', 'image', 'br'] + list(_BLOCK_LEVEL_HTML)) is None
                and not any(_local_name(t) == 'math' for t in element.find_all(True))):
            return f"<{tag}>{raw}</{tag}>"
        return self._inline_to_markdown(element, br)

    def _note_warning(self, message: str) -> None:
        """Ayrıştırma uyarısı: bir kez basılır, EPUBDocument.warnings'e gider."""
        if message not in self._warnings:
            self._warnings.append(message)
            _warn(message)

    # <br/> için geçici işaret (vurgu içinde satır bölme; XML metninde yok).
    _BR_MARK = "\x01"

    def _emphasis_markdown(self, element, mark: str, br: str) -> str:
        """<i>/<em> ('*') ya da <b>/<strong> ('**') öğesinin markdown'ı.

        R12 (2026-09-25): vurgu içindeki kaynak satır sonları (HTML'de
        boşluktur) tek boşluğa iner; eskiden '*Baltimore,\\n3 octobre*' iki
        satıra bölünüyor, dönüştürücü satır satır okuduğu için yıldızlar
        LaTeX'e/PDF'e ham giriyordu. Vurgunun içindeki <br/> gerçek satır
        sonudur: her satır ayrı vurgulanır ('*bir*\\n*iki*').
        """
        inner = self._inline_to_markdown(element, self._BR_MARK)
        if "\n" not in br and br != self._BR_MARK:
            # Başlık kipi (<br/> boşluk): tek vurgu parçası.
            inner = inner.replace(self._BR_MARK, " ")
        lines = []
        for line in inner.split(self._BR_MARK):
            line = _join_source_lines(line)
            if line:
                lines.append(f"{mark}{line}{mark}")
        return br.join(lines)

    @staticmethod
    def _edge_kind(tag, at_start: bool) -> Optional[str]:
        """Etiketin ilk (at_start) / son içeriği: 'br' (<br/>), 'space'
        (boşlukla başlayan/biten metin) ya da None. Boş etiketler
        (<a id="x"/>) atlanır."""
        from bs4 import NavigableString, Tag

        nodes = list(tag.descendants)
        for node in (nodes if at_start else reversed(nodes)):
            if isinstance(node, Tag):
                if node.name == 'br':
                    return 'br'
                continue
            if isinstance(node, NavigableString):
                text = str(node)
                if not text:
                    continue
                return 'space' if (text[0] if at_start else text[-1]).isspace() else None
        return None

    @staticmethod
    def _fill_missing_toc_uids(toc) -> None:
        """İçindekiler ağacında uid'i olmayan Link/Section'lara benzersiz
        uid ver (var olanlara dokunulmaz)."""
        used = set()
        pending = []

        def walk(entries):
            for entry in entries or []:
                if isinstance(entry, (tuple, list)):
                    head = entry[0] if entry else None
                    children = entry[1] if len(entry) > 1 else []
                    if head is not None:
                        pending.append(head)
                    walk(children)
                else:
                    pending.append(entry)

        walk(toc)
        for obj in pending:
            uid = getattr(obj, 'uid', None)
            if uid:
                used.add(uid)
        n = 0
        for obj in pending:
            if hasattr(obj, 'uid') and not getattr(obj, 'uid', None):
                n += 1
                while f"navpoint-{n}" in used:
                    n += 1
                obj.uid = f"navpoint-{n}"
                used.add(obj.uid)

    def _apply_translation(self, element, translated, soup=None,
                           prefix: Optional[str] = None, ctx=None) -> None:
        """Çeviriyi öğeye yaz (markdown işaretleri HTML'e çevrilir).

        *translated* liste ise parçalar <br/> ile birleştirilir (soup gerekir).
        *prefix*: _content_blocks'un bu öğeye koyduğu önek ('> ', '- ', '');
        ilk parçadan bir kez atılır (alıntı içindeki p de '> ' taşır). None
        ise önek öğe adından çıkarılır (li '-', blockquote '>').

        R9 (2026-09-28): öğenin içindeki kimlikli (id) öğeler — başlıktaki
        <a id="I"/> çapası, sayfa çapası <span id="Page_5"/>, dipnot işareti
        ve geri dönüş bağlantısı — ve satır içi görseller öğe temizlenirken
        kaybolmaz (_take_inline_keep / _restore_inline). *ctx*
        (_RebuildContext) not yer tutucularını çözer.
        """
        from bs4 import BeautifulSoup, NavigableString

        marker = prefix.strip() if prefix else ""
        pieces = translated if isinstance(translated, list) else [translated]
        cleaned = []
        for i, piece in enumerate(pieces):
            # Heading için prefix temizle (### gibi)
            if element.name in _HEADING_TAGS:
                piece = piece.lstrip('#').strip()
            # Liste öğesi / alıntı öneki yalnız ilk parçada bulunur
            if i == 0 and prefix is not None:
                if marker and piece.lstrip().startswith(marker):
                    piece = piece.lstrip()[len(marker):].strip()
            elif i == 0 and element.name == 'li':
                piece = piece.lstrip('-').strip()
            elif i == 0 and element.name == 'blockquote':
                piece = piece.lstrip('>').strip()
            cleaned.append(piece)

        head_break = self._heading_break(element)
        styles = self._style_wrappers(element)
        keep = self._take_inline_keep(element, ctx)
        if len(cleaned) == 1:
            translated_text = cleaned[0]
            # Element içeriğini değiştir - markdown marker'ları HTML'e çevir
            html_text = self._markdown_to_inline_html(translated_text)
            if html_text != translated_text:
                # Inline formatting var - HTML olarak parse et ve ekle
                element.clear()
                temp = BeautifulSoup(html_text, 'html.parser')
                for child in list(temp.children):
                    element.append(child.extract())
            elif len(element.contents) == 1 and isinstance(element.contents[0], NavigableString):
                element.string = translated_text
            else:
                element.clear()
                element.append(NavigableString(translated_text))
        else:
            element.clear()
            for i, piece in enumerate(cleaned):
                if i:
                    element.append(_new_tag('br'))
                temp = BeautifulSoup(self._markdown_to_inline_html(piece), 'html.parser')
                for child in list(temp.children):
                    element.append(child.extract())
        if head_break is not None:
            self._restore_heading_break(element, *head_break)
        self._restore_inline(element, keep, ctx)
        self._restore_style_wrappers(element, styles)

    @staticmethod
    def _style_wrappers(element) -> List[Tuple[str, Dict[str, str], str]]:
        """F2-08 (2026-09-29): yalnız biçim taşıyan satır içi öğeler
        (<small>, <span class>, <abbr>, <u> ...): (ad, öznitelikler, metin).

        Çeviri öğeyi yeniden yazar; bu öğelerin çeviride yeri bilinmez.
        Metinleri çeviride AYNEN ve bir kez geçiyorsa (sayı, ad, sayfa
        göndermesi, kimlik çevirisi) o metin yeniden sarılır; geçmiyorsa
        (metin çevrilmiştir) biçim uygulanamaz (_restore_style_wrappers).
        """
        out = []
        for tag in element.find_all(list(_STYLE_WRAPPER_TAGS)):
            if tag.get('id') or tag.find(True) is not None:
                continue
            if tag.name == 'span' and not (tag.get('class') or tag.get('style')):
                continue
            text = " ".join(tag.get_text().split())
            if len(text) >= 2:
                out.append((tag.name, _attr_dict(tag), text))
        return out

    @staticmethod
    def _restore_style_wrappers(element, wrappers) -> None:
        from bs4 import NavigableString

        for name, attrs, text in wrappers:
            full = " ".join(element.get_text().split())
            if full.count(text) != 1:
                continue
            for node in list(element.descendants):
                if not isinstance(node, NavigableString) or text not in str(node):
                    continue
                if node.parent is not None and node.parent.name == name:
                    break
                before, _, after = str(node).partition(text)
                wrapped = _new_tag(name, attrs)
                wrapped.string = text
                parts = ([NavigableString(before)] if before else []) + [wrapped] + (
                    [NavigableString(after)] if after else [])
                node.replace_with(parts[0])
                prev = parts[0]
                for part in parts[1:]:
                    prev.insert_after(part)
                    prev = part
                break

    @staticmethod
    def _heading_break(element):
        """Başlıkta kısa önekten (Roma rakamı, numara) sonraki <br/>'ler:
        (önek, <br/> sayısı) ya da None ('I<br/><br/>LE CLUB' ->
        ('I', 2)). Çeviri aynı önekle başlarsa satır kırılımı geri konur."""
        from bs4 import NavigableString, Tag

        if element.name not in _HEADING_TAGS:
            return None
        lead, brs = [], 0
        for node in element.descendants:
            if isinstance(node, Tag) and node.name == 'br':
                if not "".join(lead).strip():
                    return None
                brs += 1
                continue
            if isinstance(node, NavigableString):
                if brs:
                    if str(node).strip():
                        break
                    continue
                lead.append(str(node))
        prefix = " ".join("".join(lead).split())
        if not brs or not prefix or len(prefix) > 12:
            return None
        return prefix, brs

    @staticmethod
    def _restore_heading_break(element, prefix: str, brs: int) -> None:
        from bs4 import NavigableString

        first = next((n for n in element.descendants
                      if isinstance(n, NavigableString) and str(n).strip()), None)
        if first is None:
            return
        text = str(first)
        stripped = text.lstrip()
        if not (stripped.startswith(prefix + " ") and first.parent is element):
            return
        rest = stripped[len(prefix):].lstrip()
        nodes = [NavigableString(prefix)] + [_new_tag('br') for _ in range(brs)] \
            + [NavigableString(rest)]
        first.replace_with(nodes[0])
        anchor = nodes[0]
        for node in nodes[1:]:
            anchor.insert_after(node)
            anchor = node

    def _html_safe_text(self, text: str) -> str:
        """Metin HTML olarak ayrıştırılacaksa (markdown/EPUB_* işareti
        varsa) '<', '>', '&' kaçışlanır; değilse olduğu gibi (öğeye düz
        yazılır). epub_output._html_safe'in kuralı."""
        import html as _html

        if not text or not re.search(r"[<>&]", text):
            return text
        escaped = _html.escape(text, quote=False)
        if self._markdown_to_inline_html(escaped) != escaped:
            return escaped
        return text

    def _take_inline_keep(self, element, ctx) -> List[Dict]:
        """Öğe temizlenmeden önce korunacak iç öğeleri çıkar.

        Korunan: kimlikli (id) öğe, dipnot bağlantısı (adresi bir dipnota
        giden <a>), görsel (img/svg). Metni dipnot işareti ('[1]', '*')
        olmayan kimlikli öğe (çevrilen metni taşır) yalnız kimliğiyle, boş
        bir kopya olarak korunur. Yer: öncesinde metin yoksa öğenin başı,
        varsa sonu (çeviride tam yer bilinmez).
        """
        note_hrefs = ctx.note_hrefs if ctx is not None else {}
        taken: List[Dict] = []
        seen = set()
        for tag in list(element.find_all(True)):
            if any(id(a) in seen for a in tag.parents):
                continue
            tid = tag.get('id')
            href = tag.get('href') if tag.name == 'a' else None
            is_note = href is not None and href in note_hrefs
            is_image = tag.name in ('img', 'svg')
            # F1-07: MathML formülü de korunur (çeviride '$…$' nesne yer
            # tutucusunun yerine konur; eskiden formül düz metne eziliyordu).
            is_math = _local_name(tag) == 'math'
            if not (tid or is_note or is_image or is_math):
                continue
            where = 'start' if self._is_leading(element, tag) else 'end'
            text = " ".join(tag.get_text(" ").split())
            if (text and not is_note and not is_image and not is_math
                    and not _NOTE_MARKER_RE.match(text)):
                if tag.name in ('a', 'span') or tag.name in _HEADING_TAGS:
                    stub_name = tag.name if tag.name in ('a', 'span') else 'span'
                else:
                    stub_name = 'span'
                taken.append({'tag': _new_tag(stub_name, {'id': tid}),
                              'where': where, 'used': False})
                continue
            seen.add(id(tag))
            kind = 'g' if is_image else ('m' if is_math else None)
            taken.append({'tag': tag.extract(), 'where': where, 'used': False,
                          'kind': kind})
        return taken

    @staticmethod
    def _is_leading(element, tag) -> bool:
        """*tag*'den önce öğede (belge sırasıyla) görünür metin yok mu?"""
        from bs4 import NavigableString

        for node in element.descendants:
            if node is tag:
                return True
            if isinstance(node, NavigableString) and str(node).strip():
                return False
        return True

    def _restore_inline(self, element, keep: List[Dict], ctx) -> None:
        """Yer tutucuları çöz, korunan öğeleri geri koy (_take_inline_keep)."""
        from bs4 import NavigableString

        note_hrefs = ctx.note_hrefs if ctx is not None else {}

        def take(pred):
            for k in keep:
                if not k['used'] and pred(k['tag']):
                    k['used'] = True
                    return k['tag']
            return None

        # 0) Satır içi nesneler (F1-07/F1-08): formül/görsel yer tutucusu
        #    kaynaktaki aynı türden sıradaki özgün öğeyle değişir; karşılığı
        #    yoksa formülün TeX'i metin olarak kalır (kaybolmaz).
        for ph in element.find_all('kc-nesne'):
            kind = ph.get('t', '')
            tag = None
            for k in keep:
                if not k['used'] and k.get('kind') == kind:
                    k['used'] = True
                    tag = k['tag']
                    break
            if tag is not None:
                ph.replace_with(tag)
            elif kind == 'm' and ph.get_text().strip():
                ph.replace_with(NavigableString(ph.get_text()))
            else:
                ph.decompose()
        # 1) Kaynak dipnotunun işareti: özgün bağlantı ya da Gutenberg metni
        for ph in element.find_all('kc-kayref'):
            key = ph.get('k', '')
            tag = take(lambda t, key=key: t.name == 'a'
                       and note_hrefs.get(t.get('href')) == key)
            if tag is None and ctx is not None and key in ctx.text_marks:
                tag = NavigableString(ctx.text_marks[key])
            if tag is None:
                ph.decompose()
            else:
                ph.replace_with(tag)
        # 2) Çeviri özgün işaretle aynı adrese bağlanıyorsa özgün öğe (id'si
        #    geri dönüş bağlantısının hedefidir) kullanılır.
        kept_ids = {id(k['tag']) for k in keep}
        for a in list(element.find_all('a', href=True)):
            if id(a) in kept_ids:
                continue
            href = a['href']
            tag = take(lambda t, href=href: t.name == 'a' and t.get('href') == href)
            if tag is not None:
                a.replace_with(tag)
        # 2b) Çeviride aynen duran işaret metni ('[5]' sayfa numarası, '[1]')
        #     özgün öğeyle yer değiştirir (metin iki kez görünmesin, çapa
        #     doğru yerde kalsın).
        for k in keep:
            tag = k['tag']
            if k['used'] or isinstance(tag, NavigableString):
                continue
            mark = tag.get_text().strip()
            if not mark or not _NOTE_MARKER_RE.match(mark):
                continue
            placed = {id(x['tag']) for x in keep if x['used']}
            for node in list(element.descendants):
                if (not isinstance(node, NavigableString) or mark not in str(node)
                        or any(id(p) in placed for p in node.parents)):
                    continue
                before, _, after = str(node).partition(mark)
                parts = [NavigableString(before)] if before else []
                parts.append(tag)
                if after:
                    parts.append(NavigableString(after))
                node.replace_with(parts[0])
                prev = parts[0]
                for part in parts[1:]:
                    prev.insert_after(part)
                    prev = part
                k['used'] = True
                break
        # 3) Yeni notların (çevirmen notu) işaretleri
        for ph in element.find_all('kc-notref'):
            ref = ctx.note_ref(ph.get('n', '')) if ctx is not None else None
            if ref is None:
                ph.decompose()
            else:
                ph.replace_with(ref)
        # 4) Kalan korunan öğeler başa/sona
        starts = [k['tag'] for k in keep if not k['used'] and k['where'] == 'start']
        for tag in reversed(starts):
            if tag.get_text().strip():
                first = element.contents[0] if element.contents else None
                if first is not None and not (isinstance(first, NavigableString)
                                              and str(first)[:1].isspace()):
                    element.insert(0, NavigableString(" "))
            element.insert(0, tag)
        for k in keep:
            if not k['used'] and k['where'] == 'end':
                element.append(k['tag'])

    def _markdown_to_inline_html(self, text: str) -> str:
        """
        Markdown inline format marker'larını HTML tag'lerine geri çevir.
        **text** → <strong>, *text* → <em>, [text](url) → <a>

        E2 (2026-09-28):
        - Yıldız vurgusu Gutenberg işaretini ('[*]', '[* not]') yakalamaz:
          açılış '[' ya da harf/rakamdan sonra, kapanış ']' ya da harf/rakamdan
          önce gelemez ('haritalar[*] … [* Ay' eskiden <em> oluyordu).
        - Köşeli ayraçlı etiketli bağlantı ('[[1]](not.xhtml#n1)') tanınır.
        - EPUB_* işaretleri (\\textit/\\textbf/\\textsuperscript, not
          yer tutucuları) HTML'e çevrilir; not yer tutucuları geçici
          <kc-notref>/<kc-kayref> öğesi olur (_apply_translation çözer).
        """
        # F1 (2026-09-29): satır içi nesneler (EPUB_OBJ yer tutucusu, markdown
        # görseli) önce indeksli yer tutucuya alınır: içerikleri (TeX) vurgu/
        # bağlantı düzenlerinden korunur; sonunda <kc-nesne> olur ve
        # _restore_inline kaynak öğedeki özgün <math>/<img>'yi yerine koyar.
        objects: List[Tuple[str, str]] = []

        def stash(kind: str, payload: str) -> str:
            objects.append((kind, payload))
            return f"{_OBJ_PH_OPEN}{len(objects) - 1}{_OBJ_PH_CLOSE}"

        if EPUB_OBJ_OPEN in text:
            text = _EPUB_OBJ_RE.sub(lambda m: stash(m.group(1), m.group(2)), text)
        if "![" in text:
            text = _MD_IMAGE_RE.sub(lambda m: stash("g", m.group(2)), text)
        # Kaçışlı karakter düz karakterdir (F1-09): vurgu/bağlantı sayılmaz.
        if "\\" in text:
            text = re.sub(r"\\([\\`*_\[\]$#|])", lambda m: _ESC_PH[m.group(1)], text)
        # Bold önce (** before *)
        text = re.sub(r'\*\*(?=\S)(.+?)(?<=\S)\*\*', r'<strong>\1</strong>', text)
        # Sonra italic
        text = re.sub(r'(?<![\[\w*])\*(?=[^\s\]*])(.+?)(?<=[^\s\[*])\*(?![\]\w*])',
                      r'<em>\1</em>', text)
        # Link'ler
        text = _MD_LINK_HTML_RE.sub(r'<a href="\2">\1</a>', text)
        if objects:
            text = re.sub(re.escape(_OBJ_PH_OPEN) + r"(\d+)" + re.escape(_OBJ_PH_CLOSE),
                          lambda m: '<kc-nesne t="{0}">{1}</kc-nesne>'.format(
                              *objects[int(m.group(1))]), text)
        if any(ch in text for ch in _ESC_PH_BACK):
            text = "".join(_ESC_PH_BACK.get(ch, ch) for ch in text)
        if any(m in text for m in _EPUB_MARKS):
            for (o, c), tag in _EPUB_WRAPS:
                text = re.sub(re.escape(o) + r'(.*?)' + re.escape(c),
                              rf'<{tag}>\1</{tag}>', text, flags=re.S)
            text = re.sub(re.escape(EPUB_NOTE_REF_OPEN) + r'(\d+)'
                          + re.escape(EPUB_NOTE_REF_CLOSE),
                          r'<kc-notref n="\1"></kc-notref>', text)
            text = re.sub(re.escape(EPUB_SOURCE_NOTE_OPEN) + r'([^<>"'
                          + re.escape(EPUB_SOURCE_NOTE_CLOSE) + r']*)'
                          + re.escape(EPUB_SOURCE_NOTE_CLOSE),
                          r'<kc-kayref k="\1"></kc-kayref>', text)
            # Eşi olmayan işaret atılır (bozuk HTML olmasın).
            text = re.sub('[' + ''.join(_EPUB_MARKS) + ']', '', text)
        return text

    def to_markdown(self, epub_doc: EPUBDocument) -> str:
        """
        EPUB dokümanını Markdown'a dönüştür

        Üst bilgi (R18, 2026-09-25): yazar satırları kitap adı başlığından
        ÖNCE yazılır (dönüştürücü başlık sayfası sözleşmesi: ilk başlıktan
        önceki kısa satırlar yazardır; eskiden '*Yazar*' başlıktan sonra
        gövdeye düşüyordu). Kitap adı dc:title'dır; gövdedeki başlık sayfası
        başlığı aynı sözcükleri taşıyorsa büyük/küçük harf yazımı ondan
        alınır ('De la terre à la lune' -> 'De la Terre à la Lune'). Başlık
        sayfası başlıkları (kitap adı / yazar) gövdede ikinci kez yazılmaz.
        """
        lines = []
        plan = self._plan_headings(epub_doc)
        front_title, authors, consumed = self._title_page(epub_doc)

        # Başlık
        if epub_doc.title:
            for author in authors:
                lines.append(author)
            if authors:
                lines.append("")
            # dc:title düz metindir (F1-09: markdown'a kaçışlı).
            lines.append(f"# {md_escape(front_title)}")
            lines.append("")
            lines.append("---")
            lines.append("")

        # Bölümler. Bölüm başlığı AYRICA yazılmaz: chapter.title gövdedeki
        # ilk h1/h2'dir ve bloklarda zaten vardır (eskiden '## Başlık' +
        # '# Başlık' iki kez çıkıyordu). Başlık düzeyleri _plan_headings'e
        # göre normalize edilir.
        used = self._referenced_notes(epub_doc, consumed)
        defined = set()
        for ci, chapter in enumerate(epub_doc.chapters):
            if not chapter.blocks:
                # Blok bilgisi olmayan (elle kurulmuş) bölüm: eski davranış
                if chapter.title:
                    lines.append(f"\n## {chapter.title}\n")
                lines.append(chapter.content)
                lines.append("")
                continue

            rendered = []
            note_keys: List[str] = []
            images = sorted(chapter.images, key=lambda x: x[0])
            img_i = 0
            after_heading = False
            deferred: List[str] = []  # bölüm başlığından sonraya kalan görseller
            # F1-01: markdown tablosu olarak yazılan tabloların hücre blokları;
            # tablo ilk hücre bloğunun yerinde bütün olarak yazılır.
            cell_blocks = set()
            table_start: Dict[int, Dict] = {}
            for layout in chapter.tables:
                cells = [b for r in layout["rows"] for c in r if c is not None
                         for b in c["blocks"]]
                cell_blocks.update(cells)
                if cells:
                    table_start[min(cells)] = layout
            for bi, (kind, level, text) in enumerate(chapter.blocks):
                role = chapter.roles.get(bi, "")
                if deferred and not (role == "subtitle" and after_heading):
                    rendered.extend(deferred)
                    deferred = []
                    after_heading = False
                pending = []
                while img_i < len(images) and images[img_i][0] <= bi:
                    pending.append(f"![]({images[img_i][1]})")
                    img_i += 1
                if (pending and kind == 'heading' and (ci, bi) not in consumed
                        and plan.get((ci, bi), level) == 1):
                    # Bölüm başlığının hemen önündeki süs/görsel: \chapter yeni
                    # sayfa açtığı için önceki sayfada (ilk bölümde
                    # İçindekiler'den önce) yalnız kalıyordu; başlığın (ve
                    # alt adının) ardına yazılır.
                    deferred.extend(pending)
                elif pending:
                    rendered.extend(pending)
                    after_heading = False
                if bi in cell_blocks:
                    if bi in table_start:
                        rendered.append(self._render_table(
                            epub_doc, chapter, ci, table_start[bi], note_keys,
                            used, consumed))
                        after_heading = False
                    continue
                if (ci, bi) in consumed:
                    continue
                if role.startswith("note:") and role[5:] in used:
                    continue  # metni dipnot tanımında
                if role == "notes_heading" and self._notes_heading_empty(epub_doc, ci, bi, used):
                    continue
                text = self._render_note_refs(epub_doc, chapter, bi, text, note_keys, used)
                if kind != 'heading':
                    if role == "subtitle" and after_heading:
                        # R8: başlığın hemen altındaki satır (kc:altad)
                        rendered[-1] += "\n" + SUBTITLE_MARKER + _join_source_lines(text)
                        after_heading = False
                        continue
                    rendered.append(self._escape_heading_like(text))
                    after_heading = False
                    continue
                md_level = plan.get((ci, bi), level)
                if md_level is None:
                    # Başlık sayfası satırı (kitap adı / yazar): bölüm
                    # değildir, düz paragraf olarak kalır.
                    rendered.append(self._escape_heading_like(text))
                    after_heading = False
                else:
                    rendered.append(f"{'#' * md_level} {text}")
                    after_heading = True
            rendered.extend(deferred)
            for _pos, name in images[img_i:]:
                rendered.append(f"![]({name})")
            for key in note_keys:
                if key not in defined:
                    defined.add(key)
                    rendered.append(f"[^{key}]: {epub_doc.notes[key]}")
            body = re.sub(r'\n{3,}', '\n\n', "\n\n".join(rendered)).strip()
            if not body:
                continue
            lines.append(body)
            lines.append("")

        return "\n".join(lines)

    def _render_table(self, epub_doc: EPUBDocument, chapter: EPUBChapter, ci: int,
                      layout: Dict, note_keys: List[str], used, consumed) -> str:
        """F1-01: tablonun markdown'ı (satır/hücre yapısı korunur).

        Hücre = hücre bloğunun tek satır markdown'ı (<br/> boşluk olur;
        dipnot işaretleri '[^kc-N]') ya da yalnız görsel hücrede '![](yol)'.
        Başlık satırı yoksa (<th>/<thead> yok) ilk satır BOŞ başlık satırıdır:
        markdown tablosu başlık satırı ister, boş başlık "başlıksız tablo"
        demektir (C kulvarı sözleşmesi). colspan/rowspan dolguları boş hücre.
        """
        def cell_md(cell) -> str:
            if cell is None:
                return ""
            parts = []
            for bi in cell["blocks"]:
                role = chapter.roles.get(bi, "")
                if (ci, bi) in consumed or (role.startswith("note:") and role[5:] in used):
                    continue
                text = self._render_note_refs(epub_doc, chapter, bi,
                                              chapter.blocks[bi][2], note_keys, used)
                parts.append(_one_line(text))
            parts.extend(f"![]({p})" for p in cell.get("images", ()))
            return " ".join(p for p in parts if p)

        rows = [[cell_md(c) for c in r] for r in layout["rows"]]
        ncols = len(rows[0])
        if layout["header"]:
            head, body = rows[0], rows[1:]
        else:
            head, body = [""] * ncols, rows
        out = ["| " + " | ".join(head) + " |", "|" + "---|" * ncols]
        out.extend("| " + " | ".join(r) + " |" for r in body)
        return "\n".join(out)

    def _render_note_refs(self, epub_doc: EPUBDocument, chapter: EPUBChapter,
                          bi: int, text: str, note_keys: List[str], used) -> str:
        """Bloğun dipnot işaretlerini '[^kc-N]' yap (R11/R20).

        Gutenberg düz metin işaretleri kayıtlı konumlarından (sağdan sola),
        bağlantı işaretleri adresinden değiştirilir. Tanımı olmayan dipnot
        bağlantısı '<sup>N</sup>' olur (sözleşme; N ayraçsız işaret).
        Kullanılan anahtarlar ilk geçiş sırasıyla *note_keys*'e eklenir.
        """
        refs = chapter.text_note_refs.get(bi)
        found: List[Tuple[int, str]] = []
        if refs:
            for start, length, key in sorted(refs, reverse=True):
                if key in used and key in epub_doc.notes:
                    text = text[:start] + f"[^{key}]" + text[start + length:]
                    found.append((start, key))
        if chapter.note_links:
            def repl(m):
                label, href = m.group(1), m.group(2)
                if (href not in chapter.note_links
                        or not _NOTE_MARKER_RE.match(md_unescape(label).strip())):
                    return m.group(0)
                key = chapter.note_links[href]
                if key and key in used and key in epub_doc.notes:
                    found.append((m.start(), key))
                    return f"[^{key}]"
                return f"<sup>{_note_marker_text(label)}</sup>"
            text = _MD_LINK_RE.sub(repl, text)
        for _pos, key in sorted(found):
            if key not in note_keys:
                note_keys.append(key)
        return text

    def _referenced_notes(self, epub_doc: EPUBDocument, consumed) -> set:
        """Gövdede (yazılan bir blokta) işareti bulunan dipnot anahtarları.
        İşareti yazılmayan dipnotun gövdesi yerinde paragraf olarak kalır
        (metin kaybolmaz)."""
        used = set()
        for ci, chapter in enumerate(epub_doc.chapters):
            for bi, (_kind, _level, text) in enumerate(chapter.blocks):
                if (ci, bi) in consumed or chapter.roles.get(bi, "").startswith("note:"):
                    continue
                for _s, _l, key in chapter.text_note_refs.get(bi, ()):
                    used.add(key)
                if chapter.note_links:
                    for m in _MD_LINK_RE.finditer(text):
                        key = chapter.note_links.get(m.group(2))
                        if key and _NOTE_MARKER_RE.match(md_unescape(m.group(1)).strip()):
                            used.add(key)
        return {k for k in used if k in epub_doc.notes}

    @staticmethod
    def _notes_heading_empty(epub_doc: EPUBDocument, ci: int, bi: int, used) -> bool:
        """'notes_heading' başlığının kapsamındaki bütün dipnotlar yerine
        taşındı mı? (Taşınmayan dipnot kalırsa başlık da kalır.)"""
        chapters = epub_doc.chapters
        level = chapters[ci].blocks[bi][1]
        started = False
        for cj in range(ci, len(chapters)):
            blocks = chapters[cj].blocks
            for bj in range(bi + 1 if cj == ci else 0, len(blocks)):
                kind, lvl, _t = blocks[bj]
                if kind == 'heading':
                    if lvl <= level:
                        return started
                    continue
                role = chapters[cj].roles.get(bj, "")
                if not role.startswith("note:") or role[5:] not in used:
                    return False
                started = True
        return started

    # ------------------------------------------------------------------
    # Başlık sayfası (R18)
    # ------------------------------------------------------------------

    def _title_page(self, epub_doc: EPUBDocument):
        """(kitap adı, yazar satırları, başlık sayfasına alınan blokları).

        Başlık sayfası başlıkları _plan_headings'in 1. adımıyla aynı ölçütle
        bulunur (ilk başlıklı belgede, ilk FRONT_TITLE_SCAN başlık içinde
        başlayan ardışık kitap adı / yazar başlıkları). Yazar satırları:
        gövdedeki yazar başlıkları, yoksa dc:creator değerleri.
        """
        consumed = set()
        title = epub_doc.title
        bylines: List[str] = []
        title_heading = None
        for ci, bi, text, kind in self._front_title_entries(epub_doc):
            consumed.add((ci, bi))
            if kind == 'title':
                if title_heading is None:
                    title_heading = text
            else:
                bylines.append(text)
        if title_heading:
            title = self._recase_title(epub_doc.title, title_heading)
        creators = epub_doc.creators or ([epub_doc.author] if epub_doc.author else [])
        # Gövde başlığı markdown'dır (kaçışlı); dc:creator düz metindir: aynı
        # markdown biçimine kaçışlanır (F1-09).
        authors = ([self._plain_line(a) for a in bylines] if bylines
                   else [self._plain_line(md_escape(a)) for a in creators])
        return title, [a for a in authors if a], consumed

    @staticmethod
    def _plain_line(text: str) -> str:
        """Başlık sayfası satırı: tek satır, markdown vurgusu/bağlantısı yok.

        Kaçışlı düz metin karakterleri (F1-09) korunur: vurgu soyulurken
        '\\*' yıldızı vurgu sanılmaz."""
        text = _MD_LINK_RE.sub(r'\1', text or "")
        text = re.sub(r'(?<!\\)\*{1,3}((?:\\.|[^*\\])+?)(?<!\\)\*{1,3}', r'\1', text)
        return _one_line(text)

    def _front_title_entries(self, epub_doc: EPUBDocument):
        """[(ci, bi, metin, 'title'|'byline')] başlık sayfası başlıkları."""
        entries = []
        for ci, chapter in enumerate(epub_doc.chapters):
            for bi, (kind, level, text) in enumerate(chapter.blocks):
                if kind == 'heading':
                    entries.append((ci, bi, level, text))
        if not entries:
            return []
        is_title, is_byline = self._title_page_matchers(epub_doc)
        first_ci = entries[0][0]
        front = [e for e in entries if e[0] == first_ci]
        start = None
        for i, e in enumerate(front[:self.FRONT_TITLE_SCAN]):
            if is_title(e[3]) or is_byline(e[3]):
                start = i
                break
        if start is None:
            return []
        out = []
        for e in front[start:]:
            if is_title(e[3]):
                out.append((e[0], e[1], e[3], 'title'))
            elif is_byline(e[3]):
                out.append((e[0], e[1], e[3], 'byline'))
            else:
                break
        # Ardışık dizinin dışında kalan, ama başlık sayfası bölgesindeki
        # (okuma sırasında ilk metin bloğundan önceki; Calibre başlık
        # sayfasını birkaç belgeye böler) yazar başlığı da başlık
        # sayfasınındır: 'The Silent Orbit' / 'Volume II - Methods' /
        # 'Jane Doe'.
        last = (out[-1][0], out[-1][1])
        for ci in range(first_ci, len(epub_doc.chapters)):
            blocks = epub_doc.chapters[ci].blocks
            for bi in range(front[start][1] + 1 if ci == first_ci else 0, len(blocks)):
                kind, _level, text = blocks[bi]
                if kind != 'heading':
                    return out
                if (ci, bi) > last and not is_title(text) and is_byline(text):
                    out.append((ci, bi, text, 'byline'))
        return out

    @staticmethod
    def _recase_title(meta_title: str, heading: str) -> str:
        """dc:title'ı gövdedeki başlık sayfası başlığının yazımıyla yaz.

        Sözcükler sırayla (aksan ve büyük/küçük harf duyarsız) eşleştiği
        sürece her sözcük başlıktaki biçimiyle değiştirilir; ayraçlar
        (':', ' – ') dc:title'dan kalır. Başlıktaki karşılık tamamen BÜYÜK
        harfse (biçem tercihi) dc:title olduğu gibi kalır.
        """
        def fold(word: str) -> str:
            word = unicodedata.normalize('NFKD', word)
            return "".join(ch for ch in word if not unicodedata.combining(ch)).casefold()

        meta_words = list(re.finditer(r'\w+', meta_title or ""))
        head_words = [re.sub(r'[*_]', '', m.group()) for m in
                      re.finditer(r'\w+', re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', heading or ""))]
        matched = 0
        for mw, hw in zip(meta_words, head_words):
            if not hw or fold(mw.group()) != fold(hw):
                break
            matched += 1
        if not matched:
            return meta_title
        letters = [ch for hw in head_words[:matched] for ch in hw if ch.isalpha()]
        if letters and all(ch.isupper() for ch in letters) and any(ch.islower() for ch in meta_title):
            return meta_title
        out = []
        last = 0
        for mw, hw in zip(meta_words[:matched], head_words[:matched]):
            out.append(meta_title[last:mw.start()])
            out.append(hw)
            last = mw.end()
        out.append(meta_title[last:])
        return "".join(out)

    @staticmethod
    def _escape_heading_like(text: str) -> str:
        """Düz metin satırı '#' ile başlıyorsa kaçışla.

        Converter satır satır okur ve '# ...' satırını başlık sayar: EPUB'daki
        '<p># # #</p>' sahne ayracı eskiden \\chapter{\\# \\#} oluyordu. Kod
        bloğuna (```) dokunulmaz (içindeki '# yorum' satırları koddur).
        """
        if text.startswith("```"):
            return text
        return re.sub(r'(?m)^(\s*)(#{1,6})(?=\s|$)', r'\1\\\2', text)

    # ------------------------------------------------------------------
    # Başlık düzeyi planı (to_markdown)
    # ------------------------------------------------------------------

    @staticmethod
    def _tokens(text: str) -> List[str]:
        """Karşılaştırma sözcükleri: aksansız, küçük harf, markdown'sız."""
        text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text or "")
        text = unicodedata.normalize('NFKD', text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        return re.findall(r'\w+', text.casefold())

    @staticmethod
    def _main_title(title: str) -> str:
        """dc:title'ın ana kısmı: alt başlık ayracından (':', ' – ', '/',
        '(' ...) önceki bölüm. 'The Silent Orbit – Methods' -> 'The
        Silent Orbit'."""
        return re.split(r'\s*[:/(;]\s*|\s+[-–—]\s+', title or "", maxsplit=1)[0]

    def _title_page_matchers(self, epub_doc: EPUBDocument):
        """(kitap_adı_mı, yazar_adı_mı) yüklemlerini döndür.

        Kitap adı başlığı: sözcüklerinin >= TITLE_TOKEN_COVERAGE'ı dc:title /
        dc:creator sözcüğü VE dc:title'ın ana kısmını >= TITLE_MAIN_RECALL
        ya da tamamını >= TITLE_FULL_RECALL oranında içerir.
        Yazar adı başlığı: >= 2 sözcük, sözcüklerinin >= TITLE_TOKEN_COVERAGE'ı
        dc:creator sözcüğü (baş harf eşleşmesi dahil: 'Giovanni' ~ 'G.'),
        en az bir tam eşleşen >= 3 harfli ad ('Schiaparelli').
        """
        title_set = set(self._tokens(epub_doc.title))
        main_set = set(self._tokens(self._main_title(epub_doc.title))) or title_set
        creators = epub_doc.creators or ([epub_doc.author] if epub_doc.author else [])
        creator_set = set()
        for c in creators:
            creator_set.update(self._tokens(c))
        initials = {c for c in creator_set if len(c) == 1}

        def creator_hit(tok: str) -> bool:
            if tok in creator_set:
                return True
            if len(tok) == 1:
                return any(c.startswith(tok) for c in creator_set)
            return tok[0] in initials

        def is_byline(text: str) -> bool:
            toks = self._tokens(text)
            if len(toks) < 2 or not creator_set:
                return False
            hits = sum(1 for t in toks if creator_hit(t))
            strong = any(len(t) >= 3 and t in creator_set for t in toks)
            return strong and hits / len(toks) >= self.TITLE_TOKEN_COVERAGE

        def is_title(text: str) -> bool:
            toks = self._tokens(text)
            if not toks or not title_set:
                return False
            covered = sum(1 for t in toks if t in title_set or creator_hit(t))
            if covered / len(toks) < self.TITLE_TOKEN_COVERAGE:
                return False
            tset = set(toks)
            main_recall = len(tset & main_set) / len(main_set)
            full_recall = len(tset & title_set) / len(title_set)
            return (main_recall >= self.TITLE_MAIN_RECALL
                    or full_recall >= self.TITLE_FULL_RECALL)

        return is_title, is_byline

    def _plan_headings(self, epub_doc: EPUBDocument) -> Dict[Tuple[int, int], Optional[int]]:
        """Her başlık bloğu için markdown düzeyi: {(bölüm_i, blok_i): düzey}.

        Değer None ise başlık düz paragraf olur (başlık sayfası satırı).

        1. Başlık sayfası: kitabın ilk başlıklı belgesinde, ilk
           FRONT_TITLE_SCAN başlık içinde kitap adı/yazar adıyla eşleşen ilk
           başlık, ondan önceki başlıklar (yayıncı üst bilgisi) ve ardından
           gelen ardışık kitap adı/yazar başlıkları paragraf olur (bölüm
           sayılmaz; kitap adı zaten metadata'dan başlık sayfasına gider).
        2. Yazar adı başlıkları (imza, ikinci makalenin yazar satırı) her
           yerde paragraf olur.
        3. Bölüm düzeyi L: kalan başlıklarda TEKRAR EDEN en üst h düzeyi.
           En üst düzey tek kez geçiyor ve metni açan ilk başlıksa (her şeyi
           kapsayan tek bölüm) L odur. Tek kez geçen düzey (kitap sonundaki
           lisans başlığı gibi) atlanır; en çok LEVEL_RARE_MAX kez geçen ve
           bir alt düzeyi LEVEL_DOMINANCE kat sık olan düzey de atlanır.
           Hiçbir düzey uymazsa en üst düzey. Markdown düzeyi = h - L + 1;
           L'den üstteki tek tük başlıklar '#'e kırpılır. L=1 olan
           kitaplarda (ör. h1 bölümlü İngilizce EPUB'lar) düzeyler değişmez.
        """
        entries = []  # (ci, bi, level, text)
        for ci, chapter in enumerate(epub_doc.chapters):
            for bi, (kind, level, text) in enumerate(chapter.blocks):
                if kind == 'heading':
                    entries.append((ci, bi, level, text))
        if not entries:
            return {}

        is_title, is_byline = self._title_page_matchers(epub_doc)
        demoted = set()

        # 1. Başlık sayfası (ilk başlıklı belge)
        first_ci = entries[0][0]
        front = [e for e in entries if e[0] == first_ci]
        start = None
        for i, e in enumerate(front[:self.FRONT_TITLE_SCAN]):
            if is_title(e[3]) or is_byline(e[3]):
                start = i
                break
        if start is not None:
            for e in front[:start]:
                demoted.add((e[0], e[1]))
            for e in front[start:]:
                if not (is_title(e[3]) or is_byline(e[3])):
                    break
                demoted.add((e[0], e[1]))

        # 2. Yazar adı başlıkları
        for e in entries:
            if is_byline(e[3]):
                demoted.add((e[0], e[1]))

        # 3. Bölüm düzeyi
        structural = [e for e in entries if (e[0], e[1]) not in demoted]
        plan: Dict[Tuple[int, int], Optional[int]] = {k: None for k in demoted}
        if not structural:
            return plan
        counts = Counter(e[2] for e in structural)
        levels = sorted(counts)
        top = levels[0]
        chapter_level = top
        if not (counts[top] == 1 and structural[0][2] == top):
            for i, lv in enumerate(levels):
                if counts[lv] < 2:
                    continue  # tek tük (ör. sondaki lisans başlığı)
                nxt = counts[levels[i + 1]] if i + 1 < len(levels) else 0
                if (counts[lv] <= self.LEVEL_RARE_MAX
                        and nxt >= self.LEVEL_DOMINANCE * counts[lv]):
                    continue  # ör. önsöz + lisans (2) karşısında 28 bölüm
                chapter_level = lv
                break
        for ci, bi, level, _text in structural:
            plan[(ci, bi)] = min(6, max(1, level - chapter_level + 1))
        return plan

    def rebuild_epub(self, epub_doc: EPUBDocument, translations: Dict[int, str],
                     output_path: str, *, notes: Optional[Dict[int, str]] = None,
                     language: Optional[str] = None, title: Optional[str] = None,
                     stats: Optional[Dict] = None) -> str:
        """
        Çevirilmiş içerikle yeni EPUB oluştur - orijinal yapıyı korur

        Args:
            epub_doc: Orijinal EPUB dokümanı
            translations: {segment_index: translated_text} sözlüğü
            output_path: Çıktı dosya yolu
            notes: {numara: metin} yeni notlar (EPUB_NOTE_REF yer tutucusu
                bulunan belgenin sonuna <aside epub:type="footnote">)
            language: çıktının dili (html lang, dc:language; R7). None ise
                özgün dil bilgisine dokunulmaz.
            title: çıktının kitap adı (dc:title, İçindekiler başlığı)
            stats: verilirse doldurulur: "written" = öğeye yazılan
                segmentler (S7 kapsamı), "notes" = yazılan yeni not sayısı

        Returns:
            Oluşturulan dosya yolu

        NOT: Bu fonksiyon orijinal EPUB'ın tüm özelliklerini korur:
        - Cover image
        - Metadata (ISBN, publisher, vb.)
        - CSS stilleri (belge başlığındaki <link>/<meta>/<title>; E2
          2026-09-28: ebooklib belgeyi kendi şablonuyla yeniden yazdığı için
          head boşalıyor, CSS bağlantıları düşüyordu)
        - Dosya yapısı
        - Fontlar ve görseller
        - İçindekiler (nav toc + page-list; özgün nav'dan, etiketler çeviriden)
        """
        from ebooklib import epub
        from bs4 import BeautifulSoup

        warnings.filterwarnings("ignore")

        # Orijinal kitabı oku - tüm içerikler korunur (cover, CSS, metadata, vb.)
        book = epub.read_epub(epub_doc.path)
        # F1-14: parse() ile aynı kodlama düzeltmesi (segmentler aynı kalsın).
        self._normalize_encodings(book)
        chapters_by_name = {ch.name: ch for ch in epub_doc.chapters}
        text_marks = self._text_note_marks(epub_doc)
        notes = notes or {}
        written: set = set()
        docs: Dict[str, Tuple] = {}    # belge adı -> (soup, çeviri öncesi başlıklar, body id)
        notes_written = 0

        # Segment index -> çeviri eşleştirmesi için orijinal metinleri tekrar
        # işle. extract_segments ile BİREBİR aynı mantık: aynı belge sırası
        # (spine), aynı bloklar (_content_blocks), aynı başlık metni
        # (_heading_markdown), aynı paragraf bölme ve tekrar denetimi.
        # Eskiden burada manifest sırası ve get_text(" ") tabanlı ayrı bir
        # normalize kullanılıyordu; <br/>'li ya da iç etiketli öğelerde
        # indeksler kayıyordu.
        segment_index = 0
        chapter_order = 0

        # (gezinme belgesi uyarısı parse() sırasında bir kez verildi)
        for item in self._ordered_documents(book, warn=False):
            html_content = item.get_content().decode('utf-8')
            soup = BeautifulSoup(html_content, 'lxml')
            body = soup.body if soup.body else soup
            self._doc_ctx = self._doc_name(item)

            # parse() ile aynı ölçüt: içerik bloğu yoksa bölüm yok
            prefixes: Dict[int, str] = {}
            blocks = list(self._content_blocks(body, prefixes))
            if not blocks:
                continue

            ctx = _RebuildContext(self, chapters_by_name.get(self._doc_name(item)),
                                  text_marks, notes, soup)
            before = {id(h): _label_text(h) for h in body.find_all(list(_HEADING_TAGS))}
            # get_content() ebooklib şablonudur (head boş): özgün head ham
            # içerikten okunur.
            src_head = _source_head(item)

            # Chapter-local seen_texts (extract_segments ile aynı). Değer:
            # metnin segment indeksi (E2, 2026-09-25): bölüm içinde metni
            # tekrar eden öğe segment almaz, ilk geçişin çevirisini alır.
            seen_texts: Dict[str, int] = {}

            # Başlık (gövdedeki ilk h1/h2)
            title_tag = self._find_title_tag(body)
            chapter_title = self._heading_markdown(title_tag) if title_tag is not None else ""

            def emit_title() -> None:
                nonlocal segment_index
                norm_title = self._normalize_for_dedup(chapter_title)
                if norm_title not in seen_texts:
                    seen_texts[norm_title] = segment_index
                    # Bu segment'e çeviri var mı?
                    if segment_index in translations:
                        # R9: başlıktaki çapalar (<a id="I"/>) korunur;
                        # eskiden title_tag.clear() siliyordu.
                        self._apply_translation(
                            title_tag, translations[segment_index], soup=soup,
                            prefix=prefixes.get(id(title_tag), ""), ctx=ctx)
                        written.add(segment_index)
                    segment_index += 1

            # Başlık segmenti extract_segments ile aynı yerde: başlık bloğunun
            # yerinde (önünde metin yoksa en başta).
            title_pi = (self._title_block_paragraph(
                [(k, lv, t) for _e, k, lv, t in blocks], chapter_title)
                if chapter_title else None)
            if chapter_title and not title_pi:
                emit_title()
            para_pos = 0

            # İçerik bloklarını işle
            for element, kind, level, text in blocks:
                if title_pi and para_pos == title_pi:
                    emit_title()
                para_pos += len(self._split_paragraphs(self._block_markdown(kind, level, text)))
                paras = self._split_paragraphs(self._block_markdown(kind, level, text))
                # Her paragrafın çevirisinin alınacağı segment: kendi yeni
                # segmenti ya da (bölüm içi tekrar) ilk geçişinki. Eskiden
                # tekrar eden öğe kaynak dilde kalıyordu (E2).
                sources, dups = [], []
                for para in paras:
                    norm_text = self._normalize_for_dedup(para)
                    if norm_text in seen_texts:
                        sources.append(seen_texts[norm_text])
                        dups.append(True)
                        continue
                    seen_texts[norm_text] = segment_index
                    sources.append(segment_index)
                    dups.append(False)
                    segment_index += 1

                if not sources or (element is title_tag and all(dups)):
                    # Başlık öğesinin çevirisi yukarıda yazıldı.
                    continue
                pieces = [translations.get(i) for i in sources]
                # "" (çevirisi önceki segmentinkiyle birleşmiş) yalnız kendi
                # segmentinin öğesini boşaltır; tekrar eden öğe boşaltılmaz.
                if all(t is None for t in pieces) or any(
                        d and t == "" for d, t in zip(dups, pieces)):
                    continue
                if len(paras) == 1:
                    self._apply_translation(element, pieces[0], prefix=prefixes.get(id(element), ""),
                                            ctx=ctx)
                else:
                    # <br/> ile birden çok paragrafa bölünen öğe: çeviriler
                    # <br/> ile yeniden birleştirilir. Çevirisi olmayan parça
                    # (çeviride aynen kalan ya da hizalanamayan) özgün
                    # metniyle kalır; eskiden tek eksik parça bütün öğeyi
                    # kaynak dilde bırakıyordu (dil_it, 2026-09-28).
                    filled = [t if t is not None else self._html_safe_text(para)
                              for t, para in zip(pieces, paras)]
                    self._apply_translation(element, filled, soup=soup,
                                            prefix=prefixes.get(id(element), ""), ctx=ctx)
                written.update(i for i, d, t in zip(sources, dups, pieces)
                               if not d and t is not None)

            chapter_order += 1
            notes_written += ctx.append_notes(body)
            # F1-01: gevşek metin sarmalayıcıları açılır (çıktı yapısı özgün).
            for wrapper in body.find_all(_KC_BLOCK_TAG):
                wrapper.unwrap()

            # Güncellenmiş HTML'i kaydet - orijinal DOCTYPE ve yapıyı koru
            # lxml bazen DOCTYPE'ı değiştirir, bu yüzden sadece body içeriğini güncelle
            new_html = str(soup)
            item.set_content(new_html.encode('utf-8'))
            self._restore_head(item, src_head, language, self._doc_title(
                src_head["title"], before, body, epub_doc.title, title))
            # F2-08: head <style> blokları ve SVG/MathML büyük/küçük harfi
            _keep_xhtml_details(item, src_head.get("styles") or [])
            # body kimliği ebooklib şablonunda yok (Calibre içindekiler
            # çapası): hedef denetimi için ayrıca tutulur.
            docs[item.get_name()] = (soup, before, src_head["body"].get("id"))

        # Metinsiz belgeler (tam sayfa görsel, kapak sayfası) çeviri almaz;
        # ebooklib onları da şablonla (lxml.html) yeniden yazıyordu: head
        # (CSS) ve body öznitelikleri düşüyor, SVG'de 'viewBox' küçük harfe
        # iniyordu. Özgün baytları yazılır, yalnız dil özniteliği değişir.
        for item in book.get_items():
            if (isinstance(item, epub.EpubHtml)
                    and not isinstance(item, (epub.EpubNav, epub.EpubCoverHtml))
                    and item.get_name() not in docs):
                _keep_raw_document(item, language)

        # R7: dil ve kitap adı metaverisi
        if language:
            book.language = language
            book.metadata.setdefault(_DC_NAMESPACE, {})['language'] = [(language, {})]
        if title:
            book.title = title
            dc = book.metadata.setdefault(_DC_NAMESPACE, {})
            old = dc.get('title') or []
            dc['title'] = [(title, old[0][1] if old else {})]

        # İçindekiler: özgün nav'dan (etiketler çeviriden, kırık girişler
        # atılır, page-list korunur); NCX aynı ağaçtan.
        self._rebuild_navigation(book, docs, language or book.language,
                                 title or book.title)

        # EPUB3 nav'dan okunan içindekiler bağlantılarında uid yok (None);
        # ebooklib NCX yazarken navPoint id'si için uid ister ve TypeError
        # ile düşer (Gutenberg EPUB'larının hepsi). Eksik uid'ler doldurulur.
        self._fill_missing_toc_uids(book.toc)

        # Yeni dosyayı yaz - tüm orijinal öğeler (cover, CSS, metadata) korunur
        epub.write_epub(output_path, book)
        if stats is not None:
            stats["written"] = written
            stats["notes"] = notes_written
        print(f"[EPUB Rebuild] {segment_index} segment işlendi, {len(written)} çeviri uygulandı")
        return output_path

    # --- rebuild_epub yardımcıları (E2, 2026-09-28) ---------------------------

    @staticmethod
    def _text_note_marks(epub_doc: EPUBDocument) -> Dict[str, str]:
        """Gutenberg düz metin dipnot işaretleri: {anahtar: '[*]' / '[3]'}."""
        marks: Dict[str, str] = {}
        for ch in epub_doc.chapters:
            for bi, refs in ch.text_note_refs.items():
                if bi >= len(ch.blocks):
                    continue
                text = ch.blocks[bi][2]
                for start, length, key in refs:
                    # Blok metni kaçışlıdır ('[\*]'); EPUB'a özgün '[*]'.
                    marks.setdefault(key, md_unescape(text[start:start + length]))
        return marks

    @staticmethod
    def _doc_title(head_title: str, before: Dict[int, str], body,
                   book_title: str, new_book_title: Optional[str]) -> str:
        """Belgenin <title>'ı: özgün başlık bir bölüm başlığının metniyse o
        başlığın çevirisi, kitabın adıysa yeni kitap adı; değilse özgünü."""
        norm = " ".join(head_title.split())
        if not norm:
            return head_title
        fold = norm.casefold()
        for h in body.find_all(list(_HEADING_TAGS)):
            old = before.get(id(h))
            if old is not None and old.casefold() == fold:
                new = _label_text(h)
                return new or head_title
        if new_book_title and " ".join((book_title or "").split()).casefold() == fold:
            return new_book_title
        return head_title

    @staticmethod
    def _restore_head(item, src_head: Dict, language: Optional[str],
                      doc_title: str) -> None:
        """ebooklib belgeyi kendi şablonuyla yazar: <head>'i item.links/
        metas/title'dan, dili item.lang'dan kurar. Özgün head'deki bağlantı
        (CSS) ve meta öğeleri, başlık ve dil buraya taşınır."""
        if hasattr(item, 'links'):
            item.links = src_head["links"]
        if hasattr(item, 'metas'):
            item.metas = src_head["metas"]
        if hasattr(item, 'title') and doc_title:
            item.title = doc_title
        if language and hasattr(item, 'lang'):
            item.lang = language
        _keep_body_attrs(item, src_head.get("body") or {})

    def _rebuild_navigation(self, book, docs: Dict[str, Tuple], language: str,
                            title: str) -> None:
        """İçindekiler (R7).

        ebooklib nav'ı book.toc'tan yeniden üretir: bozuk (kesilmiş) özgün
        nav'da girişler düşüyor (dil_es 5 -> 1), page-list hiç yazılmıyor,
        etiketler özgün dilde kalıyordu. Yeni nav özgün nav belgesinden
        (hoşgörülü ayrıştırma) kurulur: hedefi kitapta olmayan (kırık) giriş
        atılır, etiket hedef başlığın çevirisidir (çevrilmediyse özgün),
        page-list hedefi duran sayfa çapalarıyla korunur. NCX aynı ağaçtan.
        """
        from ebooklib import epub

        nav_item = next((it for it in book.get_items()
                         if isinstance(it, epub.EpubNav)), None)
        entries = pages = landmarks = None
        nav_links: List[Dict] = []
        if nav_item is not None:
            entries, pages, landmarks, nav_links = self._parse_nav_document(nav_item)
        if entries is None:
            entries = self._toc_entries(book.toc)
        pages = pages or []
        landmarks = landmarks or []

        ids_cache: Dict[str, Optional[set]] = {}

        def alive(href: Optional[str]) -> bool:
            if not href:
                return False
            path, _, frag = href.partition('#')
            if path not in ids_cache:
                if path in docs:
                    ids_cache[path] = ({t.get('id') for t in docs[path][0].find_all(id=True)}
                                       | {docs[path][2]})
                else:
                    doc = book.get_item_with_href(path)
                    if doc is None:
                        ids_cache[path] = None
                    else:
                        ids_cache[path] = _content_ids(doc.get_content())
            ids = ids_cache[path]
            return ids is not None and (not frag or frag in ids)

        entries = self._translate_nav_entries(entries, docs, alive)
        if not entries:
            entries = self._fallback_toc(docs)
        pages = [(h, _page_label(lbl)) for h, lbl in pages if alive(h)]
        landmarks = [(h, lbl, kind) for h, lbl, kind in landmarks if alive(h)]
        book.toc = _toc_objects(entries)
        if nav_item is None:
            return
        content = _nav_xhtml(entries, pages, landmarks, language, title,
                             posixpath.dirname(nav_item.get_name()), nav_links)
        new = epub.EpubItem(uid=nav_item.get_id(), file_name=nav_item.get_name(),
                            media_type=nav_item.media_type or "application/xhtml+xml",
                            content=content)
        new.properties = ['nav']
        book.items[book.items.index(nav_item)] = new

    def _parse_nav_document(self, nav_item):
        """Özgün nav belgesi -> (toc girişleri, sayfalar, landmarks, head
        bağlantıları). Giriş: {'href', 'label', 'children'}; adres OPF
        klasörüne göredir. toc yoksa girişler None."""
        from bs4 import BeautifulSoup

        # Ham içerik (get_content() ebooklib şablonudur: head boş).
        raw = getattr(nav_item, 'content', b'') or b''
        try:
            text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
            soup = BeautifulSoup(text, 'lxml')
        except Exception:
            return None, [], [], []
        base = posixpath.dirname(nav_item.get_name())
        links = _source_head(nav_item)["links"]
        toc_nav, pages, landmarks = None, [], []
        for nav in soup.find_all('nav'):
            kinds = (nav.get('epub:type') or '').split()
            page_like = ('page-list' in kinds
                         or nav.find('ol', class_='pagelist') is not None
                         or nav.find('ol', id='pages') is not None)
            if 'toc' in kinds and toc_nav is None:
                toc_nav = nav
            elif page_like:
                for a in nav.find_all('a', href=True):
                    pages.append((_resolve_nav_href(base, a['href']),
                                  " ".join(a.get_text(" ").split())))
            elif 'landmarks' in kinds:
                for a in nav.find_all('a', href=True):
                    landmarks.append((_resolve_nav_href(base, a['href']),
                                      " ".join(a.get_text(" ").split()),
                                      a.get('epub:type') or ''))
        if toc_nav is None:
            return None, pages, landmarks, links
        flat = []
        for lab in toc_nav.find_all(['a', 'span']):
            li = lab.parent
            if li is None or li.name != 'li':
                continue
            depth = sum(1 for p in lab.parents if p.name == 'li'
                        and toc_nav in p.parents)
            href = lab.get('href') if lab.name == 'a' else None
            flat.append((depth, _resolve_nav_href(base, href) if href else None,
                         " ".join(lab.get_text(" ").split())))
        return _nest_entries(flat), pages, landmarks, links

    @staticmethod
    def _toc_entries(toc) -> List[Dict]:
        """ebooklib book.toc -> giriş ağacı (nav belgesi yoksa)."""
        from ebooklib import epub

        out = []
        for entry in toc or []:
            if isinstance(entry, (tuple, list)) and entry:
                head, children = entry[0], (entry[1] if len(entry) > 1 else [])
                href = getattr(head, 'href', None) or None
                if isinstance(head, epub.EpubHtml):
                    href = head.file_name
                out.append({'href': href, 'label': getattr(head, 'title', '') or '',
                            'children': EPUBParser._toc_entries(children)})
            elif isinstance(entry, epub.EpubHtml):
                out.append({'href': entry.file_name, 'label': entry.title or '',
                            'children': []})
            else:
                out.append({'href': getattr(entry, 'href', None) or None,
                            'label': getattr(entry, 'title', '') or '',
                            'children': []})
        return out

    def _translate_nav_entries(self, entries: List[Dict], docs, alive) -> List[Dict]:
        """Kırık girişleri at (hedefi kalan alt girişi olan başlık etiketi
        olarak kalır), etiketi hedef başlığın çevirisi yap."""
        out = []
        for e in entries:
            children = self._translate_nav_entries(e.get('children') or [], docs, alive)
            href = e.get('href')
            if href and alive(href):
                out.append({'href': href, 'label': self._nav_label(href, e['label'], docs),
                            'children': children})
            elif children:
                out.append({'href': None, 'label': e['label'], 'children': children})
        return out

    @staticmethod
    def _nav_label(href: str, label: str, docs) -> str:
        """Hedefin başlığı çevrildiyse onun metni; değilse özgün etiket."""
        path, _, frag = href.partition('#')
        if path not in docs:
            return label
        soup, before, body_id = docs[path]
        body = soup.body if soup.body else soup
        heading = None
        if frag and frag == body_id:
            frag = ""       # belgenin kendisi (body kimliği)
        if frag:
            target = body.find(id=frag)
            if target is None:
                return label
            if target.name in _HEADING_TAGS:
                heading = target
            else:
                heading = target.find_parent(list(_HEADING_TAGS))
                if heading is None and target.get_text().strip():
                    first = target.find(list(_HEADING_TAGS))
                    if first is not None and not _text_before(target, first):
                        heading = first
                elif heading is None:
                    heading = _next_heading_without_text(target)
        else:
            heading = body.find(list(_HEADING_TAGS))
        if heading is None:
            return label
        old = before.get(id(heading))
        new = _label_text(heading)
        if old is not None and new and new != old:
            return new
        return label

    @staticmethod
    def _fallback_toc(docs) -> List[Dict]:
        """Bütün girişler kırıksa: her belgenin ilk başlığı (EPUB3 nav boş
        toc kabul etmez)."""
        out = []
        for name, (soup, _before, _body_id) in docs.items():
            body = soup.body if soup.body else soup
            h = body.find(list(_HEADING_TAGS))
            label = _label_text(h) if h is not None else ""
            if label:
                href = f"{name}#{h['id']}" if h.get('id') else name
                out.append({'href': href, 'label': label, 'children': []})
        return out


# ---------------------------------------------------------------------------
# MathML -> LaTeX (F1-07, 2026-09-29)
# ---------------------------------------------------------------------------
# EPUB'daki MathML eskiden get_text ile düzleşiyordu: 'E=mc²' -> 'E=mc2',
# 'a/b' -> 'ab', '√2' -> '2' (anlam sessizce değişiyordu). Sunum MathML'i
# satır içi '$…$' TeX'ine çevrilir; TeX notu (<annotation
# encoding="application/x-tex">) varsa o kullanılır. Harfler/semboller
# Unicode kalır (şablonun unicode-math + STIX Two Math'ı dizer).

_MATHML_TEX_ENCODINGS = frozenset({"application/x-tex", "application/x-latex",
                                   "tex", "latex", "text/x-tex", "text/latex"})
# Görünmez işleçler (işlev uygulama, görünmez çarpı/ayraç/artı): atılır.
_MATHML_INVISIBLE = dict.fromkeys(map(ord, "⁡⁢⁣⁤"), None)
_MATH_SPECIALS = {"#": r"\#", "$": r"\$", "%": r"\%", "&": r"\&", "_": r"\_",
                  "{": r"\{", "}": r"\}", "~": r"\sim ", "^": r"\hat{}",
                  "\\": r"\backslash "}
_TEXT_SPECIALS = {"#": r"\#", "$": r"\$", "%": r"\%", "&": r"\&", "_": r"\_",
                  "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
                  "^": r"\textasciicircum{}", "\\": r"\textbackslash{}"}
_MATH_FUNCTIONS = frozenset({
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
    "sinh", "cosh", "tanh", "coth", "log", "ln", "lg", "exp", "lim", "liminf",
    "limsup", "max", "min", "sup", "inf", "det", "dim", "ker", "deg", "arg",
    "gcd", "hom", "Pr"})
_MATH_VARIANTS = {"normal": r"\mathrm", "bold": r"\mathbf",
                  "double-struck": r"\mathbb", "script": r"\mathcal",
                  "fraktur": r"\mathfrak", "sans-serif": r"\mathsf",
                  "monospace": r"\mathtt", "bold-italic": r"\boldsymbol"}
# mover/munder vurgu işaretleri -> LaTeX komutu
_MATH_ACCENTS = {"^": r"\hat", "ˆ": r"\hat", "̂": r"\hat",
                 "¯": r"\overline", "‾": r"\overline",
                 "̄": r"\bar", "―": r"\overline", "_": r"\underline",
                 "→": r"\vec", "⃗": r"\vec", "~": r"\tilde",
                 "˜": r"\tilde", "̃": r"\tilde", ".": r"\dot",
                 "˙": r"\dot", "̇": r"\dot", "¨": r"\ddot",
                 "̈": r"\ddot", "ˇ": r"\check", "˘": r"\breve",
                 "´": r"\acute", "`": r"\grave", "⏞": r"\overbrace",
                 "⏟": r"\underbrace"}
# Alt/üst sınırı olan büyük işleçler: munder/munderover alt/üst simge olur.
_MATH_LARGE_OPS = frozenset("∑∏∐∫∬∭∮⋃⋂⋁⋀⨁⨂⨀") | {"lim", "max", "min", "sup",
                                                   "inf", "limsup", "liminf"}


def _local_name(tag) -> str:
    """Öğenin öneksiz adı ('m:math' -> 'math')."""
    return (getattr(tag, "name", None) or "").split(":")[-1].lower()


def _math_escape(text: str) -> str:
    text = text.translate(_MATHML_INVISIBLE)
    return "".join(_MATH_SPECIALS.get(ch, ch) for ch in text)


def _text_escape(text: str) -> str:
    return "".join(_TEXT_SPECIALS.get(ch, ch) for ch in text)


class _MathMLConverter:
    """Sunum MathML'i -> LaTeX. ``unsupported``: tanınmayan öğe adları
    (içerikleri yine çevrilir; uyarı çağıranın işidir)."""

    def __init__(self):
        self.unsupported: set = set()

    def children(self, tag) -> list:
        from bs4 import Tag
        return [c for c in tag.children if isinstance(c, Tag)]

    def row(self, tag) -> str:
        from bs4 import NavigableString, Tag
        from bs4.element import PreformattedString
        out = []
        for c in tag.children:
            if isinstance(c, Tag):
                out.append(self.node(c))
            elif isinstance(c, NavigableString) and not isinstance(c, PreformattedString):
                if str(c).strip():
                    out.append(_math_escape(" ".join(str(c).split())))
        return " ".join(p for p in out if p)

    def arg(self, tag) -> str:
        return "{" + (self.node(tag) if tag is not None else "") + "}"

    def token(self, tag) -> str:
        return " ".join(tag.get_text().translate(_MATHML_INVISIBLE).split())

    def node(self, tag) -> str:
        name = _local_name(tag)
        kids = self.children(tag)
        if name in ("math", "mrow", "mstyle", "mpadded", "merror", "maction",
                    "semantics"):
            if name == "semantics":
                for ann in kids:
                    if (_local_name(ann) == "annotation"
                            and (ann.get("encoding") or "").strip().lower()
                            in _MATHML_TEX_ENCODINGS
                            and ann.get_text().strip()):
                        return " ".join(ann.get_text().split())
                kids = [k for k in kids if _local_name(k) not in
                        ("annotation", "annotation-xml")]
                return " ".join(self.node(k) for k in kids)
            if name == "maction" and kids:
                return self.node(kids[0])
            return self.row(tag)
        if name in ("annotation", "annotation-xml", "none", "mprescripts"):
            return ""
        if name == "mi":
            text = self.token(tag)
            if not text:
                return ""
            variant = (tag.get("mathvariant") or "").strip().lower()
            if len(text) > 1 and text in _MATH_FUNCTIONS and not variant:
                return "\\" + text
            body = _math_escape(text)
            if variant in _MATH_VARIANTS:
                return f"{_MATH_VARIANTS[variant]}{{{body}}}"
            if len(text) > 1 and text.isalpha():
                return rf"\mathrm{{{body}}}"
            return body
        if name == "mn":
            return _math_escape(self.token(tag))
        if name == "mo":
            text = self.token(tag)
            if text in _MATH_FUNCTIONS:
                return "\\" + text
            return _math_escape(text)
        if name in ("mtext", "ms"):
            text = " ".join(tag.get_text().split())
            if name == "ms":
                text = f"“{text}”"
            return rf"\text{{{_text_escape(text)}}}" if text else r"\ "
        if name == "mspace":
            return r"\ "
        if name == "mglyph":
            alt = (tag.get("alt") or "").strip()
            return rf"\text{{{_text_escape(alt)}}}" if alt else ""
        if name == "mphantom":
            return rf"\phantom{{{self.row(tag)}}}"
        if name == "msup" and len(kids) >= 2:
            return f"{self.arg(kids[0])}^{self.arg(kids[1])}"
        if name == "msub" and len(kids) >= 2:
            return f"{self.arg(kids[0])}_{self.arg(kids[1])}"
        if name == "msubsup" and len(kids) >= 3:
            return f"{self.arg(kids[0])}_{self.arg(kids[1])}^{self.arg(kids[2])}"
        if name == "mfrac" and len(kids) >= 2:
            num, den = self.node(kids[0]), self.node(kids[1])
            if (tag.get("bevelled") or "").lower() == "true":
                return f"{{{num}}}/{{{den}}}"
            if (tag.get("linethickness") or "").strip() in ("0", "0pt", "0px", "0em"):
                return rf"\genfrac{{}}{{}}{{0pt}}{{}}{{{num}}}{{{den}}}"
            return rf"\frac{{{num}}}{{{den}}}"
        if name == "msqrt":
            return rf"\sqrt{{{self.row(tag)}}}"
        if name == "mroot" and len(kids) >= 2:
            return rf"\sqrt[{self.node(kids[1])}]{{{self.node(kids[0])}}}"
        if name in ("mover", "munder", "munderover") and len(kids) >= 2:
            base = self.node(kids[0])
            base_text = self.token(kids[0])
            if name == "munderover" and len(kids) >= 3:
                under, over = self.node(kids[1]), self.node(kids[2])
                if base_text in _MATH_LARGE_OPS or base.startswith("\\"):
                    return f"{base}_{{{under}}}^{{{over}}}"
                return rf"\underset{{{under}}}{{\overset{{{over}}}{{{base}}}}}"
            mark = self.token(kids[1])
            if mark in _MATH_ACCENTS and len(kids) == 2:
                cmd = _MATH_ACCENTS[mark]
                if name == "munder" and cmd == r"\overline":
                    cmd = r"\underline"
                return f"{cmd}{{{base}}}"
            other = self.node(kids[1])
            if base_text in _MATH_LARGE_OPS:
                return f"{base}{'^' if name == 'mover' else '_'}{{{other}}}"
            if name == "mover":
                return rf"\overset{{{other}}}{{{base}}}"
            return rf"\underset{{{other}}}{{{base}}}"
        if name == "mfenced":
            opener = tag.get("open", "(")
            closer = tag.get("close", ")")
            seps = (tag.get("separators", ",") or "").split() or [""]
            seps = list("".join(seps)) or [""]
            inner = []
            for i, k in enumerate(kids):
                if i:
                    inner.append(_math_escape(seps[min(i - 1, len(seps) - 1)]))
                inner.append(self.node(k))
            left = r"\left" + (_math_escape(opener) or ".")
            right = r"\right" + (_math_escape(closer) or ".")
            return f"{left} {' '.join(inner)} {right}"
        if name == "menclose":
            notation = (tag.get("notation") or "").split()
            body = self.row(tag)
            if "radical" in notation:
                return rf"\sqrt{{{body}}}"
            if "box" in notation or "roundedbox" in notation:
                return rf"\boxed{{{body}}}"
            return body
        if name == "mtable":
            rows = []
            for tr in kids:
                cells = [c for c in self.children(tr) if _local_name(c) == "mtd"]
                if _local_name(tr) == "mlabeledtr" and cells:
                    cells = cells[1:]
                rows.append(" & ".join(self.row(c) for c in cells))
            return r"\begin{matrix} " + r" \\ ".join(rows) + r" \end{matrix}"
        if name == "mmultiscripts" and kids:
            base = self.arg(kids[0])
            post, pre, cur = [], [], None
            cur = post
            for k in kids[1:]:
                if _local_name(k) == "mprescripts":
                    cur = pre
                    continue
                cur.append(self.node(k))
            def scripts(seq):
                return "".join(f"_{{{seq[i]}}}^{{{seq[i + 1] if i + 1 < len(seq) else ''}}}"
                               for i in range(0, len(seq), 2))
            return (f"{{}}{scripts(pre)}" if pre else "") + base + scripts(post)
        # Tanınmayan öğe (içerik MathML'i: apply/ci/cn, mlongdiv…): içerik
        # yine yazılır, çağıran uyarır.
        if name in ("ci", "cn"):
            return _math_escape(self.token(tag))
        self.unsupported.add(name or "?")
        return self.row(tag)


def mathml_to_latex(math_tag) -> Tuple[str, bool, set]:
    """(TeX, display, desteklenmeyen öğeler). TeX tek satır; boşsa ''.

    display: <math display="block"> ya da mode="display"."""
    display = ((math_tag.get("display") or "").lower() == "block"
               or (math_tag.get("mode") or "").lower() == "display")
    conv = _MathMLConverter()
    tex = " ".join(conv.node(math_tag).split())
    return tex, display, conv.unsupported


_DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"


def _new_tag(name: str, attrs: Optional[Dict[str, str]] = None):
    """Yeni öğe (herhangi bir ağaca taşınabilir)."""
    from bs4 import BeautifulSoup

    return BeautifulSoup("", "html.parser").new_tag(name, attrs=attrs or {})


def _attr_dict(tag) -> Dict[str, str]:
    return {k: (" ".join(v) if isinstance(v, list) else str(v))
            for k, v in tag.attrs.items()}


def _source_head(item) -> Dict:
    """Belgenin ÖZGÜN head'i (ham içerikten): {"title", "links", "metas"}.
    charset/http-equiv metaları atılır (XHTML bildirimi yeterli)."""
    from bs4 import BeautifulSoup

    out = {"title": "", "links": [], "metas": [], "body": {}, "styles": []}
    raw = getattr(item, 'content', b'') or b''
    try:
        text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
        soup = BeautifulSoup(text, 'lxml')
    except Exception:
        return out
    # F2-08 (2026-09-29): head içi <style> blokları ham metinden (CSS
    # olduğu gibi; ebooklib şablonu yalnız link/meta/title yazar).
    head_end = re.search(r"<body\b", text, re.I)
    head_text = text[:head_end.start()] if head_end else text
    out["styles"] = re.findall(r"<style\b[^>]*>.*?</style\s*>", head_text, re.S | re.I)
    if soup.body is not None:
        out["body"] = _attr_dict(soup.body)
    head = soup.head
    if head is None:
        return out
    if head.title is not None:
        out["title"] = " ".join(head.title.get_text(" ").split())
    out["links"] = [_attr_dict(link) for link in head.find_all('link')]
    for meta in head.find_all('meta'):
        attrs = _attr_dict(meta)
        if 'charset' not in attrs and 'http-equiv' not in attrs:
            out["metas"].append(attrs)
    return out


_BODY_OPEN_RE = re.compile(rb'<body(\s[^>]*)?>')
_HTML_OPEN_RE = re.compile(rb'<html\b[^>]*>')
_LANG_ATTR_RE = re.compile(rb'\s(?:xml:)?lang\s*=\s*(?:"[^"]*"|\'[^\']*\')')


def _keep_raw_document(item, language: Optional[str]) -> None:
    """Belgeyi özgün baytlarıyla yaz (get_content şablon üretmesin); dil
    verilmişse <html>'in lang/xml:lang öznitelikleri o dil olur."""
    raw = getattr(item, 'content', None)
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return
    data = bytes(raw)
    if language:
        lang = language.encode('ascii', 'ignore')

        def fix(m):
            tag = _LANG_ATTR_RE.sub(b'', m.group(0))
            return tag[:-1].rstrip(b'/') + b' lang="' + lang + b'" xml:lang="' + lang + b'">'

        data = _HTML_OPEN_RE.sub(fix, data, count=1)

    def get_content(default=None, _data=data):
        return _data

    item.get_content = get_content


# F2-08 (2026-09-29): lxml.html (ebooklib şablonu ve BeautifulSoup) SVG/MathML
# öğe ve öznitelik adlarını küçük harfe indirir; XHTML büyük/küçük harfe
# duyarlıdır ('viewbox' ölçeklemeyi, 'lineargradient' degradeyi bozar).
# Metinli belgenin çıktısında HTML Standardı'nın "adjust SVG attributes /
# tag names" tablolarıyla (ve MathML definitionURL) geri çevrilir.
_SVG_TAG_NAMES = (
    "altGlyph altGlyphDef altGlyphItem animateColor animateMotion "
    "animateTransform clipPath feBlend feColorMatrix feComponentTransfer "
    "feComposite feConvolveMatrix feDiffuseLighting feDisplacementMap "
    "feDistantLight feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR "
    "feGaussianBlur feImage feMerge feMergeNode feMorphology feOffset "
    "fePointLight feSpecularLighting feSpotLight feTile feTurbulence "
    "foreignObject glyphRef linearGradient radialGradient textPath").split()
_SVG_ATTR_NAMES = (
    "attributeName attributeType baseFrequency baseProfile calcMode "
    "clipPathUnits diffuseConstant edgeMode filterUnits glyphRef "
    "gradientTransform gradientUnits kernelMatrix kernelUnitLength keyPoints "
    "keySplines keyTimes lengthAdjust limitingConeAngle markerHeight "
    "markerUnits markerWidth maskContentUnits maskUnits numOctaves pathLength "
    "patternContentUnits patternTransform patternUnits pointsAtX pointsAtY "
    "pointsAtZ preserveAlpha preserveAspectRatio primitiveUnits refX refY "
    "repeatCount repeatDur requiredExtensions requiredFeatures "
    "specularConstant specularExponent spreadMethod startOffset stdDeviation "
    "stitchTiles surfaceScale systemLanguage tableValues targetX targetY "
    "textLength viewBox viewTarget xChannelSelector yChannelSelector "
    "zoomAndPan definitionURL").split()
_CASE_TAGS = {n.lower(): n for n in _SVG_TAG_NAMES}
_CASE_ATTRS = {n.lower(): n for n in _SVG_ATTR_NAMES}
_XML_TAG_RE = re.compile(r"<(/?)([A-Za-z][\w:.-]*)((?:\s[^<>]*?)?)(/?)>")
_XML_ATTR_NAME_RE = re.compile(r"(\s)([A-Za-z_][\w:.-]*)(\s*=)")


def _fix_svg_case(xhtml: str) -> str:
    """Çıktı XHTML'inde SVG/MathML adlarının büyük/küçük harfini düzelt."""
    def attr(m: "re.Match") -> str:
        return m.group(1) + _CASE_ATTRS.get(m.group(2), m.group(2)) + m.group(3)

    def tag(m: "re.Match") -> str:
        name = _CASE_TAGS.get(m.group(2), m.group(2))
        attrs = m.group(3)
        if attrs:
            attrs = _XML_ATTR_NAME_RE.sub(attr, attrs)
        return f"<{m.group(1)}{name}{attrs}{m.group(4)}>"

    return _XML_TAG_RE.sub(tag, xhtml)


def _keep_xhtml_details(item, styles: List[str]) -> None:
    """Metinli belgenin çıktısı (F2-08): özgün head <style> blokları
    geri konur, SVG/MathML adlarının büyük/küçük harfi düzeltilir."""
    if not hasattr(item, 'get_content'):
        return
    base = item.get_content
    extra = "".join(styles).encode('utf-8')

    def get_content(default=None, _base=base, _extra=extra):
        data = _base()
        if not isinstance(data, (bytes, bytearray)) or not data:
            return data
        text = bytes(data).decode('utf-8', errors='replace')
        if _extra:
            text = re.sub(r"</head\s*>", lambda m: _extra.decode('utf-8') + m.group(0),
                          text, count=1)
        body_at = text.find("<body")
        if body_at >= 0:
            text = text[:body_at] + _fix_svg_case(text[body_at:])
        return text.encode('utf-8')

    item.get_content = get_content


def _keep_body_attrs(item, attrs: Dict[str, str]) -> None:
    """<body> öznitelikleri (Calibre içindekiler çapası body id'sindedir;
    sınıf CSS'i taşır). ebooklib şablonu body'yi özniteliksiz yazar: bu
    belgenin get_content'i çıktıdaki ilk <body>'ye özgün öznitelikleri
    ekler (ebooklib'in koyduğu 'dir' korunur)."""
    import html as _html

    attrs = {k: v for k, v in attrs.items() if k != 'dir'}
    if not attrs or not hasattr(item, 'get_content'):
        return
    extra = "".join(f' {k}="{_html.escape(v, quote=True)}"' for k, v in attrs.items())
    base = item.get_content

    def get_content(default=None, _base=base, _extra=extra.encode('utf-8')):
        data = _base()
        return _BODY_OPEN_RE.sub(lambda m: b'<body' + _extra + (m.group(1) or b'') + b'>',
                                 data, count=1)

    item.get_content = get_content


def _label_text(tag) -> str:
    """Başlık/etiket metni: iç öğeler arasına boşluk eklenmez ('<i>a</i>)'
    -> 'a)'), <br/> boşluk olur, boşluklar teke iner."""
    from bs4 import NavigableString, Tag

    parts = []
    for node in tag.descendants:
        if isinstance(node, Tag) and node.name == 'br':
            parts.append(" ")
        elif isinstance(node, NavigableString):
            parts.append(str(node))
    return " ".join("".join(parts).split())


def _content_ids(content: bytes) -> set:
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(content.decode('utf-8', errors='replace'), 'lxml')
    except Exception:
        return set()
    return {t.get('id') for t in soup.find_all(id=True)}


def _text_before(container, tag) -> bool:
    """*container* içinde *tag*'den önce görünür metin var mı?"""
    from bs4 import NavigableString

    for node in container.descendants:
        if node is tag:
            return False
        if isinstance(node, NavigableString) and str(node).strip():
            return True
    return False


def _next_heading_without_text(target):
    """Metinsiz çapadan (<a id="x"/>) sonraki başlık; arada görünür metin
    varsa None."""
    from bs4 import NavigableString, Tag

    for node in target.next_elements:
        if isinstance(node, Tag) and node.name in _HEADING_TAGS:
            return node
        if isinstance(node, NavigableString) and str(node).strip():
            return None
    return None


def _resolve_nav_href(base: str, href: str) -> str:
    """Nav belgesine göre adres -> OPF klasörüne göre 'yol#parça'."""
    parts = urlsplit(href)
    path = unquote(parts.path)
    if path:
        path = posixpath.normpath(posixpath.join(base, path)) if base else posixpath.normpath(path)
    frag = unquote(parts.fragment)
    return f"{path}#{frag}" if frag else path


def _nest_entries(flat: List[Tuple[int, Optional[str], str]]) -> List[Dict]:
    """[(derinlik, adres, etiket)] -> iç içe giriş ağacı (bozuk iç içelikte
    derinlik atlaması bir düzeye indirilir)."""
    root: List[Dict] = []
    stack: List[Tuple[int, List[Dict]]] = [(0, root)]
    for depth, href, label in flat:
        while len(stack) > 1 and stack[-1][0] >= depth:
            stack.pop()
        entry = {'href': href, 'label': label, 'children': []}
        stack[-1][1].append(entry)
        stack.append((depth, entry['children']))
    return root


_PAGE_LABEL_RE = re.compile(
    r'^(?:p|pp|pag|pág|page|pagina|página|seite|s|str|стр|с)\.?\s*'
    r'([0-9]+|[ivxlcdm]+)$', re.I)


def _page_label(label: str) -> str:
    """Sayfa etiketi kaynak dilin kısaltmasıyla ('p. 12', 'S. 4', 'стр. 7')
    -> yalnız numara ('12'); tanınmazsa özgün etiket."""
    m = _PAGE_LABEL_RE.match(label.strip())
    return m.group(1) if m else label


def _toc_objects(entries: List[Dict]):
    """Giriş ağacı -> ebooklib book.toc (NCX için)."""
    from ebooklib import epub

    out = []
    for e in entries:
        if e['children']:
            out.append((epub.Section(e['label'], e['href'] or ''),
                        _toc_objects(e['children'])))
        elif e['href']:
            out.append(epub.Link(e['href'], e['label'], None))
    return out


def _nav_xhtml(entries, pages, landmarks, language: str, title: str,
               nav_dir: str, links: List[Dict]) -> bytes:
    """EPUB3 nav belgesi (toc + page-list + landmarks)."""
    import html as _html

    esc = _html.escape

    def rel(href: str) -> str:
        path, _, frag = href.partition('#')
        if nav_dir and path:
            path = posixpath.relpath(path, nav_dir)
        return f"{path}#{frag}" if frag else path

    def ol(items, indent):
        pad = "  " * indent
        rows = [f"{pad}<ol>"]
        for e in items:
            rows.append(f"{pad}  <li>")
            if e['href']:
                rows.append(f'{pad}    <a href="{esc(rel(e["href"]))}">{esc(e["label"])}</a>')
            else:
                rows.append(f"{pad}    <span>{esc(e['label'])}</span>")
            if e['children']:
                rows.extend(ol(e['children'], indent + 2))
            rows.append(f"{pad}  </li>")
        rows.append(f"{pad}</ol>")
        return rows

    lang = esc(language or "")
    out = ["<?xml version='1.0' encoding='utf-8'?>", "<!DOCTYPE html>",
           f'<html xmlns="http://www.w3.org/1999/xhtml" '
           f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">',
           "  <head>", f"    <title>{esc(title or '')}</title>"]
    for link in links:
        attrs = " ".join(f'{esc(k)}="{esc(v)}"' for k, v in link.items())
        out.append(f"    <link {attrs}/>")
    out += ["  </head>", "  <body>",
            '    <nav epub:type="toc" id="toc" role="doc-toc">',
            f"      <h2>{esc(EPUB_TOC_TITLE_TR)}</h2>"]
    out += ol(entries, 3)
    out.append("    </nav>")
    if pages:
        out += ['    <nav epub:type="page-list" id="page-list" hidden="hidden">',
                f"      <h2>{esc(EPUB_PAGE_LIST_TITLE_TR)}</h2>", "      <ol>"]
        for href, label in pages:
            out.append(f'        <li><a href="{esc(rel(href))}">{esc(label)}</a></li>')
        out += ["      </ol>", "    </nav>"]
    if landmarks:
        out += ['    <nav epub:type="landmarks" id="landmarks" hidden="hidden">', "      <ol>"]
        for href, label, kind in landmarks:
            typ = f' epub:type="{esc(kind)}"' if kind else ""
            out.append(f'        <li><a{typ} href="{esc(rel(href))}">{esc(label)}</a></li>')
        out += ["      </ol>", "    </nav>"]
    out += ["  </body>", "</html>", ""]
    return "\n".join(out).encode("utf-8")


class _RebuildContext:
    """rebuild_epub'ın belge başına durumu: dipnot bağlantıları (adres ->
    anahtar), Gutenberg işaret metinleri, yeni notların kimlikleri."""

    def __init__(self, parser: "EPUBParser", chapter: Optional[EPUBChapter],
                 text_marks: Dict[str, str], notes: Dict[int, str], soup):
        self.parser = parser
        self.note_hrefs = dict(chapter.note_links) if chapter is not None else {}
        self.text_marks = text_marks
        self.notes = notes
        self.used: List[Tuple[int, str, str]] = []
        self.ids = {t.get('id') for t in soup.find_all(id=True)}

    def _unique(self, base: str) -> str:
        cand, k = base, 2
        while cand in self.ids:
            cand, k = f"{base}-{k}", k + 1
        self.ids.add(cand)
        return cand

    def note_ref(self, n_text: str):
        """Yeni notun işareti: <sup><a epub:type="noteref" …>N</a></sup>."""
        try:
            n = int(n_text)
        except (TypeError, ValueError):
            return None
        if n not in self.notes:
            return None
        note_id = self._unique(f"{EPUB_NOTE_ID_PREFIX}{n}")
        ref_id = self._unique(f"{EPUB_NOTE_REF_ID_PREFIX}{n}")
        self.used.append((n, note_id, ref_id))
        sup = _new_tag('sup')
        a = _new_tag('a', {'href': f'#{note_id}', 'id': ref_id,
                           'epub:type': 'noteref', 'role': 'doc-noteref'})
        a.string = str(n)
        sup.append(a)
        return sup

    def append_notes(self, body) -> int:
        """Bu belgede işareti yazılan yeni notlar belge sonuna
        <aside epub:type="footnote"> olarak; döner: not sayısı."""
        from bs4 import BeautifulSoup

        for n, note_id, ref_id in self.used:
            aside = _new_tag('aside', {'epub:type': 'footnote',
                                       'role': 'doc-footnote', 'id': note_id})
            para = _new_tag('p')
            back = _new_tag('a', {'href': f'#{ref_id}', 'role': 'doc-backlink'})
            back.string = str(n)
            para.append(back)
            para.append(" ")
            temp = BeautifulSoup(self.parser._markdown_to_inline_html(self.notes[n]),
                                 'html.parser')
            for child in list(temp.children):
                para.append(child.extract())
            for ph in para.find_all(['kc-notref', 'kc-kayref']):
                ph.decompose()
            # Notta formül/görsel nesnesi: karşılık öğe yok; formülün TeX'i
            # metin olarak kalır (kaybolmaz), görsel yer tutucusu atılır.
            for ph in para.find_all('kc-nesne'):
                if ph.get('t') == 'm' and ph.get_text().strip():
                    ph.replace_with(ph.get_text())
                else:
                    ph.decompose()
            aside.append(para)
            body.append(aside)
        return len(self.used)


# Test
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        parser = EPUBParser()
        doc = parser.parse(sys.argv[1])

        print(f"Başlık: {doc.title}")
        print(f"Yazar: {doc.author}")
        print(f"Dil: {doc.language}")
        print(f"Bölüm sayısı: {doc.total_chapters}")

        print("\nBölümler:")
        for i, chapter in enumerate(doc.chapters[:5]):
            print(f"  {i+1}. {chapter.title or '(başlıksız)'}")

        segments = parser.extract_segments(doc)
        print(f"\nToplam segment: {len(segments)}")

        print("\n--- Markdown Çıktısı (ilk 2000 karakter) ---")
        md = parser.to_markdown(doc)
        print(md[:2000])
    else:
        print("Kullanım: python epub_parser.py <epub_dosyası>")
