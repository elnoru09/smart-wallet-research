# M1 — arsip SQLite lokal, versi kode 0.3.1

## Menjalankan

Hentikan collector/sync sebelum impor. Dari checkout yang sudah menjalani import-legacy:

```powershell
py master.py db-import
py master.py db-import
py master.py db-status
py master.py db-dashboard
py master.py db-backup
```

Impor kedua harus menghasilkan `artifacts_added: 0` dan `payloads_added: 0` bila sumber belum berubah. Semua perintah ini offline dan tidak membaca `.env`, membuat Client OKX, atau meminta kredensial. SQLite adalah bagian standard library Python; tidak perlu pip install.

Database aktif: `data/research.sqlite3`. Dashboard: `data/status.html`. Keduanya lokal dan diabaikan Git. Dashboard adalah snapshot HTML dari database, bukan server web. Membuka/refresh HTML tidak mengambil data, mengimpor file atau mengirim notifikasi.

Pengumpul legacy tetap menulis JSON seperti sebelumnya. Setelah pengumpulan, jalankan `db-import` lagi untuk memperbarui snapshot. `db-status` dan `status` tidak mengimpor otomatis. Waktu `last_imported_at` menunjukkan kapan arsip diperbarui; data raw mempertahankan received_at aslinya.

## Arti jumlah dan cakupan

- `wallet_count`, `history_status_counts`, `registry_observations`: registry pada impor terakhir, bukan jumlah baris seluruh laporan yang pernah dibuat.
- `provider_payloads`: kombinasi berbeda provider + chain + wallet + hash isi record raw di seluruh versi arsip. **Bukan jumlah transaksi/fill on-chain unik.** Dua event yang benar-benar berbeda mungkin memiliki payload identik; semua kemunculan baris tetap disimpan.
- `parsed_provider_payloads`: payload yang berhasil diparse pada sedikitnya satu kemunculan.
- `raw_row_occurrences`: semua kemunculan baris pada file/versi/envelope raw yang diarsipkan, termasuk salinan raw dalam diagnosis dan kelanjutan. Impor ulang byte yang sama pada path yang sama tidak menambah kemunculan.
- `legacy_observation_rows`: baris dalam semua versi observations.json lama; replay dapat memuat ulang record yang sama. Jangan menjumlahkannya sebagai observasi baru.
- `legacy_rows_without_raw_payload`: baris lama tanpa kecocokan wallet + hash record raw dalam arsip. Baris tersebut dipertahankan, dengan lineage yang belum lengkap. Kecocokan hash belum membuktikan seluruh field hasil normalisasi lama benar.
- `active_batch`: laporan aktif menurut pointer terbaru, atau fallback project.json. Ini dapat berisi nol observasi ketika total registry tetap lebih besar.
- `accounting_ready: false`, `unique_onchain_events: null`: jumlah dan impor tidak membuktikan saldo, basis biaya, fee, urutan on-chain, hold, win rate, atau posisi sangkut.

Timestamp impor tidak menggantikan observed_at atau received_at. View `payload_provenance` memakai minimum epoch received_at yang tersimpan untuk waktu penerimaan pertama; nilai itu bukan block time atau waktu penemuan wallet. Parser tidak mengubah ordinal provider menjadi ID event.

## Model penyimpanan

Schema SQLite 1 memakai application_id SWR1. Database asing dan schema lebih baru ditolak, tidak diturunkan atau ditimpa. Perubahan parser/skema berikutnya membutuhkan migrasi eksplisit; artefak yang sama tidak diparse ulang diam-diam setelah pembaruan kode.

| Tabel/view | Isi |
|---|---|
| artifacts | Path relatif, SHA256 byte asli, BLOB byte asli, jenis, waktu impor; append-only menurut path + hash |
| current_artifacts | Versi file yang ada pada snapshot impor terbaru |
| import_state | Waktu impor terakhir dan path laporan aktif |
| wallet_snapshots | Sumber, waktu penemuan dan riwayat registry lengkap, termasuk status unknown/partial |
| payloads | Isi record provider sebagai JSON; string angka raw dipertahankan |
| occurrences | Hubungan payload → artefak, envelope, nomor baris mulai 1, endpoint, params, requested_at/received_at, hasil parse/error dan versi parser |
| payload_provenance | Waktu penerimaan pertama dan banyak kemunculan per payload; bukan identitas on-chain |
| legacy_rows | Observasi hasil parser lama, termasuk ID ordinal dan label source_file aslinya |
| reports | Laporan lama beserta audits, error, request window dan status aslinya |

File yang berubah mendapatkan versi baru; versi lama tetap ada. File yang dihapus dari workspace tidak lagi ada di current_artifacts, tetapi buktinya tetap tersimpan dalam arsip. Dengan demikian statistik raw arsip dapat lebih besar dari file yang saat ini ada di runs/.

Importer memilih project.json, watchlist/registry.json, pointer laporan, laporan/observasi dalam runs/, raw JSON dan diagnosis yang dikenali. Folder runs/demo_* tidak dimasukkan. Jangan mengganti nama atau memindahkan data demo ke folder riset nyata. Provider lain dan format raw yang tidak dikenali memerlukan adapter, bukan coercion otomatis. CSV, HTML, exports/, .env dan data/ tidak dimasukkan.

File raw mungkin sebelumnya sudah disanitasi pengumpul. BLOB menyimpan byte file lokal yang diterima importer, bukan klaim bahwa byte tersebut adalah respons HTTP asli tanpa perubahan. Original response hash provider tetap tersimpan di dalam BLOB bila tersedia.

Impor menggunakan satu transaksi SQLite dan mengambil lock yang dipakai writer legacy. Hash dan daftar sumber diperiksa kembali sebelum commit. Kegagalan membatalkan seluruh perubahan data impor; percobaan pertama yang gagal sesudah pembuatan schema dapat meninggalkan database kosong. `db-status` menandainya `DATABASE_EMPTY`. Jangan menghapus lock secara otomatis; periksa dahulu apakah proses pemilik masih aktif.

## Backup dan uji pemulihan

`db-backup` memakai SQLite backup API, memeriksa integrity_check, foreign_key_check dan SHA256 setiap BLOB artefak, kemudian menerbitkan file baru secara eksklusif di `data/backups/`. Ini membuat snapshot database konsisten, termasuk byte sumber yang sudah diarsipkan. Backup dapat memuat alamat wallet dan respons privat; jangan memasukkannya ke Git/public export.

Output memberikan path file. Untuk menguji pemulihan, gunakan path relatif dari output, misalnya:

```powershell
py master.py db-restore --backup "data/backups/research_<id-dari-output>.sqlite3"
```

Restore menghasilkan file baru di `data/restored/`, memverifikasi isi dan **tidak mengganti database aktif**. Perintah ini untuk memeriksa bahwa backup dapat dipulihkan. Penggantian database aktif saat insiden membutuhkan langkah terpisah dengan semua writer dihentikan; belum diotomatisasi. Backup SQLite tidak mencadangkan .env, seluruh workspace atau file yang belum diimpor. Simpan salinan backup pada media terpisah untuk menghadapi kegagalan disk.

Penerbitan file backup memakai hard link eksklusif pada filesystem lokal (NTFS/Linux yang mendukung). File tujuan yang sudah ada tidak ditimpa. SQLite berada pada satu mesin lokal; sinkronisasi file database aktif melalui shared/network folder belum didukung.

## Batas M1

Belum ada scheduler, rolling catch-up, perbaikan koneksi live, adapter GMGN, rekonstruksi posisi, paper portfolio atau transaksi. SQL adalah arsip bukti dan status; JSON masih jalur tulis legacy sampai M2. Dashboard awal memuat seluruh tabel lokal sekaligus dan sesuai untuk volume pilot; pagination/server web masuk tahap dashboard lanjutan.
