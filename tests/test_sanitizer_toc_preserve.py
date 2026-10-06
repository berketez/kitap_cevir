"""FIX 7 düzeltmesi (2026-07-06 sutton smoke): meşru TOC korunur.

Eski `_remove_stray_toc` KOŞULSUZ siliyordu; oysa converter ön madde →
ana madde geçişinde ``\\tableofcontents`` + ``\\mainmatter`` çiftini
BİLİNÇLİ üretir (converter.py) ve template'te ayrıca TOC yoktur. Sonuç:
derlenen kitapta otomatik İçindekiler hiç oluşmuyordu. Yeni davranış:
yalnızca ``\\mainmatter`` izlemeyen (gerçekten başıboş) olanlar silinir.
"""

from latex_pipeline.sanitizer import LatexSanitizer, SanitizeReport


def _clean(body: str) -> str:
    sanitizer = LatexSanitizer(source_lang="en", target_lang="tr")
    return sanitizer._remove_stray_toc(body, SanitizeReport())


class TestTocMainmatterPairPreserved:
    def test_legit_pair_preserved(self):
        body = (
            "önsöz metni\n\n"
            "\\tableofcontents\n\n"
            "\\mainmatter\n\n"
            "\\chapter{Giriş}\n"
        )
        out = _clean(body)
        assert "\\tableofcontents" in out
        assert "\\mainmatter" in out

    def test_stray_at_end_removed(self):
        body = "\\chapter{Giriş}\nmetin paragrafı\n\n\\tableofcontents\n"
        out = _clean(body)
        assert "\\tableofcontents" not in out

    def test_mixed_keeps_pair_removes_stray(self):
        body = (
            "\\tableofcontents\n\\mainmatter\n\\chapter{Giriş}\n"
            "metin\n\\tableofcontents\n"
        )
        out = _clean(body)
        assert out.count("\\tableofcontents") == 1
        assert "\\mainmatter" in out

    def test_different_command_untouched(self):
        # Negatif lookahead: \tableofcontents ile başlayan FARKLI komutlara
        # dokunulmaz
        body = "\\tableofcontentsline{deneme}\n"
        out = _clean(body)
        assert "\\tableofcontentsline" in out
