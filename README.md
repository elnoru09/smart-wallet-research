# Smart Wallet Research

Riset perilaku wallet Solana: entry low cap, holding, posisi terbuka/rugi, dan kelayakan mengikuti wallet setelah biaya. **Research only. Eksekusi transaksi nyata nonaktif.**

Versi kode **0.3.0 — M0: fondasi Git dan workspace lokal**. Kode dan fixture sintetis ada di Git. Kredensial, watchlist dan hasil pengumpulan tetap lokal.

## Mulai dari clone baru

Prasyarat: Git dan Python 3.10 atau lebih baru. Python 3.12 direkomendasikan untuk lingkungan pengguna yang sudah ada. Versi ini memakai standard library; belum memerlukan instalasi dependensi tambahan.

**Jika sudah memiliki master lama, gunakan bagian migrasi terlebih dahulu sebelum mencoba init.**

```powershell
git clone https://github.com/elnoru09/smart-wallet-research.git
cd smart-wallet-research
py master.py init
py master.py status
```

Workspace baru mempunyai **0 kandidat dan 0 observasi**. Tidak ada data riset pengguna yang dibundel. `status` tidak mengirim request API. Launcher Windows: klik dua kali `MULAI_DI_SINI.cmd`.

## Sudah memiliki folder master lama?

Hentikan proses pengumpulan lama dan ikuti [migrasi sekali dari master lama](docs/MIGRATION.md). Importer menyalin runtime data yang diizinkan, mempertahankan isi sumber, dan menolak konflik sebelum mulai menyalin. File kode/project.json lama tidak mengganti kode baru.

```powershell
py master.py import-legacy --source "E:\Download\Smart_Wallet_Research_Master" --include-env
py master.py status
```

Path di atas contoh lokasi lama. Folder sumber dan checkout Git harus terpisah. `.env` hanya disalin jika `--include-env` diberikan; nilai berbeda yang sudah ada di tujuan tidak ditimpa. Jangan menjalankan `init` pada checkout tujuan sebelum import jika ingin mempertahankan registry lama.

Setelah migrasi sekali, gunakan checkout Git ini sebagai folder kerja tetap. Pembaruan berikutnya memakai Git, tanpa menimpa folder menggunakan ZIP.

## Demo offline

```powershell
py master.py demo
```

Demo menyimpan laporan sintetis terpisah di `runs/demo_<id>/`. Output mencantumkan `network_requests: 0`. Demo tidak menambah registry atau mengganti pointer laporan riset aktif. Buka path `report_path` pada output untuk melihat contoh.

## Perintah saat ini

| Perintah | Kegunaan |
|---|---|
| `py master.py init` | Membuat workspace dan registry kosong, atau memakai data yang sudah ada; offline |
| `py master.py status` | Status laporan aktif dan ringkasan seluruh registry; offline |
| `py master.py wallets` | Membuka daftar kandidat lokal; offline |
| `py master.py report` | Membuka laporan riset aktif jika tersedia |
| `py master.py demo` | Contoh sintetis terpisah; offline |
| `py master.py import-legacy --source <folder>` | Menyalin runtime data lama tanpa mengganti konflik |
| `py master.py add-wallet` | Menambah alamat manual |
| `py master.py discover-wallets` | Discovery OKX terbatas maksimal dua request; memerlukan kredensial |
| `py master.py sync --max-requests 10` | Sesi history legacy, maksimal sepuluh attempt; memerlukan window sumber lama |
| `py master.py export-review` | Ekspor audit lokal bila diperlukan; bukan cara memperbarui kode |

Sinkronisasi legacy mempertahankan window historis dari run sumber, satu halaman per wallet dan checkpoint per wallet. Ini **belum pemantauan transaksi terbaru**. Pada checkout baru yang belum mengimpor run sumber, pengambilan history berhenti dengan `HISTORY_WINDOW_REQUIRED` sebelum mengirim request. Discovery dan demo dapat dipakai tanpa run sumber itu.

## Kredensial lokal

Salin `.env.example` menjadi `.env` hanya jika belum ada, kemudian isi tiga nilai OKX di laptop sendiri. Jangan mengirim isinya melalui chat atau memasukkannya ke Git.

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Environment variable yang tidak kosong memiliki prioritas atas `.env`. File `.env` dibaca di sebelah `okx_reader.py`, bukan berdasarkan lokasi terminal. Nilai tidak dieksekusi atau diekspansi. TLS verification tetap aktif.

## Pengujian dan pembaruan

```powershell
py -m unittest discover -s tests -q
py scripts/check_repository.py
```

GitHub Actions menjalankan tes pada Linux dan Windows dengan Python 3.10 dan 3.12. Seluruh tes menggunakan fixture/mock dan tidak memerlukan kredensial API. Hentikan worker sebelum memperbarui kode:

```powershell
git status --short
git pull --ff-only
py -m unittest discover -s tests -q
py master.py status
```

Jika ada edit source lokal atau branch divergen, review dahulu; jangan melakukan reset hard. Data `runs/`, `watchlist/`, `exports/`, `data/` dan `.env` diabaikan Git. Git ignore tidak menghapus file yang telanjur dilacak; gunakan pemeriksaan repository sebelum commit.

## Peta pengembangan

[Roadmap](docs/ROADMAP.md) mengarahkan M1 penyimpanan SQLite, M2 backfill/catch-up, M3 rekonstruksi posisi, M4 dashboard/notifikasi, dan M5 paper evaluation. Dashboard web, integrasi GMGN, dan paper portfolio **belum diimplementasikan** pada M0.

Minimum hold dan minimum win rate tetap tidak ditetapkan. Contoh 10 hari bukan aturan final. Anggaran US$20/token adalah rencana simulasi; proporsi 20% dan total modal perlu didefinisikan sebelum eksperimen portofolio. Data parsial tidak digunakan untuk menyatakan win rate atau posisi sangkut terverifikasi.

Lihat [protokol penelitian](docs/RESEARCH_PROTOCOL.md), [kontrak data](docs/DATA_CONTRACT.md), [kurasi sumber](docs/UPSTREAM_REFERENCES.md), dan [validasi M0](docs/QA_M0.md).
