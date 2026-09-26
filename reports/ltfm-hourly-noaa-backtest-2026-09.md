# LTFM — 8 tahmin saatinde NOAA walk-forward backtest

- Dönem: 2026-04-02–2026-09-22 (174 issue günü)
- NOAA etiketli D1 hedef günü: 171
- Hedef: issue gününden sonraki yerel günün NOAA maksimum sıcaklığı; etiketler `candidate95_seed.json` içindeki NOAA/NWS LTFM kayıtlarından alındı.
- Tahmin girdileri: her issue saatinden en az 6 saat önceki Open-Meteo Single Runs; canlı issue anı gözlemleri IEM LTFM METAR/SPECI arşivinden.
- Kalibrasyon: her slot için önceki son 60 tamamlanmış aynı-slot günü; ilk 60 gün eğitimde, skor yalnızca sonrasında.
- Düzeltme: mevcut NOAA residual modeli; çıktı en fazla bir tam °C yukarı veya aşağı kaydırılır.
- Bu walk-forward testte tüm tahmin geçmişi aynı arşiv döneminden sırayla üretildi; gelecekteki günler eğitime sızdırılmadı.

| Saat TRT | NOAA gün | Test N | Taban tam | Düzeltilmiş tam | Taban ±1°C | Düzeltilmiş ±1°C | Taban MAE | Düzeltilmiş MAE |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 00:00 | 171 | 109 | 57/109 (52.29%) | 57/109 (52.29%) | 93.58% | 92.66% | 0.569°C | 0.587°C |
| 03:00 | 171 | 109 | 60/109 (55.05%) | 62/109 (56.88%) | 92.66% | 93.58% | 0.569°C | 0.550°C |
| 06:00 | 171 | 109 | 57/109 (52.29%) | 57/109 (52.29%) | 92.66% | 93.58% | 0.606°C | 0.606°C |
| 09:00 | 171 | 110 | 62/110 (56.36%) | 62/110 (56.36%) | 95.45% | 94.55% | 0.518°C | 0.527°C |
| 12:00 | 171 | 110 | 61/110 (55.45%) | 60/110 (54.55%) | 94.55% | 93.64% | 0.536°C | 0.555°C |
| 15:00 | 171 | 109 | 55/109 (50.46%) | 59/109 (54.13%) | 93.58% | 93.58% | 0.596°C | 0.560°C |
| 18:00 | 171 | 109 | 61/109 (55.96%) | 57/109 (52.29%) | 93.58% | 92.66% | 0.541°C | 0.587°C |
| 21:00 | 171 | 110 | 64/110 (58.18%) | 63/110 (57.27%) | 91.82% | 90.00% | 0.545°C | 0.582°C |

## Tamlık ve yorum

Taban ve düzeltilmiş sonuçlar aynı aktif test günlerinde karşılaştırılmıştır. `predictions.csv` her günün taban tahmini, düzeltilmiş tahmini, eğitim örneği sayısı, düzeltme ve NOAA etiketi içerir. `hourly_seed.json` canlı motora saat-eşleşmeli geçmiş olarak aktarılabilecek feature ve NOAA etiketlerini içerir.

Tahmin üretilemeyen issue-slot sayısı: 5. NOAA etiketi olmayan D1 gün sayısı/slot: 3.

## Önemli kaynak ayrımı

NOAA/NWS seed dosyası yalnızca gerçekleşen hedefleri sağlar. Geçmiş tahmin girdileri yeniden Open-Meteo arşivinden üretilmiştir; issue anındaki geçmiş yüzey gözlemleri IEM METAR/SPECI arşivinden alınmıştır. Böylece 8 tahmin saati aynı motor, aynı issue-time cutoff ve aynı NOAA hedef etiketleriyle karşılaştırılmıştır.
