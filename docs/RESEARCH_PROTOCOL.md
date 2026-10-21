# Rancangan penelitian V0.1 — belum protokol trading final

Pertanyaan: apakah wallet yang memiliki histori entry pada MC <10k menghasilkan hasil pengikut positif setelah biaya dan keterlambatan? Pertanyaan kedua yang terpisah: apakah posisi sangkut wallet dengan perilaku tersebut memiliki pola pemulihan yang bisa digunakan ke depan?

## Pengaman yang sudah diimplementasikan

1. Timestamp event dan waktu data diketahui diperiksa terhadap cutoff.
2. Tidak mengoptimasi wallet/hold/win-rate/TP/SL. Tidak ada skor AI atau peringkat profit otomatis.
3. Closed-only win rate diberi nama eksplisit; posisi terbuka/unvalued/unresolved tetap dilaporkan.
4. Basis transfer tidak dianggap gratis. Oversell dan declared-incomplete history dikarantina.
5. Quote posisi penuh, batas umur quote, fee, average cost, reentry episode, dan diagnostik token residual.
6. Hash konfigurasi/input/report/source dan candidate discovery time. Registry create-only, output lama tidak ditimpa.
7. Data contoh diberi label sintetis pada input dan laporan.

## Aturan sebelum uji prospective

- Tentukan satu kebijakan pemilihan kandidat; sumber, jumlah, urutan pengambilan dan alasan inklusi harus dicatat. Leaderboard saat ini tidak menjadi universe historis yang point-in-time.
- Data sejarah untuk eksplorasi, pilihan kandidat untuk uji masa depan. Seluruh konfigurasi yang pernah dicoba masuk experiment registry; jangan hanya simpan pemenang.
- Tentukan satu entry dan satu exit utama, cap waktu observasi, delay, fee model dan total modal. Posisi yang belum matang tidak dilabeli win/loss sebelum waktunya.
- Menilai hanya trade yang kelak ditahan >10 hari akan membocorkan informasi masa depan jika dijadikan syarat entry.
- Posisi crossing batas train/test jangan menyumbangkan outcome masa depan ke pemilihan di masa lalu. Hanya outcome yang sudah diketahui saat cutoff boleh dipakai memilih.
- Kandidat tidak diganti ketika merugi di tengah cohort. Revisi adalah versi eksperimen baru; hasil lama tetap disimpan.
- Analisis <6k, durasi kelompok, skenario delay, biaya, dan tanpa jackpot dilabeli tambahan/ketahanan, bukan dipilih sebagai pemenang sesudah melihat hasil.
- Token sama lintas wallet tidak dianggap observasi independen. Interval ketidakpastian berikutnya perlu memperhitungkan pengelompokan token dan waktu.
- Tambahkan pembanding dengan MC/umur token/likuiditas yang serupa, menggunakan kriteria yang diketahui saat entry dan kebijakan fill/exit yang sama.
- Laporan akhir meliputi profit bersih portofolio termasuk posisi terbuka, modal terpakai, drawdown, cash availability, biaya data, failure rate dan kualitas quote. Belum dihitung oleh V0.1.

## Referensi desain

- Bailey dkk., The Probability of Backtest Overfitting: https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf
- Bailey & Lopez de Prado, The Deflated Sharpe Ratio: https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
- OKX leaderboard: https://web3.okx.com/onchainos/dev-docs/market/market-signal-leaderboard-list
- OKX portfolio overview: https://web3.okx.com/onchainos/dev-docs/market/market-portfolio-overview
- GMGN wallet details: https://docs.gmgn.ai/index/wallet-detail-page
- Jupiter slippage: https://developers.jup.ag/docs/swap/advanced/slippage

Referensi ditinjau dalam pembahasan 20 Oktober 2026. Ketersediaan API, akses, cakupan dan biaya harus diverifikasi pada tahap connector. Tidak ada klaim validasi keuntungan dari sumber tersebut.
