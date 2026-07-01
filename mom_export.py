#!/usr/bin/env python3
"""Convert a MoM .md file to HTML and PDF using the mom-template.html."""
import sys
import re
import subprocess
import tempfile
from pathlib import Path
from datetime import date

import markdown


TEMPLATE = Path(__file__).parent / ".claude/commands/mom-template.html"


def _render_mermaid(md_text: str) -> str:
    """Replace ```mermaid blocks with base64 PNG (requires mmdc). Falls back to styled block.
    PNG avoids the foreignObject/text rendering issue in WeasyPrint."""
    import base64
    pattern = re.compile(r'```mermaid\n(.*?)\n```', re.DOTALL)

    def to_png(match):
        code = match.group(1).strip()
        try:
            with tempfile.NamedTemporaryFile(suffix='.mmd', mode='w', delete=False) as f:
                f.write(code)
                mmd = Path(f.name)
            png_path = mmd.with_suffix('.png')
            r = subprocess.run(
                ['mmdc', '-i', str(mmd), '-o', str(png_path), '-b', 'white', '-s', '2'],
                capture_output=True, timeout=30
            )
            if r.returncode == 0 and png_path.exists():
                b64 = base64.b64encode(png_path.read_bytes()).decode()
                mmd.unlink(missing_ok=True)
                png_path.unlink(missing_ok=True)
                return f'\n<div class="mermaid-diagram"><img src="data:image/png;base64,{b64}" alt="diagram"></div>\n'
            mmd.unlink(missing_ok=True)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return f'\n<div class="mermaid-fallback">{code}</div>\n'

    return pattern.sub(to_png, md_text)


def _detect_lang(md_text: str) -> str:
    has_vi = bool(re.search(r'Biên bản|Tóm tắt|Người tham dự|Quyết định|Hành động tiếp theo', md_text))
    has_en = bool(re.search(r'Minutes of Meeting|Summary|Attendees|Decisions|Action Items', md_text))
    if has_vi and has_en:
        return 'bilingual'
    if has_en:
        return 'en'
    return 'vi'


_FOOTER_LABELS = {
    'vi':        'Tài liệu nội bộ — Biên bản cuộc họp',
    'en':        'Internal Document — Minutes of Meeting',
    'bilingual': 'Biên bản cuộc họp / Minutes of Meeting',
}


def _prerender_bilingual_cols(md_text: str) -> str:
    """Pre-render markdown inside .lang-col divs to HTML.

    md_in_html can't handle nested divs when the outer .bilingual div lacks
    markdown="1", so we handle it ourselves line-by-line.
    """
    result = []
    in_col = False
    label_done = False
    md_lines: list[str] = []

    for line in md_text.split('\n'):
        if not in_col:
            if re.search(r'class="lang-col".*?markdown="1"', line):
                in_col = True
                label_done = False
                md_lines = []
                result.append(re.sub(r'\s*markdown="[^"]*"', '', line))
            else:
                result.append(line)
        else:
            if not label_done and re.search(r'class="lang-label"', line):
                result.append(line)
                label_done = True
            elif line.strip() == '</div>':
                if md_lines:
                    result.append(markdown.markdown(
                        '\n'.join(md_lines).strip(),
                        extensions=["tables", "fenced_code"],
                    ))
                result.append('</div>')
                in_col = False
                md_lines = []
            else:
                md_lines.append(line)

    return '\n'.join(result)


def md_to_html_body(md_text: str) -> str:
    md_text = _prerender_bilingual_cols(md_text)
    return markdown.markdown(md_text, extensions=["tables", "fenced_code"])


def render(md_path: Path) -> tuple[Path, Path]:
    md_text = _render_mermaid(md_path.read_text())
    lang = _detect_lang(md_text)
    body = md_to_html_body(md_text)

    # Style summary section (Vietnamese or English heading)
    body = re.sub(
        r'(<h2>[^<]*(Tóm tắt|Summary)[^<]*</h2>)\s*(<p>.*?</p>)',
        r'\1<div class="summary-box">\3</div>',
        body, flags=re.DOTALL
    )

    template = TEMPLATE.read_text()
    html = (template
            .replace("{{BODY}}", body)
            .replace("{{DATE}}", date.today().isoformat())
            .replace("{{FOOTER_LABEL}}", _FOOTER_LABELS[lang]))

    html_path = md_path.with_suffix(".html")
    html_path.write_text(html)

    pdf_path = md_path.with_suffix(".pdf")
    from weasyprint import HTML
    HTML(string=html, base_url=str(md_path.parent)).write_pdf(str(pdf_path))

    return html_path, pdf_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python mom_export.py <file.md>")
        sys.exit(1)
    md = Path(sys.argv[1])
    html_out, pdf_out = render(md)
    print(f"HTML: {html_out}")
    print(f"PDF:  {pdf_out}")
