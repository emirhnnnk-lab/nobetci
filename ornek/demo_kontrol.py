"""Canlı demolarım açılıyor mu? Her adrese bir istek atar, sonucu JSON rapora yazar.

Nöbetçi altında çalıştırmak için (ayar dosyası yok):
  python nobetci.py calistir --cikti demo_raporu.json --sessizlik 300 -- python ornek/demo_kontrol.py demo_raporu.json

Hugging Face Spaces kullanılmayınca uyur; uyuyan demo ilk istekte geç cevap verir. O yüzden her adres
birkaç kez denenir. Rapor her adresten sonra log'a bir satır düşer ki Nöbetçi ilerlemeyi görebilsin.
"""
import json
import sys
import time
import urllib.error
import urllib.request

ADRESLER = [
    "https://emirhan-karaca.vercel.app",
    "https://ornek-magazalar.vercel.app",
    "https://emrhnnnnnn-musteri-hizmetleri-demo.hf.space",
    "https://emrhnnnnnn-ai-uygulamalar-demo.hf.space",
]


def bak(adres: str, deneme: int = 3, bekle: int = 20) -> dict:
    son = {}
    for n in range(1, deneme + 1):
        t0 = time.time()
        try:
            istek = urllib.request.Request(adres, headers={"User-Agent": "demo-kontrol"})
            kod = urllib.request.urlopen(istek, timeout=90).status
        except urllib.error.HTTPError as e:
            kod = e.code
        except Exception as e:
            kod = f"{type(e).__name__}"
        son = {"adres": adres, "kod": kod, "sure_sn": round(time.time() - t0, 1), "deneme": n}
        print(f"{kod} {adres} ({son['sure_sn']} sn, deneme {n})", flush=True)
        if kod == 200:
            break
        if n < deneme:
            time.sleep(bekle)
    return son


if __name__ == "__main__":
    rapor_yolu = sys.argv[1] if len(sys.argv) > 1 else "demo_raporu.json"
    sonuclar = [bak(a) for a in ADRESLER]
    acik = sum(1 for s in sonuclar if s["kod"] == 200)
    rapor = {"zaman": time.strftime("%Y-%m-%d %H:%M:%S"), "acik": acik, "toplam": len(sonuclar), "sonuclar": sonuclar}
    with open(rapor_yolu, "w", encoding="utf-8") as f:
        json.dump(rapor, f, ensure_ascii=False, indent=2)
    print(f"{acik}/{len(sonuclar)} demo açık", flush=True)
    sys.exit(0 if acik == len(sonuclar) else 1)
