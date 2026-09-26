# LTFM 3 saatlik tahmin otomasyonu

Bu depo, İstanbul Havalimanı (LTFM) günlük maksimum sıcaklık tahminini GitHub Actions üzerinde üç saatte bir çalıştırır.

Akış:

1. Open-Meteo’dan ECMWF IFS, ICON-EU ve GFS-Seamless saatlik verileri tazelenir.
2. ICON ensemble verisi ayrıca çekilir; erişilemezse deterministik ailelerle devam edilir.
3. `ltfm_engine_v2.py` sabit/deterministik motoru Europe/Istanbul takviminde bugün ve yarın için çalışır.
4. Sonuçlar Actions artifact’ı olarak saklanır ve tek bir GitHub issue’sine yorumlanır.
5. Yorumda `@Yakarca` mention’ı bulunduğu için GitHub hesap bildirimi oluşur.

Zamanlama her üç saatte bir çalışır. Düzeltme modeli her çalıştırmayı İstanbul yerel saatine göre ayırır: 00, 03, 06, 09, 12, 15, 18 ve 21. Elle çalıştırma (`workflow_dispatch`) da açıktır.

## NOAA doğrulaması ve sınırlar

Her tahmin saati kendi NOAA geçmişiyle ayrı eğitilir; örneğin 09:00 modeli yalnızca önceki 09:00 tahminlerini, 18:00 modeli yalnızca önceki 18:00 tahminlerini kullanır. Her çalıştırmada o saate ait son 60 tamamlanmış günle yeniden öğrenip yarının ana sıcaklık tahminine en fazla ±1°C tam derece düzeltme uygular. Tahmin satırı her çalıştırmada kaydedilir; NOAA günü en az 40 rapora ve 23:00 veya daha geç son rapora ulaştığında satırın gerçek değeri geçmişe eklenir. Tamamlanmamış günler eğitime girmez.

Arşivdeki başlangıç verisi yalnızca 09:00 ve 15:00 modellerini ısıtabilir. Diğer altı saat kendi geçmişini canlı çalışmalardan toplar; o saatte 60 tamamlanmış örnek oluşana kadar tahmin kaydedilir ama NOAA nokta düzeltmesi uygulanmaz. Eski 22:00 verisi 21:00 modeliyle karıştırılmaz. Düzeltme olasılık yüzdelerini değiştirmez; bu yüzdeler kalibre edilmiş değildir.

Eski 22:00 saatine ait 171 günlük yürüyen NOAA testi, eski düzeltme için 91/171 tam isabet (%53,22), MAE 0,637°C ve ±1°C içinde %88,89 verdi. Aynı günlerde v2.1 tabanı 89/171 (%52,05), MAE 0,661°C ve ±1°C içinde %87,13 verdi. Bu rakamlar yeni sekiz saatlik düzenin performans testi değildir. Üç gün yalnızca altı saatlik NOAA raporu içerdiği için karşılaştırmaya alınmadı; daha önce görülen 95/174 skor NOAA-only sonucu olarak doğrulanmadı.

Bu test 174 günlük veriyle yapılmış olsa da doğrulanan karşılaştırma 171 tam kayıtlı gündedir; yürüyen test skoru gelecekte aynı artışın süreceğini garanti etmez. Her canlı çalışmanın kaynak snapshot’ı ve sonucu Actions artifact’ında saklanır.

## Dosyalar

- `ltfm_engine_v2.py`: kilitli LTFM D0/D1 motoru.
- `ltfm_candidate95.py`: tahmin saatine göre ayrı NOAA geçmişiyle yürüyen D1 nokta düzeltmesi.
- `scripts/refresh_model_data.py`: canlı model verisini üretir.
- `scripts/run_ltfm.py`: motoru aynı çalışmadaki yerel snapshot’la çalıştırır.
- `.github/workflows/ltfm-prediction.yml`: üç saatlik Actions ve GitHub issue bildirimi.
