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
        # Testler gerçek servislere (Ollama vb.) ASLA dokunmaz: gerçek çareler testte devre dışı.
        # (08.11'de bir test gerçek model sunucusunu boşalttı; bu satır o yüzden var.)
        self._careler = dict(N.CARELER)
        N.CARELER.clear()
        # Aynı kural bildirim için: testler gerçek Telegram'a ASLA mesaj atmaz, sahte kutuya düşer.
        self._bildir, self.kutu = N.bildir, []
        N.bildir = lambda metin: self.kutu.append(metin) or True
        self._ortam = {k: os.environ.pop(k, None) for k in ("NOBETCI_TELEGRAM_TOKEN", "NOBETCI_TELEGRAM_CHAT", "NOBETCI_AYAR")}
        os.environ["NOBETCI_AYAR"] = str(self.d / "telegram.json")   # gerçek ayar dosyası testte görünmez

    def tearDown(self):
        N.bildir = self._bildir
        for k, v in self._ortam.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        N.CARELER.clear()
        N.CARELER.update(self._careler)

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

    # 13) bilinen hata + geri alınabilir iş: nöbetçi çareyi uygular, doğrular, yeniden başlatır ve sadece raporlar
    def test_bilinen_hatayi_kendi_duzeltir(self):
        isaret = self.d / "bellek_bos"
        eski = dict(N.CARELER)
        N.CARELER["Model sunucusu 500 verdi (çoğunlukla bellek baskısı)"] = (
            lambda: (isaret.write_text("1"), "sahte: 2 model boşaltıldı")[1], lambda: isaret.exists(), "Bellekteki modelleri boşalt")
        try:
            # iş, bellek boşaltılmadıysa 500 ile ölür; boşaltıldıysa başarıyla biter
            kod = (f"import sys,pathlib; b=pathlib.Path(r'{isaret}'); "
                   f"(print('HTTP Error 500: Internal Server Error'), sys.exit(1)) if not b.exists() else open('ok','w').write('1')")
            g = self.gorev("ollama", kod, cikti=str(self.d / "ok"), geri_alinabilir=True)
            self.assertEqual(N.izle([g], self.defter, aralik=0.2), {"ollama": N.BITTI})
            d = [o for o in self.olaylar() if "otomatik_duzeltme" in o][0]["otomatik_duzeltme"]
            self.assertTrue(d["dogrulandi"])
            self.assertTrue((self.d / "durum" / "KENDIM_DUZELTTIM.md").exists())
            self.assertFalse((self.d / "durum" / "INSANA_SOR.md").exists())  # insana sorulmadı
        finally:
            N.CARELER.clear(); N.CARELER.update(eski)

    # 14) çare uygulandı ama doğrulanamadı → kör yeniden deneme yok, insana sorulur
    def test_care_dogrulanamazsa_insana_sorar(self):
        eski = dict(N.CARELER)
        N.CARELER["Model sunucusu 500 verdi (çoğunlukla bellek baskısı)"] = (lambda: "sahte", lambda: False, "Bellekteki modelleri boşalt")
        try:
            g = self.gorev("inatci", "import sys; print('HTTP Error 500'); sys.exit(1)", geri_alinabilir=True)
            N.izle([g], self.defter, aralik=0.2)
            son = self.olaylar()[-1]
            self.assertEqual(son["karar"], "insana_sor")
            self.assertIn("doğrulanamadı", son["gerekce"])
        finally:
            N.CARELER.clear(); N.CARELER.update(eski)

    # 15) geri alınamaz işte çare bile denenmez
    def test_geri_alinamaz_iste_care_denenmez(self):
        cagrildi = []
        eski = dict(N.CARELER)
        N.CARELER["Model sunucusu 500 verdi (çoğunlukla bellek baskısı)"] = (lambda: cagrildi.append(1) or "x", lambda: True, "x")
        try:
            g = self.gorev("canli", "import sys; print('HTTP Error 500'); sys.exit(1)", geri_alinabilir=False)
            N.izle([g], self.defter, aralik=0.2)
            self.assertEqual(cagrildi, [])
        finally:
            N.CARELER.clear(); N.CARELER.update(eski)

    # 16) güncel topluluk imzası: Ollama'nın bellek çöküşü tanınır ve geçici sayılır
    def test_ollama_bellek_imzasi(self):
        g = self.gorev("x", "")
        Path(g.log).write_text("Error: model requires more system memory (9.1 GiB) than is available (4.2 GiB)\n", encoding="utf-8")
        b = N.teshis(g, N.OLDU)["bulgular"][0]
        self.assertEqual(b["sebep"], "Model sunucusu bellek yüzünden çöktü")
        self.assertTrue(b["gecici"])

    # 17) gerçek olay (08.11 17:13): 500 hatası Traceback ile birlikte geldi, genel imza onu "kalıcı" yaptı
    def test_traceback_belirgin_sebebi_ezmez(self):
        g = self.gorev("x", "")
        Path(g.log).write_text("Traceback (most recent call last):\n  File \"agent.py\", line 26\n"
                               "httpx.HTTPStatusError: Server error '500 Internal Server Error' for url 'http://localhost:11434/api/chat'\n",
                               encoding="utf-8")
        t = N.teshis(g, N.OLDU)
        self.assertEqual([b["sebep"] for b in t["bulgular"]], ["Model sunucusu 500 verdi (çoğunlukla bellek baskısı)"])
        self.assertEqual(N.sef_karari(N.Gorev(ad="x", komut=[], log=g.log, geri_alinabilir=True), t, 0)["karar"], "yeniden_baslat")

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

    # ---- tek komut (calistir): ayar dosyası yazmadan aynı kurallar
    def calistir(self, *secenek, kod):
        return N.calistir(["--klasor", str(self.d / "nb"), "--aralik", "0.2", *secenek, "--", PY, "-c", kod])

    def nb_olaylar(self):
        return [json.loads(s) for s in (self.d / "nb" / "olaylar.jsonl").read_text(encoding="utf-8").splitlines()]

    # 18) tek komut, başarılı iş: çıkış 0, defter ve çalıştırmaya özel log kendiliğinden oluşur
    def test_calistir_basarili(self):
        cikti = self.d / "sonuc.json"
        self.assertEqual(self.calistir("--cikti", str(cikti), "--ad", "deney",
                                       kod=f"open(r'{cikti}','w').write('{{}}'); print('tamam')"), 0)
        self.assertEqual(self.nb_olaylar()[-1]["durum"], N.BITTI)
        loglar = list((self.d / "nb" / "loglar").glob("deney-*.log"))
        self.assertEqual(len(loglar), 1)
        self.assertIn("tamam", loglar[0].read_text(encoding="utf-8"))

    # 19) tek komut, "yeşil ama ölü": çıkış kodu 0 olsa da nöbetçi 1 döner ve insana sorar
    def test_calistir_yesil_ama_olu(self):
        self.assertEqual(self.calistir("--cikti", str(self.d / "yok.json"), kod="print('bitti sanıyorum')"), 1)
        self.assertEqual(self.nb_olaylar()[-1]["karar"], "insana_sor")
        self.assertTrue((self.d / "nb" / "INSANA_SOR.md").exists())

    # 20) tek komut + --geri-alinabilir: geçici hatada (500) sormadan yeniden başlatır, iş biter
    def test_calistir_gecici_hatada_yeniden_baslatir(self):
        sayac, ok = self.d / "sayac", self.d / "ok"
        kod = (f"import sys,pathlib; p=pathlib.Path(r'{sayac}'); n=int(p.read_text()) if p.exists() else 0; "
               f"p.write_text(str(n+1)); "
               f"(print('HTTP Error 500: Internal Server Error'), sys.exit(1)) if n<1 else open(r'{ok}','w').write('1')")
        self.assertEqual(self.calistir("--cikti", str(ok), "--geri-alinabilir", kod=kod), 0)
        self.assertEqual([o["karar"] for o in self.nb_olaylar() if o["durum"] != "BASLADI"], ["yeniden_baslat", "kapat"])

    # 21) yanlış kullanım hiçbir şey başlatmaz: komut yok / olmayan program → 2, defter bile açılmaz
    def test_calistir_yanlis_kullanim(self):
        self.assertEqual(N.calistir(["--cikti", "x.json"]), 2)                       # '--' yok
        self.assertEqual(N.calistir(["--klasor", str(self.d / "nb"), "--"]), 2)      # komut boş
        self.assertEqual(N.calistir(["--klasor", str(self.d / "nb"), "--", "boyle-bir-program-yok-xyz"]), 2)
        self.assertFalse((self.d / "nb").exists())

    # 22) görev adı komuttan türetilir: yorumlayıcı değil, çalıştırılan dosya
    def test_ad_uret(self):
        self.assertEqual(N.ad_uret(["python", "src/egit.py", "--tur", "5"]), "egit")
        self.assertEqual(N.ad_uret(["ffmpeg", "-i", "a.mp4"]), "ffmpeg")
        self.assertEqual(N.ad_uret([PY, "-c", "print(1)"]), "print-1")

    # ---- bildirim: "insana sor" kararında haber gider; başka hiçbir durumda gitmez
    # 23) insana sorulan iş: mesajda görev adı, sebep ve kanıt satırı var; deftere "gitti" yazılır
    def test_insana_sorunca_bildirim_gider(self):
        g = self.gorev("garip", "import sys; print('zort'); sys.exit(3)")
        N.izle([g], self.defter, aralik=0.2)
        self.assertEqual(len(self.kutu), 1)
        for parca in ("garip", "Tanınmayan", "zort"):
            self.assertIn(parca, self.kutu[0])
        self.assertEqual(self.olaylar()[-1]["bildirim"], "gitti")

    # 24) biten iş ve kendi düzelttiği iş için kimse rahatsız edilmez
    def test_biten_is_bildirim_atmaz(self):
        g = self.gorev("iyi", "open('sonuc.json','w').write('{}')", cikti=str(self.d / "sonuc.json"))
        N.izle([g], self.defter, aralik=0.2)
        self.assertEqual(self.kutu, [])

    # 25) ayar yoksa bildirim sessizce kapalıdır; Telegram'a ulaşılamazsa da izleme çökmez
    def test_bildirim_ayarsiz_ve_agsiz(self):
        self.assertIsNone(N.bildirim_ayari())
        self.assertFalse(self._bildir("deneme"))                       # ayar yok → False, ağa çıkılmaz
        (self.d / "telegram.json").write_text('{"token": "sahte", "chat_id": "1"}', encoding="utf-8")
        self.assertEqual(N.bildirim_ayari(), {"token": "sahte", "chat_id": "1"})
        eski = N._telegram
        N._telegram = lambda *a, **k: (_ for _ in ()).throw(OSError("ağ yok"))
        try:
            self.assertFalse(self._bildir("deneme"))                   # gönderilemedi → False, istisna yok
        finally:
            N._telegram = eski


if __name__ == "__main__":
    N.konsol_utf8()
    unittest.main(verbosity=2)
