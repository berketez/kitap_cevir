"""
Integration testleri: gercek HY-MT 7B modeli ile.

Calistirma (opt-in):
    pytest -m integration tests/integration/
    pytest -m integration tests/integration/test_hymt_real.py::test_basic_translation

Default kosuda bu testler SKIP (pytest.ini markers).
"""

import platform
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


IS_MAC = platform.system() == "Darwin"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def real_translator():
    """Gercek HY-MT 7B ile BookTranslator (module-level, tek load)."""
    from core.translator import BookTranslator, TranslationConfig

    config = TranslationConfig(
        source_lang="en",
        target_lang="tr",
        mt_engine="hymt-7b",
        use_memory=False,        # Test izolasyonu icin
        glossary_mode="off",     # Temel davranis testi (glossary kapali)
        use_ai_review=False,
        run_qa=False,            # QA devre disi - test izolasyonu
        check_consistency=False,
    )

    try:
        bt = BookTranslator(config)
    except Exception as e:
        pytest.skip(f"HY-MT 7B yuklenemedi: {e}")

    # Eger model init edildi mi kontrol et
    if bt.mt_model is None:
        pytest.skip("HY-MT model init edilmedi (cache/download problemi)")

    return bt


# =============================================================================
# 1. Basic translation
# =============================================================================
def test_basic_translation(real_translator):
    """Hello world -> Turkce cikiyor mu?"""
    result = real_translator.translate_text("Hello world.")
    assert result
    assert len(result) > 0
    # Turkce karakterler veya kelimeler icermeli
    turkish_indicators = ["merhaba", "dunya", "dünya", "selam", "ı", "ğ", "ş", "ç"]
    assert any(t in result.lower() for t in turkish_indicators), \
        f"Turkce cikmadi: {result!r}"


# =============================================================================
# 2. Placeholder preservation
# =============================================================================
def test_placeholder_preserved(real_translator):
    """<<LATEX_0>> placeholder'i ceviride korunmali."""
    text = "The value <<LATEX_0>> is important for calculation."
    result = real_translator.translate_text(text)
    assert "<<LATEX_0>>" in result, \
        f"Placeholder MT tarafindan yutuldu: {result!r}"


# =============================================================================
# 3. \ref / \cite / \label protection
# =============================================================================
def test_ref_protected(real_translator):
    r"""\ref{eq:1} ceviri sirasinda bozulmamali."""
    text = r"See \ref{eq:1} and \cite{smith2020} for proof."
    result = real_translator.translate_text(text)
    # Protect/restore mekanizmasi calisiyor mu?
    assert r"\ref{eq:1}" in result, f"\\ref bozuldu: {result!r}"
    assert r"\cite{smith2020}" in result, f"\\cite bozuldu: {result!r}"


# =============================================================================
# 4. Formula preservation
# =============================================================================
def test_formula_preserved(real_translator):
    r"""$E = mc^2$ inline formula korunmali."""
    text = r"The famous equation $E = mc^2$ shows mass-energy equivalence."
    result = real_translator.translate_text(text)
    assert "$E = mc^2$" in result or "$E=mc^2$" in result, \
        f"Formul bozuldu: {result!r}"


# =============================================================================
# 5. Long text (2000 char)
# =============================================================================
def test_long_2000_char_paragraph(real_translator):
    """2000 char paragraf -> tek seferde, OOM yok."""
    long_text = (
        "The fundamentals of rocket propulsion are based on Newton's third law "
        "of motion, which states that for every action there is an equal and "
        "opposite reaction. "
    ) * 15  # ~1800-2000 char

    # Crash olmamali
    result = real_translator.translate_text(long_text[:2000])
    assert result
    assert len(result) > 100  # Trivial olmayan cikti


# =============================================================================
# 6. Prompt leak detection
# =============================================================================
def test_no_prompt_leak_in_output(real_translator):
    """MT ciktisinda 'As an AI language model' gibi leak olmamali."""
    text = "Translate this: Thermodynamics is important."
    result = real_translator.translate_text(text)
    result_lower = result.lower()
    # _strip_prompt_leaks temizlemis olmali
    assert "as an ai" not in result_lower
    assert "here is the translation" not in result_lower
    assert "i am an ai" not in result_lower


# =============================================================================
# 7. ALL-CAPS normalize
# =============================================================================
def test_all_caps_title_normalized(real_translator):
    """ALL-CAPS baslik echo etmemeli."""
    text = "BASIC PRINCIPLES OF CHEMICAL THERMODYNAMICS"
    result = real_translator.translate_text(text)
    # Result Turkce olmali, Ingilizce echo degil
    assert "BASIC PRINCIPLES" not in result.upper() or "TEMEL" in result.upper(), \
        f"ALL-CAPS baslik cevrilmedi: {result!r}"


# =============================================================================
# 8. Tabular cell translation
# =============================================================================
def test_tabular_cell_translated(real_translator):
    """Tabular hucresi cevrilmeli."""
    cell_text = "Pressure"
    result = real_translator.translate_text(cell_text)
    # "Pressure" -> "Basinc" veya benzeri
    assert result.lower() != "pressure", f"Hucre cevrilmedi: {result!r}"


# =============================================================================
# 9. Batch 20 segment
# =============================================================================
def test_batch_20_segments_no_crash(real_translator):
    """20 paragraf batch -- crash yok, hepsi cevrilmis."""
    texts = [
        f"Sentence number {i}. This is a test paragraph about rockets."
        for i in range(20)
    ]
    results = real_translator.translate_batch(texts)
    assert len(results) == 20
    # Hicbiri bos veya orijinal olmamali
    failed = sum(1 for r, t in zip(results, texts) if not r or r == t)
    assert failed < 5, f"{failed}/20 segment cevrilmedi"


# =============================================================================
# 10. Turkish language code correctness
# =============================================================================
def test_turkish_output_not_english_echo(real_translator):
    """Cikti Ingilizce echo olmamali (dil kodu dogru gidiyor mu)."""
    text = "The rocket engine produces thrust through chemical reactions."
    result = real_translator.translate_text(text)
    result_lower = result.lower()

    # Turkce karakter icermeli (Turkce'ye ozgu: c, g, i, o, s, u)
    turkish_chars = ["ç", "ğ", "ı", "ö", "ş", "ü"]
    has_turkish_char = any(c in result_lower for c in turkish_chars)

    # VEYA Turkce-ozgu ek/kelime icermeli
    turkish_tokens = [
        " ve ", " bir ", " ile ", " için ", " icin ",
        "yor", "mak", "mek", "lar", "ler", "dir", "tir",
        "motor", "kimyasal", "reaksiyon", "üretir", "itiş",
    ]
    has_turkish_token = any(w in result_lower for w in turkish_tokens)

    assert has_turkish_char or has_turkish_token, \
        f"Ingilizce echo tespit edildi (Turkce karakter/ek yok): {result!r}"

    # Ingilizce "the" cok sik olmamali
    the_count = result_lower.count(" the ")
    assert the_count <= 1, f"Fazla Ingilizce: {the_count} 'the' var"

    # Ingilizce tam cumleler echolanmamali
    assert "rocket engine" not in result_lower, \
        f"Ingilizce echo (rocket engine): {result!r}"
    assert "chemical reactions" not in result_lower, \
        f"Ingilizce echo (chemical reactions): {result!r}"
