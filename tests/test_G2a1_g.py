"""G2a-1 kulvarı (5 Ekim 2026): yerel modelin LaTeX çeviri zinciri — bozuk çıktı basılmasın.

Kanıt: yerel model testlerinin çalışma kopyası çıktıları (kaynak, çeviri,
günlük; iç denetim) ve okuma bulguları ("mt okuma MT-k").

* MT-1 — "parçalı yol": model yer tutucu yutunca satır yer tutucu sınırlarından
  bölünüp parça parça çevriliyordu; İngilizce bir model testinde 13 satırın 13'ü,
  Almanca testte 2 satırın 2'si okunamaz çıktı. Yeni sıra: aynı satır bir kez daha → sade biçim
  (kısa değişken düz metin, ham komut kabuğu sabit) → özgün dilde bırak + işaretle.
* MT-7 — sayı koruması: "Bi < 0,1" → "Bi < ≤ 0 , ≤ 1" ("Numbers lost" uyarısı
  verilip geçiliyordu). Artık parça geri alınır; kaynakta olmayan ilişki simgesi
  çıktıya giremez.
* MT-17 — LaTeX iskeleti: ``\\caption*`` altyazı metni ayraç dışına çıkmış,
  gpt-oss kabuğu düşürmüş, İngilizce testte ``h\\textasciitilde{}`` düşmüş. Her parçada
  komut/ayraç iskeleti kaynakla karşılaştırılır; tutmazsa yeniden dene → metin
  korumalı çeviri → özgün dilde bırak + işaretle.

Model yüklenmez, gerçek sunucuya bağlanılmaz: sahte Ollama (``tests/fakes``) ya
da MagicMock çevirmen. Sahte çevirmenler yalnız test altyapısıdır.
"""
from __future__ import annotations

import re
import zlib
from collections import Counter
from unittest.mock import MagicMock

from latex_pipeline.config import PipelineConfig
from latex_pipeline.translator import LatexTranslator
from tests.fakes.fake_llm_server import extract_chat_text
from test_fix_Y1_20260929 import ollama_translator  # noqa: F401  (fikstür)

# ---------------------------------------------------------------------------
# Kanıt satırları: YAPI body.tex'ten (aynı LaTeX atomları, sayılar, satır
# biçimi); İngilizce metin sentetik (KARAR 26). Almanca satırlar sentetik
# Almanca sınama metninden (HY-MT koşusu).
# ---------------------------------------------------------------------------
# İngilizce model testi; günlük: "parçalı yola düşürüldü (9 parça)"; bozuk
# çıktı (çapadan sonra büyük harfli parçalar,
# "…’nin", satır sonunda "is").
EN_PARCALI = (
    "Take a pair of bodies with different masses  $m_1$  and  $m_2$ ,  "
    "$m_1 > m_2$ ; both bodies are perfect spheres and gravity is the only "
    "force acting on them. Pick an inertial reference  $\\mathcal{F}_i$ ; "
    "position arrows  $\\vec{R}_1$  and  $\\vec{R}_2$  give the locations of masses  "
    "$m_1$  and  $m_2$ , respectively. The location of  $m_2$  with respect to  "
    "$m_1$  is")
EN_PARCALI_TR_BOZUK = (
    "Kütlelerin yerlerini verir. $m_1$ ve $m_2$ Sırasıyla. …’nin yeri. "
    "$m_2$ …ye göre $m_1$ is")

# İngilizce model testi: model
# h\textasciitilde{} (vektör oku) düşürdü.
EN_VEKTOR_OKU = (
    "is a constant vector. Because h\\textasciitilde{} points along the normal "
    "of the orbit plane, the craft keeps moving within that one plane. Nothing "
    "about this is unexpected: the only pull is central, no twisting moment acts "
    "on the probe, and so the angular momentum stays fixed.")

# Almanca model testi; günlük: "parçalı yola düşürüldü (4 parça)"; çıktı:
# "λ 20 ile arasında ◦ C ve 600 ◦Ancak C değeri ...".
DE_107 = (
    "Für Überschlagsrechnungen genügt es meist, den Wert bei Raumtemperatur zu "
    "verwenden. Bei Stahl sinkt \\textit{λ} zwischen 20 \\textit{◦}C und 600 "
    "\\textit{◦}C allerdings merklich, sodass genaue Rechnungen die "
    "Temperaturabhängigkeit berücksichtigen müssen.")

# Almanca model testi (mt okuma MT-7):
# "Bi \textit{<} $\leq$ 0 \textit{,} $\leq$ 1"; günlükte
# "Numbers lost in translation: {'01'} not found in result."
DE_153 = (
    "Ist Bi \\textit{<} 0\\textit{,}1, so darf man die Temperatur im Körper als "
    "räumlich konstant ansehen. Die Energiebilanz lautet dann")

# Almanca model testi (retranslator ham \textbf{...} gönderir).
DE_109 = (
    "\\textbf{Tabelle 1.1:} Richtwerte der Dichte \\textit{ρ} und der "
    "Wärmeleitfähigkeit \\textit{λ} bei 20 \\textit{◦}C")

# mt okuma MT-17: Almanca model testinde altyazı.
MT17_SRC = ("\\caption*{\\textbf{Abbildung 1.1:} Temperaturverlauf in einer "
            "ebenen Wand bei stationärer Wärmeleitung}")
MT17_TR = ("\\caption*{\\textbf{Şekil} 1.1:} Durağan ısı iletimi durumunda düz "
           "bir duvardaki sıcaklık değişimi")
# gpt-oss çıktısında \caption* kabuğu düşmüş.
MT17_GPTOSS_TR = "Bir düz duvarın sabit ısı iletiminde sıcaklık dağılımı"

# ---------------------------------------------------------------------------
# Sahte model (yalnız test altyapısı)
# ---------------------------------------------------------------------------
_TR = ("çalışma", "değer", "ışık", "yörünge", "şekil", "güneş", "ağırlık",
       "kütle", "hız", "yüzey", "dönüş", "öğrenci", "kuvvet", "enerji",
       "sıcaklık", "basınç", "akış", "düzlem", "eğri", "açı")
_TOKEN = re.compile(r"(<<[A-Z]+_\d+>>|\[\[[A-Za-z]+_\d+\]\]|@@[A-Za-z]+\d+@@)")
_WORDISH = re.compile(r"[^\W\d_]+(?:_[A-Za-z0-9]+)?")
_LATEX_PH = re.compile(r"<<LATEX_\d+>>")


def _tr_words(text: str) -> str:
    """Sözcük başına kararlı Türkçe sözcük. Değişkene benzeyen belirteç (tek
    harf, alt indisli ``m_1``, Yunan harfi) gerçek model gibi AYNEN kalır;
    yer tutucular, sayılar, noktalama ve LaTeX komut adları da."""
    def word(m: re.Match) -> str:
        w = m.group(0)
        start = m.start()
        if start > 0 and m.string[start - 1] == "\\":
            return w                       # \textbf gibi komut adı
        if len(w) == 1 or "_" in w:
            return w                       # değişken
        tr = _TR[zlib.crc32(w.lower().encode("utf-8")) % len(_TR)]
        return tr[:1].upper() + tr[1:] if w[:1].isupper() else tr
    return "".join(p if _TOKEN.fullmatch(p) else _WORDISH.sub(word, p)
                   for p in _TOKEN.split(text))


def _lose_over(limit: int):
    """``limit``'ten fazla <<LATEX_n>> taşıyan girdide sonuncuyu yutan model
    (yer tutucu yükü artınca kaybın arttığı gözlemi: İngilizce testte 13 satır)."""
    def reply(text: str) -> str:
        out = _tr_words(text)
        phs = _LATEX_PH.findall(out)
        if len(phs) > limit:
            out = out.replace(phs[-1], "", 1)
        return out
    return reply


def _requests(srv) -> list:
    return [extract_chat_text(b["messages"]) for b in srv.requests_to("/api/chat")]


def _ws(text: str) -> str:
    """Boşluk farkını yok say (translate() sonu çift boşlukları tekler)."""
    return " ".join(text.split())


# ===========================================================================
# MT-1 — parçalı yol yok: yeniden dene → sade biçim → özgün dilde bırak
# ===========================================================================
class TestMT1ParcaliYol:
    def test_parcali_satir_sade_bicimle_butun_cevrilir(self, ollama_translator):
        lt, srv = ollama_translator("en", reply=_lose_over(4))
        out = lt.translate(EN_PARCALI)
        sent = _requests(srv)
        # Hiçbir istem satır PARÇASI değil: hepsi cümlenin başını taşır.
        assert sent and all(t.startswith("Take a pair of bodies") for t in sent), sent
        # Bozuk birleştirme izleri yok (… artığı, çevrilmemiş "is" kuyruğu).
        assert "…" not in out and not out.rstrip().endswith(" is")
        # Matematik birebir: her atom kaynaktaki sayıda.
        for atom in ("$m_1$", "$m_2$", "$m_1 > m_2$", "$\\mathcal{F}_i$",
                     "$\\vec{R}_1$", "$\\vec{R}_2$"):
            assert out.count(atom) == EN_PARCALI.count(atom), (atom, out)
        assert lt.stats.recovered_by_simplified == 1
        assert lt.stats.kept_source_segments == 0
        assert lt.stats.placeholder_split_fallbacks == 1
        # Sade biçimde kısa değişkenler düz metin olarak gitti.
        assert any("m_1" in t and "m_2" in t and len(_LATEX_PH.findall(t)) == 4
                   for t in sent), sent

    def test_hic_tutmayan_satir_ozgun_dilde_kalir_ve_isaretlenir(self, ollama_translator):
        lt, srv = ollama_translator("en", reply=_lose_over(0))
        out = lt.translate(EN_PARCALI)
        sent = _requests(srv)
        assert all(t.startswith("Take a pair of bodies") for t in sent), sent
        # özgün satır (translate() sonu çift boşlukları tekler; metin birebir)
        assert _ws(out) == _ws(EN_PARCALI)
        assert EN_PARCALI_TR_BOZUK not in out
        assert lt.stats.kept_source_segments == 1
        kept = lt.stats.kept_source_list
        assert kept and kept[0]["kaynak"].startswith("Take a pair")
        assert "$m_1$" in kept[0]["kaynak"]               # yer tutucusuz önizleme
        assert kept[0]["neden"]

    def test_isaretli_satir_qc_de_cevrilmemis_gorunur(self, ollama_translator):
        from latex_pipeline.qc_engine import QCEngine
        lt, srv = ollama_translator("en", reply=_lose_over(0))
        body = ("\\chapter{Two-body problem}\n\n" + EN_PARCALI + "\n")
        out = lt.translate(body)
        assert _ws(EN_PARCALI) in _ws(out)
        rep = QCEngine().analyze(out, body)
        hits = [i for i in rep.issues
                if "untranslated" in (i.category or "").lower()
                or "untranslated" in (i.message or "").lower()]
        assert hits, [(i.category, i.message) for i in rep.issues]

    def test_sirali_yol_parcalamaz_sade_bicimle_cevirir(self):
        sent = []

        def single(text, *a, **k):
            sent.append(text)
            return _lose_over(2)(text)

        lt = _mock_lt(single_fn=single)
        stashed, stash = lt._stash_protected(DE_107)
        lt._stash_list = stash
        out = lt._translate_line_preserving_placeholders(stashed)
        restored = lt._restore_stash(out, stash)
        assert all(t.startswith("Für Überschlagsrechnungen") for t in sent), sent
        assert restored.count("\\textit{λ}") == 1
        assert restored.count("\\textit{◦}") == 2
        assert "Bei Stahl" not in restored                # çevrildi
        assert lt.stats.recovered_by_simplified == 1

    def test_sirali_yol_hic_tutmazsa_satir_aynen_kalir(self):
        lt = _mock_lt(single_fn=lambda t, *a, **k: _lose_over(0)(t))
        stashed, stash = lt._stash_protected(DE_107)
        lt._stash_list = stash
        out = lt._translate_line_preserving_placeholders(stashed)
        assert out == stashed
        assert lt.stats.kept_source_segments == 1

    def test_altyazi_yolu_parcalamaz(self):
        calls = []

        def batch(texts):
            calls.append(list(texts))
            return list(texts)                           # yankı

        lt = _mock_lt(batch_fn=batch, single_fn=lambda t, *a, **k: t)
        caps = ["<<LATEX_7>>: Kepler's second law.",
                "<<LATEX_9>>: Outline of Kepler's ellipse and its swept areas."]
        outs = lt._translate_lines_preserving_placeholders_batch(caps)
        assert outs == caps                              # özgün, parça yok
        assert len(calls) == 1                           # ikinci (parça) toplu çağrı yok
        assert lt.stats.kept_source_segments == 2


    def test_sembol_satiri_yankisi_isaretlenmez(self):
        """Düzyazısız (sayı/sembol) yer tutuculu satırın yankısı meşrudur:
        kurtarma sırası işlemez, satır 'çevrilemedi' diye işaretlenmez."""
        calls = []

        def batch(texts):
            calls.append(list(texts))
            return list(texts)

        lt = _mock_lt(batch_fn=batch, source_lang="en")
        line = "<<LATEX_3>> = 5 <<LATEX_4>> km"
        assert lt._translate_paragraphs(line) == line
        assert len(calls) == 1
        assert lt.stats.kept_source_segments == 0
        assert lt.stats.placeholder_split_fallbacks == 0

# ===========================================================================
# MT-7 — sayı koruması geri alır; uydurma ilişki simgesi basılmaz
# ===========================================================================
def _insert_leq(text: str) -> str:
    """Almanca model testinde modelin yaptığı: '0<<LATEX_b>>1' arasına iki '≤' ekler
    (retranslator'ın <<RT_n>> belirteçlerinde de)."""
    return re.sub(r"0(<<[A-Z]+_\d+>>)1", r"≤ 0 \1 ≤ 1", _tr_words(text))


class TestMT7SayiKorumasi:
    def test_bi_satiri_uydurma_simgeyle_basilmaz(self, ollama_translator):
        lt, srv = ollama_translator("de", reply=_insert_leq)
        out = lt.translate(DE_153)
        assert "≤" not in out and "\\leq" not in out
        assert _ws(out) == _ws(DE_153)                   # özgün (Almanca) kaldı
        assert lt.stats.number_reverts >= 1
        assert lt.stats.kept_source_segments == 1

    def test_tekrar_denemede_temiz_ceviri_kabul_edilir(self, ollama_translator):
        seen = []

        def reply(text: str) -> str:
            seen.append(text)
            return _insert_leq(text) if len(seen) == 1 else _tr_words(text)

        lt, srv = ollama_translator("de", reply=reply)
        out = lt.translate(DE_153)
        assert "≤" not in out
        assert "\\textit{<} 0\\textit{,}1" in out         # eşik birebir
        assert "Temperatur" not in out                   # çevrildi
        assert lt.stats.kept_source_segments == 0

    def test_numara_kaybi_geri_alinir(self, make_latex_translator):
        original = "Call us at fax 4002 for details."
        bad = "Bize faks yoluyla ulaşın."
        lt = make_latex_translator(single_fn=lambda text: "Yine sayısız çeviri.")
        assert lt._check_numerical_preservation(bad, original) == original
        assert lt.stats.number_reverts == 1

    def test_yer_tutucu_bitisik_rakam_yanlis_alarm_vermez(self, make_latex_translator):
        calls = []
        lt = make_latex_translator(single_fn=lambda text: calls.append(text) or text)
        original = "Ist Bi <<LATEX_64>> 0<<LATEX_65>>1, so darf man"
        good = "Bi <<LATEX_64>> 0 <<LATEX_65>> 1 ise, şöyle"
        assert lt._check_numerical_preservation(good, original) == good
        assert calls == []

    def test_iliski_simgesi_sayimi(self):
        from latex_pipeline.translator import new_relation_symbols
        assert new_relation_symbols("Ist Bi <<LATEX_64>> 0<<LATEX_65>>1",
                                    "Bi <<LATEX_64>> ≤ 0 <<LATEX_65>> ≤ 1") == Counter({"≤": 2})
        assert not new_relation_symbols("x= const", "x = sabit")
        # yer tutucunun '<'/'>' işaretleri simge sayılmaz
        assert not new_relation_symbols("a <<LATEX_1>> b", "<<LATEX_1>> a b")

    def test_retranslator_uydurma_simgeyi_kabul_etmez(self):
        from latex_pipeline.retranslator import _retranslate_line
        line = DE_153
        out = _retranslate_line(line, _insert_leq, source_lang="de")
        assert "≤" not in out
        assert out == line


# ===========================================================================
# MT-17 — LaTeX iskeleti her parçada
# ===========================================================================
class TestMT17Iskelet:
    def test_iskelet_denetimi_kanit_bicimlerini_yakalar(self):
        from latex_pipeline.translator import latex_skeleton_problem
        assert latex_skeleton_problem(MT17_SRC, MT17_TR)            # metin dışarıda
        assert latex_skeleton_problem(MT17_SRC, MT17_GPTOSS_TR)     # kabuk düşmüş
        assert latex_skeleton_problem(EN_VEKTOR_OKU, EN_VEKTOR_OKU.replace(
            "h\\textasciitilde{}", "h"))
        # meşru Türkçe söz dizimi: metin grubun dışına taşmadan yer değiştirir
        assert latex_skeleton_problem(
            "the \\emph{important} result", "\\emph{önemli} sonuç") is None
        assert latex_skeleton_problem(
            "Note <<LATEX_1>> \\textbf{here}", "<<LATEX_1>> \\textbf{burada} not") is None

    def test_vektor_oku_textasciitilde_dusmez(self, ollama_translator):
        def reply(text: str) -> str:
            # gerçek modelin yaptığı: ham \textasciitilde{} düşer
            return _tr_words(text.replace("\\textasciitilde{}", ""))

        lt, srv = ollama_translator("en", reply=reply)
        out = lt.translate(EN_VEKTOR_OKU)
        assert "h\\textasciitilde{}" in out
        assert "constant vector" not in out               # çevrildi
        assert lt.stats.skeleton_reverts >= 1
        assert lt.stats.recovered_by_simplified == 1

    def test_retranslator_kabugu_silinen_satiri_kurtarir(self):
        from latex_pipeline.retranslator import _retranslate_line
        calls = []

        def translator_fn(text: str) -> str:
            calls.append(text)
            # gpt-oss'un altyazıda yaptığı: ham \textbf{...} kabuğunu siler
            text = re.sub(r"\\textbf\{([^{}]*)\}", r"\1", text)
            return _tr_words(text)

        out = _retranslate_line(DE_109, translator_fn, source_lang="de")
        assert out.startswith("\\textbf{"), out
        assert "Richtwerte" not in out                    # çevrildi
        assert "\\textit{ρ}" in out and "\\textit{λ}" in out
        assert len(calls) == 3                            # dene, tekrar, korumalı

    def test_retranslator_hic_tutmazsa_satir_aynen_kalir(self):
        from latex_pipeline.retranslator import _retranslate_line

        def translator_fn(text: str) -> str:
            # her yer tutucuyu ve kabuğu düşüren model
            return _tr_words(re.sub(r"<<[A-Z]+_\d+>>|\\textbf\{|\}", "", text))

        assert _retranslate_line(DE_109, translator_fn, source_lang="de") == DE_109

    def test_retranslator_gercek_kopru_ile_kurtarir(self):
        """translate_book köprüsü: translator_fn = LatexTranslator._safe_translate.
        Bekçi iskelet bozulmasını görür, kurtarma metin korumalı biçimle çevirir."""
        from latex_pipeline.retranslator import scan_and_retranslate

        def model(text, *a, **k):
            return _tr_words(re.sub(r"\\textbf\{([^{}]*)\}", r"\1", text))

        lt = _mock_lt(batch_fn=lambda texts: [model(t) for t in texts],
                      single_fn=model)
        res = scan_and_retranslate(DE_109 + "\n", translator_fn=lt._safe_translate,
                                   source_lang="de")
        line = res.output_tex.split("\n")[0]
        assert line.startswith("\\textbf{"), line
        assert "Richtwerte" not in line
        assert lt.stats.skeleton_reverts >= 1
        assert lt.stats.recovered_by_simplified == 1


    def test_retranslator_baslik_iskeleti_korunur(self):
        """Kanıtta ham komutlu başlık yok (İngilizce testteki \\textit{n} yer tutucu);
        gpt-oss'un kabuk silme biçimi başlık yoluna uygulandı."""
        from latex_pipeline.retranslator import _retranslate_heading_line
        line = "\\section{The \\emph{Two-Body} Problem}"
        seen = []

        def heading_fn(text: str) -> str:
            seen.append(text)
            # ham \emph{...} kabuğunu düşüren model
            return _tr_words(re.sub(r"\\emph\{([^{}]*)\}", r"\1", text))

        out = _retranslate_heading_line(line, heading_fn, source_lang="en")
        assert out.startswith("\\section{") and "\\emph{" in out, out
        assert "Problem" not in out
        assert len(seen) == 3                     # dene, tekrar, korumalı

# ===========================================================================
# Yardımcı birimler (kabuk koruma, sade biçim)
# ===========================================================================
class TestYardimcilar:
    def test_kabuk_koruma_gidis_donus(self):
        from latex_pipeline.translator import (protect_latex_shells,
                                               restore_latex_shells)
        sent, shells = protect_latex_shells(DE_109)
        assert "\\textbf" not in sent and "Tabelle 1.1:" in sent
        assert restore_latex_shells(sent, sent, shells) == DE_109

    def test_kabuk_sirasi_ve_dolulugu_denetlenir(self):
        from latex_pipeline.translator import (protect_latex_shells,
                                               restore_latex_shells)
        src = "the \\emph{important} result"
        sent, shells = protect_latex_shells(src)
        o, c = shells[0]["ph"], shells[1]["ph"]
        assert restore_latex_shells(f"{o}önemli{c} sonuç", sent, shells) == \
            "\\emph{önemli} sonuç"
        assert restore_latex_shells(f"{c}önemli{o} sonuç", sent, shells) is None  # ters
        assert restore_latex_shells(f"{o}{c}önemli sonuç", sent, shells) is None  # boşaldı
        assert restore_latex_shells(f"{o}önemli sonuç", sent, shells) is None     # kayıp

    def test_argumansiz_komut_harfe_yapismaz(self):
        from latex_pipeline.translator import (protect_latex_shells,
                                               restore_latex_shells)
        sent, shells = protect_latex_shells("and so on \\ldots here")
        ph = shells[0]["ph"]
        assert restore_latex_shells(f"ve {ph}burada", sent, shells) == "ve \\ldots{}burada"

    def test_duz_degisken_secimi(self):
        from latex_pipeline.translator import plain_variable
        assert plain_variable("$m_1$") == "m_1"
        assert plain_variable("$v_{L}$") == "v_L"
        assert plain_variable("$\\mu$") == "μ"
        assert plain_variable("\\textit{λ}") == "λ"
        for item in ("$m_1 > m_2$", "$\\vec{R}_1$", "$\\dot{Q}$", "\\textit{◦}",
                     "\\textit{<}", "\\textbf{x}"):
            assert plain_variable(item) is None, item

    def test_sade_bicim_sayim_tutmazsa_reddedilir(self):
        from latex_pipeline.translator import unsimplify_variables
        vm = {"m_1": ["<<LATEX_0>>", "<<LATEX_6>>"]}
        assert unsimplify_variables("m_1 ve m_1'in", vm) == "<<LATEX_0>> ve <<LATEX_6>>'in"
        assert unsimplify_variables("yalnız m_1", vm) is None
        assert unsimplify_variables("m_12 ve m_1 m_1", vm) == "m_12 ve <<LATEX_0>> <<LATEX_6>>"

    def test_sade_bicim_cakisan_degiskeni_sadelestirmez(self):
        lt = _mock_lt(source_lang="en")
        lt._stash_list = ["$a$", "$m_1$"]
        sent, _ = lt._simplified_form("Take a body <<LATEX_0>> of mass <<LATEX_1>> now")
        assert "<<LATEX_0>>" in sent          # "a" metinde zaten var
        assert "m_1" in sent and "<<LATEX_1>>" not in sent


# ---------------------------------------------------------------------------
def _mock_lt(batch_fn=None, single_fn=None, source_lang="de") -> LatexTranslator:
    lt = LatexTranslator(
        pipeline_config=PipelineConfig(translate=True, segment_timeout=60),
        mt_engine="hymt-7b", source_lang=source_lang, target_lang="tr",
    )
    bt = MagicMock()
    bt.translate_batch_protected = batch_fn or (lambda texts: [_tr_words(t) for t in texts])
    bt.translate_text = single_fn or (lambda t, *a, **k: _tr_words(t))
    lt._translator = bt
    return lt

