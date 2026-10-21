# Data contract V0.1

Semua CSV UTF-8, separator koma, header wajib. Unit token sudah dinormalisasi berdasarkan decimals; USD memakai nilai USD pada waktu transaksi, bukan kurs hari ini. Identitas token adalah mint, bukan ticker. Chain MVP: Solana saja.

## wallets.csv

`wallet,source,discovered_at,history_complete`

- Satu baris per wallet. Gabungkan sumber misalnya `OKX|GMGN` setelah deduplikasi.
- `discovered_at`: timestamp ISO-8601 dengan timezone, tanggal kandidat ditemukan oleh pengumpul.
- `history_complete`: `true` hanya jika riwayat wallet-token yang disertakan sudah mencakup basis posisi dari inventory nol; jika tidak, `false`. Ini pernyataan pengimpor, bukan hasil verifikasi program.
- Simpan salinan mentah leaderboard dan alasan pengambilan pada implementasi connector berikutnya.

## trades.csv

`event_id,wallet,token,timestamp,observed_at,sequence,side,quantity,usd_value,fee_usd,entry_mc_usd`

- `event_id`: unik per event; gabungan signature + instruction/event index, bukan signature saja jika satu transaksi berisi banyak event.
- `timestamp`: block time; `observed_at`: waktu data pertama diperoleh. Jangan rekayasa observed_at mundur.
- `sequence`: urutan event untuk wallet-token pada timestamp sama. Idealnya urut slot, transaction index, instruction index lalu dipetakan deterministik. Urutan yang ambigu ditolak.
- `side`: buy/sell/transfer_in/transfer_out.
- `quantity`: jumlah token positif, adjusted decimals.
- `usd_value`: biaya BUY sebelum fee yang dilaporkan terpisah / penerimaan SELL sebelum fee. Hindari double counting jika sumber sudah memasukkan biaya ke amount; normalisasikan dahulu.
- `fee_usd`: seluruh biaya yang dialokasikan ke event, termasuk tip/priority sesuai bukti; transaksi gagal dan biaya lain belum direkonsiliasi oleh MVP.
- `entry_mc_usd`: MC pada event buy, boleh kosong jika tidak tersedia. Tidak boleh mengisi MC sekarang untuk pembelian historis. Gunakan supply point-in-time; perbedaan MC/FDV antar penyedia harus direkonsiliasi di connector. Baris MC unknown tetap ada tetapi tidak digolongkan entry kecil.
- Transfer mengarantina semua histori wallet-token dari perhitungan profit; tidak diasumsikan cost basis nol. Oversell juga mengarantina seluruh histori token.

## quotes.csv

`wallet,token,timestamp,observed_at,quantity,liquidation_value_usd,executable,source`

- Quote hipotetis penjualan **seluruh inventory tersisa** pada timestamp tersebut, penerimaan USD bersih seluruh biaya.
- `quantity` wajib sama dengan inventory yang direkonstruksi; tidak diekstrapolasi linear.
- `executable=true` adalah deklarasi pengimpor tentang quote, bukan jaminan fill atau hasil transaksi nyata. Tidak ada transaksi ditandatangani.
- Jangan menggunakan harga terakhir, harga tertinggi, atau FDV untuk memperkirakan hasil jual penuh tanpa model eksekusi.
- Quote basi, masa depan, belum diketahui pada cutoff, sesudah cutoff, sebelum transaksi terakhir, atau quantity tidak cocok tidak dipakai.
- Tidak adanya quote menghasilkan `OPEN_UNVALUED`, bukan profit nol atau loss 100%.

## Audit provenance yang perlu disimpan saat connector dibuat

Raw response, endpoint, retrieval time, cakupan pagination, source timestamp, decimals, supply dan MC method, fee conventions, serta status missing. Jangan menaruh API key di CSV atau bundle.

## Additional output diagnostics

`primary_entry_spend_fraction` and `secondary_entry_spend_fraction` are fractions of total buy cost actually observed below each MC threshold. Missing MC spend is not silently assigned below the threshold; `mc_cost_coverage` reports coverage. Quote provenance is exported as `quote_timestamp` and `quote_source`.
