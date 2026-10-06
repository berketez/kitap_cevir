# Kitapçevir — Claude Code için giriş

**Kitap çevirmek istendiğinde (kullanıcı "bu kitabı çevir" / "şunu Türkçeye çevir"
derse) → [`CEVIRI_TALIMATI.md`](CEVIRI_TALIMATI.md)'yi oku ve adım adım izle.**
Bu, yerel çeviri modeli GEREKTİRMEYEN otonom akıştır (kodda Yol A; arayüzde
"PDF'den TeX çıkar"). Talimat tek başına yeterli; özel ajan tanımı gerektirmez.

Durum: bakım modu — yalnız hata düzeltmeleri.

## İki çeviri yolu

Kodda "Yol A / Yol B" geçer. **Kullanıcıya dönük her metinde (arayüz, README,
docs/) yalnızca işlevsel adlar kullanılır:** "PDF'den TeX çıkar" ve "Yerel model
ile çevir". Ekrandaki adların tek kaynağı: `ui/branding.py`.

| | Yol A — **PDF'den TeX çıkar** (önerilen) | Yol B — **Yerel model ile çevir** (Beta) |
|---|---|---|
| Çevirmen | Kullanıcı ya da yapay zekâ asistanı (Claude Code vb.) | Yerel model: önerilen HY-MT 1.5 7B (MLX, varsayılan); ayrıca bu bilgisayardaki Ollama / LM Studio / OpenAI uyumlu yerel sunucu / GGUF (llama-cpp-python) modelleri (Beta, daha az sınandı) |
| Kaynak dil | İngilizce, Almanca, Rusça, Fransızca, İspanyolca, İtalyanca | Aynı altı dil (hedef yalnız Türkçe) |
| Çeviri modeli | Gerekmez | Gerekir: varsayılan `models/hymt-7b-mlx` (~15 GB) ya da yerel sunucu modeli; liste `python -m core.mt_engines.discovery` |
| Giriş | `python 1_cikar.py <kitap>` → çevir → `python 2_derle.py <EXPORT_TR.tex>` | `python translate_book.py <kitap> [--mt-engine KİMLİK] [--kaynak-dil KOD]` |
| Kalite | En iyi sonuç (çeviriyi yapana bağlı) | Ön okuma / taslak |

- Yol B çıkış kodları: 3 = son QC puanı eşiğin altında, 4 = kaynak dil
  desteklenmiyor, 5 = model kullanılamıyor (sunucu kapalı, model yok, çeviri
  ortasında koptu, düşünen model cevap üretemiyor); toplu kipte 5 gelince kalan
  kitaplar atlanır. Yerel model kaynakları: `core/mt_engines/` (keşif yalnız
  `models/` klasörü + bu bilgisayardaki sunucular; uzak sunucuya bağlanılmaz).
- Düşünen modeller: düşünme resmî yolla kapatılabiliyorsa kapatılır; kapatılamıyorsa
  model ayrı bir düşünme payıyla düşünerek çevirir (`core/mt_engines/thinking.py`).
- Kitap sözlüğü: her kitap kendi terim sözlüğünü kaynak dilde çıkarır
  (`latex_pipeline/kitap_sozlugu.py`; kitap klasöründe `glossary_shared.tsv`,
  iki yolda aynı biçim).
- Yol A kapıları: **`1_cikar.py`** (`output/<ad>/EXPORT.tex` çıkarır) ve
  **`2_derle.py`** (`EXPORT_TR.tex`'i al, PDF derle).
- Platform: yalnızca **macOS + Apple Silicon (M serisi)**; arayüz başka platformda
  açılmaz (`ui/runner.py: platform_problem`). Yerel model güçlü donanım ister;
  uygulama belleği ölçer, yetmezse "Yerel model ile çevir" kartını kapatır.
- Grafik arayüz: `./venv/bin/streamlit run app.py` → http://127.0.0.1:8501
  (`.streamlit/config.toml`: yalnız yerel adres, yükleme sınırı 500 MB). Solda
  kitap adı + sistem durumu; ortada kitap yükleme ve iki kart: "PDF'den TeX
  çıkar" ("1 · TeX'i çıkar" → `EXPORT.tex`; çeviri aynı klasöre
  `EXPORT_TR.tex` → "2 · Çevrilmiş TeX'i derle" → PDF; "Klasörü aç") ve
  "Yerel model ile çevir" ("Model" seçici + "Modelleri tara" → "Çevir").
  Sağda "Araçlar": Terimler (seçili kitabın sözlüğü), Bellek, Karşılaştırma.
  İşler ayrı süreçte CLI kapılarından koşar (`ui/runner.py`); ilerleme yalnız
  gerçek sayaçlardan gelir, iş sürerken "İptal", sonuçta "PDF'yi aç" / "İndir".
- Kurulum + ayrıntı: [`docs/KURULUM_VE_KULLANIM.md`](docs/KURULUM_VE_KULLANIM.md),
  PDF kılavuz: `docs/Kullanma_Kilavuzu.pdf` (kaynak: `docs/Kullanma_Kilavuzu.tex`,
  `xelatex` ile iki kez derlenir). Testler: `./venv/bin/python -m pytest -q`.

## Sahte veri / sahte animasyon yasağı (ZORUNLU)

UI, ilerleme/canlı çeviri gösterimi, demo, rapor, grafik — hepsi **gerçek
pipeline verisiyle** beslenir. Uydurma sayı, sahte ilerleme, veriye bağlı
olmayan "simülasyon" animasyonu YASAK. Test kodundaki mock'lar bu yasağın
dışındadır (kullanıcıya gösterilen veri değildir). Alt ajanlara arayüz görevi
verirken bu kural prompt'a yazılır.

## Kayıpsızlık (ZORUNLU)

Çeviri her zaman kayıpsızdır: hiçbir cümle, dipnot, tablo hücresi ya da altyazı
atlanmaz veya uydurulmaz. Sayfalama artıkları (sayfa numarası, sızmış sayfa üst
bilgisi, devam eden tablonun yinelenen başlığı ve "devamı" etiketi) dışında
kaynak metinden hiçbir şey silinmez; silinenler sayılarak raporlanır.
