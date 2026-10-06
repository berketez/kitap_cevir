#!/usr/bin/env python3
"""1_cikar.py — "PDF'den TeX çıkar", Adım 1: kitaptan çevrilecek LaTeX'i çıkar.

Bir PDF/EPUB'ı alır, çevrilmeye hazır LaTeX gövdesini (kitabın kendi
dilinde) ``output/<slug>/EXPORT.tex`` olarak üretir. Kaynak dil İngilizce,
Almanca, Rusça, Fransızca, İspanyolca ya da İtalyanca olabilir (tek kaynak:
``core.lang_detect.TEX_SOURCE_LANGS``; bu liste testle ona bağlıdır). Çeviri
MODELİ / GPU GEREKMEZ — yalnızca çıkarım (PDF için Marker OCR, EPUB için saf
Python) çalışır.

Kullanım:
    python 1_cikar.py kitap.pdf
    python 1_cikar.py kitap.epub --name "Kitap Adı"
    python 1_cikar.py kitap.pdf --device cpu        # Apple Silicon yoksa
    python 1_cikar.py kitap.pdf --yeniden           # önceki çıkarımı kullanma

Çıktı: EXPORT.tex (baştaki ``% KITAPCEVIR-META`` satırları KORUNMALIDIR),
kitap.json (sidecar), CEVIRI_TALIMATI.md (bu kitap için doldurulmuş talimat).

Aynı klasörde önceki bir çıkarım (full_book.md) varsa yeniden kullanılır;
``--yeniden`` onu ``full_book.onceki.md`` adıyla saklayıp kitabı baştan
çıkarır.

--json son satır sözleşmesi (arayüz, S2): her çıkış yolunda aynı alanlar —
ok, error, slug, book_name, output_dir, export_tex, export_tr_tex, directive,
chapters, extraction_reused, export_tr_exists, export_changed, failed_pages.
Hata kodları: input_missing, input_format (uzantı desteklenmiyor ya da dosya
açılamıyor: bozuk PDF/EPUB), name_conflict, extraction_failed, body_missing.

Çıkış kodları: 0 EXPORT.tex hazır · 1 hata (``error`` alanı) · 3 EXPORT.tex
hazır AMA bazı PDF sayfaları çıkarılamadı (``ok`` true, ``failed_pages``
1'den başlayan sayfa numaraları; EXPORT.tex'te bu sayfaların yerinde görünür
yer tutucu satır var — F1-03, 2026-09-29).

Sonraki adım: EXPORT.tex'i çevir → EXPORT_TR.tex kaydet → python 2_derle.py.
Ayrıntı için CEVIRI_TALIMATI.md.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from translate_book import (  # noqa: E402  (yol ekledikten sonra import)
    EXTRACTION_NAME,
    PIPELINE_LOG_NAME,
    PREVIOUS_EXTRACTION_NAME,
    detect_book_type,
    detect_input_format,
    extraction_usable,
    failed_pages_in_markdown,
    guess_book_name,
    input_problem,
    run_epub_extraction,
    run_extraction,
    run_pipeline,
    set_aside_unusable_extraction,
    slug_is_fallback,
    slugify,
)
from yol_a_ortak import (  # noqa: E402
    assistant_prompt,
    build_meta_header,
    meta_body,
    sha1_of_file,
    write_sidecar,
)
from core.lang_detect import TEX_SOURCE_LANGS, language_list_tr  # noqa: E402
from ui.branding import TEX_TITLE  # noqa: E402
# Kitap klasörü veri kökünün output/'undadır (TASARIM F1): paketli kipte
# KITAPCEVIR_VERI_KOKU/output, geliştirici kipinde PROJECT_ROOT/output.
from config.paths import cikti_koku  # noqa: E402

# Çıkarımın ara dosyası (EXTRACTION_NAME) ve --yeniden ile saklanan önceki
# sürümü (PREVIOUS_EXTRACTION_NAME) translate_book'tan gelir (tek kaynak).

# --json son satırının alan kümesi (S2): çıkış yolundan bağımsız SABİT.
# failed_pages (F1-03): çıkarılamayan PDF sayfaları (1'den başlar; yoksa []).
_JSON_FIELDS = (
    "ok", "error", "slug", "book_name", "output_dir", "export_tex",
    "export_tr_tex", "directive", "chapters", "extraction_reused",
    "export_tr_exists", "export_changed", "failed_pages",
)

# F1-03 (2026-09-29): EXPORT.tex üretildi ama bazı PDF sayfaları çıkarılamadı
# (2_derle'nin "PDF üretildi ama QC eşiğin altında" koduyla aynı anlam
# ailesi: ürün var, eksik var). JSON'da ok true + failed_pages dolu.
EXIT_PAGES_FAILED = 3

# F1-11: önceki denemenin kaynak kopyası, deneme hiçbir çıkarım
# üretmediyse (klasörde korunacak iş yoksa) bu ek adla saklanır.
_FAILED_ATTEMPT_SUFFIX = ".onceki"


def _count_chapters(body_tex: str) -> int:
    r"""``\chapter{...}`` + ``\part{...}`` sayısını kaba say (metadata için)."""
    return body_tex.count(r"\chapter{") + body_tex.count(r"\part{")


def _print_json_line(enabled: bool, **fields) -> None:
    """--json sözleşmesi: HER çıkış yolunda son satır aynı alanlarla JSON.

    Bilinmeyen/üretilmeyen alan null, bayraklar False olarak basılır.
    """
    if not enabled:
        return
    payload = {key: None for key in _JSON_FIELDS}
    payload.update({"ok": False, "extraction_reused": False,
                    "export_tr_exists": False, "failed_pages": []})
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False))


def _conflict_message(output_dir: Path, book_name: str, input_arg: str) -> str:
    """Ad çakışması iletisi: aynı adla BAŞKA bir kitap çıkarılmış (S2)."""
    return (
        f"HATA: '{book_name}' adıyla daha önce BAŞKA bir kitap çıkarılmış "
        f"({output_dir}).\n"
        f"      Oradaki çalışmanın üzerine yazılmaması için durduruldu; "
        f"hiçbir dosyaya dokunulmadı.\n"
        f"      Bu kitap için farklı bir kitap adı seç (komut satırında "
        f"--name):\n"
        f"      python 1_cikar.py {shlex.quote(input_arg)} "
        f"--name {shlex.quote(book_name + ' 2')}"
    )


def _emit_directive(output_dir: Path, slug: str, export_tex: Path,
                    export_tr_tex: Path, source_copy: Path | None = None,
                    book_name: str | None = None,
                    failed_pages: list[int] | None = None) -> Path | None:
    """Kök CEVIRI_TALIMATI.md'yi bu kitaba özel, tam yollarla kopyala (S6).

    Başa bu kitabın gerçek yollarını ve her klasörden çalışan derleme
    komutunu içeren bir başlık eklenir; gövdedeki ``output/<slug>`` kalıbı
    kitabın mutlak klasörüyle, kalan ``<slug>`` ise kitabın adıyla
    doldurulur. Kopya, yapay zekâ asistanına verilecek giriş noktasıdır:
    Adım 1 (çıkarım) yapılmıştır. Kök şablon yoksa None döner (çekirdek akış
    yine çalışır). ``{{...}}`` token'ları varsa doldurulur (ileri uyum).
    """
    root = PROJECT_ROOT / "CEVIRI_TALIMATI.md"
    if not root.exists():
        return None
    out_str = str(output_dir)
    body = root.read_text(encoding="utf-8")
    body = (
        body.replace("{{SLUG}}", slug)
            .replace("{{OUTPUT_DIR}}", out_str)
            .replace("{{EXPORT_TEX}}", str(export_tex))
            .replace("{{EXPORT_TR_TEX}}", str(export_tr_tex))
            .replace("output/<slug>", out_str)
            .replace("<slug>", slug)
    )
    compile_cmd = " ".join(shlex.quote(part) for part in (
        sys.executable, str(PROJECT_ROOT / "2_derle.py"), str(export_tr_tex)))
    lines = [
        f"# Bu kitap için çeviri talimatı — {book_name or slug}",
        "",
        "Bu dosya `1_cikar.py` tarafından bu kitap için üretildi. Aşağıdaki "
        "genel talimattaki kitap klasörü yolları bu kitabın klasörüyle "
        "doldurulmuştur.",
        "",
        f"- Proje klasörü: `{PROJECT_ROOT}`",
        f"- Kitap klasörü: `{out_str}`",
    ]
    if source_copy is not None:
        lines.append(f"- Kaynak kitabın kopyası: `{source_copy}`")
    lines += [
        f"- Çevrilecek LaTeX: `{export_tex}`",
        f"- Çeviriyi buraya kaydet: `{export_tr_tex}`",
        "- Derleme komutu (her klasörden çalışır):",
        "",
        "  ```bash",
        f"  {compile_cmd}",
        "  ```",
        "",
        "**Adım 1 (çıkarım) bu kitap için YAPILDI — Adım 2'den başla.** "
        "Kitabı yeniden çıkarma; kaynak dosyaya ihtiyacın yok.",
        "",
    ]
    if failed_pages:
        # F1-03: çıkarılamayan sayfalar çevirmene de görünür.
        pages = ", ".join(str(n) for n in failed_pages)
        lines += [
            f"**UYARI — çıkarılamayan sayfalar:** PDF sayfa {pages}. "
            "EXPORT.tex'te bu sayfaların yerinde `[ÇIKARILAMAYAN SAYFA: "
            "PDF sayfa N …]` satırı var. O satırı SİLME ve çevirme; "
            "sayfanın metni kaynak PDF'de duruyor, istenirse elle eklenir.",
            "",
        ]
    lines += [
        "---",
        "",
        "",
    ]
    dest = output_dir / "CEVIRI_TALIMATI.md"
    dest.write_text("\n".join(lines) + body, encoding="utf-8")
    return dest


def _folder_has_work(output_dir: Path) -> bool:
    """Klasörde korunması gereken bir çalışma var mı (F1-11)?

    Kullanılabilir çıkarım, saklanmış önceki çıkarım, EXPORT/EXPORT_TR,
    LaTeX gövdesi ya da çeviri. Hiçbiri yoksa önceki deneme hiçbir şey
    üretmemiştir (bozuk dosya, düşen çıkarım): aynı adla düzeltilmiş dosya
    ad çakışması sayılmaz.
    """
    if extraction_usable(output_dir / EXTRACTION_NAME):
        return True
    for rel in (PREVIOUS_EXTRACTION_NAME, "EXPORT.tex", "EXPORT_TR.tex",
                "source/body.tex", "source/translated_body.tex"):
        if (output_dir / rel).exists():
            return True
    compiled = output_dir / "compiled"
    return compiled.is_dir() and any(compiled.glob("*.pdf"))


def build_parser() -> argparse.ArgumentParser:
    """Komut satırı ayrıştırıcısı (dil listesi TEX_SOURCE_LANGS'ten)."""
    parser = argparse.ArgumentParser(
        description=f"{TEX_TITLE} · Adım 1: kitaptan çevrilecek EXPORT.tex'i "
                    "çıkar (çeviri modeli gerekmez; kaynak dil "
                    f"{language_list_tr(TEX_SOURCE_LANGS, ' ya da ')} "
                    "olabilir).",
    )
    parser.add_argument("input", help="Kaynak dosya (.pdf veya .epub)")
    parser.add_argument("--name", default=None,
                        help="Kitap adı (çıktı klasörü/PDF adı; varsayılan: "
                             "dosya adından otomatik). Aynı adla farklı "
                             "kitap varsa ayırmak için kullan.")
    parser.add_argument("--device", default="mps", choices=["mps", "cpu", "cuda"],
                        help="Çıkarım cihazı (Marker OCR). Apple Silicon yoksa "
                             "'cpu' (varsayılan: mps).")
    parser.add_argument("--batch-size", type=int, default=20,
                        help="PDF çıkarım parti boyutu (varsayılan: 20).")
    parser.add_argument("--yeniden", action="store_true",
                        help=f"Klasördeki önceki çıkarımı ({EXTRACTION_NAME}) "
                             "kullanma; kitabı baştan çıkar (eski dosya "
                             f"{PREVIOUS_EXTRACTION_NAME} adıyla saklanır).")
    parser.add_argument("--json", action="store_true",
                        help="Son satırda makine-okunur JSON bas (arayüz/otomasyon).")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    input_path = str(Path(args.input).resolve())
    if not Path(input_path).exists():
        print(f"HATA: Dosya bulunamadı: {input_path}")
        _print_json_line(args.json, error="input_missing")
        return 1

    try:
        input_format = detect_input_format(input_path)
    except ValueError as exc:
        print(f"HATA: {exc}")
        _print_json_line(args.json, error="input_format")
        return 1
    # F1-11 (2026-09-29): bozuk dosya klasöre hiçbir şey yazılmadan reddedilir
    # (eskiden ham traceback, JSON yok ve klasörde bozuk kaynağın kopyası
    # kalıyordu; düzeltilmiş dosya sonra "name_conflict" alıyordu).
    # JSON kodu input_format: arayüzün mevcut "Kitap dosyası tanınmadı"
    # iletisi; kesin neden HATA satırında.
    problem = input_problem(input_path, input_format)
    if problem:
        print(f"HATA: {problem}")
        _print_json_line(args.json, error="input_format")
        return 1

    book_name = args.name or guess_book_name(input_path)
    slug = slugify(book_name)
    if slug_is_fallback(book_name):
        # F5-05: yalnız noktalama işaretinden oluşan ad boş klasör adı
        # veriyordu (kitap output/ köküne yazılıyordu).
        print(f"UYARI: Kitap adı ({book_name!r}) harf ya da rakam içermiyor; "
              f"klasör adı olarak '{slug}' kullanıldı.")
    output_dir = cikti_koku(PROJECT_ROOT) / slug
    export_path = output_dir / "EXPORT.tex"
    export_tr_path = output_dir / "EXPORT_TR.tex"
    known = {
        "slug": slug, "book_name": book_name, "output_dir": str(output_dir),
        "export_tex": str(export_path), "export_tr_tex": str(export_tr_path),
    }

    # --- Çakışma guard'ı: aynı ad, farklı kitap? --------------------------
    # Klasöre (yoksa oluşturulmadan) hiçbir şey yazılmadan önce denetlenir.
    source_sha1 = sha1_of_file(input_path)
    sidecar_path = output_dir / "kitap.json"
    dest_name = "source_book.pdf" if input_format == "pdf" else "source_book.epub"
    dest_src = output_dir / dest_name
    conflict = False
    if sidecar_path.exists():
        try:
            prev = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prev = {}
        prev_sha1 = prev.get("source_sha1")
        conflict = bool(prev_sha1) and prev_sha1 != source_sha1
    # (#4) Sidecar YOKKEN de kimlik doğrulanır: Yol B ile açılmış ya da
    # yarım kalmış aynı slug'lı dizinde eski kaynak dosya durabilir —
    # eskiden guard susuyor, eski kitabın full_book.md'si YENİ kitabın
    # EXPORT.tex'i oluyordu.
    elif dest_src.exists():
        conflict = sha1_of_file(dest_src) != source_sha1
    if conflict and not _folder_has_work(output_dir):
        # F1-11: önceki deneme hiçbir çıkarım üretmedi (bozuk dosya, düşen
        # çıkarım). Aynı adla düzeltilmiş dosya reddedilmez; eski kaynak
        # kopyası silinmez, ek adla saklanır.
        kept = None
        if dest_src.exists():
            kept = dest_src.with_name(
                dest_src.stem + _FAILED_ATTEMPT_SUFFIX + dest_src.suffix)
            dest_src.replace(kept)
        print(f"UYARI: '{book_name}' adıyla önceki deneme çıkarım "
              f"üretmemişti; bu dosyayla baştan başlanıyor"
              + (f" (önceki kaynak kopyası {kept.name} adıyla saklandı)."
                 if kept else "."))
        conflict = False
    if conflict:
        print(_conflict_message(output_dir, book_name, args.input))
        _print_json_line(args.json, error="name_conflict",
                         export_tr_exists=export_tr_path.exists(), **known)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    full_md = output_dir / EXTRACTION_NAME
    previous_md = output_dir / PREVIOUS_EXTRACTION_NAME
    if (not args.yeniden and not sidecar_path.exists()
            and not dest_src.exists() and full_md.exists()):
        print(
            f"UYARI: '{output_dir.name}' klasöründe kimliği doğrulanamayan "
            f"eski bir çıkarım ({EXTRACTION_NAME}) var. Farklı bir kitapsa "
            f"--name ile ayır; aynı kitapsa devam edilebilir (baştan "
            f"çıkarmak için --yeniden)."
        )

    # --- Kaynağı output dizinine kopyala (kapak/preflight için yerelde dursun)
    if not dest_src.exists():
        shutil.copy2(input_path, dest_src)

    # (#4) Sidecar'ı ERKEN yaz: yarıda kesilen koşular da kimlik bıraksın
    # (tam sidecar, chapters/book_type ile birlikte sonda yeniden yazılır).
    book_type = detect_book_type(input_path)
    write_sidecar(output_dir, {
        "slug": slug,
        "book_name": book_name,
        "output_dir": str(output_dir),
        "source_format": input_format,
        "source_sha1": source_sha1,
        "book_type": book_type,
        "created": datetime.now().isoformat(timespec="seconds"),
    })

    print(f"\n{'='*60}")
    print(f"  {TEX_TITLE} · Adım 1 (çıkarım)")
    print(f"  Kitap:  {book_name}")
    print(f"  Format: {input_format.upper()}")
    print(f"  Çıktı:  {output_dir}")
    print(f"{'='*60}\n")

    # --- Çıkarım (full_book.md zaten varsa yeniden kullanılır) -------------
    # S2 (2026-09-25, U10): --yeniden önceki çıkarımı full_book.onceki.md
    # adıyla saklar ve kitabı baştan çıkarır; kullanıldığında bunu JSON'da
    # (extraction_reused) ve bir uyarı satırında söyler.
    moved_previous = False
    if args.yeniden and full_md.exists():
        full_md.replace(previous_md)
        moved_previous = True
        print(f"--yeniden: önceki çıkarım {PREVIOUS_EXTRACTION_NAME} adıyla "
              f"saklandı; kitap baştan çıkarılıyor.")
    # F1-11 (2026-09-29): boş/neredeyse boş full_book.md (eski sürümlerin
    # başarısız çıkarımı) yeniden KULLANILMAZ; silinmez, kenara alınır.
    set_aside = set_aside_unusable_extraction(output_dir)
    if set_aside is not None:
        print(f"UYARI: Klasördeki önceki çıkarım ({EXTRACTION_NAME}) "
              f"kullanılamaz, neredeyse boş ({set_aside.name} adıyla "
              f"saklandı); kitap baştan çıkarılıyor.")
    extraction_reused = full_md.exists()
    if extraction_reused:
        print(f"UYARI: Bu klasördeki önceki çıkarım ({EXTRACTION_NAME}) "
              f"yeniden kullanıldı; kitap baştan çıkarılmadı. Baştan "
              f"çıkarmak için --yeniden ver.")
    else:
        if input_format == "pdf":
            ok = run_extraction(input_path, str(output_dir),
                                args.batch_size, args.device)
        else:
            ok = run_epub_extraction(input_path, str(output_dir))
        if not ok:
            print(f"HATA: Çıkarım başarısız. Ayrıntı için "
                  f"{output_dir / PIPELINE_LOG_NAME} dosyasına bak.")
            if moved_previous and previous_md.exists():
                previous_md.replace(full_md)
                print(f"      Önceki çıkarım geri yüklendi ({EXTRACTION_NAME}).")
            _print_json_line(args.json, error="extraction_failed",
                             export_tr_exists=export_tr_path.exists(),
                             **known)
            return 1

    # F1-03 (2026-09-29): çıkarılamayan PDF sayfaları — tek kaynak
    # full_book.md'deki görünür yer tutucular (yeniden kullanılan çıkarımda
    # da geçerli; metin elle eklenince uyarı kalkar).
    try:
        failed_pages = failed_pages_in_markdown(
            full_md.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        failed_pages = []

    # --- Markdown → LaTeX gövdesi (body.tex), çeviri+derleme YOK -----------
    pdf_path = input_path if input_format == "pdf" else None
    exit_code = run_pipeline(
        str(output_dir), book_name,
        skip_translate=True, skip_compile=True,
        pdf_path=pdf_path, resume=False, manual_translation=False,
    )

    body_path = output_dir / "source" / "body.tex"
    if exit_code not in (0, 3) or not body_path.exists() or body_path.stat().st_size <= 100:
        print(f"\nHATA: body.tex üretilemedi (exit={exit_code}). "
              f"Ayrıntı için {output_dir / PIPELINE_LOG_NAME} dosyasına bak.")
        _print_json_line(args.json, error="body_missing",
                         extraction_reused=extraction_reused,
                         export_tr_exists=export_tr_path.exists(),
                         failed_pages=failed_pages, **known)
        return 1

    # --- EXPORT.tex = kimlik başlığı + gövde -------------------------------
    body_tex = body_path.read_text(encoding="utf-8")
    chapters = _count_chapters(body_tex)
    meta = {
        "slug": slug,
        "book_name": book_name,
        "output_dir": str(output_dir),
        "source_format": input_format,
        "source_sha1": source_sha1,
        "body_chapters": chapters,
        # (#6) 1_cikar'in tespit ettigi tur 2_derle'ye tasinir — 2_derle
        # source_book.pdf adından tespit edemeyip 'science'a düşüyordu.
        "book_type": book_type,
        "created": datetime.now().isoformat(timespec="seconds"),
        # F1-03: çıkarılamayan PDF sayfaları (1'den; yoksa boş liste).
        "failed_pages": failed_pages,
    }
    # S2 (U10): EXPORT_TR.tex dururken EXPORT.tex yeniden yazılıyorsa bunu
    # söyle; çeviri eski EXPORT.tex'e göre yapılmış olabilir. İçeriğin
    # değişip değişmediği META başlığı hariç gövdeyle ölçülür.
    export_tr_exists = export_tr_path.exists()
    export_changed = None
    if export_path.exists():
        try:
            old_body = meta_body(export_path.read_text(encoding="utf-8",
                                                       errors="replace"))
            export_changed = old_body != body_tex
        except OSError:
            export_changed = None
    export_path.write_text(build_meta_header(meta) + body_tex, encoding="utf-8")
    if export_tr_exists:
        if export_changed is False:
            detail = "içeriği değişmedi."
        elif export_changed:
            detail = ("içeriği DEĞİŞTİ; çeviri önceki EXPORT.tex'ten "
                      "yapıldıysa derlemeden önce bölüm yapısını karşılaştır.")
        else:
            detail = ("önceki EXPORT.tex yoktu, çevirinin hangi sürümden "
                      "yapıldığı bilinmiyor; derlemeden önce bölüm yapısını "
                      "karşılaştır.")
        print(f"UYARI: Bu klasörde bir çeviri ({export_tr_path.name}) var ve "
              f"EXPORT.tex yeniden yazıldı; {detail} "
              f"{export_tr_path.name}'e dokunulmadı.")

    # --- Sidecar + bu kitap için talimat kopyası ---------------------------
    write_sidecar(output_dir, meta)
    directive = _emit_directive(output_dir, slug, export_path, export_tr_path,
                                source_copy=dest_src, book_name=book_name,
                                failed_pages=failed_pages)

    # --- Sonraki-adım rehberi ---------------------------------------------
    size_kb = export_path.stat().st_size / 1024
    print(f"\n{'='*60}")
    if failed_pages:
        print(f"  ⚠ EXPORT.tex hazır ama {len(failed_pages)} sayfa "
              f"çıkarılamadı: {export_path}  ({chapters} bölüm, "
              f"{size_kb:.0f} KB)")
        print(f"    UYARI: Çıkarılamayan PDF sayfaları: "
              f"{', '.join(str(n) for n in failed_pages)}. EXPORT.tex'te bu "
              f"sayfaların yerinde görünür yer tutucu satır var; metni kaynak "
              f"PDF'den elle ekle ya da --yeniden ile yeniden dene.")
        print(f"    Günlük:           {output_dir / PIPELINE_LOG_NAME}")
    else:
        print(f"  ✓ EXPORT.tex hazır: {export_path}  ({chapters} bölüm, "
              f"{size_kb:.0f} KB)")
    if directive:
        print(f"    Talimat:          {directive}")
    print(f"{'='*60}")
    print("  Sıradaki adım:")
    print(f"    1) EXPORT.tex'i çevir → {export_tr_path.name} olarak KAYDET")
    print("       ($matematik$, \\cite/\\ref/\\label, figürler ve baştaki")
    print("        % KITAPCEVIR-META satırları AYNEN KORUNUR)")
    print(f"    2) python 2_derle.py {export_tr_path}")
    print("       → çevrilmiş PDF derlenir (model/GPU gerekmez).")
    if directive:
        print("  Claude Code kullanıyorsan proje klasöründe şunu yaz:")
        print(f"    {assistant_prompt(directive)}")
    print(f"{'='*60}\n")

    _print_json_line(
        args.json,
        ok=True,
        directive=str(directive) if directive else None,
        chapters=chapters,
        extraction_reused=extraction_reused,
        export_tr_exists=export_tr_exists,
        export_changed=export_changed,
        failed_pages=failed_pages,
        **known,
    )
    return EXIT_PAGES_FAILED if failed_pages else 0


if __name__ == "__main__":
    sys.exit(main())
