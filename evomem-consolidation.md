# Feasibility Study: Konsolidasi Evomem ke `/_self/kb`

## 1. Arsitektur Saat Ini

```
agents/<agent_id>/
├── kb/                          # User-facing, visible di UI
│   └── *.md                     # KB files buatan user/agent
└── brain/                       # Hidden dari UI
    ├── .evomem.db               # SQLite index (derived)
    ├── entities/*.md            # Entity pages (auto-generated)
    ├── notes/mem-*.md           # Memory fact pages (auto-generated)
    └── kb/*.md                  # MIRROR COPY dari agents/<id>/kb/
```

Ada **tiga** hal yang membuat struktur ini rumit:
1. **Duplikasi**: KB file di-copy dari `kb/` → `brain/kb/` setiap sync
2. **Dua direktori terpisah**: Auto-generated content (`entities/`, `notes/`) ada di `brain/`, bukan `kb/`
3. **Invisible**: User tidak bisa melihat hasil evomem di KB UI

## 2. Arsitektur Target

```
agents/<agent_id>/
└── kb/                          # Satu-satunya direktori
    ├── .evomem.db               # SQLite index
    ├── *.md                     # KB files buatan user/agent
    ├── entities/*.md            # Entity pages (auto-generated)
    └── notes/mem-*.md           # Memory fact pages (auto-generated)
```

## 3. Code Path yang Terdampak

| File | Perubahan | Kompleksitas |
|------|-----------|-------------|
| `evomem_client.py:144-146` `_get_brain_dir()` | Return `agents/{id}/kb` (bukan `brain`) | **Trivial** — 1 line |
| `evomem_client.py:149-155` `_get_kb_dir()` | Menjadi identik dengan `_get_brain_dir()` — bisa langsung dihapus/deprecated | **Trivial** |
| `evomem_client.py:158-260` `_mirror_kb_files()` | **HAPUS seluruh fungsi** — tidak diperlukan lagi karena brain dir = kb dir | **Trivial** — removal |
| `evomem_client.py:353-388` `sync()` | Hapus panggilan `_mirror_kb_files()`; sisanya pakai `_get_brain_dir()` yang sudah mengarah ke `kb/` | **Trivial** |
| `evomem_writer.py:70-73` `_brain_path()` | Sudah pakai `_get_brain_dir()` — **tidak perlu diubah** | Nol |
| `evomem_writer.py:76-81` `_ensure_brain()` | Sudah pakai `_get_brain_dir()` — **tidak perlu diubah** | Nol |
| `backend/tools/write_file.py:249` | `'/kb/' in local_path` check — **tidak perlu diubah** (tetap berfungsi) | Nol |
| `backend/tools/str_replace.py:224` | Sama — **tidak perlu diubah** | Nol |
| `backend/tools/patch.py:592` | Sama — **tidak perlu diubah** | Nol |
| `scripts/rebuild_evomem.py` | Pakai `_get_brain_dir()` — **tidak perlu diubah** | Nol |
| **Migration script** | Script baru untuk memigrasi existing agent: pindahkan `.evomem.db`, `entities/`, `notes/` dari `brain/` ke `kb/` | **Medium** |

## 4. Analisis Risiko

### ✅ Aman / Low Risk:
- **`_get_brain_dir()`** adalah **single choke point** — semua path resolution lewat sini. Ubah satu line, seluruh pipeline otomatis konsisten.
- **`_mirror_kb_files()`** bisa dihapus total. Ini ELIMINATES duplikasi, bukan menambah kompleksitas.
- **Tools (`write_file`, `str_replace`, `patch`)** tetap berfungsi karena mereka hanya check `'/kb/' in path` dan panggil `mark_dirty()`.

### ⚠️ Perlu Verifikasi:
- **Evomem binary compatibility**: Binary dijalankan dengan `--brain <dir>`. Dia scan *semua* `.md` di bawah directory itu dan infer `source_dir` dari nama subdirectory. Karena kita tetap pakai subdirectory `entities/` dan `notes/`, binary seharusnya tetap bekerja. Tapi perlu **test**.
- **KB UI pollution**: `entities/` dan `notes/` akan muncul di KB file listing UI. Apakah ini diinginkan? User bilang ingin semuanya di KB, jadi ini sesuai keinginan.
- **`.gitignore`**: `.evomem.db` sekarang di `kb/.evomem.db`. Perlu pastikan tidak ikut ke git. Saat ini `brain/` di-gitignore, jadi perlu tambah pattern spesifik.

### 🔴 Perlu Perhatian Khusus:
- **Migration path**: Agent yang sudah ada dengan `brain/` directory perlu dimigrasi. Ini non-trivial karena:
  - `brain/.evomem.db` → `kb/.evomem.db`
  - `brain/entities/` → `kb/entities/`
  - `brain/notes/` → `kb/notes/`
  - **`brain/kb/`** — ini mirror copy dari `kb/`, jadi bisa diabaikan (source of truth ada di `kb/`)
  - Setelah migrasi, `brain/` directory bisa dihapus
- **Unit tests**: Test seperti `test_kb_sync.py` yang memonkey-patch `_get_brain_dir` dan `_get_kb_dir` perlu diperbarui

## 5. Kesimpulan

**FEASIBLE** ✅ — dengan effort **LOW** (sekitar ~20-30 lines of code changes + migration script).

Core change hanya **1 line** di `_get_brain_dir()` + menghapus `_mirror_kb_files()`. Sisanya mengikuti otomatis karena semua code path resolve melalui fungsi tersebut.

---

*Generated: 2026-06-25*

