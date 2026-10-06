#!/usr/bin/env python3
"""2_derle.py — Yol A, Adım 2: çevrilmiş LaTeX'i alıp PDF derle.

``1_cikar.py`` ile üretilen EXPORT.tex'i çevirip ``EXPORT_TR.tex`` olarak
kaydettikten sonra bu script'i çalıştır. Çevrilmiş .tex'i alır, hangi
kitaba ait olduğunu (dosyaya gömülü ``% KITAPCEVIR-META`` başlığından)
çözer, sanitize + QC + XeLaTeX derlemesini çalıştırır ve Türkçe PDF üretir.

ÇEVİRİ MODELİ / GPU GEREKMEZ — yalnızca XeLaTeX gerekir. Ajan/insan
çevirisinin kelime tercihleri KORUNUR (manuel mod: MT-autofix kapalı).

Kullanım:
    python 2_derle.py output/<slug>/EXPORT_TR.tex
    python 2_derle.py cevirim.tex --output-dir output/<slug>    # kimlik kaybolmuşsa
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from translate_book import (  # noqa: E402
    PIPELINE_LOG_NAME,
    QC_REPORT_NAME,
    brace_fix_count,
    run_pipeline,
)
from latex_pipeline.inspector import (  # noqa: E402
    match_file_line_error,
    tex_log_counts,
    unwrap_tex_log,
)
from yol_a_ortak import (  # noqa: E402
    parse_meta_header,
    read_sidecar,
    resolve_book_dir,
)


def _find_pdf(output_dir: Path, slug: str) -> Path | None:
    """Derlenen PDF'yi bul: önce compiled/<slug>_TR.pdf, sonra en yeni .pdf."""
    compiled = output_dir / "compiled"
    preferred = compiled / f"{slug}_TR.pdf"
    if preferred.exists() and preferred.stat().st_size > 1024:
        return preferred
    if compiled.is_dir():
        pdfs = [p for p in compiled.glob("*.pdf") if p.stat().st_size > 1024]
        if pdfs:
            return max(pdfs, key=lambda p: p.stat().st_mtime)
    return None


def _compile_log_path(output_dir: Path) -> Path:
    """XeLaTeX log'unun GERÇEK yeri (F34, 2026-07-29).

    compiler.py XeLaTeX'i ``-output-directory=<book_tr.tex'in dizini>`` ile
    koşar (source/), başarıda yalnız PDF'yi ``compiled/``'a kopyalar; .log hiç
    kopyalanmaz. Bu yüzden derleme hatasında kullanıcıyı/otonom ajanı
    ``compiled/*.log``'a yönlendirmek boş (ya da bayat) dizine yönlendirmekti.
    """
    return output_dir / "source" / "book_tr.log"


def _log_error_lines(log_path: Path, limit: int = 5) -> list:
    """Log'tan anlamlı XeLaTeX hata satırlarını çıkar (ilk *limit* tanesi).

    Anlamlı sayılanlar: ``!`` ile başlayan satırlar ve ``-file-line-error``
    formatındaki ``dosya.tex:123:`` satırları. LaTeX hataları zincirlendiği
    için İLK hatalar kök nedene en yakın olanlardır. (R5) 79 sütunda
    kesilmiş satırlar birleştirilir; yolda boşluk olabilir.
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for line in unwrap_tex_log(text):
        s = line.strip()
        if not s:
            continue
        if s.startswith("!") or match_file_line_error(s):
            if s not in found:
                found.append(s[:200])
            if len(found) >= limit:
                break
    return found


# QC raporu (translate_book.run_pipeline yazar) ve EPUB atlama notu
# (latex_pipeline/epub_output.py yazar; sözleşme S5: tek satır Türkçe neden).
# İki adın tek kaynağı yazan modüldür: QC_REPORT_NAME translate_book'tan
# (yukarıda), EPUB_SKIP_NOTE_NAME epub_output'tan içe aktarılır.
from latex_pipeline.epub_output import EPUB_REPORT_NAME, EPUB_SKIP_NOTE_NAME  # noqa: E402
from ui.branding import TEX_COMPILE_BUTTON, TEX_TITLE  # noqa: E402
# qc_top: en çok bulgu veren kaç kontrol raporlanır (S4).
QC_TOP_N = 3
# Bu koşuda yazılmış sayılma toleransı (sn): dosya sistemi zaman damgası
# çözünürlüğü için. Önceki koşudan kalan rapor/not sonuçmuş gibi okunmaz.
_FRESH_TOLERANCE_S = 2.0


def _fresh_file(path: Path, since: float) -> bool:
    """*path* var ve bu koşuda (``since``'ten sonra) yazılmış mı?"""
    try:
        return path.is_file() and path.stat().st_mtime >= since - _FRESH_TOLERANCE_S
    except OSError:
        return False


def _read_qc_summary(output_dir: Path, since: float) -> dict:
    """Bu koşunun QC özeti: ``{"qc_score", "qc_report", "qc_top"}``.

    Kaynak: pipeline'ın yazdığı ``qc_report.txt`` (print_summary biçimi,
    okuyucusu ``qc_engine.parse_qc_report_summary``). Rapor yoksa ya da bu
    koşuda yazılmadıysa (QC hiç çalışmadı) skor/yol null, liste boştur —
    bayat rapor sonuçmuş gibi gösterilmez.
    """
    empty = {"qc_score": None, "qc_report": None, "qc_top": []}
    path = output_dir / QC_REPORT_NAME
    if not _fresh_file(path, since):
        return empty
    try:
        from latex_pipeline.qc_engine import parse_qc_report_summary
        parsed = parse_qc_report_summary(
            path.read_text(encoding="utf-8", errors="replace"), top=QC_TOP_N)
    except (OSError, ImportError):
        parsed = None
    if not parsed:
        return {**empty, "qc_report": str(path)}
    return {"qc_score": parsed["score"], "qc_report": str(path),
            "qc_top": parsed["top"]}


def _read_epub_skip_note(output_dir: Path, since: float) -> str | None:
    """S5 notu: EPUB üretilmediyse epub_output'un yazdığı tek satır neden.

    Önce ``compiled/`` (EPUB'un yazılacağı yer), sonra kitap klasörü
    aranır; yalnız bu koşuda yazılmış not okunur.
    """
    for cand in (output_dir / "compiled" / EPUB_SKIP_NOTE_NAME,
                 output_dir / EPUB_SKIP_NOTE_NAME):
        if not _fresh_file(cand, since):
            continue
        try:
            text = cand.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.strip():
                return line.strip()
    return None


# Sözleşme S7 (2026-09-25): EPUB kaynaklı kitapta Türkçe EPUB'u üreten adım
# (latex_pipeline/epub_output.py) compiled/ altına kapsam raporu yazar:
# {"kapsam": 0..1, "cevrilmemis_blok": int, "toplam_blok": int}.
# Dosya adının tek kaynağı yazan modüldür (EPUB_REPORT_NAME, yukarıda içe aktarıldı).


def _read_compile_counts(output_dir: Path, since: float) -> dict:
    """(R5) Bu koşunun derleme günlüğünden ``{"tex_hatalari", "eksik_glif"}``.

    Sayılar ``quality_report.txt`` ile aynı ayrıştırıcıdan gelir
    (``inspector.tex_log_counts``): -file-line-error hata kayıtları ve
    "Missing character" kayıtlarının toplamı. Günlük yoksa ya da bu koşuda
    yazılmadıysa (derleme hiç çalışmadı) iki sayı da 0'dır; derlemenin koşup
    koşmadığını ``ok``/``exit_code`` alanları söyler.
    """
    log_path = _compile_log_path(output_dir)
    if not _fresh_file(log_path, since):
        return {"tex_hatalari": 0, "eksik_glif": 0}
    errors, glyphs = tex_log_counts(log_path)
    return {"tex_hatalari": errors, "eksik_glif": glyphs}


def _count_or_none(value) -> int | None:
    """Sözleşme sayısı: bool olmayan, negatif olmayan tamsayı; değilse None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _read_epub_report(output_dir: Path, since: float) -> dict | None:
    """(S7) Bu koşuda yazılmış ``compiled/epub_rapor.json``.

    Dönüş: ``{"kapsam": float, "cevrilmemis_blok": int|None,
    "toplam_blok": int|None}``. Dosya yoksa, bayatsa ya da ``kapsam`` 0..1
    aralığında bir sayı değilse None (uydurma kapsam gösterilmez).
    """
    path = output_dir / "compiled" / EPUB_REPORT_NAME
    if not _fresh_file(path, since):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    kapsam = data.get("kapsam")
    if (isinstance(kapsam, bool) or not isinstance(kapsam, (int, float))
            or not 0 <= kapsam <= 1):
        return None
    return {"kapsam": float(kapsam),
            "cevrilmemis_blok": _count_or_none(data.get("cevrilmemis_blok")),
            "toplam_blok": _count_or_none(data.get("toplam_blok"))}


def _read_brace_fixes(output_dir: Path, since: float) -> int:
    """(F2-01) Bu koşunun book_tr.tex'inde paragraf sonunda kapatılan '{'
    sayısı; derleme koşmadıysa ya da dosya bayatsa 0."""
    path = output_dir / "source" / "book_tr.tex"
    if not _fresh_file(path, since):
        return 0
    try:
        return brace_fix_count(path.read_text(encoding="utf-8",
                                              errors="replace"))
    except OSError:
        return 0


def _brace_fix_line(count: int) -> str | None:
    """(F2-01) Çeviride kapanmamış '{' onarıldıysa kullanıcıya tek satır."""
    if not count:
        return None
    return (f"Çeviride {count} kapanmamış '{{' vardı; kendi paragrafının "
            f"sonunda kapatıldı (metin korunur, biçim o paragrafın sonuna "
            f"kadar uzayabilir). Satırlar pipeline.log'da; EXPORT_TR.tex'te "
            f"eksik '}}' işaretini ekleyip yeniden derle.")


def _compile_issue_line(counts: dict) -> str | None:
    """(R5) Günlükte TeX hatası / eksik glif varsa kullanıcıya tek satır."""
    parts = []
    if counts["tex_hatalari"]:
        parts.append(f"{counts['tex_hatalari']} TeX hatası")
    if counts["eksik_glif"]:
        parts.append(f"{counts['eksik_glif']} eksik glif")
    if not parts:
        return None
    return ("Derleme günlüğünde " + " ve ".join(parts)
            + " var; PDF'de bozuk dizgi ya da boş kutu (□) olabilir.")


def _print_json_line(enabled: bool, **fields) -> None:
    """--json sözleşmesi (H3, 2026-07-29): HER çıkış yolunda son satır JSON.

    Alan kümesi çıkış yoluna göre DEĞİŞMEZ: pdf/epub her zaman vardır
    (üretilmediyse null); erken çıkışlarda slug/book_name bilinmiyorsa null.
    S4 (2026-09-25): qc_score / qc_report (QC bu koşuda çalışmadıysa null),
    qc_top (en çok bulgu veren ilk üç kontrol; yoksa boş liste), epub_note
    (EPUB kaynaklı kitapta EPUB üretilmediyse Türkçe neden; yoksa null).
    R5 (2026-09-25): tex_hatalari / eksik_glif (bu koşunun derleme
    günlüğündeki TeX hatası ve eksik glif sayısı; derleme koşmadıysa 0),
    epub_kapsam (S7: Türkçe EPUB'da çevrilmiş blok oranı 0..1; EPUB
    üretilmediyse ya da rapor yoksa null).
    """
    if not enabled:
        return
    payload = {
        "ok": False, "qc_ok": False, "exit_code": 1,
        "slug": None, "book_name": None, "output_dir": None,
        "pdf": None, "epub": None, "error": None,
        "qc_score": None, "qc_report": None, "qc_top": [],
        "epub_note": None,
        "tex_hatalari": 0, "eksik_glif": 0, "epub_kapsam": None,
    }
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"{TEX_TITLE} · Adım 2: çevrilmiş .tex'i al, PDF derle "
                    "(çeviri modeli gerekmez).",
    )
    parser.add_argument("input", help="Çevrilmiş LaTeX dosyası (EXPORT_TR.tex)")
    parser.add_argument("--output-dir", default=None,
                        help="Kitabın output dizini (kimlik başlığı silinmiş/"
                             "dosya taşınmışsa elle ver).")
    # NOT (#10): --force-mt bayrağı kaldırıldı — bu script MT çevirisi hiç
    # çalıştırmadığı için (skip_translate=True) bayrak ölü bir no-op'tu ve
    # 'kilit kalktı' yanılgısı yaratıyordu. Yol A kilidini gerçekten
    # kaldırmak için: python translate_book.py <kitap> --force-mt
    parser.add_argument("--strict", action="store_true",
                        help="Yapısal ön kontrol (bölüm sayısı META ile "
                             "uyuşmazsa) hata ile çık.")
    # (F37) EPUB kaynaklı kitapta Türkçe EPUB çıktısı varsayılan AÇIK —
    # translate_book.py --import-tex akışıyla aynı sözleşme.
    parser.add_argument("--no-epub-output", action="store_true",
                        help="EPUB kaynaklı kitapta Türkçe EPUB ÜRETME "
                             "(sadece PDF; varsayılan: EPUB çıktı açık).")
    parser.add_argument("--json", action="store_true",
                        help="Son satırda makine-okunur JSON bas.")
    args = parser.parse_args()

    tex_path = Path(args.input).resolve()
    if not tex_path.exists():
        print(f"HATA: Dosya bulunamadı: {tex_path}")
        _print_json_line(args.json, error="input_missing")
        return 1
    if tex_path.stat().st_size <= 100:
        print(f"HATA: Dosya çok kısa ({tex_path.stat().st_size} byte): {tex_path}")
        _print_json_line(args.json, error="input_too_small")
        return 1

    # --- Kitabı/output dizinini çöz ---------------------------------------
    res = resolve_book_dir(tex_path, PROJECT_ROOT, args.output_dir)
    if not res["output_dir"]:
        print("HATA: Bu .tex'in hangi kitaba ait olduğu çözülemedi.")
        print("      Denenen konumlar:")
        for t in res["tried"]:
            print(f"        - {t}")
        print("      Çözüm: --output-dir output/<slug> ver, ya da önce")
        print("             `python 1_cikar.py <kitap>` çalıştır.")
        _print_json_line(args.json, error="book_dir_unresolved")
        return 1

    output_dir = Path(res["output_dir"])
    slug = res["slug"]
    book_name = res["book_name"]

    # --- Çıkarım artefaktları yerinde mi? (QC referansı + resume) ----------
    # (A-10, 2026-07-27) run_pipeline full_book.md'yi KOŞULSUZ ister (yoksa
    # exit 1 'Run extraction first'). Eski kapı body.tex'i tek başına yeterli
    # sayıyordu — kullanıcı buradan geçip pipeline'da kafa karıştırıcı hatayla
    # düşüyordu. Kapı gerçek gereksinimle hizalandı.
    body_ok = (output_dir / "source" / "body.tex").exists()
    md_ok = (output_dir / "full_book.md").exists()
    if not md_ok:
        print(f"HATA: full_book.md bulunamadı: {output_dir}")
        if body_ok:
            print("      (source/body.tex duruyor ama pipeline full_book.md'yi "
                  "koşulsuz ister)")
        print(f"      Çözüm: python 1_cikar.py <kitap>   (aynı --name ile; "
              f"mevcut çıkarım varsa hızlı geçer)")
        _print_json_line(args.json, error="extraction_missing", slug=slug,
                         book_name=book_name, output_dir=str(output_dir))
        return 1

    # --- Kimlik/tur bilgisi (META > sidecar) -------------------------------
    header_meta = parse_meta_header(tex_path)
    sidecar = read_sidecar(output_dir)
    # (#6) 1_cikar'ın tespit ettiği book_type taşınır — 'source_book.pdf'
    # adından tespit hep 'science'a düşüyordu, QC yanlış profille koşuyordu.
    book_type = (header_meta.get("book_type")
                 or sidecar.get("book_type") or None)

    # --- Kodlama ön kontrolü (F14, 2026-07-28): UTF-8 olmayan dosya sessiz
    # bozuk okunmaz; kopyalama/pipeline'a girmeden önce net hata verilir.
    # (utf-8-sig: BOM'lu UTF-8 kabul edilir, BOM yutulur.)
    try:
        tr_text = tex_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        print(f"HATA: {tex_path.name} UTF-8 değil "
              f"(bayt konumu {exc.start}: {exc.reason}).")
        print("      Dosyayı UTF-8 olarak kaydet (VS Code: sağ alttan encoding")
        print("      -> 'Save with Encoding' -> UTF-8) ya da dönüştür:")
        print(f"      iconv -f WINDOWS-1254 -t UTF-8 {tex_path.name} > duzeltilmis.tex")
        _print_json_line(args.json, error="encoding", slug=slug,
                         book_name=book_name, output_dir=str(output_dir))
        return 1

    # --- Yapısal ön kontrol (#9): bölüm sayısı META ile uyuşuyor mu? -------
    tr_chapters = tr_text.count("\\chapter{") + tr_text.count("\\part{")
    expected_raw = (header_meta.get("body_chapters")
                    or sidecar.get("body_chapters") or "")
    try:
        expected_chapters = int(str(expected_raw).strip() or 0)
    except ValueError:
        expected_chapters = 0
    if expected_chapters > 0 and tr_chapters != expected_chapters:
        print(
            f"UYARI: Çevrilmiş dosyada {tr_chapters} bölüm var, kaynak "
            f"{expected_chapters} bölümdü — parça birleştirmede bölüm "
            f"düşmüş/eklenmiş olabilir. Bölüm başlıklarını karşılaştır."
        )
        if args.strict:
            print("(--strict: yapısal uyuşmazlık hata sayıldı)")
            _print_json_line(args.json, error="structure_mismatch", slug=slug,
                             book_name=book_name, output_dir=str(output_dir))
            return 1

    # --- Çevrilmiş .tex → source/translated_body.tex -----------------------
    src_dir = output_dir / "source"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "translated_body.tex"
    shutil.copy2(tex_path, target)

    print(f"\n{'='*60}")
    print(f"  {TEX_TITLE} · Adım 2 ({TEX_COMPILE_BUTTON})")
    print(f"  Kitap:  {book_name}")
    print(f"  Çıktı:  {output_dir}   (çözüm: {res['source']})")
    print(f"  Çeviri: {tex_path.name} → source/translated_body.tex")
    print(f"{'='*60}\n")

    # --- Kapak için yerel kaynak PDF (varsa) -------------------------------
    src_pdf = output_dir / "source_book.pdf"
    pdf_path = str(src_pdf) if src_pdf.exists() and src_pdf.stat().st_size > 1024 else None

    # --json sözleşmesi (F14): pipeline beklenmedik istisnayla çökse bile
    # son satır JSON'u ve exit kodu üretilir, ham traceback sözleşmeyi bozmaz.
    # (S4) Bu koşunun başlangıcı: qc_report.txt / EPUB notunun tazelik ölçüsü.
    run_started = time.time()
    try:
        exit_code = run_pipeline(
            str(output_dir), book_name,
            skip_translate=True, skip_compile=False,
            resume=True, manual_translation=True,
            book_type=book_type, pdf_path=pdf_path,
        )
    except Exception as exc:
        traceback.print_exc()
        print(f"HATA: pipeline beklenmedik şekilde çöktü: {exc}")
        exit_code = 1

    # --- Sonuç -------------------------------------------------------------
    # PDF yalnızca derlemenin gerçekten başarılı olduğu koşularda aranır ve
    # kopyalanır (0=tam başarı, 3=derlendi ama QC düşük). Başarısız koşuda
    # (exit 1/2) önceki koşudan kalan bayat PDF'yi sonuçmuş gibi göstermek
    # kullanıcıyı/otomasyonu yanıltıyordu.
    final_pdf = None
    if exit_code in (0, 3):
        pdf = _find_pdf(output_dir, slug)
        if pdf:
            final_pdf = output_dir / f"{slug}_TR.pdf"
            try:
                if pdf.resolve() != final_pdf.resolve():
                    shutil.copy2(pdf, final_pdf)
            except OSError:
                final_pdf = pdf  # kopya olmazsa compiled/ içindekini göster

    # --- EPUB kaynaklı kitapta Türkçe EPUB çıktısı (F37) -------------------
    # translate_book.py bunu yalnız kendi main()'inde üretiyordu; Yol A'nın
    # 2_derle kapısı run_pipeline'ı doğrudan çağırdığı için EPUB kitaplarda
    # çıktı sessizce kayboluyordu (1_cikar epub_segment_mapping.json'u
    # üretmesine rağmen). PDF kaynaklı kitaplarda hiçbir şey değişmez.
    final_epub = None
    epub_note = None
    src_epub = output_dir / "source_book.epub"
    if exit_code in (0, 3) and not args.no_epub_output and src_epub.exists():
        epub_error = None
        try:
            from latex_pipeline.epub_output import generate_epub_output
            final_epub = generate_epub_output(
                original_epub=str(src_epub),
                output_dir=str(output_dir),
                book_name=book_name,
            )
        except Exception as exc:  # EPUB çıktı PDF'yi asla düşürmez
            print(f"UYARI: EPUB çıktı oluşturulamadı: {exc}")
            epub_error = f"EPUB çıktı oluşturulamadı: {exc}"
        if not final_epub:
            # (S4/S5) Neden: epub_output'un bu koşuda yazdığı not; not
            # yoksa ve adım istisnayla düştüyse istisna iletisi.
            epub_note = _read_epub_skip_note(output_dir, run_started) or epub_error

    qc = _read_qc_summary(output_dir, run_started)
    # (R5) Derleme günlüğündeki gerçek TeX hatası / eksik glif sayısı.
    counts = _read_compile_counts(output_dir, run_started)
    issue_line = _compile_issue_line(counts)
    # (F2-01) Kapanmamış '{' onarımı görünür olsun (konsol özeti; JSON alan
    # kümesi sabit sözleşme — alan eklemek arayüz kulvarının işi).
    brace_fixes = _read_brace_fixes(output_dir, run_started)
    brace_line = _brace_fix_line(brace_fixes)
    # (S7) EPUB kapsamı yalnız bu koşuda EPUB üretildiyse okunur.
    epub_report = _read_epub_report(output_dir, run_started) if final_epub else None

    print(f"\n{'='*60}")
    if exit_code == 0 and final_pdf:
        print(f"  ✓ PDF hazır: {final_pdf}")
        if brace_line:
            print(f"    UYARI: {brace_line}")
            print(f"    Günlük: {output_dir / PIPELINE_LOG_NAME}")
        if issue_line:
            print(f"    UYARI: {issue_line}")
            print(f"    Günlük: {_compile_log_path(output_dir)}")
    elif exit_code == 3 and final_pdf:
        print(f"  ⚠ PDF üretildi ama QC skoru eşiğin altında: {final_pdf}")
        if qc["qc_score"] is not None:
            print(f"    QC skoru: {qc['qc_score']:.0f}/100")
        if qc["qc_top"]:
            top = ", ".join(f"{t['check']} ({t['count']})" for t in qc["qc_top"])
            print(f"    En çok bulgu: {top}")
        if issue_line:
            print(f"    {issue_line}")
        if brace_line:
            print(f"    UYARI: {brace_line}")
            print(f"    Günlük: {output_dir / PIPELINE_LOG_NAME}")
        # (S4) Dil-nötr: kaynak İngilizce olmak zorunda değil (altı dil).
        print("    Olası neden: çeviri eksik ya da kaynak dilde kalmış olabilir")
        print("    veya bölüm/denklem sayısı kaynakla eşleşmiyor olabilir.")
        print(f"    Ayrıntı: {output_dir / QC_REPORT_NAME}")
        if issue_line:
            print(f"    Günlük: {_compile_log_path(output_dir)}")
    elif exit_code == 2:
        log_path = _compile_log_path(output_dir)
        print("  ✗ Derleme başarısız (XeLaTeX).")
        if log_path.exists():
            print(f"    Log: {log_path}")
            err_lines = _log_error_lines(log_path)
            if err_lines:
                print("    İlk hatalar:")
                for line in err_lines:
                    print(f"      {line}")
        else:
            print(f"    Log bulunamadı ({log_path}) — XeLaTeX hiç çalışmamış "
                  f"olabilir.")
        print(f"    Ayrıntı: {output_dir/'pipeline.log'}")
    else:
        print(f"  ✗ Pipeline hatası (exit={exit_code}). Ayrıntı: pipeline.log")
    if final_epub:
        print(f"  ✓ EPUB hazır: {final_epub}")
        if epub_report and epub_report["kapsam"] < 1:
            oran = f"%{epub_report['kapsam'] * 100:.1f}".replace(".", ",")
            n_bad = epub_report["cevrilmemis_blok"]
            n_all = epub_report["toplam_blok"]
            blok = (f"{n_bad}/{n_all} blok kaynak dilde kaldı"
                    if n_bad is not None and n_all is not None
                    else "bazı bloklar kaynak dilde kaldı")
            print(f"    UYARI: EPUB'da {blok} (kapsam {oran}).")
    elif epub_note:
        print(f"  EPUB üretilmedi: {epub_note}")
    print(f"{'='*60}\n")

    _print_json_line(
        args.json,
        ok=exit_code in (0, 3),         # PDF üretildi mi
        qc_ok=exit_code == 0,           # QC eşiği de geçti mi (#9)
        exit_code=exit_code,
        slug=slug,
        book_name=book_name,
        output_dir=str(output_dir),
        pdf=str(final_pdf) if final_pdf else None,
        epub=str(final_epub) if final_epub else None,   # (F37)
        epub_note=epub_note,                             # (S4/S5)
        epub_kapsam=epub_report["kapsam"] if epub_report else None,  # (S7)
        **counts,                                        # (R5)
        **qc,                                            # (S4)
    )

    # (#9) exit 3 artık 0'a ÇEVRİLMEZ: QC eşiği altı (bölüm kaybı /
    # çevrilmemiş içerik olabilir) otomasyona aynen raporlanır. PDF yine
    # üretilmiştir ('ok'/'pdf' alanları bunu söyler); CEVIRI_TALIMATI
    # akışı düzelt-tekrar-derle döngüsüyle ilerler.
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
