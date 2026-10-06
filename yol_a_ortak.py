#!/usr/bin/env python3
"""Yol A ortak yardımcıları — 1_cikar.py ve 2_derle.py paylaşır.

Bu bir KULLANICI giriş noktası DEĞİLDİR; iki wrapper script'in ortak
mantığını (kimlik başlığı, sidecar, kitap-dizini çözünürlüğü) tek yerde
tutar. Böylece ``% KITAPCEVIR-META`` başlık formatı ve çözünürlük kuralı
iki dosyada kopyalanmaz (tutarlılık).

Kimlik neden dosyanın İÇİNE gömülür: kullanıcı çevrilmiş .tex'i başka bir
klasöre kopyalar/yeniden adlandırırsa, yanındaki ``kitap.json`` sidecar'ı
kopar. LaTeX yorumu (``%`` ile başlayan satır) derlemede tamamen görünmez
olduğu için kimliği güvenle dosyaya gömebiliriz — dosya nereye giderse
kimlik onunla gider.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Kitap klasörlerinin kökü veri kökünden türer (TASARIM F1): paketli kipte
# KITAPCEVIR_VERI_KOKU/output, geliştirici kipinde <project_root>/output.
from config.paths import cikti_koku

META_VERSION = 1
META_BEGIN = "% ==== KITAPCEVIR-META v1 ===="
META_END = "% ============================"
# Başlıktaki uyarı satırı. Yalnız insan içindir: parse_meta_header yalnız
# _META_FIELDS anahtarlarını okur, bu satırın metnine BAĞLI DEĞİLDİR —
# eski EXPORT.tex'lerdeki ASCII biçimi ("Bu satirlari SILME/DEGISTIRME")
# de aynen okunur (2026-09-25, U16: metin doğru Türkçe karakterle).
META_NOTE = ("% NOT: Bu satırları SİLME/DEĞİŞTİRME — 2_derle.py kitabı "
             "bulmak için kullanır.")
SIDECAR_NAME = "kitap.json"

# Başlıkta taşınan alanlar (makine anahtarları İngilizce — kod sembolü kuralı).
_META_FIELDS = (
    "slug", "book_name", "output_dir", "source_format",
    "source_sha1", "body_chapters", "book_type", "created",
)


def sha1_of_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """Bir dosyanın SHA1'ini akışla (bellek dostu) hesapla."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def build_meta_header(meta: dict) -> str:
    """``% KITAPCEVIR-META`` başlık bloğunu üret (sonunda tek boş satır)."""
    lines = [META_BEGIN]
    for key in _META_FIELDS:
        val = meta.get(key, "")
        lines.append(f"% {key}: {val}")
    lines.append(META_NOTE)
    lines.append(META_END)
    return "\n".join(lines) + "\n\n"


def parse_meta_header(tex_path: str | Path) -> dict:
    """Bir .tex dosyasının başındaki ``% KITAPCEVIR-META`` bloğunu oku.

    Blok yoksa boş sözlük döner. Tarama sınırlı tutulur (büyük dosya
    tümüyle okunmaz): en fazla ~300 satır VE ilk gerçek içerik satırına
    (yorum/boş olmayan) kadar bakılır.

    (A-13, 2026-07-27) Sağlamlaştırma: UTF-8 BOM'lu dosyada blok
    bulunamıyordu (``utf-8-sig`` ile açılır); ayrıca çevirmen/LLM başa 60+
    satır yorum eklediğinde eski sabit 60 satır sınırı bloğu kaçırıyordu.
    """
    meta: dict = {}
    in_block = False
    try:
        with open(tex_path, "r", encoding="utf-8-sig", errors="replace") as f:
            for i, raw in enumerate(f):
                if i > 300:
                    break
                line = raw.rstrip("\n")
                if line.startswith(META_BEGIN):
                    in_block = True
                    continue
                if in_block and line.startswith(META_END):
                    break
                if in_block and line.startswith("%"):
                    body = line[1:].strip()
                    if ":" in body:
                        key, _, val = body.partition(":")
                        key = key.strip()
                        if key in _META_FIELDS:
                            meta[key] = val.strip()
                    continue
                # Blok dışında gerçek içerik başladıysa (yorum/boş değil)
                # başlık artık gelmez — taramayı bitir.
                if not in_block and line.strip() and not line.lstrip().startswith("%"):
                    break
    except OSError:
        return {}
    return meta


def meta_body(tex: str) -> str:
    """EXPORT/EXPORT_TR metninden ``% KITAPCEVIR-META`` bloğunu at, gövdeyi
    döndür (blok yoksa metnin kendisi). build_meta_header'ın ürettiği
    biçime göre: blok ``META_END`` satırı ve ardından bir boş satırla biter.

    1_cikar.py EXPORT.tex'i yeniden yazarken içeriğin değişip değişmediğini
    bununla ölçer (başlıktaki ``created`` her koşuda değişir).
    """
    if not tex.startswith(META_BEGIN):
        return tex
    end = tex.find(META_END)
    if end < 0:
        return tex
    rest = tex[end + len(META_END):]
    if rest.startswith("\n\n"):
        return rest[2:]
    return rest.lstrip("\n")


def assistant_prompt(directive_path: str | Path) -> str:
    """Kitaba özel talimatı (output/<ad>/CEVIRI_TALIMATI.md) yapay zekâ
    asistanına (Claude Code vb.) vermek için kopyalanabilir istem (S6).

    1_cikar.py sonunda basar; arayüz aynı metni gösterebilir (tek kaynak).
    """
    return (f"{Path(directive_path)} dosyasını oku ve oradaki adımlarla "
            f"bu kitabı Türkçeye çevir.")


def write_sidecar(output_dir: str | Path, meta: dict) -> Path:
    """``output/<slug>/kitap.json`` sidecar'ını yaz ve yolunu döndür."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / SIDECAR_NAME
    path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def read_sidecar(directory: str | Path) -> dict:
    """Bir dizindeki ``kitap.json``'u oku (yoksa/bozuksa boş sözlük)."""
    path = Path(directory) / SIDECAR_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_book_dir(
    tex_path: str | Path,
    project_root: Path,
    explicit_output_dir: str | None = None,
) -> dict:
    """Çevrilmiş .tex'ten kitabın output dizinini ve kimliğini çöz.

    Öncelik sırası (ilk eşleşen kazanır):
      1. ``explicit_output_dir`` (--output-dir bayrağı)
      2. Dosyaya gömülü ``% KITAPCEVIR-META`` başlığı (slug → output/<slug>,
         yoksa başlıktaki mutlak output_dir)
      3. Kardeş ``kitap.json`` sidecar'ı
      4. Dizin-adı geleneği (.tex'in parent'ının adı == output/<slug>)

    Döner: ``{"output_dir", "slug", "book_name", "source": <hangi kural>,
    "tried": [...]}`` — ``output_dir`` None ise çözülemedi (``tried`` mesajı
    kullanıcıya gösterilir).

    ``output/<slug>`` veri kökünün altındadır (``config.paths.cikti_koku``):
    ``KITAPCEVIR_VERI_KOKU`` doluysa oradan, değilse ``project_root``'tan.
    """
    tex_path = Path(tex_path).resolve()
    tried: list[str] = []
    output_root = cikti_koku(project_root)

    def _sha_conflict(header_meta: dict, directory: Path) -> bool:
        """Basliktaki source_sha1 ile hedef dizindeki sidecar sha1'i
        karsilastir. Ikisi de doluysa ve FARKLIYSA bu dizin BASKA bir
        kitaba ait demektir (#3: ayni slug'li farkli kitaba sessiz
        derleme). Biri eksikse dogrulanamaz — engelleme yok (False)."""
        h = (header_meta.get("source_sha1") or "").strip()
        if not h:
            return False
        s = (read_sidecar(directory).get("source_sha1") or "").strip()
        return bool(s) and s != h

    def _pack(output_dir: Path, meta: dict, source: str) -> dict:
        slug = meta.get("slug") or Path(output_dir).name
        book_name = meta.get("book_name") or slug.replace("_", " ")
        return {
            "output_dir": str(Path(output_dir).resolve()),
            "slug": slug,
            "book_name": book_name,
            "source": source,
            "tried": tried,
        }

    # 1) Açık bayrak
    if explicit_output_dir:
        d = Path(explicit_output_dir).resolve()
        tried.append(f"--output-dir: {d}")
        if d.is_dir():
            side = read_sidecar(d)
            return _pack(d, side, "flag")
        # Var olmayan dizin verildiyse yine de kullan (kullanıcı bilerek
        # verdi) — ama SESSİZCE değil (A-15, 2026-07-27): göreli yol CWD'ye
        # göre çözüldüğü için yazım hatası fark edilmeden yanlış yere
        # derlenebiliyordu. Uyarı bas, davranışı koru.
        print(f"UYARI: --output-dir mevcut bir dizin değil: {d}")
        print("       (göreli yol verdiyseniz CWD'ye göre çözüldü; yazım "
              "hatası değilse devam edilir)")
        return _pack(d, {}, "flag")

    # 2) Gömülü başlık
    meta = parse_meta_header(tex_path)
    if meta.get("slug"):
        cand = output_root / meta["slug"]
        tried.append(f"gömülü başlık slug → {cand}")
        if cand.is_dir():
            if not _sha_conflict(meta, cand):
                return _pack(cand, meta, "header-slug")
            tried.append(
                f"  ✗ {cand} atlandı: sha1 uyuşmadı (dizinde FARKLI kitap "
                f"var; bilinçli override için --output-dir kullan)"
            )
        # slug dizini gitmiş/uyuşmadı; başlıktaki mutlak yolu dene
        if meta.get("output_dir"):
            abs_dir = Path(meta["output_dir"])
            tried.append(f"gömülü başlık output_dir → {abs_dir}")
            if abs_dir.is_dir():
                if not _sha_conflict(meta, abs_dir):
                    return _pack(abs_dir, meta, "header-abspath")
                tried.append(f"  ✗ {abs_dir} atlandı: sha1 uyuşmadı")

    # 3) Kardeş sidecar
    # (A-F13, 2026-07-28) Bu dal da sha1 kimlik kontrolünden geçer. Eskiden
    # geçmiyordu: kural 2 "sha1 uyuşmadı" diye REDDETTİĞİ dizini kural 3
    # hemen ardından kabul ediyordu (kullanıcı EXPORT_TR.tex'i output/<slug>/
    # içine koyduğunda kardeş sidecar = o dizinin sidecar'ı). Sonuç: aynı
    # slug'lı farklı kitabın translated_body.tex'i sessizce eziliyordu (#3'ün
    # kapattığı senaryo). Çakışmada kural 4'e düşülür; o da reddederse
    # output_dir None döner ve kullanıcı `tried` listesini görür.
    side = read_sidecar(tex_path.parent)
    if side.get("slug") or side.get("output_dir"):
        od = side.get("output_dir") or str(output_root / side["slug"])
        tried.append(f"kardeş kitap.json → {od}")
        if Path(od).is_dir():
            if not _sha_conflict(meta, Path(od)):
                return _pack(Path(od), side, "sidecar")
            tried.append(
                f"  ✗ {od} atlandı: sha1 uyuşmadı (dizinde FARKLI kitap "
                f"var; bilinçli override için --output-dir kullan)"
            )

    # 4) Dizin-adı geleneği
    conv = output_root / tex_path.parent.name
    tried.append(f"dizin-adı geleneği → {conv}")
    if conv.is_dir():
        if not _sha_conflict(meta, conv):
            return _pack(conv, read_sidecar(conv), "convention")
        tried.append(f"  ✗ {conv} atlandı: sha1 uyuşmadı")

    return {
        "output_dir": None,
        "slug": None,
        "book_name": None,
        "source": None,
        "tried": tried,
    }
