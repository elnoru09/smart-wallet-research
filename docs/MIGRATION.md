# Migrasi sekali: folder master ke checkout Git

Tujuan: mempertahankan data dan kredensial lokal, sambil memindahkan pembaruan kode ke Git. Folder master lama tetap utuh sebagai cadangan. Hentikan semua worker/menu pengumpulan sebelum import; jangan mengimpor workspace yang sedang berubah.

## Contoh Windows

Jika master lama berada di `E:\Download\Smart_Wallet_Research_Master`, buat checkout Git baru sebagai folder saudara:

```powershell
cd E:\Download
git clone https://github.com/elnoru09/smart-wallet-research.git smart-wallet-research
cd .\smart-wallet-research
py master.py import-legacy --source "..\Smart_Wallet_Research_Master" --include-env
py master.py status
```

Checkout baru `E:\Download\smart-wallet-research` menjadi folder kerja tetap. Jangan memilih folder yang sudah berisi checkout lain sebagai target clone. Git dan Python harus sudah tersedia. Pada PowerShell yang hanya memiliki `python`, ganti `py` dengan `python`.

**Import dahulu, baru init bila diperlukan.** `init` membuat registry kosong pada checkout baru. Registry kosong tersebut akan berkonflik dengan registry lama yang berisi data; importer akan berhenti dan tidak menggantinya. Jika sudah telanjur init, pindahkan checkout itu sebagai cadangan dan mulai dari checkout baru, atau review konflik secara manual. Tidak ada perintah penghapusan otomatis.

## Apa yang disalin

- Berkas dalam `runs/`, `watchlist/`, `exports/`, dan `data/`.
- `.env` hanya dengan `--include-env`, tanpa menampilkan nilai atau hash kredensial.
- Isi sumber dan timestamp yang tertanam pada raw tidak diubah.
- Kode Python, source Git, dan `project.json` lama tidak disalin.
- Lock, temporary file, `__pycache__` dan `.git` tidak disalin.

Importer memeriksa kesesuaian pointer path dalam project.json. Jika master lama memakai path yang dikustomisasi, `CUSTOM_PROJECT_PATHS` meminta review, bukan mengabaikan konfigurasi tersebut.

## Pemeriksaan tanpa menyalin

```powershell
py master.py import-legacy --source "..\Smart_Wallet_Research_Master" --include-env --dry-run
```

`files_to_copy` menunjukkan berkas baru yang akan disalin. `identical_files_skipped` adalah berkas dengan byte yang sudah sama. `env_included` hanya boolean. Dry-run tidak membuat receipt atau runtime folder.

## Konflik dan pemulihan

Seluruh konflik awal diperiksa sebelum menyalin. Berkas tujuan yang berbeda tidak ditimpa. Sumber/tujuan yang tumpang tindih, symlink dan lock aktif ditolak. Publikasi setiap file menggunakan hard link atomik dari file sementara yang telah diverifikasi; gunakan filesystem lokal yang mendukung hard link, misalnya NTFS. Jika filesystem tidak mendukungnya, proses berhenti tanpa fallback yang menimpa file.

Import beberapa file bukan satu transaksi filesystem global. Jika proses mati setelah beberapa file berhasil disalin, jalankan lagi ketika kedua workspace berhenti. File identik dilewati, file baru dilanjutkan, konflik tetap perlu direview. Source tidak dihapus. Receipt lokal ada di `data/migration_receipts/` dan tidak memuat nilai kredensial.

Sesudah import, jalankan `status` dan periksa registry serta laporan. Jumlah pada laporan batch terakhir bisa nol meskipun registry menyimpan observasi dari batch terdahulu; kedua cakupan itu sengaja berbeda. M0 tidak mengubah data observasi menjadi hasil akuntansi terverifikasi.

## Pembaruan rutin

Hentikan worker, periksa perubahan lokal, lalu gunakan `git pull --ff-only` pada checkout yang sama. Baca changelog/roadmap untuk migrasi schema ketika nanti SQLite ditambahkan. Kode M0 tidak otomatis menarik source baru saat pengumpulan sedang berjalan.
