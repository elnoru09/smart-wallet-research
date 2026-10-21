# Referensi arsitektur dan batas adopsi

Ditelaah pada 21 Oktober 2026. Ini referensi desain; source awal repository ini tidak mem-vendor kode dari repo berikut. Penyalinan kode selanjutnya perlu menyertakan lisensi dan atribusi yang sesuai.

| Repo | Snapshot | Pemakaian yang direncanakan |
|---|---|---|
| [GMGNAI/gmgn-skills](https://github.com/GMGNAI/gmgn-skills) | 8422988cce6aedb881d75923b7f2959522426620 | Kontrak konektor dan aktivitas/portofolio; skor heuristik tidak diadopsi langsung |
| [solana-foundation/trading-portfolio](https://github.com/solana-foundation/trading-portfolio) | f0454d60839bdead1a421f9f874f75171777599d | Cakupan, harga hilang, cost-basis coverage dan tes |
| [anchoys1337/solana-wallet-behavior-analysis](https://github.com/anchoys1337/solana-wallet-behavior-analysis) | 3e65c87efe9d5152ab6613564c2f238f0ae7df7f | Catch-up, balance delta, state dan freshness |
| [DoradoDevs/solana-wallet-pnl-tracker](https://github.com/DoradoDevs/solana-wallet-pnl-tracker) | 1aaeda86199f8d39f3249f1818683e487154e4aa | Pemisahan indexer, antrean, posisi dan dashboard; missing basis perlu koreksi |
| [cxcx-ai/solana-dex-parser](https://github.com/cxcx-ai/solana-dex-parser) | 0c67d8ffa78c7fb2e80230ec53d0627c9531566a | Opsi parser lanjutan; perlu fixture protokol terkini |

GMGN, anchoys, Dorado dan cxcx menampilkan MIT; trading-portfolio Apache-2.0 pada snapshot yang dibaca. Dune Spellbook juga dipelajari untuk granularitas event, namun LICENSE yang dibaca adalah Business Source License 1.1, bukan MIT. Repo publik tidak otomatis bebas disalin dengan lisensi apa pun.

Pemeriksaan kode menemukan contoh missing basis yang diperlakukan sebagai nol dan estimasi copy return yang tidak setara dengan replay fill. Karena itu setiap adapter/algoritme harus lolos kontrak internal dan edge-case tests. Popularitas repo atau hasil eksperimen yang dilaporkan penulis tidak menjadi bukti strategi proyek ini.
