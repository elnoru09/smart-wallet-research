# Validasi M0 — versi 0.3.0

## Pemeriksaan lokal

`python -m unittest discover -s tests -q`: **139 tes lulus**, terdiri dari 125 tes baseline dan 14 tes workspace/migrasi. Pemeriksaan lokal dilakukan pada Linux dengan Python 3.12; ini tidak disamakan dengan pengujian pada laptop Windows pengguna.

Tes tambahan mencakup status kosong tanpa request, init offline berulang, pemisahan demo dari registry/pointer aktif, window history yang belum tersedia, import idempoten, konflik sebelum copy, perlindungan .env, dry-run, overlap folder, custom project paths, active lock, symlink jika didukung, dan perubahan sumber saat copy.

Uji integrasi terpisah memakai salinan runtime legacy lokal: jumlah registry/laporan dipertahankan, cakupan laporan batch tetap berbeda dari total registry, pengulangan tidak menyalin file yang sama, dan hash file sumber tidak berubah. Data, jumlah privat, dan identitas wallet dari uji tersebut tidak dibundel dalam repository.

## Pemeriksaan source

Repository hanya memuat kode, konfigurasi tanpa kredensial, dokumentasi dan contoh sintetis. `scripts/check_repository.py` memeriksa path file yang dilacak serta nilai kosong dalam .env.example. Pemeriksaan ini bukan pengganti review isi source atau secret scanning.

Fresh-checkout smoke checks menjalankan status, init dan demo tanpa API. GitHub Actions mengulang tes pada Ubuntu/Windows dengan Python 3.10 dan 3.12. Hasil remote aktual ada pada tab Actions; keberadaan workflow saja tidak membuktikan bahwa semua job telah lulus.

## Batas

Belum ada pengujian API berautentikasi di lingkungan pengembang, live GMGN, dashboard web baru, monitoring terus-menerus, SQLite, rekonsiliasi posisi nyata, atau paper portfolio. Semua itu mengikuti milestone berikutnya. Symlink test dapat dilewati pada filesystem yang tidak mengizinkan pembuatan symlink.

Importer membutuhkan filesystem lokal dengan hard link. Copy multi-file bukan transaksi global: setelah interupsi, file yang sudah selesai tetap ada dan import ulang melanjutkan dengan pemeriksaan konflik.
