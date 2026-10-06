"""TOC tabanlı başlık seviyesi düzeltici (Marker heading over-promotion fix).

Marker, PDF'nin görsel layout'undan başlık seviyesini tahmin eder ve sık sık
section/subsection'ı chapter'a terfi ettirir (gözlem: 13 bölümlük kitapta
61 ``\\chapter``). PDF'nin bookmark outline'ı (TOC / get_toc) doğru hiyerarşiyi
bilir; bu modül body.tex'teki ``\\chapter/\\section/...`` komutlarını TOC'a
göre yeniden seviyelendirir.

Tasarım ilkeleri:
- **Genel**: TOC'u olan her kitap faydalanır. TOC yoksa/yetersizse body
  DEĞİŞMEZ (güvenli fallback) — eski davranış korunur.
- **Yer-gerçeği TOC**: Outline seviyesi otoritedir. Eşleme fuzzy (OCR hatası
  "Ouestion"->"Question" için).
- **Konservatif**: Eşleşmeyen ``\\chapter`` ancak over-promotion KANITLIYSA
  (body'deki chapter sayısı TOC chapter sayısının >1.3 katı) section'a indirilir.
  Marker doğru seviyelemiş kitaplarda pass kendini devre dışı bırakır.

Saf fonksiyonlar — birim test edilebilir (PDF gerektirmez; ``toc`` listesi verilir).
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# PDF outline seviyesi -> LaTeX komutu, "chapter seviyesi" 2 varsayımıyla
# (Springer monografi: 1=Part/önmadde, 2=Chapter, 3=section, 4=subsection).
# Part'sız kitaplarda chapter seviyesi 1'dir; build_toc_index şemayı
# _detect_chapter_level ile tespit edip bu tabloyu kaydırarak kullanır.
_LEVEL_TO_CMD = {2: "chapter", 3: "section", 4: "subsection", 5: "subsubsection"}

_PART_PATTERN = re.compile(r"^part\s+[0-9ivxlcdm]+\b")
_CHAPTER_PATTERN = re.compile(r"^(chapter|bölüm|bolum)\s+[0-9ivxlcdm]+\b")

# L1'de Part DIŞINDA görülebilen önmadde başlıkları (numarasız \chapter* olur).
_FRONTMATTER = {
    "preface", "acknowledgments", "acknowledgements", "contents",
    "index", "foreword", "dedication", "about the author", "about the authors",
    "introduction to the series", "series preface",
}

_HEADING_CMDS = ("part", "chapter", "section", "subsection", "subsubsection",
                 "paragraph", "subparagraph")

# Komut -> derinlik sırası (küçük = üst seviye). Terfi sınırı için.
_RANK = {"part": 0, "chapter": 1, "section": 2, "subsection": 3,
         "subsubsection": 4, "paragraph": 5, "subparagraph": 6}
_HEADING_START_RE = re.compile(
    r"\\(" + "|".join(_HEADING_CMDS) + r")(\*?)\s*\{"
)

# Normalleştirme için ön-ek/temizlik desenleri
# 2026-10-05 (G1a-1): numarasız/harfli ek öneki de atılır ("Appendix:
# Methodology", "Appendix A - X", "Anhang: X"; altı kaynak dil). converter
# kitap sonu eklerini \appendix + öneksiz adla basar ("Methodology"); yer
# imi "Appendix: Methodology" kalınca kısa adlar bulanık eşikte (0,82)
# eşleşmiyor, aşırı terfi kipinde \section'a indiriliyordu.
_PREFIX_RE = re.compile(
    r"^\s*(?:(chapter|part|appendix|bölüm|kısım)\s+[0-9ivxlcIVXLC]+\s*[:.\-–—]?"
    r"|(?:appendix|anhang|annexe|appendice|ap[ée]ndice|anexo|приложение)"
    r"(?:\s+(?:[0-9]{1,2}|[A-ZА-Я]))?\s*[:.\-–—])\s*",
    re.IGNORECASE)
_SECNUM_RE = re.compile(r"^\s*[0-9]+(?:\.[0-9]+)*\.?\s+")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?")


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _normalize(title: str) -> str:
    """Başlık metnini eşleme için sadeleştir: LaTeX komutları, ön-ek, numara,
    aksan ve noktalama atılır; küçük harfe indirilir."""
    s = title
    # LaTeX argümanlı sarmalayıcılar: \textit{X} -> X (sadece içeriği bırak)
    s = re.sub(r"\\(?:textit|textbf|emph|texttt|textsc|mathrm|text)\*?\{([^{}]*)\}", r"\1", s)
    s = _LATEX_CMD_RE.sub(" ", s)          # kalan komutlar
    s = s.replace("{", " ").replace("}", " ")
    s = _strip_accents(s)
    s = s.replace("\\&", "and").replace("&", "and")
    s = _PREFIX_RE.sub("", s)              # "Chapter 1:" / "Part II:" ön-eki
    s = _SECNUM_RE.sub("", s)              # "2.2.3 " section numarası
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _detect_chapter_level(toc: list) -> int:
    """TOC şemasını tespit et: hangi outline seviyesi LaTeX ``chapter``\\'dır?

    Sıra: (1) L1'de "Part N" var -> Springer şeması, chapter=2.
    (2) Ham başlığı "Chapter N ..." olan girdi hangi seviyedeyse o.
    (3) Aksi halde en sığ seviye chapter'dır (part'sız kitapların en yaygın
    düzeni: L1=chapter, L2=section). Eski kod şemayı 2'ye sabitleyip part'sız
    kitaplarda tüm yapıyı bozuyordu (#44).
    """
    levels = []
    for entry in toc:
        try:
            level, title = int(entry[0]), str(entry[1])
        except Exception:
            continue
        raw_low = _strip_accents(title).lower().strip()
        if level == 1 and _PART_PATTERN.match(raw_low):
            return 2
        levels.append((level, raw_low))
    for level, raw_low in levels:
        if _CHAPTER_PATTERN.match(raw_low):
            return level
    if levels:
        return min(level for level, _ in levels)
    return 2


def build_toc_index(toc: list) -> list[dict]:
    """``fitz get_toc()`` -> normalize edilmiş eşleme kayıtları.

    Her kayıt: {norm, cmd, star, level, raw}. ``cmd`` LaTeX komutu, ``star``
    numarasız (önmadde) mi. Boş/normalize-edilemeyen başlıklar atlanır.
    """
    chapter_level = _detect_chapter_level(toc)
    out = []
    for entry in toc:
        try:
            level, title = entry[0], entry[1]
        except Exception:
            continue
        norm = _normalize(title)
        if not norm:
            continue
        # Part/önmadde tespiti HAM başlıkta yapılır (normalize ön-eki siler).
        raw_low = _strip_accents(str(title)).lower().strip()
        offset = level - chapter_level
        if offset < 0:
            # chapter seviyesinin ÜSTÜ: part ya da önmadde
            if _PART_PATTERN.match(raw_low):
                cmd, star = "part", False
            elif norm in _FRONTMATTER or raw_low.rstrip(" :.") in _FRONTMATTER:
                cmd, star = "chapter", True
            else:
                # Bilinmeyen üst seviye (nadiren): bölüm gibi davran ama numarasız
                cmd, star = "chapter", True
        elif offset == 0:
            if norm in _FRONTMATTER or raw_low.rstrip(" :.") in _FRONTMATTER:
                cmd, star = "chapter", True
            else:
                cmd, star = "chapter", False
        else:
            cmd = _LEVEL_TO_CMD.get(offset + 2, "subsubsection")
            star = False
        out.append({"norm": norm, "cmd": cmd, "star": star, "level": level, "raw": title})
    return out


def _iter_headings(body: str):
    """body içindeki her başlık makrosunu dengeli-parantezle yield et:
    (start, end, cmd, star, title). ``end`` kapanış ``}``'dan SONRAKİ indeks."""
    for m in _HEADING_START_RE.finditer(body):
        cmd = m.group(1)
        star = m.group(2) == "*"
        i = m.end()  # '{' sonrası
        depth = 1
        j = i
        n = len(body)
        while j < n and depth > 0:
            c = body[j]
            if c == "\\":          # kaçışlı karakteri atla
                j += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        if depth != 0:
            continue  # dengesiz; dokunma
        title = body[i:j - 1]
        yield (m.start(), j, cmd, star, title)


def _nearest_rank(candidates: list[dict], cur_cmd: str):
    """Aynı norm'a sahip birden çok TOC kaydından, başlığın MEVCUT seviyesine
    en yakın olanı seç. Eşitlikte terfi yerine demotion/aynı-seviye tercih
    edilir (#47/#76: her bölümde tekrarlanan 'Introduction' section'ı, ilk
    TOC kaydına — chapter'a — terfi ediyordu)."""
    cur_rank = _RANK.get(cur_cmd, 2)
    return min(
        candidates,
        key=lambda r: (abs(_RANK[r["cmd"]] - cur_rank),
                       _RANK[r["cmd"]] < cur_rank),  # tie: terfi en son
    )


def _best_match(norm: str, index: list[dict], threshold: float, cur_cmd: str):
    """norm başlığa en iyi TOC kaydını döndür (exact > fuzzy), yoksa None.

    Aynı norm birden çok seviyede geçiyorsa (ör. önmadde 'Introduction'
    chapter'ı + her bölümdeki 'Introduction' section'ı) mevcut komut
    seviyesine en yakın kayıt kazanır — pozisyonsuz ilk-kayıt seçimi
    tekrarlı başlıklarda yapıyı bozuyordu.
    """
    if not norm:
        return None
    # exact
    exact = [rec for rec in index if rec["norm"] == norm]
    if exact:
        return _nearest_rank(exact, cur_cmd)
    # fuzzy
    best_r = 0.0
    ratios: list[tuple[float, dict]] = []
    for rec in index:
        r = SequenceMatcher(None, norm, rec["norm"]).ratio()
        ratios.append((r, rec))
        if r > best_r:
            best_r = r
    if best_r < threshold:
        return None
    top = [rec for r, rec in ratios if r >= best_r - 1e-9]
    return _nearest_rank(top, cur_cmd)


def relevel(body: str, toc: list, threshold: float = 0.82) -> tuple[str, dict]:
    """body.tex'teki başlıkları TOC'a göre yeniden seviyelendir.

    Returns (yeni_body, rapor). TOC yetersizse body aynen döner.
    """
    report = {"applied": False, "matched": 0, "demoted": 0, "unchanged": 0,
              "toc_chapters": 0, "body_chapters_before": 0, "reason": ""}

    index = build_toc_index(toc)
    toc_chapters = sum(1 for r in index if r["cmd"] == "chapter" and not r["star"])
    report["toc_chapters"] = toc_chapters
    if toc_chapters == 0:
        report["reason"] = "TOC'ta chapter-seviyesi girdi yok"
        return body, report

    headings = list(_iter_headings(body))
    body_chapters = sum(1 for h in headings if h[2] == "chapter" and not h[3])
    report["body_chapters_before"] = body_chapters

    # Over-promotion kanıtı: body chapter'ları TOC'un belirgin üstünde mi?
    over_promoted = body_chapters > max(toc_chapters * 1.3, toc_chapters + 2)

    # Body'de aynı norm'dan kaç başlık var? Tekrarlanan başlıklar ('Summary',
    # 'Exercises', her bölümdeki 'Introduction') chapter'a TERFİ edemez —
    # hangisinin TOC'taki tekil kayda karşılık geldiği belirsizdir (#76).
    body_norm_counts: dict[str, int] = {}
    for h in headings:
        n = _normalize(h[4])
        body_norm_counts[n] = body_norm_counts.get(n, 0) + 1

    pieces = []
    last = 0
    for (start, end, cmd, star, title) in headings:
        pieces.append(body[last:start])
        last = end
        norm = _normalize(title)
        rec = _best_match(norm, index, threshold, cmd)
        # 3+ seviye YUKARI terfiyi reddet: Marker bölüm/part'ları en fazla 1-2
        # seviye yanlış (section->part olur), ama stray TOC-referansı (subsection
        # "Part I") part'a 3 seviye sıçramaz. Bu eşik gerçek part'ı korur,
        # Contents'ten sızan kopyayı reddeder.
        if rec is not None and (_RANK[cmd] - _RANK[rec["cmd"]]) >= 3:
            rec = None
        # Terfi (rank küçülmesi) yalnızca güvenliyse: hedef 'part' (açık
        # "Part N" deseniyle kanıtlı) ya da bu başlık body'de TEK. Tekrarlı
        # generic başlığın chapter'a terfisi sahte bölümler türetiyordu.
        if (rec is not None
                and _RANK[rec["cmd"]] < _RANK[cmd]
                and rec["cmd"] != "part"
                and body_norm_counts.get(norm, 0) > 1):
            rec = None
        new_cmd, new_star = cmd, star
        if rec is not None:
            new_cmd, new_star = rec["cmd"], rec["star"]
            if (new_cmd, new_star) != (cmd, star):
                report["matched"] += 1
            else:
                report["unchanged"] += 1
        elif over_promoted and cmd in ("chapter", "part"):
            # Eşleşmeyen terfi etmiş başlık -> section'a indir
            new_cmd, new_star = "section", False
            report["demoted"] += 1
        else:
            report["unchanged"] += 1
        star_s = "*" if new_star else ""
        pieces.append(f"\\{new_cmd}{star_s}{{{title}}}")
    pieces.append(body[last:])

    new_body = "".join(pieces)
    report["body_chapters_after"] = sum(
        1 for h in _iter_headings(new_body) if h[2] == "chapter" and not h[3])
    report["applied"] = (report["matched"] + report["demoted"]) > 0
    if not report["applied"]:
        report["reason"] = "değişiklik gerekmedi (seviyeler zaten doğru)"
    return new_body, report


def relevel_from_pdf(body: str, pdf_path: str, threshold: float = 0.82) -> tuple[str, dict]:
    """PDF yolundan TOC çıkarıp relevel uygular. PDF/TOC yoksa body aynen döner."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        toc = doc.get_toc()
        doc.close()
    except Exception as exc:  # pragma: no cover
        return body, {"applied": False, "reason": f"TOC okunamadı: {exc}"}
    if not toc:
        return body, {"applied": False, "reason": "PDF'de bookmark/TOC yok"}
    return relevel(body, toc, threshold)
