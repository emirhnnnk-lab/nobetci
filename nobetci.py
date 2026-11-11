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
Bağımlılık yok (stdlib). Ollama varsa teşhis özetini yerel LLM yazar; yoksa kural tabanlı teşhis yeter.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
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


def nabiz_kontrol(klasor: str, esik_sn: int = 180) -> int:
    """Nöbetçiyi nöbetçiden bağımsız kontrol eder (zamanlanmış görev / başka makine çağırır)."""
    p = Path(klasor) / "nabiz"
    if not p.exists():
        print("ALARM: nabız dosyası yok — nöbetçi hiç çalışmamış ya da yanlış klasör")
        return 2
    yas = time.time() - float(p.read_text())
    if yas > esik_sn:
        print(f"ALARM: nöbetçi {int(yas)} sn'dir nabız atmıyor — nöbetçinin kendisi ölü")
        return 1
    print(f"tamam: son nabız {int(yas)} sn önce")
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
    else:
        print(__doc__)
