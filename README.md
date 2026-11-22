# Manager skill — Claude Code için Antigravity işçi düzeni

Claude Code'a "manager ol" dediğinde Claude işi kendisi yazmaz: parçalara böler, tmux'ta
Antigravity CLI (`agy`) işçileri açar, sonuçları kendisi kontrol eder.

Claude patrondur: işi böler, görevleri yazar, dönen her değişikliği kendisi okur ve testleri
çalıştırır. Kodu yalnızca işçiler çözemediğinde yazar.

| İş | Kim yapar |
|---|---|
| Normal işler | Gemini 4 Hydrogen |
| Önemli işler ve Hydrogen'in çözemedikleri | Gemini 4 Neon |
| Hydrogen'in yazdığının review'u | Gemini 4 Neon |
| Neon'un yazdığının review'u | Claude Opus 5.5 |
| Güvenlik incelemesi | Claude Opus 5.5 (Neon'a verilmez: Gemini filtresi engelliyor) |
| Test yazımı | Gemini 4 Hydrogen |
| İşçilerin çözemediği işler | Claude (yönetici) |

Yazan model kendi işini incelemez. Bu dağılım itibara değil `bench/` klasöründeki ölçümlere
dayanır; sınavlar ve sonuçlar [bench/README.md](bench/README.md) içinde. Google kotası dolunca
review ve güvenlik rolleri Claude modellerine kayar.

## Kurulum

Makinede yalnızca Claude Code olması yeterli; gerisini kurulum scripti halleder.

### En kolayı: Claude Code'a yaptır

Claude Code'u aç, bu deponun linkini ver ve şunu söyle:

> Bu depoyu klonla ve içindeki install scriptini çalıştır.

Claude depoyu indirir, işletim sistemine göre `install.sh` veya `install.ps1` çalıştırır ve
senin yapman gereken bir adım kalırsa söyler.

### Elle

macOS / Linux — depoyu indir, Terminal'de klasöre gir ve çalıştır:

```bash
bash install.sh
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

## Kurulum scripti ne yapar

1. Skill'i `~/.claude/skills/manager` altına kopyalar.
2. Eksikleri kurar:
   - **tmux** — Mac'te Homebrew ile, Windows'ta winget ile `psmux` (tmux karşılığı)
   - **Python 3** — Mac'te Homebrew ile, Windows'ta winget ile
   - **Antigravity CLI (`agy`)** — Google'ın resmi kurulum scripti (Mac/Linux) veya winget (Windows)
3. Google hesabının Hydrogen ve Neon'u görüp görmediğini kontrol eder.
4. İsteğe bağlı iki aracı da kurar ve durumlarını `[opt]` satırıyla bildirir:
   - **Claude Code CLI (`claude`)** — Opus gibi Claude modellerini işçi olarak çalıştırmak için
   - **GitHub CLI (`gh`)** — pull request açmak ve birleştirmek için

   Bunlar eksik olsa da kurulum "hazır" sayılır; yalnızca ilgili işler yapılamaz.

Script tekrar tekrar çalıştırılabilir; kurulu olanı yeniden kurmaz.

## Senin yapman gerekenler

Script bunları senin yerine yapamaz; gerektiğinde `[you]` satırıyla söyler:

- **Homebrew (yalnızca Mac'te ve kurulu değilse):** Mac parolanı sorduğu için kendin çalıştırmalısın.
  Script komutu ekrana yazar; çalıştırdıktan sonra `bash install.sh` komutunu tekrarla.
- **Google girişi:** Terminal'de bir kez `agy` yaz, tarayıcıda Google hesabınla giriş yap.
  Sonra `bash install.sh` komutunu tekrarla; `RESULT: ready.` yazmalı.
- **Plan:** Hydrogen ve Neon için Google AI Pro, Ultra veya Enterprise planı gerekir.
- **Claude girişi (isteğe bağlı):** `claude auth login` yaz, tarayıcıda onayla.
- **GitHub girişi (isteğe bağlı):** `gh auth login --web` yaz; ekrandaki kodu çıkan bağlantıya gir.

## Kullanım

Claude Code'u projende aç ve şunlardan birini söyle:

- "manager ol, şu işi işçilere dağıt: …"
- "tmux aç, 3 Hydrogen ile şunları yaptır: …"

Bir işin tipik akışı: Claude işi dosyalara göre böler (iki işçi aynı dosyaya dokunmaz), görevleri
yazar, işçileri paralel çalıştırır, farkları okur, testleri çalıştırır, önemli değişiklikleri başka
bir modele inceletir ve sana ne yapıldığını, neyin doğrulandığını ve neyin eksik kaldığını raporlar.
Commit, push, pull request ve birleştirme yalnızca sen isteyince yapılır.

İşçileri canlı izlemek için ayrı bir terminalde:

```bash
tmux attach -t orch
```

## Paketin içi

```
install.sh / install.ps1     kurulum
manager/SKILL.md             Claude'un izlediği talimat
manager/scripts/orch.py      işçileri açan, izleyen, sonuçları toplayan araç
manager/scripts/setup.sh     Mac/Linux hazırlık ve kontrol
manager/scripts/setup.ps1    Windows hazırlık ve kontrol
bench/                       rol ölçümü: sınavlar, puanlama aracı, sonuçlar
```

Çalışma kayıtları `~/.claude/manager-runs/` altında tutulur; silmekte sakınca yoktur.
