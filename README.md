# LTFM 3 saatlik tahmin otomasyonu

Bu depo, İstanbul Havalimanı (LTFM) günlük maksimum sıcaklık tahminini GitHub Actions üzerinde üç saatte bir çalıştırır.

Akış:

1. Open-Meteo’dan ECMWF IFS, ICON-EU ve GFS-Seamless saatlik verileri tazelenir.
2. ICON ensemble verisi ayrıca çekilir; erişilemezse deterministik ailelerle devam edilir.
3. `ltfm_engine_v2.py` sabit/deterministik motoru Europe/Istanbul takviminde bugün ve yarın için çalışır.
4. Sonuçlar Actions artifact’ı olarak saklanır ve tek bir GitHub issue’sine yorumlanır.
5. Yorumda `@Yakarca` mention’ı bulunduğu için GitHub hesap bildirimi oluşur.

Zamanlama her üç saatte bir çalışır; ayrıca NOAA düzeltmesi için her gün 22:00 TRT çalışması vardır. Elle çalıştırma (`workflow_dispatch`) da açıktır.

## NOAA doğrulaması ve sınırlar

22:00 TRT çalışmasında yarının ana sıcaklık derecesine, önceki 60 NOAA etiketinden öğrenen sınırlı bir nokta düzeltmesi uygulanır. NOAA tohum verisi `data/candidate95_seed.json` içindedir; yeni tahminler NOAA günü tamamlandıktan sonra geçmişe eklenir. Düzeltme olasılık yüzdelerini değiştirmez; bu yüzdeler kalibre edilmiş değildir.

Kayıtları yeterli 171 günde yürüyen NOAA testi, düzeltilmiş motor için 91/171 tam isabet (%53,22), MAE 0,637°C ve ±1°C içinde %88,89 verdi. Aynı NOAA günlerinde v2.1 tabanı 89/171 (%52,05), MAE 0,661°C ve ±1°C içinde %87,13 verdi. Üç gün yalnızca altı saatlik NOAA raporu içerdiği için karşılaştırmaya alınmadı. Daha önce görülen 95/174 skor NOAA-only sonucu olarak doğrulanmadı.

Bu test 174 günlük veriyle yapılmış olsa da doğrulanan karşılaştırma 171 tam kayıtlı gündedir; yürüyen test skoru gelecekte aynı artışın süreceğini garanti etmez. Her canlı çalışmanın kaynak snapshot’ı ve sonucu Actions artifact’ında saklanır.

## Dosyalar

- `ltfm_engine_v2.py`: kilitli LTFM D0/D1 motoru.
- `ltfm_candidate95.py`: NOAA geçmişiyle yürüyen D1 nokta düzeltmesi.
- `scripts/refresh_model_data.py`: canlı model verisini üretir.
- `scripts/run_ltfm.py`: motoru aynı çalışmadaki yerel snapshot’la çalıştırır.
- `.github/workflows/ltfm-prediction.yml`: üç saatlik Actions ve GitHub issue bildirimi.
