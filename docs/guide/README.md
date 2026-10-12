# Maintaining the field guide

Read this when changing the documentation or any behavior it describes. It is
the shared maintenance guide for human contributors and coding agents.

The first set covers the overview, flashing, first boot, device connections,
mesh behavior, and voice. Full existing references are included without manual
copying. Further topics can be added to `content/` and `topics.json` together.

## Where to edit

- `content/*.md`: one topic per file, with `## Marine`, `## User`, and
  `## Developer`, in that order. Keep all three in the same change.
- `topics.json`: topic order, titles, and the explicit list of existing
  reference documents included in the site.
- `assets/`: illustrations, saved image prompts, CSS, and reading-level logic.
- `template.html` and `build.py`: shared presentation and Markdown build.
- `_site/`: generated preview only. Never edit or commit it.

## When behavior changes

1. Check the implementation and current evidence. Handoffs explain history;
   they are not proof of current behavior. Distinguish source behavior from a
   published release and hardware-verified behavior.
2. Update the existing canonical README or internals document for the area.
   Find matching guide topics using `rg` in `content/` and their source links.
3. Update every affected tier. Marine explains the action with drawings and
   very short captions. User gives steps, expected results, and recovery.
   Developer explains mechanisms, files, invariants, and failure cases, with
   links to the full reference. The tiers must agree on requirements.
4. Check the pictures too: connectors, indicators, step order, and settings
   must match the text. Replace outdated pictures rather than explaining
   their errors in a caption. Add useful alt text.
5. Build/check below. For presentation or navigation changes, inspect desktop
   and phone widths, all tiers, refresh, Back, and a link to a selected tier.

Keep public documentation factual. Do not copy private handoffs, session
diaries, bench credentials, or collaboration transcripts into it. The builder
publishes only the topics, the listed references, and the assets they use.

## Marine typography

On Marine topic pages, all text uses Comic Sans, including navigation, headings,
and inline code. User, Developer, and full reference pages keep their standard
fonts. The Marine section also keeps this font when JavaScript is disabled.

Devices without Comic Sans use the bundled Comic Neue regular and bold fonts
from [Google Fonts](https://github.com/google/fonts/tree/main/ofl/comicneue).
Keep `assets/fonts/OFL.txt` with these font files; the builder includes them so
the fallback works offline without a font service.

## Drawing style

Use image generation for wax-crayon drawings that look made by a second grader.
`assets/first-boot-crayon.png` is the clearest/cleanest end of the style; other
drawings can be a little rougher. Use uneven boxes, scribbled fills, crooked
arrows, and occasional backwards letters/numerals. Keep the actual action
recognizable. The green two-antenna radio is a schematic symbol, not a wiring
diagram for a particular board.

Keep important instructions, URLs, passwords, and configuration values in
Markdown, not inside artwork. Keep a consistent radio design and cable colors
using the first-boot image as a reference. Save the exact prompt in
`assets/prompts.json`. Review the output before using it. New alternatives get
a new filename until the preferred image is selected.

## Build and preview

Requires Python 3.10+ and Python-Markdown. If it is not installed, use a local
virtual environment and `python -m pip install -r docs/guide/requirements.txt`.

From the repository root:

```bash
python3 docs/guide/build.py --check
python3 docs/guide/build.py
```

Open `docs/guide/_site/index.html` in a browser. No server or internet is needed
for the generated documentation and illustrations. Source links, downloads,
and the radio dashboard links still need their respective destinations.

For HTTP preview:

```bash
python3 -m http.server 8000 --bind 127.0.0.1 --directory docs/guide/_site
```

Visit `http://127.0.0.1:8000/first-boot.html?tier=marine`. Valid tiers are
`marine`, `user`, and `developer`; the default is User. Explicit URL choices
override the stored preference. With JavaScript disabled, all levels are shown.

## GitHub Pages

The `Documentation` workflow checks pull requests and publishes documentation
changes pushed to `main`. Manual runs on `main` also publish. Its deploy job
publishes only `docs/guide/_site`, not the repository. GitHub Pages must use
**GitHub Actions** as its source in Settings → Pages.
See [GitHub's workflow documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).

The site address is `https://very-srs.github.io/MANET/`. After a push, check
that the `Documentation` workflow succeeds, then refresh the page. The URL
stays the same as the guide develops. Keep the site out of prominent README
promotion while it is being developed. Commits and pushes follow the
repository's authorization rules; a local edit does not itself publish it.
