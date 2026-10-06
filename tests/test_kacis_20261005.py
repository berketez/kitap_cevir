r"""Marker metninde ters bölü, HTML varlığı ve sahte bağlantı (5 Ekim 2026).

Düzenli ifade alıntılayan bir raporun gerçek akışında "\d" alt nokta aksanı
oluyor ("\d+" -> "+̣"), "&quot;" sanitizer'da "\"" (umlaut) oluyor,
"[\_-](?P<ad>...)" bağlantı sanılıp kalıbın yarısı düşüyordu.
Metinler sentetiktir.
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline.config import PipelineConfig  # noqa: E402
from latex_pipeline.converter import MarkdownToLatex  # noqa: E402
from latex_pipeline.sanitizer import LatexSanitizer  # noqa: E402

_HEAD = "# Chapter 1 Sentetik\n\nSentetik gövde paragrafı burada yer alır.\n\n"


@pytest.fixture
def conv(tmp_path):
    def _run(md: str) -> str:
        cfg = PipelineConfig(document_class="book", output_dir=str(tmp_path))
        out = MarkdownToLatex(cfg).convert(_HEAD + md + "\n")
        assert "\x00" not in out and "\x02" not in out, "yer tutucu sızdı"
        return out.split("Sentetik gövde paragrafı burada yer alır.", 1)[1]
    return _run


class TestTekTersBolu:
    def test_regex_ve_yol(self, conv):
        out = conv(r"Kalıp ^zz(?:A|B)\d{2,6} ve yol C:\Users\veri burada.")
        assert r"\textbackslash{}d\{2,6\}" in out
        assert r"C:\textbackslash{}Users\textbackslash{}veri" in out
        assert not re.search(r"(?<!textbackslash\{\})\\(?:d|Users|veri)\b", out)

    @pytest.mark.parametrize("md, beklenen", [
        ("## Başlık \\w+ belirteci\n\nMetin.", r"\textbackslash{}w+"),
        ("| A | B |\n|---|---|\n| \\d+ | C:\\x |\n", r"\textbackslash{}d+ & C:\textbackslash{}x"),
        (r"Bir **kalın \s+** sözcük.", r"\textbf{kalın \textbackslash{}s+}"),
    ])
    def test_baslik_tablo_kalin(self, conv, md, beklenen):
        assert beklenen in conv(md)

    def test_kod_parcasinda_ters_bolu(self, conv):
        out = conv(r"Kod `a\d+` burada.")
        assert r"\texttt{a\textbackslash{}d+}" in out

    def test_bilinen_simge_matematik_olur(self, conv):
        out = conv(r"Tepkime (2 g) + (16 g) \rightarrow (18 g) ve \alpha açısı.")
        assert r"(16 g) $\rightarrow$ (18 g)" in out and r"$\alpha$ açısı" in out

    def test_yapisik_satir_sonu_artigi_bosluk(self, conv):
        out = conv(r"Getiriler (e.g., due\nto careful planning) gerçektir.")
        assert "due to careful planning" in out and "textbackslash" not in out

    def test_bosluktan_sonraki_n_duz_ters_bolu(self, conv):
        out = conv(r"Satır sonu için \n kullanılır.")
        assert r"için \textbackslash{}n kullanılır" in out


class TestSahteBaglanti:
    def test_regex_parcasi_baglanti_olmaz(self, conv):
        out = conv(r"Kalıp [\_-](?P<veri>[A-Z0-9\_-]{20,})\$ sonu.")
        assert r"\href" not in out
        assert "{]}(?P<veri>" in out and r"\{20,\}" in out

    def test_gercek_baglanti_surer(self, conv):
        out = conv("Bkz. [kaynak](https://example.org/a_b?x=1) burada.")
        assert r"\href{https://example.org/a_b?x=1}{kaynak}" in out


class TestHtmlVarligi:
    def test_varliklar_cozulur(self, conv):
        out = conv("Bir &quot;alıntı&quot;, &#39;tek&#39; ve A &amp; B.")
        assert "Bir \"alıntı\", 'tek' ve A \\& B." in out
        assert "quot" not in out and "\\#39" not in out

    def test_lt_gt_ve_kod_cozulmez(self, conv):
        out = conv("Etiket &lt;b&gt; ve `x &amp; y` kodu.")
        assert r"\&lt;b\&gt;" in out and r"\texttt{x \&amp; y}" in out

    def test_bolunmus_varlik_kalinla(self, conv):
        out = conv("<sup>&</sup>quot;**KALIN**\\_devam&quot; sonu.")
        assert '"\\textbf{KALIN}\\_devam" sonu.' in out


class TestSanitizerVarlik:
    @pytest.mark.parametrize("girdi, beklenen", [
        (r"\&quot;\textbf{X}\_Y\&quot; son", r'"\textbf{X}\_Y" son'),
        (r"A \&amp; B", r"A \& B"),
        (r"\&\#39;tek\&\#39;", "'tek'"),
        (r"yüzde \&\#37; oran", r"yüzde \% oran"),
        (r"satır\\&quot;son", r'satır\\"son'),
    ])
    def test_kacisli_varlik(self, girdi, beklenen):
        out, _ = LatexSanitizer(manual_translation=True).sanitize(girdi + "\n")
        assert beklenen in out, out
