"""Nöbetçi — uzun süren işleri DIŞARIDAN izleyen, sessiz ölümü yakalayan, teşhis koyan ve
geri alınabilir durumlarda kendi başına toparlayan küçük bir ajan sistemi.

Üç rol, hepsi dosya üzerinden konuşur (birbirine mesaj atmaz):
  gözcü   → işin "çalışıyor mu" değil "ilerliyor mu" olduğunu ölçer (süreç, log, çıktı dosyası)
  teşhis  → ölüm/askıda kalma anında log + sistem durumundan sebep çıkarır; her iddia logdan bir satırla kanıtlanır
  şef     → kurala göre karar verir: yeniden başlat (geri alınabilirse, hak varsa) ya da insana sor

Kullanım:
  python nobetci.py calistir --cikti sonuc.json -- python deney.py   # tek komut, ayar dosyası yok
  python nobetci.py izle gorevler.json        # birden fazla görevi ayar dosyasından başlatır ve izler
  python nobetci.py durum  durum/             # nöbetçinin kendisi yaşıyor mu (dışarıdan nabız kontrolü)
  python nobetci.py nabiz  .nobetci           # aynı kontrol + alarmda Telegram (Görev Zamanlayıcı her N dakikada çağırır)
  python nobetci.py zamanla .nobetci          # Görev Zamanlayıcı komutlarını yalnız EKRANA yazar, kayıt yapmaz
  python nobetci.py panel  .nobetci           # son işleri tek dosyalık HTML panele döker
Bağımlılık yok (stdlib). Ollama varsa teşhis özetini yerel LLM yazar; yoksa kural tabanlı teşhis yeter.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- durumlar
CALISIYOR = "CALISIYOR"
BITTI = "BITTI"                      # çıkış 0 ve beklenen çıktı var
YESIL_AMA_OLU = "YESIL_AMA_OLU"      # çıkış 0 ama beklenen çıktı yok (en sinsi arıza)
OLDU = "OLDU"                        # sıfırdan farklı çıkış kodu
ASKIDA = "ASKIDA"                    # süreç yaşıyor ama log/çıktı N dakikadır kıpırdamıyor


@dataclass
class Gorev:
    ad: str
    komut: list[str]
    log: str
    cikti: str | None = None              # varsa: iş ancak bu dosya oluşunca "bitti" sayılır
    cwd: str = "."
    sessizlik_sn: int = 600               # bu kadar süre log/çıktı değişmezse ASKIDA
    geri_alinabilir: bool = False         # True ise şef insana sormadan yeniden başlatabilir
    yeniden_hak: int = 2
    devam_bayragi: list[str] = field(default_factory=list)  # yeniden başlatırken eklenecek argümanlar (ör. --devam)
    env: dict = field(default_factory=dict)  # göreve özel ortam değişkenleri


# ---------------------------------------------------------------- gözcü
def ilerleme_izi(g: Gorev) -> float:
    """İşin dışarıdan görülebilen son ilerleme anı: log ya da çıktı dosyasının en yeni mtime'ı."""
    t = 0.0
    for p in (g.log, g.cikti):
        if p and Path(p).exists():
            t = max(t, Path(p).stat().st_mtime)
    return t


def siniflandir(g: Gorev, kod: int | None, son_ilerleme: float, simdi: float) -> str:
    """Sonuç kontrolü: bileşenin 'yaşıyor' olması değil, işin yapılıp yapılmadığı."""
    cikti_var = bool(g.cikti) and Path(g.cikti).exists() and Path(g.cikti).stat().st_size > 0
    if kod is None:                                   # süreç hâlâ ayakta
        return ASKIDA if simdi - son_ilerleme > g.sessizlik_sn else CALISIYOR
    if kod != 0:
        return OLDU
    if g.cikti and not cikti_var:
        return YESIL_AMA_OLU
    return BITTI


def dis_gozlem(g: Gorev, simdi: float | None = None) -> str:
    """Süreci biz başlatmadıysak (başka bir terminalde koşan iş): yalnız dosyalara bakarak karar.
    Ders: 'log sustu' tek başına ölüm değildir — çıktı yazıldıysa iş bitmiştir."""
    simdi = simdi or time.time()
    cikti_var = bool(g.cikti) and Path(g.cikti).exists() and Path(g.cikti).stat().st_size > 0
    if cikti_var:
        return BITTI
    return ASKIDA if simdi - ilerleme_izi(g) > g.sessizlik_sn else CALISIYOR


# ---------------------------------------------------------------- teşhis
# (desen, sebep, geçici_mi) — geçici olanlar yeniden denemeyle düzelebilir
IMZALAR = [
    (r"CUDA out of memory|CUDA error: out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED", "GPU belleği yetmedi", True),
    (r"CUDA error|device-side assert", "CUDA hatası", True),
    # Ollama'nın bilinen bellek hataları (topluluk issue'larında en sık görülenler)
    (r"model requires more system memory|llama runner process has terminated", "Model sunucusu bellek yüzünden çöktü", True),
    (r"MemoryError|Cannot allocate memory|std::bad_alloc", "RAM yetmedi", True),
    (r"HTTP Error 500|status code 500|Internal Server Error", "Model sunucusu 500 verdi (çoğunlukla bellek baskısı)", True),
    (r"Connection refused|ConnectionResetError|URLError|timed out|TimeoutError", "Ağ/servis bağlantısı koptu", True),
    (r"No space left on device", "Disk doldu", False),
    (r"FileNotFoundError|No such file or directory", "Dosya bulunamadı", False),
    (r"ModuleNotFoundError|ImportError", "Eksik paket", False),
    (r"denemede geçerli çıktı yok|no valid output after", "Model çıktısı doğrulamayı geçemedi (prompt/kural düzeltmesi gerekir)", False),
    (r"Traceback \(most recent call last\)", "Python istisnası", False),
]


def sistem_durumu() -> dict:
    d: dict = {"disk_bos_gb": round(shutil.disk_usage(".").free / 2**30, 1)}
    try:
        if os.name == "nt":
            o = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"],
                               capture_output=True, text=True, timeout=15).stdout.strip()
            d["ram_bos_gb"] = round(int(o) / 2**20, 1)
        else:
            for s in Path("/proc/meminfo").read_text().splitlines():
                if s.startswith("MemAvailable"):
                    d["ram_bos_gb"] = round(int(s.split()[1]) / 2**20, 1)
    except Exception:
        pass
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=15).stdout.strip()
        u, t = (int(x) for x in o.split(",")[:2])
        d["gpu_mb"] = f"{u}/{t}"
    except Exception:
        pass
    return d


def log_kuyrugu(g: Gorev, n: int = 60) -> list[str]:
    p = Path(g.log)
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


def teshis(g: Gorev, durum: str, llm_model: str | None = None) -> dict:
    satirlar = log_kuyrugu(g)
    bulgular = []
    for desen, sebep, gecici in IMZALAR:
        for s in reversed(satirlar):
            if re.search(desen, s):
                bulgular.append({"sebep": sebep, "gecici": gecici, "kanit": s.strip()[:300]})
                break
    if durum == ASKIDA and not bulgular:
        bulgular.append({"sebep": f"{g.sessizlik_sn} sn boyunca hiçbir ilerleme yok (kilitlenme/sonsuz bekleme)",
                         "gecici": True, "kanit": (satirlar[-1].strip()[:300] if satirlar else "(log boş)")})
    # "Python istisnası" genel bir imzadır: her hatanın arkasında Traceback olur. Daha belirgin bir sebep
    # bulunduysa onu sebep saymak teşhisi bozar (08.11: 500 hatası bu yüzden 'kalıcı' sanıldı).
    if len(bulgular) > 1:
        bulgular = [b for b in bulgular if b["sebep"] != "Python istisnası"]
    if durum == OLDU and not bulgular:
        # tanınmayan ölüm: "kalıcı" diye uydurma; bilinmediğini söyle ve son satırı kanıt olarak ver
        bulgular.append({"sebep": "Tanınmayan sebep (imza listesinde yok) — son log satırına bak", "gecici": False,
                         "kanit": (satirlar[-1].strip()[:300] if satirlar else "(log boş)")})
    if durum == YESIL_AMA_OLU:
        bulgular.insert(0, {"sebep": "Süreç başarıyla çıktı ama beklenen çıktı dosyası yok", "gecici": False,
                            "kanit": f"beklenen: {g.cikti}"})
    t = {"durum": durum, "bulgular": bulgular, "sistem": sistem_durumu(), "son_satirlar": satirlar[-8:]}
    if llm_model and bulgular:
        t["ozet"] = llm_ozet(llm_model, t)
    return t


def llm_ozet(model: str, t: dict) -> str | None:
    """Yerel LLM'den iki cümlelik insan dili özet. Kanıt disiplini: özet, logda olmayan bir hatadan
    bahsederse (tırnak içindeki alıntı logda yoksa) reddedilir."""
    istem = ("You are a failure analyst. In 2 short Turkish sentences explain the most likely cause and the next step. "
             "Quote the decisive log line in double quotes exactly as it appears.\n" + json.dumps(t, ensure_ascii=False)[:4000])
    try:
        r = urllib.request.Request("http://localhost:11434/api/generate",
                                   data=json.dumps({"model": model, "prompt": istem, "stream": False, "keep_alive": 0}).encode(),
                                   headers={"Content-Type": "application/json"})
        cevap = json.loads(urllib.request.urlopen(r, timeout=180).read())["response"].strip()
    except Exception:
        return None
    log_metni = "\n".join(t["son_satirlar"] + [b["kanit"] for b in t["bulgular"]])
    return cevap if alinti_gecerli(cevap, log_metni) else None  # uydurma → kural tabanlı teşhis geçerli


def alinti_gecerli(cevap: str, log_metni: str) -> bool:
    """Özet en az bir alıntı içermeli ve tüm alıntılar logda birebir geçmeli."""
    alintilar = [a.strip() for a in re.findall(r'"([^"]{8,})"', cevap)]
    return bool(alintilar) and all(a in log_metni for a in alintilar)


# ---------------------------------------------------------------- şef
def sef_karari(g: Gorev, t: dict, kullanilan_hak: int) -> dict:
    if t["durum"] in (CALISIYOR, BITTI):
        return {"karar": "devam" if t["durum"] == CALISIYOR else "kapat", "gerekce": t["durum"]}
    gecici = bool(t["bulgular"]) and all(b["gecici"] for b in t["bulgular"])
    if not g.geri_alinabilir:
        return {"karar": "insana_sor", "gerekce": "Görev geri alınabilir işaretlenmemiş; otomatik müdahale yok."}
    if not gecici:
        return {"karar": "insana_sor", "gerekce": "Sebep kalıcı görünüyor; yeniden denemek aynı hatayı verir."}
    if kullanilan_hak >= g.yeniden_hak:
        return {"karar": "insana_sor", "gerekce": f"{g.yeniden_hak} yeniden deneme hakkı bitti."}
    return {"karar": "yeniden_baslat", "gerekce": "Geçici sebep, geri alınabilir iş, hak var."}


# ---------------------------------------------------------------- çare kataloğu (bilinen hatayı kendi düzeltme)
# Kural: yalnızca imza eşleşmesi kesinse, çare geri alınabilirse ve sonrasında doğrulanabiliyorsa otomatik.
# LLM burada karar vermez; çareler önceden yazılmış, sınırlı eylemlerdir. Her uygulama rapora düşer.
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def _ollama(yol: str, veri: dict | None = None, timeout: int = 30):
    r = urllib.request.Request(OLLAMA + yol, data=json.dumps(veri).encode() if veri is not None else None,
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read() or b"{}")


def ollama_bosalt() -> str:
    """Bellekteki modelleri boşaltır (keep_alive=0). Geri alınabilir: model bir sonraki istekte yeniden yüklenir."""
    yuklu = [m["name"] for m in _ollama("/api/ps").get("models", [])]
    for m in yuklu:
        _ollama("/api/generate", {"model": m, "keep_alive": 0}, timeout=60)
    return f"{len(yuklu)} model bellekten çıkarıldı: {', '.join(yuklu) or '-'}"


def ollama_bos_mu() -> bool:
    return not _ollama("/api/ps").get("models")


def ollama_ayakta_mi() -> bool:
    try:
        _ollama("/api/tags", timeout=10)
        return True
    except Exception:
        return False


def bekle_ve_bak(sn: int = 30):
    def eylem() -> str:
        time.sleep(sn)
        return f"{sn} sn beklendi"
    return eylem


# sebep → (eylem, doğrulama, açıklama). Testler bu sözlüğü sahte fonksiyonlarla değiştirebilir.
CARELER: dict = {
    "Model sunucusu 500 verdi (çoğunlukla bellek baskısı)": (ollama_bosalt, ollama_bos_mu, "Bellekteki modelleri boşalt"),
    "Model sunucusu bellek yüzünden çöktü": (ollama_bosalt, ollama_bos_mu, "Bellekteki modelleri boşalt"),
    "RAM yetmedi": (ollama_bosalt, ollama_bos_mu, "Model sunucusunun tuttuğu RAM'i boşalt"),
    "GPU belleği yetmedi": (ollama_bosalt, ollama_bos_mu, "Model sunucusunun tuttuğu GPU belleğini boşalt"),
    "Ağ/servis bağlantısı koptu": (bekle_ve_bak(30), ollama_ayakta_mi, "Bekle, servis geri geldi mi bak"),
}


def care_uygula(t: dict) -> dict | None:
    """Teşhisteki ilk bulguya uyan çareyi uygular ve doğrular. Uyan çare yoksa None."""
    for b in t.get("bulgular", []):
        if b["sebep"] in CARELER:
            eylem, dogrula, aciklama = CARELER[b["sebep"]]
            try:
                ne = eylem()
                ok = bool(dogrula())
            except Exception as e:  # çarenin kendisi patlarsa: düzeltilmiş sayılmaz
                ne, ok = f"çare uygulanamadı: {type(e).__name__}: {e}", False
            return {"sebep": b["sebep"], "care": aciklama, "yapilan": ne, "dogrulandi": ok}
    return None


# ---------------------------------------------------------------- bildirim (Telegram)
# Anahtar depoda DEĞİL: ortam değişkeninde ya da kullanıcı klasöründeki ~/.nobetci/telegram.json dosyasında durur.
TELEGRAM = "https://api.telegram.org/bot{token}/{yontem}"


def bildirim_dosyasi() -> Path:
    return Path(os.environ.get("NOBETCI_AYAR") or Path.home() / ".nobetci" / "telegram.json")


def bildirim_ayari() -> dict | None:
    """Token ve sohbet numarası ikisi de varsa döner; yoksa None (bildirim kapalı, hata değil)."""
    a = {"token": os.environ.get("NOBETCI_TELEGRAM_TOKEN", ""), "chat_id": os.environ.get("NOBETCI_TELEGRAM_CHAT", "")}
    if not (a["token"] and a["chat_id"]):
        try:
            d = json.loads(bildirim_dosyasi().read_text(encoding="utf-8-sig"))
            a = {"token": str(d.get("token", "")).strip(), "chat_id": str(d.get("chat_id", "")).strip()}
        except Exception:
            return None
    return a if a["token"] and a["chat_id"] else None


def _telegram(token: str, yontem: str, veri: dict | None = None) -> dict:
    r = urllib.request.Request(TELEGRAM.format(token=token, yontem=yontem),
                               data=json.dumps(veri or {}).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=20).read())


def bildir(metin: str) -> bool:
    """Telegram'a mesaj atar. Ayar yoksa ya da gönderilemezse False döner; izlemeyi asla durdurmaz."""
    a = bildirim_ayari()
    if not a:
        return False
    try:
        return bool(_telegram(a["token"], "sendMessage", {"chat_id": a["chat_id"], "text": metin[:3500]}).get("ok"))
    except Exception:
        return False


def bildirim_metni(olay: dict) -> str:
    s = [f"🔔 Nöbetçi: {olay['gorev']} → {olay['durum']}", olay.get("gerekce", "")]
    for b in olay.get("teshis", {}).get("bulgular", [])[:3]:
        s.append(f"• {b['sebep']}\n  kanıt: {b['kanit'][:200]}")
    return "\n".join(x for x in s if x)


def telegram_kur() -> int:
    """Kurulum yardımcısı: dosyadaki token'la sohbet numarasını bulur, dosyaya yazar, deneme mesajı atar.
    Token'ı ekrana ASLA basmaz."""
    p = bildirim_dosyasi()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{\n  "token": "",\n  "chat_id": ""\n}\n', encoding="utf-8")
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        print(f"Ayar dosyası okunamadı (JSON bozuk): {p}")
        return 2
    token = str(d.get("token", "")).strip()
    if not token:
        print(f"Token boş. BotFather'dan aldığın token'ı şu dosyada \"token\" tırnaklarının içine yapıştır:\n  {p}")
        return 2
    if not str(d.get("chat_id", "")).strip():
        try:
            mesajlar = [u["message"] for u in _telegram(token, "getUpdates").get("result", []) if "message" in u]
        except Exception as e:
            print(f"Telegram'a ulaşılamadı ya da token geçersiz ({type(e).__name__}).")
            return 1
        if not mesajlar:
            print("Bot henüz mesaj almamış. Telegram'da botuna herhangi bir şey yaz (ör. 'selam'), sonra yeniden dene.")
            return 1
        d["chat_id"] = str(mesajlar[-1]["chat"]["id"])
        p.write_text(json.dumps({"token": token, "chat_id": d["chat_id"]}, indent=2) + "\n", encoding="utf-8")
        print("Sohbet numarası bulundu ve dosyaya yazıldı.")
    if bildir("✅ Nöbetçi bağlandı. Bir iş 'insana sor' derse haber buradan gelecek."):
        print("Deneme mesajı gönderildi; telefonuna bak.")
        return 0
    print("Deneme mesajı gönderilemedi (token ya da sohbet numarası yanlış olabilir).")
    return 1


# ---------------------------------------------------------------- olay defteri + nabız
class Defter:
    def __init__(self, klasor: str):
        self.k = Path(klasor)
        self.k.mkdir(parents=True, exist_ok=True)

    def yaz(self, olay: dict):
        olay = {"zaman": time.strftime("%Y-%m-%d %H:%M:%S"), **olay}
        if olay.get("karar") == "insana_sor":   # haber verildi mi, deftere de yazılır
            olay["bildirim"] = "gitti" if bildir(bildirim_metni(olay)) else ("gitmedi" if bildirim_ayari() else "kapali")
        with (self.k / "olaylar.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(olay, ensure_ascii=False) + "\n")
        if olay.get("karar") == "insana_sor":
            with (self.k / "INSANA_SOR.md").open("a", encoding="utf-8") as f:
                f.write(f"\n## {olay['zaman']} · {olay['gorev']} · {olay['durum']}\n{olay['gerekce']}\n")
                for b in olay.get("teshis", {}).get("bulgular", []):
                    f.write(f"- **{b['sebep']}** — kanıt: `{b['kanit']}`\n")
                if olay.get("teshis", {}).get("ozet"):
                    f.write(f"- Özet: {olay['teshis']['ozet']}\n")
        d = olay.get("otomatik_duzeltme")
        if d:  # kendi düzelttiği her şeyi raporlar; insana sormaz ama saklamaz da
            with (self.k / "KENDIM_DUZELTTIM.md").open("a", encoding="utf-8") as f:
                f.write(f"\n## {olay['zaman']} · {olay['gorev']}\n- Sebep: **{d['sebep']}**\n- Çare: {d['care']} → {d['yapilan']}\n"
                        f"- Doğrulandı: {'evet, iş yeniden başlatıldı' if d['dogrulandi'] else 'HAYIR, insana soruldu'}\n")
        print(f"[{olay['zaman']}] {olay['gorev']}: {olay['durum']} → {olay.get('karar', '')}"
              + (f" (çare: {d['care']}, doğrulandı={d['dogrulandi']})" if d else ""), flush=True)

    def nabiz(self):
        (self.k / "nabiz").write_text(str(time.time()))


def nabiz_bak(klasor: str, esik_sn: int = 180) -> tuple[int, str]:
    """Nabız dosyasına bakar. Dönüş: (0 tamam / 1 nabız eski / 2 nabız dosyası yok, açıklama).
    Nabız eski ama defterdeki her iş kapanmışsa alarm yok: nöbetçi ölmedi, nöbeti bitti (kayıt sustu ≠ ölüm)."""
    p = Path(klasor) / "nabiz"
    if not p.exists():
        return 2, "ALARM: nabız dosyası yok — nöbetçi hiç çalışmamış ya da yanlış klasör"
    try:
        yas = time.time() - float(p.read_text())
    except ValueError:
        return 2, "ALARM: nabız dosyası okunamadı — nöbetçi yazarken kesilmiş olabilir"
    if yas > esik_sn:
        isler = isleri_cikar(olaylari_oku(Path(klasor) / "olaylar.jsonl")[0])
        if isler and all(i["bitis"] for i in isler):
            return 0, f"tamam: nöbet bitti, açık iş yok (son nabız {int(yas)} sn önce)"
        return 1, f"ALARM: nöbetçi {int(yas)} sn'dir nabız atmıyor — nöbetçinin kendisi ölü"
    return 0, f"tamam: son nabız {int(yas)} sn önce"


def nabiz_kontrol(klasor: str, esik_sn: int = 180) -> int:
    """Nöbetçiyi nöbetçiden bağımsız kontrol eder (zamanlanmış görev / başka makine çağırır)."""
    kod, mesaj = nabiz_bak(klasor, esik_sn)
    print(mesaj)
    return kod


# ---------------------------------------------------------------- zamanlanmış nabız (alarmda Telegram)
NABIZ_TEKRAR_SN = 6 * 3600   # aynı alarm en erken bu kadar sonra yeniden hatırlatılır


def nabiz_tur(klasor: str, esik_sn: int = 180, tekrar_sn: int = NABIZ_TEKRAR_SN) -> int:
    """Tek tur nabız kontrolü. Alarm varsa Telegram'a haber verir, yoksa sessiz kalır.
    Aynı alarm her turda yeniden gitmez: son bildirilen durum klasördeki nabiz_bildirim.json'da durur;
    yalnız durum değişince (ya da tekrar_sn geçince) yeniden haber gider. Alarm düzelince tek bir 'düzeldi' gider.
    Gönderilemeyen haber kaydedilmez, yani bir sonraki turda yeniden denenir. Dönüş: nabiz_bak'ın kodu."""
    kod, mesaj = nabiz_bak(klasor, esik_sn)
    print(mesaj, flush=True)
    kayit = Path(klasor) / "nabiz_bildirim.json"
    try:
        son = json.loads(kayit.read_text(encoding="utf-8"))
    except Exception:
        son = {}
    simdi = time.time()
    ad = Path(klasor).resolve().name or str(klasor)
    if kod:
        if son.get("kod") == kod and simdi - float(son.get("zaman", 0)) < tekrar_sn:
            print("  haber: aynı alarm zaten bildirildi; tekrar gönderilmedi")
            return kod
        metin = f"🚨 Nöbetçi nabız alarmı ({ad})\n{mesaj}"
        if son.get("kod") == kod:
            metin += f"\n(hatırlatma: alarm {son.get('ilk', '?')} tarihinden beri sürüyor)"
    elif son.get("kod"):
        metin = f"✅ Nöbetçi nabzı düzeldi ({ad})\n{mesaj}"
    else:
        return 0                                       # alarm yok, bildirilmiş alarm da yok: sessiz
    if bildir(metin):
        ilk = son.get("ilk") if son.get("kod") == kod else time.strftime("%Y-%m-%d %H:%M:%S")
        kayit.parent.mkdir(parents=True, exist_ok=True)
        kayit.write_text(json.dumps({"kod": kod, "zaman": simdi, "ilk": ilk}, ensure_ascii=False), encoding="utf-8")
        print("  haber: Telegram'a gönderildi")
    elif bildirim_ayari() is None:
        print("  haber: Telegram ayarı yok, bildirim gönderilmedi (kurmak için: python nobetci.py telegram-kur)")
    else:
        print("  haber: Telegram'a gönderilemedi; bir sonraki turda yeniden denenecek")
    return kod


NABIZ_KULLANIM = """Kullanım: python nobetci.py nabiz <klasör> [seçenekler]
  --esik SN        nabız bu kadar saniyeden eskiyse alarm (varsayılan 180)
  --tekrar SN      aynı alarmı en erken bu kadar saniye sonra yeniden hatırlat (varsayılan 21600 = 6 saat)
  --aralik SN      verilirse kendi içinde döner, her SN saniyede bir bakar (verilmezse tek tur: Görev Zamanlayıcı için)
  --tur N          --aralik ile en fazla N tur (deneme için; verilmezse Ctrl+C'ye kadar)
Çıkış kodu: 0 = tamam, 1 = nabız eski, 2 = nabız dosyası yok, 3 = kullanım hatası.
Örnek: python nobetci.py nabiz .nobetci --aralik 300"""


def nabiz(argv: list[str]) -> int:
    import argparse
    if not argv or argv[0].startswith("-"):
        print(NABIZ_KULLANIM)
        return 3
    ap = argparse.ArgumentParser(prog="nobetci.py nabiz", add_help=False)
    ap.add_argument("klasor")
    ap.add_argument("--esik", type=int, default=180)
    ap.add_argument("--tekrar", type=int, default=NABIZ_TEKRAR_SN)
    ap.add_argument("--aralik", type=float, default=0)
    ap.add_argument("--tur", type=int, default=0)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        print(NABIZ_KULLANIM)
        return 3
    kod, n = 0, 0
    try:
        while True:
            kod = nabiz_tur(a.klasor, a.esik, a.tekrar)
            n += 1
            if not a.aralik or (a.tur and n >= a.tur):
                return kod
            time.sleep(a.aralik)
    except KeyboardInterrupt:
        print("Nabız takibi durduruldu.")
        return kod


def zamanla(argv: list[str]) -> int:
    """Görev Zamanlayıcı kaydı için komutları yalnız EKRANA YAZAR. Hiçbir şeyi çalıştırmaz, sisteme dokunmaz;
    kaydı kullanıcı kendi onayıyla yapar."""
    import argparse
    ap = argparse.ArgumentParser(prog="nobetci.py zamanla", add_help=False)
    ap.add_argument("klasor", nargs="?", default=".nobetci")
    ap.add_argument("--dakika", type=int, default=10)
    ap.add_argument("--esik", type=int, default=180)
    ap.add_argument("--ad")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        print("Kullanım: python nobetci.py zamanla [klasör] [--dakika N] [--esik SN] [--ad GÖREV_ADI]")
        return 3
    klasor = Path(a.klasor).resolve()
    betik = Path(__file__).resolve()
    py = Path(sys.executable)
    if os.name == "nt" and py.with_name("pythonw.exe").exists():
        py = py.with_name("pythonw.exe")               # her turda siyah pencere açılıp kapanmasın
    ad = a.ad or "Nobetci-nabiz-" + (re.sub(r"[^\w-]+", "-", klasor.name).strip("-") or "klasor")
    esik = f" --esik {a.esik}" if a.esik != 180 else ""
    tr = f'\\"{py}\\" \\"{betik}\\" nabiz \\"{klasor}\\"{esik}'
    kur = f'/Create /SC MINUTE /MO {a.dakika} /TN "{ad}" /TR "{tr}"'
    print("Görev Zamanlayıcı kaydı — HİÇBİR ŞEY KAYDEDİLMEDİ. Kurmak istersen aşağıdaki satırı kendin çalıştır.\n")
    print("Kurmak (cmd):")
    print(f"  schtasks {kur}")
    print("Kurmak (PowerShell; --% satırın geri kalanını olduğu gibi geçirir):")
    print(f"  schtasks --% {kur}")
    print(f'Hemen bir kez denemek:  schtasks /Run /TN "{ad}"')
    print(f'Kurulu mu bakmak:       schtasks /Query /TN "{ad}"')
    print(f'Kaldırmak:              schtasks /Delete /TN "{ad}" /F')
    if len(tr.replace('\\"', '"')) > 261:
        print("\nUyarı: /TR metni 261 karakteri aşıyor; schtasks kabul etmeyebilir. Projeyi daha kısa bir yola taşı.")
    print(f"\nNot: görev her {a.dakika} dakikada senin kullanıcınla, oturum açıkken çalışır; Telegram ayarını "
          f"{bildirim_dosyasi()} dosyasından okur. Alarm yoksa sessizdir.")
    if os.name != "nt":
        print(f"\nLinux/macOS (crontab -e):\n  */{a.dakika} * * * * {sys.executable} {betik} nabiz {klasor}{esik}")
    return 0


# ---------------------------------------------------------------- döngü
def baslat(g: Gorev, ek: list[str] | None = None) -> subprocess.Popen:
    Path(g.log).parent.mkdir(parents=True, exist_ok=True)
    with open(g.log, "a", encoding="utf-8") as f:  # çocuk süreç tanıtıcıyı devralır; bizdeki kopya kapanır
        return subprocess.Popen(g.komut + (ek or []), cwd=g.cwd, stdout=f, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **g.env})


def izle(gorevler: list[Gorev], defter: Defter, aralik: float = 15, llm_model: str | None = None,
         sonsuz: bool = False) -> dict[str, str]:
    surec = {g.ad: baslat(g) for g in gorevler}
    hak = {g.ad: 0 for g in gorevler}
    son = {g.ad: time.time() for g in gorevler}
    bitis: dict[str, str] = {}
    for g in gorevler:
        defter.yaz({"gorev": g.ad, "durum": "BASLADI", "karar": "", "komut": g.komut})
    while len(bitis) < len(gorevler) or sonsuz:
        defter.nabiz()
        for g in gorevler:
            if g.ad in bitis:
                continue
            simdi = time.time()
            iz = ilerleme_izi(g)
            if iz > son[g.ad]:
                son[g.ad] = iz
            p = surec[g.ad]
            durum = siniflandir(g, p.poll(), son[g.ad], simdi)
            if durum == CALISIYOR:
                continue
            if durum == ASKIDA:
                p.kill()
                p.wait()
            t = teshis(g, durum, llm_model) if durum != BITTI else {"durum": BITTI, "bulgular": []}
            k = sef_karari(g, t, hak[g.ad])
            duzeltme = None
            if k["karar"] == "yeniden_baslat":
                duzeltme = care_uygula(t)
                if duzeltme and not duzeltme["dogrulandi"]:
                    k = {"karar": "insana_sor", "gerekce": f"Bilinen çare ({duzeltme['care']}) uygulandı ama işe yaradığı doğrulanamadı."}
            defter.yaz({"gorev": g.ad, "durum": durum, **k, "teshis": t, **({"otomatik_duzeltme": duzeltme} if duzeltme else {})})
            if k["karar"] == "yeniden_baslat":
                hak[g.ad] += 1
                surec[g.ad] = baslat(g, g.devam_bayragi)
                son[g.ad] = time.time()
            else:
                bitis[g.ad] = durum
        if len(bitis) < len(gorevler):
            time.sleep(aralik)
    return bitis


def yukle(dosya: str) -> list[Gorev]:
    return [Gorev(**d) for d in json.loads(Path(dosya).read_text(encoding="utf-8"))["gorevler"]]


# ---------------------------------------------------------------- tek komut (ayar dosyası yazmadan)
_YORUMLAYICI = {"python", "python3", "py", "pythonw", "node", "npx", "npm", "bash", "sh", "powershell", "pwsh", "cmd", "uv"}


def ad_uret(komut: list[str]) -> str:
    """Komuttan okunur bir görev adı: 'python src/egit.py --x' → 'egit', 'ffmpeg -i a.mp4' → 'ffmpeg'."""
    ad = Path(komut[0]).stem
    if ad.lower() in _YORUMLAYICI:
        for parca in komut[1:]:
            if not parca.startswith("-") and re.search(r"\w", Path(parca).stem):
                ad = Path(parca).stem
                break
    return re.sub(r"[^\w-]+", "-", ad).strip("-")[:40] or "is"


CALISTIR_KULLANIM = """Kullanım: python nobetci.py calistir [seçenekler] -- <komut ...>
  --cikti DOSYA        iş ancak bu dosya oluşunca "bitti" sayılır (önerilir; yoksa yalnız çıkış koduna bakılır)
  --ad AD              görev adı (verilmezse komuttan türetilir)
  --sessizlik SN       bu kadar saniye hiçbir şey değişmezse "askıda" (varsayılan 600)
  --geri-alinabilir    geçici hatada sormadan yeniden başlatabilir
  --hak N              yeniden başlatma hakkı (varsayılan 2)
  --devam "ARGÜMAN"    yeniden başlatırken komuta eklenecek argüman (ör. "--devam")
  --klasor KLASOR      defter ve logların yazılacağı yer (varsayılan .nobetci)
  --aralik SN          kontrol aralığı (varsayılan 2)
  --llm MODEL          teşhis özetini yerel modelle yaz (Ollama)
Örnek: python nobetci.py calistir --cikti sonuc.json -- python deney.py --tur 5"""


def calistir(argv: list[str]) -> int:
    """Tek bir komutu nöbet altında çalıştırır. Dönüş: 0 = bitti, 1 = bitmedi (rapor yazıldı), 2 = kullanım hatası."""
    import argparse
    if "--" not in argv or not argv[argv.index("--") + 1:]:
        print(CALISTIR_KULLANIM)
        return 2
    i = argv.index("--")
    komut = list(argv[i + 1:])
    ap = argparse.ArgumentParser(prog="nobetci.py calistir", add_help=False)
    ap.add_argument("--cikti")
    ap.add_argument("--ad")
    ap.add_argument("--sessizlik", type=int, default=600)
    ap.add_argument("--geri-alinabilir", action="store_true")
    ap.add_argument("--hak", type=int, default=2)
    ap.add_argument("--devam", default="")
    ap.add_argument("--klasor", default=".nobetci")
    ap.add_argument("--aralik", type=float, default=2)
    ap.add_argument("--llm")
    a = ap.parse_args(argv[:i])

    # Windows'ta 'npm' gibi komutlar aslında npm.cmd'dir; tam yolu bulamazsak hiç başlatmadan söyle.
    yol = shutil.which(komut[0])
    if not yol and not Path(komut[0]).exists():
        print(f"Komut bulunamadı: {komut[0]} (yazım hatası olabilir ya da PATH'te değil). Hiçbir şey başlatılmadı.")
        return 2
    komut[0] = yol or komut[0]

    ad = a.ad or ad_uret(argv[i + 1:])
    klasor = Path(a.klasor)
    log = klasor / "loglar" / f"{ad}-{time.strftime('%Y%m%d-%H%M%S')}.log"   # her çalıştırmaya ayrı log: eski hata yeni teşhise karışmaz
    g = Gorev(ad=ad, komut=komut, log=str(log), cikti=a.cikti, sessizlik_sn=a.sessizlik,
              geri_alinabilir=a.geri_alinabilir, yeniden_hak=a.hak, devam_bayragi=a.devam.split())
    t0 = time.time()
    durum = izle([g], Defter(str(klasor)), a.aralik, a.llm)[ad]

    print(f"\nNöbetçi: {ad} → {durum} ({int(time.time() - t0)} sn)")
    print(f"  log:   {log}")
    if a.cikti:
        print(f"  çıktı: {a.cikti}" + ("" if durum == BITTI else "  (YOK)"))
    else:
        print("  not:   --cikti verilmedi; yalnız çıkış koduna bakıldı ('yeşil ama ölü' yakalanamaz)")
    if durum != BITTI:
        son = [json.loads(s) for s in (klasor / "olaylar.jsonl").read_text(encoding="utf-8").splitlines()][-1]
        print(f"  karar: {son.get('karar')} — {son.get('gerekce')}")
        for b in son.get("teshis", {}).get("bulgular", []):
            print(f"  sebep: {b['sebep']}\n  kanıt: {b['kanit']}")
        print(f"  rapor: {klasor / 'INSANA_SOR.md'}")
        if son.get("bildirim"):
            print(f"  haber: Telegram bildirimi {son['bildirim']}")
    return 0 if durum == BITTI else 1


# ---------------------------------------------------------------- panel (tek dosyalık statik HTML)
# Panele yalnız defterdeki karar bilgisi girer: kanıt satırı (log içeriği) girmez, komuttaki yollar dosya adına
# kısalır, token/şifre gibi argümanlar maskelenir. Telegram ayarı hiç okunmaz.
DURUM_ETIKET = {BITTI: "Bitti", YESIL_AMA_OLU: "Yeşil ama ölü", OLDU: "Öldü", ASKIDA: "Askıda", "YARIM": "Kayıt yarım"}
KARAR_ETIKET = {"kapat": "kapatıldı", "insana_sor": "insana soruldu", "yeniden_baslat": "yeniden başlatıldı"}
_GIZLI = re.compile(r"(?i)token|key|secret|passw|sifre|şifre|parola")
_ZAMAN = "%Y-%m-%d %H:%M:%S"


def olaylari_oku(dosya: Path) -> tuple[list[dict], int]:
    """olaylar.jsonl'ı okur. Bozuk/yarım satır paneli çökertmez: atlanır ve sayılır. Dönüş: (olaylar, bozuk)."""
    if not Path(dosya).exists():
        return [], 0
    olaylar, bozuk = [], 0
    for s in Path(dosya).read_text(encoding="utf-8", errors="replace").splitlines():
        if not s.strip():
            continue
        try:
            o = json.loads(s)
        except ValueError:
            bozuk += 1
            continue
        if isinstance(o, dict) and o.get("gorev"):
            olaylar.append(o)
        else:
            bozuk += 1
    return olaylar, bozuk


def _ilk_sebep(o: dict) -> str:
    t = o.get("teshis")
    bulgular = t.get("bulgular") if isinstance(t, dict) else None
    for b in bulgular if isinstance(bulgular, list) else []:
        if isinstance(b, dict) and b.get("sebep"):
            return str(b["sebep"])
    return ""


def isleri_cikar(olaylar: list[dict]) -> list[dict]:
    """Olayları işlere çevirir: BASLADI bir iş açar, 'yeniden_baslat' dışındaki ilk karar onu kapatır.
    Kapanmamış iş 'YARIM' kalır (hâlâ sürüyor ya da nöbetçi yarıda kesilmiş)."""
    isler: list[dict] = []
    acik: dict[str, dict] = {}
    for o in olaylar:
        ad = str(o.get("gorev"))
        if o.get("durum") == "BASLADI":
            acik[ad] = {"gorev": ad, "baslangic": o.get("zaman"), "bitis": None, "komut": o.get("komut"),
                        "durum": "YARIM", "karar": "", "yeniden": 0, "sebep": "", "bildirim": ""}
            isler.append(acik[ad])
            continue
        is_ = acik.get(ad)
        if is_ is None:                                  # başlangıcı defterde olmayan karar (eski kayıt)
            is_ = acik[ad] = {"gorev": ad, "baslangic": None, "bitis": None, "komut": None,
                              "durum": "YARIM", "karar": "", "yeniden": 0, "sebep": "", "bildirim": ""}
            isler.append(is_)
        if o.get("karar") == "yeniden_baslat":
            is_["yeniden"] += 1
            continue
        is_.update(durum=str(o.get("durum") or "?"), karar=str(o.get("karar") or ""), bitis=o.get("zaman") or "?",
                   sebep=_ilk_sebep(o), bildirim=str(o.get("bildirim") or ""))
        del acik[ad]
    return isler


def sure_metni(bas, bit) -> str:
    try:
        sn = int((datetime.strptime(str(bit), _ZAMAN) - datetime.strptime(str(bas), _ZAMAN)).total_seconds())
    except (TypeError, ValueError):
        return "—"
    if sn < 0:
        return "—"
    if sn < 60:
        return f"{sn} sn"
    if sn < 3600:
        return f"{sn // 60} dk {sn % 60} sn"
    return f"{sn // 3600} sa {sn % 3600 // 60} dk"


def kisa_komut(komut) -> str:
    """Komutu panele uygun hale getirir: yollar dosya adına iner (kişisel klasör adı sızmaz), gizli argüman maskelenir."""
    parcalar = komut if isinstance(komut, list) else str(komut or "").split()
    cikti, gizle = [], False
    for i, p in enumerate(map(str, parcalar)):
        if gizle:
            cikti.append("•••")
            gizle = False
            continue
        if p.startswith("-") and _GIZLI.search(p):
            if "=" in p:
                cikti.append(p.split("=", 1)[0] + "=•••")
            else:
                cikti.append(p)
                gizle = True
            continue
        if re.search(r"[\\/]", p) and " " not in p:
            p = re.split(r"[\\/]", p.rstrip("\\/"))[-1] or p
        if i == 0:
            p = re.sub(r"(?i)\.exe$", "", p)
        cikti.append(f'"{p}"' if " " in p else p)
    s = " ".join(cikti)
    return s if len(s) <= 160 else s[:159] + "…"


PANEL_CSS = """
:root{color-scheme:light dark;--zemin:#f5f6f8;--kart:#fff;--yazi:#1b1f24;--soluk:#5d6672;--cizgi:#e2e5e9;
--yesil:#1d7a3e;--yesil-z:#e2f3e7;--kirmizi:#b42318;--kirmizi-z:#fde7e4;--sari:#8a5a00;--sari-z:#fff1d1;
--mor:#6a3fc8;--mor-z:#eee8fd;--gri:#59626d;--gri-z:#eceef1}
@media (prefers-color-scheme:dark){:root{--zemin:#0f1216;--kart:#171b21;--yazi:#e6e9ee;--soluk:#9ba4af;--cizgi:#272d35;
--yesil:#62d38c;--yesil-z:#11301e;--kirmizi:#ff8b7c;--kirmizi-z:#3b1714;--sari:#f1c14f;--sari-z:#33270b;
--mor:#bba5ff;--mor-z:#251c3e;--gri:#a4adb8;--gri-z:#232830}}
*{box-sizing:border-box}
body{margin:0;background:var(--zemin);color:var(--yazi);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:28px 16px 40px}
h1{font-size:22px;margin:0 0 2px;letter-spacing:-.01em}
.alt{color:var(--soluk);font-size:13px;margin:0}
.ozet{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 20px}
.sayi{background:var(--kart);border:1px solid var(--cizgi);border-radius:10px;padding:8px 14px;min-width:104px}
.sayi b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.sayi span{color:var(--soluk);font-size:12px}
ol{list-style:none;margin:0;padding:0;display:grid;gap:10px}
li{background:var(--kart);border:1px solid var(--cizgi);border-left:4px solid var(--renk);border-radius:10px;
padding:12px 14px;display:grid;grid-template-columns:auto 1fr auto;gap:4px 14px;align-items:baseline}
.rozet{background:var(--rz);color:var(--renk);font-weight:600;font-size:12.5px;padding:2px 10px;border-radius:999px;white-space:nowrap;justify-self:start}
.ad{font-weight:600;overflow-wrap:anywhere}
.zaman{color:var(--soluk);font-size:13px;white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}
.komut,.detay{grid-column:2/4;overflow-wrap:anywhere}
.komut{font:12.5px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--soluk)}
.detay{font-size:13.5px}
.detay em{font-style:normal;color:var(--soluk)}
.k-BITTI{--renk:var(--yesil);--rz:var(--yesil-z)}
.k-OLDU{--renk:var(--kirmizi);--rz:var(--kirmizi-z)}
.k-YESIL_AMA_OLU{--renk:var(--sari);--rz:var(--sari-z)}
.k-ASKIDA{--renk:var(--mor);--rz:var(--mor-z)}
.k-diger{--renk:var(--gri);--rz:var(--gri-z)}
.bos{background:var(--kart);border:1px dashed var(--cizgi);border-radius:10px;padding:28px 18px;text-align:center}
.bos code{font-size:13px;overflow-wrap:anywhere}
footer{margin-top:22px;color:var(--soluk);font-size:12.5px}
@media (max-width:560px){li{grid-template-columns:1fr}.komut,.detay{grid-column:auto}.zaman{text-align:left;white-space:normal}}
"""


def panel_html(isler: list[dict], bozuk: int = 0, kaynaklar: list[str] | None = None, son: int = 20,
               dosya_yok: bool = False) -> str:
    e = lambda x: html.escape(str(x), quote=True)  # noqa: E731 — panele giren her metin buradan geçer
    gosterilen = list(reversed(isler))[:son]
    sayilar = [(len(isler), "iş"), (sum(i["durum"] == BITTI for i in isler), "bitti"),
               (sum(i["karar"] == "insana_sor" for i in isler), "insana soruldu"),
               (sum(i["yeniden"] for i in isler), "yeniden başlatma")]
    govde = []
    if not isler:
        neden = "Defter dosyası (olaylar.jsonl) bulunamadı." if dosya_yok else "Defterde henüz kayıtlı iş yok."
        govde.append(f'<div class="bos"><p><b>{e(neden)}</b></p><p class="alt">Bir işi nöbet altında başlatınca burada görünür:</p>'
                     f'<p><code>python nobetci.py calistir --cikti sonuc.json -- &lt;komut&gt;</code></p></div>')
    else:
        govde.append('<div class="ozet">' + "".join(f'<div class="sayi"><b>{e(n)}</b><span>{e(t)}</span></div>'
                                                    for n, t in sayilar) + "</div>")
        govde.append("<ol>")
        for i in gosterilen:
            sinif = i["durum"] if i["durum"] in (BITTI, OLDU, YESIL_AMA_OLU, ASKIDA) else "diger"
            detay = [KARAR_ETIKET.get(i["karar"], i["karar"]) or "karar yok"]
            if i["yeniden"]:
                detay.append(f"{i['yeniden']}× yeniden başlatıldı")
            if i["bildirim"]:
                detay.append(f"Telegram: {i['bildirim']}")
            satir = " · ".join(e(x) for x in detay)
            if i["sebep"]:
                satir += f' <em>— {e(i["sebep"])}</em>'
            komut = kisa_komut(i["komut"])
            govde.append(
                f'<li class="k-{e(sinif)}"><span class="rozet">{e(DURUM_ETIKET.get(i["durum"], i["durum"]))}</span>'
                f'<span class="ad">{e(i["gorev"])}</span>'
                f'<span class="zaman">{e(i["baslangic"] or i["bitis"] or "—")} · {e(sure_metni(i["baslangic"], i["bitis"]))}</span>'
                + (f'<span class="komut">{e(komut)}</span>' if komut else "")
                + f'<span class="detay">{satir}</span></li>')
        govde.append("</ol>")
    alt = [f"Son {min(son, len(isler))} / {len(isler)} iş" if isler else "Kayıt yok"]
    if kaynaklar:
        alt.append("defter: " + ", ".join(kaynaklar))
    if bozuk:
        alt.append(f"{bozuk} bozuk satır atlandı")
    alt.append("üretildi: " + time.strftime(_ZAMAN))
    return ("<!doctype html>\n<html lang=\"tr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>Nöbetçi paneli</title><style>{PANEL_CSS}</style></head><body><main>"
            "<h1>Nöbetçi paneli</h1><p class=\"alt\">Uzun süren işler: hangisi bitti, hangisi sessizce öldü, hangisi insana soruldu.</p>"
            + "".join(govde) + f"<footer>{' · '.join(e(x) for x in alt)}</footer></main></body></html>\n")


def panel_uret(klasorler: list[str], cikti: str | Path, son: int = 20) -> tuple[Path, int, int]:
    """Bir ya da birkaç defter klasöründen panel üretir. Dönüş: (dosya, iş sayısı, bozuk satır)."""
    isler, bozuk, dosya_var = [], 0, False
    for k in klasorler:
        d = Path(k) / "olaylar.jsonl"
        dosya_var = dosya_var or d.exists()
        o, b = olaylari_oku(d)
        isler += isleri_cikar(o)
        bozuk += b
    isler.sort(key=lambda i: str(i["baslangic"] or i["bitis"] or ""))   # birkaç defter: zamana göre tek akış
    kaynaklar = [Path(k).resolve().name for k in klasorler]               # tam yol değil: kişisel klasör adı sızmaz
    p = Path(cikti)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(panel_html(isler, bozuk, kaynaklar, son, dosya_yok=not dosya_var), encoding="utf-8")
    return p, len(isler), bozuk


def panel(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="nobetci.py panel", add_help=False)
    ap.add_argument("klasorler", nargs="*", default=[".nobetci"])
    ap.add_argument("--cikti")
    ap.add_argument("--son", type=int, default=20)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        print("Kullanım: python nobetci.py panel [klasör ...] [--cikti panel.html] [--son N]")
        return 3
    p, n, bozuk = panel_uret(a.klasorler, a.cikti or Path(a.klasorler[0]) / "panel.html", max(1, a.son))
    print(f"Panel yazıldı: {p}  ({n} iş" + (f", {bozuk} bozuk satır atlandı" if bozuk else "") + ")")
    return 0


def konsol_utf8():
    """Windows'ta çıktı bir dosyaya/boruya yönlenince Türkçe harf ve ok işareti programı çökertmesin."""
    for akis in (sys.stdout, sys.stderr):
        try:
            akis.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


if __name__ == "__main__":
    konsol_utf8()
    if len(sys.argv) >= 2 and sys.argv[1] == "calistir":
        sys.exit(calistir(sys.argv[2:]))
    elif len(sys.argv) >= 2 and sys.argv[1] == "telegram-kur":
        sys.exit(telegram_kur())
    elif len(sys.argv) >= 3 and sys.argv[1] == "izle":
        ayar = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        sonuc = izle(yukle(sys.argv[2]), Defter(ayar.get("defter", "durum")), ayar.get("aralik_sn", 15), ayar.get("llm"))
        print(json.dumps(sonuc, ensure_ascii=False))
    elif len(sys.argv) >= 3 and sys.argv[1] == "durum":
        sys.exit(nabiz_kontrol(sys.argv[2]))
    elif len(sys.argv) >= 2 and sys.argv[1] == "nabiz":
        sys.exit(nabiz(sys.argv[2:]))
    elif len(sys.argv) >= 2 and sys.argv[1] == "zamanla":
        sys.exit(zamanla(sys.argv[2:]))
    elif len(sys.argv) >= 2 and sys.argv[1] == "panel":
        sys.exit(panel(sys.argv[2:]))
    else:
        print(__doc__)
