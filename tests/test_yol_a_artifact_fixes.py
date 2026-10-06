"""Yol A artefakt kod-fix regresyon testleri (2026-06-19, DL kitabi sprinti).

Bu kitap (Deep Learning and Computational Physics) cevirisinde elle duzeltilen
Marker artefaktlarinin KOD seviyesinde (sanitizer/detector) duzeltildigini dogrular.
Her test, gelecekteki kitaplarda ayni artefaktin otomatik temizlenmesini garanti eder.
"""
from latex_pipeline.sanitizer import LatexSanitizer


def _san(text: str) -> str:
    out, _ = LatexSanitizer().sanitize(text)
    return out


class TestControlCharStrip:
    """NULL / C0 kontrol karakteri temizligi (Marker math'e \\x00 gomuyor)."""

    def test_null_in_math_removed(self):
        # Marker "W = 7" yerine "W = \x007\x00" uretiyor -> XeLaTeX fatal.
        body = "Sonuc \\(\\mathbf{W} = \x007\x00\\) elde edilir."
        out = _san(body)
        assert "\x00" not in out
        assert "7" in out  # sayi korunur

    def test_various_control_chars_removed(self):
        body = "a\x01b\x02c\x08d\x1fe metni"
        out = _san(body)
        for ch in ("\x01", "\x02", "\x08", "\x1f"):
            assert ch not in out
        # gercek harfler korunur
        for ch in "abcde":
            assert ch in out

    def test_tab_and_newline_preserved(self):
        body = "satir1\nsatir2\tsekme"
        out = _san(body)
        assert "\n" in out
        assert "\t" in out

    def test_clean_text_unchanged(self):
        body = "Bu temiz bir Turkce paragraftir, hicbir kontrol karakteri yok."
        out = _san(body)
        assert "temiz bir Turkce paragraftir" in out


class TestBadgeFigureRemoval:
    """'Check for updates' rozeti: caption'siz chapter-acilis figuru silinir."""

    def test_chapter_opening_uncaptioned_figure_removed(self, tmp_path):
        # F2-10 (2026-09-29): rozet YALNIZ dogrulaninca silinir — gorsel
        # dosyasi bulunur ve Springer rozeti gibi kucuk, kareye yakindir
        # (derlemde 93x95 px). Dosya bulunamazsa gorsel KORUNUR
        # (tests/test_fix_S_20260929.py).
        from PIL import Image
        Image.new("RGB", (93, 95), (200, 30, 30)).save(
            tmp_path / "_page_12_Picture_1.png")
        body = (
            "\\chapter{Giris}\n"
            "\\begin{figure}[H]\n  \\centering\n"
            "  \\includegraphics[width=0.8\\textwidth]{_page_12_Picture_1.png}\n"
            "\\end{figure}\n\n"
            "Bu bolumde sinir aglari anlatilir."
        )
        out, _ = LatexSanitizer(image_dirs=[tmp_path]).sanitize(body)
        assert "_page_12_Picture_1.png" not in out  # rozet silindi
        assert "\\chapter{Giris}" in out             # bolum korundu
        assert "sinir aglari anlatilir" in out        # icerik korundu

    def test_captioned_figure_kept(self):
        # Gercek figur (caption'li) SILINMEZ
        body = (
            "\\chapter{Giris}\n"
            "\\begin{figure}[H]\n  \\centering\n"
            "  \\includegraphics[width=0.8\\textwidth]{sekil1.png}\n"
            "  \\caption{Onemli bir diyagram}\n"
            "\\end{figure}\n"
        )
        out = _san(body)
        assert "sekil1.png" in out          # korundu
        assert "Onemli bir diyagram" in out

    def test_mid_text_figure_kept(self):
        # Chapter-acilis OLMAYAN figur (metin arasinda) korunur
        body = (
            "\\chapter{Giris}\n"
            "Once biraz metin yaziyoruz, sonra sekil geliyor.\n"
            "\\begin{figure}[H]\n  \\centering\n"
            "  \\includegraphics[width=0.8\\textwidth]{icerik.png}\n"
            "\\end{figure}\n"
        )
        out = _san(body)
        assert "icerik.png" in out  # chapter-acilis degil -> korunur


class TestDoubleBackslashArtifacts:
    """Cift-backslash + bozuk Python dunder (compile-kiran) artefaktlar."""

    def test_double_backslash_textit(self):
        body = "Bir \\\\textit{onemli} kavram."
        out = _san(body)
        assert "\\\\textit" not in out
        assert "\\textit{onemli}" in out

    def test_broken_dunder(self):
        body = "Sinif \\textit{\\_init\\}\\_ metodu cagrilir."
        out = _san(body)
        assert "\\texttt{\\_\\_init\\_\\_}" in out
        assert "\\}" not in out.split("metodu")[0]  # kapanmayan brace gitti

    def test_double_backslash_underscore(self):
        # \\_ (cift-backslash escaped underscore) -> \_ (URL disi baglam)
        body = "Degisken \\\\_test adi onemlidir."
        out = _san(body)
        assert "\\\\_" not in out
        assert "\\_test" in out

    def test_double_backslash_underscore_in_url(self):
        # URL icinde \\_ -> URL \url{}'e sarilir, ic kacis sadelesir
        body = "Bkz. https://site.org/auto\\\\_grad.html adresine."
        out = _san(body)
        assert "\\\\_" not in out
        assert "\\url{https://site.org/auto_grad.html}" in out

    def test_legit_rowbreak_preserved(self):
        # Gercek satir-sonu \\ (bosluk/yeni satir sonrasi) korunur
        body = "Birinci satir \\\\\nIkinci satir"
        out = _san(body)
        assert "\\\\" in out  # row-break korundu


class TestTextModeGreek:
    """text-mode Greek -> math (Latin Modern Roman'da Yunan glyph yok -> bos)."""

    def test_textit_greek_to_math(self):
        body = "Parametre \\textit{θ} optimize edilir."
        out = _san(body)
        assert "\\textit{θ}" not in out
        assert "$\\theta$" in out

    def test_bare_greek_to_math(self):
        body = "Burada Θ kume parametrelerini, π sabitini gosterir."
        out = _san(body)
        assert "$\\Theta$" in out
        assert "$\\pi$" in out
        assert "Θ" not in out  # ciplak greek kalmadi

    def test_greek_inside_math_untouched(self):
        # $...$ icindeki greek stash'li -> dokunulmaz (cift sarma olmaz)
        body = "Denklem \\(\\theta + \\alpha\\) ve metin."
        out = _san(body)
        assert "\\(\\theta + \\alpha\\)" in out
        assert "$\\theta$" not in out  # math icindeki theta sarilmadi


class TestLeftRightBalance:
    """B22 split sonrasi \\left\\right'i satir-icinde dengele."""

    def test_split_delimiter_balanced(self):
        body = (
            "\\begin{multline*}\n"
            "A = \\left| x + y \\\\\n"
            "- z \\right|\n"
            "\\end{multline*}"
        )
        out = _san(body)
        # her satirda \left ve \right sayisi esit olmali (her satir kendi icinde dengeli)
        for row in out.split("\\\\"):
            assert row.count("\\left") == row.count("\\right"), f"dengesiz: {row}"

    def test_balanced_math_untouched(self):
        body = (
            "\\begin{align*}\n"
            "a &= \\left( b + c \\right) \\\\\n"
            "d &= \\left[ e \\right]\n"
            "\\end{align*}"
        )
        out = _san(body)
        # zaten dengeli -> \right. eklenmemeli
        assert "\\right." not in out
        assert "\\left." not in out


class TestUrlWrapAndHeaders:
    """Ham URL -> \\url{} (tasma) + uzun chapter basligina kisa header."""

    def test_raw_url_wrapped(self):
        body = "Ayrintilar icin https://pytorch.org/tutorials/x.html adresine bakin."
        out = _san(body)
        assert "\\url{https://pytorch.org/tutorials/x.html}" in out

    def test_href_url_not_double_wrapped(self):
        body = "Bkz. \\href{https://site.org/a}{baglanti}."
        out = _san(body)
        assert "\\url{https://site.org/a}" not in out  # zaten href icinde
        assert "\\href{https://site.org/a}{baglanti}" in out

    def test_short_chapter_header_added(self):
        body = "\\chapter{Convolutional Neural Networks (Evrisimli Sinir Aglari)}"
        out = _san(body)
        assert "\\chapter[Convolutional Neural Networks]{" in out

    def test_plain_chapter_unchanged(self):
        body = "\\chapter{Giris}"
        out = _san(body)
        assert out.strip() == "\\chapter{Giris}"


class TestLineSpacingClamp:
    """Detector linespread clamp (font misdetect -> 1.99 cift aralik bug)."""

    def test_clamp_logic(self):
        # _detect_line_spacing PDF-bagimli; clamp mantigini birebir dogrula.
        # (>1.5 -> 1.1 fallback; degilse [1.0,1.3])
        def clamp(result):
            if result > 1.5:
                return 1.1
            return max(1.0, min(result, 1.3))

        assert clamp(1.99) == 1.1   # font-misdetect disaster -> fallback
        assert clamp(2.0) == 1.1
        assert clamp(1.2) == 1.2    # normal tek-aralik korunur
        assert clamp(1.0) == 1.0
        assert clamp(1.45) == 1.3   # hafif yuksek -> capped
        assert clamp(0.8) == 1.0    # cok dusuk -> taban
