"""F3 kulvarı (30 Eylül 2026): makine çevirisi künyesi + LM Studio bellek hükmü.

1. KÜNYE (Berke kararı): "Yerel model ile çevir" çıktısının (PDF ve EPUB)
   başında kısa, görünür makine çevirisi künyesi: hangi yerel model, bu
   bilgisayarda, gözden geçirilmemiş taslak, kaynak dil, tarih. Metin kalıbı
   ui/branding.py'de. "PDF'den TeX çıkar" (2_derle, manuel çeviri) çıktısı
   DEĞİŞMEZ. Künye çeviri gövdesine (translated_body.tex) girmez: kayıpsızlık,
   QC ve hizalama denetimleri onu görmez; İngilizce kilidi değişmez.
2. MT-12: LM Studio / OpenAI uyumlu sunucu KV şeklini bildirmez; oran yedeği
   MoE / karma dikkatli modellerde aşırı ("lmstudio:qwen/qwen3.5-35b-a3b
   sığmaz (~33,5 GB)"). Sunucunun yüklü tuttuğu modele "sığmaz" denmez; şekli
   bilinmeyen sunucu modelinde "sığmaz" engel değil gerekçeli uyarı. MLX / GGUF
   / Ollama engeli aynen.

Model YÜKLENMEZ; gerçek Ollama / LM Studio'ya gidilmez (sahte sunucu, sahte
mlx_lm). Senaryo değerleri sahte test verisidir.
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import date
from pathlib import Path

import pytest

from core import hardware
from core.mt_engines import contracts, discovery
from tests.fakes.fake_llm_server import FakeModel
from ui import branding, runner

GIB = 2 ** 30
# 36 GB M4 Max: Metal recommendedMaxWorkingSetSize (test_donanim_20260928.MAC_36GB)
MAC_36GB = hardware.HardwareInfo(available_bytes=30_150_672_384, memory_bytes=38_654_705_664,
                                 chip="Test Mac", source=hardware.SOURCE_METAL)
# qwen3.5-35b-a3b ağırlığı: RAPOR MT-11 notundaki "~33,5 GB" oran yedeği
# tahmininden geri hesaplandı (sahte test verisi; gerçek sunucuya gidilmedi).
QWEN_MOE = "qwen/qwen3.5-35b-a3b"
QWEN_MOE_WEIGHTS = 22_130_000_000


def _use(monkeypatch, info: hardware.HardwareInfo) -> None:
    monkeypatch.setattr(hardware, "_measure", lambda: info)
    monkeypatch.setattr(hardware, "_CACHE", None)


def _lmstudio(monkeypatch, mt_isolation, fake_llm_server, **model_kw):
    srv = fake_llm_server("lmstudio", models=[FakeModel(QWEN_MOE, size_bytes=QWEN_MOE_WEIGHTS,
                                                        format="mlx", **model_kw)])
    cfg = contracts.SourcesConfig(ollama_url=mt_isolation.closed_url,
                                  lmstudio_url=srv.base_url)
    monkeypatch.setattr(discovery, "load_sources", lambda path=None, *a, **k: cfg)
    return srv


# ===========================================================================
# MT-12 — sunucu modelinde bellek hükmü
# ===========================================================================
class TestMT12:
    def test_oran_yedegi_hatayi_uretir(self):
        """Kanıt: RAPOR'daki satır — oran yedeği "~33,5 GB, sığmaz" diyor."""
        fit = hardware.assess(QWEN_MOE_WEIGHTS, None, mode=hardware.MODE_CONTEXT, hw=MAC_36GB)
        assert fit.kv_source == hardware.KV_SOURCE_RATIO
        assert fit.fits is False
        assert hardware.format_gb(fit.minimum_bytes, round_up=True) == "33,5"
        # Ham hüküm bağlayıcıdır (MLX/GGUF/Ollama yolu bunu kullanır)
        assert fit.verdict == hardware.VERDICT_BINDING and hardware.blocks(fit)

    def test_server_verdict_kurallari(self):
        fit = hardware.assess(QWEN_MOE_WEIGHTS, None, mode=hardware.MODE_CONTEXT, hw=MAC_36GB)
        advisory = hardware.server_verdict(fit, loaded=False)
        assert advisory.fits is False and advisory.verdict == hardware.VERDICT_ADVISORY
        assert not hardware.blocks(advisory)
        assert "kaba tahmin ~33,5 GB" in hardware.fit_warning(advisory)
        loaded = hardware.server_verdict(fit, loaded=True)
        assert loaded.fits is None and loaded.verdict == hardware.VERDICT_LOADED
        assert not hardware.blocks(loaded) and hardware.fit_warning(loaded) == ""
        # Şekli bilinen (oran değil) tahmin: engel aynen
        kv = hardware.KvShape(64, 64, 128, 128, 2, hardware.KV_SOURCE_GGUF)
        shaped = hardware.assess(QWEN_MOE_WEIGHTS, kv, mode=hardware.MODE_CONTEXT, hw=MAC_36GB)
        assert shaped.fits is False
        assert hardware.server_verdict(shaped, loaded=False).verdict == hardware.VERDICT_BINDING
        # Sığan tahmin değişmez
        small = hardware.assess(4 * 10 ** 9, None, mode=hardware.MODE_CONTEXT, hw=MAC_36GB)
        assert hardware.server_verdict(small, loaded=False) == small

    def test_iletiler_bolme_dalini_tetiklemez(self):
        """core/translator.py bölme dalı 'oom'/'mps'/'cuda'/'out of memory' arar."""
        for text in (hardware.MSG_HW_ROUGH, hardware.MSG_HW_ROUGH_LABEL,
                     hardware.MSG_HW_LOADED_NOTE):
            low = text.lower()
            assert not any(s in low for s in ("oom", "mps", "cuda", "out of memory"))

    def test_lmstudio_yuklu_degil_uyari_kullanilabilir(self, monkeypatch, mt_isolation,
                                                       fake_llm_server):
        _use(monkeypatch, MAC_36GB)
        _lmstudio(monkeypatch, mt_isolation, fake_llm_server, loaded=False)
        model = discovery.discover_all().model_by_id(f"lmstudio:{QWEN_MOE}")
        assert model.usable and model.reason == ""
        assert model.memory.fits is False
        assert model.memory.verdict == hardware.VERDICT_ADVISORY
        label = runner.model_option_label(model)
        assert "sığmayabilir (kaba tahmin ~33,5 GB" in label
        assert branding.MODEL_UNUSABLE_LABEL not in label and "sığmaz" not in label
        ready, reason = runner.engine_ready(model.engine_id,
                                            contracts.DiscoveryReport((model,), (), 0.0))
        assert ready and reason == ""
        line = discovery._memory_line(model.memory)
        assert "sığmayabilir" in line
        check = discovery.check_engine(f"lmstudio:{QWEN_MOE}")
        assert check.ok and check.reason == ""
        assert isinstance(check, discovery.WarnedEngineCheck)
        assert "kaba tahmin ~33,5 GB" in check.warning and "sunucu" in check.warning
        data = discovery.report_to_json(discovery.discover_all())
        mem = [m for m in data["models"] if m["engine_id"] == model.engine_id][0]["memory"]
        assert mem["verdict"] == "uyarı" and mem["fits"] is False

    def test_lmstudio_yuklu_model_sigmaz_denmez(self, monkeypatch, mt_isolation,
                                                fake_llm_server):
        _use(monkeypatch, MAC_36GB)
        _lmstudio(monkeypatch, mt_isolation, fake_llm_server, loaded=True)
        model = discovery.discover_all().model_by_id(f"lmstudio:{QWEN_MOE}")
        assert model.usable and model.loaded is True
        assert model.memory.fits is None and model.memory.verdict == hardware.VERDICT_LOADED
        label = runner.model_option_label(model)
        assert "sığmaz" not in label and "sığmayabilir" not in label
        assert hardware.MSG_HW_LOADED_NOTE in discovery._memory_line(model.memory)
        check = discovery.check_engine(f"lmstudio:{QWEN_MOE}")
        assert check.ok and not getattr(check, "warning", "")

    def test_kayitli_lmstudio_motoru_ayni_kural(self, monkeypatch, mt_isolation,
                                                fake_llm_server):
        _use(monkeypatch, MAC_36GB)
        srv = _lmstudio(monkeypatch, mt_isolation, fake_llm_server, loaded=False)
        mt_isolation.user_specs_path.write_text(json.dumps({
            "buyuk-moe": {"backend": "openai", "flavor": "lmstudio", "model": QWEN_MOE,
                          "base_url": srv.base_url}}), encoding="utf-8")
        model = discovery.discover_all().model_by_id("buyuk-moe")
        assert model.usable and model.memory.verdict == hardware.VERDICT_ADVISORY
        check = discovery.check_engine("buyuk-moe")
        assert check.ok and "kaba tahmin" in check.warning

    def test_gguf_ve_ollama_engeli_aynen(self, monkeypatch, mt_isolation, fake_llm_server):
        """Şekli okunan / dosyası okunan model: "sığmaz" engel kalır."""
        _use(monkeypatch, hardware.HardwareInfo(available_bytes=int(16 * GIB * 2 / 3),
                                                memory_bytes=16 * GIB, chip="Test Mac",
                                                source=hardware.SOURCE_METAL))
        srv = fake_llm_server("ollama", models=[FakeModel("dev:70b", size_bytes=40 * 10 ** 9,
                                                          loaded=True)])
        cfg = contracts.SourcesConfig(ollama_url=srv.url,
                                      lmstudio_url=mt_isolation.closed_url + "/v1")
        monkeypatch.setattr(discovery, "load_sources", lambda path=None, *a, **k: cfg)
        big = discovery.discover_all().model_by_id("ollama:dev:70b")
        assert not big.usable and big.memory.fits is False
        assert big.memory.verdict == hardware.VERDICT_BINDING
        assert big.reason == hardware.too_big_message(big.memory)
        assert not discovery.check_engine("ollama:dev:70b").ok

    def test_genel_kapi_degismez(self, monkeypatch):
        _use(monkeypatch, hardware.HardwareInfo(available_bytes=int(8 * GIB * 2 / 3),
                                                memory_bytes=8 * GIB, chip="Test Mac",
                                                source=hardware.SOURCE_METAL))
        gate = hardware.general_gate()
        assert not gate.allowed
        assert gate.reference.verdict == hardware.VERDICT_BINDING


# ===========================================================================
# 1. KÜNYE — metin kalıbı (ui/branding.py, tek yer)
# ===========================================================================
import random  # noqa: E402

import translate_book as tb  # noqa: E402
from latex_pipeline import epub_output as eo  # noqa: E402
from latex_pipeline import template_engine as te  # noqa: E402
from latex_pipeline.config import PipelineConfig  # noqa: E402
from latex_pipeline.inspector import QualityInspector  # noqa: E402
# Modelsiz run_pipeline kurulumu ve sahte mlx_lm: İngilizce kilit testinin
# fikstürleri (yalnız fikstür / yardımcı; test sınıfları içe aktarılmaz).
from tests.test_kaynak_dil_20260928 import (  # noqa: E402,F401
    EN_BODY,
    fake_mlx,
    make_book,
    pipeline_env,
    restore_root_logger,
    run_book,
)
from tests.test_dalga_e2_20260925 import _book, _compare, _setup  # noqa: E402

DAY = date(2026, 9, 30)


def _lines(model="openai/gpt-oss-20b (LM Studio)", lang="de"):
    return branding.mt_notice_lines(model, lang, DAY)


class TestKunyeMetni:
    def test_alanlar(self):
        title, text, fields = _lines()
        assert "Makine çevirisi" in title and "gözden geçirilmemiş" in title
        assert "bu bilgisayardaki bir yerel çeviri modelinin" in text
        assert "gözden geçirilmemiş bir taslaktır" in text
        assert branding.quoted(branding.MODEL_TITLE) in text
        assert fields == ("Model: openai/gpt-oss-20b (LM Studio) · Kaynak dil: Almanca · "
                          "Çeviri tarihi: 30 Eylül 2026")
        for line in (title, text, fields):
            assert "Yol A" not in line and "Yol B" not in line

    def test_bilinmeyen_uydurulmaz(self):
        _t, _x, fields = branding.mt_notice_lines(None, None, DAY)
        assert "Model: kaydedilmemiş" in fields and "Kaynak dil: belirlenemedi" in fields

    def test_tarih(self):
        assert branding.format_date_tr(date(2026, 2, 1)) == "1 Şubat 2026"
        assert branding.format_date_tr(date(2026, 12, 31)) == "31 Aralık 2026"


# ===========================================================================
# 1. KÜNYE — PDF: tam belgede yer ve dizgi (latex_pipeline/template_engine.py)
# ===========================================================================
_COVER = ("% -- Original cover from source PDF (KURALLAR.md Madde 2) --\n"
          "\\includepdf[pages={1}]{/tmp/kitap/source_book.pdf}\n")
_TITLEPAGE = ("\\begin{titlepage}\n  \\centering\n  {\\Huge\\bfseries Kitap}\\par\n"
              "\\end{titlepage}\n\n")
_BODY = ("\\chapter*{Önsöz}\n\nMetin $x^2$.\n\n\\tableofcontents\n\n\\mainmatter\n\n"
         "\\chapter{Giriş}\n\nBir paragraf.\n\n\\section{Alt}\n\n\\begin{equation}\na=b\n"
         "\\end{equation}\n")


def _doc(body: str) -> str:
    return te.TemplateEngine(PipelineConfig()).wrap_content(body)


class TestKunyePdf:
    def test_kapak_arkasinda(self):
        tex = _doc(_BODY).replace("\\begin{document}\n", "\\begin{document}\n" + _COVER, 1)
        out = te.insert_front_notice(tex, _lines())
        cover = out.index("\\includepdf")
        mark = out.index(te.MT_NOTICE_MARK)
        assert cover < mark < out.index("\\chapter*{Önsöz}")

    def test_baslik_sayfasi_arkasinda(self):
        out = te.insert_front_notice(_doc(_TITLEPAGE + _BODY), _lines())
        assert out.index("\\end{titlepage}") < out.index(te.MT_NOTICE_MARK)
        assert out.index(te.MT_NOTICE_MARK) < out.index("\\chapter*{Önsöz}")

    def test_yoksa_en_basta(self):
        out = te.insert_front_notice(_doc(_BODY), _lines())
        begin = out.index("\\begin{document}")
        assert begin < out.index(te.MT_NOTICE_MARK) < out.index("\\chapter*{Önsöz}")

    def test_kod_ornegindeki_baslik_sayfasi_sayilmaz(self):
        body = ("\\begin{verbatim}\n\\begin{titlepage}\nX\n\\end{titlepage}\n"
                "\\end{verbatim}\n\n" + _BODY)
        out = te.insert_front_notice(_doc(body), _lines())
        assert out.index(te.MT_NOTICE_MARK) < out.index("\\begin{verbatim}")

    def test_kacis_ve_ayrac_dengesi(self):
        out = te.insert_front_notice(_doc(_BODY), _lines(model="a_b %50 & #1 {x} ~^ \\ (LM Studio)"))
        assert "a\\_b \\%50 \\& \\#1 \\{x\\} \\textasciitilde{}\\textasciicircum{} " \
               "\\textbackslash{} (LM Studio)" in out
        assert tb.check_brace_balance(out)[2] == 0
        block = te.front_notice_block(_lines())
        assert tb.check_brace_balance(block)[2] == 0

    def test_bir_kez(self):
        once = te.insert_front_notice(_doc(_BODY), _lines())
        assert te.insert_front_notice(once, _lines()) == once
        assert once.count(te.MT_NOTICE_MARK) == 1

    def test_inceleme_sayimlari_degismez(self, tmp_path):
        """Phase 7 incelemesi: başlık / formül / görsel sayıları aynı."""
        plain = _doc(_TITLEPAGE + _BODY)
        noted = te.insert_front_notice(plain, _lines())
        reports = []
        for name, tex in (("a.tex", plain), ("b.tex", noted)):
            path = tmp_path / name
            path.write_text(tex, encoding="utf-8")
            reports.append(QualityInspector().inspect(path))
        a, b = reports
        assert a.formulas == b.formulas
        assert len(a.headings) == len(b.headings)
        assert len(a.images) == len(b.images)

    def test_sablon_degismedi(self):
        """"PDF'den TeX çıkar" çıktısı: şablon ve wrap_content aynen (künye yok)."""
        tex = _doc(_BODY)
        assert te.MT_NOTICE_MARK not in tex and "Makine çevirisi" not in tex
        template = (Path(te.__file__).parent / "templates" / "turkish_book.tex").read_text(
            encoding="utf-8")
        assert "künye" not in template.lower() and "Makine çevirisi" not in template


# ===========================================================================
# 1. KÜNYE — run_pipeline (modelsiz, sahte mlx_lm)
# ===========================================================================
def _book_tr(out: Path) -> str:
    return (out / "source" / "book_tr.tex").read_text(encoding="utf-8")


class TestKunyeHat:
    def test_yerel_model_ciktisinda_kunye_var(self, fake_mlx, pipeline_env):
        from core.mt_engines import registry

        out = make_book(pipeline_env, EN_BODY, "kunye")
        assert run_book(out, registry.DEFAULT_ENGINE) == 0
        info = json.loads((out / "source" / tb.MT_NOTICE_INFO_NAME).read_text(encoding="utf-8"))
        label = tb._engine_description(registry.DEFAULT_ENGINE)
        assert label and info["model"] == label and info["source_lang"] == "en"
        assert info["engine"] == registry.DEFAULT_ENGINE
        assert info["date"] == date.today().isoformat()
        book = _book_tr(out)
        assert book.count(te.MT_NOTICE_MARK) == 1
        assert f"Model: {te._tex_escape(label)}" in book
        assert "Kaynak dil: İngilizce" in book
        assert branding.format_date_tr(date.today()) in book
        # Künye çeviri gövdesinde YOK: kayıpsızlık / QC / hizalama onu görmez
        tr_body = (out / "source" / "translated_body.tex").read_text(encoding="utf-8")
        assert te.MT_NOTICE_MARK not in tr_body and "Makine çevirisi" not in tr_body
        # EPUB argümanları aynı kayıttan
        kwargs = tb.mt_notice_epub_kwargs(out)
        assert kwargs["mt_notice"][2].startswith(f"Model: {label}")
        assert label in kwargs["mt_contributor"]

    def test_resume_cevireni_kayittan_alir(self, fake_mlx, pipeline_env):
        from core.mt_engines import registry

        out = make_book(pipeline_env, EN_BODY, "kunye_resume")
        assert run_book(out, registry.DEFAULT_ENGINE) == 0
        path = out / "source" / tb.MT_NOTICE_INFO_NAME
        info = json.loads(path.read_text(encoding="utf-8"))
        info.update(model="qwen/qwen3.8-27b (LM Studio)", source_lang="de", date="2026-09-29")
        path.write_text(json.dumps(info), encoding="utf-8")
        assert run_book(out, registry.DEFAULT_ENGINE) == 0     # Phase 5.5'ten sürer
        book = _book_tr(out)
        assert "Model: qwen/qwen3.8-27b (LM Studio)" in book
        assert "Kaynak dil: Almanca" in book and "29 Eylül 2026" in book

    def test_kayit_yoksa_uydurulmaz(self, fake_mlx, pipeline_env):
        from core.mt_engines import registry

        out = make_book(pipeline_env, EN_BODY, "kunye_eski")
        assert run_book(out, registry.DEFAULT_ENGINE) == 0
        (out / "source" / tb.MT_NOTICE_INFO_NAME).unlink()
        assert run_book(out, registry.DEFAULT_ENGINE) == 0
        book = _book_tr(out)
        assert "Model: kaydedilmemiş" in book
        # Dil body.tex'ten ölçülür; bu kısa örnekte ölçülebilir düzyazı az
        # (analyze_language: und) -> varsayım yazılmaz
        assert "Kaynak dil: belirlenemedi" in book
        mtime = (out / "source" / "translated_body.tex").stat().st_mtime
        from datetime import datetime
        assert branding.format_date_tr(datetime.fromtimestamp(mtime).date()) in book

    def test_kayit_yoksa_dil_olculur(self, tmp_path):
        source = tmp_path / "k" / "source"
        source.mkdir(parents=True)
        de = ("Die Wärmeleitung beschreibt den Energietransport in einem Körper, ohne "
              "dass sich die Teilchen makroskopisch bewegen. Wer die Wärmeleitung "
              "versteht, kann Dämmschichten bemessen und Kühlkörper auslegen. ") * 8
        (source / "body.tex").write_text("\\chapter{Wärme}\n\n" + de, encoding="utf-8")
        (source / "translated_body.tex").write_text("\\chapter{Isı}\n\nMetin.\n" * 20,
                                                    encoding="utf-8")
        fields = tb.mt_notice_fields(tmp_path / "k")
        assert fields["model"] is None and fields["source_lang"] == "de"

    def test_tex_yolunda_kunye_yok(self, pipeline_env):
        """"PDF'den TeX çıkar" (2_derle: manual_translation) çıktısı değişmez."""
        out = make_book(pipeline_env, EN_BODY, "kunye_manuel")
        (out / "source" / "translated_body.tex").write_text(
            "\\chapter{Yörünge}\n\n"
            + "Bu bölüm yörüngeyi anlatır; çeviriyi bir insan yaptı ve gözden geçirdi.\n\n" * 4,
            encoding="utf-8")
        # Önceki bir yerel model koşusundan kalmış kayıt bile künye yazdırmaz
        (out / "source" / tb.MT_NOTICE_INFO_NAME).write_text(json.dumps(
            {"engine": "x", "model": "x", "source_lang": "en", "date": "2026-09-01"}),
            encoding="utf-8")
        code = tb.run_pipeline(str(out), "Kilit Kitap", skip_translate=True,
                               skip_compile=True, skip_qc=True, resume=True,
                               manual_translation=True, book_type="science")
        assert code == 0
        book = _book_tr(out)
        assert te.MT_NOTICE_MARK not in book and "Makine çevirisi" not in book
        assert tb.mt_notice_fields(out) is None and tb.mt_notice_epub_kwargs(out) == {}


# ===========================================================================
# 1. KÜNYE — EPUB (latex_pipeline/epub_output.py)
# ===========================================================================
def _opf(epub_path) -> str:
    with zipfile.ZipFile(epub_path) as z:
        container = z.read("META-INF/container.xml").decode("utf-8")
        name = re.search(r'full-path="([^"]+)"', container).group(1)
        return z.read(name).decode("utf-8")


class TestKunyeEpub:
    def test_kunye_sayfasi_ve_ust_veri(self, tmp_path):
        out, epub_path, _body, _tr = _setup(tmp_path, _book(random.Random(10)))
        plain_dir = tmp_path / "duz"
        plain_out, plain_epub, _b, _t = _setup(plain_dir, _book(random.Random(10)))
        plain = eo.generate_epub_output(plain_epub, str(plain_out), "Kitap")
        res = eo.generate_epub_output(epub_path, str(out), "Kitap",
                                      mt_notice=_lines(),
                                      mt_contributor=branding.mt_notice_contributor(
                                          "openai/gpt-oss-20b (LM Studio)"))
        assert res and plain
        # Çeviri yerleşimi ve kapsam raporu künyeden etkilenmez
        assert _compare(epub_path, res) == (45, 0, 0, 0)
        report = json.loads((out / "compiled" / eo.EPUB_REPORT_NAME).read_text())
        plain_report = json.loads((plain_out / "compiled" / eo.EPUB_REPORT_NAME).read_text())
        assert report == plain_report
        with zipfile.ZipFile(res) as z:
            infos = z.infolist()
            assert infos[0].filename == "mimetype"
            assert infos[0].compress_type == zipfile.ZIP_STORED
            page = [n for n in z.namelist() if n.endswith(eo.MT_NOTICE_EPUB_FILE)]
            assert len(page) == 1
            xhtml = z.read(page[0]).decode("utf-8")
        assert "Makine çevirisi" in xhtml and "gözden geçirilmemiş" in xhtml
        assert "openai/gpt-oss-20b (LM Studio)" in xhtml and "30 Eylül 2026" in xhtml
        opf = _opf(res)
        refs = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
        # Kapak / başlık sayfası yok: künye kitabın en başında
        assert refs[0] == eo.MT_NOTICE_EPUB_ID
        assert "(yerel model, makine çevirisi)</dc:contributor>" in opf
        assert "<dc:description>Makine çevirisi" in opf
        assert eo.MT_NOTICE_EPUB_ID not in _opf(plain)       # künyesiz çıktı aynen
        # Ayrıştırıcı çıktıyı okuyabiliyor (geçerli EPUB)
        from parsers.epub_parser import EPUBParser
        assert EPUBParser().parse(res).chapters
        # Bir kez
        assert eo.add_front_notice_to_epub(res, _lines()) is False

    def test_kapak_arkasina(self, tmp_path):
        from ebooklib import epub

        book = epub.EpubBook()
        book.set_identifier("urn:uuid:f3-test")
        book.set_title("Kapaklı")
        book.set_language("en")
        cover = epub.EpubHtml(uid="cover", file_name="cover.xhtml", title="Kapak")
        cover.content = "<html><body><p>Kapak</p></body></html>"
        chap = epub.EpubHtml(uid="c0", file_name="c0.xhtml", title="Bir")
        chap.content = "<html><body><h1>Bir</h1><p>Metin.</p></body></html>"
        for it in (cover, chap):
            book.add_item(it)
        book.add_item(epub.EpubNcx())
        book.spine = [cover, chap]
        path = tmp_path / "k.epub"
        epub.write_epub(str(path), book)
        assert eo.add_front_notice_to_epub(path, _lines(), "m (yerel model, makine çevirisi)")
        refs = re.findall(r'<itemref[^>]*idref="([^"]+)"', _opf(path))
        assert refs[:3] == ["cover", eo.MT_NOTICE_EPUB_ID, "c0"]


class TestUyariGunlukte:
    def test_onkontrol_uyarisi_pipeline_logda(self, fake_llm_server, monkeypatch, pipeline_env):
        """MT-12: olumlu ön kontrolün uyarısı koşuyu durdurmaz, günlüğe yazılır."""
        from tests.test_kaynak_dil_20260928 import OPENAI_MODEL, _point_sources, fake_turkish
        from core.mt_engines.contracts import OpenAIServer

        srv = fake_llm_server("openai", models=[OPENAI_MODEL], reply=fake_turkish)
        _point_sources(monkeypatch, openai_servers=[OpenAIServer(name="sahte",
                                                                 url=srv.base_url)])
        engine = f"openai:{srv.base_url}|{OPENAI_MODEL}"
        warning = "Bellek uyarısı: bu model bu Mac'e sığmayabilir (kaba tahmin ~33,5 GB)."
        monkeypatch.setattr(tb, "_check_engine", lambda e: discovery.WarnedEngineCheck(
            engine_id=e, ok=True, label=e, warning=warning))
        out = make_book(pipeline_env, EN_BODY, "uyari")
        assert run_book(out, engine) == 0
        log = (out / tb.PIPELINE_LOG_NAME).read_text(encoding="utf-8")
        assert warning in log
