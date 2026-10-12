# Maintaining the field guide

Read this when changing the documentation or any behavior it describes. It is
the shared maintenance guide for human contributors and coding agents.

The guide covers the overview, flashing, first boot, device connections,
dashboard, mesh behavior, voice, settings, tools updates, and troubleshooting.
Full existing references are included without manual copying. Further topics
can be added to `content/` and `topics.json` together.

## Where to edit

- `content/*.md`: one topic per file, with `## Marine`, `## User`, and
  `## Developer`, in that order. Keep all three in the same change.
- `topics.json`: topic order, titles, and the explicit list of existing
  reference documents included in the site.
- `assets/`: illustrations, saved image prompts, CSS, and reading-level logic.
- `template.html` and `build.py`: shared presentation and Markdown build.
- `_site/`: generated preview only. Never edit or commit it.

For small wording changes, edit the relevant `content/*.md` file locally and
ask a contributor or coding agent to review, build, and push those edits. Every
topic keeps all three tiers together; change the matching tier, and check the
other two if the meaning changes. Text on full reference pages comes from the
original files listed in `topics.json`, so edit those originals. The **View
Markdown** link at the bottom of a page identifies its source.

Local edits become visible on GitHub Pages only after they are committed,
pushed to `main`, and the documentation deployment succeeds. A documentation
publication does not require a radio package release. If the local branch also
contains unpublished runtime work, publish only the intended documentation
changes from a checkout based on `origin/main` and reconcile the local branch
afterward; do not include unrelated commits just to refresh the site.

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

## Reading levels and developer depth

**Developer** is the explanation a technical person or coding agent would
want to read to understand how the mesh works. Explain the engineering in
connected technical prose. Naming a subsystem or saying that it handles
collisions, recovery, or authentication is insufficient. The implementation
lives in the repository; do not reproduce source code or turn the page into
an inventory of scripts. Use calculations, examples, tables, or diagrams
where they make the mechanism easier to follow.

**User** assumes no computer or networking background. Explain what happens,
why it happens, what the person needs to do, and how to recognize the result.
Define necessary terms and describe visible consequences of automatic
behavior. It needs enough context to understand the system, not just steps.

**Marine** explains the same idea as if to a child, using the established
crayon illustrations and short, concrete captions. Keep the mechanism honest
while simplifying the language. Do not introduce facts that contradict the
other tiers.

In Developer, describe each major mechanism's inputs and source of truth, the
algorithm or state transitions, how information moves between nodes, and
what causes a retry, rejection, or recovery. Include relevant formulas, timing,
tie-breakers, persistence boundaries, and a worked example when they clarify
behavior. Explain why the design makes those choices. Distinguish intended
properties from guarantees the implementation actually provides.

For example, IPv4 allocation needs the block-size/address calculation, how
Alfred claims become an occupied-range snapshot, startup discovery, candidate
selection, and what happens when simultaneous claims or partition merges
produce an overlap. "Distributed allocation with collision handling" does not
answer those questions.

Keep the central explanation on the topic page. Longer mechanisms may live in
a dedicated topic or canonical reference, but link directly to the relevant
section from the paragraph that introduces them. Add focused source or test
links where they help readers investigate further. A broad link to the entire
runtime README at the bottom is not a substitute for the missing explanation.
Do not defer essential detail to an unwritten future page.

Topic headings receive a reading-level prefix in the built site. For a link
to another topic's Developer section, use the generated fragment, such as
`mesh.md#developer-one-connected-component-one-channel-decision`.
Reference headings retain their normal fragments. The build checks both.

Review technical statements against the implementation being documented,
including failure paths. When publication excludes local runtime commits,
avoid presenting those unpublished changes as part of the published source.
Documentation-only edits need build/link checks and appropriate browser
review; run behavior tests when needed to resolve a factual uncertainty.

## Copy and layout

Use plain, factual documentation. Do not add marketing copy, taglines, slogans,
or decorative prose to make the pages sound more exciting. Descriptions should
explain what a page covers; labels should identify controls or destinations.
Keep **Reading level** directly above the three selectors.

Use the existing FER logo from `MANET/share/manet/fer-logo-black.png`, as the
status and management pages do. The builder copies that canonical asset into
the site; do not create a separate documentation logo or maintain a duplicate
source image. Logo changes also trigger the documentation workflow.

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
