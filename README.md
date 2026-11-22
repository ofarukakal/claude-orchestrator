# Manager skill — Claude Code için Antigravity işçi düzeni

Claude Code'a "manager ol" dediğinde Claude işi kendisi yazmaz: parçalara böler, tmux'ta
Antigravity CLI (`agy`) işçileri açar, sonuçları kendisi kontrol eder.

- **Gemini 4 Hydrogen** — normal işler
- **Gemini 4 Neon** — önemli işler ve Hydrogen'in çözemedikleri
- **Claude** — yalnızca işçilerin çözemediği işler

Büyük veya riskli işlerde üç kontrol rolü de devreye girer: code review, güvenlik incelemesi ve
test yazımı. Hangi modelin hangi rolü aldığı `bench/` klasöründeki ölçüme dayanır; ayrıntı ve
sonuçlar [bench/README.md](bench/README.md) içinde.

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

Script tekrar tekrar çalıştırılabilir; kurulu olanı yeniden kurmaz.

## Senin yapman gerekenler

Script bunları senin yerine yapamaz; gerektiğinde `[you]` satırıyla söyler:

- **Homebrew (yalnızca Mac'te ve kurulu değilse):** Mac parolanı sorduğu için kendin çalıştırmalısın.
  Script komutu ekrana yazar; çalıştırdıktan sonra `bash install.sh` komutunu tekrarla.
- **Google girişi:** Terminal'de bir kez `agy` yaz, tarayıcıda Google hesabınla giriş yap.
  Sonra `bash install.sh` komutunu tekrarla; `RESULT: ready.` yazmalı.
- **Plan:** Hydrogen ve Neon için Google AI Pro, Ultra veya Enterprise planı gerekir.

## Kullanım

Claude Code'u projende aç ve şunlardan birini söyle:

- "manager ol, şu işi işçilere dağıt: …"
- "tmux aç, 3 Hydrogen ile şunları yaptır: …"

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
