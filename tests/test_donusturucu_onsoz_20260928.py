"""Dönüştürücü: ilk başlıktan önceki metin sessizce atılmaz (2026-09-28).

_build_title_page ilk başlıktan önceki, yazar satırına benzemeyen her satırı
"tüketiyor" ama hiçbir yere yazmıyordu. Başlıksız başlayan belgede (rapor,
makale, kitap kesiti) ilk sayfalar kayboluyor, ilk bölüm başlığı kitap adı
sanılıyordu: bir raporda 4 sayfa (9 400 karakter), başka bir test kitabında
6 300 karakter. Artık başlıktan önce düzyazı varsa başlık sayfası kurulmaz.
"""

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex


def _convert(md: str) -> str:
    return MarkdownToLatex(PipelineConfig(document_class="book")).convert(md)


def test_basliktan_once_duzyazi_atilmaz():
    md = (
        "Rapor alt başlığı\n\n"
        "**Özet notu:** Bu rapordaki karartmalar yalnız açıkça belirtilen yerlerdedir ve "
        "sonuçlarımız için önemli hiçbir bilgi çıkarılmamıştır, bu cümle uzundur.\n\n"
        "İkinci giriş paragrafı: kapsam, yöntem ve zaman çizelgesi burada özetlenir; "
        "okur bu paragrafı kaybetmemelidir çünkü raporun ilk sayfasıdır.\n\n"
        "## Temel çıkarımlar\n\n"
        "Gövde metni.\n\n"
        "## İkinci bölüm\n\n"
        "Daha fazla metin.\n"
    )
    tex = _convert(md)
    assert "titlepage" not in tex
    assert "Özet notu" in tex
    assert "İkinci giriş paragrafı" in tex
    assert "Rapor alt başlığı" in tex
    # İlk başlık kitap adı sanılıp gövdeden çıkarılmaz
    assert "Temel çıkarımlar" in tex.split("Gövde metni")[0]


def test_baslikla_baslayan_kitapta_baslik_sayfasi_aynen():
    md = (
        "# Roket İtkisinin Temelleri\n\n"
        "Ayşe Yılmaz\n\n"
        "## Giriş\n\n"
        "Gövde metni.\n"
    )
    tex = _convert(md)
    assert "\\begin{titlepage}" in tex
    assert "Roket İtkisinin Temelleri" in tex.split("\\end{titlepage}")[0]
    # Başlıktan sonraki satır gövdede kalır (yazar yalnız başlıktan ÖNCE aranır)
    assert "Ayşe Yılmaz" in tex


def test_kisa_yazar_satiri_basliktan_once_baslik_sayfasina_gider():
    md = "Ayşe Yılmaz\n\n# Kitabın Adı\n\n## Bölüm\n\nMetin.\n"
    tex = _convert(md)
    head = tex.split("\\end{titlepage}")[0]
    assert "Kitabın Adı" in head and "Ayşe Yılmaz" in head


def test_basliktan_once_yalniz_kapak_gorseli_eski_davranis():
    # Kapak görseli + başlık: başlık sayfası kurulur (kapağı şablon PDF'ten basar).
    md = "![](_page_0_Picture_0.jpeg)\n\n# Kitabın Adı\n\n## Bölüm\n\nMetin.\n"
    tex = _convert(md)
    assert "\\begin{titlepage}" in tex
    assert "Kitabın Adı" in tex.split("\\end{titlepage}")[0]


def test_aci_ayracli_duzyazi_html_etiketi_sanilmaz():
    """2026-09-29: '<p…>'/'<i…>' ile başlayan düzyazı ve regex grupları siliniyordu."""
    md = (
        "# Başlık\n\n## Bölüm\n\n"
        "Ajan şöyle düşündü: <periodic check> ve <I'm free to check this>; "
        "regex (?P<prefix>zz) ile <payload> ve <idx> kaldı. <p>Gerçek etiket</p> silinir, "
        "<i>italik</i> de, <span class=\"x\">yayılım</span> de.\n"
    )
    tex = _convert(md)
    for parca in ("periodic check", "I'm free to check this", "prefix", "payload", "idx"):
        assert parca in tex, parca
    assert "<p>" not in tex and "</p>" not in tex and "<span" not in tex
    assert "Gerçek etiket" in tex and "yayılım" in tex


def test_pre_ve_br_etiketleri_eskisi_gibi_silinir():
    md = "# Başlık\n\n## Bölüm\n\n<pre>return x</pre> satır<br>sonu\n"
    tex = _convert(md)
    assert "</pre>" not in tex and "<pre>" not in tex and "<br>" not in tex
    assert "return x" in tex
