"""
Shared prompt leak detection patterns.

All prompt leakage regex patterns used across the pipeline are defined
HERE and only here.  Other modules import from this file:

    from latex_pipeline.patterns import PROMPT_LEAK_PATTERNS

This is the UNION of all patterns that previously lived in:
  - latex_pipeline/translator.py  (_PROMPT_LEAK_PATTERNS, class attr)
  - latex_pipeline/sanitizer.py   (_PROMPT_LEAK_PATTERNS, module-level)
  - latex_pipeline/autofix.py     (_PROMPT_LEAK_PATTERNS, module-level)

No pattern was removed during the merge.  Duplicates were deduplicated
manually; the most general form of each pattern was kept.

Categories:
  A. AI identity / system marker patterns  (from autofix)
  B. Code fence / chat template artifacts  (from autofix)
  C. HY-MT 7B Turkish instruction echoes   (all three files)
  D. English instruction echoes             (all three files)
  E. Angle-bracket instruction markers      (sanitizer + autofix)
  F. Turkish instruction sentences          (sanitizer + autofix)
  G. Session 4 Codex findings               (sanitizer + autofix)
  H. Session 5 aggressive patterns          (all three files)
  I. CJK character artifacts                (autofix -- inside pattern list)
  J. Broad catch-all line matcher           (sanitizer)
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List

# =========================================================================
# PROMPT_LEAK_PATTERNS -- canonical, single-source-of-truth list
# =========================================================================

PROMPT_LEAK_PATTERNS: List[re.Pattern] = [
    # -----------------------------------------------------------------
    # A. AI identity / system marker patterns  (originally autofix only)
    # -----------------------------------------------------------------
    # Not: `[^\n]*` ile satir sonuna kadar silme YAPILMIYOR -- leak frazi
    # bittikten sonra gelen gercek ceviri metni korunmali. Sadece leak
    # frazindan hemen sonraki ": " veya ", " gibi ayiraclari sil.
    # Cumle icindeki leak'ler icin [^,.\n]* (virgule kadar) kullaniyoruz,
    # boylece "As an AI language model, here is..." gibi yapilarda ilk
    # virgule kadar silinir, sonrasi diger pattern'lara birakilir.
    re.compile(
        r"(?i)\b(?:as an ai[^,.\n]*|"
        r"i cannot[^,.\n]*|"
        r"i can't[^,.\n]*|"
        r"i'm an ai[^,.\n]*|"
        r"i am an ai language model[^,.\n]*|"
        r"here is the translat(?:ion|ed)(?:\s+(?:text|version|of))?|"
        r"translated text|"
        r"note:\s*this translation[^.\n]*\.?|"
        r"please note that[^.\n]*\.?|"
        r"i have translat(?:ed|ion)[^.\n]*\.?|"
        r"the translat(?:ed|ion) (?:text|version) is)"
        r"[:\s,]*"
    ),

    # -----------------------------------------------------------------
    # B. Code fence / chat template artifacts  (originally autofix only)
    # -----------------------------------------------------------------
    # Markdown code fences that leak through
    re.compile(r"^```(?:latex|tex)?\s*$", re.MULTILINE),
    re.compile(r"^```\s*$", re.MULTILINE),
    # Instruction remnants with special tokens
    re.compile(r"<\|(?:im_start|im_end|system|user|assistant)\|>"),
    # ChatGPT-style artifacts
    re.compile(r"\[INST\].*?\[/INST\]", re.DOTALL),
    re.compile(r"<<SYS>>.*?<</SYS>>", re.DOTALL),

    # -----------------------------------------------------------------
    # C. HY-MT 7B Turkish instruction echoes  (translator + autofix)
    # -----------------------------------------------------------------
    # Full Turkish instruction block: "denklemleri, degisken adlarini ... kullanin"
    re.compile(
        r",?\s*denklemleri,?\s*de\u011fi\u015fken\s+adlar\u0131n\u0131\s*\([^)]*\)"
        r"[^.]*?(?:kullan\u0131n|kullan)\.?\s*"
        r"(?:terimlerini\s+kullan\u0131n\.?\s*)?"
        r"(?:Ba\u015fka\s+hi\u00e7bir\s+\u015fey\s+kullanmay\u0131n\.?\s*)?",
        re.IGNORECASE,
    ),
    # Shorter Turkish variant: "birim kisaltmalarini ... kullanin"
    re.compile(
        r",?\s*birim\s+k\u0131saltmalar\u0131n\u0131\s*\([^)]*\)"
        r"[^.]*?(?:kullan\u0131n|kullan)\.?\s*",
        re.IGNORECASE,
    ),
    # "formulleri degistirmeden kullanin" standalone
    re.compile(
        r",?\s*form\u00fclleri\s+de\u011fi\u015ftirmeden\s+kullan\u0131n\.?\s*",
        re.IGNORECASE,
    ),
    # "terimlerini kullanin" standalone
    re.compile(
        r",?\s*terimlerini\s+kullan\u0131n\.?\s*",
        re.IGNORECASE,
    ),
    # Catch-all Turkish combined fragment (translator)
    re.compile(
        r",?\s*denklemleri,?\s*de\u011fi\u015fken\s+adlar\u0131n\u0131[^.]*?"
        r"(?:Ba\u015fka\s+hi\u00e7bir\s+\u015fey\s+kullanmay\u0131n\.?\s*)",
        re.IGNORECASE | re.DOTALL,
    ),

    # -----------------------------------------------------------------
    # D. English instruction echoes  (translator + autofix)
    # -----------------------------------------------------------------
    re.compile(
        r"Keep\s+all\s+mathematical\s+notation.*?unchanged\.?\s*",
        re.IGNORECASE,
    ),
    re.compile(
        r"Use\s+standard\s+Turkish\s+scientific\s+terminology\.?\s*",
        re.IGNORECASE,
    ),
    re.compile(
        r"Only\s+output\s+the\s+translation,?\s*nothing\s+else\.?\s*",
        re.IGNORECASE,
    ),

    # -----------------------------------------------------------------
    # E. Angle-bracket instruction markers  (sanitizer + autofix)
    # -----------------------------------------------------------------
    re.compile(r"<{2,3}[^>]*\u00c7EV\u0130R[\u0130I][^>]*>{2,3}", re.IGNORECASE),
    re.compile(r"<{2,3}[^>]*BA\u015eLA[^>]*>{2,3}", re.IGNORECASE),
    re.compile(r"<{2,3}[^>]*SON[^>]*>{2,3}", re.IGNORECASE),
    re.compile(r"<{2,3}[^>]*TRANSLATE[^>]*>{2,3}", re.IGNORECASE),
    # G2a-2 (2026-10-05): karışık ayraçlı bozuk biçim ("<<><TRANSLATE_START>>";
    # bir test kitabının çevirisinde görüldü) yukarıdaki desenden
    # kaçıyordu. translator._PROMPT_MARKER_RE ile aynı (oraya da buradan
    # içe aktarılması önerilir: tek kaynak).
    re.compile(r"[<>]{1,4}[ \t]*(?:TRANSLAT\w*|\u00c7EV[\u0130I]R\w*)"
               r"(?:[ \t_\-]+\w+)?[ \t]*[<>]{1,4}", re.IGNORECASE),
    re.compile(r"<{2,3}[^>]*BEGIN[^>]*>{2,3}", re.IGNORECASE),
    re.compile(r"<{2,3}[^>]*END[^>]*>{2,3}", re.IGNORECASE),

    # -----------------------------------------------------------------
    # F. Turkish instruction sentences  (sanitizer + autofix)
    # -----------------------------------------------------------------
    re.compile(
        r"(?i)(?:a[\u015fs]a[\u011fg][\u0131i]daki|yukar[\u0131i]daki)\s+"
        r"(?:metni|paragraf[\u0131i]|b[\u00f6o]l[\u00fcu]m[\u00fcu])\s+"
        r"(?:t[\u00fcu]rk[\u00e7c]eye?\s+)?[\u00e7c]evir[^\n]*"
    ),
    re.compile(
        r"(?i)[\u00e7c]eviri(?:ye|nin|yi)?\s+(?:ba[\u015fs]la|sonu?|bitir)[^\n]*"
    ),
    # Sanitizer-only patterns (broader variants)
    # NOT: talimat fiili sart \u2014 fiilsiz hali mesru cumleleri de yutuyordu
    # ("Tum matematiksel notasyonlari Bolum 1'de ozetledik", audit #28).
    re.compile(
        r"T[\u00fcu]m\s+matematiksel\s+notasyonlar[\u0131i]n?[\u0131i]?\s+"
        r"(?:koru|aynen|oldu[\u011fg]u\s+gibi|de[\u011fg]i[\u015fs]tirme)",
        re.IGNORECASE,
    ),
    re.compile(r"Standart\s+T[\u00fcu]rk\s+bilimsel", re.IGNORECASE),
    re.compile(r"translate\s+the\s+following", re.IGNORECASE),
    re.compile(r"You\s+are\s+a\s+professional\s+translator", re.IGNORECASE),
    re.compile(r"LaTeX\s+komutlar[\u0131i]n[\u0131i]\s+koruyun", re.IGNORECASE),
    re.compile(r"[\u00e7c]eviri\s+yaparken", re.IGNORECASE),

    # -----------------------------------------------------------------
    # G. Session 4 Codex findings  (sanitizer + autofix)
    # -----------------------------------------------------------------
    # "Matematiksel gosterimleri koru" variants
    re.compile(r"(?i)matematiksel\s+g[\u00f6o]sterimleri\s+koru[^\n]*"),
    re.compile(r"(?i)matematiksel\s+ifadeleri\s+koru[^\n]*"),
    # "Yalnizca bu kismi cevir" and variants
    re.compile(r"(?i)yaln[\u0131i]zca\s+bu\s+k[\u0131i]sm[\u0131i]\s+[\u00e7c]evir[^\n]*"),
    re.compile(r"(?i)sadece\s+bu\s+k[\u0131i]sm[\u0131i]\s+[\u00e7c]evir[^\n]*"),
    # "Lutfen cevirin"
    re.compile(r"(?i)l[\u00fcu]tfen\s+[\u00e7c]evirin[^\n]*"),
    # "Asagidaki metni cevirin"
    re.compile(r"(?i)a[\u015fs]a[\u011fg][\u0131i]daki\s+metni\s+[\u00e7c]evirin[^\n]*"),
    # "Ceviri talimatlari"
    re.compile(r"(?i)[\u00e7c]eviri\s+talimatlar[\u0131i][^\n]*"),
    # "Bu bir ceviri" (standalone instruction marker -- sanitizer only)
    # Yalniz TEK BASINA satir olusturuyorsa sil; cumle icinde gecen
    # "bu bir ceviri romanidir" gibi mesru kullanim korunur (audit #28).
    re.compile(r"(?im)^\s*bu\s+bir\s+[\u00e7c]eviridir?\s*[.!]?\s*$"),
    # "Orijinal metin" \u2014 talimat fiili sart: fiilsiz hali "Orijinal metinde
    # yazar bu konuyu ele alir" gibi mesru cumleleri satir sonuna kadar
    # siliyordu (audit #28).
    re.compile(
        r"(?i)orijinal\s+metn?i?n?(?:deki|in)?\s+(?:format[\u0131i]n?[\u0131i]?\s+)?"
        r"(?:koru|aynen|oldu[\u011fg]u\s+gibi|de[\u011fg]i[\u015fs]tirme)[^\n]*"
    ),
    # "Metni Turkceye cevirin"
    re.compile(r"(?i)metni\s+t[\u00fcu]rk[\u00e7c]eye\s+[\u00e7c]evir[^\n]*"),
    # "Kaynak dili/dilini koru"
    re.compile(r"(?i)kaynak\s+dil[i\u0131]n?[i\u0131]?\s+koru[^\n]*"),
    # "Ceviriyi yap" \u2014 yalniz tek basina talimat satiri; "ceviriyi yapan
    # cevirmen ..." gibi mesru cumleler korunur (audit #28 ayni sinif).
    re.compile(r"(?im)^\s*[\u00e7c]eviriyi\s+yap[.!]?\s*$"),
    # English: "keep mathematical notations"
    re.compile(r"(?i)keep\s+(?:all\s+)?mathematical\s+notations?[^\n]*"),
    # English: "only translate this part"
    re.compile(r"(?i)only\s+translate\s+this\s+(?:part|section|segment)[^\n]*"),
    # English: "do not translate"
    re.compile(r"(?i)do\s+not\s+translate[^\n]*"),
    # English: "preserve the original formatting"
    re.compile(r"(?i)preserve\s+(?:the\s+)?original\s+formatting[^\n]*"),
    # Turkish: "formulleri degistirmeden"
    re.compile(r"(?i)form[\u00fcu]lleri\s+de[\u011fg]i[\u015fs]tirmeden[^\n]*"),
    # Turkish: "LaTeX komutlarini degistirme"
    re.compile(r"(?i)LaTeX\s+komutlar[\u0131i]n[\u0131i]\s+de[\u011fg]i[\u015fs]tirme[^\n]*"),

    # -----------------------------------------------------------------
    # H. Session 5 aggressive patterns  (all three files)
    # -----------------------------------------------------------------
    # HY-MT 7B system prompt: "Translate from X to Y"
    re.compile(r"(?i)translate\s+from\s+\w+\s+to\s+\w+[^\n]*"),
    re.compile(r"(?i)output\s+only\s+the\s+translation[^\n]*"),
    re.compile(r"(?i)preserve\s+math\s+notation[^\n]*"),
    re.compile(r"(?i)preserve\s+(?:the\s+)?(?:mathematical|math)\s+(?:notation|formulas?)[^\n]*"),
    # Turkish echoes
    re.compile(r"(?i)sadece\s+[\u00e7c]eviriyi\s+[\u00e7c][\u0131i]kt[\u0131i][^\n]*"),
    re.compile(r"(?i)sadece\s+[\u00e7c]eviri(?:yi)?\s+ver[^\n]*"),
    # Talimat fiili sart: fiilsiz "matematiksel notasyon[^\n]*" mesru
    # cumleleri ("Bu kitapta kullanilan matematiksel notasyon Bolum 1'de
    # ozetlenmistir") satir sonuna kadar siliyordu (audit #28).
    re.compile(
        r"(?i)matematik(?:sel)?\s+notasyon\w*\s+"
        r"(?:koru|aynen|oldu[ğg]u\s+gibi|de[ğg]i[şs]tirme)[^\n]*"
    ),
    re.compile(r"(?i)(?:keep|preserve|maintain)\s+(?:the\s+)?math\s+notation[^\n]*"),
    # Turkish: "Baglami koru" \u2014 yalniz talimat bicimi; "tarihsel baglami
    # korumak gerekir" gibi mesru kullanim korunur (audit #28 ayni sinif).
    re.compile(
        r"(?i)ba[\u011fg]lam[\u0131i]\s+koru(?:yun|n|mal[\u0131i]s[\u0131i]n[\u0131i]z|mal[\u0131i]s[\u0131i]n)?"
        r"\b[^\n]*"
    ),
    # Turkish: "Sadece cevrilmis metni ver"
    re.compile(r"(?i)sadece\s+[\u00e7c]evrilmi[\u015fs]\s+metni[^\n]*"),
    # Turkish: "Sadece ceviriyi dondur"  (sanitizer only)
    re.compile(r"(?i)sadece\s+[\u00e7c]eviriyi\s+d[\u00f6o]nd[\u00fcu]r[^\n]*"),
    # Turkish: "Aciklama yapma"
    re.compile(r"(?i)a[\u00e7c][\u0131i]klama\s+yapma[^\n]*"),
    # Turkish: "Yorum ekleme"  (sanitizer + autofix)
    re.compile(r"(?i)yorum\s+ekleme[^\n]*"),
    # English: "Please translate" / "I will translate"
    re.compile(r"(?i)please\s+translate[^\n]*"),
    re.compile(r"(?i)I\s+will\s+translate[^\n]*"),
    # Not: Satir sonuna kadar silme YAPMIYORUZ -- leak frazi bittikten sonra
    # gerçek ceviri metni korunmali. Sadece leak frazi + sonraki ": " siliniyor.
    re.compile(r"(?i)here\s+is\s+the\s+translat(?:ion|ed)(?:\s+(?:text|version|of))?[:\s,]*"),
    # Turkish: "Ingilizce'den Turkce'ye"
    re.compile(r"(?i)[\u0130Ii]ngilizce'?den\s+T[\u00fcu]rk[\u00e7c]e'?ye[^\n]*"),

    # -----------------------------------------------------------------
    # I. CJK character artifacts \u2014 MT-ONLY listesine TA\u015eINDI (A-4,
    #    2026-07-27): manuel \u00e7eviride \u00c7ince/Japonca/Korece al\u0131nt\u0131 me\u015fru
    #    kullan\u0131c\u0131 i\u00e7eri\u011fidir; MT hal\u00fcsinasyon temizli\u011fi a\u015fa\u011f\u0131daki
    #    PROMPT_LEAK_PATTERNS_MT_ONLY \u00fczerinden yaln\u0131z Yol B'de uygulan\u0131r.
    # -----------------------------------------------------------------

    # -----------------------------------------------------------------
    # J. Broad catch-all line matcher  (sanitizer only)
    # -----------------------------------------------------------------
    # Any line that is purely a system instruction remnant.
    # NOT (audit 2026-07-16 #28): "Orijinal metin", "Matematik.*notasyon",
    # "\u00c7eviriyi yap", "Kaynak dil", "T\u00fcm matematiksel" gibi fiilsiz genis
    # on-ekler MESRU kitap satirlarini da siliyordu; talimat fiili zorunlu
    # kilindi ya da listeden cikarildi (dar F/G/H desenleri echo'lari
    # zaten yakaliyor).
    re.compile(
        r"^\s*(?:T\u00fcm\s+matematiksel\s+notasyonlar\u0131n?\u0131?\s+koru|"
        r"translate\s+the|"
        r"You\s+are\s+a|LaTeX\s+komutlar\u0131n\u0131|"
        r"Matematiksel\s+g\u00f6sterimleri\s+koru|"
        r"Yaln\u0131zca\s+bu\s+k\u0131sm\u0131|"
        r"L\u00fctfen\s+\u00e7evirin|"
        r"A\u015fa\u011f\u0131daki\s+metni|"
        r"\u00c7eviri\s+talimatlar\u0131|"
        r"Orijinal\s+metni\s+(?:koru|de\u011fi\u015ftirme|aynen)|"
        r"Kaynak\s+dil[i\u0131]n?[i\u0131]?\s+koru|"
        r"keep\s+mathematical|only\s+translate\s+this|"
        r"do\s+not\s+translate|preserve\s+.*original|"
        r"form\u00fclleri\s+de\u011fi\u015ftirmeden|"
        r"Translate\s+from|Output\s+only|Preserve\s+math|"
        r"Sadece\s+\u00e7eviriyi|"
        r"Matematik(?:sel)?\s+notasyon\w*\s+(?:koru|aynen|de\u011fi\u015ftirme)|"
        r"A\u00e7\u0131klama\s+yapma|"
        r"Please\s+translate|I\s+will\s+translate|"
        r"Here\s+is\s+the\s+translat).*$",
        re.MULTILINE | re.IGNORECASE,
    ),
]

# ---------------------------------------------------------------------------
# Yer tutucu koruma cümlesinin yankısı (2026-09-24, Yol B)
# ---------------------------------------------------------------------------
# core/translator.py _PLACEHOLDER_CLAUSE yalnız yer tutuculu segmentin MT
# talimatına eklenir ("Keep placeholder tokens like <<LATEX_N>> exactly as
# they are."). Model bu cümleyi İngilizce ya da Türkçe çevirip çıktıya
# yankılayabilir. Türkçe biçim ekli ("yer tutucuları"), araya sözcük giren
# ("belirteçlerini tam olarak") ve "olduğu/oldukları gibi" varyantlarıyla gelir.
# Kalıp yalnız EMİR kipiyle biten cümleyi yakalar (koru/koruyun, bırak/bırakın,
# tut/tutun): "Derleyici yer tutucuyu olduğu gibi bırakır." gibi meşru cümle
# eşleşmez. Örnek token hem eski "<<LATEX_12>>" hem yeni "<<LATEX_N>>" biçimini
# tanır. Yankı cümle sonunda biter (nokta/ünlem ya da satır sonu); "bırakın
# dedi" gibi devam eden cümle eşleşmez. Silme ``sub("")`` ile yapılır ve metni
# yapıştırmaz (denetçi B, 24 Eyl 2026: eski kalıp sondaki boşluğu/satır sonunu
# yutuyordu, "verilirSonra", "verilir\section{B}" oluyordu). Üç biçim:
#   1. Satırın tamamı yankı: satır, satır sonuyla birlikte silinir.
#   2. Cümle ortasında bağlaçla ("; ayrıca", ", lütfen", " ve"): bağlaç ve
#      yankı silinir, kapanış noktası önceki cümleyi kapatmak için kalır.
#   3. Kendi cümlesi: yankı, noktası ve ardındaki boşluk (satır sonu değil)
#      silinir.
# Tek kaynak: core/translator.py (_strip_prompt_leaks) bunu içe aktarır;
# ayrıca PROMPT_LEAK_PATTERNS_MT_ONLY üzerinden sanitizer/autofix/retranslator
# ve latex_pipeline/translator.py çıktı temizliğinde (yalnız Yol B) uygulanır.
_PLACEHOLDER_CLAUSE_BODY = (
    r"(?:"
    # İngilizce yankı (talimatın kendisi)
    r"Keep\s+(?:the\s+)?placeholder\s+tokens?"
    r"(?:\s+like\s+<<\s*LATEX_\w+\s*>>)?"
    r"\s+exactly\s+as\s+(?:they\s+are|it\s+is)"
    r"|"
    # Türkçe yankı: [<<LATEX_x>> gibi] yer tutucu… [≤3 sözcük] aynen/olduğu
    # gibi/oldukları gibi/olduğu şekliyle + emir kipi
    r"(?:<<\s*LATEX_\w+\s*>>\s+gibi\s+)?"
    r"yer\s+tutucu\w*"
    r"(?:\s+[^\s.!?]+){0,3}?"  # cümle sınırını aşmaz
    r"\s+(?:aynen|(?:olduğu|oldukları)\s+(?:gibi|şekliyle))"
    r"\s+(?:koru(?:yun(?:uz)?)?|bırak(?:ın(?:ız)?)?|tut(?:un(?:uz)?)?"
    r"|muhafaza\s+ed(?:in(?:iz)?)?)"
    r"(?!\w)"
    r")"
)
PLACEHOLDER_CLAUSE_LEAK_RE: re.Pattern = re.compile(
    # 1. satırın tamamı
    r"^[ \t]*" + _PLACEHOLDER_CLAUSE_BODY + r"[ \t]*[.!]?[ \t]*(?:\n|\Z)"
    # 2. bağlaçla cümle ortasında (nokta kalır)
    r"|[ \t]*(?:[,;][ \t]*(?:(?:ayrıca|lütfen|ve)[ \t]+)?|[ \t]+(?:ayrıca|ve)[ \t]+)"
    + _PLACEHOLDER_CLAUSE_BODY + r"(?=[ \t]*(?:[.!]|$))"
    # 3. kendi cümlesi
    r"|" + _PLACEHOLDER_CLAUSE_BODY + r"[ \t]*(?:[.!][ \t]*|$)",
    re.IGNORECASE | re.MULTILINE,
)

# ---------------------------------------------------------------------------
# MT-ONLY leak kalıpları (2026-07-27, Yol A serbestlik denetimi A-1)
# ---------------------------------------------------------------------------
# "Çeviri notu" / "Çevirmen notu": Yol B'de HY-MT halüsinasyonudur, silinmesi
# doğrudur. Yol A'da (manuel/ajan çeviri) ise ÇEVİRMENİN bilinçli eklediği
# meşru bir nottur — satır sonuna kadar silinmesi kullanıcı içeriği kaybıydı
# (footnote içindeyken kapanış brace'i de kopuyordu). Bu kalıplar yalnız MT
# çıktısı işleyen yollarda uygulanır: core/latex çeviricinin çıktı temizliği,
# retranslator ve manuel-olmayan (Yol B) sanitize/autofix.
PROMPT_LEAK_PATTERNS_MT_ONLY: List[re.Pattern] = [
    re.compile(r"(?i)[ÇçCc]eviri\s+notu[^\n]*"),
    re.compile(r"(?i)[ÇçCc]evirmen\s+notu[^\n]*"),
    # CJK karakter artefaktları (HY-MT halüsinasyonu) — eski bölüm I.
    # Manuel çeviride CJK alıntı kullanıcı içeriğidir, silinmez (A-4).
    re.compile(
        r"[一-鿿　-〿぀-ゟ"
        r"゠-ヿ가-힯＀-￯]+"
    ),
    # Yer tutucu koruma cümlesinin yankısı (2026-09-24): yalnız MT
    # talimatından gelir; manuel çeviride (Yol A) uygulanmaz.
    PLACEHOLDER_CLAUSE_LEAK_RE,
]

# Tam liste — MT çıktısını temizleyen çağıranlar bunu kullanır.
ALL_PROMPT_LEAK_PATTERNS: List[re.Pattern] = (
    PROMPT_LEAK_PATTERNS + PROMPT_LEAK_PATTERNS_MT_ONLY
)


# ---------------------------------------------------------------------------
# Bölüm ETİKETİ kelimeleri (2026-09-24)
# ---------------------------------------------------------------------------
# "Chapter 2", "Глава 1", "Kapitel 3": Marker bölüm numarasını ve adını ayrı
# başlık satırları olarak verebiliyor; converter ve sanitizer bu etiketi tanıyıp
# ardındaki başlıkla tek \chapter yapar. Kapsam: TeX yolunun kaynak dilleri
# (İngilizce, Almanca, Rusça, Fransızca, İspanyolca, İtalyanca) + Türkçe.
# Büyük/küçük harf duyarsız ("ГЛАВА 1", "CHAPTER 2").
CHAPTER_LABEL_WORDS = r"(?i:Chapter|B[öo]l[üu]m|Kapitel|Chapitre|Cap[ií]tulo|Capitolo|Глава)"


# ---------------------------------------------------------------------------
# Çok dilli bölüm YAPISI sözcükleri (2026-09-25, Y1)
# ---------------------------------------------------------------------------
# Kapsam: TeX yolunun kaynak dilleri (İngilizce, Almanca, Rusça, Fransızca,
# İspanyolca, İtalyanca; core/lang_detect.TEX_SOURCE_LANGS) + Türkçe etiket
# sözcükleri. Tüm listeler BURADA tek kaynaktır; converter bunları içe
# aktarır. CHAPTER_LABEL_WORDS (yukarıda) sanitizer/QC tarafından da
# kullanıldığı için DEĞİŞTİRİLMEDİ; aşağıdakiler yalnız converter'ın
# yapı çözümlemesinde kullanılır.


def fold_accents(text: str) -> str:
    """Aksanları atar, harf sayısını KORUR (karakter karakter).

    "CAPÍTULO" -> "CAPITULO", "четвёртая" -> "четвертая". Her karakter tek
    başına NFD'ye açılıp birleşik işaretleri atılır; açılım birden fazla
    taban harf verirse karakter olduğu gibi kalır. Böylece katlanmış metin
    üzerindeki eşleşme konumları özgün metinde de geçerlidir.
    """
    out = []
    for ch in text:
        base = "".join(
            c for c in unicodedata.normalize("NFD", ch)
            if not unicodedata.combining(c)
        )
        out.append(base if len(base) == 1 else ch)
    return "".join(out)


# Kısım (bölümlerin üstü) etiketleri: "Part II", "Teil 1", "Часть первая",
# "Deuxième partie", "Parte prima", "Libro primero", "Book I", "Kısım 3".
# Politika (2026-05-17 EPUB oturumu, 2026-09-24 denetim M1#2): kısım
# etiketi bölüm başlığıyla BİRLEŞTİRİLMEZ ve \chapter sayısını şişirmez;
# converter geçerli bir kısım dizisini \part yapar.
PART_LABEL_WORDS = (
    r"(?i:Part|Book|Teil|Buch|Часть|Книга|Partie|Livre|Parte|Libro|K[ıi]s[ıi]m)"
)

# Katı Roma rakamı (I..MMMCMXCIX). Büyük/küçük harf bayrağı çağırana aittir;
# "CIVIL", "LIVE" gibi sözcükleri reddeder (geçersiz dizilim ya da ardından
# harf gelir). Değer üst sınırı converter'da denetlenir ("MIX" = 1009).
ROMAN_NUMERAL_PATTERN = (
    r"(?=[MDCLXVImdclxvi])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
)

_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(numeral: str) -> int:
    """Geçerli Roma rakamının değeri ("XIV" -> 14); geçersizse 0."""
    s = numeral.strip().upper()
    if not s or not re.fullmatch(ROMAN_NUMERAL_PATTERN, s):
        return 0
    total = 0
    for k, ch in enumerate(s):
        v = _ROMAN_VALUES[ch]
        if k + 1 < len(s) and _ROMAN_VALUES[s[k + 1]] > v:
            total -= v
        else:
            total += v
    return total


# Sıra (ve bazı dillerde sayma) sözcükleri 1-20: "Chapter One", "Erstes
# Kapitel", "Глава первая", "Chapitre premier", "Capítulo primero",
# "Capitolo primo". Anahtarlar doğru yazımla (aksanlı) verilir; eşleme
# aksansız biçimi de kabul eder (fold_accents). İspanyolca/İtalyanca -o
# ile biten sözcüklerin dişil (-a) biçimi otomatik eklenir; Almanca sıra
# sözcükleri gövde + çekim ekiyle üretilir.
_NUMBER_WORDS_BY_VALUE: Dict[int, tuple] = {
    # İngilizce: sıra + sayma sözcükleri
    # Almanca: sıra gövdesi ("erst" -> erste/erstes/...) + sayma sözcüğü
    # Rusça: dişil sıra sözcüğü (глава/часть/книга dişildir)
    # Fransızca, İspanyolca, İtalyanca: sıra + sayma sözcükleri
    1: ("first", "one", "erst", "eins", "первая",
        "premier", "première", "un", "une",
        "primero", "primer", "uno", "primo"),
    2: ("second", "two", "zweit", "zwei", "вторая",
        "deuxième", "second", "seconde", "deux",
        "segundo", "dos", "secondo", "due"),
    3: ("third", "three", "dritt", "drei", "третья",
        "troisième", "trois", "tercero", "tercer", "tres", "terzo", "tre"),
    4: ("fourth", "four", "viert", "vier", "четвёртая",
        "quatrième", "quatre", "cuarto", "cuatro", "quarto", "quattro"),
    5: ("fifth", "five", "fünft", "fünf", "пятая",
        "cinquième", "cinq", "quinto", "cinco", "cinque"),
    6: ("sixth", "six", "sechst", "sechs", "шестая",
        "sixième", "sexto", "seis", "sesto", "sei"),
    7: ("seventh", "seven", "siebt", "siebent", "sieben", "седьмая",
        "septième", "sept", "séptimo", "sétimo", "siete", "settimo", "sette"),
    8: ("eighth", "eight", "acht", "восьмая",
        "huitième", "huit", "octavo", "ocho", "ottavo", "otto"),
    9: ("ninth", "nine", "neunt", "neun", "девятая",
        "neuvième", "neuf", "noveno", "nono", "nueve", "nove"),
    10: ("tenth", "ten", "zehnt", "zehn", "десятая",
         "dixième", "dix", "décimo", "diez", "decimo", "dieci"),
    11: ("eleventh", "eleven", "elft", "elf", "одиннадцатая",
         "onzième", "onze", "undécimo", "decimoprimero", "once",
         "undicesimo", "undici"),
    12: ("twelfth", "twelve", "zwölft", "zwölf", "двенадцатая",
         "douzième", "douze", "duodécimo", "decimosegundo", "doce",
         "dodicesimo", "dodici"),
    13: ("thirteenth", "thirteen", "dreizehnt", "dreizehn", "тринадцатая",
         "treizième", "treize", "decimotercero", "decimotercio", "trece",
         "tredicesimo", "tredici"),
    14: ("fourteenth", "fourteen", "vierzehnt", "vierzehn", "четырнадцатая",
         "quatorzième", "quatorze", "decimocuarto", "catorce",
         "quattordicesimo", "quattordici"),
    15: ("fifteenth", "fifteen", "fünfzehnt", "fünfzehn", "пятнадцатая",
         "quinzième", "quinze", "decimoquinto", "quince",
         "quindicesimo", "quindici"),
    16: ("sixteenth", "sixteen", "sechzehnt", "sechzehn", "шестнадцатая",
         "seizième", "seize", "decimosexto", "dieciséis",
         "sedicesimo", "sedici"),
    17: ("seventeenth", "seventeen", "siebzehnt", "siebenzehnt", "siebzehn",
         "семнадцатая", "dix-septième", "dix-sept", "decimoséptimo",
         "diecisiete", "diciassettesimo", "diciassette"),
    18: ("eighteenth", "eighteen", "achtzehnt", "achtzehn", "восемнадцатая",
         "dix-huitième", "dix-huit", "decimoctavo", "dieciocho",
         "diciottesimo", "diciotto"),
    19: ("nineteenth", "nineteen", "neunzehnt", "neunzehn", "девятнадцатая",
         "dix-neuvième", "dix-neuf", "decimonoveno", "decimonono",
         "diecinueve", "diciannovesimo", "diciannove"),
    20: ("twentieth", "twenty", "zwanzigst", "zwanzig", "двадцатая",
         "vingtième", "vingt", "vigésimo", "veinte", "ventesimo", "venti"),
}

# Almanca sıra gövdeleri (çekim ekleri e/es/er/en/em ile açılır). Sayma
# sözcüğüyle çakışan "acht" hem 8 (sayma) hem "achte" (sıra) olarak kalır.
_GERMAN_ORDINAL_STEMS = frozenset({
    "erst", "zweit", "dritt", "viert", "fünft", "sechst", "siebt", "siebent",
    "acht", "neunt", "zehnt", "elft", "zwölft", "dreizehnt", "vierzehnt",
    "fünfzehnt", "sechzehnt", "siebzehnt", "siebenzehnt", "achtzehnt",
    "neunzehnt", "zwanzigst",
})
# Sayma sözcüğü olarak da geçerli Almanca gövdeler (eksiz kullanılır).
_GERMAN_BARE_OK = frozenset({"acht"})
# Dişil biçimi (-a) üretilecek İspanyolca/İtalyanca sıra sözcükleri
# (sayma sözcükleri "uno/due/..." değil; "primer/tercer" kısaltmaları değil).
_ROMANCE_ORDINALS_O = frozenset({
    "primero", "segundo", "tercero", "cuarto", "quinto", "sexto", "séptimo",
    "sétimo", "octavo", "noveno", "nono", "décimo", "undécimo",
    "decimoprimero", "duodécimo", "decimosegundo", "decimotercero",
    "decimotercio", "decimocuarto", "decimoquinto", "decimosexto",
    "decimoséptimo", "decimoctavo", "decimonoveno", "decimonono", "vigésimo",
    "primo", "secondo", "terzo", "quarto", "sesto", "settimo", "ottavo",
    "decimo", "undicesimo", "dodicesimo", "tredicesimo", "quattordicesimo",
    "quindicesimo", "sedicesimo", "diciassettesimo", "diciottesimo",
    "diciannovesimo", "ventesimo",
})
# İngilizce sayı sözcükleri: ters sıralı etikette ("First Book of Moses")
# kabul edilmez; converter bunu bilmek için bu kümeye bakar.
ENGLISH_NUMBER_WORDS = frozenset(
    w for v in _NUMBER_WORDS_BY_VALUE.values() for w in v[:2]
) | {"seven", "seventh"}


def _build_number_words() -> Dict[str, int]:
    words: Dict[str, int] = {}
    for value, forms in _NUMBER_WORDS_BY_VALUE.items():
        for form in forms:
            if form in _GERMAN_ORDINAL_STEMS:
                for suffix in ("e", "es", "er", "en", "em"):
                    words[form + suffix] = value
                if form not in _GERMAN_BARE_OK:
                    continue
            words[form] = value
            if form in _ROMANCE_ORDINALS_O:
                words[form[:-1] + "a"] = value
    return words


# Sözcük -> değer; anahtarlar küçük harf + aksanlı özgün yazım.
NUMBER_WORD_VALUES: Dict[str, int] = _build_number_words()
# Aksansız arama tablosu (fold_accents(sözcük) -> değer).
_NUMBER_WORD_VALUES_FOLDED: Dict[str, int] = {
    fold_accents(k): v for k, v in NUMBER_WORD_VALUES.items()
}


def number_word_value(word: str) -> int:
    """"Primero" / "PREMIÈRE" / "четвертая" -> değer; tanınmazsa 0."""
    return _NUMBER_WORD_VALUES_FOLDED.get(fold_accents(word.strip().lower()), 0)


def _accent_tolerant(word: str) -> str:
    """Regex parçası: aksanlı harf hem aksanlı hem aksansız eşleşir."""
    parts = []
    for ch in word:
        base = fold_accents(ch)
        if base != ch:
            parts.append("[" + re.escape(base) + re.escape(ch) + "]")
        else:
            parts.append(re.escape(ch))
    return "".join(parts)


# Sayı sözcükleri alternasyonu (uzundan kısaya: "dix-septième" "dix"ten önce).
# Büyük/küçük harf duyarsız; ardından harf ya da tire gelmemeli.
NUMBER_WORDS_PATTERN = (
    r"(?i:"
    + "|".join(_accent_tolerant(w) for w in sorted(
        NUMBER_WORD_VALUES, key=lambda w: (-len(w), w)))
    + r")(?![\w\-])"
)

# Ek (appendix) etiketleri: "Appendix A", "Anhang 2", "Приложение 1",
# "Annexe B", "Apéndice I", "Anexo A", "Appendice 3". Bölüm çapası olan
# belgede etiketsiz başlıklar \section'a iner; ekler bu kuraldan muaftır.
# 2026-10-05 (G1a-1 / mt okuma MT-23): converter kitap sonu eklerini
# \appendix ile "Ek A/B" yapar, öteki ek başlıklarını numarasız basar. Liste
# altı kaynak dilin tekil ve çoğul (ayraç başlığı: "Appendices", "Anhänge",
# "Annexes", "Apéndices", "Anexos", "Appendici", "Приложения") biçimlerini
# kapsar; İspanyolca "Anexo" eklendi.
APPENDIX_LABEL_WORDS = (
    r"(?i:Appendix|Appendices|Anhang|Anh[äa]nge|Приложени[ея]|Annexes?"
    r"|Appendice|Appendici|Ap[ée]ndices?|Anexos?)"
)

# İçindekiler başlıkları: bu başlığın altındaki (içindekiler bölgesindeki)
# başlıklar bölüm çapası sayılmaz ("### Part I Received Wisdom" TOC girdisi).
CONTENTS_HEADING_NAMES = frozenset({
    "contents", "table of contents", "inhalt", "inhaltsverzeichnis",
    "содержание", "оглавление", "table des matieres", "sommaire",
    "indice", "sumario", "indice general", "sommario",
})

# Ön madde başlıkları (içindekiler dahil), küçük harf + aksansız
# (fold_accents). Yalnız converter'ın yapı planında kullanılır: bölüm
# dizisinin "yapı dışı" ebeveyni sayılırlar. converter._FRONT_MATTER_NAMES
# (çapasız belgede eski ön madde -> ana madde geçişi) İngilizce kalır;
# "introduction" orada geçiş tetikler, bu küme onu değiştirmez.
FRONT_MATTER_HEADING_NAMES = CONTENTS_HEADING_NAMES | frozenset({
    "preface", "foreword", "acknowledgements", "acknowledgments",
    "dedication", "about the authors", "about the author", "about this book",
    "prologue", "introduction",
    "vorwort", "geleitwort", "danksagung", "widmung", "einleitung", "prolog",
    "предисловие", "введение", "от автора", "посвящение", "пролог",
    "preface de l'auteur", "avant-propos", "remerciements", "dedicace",
    "prefacio", "prologo", "agradecimientos", "dedicatoria", "introduccion",
    "prefazione", "premessa", "ringraziamenti", "dedica", "introduzione",
})

# Arka madde başlıkları (küçük harf + aksansız): bölüm çapası olan belgede
# de \chapter olarak kalabilir (etiketsiz olmalarına rağmen \section'a
# inmezler). "References/Bibliography/Appendix" converter'ın eski
# _NON_CHAPTER kuralına tabidir (orada \section'a iner).
BACK_MATTER_HEADING_NAMES = frozenset({
    "index", "glossary", "epilogue", "afterword", "postscript", "notes",
    "register", "stichwortverzeichnis", "sachregister", "glossar", "epilog",
    "nachwort", "anmerkungen",
    "указатель", "предметный указатель", "глоссарий", "эпилог",
    "послесловие", "примечания",
    "glossaire", "postface",
    "glosario", "epilogo", "notas",
    "glossario", "postfazione", "note",
})

# Kaynakça başlıkları (2026-09-25, R18), küçük harf + aksansız (converter
# _norm_name biçimi). Kaynakça numaralı bölüm değildir: converter bu adlı
# başlığı numarasız (yıldızlı) basar; eskiden sentetik belgelerin hepsinde
# "2.4 Kaynakça" çıkıyor, sonraki numaralara da yer açıyordu. Düzeyine
# dokunulmaz (bölüm sonu kaynakçası \section*, kitap sonu \chapter* kalır).
BIBLIOGRAPHY_HEADING_NAMES = frozenset({
    "references", "bibliography", "works cited", "literature cited",
    "reference list", "list of references",
    "literatur", "literaturverzeichnis", "literaturhinweise", "quellen",
    "quellenverzeichnis", "quellen und literatur", "bibliographie",
    "bibliografie",
    "литература", "список литературы", "библиография",
    "библиографический список", "список использованной литературы",
    "использованная литература",
    "references bibliographiques",
    "bibliografia", "referencias", "referencias bibliograficas",
    "riferimenti bibliografici",
    "kaynakca", "kaynaklar",
})

# Kutu (yan metin) ETİKETİ sözcükleri (2026-09-25, R8): "Exkurs 1.1",
# "Encadré 1.1 — Le décrochage", "Recuadro 1.1. …", "Riquadro 1.1 – …",
# "Врезка 1.1", "Box 2.3: …". Converter etiket + numaralı başlığı numarasız
# (\subsubsection*) basar: kutu bölüm/alt bölüm değildir, numaralamayı
# kaydırmamalıdır (sent_ru'da kutu \section olup 1.3'ü 1.4 yapıyordu).
# İspanyolca "Cuadro" bilerek YOK: çoğunlukla tablo demektir.
BOX_LABEL_WORDS = (
    r"(?i:Box|Sidebar|Exkurs|Kasten|Infobox|Encadr[ée]|Recuadro|Riquadro"
    r"|Врезка)"
)

# EPUB bölüm alt adı işareti (2026-09-25 R8, E1 ↔ C1 sözleşmesi; tek kaynak
# 2026-09-28): epub_parser bölüm başlığının alt adını (p.subh2) başlığın
# hemen altına bu işaretle başlayan tek satır olarak yazar; converter satırı
# başlığa katar, başlığa bağlanamazsa işaretsiz düz paragraf basar. İşaret
# hiçbir çıktıya (TeX, EPUB) metin olarak geçmemelidir.
ALTAD_MARKER = "<!-- kc:altad -->"


# ---------------------------------------------------------------------------
# Kaynak metin ATIF sözcükleri (2026-09-25, dalga Q)
# ---------------------------------------------------------------------------
# "Figure 1.2", "Рис. 1.2", "Abb. 3", "Tabla 2.1", "гл. 4" gibi atıfların
# etiket sözcükleri: kategori -> dil -> regex alternation (yakalama grubu
# YOK; büyük/küçük harf bayrağı çağırana aittir, QC IGNORECASE kullanır).
# Kapsam: TeX yolunun kaynak dilleri (core/lang_detect.TEX_SOURCE_LANGS:
# en, de, ru, fr, es, it). QC'nin kaynak taraf atıf numarası çıkarımı
# (number_hallucination, "Uydurulmus referans") bunu kullanır.
#
# "en" girdileri QC'nin 2026-09-24 İngilizce listesiyle AYNIDIR ve aynı
# sıradadır: Türkçe taraf deseni (QCEngine._XREF_NUM_RE) yalnız "en"
# girdilerinden kurulur, o desen bu tabloyla birebir değişmeden kalır.
# Diğer diller yalnız KAYNAK tarafa eklenir.
#
# Kısaltmalar noktalı yazılır ("Cap.", "Kap."): noktasız "cap 3" İngilizce
# metinde sıradan sözcüktür. İstisna "Рис" (OCR noktayı sık düşürür:
# "Рис 1.8").
XREF_LABEL_WORDS: Dict[str, Dict[str, str]] = {
    "figure": {
        "en": r"Figures?|Figs?\.?",
        "de": r"Abbildung(?:en)?|Abb\.",
        "ru": r"Рисун(?:ок|ка|ке|ку|ком|ки|ков|кам|ках|ками)|Рис\.?",
        "fr": r"Figures?|Fig\.",
        "es": r"Figuras?|Fig\.",
        "it": r"Figur[ae]|Fig\.",
    },
    "table": {
        "en": r"Tables?|Tab\.?",
        "de": r"Tabellen?|Tab\.",
        "ru": r"Таблиц(?:а|ы|е|у|ей|ам|ах|ами)?|Табл\.",
        "fr": r"Tableau(?:x)?|Tabl?\.",
        "es": r"Tablas?|Tab\.",
        "it": r"Tabell[ae]|Tab\.",
    },
    "definition": {"en": r"Definitions?|Def\.?"},
    "theorem": {"en": r"Theorems?|Thm\.?"},
    "lemma": {"en": r"Lemmas?"},
    "proposition": {"en": r"Propositions?|Prop\.?"},
    "corollary": {"en": r"Corollar(?:y|ies)"},
    "example": {"en": r"Examples?|Ex\.?"},
    "exercise": {"en": r"Exercises?"},
    "algorithm": {"en": r"Algorithms?"},
    "section": {
        "en": r"Sections?|Sec\.?",
        "de": r"Abschnitt(?:e|s|en)?|Abschn\.",
        "ru": r"Раздел(?:а|е|у|ом|ы|ов|ам|ах|ами)?|Разд\.",
        "fr": r"Sections?|Sect\.",
        "es": r"Secci[óo]n(?:es)?|Secc?\.",
        "it": r"Sezion[ei]|Sez\.",
    },
    "chapter": {
        "en": r"Chapters?|Ch\.?",
        "de": r"Kapiteln?|Kap\.",
        "ru": r"Глав(?:а|ы|е|у|ой|ам|ах|ами)|Гл\.",
        "fr": r"Chapitres?|Chap\.",
        "es": r"Cap[íi]tulos?|Cap\.",
        "it": r"Capitol[oi]|Cap\.",
    },
    "equation": {
        "en": r"Equations?|Eqs?\.?",
        "de": r"Gleichung(?:en)?|Gl\.",
        "ru": (r"Уравнени(?:е|я|ю|ем|и|й|ям|ях|ями)|Ур\."
               r"|Формул(?:а|ы|е|у|ой|ам|ах|ами)?"),
        "fr": r"[ÉE]quations?|[ÉE]q\.",
        "es": r"Ecuaci[óo]n(?:es)?|Ec\.",
        "it": r"Equazion[ei]|Eq\.",
    },
    "remark": {"en": r"Remarks?"},
    "proof": {"en": r"Proofs?"},
    "property": {"en": r"Propert(?:y|ies)"},
    "appendix": {"en": r"Appendix|Appendices"},
}

# Uydurma referans denetiminin ("Uydurulmus referans") baktığı kategoriler:
# Türkçe tarafta Şekil / Tablo / Denklem karşılığı olanlar.
XREF_FABRICATION_CATEGORIES = ("figure", "table", "equation")

# Şekil altyazısı paragrafının üst sınırı (karakter; 2026-09-28, R13, kulvar
# C2). Dönüştürücü görselin hemen önündeki/ardındaki "Abb. 1.2 …", "Рис. 1.3.
# …", "Figura 1.1: …" paragrafını şeklin \caption*'ı yapar; daha uzun paragraf
# altyazı değil, şekle atıfla başlayan gövde metnidir. Derlemdeki en uzun
# gerçek altyazı ~600 karakter (bir test kitabında). Sanitizer'ın
# derleme öncesi yapıştırma sınırı (_GLUE_MAX_CAPTION_CHARS) aynı ölçüttür.
FLOAT_CAPTION_MAX_CHARS = 1000


def _split_top_level_alternatives(alternation: str) -> List[str]:
    """``a|b(?:c|d)|e`` -> ``["a", "b(?:c|d)", "e"]`` (yalnız en dış ``|``)."""
    parts: List[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(alternation):
        ch = alternation[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            parts.append(alternation[start:i])
            start = i + 1
        i += 1
    parts.append(alternation[start:])
    return parts


def xref_label_alternation(categories=None, langs=None) -> str:
    """``XREF_LABEL_WORDS``'ten regex alternation kurar (grup yok).

    *categories*: kategori adları (varsayılan: tablodaki sırayla hepsi).
    *langs*: dil kodları (varsayılan: tablodaki tüm diller). Sıra korunur:
    önce kategori, kategori içinde dil sırası; birebir aynı alternatif bir
    kez yazılır ("Fig." en/fr/es/it ortak).
    """
    cats = list(categories) if categories is not None else list(XREF_LABEL_WORDS)
    seen = set()
    out: List[str] = []
    for cat in cats:
        per_lang = XREF_LABEL_WORDS[cat]
        codes = list(langs) if langs is not None else list(per_lang)
        for code in codes:
            alt = per_lang.get(code)
            if not alt:
                continue
            for part in _split_top_level_alternatives(alt):
                if part not in seen:
                    seen.add(part)
                    out.append(part)
    return "|".join(out)


# ---------------------------------------------------------------------------
# Çevirmen notu dipnotu (2026-09-25, dalga Q)
# ---------------------------------------------------------------------------
# Yol A çevirmeninin bilinçli eklediği açıklama dipnotu; biçim
# CEVIRI_TALIMATI.md'de: \footnote{Çevirmen notu: ...}. Kaynakta karşılığı
# olmayan meşru ektir: QC'nin uzunluk oranı / halüsinasyon / bilgi kaybı
# hesapları (yalnız manuel modda) bu dipnotları sayMAZ. Desen dipnotun
# AÇILIŞINI tanır (\footnote[...]{ + isteğe bağlı \textit{/\emph{/\textbf{
# + "Çevirmen notu" / "Çeviri notu" / "Çevirenin notu" / "Çevirmenin
# notu"); kapanış süslü parantezi çağıran dengeli sayarak bulur. Başka
# dipnotlar bu desene uymaz, dokunulmaz.
TRANSLATOR_NOTE_FOOTNOTE_OPEN_RE: re.Pattern = re.compile(
    r"\\footnote\s*(?:\[[^\]]*\]\s*)?\{\s*"
    r"(?:\\(?:textit|emph|textbf|textsl)\s*\{\s*)?"
    r"(?:Çevirmen|Çeviri|Çevirenin|Çevirmenin)\s+notu\b",
    re.IGNORECASE,
)
