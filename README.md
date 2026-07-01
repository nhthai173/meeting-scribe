# meeting-scribe

Audio recording → transcript → Biên bản cuộc họp (MD + HTML + PDF).

## Skills

| Skill | Dùng khi |
|-------|----------|
| `/mom <audio>` | Tạo MoM từ file ghi âm (transcribe + viết + export) |
| `/mom-export <file.md>` | Re-export HTML + PDF từ file `.md` đã có (sau khi sửa thủ công) |

**Output** của `/mom` vào `MOM/<tên-file>/`:
```
MOM/meeting/
  meeting.md        ← biên bản (Markdown)
  meeting.html      ← bản HTML
  meeting.pdf       ← bản PDF
  tmp/
    normalized.wav  ← audio đã chuẩn hoá âm lượng
    meeting.txt     ← raw transcript
```

---

## Cài đặt

### 1. FFmpeg (bắt buộc — mọi platform)

| Platform | Lệnh |
|----------|-------|
| macOS | `brew install ffmpeg` |
| Ubuntu/Debian | `sudo apt install ffmpeg` |
| Windows | [ffmpeg.org/download](https://ffmpeg.org/download.html) → thêm vào PATH |

### 2. Node.js + Mermaid CLI (bắt buộc để render diagram trong PDF)

```bash
# macOS
brew install node
npm install -g @mermaid-js/mermaid-cli

# Ubuntu/Debian
sudo apt install nodejs npm
npm install -g @mermaid-js/mermaid-cli

# Windows
# Cài Node.js từ nodejs.org, rồi:
npm install -g @mermaid-js/mermaid-cli
```

> Nếu không cài, diagram trong PDF sẽ hiển thị dạng code block thay vì hình vẽ.

### 3. Python dependencies

```bash
pip install -r requirements.txt
```

**macOS (Apple Silicon M1/M2/M3):**
```
openai-whisper
mlx-whisper
markdown
weasyprint
```

**Linux / Windows — có NVIDIA GPU:**
```
openai-whisper
markdown
weasyprint
```
Cài thêm PyTorch với CUDA (chọn đúng version tại [pytorch.org](https://pytorch.org)):
```bash
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

**Linux / Windows — chỉ CPU:**
```
openai-whisper
markdown
weasyprint
```

### 4. Font tiếng Việt cho PDF (nếu bị lỗi ký tự)

| Platform | Fix |
|----------|-----|
| macOS | Sẵn có, không cần làm gì |
| Ubuntu | `sudo apt install fonts-noto` |
| Windows | Cài [Noto Sans](https://fonts.google.com/noto) từ Google Fonts |

---

## Backend transcription tự động theo platform

| Platform | Thứ tự ưu tiên | Tốc độ |
|----------|----------------|--------|
| macOS Apple Silicon | **MLX → CPU → MPS** | MLX ~4× CPU; MPS chậm hơn CPU với model large |
| Linux / Windows + NVIDIA | **CUDA → CPU** | CUDA ~8–10× CPU |
| Linux / Windows không GPU | **CPU** | baseline |

Override thủ công:
```bash
python transcribe.py audio.m4a --device cpu
python transcribe.py audio.m4a --device cuda
python transcribe.py audio.m4a --device mps
```

---

## Chạy thủ công (không qua skill)

```bash
# Transcribe một file audio
python transcribe.py recording.m4a --language vi --output_dir ./out/

# Re-export HTML + PDF từ file .md đã chỉnh sửa
python mom_export.py MOM/meeting/meeting.md
```

---

## Files

| File | Mục đích |
|------|---------|
| `transcribe.py` | Transcription wrapper đa nền tảng (MLX / CUDA / CPU / MPS) |
| `mom_export.py` | Convert `.md` → `.html` + `.pdf` (có render Mermaid diagram) |
| `.claude/commands/mom.md` | Skill `/mom` |
| `.claude/commands/mom-export.md` | Skill `/mom-export` |
| `.claude/commands/mom-template.html` | HTML/PDF template |
