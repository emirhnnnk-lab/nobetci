# Nöbetçi — sessiz ölümü yakalayan izleme ajanı

> **EN summary:** A small, dependency-free agent system that watches long-running jobs *from the outside*. It catches "silent death" (jobs that are green but dead, or alive but stuck), diagnoses the cause from logs with evidence-backed claims, and auto-recovers only when the job is reversible and the cause is transient. Everything else is escalated to a human. Built after my own ML experiments died silently on a 4 GB GPU machine. 12 failure-scenario tests, all reproduced from real incidents.

Uzun süren işler (model eğitimi, deney koşuları, video render'ı, gece çalışan ajanlar) çoğu zaman **gürültüsüz** ölür. Hata mesajı ekrana düşer ama kimse okumaz. Süreç ayakta görünür ama hiçbir şey yapmıyordur. Ya da "başarıyla" çıkar ama beklenen dosyayı yazmamıştır.

Nöbetçi bu yüzden "süreç yaşıyor mu?" diye sormaz, **"iş ilerliyor mu, sonuç ortada mı?"** diye sorar.

## Neden yazdım

Kendi deneylerimde arka arkaya bunlar oldu:

| Ne oldu | Gerçek sebep | Nöbetçideki karşılığı |
|---|---|---|
| Ollama tabanlı video ajanı ara ara `HTTP 500` ile düştü | 16 GB RAM'de 7B + 14B model birlikte yüklüydü; boş RAM ~1 GB | Geçici sebep → otomatik yeniden deneme (test 4) |
| Yorumlanabilirlik deneyi CUDA hatasıyla düştü, ben saatler sonra fark ettim | 4 GB GPU'da bellek baskısı | GPU belleği imzası, hak sınırlı yeniden başlatma (test 3) |
| Bir koşunun log'u 21:36'da kesildi, "sessizce öldü" sandım ve yeniden başlattım | **Ölmemişti — bitmişti.** Son log satırı zaten son adımdı; sonuç dosyası aynı dakikada yazılmıştı | "Log sustu" tek başına ölüm sayılmaz; çıktı dosyası varsa iş **bitti** (test 7) |

Üçüncüsü en öğretici olanı: yanlış alarm da gerçek bir arızadır, insanın vaktini yer. Bu yüzden Nöbetçi'nin "bitti" tanımı log'a değil **sonuç dosyasına** bakar.

## Nasıl çalışır

Üç rol var, birbirleriyle konuşmazlar; her şey dosyaya yazılır (`olaylar.jsonl`, `INSANA_SOR.md`, `nabiz`):

```
 gözcü ──► durum ──► teşhis ──► şef ──► yeniden başlat  (geri alınabilir + geçici sebep + hak var)
   │                    │          └──► INSANA_SOR.md   (diğer her şey)
   │                    └─ her iddia log'dan birebir bir satırla kanıtlanır
   └─ log/çıktı dosyasının değişim zamanı + çıkış kodu + beklenen çıktı
```

- **Gözcü** beş durumdan birini verir:
  - `CALISIYOR`
  - `BITTI`: çıkış 0 ve beklenen çıktı dosyası var.
  - `YESIL_AMA_OLU`: çıkış 0 ama çıktı yok.
  - `OLDU`: sıfırdan farklı çıkış kodu.
  - `ASKIDA`: süreç ayakta ama N saniyedir ne log ne çıktı değişti.
- **Teşhis** önce kural tabanlı imzalara bakar: GPU belleği, CUDA, RAM, model sunucusu 500, ağ, disk, eksik dosya, eksik paket. Bulduğu her sebebi, log'daki kanıt satırıyla birlikte yazar. Ollama varsa yerel bir LLM iki cümlelik özet de yazar. Ama **özet, log'da birebir geçen bir satırı alıntılamıyorsa atılır.** Model kanıtsız teşhis koyamaz.
- **Şef** sabit kurallarla karar verir, model değil:
  - Görev "geri alınabilir" işaretli değilse hiçbir şeye dokunmaz.
  - Sebep kalıcıysa (dosya yok, disk dolu) yeniden denemenin anlamı yok, insana sorar.
  - Yeniden deneme hakkı bitince insana sorar.
- **Nabız:** nöbetçinin kendisi de ölebilir. Her turda `nabiz` dosyasına zaman yazar. `python nobetci.py durum <klasör>` komutu bunu **dışarıdan** kontrol eder; zamanlanmış görevle veya başka bir makineden çağrılması gerekir. İçeriden yapılan sağlık kontrolü, ölçtüğü şeyle birlikte ölür.

## Kullanım

```bash
python nobetci.py izle ornek/ytfactory_senaryo.json   # görevleri başlat ve izle
python nobetci.py durum ornek/durum                    # nöbetçi yaşıyor mu? (0 = tamam, 1 = alarm)
python tests/test_nobetci.py                           # 12 arıza senaryosu
```

Görev tanımı (`ornek/ytfactory_senaryo.json`):

```json
{
  "ad": "ytfactory-senaryo",
  "komut": ["python", "src/ajan.py", "..."],
  "log": "ornek/loglar/ytfactory-senaryo.log",
  "cikti": ".../scenes.spec.json",
  "sessizlik_sn": 900,
  "geri_alinabilir": true,
  "yeniden_hak": 2
}
```

İzlenen programın kodu değiştirilmez. Nöbetçi'nin bilmesi gereken üç şey var: komut, log dosyası ve "bitti" sayılacak çıktı dosyası.

## Testler: her biri yaşanmış bir arıza

1. Normal iş → `BITTI`
2. Çıkış 0 ama çıktı yok → `YESIL_AMA_OLU`, insana sorulur
3. CUDA OOM → 2 kez yeniden başlatma, sonra insana sorulur
4. Model sunucusu bir kez 500 veriyor → yeniden başlatma işi kurtarıyor
5. Kalıcı hata (eksik dosya) → yeniden denenmez
6. Askıda kalan iş → öldürülür, teşhis edilir
7. **Log susmuş ama çıktı yazılmış → bitmiş sayılır, ölü değil** (21:36 dersi)
8. Geri alınamaz işe otomatik dokunulmaz
9. Nöbetçinin nabzı eskirse dış kontrol alarm verir
10. LLM, log'da olmayan bir satırı "alıntılarsa" ya da hiç alıntı yapmazsa özet reddedilir

## Gerçek kullanım: ilk gece (08.11)

Nöbetçiyi ilk kez gerçek bir işin başına koydum: [ytfactory](https://github.com/emirhnnnk-lab/ytfactory) video ajanının senaryo ve sahne adımı (`ornek/ytfactory_senaryo.json`).

- **15 dk 32 sn** sonra iş sıfırdan farklı bir kodla çıktı. Ajan sahne tarifini 3 denemede de doğrulamadan geçirememişti (bir satırda 3 vurgu vardı, kural en fazla 2'ye izin veriyor). Nöbetçi bunu `OLDU` olarak yakaladı ve yeniden denemek yerine insana sordu. Doğru karardı: aynı girdiyle yeniden denemek aynı sonucu verirdi.
- **Ama nöbetçinin kendi teşhisi zayıftı.** Bu hata tipi imza listesinde yoktu. Nöbetçi sebebi tanımadığı halde "kalıcı görünüyor" yazdı ve kanıt satırı eklemedi. Düzeltme iki parça oldu:
  1. Yeni imza: "model çıktısı doğrulamayı geçemedi". Bu sebep kalıcı sayılır, çünkü düzeltmek için prompt ya da kural değişikliği gerekir.
  2. Tanınmayan bir ölümde nöbetçi artık **bilmediğini söylüyor** ve son log satırını kanıt olarak ekliyor.

  İki durum için test eklendi: 7b ve 7c.
- Asıl hata ytfactory'deydi. Onarım katmanı vurgu sayısını düzeltmiyordu; bu ayrıca düzeltilecek.

## Sınırlar

- Kaldığı yerden devam etmeyi Nöbetçi yapmaz, izlenen işin kendisi yapmalı. Nöbetçi yeniden başlatırken `devam_bayragi` ile bir argüman ekleyebilir, örneğin `--devam`.
- Teşhis imzaları benim karşılaştığım arızalarla sınırlı. Yeni bir arıza tipi `IMZALAR` listesine eklenir.
- Tek makine. Dağıtık işleri izlemez.

---
Geliştiren: **Emirhan Karaca**
