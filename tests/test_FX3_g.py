"""FX3 (5 Ekim 2026) — "Yerel model ile çevir" zincirinin sağlamlığı.

Kalemler:
  0a  Bağlam grubunda display'den sonra gelen, kaynakta karşılığı olmayan
      Türkçe devam parçası ("… elde edilir.") display'e BOŞ SATIRSIZ eklenir
      (aynı paragrafın devamı); eskiden ayrı paragraf basılıyor, QC 29a
      hizası kayıyordu.
  0b  QC 29a: aynı display/şekil (çapa) iki tarafta birebir durur; kısa
      eşlenmemiş paragraf atlanıp çapada yeniden hizalanır; çapanın iki
      yanındaki metin yer değiştirdiyse (Türkçe söz dizimi) bilgi kaybı
      sayılmaz. Gerçek kayıp/şişme yine yakalanır.

Bütün metinler SENTETİKTİR (KARAR 26): kitap cümlesi kopyalanmadı; olgunun
yapısı (kısa bağlaç parçası + display + şekil, Türkçe fiilin display'in öbür
yanına geçmesi) aynen kuruldu.
"""
from __future__ import annotations

import re
import zlib
from unittest.mock import MagicMock

from latex_pipeline.config import PipelineConfig
from latex_pipeline.qc_engine import QCEngine
from latex_pipeline.translator import LatexTranslator

# ---------------------------------------------------------------------------
# Sahte model (yalnız test altyapısı)
# ---------------------------------------------------------------------------
_TR = ("çalışma", "değer", "ışık", "yörünge", "şekil", "güneş", "ağırlık",
       "kütle", "hız", "yüzey", "dönüş", "öğrenci", "kuvvet", "enerji",
       "sıcaklık", "basınç", "akış", "düzlem", "eğri", "açı")
_TOKEN = re.compile(r"(<<[A-Z]+_\d+>>|\[\[[A-Za-z]+_\d+\]\]|@@[A-Za-z]+\d+@@)")
_WORDISH = re.compile(r"[^\W\d_]+(?:_[A-Za-z0-9]+)?")


def _tr_words(text: str) -> str:
    """Sözcük başına kararlı Türkçe sözcük; yer tutucu, sayı, noktalama ve
    LaTeX komut adları aynen kalır."""
    def word(m: re.Match) -> str:
        w = m.group(0)
        if m.start() > 0 and m.string[m.start() - 1] == "\\":
            return w
        if len(w) == 1 or "_" in w:
            return w
        tr = _TR[zlib.crc32(w.lower().encode("utf-8")) % len(_TR)]
        return tr[:1].upper() + tr[1:] if w[:1].isupper() else tr
    return "".join(p if _TOKEN.fullmatch(p) else _WORDISH.sub(word, p)
                   for p in _TOKEN.split(text))


def _mock_lt(batch_fn=None, single_fn=None, source_lang="en",
             calls=None) -> LatexTranslator:
    """MagicMock çevirmenli LatexTranslator; *calls* listesine modele giden
    her metin eklenir."""
    lt = LatexTranslator(
        pipeline_config=PipelineConfig(translate=True, segment_timeout=60),
        mt_engine="hymt-7b", source_lang=source_lang, target_lang="tr",
    )
    sent = calls if calls is not None else []
    b_fn = batch_fn or (lambda texts: [_tr_words(t) for t in texts])
    s_fn = single_fn or (lambda t, *a, **k: _tr_words(t))

    def batch(texts):
        sent.extend(texts)
        return b_fn(texts)

    def single(text, *a, **k):
        sent.append(text)
        return s_fn(text, *a, **k)

    bt = MagicMock()
    bt.translate_batch_protected = batch
    bt.translate_text = single
    lt._translator = bt
    return lt


def _paras(text):
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def _rule_model(rules):
    """*rules*: [(desen, şablon)] — desen modele giden metne tam uyarsa
    şablon (grup adlarıyla) döner; yoksa _tr_words."""
    compiled = [(re.compile(p), t) for p, t in rules]

    def one(text):
        for pat, tmpl in compiled:
            m = pat.fullmatch(text)
            if m:
                return m.expand(tmpl)
        return _tr_words(text)

    return (lambda texts: [one(t) for t in texts],
            lambda t, *a, **k: one(t))


EQ_A = "\\[\nx = y + z\n\\]"
EQ_B = "\\[\nu = v\\,w\n\\]"


# ===========================================================================
# Kalem 0a — display sonrası Türkçe devam parçası aynı paragrafta kalır
# ===========================================================================
class TestBaglacDevamParcasi:
    def test_tek_display_devam_bos_satirsiz_eklenir(self):
        body = ("\\section{Sums}\n\nBy adding both sides, one gets\n\n" + EQ_A
                + "\n\n\\section{Next part}\n")
        b, s = _rule_model([(r"By adding both sides, one gets (<<LATEX_\d+>>)",
                             r"İki taraf toplanarak \1 bulunur.")])
        lt = _mock_lt(batch_fn=b, single_fn=s)
        out = lt.translate(body)
        assert EQ_A + "\nbulunur." in out, out
        # Kaynakta karşılığı olmayan devam ayrı paragraf DEĞİL: sayı aynı.
        assert len(_paras(out)) == len(_paras(body)), _paras(out)
        assert "İki taraf toplanarak" in _paras(out)
        assert lt.stats.context_groups == 1

    def test_iki_display_son_devam_bos_satirsiz(self):
        body = ("\\section{Area}\n\nAs a result, the area\n\n" + EQ_A
                + "\n\ncan be rewritten as\n\n" + EQ_B
                + "\n\n\\section{Next part}\n")
        b, s = _rule_model([(
            r"As a result, the area (<<LATEX_\d+>>) can be rewritten as "
            r"(<<LATEX_\d+>>)",
            r"Sonuç olarak alan \1 ifadesi \2 biçiminde yazılabilir.")])
        lt = _mock_lt(batch_fn=b, single_fn=s)
        out = lt.translate(body)
        ps = _paras(out)
        assert len(ps) == len(_paras(body)), ps
        i_a = ps.index(EQ_A)
        assert ps[i_a - 1] == "Sonuç olarak alan"
        assert ps[i_a + 1] == "ifadesi"
        assert ps[i_a + 2] == EQ_B + "\nbiçiminde yazılabilir."
        assert lt.stats.context_groups == 1

    def test_on_parca_bos_devam_display_e_eklenir(self):
        """Model display'i başa aldı ve kaynakta devam paragrafı yok: parça
        paragrafı erir (içerik display'in ardına geçti), devam display'e
        boş satırsız eklenir; display birebir ve tek."""
        body = ("\\section{Rates}\n\nRecalling the identity\n\n" + EQ_A
                + "\n\n\\section{Next part}\n")
        b, s = _rule_model([(r"Recalling the identity (<<LATEX_\d+>>)",
                             r"\1 özdeşliği hatırlanarak")])
        lt = _mock_lt(batch_fn=b, single_fn=s)
        out = lt.translate(body)
        assert out.count(EQ_A) == 1
        assert EQ_A + "\nözdeşliği hatırlanarak" in out, out
        assert "Recalling" not in out

    def test_kaynakta_devam_varsa_ayri_paragraf_kalir(self):
        """Kaynakta display'den sonra devam paragrafı VARSA ona düşen çeviri
        kendi paragrafında kalır (kaynağın ayracı)."""
        tail = "is the sum of the two parts taken together."
        body = ("\\section{Sums}\n\nwhere\n\n" + EQ_A + "\n\n" + tail
                + "\n\n\\section{Next part}\n")
        b, s = _rule_model([(
            r"where (<<LATEX_\d+>>) is the sum of the two parts taken together\.",
            r"burada \1 iki parçanın toplamıdır.")])
        lt = _mock_lt(batch_fn=b, single_fn=s)
        out = lt.translate(body)
        ps = _paras(out)
        i_a = ps.index(EQ_A)
        assert ps[i_a - 1] == "burada"
        assert ps[i_a + 1] == "iki parçanın toplamıdır."
        assert len(ps) == len(_paras(body))


# ===========================================================================
# Kalem 0b — QC 29a çapa (display/şekil) yeniden hizalaması
# ===========================================================================
def _fig(name, caption):
    return ("\\begin{figure}[H]\n\\centering\n\\includegraphics[width=0.5\\textwidth]{"
            + name + "}\n\\caption*{" + caption + "}\n\\end{figure}")


_P1 = ("The first chapter introduces the basic quantities that appear in every "
       "later derivation, together with the units used throughout the text.")
_P1_TR = ("Birinci bölüm, sonraki her türetmede görünen temel nicelikleri ve metin "
          "boyunca kullanılan birimleri tanıtır.")
_P2 = ("A second relation follows from the same reasoning once the vectors are "
       "written in components and the common factor is removed from both sides.")
_P2_TR = ("Vektörler bileşenleriyle yazılıp ortak çarpan iki taraftan atıldığında "
          "aynı akıl yürütmeden ikinci bir bağıntı çıkar.")
_P3 = ("lets one determine the direction of the vector without computing its "
       "length, which simplifies the following steps considerably.")
_P3_TR = ("Skaler çarpım, vektörün uzunluğunu hesaplamadan yönünü belirlemeyi "
          "sağlar; bu, sonraki adımları epeyce sadeleştirir.")
_P4 = ("The last part of the section collects the results in a table and compares "
       "them with the values obtained by direct numerical integration.")
_P4_TR = ("Bölümün son kısmı sonuçları bir tabloda toplar ve doğrudan sayısal "
          "tümlevle elde edilen değerlerle karşılaştırır.")


def _en_doc():
    return "\n\n".join([
        _P1, "By adding both sides, one gets", EQ_A,
        _fig("a.png", "Figure 1.1: A sketch of the setup."),
        _P2, "The scalar product", EQ_B, _P3, _P4,
    ])


def _halluc_paras(tr_text, en_text):
    report = QCEngine().analyze(tr_text, en_text)
    return [i for i in report.issues
            if i.check_name == "hallucination" and "paragraph" in i.message]


class TestQC29aCapaHizasi:
    def test_kisa_devam_ve_eriyen_parca_zincirleme_critical_uretmez(self):
        # TR: display'den sonra kaynakta olmayan kısa devam ("bulunur.") VE
        # ikinci grupta parça paragrafı erimiş (içerik display'in ardında):
        # paragraf sayısı EŞİT, hiza yerel olarak +1 sonra -1 kayıyor.
        tr = "\n\n".join([
            _P1_TR, "İki taraf toplanarak", EQ_A, "bulunur.",
            _fig("a.png", "Şekil 1.1: Düzeneğin bir çizimi."),
            _P2_TR, EQ_B, _P3_TR, _P4_TR,
        ])
        en = _en_doc()
        assert len(_paras(tr)) == len(_paras(en))
        assert _halluc_paras(tr, en) == []

    def test_devam_display_e_bitisikse_de_hizali(self):
        tr = "\n\n".join([
            _P1_TR, "İki taraf toplanarak", EQ_A + "\nbulunur.",
            _fig("a.png", "Şekil 1.1: Düzeneğin bir çizimi."),
            _P2_TR, EQ_B, _P3_TR, _P4_TR,
        ])
        assert _halluc_paras(tr, _en_doc()) == []

    def test_gercek_bilgi_kaybi_capa_varken_yakalanir(self):
        tr = "\n\n".join([
            _P1_TR, "İki taraf toplanarak", EQ_A, "bulunur.",
            _fig("a.png", "Şekil 1.1: Düzeneğin bir çizimi."),
            "Vektörler bileşenleriyle.",            # _P2 kesilmiş (gerçek kayıp)
            EQ_B, _P3_TR, _P4_TR,
        ])
        found = _halluc_paras(tr, _en_doc())
        assert len(found) == 1, [i.message for i in found]
        assert "Information loss" in found[0].message
        assert "Vektörler bileşenleriyle." in found[0].context

    def test_gercek_siskinlik_capa_varken_yakalanir(self):
        tr = "\n\n".join([
            _P1_TR, "İki taraf toplanarak", EQ_A, "bulunur.",
            _fig("a.png", "Şekil 1.1: Düzeneğin bir çizimi."),
            _P2_TR, EQ_B, _P3_TR, _P4_TR + " " + _P4_TR + " " + _P4_TR + " " + _P4_TR,
        ])
        found = _halluc_paras(tr, _en_doc())
        assert len(found) == 1, [i.message for i in found]
        assert "hallucination/repetition" in found[0].message

    def test_kisa_olmayan_fazla_paragraf_atlanmaz(self):
        # Display'den sonra UZUN (kaynakta olmayan) paragraf kısa devam
        # değildir: çapa hipotezi onu atlamaz (yalnız kısa, çapa olmayan
        # paragraf atlanır — 29a'nın zaten yargılamadığı boy).
        key = QCEngine._para_anchor_key
        en = [_P1, EQ_A, _fig("a.png", "Figure 1.1: A sketch.")]
        long_extra = ("bulunur ve bu sonuç, bölümün geri kalanında defalarca "
                      "kullanılacak olan önemli bir ara adımdır.")
        tr = [_P1_TR, EQ_A, long_extra, _fig("a.png", "Şekil 1.1: Bir çizim.")]
        en_k = [key(p) for p in en]
        tr_k = [key(p) for p in tr]
        assert en_k[2] is not None and en_k[2] == tr_k[3]
        assert QCEngine._para_anchor_resync(en, tr, en_k, tr_k, 2, 2) is None
        tr[2] = "bulunur."
        assert QCEngine._para_anchor_resync(en, tr, en_k, tr_k, 2, 2) == (2, 3)

    def test_capa_anahtari_display_devamini_ve_altyaziyi_saymaz(self):
        key = QCEngine._para_anchor_key
        assert key("\\[\nx = y + z\n\\]") == key("\\[\nx = y + z\n\\]\nbulunur.")
        assert (key("\\centering\n\\includegraphics[width=2in]{a.png}\n"
                    "\\caption*{Figure 1: A.}")
                == key("\\centering\n\\includegraphics[width=2in]{a.png}\n"
                       "\\caption*{Şekil 1: A.}"))
        assert key(_P1) is None and key("bulunur.") is None

    def test_capanin_iki_yaninda_yer_degistirme_kayip_sayilmaz(self):
        # Türkçe söz dizimi: kaynakta display'den SONRA gelen tanım cümlesi
        # display'den önceki paragrafa geçti; sonraki paragraf yalnız fiil.
        lead = "The travel time along the curved path is"          # < 50
        defn = "where the auxiliary angle D is introduced as follows"  # >= 50
        en = "\n\n".join([_P1, lead, EQ_A, defn, EQ_B, _P4])
        tr = "\n\n".join([
            _P1_TR,
            "Eğri yol boyunca geçen süre, yardımcı açı D aşağıdaki gibi tanımlandığında",
            EQ_A, "şeklindedir.", EQ_B, _P4_TR,
        ])
        assert _halluc_paras(tr, en) == []

    def test_yer_degistirme_telafisi_yoksa_kayip_yakalanir(self):
        # Önceki paragraf uzamadı (içerik taşınmadı): kısa paragraf gerçek kayıp.
        lead = "The travel time along the curved path is"
        defn = "where the auxiliary angle D is introduced as follows"
        en = "\n\n".join([_P1, lead, EQ_A, defn, EQ_B, _P4])
        tr = "\n\n".join([
            _P1_TR, "Eğri yol boyunca süre", EQ_A, "şeklindedir.", EQ_B, _P4_TR,
        ])
        found = _halluc_paras(tr, en)
        assert len(found) == 1 and "Information loss" in found[0].message

    def test_hizali_belge_degismedi(self):
        tr = "\n\n".join([
            _P1_TR, "İki taraf toplanarak", EQ_A,
            _fig("a.png", "Şekil 1.1: Düzeneğin bir çizimi."),
            _P2_TR, "Skaler çarpım", EQ_B, _P3_TR, _P4_TR,
        ])
        assert _halluc_paras(tr, _en_doc()) == []


# ===========================================================================
# Kalem 0 (MT-13) — tutmayan bağlam grubunda komşu cümleyle daraltılmış grup
# ===========================================================================
_SHAPE_BODY = ("\\section{Shape}\n\nBy collecting the terms, one gets\n\n" + EQ_A
               + "\n\nwhich is the general form of the curve. The constant $k$ is "
               "called the shape factor of the curve.\n\n\\section{Next part}\n")


def _lossy_when(pred):
    """*pred(metin)* doğruysa son yer tutucuyu yutan sahte model."""
    def one(t):
        out = _tr_words(t)
        if pred(t):
            phs = re.findall(r"<<LATEX_\d+>>", out)
            if phs:
                out = out.replace(phs[-1], "", 1)
        return out
    return (lambda ts: [one(t) for t in ts]), (lambda t, *a, **k: one(t))


class TestDaraltilmisBaglamGrubu:
    def test_tutmayan_grup_ilk_cumleyle_yeniden_denenir(self):
        calls = []
        # Yalnız "constant" cümlesini İÇEREN grup metni bozulur.
        b, s = _lossy_when(lambda t: t.startswith("By collecting") and "constant" in t)
        lt = _mock_lt(batch_fn=b, single_fn=s, calls=calls)
        out = lt.translate(_SHAPE_BODY)
        reduced = [c for c in calls if c.startswith("By collecting the terms, one gets <<LATEX_")
                   and c.endswith("general form of the curve.")]
        assert reduced, calls
        # Parça TEK BAŞINA modele gitmedi (eski "Çarpım noktası" yolu).
        assert "By collecting the terms, one gets" not in [c.strip() for c in calls]
        ps = _paras(out)
        i_a = ps.index(EQ_A)
        assert "By collecting" not in ps[i_a - 1]
        # Kalan cümle aynı devam paragrafında, matematik birebir.
        assert "$k$" in ps[i_a + 1] and "constant" not in ps[i_a + 1]
        assert len(ps) == len(_paras(_SHAPE_BODY))
        assert lt.stats.context_groups == 1
        assert lt.stats.context_groups_reduced == 1
        assert lt.stats.context_group_fallbacks == 0

    def test_daraltilmis_da_tutmazsa_eski_ayri_ceviri_kayipsiz(self):
        calls = []
        b, s = _lossy_when(lambda t: t.startswith("By collecting") and "<<LATEX_" in t)
        lt = _mock_lt(batch_fn=b, single_fn=s, calls=calls)
        out = lt.translate(_SHAPE_BODY)
        assert "By collecting the terms, one gets" in [c.strip() for c in calls]
        assert out.count(EQ_A) == 1 and "$k$" in out
        assert lt.stats.context_group_fallbacks == 1
        assert lt.stats.context_groups_reduced == 0

    def test_tek_cumlelik_devamda_daraltma_denenmez(self):
        calls = []
        body = ("\\section{Shape}\n\nBy collecting the terms, one gets\n\n" + EQ_A
                + "\n\nwhich is the general form of the curve.\n\n\\section{Next part}\n")
        b, s = _lossy_when(lambda t: t.startswith("By collecting") and "<<LATEX_" in t)
        lt = _mock_lt(batch_fn=b, single_fn=s, calls=calls)
        lt.translate(body)
        grouped = [c for c in calls if c.startswith("By collecting the terms, one gets <<LATEX_")]
        # Tam grup (ilk deneme + kurtarma) dışında ek daraltılmış deneme yok.
        assert all(c.endswith("general form of the curve.") for c in grouped)
        assert lt.stats.context_groups_reduced == 0


# ===========================================================================
# Kalem 0 (gerçek HY-MT çıktısında kalan bulgu) — "[[FORMÜLA_n]]" onarılmıyordu
# ===========================================================================
def _bare_bt():
    from types import SimpleNamespace

    from core.translator import BookTranslator
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = SimpleNamespace(source_lang="en")
    bt.stats = {}
    return bt


class TestBozukFormulYerTutucusu:
    def test_formula_turkcelesmis_govde_onarilir(self):
        # Bir model testinde model "[[FORMULA_0]]" -> "[[FORMÜLA_0]]"; onarım deseni
        # "FORMÜL" + rakam bekliyordu, "A" yüzünden tutmuyordu -> kalıntı +
        # formül yer değiştirdi (QC 3 HIGH).
        bt = _bare_bt()
        out = bt._restore_formulas(
            "açı [[FORMÜLA_0]], hız [[Formüla_1]] ve [[formüla 2]]",
            ["$\\alpha$", "$v$", "$w$"])
        assert out == "açı $\\alpha$, hız $v$ ve $w$"
        assert bt.stats["placeholders_repaired"] == 3

    def test_eski_bicimler_hala_onarilir(self):
        bt = _bare_bt()
        assert bt._restore_formulas("a [[FORMÜL_1]] b [[FORMULA_0]]",
                                    ["$x$", "$y$"]) == "a $y$ b $x$"

    def test_latex_bekcisi_bozuk_kalintiyi_yakalar(self):
        lt = _mock_lt()
        orig = "The angle <<LATEX_3>> grows slowly."
        out = lt._post_process_translation("Açı [[FORMÜLA_0]] yavaşça büyür.", orig)
        assert out == orig
        assert lt.stats.remnant_reverts == 1


# ===========================================================================
# Kalem 1 (V2 Y1) — bellek zehirlenmesi: yazım latex bekçisi kabul ettikten
# SONRA; kurtarma denemeleri belleği atlar; reddedilen çeviri bellekten silinir
# ===========================================================================
import collections  # noqa: E402

import pytest  # noqa: E402

_Y1_NUM = ("The survey contains exactly 1250 single values that were recorded on "
           "two separate days in the laboratory.")
_Y1_AMP = ("The publisher Hartmann \\& Sons printed the results in a small edition "
           "during the following year.")
_Y1_BODY = "\\section{Values}\n\n" + _Y1_NUM + "\n\n" + _Y1_AMP + "\n"


def _y1_reply(calls, poison_first=True):
    def reply(text):
        calls[text] += 1
        out = _tr_words(text)
        if poison_first and calls[text] == 1:
            out = out.replace("1250", "").replace("\\&", "&")
        return out
    return reply


@pytest.fixture
def y1_engine(fake_llm_server, monkeypatch):
    import core.mt_engines as cme
    from core.mt_engines.base import EngineSpec
    from core.mt_engines.chat import ChatTemplateEngine

    calls = collections.Counter()
    srv = fake_llm_server("ollama", models=["sahte"], reply=_y1_reply(calls))
    spec = EngineSpec(name="ollama:sahte", format="chat", backend="ollama",
                      model="sahte", base_url=srv.url, source="adhoc")
    monkeypatch.setattr(cme, "create_engine",
                        lambda name, *a, **k: ChatTemplateEngine(spec))
    return srv, calls


def _y1_lt(db):
    return LatexTranslator(
        pipeline_config=PipelineConfig(translate=True, segment_timeout=30),
        mt_engine="ollama:sahte", source_lang="en", target_lang="tr",
        project_name="fx3-bellek",
        translation_overrides=dict(memory_db_path=str(db), use_memory=True,
                                   glossary_mode="off", run_qa=False,
                                   check_consistency=False))


def _y1_memory(db):
    from core.memory import TranslationMemory, memory_project_key
    return TranslationMemory(db_path=str(db),
                             project_name=memory_project_key("fx3-bellek", "ollama:sahte"))


class TestBellekZehri:
    def test_ilk_kosu_tekrar_modele_gider_bellek_temiz(self, y1_engine, tmp_path):
        srv, calls = y1_engine
        db = tmp_path / "tm.db"
        lt = _y1_lt(db)
        out = lt.translate(_Y1_BODY)
        lt._translator.memory.close()
        # Sayı düşüren ilk çeviri reddedildi; tekrar MODELE gitti ve tuttu.
        assert calls[_Y1_NUM] >= 2, dict(calls)
        assert "1250" in out and "survey" not in out
        assert "\\&" in out and "publisher" not in out
        assert lt.stats.kept_source_segments == 0
        with _y1_memory(db) as tm:
            num = tm.get(_Y1_NUM)
            amp = tm.get(_Y1_AMP)
        # Bellekte yalnız KABUL edilen çeviri var (sayı ve \& sağlam).
        assert num is not None and "1250" in num["target_text"]
        assert amp is not None and "\\&" in amp["target_text"]

    def test_eski_zehirli_kayit_reddedilip_silinir(self, y1_engine, tmp_path):
        srv, calls = y1_engine
        db = tmp_path / "tm.db"
        # Eski kodun yazdığı bozuk kayıtlar (sayı düşmüş, \& kaçışsız).
        with _y1_memory(db) as tm:
            assert tm.add(_Y1_NUM, _tr_words(_Y1_NUM).replace("1250", ""))
            assert tm.add(_Y1_AMP, _tr_words(_Y1_AMP).replace("\\&", "&"))
        calls.update({_Y1_NUM: 1, _Y1_AMP: 1})      # model artık sağlıklı
        lt = _y1_lt(db)
        out = lt.translate(_Y1_BODY)
        lt._translator.memory.close()
        assert "1250" in out and "\\&" in out
        assert lt.stats.kept_source_segments == 0
        with _y1_memory(db) as tm:
            assert "1250" in tm.get(_Y1_NUM)["target_text"]
            assert "\\&" in tm.get(_Y1_AMP)["target_text"]

    def test_ikinci_kosu_bellekten_kabul_edilen_gelir(self, y1_engine, tmp_path):
        srv, calls = y1_engine
        db = tmp_path / "tm.db"
        lt = _y1_lt(db)
        lt.translate(_Y1_BODY)
        lt._translator.memory.close()
        before = dict(calls)
        lt2 = _y1_lt(db)
        out = lt2.translate(_Y1_BODY)
        lt2._translator.memory.close()
        assert "1250" in out and "\\&" in out
        assert "survey" not in out and "publisher" not in out
        assert lt2.stats.kept_source_segments == 0
        # İki paragraf da bellekten (modele gitmedi).
        assert calls[_Y1_NUM] == before[_Y1_NUM]
        assert calls[_Y1_AMP] == before[_Y1_AMP]


class TestCekirdekOnayliBellek:
    def _bt(self, db, reply):
        import core.mt_engines as cme  # noqa: F401
        from core.translator import BookTranslator, TranslationConfig
        return BookTranslator(TranslationConfig(
            mt_engine="ollama:sahte", project_name="fx3-bellek",
            memory_db_path=str(db), use_memory=True, glossary_mode="off",
            run_qa=False, check_consistency=False))

    def test_ertelenmis_kipte_onaysiz_yazilmaz(self, y1_engine, tmp_path):
        db = tmp_path / "tm.db"
        bt = self._bt(db, None)
        bt.set_memory_deferred(True)
        bt.translate_text(_Y1_NUM)
        assert bt.memory.get(_Y1_NUM) is None          # bekliyor
        assert bt.memory_commit(_Y1_NUM) is True
        assert bt.memory.get(_Y1_NUM) is not None
        bt.memory.close()

    def test_red_bekleyeni_ve_kaydi_siler(self, y1_engine, tmp_path):
        db = tmp_path / "tm.db"
        bt = self._bt(db, None)
        bt.memory.add(_Y1_AMP, "Eski bozuk kayıt & burada.")
        bt.set_memory_deferred(True)
        bt.memory_reject(_Y1_AMP)
        assert bt.memory.get(_Y1_AMP) is None
        assert bt.stats.get("memory_rejected") == 1
        bt.memory.close()

    def test_atlama_kipinde_bellek_okunmaz(self, y1_engine, tmp_path):
        srv, calls = y1_engine
        db = tmp_path / "tm.db"
        bt = self._bt(db, None)
        bt.memory.add(_Y1_NUM, "Bellekteki çeviri 1250 değer.")
        assert bt.translate_text(_Y1_NUM) == "Bellekteki çeviri 1250 değer."
        n = calls[_Y1_NUM]
        with bt.memory_bypass():
            out = bt.translate_text(_Y1_NUM)
        assert calls[_Y1_NUM] == n + 1 and out != "Bellekteki çeviri 1250 değer."
        bt.memory.close()

    def test_varsayilan_kip_eskisi_gibi_hemen_yazar(self, y1_engine, tmp_path):
        db = tmp_path / "tm.db"
        bt = self._bt(db, None)
        bt.translate_text(_Y1_AMP)
        assert bt.memory.get(_Y1_AMP) is not None
        bt.memory.close()


# ===========================================================================
# Kalem 2 (V2 Y2) — istem işareti temizliği meşru içeriği silmez; her temizlik
# günlükte ve sayaçta
# ===========================================================================
_Y2_VERB = ("\\begin{verbatim}\n<translate>Hello world</translate>\n"
            "<<TRANSLATE>> macro\n\\end{verbatim}")
_Y2_LST = ("\\begin{lstlisting}\n<translation key=\"a\">x</translation>\n"
           "<translate>Hi</translate>\n\\end{lstlisting}")
_Y2_BODY = ("\\section{Templates}\n\nThe directive marks text for the localization "
            "tools in every template.\n\n" + _Y2_VERB + "\n\n" + _Y2_LST + "\n")


class TestIstemIsaretiKayipsiz:
    def test_kod_bloklarindaki_isaret_silinmez(self):
        lt = _mock_lt()
        out = lt.translate(_Y2_BODY)
        assert _Y2_VERB in out, out
        assert _Y2_LST in out, out
        assert lt.stats.prompt_marker_leaks == 0

    def test_kaynakta_gecen_isaret_cikti_da_kalir(self):
        calls = []
        b, s = _rule_model([(
            r"Put the (<translate>) tag around every visible string in the template\.",
            r"Şablondaki her görünür dizenin çevresine \1 etiketini koyun.")])
        lt = _mock_lt(batch_fn=b, single_fn=s, calls=calls)
        body = ("\\section{Tags}\n\nPut the <translate> tag around every visible "
                "string in the template.\n")
        out = lt.translate(body)
        assert any("<translate>" in c for c in calls), calls     # model gördü
        assert "etiketini koyun" in out and "<translate>" in out, out
        assert lt.stats.prompt_marker_leaks == 0

    def test_safe_translate_girdisinde_yalniz_protokol_isareti_ayiklanir(self):
        calls = []
        lt = _mock_lt(calls=calls)
        lt._safe_translate("<<<TRANSLATE_START>>>Use the <translate> tag here."
                           "<<<TRANSLATE_END>>>")
        sent = [c for c in calls if "tag here" in c]
        assert sent and "<translate>" in sent[0], calls
        assert "TRANSLATE_START" not in sent[0] and "TRANSLATE_END" not in sent[0]

    def test_kaynakta_olmayan_isaret_silinir_gunluk_sayac(self, caplog):
        b, s = _rule_model([(r"The value grows slowly over time\.",
                             r"Değer zamanla yavaşça artar. <<<TRANSLATE_END>>>")])
        lt = _mock_lt(batch_fn=b, single_fn=s)
        with caplog.at_level("WARNING"):
            out = lt.translate("\\section{Growth}\n\nThe value grows slowly over time.\n")
        assert "TRANSLATE" not in out
        assert lt.stats.prompt_marker_leaks >= 1
        assert any("İstem işareti" in r.getMessage() for r in caplog.records)

    def test_cekirdek_temizligi_kaynaktakini_korur_sizintiyi_sayar(self):
        bt = _bare_bt()
        kept = bt._strip_prompt_leaks("Şablona <translate> etiketini koy.",
                                      original="Put the <translate> tag.")
        assert "<translate>" in kept
        assert bt.stats.get("prompt_marker_leaks", 0) == 0
        cleaned = bt._strip_prompt_leaks("Değer artar. <<<TRANSLATE_END>>>",
                                         original="The value grows.")
        assert "TRANSLATE" not in cleaned and "Değer artar." in cleaned
        assert bt.stats["prompt_marker_leaks"] == 1

    def test_cekirdek_temizligi_latex_sayacina_aktarilir(self, fake_llm_server,
                                                          monkeypatch, tmp_path):
        import core.mt_engines as cme
        from core.mt_engines.base import EngineSpec
        from core.mt_engines.chat import ChatTemplateEngine

        srv = fake_llm_server("ollama", models=["sahte"],
                              reply=lambda t: _tr_words(t) + " <<<TRANSLATE_END>>>")
        spec = EngineSpec(name="ollama:sahte", format="chat", backend="ollama",
                          model="sahte", base_url=srv.url, source="adhoc")
        monkeypatch.setattr(cme, "create_engine",
                            lambda name, *a, **k: ChatTemplateEngine(spec))
        lt = _y1_lt(tmp_path / "tm.db")
        out = lt.translate("\\section{Growth}\n\nThe value grows slowly over "
                           "the whole period of the measurement.\n")
        lt._translator.memory.close()
        assert "TRANSLATE" not in out
        assert lt.stats.prompt_marker_leaks >= 1


class TestAutofixBaglamProtokolu:
    def test_autofix_hedefi_cikarir_baglam_modele_gitmez(self):
        """Bir model testinde autofix'in bağlamlı yeniden çevirisi 5 blokta
        "Could not extract target" ile boşa gidiyordu (işaret modele
        gitmiyor, çıktıda yoktu). Artık hedef çevrilir ve çıkarılır."""
        from latex_pipeline.autofix import AutoFixer
        calls = []
        lt = _mock_lt(calls=calls)
        fixer = AutoFixer(qc_engine=QCEngine(), translator=lt)
        out = fixer._retranslate_with_context(
            "The pump moves the water from the well.",
            "Depo her sabah doldurulur.", "Vana gece kapatılır.")
        assert out.strip() and "pump" not in out and "TRANSLATE" not in out
        assert "Depo" not in out and "Vana" not in out
        assert calls == ["The pump moves the water from the well."], calls


# ===========================================================================
# Kalem 3 (V2 Y3) — cevapsız parça: türetilmiş metin yeniden gönderilmez;
# yarı çevrilmiş paragraf işaretlenir (kayıpsız)
# ===========================================================================
from tests import test_G2b2_g as G2B  # noqa: E402  (sahte LM Studio yardımcıları)


def _y3_lt(max_chars=None):
    kw = dict(translate=True, segment_timeout=30)
    if max_chars is not None:
        kw["max_segment_chars"] = max_chars
    return LatexTranslator(pipeline_config=PipelineConfig(**kw),
                           mt_engine=G2B.ENGINE_ID, source_lang="en", target_lang="tr",
                           translation_overrides=dict(G2B._OFF))


def _y3_server(fake_llm_server, monkeypatch, fail_key):
    def reply(text, body=None):
        if fail_key in (text or ""):
            return FakeReply(content="", thinking=G2B._thought(G2B.ENDLESS_WORDS))
        return FakeReply(content=G2B._fake_turkish(text),
                         thinking=G2B._thought(G2B.SHORT_THINK_WORDS))
    srv = G2B._lms_server(fake_llm_server, reply, options=["on"])
    G2B._point_lmstudio(monkeypatch, srv)
    return srv


def _y3_requests_with(srv, key):
    return [r for r in G2B._gen_requests(srv) if key in G2B._req_text(r)]


from tests.fakes import FakeReply  # noqa: E402


class TestCevapsizTuretilmisMetin:
    def test_yer_tutuculu_cevapsiz_parca_sade_bicimle_yeniden_gitmez(
            self, fake_llm_server, monkeypatch):
        srv = _y3_server(fake_llm_server, monkeypatch, "Paragraph three")
        body = ("\\chapter{Motion}\n\n" + G2B._paragraph("one") + "\n\n"
                "Paragraph three describes the journey of the planet $x$ around "
                "the distant sun $y$ in detail.\n\n" + G2B._paragraph("four") + "\n")
        lt = _y3_lt()
        out = lt.translate(body)
        assert "Paragraph three describes the journey" in out      # kayıpsız
        tries = _y3_requests_with(srv, "Paragraph three")
        # ilk deneme + bekçinin TEK yeniden denemesi; sade biçim / cümle
        # düzeyi türetilmiş metin GÖNDERİLMEZ.
        assert len(tries) == 2, [G2B._req_text(r)[:60] for r in tries]
        kept = [k for k in lt.stats.kept_source_list if "Paragraph three" in k["kaynak"]]
        assert kept and "cevap" in kept[0]["neden"]
        assert lt.stats.recovered_by_retry == 0
        assert lt.stats.no_answer_segments == 1

    def test_uzun_paragrafta_cevapsiz_cumle_isaretlenir_tekrar_gitmez(
            self, fake_llm_server, monkeypatch):
        srv = _y3_server(fake_llm_server, monkeypatch, "silent comet")
        para = ("The first sentence describes the long journey of the outer planet. "
                "The second sentence mentions the silent comet near the distant sun. "
                "The third sentence describes the motion of the planet in detail.")
        lt = _y3_lt(max_chars=120)
        out = lt.translate("\\chapter{Motion}\n\n" + para + "\n")
        assert "The second sentence mentions the silent comet near the distant sun." in out
        assert "The first sentence" not in out and "The third sentence" not in out
        tries = _y3_requests_with(srv, "silent comet")
        assert len(tries) == 2, [G2B._req_text(r)[:60] for r in tries]
        kept = [k for k in lt.stats.kept_source_list if "silent comet" in k["kaynak"]]
        assert kept and kept[0]["kismi"] is True and "cevap" in kept[0]["neden"]
        assert lt.stats.recovered_by_retry == 0

    def test_cevapsiz_baglam_grubu_parcalara_bolunup_yeniden_gitmez(
            self, fake_llm_server, monkeypatch):
        srv = _y3_server(fake_llm_server, monkeypatch, "silent comet")
        body = ("\\chapter{Motion}\n\nBy watching the silent comet, one gets\n\n" + EQ_A
                + "\n\nwhich holds for every orbit. The planet keeps moving around the "
                "distant sun in detail.\n\n" + G2B._paragraph("four") + "\n")
        lt = _y3_lt()
        out = lt.translate(body)
        tries = _y3_requests_with(srv, "silent comet")
        assert len(tries) == 2, [G2B._req_text(r)[:70] for r in tries]
        assert "By watching the silent comet, one gets" in out       # kayıpsız
        assert out.count(EQ_A) == 1
        assert lt.stats.context_groups_reduced == 0
        kept = [k for k in lt.stats.kept_source_list if "silent comet" in k["kaynak"]]
        assert kept and "cevap" in kept[0]["neden"]

    def test_cevapsiz_baslik_isaretlenir(self, fake_llm_server, monkeypatch):
        srv = _y3_server(fake_llm_server, monkeypatch, "Silent Comet")
        lt = _y3_lt()
        out = lt.translate("\\chapter{The Silent Comet}\n\n" + G2B._paragraph("one") + "\n")
        assert "The Silent Comet" in out                              # kayıpsız
        kept = [k for k in lt.stats.kept_source_list if "Silent Comet" in k["kaynak"]]
        assert kept and "cevap" in kept[0]["neden"]


# ===========================================================================
# Kalem 4a (V2 Y6) — kesici iletisi bir kez; özet nedeni gerçek kayıttan
# ===========================================================================
class TestKesiciIletisiBirKez:
    def _down_lt(self, msg):
        from core.mt_engines.contracts import BackendUnavailable

        def boom(*a, **k):
            raise BackendUnavailable(msg)
        lt = _mock_lt(batch_fn=boom, single_fn=boom)
        lt._translator.backend_health = lambda: msg
        return lt

    def test_ileti_bir_kez_yazilir(self, caplog):
        msg = "Bu model düşünmesini bitirip çeviri üretemiyor (sentetik kesici)."
        lt = self._down_lt(msg)
        body = "\\section{Notes}\n\n" + "\n\n".join(
            f"Paragraph number {w} talks about the weather of the small town today."
            for w in ("one", "two", "three", "four", "five", "six"))
        with caplog.at_level("DEBUG"):
            out = lt.translate(body)
        # Çağrı hataları içinde neden BİR KEZ (checkpoint'in kendi tek satırı ayrı).
        loud = [r for r in caplog.records if r.levelno >= 30 and msg in r.getMessage()
                and "FINAL checkpoint" not in r.getMessage()]
        assert len(loud) == 1, [r.getMessage()[:80] for r in loud]
        assert "Paragraph number six" in out                         # kayıpsız
        assert any("denenmeden geçti" in r.getMessage() for r in caplog.records
                   if r.levelno >= 30)

    def test_ozet_nedeni_gercek_kayittan(self, caplog):
        msg = "Sunucu kapandı (sentetik)."
        lt = self._down_lt(msg)
        body = ("\\section{Notes}\n\nThe value $x$ grows slowly over the whole "
                "period of the measurement.\n")
        with caplog.at_level("WARNING"):
            lt.translate(body)
        kept = lt.stats.kept_source_list
        assert kept and all("model kullanılamıyor" in k["neden"] for k in kept), kept
        summary = [r.getMessage() for r in caplog.records
                   if "parça çevrilemedi ve özgün dilinde bırakıldı" in r.getMessage()]
        assert summary and "model kullanılamıyor" in summary[0]
        assert "yer tutucu / LaTeX iskeleti / sayı bekçisi" not in summary[0]


# ===========================================================================
# Kalem 4b (V2 Y7) — geri yazım kaynakta olmayan boşluk eklemez
# ===========================================================================
class TestGeriYazimBoslugu:
    def test_kaynakta_bitisik_isaretlerde_bosluk_eklenmez(self):
        body = ("\\section{Units}\n\nThe flux density is measured in W/$m^{2}$ for "
                "every wall, \\textit{Tn}+1 marks the outer face and for a ball "
                "\\textit{r}/3 holds.\n")
        lt = _mock_lt()
        out = lt.translate(body)
        assert "W/$m^{2}$" in out, out
        assert "\\textit{Tn}+1" in out, out
        assert "\\textit{r}/3" in out, out

    def test_harf_harfe_yapisma_hala_ayrilir(self):
        body = "\\section{Units}\n\nThe symbol $x$ marks the unknown value here.\n"
        b, s = _rule_model([(r"The symbol (<<LATEX_\d+>>) marks the unknown value here\.",
                             r"Simge\1bilinmeyen değeri gösterir.")])
        lt = _mock_lt(batch_fn=b, single_fn=s)
        out = lt.translate(body)
        assert "Simge $x$ bilinmeyen" in out, out

    def test_kaynakta_bosluk_varsa_korunur(self):
        body = "\\section{Units}\n\nThe ratio = $q$ stays constant over time.\n"
        b, s = _rule_model([(r"The ratio = (<<LATEX_\d+>>) stays constant over time\.",
                             r"Oran =\1 zamanla sabit kalır.")])
        lt = _mock_lt(batch_fn=b, single_fn=s)
        out = lt.translate(body)
        assert "Oran = $q$ zamanla" in out, out


# ===========================================================================
# Kalem 5 — MT-18 markdown "**" artığı; MT-15 (kısmen) tablo hücresi ondalığı
# ===========================================================================
class TestMarkdownArtigi:
    def test_kaynakta_olmayan_tek_yildiz_cifti_silinir(self):
        lt = _mock_lt(source_lang="de")
        out = lt._post_process_translation("Cam**", "Fensterglas")
        assert out == "Cam", out
        assert lt.stats.markdown_remnants_removed == 1

    def test_eslesen_cift_kalin_olur(self):
        lt = _mock_lt()
        out = lt._post_process_translation("**Uyarı** metni burada.",
                                           "Warning text goes here.")
        assert out == "\\textbf{Uyarı} metni burada.", out

    def test_kaynakta_gecen_yildiz_korunur(self):
        lt = _mock_lt()
        src = "In Python the ** operator raises a number to a power."
        out = lt._post_process_translation(
            "Python'da ** işleci bir sayının kuvvetini alır.", src)
        assert "**" in out
        assert lt.stats.markdown_remnants_removed == 0


class TestTabloHucresiOndaligi:
    def test_sayi_hucresi_bicimi(self):
        from latex_pipeline.translator import localize_numeric_cell
        assert localize_numeric_cell(" 1.327 ") == " 1,327 "
        assert localize_numeric_cell("696,000") == "696.000"
        assert localize_numeric_cell("1,234.5") == "1.234,5"
        assert localize_numeric_cell("−0.25") == "−0,25"
        for same in ("12", "1.2.3", "1,2", "abc", "3.0e5", ""):
            assert localize_numeric_cell(same) is None, same

    def test_ingilizce_tabloda_sayi_hucresi_yerellesir(self):
        lt = _mock_lt()
        body = ("Planet & Mass factor & Radius \\\\\n\\midrule\n"
                "Earth & 1.327 & 696,000 \\\\\n")
        out = lt._translate_tabular_body(body)
        assert "& 1,327 &" in out and "696.000" in out, out
        assert lt.stats.decimal_separators_localized == 2

    def test_bolum_numarasi_sutunu_dokunulmaz(self):
        lt = _mock_lt()
        body = ("Section & Title & Page \\\\\n\\midrule\n"
                "1.10 & Orbits of comets & 12 \\\\\n")
        out = lt._translate_tabular_body(body)
        assert "1.10 &" in out, out

    def test_ingilizce_olmayan_kaynakta_dokunulmaz(self):
        lt = _mock_lt(source_lang="de")
        body = "Stoff & Wert \\\\\n\\midrule\nGlas & 0,76 \\\\\nStahl & 1.5 \\\\\n"
        out = lt._translate_tabular_body(body)
        assert "0,76" in out and "1.5" in out, out

    def test_birim_hucresi_modele_gitmez(self):
        calls = []
        lt = _mock_lt(calls=calls)
        body = "Body & Unit \\\\\n\\midrule\nSun & km\\textsuperscript{3} $s^{-2}$ \\\\\n"
        out = lt._translate_tabular_body(body)
        assert "km\\textsuperscript{3} $s^{-2}$" in out
        assert not any("textsuperscript" in c for c in calls), calls


# ===========================================================================
# Kalem 6 (KARAR 35) — dış AI API kodu yok; yerel "OpenAI uyumlu" sunucu kalır
# ===========================================================================
class TestDisAiApiKoduYok:
    def test_yapilandirmada_ai_alanlari_yok(self):
        import dataclasses

        from core.translator import TranslationConfig
        names = {f.name for f in dataclasses.fields(TranslationConfig)}
        for gone in ("use_ai_review", "ai_provider", "ai_model", "ai_api_key"):
            assert gone not in names, gone

    def test_cevirmende_ai_yollari_yok(self):
        from core.translator import BookTranslator
        for gone in ("_ai_review", "_init_ai_client"):
            assert not hasattr(BookTranslator, gone), gone

    def test_kaynakta_dis_istemci_ve_anahtar_yok(self):
        from pathlib import Path

        import core.translator as ct
        src = Path(ct.__file__).read_text(encoding="utf-8")
        for gone in ("from openai", "from anthropic", "google.generativeai",
                     "api_key", "ai_client", "ai_reviews"):
            assert gone not in src, gone

    def test_merkezi_sabitlerde_api_kalintisi_yok(self):
        from config import defaults
        for gone in ("DEFAULT_BATCH_SIZE_API", "AI_REVIEW_MAX_TOKENS",
                     "MT_API_TIMEOUT_S"):
            assert not hasattr(defaults, gone), gone
            assert gone not in defaults.__all__, gone

    def test_yerel_openai_uyumlu_arka_uc_duruyor(self):
        from core.mt_engines.http_backends import OpenAIBackend
        assert OpenAIBackend is not None
