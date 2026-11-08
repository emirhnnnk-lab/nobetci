"""Her test gerçekten yaşanmış bir arızayı küçük bir sahte işle canlandırır."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nobetci as N  # noqa: E402

PY = sys.executable


def is_(kod: str) -> list[str]:
    return [PY, "-c", kod]


class Senaryolar(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.defter = N.Defter(str(self.d / "durum"))

    def gorev(self, ad, kod, **k):
        return N.Gorev(ad=ad, komut=is_(kod), log=str(self.d / f"{ad}.log"), cwd=str(self.d), **k)

    def olaylar(self):
        return [json.loads(s) for s in (self.d / "durum" / "olaylar.jsonl").read_text(encoding="utf-8").splitlines()]

    # 1) normal iş: çıktı yazıldı, çıkış 0
    def test_basarili_is(self):
        g = self.gorev("iyi", "open('sonuc.json','w').write('{}'); print('tamam')", cikti=str(self.d / "sonuc.json"))
        self.assertEqual(N.izle([g], self.defter, aralik=0.2), {"iyi": N.BITTI})

    # 2) "yeşil ama ölü": çıkış 0 ama beklenen dosya yok → insana sorulur
    def test_yesil_ama_olu(self):
        g = self.gorev("yesil", "print('bitti sanıyorum')", cikti=str(self.d / "yok.json"), geri_alinabilir=True)
        self.assertEqual(N.izle([g], self.defter, aralik=0.2), {"yesil": N.YESIL_AMA_OLU})
        self.assertEqual(self.olaylar()[-1]["karar"], "insana_sor")

    # 3) CUDA OOM: geçici sebep + geri alınabilir → şef 2 kez yeniden başlatır, sonra insana sorar
    def test_cuda_oom_yeniden_dene_sonra_sor(self):
        g = self.gorev("oom", "import sys; print('RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB'); sys.exit(1)",
                       geri_alinabilir=True, yeniden_hak=2)
        self.assertEqual(N.izle([g], self.defter, aralik=0.2), {"oom": N.OLDU})
        kararlar = [o["karar"] for o in self.olaylar() if o["durum"] == N.OLDU]
        self.assertEqual(kararlar, ["yeniden_baslat", "yeniden_baslat", "insana_sor"])
        self.assertIn("GPU belleği", self.olaylar()[-1]["teshis"]["bulgular"][0]["sebep"])

    # 4) üçüncüde tutan iş: geçici hata sonrası yeniden başlatma işi kurtarır
    def test_gecici_hata_sonra_kurtulur(self):
        sayac = self.d / "sayac"
        kod = (f"import sys,pathlib; p=pathlib.Path(r'{sayac}'); n=int(p.read_text()) if p.exists() else 0; "
               f"p.write_text(str(n+1)); "
               f"(print('urllib.error.HTTPError: HTTP Error 500: Internal Server Error'), sys.exit(1)) if n<1 else open('ok','w').write('1')")
        g = self.gorev("ollama500", kod, cikti=str(self.d / "ok"), geri_alinabilir=True)
        self.assertEqual(N.izle([g], self.defter, aralik=0.2), {"ollama500": N.BITTI})

    # 5) kalıcı hata (dosya yok) → yeniden denemek anlamsız, direkt insana
    def test_kalici_hata_yeniden_denenmez(self):
        g = self.gorev("kalici", "open('olmayan_veri.csv')", geri_alinabilir=True)
        N.izle([g], self.defter, aralik=0.2)
        oo = [o for o in self.olaylar() if o["durum"] == N.OLDU]
        self.assertEqual(len(oo), 1)
        self.assertEqual(oo[0]["karar"], "insana_sor")

    # 6) askıda kalan iş: süreç yaşıyor ama log kıpırdamıyor → öldürülür, teşhis edilir
    def test_askida(self):
        g = self.gorev("asili", "print('yükleniyor', flush=True); import time; time.sleep(60)", sessizlik_sn=1)
        t0 = time.time()
        self.assertEqual(N.izle([g], self.defter, aralik=0.3), {"asili": N.ASKIDA})
        self.assertLess(time.time() - t0, 20)

    # 7) 07.11 21:36 dersi: dışarıdan bakınca log susmuş ama çıktı yazılmış → ÖLÜ DEĞİL, BİTMİŞ
    def test_log_sustu_ama_cikti_var_bitti_sayilir(self):
        log = self.d / "d3.log"
        log.write_text("özgüven +4.0: {'mmlu': 0.217}\n", encoding="utf-8")
        cikti = self.d / "zeka_haritasi.json"
        cikti.write_text("{}", encoding="utf-8")
        eski = time.time() - 3600
        os.utime(log, (eski, eski)); os.utime(cikti, (eski, eski))
        g = N.Gorev(ad="d3", komut=[], log=str(log), cikti=str(cikti), sessizlik_sn=600)
        self.assertEqual(N.dis_gozlem(g), N.BITTI)
        cikti.unlink()
        self.assertEqual(N.dis_gozlem(g), N.ASKIDA)

    # 7b) gerçek demo (08.11): ytfactory ajanı 3 denemede doğrulamayı geçemedi → kalıcı, yeniden deneme yok, kanıtlı
    def test_dogrulama_gecilemedi(self):
        g = self.gorev("ytf", "import sys; sys.exit(\"sahne: 3 denemede geçerli çıktı yok, son hatalar: ['l2: 3 accents']\")",
                       geri_alinabilir=True)
        N.izle([g], self.defter, aralik=0.2)
        o = [x for x in self.olaylar() if x["durum"] == N.OLDU]
        self.assertEqual([x["karar"] for x in o], ["insana_sor"])
        self.assertIn("doğrulamayı geçemedi", o[0]["teshis"]["bulgular"][0]["sebep"])

    # 7c) tanınmayan ölüm "kalıcı" diye uydurulmaz; bilinmediği söylenir ve son satır kanıt olur
    def test_taninmayan_sebep(self):
        g = self.gorev("garip", "import sys; print('zort'); sys.exit(3)")
        N.izle([g], self.defter, aralik=0.2)
        b = self.olaylar()[-1]["teshis"]["bulgular"][0]
        self.assertIn("Tanınmayan", b["sebep"])
        self.assertEqual(b["kanit"], "zort")

    # 8) geri alınamaz işe asla otomatik dokunulmaz
    def test_geri_alinamaz_ise_dokunulmaz(self):
        g = self.gorev("yayin", "import sys; print('ConnectionResetError'); sys.exit(1)", geri_alinabilir=False)
        N.izle([g], self.defter, aralik=0.2)
        self.assertEqual(self.olaylar()[-1]["karar"], "insana_sor")

    # 9) nöbetçinin kendisi: nabız eskiyse dış kontrol alarm verir
    def test_nabiz(self):
        k = self.d / "durum"
        self.defter.nabiz()
        self.assertEqual(N.nabiz_kontrol(str(k), esik_sn=60), 0)
        (k / "nabiz").write_text(str(time.time() - 600))
        self.assertEqual(N.nabiz_kontrol(str(k), esik_sn=60), 1)

    # 10) kanıt disiplini: LLM logda olmayan bir satırı "alıntılarsa" özeti atılır
    def test_uydurma_alinti_reddedilir(self):
        log = "loading...\nRuntimeError: CUDA out of memory"
        self.assertTrue(N.alinti_gecerli('Sebep "RuntimeError: CUDA out of memory" satırı.', log))
        self.assertFalse(N.alinti_gecerli('Sebep "disk quota exceeded on /mnt/data" hatası.', log))
        self.assertFalse(N.alinti_gecerli("Muhtemelen bellek sorunu.", log))  # kanıtsız iddia da geçmez


if __name__ == "__main__":
    unittest.main(verbosity=2)
