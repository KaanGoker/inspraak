# Inspraak brand assets

Idea: a speech bubble (having a say) filled with a hemicycle seating plan of dots. One amber dot is the voice that gets heard. No EU flag, stars or institutional branding.

## Colours (from inspraak/web/static/style.css)

- Ink `#1c2431` (wordmark), body grey `#3a4352`
- Accent blue `#1f4e8c` (bubble, bar, byline), `#163a69` dark blue
- Paper `#f6f5f1` (banner background, dots)
- Amber `#d7a93b` (the single highlighted dot)

Type: wordmark in a bold serif (DejaVu Serif Bold, matches the site's serif headings), small text in DejaVu Sans. All text in the SVGs is converted to outlines, so no font is needed to display them.

## How each file was made

- Mark: gemini-3.1-pro-high via agy, two rounds. Round one (dashed arcs) looked like a wifi icon and was rejected; round two (bubble with three dot rows, one amber) was kept. Two other first-round variants (gemini-3.8-flash-high for a ballot-box idea, a quotation badge from 3.1-pro) were weaker or empty. agy lists no image model, so the art is SVG code.
- Layout, wordmark, banners: a local Python script (fontTools outlines the text) composed the agy mark with the text. Rendered to PNG with rsvg-convert.
- `mark.svg`: mark alone, 128x128. `favicon.png` 32 px and `logo-512.png` are renders of it.
- `logo.svg`: mark plus wordmark, transparent. The wordmark turns light under a dark colour scheme (CSS media query), so it works on light and dark GitHub.
- `banner.png` 1280x640, `og.png` 1200x630, `README-header.svg` and `README-header.png` 1200x300. Paper background, so they look the same on light and dark pages.

## Where they go

- README top: `README-header.svg` (or `.png`), then the existing screenshot. Use `logo.svg` if a transparent logo is preferred.
- Site: `favicon.png` (and `mark.svg` as an svg icon) as favicon; `og.png` as `og:image` and `twitter:image`.
- GitHub: upload `banner.png` under Settings, Social preview.
