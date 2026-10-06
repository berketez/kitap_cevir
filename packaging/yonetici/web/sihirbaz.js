/*
  Kitapçevir kurulum sihirbazı ve Bileşenler ekranı (TASARIM E).

  Kurallar:
  - Sayfa sayı hesaplamaz ve sayı uydurmaz: boyutlar, toplamlar, hız ve
    ilerleme yöneticiden biçimlenmiş gelir (/api/durum, /api/plan, olaylar).
    Tek istisna saattir: süren adımın geçen süresi ve bekleme geri sayımı
    sunucunun verdiği başlangıç anından gerçek saatle sayılır.
  - Olmayan bilgi gösterilmez ("—").
  - alert/confirm/prompt ve yeni sekme kullanılmaz (WKWebView kabuğu bunları
    göstermez); onaylar sayfa içi pencereyle, dış bağlantılar /api/ac ile açılır.
  - Olay akışı fetch ile okunur: EventSource özel anahtar başlığı taşıyamaz.
*/
"use strict";
(function () {
  const PARAM = new URLSearchParams(window.location.search);
  const KIP = PARAM.get("kip") || "";
  const ANAHTAR_DEPO_ADI = "kitapcevir-anahtar";
  // K9 D7: anahtar okunduktan sonra adres çubuğundan silinir ("Tarayıcıda Aç" ve geçmiş
  // onu taşımasın). Yeniden yüklemede yalnız bu pencerenin oturum deposundan okunur;
  // depo kullanılamıyorsa adres olduğu gibi kalır (sayfa çalışmaya devam etsin).
  const ANAHTAR = (function () {
    let anahtar = PARAM.get("anahtar") || "";
    let saklandi = false;
    try {
      if (anahtar) {
        window.sessionStorage.setItem(ANAHTAR_DEPO_ADI, anahtar);
        saklandi = true;
      } else {
        anahtar = window.sessionStorage.getItem(ANAHTAR_DEPO_ADI) || "";
      }
    } catch (e) { /* oturum deposu kapalı */ }
    if (saklandi) {
      PARAM.delete("anahtar");
      const kalan = PARAM.toString();
      try {
        window.history.replaceState(null, "", window.location.pathname +
          (kalan ? "?" + kalan : "") + window.location.hash);
      } catch (e) { /* adres değiştirilemedi */ }
    }
    return anahtar;
  })();
  const ANAHTAR_BASLIGI = "X-Kitapcevir-Anahtar";
  const AKIS_YENIDEN_BAGLAN_MS = 1500;
  const TEX_GRUBU = "tex";
  const INTERNET = { var: "bağlı", yok: "bağlantı yok", bilinmiyor: "denetlenemedi" };
  const ISARET = { bitti: "✓", suruyor: "⟳", bekliyor: "·", hata: "✗", iptal: "–" };
  const EKRANLAR = ["yukleniyor", "hosgeldin", "lisans", "secim", "ozet", "kurulum", "bitti",
    "bilesenler", "acilis", "sorun", "kaldirildi"];

  const S = {
    D: null,              // son /api/durum yanıtı
    plan: null,           // son /api/plan yanıtı
    sonSecim: null,       // planın isteği (Sürdür aynı seçimi yeniden kurar)
    donus: "secim",       // özet/kurulumdan "Geri"nin gideceği ekran
    secim: { tex: null, istege: new Set() },
    is: null,             // kurulum görünümü: {is, genel, bekleme, bilesenler}
    saatFarki: 0,         // sunucu saati - sayfa saati (sn)
    akisBasladi: false,
    ayriliyor: false,
    sinananlar: new Set(),
    acikLisanslar: new Set(),
    resmiMetinler: {},    // bileşen → /api/lisans_metni yanıtı (panel yenilenince yeniden istenmez)
    iptalDonus: false,
    lisansBileseni: null,
  };
  let etkinEkran = "yukleniyor";

  // ------------------------------------------------------------------
  // Yardımcılar
  // ------------------------------------------------------------------
  const $ = (kimlik) => document.getElementById(kimlik);

  function el(etiket, ozellikler, ...cocuklar) {
    const dugum = document.createElement(etiket);
    for (const [ad, deger] of Object.entries(ozellikler || {})) {
      if (deger === null || deger === undefined || deger === false) continue;
      if (ad === "class") dugum.className = deger;
      else if (ad.startsWith("on") && typeof deger === "function") {
        dugum.addEventListener(ad.slice(2), deger);
      } else dugum.setAttribute(ad, deger === true ? "" : String(deger));
    }
    for (const cocuk of cocuklar.flat()) {
      if (cocuk === null || cocuk === undefined || cocuk === false) continue;
      dugum.append(cocuk instanceof Node ? cocuk : document.createTextNode(String(cocuk)));
    }
    return dugum;
  }

  function metin(kimlik, deger) { $(kimlik).textContent = deger || ""; }
  /* replaceChildren null/false'u "null" metni olarak basar; boşlar atılır. */
  function doldur(kap, ...cocuklar) {
    kap.replaceChildren(...cocuklar.flat().filter((c) => c !== null && c !== undefined && c !== false));
  }
  function goster(kimlik, acik) { $(kimlik).hidden = !acik; }

  function ileti(kimlik, deger) {
    const kutu = $(kimlik);
    kutu.textContent = deger || "";
    kutu.hidden = !deger;
  }

  function uygulamaAdi() {
    return (S.D && S.D.adlar && S.D.adlar.uygulama) || $("uygulama-adi").textContent;
  }

  /* Yöneticinin bicim_sure'siyle aynı kural: "7 sn", "2 dk 5 sn", "1 sa 3 dk".
     Yalnız canlı sayaç (geçen süre, geri sayım) için; sayfa açıkken her saniye
     sunucuya sormamak için burada. */
  function sureMetni(saniye) {
    if (!(saniye >= 0)) return "—";
    const toplam = Math.floor(saniye + 0.5);
    if (toplam < 60) return toplam + " sn";
    const dakika = Math.floor(toplam / 60), sn = toplam % 60;
    if (dakika < 60) return sn ? `${dakika} dk ${sn} sn` : `${dakika} dk`;
    const saat = Math.floor(dakika / 60), dk = dakika % 60;
    return dk ? `${saat} sa ${dk} dk` : `${saat} sa`;
  }

  class ApiHata extends Error {
    constructor(kod, iletiMetni, durumKodu, ayrinti) {
      super(iletiMetni);
      this.kod = kod;
      this.durumKodu = durumKodu;
      this.ayrinti = ayrinti || {};
    }
  }

  async function api(yol, govde) {
    const secenek = { method: govde === undefined ? "GET" : "POST", cache: "no-store",
      headers: { [ANAHTAR_BASLIGI]: ANAHTAR } };
    if (govde !== undefined) {
      secenek.headers["Content-Type"] = "application/json";
      secenek.body = JSON.stringify(govde);
    }
    let yanit;
    try {
      yanit = await fetch(yol, secenek);
    } catch (e) {
      throw new ApiHata("ag", "Yöneticiye ulaşılamadı.", 0);
    }
    let veri = null;
    try { veri = await yanit.json(); } catch (e) { veri = null; }
    if (!yanit.ok) {
      const h = (veri && veri.hata) || {};
      throw new ApiHata(h.kod || "hata", h.ileti || `İstek yerine getirilemedi (${yanit.status}).`,
        yanit.status, h);
    }
    return veri;
  }

  async function ac(hedef, ek) {
    try {
      await api("/api/ac", Object.assign({ hedef }, ek || {}));
    } catch (e) {
      genelIleti(e.message);
    }
  }

  function genelIleti(deger) { ileti("genel-ileti", deger); }

  function ekranGoster(ad) {
    for (const e of EKRANLAR) $("ekran-" + e).hidden = e !== ad;
    etkinEkran = ad;
    genelIleti("");
    window.scrollTo(0, 0);
  }

  // ------------------------------------------------------------------
  // Sayfa içi pencere (onay ve lisans metni)
  // ------------------------------------------------------------------
  function pencereAc(baslik, govde, dugmeler) {
    metin("pencere-baslik", baslik);
    doldur($("pencere-govde"), govde);
    const kap = $("pencere-dugmeler");
    kap.replaceChildren(...dugmeler.map((d) => el("button", {
      type: "button", class: d.sinif || "ikincil",
      onclick: async () => { if (d.tik) await d.tik(); if (!d.acikKalsin) pencereKapat(); },
    }, d.metin)));
    goster("pencere", true);
    const ilk = kap.querySelector("button");
    if (ilk) ilk.focus();
  }

  function pencereKapat() { goster("pencere", false); }

  function onayIste(baslik, aciklama, onayMetni, tehlikeli) {
    return new Promise((coz) => {
      pencereAc(baslik, [el("p", null, aciklama)], [
        { metin: "Vazgeç", tik: () => coz(false) },
        { metin: onayMetni, sinif: tehlikeli ? "ikincil tehlike" : "birincil", tik: () => coz(true) },
      ]);
    });
  }

  // KARAR 34: lisans özetimiz yok. Resmî metin (uygulamadaki kopya; yönetici
  // SHA-256'sını manifestle eşleştirmeden metin vermez) sayfanın içinde gösterilir,
  // resmî adres tarayıcıda açılabilir.
  function resmiMetinCiz(kutu, kimlik, m, y) {
    const parcalar = [];
    if (y.metin) {
      parcalar.push(el("pre", { class: "lisans-metni", tabindex: "0" }, y.metin));
    } else {
      parcalar.push(el("p", { class: "uyari-metin" }, "Resmî metnin bu Mac'teki kopyası " +
        `gösterilemiyor (${y.neden}). Kabul etmeden önce resmî adresten oku.`));
    }
    if (y.url) {
      parcalar.push(el("p", { class: "soluk kucuk yol" }, "Resmî adres: " + y.url + " · ",
        el("button", { type: "button", class: "baglanti",
          onclick: () => ac("lisans_tam_metin", { bilesen: kimlik }) }, m.tam_metin_etiketi)));
    }
    kutu.replaceChildren(...parcalar);
  }

  function resmiMetinKutusu(kimlik, m) {
    const kutu = el("div", { class: "resmi-metin" });
    const onceki = S.resmiMetinler[kimlik];
    if (onceki) {
      resmiMetinCiz(kutu, kimlik, m, onceki);
      return kutu;
    }
    kutu.append(el("p", { class: "soluk" }, "Resmî metin okunuyor…"));
    api("/api/lisans_metni?bilesen=" + encodeURIComponent(kimlik)).then((y) => {
      if (y.metin) S.resmiMetinler[kimlik] = y;
      resmiMetinCiz(kutu, kimlik, m, y);
    }).catch((e) => {
      kutu.replaceChildren(el("p", { class: "uyari-metin" }, e.message));
    });
    return kutu;
  }

  // ------------------------------------------------------------------
  // Durum
  // ------------------------------------------------------------------
  async function durumYenile(sorgu) {
    S.D = await api("/api/durum" + (sorgu ? "?" + sorgu : ""));
    adlariYaz();
    return S.D;
  }

  function adlariYaz() {
    const adlar = (S.D && S.D.adlar) || {};
    if (adlar.uygulama) {
      document.title = adlar.uygulama;
      for (const s of document.querySelectorAll('[data-ad="uygulama"]')) s.textContent = adlar.uygulama;
      metin("uygulama-adi", adlar.uygulama);
    }
    metin("alt-baslik", adlar.alt_baslik || "");
  }

  function bilesen(kimlik) {
    return ((S.D && S.D.bilesenler) || []).find((b) => b.kimlik === kimlik) || null;
  }

  function sorunGoster(deger) {
    metin("sorun-iletisi", deger);
    metin("sorun-gunluk", (S.D && S.D.gunluk_metni) || "");
    ekranGoster("sorun");
  }

  // ------------------------------------------------------------------
  // E.1 Hoş geldin
  // ------------------------------------------------------------------
  function hosgeldinGoster() {
    const D = S.D;
    const hw = D.donanim || {};
    const parcalar = [];
    if (hw.baslik) parcalar.push(hw.baslik);
    if (D.macos) parcalar.push("macOS " + D.macos);
    parcalar.push("boş disk " + D.bos_disk_metni);
    metin("mac-bilgisi", "Bu Mac: " + parcalar.join(" · "));
    const yarim = (D.bilesenler || []).some((b) => b.durum !== "eksik");
    goster("hosgeldin-ilk", !yarim);
    goster("hosgeldin-yarim", yarim);
    metin("internet-bilgisi", "İnternet: " + (INTERNET[D.internet] || D.internet));
    const uyarilar = [hw.sorun, hw.genel_kapi && hw.genel_kapi.uyari].filter(Boolean);
    metin("donanim-uyarisi", uyarilar.join(" "));
    goster("donanim-uyarisi", uyarilar.length > 0);
    if (D.kaynaklar && D.kaynaklar.length) {
      metin("kaynaklar-bilgisi", `Bileşenler yayıncılarının kendi sunucularından doğrudan iner ` +
        `(${D.kaynaklar.join(", ")}). ${uygulamaAdi()} bu dosyaları barındırmaz.`);
    }
    ekranGoster("hosgeldin");
  }

  // ------------------------------------------------------------------
  // E.2 Lisanslar (zorunlu bileşen onayı; reddedilirse kurulum durur)
  // ------------------------------------------------------------------
  function zorunluLisansBileseni() {
    return (S.D.bilesenler || []).find((b) => b.zorunluluk === "zorunlu" && b.lisans.onay_gerekli
      && b.durum !== "kurulu") || null;
  }

  function lisansGovdesi(m, kimlik) {
    const parcalar = [el("p", null, m.giris),
      el("ul", null, m.bilesenler.map((satir) => el("li", null, satir))),
      resmiMetinKutusu(kimlik, m)];
    if (m.resmi_not) parcalar.push(el("p", { class: "soluk kucuk" }, m.resmi_not));
    if (m.beyan) parcalar.push(el("p", { class: "soluk" }, m.beyan));
    return parcalar;
  }

  function lisansGoster() {
    const b = zorunluLisansBileseni();
    if (!b) { secimGoster(); return; }
    const m = b.lisans.metin;
    S.lisansBileseni = b.kimlik;
    if (!m) {
      sorunGoster(`${b.ad}: lisans metni gösterilemiyor (${b.lisans.neden}). ` +
        "Onay verilmeden bu bileşen kurulamaz.");
      return;
    }
    metin("lisans-baslik", m.baslik);
    $("lisans-govde").replaceChildren(...lisansGovdesi(m, S.lisansBileseni));
    metin("lisans-tam-metin", m.tam_metin_etiketi);
    metin("lisans-kutu-etiketi", m.kutu_etiketi);
    metin("lisans-red-iletisi", m.red_iletisi);
    $("lisans-kutu").checked = !!b.lisans.onaylandi;
    $("lisans-devam").disabled = !$("lisans-kutu").checked;
    goster("lisans-red", false);
    ekranGoster("lisans");
  }

  async function lisansDevam() {
    $("lisans-devam").disabled = true;
    try {
      await api("/api/lisans", { bilesen: S.lisansBileseni, kabul: true });
      await durumYenile();
      secimGoster();
    } catch (e) {
      genelIleti(e.message);
      $("lisans-devam").disabled = !$("lisans-kutu").checked;
    }
  }

  async function lisansReddet() {
    const b = bilesen(S.lisansBileseni);
    $("lisans-kutu").checked = false;
    $("lisans-devam").disabled = true;
    if (b && b.lisans.onaylandi) {
      try {
        await api("/api/lisans", { bilesen: b.kimlik, kabul: false });
        await durumYenile();
      } catch (e) {
        genelIleti(e.message);
      }
    }
    goster("lisans-red", true);
    $("lisans-red").scrollIntoView({ block: "nearest" });
  }

  // ------------------------------------------------------------------
  // E.3 Bileşen seçimi
  // ------------------------------------------------------------------
  function texUyeleri() {
    return (S.D.bilesenler || []).filter((b) => b.grup === TEX_GRUBU && b.gorunur);
  }

  function texVarsayilani() {
    const uyeler = texUyeleri();
    const secilebilir = uyeler.filter((b) => b.secilebilir || b.durum === "kurulu");
    const varsayilan = secilebilir.find((b) => b.kimlik === S.D.tex_varsayilan);
    return S.D.tex_etkin || (varsayilan && varsayilan.kimlik)
      || (secilebilir[0] && secilebilir[0].kimlik) || null;
  }

  function boyutSutunlari(b) {
    return el("div", { class: "boyutlar" },
      el("span", { class: "boyut" }, b.boyut.indirme_metni),
      el("span", { class: "boyut" }, b.boyut.disk_metni));
  }

  function donanimNotlari(b) {
    const notlar = [];
    if (!b.secilebilir && b.neden) notlar.push(el("div", { class: "not hata-metin" }, b.neden));
    if (!b.secilebilir && b.ipucu) notlar.push(el("div", { class: "not soluk" }, b.ipucu));
    // Algılanan TeX'in yolu ve sürümü texAdayNotu'nda; K3'ün "bilgi"si aynısıdır.
    if (b.secilebilir && b.bilgi && !b.algilanan) notlar.push(el("div", { class: "not soluk" }, b.bilgi));
    if (b.uyari) notlar.push(el("div", { class: "not uyari-metin" }, b.uyari));
    return notlar;
  }

  function secimSatiri(b, girdi, notlar) {
    const kimlik = "sec-" + b.kimlik;
    girdi.id = kimlik;
    return el("div", { class: "satir" + (girdi.disabled && !girdi.checked ? " pasif" : "") },
      girdi,
      el("div", { class: "govde" },
        el("label", { for: kimlik, class: "ad" }, b.ad),
        b.aciklama ? el("div", { class: "aciklama" }, b.aciklama) : null,
        notlar),
      boyutSutunlari(b));
  }

  function durumNotu(b) {
    if (b.durum === "kurulu") return el("div", { class: "not basari-metin" }, "Kurulu");
    if (b.durum === "bozuk" || b.durum === "guncellenecek") {
      return el("div", { class: "not uyari-metin" }, b.durum_metni +
        (b.durum_nedeni ? ` (${b.durum_nedeni})` : ""));
    }
    return null;
  }

  function lisansPaneli(b, yenile) {
    const m = b.lisans.metin;
    if (!m) return el("div", { class: "not hata-metin" }, b.lisans.neden || "Lisans metni yok.");
    const kutu = el("input", { type: "checkbox" });
    kutu.checked = !!b.lisans.onaylandi;
    kutu.addEventListener("change", async () => {
      kutu.disabled = true;
      try {
        const y = await api("/api/lisans", { bilesen: b.kimlik, kabul: kutu.checked });
        b.lisans = y.lisans;
      } catch (e) {
        kutu.checked = !kutu.checked;
        genelIleti(e.message);
      }
      kutu.disabled = false;
      yenile();
    });
    return el("div", { class: "lisans-paneli" },
      el("strong", null, m.baslik),
      lisansGovdesi(m, b.kimlik),
      el("label", { class: "onay-kutusu" }, kutu, el("span", null, m.kutu_etiketi)));
  }

  function lisansAcici(b, yenile) {
    const acik = S.acikLisanslar.has(b.kimlik);
    return el("div", { class: "not" },
      el("button", { type: "button", class: "baglanti", "aria-expanded": acik ? "true" : "false",
        onclick: () => {
          if (acik) S.acikLisanslar.delete(b.kimlik); else S.acikLisanslar.add(b.kimlik);
          yenile();
        } }, acik ? "Lisans metnini gizle" : "Lisans metnini oku"),
      b.lisans.onaylandi ? el("span", { class: "basari-metin" }, " · kabul edildi") :
        el("span", { class: "soluk" }, " · kabul edilmedi"),
      acik ? lisansPaneli(b, yenile) : null);
  }

  async function texSina(aday) {
    if (S.sinananlar.has(aday.yol)) return;
    S.sinananlar.add(aday.yol);
    try {
      await api("/api/tex_sina", { yol: aday.yol });
      await durumYenile();
    } catch (e) {
      genelIleti(e.message);
    }
    if (etkinEkran === "secim") secimCiz();
    if (etkinEkran === "bilesenler" && !$("bilesenler-panel").hidden) texPaneliCiz();
  }

  function texAdayNotu(b) {
    const aday = b.algilanan;
    if (!aday) return [];
    const parcalar = [aday.yol];
    if (aday.surum) parcalar.push(aday.surum);
    let sinama;
    if (aday.sinama && aday.sinama.gecti === true) sinama = "şablon sınaması: geçti";
    else if (aday.sinama) sinama = null;
    else sinama = "şablon sınanıyor…";
    if (sinama) parcalar.push(sinama);
    if (!aday.sinama) texSina(aday);
    return [el("div", { class: "not soluk yol" }, parcalar.join(" · "))];
  }

  function texSatirlari(kap, yenile) {
    const secili = S.secim.tex;
    for (const b of texUyeleri()) {
      const girdi = el("input", { type: "radio", name: "tex", value: b.kimlik });
      girdi.checked = secili === b.kimlik;
      girdi.disabled = !b.secilebilir && b.durum !== "kurulu";
      girdi.addEventListener("change", () => { S.secim.tex = b.kimlik; yenile(); });
      const notlar = [...texAdayNotu(b), durumNotu(b), ...donanimNotlari(b)];
      if (S.D.tex_etkin === b.kimlik) notlar.unshift(el("div", { class: "not basari-metin" }, "Etkin"));
      kap.append(secimSatiri(b, girdi, notlar));
    }
  }

  function secimCiz() {
    const D = S.D;
    const zorunlu = $("secim-zorunlu");
    zorunlu.replaceChildren();
    for (const b of D.bilesenler.filter((x) => x.zorunluluk === "zorunlu" && x.gorunur)) {
      const girdi = el("input", { type: "checkbox" });
      girdi.checked = true;
      girdi.disabled = true;
      const notlar = [durumNotu(b)];
      if (b.lisans.onay_gerekli) {
        notlar.push(el("div", { class: "not " + (b.lisans.onaylandi ? "basari-metin" : "hata-metin") },
          b.lisans.onaylandi ? "Lisans: kabul edildi" : "Lisans: kabul edilmedi"));
      }
      zorunlu.append(secimSatiri(b, girdi, notlar));
    }
    const tex = $("secim-tex");
    tex.replaceChildren();
    texSatirlari(tex, secimCiz);

    const istege = $("secim-istege");
    istege.replaceChildren();
    for (const b of D.bilesenler.filter((x) => x.zorunluluk === "istege_bagli" && x.gorunur)) {
      const girdi = el("input", { type: "checkbox" });
      const kurulu = b.durum !== "eksik";
      girdi.checked = kurulu || S.secim.istege.has(b.kimlik);
      girdi.disabled = kurulu || !b.secilebilir;
      girdi.addEventListener("change", () => {
        if (girdi.checked) {
          S.secim.istege.add(b.kimlik);
          if (b.lisans.onay_gerekli && !b.lisans.onaylandi) S.acikLisanslar.add(b.kimlik);
        } else {
          S.secim.istege.delete(b.kimlik);
        }
        secimCiz();
      });
      const notlar = [durumNotu(b), ...donanimNotlari(b)];
      if (!kurulu && b.secilebilir && b.lisans.onay_gerekli) notlar.push(lisansAcici(b, secimCiz));
      istege.append(secimSatiri(b, girdi, notlar));
    }
    metin("secim-olcum", D.olcum_metni);
    const eksikLisans = [...S.secim.istege].map(bilesen)
      .filter((b) => b && b.lisans.onay_gerekli && !b.lisans.onaylandi);
    ileti("secim-ileti", eksikLisans.length
      ? eksikLisans.map((b) => `${b.ad} için lisansı kabul etmelisin ya da seçimi kaldırmalısın.`).join(" ")
      : "");
    $("secim-devam").disabled = eksikLisans.length > 0 || !S.secim.tex;
  }

  function secimGoster() {
    if (S.secim.tex === null || !texUyeleri().some((b) => b.kimlik === S.secim.tex)) {
      S.secim.tex = texVarsayilani();
    }
    for (const k of [...S.secim.istege]) {
      const b = bilesen(k);
      if (!b || !b.secilebilir) S.secim.istege.delete(k);
    }
    secimCiz();
    ekranGoster("secim");
  }

  // ------------------------------------------------------------------
  // E.4 Özet
  // ------------------------------------------------------------------
  async function planGoster(secim, donus, iletiKimligi) {
    try {
      S.plan = await api("/api/plan", secim);
    } catch (e) {
      if (iletiKimligi) ileti(iletiKimligi, e.message); else genelIleti(e.message);
      return false;
    }
    S.sonSecim = secim;
    S.donus = donus;
    ozetCiz();
    ekranGoster("ozet");
    return true;
  }

  function ozetCiz() {
    const p = S.plan;
    metin("ozet-kurulacaklar", p.yalniz_tex
      ? `yok. Etkin TeX değişecek: ${p.tex_ad} (kurulu; yeniden inmez)`
      : (p.bos ? "yok (seçilenlerin hepsi kurulu)" : p.kurulacaklar_metni));
    metin("ozet-toplam", `Toplam indirme: ${p.indirme_metni} · Diskte yer: ${p.disk_metni}`);
    metin("ozet-olculmeyen", p.disk_olculmeyen.length
      ? "Diskte kapladığı yer ölçülmedi: " + p.disk_olculmeyen.join(", ") + "." : "");
    goster("ozet-olculmeyen", p.disk_olculmeyen.length > 0);
    const disk = $("ozet-disk");
    disk.className = p.yeter ? "" : "hata-metin";
    disk.textContent = p.yeter
      ? `Boş disk: ${p.bos_disk_metni} (yeterli; güvenlik payıyla gereken ${p.gerekli_metni})`
      : `Boş disk: ${p.bos_disk_metni}. Yetmiyor: ${p.eksik_metni} daha gerekiyor.`;
    metin("ozet-yer", p.kok_metni);
    $("ozet-tablo").replaceChildren(...p.satirlar.map((a) => el("div", { class: "satir" },
      el("div", { class: "govde" }, el("span", { class: "ad" }, a.ad + (a.onar ? " (onarım)" : ""))),
      el("div", { class: "boyutlar" }, el("span", { class: "boyut" }, a.indirme_metni),
        el("span", { class: "boyut" }, a.disk_metni)))));
    goster("ozet-internet", p.internet === "yok");
    /* Onayı eksik lisans burada da okunup kabul edilebilir: "Bileşenler" ekranından
       ("Eksikleri kur") gelen, lisans ekranını görmemiş olabilir. Kabul edilmedikçe
       "Kur" kapalı kalır; kabul edilince plan yeniden istenir (onay sunucuda denetlenir). */
    const onay = $("ozet-onay");
    doldur(onay, p.eksik_onaylar.map((e) => {
      const b = bilesen(e.bilesen);
      return el("div", { class: "eksik-onay" },
        el("p", null, e.ileti || `${e.ad}: lisans kabul edilmedi.`),
        b && b.lisans && b.lisans.metin
          ? lisansPaneli(b, () => { if (S.sonSecim) planGoster(S.sonSecim, S.donus); })
          : null);
    }));
    onay.hidden = p.eksik_onaylar.length === 0;
    metin("ozet-kur", p.yalniz_tex ? "Uygula" : "Kur");
    $("ozet-kur").disabled = (p.bos && !p.yalniz_tex) || !p.yeter || p.eksik_onaylar.length > 0
      || (p.internet === "yok" && !p.bos);
  }

  async function kurBaslat() {
    $("ozet-kur").disabled = true;
    let yanit;
    try {
      yanit = await api("/api/kur", { plan_kimligi: S.plan.plan_kimligi });
    } catch (e) {
      if ((e.kod === "plan_eskidi" || e.kod === "plan_yok") && S.sonSecim) {
        await planGoster(S.sonSecim, S.donus);
      }
      genelIleti(e.message);
      if (S.plan) ozetCiz();
      return;
    }
    S.iptalDonus = false;
    // "is_basladi" olayı yanıttan önce geldiyse görünüm zaten dolu: ezilmez.
    const gelen = S.is && S.is.is && S.is.is.plan_kimligi === yanit.plan_kimligi;
    if (!gelen) S.is = { is: { calisiyor: true, plan_kimligi: yanit.plan_kimligi }, genel: null, bekleme: null,
      bilesenler: S.plan.satirlar.map((a) => ({ kimlik: a.bilesen, ad: a.ad, onar: a.onar,
        durum: "bekliyor", durum_metni: "bekliyor", adimlar: [] })) };
    kurulumCiz();
    ekranGoster("kurulum");
  }

  // ------------------------------------------------------------------
  // E.5 Kurulum (gerçek ilerleme)
  // ------------------------------------------------------------------
  function adimSagi(a) {
    const sinif = "sag-metin";
    /* Paket adımı tekerleklerini kendisi indirir; indirme bitince (oran 1) uv kurar.
       O aşamada inen bayt yanında gerçek geçen süre gösterilir (E.5 "kuruluyor… 7 sn"). */
    const indirmeBitti = a.tur === "kurulum" && a.oran === 1;
    if (a.durum === "suruyor" && (!a.bayt_metni || indirmeBitti)) {
      const onek = (a.bayt_metni ? a.bayt_metni + " · " : "") +
        (a.sayac_metni ? a.sayac_metni + " · " : "") +
        (a.tur === "kurulum" ? "kuruluyor… " : "sürüyor… ");
      return el("span", { class: sinif, "data-baslangic": a.baslangic, "data-onek": onek },
        onek + sureMetni(Date.now() / 1000 + S.saatFarki - a.baslangic));
    }
    if (a.bayt_metni) return el("span", { class: sinif }, a.bayt_metni);
    if (a.sayac_metni) return el("span", { class: sinif }, a.sayac_metni);
    if (a.durum === "bitti" && a.sure_metni) return el("span", { class: sinif }, a.sure_metni);
    return el("span", { class: sinif }, a.durum_metni);
  }

  function kucukCubuk(oran) {
    const dolu = el("div", { class: "cubuk-dolu" });
    dolu.style.width = (Math.max(0, Math.min(1, oran)) * 100).toFixed(1) + "%";
    return el("div", { class: "cubuk kucuk-cubuk", "aria-hidden": "true" }, dolu);
  }

  function bilesenBlogu(b) {
    const sagMetin = b.durum === "bekliyor" ? "bekliyor" : b.durum_metni;
    const blok = el("li", { class: "d-" + b.durum, "data-bilesen": b.kimlik },
      el("div", { class: "adim-baslik" },
        el("span", null, el("span", { class: "isaret", "aria-hidden": "true" }, ISARET[b.durum] || ""),
          el("span", { class: "ad" }, b.ad + (b.onar ? " (onarım)" : ""))),
        el("span", { class: "sag-metin" }, sagMetin)));
    if (b.adimlar && b.adimlar.length) {
      const alt = el("ul", { class: "adim-alt" });
      for (const a of b.adimlar) {
        alt.append(el("li", { class: "d-" + a.durum },
          el("span", null, el("span", { class: "isaret", "aria-hidden": "true" }, ISARET[a.durum] || ""), a.ad),
          adimSagi(a)));
        if (a.oge) {
          alt.append(el("li", { class: "oge-satiri" }, el("span", null, a.oge.ad),
            el("span", { class: "sag-metin" }, a.oge.metin)));
        }
        if (a.durum === "suruyor" && typeof a.oran === "number") alt.append(el("li", null, kucukCubuk(a.oran)));
      }
      blok.append(alt);
    }
    return blok;
  }

  function genelCiz(g) {
    const cubuk = $("genel-cubuk");
    if (!g) {
      metin("genel-metin", "—");
      metin("genel-hiz", "");
      cubuk.hidden = true;
      return;
    }
    metin("genel-metin", g.metin);
    metin("genel-hiz", "hız " + g.hiz_metni + (g.kalan_metni ? " · kalan " + g.kalan_metni : ""));
    cubuk.hidden = typeof g.oran !== "number";
    if (typeof g.oran === "number") {
      const yuzde = Math.max(0, Math.min(1, g.oran)) * 100;
      cubuk.firstElementChild.style.width = yuzde.toFixed(1) + "%";
      cubuk.setAttribute("aria-valuenow", yuzde.toFixed(0));
      cubuk.setAttribute("aria-valuetext", g.metin);
    }
  }

  function beklemeCiz(bk) {
    const satir = $("bekleme-satiri");
    if (!bk) { satir.hidden = true; satir.textContent = ""; return; }
    satir.hidden = false;
    if (typeof bk.kalan_sn === "number") {
      const bitis = bk.zaman + bk.kalan_sn;
      satir.dataset.bitis = String(bitis);
      satir.dataset.onek = bk.neden + ": ";
      const kalan = Math.max(0, Math.round(bitis - (Date.now() / 1000 + S.saatFarki)));
      satir.textContent = `${bk.neden}: ${kalan} sn sonra devam edilecek`;
    } else {
      delete satir.dataset.bitis;
      satir.textContent = bk.neden;
    }
  }

  function isDurumuCiz() {
    const is = (S.is && S.is.is) || {};
    metin("kurulum-gunluk", is.gunluk_metni || is.gunluk || "—");
    goster("kurulum-gunluk-goster", !!is.gunluk);
    const suruyor = is.calisiyor !== false;
    goster("kurulum-duraklat", suruyor);
    goster("kurulum-iptal", suruyor);
    goster("kurulum-surdur", !suruyor && is.sonuc !== "tamam");
    goster("kurulum-geri", !suruyor && is.sonuc !== "tamam");
    const sonuc = $("kurulum-sonuc");
    if (suruyor || is.sonuc === "tamam") { sonuc.hidden = true; return; }
    sonuc.replaceChildren();
    if (is.sonuc === "iptal") {
      sonuc.className = "kutu uyari";
      sonuc.append(el("p", null, "Durduruldu. İndirilen kısım saklandı; “Sürdür”e basınca kaldığı yerden devam eder."));
      metin("kurulum-surdur", "Sürdür");
    } else {
      sonuc.className = "kutu hata";
      sonuc.append(el("p", null, (is.hata && is.hata.ileti) || "Kurulum tamamlanamadı."));
      if (is.hata && is.hata.ayrinti_gunluk) {
        sonuc.append(el("p", { class: "soluk kucuk yol" }, "Ayrıntı: " + is.hata.ayrinti_gunluk));
      }
      metin("kurulum-surdur", "Yeniden dene");
    }
    sonuc.hidden = false;
  }

  function kurulumCiz() {
    if (!S.is) return;
    genelCiz(S.is.genel);
    beklemeCiz(S.is.bekleme);
    $("kurulum-listesi").replaceChildren(...(S.is.bilesenler || []).map(bilesenBlogu));
    isDurumuCiz();
  }

  function bilesenBlogunuYenile(b) {
    const eski = $("kurulum-listesi").querySelector(`[data-bilesen="${CSS.escape(b.kimlik)}"]`);
    if (eski) eski.replaceWith(bilesenBlogu(b));
    else $("kurulum-listesi").append(bilesenBlogu(b));
  }

  async function isBitti(sonuc) {
    try { await durumYenile(); } catch (e) { /* durum sonra yenilenir */ }
    if (etkinEkran !== "kurulum") return;
    if (sonuc === "tamam" && S.donus === "bilesenler") {
      const yalnizTex = S.plan && S.plan.yalniz_tex;
      await bilesenlerGoster();
      ileti("bilesenler-basari", yalnizTex ? `Etkin TeX: ${S.plan.tex_ad}.` : "Kurulum tamamlandı.");
      return;
    }
    if (sonuc === "tamam") { bittiGoster(); return; }
    if (sonuc === "iptal" && S.iptalDonus) {
      await geriDon();
      genelIleti("Kurulum durduruldu. İndirilen kısım saklandı; sonra kaldığı yerden sürer.");
    }
  }

  function olayIsle(tur, veri) {
    if (typeof veri.sunucu_zamani === "number") S.saatFarki = veri.sunucu_zamani - Date.now() / 1000;
    if (tur === "ozet") {
      if (veri.is) { S.is = veri; if (etkinEkran === "kurulum") kurulumCiz(); }
      return;
    }
    const g = veri.gorunum || {};
    if (!S.is) S.is = { is: null, genel: null, bekleme: null, bilesenler: [] };
    if (g.genel) S.is.genel = g.genel;
    S.is.bekleme = g.bekleme || null;
    if (g.is) S.is.is = g.is;
    const tamCizim = !!g.bilesenler;
    if (g.bilesenler) S.is.bilesenler = g.bilesenler;
    else if (g.bilesen) {
      const sira = S.is.bilesenler.findIndex((b) => b.kimlik === g.bilesen.kimlik);
      if (sira >= 0) S.is.bilesenler[sira] = g.bilesen; else S.is.bilesenler.push(g.bilesen);
    }
    if (tur === "is_basladi" && etkinEkran !== "kurulum" && etkinEkran !== "acilis") {
      ekranGoster("kurulum");
    }
    if (etkinEkran === "kurulum") {
      if (tamCizim) kurulumCiz();
      else {
        genelCiz(S.is.genel);
        beklemeCiz(S.is.bekleme);
        if (g.bilesen) bilesenBlogunuYenile(g.bilesen);
      }
    }
    if (tur === "bitti") isBitti(veri.sonuc);
  }

  function blokIsle(blok) {
    let tur = "message";
    const veriler = [];
    for (const satir of blok.split("\n")) {
      if (satir.startsWith(":")) continue;
      if (satir.startsWith("event: ")) tur = satir.slice(7);
      else if (satir.startsWith("data: ")) veriler.push(satir.slice(6));
    }
    if (!veriler.length) return;
    let veri;
    try { veri = JSON.parse(veriler.join("\n")); } catch (e) { return; }
    olayIsle(tur, veri);
  }

  async function olayAkisi() {
    if (S.akisBasladi) return;
    S.akisBasladi = true;
    while (!S.ayriliyor) {
      try {
        const yanit = await fetch("/api/olaylar", { cache: "no-store",
          headers: { [ANAHTAR_BASLIGI]: ANAHTAR } });
        if (!yanit.ok || !yanit.body) throw new Error("akış açılamadı");
        const okuyucu = yanit.body.getReader();
        const cozucu = new TextDecoder("utf-8");
        let tampon = "";
        for (;;) {
          const { value, done } = await okuyucu.read();
          if (done) break;
          tampon += cozucu.decode(value, { stream: true });
          let ayrac;
          while ((ayrac = tampon.indexOf("\n\n")) >= 0) {
            blokIsle(tampon.slice(0, ayrac));
            tampon = tampon.slice(ayrac + 2);
          }
        }
      } catch (e) {
        /* bağlantı koptu: aşağıda yeniden bağlanılır, özet yeniden gelir */
      }
      if (S.ayriliyor) break;
      await new Promise((coz) => setTimeout(coz, AKIS_YENIDEN_BAGLAN_MS));
    }
  }

  function saatTik() {
    const simdi = Date.now() / 1000 + S.saatFarki;
    for (const s of document.querySelectorAll("[data-baslangic]")) {
      s.textContent = s.dataset.onek + sureMetni(simdi - Number(s.dataset.baslangic));
    }
    const bekleme = $("bekleme-satiri");
    if (!bekleme.hidden && bekleme.dataset.bitis) {
      const kalan = Math.max(0, Math.round(Number(bekleme.dataset.bitis) - simdi));
      bekleme.textContent = `${bekleme.dataset.onek}${kalan} sn sonra devam edilecek`;
    }
  }

  async function durdur(donsun) {
    S.iptalDonus = donsun;
    $("kurulum-duraklat").disabled = true;
    $("kurulum-iptal").disabled = true;
    try {
      await api("/api/iptal", {});
    } catch (e) {
      genelIleti(e.message);
    }
    $("kurulum-duraklat").disabled = false;
    $("kurulum-iptal").disabled = false;
  }

  async function surdur() {
    if (!S.sonSecim) { await geriDon(); return; }
    try {
      S.plan = await api("/api/plan", S.sonSecim);
    } catch (e) {
      genelIleti(e.message);
      return;
    }
    if ((S.plan.bos && !S.plan.yalniz_tex) || !S.plan.yeter || S.plan.eksik_onaylar.length
        || (S.plan.internet === "yok" && !S.plan.bos)) {
      ozetCiz();
      ekranGoster("ozet");
      return;
    }
    await kurBaslat();
  }

  async function geriDon() {
    try { await durumYenile(S.donus === "bilesenler" ? "diskte=1" : ""); } catch (e) { /* aşağıda gösterilir */ }
    if (S.donus === "bilesenler") await bilesenlerGoster();
    else secimGoster();
  }

  // ------------------------------------------------------------------
  // E.6 Bitti
  // ------------------------------------------------------------------
  function bittiGoster() {
    const liste = $("bitti-listesi");
    liste.replaceChildren(...((S.is && S.is.bilesenler) || []).map((b) => el("li", null, b.ad)));
    const hazir = !!(S.D && S.D.uygulama_hazir);
    /* İş başarıyla bitse de uygulama açılamıyorsa (ör. etkin TeX yok) başlık bunu söyler. */
    metin("bitti-baslik", hazir ? "Kurulum tamamlandı."
      : `Kurulum işi bitti, ama ${uygulamaAdi()} henüz açılamıyor.`);
    $("bitti-ac").disabled = !hazir;
    ileti("bitti-neden", hazir ? "" : ((S.D && S.D.hazir_degil_nedeni) || ""));
    ekranGoster("bitti");
  }

  async function uygulamayiAc() {
    ekranGoster("acilis");
    try {
      const yanit = await api("/api/uygulama", {});
      S.ayriliyor = true;
      window.location.assign(yanit.url);
    } catch (e) {
      try { await durumYenile("diskte=1"); } catch (e2) { /* yok sayılır */ }
      await bilesenlerGoster();
      ileti("bilesenler-ileti", e.message + (e.ayrinti.gunluk ? ` Günlük: ${e.ayrinti.gunluk}` : ""));
    }
  }

  // ------------------------------------------------------------------
  // E.7 Bileşenler
  // ------------------------------------------------------------------
  function islemDugmesi(metinDegeri, tik, secenek) {
    const s = secenek || {};
    return el("button", { type: "button", class: (s.sinif || "ikincil") + " kucuk-dugme",
      disabled: s.pasif ? true : null, title: s.baslik || null, onclick: tik }, metinDegeri);
  }

  function zorunluIslemleri(b) {
    if (b.durum === "kuruluyor") return [];
    if (b.durum === "eksik") return [islemDugmesi("Kur", () => eksikleriKur())];
    if (b.durum === "guncellenecek") return [islemDugmesi("Güncelle", () => eksikleriKur())];
    return [islemDugmesi("Onar", () => onar(b))];
  }

  function onar(b) {
    return planGoster({ tex: S.D.tex_etkin, istege_bagli: [], onar: [b.kimlik] },
      "bilesenler", "bilesenler-ileti");
  }

  function eksikleriKur() {
    return planGoster({ tex: S.D.tex_etkin, istege_bagli: [], onar: [] }, "bilesenler",
      "bilesenler-ileti");
  }

  async function kaldir(b) {
    const tamam = await onayIste(`${b.ad} kaldırılsın mı?`,
      `${b.ad} kaldırılacak (${b.diskte_metni} silinecek). Kitapların ve çeviri belleğin silinmez.`,
      "Kaldır", true);
    if (!tamam) return;
    try {
      await api("/api/kaldir", { bilesen: b.kimlik });
    } catch (e) {
      ileti("bilesenler-ileti", e.message);
      return;
    }
    await bilesenlerGoster();
  }

  function istegeIslemleri(b) {
    if (b.durum === "kuruluyor") return [];
    if (b.durum === "eksik") {
      return [islemDugmesi(`İndir ${b.boyut.indirme_metni}`, () => indirmeBaslat(b),
        { pasif: !b.secilebilir, baslik: b.neden || null, sinif: "birincil" })];
    }
    const islemler = [islemDugmesi("Onar", () => onar(b))];
    if (b.kaldirilabilir) islemler.push(islemDugmesi("Kaldır", () => kaldir(b), { sinif: "ikincil tehlike" }));
    return islemler;
  }

  function indirmeBaslat(b) {
    if (b.lisans.onay_gerekli && !b.lisans.onaylandi) {
      S.acikLisanslar.add(b.kimlik);
      indirmePaneliCiz(b);
      return;
    }
    planGoster({ tex: S.D.tex_etkin, istege_bagli: [b.kimlik], onar: [] }, "bilesenler",
      "bilesenler-ileti");
  }

  function indirmePaneliCiz(b) {
    const panel = $("bilesenler-panel");
    const yenile = () => indirmePaneliCiz(bilesen(b.kimlik) || b);
    const guncel = bilesen(b.kimlik) || b;
    doldur(panel,
      el("h2", null, guncel.ad),
      ...donanimNotlari(guncel),
      lisansPaneli(guncel, yenile),
      el("div", { class: "dugmeler" },
        el("button", { type: "button", class: "ikincil", onclick: () => goster("bilesenler-panel", false) }, "Vazgeç"),
        el("button", { type: "button", class: "birincil", disabled: guncel.lisans.onaylandi ? null : true,
          onclick: () => planGoster({ tex: S.D.tex_etkin, istege_bagli: [guncel.kimlik], onar: [] },
            "bilesenler", "bilesenler-ileti") }, `İndir ${guncel.boyut.indirme_metni}`)));
    panel.hidden = false;
    panel.scrollIntoView({ block: "nearest" });
  }

  function texPaneliCiz() {
    const panel = $("bilesenler-panel");
    const kap = el("div", { class: "liste", role: "radiogroup", "aria-label": "TeX dağıtımı" });
    texSatirlari(kap, texPaneliCiz);
    const secilen = texUyeleri().find((b) => b.kimlik === S.secim.tex);
    doldur(panel,
      el("h2", null, "TeX dağıtımı"), kap,
      el("div", { class: "dugmeler" },
        el("button", { type: "button", class: "ikincil", onclick: () => goster("bilesenler-panel", false) }, "Vazgeç"),
        el("button", { type: "button", class: "birincil",
          disabled: (!secilen || S.secim.tex === S.D.tex_etkin) ? true : null,
          onclick: () => planGoster({ tex: S.secim.tex, istege_bagli: [], onar: [] }, "bilesenler",
            "bilesenler-ileti") }, "Devam →")));
    panel.hidden = false;
  }

  function tabloSatiri(ad, b, islemler) {
    const eksik = b.durum === "eksik";
    return el("tr", null,
      el("td", { class: "ad" }, ad),
      el("td", { class: "durum-" + b.durum }, b.durum_metni +
        (b.durum_nedeni ? ` (${b.durum_nedeni})` : "")),
      el("td", null, eksik ? "—" : (b.surum || "—")),
      el("td", { class: "sayi" }, eksik ? "—" : b.diskte_metni),
      el("td", { class: "islem" }, islemler));
  }

  async function bilesenlerGoster() {
    try {
      await durumYenile("diskte=1");
    } catch (e) {
      sorunGoster(e.message);
      return;
    }
    const D = S.D;
    if (D.sorun) { sorunGoster(D.sorun.ileti); return; }
    goster("bilesenler-panel", false);
    ileti("bilesenler-ileti", "");
    ileti("bilesenler-basari", "");
    metin("bilesenler-hazir-neden", D.hazir_degil_nedeni);
    goster("bilesenler-hazir-degil", !D.uygulama_hazir);

    const yetimler = D.yetimler || [];
    goster("bilesenler-yetim", yetimler.length > 0);
    metin("bilesenler-yetim-metin", `Önceki oturumdan kalan ${yetimler.length} süreç çalışıyor.`);
    $("bilesenler-yetim-listesi").replaceChildren(...yetimler.map((y) => el("li", null, `${y.pid}: ${y.komut}`)));

    const govde = $("bilesenler-tablo");
    govde.replaceChildren();
    for (const b of D.bilesenler.filter((x) => x.zorunluluk === "zorunlu" && x.gorunur)) {
      govde.append(tabloSatiri(b.ad, b, zorunluIslemleri(b)));
    }
    const etkin = bilesen(D.tex_etkin);
    if (etkin) {
      govde.append(tabloSatiri("TeX: " + etkin.ad, etkin, [
        ...zorunluIslemleri(etkin),
        islemDugmesi("Türünü değiştir…", () => { S.secim.tex = D.tex_etkin; texPaneliCiz(); })]));
    } else {
      govde.append(el("tr", null, el("td", { class: "ad" }, "TeX"),
        el("td", { class: "durum-eksik" }, "kurulu değil"), el("td", null, "—"),
        el("td", { class: "sayi" }, "—"),
        el("td", { class: "islem" }, islemDugmesi("Seç…", () => { S.secim.tex = texVarsayilani(); texPaneliCiz(); }))));
    }
    for (const b of texUyeleri().filter((x) => x.kimlik !== D.tex_etkin && x.durum !== "eksik")) {
      govde.append(tabloSatiri("TeX: " + b.ad, b,
        b.kaldirilabilir ? [islemDugmesi("Kaldır", () => kaldir(b), { sinif: "ikincil tehlike" })] : []));
    }
    for (const b of D.bilesenler.filter((x) => x.zorunluluk === "istege_bagli" && x.gorunur)) {
      const satir = tabloSatiri(b.ad, b, istegeIslemleri(b));
      if (b.durum === "eksik" && !b.secilebilir && b.neden) {
        satir.firstElementChild.append(el("div", { class: "kucuk hata-metin" }, b.neden));
      }
      govde.append(satir);
    }
    metin("kaynak-kodu", D.kaynak_kodu_metni);
    metin("bilesenler-surum", `Sürüm ${D.surum}${D.yapi ? " (" + D.yapi + ")" : ""} · Kurulum yeri: ${D.kok_metni}`);
    goster("bilesenler-ac", D.uygulama_hazir);
    ekranGoster("bilesenler");
  }

  // ------------------------------------------------------------------
  // G.5 Kitapçevir'i kaldır…
  // ------------------------------------------------------------------
  async function kaldirmaPaneli() {
    let p;
    try {
      p = await api("/api/kaldirma_plani");
    } catch (e) {
      ileti("bilesenler-ileti", e.message);
      return;
    }
    const panel = $("bilesenler-panel");
    const liste = el("div", { class: "liste" }, p.ogeler.map((o) => el("div", { class: "satir" },
      el("div", { class: "govde yol" }, o.yol_metni),
      el("div", { class: "boyutlar" }, el("span", { class: "boyut" }, o.metin)))));
    const veriKutusu = el("input", { type: "checkbox", id: "kaldir-veri" });
    // K9 O1: veri/ ile korunan sihirbaz bileşenleri (HY-MT) adıyla ve gerçek
    // boyutuyla söylenir; her biri için ayrı "de sil" kutusu (varsayılan: korunur).
    const veride = p.veride_bilesenler || [];
    const bilesenKutulari = veride.map((b) => ({ b, kutu: el("input", { type: "checkbox" }) }));
    veriKutusu.addEventListener("change", () => {
      for (const { kutu } of bilesenKutulari) {
        kutu.disabled = veriKutusu.checked;
        if (veriKutusu.checked) kutu.checked = true;
      }
    });
    const kaldirDugmesi = el("button", { type: "button", class: "ikincil tehlike",
      disabled: p.izin ? null : true,
      onclick: () => kaldirmaOnayi(veriKutusu.checked,
        bilesenKutulari.filter((x) => x.kutu.checked).map((x) => x.b), p) },
      "Kaldır");
    doldur(panel,
      el("h2", null, `${uygulamaAdi()}'i kaldır`),
      el("p", null, `Silinecekler (toplam ${p.silinecek_metni}):`),
      liste,
      p.veri.var ? el("p", null, "Korunanlar: kitapların, çeviri belleğin ve modeller klasörü " +
        `(${p.veri.yol_metni}, ${p.veri.metin}).`) : null,
      veride.length ? el("p", { class: "uyari-metin" }, "Bu klasörde sihirbazın kurduğu şu " +
        "bileşenler de duruyor: " + veride.map((b) => `${b.ad} (${b.metin}, ${b.yol_metni})`)
          .join("; ") + ". İşaretlemezsen bunlar da korunur.") : null,
      bilesenKutulari.map(({ b, kutu }) => el("label", { class: "onay-kutusu" }, kutu,
        el("span", null, `Şunu da sil: ${b.ad} (${b.metin})`))),
      p.veri.var ? el("label", { class: "onay-kutusu" }, veriKutusu,
        el("span", null, `Kitaplarımı, belleği ve modeller klasörünün tamamını da sil (${p.veri.metin})`))
        : null,
      p.mactex ? el("p", { class: "soluk" },
        "MacTeX sistem geneline kurulu olduğu için kaldırılmaz.") : null,
      p.izin ? null : el("p", { class: "kutu uyari" }, p.neden),
      el("div", { class: "dugmeler" },
        el("button", { type: "button", class: "ikincil", onclick: () => goster("bilesenler-panel", false) }, "Vazgeç"),
        kaldirDugmesi));
    panel.hidden = false;
    panel.scrollIntoView({ block: "nearest" });
  }

  async function kaldirmaOnayi(veriDeSil, silinecekBilesenler, p) {
    const veride = p.veride_bilesenler || [];
    const kalanlar = veride.filter((b) => !silinecekBilesenler.includes(b));
    let aciklama;
    if (veriDeSil) {
      aciklama = `${p.silinecek_metni} ve veri klasörü (${p.veri.metin}; kitapların ve modeller ` +
        "dahil) silinecek. Bu geri alınamaz.";
    } else {
      aciklama = `${p.silinecek_metni} silinecek.`;
      if (silinecekBilesenler.length) {
        aciklama += " Ayrıca silinecek: " +
          silinecekBilesenler.map((b) => `${b.ad} (${b.metin})`).join(", ") + ".";
      }
      aciklama += " Kitapların ve çeviri belleğin korunur" + (kalanlar.length
        ? "; şunlar da korunur: " + kalanlar.map((b) => `${b.ad} (${b.metin})`).join(", ") : "") + ".";
    }
    const tamam = await onayIste(`${uygulamaAdi()} kaldırılsın mı?`, aciklama, "Kaldır", true);
    if (!tamam) return;
    let y;
    try {
      y = await api("/api/tumunu_kaldir", { onay: true, veri_de_sil: veriDeSil,
        bilesenleri_de_sil: veriDeSil ? [] : silinecekBilesenler.map((b) => b.kimlik) });
    } catch (e) {
      ileti("bilesenler-ileti", e.message);
      return;
    }
    metin("kaldirildi-silinen", `${y.silinen_metni} silindi.`);
    const korunan = (y.veride_kalanlar || []).map((b) => `${b.ad} (${b.metin})`);
    metin("kaldirildi-veri", y.veri_korundu
      ? `Kitapların ve çeviri belleğin korundu: ${y.veri_yolu_metni}.` + (korunan.length
        ? ` Bu klasörde şunlar da duruyor: ${korunan.join(", ")}; gerekmiyorsa elle silebilirsin.`
        : "")
      : "");
    ileti("kaldirildi-silinmeyen", y.silinmeyen.length
      ? "Silinemeyenler (elle silebilirsin): " + y.silinmeyen.join(", ") : "");
    goster("kaldirildi-mactex", !!p.mactex);
    ekranGoster("kaldirildi");
  }

  async function guncellemeDenetle() {
    const dugme = $("guncelleme-denetle");
    dugme.disabled = true;
    metin("guncelleme-sonuc", "denetleniyor…");
    goster("guncelleme-sayfa", false);
    try {
      const y = await api("/api/guncelleme", {});
      metin("guncelleme-sonuc", y.ileti + (y.baslik && y.baslik !== y.surum ? ` (${y.baslik})` : ""));
      goster("guncelleme-sayfa", y.durum === "yeni");
    } catch (e) {
      metin("guncelleme-sonuc", e.message);
    }
    dugme.disabled = false;
  }

  async function yetimleriDurdur() {
    const yetimler = (S.D && S.D.yetimler) || [];
    const tamam = await onayIste("Süreçler durdurulsun mu?",
      `Önceki oturumdan kalan ${yetimler.length} süreç durdurulacak. Yarım kalan çeviri sonra kaldığı yerden sürdürülebilir.`,
      "Durdur", true);
    if (!tamam) return;
    try {
      await api("/api/yetimler", { pidler: yetimler.map((y) => y.pid) });
    } catch (e) {
      ileti("bilesenler-ileti", e.message);
    }
    await bilesenlerGoster();
  }

  // ------------------------------------------------------------------
  // Bağlamalar ve açılış
  // ------------------------------------------------------------------
  function baglamalar() {
    $("hosgeldin-devam").addEventListener("click", lisansGoster);
    $("lisans-kutu").addEventListener("change", () => {
      $("lisans-devam").disabled = !$("lisans-kutu").checked;
      if ($("lisans-kutu").checked) goster("lisans-red", false);
    });
    $("lisans-devam").addEventListener("click", lisansDevam);
    $("lisans-reddet").addEventListener("click", lisansReddet);
    $("lisans-geri").addEventListener("click", hosgeldinGoster);
    $("lisans-tam-metin").addEventListener("click",
      () => ac("lisans_tam_metin", { bilesen: S.lisansBileseni }));
    $("lisans-hepsi").addEventListener("click", () => ac("lisanslar"));
    $("secim-geri").addEventListener("click", () => (zorunluLisansBileseni() ? lisansGoster() : hosgeldinGoster()));
    $("secim-devam").addEventListener("click", () => planGoster(
      { tex: S.secim.tex, istege_bagli: [...S.secim.istege], onar: [] }, "secim", "secim-ileti"));
    $("ozet-geri").addEventListener("click", geriDon);
    $("ozet-kur").addEventListener("click", kurBaslat);
    $("ozet-internet-yenile").addEventListener("click", async () => {
      try { await durumYenile("internet=yenile"); } catch (e) { genelIleti(e.message); }
      if (S.sonSecim) await planGoster(S.sonSecim, S.donus);
    });
    $("kurulum-duraklat").addEventListener("click", () => durdur(false));
    $("kurulum-iptal").addEventListener("click", () => durdur(true));
    $("kurulum-surdur").addEventListener("click", surdur);
    $("kurulum-geri").addEventListener("click", geriDon);
    $("kurulum-gunluk-goster").addEventListener("click", () => ac("gunluk"));
    $("bitti-ac").addEventListener("click", uygulamayiAc);
    $("bitti-bilesenler").addEventListener("click", bilesenlerGoster);
    $("bilesenler-ac").addEventListener("click", uygulamayiAc);
    $("bilesenler-tamamla").addEventListener("click", eksikleriKur);
    $("bilesenler-yetim-durdur").addEventListener("click", yetimleriDurdur);
    $("guncelleme-denetle").addEventListener("click", guncellemeDenetle);
    $("guncelleme-sayfa").addEventListener("click", () => ac("surumler"));
    $("kaynak-kodu").addEventListener("click", () => ac("kaynak_kodu"));
    $("bilesenler-lisanslar").addEventListener("click", () => ac("lisanslar"));
    $("bilesenler-gunlukler").addEventListener("click", () => ac("gunlukler"));
    $("kaldir-hepsi").addEventListener("click", kaldirmaPaneli);
    $("acilis-bilesenler").addEventListener("click", () => { S.ayriliyor = false; bilesenlerGoster(); });
    $("sorun-gunluk-goster").addEventListener("click", () => ac("gunlukler"));
    document.addEventListener("keydown", (olay) => {
      if (olay.key === "Escape" && !$("pencere").hidden) pencereKapat();
    });
  }

  async function basla() {
    baglamalar();
    setInterval(saatTik, 1000);
    try {
      await durumYenile();
    } catch (e) {
      sorunGoster(e.message);
      return;
    }
    olayAkisi();
    const D = S.D;
    if (D.kaldirildi) { ekranGoster("kaldirildi"); return; }
    if (D.sorun) { sorunGoster(D.sorun.ileti); return; }
    if (D.is.calisiyor) {
      S.donus = D.kip_onerisi === "bilesenler" ? "bilesenler" : "secim";
      try { S.is = await api("/api/is"); } catch (e) { /* olay akışı özet getirir */ }
      kurulumCiz();
      ekranGoster("kurulum");
      return;
    }
    if (KIP === "bilesenler") { await bilesenlerGoster(); return; }
    if (D.kip_onerisi === "bilesenler") {
      // Önceki oturumdan kalan süreç varsa önce kullanıcıya gösterilir (B.6-4).
      if (D.uygulama_hazir && !(D.yetimler || []).length) await uygulamayiAc();
      else await bilesenlerGoster();
      return;
    }
    hosgeldinGoster();
  }

  // Yalnız sayaç biçiminin yöneticininkiyle aynı olduğunu denetlemek için.
  window.KitapcevirSihirbaz = Object.freeze({ sureMetni });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", basla);
  else basla();
})();
