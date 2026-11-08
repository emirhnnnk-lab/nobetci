"""Nöbetçi — uzun süren işleri DIŞARIDAN izleyen, sessiz ölümü yakalayan, teşhis koyan ve
geri alınabilir durumlarda kendi başına toparlayan küçük bir ajan sistemi.

Üç rol, hepsi dosya üzerinden konuşur (birbirine mesaj atmaz):
  gözcü   → işin "çalışıyor mu" değil "ilerliyor mu" olduğunu ölçer (süreç, log, çıktı dosyası)
  teşhis  → ölüm/askıda kalma anında log + sistem durumundan sebep çıkarır; her iddia logdan bir satırla kanıtlanır
  şef     → kurala göre karar verir: yeniden başlat (geri alınabilirse, hak varsa) ya da insana sor

Kullanım:
  python nobetci.py izle gorevler.json        # görevleri başlatır ve izler
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
    (r"CUDA out of memory|OutOfMemoryError|CUBLAS_STATUS_ALLOC_FAILED", "GPU belleği yetmedi", True),
    (r"CUDA error|device-side assert", "CUDA hatası", True),
    (r"MemoryError|Cannot allocate memory|std::bad_alloc", "RAM yetmedi", True),
    (r"HTTP Error 500|status code 500|Internal Server Error", "Model sunucusu 500 verdi (çoğunlukla bellek baskısı)", True),
    (r"Connection refused|ConnectionResetError|URLError|timed out|TimeoutError", "Ağ/servis bağlantısı koptu", True),
    (r"No space left on device", "Disk doldu", False),
    (r"FileNotFoundError|No such file or directory", "Dosya bulunamadı", False),
    (r"ModuleNotFoundError|ImportError", "Eksik paket", False),
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


# ---------------------------------------------------------------- olay defteri + nabız
class Defter:
    def __init__(self, klasor: str):
        self.k = Path(klasor)
        self.k.mkdir(parents=True, exist_ok=True)

    def yaz(self, olay: dict):
        olay = {"zaman": time.strftime("%Y-%m-%d %H:%M:%S"), **olay}
        with (self.k / "olaylar.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(olay, ensure_ascii=False) + "\n")
        if olay.get("karar") == "insana_sor":
            with (self.k / "INSANA_SOR.md").open("a", encoding="utf-8") as f:
                f.write(f"\n## {olay['zaman']} · {olay['gorev']} · {olay['durum']}\n{olay['gerekce']}\n")
                for b in olay.get("teshis", {}).get("bulgular", []):
                    f.write(f"- **{b['sebep']}** — kanıt: `{b['kanit']}`\n")
                if olay.get("teshis", {}).get("ozet"):
                    f.write(f"- Özet: {olay['teshis']['ozet']}\n")
        print(f"[{olay['zaman']}] {olay['gorev']}: {olay['durum']} → {olay.get('karar', '')}", flush=True)

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
                                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})


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
            defter.yaz({"gorev": g.ad, "durum": durum, **k, "teshis": t})
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


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "izle":
        ayar = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        sonuc = izle(yukle(sys.argv[2]), Defter(ayar.get("defter", "durum")), ayar.get("aralik_sn", 15), ayar.get("llm"))
        print(json.dumps(sonuc, ensure_ascii=False))
    elif len(sys.argv) >= 3 and sys.argv[1] == "durum":
        sys.exit(nabiz_kontrol(sys.argv[2]))
    else:
        print(__doc__)
