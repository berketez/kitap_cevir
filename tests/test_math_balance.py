"""B7: Math balance check (WARN-only) regresyon testleri.

LatexSanitizer._check_math_balance ve preflight_math_check için testler.
Autofix YOK — sadece tespit ve raporlama.
"""
import pytest


def _get_sanitizer():
    """Gerçek LatexSanitizer yapısına uygun instance döner."""
    try:
        from latex_pipeline.sanitizer import LatexSanitizer
    except ImportError:
        return None
    return LatexSanitizer(source_lang="en", target_lang="tr")


class TestMathBalance:
    """LatexSanitizer._check_math_balance regresyon testleri."""

    def test_balanced_math_no_issues(self):
        san = _get_sanitizer()
        if san is None:
            pytest.skip("LatexSanitizer import edilemedi")
        body = "Formül $E=mc^2$ önemlidir. Başka bir $F=ma$ var."
        issues = san._check_math_balance(body)
        assert len(issues) == 0, f"Dengeli input'ta sorun olmamalı: {issues}"

    def test_unbalanced_single_dollar_detected(self):
        san = _get_sanitizer()
        if san is None:
            pytest.skip("LatexSanitizer import edilemedi")
        body = "Tek $ açık var ve devam ediyor paragraf boyunca."
        issues = san._check_math_balance(body)
        assert len(issues) >= 1, "Tek açık $ tespit edilmeli"
        assert issues[0]["single_count"] % 2 == 1

    def test_escaped_dollar_ignored(self):
        san = _get_sanitizer()
        if san is None:
            pytest.skip("LatexSanitizer import edilemedi")
        body = r"Para \$100 yeterli, $x=1$ formül."
        issues = san._check_math_balance(body)
        assert len(issues) == 0, (
            f"\\$ escape edildi, $x=1$ dengeli → sorun olmamalı: {issues}"
        )

    def test_double_dollar_display_balanced(self):
        san = _get_sanitizer()
        if san is None:
            pytest.skip("LatexSanitizer import edilemedi")
        body = "Display: $$y = 2x$$\n\nSonra normal metin."
        issues = san._check_math_balance(body)
        assert len(issues) == 0, f"$$...$$ dengeli olmalı: {issues}"

    def test_multiple_paragraphs_mixed(self):
        san = _get_sanitizer()
        if san is None:
            pytest.skip("LatexSanitizer import edilemedi")
        body = (
            "Paragraf 1 $x=1$ dengeli.\n\n"
            "Paragraf 2 $y açık kaldı Türkçe metin devam.\n\n"
            "Paragraf 3 normal metin."
        )
        issues = san._check_math_balance(body)
        assert len(issues) == 1, (
            f"Sadece 2. paragrafta sorun olmalı, bulunan: {len(issues)} → {issues}"
        )

    def test_real_bug_b7_scenario(self):
        """B7 gerçek kanıtı: middle PDF sayfa 5'teki senaryo."""
        san = _get_sanitizer()
        if san is None:
            pytest.skip("LatexSanitizer import edilemedi")
        # Açılmış ama kapanmamış $ → sonraki Türkçe paragraf math mode'a girer
        body = (
            "Bir $m^3$ tic cismin ifadesi, u şekilde ifade edilir:\n\n"
            "Fakat burada $ açık kaldı ve Türkçe metin devam ediyor.\n\n"
            "Bu paragraf yeni başlar."
        )
        issues = san._check_math_balance(body)
        # 2. paragrafta tek $ kalır
        assert len(issues) >= 1, "B7 gerçek bug senaryosu tespit edilmeli"

    def test_empty_body(self):
        san = _get_sanitizer()
        if san is None:
            pytest.skip("LatexSanitizer import edilemedi")
        assert san._check_math_balance("") == []
        assert san._check_math_balance("\n\n\n") == []

    def test_preflight_returns_true_even_on_issues(self):
        """preflight_math_check her zaman True döner (autofix yok)."""
        san = _get_sanitizer()
        if san is None:
            pytest.skip("LatexSanitizer import edilemedi")
        body = "Açık $ var burada sorun var."
        assert san.preflight_math_check(body) is True

    def test_autofix_flag_default_false(self):
        """autofix_math_balance flag default False (WARN-only kuralı)."""
        san = _get_sanitizer()
        if san is None:
            pytest.skip("LatexSanitizer import edilemedi")
        # getattr default False — attribute yoksa bile güvenli
        assert getattr(san, "autofix_math_balance", False) is False
