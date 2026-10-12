#!/usr/bin/env python3
"""Build the three reading levels and an explicit set of existing references."""

import argparse
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
from string import Template
import tempfile
from urllib.parse import quote, unquote, urlsplit

try:
    import markdown
except ImportError:
    raise SystemExit("Install the build dependency: python3 -m pip install -r docs/guide/requirements.txt")


GUIDE = Path(__file__).resolve().parent
ROOT = GUIDE.parents[1]
TIERS = ("marine", "user", "developer")
REPOSITORY = "https://github.com/very-srs/MANET"


def slugify(value, separator):
    """Match the heading fragments used by the repository's Markdown links."""
    return re.sub(r"[^\w\- ]", "", value.lower()).replace(" ", separator)


def relative(target, output):
    """Make asset and document links work under a project URL or file://."""
    return Path(os.path.relpath(target, output.parent)).as_posix()


class RewriteLinks(HTMLParser):
    """Resolve Markdown links against their source file before relocating HTML."""

    def __init__(self, source, output, destinations, images):
        """Remember the source/output locations and the explicit publication map."""
        super().__init__(convert_charrefs=False)
        self.source, self.output = source, output
        self.destinations, self.images = destinations, images
        self.parts = []

    def relocate(self, value, image=False):
        """Keep published documentation local; source-code links lead to GitHub."""
        link = urlsplit(value)
        if link.scheme or link.netloc or not link.path:
            return value
        source = (self.source.parent / unquote(link.path)).resolve()
        if not source.is_relative_to(ROOT) or not source.exists():
            raise ValueError(f"{self.source.relative_to(ROOT)}: missing local link {value}")
        if source in self.destinations:
            url = relative(self.destinations[source], self.output)
        elif image:
            if source.is_relative_to(GUIDE / "assets"):
                target = Path("assets") / source.relative_to(GUIDE / "assets")
            elif source.is_relative_to(ROOT / "docs/images"):
                target = Path("assets/reference") / source.relative_to(ROOT / "docs/images")
            else:
                raise ValueError(f"Image is outside the documentation asset directories: {source}")
            self.images[target] = source
            url = relative(target, self.output)
        else:
            kind = "tree" if source.is_dir() else "blob"
            url = f"{REPOSITORY}/{kind}/main/{quote(source.relative_to(ROOT).as_posix())}"
        if link.query:
            url += "?" + link.query
        if link.fragment:
            url += "#" + link.fragment
        return url

    def handle_starttag(self, tag, attrs):
        """Rewrite only URL attributes, preserving the Markdown renderer's HTML."""
        rewritten = []
        local_document = False
        for key, value in attrs:
            if value is not None and key in ("href", "src"):
                value = self.relocate(value, image=(tag == "img" and key == "src"))
                link = urlsplit(value)
                local_document = tag == "a" and not link.scheme and link.path.endswith(".html")
            if key == "alt" and tag == "img" and not value:
                raise ValueError(f"{self.source}: illustration needs descriptive alt text")
            rewritten.append(key if value is None else f'{key}="{html.escape(value, quote=True)}"')
        if local_document:
            rewritten.append("data-guide-link")
        if tag == "img":
            rewritten.append('loading="lazy"')
        self.parts.append("<" + tag + (" " + " ".join(rewritten) if rewritten else "") + ">")

    def handle_startendtag(self, tag, attrs):
        """Serialize void elements using the same link handling as other elements."""
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        """Retain closing tags from trusted repository Markdown."""
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        """Retain ordinary text and code blocks."""
        self.parts.append(data)

    def handle_entityref(self, name):
        """Preserve escaped characters rather than interpreting them as HTML."""
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        """Preserve numeric character references."""
        self.parts.append(f"&#{name};")


def render(text, source, output, destinations, images, prefix=""):
    """Render Markdown and give each tier its own non-colliding heading IDs."""
    body = markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists", "toc"],
                             extension_configs={"toc": {"slugify": slugify}})
    if prefix:
        body = re.sub(r'id="([^"]+)"', lambda m: f'id="{prefix}{m[1]}"', body)
        body = re.sub(r'href="#([^"]+)"', lambda m: f'href="#{prefix}{m[1]}"', body)
    rewriter = RewriteLinks(source, output, destinations, images)
    rewriter.feed(body)
    return "".join(rewriter.parts)


def read_tiers(source):
    """Require all three levels together so a topic cannot silently lose a tier."""
    parts = re.split(r"^## (Marine|User|Developer)\s*$", source.read_text(), flags=re.M)
    if [value.lower() for value in parts[1::2]] != list(TIERS):
        raise ValueError(f"{source.name}: expected ## Marine, ## User, ## Developer, in that order")
    tiers = dict(zip(TIERS, parts[2::2]))
    if any(not text.strip() for text in tiers.values()):
        raise ValueError(f"{source.name}: every tier needs content")
    if "![" not in tiers["marine"]:
        raise ValueError(f"{source.name}: Marine needs an illustration with alt text")
    return tiers


class PageLinks(HTMLParser):
    """Collect generated HTML destinations for offline link validation."""

    def __init__(self):
        """Initialize a page's fragment and link indexes."""
        super().__init__()
        self.ids, self.links = set(), []

    def handle_starttag(self, tag, attrs):
        """Read fragment IDs and document/asset links."""
        attrs = dict(attrs)
        if "id" in attrs:
            if attrs["id"] in self.ids:
                raise ValueError(f"Duplicate HTML id: {attrs['id']}")
            self.ids.add(attrs["id"])
        for name in ("href", "src"):
            if attrs.get(name):
                self.links.append(attrs[name])


def check_site(directory):
    """Fail on missing local pages/assets and broken generated heading links."""
    pages = {}
    for path in directory.rglob("*.html"):
        parser = PageLinks()
        parser.feed(path.read_text())
        pages[path.resolve()] = parser
    for path, parser in pages.items():
        for value in parser.links:
            link = urlsplit(value)
            if link.scheme or link.netloc:
                continue
            target = (path.parent / unquote(link.path)).resolve() if link.path else path
            if not target.is_file():
                raise ValueError(f"{path.name}: missing built target {value}")
            if link.fragment and target in pages and unquote(link.fragment) not in pages[target].ids:
                raise ValueError(f"{path.name}: missing built heading {value}")


def build(destination):
    """Assemble only listed documents and assets into a disposable site directory."""
    manifest = json.loads((GUIDE / "topics.json").read_text())
    topics, references = manifest["topics"], manifest["references"]
    destinations = {(GUIDE / "content" / topic["source"]).resolve(): Path(topic["id"] + ".html") for topic in topics}
    destinations.update({(ROOT / ref["path"]).resolve(): Path("reference") / (ref["id"] + ".html") for ref in references})
    template = Template((GUIDE / "template.html").read_text())
    images = {}
    destination.mkdir(parents=True, exist_ok=True)

    def page(output, title, description, body, source, current="", reference=False, following=None):
        """Wrap a topic or long-form reference in the shared navigation."""
        nav, group = [], None
        for index, topic in enumerate(topics, 1):
            if group != topic["group"]:
                group = topic["group"]
                nav.append(f'<li class="nav-group">{html.escape(group)}</li>')
            selected = ' aria-current="page"' if topic["id"] == current else ""
            nav.append(f'<li><a data-topic href="{relative(Path(topic["id"] + ".html"), output)}"{selected}><span>{index:02}</span>{html.escape(topic["title"])}</a></li>')
        tier_links = "" if reference else '<div class="tier-switch" aria-label="Reading level">' + "".join(
            f'<a href="?tier={tier}" data-tier-choice="{tier}" aria-controls="tier-{tier}">{label}</a>'
            for tier, label in (("marine", "Marine"), ("user", "User"), ("developer", "Developer"))) + '</div>'
        next_link = ""
        if following:
            next_link = f'<a class="next-topic" data-topic href="{relative(Path(following["id"] + ".html"), output)}"><span>Up next</span><strong>{html.escape(following["title"])} <span aria-hidden="true">↗</span></strong></a>'
        document = template.substitute(
            title=html.escape(title), description=html.escape(description),
            stylesheet=relative(Path("assets/style.css"), output), script=relative(Path("assets/site.js"), output),
            home=relative(Path("index.html"), output), nav="".join(nav), tiers=tier_links,
            body=body, next_link=next_link, page_kind="reference" if reference else "topic",
            eyebrow="Technical reference" if reference else "MANET field guide",
            source=f"{REPOSITORY}/blob/main/{source.relative_to(ROOT).as_posix()}",
            source_label="View Markdown", repository=REPOSITORY)
        target = destination / output
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document)

    for index, topic in enumerate(topics):
        source = (GUIDE / "content" / topic["source"]).resolve()
        output = destinations[source]
        tiers = read_tiers(source)
        body = "".join(f'<section id="tier-{tier}" class="tier-content" data-tier="{tier}" aria-label="{tier.title()} reading level"><h2 class="tier-fallback">{tier.title()} view</h2>'
                       + render(tiers[tier], source, output, destinations, images, tier + "-") + '</section>' for tier in TIERS)
        following = topics[index + 1] if index + 1 < len(topics) else None
        page(output, topic["title"], topic["description"], body, source, topic["id"], following=following)
    for ref in references:
        source = (ROOT / ref["path"]).resolve()
        # The template owns h1; keep the original document's remaining headings.
        text = re.sub(r"\A# [^\n]+\n", "", source.read_text(), count=1)
        body = '<article class="reference-content">' + render(text, source, destinations[source], destinations, images) + '</article>'
        page(destinations[source], ref["title"], "The complete repository reference, available here for offline reading.", body, source, reference=True)
    for target, source in images.items():
        (destination / target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination / target)
    for name in ("style.css", "site.js"):
        (destination / "assets").mkdir(exist_ok=True)
        shutil.copyfile(GUIDE / "assets" / name, destination / "assets" / name)
    for name in ("ComicNeue-Regular.ttf", "ComicNeue-Bold.ttf", "OFL.txt"):
        (destination / "assets/fonts").mkdir(exist_ok=True)
        shutil.copyfile(GUIDE / "assets/fonts" / name, destination / "assets/fonts" / name)
    (destination / ".nojekyll").touch()
    check_site(destination)


def main():
    """Build a clean preview, or check in a temporary directory without outputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate a temporary build without changing the preview")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="manet-docs-") as temporary:
        staging = Path(temporary)
        build(staging)
        if not args.check:
            destination = GUIDE / "_site"
            # This fixed directory contains build outputs only, never source files.
            if destination.is_symlink():
                raise ValueError("Refusing to replace a symlink at _site")
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(staging, destination)
            print(f"Preview: {destination / 'index.html'}")
        print("Documentation build and local links verified.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error))
