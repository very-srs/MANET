/* Reading level is a view preference; every level remains in the static HTML. */
(() => {
  "use strict";
  const tiers = ["marine", "user", "developer"];
  const storageKey = "manet-guide-tier";
  const panels = [...document.querySelectorAll("[data-tier]")];
  const choices = [...document.querySelectorAll("[data-tier-choice]")];
  const contents = document.querySelector(".contents-toggle");

  // Collapse the longer chapter list on phones; no-JS keeps plain links.
  if (contents) {
    contents.hidden = false;
    document.documentElement.classList.add("has-topic-menu");
    contents.addEventListener("click", () => {
      const expanded = contents.getAttribute("aria-expanded") !== "true";
      contents.setAttribute("aria-expanded", String(expanded));
      contents.closest(".sidebar").classList.toggle("expanded", expanded);
    });
  }

  // Private browsing and file:// may deny storage; links still carry the choice.
  function rememberedTier() {
    try { return localStorage.getItem(storageKey); } catch (_) { return null; }
  }

  // An explicit URL always wins over a remembered preference.
  function requestedTier() {
    const requested = new URL(location.href).searchParams.get("tier");
    if (requested !== null) return tiers.includes(requested) ? requested : "user";
    const saved = rememberedTier();
    return tiers.includes(saved) ? saved : "user";
  }

  // Keep links shareable and make history navigation restore the visible level.
  function showTier(tier, push = false) {
    document.documentElement.dataset.readingTier = tier;
    panels.forEach(panel => { panel.hidden = panel.dataset.tier !== tier; });
    choices.forEach(choice => {
      const selected = choice.dataset.tierChoice === tier;
      if (selected) choice.setAttribute("aria-current", "true");
      else choice.removeAttribute("aria-current");
    });
    const url = new URL(location.href);
    if (panels.length) {
      url.searchParams.set("tier", tier);
      let fragment = url.hash.slice(1);
      try { fragment = decodeURIComponent(fragment); } catch (_) { /* Ignore malformed escapes. */ }
      const anchor = document.getElementById(fragment);
      if (anchor?.closest("[data-tier]")?.hidden) url.hash = "";
      try {
        history[push ? "pushState" : "replaceState"]({}, "", url);
      } catch (_) { /* Some local-file contexts disallow history changes. */ }
    }
    document.querySelectorAll("a[data-topic], a[data-guide-link]").forEach(link => {
      const target = new URL(link.getAttribute("href"), location.href);
      target.searchParams.set("tier", tier);
      link.href = target.href;
    });
    try { localStorage.setItem(storageKey, tier); } catch (_) { /* Preference is optional. */ }
    const announcement = document.getElementById("tier-announcement");
    if (push && announcement) announcement.textContent = `${tier[0].toUpperCase()}${tier.slice(1)} reading level`;
  }

  choices.forEach(choice => choice.addEventListener("click", event => {
    // Preserve opening a specific level in a new tab/window.
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    if (choice.dataset.tierChoice !== document.documentElement.dataset.readingTier) {
      showTier(choice.dataset.tierChoice, true);
    }
  }));
  window.addEventListener("popstate", () => showTier(requestedTier()));
  showTier(requestedTier());
})();
