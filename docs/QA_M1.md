# Validasi M1 — 0.3.1

Pengujian lokal Linux/Python 3.12: **158 tes lulus**, terdiri dari 139 baseline dan 19 tes penyimpanan. Pengujian lokal ini tidak menggantikan pemeriksaan GitHub Actions Windows/Linux Python 3.10/3.12 pada commit baru.

Cakupan baru: impor offline idempoten, sumber byte-identik, secret tidak diimpor, raw decimal string, record identik/salinan/penerimaan berulang, diagnosis tertanam, original received_at, missing MC tetap null, parse error tersimpan, lineage raw yang hilang, versi sumber baru, sumber dihapus tetap diarsipkan, rollback untuk sumber invalid atau berubah, lock pemilik lain dipertahankan, status read-only, backup/restore dengan file sumber dihapus, integritas BLOB, no-overwrite, database asing/schema masa depan, pemisahan demo, escaping HTML, symlink bila didukung, path traversal, JSON nonfinite, fallback laporan dan integrasi entry point.

Uji terpisah pada salinan workspace legacy mempertahankan jumlah registry/riwayat parsial dan menghasilkan nol penambahan pada impor ulang. Backup dan restored copy mempunyai ringkasan sama; seluruh SHA256 file sumber tetap sama. Data dan identitas wallet pengguna dari uji tersebut tidak dimasukkan ke source/fixture Git.

CI ditambah smoke check impor dua kali, status dan backup pada workspace kosong yang diinisialisasi. Demo tetap terpisah. Remote CI dinyatakan lulus hanya setelah hasil Actions pada commit baru diperiksa.

Tidak ada API berautentikasi, pengujian profitabilitas, validasi event on-chain, atau pembuktian bahwa raw provider lengkap. Dashboard HTML diperiksa melalui tes struktur/escaping; tampilan browser Windows belum diverifikasi pada lingkungan pengembang.
