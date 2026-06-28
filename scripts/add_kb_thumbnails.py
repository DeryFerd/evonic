#!/usr/bin/env python3
"""One-time migration: add `thumbnail` frontmatter to KB docs that contain images.

Scans agents/*/kb/**/*.md for the first markdown image `![...](url)` in the body.
If found and no `thumbnail:` already in frontmatter, injects `thumbnail: <url>`.
"""

import glob
import os
import re
import sys

IMG_RE = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')

def process_file(path: str, dry_run: bool = False) -> bool:
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    if not content.startswith('---'):
        return False

    second = content.find('---', 3)
    if second == -1:
        return False

    fm = content[3:second]
    body = content[second + 3:]

    if 'thumbnail:' in fm:
        return False

    m = IMG_RE.search(body)
    if not m:
        return False

    url = m.group(1)
    new_fm = fm.rstrip('\n') + f'\nthumbnail: "{url}"\n'
    new_content = '---' + new_fm + '---' + body

    if dry_run:
        print(f"  [dry-run] {path} → {url}")
    else:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"  [updated] {path} → {url}")
    return True


def main():
    dry_run = '--dry-run' in sys.argv
    base = os.path.join(os.path.dirname(__file__), '..', 'agents')
    base = os.path.abspath(base)

    files = glob.glob(os.path.join(base, '**', 'kb', '**', '*.md'), recursive=True)
    updated = 0
    for path in sorted(files):
        if process_file(path, dry_run=dry_run):
            updated += 1

    print(f"\n{'[dry-run] ' if dry_run else ''}Done: {updated}/{len(files)} files updated.")


if __name__ == '__main__':
    main()
