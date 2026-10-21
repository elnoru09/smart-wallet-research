# Roadmap sistem

Satu repository, satu workspace lokal, satu backlog. Milestone bukan paket ZIP terpisah.

| Milestone | Hasil | Kriteria selesai | Status |
|---|---|---|---|
| M0 | Fondasi Git, fixture, status awal, importer legacy, CI | Clone kosong dapat dipakai offline; data/secret tidak dilacak; konflik import tidak ditimpa; baseline lulus | Implementasi tersedia pada 0.3.0; hasil CI ditampilkan di Actions |
| M1 | SQLite, lineage, coverage, dashboard status awal | Impor idempoten; raw lama utuh; data partial tetap partial; backup/restore diuji | Implementasi offline 0.3.1; verifikasi CI dan impor laptop pada commit baru diperlukan |
| M2 | Backfill/catch-up terpisah dan scheduler | Checkpoint, gap, restart dan budget diuji; canary live lokal cocok | Direncanakan |
| M3 | Lot/episode, saldo, fee dan profil | Transfer, oversell, missing basis dan partial exit diuji; metrik mempunyai coverage | Direncanakan |
| M4 | Dashboard detail dan notifikasi | Wallet→posisi→bukti dapat ditelusuri; refresh UI tidak memicu backfill; outbox terdeduplikasi | Direncanakan |
| M5 | Paper portfolio prospektif | Universe/aturan/biaya/modal dibekukan; hasil termasuk posisi terbuka, missing dan kegagalan fill | Belum dimulai |

## Arsitektur target

Python melanjutkan kode saat ini. SQLite lokal menyimpan observasi, event, checkpoint dan profil. Satu worker menulis dan menangani antrean. FastAPI dengan antarmuka web sederhana membaca data tersimpan; scheduler tidak diduplikasi pada setiap worker web. Runtime data tidak disertakan dalam repository.

Adapter provider memisahkan OKX, GMGN opsional dan RPC untuk rekonsiliasi. Pengayaan data tidak boleh mengganti received_at asli atau menganggap ordinal provider sebagai identitas on-chain. Saldo saat ini tidak otomatis membuktikan basis pembelian asal.

Di M3, metode FIFO yang terversi akan dibedakan dari baseline average-cost dalam walletlab.py. Hasil dari metode berbeda tidak digabung tanpa label. Unknown tetap unknown; tidak menjadi harga atau basis nol.

## Backlog dekat

1. SW-003: schema SQLite 1 dan verified backup/restore ke file terpisah tersedia pada 0.3.1.
2. SW-004: arsip registry, raw dan observasi lama tersedia; hash payload tidak diklaim sebagai event on-chain.
3. SW-005: dashboard status HTML read-only dari SQLite tersedia; pembaruan lewat db-import.
4. SW-006: job/checkpoint dan budget global.
5. SW-007: adapter OKX untuk backfill/catch-up.

Spike adapter GMGN berjalan setelah kontrak penyimpanan tersedia; tidak menghambat pipeline offline. Ketersediaan akses, auth, kuota dan makna field perlu diuji memakai kredensial lokal. Skor upstream tidak menjadi skor otomatis sistem ini.

M1 mempertahankan jalur tulis legacy JSON. Database adalah arsip snapshot eksplisit; status tidak memicu impor atau HTTP. M2 menghubungkan worker/checkpoint ke penyimpanan ini secara bertahap. Rincian operasi dan batas ada di [SQLITE_M1.md](SQLITE_M1.md).

## Definisi penelitian

MC <6k dan <10k adalah bucket deskriptif. Bedakan circulating MC, FDV dan estimasi. Tampilkan distribusi holding, first sell, full exit, usia sisa lot, profit concentration, realized/unrealized dan posisi belum dapat dinilai.

Posisi sangkut perlu membedakan kerugian sisa posisi dari net hasil seluruh episode yang mungkin sudah untung. Contoh hold 10 hari belum menjadi minimum. Closed-only win rate tidak dipakai sendirian untuk memilih wallet.

Evaluasi memakai waktu saat sistem mengetahui sinyal, bukan sekadar timestamp transaksi. Memilih wallet pemenang hari ini lalu menguji masa lalunya bukan evaluasi prospektif. Banyak wallet pada token sama juga bukan observasi independen.

## Di luar MVP

Eksekusi trading nyata, optimasi bobot genetik, model ML untuk ranking, layanan multiuser/cloud dan seluruh komponen microservice tidak masuk MVP. MVP penelitian tetap dapat selesai ketika hasil eksperimen tidak menemukan keunggulan.
