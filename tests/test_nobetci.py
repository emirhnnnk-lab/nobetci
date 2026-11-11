"""Her test gerçekten yaşanmış bir arızayı küçük bir sahte işle canlandırır."""
import contextlib
import io
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

    # ---- zamanlanmış nabız: alarmda haber, aynı alarm bir kez, düzelince bir kez
    def nabiz_yaz(self, yas_sn: float):
        k = self.d / "durum"
        (k / "nabiz").write_text(str(time.time() - yas_sn))
        return str(k)

    def sessiz(self, f, *a, **k):
        cikti = io.StringIO()
        with contextlib.redirect_stdout(cikti):
            sonuc = f(*a, **k)
        return sonuc, cikti.getvalue()

    # 26) nöbetçinin nabzı eski → alarm kodu döner ve Telegram'a tek mesaj gider
    def test_nabiz_alarmda_bildirim_gider(self):
        k = self.nabiz_yaz(600)
        kod, _ = self.sessiz(N.nabiz_tur, k, esik_sn=60)
        self.assertEqual(kod, 1)
        self.assertEqual(len(self.kutu), 1)
        self.assertIn("alarm", self.kutu[0])

    # 27) nabız taze → 0 döner, kimse rahatsız edilmez
    def test_nabiz_tamamsa_sessiz(self):
        k = self.nabiz_yaz(5)
        self.assertEqual(self.sessiz(N.nabiz_tur, k, esik_sn=60)[0], 0)
        self.assertEqual(self.kutu, [])

    # 28) aynı alarm ikinci turda tekrar gitmez; alarm türü değişirse ya da hatırlatma süresi dolarsa gider
    def test_ayni_alarm_tekrar_gitmez(self):
        k = self.nabiz_yaz(600)
        self.sessiz(N.nabiz_tur, k, esik_sn=60)
        kod, yazi = self.sessiz(N.nabiz_tur, k, esik_sn=60)
        self.assertEqual(kod, 1)
        self.assertEqual(len(self.kutu), 1)
        self.assertIn("tekrar gönderilmedi", yazi)
        self.sessiz(N.nabiz_tur, k, esik_sn=60, tekrar_sn=0)          # hatırlatma süresi doldu
        self.assertEqual(len(self.kutu), 2)
        self.assertIn("hatırlatma", self.kutu[1])
        (Path(k) / "nabiz").unlink()                                    # alarm türü değişti: nabız dosyası yok
        self.assertEqual(self.sessiz(N.nabiz_tur, k, esik_sn=60)[0], 2)
        self.assertEqual(len(self.kutu), 3)

    # 29) alarm düzelince tek bir "düzeldi" mesajı gider, sonra yine sessizlik
    def test_nabiz_duzelince_tek_mesaj(self):
        k = self.nabiz_yaz(600)
        self.sessiz(N.nabiz_tur, k, esik_sn=60)
        self.nabiz_yaz(1)
        self.assertEqual(self.sessiz(N.nabiz_tur, k, esik_sn=60)[0], 0)
        self.sessiz(N.nabiz_tur, k, esik_sn=60)
        self.assertEqual(len(self.kutu), 2)
        self.assertIn("düzeldi", self.kutu[1])

    # 30) Telegram ayarı yoksa çökmez, anlaşılır mesaj verir, alarm kodu yine döner; ağa çıkılmaz
    def test_nabiz_ayarsiz(self):
        N.bildir = self._bildir                                         # gerçek bildir, ama ayar yok
        eski = N._telegram
        N._telegram = lambda *a, **k: (_ for _ in ()).throw(AssertionError("ağa çıkılmamalıydı"))
        try:
            kod, yazi = self.sessiz(N.nabiz_tur, self.nabiz_yaz(600), esik_sn=60)
        finally:
            N._telegram = eski
        self.assertEqual(kod, 1)
        self.assertIn("Telegram ayarı yok", yazi)
        self.assertFalse((self.d / "durum" / "nabiz_bildirim.json").exists())   # bir dahakine yine denenir

    # 31) nöbet bitip nöbetçi kapandıysa eski nabız alarm değildir; açık iş varken eskirse alarmdır
    def test_nobet_bitince_eski_nabiz_alarm_degil(self):
        g = self.gorev("iyi", "open('sonuc.json','w').write('{}')", cikti=str(self.d / "sonuc.json"))
        N.izle([g], self.defter, aralik=0.2)
        k = self.nabiz_yaz(600)
        self.assertEqual(self.sessiz(N.nabiz_tur, k, esik_sn=60)[0], 0)
        with (Path(k) / "olaylar.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"zaman": "2026-11-11 10:00:00", "gorev": "yarim", "durum": "BASLADI"}) + "\n")
        self.assertEqual(self.sessiz(N.nabiz_tur, k, esik_sn=60)[0], 1)
        self.assertEqual(len(self.kutu), 1)

    # 32) --aralik ile kendi içinde döner; 3 turda aynı alarm yine tek mesaj
    def test_nabiz_dongu(self):
        k = self.nabiz_yaz(600)
        kod, _ = self.sessiz(N.nabiz, [k, "--esik", "60", "--aralik", "0.01", "--tur", "3"])
        self.assertEqual(kod, 1)
        self.assertEqual(len(self.kutu), 1)
        self.assertEqual(self.sessiz(N.nabiz, [])[0], 3)                # kullanım hatası

    # 33) zamanla yalnız komutu ekrana yazar: hiçbir süreç başlatmaz, sisteme dokunmaz
    def test_zamanla_yalniz_yazar(self):
        yasak = lambda *a, **k: (_ for _ in ()).throw(AssertionError("zamanla bir şey çalıştırmamalı"))  # noqa: E731
        eski = (N.subprocess.run, N.subprocess.Popen, N.os.system)
        N.subprocess.run = N.subprocess.Popen = N.os.system = yasak
        try:
            kod, yazi = self.sessiz(N.zamanla, [str(self.d / "durum"), "--dakika", "15"])
        finally:
            N.subprocess.run, N.subprocess.Popen, N.os.system = eski
        self.assertEqual(kod, 0)
        for parca in ("schtasks /Create /SC MINUTE /MO 15", "/Delete", "nabiz", str((self.d / "durum").resolve()),
                      "KAYDEDİLMEDİ"):
            self.assertIn(parca, yazi)

    # ---- panel: defterden tek dosyalık HTML
    def defter_yaz(self, satirlar: list):
        p = self.d / "durum" / "olaylar.jsonl"
        p.write_text("".join((s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)) + "\n" for s in satirlar),
                     encoding="utf-8")

    def panel(self, *klasor):
        p, n, bozuk = N.panel_uret(list(klasor) or [str(self.d / "durum")], self.d / "panel.html")
        return p.read_text(encoding="utf-8"), n, bozuk

    # 34) normal: her iş için zaman, ad, komut, karar, süre; karar renk sınıfıyla ayrılır; dış kaynak yok
    def test_panel_normal(self):
        self.defter_yaz([
            {"zaman": "2026-11-08 16:13:55", "gorev": "senaryo", "durum": "BASLADI", "komut": ["python", "src/ajan.py"]},
            {"zaman": "2026-11-08 16:20:00", "gorev": "senaryo", "durum": "OLDU", "karar": "yeniden_baslat"},
            {"zaman": "2026-11-08 16:29:27", "gorev": "senaryo", "durum": "OLDU", "karar": "insana_sor",
             "teshis": {"bulgular": [{"sebep": "GPU belleği yetmedi", "gecici": True, "kanit": "CUDA out of memory"}]}},
            {"zaman": "2026-11-11 17:02:35", "gorev": "demo", "durum": "BASLADI", "komut": ["python", "demo.py"]},
            {"zaman": "2026-11-11 17:02:39", "gorev": "demo", "durum": "BITTI", "karar": "kapat"},
        ])
        h, n, bozuk = self.panel()
        self.assertEqual((n, bozuk), (2, 0))
        for parca in ("senaryo", "ajan.py", "15 dk 32 sn", "insana soruldu", "1× yeniden başlatıldı", "GPU belleği yetmedi",
                      "demo", "4 sn", 'class="k-BITTI"', 'class="k-OLDU"', "prefers-color-scheme:dark", "viewport"):
            self.assertIn(parca, h)
        self.assertNotIn("CUDA out of memory", h)                       # kanıt satırı (log içeriği) panele girmez
        self.assertLess(h.index("demo"), h.index("senaryo"))            # en yeni üstte
        for dis in ("<script", "http://", "https://", "@import", "<link"):
            self.assertNotIn(dis, h)

    # 35) defter yoksa ya da boşsa anlamlı boş durum sayfası
    def test_panel_bos(self):
        h, n, _ = self.panel(str(self.d / "olmayan"))
        self.assertEqual(n, 0)
        self.assertIn("bulunamadı", h)
        self.defter_yaz([])
        h, n, _ = self.panel()
        self.assertIn("henüz kayıtlı iş yok", h)

    # 36) bozuk/yarım satır paneli çökertmez: atlanır ve sayılır
    def test_panel_bozuk_satir(self):
        self.defter_yaz([
            {"zaman": "2026-11-11 10:00:00", "gorev": "iyi", "durum": "BASLADI", "komut": ["python", "a.py"]},
            '{"zaman": "2026-11-11 10:00:05", "gorev": "iy',               # yarım yazılmış satır
            "[1, 2]",                                                       # JSON ama olay değil
            {"zaman": "2026-11-11 10:00:09", "gorev": "iyi", "durum": "BITTI", "karar": "kapat", "teshis": "bozuk"},
        ])
        h, n, bozuk = self.panel()
        self.assertEqual((n, bozuk), (1, 2))
        self.assertIn("2 bozuk satır atlandı", h)
        self.assertIn("9 sn", h)

    # 37) olaylardaki her metin kaçışlanır: iş adı ya da sebep HTML/script enjekte edemez
    def test_panel_html_kacisi(self):
        self.defter_yaz([
            {"zaman": "2026-11-11 10:00:00", "gorev": "<script>alert(1)</script>", "durum": "BASLADI",
             "komut": ["python", "<img src=x onerror=alert(2)>"]},
            {"zaman": "2026-11-11 10:00:01", "gorev": "<script>alert(1)</script>", "durum": "OLDU", "karar": "insana_sor",
             "teshis": {"bulgular": [{"sebep": "a & b <b>kalın</b>"}]}},
        ])
        h = self.panel()[0]
        self.assertNotIn("<script>", h)
        self.assertNotIn("<img", h)
        self.assertNotIn("<b>kalın", h)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", h)
        self.assertIn("a &amp; b &lt;b&gt;kalın&lt;/b&gt;", h)

    # 38) panele gizli bilgi girmez: token argümanı maskelenir, kişisel yol dosya adına iner, Telegram ayarı okunmaz
    def test_panel_gizli_bilgi_sizmaz(self):
        (self.d / "telegram.json").write_text('{"token": "SAHTE-TOKEN-987", "chat_id": "55"}', encoding="utf-8")
        self.defter_yaz([
            {"zaman": "2026-11-11 10:00:00", "gorev": "egit", "durum": "BASLADI",
             "komut": [r"C:\Users\birisi\AppData\python.exe", r"C:\Users\birisi\proje\egit.py", "--token", "gizli123",
                       "--api-key=gizli456"]},
        ])
        h = self.panel()[0]
        for sizinti in ("SAHTE-TOKEN-987", "gizli123", "gizli456", "birisi", "Users"):
            self.assertNotIn(sizinti, h)
        self.assertIn("python egit.py --token ••• --api-key=•••", h)
        self.assertIn("Kayıt yarım", h)                                  # bitmemiş iş de görünür


if __name__ == "__main__":
    N.konsol_utf8()
    unittest.main(verbosity=2)
