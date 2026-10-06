"""Yol A (ajan/LLM çevirisi) koruma regression testleri (2026-06-15).

İki kollu mimari: Yol A = ajan/LLM çevirisi (manual_translation=True), Yol B =
HY-MT 7B. Ortak compile pipeline'ı Yol B için tasarlanmış HY-MT/global-glossary
post-pass'leri içerir. Bunlar manual modda ÇALIŞIRSA ajan çevirisini bozar
(örn. "transfer orbit" -> "aktarım aktarmak", "escape velocity" -> "kacis hızı").

Bu testler:
  1. Sanitizer Marker-artefakt pass'lerini (P1/P2/T5/T7) doğrular.
  2. manual_translation modunda HY-MT post-pass'lerinin ATLANDIĞINI garanti eder
     (guard'lardan biri kaldırılırsa test patlar -> Yol A kilidi).
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from latex_pipeline.sanitizer import LatexSanitizer

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Sanitizer Marker-artefakt pass'leri (P1/P2/T5/T7)
# ---------------------------------------------------------------------------

def _san(text: str) -> str:
    out, _ = LatexSanitizer().sanitize(text)
    return out


def test_p1_spurious_chapter_removed():
    """Marker'ın boş 'Chapter N'/'Bölüm N' sahte chapter'ları silinir."""
    out = _san("\\chapter{Bölüm 1}\n\n\\chapter{İki-Cisim Mekaniği}\n\nMetin.\n")
    assert "\\chapter{Bölüm 1}" not in out
    assert "İki-Cisim Mekaniği" in out


def test_p1_title_chapter_unnumbered():
    """İlk başlık-chapter (ardından \\section gelmeden) numarasız (\\chapter*)."""
    out = _san("\\chapter{Gök Mekaniği}\n\nYazar bilgisi.\n\n"
               "\\chapter{Gerçek Bölüm}\n\n\\section{1.1 Alt}\n\nMetin.\n")
    assert "\\chapter*{Gök Mekaniği}" in out


def test_p2_section_number_prefix_stripped():
    """'\\section{1.1 X}' -> '\\section{X}' (çift numara önlenir)."""
    out = _san("\\section{1.5 Basit Sarkaçta Hareket Denklemi}\n\nMetin.\n")
    assert "\\section{Basit Sarkaçta Hareket Denklemi}" in out
    assert "1.5 Basit" not in out


def test_p2_does_not_strip_legitimate_number():
    """Boşluksuz baş-numara (3D gibi) korunur."""
    out = _san("\\section{3D Görselleştirme}\n\nMetin.\n")
    assert "3D Görselleştirme" in out


def test_t5_celestial_names_in_table_translated():
    """Tablo içinde gök cismi adları Türkçeleşir; tablo DIŞI dokunulmaz değil."""
    tex = ("\\begin{tabular}{l l}\nSun & $1.327$ \\\\\n"
           "Mercury & $0.387$ \\\\\nPluto & $39$ \\\\\n\\end{tabular}\n")
    out = _san(tex)
    assert "Güneş &" in out
    assert "Merkür &" in out
    assert "Plüton &" in out
    assert "Sun &" not in out


def test_t5_mars_unchanged():
    """Mars Türkçede de Mars -> değişmez."""
    out = _san("\\begin{tabular}{l l}\nMars & $1.52$ \\\\\n\\end{tabular}\n")
    assert "Mars &" in out


def test_t7_mangled_footnote_cleaned():
    """Marker bozuk footnote markup -> \\textsuperscript."""
    out = _san("${}^{&}$lt;sup>2</sup>Bir dipnot metni.\n")
    assert "\\textsuperscript{2}" in out
    assert "lt;sup" not in out


def test_math_not_touched_by_passes():
    """Pass'ler matematiğe dokunmaz."""
    tex = "Denklem $\\vec{F} = m\\vec{a}$ ve \\[ E = \\frac{1}{2}mv^2 \\]\n"
    out = _san(tex)
    assert "\\vec{F} = m\\vec{a}" in out
    assert "E = \\frac{1}{2}mv^2" in out


# ---------------------------------------------------------------------------
# Manual-mode HY-MT kontaminasyon guard'ları (Yol A kilidi)
# ---------------------------------------------------------------------------

def test_autofix_manual_skip_includes_mt_checks():
    """AutoFix manual-skip seti MT-çağıran check'leri içermeli."""
    src = open(os.path.join(PROJECT_ROOT, "latex_pipeline", "autofix.py"),
               encoding="utf-8").read()
    # _MANUAL_TRANSLATION_SKIP set'ini bul
    m = re.search(r"_MANUAL_TRANSLATION_SKIP\s*=\s*\{(.*?)\}", src, re.DOTALL)
    assert m, "_MANUAL_TRANSLATION_SKIP bulunamadı"
    skip_block = m.group(1)
    for name in ("untranslated", "untranslated_heading", "mixed_language_line",
                 "glossary_compliance", "wrong_translation"):
        assert f'"{name}"' in skip_block, f"{name} manual-skip'te yok"


def test_pipeline_mt_phases_guarded_by_manual_translation():
    """Phase 5.7/5.75/5.8 (HY-MT/global-glossary post-pass'leri) manual modda
    atlanmalı. Guard'lardan biri kaldırılırsa Yol A bozulur -> bu test patlar.

    Her HY-MT/global-glossary tetikleyicisinin etrafında (guard before/after)
    'not manual_translation' bulunmalı."""
    src = open(os.path.join(PROJECT_ROOT, "translate_book.py"),
               encoding="utf-8").read()
    # Faz comment marker'ları; guard if'i hemen sonrasında gelir.
    for marker in ("Phase 5.7: Second Pass",   # Retranslator (HY-MT 2. geçiş)
                   "Phase 5.75: Kitap sözlüğü doğrulama geçişi",   # kitap sözlüğü (KARAR 25; eskiden genel sözlük)
                   "Targeted Segment Retry"):   # QC<99 segment HY-MT retry
        idx = src.find(marker)
        assert idx != -1, f"{marker} translate_book.py'de bulunamadı"
        window = src[idx:idx + 1000]
        assert "not manual_translation" in window, \
            f"{marker} guard'ında 'not manual_translation' yok (Yol A açık)"
