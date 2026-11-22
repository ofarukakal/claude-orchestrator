# Rol ölçümü

Hangi modelin code review, güvenlik incelemesi ve test yazmada iyi olduğunu, itibara değil kendi
ölçümümüze göre seçmek için.

| Sınav | Ne ölçer | Puan |
|---|---|---|
| `review` | `inventory.py` içine gömülü 12 hatayı bulma (6 fonksiyon bilerek doğru) | bulunan / 12, anahtar dışı bulgu sayısı |
| `security` | Flask uygulamasına gömülü 14 açığı bulma (bazı uçlar bilerek güvenli) | bulunan / 14 |
| `tests` | `billing.py` için test yazma; testler 17 bozuk sürüme karşı çalıştırılır | yakalanan / 17 |
| `field` | `ledger.py`: gerçek bir muhasebe projesinde düzeltilmiş 12 hata türünün yeniden kurulmuş hâli (6 fonksiyon bilerek doğru) | bulunan / 12 |

İşçiler yalnızca `<sınav>/public` klasörünü ve `task.md` dosyasını görür; `key.json` ve
`reference_test.py` gizlidir.

```bash
python bench/bench.py selfcheck
python bench/bench.py start gemini-4-hydrogen-high gemini-4-argon-high claude-opus-5-5
python manager/scripts/orch.py wait <start'ın yazdığı adlar>
python bench/bench.py score <RUN_ID> --extras
```

## Sonuçlar — 22 Kasım 2026

| Model | Review (12) | Güvenlik (14) | Test (17) | Süre (sn) |
|---|---|---|---|---|
| Gemini 4 Hydrogen high | 12 (+3 gömülmemiş gerçek hata) | 14 | 17 | 107 / 85 / 107 |
| Gemini 4 Argon high | 12 | 14 | 17 | 149 / 269 / 198 |
| Gemini 4 Neon high | 12 | **engellendi** (2 denemede 2) | 17 | 207 / — / 276 |
| Gemini 3.8 Flash high | 12 | 13 | 16 | 305 / 602 / 311 |
| Claude Opus 5.5 | 12 (+1 gömülmemiş gerçek hata) | 14, 14 | 17 | 51 / 53 / 79 |
| Claude Sonnet 5.5 | 12, 12 | 14, 12 | 17 | 21 / 27 / 32 |
| Claude Haiku 5.5 | 12, 11 | 11, 11 | 17 | 44 / 41 / 53 |

İki sayı olan hücreler iki ayrı koşudur.

Ne söylüyor, ne söylemiyor:

- **Sınavlar üst modelleri ayırmıyor.** Çoğu tam puan aldı; aradaki 1-2 puanlık farklar koşudan
  koşuya değişiyor (Sonnet güvenlikte bir kez 14, bir kez 12 aldı). Her model için bir-iki koşu var.
- **Kesin olan:** Neon güvenlik incelemesinde Gemini'nin filtresine takılıyor; Flash 3.8 her sınavda
  en yavaş ve en düşük; Haiku güvenlikte iki koşuda da 11.
- **Yanlış alarm yok.** Anahtar dışı bulguların hepsi elle incelendi; hepsi gerçek hataydı
  (ör. `reserve_stock` bilinmeyen ürün için 0 adet isteğinde `True` dönüyor).

### `field` sınavı — gerçek projeden hata türleri

Hatalar gerçek bir projenin geçmişinden alındı (tutar çözümleme, dönem doğrulama, saat dilimi,
borcun çift sayılması, uydurma tutar, eksik yetki denetimi, Türkçe arama vb.); kod ve veriler
uydurma, projeden hiçbir satır kopyalanmadı.

| Model | 1. tur | 2. tur | Süre (sn) |
|---|---|---|---|
| Gemini 4 Hydrogen high | 12/12 | 12/12 | 100 / 89 |
| Gemini 4 Argon high | 12/12 | 12/12 | 181 / 145 |
| Gemini 4 Neon high | 12/12 | 12/12 | 254 / 212 |
| Claude Opus 5.5 | 12/12 | 12/12 | 38 / 39 |
| Claude Sonnet 5.5 | 12/12 | 12/12 | 22 / 20 |
| Claude Haiku 5.5 | 12/12 | 12/12 | 42 / 43 |

Hiçbirinde anahtar dışı bulgu yok. **Sonuç: bu biçimdeki sınav doymuş durumda.** Tek dosya, her
fonksiyonun üstünde açık bir tanım ve birbirinden bağımsız hatalar olduğunda bu modellerin hepsi
hepsini buluyor; hatanın gerçek projeden gelmesi bunu değiştirmedi. Modelleri ayırmak için hatanın
birkaç dosyaya yayıldığı ve yazılı bir tanımın olmadığı koşullar gerekir (ör. gerçek depoda,
düzeltmeden önceki commit'in incelenmesi).
