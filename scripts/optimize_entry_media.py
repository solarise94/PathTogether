"""Build homepage WebP derivatives; originals and concentric fields stay intact.
Run from any directory: python3 scripts/optimize_entry_media.py
"""
from pathlib import Path
from PIL import Image

MEDIA = Path(__file__).resolve().parents[1] / 'static' / 'entry-media'

def main():
    sources = [MEDIA / 'tcga-session-base.jpg']
    sources += [MEDIA / f'tcga-{region}-{mag}.jpg'
                for region in ('left', 'upper', 'right') for mag in (2, 10, 20, 40)]
    for source in sources + [MEDIA / 'tcga-session-low.jpg']:
        widths = (480, 960) if source.stem == 'tcga-session-low' else (640, 1024)
        for width in widths:
            with Image.open(source) as image:
                image.thumbnail((width, width), Image.Resampling.LANCZOS)
                image.save(MEDIA / f'{source.stem}-{width}.webp',
                           'WEBP', quality=82, method=6)
    for width in (640, 1024):
        before = sum(p.stat().st_size for p in sources)
        after = sum((MEDIA / f'{p.stem}-{width}.webp').stat().st_size for p in sources)
        print(f'{width}px walkthrough: {before:,} -> {after:,} bytes ({1-after/before:.1%} smaller)')

if __name__ == '__main__':
    main()
