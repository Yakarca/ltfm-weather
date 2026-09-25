# LTFM 3 saatlik tahmin otomasyonu

Bu depo, İstanbul Havalimanı (LTFM) günlük maksimum sıcaklık tahminini GitHub Actions üzerinde üç saatte bir çalıştırır.

Akış:

1. Open-Meteo’dan ECMWF IFS, ICON-EU ve GFS-Seamless saatlik verileri tazelenir.
2. ICON ensemble verisi ayrıca çekilir; erişilemezse deterministik ailelerle devam edilir.
3. `ltfm_engine_v2.py` sabit/deterministik motoru Europe/Istanbul takviminde bugün ve yarın için çalışır.
4. Sonuçlar Actions artifact’ı olarak saklanır ve tek bir GitHub issue’sine yorumlanır.
5. Yorumda `@Yakarca` mention’ı bulunduğu için GitHub hesap bildirimi oluşur.

Zamanlama GitHub Actions cron ifadesiyle `0 */3 * * *` şeklindedir. Ayrıca elle çalıştırma (`workflow_dispatch`) açıktır.

## Önemli sınır

Motorun olasılıkları geçmiş LTFM sonuçlarıyla tam kalibre edilmiş bir başarı garantisi değildir. 174 günlük araştırmadaki hedef içi skor ile canlı, ileriye dönük doğruluk aynı şey değildir; workflow her çalışmanın kaynak snapshot’ını ve sonucu artifact olarak saklar.

## Dosyalar

- `ltfm_engine_v2.py`: kilitli LTFM D0/D1 motoru.
- `scripts/refresh_model_data.py`: canlı model verisini üretir.
- `scripts/run_ltfm.py`: motoru aynı çalışmadaki yerel snapshot’la çalıştırır.
- `.github/workflows/ltfm-prediction.yml`: üç saatlik Actions ve GitHub issue bildirimi.
